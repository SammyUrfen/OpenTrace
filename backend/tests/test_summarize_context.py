"""Tests for the enriched, token-budgeted AI-summary context (app.summarize)."""
from __future__ import annotations

from app import summarize


class _Run:
    command = "python app.py"
    cwd = "/home/u/app"
    duration_ms = 2500
    exit_code = 0
    exit_signal = None
    started_at = 1000


def test_timeline_relative_times_and_error_collapse():
    events = [
        {"timestamp_ms": 1000, "event_type": "syscall", "syscall": "execve", "path": "/usr/bin/python"},
        {"timestamp_ms": 1300, "event_type": "syscall", "syscall": "openat", "error": "ENOENT"},
        {"timestamp_ms": 1400, "event_type": "syscall", "syscall": "openat", "error": "ENOENT"},
        {"timestamp_ms": 1500, "event_type": "syscall", "syscall": "openat", "error": "ENOENT"},
        {"timestamp_ms": 2200, "event_type": "syscall", "syscall": "fsync", "latency_ms": 1200},
        {"timestamp_ms": 3500, "event_type": "exit", "syscall": "exit", "retval": "0"},
    ]
    tl = summarize._timeline(events, 1000)
    assert any("exec" in l and "python" in l for l in tl)
    # the three ENOENT openat collapse into one ×3 line
    enoent = [l for l in tl if "ENOENT" in l]
    assert len(enoent) == 1 and "×3" in enoent[0]
    assert any("fsync took 1200ms" in l for l in tl)
    assert any("exited 0" in l for l in tl)
    # relative seconds are present (start at +0.00s)
    assert tl[0].startswith("+0.00s")


def test_trajectory_describes_growth():
    metrics = [
        {"rss_mb": 20, "cpu_pct": 90, "open_fds": 8},
        {"rss_mb": 120, "cpu_pct": 95, "open_fds": 30},
    ]
    traj = summarize._trajectory(metrics)
    assert any("RSS over time: 20→120MB" in t and "rising" in t for t in traj)
    assert any("CPU over time" in t for t in traj)


def test_build_messages_includes_new_sections():
    msgs = summarize.build_messages(
        _Run(), {"totals": {"syscall_events": 100}, "peaks": {"rss_mb": 120}},
        [{"severity": "high", "title": "Heap leak", "description": "640KB", "occurrence_count": 1}],
        timeline=["+0.00s exec python", "+1.20s fsync took 1200ms"],
        trajectory=["RSS over time: 20→120MB, peak 120MB (rising)"],
        profile={"malloc": {"supported": True, "n_alloc": 282, "n_free": 201,
                            "bytes_allocated": 1638400, "peak_live_bytes": 671744,
                            "outstanding_bytes": 655360, "outstanding_blocks": 80},
                 "hotspots": [{"function": "malloc", "calls": 282, "total_ms": 12.5}]},
    )
    user = msgs[1]["content"]
    assert "Event timeline" in user and "fsync took 1200ms" in user
    assert "Resource trajectory" in user and "rising" in user
    assert "Allocation profile" in user and "LEAKED at exit" in user
    assert "## What Happened" in msgs[0]["content"]  # system prompt asks for narrative


def test_unsupported_profile_and_flamegraph_omitted():
    assert summarize._profile_lines({"malloc": {"supported": False}}) == []
    assert summarize._flame_lines({"supported": False}) == []
