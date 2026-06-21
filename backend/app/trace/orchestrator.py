"""Run lifecycle orchestration — the trace engine's control plane.

Wraps the HTTP handshake the shell wrapper performs:

    POST /runs/start   -> start_run()    creates the run row + dir, opens an SSE
                                          channel; returns run + strace_log path.
    POST /runs/{id}/pid-> report_pid()   the wrapper reports strace's PID; we
                                          launch the psutil poller on its subtree.
    POST /runs/{id}/end-> end_run()      stop polling, then finalize: parse
                                          strace.log, derive metrics, run rules,
                                          write derived files, stamp the run.

Live metric samples and lifecycle transitions are pushed to the renderer via
`streaming.broker`. Everything heavy happens off the request thread (the poller
has its own thread; finalize runs inline on the /end request, which is cheap for
typical runs and keeps ordering simple).

Public surface:
- `start_run(data) -> Run`
- `report_pid(run_id, pid) -> bool`
- `end_run(run_id, *, exit_code, exit_signal, ended_at) -> Run | None`
- `reconcile_orphans() -> int`  (startup cleanup of interrupted runs)
"""
from __future__ import annotations

import bisect
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import db, runs, storage
from ..rules import RuleContext, run_rules
from ..streaming import broker
from ..util import now_ms
from . import metrics as metrics_mod
from . import strace_parser
from .events import EXIT, SIGNAL, MetricSample, TraceEvent

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_MS = 250.0
# A context that got /start but never a /pid (its otrace died instantly) is
# swept after this long so `_active` can't grow without bound.
_NO_POLLER_TTL_MS = 600_000


@dataclass
class _RunContext:
    run: runs.Run
    poller: metrics_mod.MetricsPoller | None = None
    samples: list[MetricSample] = field(default_factory=list)
    root_pid: int | None = None
    created_ms: int = 0


_active: dict[str, _RunContext] = {}
_lock = threading.Lock()


def _sweep_stale() -> None:
    """Drop never-polled contexts whose otrace died before reporting a pid."""
    cutoff = now_ms() - _NO_POLLER_TTL_MS
    stale: list[str] = []
    with _lock:
        for rid, ctx in list(_active.items()):
            if ctx.poller is None and ctx.created_ms and ctx.created_ms < cutoff:
                _active.pop(rid, None)
                stale.append(rid)
    for rid in stale:
        runs.set_status(rid, runs.ERROR)
        log.warning("swept stale run context %s (no pid reported)", rid)


# --- lifecycle --------------------------------------------------------------

def start_run(data: runs.RunCreate) -> runs.Run:
    _sweep_stale()
    run = runs.create(data)
    with _lock:
        _active[run.id] = _RunContext(run=run, created_ms=now_ms())
    broker.publish(run.id, "run_started", run.model_dump())
    log.info("run %s started: %s", run.id, run.command)
    return run


def report_pid(run_id: str, pid: int) -> bool:
    run = runs.get(run_id)
    if run is None:
        return False
    with _lock:
        ctx = _active.get(run_id)
        if ctx is None:
            ctx = _RunContext(run=run)
            _active[run_id] = ctx
        if ctx.poller is not None:
            return True  # already polling; ignore duplicate report
        ctx.root_pid = pid
        poller = metrics_mod.MetricsPoller(
            pid,
            on_sample=lambda s, rid=run_id: _on_sample(rid, s),
            on_exhausted=lambda rid=run_id: _auto_finalize(rid),
        )
        ctx.poller = poller
    poller.start()
    log.info("run %s polling pid tree under %d", run_id, pid)
    return True


def _auto_finalize(run_id: str) -> None:
    """Finalize a run whose process tree vanished without a `/runs/end` call
    (its otrace was killed). Runs on the poller thread, so it must NOT try to
    stop/join that same thread."""
    log.warning("run %s: tree gone without /end — auto-finalizing", run_id)
    end_run(run_id, exit_code=None, exit_signal=None, ended_at=None,
            _stop_poller=False)


def _on_sample(run_id: str, sample: MetricSample) -> None:
    with _lock:
        ctx = _active.get(run_id)
        if ctx is not None:
            ctx.samples.append(sample)
    try:
        storage.insert_metric(run_id, sample)
    except Exception:  # noqa: BLE001
        log.debug("metric insert failed for %s", run_id, exc_info=True)
    broker.publish(run_id, "metric", sample.to_ndjson())


