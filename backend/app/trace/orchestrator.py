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
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .. import db, runs, storage
from .. import perf as perf_mod
from .. import profile as profile_mod
from ..rules import CustomRuleDef, RuleContext, run_custom_rules, run_rules
from ..rules.engine import RuleThresholds
from ..streaming import broker
from ..util import new_id, now_ms
from . import metrics as metrics_mod
from . import ltrace_parser, strace_parser
from .events import EXIT, LIBCALL, SIGNAL, Anomaly, MetricSample, TraceEvent

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
    # collapse repeats: rule_id -> {id, count, last_pub}. One feed row per rule,
    # with an occurrence count, instead of a new row every re-fire.
    rule_incidents: dict = field(default_factory=dict)
    pending_hot: list = field(default_factory=list)  # incident dicts awaiting a hot path
    # live-alert state
    last_rss: float | None = None
    cpu_streak: int = 0
    alerts_fired: set = field(default_factory=set)
    # per-alert-key count of consecutive below-threshold samples; a key is cleared
    # from `alerts_fired` (re-armed) once it stays quiet long enough (R10).
    alert_cooldown: dict = field(default_factory=dict)
    # slow-leak long-horizon check (R8): scan counter + baseline RSS captured at
    # monitor start, so a leak too slow for the 90s sliding window still surfaces.
    scan_count: int = 0
    baseline_rss: float | None = None


_active: dict[str, _RunContext] = {}
_lock = threading.Lock()


def _rule_thresholds() -> RuleThresholds:
    """Rule thresholds from config (config.tracing.rule_thresholds over the
    engine defaults). Fail-open to plain defaults — a bad/absent config must
    never break analysis."""
    try:
        from .. import config
        return RuleThresholds.from_overrides(config.load().tracing.rule_thresholds)
    except Exception:  # noqa: BLE001
        return RuleThresholds()


def _disabled_rules() -> frozenset[str]:
    """Built-in rule ids turned off from Settings -> Rules. Fail-open to the
    empty set (every rule runs) — a bad/absent config must never break analysis."""
    try:
        from .. import config
        return frozenset(config.load().tracing.disabled_rules)
    except Exception:  # noqa: BLE001
        return frozenset()


def _custom_rule_defs() -> list[CustomRuleDef]:
    """User-authored rules (Settings -> Rules). Fail-open to none — a corrupt
    row or a DB hiccup must never break analysis."""
    try:
        return storage.list_custom_rules()
    except Exception:  # noqa: BLE001
        return []


def _sweep_stale() -> None:
    """Drop never-polled contexts whose otrace died before reporting a pid."""
    cutoff = now_ms() - _NO_POLLER_TTL_MS
    stale: list[str] = []
    with _lock:
        for rid, ctx in list(_active.items()):
            if ctx.poller is None and ctx.created_ms and ctx.created_ms < cutoff:
                # A psutil-off run has no poller by design; while its reported
                # pid is alive it's a legitimate long run, not a dead otrace.
                if ctx.root_pid is not None and psutil.pid_exists(ctx.root_pid):
                    continue
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
    # No psutil collector -> acknowledge but don't poll metrics. Still record the
    # pid so _sweep_stale can tell this live run apart from a dead otrace.
    if not collectors.get("psutil", True):
        with _lock:
            ctx = _active.get(run_id)
            if ctx is not None:
                ctx.root_pid = pid
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
# Ceiling on concurrent attach/monitor runs — each spawns profiler (+ eBPF)
# threads, so an unbounded burst is a CPU-exhaustion hazard.
_MAX_ATTACH_ACTIVE = 16


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _descendant_pids(root: int, *, include_root: bool = True) -> list[int]:
    """Root + its live descendant PIDs at call time (deduped, root first). Attaching
    to a master (gunicorn/nginx/postgres) profiles an idle master unless the WORKERS
    are captured too — this feeds the multi-target profilers (perf `-p a,b,c`, the
    biosnoop pid filter). Fail-open: just [root] when the tree can't be walked."""
    pids: list[int] = [root] if include_root else []
    try:
        for child in psutil.Process(root).children(recursive=True):
            if child.pid not in pids:
                pids.append(child.pid)
    except (psutil.Error, OSError):
        pass
    return pids


