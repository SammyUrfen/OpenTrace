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
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .. import db, runs, storage
from .. import perf as perf_mod
from .. import profile as profile_mod
from ..rules import RuleContext, run_rules
from ..streaming import broker
from ..util import new_id, now_ms
from . import metrics as metrics_mod
from . import ltrace_parser, strace_parser
from .events import EXIT, LIBCALL, SIGNAL, MetricSample, TraceEvent

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
    # attach runs: set when the target vanishes (or Stop is pressed), so the
    # profiler thread wraps up early (the poller must NOT finalize — it owns that).
    stop_event: threading.Event = field(default_factory=threading.Event)
    # monitor mode: keep the run live, capture rolling snapshots, emit incidents.
    monitor: bool = False
    latest_hot: dict | None = None   # {functions, stack} from the current snapshot
    incident_cooldown: dict = field(default_factory=dict)  # rule_id -> last-fired ts (dedup)
    pending_hot: list = field(default_factory=list)  # incident dicts awaiting a hot path
    # live-alert state
    last_rss: float | None = None
    cpu_streak: int = 0
    alerts_fired: set = field(default_factory=set)


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
    if data.collector_config is None:
        from .. import config
        data.collector_config = config.load().tracing.collectors.model_dump()
    run = runs.create(data)
    with _lock:
        _active[run.id] = _RunContext(run=run, created_ms=now_ms())
    broker.publish(run.id, "run_started", run.model_dump())
    log.info("run %s started: %s", run.id, run.command)
    return run


def _begin_polling(
    run: runs.Run, pid: int, descendants_only: bool, *, finalize_on_exhausted: bool = True,
) -> None:
    """Launch the psutil metrics poller for a run on `pid`. Idempotent per run.

    When the watched tree vanishes the poller fires `on_exhausted`. For the launch
    path that means auto-finalize (the wrapper died). For an ATTACH run the perf
    thread owns finalization, so the poller only signals `stop_event` — otherwise
    it would finalize on a `perf.data` perf is still writing and discard the real
    capture.
    """
    run_id = run.id
    with _lock:
        ctx = _active.get(run_id)
        if ctx is None:
            ctx = _RunContext(run=run)
            _active[run_id] = ctx
        if ctx.poller is not None:
            return  # already polling; ignore duplicate
        ctx.root_pid = pid
        if finalize_on_exhausted:
            on_exhausted = lambda rid=run_id: _auto_finalize(rid)  # noqa: E731
        else:
            on_exhausted = lambda ev=ctx.stop_event: ev.set()  # noqa: E731
        poller = metrics_mod.MetricsPoller(
            pid,
            on_sample=lambda s, rid=run_id: _on_sample(rid, s),
            on_exhausted=on_exhausted,
            descendants_only=descendants_only,
        )
        ctx.poller = poller
    poller.start()
    log.info("run %s polling pid tree under %d (descendants_only=%s)",
             run_id, pid, descendants_only)


def report_pid(run_id: str, pid: int) -> bool:
    run = runs.get(run_id)
    if run is None:
        return False
    collectors = run.collector_config or {}
    # No psutil collector -> acknowledge but don't poll metrics.
    if not collectors.get("psutil", True):
        return True
    # A wrapper (strace/ltrace/perf) is `pid`; the workload is its descendant, so
    # watch descendants only. Running bare, `pid` IS the workload — include root.
    descendants_only = (
        collectors.get("strace", True)
        or collectors.get("ltrace", False)
        or collectors.get("perf", False)
    )
    _begin_polling(run, pid, descendants_only)
    return True


# --- attach-to-running-PID (profiling Phase A) ------------------------------

