"""End-to-end pipeline test: simulate the shell wrapper exactly (run a real
command under strace, report the pid, end the run) and assert the orchestrator
produces a fully finalized run with derived files, metrics, and artifacts."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import runs, sessions, storage, terminals
from app.trace import orchestrator
from app.util import now_ms

# A program that runs long enough for the 250ms poller to sample it and opens
# the same app-level file >10 times to trip `repeated_open_same_file`.
PROG = (
    "import time\n"
    "p='/tmp/ot_pipeline_target.txt'\n"
    "for i in range(14):\n"
    "    f=open(p,'w'); f.write('x'*2048); f.close()\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.skipif(not shutil.which("strace"), reason="strace not available")
def test_full_run_pipeline(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Pipeline Proj"))
    t = terminals.create(terminals.TerminalCreate(
        session_id=s.id, shell="/bin/bash", cwd="/tmp"))

    run = orchestrator.start_run(runs.RunCreate(
        command="python3 -c '<prog>'", cwd="/tmp",
        session_id=s.id, terminal_id=t.id,
        collector_config={"strace": True, "psutil": True},
    ))
    assert run.status == runs.RUNNING
    strace_log = Path(run.run_dir) / "strace.log"

    proc = subprocess.Popen(
        ["strace", "-f", "-T", "-ttt", "-o", str(strace_log), "--",
         "python3", "-c", PROG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert orchestrator.report_pid(run.id, proc.pid) is True
    proc.wait(timeout=60)
    final = orchestrator.end_run(
        run.id, exit_code=proc.returncode, ended_at=now_ms())

    assert final is not None
    assert final.status == runs.COMPLETED
    assert final.exit_code == 0
    assert final.duration_ms is not None and final.duration_ms > 0

    # derived files written
    rd = Path(run.run_dir)
    assert (rd / "events.ndjson.zst").exists()
    assert (rd / "metrics.ndjson.zst").exists()
    assert (rd / "meta.json").exists()
    assert strace_log.exists()

    # events captured (the target file was opened repeatedly)
    full_events = list(storage.read_ndjson_zst(rd / "events.ndjson.zst"))
    assert len(full_events) > 20
    assert any(e.get("path") == "/tmp/ot_pipeline_target.txt" for e in full_events)

    # metrics were sampled (program ran ~0.7s, poller at 250ms)
    metrics = storage.read_metrics(run.id)
    assert len(metrics) >= 1

    # artifacts registered
    kinds = {a["kind"] for a in storage.read_artifacts(run.id)}
    assert {"strace-log", "events", "metrics", "meta"} <= kinds

    # the repeated-open rule should have fired -> non-clean severity
    rule_ids = {a["rule_id"] for a in storage.read_anomalies(run.id)}
    assert "repeated_open_same_file" in rule_ids
    assert final.max_severity in ("high", "critical", "medium")


@pytest.mark.skipif(not shutil.which("strace"), reason="strace not available")
def test_reconcile_orphans(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Orphan Proj"))
    run = orchestrator.start_run(runs.RunCreate(
        command="sleep 100", cwd="/tmp", session_id=s.id))
    assert runs.get(run.id).status == runs.RUNNING
    # simulate a backend restart: nothing finalized this run
    n = orchestrator.reconcile_orphans()
    assert n >= 1
    assert runs.get(run.id).status == runs.ERROR
