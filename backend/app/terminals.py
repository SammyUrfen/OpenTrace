"""Terminals = shell instances inside a session.

Each terminal owns a folder under `<session>/terminals/<term-NN>/` holding its
persistent `history` file and a `cwd.txt`. The PTY layer (Electron) can point
the shell's `HISTFILE` at `histfile_path` so history survives across launches.

Public surface (stable):
- `Terminal`, `TerminalCreate`, `TerminalUpdate` — pydantic models
- `create`, `get`, `list_for_session`, `update`, `close`
- `router` — FastAPI APIRouter mounted at `/terminals`
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, paths, sessions
from .util import new_id, now_ms


class TerminalCreate(BaseModel):
    session_id: str
    shell: str
    cwd: str
    display_name: str | None = None


class TerminalUpdate(BaseModel):
    display_name: str | None = None
    cwd: str | None = None
    is_active: bool | None = None


class Terminal(BaseModel):
    id: str
    session_id: str
    display_name: str | None = None
    shell: str
    cwd: str
    histfile_path: str
    created_at: int
    last_seen_at: int
    is_active: bool


def _row_to_terminal(row: sqlite3.Row) -> Terminal:
    return Terminal(
        id=row["id"],
        session_id=row["session_id"],
        display_name=row["display_name"],
        shell=row["shell"],
        cwd=row["cwd"],
        histfile_path=row["histfile_path"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        is_active=bool(row["is_active"]),
    )


def _free_terminal_folder(slug: str) -> str:
    """First `term-NN` folder name not already present on disk for the session."""
    base = paths.terminals_dir(slug)
    n = 1
    while (base / f"term-{n:02d}").exists():
        n += 1
    return f"term-{n:02d}"


def create(data: TerminalCreate) -> Terminal:
    sess = sessions.get(data.session_id)
    if sess is None:
        raise ValueError(f"unknown session_id: {data.session_id}")

    folder = _free_terminal_folder(sess.slug)
    term_dir = paths.create_terminal_dir(sess.slug, folder)
    histfile = term_dir / "history"
    histfile.touch(exist_ok=True)
    (term_dir / "cwd.txt").write_text(data.cwd, encoding="utf-8")

    tid = new_id()
    now = now_ms()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO terminals
                (id, session_id, display_name, shell, cwd, histfile_path,
                 created_at, last_seen_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (tid, data.session_id, data.display_name, data.shell, data.cwd,
             str(histfile), now, now),
        )
    term = get(tid)
    assert term is not None
    return term


def get(tid: str) -> Terminal | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM terminals WHERE id = ?", (tid,)
        ).fetchone()
        return _row_to_terminal(row) if row else None


def list_for_session(session_id: str) -> list[Terminal]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM terminals WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [_row_to_terminal(r) for r in rows]


def update(tid: str, data: TerminalUpdate) -> Terminal | None:
    fields = data.model_dump(exclude_none=True)
    if "is_active" in fields:
        fields["is_active"] = 1 if fields["is_active"] else 0
    if not fields:
        return get(tid)
    fields["last_seen_at"] = now_ms()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [tid]
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE terminals SET {assignments} WHERE id = ?", values
        )
        if cur.rowcount == 0:
            return None
    term = get(tid)
    if term is not None and data.cwd is not None:
        # histfile_path is `<...>/terminals/<folder>/history`; cwd.txt is beside it.
        try:
            Path(term.histfile_path).with_name("cwd.txt").write_text(
                term.cwd, encoding="utf-8"
            )
        except OSError:
            pass
    return term


def close(tid: str) -> Terminal | None:
    """Mark a terminal inactive (its shell exited). Row is kept for history."""
    return update(tid, TerminalUpdate(is_active=False))


# --- HTTP -------------------------------------------------------------------

router = APIRouter(prefix="/terminals", tags=["terminals"])


class TerminalAttach(BaseModel):
    shell: str
    cwd: str
    session_id: str | None = None
    display_name: str | None = None


class AttachResponse(BaseModel):
    session_id: str
    terminal_id: str
    histfile_path: str


@router.post("/attach", response_model=AttachResponse)
def http_attach(data: TerminalAttach) -> AttachResponse:
    """One-shot used by the shell hook: ensure a session and register a terminal.

    Falls back to the default session when none is specified, so a freshly
    spawned shell can self-register without the UI having picked a project yet.
    """
    sess = sessions.get(data.session_id) if data.session_id else None
    if sess is None:
        sess = sessions.get_or_create_default()
    term = create(TerminalCreate(
        session_id=sess.id, shell=data.shell, cwd=data.cwd,
        display_name=data.display_name,
    ))
    return AttachResponse(
        session_id=sess.id, terminal_id=term.id, histfile_path=term.histfile_path
    )


@router.post("", response_model=Terminal)
def http_create(data: TerminalCreate) -> Terminal:
    try:
        return create(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[Terminal])
def http_list(session_id: str) -> list[Terminal]:
    return list_for_session(session_id)


@router.get("/{tid}", response_model=Terminal)
def http_get(tid: str) -> Terminal:
    term = get(tid)
    if term is None:
        raise HTTPException(status_code=404, detail="terminal not found")
    return term


@router.patch("/{tid}", response_model=Terminal)
def http_update(tid: str, data: TerminalUpdate) -> Terminal:
    term = update(tid, data)
    if term is None:
        raise HTTPException(status_code=404, detail="terminal not found")
    return term


@router.post("/{tid}/close", response_model=Terminal)
def http_close(tid: str) -> Terminal:
    term = close(tid)
    if term is None:
        raise HTTPException(status_code=404, detail="terminal not found")
    return term