# Bounded sampling window; clamped so a capture always self-terminates.
_ATTACH_MIN_S = 3
_ATTACH_MAX_S = 120
_PERF_HZ = 99


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def start_attach_run(
    pid: int, window_s: int = 20, session_id: str | None = None, monitor: bool = False,
) -> runs.Run:
    """Attach to an already-running process and profile it.

    Unlike the launch path (`otrace -- cmd`), no command is spawned: we create a
    run, watch the TARGET pid directly with psutil (it IS the workload, so
    descendants_only=False), and run the runtime's profiler off the request thread.
    Finalize reuses the profiler → flamegraph.json pipeline. Fail-open: if the
    profiler is missing or attach is denied, the run still completes with the
    psutil timeline.

    `monitor=True` keeps the run LIVE: continuous metrics + back-to-back profiling
    snapshots + sliding-window rule scans, emitting *incidents* (anomaly + when +
    where(hot path) + leading metrics) until Stop (`stop_monitor`) or target exit.
    """
    from .. import attach as attach_mod  # local import: avoids a cycle at import time

    if pid <= 0 or not psutil.pid_exists(pid):
        raise ValueError(f"no such process: {pid}")
    try:
        info = attach_mod.target_info(pid)
    except psutil.Error as e:
        raise ValueError(f"cannot inspect pid {pid}: {e}") from e

    window = max(_ATTACH_MIN_S, min(int(window_s), _ATTACH_MAX_S))
    # Pick the runtime's dedicated sampler if installed (Phase B), else perf.
    plan = attach_mod.profiler_plan(info["runtime"])
    if plan:
        profiler, prof_fmt, prof_file = plan["tool"], plan["format"], plan["out_file"]
    else:
        profiler, prof_fmt, prof_file = "perf", "perf", "perf.data"
    data = runs.RunCreate(
        command=info["cmdline"],
        cwd=_proc_cwd(pid),
        session_id=session_id,
        collector_config={
            "psutil": True, "perf": True, "attach": True, "monitor": monitor,
            "runtime": info["runtime"], "profiler": profiler,
            "profile_format": prof_fmt, "profile_file": prof_file,
        },
        label=f"{'monitor' if monitor else 'attach'}: {info['name']} (pid {pid})",
    )
    run = runs.create(data)
    with _lock:
        _active[run.id] = _RunContext(run=run, created_ms=now_ms(), monitor=monitor)
    broker.publish(run.id, "run_started", run.model_dump())
    log.info("%s run %s: pid=%d runtime=%s profiler=%s window=%ds",
             "monitor" if monitor else "attach", run.id, pid, info["runtime"], profiler, window)

    # psutil timeline on the target directly (include root — it's the workload).
    # finalize_on_exhausted=False: if the target dies the poller only signals
    # stop_event; the profiler thread remains the sole finalizer.
    _begin_polling(run, pid, descendants_only=False, finalize_on_exhausted=False)
    target = _run_attach_monitor if monitor else _run_attach_profile
    threading.Thread(target=target, args=(run.id, pid, window), daemon=True).start()
    return run


def stop_monitor(run_id: str) -> bool:
    """Ask a live monitor run to wrap up (its thread finalizes). Idempotent."""
    with _lock:
        ctx = _active.get(run_id)
    if ctx is None:
        return False
    ctx.stop_event.set()
    return True


def _perf_fail_reason(stderr: str, profiler: str = "perf") -> str:
    """A user-facing reason a profiler attach produced no flamegraph."""
    s = (stderr or "").lower()
    if "no such process" in s or "process ended" in s or "terminated" in s:
        return "the target exited before profiling could finish."
    if any(k in s for k in ("permission", "not permitted", "denied", "paranoid",
                            "ptrace", "operation not permitted", "capab", "eperm")):
        if profiler == "perf":
            return ("perf attach denied — raise privileges "
                    "(sudo sysctl kernel.perf_event_paranoid=1, or grant CAP_PERFMON).")
        return (f"{profiler} attach denied — needs same-user access "
                "(sudo sysctl kernel.yama.ptrace_scope=0, or run as the target's user).")
    if "version" in s and profiler in ("py-spy", "rbspy"):
        return f"{profiler} version mismatch with the target runtime — update {profiler}."
    return f"{profiler} captured no samples (target idle, or too short a window)."


def _fold_profile(fmt: str, raw: Path) -> dict | None:
    """Fold a profiler's raw output into a flamegraph dict, dispatching on format
    (perf.data / collapsed / speedscope). Returns None when the capture is
    missing/empty/unparseable so the run keeps just its psutil timeline."""
    if not raw.exists() or raw.stat().st_size == 0:
        return None
    try:
        if fmt == "collapsed":
            return perf_mod.fold_collapsed(raw.read_text(errors="replace"))
        if fmt == "speedscope":
            return perf_mod.fold_speedscope(json.loads(raw.read_text()))
        return perf_mod.build_flamegraph(raw)  # perf.data
    except Exception:  # noqa: BLE001
        log.exception("folding profile %s (format=%s) failed", raw, fmt)
        return None


