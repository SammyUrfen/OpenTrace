"""Shared pytest fixtures. Redirects OpenTrace's home to a temp dir so tests
never touch the real `~/.opentrace`.

`paths.home()` reads `OPENTRACE_HOME` on every call, so setting the env var is
enough — no module reloads needed, and every module that imports `paths`/`db`
picks up the temp location transparently.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def ot_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENTRACE_HOME", str(tmp_path))
    from app import db

    db.init()
    yield tmp_path
