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
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Iterator

import zstandard as zstd

from . import db
from .rules.custom import CustomRuleDef
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
              FROM events WHERE run_id = ? AND event_type != 'request'
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


def insert_request_spans(run_id: str, rows: Iterable[dict]) -> None:
    """Persist CURATED request spans into `events` with `event_type='request'` (roadmap
    §3.5 Phase-2 curated write). Each row is {timestamp_ms (EPOCH ms — converted from the
    span's CLOCK_MONOTONIC nsecs via the child-launch anchor, §2.6), pid, payload}. Kept
    separate from insert_events, and read_events excludes event_type='request', so the
    syscall aggregations (syscall_stats/io_stats, which filter event_type=='syscall') and
    the raw Events tab stay untouched. read_request_spans is the dedicated reader."""
    out = []
    for r in rows:
        out.append((
            new_id(), run_id, r["timestamp_ms"], "bpftrace", "request", r.get("pid"),
            json.dumps(r["payload"], separators=(",", ":")).encode(),
        ))
    if out:
        with db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO events
                    (id, run_id, timestamp_ms, source, event_type, pid, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                out,
            )


def read_request_spans(run_id: str, limit: int = 500, since_ms: float | None = None,
                       until_ms: float | None = None) -> list[dict]:
    """Curated request spans for a run (event_type='request'), newest first, epoch-
    timestamped so they time-correlate with metrics/incidents. Optional [since_ms, until_ms]
    window (for an incident-evidence / timeline overlay)."""
    q = ["SELECT timestamp_ms, pid, payload FROM events WHERE run_id = ? AND event_type = 'request'"]
    params: list = [run_id]
    if since_ms is not None:
        q.append("AND timestamp_ms >= ?")
        params.append(since_ms)
    if until_ms is not None:
        q.append("AND timestamp_ms <= ?")
        params.append(until_ms)
    q.append("ORDER BY timestamp_ms DESC LIMIT ?")
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(" ".join(q), params).fetchall()
    out = []
    for r in rows:
        payload = json.loads(bytes(r["payload"]).decode())
        out.append({"timestamp_ms": r["timestamp_ms"], "pid": r["pid"], **payload})
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


def read_metrics(run_id: str, max_points: int | None = None) -> list[dict]:
    """Read a run's metric samples, oldest first.

    `max_points=None` returns the complete stream (what finalize/summarize need
    — exact timestamps for the syscall_rate backfill and the full archive).
    When set, rows are STRIDE-downsampled (every Nth real row, never averaged,
    so spikes and exact timestamps survive) to at most ~max_points."""
    with db.connect() as conn:
        if max_points is not None and max_points > 0:
            total = conn.execute(
                "SELECT COUNT(*) FROM metrics WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            stride = -(-total // max_points)  # ceil
            if stride > 1:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT m.*, ROW_NUMBER() OVER (ORDER BY timestamp_ms) - 1 AS rn
                          FROM metrics m WHERE run_id = ?
                    ) WHERE rn % ? = 0 ORDER BY timestamp_ms
                    """,
                    (run_id, stride),
                ).fetchall()
                return [{k: r[k] for k in r.keys() if k != "rn"} for r in rows]
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


def write_json(path: str | Path, doc: dict) -> None:
    """Write a derived JSON artifact (e.g. profile.json, flamegraph.json) atomically
    (temp + os.replace) so a concurrent reader (a GET, or a monitor re-write) never
    sees a truncated/torn file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
    os.replace(tmp, p)


# --- monitor-mode incidents (ndjson in the run dir) -------------------------
# All access is serialized: append (poller/monitor threads), full-file rewrite
# (backfill + AI worker threads), and read (request thread) run concurrently, so
# without this lock a truncating rewrite races an append → torn/lost lines.
_incidents_lock = threading.Lock()


def _incidents_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "incidents.ndjson"


def _read_incidents_unlocked(run_dir: str | Path) -> list[dict]:
    p = _incidents_path(run_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_incident(run_dir: str | Path, incident: dict) -> None:
    p = _incidents_path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _incidents_lock, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(incident, separators=(",", ":"), default=str) + "\n")


