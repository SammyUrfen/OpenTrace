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


def test_infinite_loop_silent_on_eventless_runs():
    """No syscall collector (attach runs, strace off, sliding monitor scans)
    means events=[] — the rule must not treat missing data as being stuck."""
    metrics = [metric(i * 250, cpu_pct=50.0) for i in range(200)]
    out = run_rules(RuleContext(events=[], metrics=metrics, duration_ms=60_000))
    assert fired(out, "infinite_loop_no_progress") is None


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


def test_connect_peer_parsing_variants():
    from app.rules.engine import parse_connect_peer
    assert parse_connect_peer(
        '3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("10.0.0.5")}, 16'
    ) == "10.0.0.5:443"
    assert parse_connect_peer('3, {sa_family=AF_INET, sin_addr=inet_addr("1.2.3.4")}, 16') == "1.2.3.4"
    assert parse_connect_peer(
        '7, {sa_family=AF_INET6, sin6_port=htons(6379), sin6_addr=inet_pton(AF_INET6, "::1")}, 28'
    ) == "[::1]:6379"
    assert parse_connect_peer(
        '9, {sa_family=AF_UNIX, sun_path="/var/run/postgresql/.s.PGSQL.5432"}, 110'
    ) == "unix:/var/run/postgresql/.s.PGSQL.5432"
    assert parse_connect_peer('') is None


def test_no_connection_reuse_unix_socket():
    args = '7, {sa_family=AF_UNIX, sun_path="/tmp/redis.sock"}, 110'
    events = [ev(i, "connect", error=None, retval="0", args=args) for i in range(6)]
    a = fired(run_rules(RuleContext(events=events)), "no_connection_reuse")
    assert a is not None and "unix:/tmp/redis.sock" in a.title


def test_no_connection_reuse_ipv6():
    args = ('7, {sa_family=AF_INET6, sin6_port=htons(6379), '
            'sin6_addr=inet_pton(AF_INET6, "::1"), sin6_scope_id=0}, 28')
    events = [ev(i, "connect", error="EINPROGRESS", retval="-1", args=args) for i in range(6)]
    a = fired(run_rules(RuleContext(events=events)), "no_connection_reuse")
    assert a is not None and "[::1]:6379" in a.title


def test_slow_downstream_peer():
    conn = '7, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.9")}, 16'
    events = [ev(0, "connect", error="EINPROGRESS", retval="-1", args=conn)]
    # blocking reads on that same fd dominate the wait (5 x 300ms = 1.5s)
    events += [ev(10 + i, "recvfrom", retval="512", fd=7, latency_ms=300.0) for i in range(5)]
    a = fired(run_rules(RuleContext(events=events)), "slow_downstream_peer")
    assert a is not None and a.severity == "high" and "10.0.0.9:5432" in a.title


def test_slow_downstream_peer_silent_on_fast_reads():
    conn = '7, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.9")}, 16'
    events = [ev(0, "connect", error="EINPROGRESS", retval="-1", args=conn)]
    events += [ev(10 + i, "recvfrom", retval="512", fd=7, latency_ms=5.0) for i in range(30)]
    assert fired(run_rules(RuleContext(events=events)), "slow_downstream_peer") is None


def test_slow_downstream_peer_ignores_closed_fd_reuse():
    # fd 7 connects to peer A, closes, then a plain-file read on reused fd 7 is
    # slow — must NOT be attributed to peer A.
    conn = '7, {sa_family=AF_INET, sin_port=htons(5432), sin_addr=inet_addr("10.0.0.9")}, 16'
    events = [
        ev(0, "connect", error="EINPROGRESS", retval="-1", args=conn),
        ev(1, "close", retval="0", fd=7, args="7"),
    ]
    events += [ev(10 + i, "read", retval="512", fd=7, latency_ms=400.0) for i in range(5)]
    assert fired(run_rules(RuleContext(events=events)), "slow_downstream_peer") is None


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


# --- cgroup-limit-aware rules (R7) ------------------------------------------

def test_cpu_throttled_fires_at_quota_ceiling():
    # 0.5-core quota → ceiling ~45% cpu_pct; sustained 50% is throttled even though
    # it never approaches the 90%-of-a-core gate.
    metrics = [metric(i * 250, cpu_pct=50.0) for i in range(12)]
    a = fired(run_rules(RuleContext(
        events=[], metrics=metrics, cgroup_cpu_quota_cores=0.5)), "cpu_throttled")
    assert a is not None and a.severity == "high"
    # the plain metric CPU rule stays silent — 50% is nowhere near a full core
    assert fired(run_rules(RuleContext(
        events=[], metrics=metrics, cgroup_cpu_quota_cores=0.5)), "cpu_bound_metric") is None


def test_cpu_throttled_silent_without_quota():
    # bare-metal target (no quota) → the scaled gate must not fire
    metrics = [metric(i * 250, cpu_pct=50.0) for i in range(12)]
    assert fired(run_rules(RuleContext(events=[], metrics=metrics)), "cpu_throttled") is None


def test_rss_near_cgroup_limit_fires():
    limit = 100 * 1024 * 1024  # 100MB
    metrics = [metric(i * 250, rss_mb=v) for i, v in enumerate([50, 70, 95])]
    a = fired(run_rules(RuleContext(
        events=[], metrics=metrics, cgroup_mem_limit_bytes=limit)), "rss_near_cgroup_limit")
    assert a is not None and a.severity == "critical"


def test_rss_near_cgroup_limit_silent_when_below():
    limit = 100 * 1024 * 1024
    metrics = [metric(i * 250, rss_mb=v) for i, v in enumerate([50, 55, 60])]
    assert fired(run_rules(RuleContext(
        events=[], metrics=metrics, cgroup_mem_limit_bytes=limit)), "rss_near_cgroup_limit") is None
    # and no limit at all → never fires
    metrics_hi = [metric(i * 250, rss_mb=99000) for i in range(5)]
    assert fired(run_rules(RuleContext(events=[], metrics=metrics_hi)),
                 "rss_near_cgroup_limit") is None