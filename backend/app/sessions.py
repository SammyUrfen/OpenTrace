"""Session records: create / read / update / list.

A session is a single execution span the UI cares about — for Phase 0 that
means one record per terminal shell, but the schema is general enough for
later phases (one record per traced command, per replay, etc.).

Public surface (stable):
- `Session`, `SessionCreate`, `SessionUpdate` — pydantic models
- `create(data) -> Session`
- `get(sid) -> Session | None`
- `update(sid, data) -> Session | None`
- `list_recent(limit, offset) -> list[Session]`
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from pydantic import BaseModel, Field

from . import db


class SessionCreate(BaseModel):
    command: str
    cwd: str
    process_name: str | None = None
    label: str | None = None
    tags: list[str] | None = None


class SessionUpdate(BaseModel):
    ended_at: int | None = None
    exit_code: int | None = None
    exit_signal: str | None = None
    label: str | None = None
    tags: list[str] | None = None


class Session(BaseModel):
    id: str
    process_name: str
    command: str
    cwd: str
    started_at: int
    ended_at: int | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    exit_signal: str | None = None
    label: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: int


def _row_to_session(row: sqlite3.Row) -> Session:
    tags_raw = row["tags"]
    return Session(
        id=row["id"],
        process_name=row["process_name"],
        command=row["command"],
        cwd=row["cwd"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        exit_code=row["exit_code"],
        exit_signal=row["exit_signal"],
        label=row["label"],
        tags=json.loads(tags_raw) if tags_raw else [],
        created_at=row["created_at"],
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def create(data: SessionCreate) -> Session:
    sid = uuid.uuid4().hex
    now = _now_ms()
    process_name = (
        data.process_name
        or (data.command.split()[0] if data.command.strip() else "unknown")
    )
    tags_json = json.dumps(data.tags) if data.tags else None
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (id, process_name, command, cwd, started_at,
                 label, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, process_name, data.command, data.cwd, now,
             data.label, tags_json, now),
        )
    found = get(sid)
    assert found is not None  # we just inserted it
    return found


def get(sid: str) -> Session | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return _row_to_session(row) if row else None


def update(sid: str, data: SessionUpdate) -> Session | None:
    existing = get(sid)
    if existing is None:
        return None

    fields = data.model_dump(exclude_none=True)
    if not fields:
        return existing

    # Compute duration_ms when ended_at is provided and caller didn't pass one.
    if "ended_at" in fields and "duration_ms" not in fields:
        fields["duration_ms"] = max(0, fields["ended_at"] - existing.started_at)

    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"]) if fields["tags"] else None

    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [sid]
    with db.connect() as conn:
        conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", values)
    return get(sid)


def list_recent(limit: int = 50, offset: int = 0) -> list[Session]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_session(r) for r in rows]
