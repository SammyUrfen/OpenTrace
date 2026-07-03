"""Tests for the expanded §5 rule set (synthetic events/metrics)."""
from __future__ import annotations

from app.rules.engine import RuleContext, run_rules
from app.trace.events import EXIT, SYSCALL, TraceEvent


def ev(ts, syscall, *, error=None, retval="0", latency_ms=None, fd=None, args="", event_type=SYSCALL):
    return TraceEvent(
        timestamp_ms=ts, pid=1, event_type=event_type, syscall=syscall,
        error=error, retval=retval, latency_ms=latency_ms, fd=fd, args=args,
    )


def metric(ts, **kw):
    base = dict(timestamp_ms=ts, cpu_pct=None, rss_mb=None, vms_mb=None,
                open_fds=None, threads=None, syscall_rate=None,
                io_read_bps=None, io_write_bps=None)
    base.update(kw)
    return base


def fired(out, rule_id):
    return next((a for a in out if a.rule_id == rule_id), None)


def test_infinite_loop_no_progress():
    events = [ev(i, "getpid") for i in range(10)]  # ~no syscalls
    out = run_rules(RuleContext(events=events, duration_ms=35_000))
    a = fired(out, "infinite_loop_no_progress")
    assert a is not None and a.severity == "critical"


def test_memory_spike():
    metrics = [metric(0, rss_mb=50), metric(250, rss_mb=60), metric(500, rss_mb=220)]
    a = fired(run_rules(RuleContext(events=[], metrics=metrics)), "memory_spike")
    assert a is not None and a.severity == "medium"


def test_slow_file_io():
    events = [ev(1, "write", latency_ms=250.0, retval="100", fd=5)]
    a = fired(run_rules(RuleContext(events=events)), "slow_file_io")
    assert a is not None and a.severity == "high"


def test_excessive_subprocess():
    events = [ev(i, "execve", retval="0") for i in range(51)]
    a = fired(run_rules(RuleContext(events=events)), "excessive_subprocess")
    assert a is not None and a.occurrence_count == 51


def test_connection_error():
    events = [ev(1, "connect", error="ECONNREFUSED", retval="-1")]
    a = fired(run_rules(RuleContext(events=events)), "connection_error")
    assert a is not None and "ECONNREFUSED" in a.title


def test_no_connection_reuse():
    args = '3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("10.0.0.5")}, 16'
    events = [ev(i, "connect", error="EINPROGRESS", retval="-1", args=args) for i in range(6)]
    a = fired(run_rules(RuleContext(events=events)), "no_connection_reuse")
    assert a is not None and "10.0.0.5" in a.title


def test_mutex_contention():
    events = [ev(i, "futex", latency_ms=15.0) for i in range(21)]
    a = fired(run_rules(RuleContext(events=events)), "mutex_contention")
    assert a is not None and a.severity == "high"


def test_io_retry_loop():
    events = [ev(i, "read", error="EAGAIN", retval="-1", fd=7) for i in range(101)]
    a = fired(run_rules(RuleContext(events=events)), "io_retry_loop")
    assert a is not None and a.occurrence_count == 101


def test_small_read_storm():
    events = [ev(i, "read", retval="16", fd=4) for i in range(2001)]
    a = fired(run_rules(RuleContext(events=events)), "small_read_storm")
    assert a is not None


def test_write_storm():
    events = [ev(i, "write", retval="20", fd=5) for i in range(1001)]
    a = fired(run_rules(RuleContext(events=events)), "write_storm")
    assert a is not None
    # writes to stdout/stderr (fd 1/2) are NOT a storm
    out2 = run_rules(RuleContext(events=[ev(i, "write", retval="20", fd=1) for i in range(1001)]))
    assert fired(out2, "write_storm") is None


def test_spin_loop():
    events = [ev(i, "epoll_wait", latency_ms=0.05, retval="0") for i in range(2001)]
    metrics = [metric(i * 250, cpu_pct=95) for i in range(5)]
    a = fired(run_rules(RuleContext(events=events, metrics=metrics)), "spin_loop")
    assert a is not None and a.severity == "high"
    # without high CPU it should not fire
    assert fired(run_rules(RuleContext(events=events, metrics=[metric(0, cpu_pct=5)])), "spin_loop") is None


def test_clean_run_no_new_false_positives():
    events = [ev(i, "read", retval="4096", fd=3) for i in range(50)]
    metrics = [metric(i * 250, cpu_pct=10, rss_mb=50, open_fds=8) for i in range(10)]
    out = run_rules(RuleContext(events=events, metrics=metrics, duration_ms=2000))
    assert out == []