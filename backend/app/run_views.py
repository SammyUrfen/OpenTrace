"""Per-run, per-view persisted UI state (overview, timeline, memory, ...).

Lets the renderer remember things like scroll position, selected event, or
zoom window for each analytics tab of a run, keyed by `(run_id, view_name)`.

Public surface (stable):
- `RunView`, `RunViewUpsert` — pydantic models
- `upsert`, `get`, `list_for_run`
- `router` — FastAPI APIRouter mounted at `/runs/{rid}/views`
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db
from .util import now_ms


class RunViewUpsert(BaseModel):
    state: dict


class RunView(BaseModel):
    run_id: str
    view_name: str
    state: dict
    updated_at: int


def _row_to_view(row: sqlite3.Row) -> RunView:
    return RunView(
        run_id=row["run_id"],
        view_name=row["view_name"],
        state=json.loads(row["state_json"]),
        updated_at=row["updated_at"],
    )


def upsert(run_id: str, view_name: str, state: dict) -> RunView:
    now = now_ms()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO run_views (run_id, view_name, state_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id, view_name)
            DO UPDATE SET state_json = excluded.state_json,
                          updated_at = excluded.updated_at
            """,
            (run_id, view_name, json.dumps(state), now),
        )
    view = get(run_id, view_name)
    assert view is not None
    return view


def get(run_id: str, view_name: str) -> RunView | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM run_views WHERE run_id = ? AND view_name = ?",
            (run_id, view_name),
        ).fetchone()
        return _row_to_view(row) if row else None


def list_for_run(run_id: str) -> list[RunView]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_views WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [_row_to_view(r) for r in rows]


# --- HTTP -------------------------------------------------------------------

router = APIRouter(prefix="/runs/{rid}/views", tags=["run_views"])


@router.get("", response_model=list[RunView])
def http_list(rid: str) -> list[RunView]:
    return list_for_run(rid)


@router.get("/{view_name}", response_model=RunView)
def http_get(rid: str, view_name: str) -> RunView:
    view = get(rid, view_name)
    if view is None:
        raise HTTPException(status_code=404, detail="view state not found")
    return view


@router.put("/{view_name}", response_model=RunView)
def http_upsert(rid: str, view_name: str, data: RunViewUpsert) -> RunView:
    return upsert(rid, view_name, data.state)
