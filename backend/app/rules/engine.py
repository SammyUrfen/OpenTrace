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


# --- additional §5 rules (syscall + metric derivable) ----------------------

def infinite_loop_no_progress(ctx: RuleContext) -> Anomaly | None:
    """Ran a long time while issuing almost no syscalls — stuck (CRITICAL)."""
    if ctx.duration_ms is None or ctx.duration_ms < 30_000:
        return None
    syscalls = sum(1 for e in ctx.events if e.event_type == "syscall")
    if syscalls > 200:
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


def memory_spike(ctx: RuleContext) -> Anomaly | None:
    """RSS jumped >100MB between two samples — an allocation burst (MEDIUM)."""
    series = _series(ctx.metrics, "rss_mb")
    spikes = [(t1, v1 - v0) for (_t0, v0), (t1, v1) in zip(series, series[1:]) if v1 - v0 > 100]
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


def slow_file_io(ctx: RuleContext) -> Anomaly | None:
    """A file write/open/fsync over 100ms — slow disk/FS (HIGH)."""
    slow = [
        e for e in ctx.events
        if e.latency_ms is not None and e.latency_ms > 100
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


def excessive_subprocess(ctx: RuleContext) -> Anomaly | None:
    """More than 50 execve — shelling out in a loop (MEDIUM)."""
    execs = [e for e in ctx.events if e.syscall in ("execve", "execveat") and e.error is None]
    if len(execs) <= 50:
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


_INET_ADDR = re.compile(r'inet_addr\("([^"]+)"\)')


def no_connection_reuse(ctx: RuleContext) -> Anomaly | None:
    """Many separate TCP connects to the same host — no pooling (HIGH)."""
    hosts: dict[str, int] = defaultdict(int)
    for e in ctx.events:
        if e.syscall == "connect" and e.error in (None, "EINPROGRESS"):
            m = _INET_ADDR.search(e.args or "")
            if m:
                hosts[m.group(1)] += 1
    if not hosts:
        return None
    host, cnt = max(hosts.items(), key=lambda kv: kv[1])
    if cnt <= 5:
        return None
    return Anomaly(
        rule_id="no_connection_reuse", severity="high", severity_score=_score("high", cnt),
        title=f"No connection reuse — {cnt} connects to {host}",
        description=(
            f"The program opened {cnt} separate TCP connections to {host}. "
            f"Re-establishing a connection per request is slow — use a connection "
            f"pool or HTTP keep-alive."
        ),
        occurrence_count=cnt,
    )


def mutex_contention(ctx: RuleContext) -> Anomaly | None:
    """Repeated futex waits over 10ms — lock contention (HIGH)."""
    slow = [e for e in ctx.events if e.syscall == "futex" and e.latency_ms is not None and e.latency_ms > 10]
    if len(slow) <= 20:
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


def io_retry_loop(ctx: RuleContext) -> Anomaly | None:
    """Same syscall busy-retrying with EAGAIN/EINTR >100× on one fd (HIGH)."""
    counts: dict[tuple, list[TraceEvent]] = defaultdict(list)
    for e in ctx.events:
        if e.error in ("EAGAIN", "EWOULDBLOCK", "EINTR") and e.fd is not None and e.syscall:
            counts[(e.pid, e.syscall, e.fd)].append(e)
    if not counts:
        return None
    (_pid, sc, fd), evs = max(counts.items(), key=lambda kv: len(kv[1]))
    if len(evs) <= 100:
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
    if len(evs) <= 2000:
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
    if len(evs) <= 1000:
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


def spin_loop(ctx: RuleContext) -> Anomaly | None:
    """High CPU + thousands of immediately-returning polls — busy-wait (HIGH)."""
    immediate = [
        e for e in ctx.events
        if e.syscall in _POLL_FAMILY and e.latency_ms is not None and e.latency_ms < 1.0
    ]
    if len(immediate) <= 2000:
        return None
    if not any((m.get("cpu_pct") or 0) > 80 for m in ctx.metrics):
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


RULES: list[Callable[[RuleContext], Anomaly | None]] = [
    repeated_open_same_file,
    failed_file_opens,
    slow_syscall,
    slow_network_connect,
    slow_file_io,
    monotonic_memory_growth,
    memory_spike,
    fd_count_growing,
    cpu_bound_no_syscalls,
    spin_loop,
    infinite_loop_no_progress,
    excessive_subprocess,
    connection_error,
    no_connection_reuse,
    mutex_contention,
    io_retry_loop,
    small_read_storm,
    write_storm,
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
