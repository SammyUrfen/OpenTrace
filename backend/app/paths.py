"""Central registry of OpenTrace filesystem paths.

Every module that needs a path imports it from here. Tests and dev tooling
can override the base directory by setting the `OPENTRACE_HOME` environment
variable.

On-disk layout (mirrors the data-model spec):

    ~/.opentrace/
    ├── config.json
    ├── sessions.db
    └── sessions/
        └── <session-slug>/
            ├── session.json
            ├── terminals/<term-folder>/{history,cwd.txt}
            └── runs/<cmd>-<YYYYMMDD>_<HHMMSS>/
                ├── meta.json
                ├── events.ndjson.zst
                ├── metrics.ndjson.zst
                ├── strace.log
                └── artifacts/
"""
from __future__ import annotations

import os
import re
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


# --- naming helpers ---------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_EDGES = re.compile(r"^[-._]+|[-._]+$")


def slugify(name: str, *, fallback: str = "session") -> str:
    """Turn an arbitrary display name into a filesystem-safe folder name.

    Spaces collapse to hyphens, unsafe characters are dropped, and the result
    is trimmed of leading/trailing separators. Never returns an empty string.
    """
    s = name.strip().replace(" ", "-")
    s = _SLUG_STRIP.sub("", s)
    s = _SLUG_EDGES.sub("", s)
    return s or fallback


def command_basename(command: str) -> str:
    """Best-effort program name from a full command line.

    `python app.py` -> `python`; `./build/my_bin --x` -> `my_bin`;
    `/usr/bin/node server.js` -> `node`. Used for run display names and folders.
    """
    command = command.strip()
    if not command:
        return "command"
    first = command.split()[0]
    base = os.path.basename(first)
    return base or "command"


def run_folder_name(command: str, started: "object") -> str:
    """`<cmd_basename>-<YYYYMMDD>_<HHMMSS>` from a datetime-like `started`.

    `started` must expose `strftime`. Kept here so naming lives next to the
    layout it produces.
    """
    stamp = started.strftime("%Y%m%d_%H%M%S")  # type: ignore[attr-defined]
    return f"{slugify(command_basename(command), fallback='command')}-{stamp}"


# --- directory getters (no side effects) ------------------------------------

def session_dir(slug: str) -> Path:
    return sessions_dir() / slug


def session_json(slug: str) -> Path:
    return session_dir(slug) / "session.json"


def terminals_dir(slug: str) -> Path:
    return session_dir(slug) / "terminals"


def terminal_dir(slug: str, terminal_folder: str) -> Path:
    return terminals_dir(slug) / terminal_folder


def runs_dir(slug: str) -> Path:
    return session_dir(slug) / "runs"


def run_dir(slug: str, run_folder: str) -> Path:
    return runs_dir(slug) / run_folder


# --- directory creators (mkdir -p) ------------------------------------------

def create_project_dir(slug: str) -> Path:
    """Create the `<session>/{terminals,runs}` skeleton. Returns the session dir."""
    project_dir = session_dir(slug)
    (project_dir / "terminals").mkdir(parents=True, exist_ok=True)
    (project_dir / "runs").mkdir(parents=True, exist_ok=True)
    return project_dir


def create_terminal_dir(slug: str, terminal_folder: str) -> Path:
    d = terminal_dir(slug, terminal_folder)
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_run_dir(slug: str, run_folder: str) -> Path:
    d = run_dir(slug, run_folder)
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs() -> None:
    """Create `home` and `sessions_dir` if they don't already exist."""
    home().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
