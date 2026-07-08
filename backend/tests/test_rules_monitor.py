"""Orchestrator-shaped rule tests — the eventless metric-window contexts that
attach & live-monitor runs actually produce (the shape that hid R1).

Where the other rule tests feed hand-picked events, these feed the *whole run
shape*: a ~90s window of metric samples with NO syscall events (events=[]),
exactly as `_eval_sliding_rules` builds it. Covers the metric-only rules, the
signal gate that keeps events-rules from false-firing on absence, the incident →
anomaly collapse, and the R6 duration gating.
"""
from __future__ import annotations

from app.rules.engine import RuleContext, RuleThresholds, run_rules
from app.trace.orchestrator import _incidents_to_anomalies, _SEV_RANK


def metric(ts, **kw):
    base = dict(timestamp_ms=ts, cpu_pct=None, rss_mb=None, vms_mb=None,
                open_fds=None, threads=None, syscall_rate=None,
                io_read_bps=None, io_write_bps=None)
    base.update(kw)
    return base


def fired(out, rule_id):
    return next((a for a in out if a.rule_id == rule_id), None)


def _window(n, *, cpu_pct, io_read=0.0, io_write=0.0, rss=None, start=0, step=250):
    """A monitor-shaped eventless window: n metric samples, no syscall events."""
    return [
        metric(start + i * step, cpu_pct=cpu_pct, io_read_bps=io_read,
               io_write_bps=io_write, rss_mb=rss)
        for i in range(n)
    ]


# --- (1) a healthy eventless ~90s window must produce ZERO anomalies ---------

def test_healthy_monitor_window_is_silent():
    # Moderate CPU, light I/O, flat memory — a normal idle-ish service. None of
    # the eventless rules (cpu_bound_metric / io_wait_metric / trends) may fire,
    # and no events-rule may false-fire on the empty event stream (R1 regression).
    window = _window(360, cpu_pct=30.0, io_read=8_192.0, io_write=4_096.0, rss=120.0)
    ctx = RuleContext(events=[], metrics=window, duration_ms=90_000)
    assert run_rules(ctx) == []


def test_eventless_window_never_fires_infinite_loop():
    # 90s+ of samples with events=[] is "no syscall collector", not "stuck".
    window = _window(400, cpu_pct=55.0)
    ctx = RuleContext(events=[], metrics=window, duration_ms=120_000)
    assert fired(run_rules(ctx), "infinite_loop_no_progress") is None


# --- (2) the metric-only rules fire on the right shapes ----------------------

def test_cpu_bound_metric_fires_on_spinning_window():
    window = _window(40, cpu_pct=140.0, io_read=0.0, io_write=0.0)
    a = fired(run_rules(RuleContext(events=[], metrics=window)), "cpu_bound_metric")
    assert a is not None and a.severity == "medium"


def test_cpu_bound_metric_ignored_when_io_present():
    # High CPU but also heavy I/O -> not "spinning", must not fire.
    window = _window(40, cpu_pct=140.0, io_read=5_000_000.0)
    assert fired(run_rules(RuleContext(events=[], metrics=window)), "cpu_bound_metric") is None


def test_cpu_bound_metric_defers_to_strace_rule_on_launch():
    # syscall_rate present => strace-backed launch run; the metric variant defers
    # to cpu_bound_no_syscalls so the two don't double-report.
    window = [metric(i * 250, cpu_pct=140.0, io_read_bps=0.0, io_write_bps=0.0,
                     syscall_rate=2.0) for i in range(40)]
    out = run_rules(RuleContext(events=[], metrics=window))
    assert fired(out, "cpu_bound_metric") is None
    assert fired(out, "cpu_bound_no_syscalls") is not None


def test_io_wait_metric_fires_on_io_bound_window():
    window = _window(40, cpu_pct=8.0, io_read=4_000_000.0, io_write=0.0)
    a = fired(run_rules(RuleContext(events=[], metrics=window)), "io_wait_metric")
    assert a is not None and a.severity == "medium"


def test_io_wait_metric_silent_when_cpu_high():
    # Busy CPU with I/O is not "waiting"; io_wait must stay quiet.
    window = _window(40, cpu_pct=140.0, io_read=4_000_000.0)
    assert fired(run_rules(RuleContext(events=[], metrics=window)), "io_wait_metric") is None


# --- (3) incident -> anomaly collapse: a critical beats a later low duplicate -

