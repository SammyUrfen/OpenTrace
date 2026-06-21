"""Runs = a single traced command execution.

A run belongs to a session (project) and, usually, a terminal. Creating a run
also creates its on-disk folder (`<session>/runs/<cmd>-<stamp>/`). The trace
engine (`app.trace.orchestrator`) drives the rest of the lifecycle: it polls
metrics while the command runs, then on `finalize` parses strace, derives
metrics, computes anomalies, writes the derived files, and flips `status`.

Public surface (stable):
- `Run`, `RunCreate`, `RunUpdate` — pydantic models
- `create`, `get`, `list_for_session`, `list_recent`, `update`, `finalize`,
  `set_status`, `delete`
- statuses: `RUNNING`, `ANALYZING`, `COMPLETED`, `ERROR`
- `router` — FastAPI APIRouter mounted at `/runs`
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, paths, sessions, storage
from .util import new_id, now_ms

RUNNING = "running"
ANALYZING = "analyzing"
COMPLETED = "completed"
ERROR = "error"


class RunCreate(BaseModel):
    command: str
    cwd: str
    session_id: str | None = None
    terminal_id: str | None = None
    label: str | None = None
    collector_config: dict | None = None


class RunUpdate(BaseModel):
    label: str | None = None
    display_name: str | None = None
    ui_state: dict | None = None


class Run(BaseModel):
    id: str
    session_id: str
    terminal_id: str | None = None
    display_name: str
    command: str
    command_basename: str
    cwd: str
    run_dir: str
    started_at: int
    ended_at: int | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    exit_signal: str | None = None
    status: str
    label: str | None = None
    collector_config: dict | None = None
    max_severity: str | None = None
    ui_state: dict | None = None
    created_at: int


def _loads(raw: str | None) -> dict | None:
    return json.loads(raw) if raw else None


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        session_id=row["session_id"],
        terminal_id=row["terminal_id"],
        display_name=row["display_name"],
        command=row["command"],
        command_basename=row["command_basename"],
        cwd=row["cwd"],
        run_dir=row["run_dir"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        exit_code=row["exit_code"],
        exit_signal=row["exit_signal"],
        status=row["status"],
        label=row["label"],
        collector_config=_loads(row["collector_config_json"]),
        max_severity=row["max_severity"],
        ui_state=_loads(row["ui_state_json"]),
        created_at=row["created_at"],
    )


def _unique_run_dir(slug: str, folder: str) -> tuple[str, "object"]:
    """Reserve a non-colliding run folder on disk; return (folder, abspath)."""
    candidate = folder
    n = 2
    while paths.run_dir(slug, candidate).exists():
        candidate = f"{folder}-{n}"
        n += 1
    abspath = paths.create_run_dir(slug, candidate)
    return candidate, abspath


def create(data: RunCreate) -> Run:
    sess = (
        sessions.get(data.session_id)
        if data.session_id
        else sessions.get_or_create_default()
    )
    if sess is None:
        raise ValueError(f"unknown session_id: {data.session_id}")

    started = datetime.now()
    started_ms = int(started.timestamp() * 1000)
    basename = paths.command_basename(data.command)
    folder = paths.run_folder_name(data.command, started)
    _folder, run_path = _unique_run_dir(sess.slug, folder)
    display_name = f"{basename}_{started.strftime('%Y%m%d_%H%M%S')}"

    rid = new_id()
    now = now_ms()
    collector_json = (
        json.dumps(data.collector_config) if data.collector_config else None
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO runs
                (id, session_id, terminal_id, display_name, command,
                 command_basename, cwd, run_dir, started_at, status,
                 label, collector_config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, sess.id, data.terminal_id, display_name, data.command,
             basename, data.cwd, str(run_path), started_ms, RUNNING,
             data.label, collector_json, now),
        )
    run = get(rid)
    assert run is not None
    return run


def get(rid: str) -> Run | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        return _row_to_run(row) if row else None


def list_for_session(session_id: str) -> list[Run]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE session_id = ? ORDER BY started_at DESC",
            (session_id,),
        ).fetchall()
        return [_row_to_run(r) for r in rows]


def list_recent(limit: int = 100, offset: int = 0) -> list[Run]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_run(r) for r in rows]


def update(rid: str, data: RunUpdate) -> Run | None:
    fields: dict[str, object] = {}
    if data.label is not None:
        fields["label"] = data.label
    if data.display_name is not None:
        fields["display_name"] = data.display_name
    if data.ui_state is not None:
        fields["ui_state_json"] = json.dumps(data.ui_state)
    if not fields:
        return get(rid)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [rid]
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)
        if cur.rowcount == 0:
            return None
    return get(rid)


def set_status(rid: str, status: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, rid))


def finalize(
    rid: str,
    *,
    ended_at: int | None = None,
    exit_code: int | None = None,
    exit_signal: str | None = None,
    status: str = COMPLETED,
    max_severity: str | None = None,
) -> Run | None:
    """Stamp terminal state on a run (exit, duration, severity, status)."""
    run = get(rid)
    if run is None:
        return None
    ended = ended_at if ended_at is not None else now_ms()
    duration = max(0, ended - run.started_at)
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE runs
               SET ended_at = ?, duration_ms = ?, exit_code = ?,
                   exit_signal = ?, status = ?, max_severity = ?
             WHERE id = ?
            """,
            (ended, duration, exit_code, exit_signal, status, max_severity, rid),
        )
    return get(rid)


