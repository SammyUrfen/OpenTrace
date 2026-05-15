"""Central registry of OpenTrace filesystem paths.

Every module that needs a path imports it from here. Tests and dev tooling
can override the base directory by setting the `OPENTRACE_HOME` environment
variable.
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Root directory for OpenTrace user-local data.

    Defaults to `~/.opentrace`. Override with `OPENTRACE_HOME`.
    """
    override = os.environ.get("OPENTRACE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".opentrace"


def config_file() -> Path:
    return home() / "config.json"


def sessions_db() -> Path:
    return home() / "sessions.db"


def sessions_dir() -> Path:
    return home() / "sessions"


def ensure_dirs() -> None:
    """Create `home` and `sessions_dir` if they don't already exist."""
    home().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