def end_run(
    run_id: str,
    *,
    exit_code: int | None = None,
    exit_signal: str | None = None,
    ended_at: int | None = None,
    _stop_poller: bool = True,
) -> runs.Run | None:
    run = runs.get(run_id)
    if run is None:
        return None
    with _lock:
        ctx = _active.pop(run_id, None)
    if ctx is None:
        # Already finalized (e.g. by _auto_finalize racing a real /end).
        return run
    if ctx.poller is not None and _stop_poller:
        # One last sample to catch end-of-run state, then stop the thread.
        try:
            final_sample = ctx.poller.sample_now()
            if final_sample.rss_mb is not None:
                _on_sample(run_id, final_sample)
        except Exception:  # noqa: BLE001
            pass
        ctx.poller.stop()

    runs.set_status(run_id, runs.ANALYZING)
    broker.publish(run_id, "run_analyzing", {"id": run_id})
    try:
        final = _finalize(run, ctx, exit_code, exit_signal, ended_at)
    except Exception:  # noqa: BLE001
        log.exception("finalize failed for run %s", run_id)
        final = runs.finalize(
            run_id, ended_at=ended_at, exit_code=exit_code,
            exit_signal=exit_signal, status=runs.ERROR,
        )
    broker.publish(run_id, "run_ended", final.model_dump() if final else {"id": run_id})
    log.info("run %s finalized (severity=%s)", run_id, final.max_severity if final else "?")
    return final


# --- finalize ---------------------------------------------------------------

def _finalize(
    run: runs.Run,
    ctx: _RunContext | None,
    exit_code: int | None,
    exit_signal: str | None,
    ended_at: int | None,
) -> runs.Run | None:
    run_dir = Path(run.run_dir)
    strace_log = run_dir / "strace.log"

    events: list[TraceEvent] = (
        list(strace_parser.parse_file(strace_log)) if strace_log.exists() else []
    )

    # Full event stream -> compressed ndjson (source of truth for replay).
    storage.write_ndjson_zst(
        run_dir / "events.ndjson.zst", (e.to_ndjson() for e in events)
    )

    # Metrics from DB (live inserts), then derive + backfill syscall_rate.
    metrics_rows = storage.read_metrics(run.id)
    rate_by_ts = _syscall_rate_by_sample(events, metrics_rows)
    storage.backfill_syscall_rate(run.id, rate_by_ts)
    for m in metrics_rows:
        if m["timestamp_ms"] in rate_by_ts:
            m["syscall_rate"] = rate_by_ts[m["timestamp_ms"]]
    storage.write_ndjson_zst(run_dir / "metrics.ndjson.zst", iter(metrics_rows))

    # Detect anomalies.
    rctx = RuleContext(
        events=events,
        metrics=metrics_rows,
        duration_ms=run.duration_ms,
        cpu_cores=metrics_mod.cpu_count(),
    )
    anomalies = run_rules(rctx)

    # Persist a curated subset of events (+ anomaly evidence), then link ids.
    curated = _curate_events(events, anomalies)
    ids = storage.insert_events(run.id, curated)
    idmap = {id(ev): eid for ev, eid in zip(curated, ids)}
    for a in anomalies:
        a.evidence_ids = [idmap[id(ev)] for ev in a.evidence if id(ev) in idmap]
    storage.insert_anomalies(run.id, anomalies)
    severity = storage.max_severity(a.severity for a in anomalies)

    # Register artifacts (raw + derived).
    for kind, path in (
        ("strace-log", strace_log),
        ("events", run_dir / "events.ndjson.zst"),
        ("metrics", run_dir / "metrics.ndjson.zst"),
    ):
        storage.record_artifact(run.id, kind, path)

    # Human-readable meta.json summary.
    summary = _summary(run, events, metrics_rows, anomalies, exit_code, exit_signal)
    storage.write_meta(run_dir, summary)
    storage.record_artifact(run.id, "meta", run_dir / "meta.json")

    return runs.finalize(
        run.id,
        ended_at=ended_at,
        exit_code=exit_code,
        exit_signal=exit_signal,
        status=runs.COMPLETED,
        max_severity=severity,
    )


