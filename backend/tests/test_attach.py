"""Tests for attach-to-running-PID profiling (app.attach + the /runs/attach endpoints).

The pure detection/enumeration functions are deterministic; the full perf-attach
lifecycle (thread + subprocess) is covered by manual end-to-end verification.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest
from fastapi import HTTPException

from app import attach


def test_detect_runtime_self_is_python():
    # The test runner is CPython — via libpython in maps or the exe name.
    assert attach.detect_runtime(os.getpid()) == "python"


def test_detect_runtime_missing_pid_is_unknown():
    assert attach.detect_runtime(2**31 - 1) == "unknown"


def test_target_info_shape_and_whitespace_collapsed():
    info = attach.target_info(os.getpid())
    assert {"pid", "name", "cmdline", "runtime", "runtime_label", "hint", "rss_mb"} <= set(info)
    assert info["pid"] == os.getpid()
    assert info["cmdline"]  # non-empty
    # a multi-line/tabbed argv must not smear across lines
    assert "\n" not in info["cmdline"] and "\t" not in info["cmdline"]
    assert isinstance(info["rss_mb"], float)


def test_list_targets_shape_excludes_self_and_sorted():
    targets = attach.list_targets(limit=40)
    assert isinstance(targets, list)
    own = os.getpid()
    for t in targets:
        assert {"pid", "name", "cmdline", "runtime", "runtime_label", "hint", "rss_mb"} <= set(t)
        assert t["pid"] != own  # the backend excludes itself
        assert t["runtime"] in attach.RUNTIME_LABELS
    # biggest-RSS-first ordering
    rss = [t["rss_mb"] for t in targets]
    assert rss == sorted(rss, reverse=True)


def test_profiler_hint_distinguishes_native_from_interpreted():
    assert "real symbols" in attach.profiler_hint("native")
    jvm_hint = attach.profiler_hint("jvm")
    assert "VM" in jvm_hint or "interpreter" in jvm_hint


# --- endpoint validation (called directly; no HTTP layer needed) ------------

def test_attach_targets_endpoint_returns_list(ot_home):
    from app import runs

    out = runs.http_attach_targets()
    assert isinstance(out, list)


def test_attach_endpoint_requires_pid_or_port(ot_home):
    from app import runs

    with pytest.raises(HTTPException) as ei:
        runs.http_attach(runs.AttachRequest())
    assert ei.value.status_code == 400


def test_attach_endpoint_rejects_dead_pid(ot_home):
    from app import runs

    with pytest.raises(HTTPException) as ei:
        runs.http_attach(runs.AttachRequest(pid=2**31 - 1))
    assert ei.value.status_code == 400


def test_attach_fail_open_sustains_psutil_window(ot_home, monkeypatch):
    """With perf unavailable, an attach run must still complete with a real
    resource timeline over the window — not ~1 sample (the fail-open guarantee)."""
    from app import runs, storage
    from app.trace import orchestrator

    monkeypatch.setattr(orchestrator.shutil, "which", lambda _name: None)  # "perf missing"
    target = subprocess.Popen(["sleep", "10"])
    try:
        run = orchestrator.start_attach_run(target.pid, window_s=3)
        # window (3s) + finalize headroom
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if runs.get(run.id).status == runs.COMPLETED:
                break
            time.sleep(0.2)
        final = runs.get(run.id)
        assert final.status == runs.COMPLETED
        # a ~3s window at 250ms should yield many samples, not ~1
        assert len(storage.read_metrics(run.id)) >= 5
    finally:
        target.terminate()
        target.wait(timeout=5)


def test_profiler_plan_none_when_tool_missing(monkeypatch):
    monkeypatch.setattr(attach.shutil, "which", lambda _t: None)
    assert attach.profiler_plan("python") is None   # no py-spy installed
    assert attach.profiler_plan("native") is None   # native has no dedicated sampler


def test_profiler_plan_selects_installed_sampler(monkeypatch):
    monkeypatch.setattr(attach.shutil, "which", lambda t: "/usr/bin/" + t)
    plan = attach.profiler_plan("python")
    assert plan == {"tool": "py-spy", "format": "collapsed", "out_file": "pyspy.folded"}
    assert attach.profiler_plan("ruby")["tool"] == "rbspy"
    assert attach.profiler_plan("jvm")["format"] == "collapsed"


def test_sampler_argv_shapes():
    py = attach.sampler_argv("py-spy", 42, 15, "/tmp/o.folded")
    assert py[:2] == ["py-spy", "record"] and "--pid" in py and "42" in py and "/tmp/o.folded" in py
    rb = attach.sampler_argv("rbspy", 42, 15, "/tmp/o.json")
    assert rb[:2] == ["rbspy", "record"] and "speedscope" in rb
    jv = attach.sampler_argv("asprof", 42, 15, "/tmp/o.txt")
    assert jv[0] == "asprof" and "collapsed" in jv and jv[-1] == "42"


def test_incident_storage_roundtrip(tmp_path):
    from app import storage
    inc = {"id": "i1", "ts": 1, "severity": "medium", "title": "CPU pegged",
           "hot": None, "metrics": [], "ai": None}
    storage.append_incident(tmp_path, inc)
    storage.append_incident(tmp_path, {**inc, "id": "i2", "title": "mem spike"})
    got = storage.read_incidents(tmp_path)
    assert [i["id"] for i in got] == ["i1", "i2"]
    # backfill the hot path (as _refresh_flamegraph does) + AI note
    storage.update_incident(tmp_path, "i1", hot={"stack": ["a", "b"], "functions": ["b"]})
    storage.update_incident(tmp_path, "i1", ai="looks like a busy loop in b()")
    got = storage.read_incidents(tmp_path)
    i1 = next(i for i in got if i["id"] == "i1")
    assert i1["hot"]["stack"] == ["a", "b"] and i1["ai"].startswith("looks like")
    assert next(i for i in got if i["id"] == "i2")["hot"] is None  # untouched


def test_read_incidents_missing_is_empty(tmp_path):
    from app import storage
    assert storage.read_incidents(tmp_path) == []