def test_incidents_to_anomalies_keeps_highest_severity_per_rule():
    incidents = [
        {"rule_id": "fd_leak_live", "severity": "critical", "title": "fd blow-up",
         "count": 3, "ts": 1000, "hot": {"stack": ["app", "leak"]}},
        {"rule_id": "fd_leak_live", "severity": "low", "title": "fd blip",
         "count": 1, "ts": 5000, "hot": None},
    ]
    out = _incidents_to_anomalies(incidents)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].severity_score == 0.4 + 0.1 * _SEV_RANK["critical"]
    # occurrence count from the surviving (critical) incident
    assert "×3" in out[0].title


def test_incidents_to_anomalies_one_row_per_rule():
    incidents = [
        {"rule_id": "cpu_bound_metric", "severity": "medium", "title": "cpu", "count": 2},
        {"rule_id": "io_wait_metric", "severity": "medium", "title": "io", "count": 1},
        {"rule_id": "cpu_bound_metric", "severity": "high", "title": "cpu2", "count": 1},
    ]
    out = _incidents_to_anomalies(incidents)
    by_rule = {a.rule_id: a for a in out}
    assert set(by_rule) == {"cpu_bound_metric", "io_wait_metric"}
    assert by_rule["cpu_bound_metric"].severity == "high"


# --- (4) R6 duration-gating + threshold overrides ----------------------------

def test_trend_uses_full_history_on_short_run():
    # A short launch run: whole-history growth 50->230 MB fires.
    metrics = [metric(i * 1000, rss_mb=50 + i * 20) for i in range(10)]
    ctx = RuleContext(events=[], metrics=metrics, duration_ms=10_000)
    assert fired(run_rules(ctx), "monotonic_memory_growth") is not None


def test_trend_gates_warmup_on_long_run():
    # A long-lived server: RSS climbs 100->300 during warmup (first 20 samples)
    # then holds flat for the rest of a very long run. Judged over the trailing
    # window it's FLAT, so the leak rule must NOT fire on warmup.
    warmup = [metric(i * 1000, rss_mb=100 + i * 10) for i in range(20)]   # 100->290
    flat = [metric((20 + i) * 1000, rss_mb=300.0) for i in range(400)]     # steady
    ctx = RuleContext(events=[], metrics=warmup + flat, duration_ms=3_600_000)
    assert fired(run_rules(ctx), "monotonic_memory_growth") is None


def test_trend_still_catches_tail_leak_on_long_run():
    # Same long run, but the tail keeps climbing -> a real ongoing leak fires
    # even under the sliding-window (trailing) treatment.
    warmup = [metric(i * 1000, rss_mb=100.0) for i in range(20)]
    leak = [metric((20 + i) * 1000, rss_mb=100 + i * 2) for i in range(400)]  # climbs
    ctx = RuleContext(events=[], metrics=warmup + leak, duration_ms=3_600_000)
    assert fired(run_rules(ctx), "monotonic_memory_growth") is not None


def test_threshold_override_changes_firing():
    # 6 opens: below the default (>10) so silent, but a lowered override fires.
    from app.trace.events import SYSCALL, TraceEvent

    def ev(ts):
        return TraceEvent(timestamp_ms=ts, pid=1, event_type=SYSCALL,
                          syscall="openat", path="/app/x.txt", retval="3")

    events = [ev(i) for i in range(6)]
    assert fired(run_rules(RuleContext(events=events)), "repeated_open_same_file") is None
    thr = RuleThresholds.from_overrides({"repeated_open_min_opens": 3})
    out = run_rules(RuleContext(events=events, thresholds=thr))
    assert fired(out, "repeated_open_same_file") is not None


def test_from_overrides_ignores_unknown_keys():
    thr = RuleThresholds.from_overrides({"bogus_key": 1, "slow_syscall_ms": 500.0})
    assert not hasattr(thr, "bogus_key")
    assert thr.slow_syscall_ms == 500.0


# --- signal gating: events-rules skipped when events absent ------------------

def test_events_rules_skipped_on_metric_only_context():
    # A pure metric window must invoke no events-rule at all (they'd all no-op,
    # but the gate makes it explicit and cheap). Sanity: only metric rules seen.
    window = _window(40, cpu_pct=140.0, io_read=0.0)
    out = run_rules(RuleContext(events=[], metrics=window))
    assert all(a.rule_id in {"cpu_bound_metric", "io_wait_metric",
                             "monotonic_memory_growth", "fd_count_growing",
                             "memory_spike", "cpu_bound_no_syscalls"}
               for a in out)