def _capture_profile(run: runs.Run, pid: int, window_s: int, stop: threading.Event) -> tuple[bool, str | None]:
    """Run the run's chosen profiler ONCE for a bounded window into its output
    file. Returns (ok, failure_reason). Drives the window itself (so the psutil
    timeline is real even when the profiler can't attach) and cuts it short if the
    target vanishes; SIGINTs the profiler to flush, with a hard-kill watchdog."""
    from .. import attach as attach_mod

    collectors = run.collector_config or {}
    profiler = collectors.get("profiler", "perf")
    out_path = Path(run.run_dir) / collectors.get("profile_file", "perf.data")
    if profiler == "perf":
        cmd = ["perf", "record", "-p", str(pid), "-g", "-F", str(_PERF_HZ),
               "-o", str(out_path), "--", "sleep", str(window_s)]
    else:
        cmd = attach_mod.sampler_argv(profiler, pid, window_s, str(out_path))

    proc: subprocess.Popen | None = None
    reason: str | None = None
    if shutil.which(profiler):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:  # noqa: BLE001
            log.exception("attach %s failed to start for run %s", profiler, run.id)
            proc, reason = None, f"could not start {profiler}."
    else:
        reason = f"{profiler} is not installed — captured the resource timeline only."

    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if stop.is_set() or not psutil.pid_exists(pid):
            break
        time.sleep(0.2)

    ok = False
    if proc is not None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # graceful: flush the profiler output
        try:
            _out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
        ok = out_path.exists() and out_path.stat().st_size > 0
        if not ok:
            reason = _perf_fail_reason((err or b"").decode(errors="replace"), profiler)
            log.warning("attach run %s: no %s capture", run.id, profiler)
    return ok, reason


def _top_stack(tree: dict | None, max_depth: int = 12) -> list[str]:
    """The dominant root->leaf call path of a flame tree (children are value-sorted)
    — the 'which classes/functions' for an incident."""
    if not tree:
        return []
    path: list[str] = []
    node = tree
    while node.get("children") and len(path) < max_depth:
        node = node["children"][0]
        path.append(node["name"])
    return path


def _refresh_flamegraph(run: runs.Run) -> dict | None:
    """Fold the current profiler output → write flamegraph.json → update the live
    context's `latest_hot` (functions + dominant stack) for incident attribution."""
    collectors = run.collector_config or {}
    fmt = collectors.get("profile_format", "perf")
    out_path = Path(run.run_dir) / collectors.get("profile_file", "perf.data")
    fg = _fold_profile(fmt, out_path)
    if fg and fg.get("supported"):
        storage.write_json(Path(run.run_dir) / "flamegraph.json", fg)
        hot = {
            "functions": [h["function"] for h in (fg.get("hotspots") or [])[:5]],
            "stack": _top_stack(fg.get("tree")),
            "samples": fg.get("samples", 0),
        }
        # Set latest_hot + take the pending backfill list atomically (the poller
        # thread appends incidents to pending_hot under the same lock).
        with _lock:
            ctx = _active.get(run.id)
            if ctx is not None:
                ctx.latest_hot = hot
                pending, ctx.pending_hot = ctx.pending_hot, []
            else:
                pending = []
        # Backfill the "where" for incidents that fired before a snapshot existed
        # (e.g. a CPU spike in the first ~2s), then run their AI now that we know it.
        for inc in pending:
            inc["hot"] = hot
            try:
                storage.update_incident(run.run_dir, inc["id"], hot=hot)
            except Exception:  # noqa: BLE001
                pass
            broker.publish(run.id, "incident_update", {"id": inc["id"], "hot": hot})
            _maybe_incident_ai(run, inc)
        broker.publish(run.id, "profile_updated", {"samples": fg.get("samples", 0)})
    return fg


def _ensure_flamegraph_reason(run_dir: Path, reason: str | None) -> None:
    """Guarantee the Flamegraph tab has an explanation when there's no usable
    profile; never clobber a real flamegraph or a fold's own (more specific) reason."""
    fg_path = run_dir / "flamegraph.json"
    fg = None
    if fg_path.exists():
        try:
            fg = json.loads(fg_path.read_text())
        except Exception:  # noqa: BLE001
            fg = None
    if fg is None or (not fg.get("supported") and not fg.get("reason")):
        try:
            storage.write_json(fg_path, {
                "supported": False, "samples": 0, "tree": None, "hotspots": [],
                "reason": reason or "no CPU profile was produced.",
            })
        except Exception:  # noqa: BLE001
            log.debug("could not write flamegraph reason", exc_info=True)


