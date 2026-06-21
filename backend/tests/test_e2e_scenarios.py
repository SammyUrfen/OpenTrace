"""End-to-end scenario tests: run REAL workloads under REAL strace through the
orchestrator (exactly as the otrace wrapper does) and assert the trace engine
detects the right behaviour. This is the "does it actually work" proof for the
rule engine + metrics poller against genuine process traces.

Slower than the unit suite (each scenario runs a ~2-3s subprocess); skipped when
strace is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import runs, sessions, storage
from app.trace import orchestrator
from app.util import now_ms

pytestmark = pytest.mark.skipif(
    not shutil.which("strace"), reason="strace not available"
)


def _run_scenario(prog: str, *, ot_home) -> tuple[runs.Run, set[str], list[dict]]:
    s = sessions.create(sessions.SessionCreate(display_name="Scenarios"))
    run = orchestrator.start_run(runs.RunCreate(
        command=f"python3 -c {prog!r}", cwd="/tmp", session_id=s.id))
    log = Path(run.run_dir) / "strace.log"
    proc = subprocess.Popen(
        ["strace", "-f", "-T", "-ttt", "-o", str(log), "--", "python3", "-c", prog],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert orchestrator.report_pid(run.id, proc.pid) is True
    proc.wait(timeout=90)
    final = orchestrator.end_run(run.id, exit_code=proc.returncode, ended_at=now_ms())
    assert final is not None
    rules = {a["rule_id"] for a in storage.read_anomalies(run.id)}
    metrics = storage.read_metrics(run.id)
    return final, rules, metrics


def test_monotonic_memory_growth_detected(ot_home):
    prog = (
        "import time\n"
        "chunks=[]\n"
        "for i in range(14):\n"
        "    chunks.append(bytearray(15*1024*1024))\n"
        "    time.sleep(0.25)\n"
    )
    final, rules, metrics = _run_scenario(prog, ot_home=ot_home)
    assert final.status == runs.COMPLETED and final.exit_code == 0
    assert len(metrics) >= 6, "poller should have sampled a ~3.5s run"
    # RSS should have climbed substantially across the run
    rss = [m["rss_mb"] for m in metrics if m["rss_mb"] is not None]
    assert max(rss) - min(rss) > 100, f"expected >100MB growth, got {rss}"
    assert "monotonic_memory_growth" in rules
    assert final.max_severity in ("high", "critical")


def test_fd_leak_detected(ot_home):
    prog = (
        "import time, tempfile\n"
        "fds=[]\n"
        "for i in range(80):\n"
        "    fds.append(open(tempfile.mktemp(), 'w'))\n"
        "    if i % 6 == 0:\n"
        "        time.sleep(0.25)\n"
    )
    final, rules, metrics = _run_scenario(prog, ot_home=ot_home)
    assert final.status == runs.COMPLETED
    fds = [m["open_fds"] for m in metrics if m["open_fds"] is not None]
    assert fds and max(fds) - min(fds) > 30, f"fds should climb, got {fds}"
    assert "fd_count_growing" in rules
    assert final.max_severity == "critical"


def test_repeated_open_detected(ot_home):
    prog = (
        "import time\n"
        "for i in range(20):\n"
        "    open('/tmp/ot_scn_repeat.txt', 'w').close()\n"
        "    time.sleep(0.03)\n"
    )
    final, rules, _ = _run_scenario(prog, ot_home=ot_home)
    assert "repeated_open_same_file" in rules
    assert final.max_severity == "high"


def test_clean_run_has_no_false_positives(ot_home):
    prog = "import time\ntime.sleep(1.5)\n"
    final, rules, metrics = _run_scenario(prog, ot_home=ot_home)
    assert final.status == runs.COMPLETED and final.exit_code == 0
    assert rules == set(), f"clean run should have no anomalies, got {rules}"
    assert final.max_severity == "clean"


def test_exit_code_preserved(ot_home):
    final, _, _ = _run_scenario("import sys; sys.exit(42)", ot_home=ot_home)
    assert final.exit_code == 42
    assert final.status == runs.COMPLETED


def test_metrics_only_when_strace_disabled(ot_home):
    """With the syscall collector off, the command runs BARE (no strace) but the
    psutil poller still samples it directly (descendants_only flips to include
    the workload root)."""
    s = sessions.create(sessions.SessionCreate(display_name="MetricsOnly"))
    run = orchestrator.start_run(runs.RunCreate(
        command="python3 sleeper", cwd="/tmp", session_id=s.id,
        collector_config={"strace": False, "psutil": True},
    ))
    # otrace would run the command bare here; the reported pid IS the workload.
    proc = subprocess.Popen(
        ["python3", "-c", "import time\nfor _ in range(8): time.sleep(0.25)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert orchestrator.report_pid(run.id, proc.pid) is True
    proc.wait(timeout=30)
    final = orchestrator.end_run(run.id, exit_code=proc.returncode, ended_at=now_ms())

    assert final.status == runs.COMPLETED
    metrics = storage.read_metrics(run.id)
    assert len(metrics) >= 4, "psutil should sample even without strace"
    # no strace.log -> no events -> clean (no anomalies need events)
    assert not (Path(run.run_dir) / "strace.log").exists()
    assert final.max_severity == "clean"
