"""Detection rules over a finalized run's events + metrics.

A deliberately noise-controlled subset of Roadmap §5 — every rule here is
computable from strace + psutil alone and tuned to avoid the obvious false
positives (e.g. the dynamic linker's library probing, programs that exit
without explicitly closing fds, syscalls that are *supposed* to block).

Each rule is `fn(ctx: RuleContext) -> Anomaly | None`. Add a rule by writing
the function and appending it to `RULES`. The engine fills severity scores so
rankings stay within severity bands.

Public surface:
- `RuleContext`
- `run_rules(ctx) -> list[Anomaly]`
"""
from __future__ import annotations

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
class RuleContext:
    events: list[TraceEvent]
    metrics: list[dict] = field(default_factory=list)  # DB rows (post-backfill)
    duration_ms: int | None = None
    cpu_cores: int = 1


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
    if worst_path is None or len(worst) <= 10:
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
    if len(fails) <= 5:
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
_SLOW_NET_MS = 1000.0


def slow_network_connect(ctx: RuleContext) -> Anomaly | None:
    """A network connect that took over 1s (HIGH).

    Handles both shapes: a directly-blocking `connect()` whose own latency is
    high, and the non-blocking pattern (`connect` returns EINPROGRESS, then a
    `poll`/`select`/`epoll_wait` blocks waiting for it) that Python's
    `socket.settimeout` produces — where the slow time lives in the poll, not
    the connect, so the generic slow-syscall rule (which ignores poll) misses it.
    """
    pending: dict[int, TraceEvent] = {}  # pid -> in-flight connect
    slow: list[TraceEvent] = []
    for ev in ctx.events:
        if ev.syscall == "connect":
            if ev.latency_ms is not None and ev.latency_ms > _SLOW_NET_MS:
                slow.append(ev)  # blocking connect, slow on its own
                pending.pop(ev.pid, None)
            elif ev.error in ("EINPROGRESS", "EALREADY"):
                pending[ev.pid] = ev
            else:
                pending.pop(ev.pid, None)  # completed quickly
        elif ev.syscall in _POLL_FAMILY and ev.pid in pending:
            if ev.latency_ms is not None and ev.latency_ms > _SLOW_NET_MS:
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


def slow_syscall(ctx: RuleContext) -> Anomaly | None:
    """A non-blocking syscall that took over 1s (HIGH)."""
    slow = [
        ev for ev in ctx.events
        if ev.latency_ms is not None and ev.latency_ms > 1000.0
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


def monotonic_memory_growth(ctx: RuleContext) -> Anomaly | None:
    """RSS climbs across the run and ends much higher than it started (HIGH)."""
    series = _series(ctx.metrics, "rss_mb")
    if len(series) < 8:
        return None
    values = [v for _, v in series]
    first, last, peak = values[0], values[-1], max(values)
    decreases = sum(1 for a, b in zip(values, values[1:]) if b < a - 0.5)
    grew_enough = last - first > 50 and last > first * 1.4
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


def fd_count_growing(ctx: RuleContext) -> Anomaly | None:
    """Open fd count trends upward and never recovers (CRITICAL)."""
    series = _series(ctx.metrics, "open_fds")
    if len(series) < 8:
        return None
    values = [int(v) for _, v in series]
    first, last, peak = values[0], values[-1], max(values)
    decreases = sum(1 for a, b in zip(values, values[1:]) if b < a)
    if last - first < 30 or decreases > len(values) * 0.25:
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


def cpu_bound_no_syscalls(ctx: RuleContext) -> Anomaly | None:
    """Sustained high CPU with almost no syscalls — pure compute (MEDIUM).

    `cpu_pct` is psutil's per-core percentage summed across the tree (100% = one
    fully-busy core), so a single-threaded hot loop reads ~100% regardless of how
    many cores the host has. We threshold on raw saturation of ~one core, NOT on
    a fraction of all cores (which would never trip on a many-core machine).
    """
    rows = [
        m for m in ctx.metrics
        if m.get("cpu_pct") is not None and m.get("syscall_rate") is not None
    ]
    if len(rows) < 8:
        return None
    hot = [m for m in rows if m["cpu_pct"] > 90 and m["syscall_rate"] < 50]
    # Require ~2s of sustained hot samples (≈8 ticks at 250ms).
    if len(hot) < 8:
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


RULES: list[Callable[[RuleContext], Anomaly | None]] = [
    repeated_open_same_file,
    failed_file_opens,
    slow_syscall,
    slow_network_connect,
    monotonic_memory_growth,
    fd_count_growing,
    cpu_bound_no_syscalls,
]


def run_rules(ctx: RuleContext) -> list[Anomaly]:
    found: list[Anomaly] = []
    for rule in RULES:
        try:
            result = rule(ctx)
        except Exception:  # noqa: BLE001 — one bad rule must not sink the run
            continue
        if result is not None:
            found.append(result)
    found.sort(key=lambda a: a.severity_score, reverse=True)
    return found