def _run_attach_profile(run_id: str, pid: int, window_s: int) -> None:
    """Single-shot attach: one profiling window, then finalize (sole finalizer)."""
    run = runs.get(run_id)
    if run is None:
        return
    with _lock:
        ctx = _active.get(run_id)
    stop = ctx.stop_event if ctx else threading.Event()
    ok, reason = _capture_profile(run, pid, window_s, stop)
    end_run(run_id, exit_code=0 if ok else None, exit_signal=None, ended_at=None)
    _ensure_flamegraph_reason(Path(run.run_dir), reason)


def _run_attach_monitor(run_id: str, pid: int, window_s: int) -> None:
    """Monitor mode: keep the run live — back-to-back profiling snapshots (each
    refreshes the flamegraph + hot path) + sliding-window rule scans that emit
    incidents — until Stop (stop_event) or the target exits. Sole finalizer."""
    run = runs.get(run_id)
    if run is None:
        return
    with _lock:
        ctx = _active.get(run_id)
    if ctx is None:
        return
    stop = ctx.stop_event
    reason = None
    while not stop.is_set() and psutil.pid_exists(pid):
        ok, reason = _capture_profile(run, pid, window_s, stop)
        if ok:
            _refresh_flamegraph(run)
        _eval_sliding_rules(ctx)
    end_run(run_id, exit_code=0, exit_signal=None, ended_at=None)
    _ensure_flamegraph_reason(Path(run.run_dir), reason)


# --- live incidents (monitor mode) ------------------------------------------

_INCIDENT_WINDOW_MS = 30_000   # leading metric context stored per incident
_SLIDING_N = 360               # trailing samples (~90s at 250ms) for rule scans
_MAX_LIVE_SAMPLES = 2400       # ring-buffer cap on in-memory samples (~10min)
_INCIDENT_COOLDOWN_MS = 20_000  # per-rule dedup: re-fire only after this quiet gap


def _make_incident(ctx: _RunContext, rule_id: str, severity: str, title: str, ts: float) -> None:
    """Record + stream an incident: anomaly + when + where (hot path) + leading
    metric context. Monitor runs only. De-duped per rule by a cooldown (so a fast-
    growing metric can't spam an incident every sample, yet a recovered-then-
    recurring condition still re-fires)."""
    if not ctx.monitor:
        return
    with _lock:  # cooldown check + snapshot shared state atomically
        last = ctx.incident_cooldown.get(rule_id)
        if last is not None and ts - last < _INCIDENT_COOLDOWN_MS:
            return
        ctx.incident_cooldown[rule_id] = ts
        hot = ctx.latest_hot
        samples = list(ctx.samples)
    metrics = [s.to_ndjson() for s in samples if abs(s.timestamp_ms - ts) <= _INCIDENT_WINDOW_MS]
    incident = {
        "id": new_id(), "run_id": ctx.run.id, "ts": ts, "rule_id": rule_id,
        "severity": severity, "title": title, "hot": hot, "metrics": metrics, "ai": None,
    }
    if hot is None:  # no profile yet — queue for the "where" backfill on the next snapshot
        with _lock:
            ctx.pending_hot.append(incident)
    try:
        storage.append_incident(ctx.run.run_dir, incident)
    except Exception:  # noqa: BLE001
        log.debug("append_incident failed for %s", ctx.run.id, exc_info=True)
    broker.publish(ctx.run.id, "incident", incident)
    if hot is not None:  # AI now if we know the "where"; else after the backfill
        _maybe_incident_ai(ctx.run, incident)


def _maybe_incident_ai(run: runs.Run, incident: dict) -> None:
    """If continuous AI is enabled + an LLM is configured, generate a short
    plain-English explanation for the incident (best-effort, off the poller
    thread) and publish/persist it. No-op otherwise."""
    try:
        from .. import config, llm
        if not getattr(config.load().llm, "continuous_summaries", False):
            return
        if not llm.is_configured():
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(
        target=_incident_ai_worker, args=(run.id, run.run_dir, incident), daemon=True,
    ).start()


