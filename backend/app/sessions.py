"""Sessions = projects / workspaces.

A session groups terminals and runs under one filesystem-safe slug. It is the
top of the data model; terminals and runs reference it by `session_id`.

Public surface (stable):
- `Session`, `SessionCreate`, `SessionUpdate` — pydantic models
- `create`, `get`, `get_by_slug`, `update`, `delete`, `list_all`
- `get_or_create_default()` — convenience for the single-window app shell
- `router` — FastAPI APIRouter mounted at `/sessions`
"""
from __future__ import annotations

import json
import shutil
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, paths
from .util import new_id, now_ms

DEFAULT_DISPLAY_NAME = "Default"


class SessionCreate(BaseModel):
    display_name: str
    notes: str | None = None


class SessionUpdate(BaseModel):
    display_name: str | None = None
    notes: str | None = None


class Session(BaseModel):
    id: str
    display_name: str
    slug: str
    created_at: int
    updated_at: int
    last_opened_at: int | None = None
    notes: str | None = None


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        display_name=row["display_name"],
        slug=row["slug"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_opened_at=row["last_opened_at"],
        notes=row["notes"],
    )


def _unique_slug(conn: sqlite3.Connection, display_name: str) -> str:
    """A slug not already used by another session (suffix -2, -3, … on clash)."""
    base = paths.slugify(display_name)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM sessions WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _write_session_json(sess: Session) -> None:
    """Mirror the session record to `<session>/session.json` on disk."""
    paths.create_project_dir(sess.slug)
    paths.session_json(sess.slug).write_text(
        json.dumps(sess.model_dump(), indent=2), encoding="utf-8"
    )


def create(data: SessionCreate) -> Session:
    sid = new_id()
    now = now_ms()
    with db.connect() as conn:
        slug = _unique_slug(conn, data.display_name)
        conn.execute(
            """
            INSERT INTO sessions
                (id, display_name, slug, created_at, updated_at,
                 last_opened_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, data.display_name, slug, now, now, now, data.notes),
        )
    sess = get(sid)
    assert sess is not None
    _write_session_json(sess)
    return sess


def get(sid: str) -> Session | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return _row_to_session(row) if row else None


def get_by_slug(slug: str) -> Session | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE slug = ?", (slug,)
        ).fetchone()
        return _row_to_session(row) if row else None


def list_all() -> list[Session]:
    # Newest-created first. (last_opened_at was only ever set at creation — the
    # touch endpoint that would have updated it had no callers and was removed —
    # so created_at DESC preserves the order the app has always shown.)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_session(r) for r in rows]


def update(sid: str, data: SessionUpdate) -> Session | None:
    fields = data.model_dump(exclude_none=True)
    if not fields:
        return get(sid)
    fields["updated_at"] = now_ms()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [sid]
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?", values
        )
        if cur.rowcount == 0:
            return None
    sess = get(sid)
    if sess is not None:
        _write_session_json(sess)
    return sess


def delete(sid: str) -> bool:
    """Remove a session, its DB rows (cascade), and its on-disk directory."""
    sess = get(sid)
    if sess is None:
        return False
    # The DB cascade deletes run rows, but any live run's poller/monitor threads
    # must be torn down explicitly or they keep running against nothing.
    # Function-local imports: runs/orchestrator import this module.
    from . import runs
    from .trace import orchestrator

    for run in runs.list_for_session(sid):
        orchestrator.abort_run(run.id)
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    shutil.rmtree(paths.session_dir(sess.slug), ignore_errors=True)
    return True


def get_or_create_default() -> Session:
    """Return the most-recent session, creating a `Default` one if none exist."""
    existing = list_all()
    if existing:
        return existing[0]
    return create(SessionCreate(display_name=DEFAULT_DISPLAY_NAME))


# --- HTTP -------------------------------------------------------------------

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=Session)
def http_create(data: SessionCreate) -> Session:
    return create(data)


@router.get("", response_model=list[Session])
def http_list() -> list[Session]:
    return list_all()


@router.get("/{sid}", response_model=Session)
def http_get(sid: str) -> Session:
    sess = get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


@router.patch("/{sid}", response_model=Session)
def http_update(sid: str, data: SessionUpdate) -> Session:
    sess = update(sid, data)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


@router.delete("/{sid}")
def http_delete(sid: str) -> dict[str, bool]:
    return {"deleted": delete(sid)}
