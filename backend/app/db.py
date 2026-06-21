"""SQLite bootstrap and connection.

Creates `paths.sessions_db()` and the OpenTrace schema (the three-level
sessions -> terminals -> runs model from the user's data-model spec, plus the
per-run `run_views`, `events`, `metrics`, `anomalies`, and `artifacts` tables).

A *session* is a project/workspace (e.g. "My-compiler-app"); a *terminal* is a
shell living inside it; a *run* is a single traced command execution. Everything
analytical (events/metrics/anomalies/artifacts) hangs off a `run_id`.

Schema versions live in `schema_version`; `init()` is idempotent and applies
any pending migrations in order. To add a new migration: bump `CURRENT_VERSION`,
append `(version, sql)` to `MIGRATIONS`, and ship it.

Public surface (stable):
- `connect() -> sqlite3.Connection`
- `init() -> None`
- `CURRENT_VERSION: int`
"""
from __future__ import annotations

import logging
import sqlite3

from . import paths

log = logging.getLogger(__name__)


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- A session is a project / workspace that groups terminals and runs.
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_opened_at INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_opened
    ON sessions(last_opened_at DESC);

-- A terminal is a shell instance inside a session.
CREATE TABLE IF NOT EXISTS terminals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    display_name TEXT,
    shell TEXT NOT NULL,
    cwd TEXT NOT NULL,
    histfile_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_terminals_session ON terminals(session_id);

-- A run is a single traced command execution.
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    terminal_id TEXT REFERENCES terminals(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    command TEXT NOT NULL,
    command_basename TEXT NOT NULL,
    cwd TEXT NOT NULL,
    -- Absolute path of this run's on-disk folder (meta.json, *.ndjson.zst,
    -- strace.log, artifacts/). Implied by the layout spec; stored so the
    -- orchestrator can locate derived/raw outputs without re-deriving names.
    run_dir TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    duration_ms INTEGER,
    exit_code INTEGER,
    exit_signal TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    label TEXT,
    collector_config_json TEXT,
    max_severity TEXT,
    ui_state_json TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_terminal ON runs(terminal_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- Per-run, per-view persisted UI state (overview, timeline, memory, ...).
CREATE TABLE IF NOT EXISTS run_views (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    view_name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, view_name)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp_ms REAL NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    pid INTEGER,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(run_id, timestamp_ms);

CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp_ms REAL NOT NULL,
    cpu_pct REAL,
    rss_mb REAL,
    vms_mb REAL,
    open_fds INTEGER,
    threads INTEGER,
    syscall_rate REAL,
    io_read_bps REAL,
    io_write_bps REAL,
    PRIMARY KEY (run_id, timestamp_ms)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    severity_score REAL NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_ids TEXT,
    first_seen_ms REAL,
    last_seen_ms REAL,
    occurrence_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_anomalies_run ON anomalies(run_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER,
    sha256 TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
"""

# Ordered list of (version, sql) tuples beyond the base schema. Each tuple is
# applied exactly once when the on-disk version is older. Keep migrations
# additive and idempotent where possible.
MIGRATIONS: list[tuple[int, str]] = [
    # (2, "ALTER TABLE runs ADD COLUMN ..."),
]

CURRENT_VERSION = 1 + len(MIGRATIONS)

# Tables owned by this schema, in dependency order (parents before children).
# Used only to tear down a pre-release legacy database (see `_drop_legacy`).
_OWNED_TABLES = [
    "artifacts", "anomalies", "metrics", "events", "run_views",
    "runs", "terminals", "sessions",
]


def connect() -> sqlite3.Connection:
    """Open a connection to the sessions database.

    Foreign keys are enforced; WAL + a busy timeout keep the metrics poller
    thread and the request handlers from tripping over each other.
    """
    conn = sqlite3.connect(paths.sessions_db(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    import time

    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (version, int(time.time() * 1000)),
    )


def _is_legacy(conn: sqlite3.Connection) -> bool:
    """True if a pre-spec `sessions` table (flat Phase-0 schema) is present.

    The old schema had a `process_name` column on `sessions` and no `slug`;
    the new one is the project/terminal/run model. They are incompatible and
    carry no shippable data, so we rebuild rather than migrate.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    if not row:
        return False
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    return "slug" not in cols


def _drop_legacy(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in _OWNED_TABLES + ["schema_version"]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")


def init() -> None:
    """Create the DB file, base schema, and any pending migrations. Idempotent.

    A leftover legacy (flat Phase-0) database is torn down and rebuilt, since
    OpenTrace is pre-release and that data is not worth migrating.
    """
    paths.ensure_dirs()
    db_path = paths.sessions_db()
    fresh = not db_path.exists()
    with connect() as conn:
        if _is_legacy(conn):
            log.warning("dropping legacy Phase-0 database and rebuilding schema")
            _drop_legacy(conn)
        conn.executescript(_BASE_SCHEMA)
        if _current_version(conn) == 0:
            _record_version(conn, 1)
        for version, sql in MIGRATIONS:
            if _current_version(conn) < version:
                conn.executescript(sql)
                _record_version(conn, version)
                log.info("applied db migration v%d", version)
    if fresh:
        log.info("created sessions database at %s", db_path)