def _collapse_legacy_incidents(incidents: list[dict]) -> list[dict] | None:
    """Group pre-collapse incident rows (a `rule_id` but no `count` field, one row
    per re-fire) into one collapsed row per rule. Rows without a rule_id and
    already-collapsed rows pass through untouched. None when nothing is legacy."""
    if not any(i.get("rule_id") and "count" not in i for i in incidents):
        return None
    by_rule: dict[str, dict] = {}
    out: list[dict] = []
    for inc in incidents:
        rid = inc.get("rule_id")
        if not rid or "count" in inc:
            out.append(inc)
            continue
        ts = inc.get("ts")
        cur = by_rule.get(rid)
        if cur is None:
            cur = dict(inc)
            cur.update(count=1, first_ts=ts, last_ts=ts)
            by_rule[rid] = cur
            out.append(cur)
            continue
        cur["count"] += 1
        if ts is not None:
            if cur.get("first_ts") is None or ts < cur["first_ts"]:
                cur["first_ts"] = ts
                cur["ts"] = ts
            if cur.get("last_ts") is None or ts > cur["last_ts"]:
                cur["last_ts"] = ts
        if _SEV_RANK.get(inc.get("severity"), 99) < _SEV_RANK.get(cur.get("severity"), 99):
            cur["severity"] = inc["severity"]
        for k in ("hot", "ai"):
            if inc.get(k) is not None:
                cur[k] = inc[k]
        if inc.get("metrics"):
            cur["metrics"] = inc["metrics"]
    return out


def _rewrite_incidents_unlocked(run_dir: str | Path, incidents: list[dict]) -> None:
    p = _incidents_path(run_dir)
    tmp = p.with_suffix(".ndjson.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for inc in incidents:
            f.write(json.dumps(inc, separators=(",", ":"), default=str) + "\n")
    os.replace(tmp, p)


def read_incidents(run_dir: str | Path, *, compact_legacy: bool = False) -> list[dict]:
    """Read a run's incidents. With `compact_legacy` (finished runs only), rows
    written by pre-collapse builds (one per re-fire, each embedding a full metrics
    window — multi-MB files) are collapsed one-time to one row per rule and the
    file rewritten; idempotent thereafter."""
    with _incidents_lock:
        incidents = _read_incidents_unlocked(run_dir)
        if compact_legacy:
            compacted = _collapse_legacy_incidents(incidents)
            if compacted is not None:
                try:
                    _rewrite_incidents_unlocked(run_dir, compacted)
                except OSError:
                    pass  # serve the compacted view even if the rewrite failed
                incidents = compacted
    return incidents


def update_incident(run_dir: str | Path, incident_id: str, **fields) -> None:
    """Patch fields on a stored incident (e.g. ai=..., hot=...). Atomic rewrite
    (temp + os.replace) under the incidents lock so concurrent appends/updates
    can't tear or lose data."""
    with _incidents_lock:
        incidents = _read_incidents_unlocked(run_dir)
        changed = False
        for inc in incidents:
            if inc.get("id") == incident_id:
                inc.update(fields)
                changed = True
        if not changed:
            return
        _rewrite_incidents_unlocked(run_dir, incidents)


# --- SQLite: custom rules (user-authored, Settings -> Rules) ----------------

def _row_to_custom_rule(r: sqlite3.Row) -> CustomRuleDef:
    return CustomRuleDef(
        id=r["id"], name=r["name"], description=r["description"],
        signal=r["signal"], expression=r["expression"], severity=r["severity"],
        enabled=bool(r["enabled"]), min_count=r["min_count"],
        duration_ms=r["duration_ms"], created_at=r["created_at"],
    )


def list_custom_rules() -> list[CustomRuleDef]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM custom_rules ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_custom_rule(r) for r in rows]


def get_custom_rule(rule_id: str) -> CustomRuleDef | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM custom_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    return _row_to_custom_rule(row) if row else None


def create_custom_rule(
    *, name: str, description: str, signal: str, expression: str,
    severity: str = "medium", enabled: bool = True,
    min_count: int = 5, duration_ms: int = 5000,
) -> CustomRuleDef:
    rule = CustomRuleDef(
        id=new_id(), name=name, description=description, signal=signal,
        expression=expression, severity=severity, enabled=enabled,
        min_count=min_count, duration_ms=duration_ms, created_at=now_ms(),
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO custom_rules
                (id, name, description, signal, expression, severity, enabled,
                 min_count, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rule.id, rule.name, rule.description, rule.signal, rule.expression,
             rule.severity, int(rule.enabled), rule.min_count, rule.duration_ms,
             rule.created_at),
        )
    return rule


def update_custom_rule(rule_id: str, **fields) -> CustomRuleDef | None:
    """Patch a subset of columns (name/description/signal/expression/severity/
    enabled/min_count/duration_ms). Returns the updated row, or None if it
    doesn't exist."""
    cols = {
        "name", "description", "signal", "expression", "severity",
        "enabled", "min_count", "duration_ms",
    }
    sets = {k: v for k, v in fields.items() if k in cols and v is not None}
    if not sets:
        return get_custom_rule(rule_id)
    if "enabled" in sets:
        sets["enabled"] = int(sets["enabled"])
    with db.connect() as conn:
        conn.execute(
            f"UPDATE custom_rules SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), rule_id),
        )
    return get_custom_rule(rule_id)


def delete_custom_rule(rule_id: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM custom_rules WHERE id = ?", (rule_id,))
    return cur.rowcount > 0