def delete(rid: str) -> bool:
    run = get(rid)
    if run is None:
        return False
    with db.connect() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
    if run.run_dir:
        shutil.rmtree(run.run_dir, ignore_errors=True)
    return True


# --- HTTP -------------------------------------------------------------------

router = APIRouter(prefix="/runs", tags=["runs"])


class RunStartResponse(BaseModel):
    run: Run
    strace_log_path: str
    run_dir: str


class PidReport(BaseModel):
    pid: int


class EndReport(BaseModel):
    exit_code: int | None = None
    exit_signal: str | None = None
    ended_at: int | None = None


@router.post("/start", response_model=RunStartResponse)
def http_start(data: RunCreate) -> RunStartResponse:
    from .trace import orchestrator

    try:
        run = orchestrator.start_run(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RunStartResponse(
        run=run,
        strace_log_path=str(Path(run.run_dir) / "strace.log"),
        run_dir=run.run_dir,
    )


@router.post("/{rid}/pid")
def http_pid(rid: str, data: PidReport) -> dict[str, bool]:
    from .trace import orchestrator

    ok = orchestrator.report_pid(rid, data.pid)
    if not ok:
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True}


@router.post("/{rid}/end", response_model=Run)
def http_end(rid: str, data: EndReport) -> Run:
    from .trace import orchestrator

    run = orchestrator.end_run(
        rid,
        exit_code=data.exit_code,
        exit_signal=data.exit_signal,
        ended_at=data.ended_at,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("", response_model=list[Run])
def http_list(session_id: str | None = None, limit: int = 100) -> list[Run]:
    if session_id:
        return list_for_session(session_id)
    return list_recent(limit=limit)


@router.get("/{rid}", response_model=Run)
def http_get(rid: str) -> Run:
    run = get(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.patch("/{rid}", response_model=Run)
def http_update(rid: str, data: RunUpdate) -> Run:
    run = update(rid, data)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.delete("/{rid}")
def http_delete(rid: str) -> dict[str, bool]:
    return {"deleted": delete(rid)}


# --- run analytical detail --------------------------------------------------

def _require(rid: str) -> Run:
    run = get(rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/{rid}/events")
def http_events(rid: str, limit: int = 5000) -> list[dict]:
    _require(rid)
    return storage.read_events(rid, limit=limit)


@router.get("/{rid}/metrics")
def http_metrics(rid: str) -> list[dict]:
    _require(rid)
    return storage.read_metrics(rid)


@router.get("/{rid}/anomalies")
def http_anomalies(rid: str) -> list[dict]:
    _require(rid)
    return storage.read_anomalies(rid)


@router.get("/{rid}/artifacts")
def http_artifacts(rid: str) -> list[dict]:
    _require(rid)
    return storage.read_artifacts(rid)


@router.get("/{rid}/summary")
def http_summary(rid: str) -> dict:
    run = _require(rid)
    meta = Path(run.run_dir) / "meta.json"
    if meta.exists():
        return json.loads(meta.read_text())
    return {"run_id": rid, "status": run.status, "pending": True}


@router.get("/{rid}/syscalls")
def http_syscalls(rid: str) -> list[dict]:
    """Per-syscall stats aggregated from the full events.ndjson.zst stream."""
    from . import aggregate

    run = _require(rid)
    events = storage.read_ndjson_zst(Path(run.run_dir) / "events.ndjson.zst")
    return aggregate.syscall_stats(events)
