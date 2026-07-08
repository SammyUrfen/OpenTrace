"""Detection rules over a finalized run's events + metrics.

A deliberately noise-controlled subset of Roadmap §5 — every rule here is
computable from strace + psutil alone and tuned to avoid the obvious false
positives (e.g. the dynamic linker's library probing, programs that exit
without explicitly closing fds, syscalls that are *supposed* to block).

Each rule is `fn(ctx: RuleContext) -> Anomaly | None`, tagged with the signal it
needs (`@_needs("events")` or `@_needs("metrics")`). `run_rules` only invokes a
rule whose input is actually present, so an events-requiring rule never fires on
absence-of-signal (attach / live-monitor runs carry metric rows but no syscall
events) and a metric rule never wastes a pass on an event-only context.

Thresholds live on `RuleContext.thresholds` (a `RuleThresholds` with the current
values as defaults, overridable from `config.tracing.rule_thresholds`); rules
read them from the context, never via a fresh `config` import (which would hit
disk and break the synthetic tests that construct a `RuleContext` directly).

Public surface:
- `RuleContext`, `RuleThresholds`
- `run_rules(ctx) -> list[Anomaly]`
- `parse_connect_peer(args) -> str | None`  (shared connect-arg peer parser)
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from ..trace.events import Anomaly, TraceEvent

# Severity band bases. Occurrence bonus (<=9.99) keeps ordering within a band
# without ever crossing into the next.
_SEV_BASE = {"critical": 90.0, "high": 70.0, "medium": 45.0, "low": 20.0}


def _score(severity: str, occurrences: int) -> float:
    base = _SEV_BASE.get(severity, 20.0)
    return round(base + min(occurrences, 1000) / 1000 * 9.99, 3)


def _retval_int(ev: TraceEvent) -> int | None:
    if ev.retval is None:
        return None
    try:
        return int(ev.retval, 0)  # handles "3" and "0x7f.."
    except (ValueError, TypeError):
        return None


@dataclass
class RuleThresholds:
    """Per-rule tunables. Defaults are the historical hardcoded values; overrides
    come from `config.tracing.rule_thresholds` (a sparse {name: value} block) via
    `from_overrides`, so operators can retune without touching code and the tests
    keep working against a plain default-constructed instance."""

    # file I/O
    repeated_open_min_opens: int = 10
    failed_open_min: int = 5
    # syscall latency
    slow_syscall_ms: float = 1000.0
    slow_net_ms: float = 1000.0
    slow_file_io_ms: float = 100.0
    mutex_ms: float = 10.0
    mutex_min: int = 20
    # metric trends
    mem_growth_min_mb: float = 50.0
    mem_growth_ratio: float = 1.4
    fd_growth_min: int = 30
    memory_spike_mb: float = 100.0
    # cpu-bound (strace-backed: needs syscall_rate)
    cpu_bound_pct: float = 90.0
    cpu_bound_syscall_rate: float = 50.0
    cpu_bound_min_samples: int = 8
    # storms / loops
    excessive_subprocess_min: int = 50
    conn_reuse_min: int = 5
    io_retry_min: int = 100
    small_read_min: int = 2000
    write_storm_min: int = 1000
    spin_loop_min: int = 2000
    spin_cpu_pct: float = 80.0
    infinite_loop_min_ms: int = 30_000
    infinite_loop_max_syscalls: int = 200
    # metric-only cpu/io rules (attach & monitor, where there's no syscall_rate)
    io_idle_bps: float = 262_144.0     # <256KB/s counts as "no I/O" (spinning)
    io_active_bps: float = 65_536.0    # >64KB/s counts as sustained I/O
    io_wait_cpu_pct: float = 40.0
    metric_min_samples: int = 8
    # downstream-peer slowness (R2)
    downstream_read_min_ms: float = 100.0
    downstream_wait_ms: float = 1000.0
    # cgroup-limit-aware rules (R7): fraction of the cgroup CPU quota / memory
    # limit a container must sit at to be flagged throttled / near-OOM.
    cpu_throttled_ratio: float = 0.9
    rss_cgroup_ratio: float = 0.9
    # whole-history trend gating (R6): beyond this run duration the trend rules
    # judge only a trailing window (sliding treatment), so a long-lived server's
    # warmup RSS/fd climb isn't misread as a leak.
    trend_max_duration_ms: int = 600_000

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "RuleThresholds":
        t = cls()
        for k, v in (overrides or {}).items():
            if v is not None and hasattr(t, k):
                setattr(t, k, v)
        return t


@dataclass
class RuleContext:
    events: list[TraceEvent]
    metrics: list[dict] = field(default_factory=list)  # DB rows (post-backfill)
    duration_ms: int | None = None
    cpu_cores: int = 1
    # Which collectors ran (strace/ltrace/perf/psutil/attach/monitor/ebpf). Lets a
    # rule tell "no data" apart from "signal absent". All new fields DEFAULT so
    # existing RuleContext(events, metrics, ...) constructions still compile.
    collectors: dict = field(default_factory=dict)
    # Container cgroup limits for the target (R7), when attaching to a containerized
    # process — the CPU quota (in cores) and memory limit (bytes) that box it. Both
    # default None so bare-metal/launch runs behave exactly as before.
    cgroup_cpu_quota_cores: float | None = None
    cgroup_mem_limit_bytes: int | None = None
    thresholds: RuleThresholds = field(default_factory=RuleThresholds)


# --- rule registration / signal gating --------------------------------------

RULES: list[Callable[[RuleContext], Anomaly | None]] = []


def _needs(signal: str) -> Callable[[Callable], Callable]:
    """Tag a rule with the signal it requires ('events' | 'metrics') and register
    it. `run_rules` skips a rule whose signal is absent, so an events rule can't
    false-fire on an eventless attach/monitor context."""
    def deco(fn: Callable) -> Callable:
        fn._needs = signal  # type: ignore[attr-defined]
        RULES.append(fn)
        return fn
    return deco


# --- shared connect-peer parsing (R9, reused by R2) -------------------------

_INET_ADDR = re.compile(r'inet_addr\("([^"]+)"\)')
_INET_PTON6 = re.compile(r'inet_pton\(AF_INET6,\s*"([^"]+)"')
_SIN_ADDR_KV = re.compile(r'sin6?_addr=[^,}]*?"([0-9a-fA-F:.]+)"')
_SUN_PATH = re.compile(r'sun_path="([^"]+)"')
_PORT = re.compile(r'sin6?_port=htons\((\d+)\)')
_FIRST_INT = re.compile(r"^\s*(-?\d+)")


def parse_connect_peer(args: str | None) -> str | None:
    """Best-effort "who is this connect talking to" from strace connect() args.

    Handles the shapes real programs produce: IPv4 `inet_addr("h")` with an
    optional `sin_port`, IPv6 `inet_pton(AF_INET6, "h")`, and AF_UNIX
    `sun_path="…"` (local Postgres/Redis default transport). Returns a stable key
    like `10.0.0.5:443`, `[::1]:6379`, or `unix:/var/run/…` — or None when the
    address family isn't one we key on."""
    if not args:
        return None
    m = _SUN_PATH.search(args)
    if m:
        return f"unix:{m.group(1)}"
    host: str | None = None
    m6 = _INET_PTON6.search(args)
    if m6:
        host = f"[{m6.group(1)}]"
    else:
        m4 = _INET_ADDR.search(args)
        if m4:
            host = m4.group(1)
        else:
            # some strace builds render the address as a bare sin_addr="..." kv
            mk = _SIN_ADDR_KV.search(args)
            if mk:
                h = mk.group(1)
                host = f"[{h}]" if ":" in h else h
    if host is None:
        return None
    p = _PORT.search(args)
    return f"{host}:{p.group(1)}" if p else host


