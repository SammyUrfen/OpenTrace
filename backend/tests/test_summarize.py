"""Tests for the AI-summary prompt builder (pure; no network)."""
from __future__ import annotations

from app import runs
from app.summarize import SYSTEM_PROMPT, build_messages


def _run() -> runs.Run:
    return runs.Run(
        id="r1", session_id="s1", display_name="python_x", command="python3 train.py",
        command_basename="python3", cwd="/work", run_dir="/tmp/r1", started_at=0,
        duration_ms=3500, exit_code=0, status="completed", created_at=0,
    )


def test_system_prompt_has_all_sections():
    for h in ["## What's Wrong", "## Why It Matters", "## What to Investigate",
              "## What Looks Fine", "## Confidence"]:
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
