"""Deterministic tests for each anomaly rule using synthetic events/metrics."""
from __future__ import annotations

from app.rules.engine import RuleContext, run_rules
from app.trace.events import SYSCALL, TraceEvent


def ev(ts, syscall, *, error=None, path=None, retval="0", latency_ms=None, fd=None):
    return TraceEvent(
        timestamp_ms=ts, pid=1, event_type=SYSCALL, syscall=syscall,
        error=error, path=path, retval=retval, latency_ms=latency_ms, fd=fd,
    )


def metric(ts, **kw):
    base = dict(
        timestamp_ms=ts, cpu_pct=None, rss_mb=None, vms_mb=None,
        open_fds=None, threads=None, syscall_rate=None,
        io_read_bps=None, io_write_bps=None,
    )
    base.update(kw)
    return base


def fired(anomalies, rule_id):
    return next((a for a in anomalies if a.rule_id == rule_id), None)


def test_repeated_open_same_file_fires():
    events = [ev(i * 10, "openat", path="/app/data.txt", retval="3") for i in range(12)]
    out = run_rules(RuleContext(events=events))
    a = fired(out, "repeated_open_same_file")
    assert a is not None and a.severity == "high"
    assert a.occurrence_count == 12
    assert len(a.evidence_ids) == 0  # filled by orchestrator, not the rule
    assert len(a.evidence) > 0


def test_library_opens_do_not_trigger_repeated():
    events = [ev(i, "openat", path="/usr/lib/libc.so.6", retval="3") for i in range(30)]
    out = run_rules(RuleContext(events=events))
    assert fired(out, "repeated_open_same_file") is None


def test_failed_file_opens_excludes_linker_probing():
    lib_fails = [ev(i, "openat", path="/lib/x86_64/libz.so", error="ENOENT", retval="-1") for i in range(20)]
    out = run_rules(RuleContext(events=lib_fails))
    assert fired(out, "failed_file_opens") is None

    app_fails = [ev(i, "openat", path=f"/app/cfg{i}.yaml", error="ENOENT", retval="-1") for i in range(6)]
    out2 = run_rules(RuleContext(events=app_fails))
    a = fired(out2, "failed_file_opens")
    assert a is not None and a.severity == "medium"


def test_failed_file_opens_ignores_interpreter_probing():
    # Python/Node import search misses + system descriptor probes are noise.
    probes = (
        [ev(i, "stat", path=f"/usr/lib/python3.11/foo{i}.py", error="ENOENT", retval="-1") for i in range(10)]
        + [ev(i, "openat", path=f"/proj/__pycache__/m{i}.pyc", error="ENOENT", retval="-1") for i in range(10)]
        + [ev(i, "openat", path=f"/proj/node_modules/x{i}", error="ENOENT", retval="-1") for i in range(10)]
        + [ev(i, "access", path="/etc/os-release", error="ENOENT", retval="-1") for i in range(10)]
    )
    assert fired(run_rules(RuleContext(events=probes)), "failed_file_opens") is None


def test_slow_syscall_ignores_blocking_calls():
    events = [
        ev(1, "epoll_wait", latency_ms=5000),   # blocking — ignored
        ev(2, "poll", latency_ms=3000),          # blocking — ignored
        ev(3, "openat", path="/data", latency_ms=1500, retval="3"),  # slow!
    ]
    out = run_rules(RuleContext(events=events))
    a = fired(out, "slow_syscall")
    assert a is not None and a.severity == "high"
    assert a.occurrence_count == 1
    assert "openat" in a.title


def test_monotonic_memory_growth():
    metrics = [metric(i * 1000, rss_mb=50 + i * 20) for i in range(10)]  # 50 -> 230
    out = run_rules(RuleContext(events=[], metrics=metrics))
    a = fired(out, "monotonic_memory_growth")
    assert a is not None and a.severity == "high"


def test_stable_memory_does_not_fire():
    metrics = [metric(i * 1000, rss_mb=100 + (i % 2)) for i in range(10)]
    out = run_rules(RuleContext(events=[], metrics=metrics))
    assert fired(out, "monotonic_memory_growth") is None


def test_fd_count_growing_is_critical():
    metrics = [metric(i * 1000, open_fds=10 + i * 8) for i in range(10)]  # 10 -> 82
    out = run_rules(RuleContext(events=[], metrics=metrics))
    a = fired(out, "fd_count_growing")
    assert a is not None and a.severity == "critical"


def test_cpu_bound_no_syscalls():
    cores = 4
    metrics = [metric(i * 250, cpu_pct=cores * 95, syscall_rate=5) for i in range(10)]
    out = run_rules(RuleContext(events=[], metrics=metrics, cpu_cores=cores))
    a = fired(out, "cpu_bound_no_syscalls")
    assert a is not None and a.severity == "medium"


def test_clean_run_has_no_anomalies():
    events = [ev(i, "read", fd=3, retval="100") for i in range(5)]
    metrics = [metric(i * 250, cpu_pct=10, rss_mb=50, open_fds=8, syscall_rate=20) for i in range(10)]
    out = run_rules(RuleContext(events=events, metrics=metrics, cpu_cores=4))
    assert out == []