def _syscall_rate_by_sample(
    events: list[TraceEvent], metrics_rows: list[dict]
) -> dict[float, float]:
    """syscalls/sec for each metric sample, binned into the metric timeline."""
    syscall_ts = sorted(
        e.timestamp_ms for e in events if e.event_type not in (SIGNAL, EXIT)
    )
    if not syscall_ts or not metrics_rows:
        return {}
    sample_ts = sorted(m["timestamp_ms"] for m in metrics_rows)
    out: dict[float, float] = {}
    for i, t in enumerate(sample_ts):
        t0 = sample_ts[i - 1] if i > 0 else t - _DEFAULT_INTERVAL_MS
        dt = (t - t0) / 1000.0
        if dt <= 0:
            continue
        lo = bisect.bisect_right(syscall_ts, t0)
        hi = bisect.bisect_right(syscall_ts, t)
        out[t] = round((hi - lo) / dt, 2)
    return out


# event_types worth keeping in SQLite even when they aren't anomaly evidence.
_SLOW_MS = 100.0
_MAX_CURATED = 3000


def _curate_events(
    events: list[TraceEvent], anomalies: list
) -> list[TraceEvent]:
    """Bounded, de-duplicated set of 'interesting' events for fast querying.

    The full stream lives in events.ndjson.zst; SQLite only needs the events a
    user (or the timeline) will actually jump to: anomaly evidence, lifecycle
    (signals/exits/exec), slow calls, and app-level errors.
    """
    seen: set[int] = set()
    out: list[TraceEvent] = []

    def add(ev: TraceEvent) -> None:
        if id(ev) not in seen and len(out) < _MAX_CURATED:
            seen.add(id(ev))
            out.append(ev)

    for a in anomalies:
        for ev in a.evidence:
            add(ev)
    for ev in events:
        if ev.event_type in (SIGNAL, EXIT):
            add(ev)
        elif ev.syscall in ("execve", "execveat"):
            add(ev)
        elif ev.latency_ms is not None and ev.latency_ms > _SLOW_MS:
            add(ev)
        elif ev.error is not None and not _is_lib(ev.path):
            add(ev)
    out.sort(key=lambda e: e.timestamp_ms)
    return out


def _is_lib(path: str | None) -> bool:
    if not path:
        return False
    return any(h in path for h in (".so", "/lib/", "/usr/lib", "/etc/ld.so"))


def _summary(
    run: runs.Run,
    events: list[TraceEvent],
    metrics_rows: list[dict],
    anomalies: list,
    exit_code: int | None,
    exit_signal: str | None,
) -> dict:
    syscalls = [e for e in events if e.event_type not in (SIGNAL, EXIT)]
    errors = [e for e in syscalls if e.error is not None]
    top = Counter(e.syscall for e in syscalls if e.syscall).most_common(10)

    def peak(key: str) -> float | None:
        vals = [m[key] for m in metrics_rows if m.get(key) is not None]
        return round(max(vals), 3) if vals else None

    def avg(key: str) -> float | None:
        vals = [m[key] for m in metrics_rows if m.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "run_id": run.id,
        "command": run.command,
        "cwd": run.cwd,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "totals": {
            "syscall_events": len(syscalls),
            "errors": len(errors),
            "signals": sum(1 for e in events if e.event_type == SIGNAL),
            "metric_samples": len(metrics_rows),
            "top_syscalls": top,
        },
        "peaks": {
            "rss_mb": peak("rss_mb"),
            "cpu_pct": peak("cpu_pct"),
            "open_fds": peak("open_fds"),
            "threads": peak("threads"),
        },
        "averages": {
            "cpu_pct": avg("cpu_pct"),
            "rss_mb": avg("rss_mb"),
        },
        "anomalies": [
            {"rule_id": a.rule_id, "severity": a.severity, "title": a.title}
            for a in anomalies
        ],
        "max_severity": storage.max_severity(a.severity for a in anomalies),
    }


# --- startup reconciliation -------------------------------------------------

def reconcile_orphans() -> int:
    """Mark runs left mid-flight by a previous backend process as errored.

    Their in-memory poller/context died with that process, so they can never
    complete on their own. Returns the number reconciled.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM runs WHERE status IN (?, ?)",
            (runs.RUNNING, runs.ANALYZING),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for rid in ids:
            conn.execute(
                "UPDATE runs SET status = ? WHERE id = ?", (runs.ERROR, rid)
            )
    if ids:
        log.warning("reconciled %d orphaned run(s) to error", len(ids))
    return len(ids)
