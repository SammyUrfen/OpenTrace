"""Tests for the AI-summary prompt builder (pure; no network)."""
from __future__ import annotations

from app import runs
from app.summarize import (
    DIFF_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_diff_messages,
    build_messages,
)


def _run() -> runs.Run:
    return runs.Run(
        id="r1", session_id="s1", display_name="python_x", command="python3 train.py",
        command_basename="python3", cwd="/work", run_dir="/tmp/r1", started_at=0,
        duration_ms=3500, exit_code=0, status="completed", created_at=0,
    )


def test_system_prompt_has_all_sections():
    for h in ["## What Happened", "## What's Wrong", "## Why It Matters",
              "## What to Investigate", "## Confidence"]:
        assert h in SYSTEM_PROMPT


def test_user_prompt_includes_run_data_and_anomalies():
    summary = {
        "totals": {"syscall_events": 1234, "errors": 3, "metric_samples": 12,
                   "top_syscalls": [["openat", 500], ["read", 300]]},
        "peaks": {"cpu_pct": 8, "rss_mb": 219, "open_fds": 3, "threads": 1},
    }
    anomalies = [{
        "severity": "high", "title": "Memory grew 24MB → 219MB",
        "description": "RSS climbed monotonically.", "occurrence_count": 170,
    }]
    msgs = build_messages(_run(), summary, anomalies)
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "python3 train.py" in user
    assert "219" in user and "openat×500" in user
    assert "[HIGH] Memory grew 24MB → 219MB (×170)" in user


def test_user_prompt_handles_no_anomalies():
    user = build_messages(_run(), {"totals": {}, "peaks": {}}, [])[1]["content"]
    assert "no anomalies" in user.lower()


def _run2() -> runs.Run:
    return runs.Run(
        id="r2", session_id="s1", display_name="python_y", command="python3 train.py",
        command_basename="python3", cwd="/work", run_dir="/tmp/r2", started_at=0,
        duration_ms=600, exit_code=0, status="completed", created_at=0,
    )


def test_diff_prompt_has_verdict_and_deltas():
    for h in ["## Verdict", "## What Changed", "## Likely Cause", "## What to Check"]:
        assert h in DIFF_SYSTEM_PROMPT
    sa = {"peaks": {"rss_mb": 219, "cpu_pct": 8}, "totals": {"syscall_events": 1000, "errors": 3}}
    sb = {"peaks": {"rss_mb": 69, "cpu_pct": 8}, "totals": {"syscall_events": 1400, "errors": 9}}
    anom_a = [{"rule_id": "monotonic_memory_growth", "severity": "high", "title": "leak"}]
    anom_b = [{"rule_id": "fd_count_growing", "severity": "critical", "title": "fd leak"}]
    msgs = build_diff_messages(_run(), sa, anom_a, _run2(), sb, anom_b)
    user = msgs[1]["content"]
    assert "Run A" in user and "Run B" in user
    assert "rss_mb: A=219 B=69 (Δ -150)" in user
    assert "duration" in user and "-2900ms" in user  # 600 - 3500
    assert "anomalies added in B: ['fd leak']" in user
    assert "anomalies gone in B: ['leak']" in user