def start_attach_run(
    pid: int, window_s: int = 20, session_id: str | None = None, monitor: bool = False,
    ebpf: bool = False, requests: bool = False,
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
        target_uid = psutil.Process(pid).uids().real
        info = attach_mod.target_info(pid)
    except psutil.Error as e:
        raise ValueError(f"cannot inspect pid {pid}: {e}") from e
    # Ownership: only profile the caller's own processes (root can profile any).
    if os.getuid() != 0 and target_uid != os.getuid():
        raise ValueError(f"pid {pid} belongs to another user — attach requires a same-user process")
    with _lock:
        attach_active = sum(
            1 for c in _active.values() if (c.run.collector_config or {}).get("attach")
        )
    if attach_active >= _MAX_ATTACH_ACTIVE:
        raise ValueError(
            f"too many concurrent attach/monitor runs ({attach_active}) — stop one first"
        )

    window = max(_ATTACH_MIN_S, min(int(window_s), _ATTACH_MAX_S))
    # Pick the runtime's dedicated sampler if installed (Phase B), else perf.
    plan = attach_mod.profiler_plan(info["runtime"])
    if plan:
        profiler, prof_fmt, prof_file = plan["tool"], plan["format"], plan["out_file"]
    else:
        profiler, prof_fmt, prof_file = "perf", "perf", "perf.data"
    # cgroup limits (R7): if the target is containerized, the CPU quota + memory
    # limit that box it. Stored on the run so the rules can flag a quota-saturated /
    # near-OOM container (fail-open: None on a bare-metal target).
    from .. import container  # local import: keep module import graph flat
    climits = container.cgroup_limits(pid)
    data = runs.RunCreate(
        command=info["cmdline"],
        cwd=_proc_cwd(pid),
        session_id=session_id,
        collector_config={
            "psutil": True, "perf": True, "attach": True, "monitor": monitor,
            "ebpf": ebpf, "requests": requests, "runtime": info["runtime"], "profiler": profiler,
            "profile_format": prof_fmt, "profile_file": prof_file,
            "container": info.get("container"),
            "cgroup_cpu_quota_cores": climits["cpu_quota_cores"],
            "cgroup_mem_limit_bytes": climits["mem_limit_bytes"],
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


def abort_run(run_id: str) -> None:
    """Tear down a live run's in-memory machinery WITHOUT finalizing — for when
    the run row/dir is being deleted out from under it. No finalize pass (its
    data is being rmtree'd) and no lifecycle SSE (the UI already dropped it)."""
    with _lock:
        ctx = _active.pop(run_id, None)
    if ctx is None:
        return
    ctx.stop_event.set()
    if ctx.poller is not None:
        ctx.poller.stop(join=False)  # don't block the HTTP thread on the join
    log.info("run %s aborted (deleted while active)", run_id)


def _fail_run(run_id: str) -> None:
    """Last-resort teardown when an attach/monitor thread dies unexpectedly:
    stop the run's machinery and unstick its status. Never raises."""
    with _lock:
        ctx = _active.pop(run_id, None)
    if ctx is not None:
        ctx.stop_event.set()
        if ctx.poller is not None:
            try:
                ctx.poller.stop(join=False)
            except Exception:  # noqa: BLE001
                pass
    try:
        run = runs.get(run_id)
        if run is not None and run.status in (runs.RUNNING, runs.ANALYZING):
            if runs.finalize(run_id, status=runs.ERROR) is None:
                runs.set_status(run_id, runs.ERROR)
    except Exception:  # noqa: BLE001
        log.debug("could not stamp run %s as error", run_id, exc_info=True)
    broker.publish(run_id, "run_ended", {"id": run_id})


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
    (perf.data / collapsed / speedscope / cpuprofile / phpspy). Returns None when
    the capture is missing/empty/unparseable so the run keeps its psutil timeline."""
    try:
        # dotnet-trace writes the speedscope alongside the .nettrace; be robust to
        # the exact filename by falling back to the newest *.speedscope.json in the
        # dir. The stat/glob is inside the try so a concurrent run deletion
        # degrades to None (psutil-timeline-only run), not a crashed thread.
        if fmt == "speedscope" and (not raw.exists() or raw.stat().st_size == 0):
            cands = sorted(raw.parent.glob("*.speedscope.json"), key=lambda p: p.stat().st_mtime)
            if cands:
                raw = cands[-1]
        if not raw.exists() or raw.stat().st_size == 0:
            return None
        if fmt == "collapsed":
            return perf_mod.fold_collapsed(raw.read_text(errors="replace"))
        if fmt == "speedscope":
            return perf_mod.fold_speedscope(json.loads(raw.read_text()))
        if fmt == "cpuprofile":
            return perf_mod.fold_cpuprofile(json.loads(raw.read_text()))
        if fmt == "phpspy":
            return perf_mod.fold_phpspy(raw.read_text(errors="replace"))
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

    # Node/Deno/Bun profile via the V8 inspector (CDP over WebSocket), not a Popen'd
    # CLI sampler — a different capture model, so it gets its own branch.
    if profiler == "node-cdp":
        from .. import node_cdp
        return node_cdp.capture(pid, window_s, str(out_path), stop=stop)

    if profiler == "perf":
        # perf `-p` takes a comma-separated pid list — include the target's current
        # children so a master's worker frames are attributed, not just the (often
        # idle) master. Snapshot at capture start; late-forked workers are missed
        # but the common pre-forked pool (gunicorn/nginx) is covered.
        targets = ",".join(str(p) for p in _descendant_pids(pid))
        cmd = ["perf", "record", "-p", targets, "-g", "-F", str(_PERF_HZ),
               "-o", str(out_path), "--", "sleep", str(window_s)]
    else:
        cmd = attach_mod.sampler_argv(profiler, pid, window_s, str(out_path))

    proc: subprocess.Popen | None = None
    reason: str | None = None
    outf = errf = None
    if shutil.which(profiler):
        # Temp files, never PIPEs: an undrained pipe fills its 64KB buffer
        # mid-window and stalls a chatty sampler (same hazard ebpf._run_proc
        # documents and avoids).
        outf = tempfile.TemporaryFile()
        errf = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(cmd, stdout=outf, stderr=errf)
        except Exception:  # noqa: BLE001
            log.exception("attach %s failed to start for run %s", profiler, run.id)
            proc, reason = None, f"could not start {profiler}."
            outf.close()
            errf.close()
            outf = errf = None
    else:
        reason = f"{profiler} is not installed — captured the resource timeline only."

    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if stop.is_set() or not psutil.pid_exists(pid):
            break
        time.sleep(0.2)

    ok = False
    try:
        if proc is not None:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)  # graceful: flush the profiler output
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            ok = out_path.exists() and out_path.stat().st_size > 0
            if not ok:
                errf.seek(0)
                err = errf.read().decode(errors="replace")
                reason = _perf_fail_reason(err, profiler)
                log.warning("attach run %s: no %s capture", run.id, profiler)
    finally:
        if outf is not None:
            outf.close()
            errf.close()
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


def _start_ebpf(run: runs.Run, pid: int, window_s: int, stop: threading.Event) -> list[threading.Thread]:
    """Spawn the concurrent bpftrace/bcc captures for the window: the eBPF suite
    (off-CPU + latency, gated on the `ebpf` flag) and/or request tracing (HTTP boundary
    + libpq DB spans, gated on the `requests` flag). Each is its OWN dedicated program on
    its OWN flag, so a requests-only run produces NO off-CPU/latency artifacts and an
    ebpf-only run produces no request artifacts (§3.6). Returns the spawned threads (the
    caller joins them before finalize); an empty list when the run opted into neither."""
    cfg = run.collector_config or {}
    threads: list[threading.Thread] = []
    if cfg.get("ebpf"):
        threads.append(threading.Thread(target=_capture_ebpf, args=(run, pid, window_s, stop), daemon=True))
    if cfg.get("requests"):
        threads.append(threading.Thread(target=_capture_requests, args=(run, pid, window_s, stop), daemon=True))
    for t in threads:
        t.start()
    return threads


def _capture_ebpf(run: runs.Run, pid: int, window_s: int, stop: threading.Event) -> None:
    """Off-CPU flamegraph (offcputime) + run-queue/block-I/O latency histograms
    (runqlat/biolatency), for the window. The three tools run CONCURRENTLY (they
    each cover the same window; sequential would triple the wall-clock and overrun
    the caller's join). Reuses fold_collapsed for the off-CPU flame. Fail-open: a
    denied/missing tool just writes a reason stub. `stop` cuts every tool short."""
    from .. import ebpf as ebpf_mod

    caps = ebpf_mod.capabilities()
    use_sudo = caps.get("use_sudo", False)
    run_dir = Path(run.run_dir)
    n = str(max(2, int(window_s)))
    tmo = window_s + 30

    results: dict = {}
    # bpftrace (CO-RE) for the histograms where available — bcc's runqlat/biolatency
    # won't compile on very new kernels; offcputime (folded stacks) stays on bcc.
    use_bt = ebpf_mod.bpftrace_available()

    def _run_bcc(key: str, name: str, args: list[str], kwargs: dict | None = None) -> None:
        results[key] = ebpf_mod.run_tool(name, args, use_sudo=use_sudo, timeout=tmo,
                                         stop=stop, **(kwargs or {}))

    def _run_bt(key: str, script: str) -> None:
        results[key] = ebpf_mod.run_bpftrace(script, timeout=tmo, stop=stop)

    threads: list[threading.Thread] = []

    def _spawn(fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        t.start()
        threads.append(t)

    # USDT GC (Python only, and only if the interpreter exposes gc__start) — gate
    # cheaply (readelf, no root) before spending a capture slot.
    runtime = (run.collector_config or {}).get("runtime")
    gc_gated = caps["available"] and runtime == "python" and "gc__start" in ebpf_mod.usdt_probes(pid)
    gc_lib = ebpf_mod.libpython_path(pid) if gc_gated else None

    _spawn(_run_bcc, "off", "offcputime", ["-f", "-p", str(pid), n], {})
    _spawn(_run_bcc, "bsnoop", "biosnoop", [], {"duration": window_s, "line_buffered": True})
    if use_bt:
        # ONE combined bpftrace (run-queue + block-I/O + optional GC) — avoids
        # multiple concurrent CO-RE compiles wedging each other. Run WITHOUT -p
        # (GC is scoped by an in-script /pid==PID/ filter; -p would break the
        # system-wide sched/block tracepoints).
        combined = ebpf_mod.build_combined_bt(pid, n, gc_lib if gc_gated else None)
        _spawn(_run_bt, "bt", combined)
    else:
        _spawn(_run_bcc, "rq", "runqlat", ["-m", "-p", str(pid), n, "1"], {})
        _spawn(_run_bcc, "bio", "biolatency", ["-m", n, "1"], {})
        if gc_gated:
            _spawn(_run_bcc, "gc", "pythongc", ["-m", str(pid)],
                   {"duration": window_s, "line_buffered": True})

    for t in threads:
        t.join(timeout=tmo + 5)

    if runs.get(run.id) is None:
        return  # run deleted mid-capture — don't recreate its dir

    ok, out, reason = results.get("off", (False, "", "off-CPU capture didn't run."))
    if ok and out.strip():
        fg = perf_mod.fold_collapsed(out, count_is_usec=True)
    else:
        fg = {"supported": False, "samples": 0, "tree": None, "hotspots": [],
              "unit": "usec", "reason": reason or caps.get("reason") or "no off-CPU samples."}
    storage.write_json(run_dir / "offcpu-flamegraph.json", fg)
    storage.record_artifact(run.id, "offcpu-flamegraph", run_dir / "offcpu-flamegraph.json")

    bs_ok, bs_out, bs_reason = results.get("bsnoop", (False, "", "biosnoop didn't run."))
    if use_bt:
        bt_ok, bt_out, bt_reason = results.get("bt", (False, "", "bpftrace didn't run."))
        if bt_ok:
            runqueue = ebpf_mod.parse_bpftrace_hist(ebpf_mod.extract_bt_map(bt_out, "runq_us"), "usecs")
            block_io = ebpf_mod.parse_bpftrace_hist(ebpf_mod.extract_bt_map(bt_out, "bio_ms"), "msecs")
        else:
            runqueue = block_io = {"error": bt_reason}
    else:
        rq_ok, rq_out, rq_reason = results.get("rq", (False, "", "run-queue capture didn't run."))
        bio_ok, bio_out, bio_reason = results.get("bio", (False, "", "block-I/O capture didn't run."))
        runqueue = ebpf_mod.parse_log2_hist(rq_out) if rq_ok else {"error": rq_reason}
        block_io = ebpf_mod.parse_log2_hist(bio_out) if bio_ok else {"error": bio_reason}
    latency = {
        "available": caps["available"],
        "reason": caps["reason"],
        "engine": "bpftrace" if use_bt else "bcc",
        "runqueue": runqueue,
        "block_io": block_io,
        "block_io_pid": ebpf_mod.parse_biosnoop(bs_out, set(_descendant_pids(pid))) if bs_ok else {"error": bs_reason},
    }
    storage.write_json(run_dir / "latency.json", latency)
    storage.record_artifact(run.id, "latency", run_dir / "latency.json")

    # USDT GC timeline (Python only) — gc-timeline.json artifact
    if gc_gated:
        if use_bt:  # GC events are inline in the combined bpftrace output
            bt_ok, bt_out, bt_reason = results.get("bt", (False, "", "GC capture didn't run."))
            gc = ({"available": True, "reason": None, "events": ebpf_mod.parse_bpftrace_gc(bt_out)}
                  if bt_ok else {"available": False, "reason": bt_reason, "events": []})
        else:
            gc_ok, gc_out, gc_reason = results.get("gc", (False, "", "GC capture didn't run."))
            gc = ({"available": True, "reason": None, "events": ebpf_mod.parse_ugc(gc_out)}
                  if gc_ok else {"available": False, "reason": gc_reason, "events": []})
    elif runtime == "python":
        gc = {"available": False, "events": [], "reason": (
            "no USDT probes on this interpreter — conda/statically-linked python and "
            "Node don't ship them; use a --enable-dtrace python build."
            if caps["available"] else caps["reason"])}
    else:
        gc = {"available": False, "events": [], "reason": "GC tracing is Python-only."}
    storage.write_json(run_dir / "gc-timeline.json", gc)
    storage.record_artifact(run.id, "gc-timeline", run_dir / "gc-timeline.json")

    # Monitor: surface latency findings as LIVE incidents too, so the Incidents feed
    # matches the Overview (the A.1 guarantee) — otherwise they'd only appear at
    # finalize. Collapse-by-rule dedups repeats across snapshots.
    lat_anoms = ebpf_mod.latency_anomalies(latency)
    if lat_anoms:
        with _lock:
            ctx = _active.get(run.id)
        if ctx is not None and ctx.monitor:
            ts = ctx.samples[-1].timestamp_ms if ctx.samples else time.time() * 1000
            for a in lat_anoms:
                _make_incident(ctx, a.rule_id, a.severity, a.title, ts)


def _capture_requests(run: runs.Run, pid: int, window_s: int, stop: threading.Event) -> None:
    """Attach the DEDICATED request-tracing bpftrace (plaintext-HTTP/1.x boundary +
    libpq DB spans) for the window; parse → correlate (tid + time-window) → write
    requests.ndjson.zst (full stream) + requests.json (rollup, atomic) + a request_rollup
    SSE; and for a MONITOR run emit slow/errored-endpoint incidents (collapse per
    endpoint) so the feed matches the Overview. Fail-open at every step: no bpftrace /
    privilege / libpq / HTTP traffic completes with a friendly `reason`, never raises.
    Gated on the `requests` flag and independent of the eBPF suite (§3.6)."""
    from .. import ebpf as ebpf_mod, aggregate
    run_dir = Path(run.run_dir)
    n = str(max(2, int(window_s)))
    tmo = window_s + 30

    def _persist(rollup: dict, spans: list, off_intervals: list | None = None) -> None:
        if runs.get(run.id) is None:  # deleted mid-capture — don't recreate its dir
            return
        try:
            # Full stream: http/db spans (Span.to_ndjson) + off-CPU/runq intervals (dicts).
            def _rows():
                for s in spans:
                    yield s.to_ndjson()
                for iv in (off_intervals or []):
                    yield {"event_type": "offcpu", **iv}
            storage.write_ndjson_zst(run_dir / "requests.ndjson.zst", _rows())
            storage.write_json(run_dir / "requests.json", rollup)
            storage.record_artifact(run.id, "requests", run_dir / "requests.json")
            broker.publish(run.id, "request_rollup", rollup)
        except Exception:  # noqa: BLE001
            log.debug("persist requests failed for %s", run.id, exc_info=True)

    try:
        caps = ebpf_mod.request_capabilities()
        if not caps["available"]:
            _persist(aggregate.request_rollup([], [], window_s=window_s,
                                              available=False, reason=caps["reason"]), [])
            return

        # Resolve the target's mapped client libs: Postgres (libpq), MySQL/SQLite (db_libs),
        # and TLS (libssl → plaintext recovery for an HTTPS server). Each is fail-open None.
        pq_lib = ebpf_mod.libpq_path(pid)      # None → no dynamically-linked Postgres
        db_libs = ebpf_mod.db_libs(pid)        # [(engine, path)] for MySQL/SQLite
        ssl_lib = ebpf_mod.libssl_path(pid)    # None → plaintext only
        script = ebpf_mod.build_request_bt(pid, n, pq_lib=pq_lib, db_libs=db_libs,
                                           ssl_lib=ssl_lib, off_cpu=True)
        # CLOCK_MONOTONIC→epoch anchor (§2.6), captured back-to-back at child launch so the
        # curated SQLite spans (monotonic start_ns) can be stored as epoch timestamp_ms.
        mono0 = time.clock_gettime(time.CLOCK_MONOTONIC)
        wall0 = time.time()
        _ok, out, _reason = ebpf_mod.run_bpftrace(script, timeout=tmo, stop=stop)
        if runs.get(run.id) is None:
            return

        http_spans = ebpf_mod.parse_bpftrace_http(out)
        db_spans = ebpf_mod.parse_bpftrace_sql(out)
        off_intervals = ebpf_mod.parse_bpftrace_offcpu(out)
        has_db = bool(pq_lib or db_libs)
        reason = None if has_db else (
            "DB spans unavailable — the target maps no dynamically-linked libpq / "
            "libmysqlclient / libsqlite3 (a statically-bundled psycopg2-binary, or an "
            "asyncpg/pure-wire driver). Endpoint timings are still shown.")
        if not http_spans:
            # bpftrace ran but saw no request boundaries: an idle server, an HTTP/2
            # endpoint, or a non-HTTP process — a valid empty result, not a failure.
            # (TLS is now recovered via libssl, so it's no longer a blind spot.)
            reason = reason or (
                "No HTTP/1.x requests were observed on the target during the window "
                "(idle server, HTTP/2 endpoint, or a non-HTTP process).")

        rollup = aggregate.request_rollup(http_spans, db_spans, window_s=window_s,
                                          engine="bpftrace", reason=reason, available=True,
                                          off_intervals=off_intervals)
        _persist(rollup, sorted(http_spans + db_spans, key=lambda s: s.start_ns), off_intervals)

        # Curated slow/errored spans → SQLite (queryable via GET /runs/{id}/request-spans),
        # epoch-timestamped for time-correlation. Fail-open — never strands the run.
        if http_spans and runs.get(run.id) is not None:
            try:
                curated = aggregate.curate_request_spans(
                    http_spans, db_spans, rollup["endpoints"], mono0=mono0, wall0=wall0)
                storage.insert_request_spans(run.id, curated)
            except Exception:  # noqa: BLE001
                log.debug("curate request spans failed for %s", run.id, exc_info=True)

        # Per-tid off-CPU flamegraphs (the span→off-CPU-flamegraph drill). One flame per
        # request-serving tid, folded from the @ostk kernel-stack dump. Served tid-filtered
        # by GET /runs/{id}/offcpu-flamegraph?tid=. Fail-open.
        try:
            stacks = ebpf_mod.extract_offcpu_stacks(out)
            if stacks and runs.get(run.id) is not None:
                flames = {tid: perf_mod.fold_collapsed(txt, count_is_usec=True)
                          for tid, txt in stacks.items()}
                storage.write_json(run_dir / "request-offcpu.json", flames)
                storage.record_artifact(run.id, "request-offcpu", run_dir / "request-offcpu.json")
        except Exception:  # noqa: BLE001
            log.debug("request off-cpu flamegraph failed for %s", run.id, exc_info=True)

        # Monitor: slow/errored endpoints → live incidents (collapse per endpoint), so the
        # Incidents feed matches the Overview. The incident ts MUST be epoch ms — use the
        # latest sample's timestamp, never a span's monotonic nsecs (§2.6 / §3.10).
        anoms = aggregate.reqtrace_anomalies(rollup)
        if anoms:
            with _lock:
                ctx = _active.get(run.id)
            if ctx is not None and ctx.monitor:
                ts = ctx.samples[-1].timestamp_ms if ctx.samples else time.time() * 1000
                for a in anoms:
                    _make_incident(ctx, a.rule_id, a.severity, a.title, ts)
    except Exception:  # noqa: BLE001 — fail-open: a request-capture crash never strands the run
        log.debug("request capture failed for %s", run.id, exc_info=True)


def _run_attach_profile(run_id: str, pid: int, window_s: int) -> None:
    """Single-shot attach: one profiling window, then finalize (sole finalizer)."""
    run = runs.get(run_id)
    if run is None:
        return
    try:
        with _lock:
            ctx = _active.get(run_id)
        stop = ctx.stop_event if ctx else threading.Event()
        ebpf_ts = _start_ebpf(run, pid, window_s, stop)
        ok, reason = _capture_profile(run, pid, window_s, stop)
        for t in ebpf_ts:
            t.join(timeout=window_s + 40)
        final = end_run(run_id, exit_code=0 if ok else None, exit_signal=None, ended_at=None)
        if final is not None:  # None: run deleted mid-window — don't recreate its dir
            _ensure_flamegraph_reason(Path(run.run_dir), reason)
    except Exception:  # noqa: BLE001 — sole finalizer: a crash must not strand the run
        log.exception("attach profile thread failed for run %s", run_id)
        _fail_run(run_id)


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
    try:
        stop = ctx.stop_event
        reason = None
        while not stop.is_set() and psutil.pid_exists(pid):
            ebpf_ts = _start_ebpf(run, pid, window_s, stop)  # concurrent off-CPU/latency + requests
            ok, reason = _capture_profile(run, pid, window_s, stop)
            for t in ebpf_ts:
                t.join(timeout=window_s + 40)
            if stop.is_set():
                break  # Stop/abort mid-window: no snapshot refresh, no late incidents
            if ok and runs.get(run_id) is not None:  # deleted run: don't recreate its dir
                _refresh_flamegraph(run)
            _eval_sliding_rules(ctx)
            _check_slow_leak(ctx)  # long-horizon leak the sliding window can't see
        final = end_run(run_id, exit_code=0, exit_signal=None, ended_at=None)
        if final is not None:
            _ensure_flamegraph_reason(Path(run.run_dir), reason)
    except Exception:  # noqa: BLE001 — sole finalizer: a crash must not strand the run
        log.exception("monitor thread failed for run %s", run_id)
        _fail_run(run_id)


# --- live incidents (monitor mode) ------------------------------------------

_INCIDENT_WINDOW_MS = 30_000   # leading metric context stored per incident
_SLIDING_N = 360               # trailing samples (~90s at 250ms) for rule scans
_MAX_LIVE_SAMPLES = 2400       # ring-buffer cap on in-memory samples (~10min)
_INCIDENT_UPDATE_MS = 10_000   # throttle re-publish of a collapsed incident's count


def _make_incident(ctx: _RunContext, rule_id: str, severity: str, title: str, ts: float) -> None:
    """Record + stream an incident (monitor runs only). Repeats of the SAME rule
    COLLAPSE into one feed entry that accrues an occurrence `count` + `last_ts`
    (re-published at most every _INCIDENT_UPDATE_MS), instead of a new row per
    re-fire — so a request-driven server that spikes on every request shows one
    'CPU pegged ×N' row, not hundreds."""
    if not ctx.monitor:
        return
    with _lock:  # decide new-vs-collapse + snapshot shared state atomically
        hot = ctx.latest_hot
        samples = list(ctx.samples)
        rec = ctx.rule_incidents.get(rule_id)
        if rec is None:
            inc_id = new_id()
            ctx.rule_incidents[rule_id] = {"id": inc_id, "count": 1, "last_pub": ts}
            is_new = True
        else:
            rec["count"] += 1
            if ts - rec["last_pub"] < _INCIDENT_UPDATE_MS:
                return  # counted in-memory; throttle disk + SSE churn
            rec["last_pub"] = ts
            inc_id, count, is_new = rec["id"], rec["count"], False

    if is_new:
        metrics = [s.to_ndjson() for s in samples if abs(s.timestamp_ms - ts) <= _INCIDENT_WINDOW_MS]
        incident = {
            "id": inc_id, "run_id": ctx.run.id, "ts": ts, "first_ts": ts, "last_ts": ts,
            "count": 1, "rule_id": rule_id, "severity": severity, "title": title,
            "hot": hot, "metrics": metrics, "ai": None,
        }
        if hot is None:  # queue for the "where" backfill on the next snapshot
            with _lock:
                ctx.pending_hot.append(incident)
        try:
            storage.append_incident(ctx.run.run_dir, incident)
        except Exception:  # noqa: BLE001
            log.debug("append_incident failed for %s", ctx.run.id, exc_info=True)
        broker.publish(ctx.run.id, "incident", incident)
        if hot is not None:
            _maybe_incident_ai(ctx.run, incident)
    else:
        # No metrics in the patch: the stored/displayed first-occurrence window
        # persists (the frontend shallow-merges patches), and re-embedding ~19KB
        # of samples per rule every 10s just bloats incidents.ndjson.
        patch: dict = {"count": count, "last_ts": ts}
        if hot is not None:
            patch["hot"] = hot
        try:
            storage.update_incident(ctx.run.run_dir, inc_id, **patch)
        except Exception:  # noqa: BLE001
            log.debug("update_incident failed for %s", ctx.run.id, exc_info=True)
        broker.publish(ctx.run.id, "incident_update", {"id": inc_id, **patch})


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


_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _incidents_to_anomalies(incidents: list[dict]) -> list[Anomaly]:
    """Collapse a monitor run's incidents into its anomaly record (one per rule,
    highest severity, with the occurrence count + where), so the Overview 'Top
    Findings' match the Incidents feed exactly."""
    by_rule: dict[str, dict] = {}
    for inc in incidents:
        rid = inc.get("rule_id", "incident")
        cur = by_rule.get(rid)
        if cur is None or _SEV_RANK.get(inc.get("severity"), 0) >= _SEV_RANK.get(cur.get("severity"), 0):
            by_rule[rid] = inc
    out: list[Anomaly] = []
    for rid, inc in by_rule.items():
        stack = (inc.get("hot") or {}).get("stack") or []
        where = " → ".join(stack[-4:]) if stack else "off-CPU (not attributable to a CPU hot path)"
        count = inc.get("count", 1)
        sev = inc.get("severity", "medium")
        out.append(Anomaly(
            rule_id=rid,
            severity=sev,
            severity_score=0.4 + 0.1 * _SEV_RANK.get(sev, 1),
            title=inc.get("title", "incident") + (f" (×{count})" if count > 1 else ""),
            description=(f"Live monitor incident{f', seen {count}×' if count > 1 else ''}. "
                        f"Where: {where}."),
        ))
    return out


def _eval_sliding_rules(ctx: _RunContext) -> None:
    """Run the metric-based rules over the trailing window so sustained conditions
    (memory growth, sustained CPU) surface as incidents while the run is live."""
    with _lock:  # snapshot — the poller thread appends/trims ctx.samples
        window = ctx.samples[-_SLIDING_N:]
    if len(window) < 8:
        return
    span = window[-1].timestamp_ms - window[0].timestamp_ms
    cc = ctx.run.collector_config or {}
    rctx = RuleContext(
        events=[],
        metrics=[s.to_ndjson() for s in window],
        duration_ms=int(span) or None,
        cpu_cores=metrics_mod.cpu_count(),
        collectors=cc,
        cgroup_cpu_quota_cores=cc.get("cgroup_cpu_quota_cores"),
        cgroup_mem_limit_bytes=cc.get("cgroup_mem_limit_bytes"),
        thresholds=_rule_thresholds(),
        disabled_rules=_disabled_rules(),
    )
    try:
        found = run_rules(rctx)
    except Exception:  # noqa: BLE001
        log.debug("sliding rules failed for %s", ctx.run.id, exc_info=True)
        return
    try:
        found = found + run_custom_rules(rctx, _custom_rule_defs())
    except Exception:  # noqa: BLE001
        log.debug("custom sliding rules failed for %s", ctx.run.id, exc_info=True)
    for a in found:
        # cooldown-deduped inside _make_incident (re-fires after a quiet gap)
        _make_incident(ctx, a.rule_id, a.severity, a.title, window[-1].timestamp_ms)


# Slow-leak long-horizon check (R8): the 90s sliding window can't see a ~1MB/min
# creep, and monitor finalize derives findings only from incidents — so a slow
# leak would be wholly invisible. We compare the full DB metric history against a
# baseline every Nth scan and raise an incident (→ Overview, via _incidents_to_
# anomalies) when RSS has grown steadily over a multi-minute horizon.
_SLOW_LEAK_SCAN_EVERY = 4          # ~ every 4th monitor snapshot
_SLOW_LEAK_MIN_HORIZON_MS = 180_000  # need >=3min of history to call it a trend
_SLOW_LEAK_MIN_MB = 50.0           # ignore sub-50MB drift (allocator noise)
_SLOW_LEAK_MIN_MB_PER_MIN = 0.5    # sustained creep rate to flag


def _check_slow_leak(ctx: _RunContext) -> None:
    """Long-horizon RSS-growth check for a live monitor run. Reads the complete
    metric history (not just the in-memory ring buffer) every Nth scan so a leak
    too slow for the sliding window still fires. Raises a collapsed incident, so
    the Overview and Incidents feed stay in agreement."""
    if not ctx.monitor:
        return
    ctx.scan_count += 1
    if ctx.scan_count % _SLOW_LEAK_SCAN_EVERY != 0:
        return
    try:
        rows = storage.read_metrics(ctx.run.id)
    except Exception:  # noqa: BLE001
        return
    series = [(r["timestamp_ms"], r["rss_mb"]) for r in rows if r.get("rss_mb") is not None]
    if len(series) < 20:
        return
    span_ms = series[-1][0] - series[0][0]
    if span_ms < _SLOW_LEAK_MIN_HORIZON_MS:
        return
    # Baseline = a low-percentile of the opening tenth (robust to a warmup blip);
    # current = median of the closing tenth (robust to a momentary spike/dip).
    head = sorted(v for _, v in series[: max(2, len(series) // 10)])
    tail = sorted(v for _, v in series[-max(2, len(series) // 10):])
    if ctx.baseline_rss is None:
        ctx.baseline_rss = head[0]
    baseline = min(ctx.baseline_rss, head[len(head) // 2])
    current = tail[len(tail) // 2]
    growth = current - baseline
    rate = growth / (span_ms / 60_000.0)  # MB per minute
    if growth < _SLOW_LEAK_MIN_MB or rate < _SLOW_LEAK_MIN_MB_PER_MIN:
        return
    # Confirm it's a steady climb, not one step: majority of samples non-decreasing.
    vals = [v for _, v in series]
    non_decr = sum(1 for a, b in zip(vals, vals[1:]) if b >= a - 0.5)
    if non_decr < len(vals) * 0.7:
        return
    _make_incident(
        ctx, "slow_memory_leak", "high",
        f"Slow memory leak — RSS crept {baseline:.0f}MB → {current:.0f}MB "
        f"(~{rate:.1f}MB/min over {span_ms / 60_000.0:.0f}min)",
        series[-1][0],
    )


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
_ALERT_REARM = 8         # consecutive below-threshold samples (~2s) before re-arming


def _live_detect(ctx: _RunContext, sample: MetricSample) -> None:
    """Emit `anomaly_alert` SSE events from metric thresholds as a run unfolds, so
    the Live Monitor can warn the developer before the run even finishes. For a
    monitor run these also become *incidents* (with when/where/leading-metrics).

    Threshold alerts use hysteresis, not a one-shot latch: a fired key is cleared
    (re-armed) once its metric stays below threshold for `_ALERT_REARM` samples,
    so a genuine RE-occurrence re-alerts — and the collapsed incident count keeps
    growing — instead of firing once and going silent forever."""
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

    def rearm(key: str, active: bool) -> None:
        """Track how long `key` has been quiet; clear the latch once it's been
        below threshold long enough so it can fire again on the next spike."""
        if active:
            ctx.alert_cooldown[key] = 0
        else:
            c = ctx.alert_cooldown.get(key, 0) + 1
            ctx.alert_cooldown[key] = c
            if c >= _ALERT_REARM:
                ctx.alerts_fired.discard(key)
                ctx.alert_cooldown[key] = 0

    if sample.open_fds is not None:
        fd_active = sample.open_fds > _FD_ALERT
        if fd_active:
            once("fd", "fd_leak_live", "high", f"Open file descriptors exceed {_FD_ALERT} "
                                               f"({sample.open_fds}) — possible leak")
        rearm("fd", fd_active)
    if sample.rss_mb is not None:
        if ctx.last_rss is not None and sample.rss_mb - ctx.last_rss > _RSS_SPIKE_MB:
            # incidents are cooldown-deduped in _make_incident (no per-sample spam)
            emit("mem_spike", "medium",
                 f"Memory spiked +{sample.rss_mb - ctx.last_rss:.0f}MB (now {sample.rss_mb:.0f}MB)")
        ctx.last_rss = sample.rss_mb
    if sample.cpu_pct is not None:
        if sample.cpu_pct > _CPU_HOT:
            ctx.cpu_streak += 1
            if ctx.cpu_streak == _CPU_STREAK:
                once("cpu", "cpu_hot_live", "medium", "CPU pegged for ~2s — compute-bound")
            rearm("cpu", True)
        else:
            ctx.cpu_streak = 0
            rearm("cpu", False)


def end_run(
    run_id: str,
    *,
    exit_code: int | None = None,
    exit_signal: str | None = None,
    ended_at: int | None = None,
    _stop_poller: bool = True,
) -> runs.Run | None:
    # Teardown BEFORE the missing-row check: a run deleted while active must
    # still have its context popped, monitor loop stopped, and poller torn down
    # — otherwise the ctx leaks in `_active` forever.
    with _lock:
        ctx = _active.pop(run_id, None)
    if ctx is not None:
        # Stop a live monitor loop too — otherwise a finalize from elsewhere
        # (e.g. a generic POST /runs/{id}/end) leaves the monitor thread spawning
        # profilers and appending incidents to an already-completed run.
        ctx.stop_event.set()
    run = runs.get(run_id)
    if run is None:
        # Row deleted out from under a live run: wind down, nothing to finalize.
        # (_stop_poller=False means we're ON the poller thread — never join it.)
        if ctx is not None and ctx.poller is not None and _stop_poller:
            ctx.poller.stop()
        return None
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

    final = None
    try:
        runs.set_status(run_id, runs.ANALYZING)
        broker.publish(run_id, "run_analyzing", {"id": run_id})
        final = _finalize(run, ctx, exit_code, exit_signal, ended_at)
    except Exception:  # noqa: BLE001
        log.exception("finalize failed for run %s", run_id)
        try:
            final = runs.finalize(
                run_id, ended_at=ended_at, exit_code=exit_code,
                exit_signal=exit_signal, status=runs.ERROR,
            )
        except Exception:  # noqa: BLE001
            log.exception("error-finalize failed for run %s", run_id)
    broker.publish(run_id, "run_ended", final.model_dump() if final else {"id": run_id})
    log.info("run %s finalized (severity=%s)", run_id, final.max_severity if final else "?")
    return final


# --- finalize ---------------------------------------------------------------

# Cap on events held in memory for analysis. The complete stream still goes to
# events.ndjson.zst; past the cap only running totals are kept, so a traced
# build/find-style command (tens of millions of syscalls) can't OOM the backend.
_MAX_ANALYZED_EVENTS = 1_000_000


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
        parser = ltrace_parser
    else:
        trace_log = run_dir / "strace.log"
        trace_kind = "strace-log"
        parser = strace_parser

    # Single pass: every event streams straight into the compressed archive
    # (source of truth for replay), but only the first _MAX_ANALYZED_EVENTS stay
    # in memory for rules/curation. Full-stream counters keep the summary honest.
    events: list[TraceEvent] = []
    stream_totals: Counter = Counter()
    top_syscalls: Counter = Counter()

    def _tee():
        for ev in (parser.parse_file(trace_log) if trace_log.exists() else ()):
            stream_totals["events"] += 1
            if ev.event_type == SIGNAL:
                stream_totals["signals"] += 1
            elif ev.event_type not in (EXIT, LIBCALL):
                stream_totals["syscall_events"] += 1
                if ev.error is not None:
                    stream_totals["errors"] += 1
                if ev.syscall:
                    top_syscalls[ev.syscall] += 1
            if len(events) < _MAX_ANALYZED_EVENTS:
                events.append(ev)
            yield ev.to_ndjson()

    storage.write_ndjson_zst(run_dir / "events.ndjson.zst", _tee())

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

    # Detect anomalies. For a MONITOR run the findings ARE its live incidents —
    # a whole-session rule pass over a long-lived process is misleading (baseline
    # drift flags spurious "growth"), and it would diverge from the Incidents tab.
    monitor = collectors.get("monitor", False)
    if monitor:
        anomalies = _incidents_to_anomalies(storage.read_incidents(run_dir))
    else:
        rctx = RuleContext(
            events=syscall_events,
            metrics=metrics_rows,
            duration_ms=run.duration_ms,
            cpu_cores=metrics_mod.cpu_count(),
            collectors=collectors,
            cgroup_cpu_quota_cores=collectors.get("cgroup_cpu_quota_cores"),
            cgroup_mem_limit_bytes=collectors.get("cgroup_mem_limit_bytes"),
            thresholds=_rule_thresholds(),
            disabled_rules=_disabled_rules(),
        )
        anomalies = run_rules(rctx)
        try:
            anomalies += run_custom_rules(rctx, _custom_rule_defs())
        except Exception:  # noqa: BLE001 — a bad custom rule must never break finalize
            log.warning("custom rule evaluation failed for %s", run.id, exc_info=True)

    # Fail-open note when the in-memory cap truncated the analysis window.
    if stream_totals["events"] > len(events):
        anomalies.append(Anomaly(
            rule_id="analysis_truncated",
            severity="low",
            severity_score=0.1,
            title=f"Analysis truncated after {len(events):,} of "
                  f"{stream_totals['events']:,} events",
            description="The complete stream is archived in events.ndjson.zst; "
                        "rules, curation, and syscall rates were computed over "
                        f"the first {len(events):,} events only.",
        ))

    # Phase-6 profiling artifacts (only for the collectors that ran).
    if use_ltrace and events:
        prof = profile_mod.malloc_profile(e.to_ndjson() for e in events)
        storage.write_json(run_dir / "profile.json", {
            "malloc": prof,
            "hotspots": profile_mod.libcall_stats(e.to_ndjson() for e in events),
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
            if not monitor:  # monitor findings come from incidents (above)
                anomalies.extend(perf_mod.perf_anomalies(flamegraph))

    # eBPF latency findings (run-queue / block-I/O tails). For MONITOR runs these
    # are already emitted as live incidents (→ picked up by _incidents_to_anomalies
    # above), so adding them here too would duplicate them and put a finding in
    # Overview that isn't in the feed. Only single-shot runs need this pass.
    if collectors.get("ebpf", False) and not monitor:
        lat_path = run_dir / "latency.json"
        if lat_path.exists():
            from .. import ebpf as ebpf_mod
            try:
                anomalies.extend(ebpf_mod.latency_anomalies(json.loads(lat_path.read_text())))
            except Exception:  # noqa: BLE001
                log.debug("latency anomalies failed for %s", run.id, exc_info=True)

    # Request-tracing findings (slow / errored endpoints). MONITOR runs already emit these
    # as live incidents (picked up by _incidents_to_anomalies above), so adding them here
    # too would duplicate them and desync Overview from the feed — single-shot only (§3.10).
    if collectors.get("requests", False) and not monitor:
        req_path = run_dir / "requests.json"
        if req_path.exists():
            from .. import aggregate
            try:
                anomalies.extend(aggregate.reqtrace_anomalies(json.loads(req_path.read_text())))
            except Exception:  # noqa: BLE001
                log.debug("reqtrace anomalies failed for %s", run.id, exc_info=True)

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
    # Totals come from the full-stream counters, so they stay honest even when
    # the in-memory analysis window was truncated.
    totals = {
        "syscall_events": stream_totals["syscall_events"],
        "errors": stream_totals["errors"],
        "signals": stream_totals["signals"],
        "top_syscalls": top_syscalls.most_common(10),
    }
    summary = _summary(run, totals, metrics_rows, anomalies, exit_code, exit_signal)
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
    totals: dict,
    metrics_rows: list[dict],
    anomalies: list,
    exit_code: int | None,
    exit_signal: str | None,
) -> dict:
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
            "syscall_events": totals.get("syscall_events", 0),
            "errors": totals.get("errors", 0),
            "signals": totals.get("signals", 0),
            "metric_samples": len(metrics_rows),
            "top_syscalls": totals.get("top_syscalls", []),
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
