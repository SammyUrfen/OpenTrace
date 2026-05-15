"""SQLite bootstrap and connection.

Creates `paths.sessions_db()` and the Phase 0 schema (sessions, events,
metrics, anomalies — matching `OpenTrace_Roadmap.md` §8) on first run.

Schema versions live in `schema_version`; `init()` is idempotent and applies
any pending migrations in order. To add a new migration: bump
`CURRENT_VERSION`, append `(version, sql)` to `MIGRATIONS`, and ship it.

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

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    process_name TEXT NOT NULL,
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    duration_ms INTEGER,
    exit_code INTEGER,
    exit_signal TEXT,
    label TEXT,
    tags TEXT,
    ai_summary TEXT,
    max_severity TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp_ms REAL NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    pid INTEGER,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(session_id, timestamp_ms);

CREATE TABLE IF NOT EXISTS metrics (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp_ms REAL NOT NULL,
    cpu_pct REAL,
    rss_mb REAL,
    vms_mb REAL,
    open_fds INTEGER,
    threads INTEGER,
    syscall_rate REAL,
    io_read_bps REAL,
    io_write_bps REAL,
    PRIMARY KEY (session_id, timestamp_ms)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
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
"""

# Ordered list of (version, sql) tuples beyond the base schema. Each tuple is
# applied exactly once when the on-disk version is older. Keep migrations
# additive and idempotent where possible.
MIGRATIONS: list[tuple[int, str]] = [
    # (2, "ALTER TABLE sessions ADD COLUMN ..."),
]

CURRENT_VERSION = 1 + len(MIGRATIONS)


def connect() -> sqlite3.Connection:
    """Open a connection to the sessions database with foreign keys enabled."""
    conn = sqlite3.connect(paths.sessions_db())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def init() -> None:
    """Create the DB file, base schema, and any pending migrations. Idempotent."""
    paths.ensure_dirs()
    db_path = paths.sessions_db()
    fresh = not db_path.exists()
    with connect() as conn:
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