# Cap concurrent incident-AI requests so a burst can't spawn unbounded LLM calls.
_AI_SEM = threading.Semaphore(2)


def _incident_ai_worker(run_id: str, run_dir: str, incident: dict) -> None:
    from .. import summarize
    if not _AI_SEM.acquire(blocking=False):
        return  # already at the concurrency cap — skip this one's AI note
    try:
        text = summarize.incident_summary(incident)
    except Exception:  # noqa: BLE001
        log.debug("incident AI failed for %s", run_id, exc_info=True)
        return
    finally:
        _AI_SEM.release()
    if not text:
        return
    try:
        storage.update_incident(run_dir, incident["id"], ai=text)
    except Exception:  # noqa: BLE001
        log.debug("persist incident AI failed for %s", run_id, exc_info=True)
    broker.publish(run_id, "incident_ai", {"id": incident["id"], "ai": text})


def _eval_sliding_rules(ctx: _RunContext) -> None:
    """Run the metric-based rules over the trailing window so sustained conditions
    (memory growth, sustained CPU) surface as incidents while the run is live."""
    with _lock:  # snapshot — the poller thread appends/trims ctx.samples
        window = ctx.samples[-_SLIDING_N:]
    if len(window) < 8:
        return
    span = window[-1].timestamp_ms - window[0].timestamp_ms
    rctx = RuleContext(
        events=[],
        metrics=[s.to_ndjson() for s in window],
        duration_ms=int(span) or None,
        cpu_cores=metrics_mod.cpu_count(),
    )
    try:
        found = run_rules(rctx)
    except Exception:  # noqa: BLE001
        log.debug("sliding rules failed for %s", ctx.run.id, exc_info=True)
        return
    for a in found:
        # cooldown-deduped inside _make_incident (re-fires after a quiet gap)
        _make_incident(ctx, a.rule_id, a.severity, a.title, window[-1].timestamp_ms)


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
            # ring-buffer cap so a long-lived monitor run can't grow unbounded
            # (the full metric stream is persisted to the DB regardless).
            if len(ctx.samples) > _MAX_LIVE_SAMPLES:
                del ctx.samples[:-_MAX_LIVE_SAMPLES]
    try:
        storage.insert_metric(run_id, sample)
    except Exception:  # noqa: BLE001
        log.debug("metric insert failed for %s", run_id, exc_info=True)
    broker.publish(run_id, "metric", sample.to_ndjson())
    if ctx is not None:
        _live_detect(ctx, sample)


# Live-alert thresholds (cheap checks on the metric stream during a run).
_FD_ALERT = 200
_RSS_SPIKE_MB = 100.0
_CPU_HOT = 90.0          # raw % ~= one full core
_CPU_STREAK = 8          # ~2s sustained


