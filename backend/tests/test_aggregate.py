"""Unit tests for per-syscall aggregation (pure, no I/O)."""
from __future__ import annotations

from app.aggregate import syscall_stats


def ev(syscall, latency_ms=None, error=None, event_type="syscall"):
    return {
        "syscall": syscall, "latency_ms": latency_ms, "error": error,
        "event_type": event_type,
    }


def test_counts_latency_and_errors():
    events = [
        ev("openat", 1.0), ev("openat", 3.0, error="ENOENT"), ev("openat", 2.0),
        ev("read", 10.0), ev("read", 30.0),
        ev("write"),  # no latency recorded
        ev("SIGCHLD", event_type="signal"),  # ignored (not a syscall)
        ev("exit", event_type="exit"),        # ignored
    ]
    rows = syscall_stats(events)
    by = {r["syscall"]: r for r in rows}

    assert set(by) == {"openat", "read", "write"}
    assert by["openat"]["count"] == 3
    assert by["openat"]["errors"] == 1
    assert by["openat"]["total_ms"] == 6.0
    assert by["openat"]["avg_ms"] == 2.0
    assert by["read"]["total_ms"] == 40.0
    assert by["write"]["count"] == 1 and by["write"]["avg_ms"] is None


def test_sorted_by_total_latency_desc():
    rows = syscall_stats([
        ev("a", 1.0), ev("b", 100.0), ev("c", 50.0),
    ])
    assert [r["syscall"] for r in rows] == ["b", "c", "a"]


def test_percentiles_and_pct_runtime():
    # 100 read calls of latency 1..100ms
    events = [ev("read", float(i)) for i in range(1, 101)]
    events += [ev("write", 100.0)]  # total runtime denominator includes this
    rows = syscall_stats(events)
    read = next(r for r in rows if r["syscall"] == "read")
    assert read["p50_ms"] == 50.5
    assert read["p95_ms"] == 95.05
    assert read["p99_ms"] == 99.01
    # read total = 5050ms, write = 100ms -> read ~98.06% of in-syscall time
    assert 97.0 < read["pct_runtime"] < 99.0


def test_empty_stream():
    assert syscall_stats([]) == []