def _syscall_fd(ev: TraceEvent) -> int | None:
    if ev.fd is not None:
        return ev.fd
    m = _FIRST_INT.match(ev.args or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# --- file I/O rules ---------------------------------------------------------

_OPEN_SYSCALLS = {"open", "openat", "creat"}
# Paths whose missing-ness is expected during normal startup (shared-lib search).
_LIB_HINTS = (".so", "/lib/", "/lib64/", "/usr/lib", "/etc/ld.so")
# Broader set of paths that runtimes/interpreters probe-and-miss as a matter of
# course (Python/Node import search, locale + system descriptors). Failed opens
# on these are noise, not app bugs — only counted out by `failed_file_opens`.
_PROBE_HINTS = _LIB_HINTS + (
    ".pyc", "__pycache__", "site-packages", "dist-packages", ".egg",
    ".dist-info", "node_modules", "/proc/", "/sys/", "/dev/",
    "/usr/share/locale", "/gconv/", "/etc/os-release", "/etc/lsb-release",
    ".terminfo", "/run/", "/var/run/",
    # PATH binary search + interpreter/venv startup probes — every program
    # does these and they routinely miss; counting them drowns real findings.
    "/bin/", "/sbin/", "/.local/", "/.npm-global/", "/.config/", "/.cache/",
    "pyvenv.cfg", "._pth", "pybuilddir", "Setup.local", "/.git/",
)


def _is_library_path(path: str | None) -> bool:
    if not path:
        return False
    return any(h in path for h in _LIB_HINTS)


def _is_probe_path(path: str | None) -> bool:
    if not path:
        return True  # no path -> can't be an app-level miss
    return any(h in path for h in _PROBE_HINTS)


@_needs("events")
def repeated_open_same_file(ctx: RuleContext) -> Anomaly | None:
    """Same file opened many times — a missing persistent handle (HIGH)."""
    by_path: dict[str, list[TraceEvent]] = defaultdict(list)
    for ev in ctx.events:
        if ev.syscall in _OPEN_SYSCALLS and ev.error is None and ev.path:
            if _is_library_path(ev.path):
                continue
            by_path[ev.path].append(ev)
    worst_path, worst = None, []
    for path, evs in by_path.items():
        if len(evs) > len(worst):
            worst_path, worst = path, evs
    if worst_path is None or len(worst) <= ctx.thresholds.repeated_open_min_opens:
        return None
    n = len(worst)
    return Anomaly(
        rule_id="repeated_open_same_file",
        severity="high",
        severity_score=_score("high", n),
        title=f"Repeatedly opening {worst_path}",
        description=(
            f"'{worst_path}' was opened {n} times during this run. Opening and "
            f"closing the same file in a loop is far slower than holding a "
            f"single persistent handle — cache the descriptor instead."
        ),
        evidence=worst[:20],
        first_seen_ms=worst[0].timestamp_ms,
        last_seen_ms=worst[-1].timestamp_ms,
        occurrence_count=n,
    )


@_needs("events")
def failed_file_opens(ctx: RuleContext) -> Anomaly | None:
    """App-level missing/forbidden files (MEDIUM).

    EACCES (permission denied) is high-signal and kept unless it's a pure
    library path. ENOENT (missing) is overwhelmingly startup/PATH probing, so
    it's filtered through the broad `_is_probe_path` set before counting.
    """
    openish = _OPEN_SYSCALLS | {"access", "stat", "newfstatat"}
    fails = []
    for ev in ctx.events:
        if ev.syscall not in openish:
            continue
        if ev.error == "EACCES" and not _is_library_path(ev.path):
            fails.append(ev)
        elif ev.error == "ENOENT" and not _is_probe_path(ev.path):
            fails.append(ev)
    if len(fails) <= ctx.thresholds.failed_open_min:
        return None
    n = len(fails)
    sample = ", ".join(sorted({ev.path for ev in fails if ev.path})[:5])
    return Anomaly(
        rule_id="failed_file_opens",
        severity="medium",
        severity_score=_score("medium", n),
        title=f"{n} failed file opens",
        description=(
            f"{n} file open/stat calls failed with ENOENT/EACCES on non-library "
            f"paths (e.g. {sample}). Missing config/data files or a wrong working "
            f"directory often hide here."
        ),
        evidence=fails[:20],
        first_seen_ms=fails[0].timestamp_ms,
        last_seen_ms=fails[-1].timestamp_ms,
        occurrence_count=n,
    )


# --- syscall latency rule ---------------------------------------------------

# Syscalls that are *supposed* to block; long durations are expected, not slow.
_BLOCKING_SYSCALLS = {
    "epoll_wait", "epoll_pwait", "poll", "ppoll", "select", "pselect6",
    "futex", "wait4", "waitid", "accept", "accept4", "nanosleep",
    "clock_nanosleep", "pause", "sigtimedwait", "rt_sigtimedwait",
    "io_getevents", "recvmsg", "recvfrom", "read", "readv", "recv",
    "msgrcv", "semop", "flock", "fcntl",
}


_POLL_FAMILY = {
    "poll", "ppoll", "select", "pselect6", "epoll_wait", "epoll_pwait",
}


@_needs("events")
def slow_network_connect(ctx: RuleContext) -> Anomaly | None:
    """A network connect that took over 1s (HIGH).

    Handles both shapes: a directly-blocking `connect()` whose own latency is
    high, and the non-blocking pattern (`connect` returns EINPROGRESS, then a
    `poll`/`select`/`epoll_wait` blocks waiting for it) that Python's
    `socket.settimeout` produces — where the slow time lives in the poll, not
    the connect, so the generic slow-syscall rule (which ignores poll) misses it.
    """
    thr = ctx.thresholds.slow_net_ms
    pending: dict[int, TraceEvent] = {}  # pid -> in-flight connect
    slow: list[TraceEvent] = []
    for ev in ctx.events:
        if ev.syscall == "connect":
            if ev.latency_ms is not None and ev.latency_ms > thr:
                slow.append(ev)  # blocking connect, slow on its own
                pending.pop(ev.pid, None)
            elif ev.error in ("EINPROGRESS", "EALREADY"):
                pending[ev.pid] = ev
            else:
                pending.pop(ev.pid, None)  # completed quickly
        elif ev.syscall in _POLL_FAMILY and ev.pid in pending:
            if ev.latency_ms is not None and ev.latency_ms > thr:
                slow.append(pending[ev.pid])  # the connect this poll waited on
            pending.pop(ev.pid, None)
    if not slow:
        return None
    n = len(slow)
    return Anomaly(
        rule_id="slow_network_connect",
        severity="high",
        severity_score=_score("high", n),
        title=f"Slow network connect — {n} connection(s) blocked >1s",
        description=(
            f"{n} outbound connect() attempt(s) took longer than 1s to complete "
            f"or time out. An unreachable/slow host, a missing route, or DNS/"
            f"firewall delay stalls the program here — add timeouts and retries, "
            f"and check the destination is reachable."
        ),
        evidence=slow[:20],
        first_seen_ms=min(e.timestamp_ms for e in slow),
        last_seen_ms=max(e.timestamp_ms for e in slow),
        occurrence_count=n,
    )


_READ_SYSCALLS = {"read", "recv", "recvfrom", "recvmsg", "readv"}


@_needs("events")
def slow_downstream_peer(ctx: RuleContext) -> Anomaly | None:
    """A downstream dependency (DB/cache/API) is slow to answer (HIGH).

    `read`/`recv` on a socket are *supposed* to block, so the generic slow-syscall
    rule excludes them — which makes "my Postgres query is slow" invisible. Here
    we join each `connect()` (fd → peer) with the subsequent long-blocking reads
    on that same fd, and flag when one peer's cumulative (or single worst) read
    wait dominates. Attach runs get this story from the off-CPU flamegraph
    instead; this rule is launch-only (needs the syscall stream)."""
    th = ctx.thresholds
    fd_peer: dict[tuple[int, int], str] = {}
    waits: dict[str, list[TraceEvent]] = defaultdict(list)
    peer_total: dict[str, float] = defaultdict(float)
    for e in ctx.events:
        if e.syscall == "connect":
            peer = parse_connect_peer(e.args)
            fd = _syscall_fd(e)
            if peer and fd is not None:
                fd_peer[(e.pid, fd)] = peer
        elif e.syscall == "close":
            fd = _syscall_fd(e)
            if fd is not None:
                fd_peer.pop((e.pid, fd), None)
        elif e.syscall in _READ_SYSCALLS and e.latency_ms is not None:
            fd = _syscall_fd(e)
            if fd is None:
                continue
            peer = fd_peer.get((e.pid, fd))
            if peer and e.latency_ms > th.downstream_read_min_ms:
                waits[peer].append(e)
                peer_total[peer] += e.latency_ms
    if not peer_total:
        return None
    peer, total = max(peer_total.items(), key=lambda kv: kv[1])
    evs = sorted(waits[peer], key=lambda e: e.timestamp_ms)
    worst = max(evs, key=lambda e: e.latency_ms or 0)
    if total < th.downstream_wait_ms and (worst.latency_ms or 0) < th.downstream_wait_ms:
        return None
    n = len(evs)
    return Anomaly(
        rule_id="slow_downstream_peer",
        severity="high",
        severity_score=_score("high", n),
        title=f"Slow downstream peer {peer} — {total / 1000:.1f}s waiting",
        description=(
            f"{n} read(s) from {peer} blocked over {th.downstream_read_min_ms:.0f}ms "
            f"each ({total / 1000:.1f}s total, slowest {worst.latency_ms / 1000:.2f}s). "
            f"The program is fast — the dependency it calls is slow. Check that "
            f"peer's health, add a timeout, and cache or batch the calls."
        ),
        evidence=evs[:20],
        first_seen_ms=evs[0].timestamp_ms,
        last_seen_ms=evs[-1].timestamp_ms,
        occurrence_count=n,
    )


@_needs("events")
def slow_syscall(ctx: RuleContext) -> Anomaly | None:
    """A non-blocking syscall that took over 1s (HIGH)."""
    thr = ctx.thresholds.slow_syscall_ms
    slow = [
        ev for ev in ctx.events
        if ev.latency_ms is not None and ev.latency_ms > thr
        and ev.syscall not in _BLOCKING_SYSCALLS
    ]
    if not slow:
        return None
    slow.sort(key=lambda e: e.latency_ms or 0, reverse=True)
    worst = slow[0]
    n = len(slow)
    return Anomaly(
        rule_id="slow_syscall",
        severity="high",
        severity_score=_score("high", n),
        title=f"Slow {worst.syscall}() — {worst.latency_ms / 1000:.2f}s",
        description=(
            f"{n} non-blocking syscall(s) took longer than 1s; the slowest was "
            f"{worst.syscall}() at {worst.latency_ms / 1000:.2f}s"
            + (f" on {worst.path}" if worst.path else "")
            + ". Slow disk, a stalled mount, or contention is the usual cause."
        ),
        evidence=slow[:20],
        first_seen_ms=min(e.timestamp_ms for e in slow),
        last_seen_ms=max(e.timestamp_ms for e in slow),
        occurrence_count=n,
    )


# --- metric-trend rules -----------------------------------------------------

def _series(metrics: list[dict], key: str) -> list[tuple[float, float]]:
    return [
        (m["timestamp_ms"], m[key])
        for m in metrics
        if m.get(key) is not None
    ]


# Trailing samples a whole-history trend rule falls back to on a long launch run.
_TREND_WINDOW_N = 360


def _trend_series(ctx: RuleContext, key: str) -> list[tuple[float, float]]:
    """Series for a whole-history trend rule. For a launch run longer than
    `trend_max_duration_ms` we return only the trailing window — the same
    sliding-window treatment the monitor path uses — so a long-lived server's
    warmup climb at the start isn't mistaken for a leak, while a genuine ongoing
    leak in the tail still surfaces. Short runs (and monitor windows, whose
    duration is the window span) use the full series unchanged."""
    series = _series(ctx.metrics, key)
    if ctx.duration_ms is not None and ctx.duration_ms > ctx.thresholds.trend_max_duration_ms:
        return series[-_TREND_WINDOW_N:]
    return series


@_needs("metrics")
def monotonic_memory_growth(ctx: RuleContext) -> Anomaly | None:
    """RSS climbs across the run and ends much higher than it started (HIGH)."""
    series = _trend_series(ctx, "rss_mb")
    if len(series) < 8:
        return None
    th = ctx.thresholds
    values = [v for _, v in series]
    first, last, peak = values[0], values[-1], max(values)
    decreases = sum(1 for a, b in zip(values, values[1:]) if b < a - 0.5)
    grew_enough = last - first > th.mem_growth_min_mb and last > first * th.mem_growth_ratio
    mostly_up = decreases < len(values) * 0.25
    if not (grew_enough and mostly_up):
        return None
    return Anomaly(
        rule_id="monotonic_memory_growth",
        severity="high",
        severity_score=_score("high", int(last - first)),
        title=f"Memory grew {first:.0f}MB → {last:.0f}MB",
        description=(
            f"Resident memory rose almost monotonically from {first:.0f}MB to "
            f"{last:.0f}MB (peak {peak:.0f}MB) and rarely fell. That pattern is "
            f"the signature of a leak or an unbounded buffer/cache."
        ),
        evidence=[],
        first_seen_ms=series[0][0],
        last_seen_ms=series[-1][0],
        occurrence_count=int(last - first),
    )


@_needs("metrics")
def fd_count_growing(ctx: RuleContext) -> Anomaly | None:
    """Open fd count trends upward and never recovers (CRITICAL)."""
    series = _trend_series(ctx, "open_fds")
    if len(series) < 8:
        return None
    values = [int(v) for _, v in series]
    first, last, peak = values[0], values[-1], max(values)
    decreases = sum(1 for a, b in zip(values, values[1:]) if b < a)
    if last - first < ctx.thresholds.fd_growth_min or decreases > len(values) * 0.25:
        return None
    return Anomaly(
        rule_id="fd_count_growing",
        severity="critical",
        severity_score=_score("critical", last - first),
        title=f"Open file descriptors grew {first} → {last}",
        description=(
            f"The open file-descriptor count climbed from {first} to {last} "
            f"(peak {peak}) without recovering — descriptors are being opened "
            f"and never closed. Left unchecked this ends in 'Too many open files'."
        ),
        evidence=[],
        first_seen_ms=series[0][0],
        last_seen_ms=series[-1][0],
        occurrence_count=last - first,
    )


@_needs("metrics")
def cpu_bound_no_syscalls(ctx: RuleContext) -> Anomaly | None:
    """Sustained high CPU with almost no syscalls — pure compute (MEDIUM).

    `cpu_pct` is psutil's per-core percentage summed across the tree (100% = one
    fully-busy core), so a single-threaded hot loop reads ~100% regardless of how
    many cores the host has. We threshold on raw saturation of ~one core, NOT on
    a fraction of all cores (which would never trip on a many-core machine).

    Needs `syscall_rate`, which is only backfilled from strace — so this is the
    LAUNCH-run variant; attach/monitor (no syscall stream) use `cpu_bound_metric`.
    """
    th = ctx.thresholds
    rows = [
        m for m in ctx.metrics
        if m.get("cpu_pct") is not None and m.get("syscall_rate") is not None
    ]
    if len(rows) < th.cpu_bound_min_samples:
        return None
    hot = [m for m in rows if m["cpu_pct"] > th.cpu_bound_pct and m["syscall_rate"] < th.cpu_bound_syscall_rate]
    # Require ~2s of sustained hot samples (≈8 ticks at 250ms).
    if len(hot) < th.cpu_bound_min_samples:
        return None
    return Anomaly(
        rule_id="cpu_bound_no_syscalls",
        severity="medium",
        severity_score=_score("medium", len(hot)),
        title="CPU-bound: high CPU, almost no syscalls",
        description=(
            f"For {len(hot)} samples the program ran above 90% of one core while "
            f"issuing almost no syscalls — it is compute-bound, not waiting on "
            f"I/O. If that is unexpected, look for a hot loop."
        ),
        evidence=[],
        first_seen_ms=hot[0]["timestamp_ms"],
        last_seen_ms=hot[-1]["timestamp_ms"],
        occurrence_count=len(hot),
    )


def _has_syscall_rate(rows: list[dict]) -> bool:
    """True when this is a strace-backed context (syscall_rate backfilled). The
    metric-only cpu/io rules skip it so they don't duplicate the strace-aware
    `cpu_bound_no_syscalls` on launch runs — they exist for attach/monitor, where
    there's no syscall stream."""
    return any(m.get("syscall_rate") is not None for m in rows)


@_needs("metrics")
def cpu_bound_metric(ctx: RuleContext) -> Anomaly | None:
    """Sustained ~one-core CPU with essentially no disk/socket I/O — the process
    is spinning on compute (MEDIUM). Metric-only, so it fires on attach/monitor
    runs (which carry no syscall events); on launch runs `cpu_bound_no_syscalls`
    already covers this from the syscall stream, so we defer to it there."""
    th = ctx.thresholds
    rows = [m for m in ctx.metrics if m.get("cpu_pct") is not None]
    if len(rows) < th.metric_min_samples or _has_syscall_rate(rows):
        return None
    hot = [
        m for m in rows
        if m["cpu_pct"] > th.cpu_bound_pct
        and (m.get("io_read_bps") or 0) < th.io_idle_bps
        and (m.get("io_write_bps") or 0) < th.io_idle_bps
    ]
    if len(hot) < th.metric_min_samples:
        return None
    return Anomaly(
        rule_id="cpu_bound_metric",
        severity="medium",
        severity_score=_score("medium", len(hot)),
        title="CPU-bound / spinning — high CPU, no I/O",
        description=(
            f"For {len(hot)} samples the process held above one full core of CPU "
            f"while doing almost no disk or network I/O — it's compute-bound, not "
            f"waiting on anything. If that's unexpected, profile for a hot loop "
            f"(the flamegraph shows where the cycles go)."
        ),
        evidence=[],
        first_seen_ms=hot[0]["timestamp_ms"],
        last_seen_ms=hot[-1]["timestamp_ms"],
        occurrence_count=len(hot),
    )


@_needs("metrics")
def io_wait_metric(ctx: RuleContext) -> Anomaly | None:
    """Low CPU alongside sustained disk/socket I/O — the process is I/O-bound,
    spending its time waiting rather than computing (MEDIUM). Metric-only, for
    attach/monitor runs (no syscall stream)."""
    th = ctx.thresholds
    rows = [m for m in ctx.metrics if m.get("cpu_pct") is not None]
    if len(rows) < th.metric_min_samples or _has_syscall_rate(rows):
        return None
    waiting = [
        m for m in rows
        if m["cpu_pct"] < th.io_wait_cpu_pct
        and ((m.get("io_read_bps") or 0) > th.io_active_bps
             or (m.get("io_write_bps") or 0) > th.io_active_bps)
    ]
    if len(waiting) < th.metric_min_samples:
        return None
    peak_bps = max(
        max(m.get("io_read_bps") or 0, m.get("io_write_bps") or 0) for m in waiting
    )
    return Anomaly(
        rule_id="io_wait_metric",
        severity="medium",
        severity_score=_score("medium", len(waiting)),
        title="I/O-bound / waiting — low CPU, sustained I/O",
        description=(
            f"For {len(waiting)} samples the process ran at low CPU while moving a "
            f"sustained stream of I/O (up to {peak_bps / (1024 * 1024):.1f} MB/s). "
            f"It's spending its time waiting on disk or the network, not computing "
            f"— the off-CPU flamegraph and latency histograms show where it blocks."
        ),
        evidence=[],
        first_seen_ms=waiting[0]["timestamp_ms"],
        last_seen_ms=waiting[-1]["timestamp_ms"],
        occurrence_count=len(waiting),
    )


# --- cgroup-limit-aware rules (R7, attach/monitor of a containerized target) --

@_needs("metrics")
def cpu_throttled(ctx: RuleContext) -> Anomaly | None:
    """A CPU-quota'd container pinned against its cgroup limit (HIGH).

    `cpu_pct` is per-core summed (100 = one full core), so a 0.5-core-quota
    container tops out around 50% and never trips the 90%-of-a-core gates. When we
    know the quota (attach to a containerized target) we scale the gate to it:
    sustained CPU at/above the quota ceiling means the container is compute-bound
    *and throttled by its own limit* — raising the quota (or the work) is the fix.
    Fail-open: no quota → no-op (bare-metal/launch runs)."""
    q = ctx.cgroup_cpu_quota_cores
    if not q or q <= 0:
        return None
    th = ctx.thresholds
    rows = [m for m in ctx.metrics if m.get("cpu_pct") is not None]
    if len(rows) < th.metric_min_samples:
        return None
    ceiling = q * 100.0 * th.cpu_throttled_ratio  # cpu_pct units (per-core summed)
    hot = [m for m in rows if m["cpu_pct"] >= ceiling]
    if len(hot) < th.metric_min_samples:
        return None
    peak = max(m["cpu_pct"] for m in hot)
    return Anomaly(
        rule_id="cpu_throttled",
        severity="high",
        severity_score=_score("high", len(hot)),
        title=f"CPU throttled — pinned at its {q:.2g}-core cgroup quota",
        description=(
            f"For {len(hot)} samples the container ran at up to {peak:.0f}% CPU "
            f"(per-core summed), at or above its {q:.2g}-core cgroup quota "
            f"({q * 100:.0f}%). It's compute-bound AND capped by its own CPU limit "
            f"— the kernel is throttling it. Raise the quota (or cut the work); the "
            f"flamegraph shows where the cycles go."
        ),
        evidence=[],
        first_seen_ms=hot[0]["timestamp_ms"],
        last_seen_ms=hot[-1]["timestamp_ms"],
        occurrence_count=len(hot),
    )


@_needs("metrics")
def rss_near_cgroup_limit(ctx: RuleContext) -> Anomaly | None:
    """RSS approaching the cgroup memory limit — imminent OOM-kill (CRITICAL).

    A container's memory.max is a hard ceiling: cross it and the kernel OOM-kills
    the process, no warning. When RSS climbs within `rss_cgroup_ratio` of the limit
    we flag it before the kill. Fail-open: no memory limit → no-op."""
    limit = ctx.cgroup_mem_limit_bytes
    if not limit or limit <= 0:
        return None
    th = ctx.thresholds
    series = _series(ctx.metrics, "rss_mb")
    if not series:
        return None
    limit_mb = limit / (1024 * 1024)
    peak = max(v for _, v in series)
    if peak < limit_mb * th.rss_cgroup_ratio:
        return None
    at_peak = [(t, v) for t, v in series if v == peak]
    return Anomaly(
        rule_id="rss_near_cgroup_limit",
        severity="critical",
        severity_score=_score("critical", int(peak)),
        title=f"RSS {peak:.0f}MB near cgroup limit {limit_mb:.0f}MB — imminent OOM",
        description=(
            f"Resident memory reached {peak:.0f}MB, {peak / limit_mb * 100:.0f}% of "
            f"the container's {limit_mb:.0f}MB cgroup memory limit. Crossing that "
            f"ceiling triggers an OOM-kill with no warning — raise the limit or find "
            f"the allocation/leak before the kernel kills the process."
        ),
        evidence=[],
        first_seen_ms=series[0][0],
        last_seen_ms=at_peak[-1][0],
        occurrence_count=int(peak),
    )


# --- additional §5 rules (syscall + metric derivable) ----------------------

@_needs("events")
def infinite_loop_no_progress(ctx: RuleContext) -> Anomaly | None:
    """Ran a long time while issuing almost no syscalls — stuck (CRITICAL)."""
    th = ctx.thresholds
    if ctx.duration_ms is None or ctx.duration_ms < th.infinite_loop_min_ms:
        return None
    if not ctx.events:
        # No syscall collector ran (attach runs, strace toggled off, sliding
        # monitor scans) — absence of events is absence of data, not evidence.
        return None
    syscalls = sum(1 for e in ctx.events if e.event_type == "syscall")
    if syscalls > th.infinite_loop_max_syscalls:
        return None
    return Anomaly(
        rule_id="infinite_loop_no_progress", severity="critical",
        severity_score=_score("critical", 1),
        title=f"No progress for {ctx.duration_ms / 1000:.0f}s",
        description=(
            f"The program ran {ctx.duration_ms / 1000:.0f}s but issued only "
            f"{syscalls} syscalls — no I/O, no waiting. That is the signature of "
            f"an infinite/busy loop making no system calls."
        ),
        occurrence_count=1,
    )


@_needs("metrics")
def memory_spike(ctx: RuleContext) -> Anomaly | None:
    """RSS jumped >100MB between two samples — an allocation burst (MEDIUM)."""
    thr = ctx.thresholds.memory_spike_mb
    series = _series(ctx.metrics, "rss_mb")
    spikes = [(t1, v1 - v0) for (_t0, v0), (t1, v1) in zip(series, series[1:]) if v1 - v0 > thr]
    if not spikes:
        return None
    biggest = max(s[1] for s in spikes)
    return Anomaly(
        rule_id="memory_spike", severity="medium",
        severity_score=_score("medium", len(spikes)),
        title=f"Memory spike +{biggest:.0f}MB",
        description=(
            f"RSS jumped by up to {biggest:.0f}MB between two samples "
            f"({len(spikes)} spike(s)). A large allocation burst — often loading "
            f"a whole file/dataset into memory at once."
        ),
        first_seen_ms=spikes[0][0], last_seen_ms=spikes[-1][0],
        occurrence_count=len(spikes),
    )


_SLOW_IO_SYSCALLS = {
    "write", "pwrite64", "writev", "fsync", "fdatasync", "openat", "open",
    "creat", "rename", "unlink", "truncate", "ftruncate",
}


@_needs("events")
def slow_file_io(ctx: RuleContext) -> Anomaly | None:
    """A file write/open/fsync over 100ms — slow disk/FS (HIGH)."""
    thr = ctx.thresholds.slow_file_io_ms
    slow = [
        e for e in ctx.events
        if e.latency_ms is not None and e.latency_ms > thr
        and e.syscall in _SLOW_IO_SYSCALLS and e.error is None
    ]
    if not slow:
        return None
    slow.sort(key=lambda e: e.latency_ms or 0, reverse=True)
    worst = slow[0]
    return Anomaly(
        rule_id="slow_file_io", severity="high", severity_score=_score("high", len(slow)),
        title=f"Slow file I/O — {worst.syscall}() {worst.latency_ms:.0f}ms",
        description=(
            f"{len(slow)} file-I/O call(s) took over 100ms; slowest "
            f"{worst.syscall}() at {worst.latency_ms:.0f}ms"
            + (f" on {worst.path}" if worst.path else "")
            + ". Slow disk, a network filesystem, or fsync pressure is the cause."
        ),
        evidence=slow[:20],
        first_seen_ms=min(e.timestamp_ms for e in slow),
        last_seen_ms=max(e.timestamp_ms for e in slow),
        occurrence_count=len(slow),
    )


@_needs("events")
def excessive_subprocess(ctx: RuleContext) -> Anomaly | None:
    """More than 50 execve — shelling out in a loop (MEDIUM)."""
    execs = [e for e in ctx.events if e.syscall in ("execve", "execveat") and e.error is None]
    if len(execs) <= ctx.thresholds.excessive_subprocess_min:
        return None
    return Anomaly(
        rule_id="excessive_subprocess", severity="medium",
        severity_score=_score("medium", len(execs)),
        title=f"{len(execs)} subprocesses spawned",
        description=(
            f"The program exec'd {len(execs)} times. Spawning many short-lived "
            f"processes (shelling out in a loop) is far slower than doing the work "
            f"in-process or batching it."
        ),
        evidence=execs[:20],
        first_seen_ms=execs[0].timestamp_ms, last_seen_ms=execs[-1].timestamp_ms,
        occurrence_count=len(execs),
    )


_CONN_ERRORS = {"ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH"}
_NET_SYSCALLS = {"connect", "sendto", "recvfrom", "send", "recv", "read", "write", "sendmsg", "recvmsg"}


@_needs("events")
def connection_error(ctx: RuleContext) -> Anomaly | None:
    """Network calls failing with refused/reset/timeout (HIGH)."""
    errs = [e for e in ctx.events if e.error in _CONN_ERRORS and e.syscall in _NET_SYSCALLS]
    if not errs:
        return None
    kinds = sorted({e.error for e in errs if e.error})
    return Anomaly(
        rule_id="connection_error", severity="high", severity_score=_score("high", len(errs)),
        title=f"Network errors: {', '.join(kinds)}",
        description=(
            f"{len(errs)} network call(s) failed with {', '.join(kinds)} — the "
            f"remote host refused the connection, reset it, or was unreachable. "
            f"Check the destination address, port, and firewall."
        ),
        evidence=errs[:20],
        first_seen_ms=errs[0].timestamp_ms, last_seen_ms=errs[-1].timestamp_ms,
        occurrence_count=len(errs),
    )


@_needs("events")
def no_connection_reuse(ctx: RuleContext) -> Anomaly | None:
    """Many separate TCP/socket connects to the same peer — no pooling (HIGH)."""
    hosts: dict[str, int] = defaultdict(int)
    for e in ctx.events:
        if e.syscall == "connect" and e.error in (None, "EINPROGRESS"):
            peer = parse_connect_peer(e.args)
            if peer:
                hosts[peer] += 1
    if not hosts:
        return None
    host, cnt = max(hosts.items(), key=lambda kv: kv[1])
    if cnt <= ctx.thresholds.conn_reuse_min:
        return None
    return Anomaly(
        rule_id="no_connection_reuse", severity="high", severity_score=_score("high", cnt),
        title=f"No connection reuse — {cnt} connects to {host}",
        description=(
            f"The program opened {cnt} separate connections to {host}. "
            f"Re-establishing a connection per request is slow — use a connection "
            f"pool or HTTP keep-alive."
        ),
        occurrence_count=cnt,
    )


@_needs("events")
def mutex_contention(ctx: RuleContext) -> Anomaly | None:
    """Repeated futex waits over 10ms — lock contention (HIGH)."""
    th = ctx.thresholds
    slow = [e for e in ctx.events if e.syscall == "futex" and e.latency_ms is not None and e.latency_ms > th.mutex_ms]
    if len(slow) <= th.mutex_min:
        return None
    total = sum(e.latency_ms or 0 for e in slow)
    return Anomaly(
        rule_id="mutex_contention", severity="high", severity_score=_score("high", len(slow)),
        title=f"Lock contention — {len(slow)} slow futex waits",
        description=(
            f"{len(slow)} futex (lock) waits took over 10ms ({total / 1000:.1f}s "
            f"total). Threads are blocking on a contended lock — a serialization "
            f"bottleneck. Shorten the critical section or shard the lock."
        ),
        evidence=slow[:20],
        first_seen_ms=slow[0].timestamp_ms, last_seen_ms=slow[-1].timestamp_ms,
        occurrence_count=len(slow),
    )


@_needs("events")
def io_retry_loop(ctx: RuleContext) -> Anomaly | None:
    """Same syscall busy-retrying with EAGAIN/EINTR >100× on one fd (HIGH)."""
    counts: dict[tuple, list[TraceEvent]] = defaultdict(list)
    for e in ctx.events:
        if e.error in ("EAGAIN", "EWOULDBLOCK", "EINTR") and e.fd is not None and e.syscall:
            counts[(e.pid, e.syscall, e.fd)].append(e)
    if not counts:
        return None
    (_pid, sc, fd), evs = max(counts.items(), key=lambda kv: len(kv[1]))
    if len(evs) <= ctx.thresholds.io_retry_min:
        return None
    return Anomaly(
        rule_id="io_retry_loop", severity="high", severity_score=_score("high", len(evs)),
        title=f"I/O retry loop — {sc}() retried {len(evs)}×",
        description=(
            f"{sc}() on fd {fd} returned EAGAIN/EINTR {len(evs)} times — the program "
            f"is busy-retrying a non-blocking operation instead of waiting for "
            f"readiness (poll/epoll). This burns CPU for nothing."
        ),
        evidence=evs[:20], occurrence_count=len(evs),
    )


@_needs("events")
def small_read_storm(ctx: RuleContext) -> Anomaly | None:
    """Thousands of sub-512-byte reads on one fd — unbuffered I/O (MEDIUM)."""
    by_fd: dict[tuple, list[TraceEvent]] = defaultdict(list)
    for e in ctx.events:
        if e.syscall in ("read", "pread64", "recv", "recvfrom") and e.error is None and e.fd is not None:
            r = _retval_int(e)
            if r is not None and 0 < r < 512:
                by_fd[(e.pid, e.fd)].append(e)
    if not by_fd:
        return None
    evs = max(by_fd.values(), key=len)
    if len(evs) <= ctx.thresholds.small_read_min:
        return None
    return Anomaly(
        rule_id="small_read_storm", severity="medium", severity_score=_score("medium", len(evs)),
        title=f"Small-read storm — {len(evs)} tiny reads",
        description=(
            f"{len(evs)} reads returned fewer than 512 bytes on a single descriptor. "
            f"Reading in tiny chunks is slow — wrap it in a buffered reader."
        ),
        evidence=evs[:20],
        first_seen_ms=evs[0].timestamp_ms, last_seen_ms=evs[-1].timestamp_ms,
        occurrence_count=len(evs),
    )


@_needs("events")
def write_storm(ctx: RuleContext) -> Anomaly | None:
    """Over 1000 writes to one file descriptor — logging in a hot loop (MEDIUM)."""
    by_fd: dict[tuple, list[TraceEvent]] = defaultdict(list)
    for e in ctx.events:
        if e.syscall in ("write", "pwrite64", "writev") and e.error is None \
                and e.fd is not None and e.fd > 2:
            by_fd[(e.pid, e.fd)].append(e)
    if not by_fd:
        return None
    evs = max(by_fd.values(), key=len)
    if len(evs) <= ctx.thresholds.write_storm_min:
        return None
    return Anomaly(
        rule_id="write_storm", severity="medium", severity_score=_score("medium", len(evs)),
        title=f"Write storm — {len(evs)} writes to one file",
        description=(
            f"{len(evs)} write() calls to a single descriptor. If this is a log "
            f"file, you are likely logging inside a hot loop — batch or rate-limit."
        ),
        evidence=evs[:20],
        first_seen_ms=evs[0].timestamp_ms, last_seen_ms=evs[-1].timestamp_ms,
        occurrence_count=len(evs),
    )


@_needs("events")
def spin_loop(ctx: RuleContext) -> Anomaly | None:
    """High CPU + thousands of immediately-returning polls — busy-wait (HIGH)."""
    th = ctx.thresholds
    immediate = [
        e for e in ctx.events
        if e.syscall in _POLL_FAMILY and e.latency_ms is not None and e.latency_ms < 1.0
    ]
    if len(immediate) <= th.spin_loop_min:
        return None
    if not any((m.get("cpu_pct") or 0) > th.spin_cpu_pct for m in ctx.metrics):
        return None
    return Anomaly(
        rule_id="spin_loop", severity="high", severity_score=_score("high", len(immediate)),
        title=f"Busy-wait spin loop — {len(immediate)} non-blocking polls",
        description=(
            f"{len(immediate)} poll/epoll/select calls returned immediately "
            f"(timeout≈0) while CPU was high — the program is busy-polling in a "
            f"tight loop instead of blocking for events. Use a blocking wait."
        ),
        evidence=immediate[:20], occurrence_count=len(immediate),
    )


def run_rules(ctx: RuleContext) -> list[Anomaly]:
    found: list[Anomaly] = []
    for rule in RULES:
        needs = getattr(rule, "_needs", None)
        if needs == "events" and not ctx.events:
            continue
        if needs == "metrics" and not ctx.metrics:
            continue
        try:
            result = rule(ctx)
        except Exception:  # noqa: BLE001 — one bad rule must not sink the run
            continue
        if result is not None:
            found.append(result)
    found.sort(key=lambda a: a.severity_score, reverse=True)
    return found