def _live_detect(ctx: _RunContext, sample: MetricSample) -> None:
    """Emit `anomaly_alert` SSE events from metric thresholds as a run unfolds, so
    the Live Monitor can warn the developer before the run even finishes. For a
    monitor run these also become *incidents* (with when/where/leading-metrics)."""
    rid = ctx.run.id
    ts = sample.timestamp_ms

    def emit(rule_id: str, severity: str, title: str) -> None:
        broker.publish(rid, "anomaly_alert", {
            "severity": severity, "title": title, "timestamp_ms": ts,
        })
        _make_incident(ctx, rule_id, severity, title, ts)  # no-op unless monitor

    def once(key: str, rule_id: str, severity: str, title: str) -> None:
        if key not in ctx.alerts_fired:
            ctx.alerts_fired.add(key)
            emit(rule_id, severity, title)

    if sample.open_fds is not None and sample.open_fds > _FD_ALERT:
        once("fd", "fd_leak_live", "high", f"Open file descriptors exceed {_FD_ALERT} "
                                           f"({sample.open_fds}) — possible leak")
    if sample.rss_mb is not None:
        if ctx.last_rss is not None and sample.rss_mb - ctx.last_rss > _RSS_SPIKE_MB:
            # incidents are cooldown-deduped in _make_incident (no per-sample spam)
            emit("mem_spike", "medium",
                 f"Memory spiked +{sample.rss_mb - ctx.last_rss:.0f}MB (now {sample.rss_mb:.0f}MB)")
        ctx.last_rss = sample.rss_mb
    if sample.cpu_pct is not None and sample.cpu_pct > _CPU_HOT:
        ctx.cpu_streak += 1
        if ctx.cpu_streak == _CPU_STREAK:
            once("cpu", "cpu_hot_live", "medium", "CPU pegged for ~2s — compute-bound")
    elif sample.cpu_pct is not None:
        ctx.cpu_streak = 0


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
    # Stop a live monitor loop too — otherwise a finalize from elsewhere (e.g. a
    # generic POST /runs/{id}/end) leaves the monitor thread spawning profilers and
    # appending incidents to an already-completed run.
    ctx.stop_event.set()
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
    collectors = run.collector_config or {}
    use_ltrace = collectors.get("ltrace", False)

    # ltrace mode replaces strace as the ptrace backend; its log is a superset
    # (library calls + @SYS syscalls), so the syscall pipeline still works.
    if use_ltrace:
        trace_log = run_dir / "ltrace.log"
        trace_kind = "ltrace-log"
        events: list[TraceEvent] = (
            list(ltrace_parser.parse_file(trace_log)) if trace_log.exists() else []
        )
    else:
        trace_log = run_dir / "strace.log"
        trace_kind = "strace-log"
        events = (
            list(strace_parser.parse_file(trace_log)) if trace_log.exists() else []
        )

    # Full event stream -> compressed ndjson (source of truth for replay).
    storage.write_ndjson_zst(
        run_dir / "events.ndjson.zst", (e.to_ndjson() for e in events)
    )

    # Syscall-oriented analysis (rate, rules, summary) must NOT see ltrace
    # LIBCALL events: they're profiled separately and would otherwise inflate the
    # syscall rate, mislabel slow library calls as slow syscalls, and double-count
    # libc-wrapper names (read/write/open) in the storm rules. No-op for strace.
    syscall_events = [e for e in events if e.event_type != LIBCALL]

    # Metrics from DB (live inserts), then derive + backfill syscall_rate.
    metrics_rows = storage.read_metrics(run.id)
    rate_by_ts = _syscall_rate_by_sample(syscall_events, metrics_rows)
    storage.backfill_syscall_rate(run.id, rate_by_ts)
    for m in metrics_rows:
        if m["timestamp_ms"] in rate_by_ts:
            m["syscall_rate"] = rate_by_ts[m["timestamp_ms"]]
    storage.write_ndjson_zst(run_dir / "metrics.ndjson.zst", iter(metrics_rows))

    # Detect anomalies (on syscalls/signals/exits only).
    rctx = RuleContext(
        events=syscall_events,
        metrics=metrics_rows,
        duration_ms=run.duration_ms,
        cpu_cores=metrics_mod.cpu_count(),
    )
    anomalies = run_rules(rctx)

    # Phase-6 profiling artifacts (only for the collectors that ran).
    if use_ltrace and events:
        event_dicts = [e.to_ndjson() for e in events]
        prof = profile_mod.malloc_profile(event_dicts)
        storage.write_json(run_dir / "profile.json", {
            "malloc": prof,
            "hotspots": profile_mod.libcall_stats(event_dicts),
        })
        storage.record_artifact(run.id, "profile", run_dir / "profile.json")
        anomalies.extend(profile_mod.profile_anomalies(prof, run.duration_ms))

    if collectors.get("perf", False):
        fmt = collectors.get("profile_format", "perf")
        raw = run_dir / collectors.get("profile_file", "perf.data")
        flamegraph = _fold_profile(fmt, raw)
        if flamegraph is not None:
            storage.write_json(run_dir / "flamegraph.json", flamegraph)
            storage.record_artifact(run.id, "flamegraph", run_dir / "flamegraph.json")
            storage.record_artifact(run.id, "perf-data" if fmt == "perf" else "profile", raw)
            anomalies.extend(perf_mod.perf_anomalies(flamegraph))

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
        (trace_kind, trace_log),
        ("events", run_dir / "events.ndjson.zst"),
        ("metrics", run_dir / "metrics.ndjson.zst"),
    ):
        storage.record_artifact(run.id, kind, path)

    # Human-readable meta.json summary (syscall totals exclude library calls).
    summary = _summary(run, syscall_events, metrics_rows, anomalies, exit_code, exit_signal)
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
