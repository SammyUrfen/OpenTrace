"""Persistence for a run's analytical output: SQLite rows + on-disk files.

Two destinations, by design:

* **On disk** (`<run>/events.ndjson.zst`, `metrics.ndjson.zst`, `meta.json`,
  raw `strace.log`): the *complete*, append-friendly, compressed record. This
  is the source of truth for replay/export.
* **SQLite** (`events`, `metrics`, `anomalies`, `artifacts`): fast queryable
  views. Metrics are stored in full (a few per second). Events are stored as a
  *curated subset* — errors, slow/lifecycle/signal events, and anomaly evidence
  — because persisting every syscall would bloat the DB; the full stream lives
  in `events.ndjson.zst`.

Public surface (stable):
- `write_ndjson_zst(path, rows)` / `read_ndjson_zst(path)`
- `insert_events`, `insert_metrics`, `backfill_syscall_rate`
- `insert_anomalies`, `record_artifact`, `write_meta`
- `read_metrics`, `read_events`, `read_anomalies`, `read_artifacts`
- `SEVERITY_ORDER`, `max_severity`
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

import zstandard as zstd

from . import db
from .trace.events import Anomaly, MetricSample, TraceEvent
from .util import new_id, now_ms

# Highest-to-lowest. `clean` is the implicit floor when nothing fired.
SEVERITY_ORDER = ["critical", "high", "medium", "low", "clean"]
_SEV_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}


def max_severity(severities: Iterable[str]) -> str:
    """Return the most severe label in `severities` (or `clean` if empty)."""
    best = "clean"
    best_rank = _SEV_RANK["clean"]
    for s in severities:
        rank = _SEV_RANK.get(s, _SEV_RANK["clean"])
        if rank < best_rank:
            best, best_rank = s, rank
    return best


# --- ndjson.zst -------------------------------------------------------------

def write_ndjson_zst(path: str | Path, rows: Iterable[dict]) -> int:
    """Stream `rows` as newline-delimited JSON, zstd-compressed. Returns count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cctx = zstd.ZstdCompressor(level=10)
    n = 0
    with open(path, "wb") as raw, cctx.stream_writer(raw) as out:
        for row in rows:
            out.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
            n += 1
    return n


def read_ndjson_zst(path: str | Path) -> Iterator[dict]:
    """Yield decoded objects from a `write_ndjson_zst` file."""
    path = Path(path)
    if not path.exists():
        return
    dctx = zstd.ZstdDecompressor()
    with open(path, "rb") as raw, dctx.stream_reader(raw) as reader:
        buf = b""
        while True:
            chunk = reader.read(65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if line:
                    yield json.loads(line)
        if buf.strip():
            yield json.loads(buf)


# --- SQLite: events ---------------------------------------------------------

def insert_events(run_id: str, events: Iterable[TraceEvent]) -> list[str]:
    """Insert curated events; returns the generated event ids (for evidence)."""
    ids: list[str] = []
    rows = []
    for ev in events:
        eid = new_id()
        ids.append(eid)
        rows.append((
            eid, run_id, ev.timestamp_ms, ev.source, ev.event_type, ev.pid,
            json.dumps(ev.to_payload(), separators=(",", ":")).encode(),
        ))
    if rows:
        with db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO events
                    (id, run_id, timestamp_ms, source, event_type, pid, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    return ids


def read_events(run_id: str, limit: int = 5000) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp_ms, source, event_type, pid, payload
              FROM events WHERE run_id = ?
             ORDER BY timestamp_ms LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(bytes(r["payload"]).decode())
        out.append({
            "id": r["id"], "timestamp_ms": r["timestamp_ms"],
            "source": r["source"], "event_type": r["event_type"],
            "pid": r["pid"], **payload,
        })
    return out


# --- SQLite: metrics --------------------------------------------------------

def insert_metric(run_id: str, sample: MetricSample) -> None:
    """Insert (or replace) a single live metric sample."""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO metrics
                (run_id, timestamp_ms, cpu_pct, rss_mb, vms_mb, open_fds,
                 threads, syscall_rate, io_read_bps, io_write_bps)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, *sample.as_row()),
        )


def insert_metrics(run_id: str, samples: Iterable[MetricSample]) -> None:
    rows = [(run_id, *s.as_row()) for s in samples]
    if rows:
        with db.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO metrics
                    (run_id, timestamp_ms, cpu_pct, rss_mb, vms_mb, open_fds,
                     threads, syscall_rate, io_read_bps, io_write_bps)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


def backfill_syscall_rate(run_id: str, rate_by_ts: dict[float, float]) -> None:
    if not rate_by_ts:
        return
    with db.connect() as conn:
        conn.executemany(
            "UPDATE metrics SET syscall_rate = ? WHERE run_id = ? AND timestamp_ms = ?",
            [(rate, run_id, ts) for ts, rate in rate_by_ts.items()],
        )


def read_metrics(run_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM metrics WHERE run_id = ? ORDER BY timestamp_ms",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- SQLite: anomalies ------------------------------------------------------

def insert_anomalies(run_id: str, anomalies: Iterable[Anomaly]) -> None:
    rows = []
    for a in anomalies:
        rows.append((
            new_id(), run_id, a.rule_id, a.severity, a.severity_score,
            a.title, a.description, json.dumps(a.evidence_ids),
            a.first_seen_ms, a.last_seen_ms, a.occurrence_count,
        ))
    if rows:
        with db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO anomalies
                    (id, run_id, rule_id, severity, severity_score, title,
                     description, evidence_ids, first_seen_ms, last_seen_ms,
                     occurrence_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


def read_anomalies(run_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM anomalies WHERE run_id = ?
             ORDER BY severity_score DESC
            """,
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_ids"] = json.loads(d["evidence_ids"]) if d["evidence_ids"] else []
        out.append(d)
    return out


# --- SQLite: artifacts ------------------------------------------------------

def record_artifact(run_id: str, kind: str, path: str | Path) -> dict | None:
    """Record an on-disk artifact (size + sha256) for a run. Skips if missing."""
    p = Path(path)
    if not p.exists():
        return None
    size = p.stat().st_size
    sha = _sha256(p)
    aid = new_id()
    now = now_ms()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts
                (id, run_id, kind, path, size_bytes, sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, run_id, kind, str(p), size, sha, now),
        )
    return {"id": aid, "kind": kind, "path": str(p), "size_bytes": size,
            "sha256": sha, "created_at": now}


def read_artifacts(run_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- meta.json --------------------------------------------------------------

def write_meta(run_dir: str | Path, meta: dict) -> None:
    """Write the human-readable `meta.json` summary into the run folder."""
    p = Path(run_dir) / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
