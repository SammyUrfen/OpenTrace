"""Tests for tracing-tool detection (app.tools)."""
from __future__ import annotations

from app import tools


def test_detect_has_the_three_tools_and_shape():
    d = tools.detect()
    assert {"tools", "perf_event_paranoid", "distro"} <= set(d)
    names = {t["name"] for t in d["tools"]}
    assert names == {"strace", "ltrace", "perf"}
    for t in d["tools"]:
        assert {"name", "label", "unlocks", "available", "path", "version", "install_hint"} <= set(t)
        # available <=> a resolved path; install hint only when missing
        assert t["available"] == (t["path"] is not None)
        if t["available"]:
            assert t["install_hint"] is None
        # version is a string or None
        assert t["version"] is None or isinstance(t["version"], str)


def test_version_parsing(monkeypatch):
    class _R:
        def __init__(self, out):
            self.stdout = out
            self.stderr = ""

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _R("strace -- version 7.0\n(c) ...\n"))
    assert tools._version("/usr/bin/strace", "strace") == "strace 7.0"
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _R("perf version 7.0.12-201.fc44\n"))
    assert tools._version("/usr/bin/perf", "perf") == "perf 7.0.12"


def test_install_hint_per_distro(monkeypatch):
    tools._distro_id.cache_clear()
    monkeypatch.setattr(tools, "_distro_id", lambda: "ubuntu")
    # perf is packaged as linux-tools-generic on ubuntu
    assert tools._install_hint("perf") == "sudo apt install -y linux-tools-generic"
    assert tools._install_hint("strace") == "sudo apt install -y strace"
    monkeypatch.setattr(tools, "_distro_id", lambda: "fedora")
    assert tools._install_hint("ltrace") == "sudo dnf install -y ltrace"
    monkeypatch.setattr(tools, "_distro_id", lambda: "unknownos")
    assert tools._install_hint("strace") is None
