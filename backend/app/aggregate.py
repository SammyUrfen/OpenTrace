"""Pure aggregations over a run's event stream (no I/O, easily unit-tested).

Currently: per-syscall statistics for the Syscall Explorer tab, computed from
the full `events.ndjson.zst` stream (decoded to dicts by the caller).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v, 0) if isinstance(v, str) else int(v)
    except (ValueError, TypeError):
        return None


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 4)
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    val = sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
    return round(val, 4)


def syscall_stats(events: Iterable[dict]) -> list[dict]:
    """Aggregate per-syscall: count, latency total/avg/P50/P95/P99, errors, %time.

    `events` are decoded ndjson dicts (from `TraceEvent.to_ndjson`). Only
    `event_type == 'syscall'` rows are considered. `%time` is the share of total
    in-syscall wall time attributable to each syscall.
    """
    latencies: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)

    for e in events:
        if e.get("event_type") != "syscall":
            continue
        name = e.get("syscall")
        if not name:
            continue
        counts[name] += 1
        lat = e.get("latency_ms")
        if lat is not None:
            latencies[name].append(float(lat))
        if e.get("error"):
            errors[name] += 1

    total_latency = sum(sum(v) for v in latencies.values()) or 1.0

    rows: list[dict] = []
    for name, count in counts.items():
        lat = sorted(latencies[name])
        total = sum(lat)
        rows.append({
            "syscall": name,
            "count": count,
            "total_ms": round(total, 3),
            "avg_ms": round(total / len(lat), 4) if lat else None,
            "p50_ms": _percentile(lat, 50),
            "p95_ms": _percentile(lat, 95),
            "p99_ms": _percentile(lat, 99),
            "errors": errors[name],
            "pct_runtime": round(total / total_latency * 100.0, 2),
        })
    rows.sort(key=lambda r: r["total_ms"], reverse=True)
    return rows


_OPEN = {"open", "openat", "creat"}
_READ = {"read", "pread64", "readv", "preadv"}
_WRITE = {"write", "pwrite64", "writev", "pwritev"}


def io_stats(events: Iterable[dict]) -> list[dict]:
    """Per-file I/O: opens/closes, read/write counts + bytes, and leaked fds.

    Resolves read/write byte counts to their file by tracking the fd a
    successful `open*` returned, so each row is one path the program touched.
    `leaked` counts fds that were opened and never closed by the end of the run
    (a heuristic: meaningful for long-running processes, expected for short ones
    that rely on exit to close — the UI labels it accordingly).
    """
    fd_path: dict[tuple[int, int], str] = {}
    stats: dict[str, dict] = {}

    def row(path: str) -> dict:
        return stats.setdefault(path, {
            "path": path, "opens": 0, "closes": 0, "reads": 0,
            "writes": 0, "read_bytes": 0, "write_bytes": 0, "leaked": 0,
        })

    for e in events:
        if e.get("event_type") != "syscall":
            continue
        sc = e.get("syscall")
        pid = e.get("pid")
        if e.get("error") is not None:
            continue
        ret = _int(e.get("retval"))
        if sc in _OPEN and ret is not None and ret >= 0 and e.get("path"):
            fd_path[(pid, ret)] = e["path"]
            row(e["path"])["opens"] += 1
        elif sc == "close":
            fd = e.get("fd")
            p = fd_path.pop((pid, fd), None)
            if p:
                row(p)["closes"] += 1
        elif sc in _READ and ret is not None and ret >= 0:
            p = fd_path.get((pid, e.get("fd")))
            if p:
                r = row(p)
                r["reads"] += 1
                r["read_bytes"] += ret
        elif sc in _WRITE and ret is not None and ret >= 0:
            p = fd_path.get((pid, e.get("fd")))
            if p:
                r = row(p)
                r["writes"] += 1
                r["write_bytes"] += ret

    for (_pid, _fd), p in fd_path.items():
        if p in stats:
            stats[p]["leaked"] += 1

    rows = list(stats.values())
    rows.sort(
        key=lambda r: (r["opens"] + r["reads"] + r["writes"]), reverse=True
    )
    return rows


_FORK_SYSCALLS = {"clone", "clone3", "fork", "vfork"}
_EPHEMERAL_MS = 250.0


def process_stats(events: Iterable[dict]) -> list[dict]:
    """Per-process summary distilled from the event stream.

    Resolves each PID's command (from `execve`), parent (from a `clone`/`fork`
    return value), syscall count, and lifespan. `ephemeral` flags processes that
    lived under one metric sample (≤250ms) — easy to miss in the live poller but
    captured in the trace.
    """
    info: dict[int, dict] = {}

    def rec(pid: int) -> dict:
        return info.setdefault(pid, {
            "pid": pid, "parent_pid": None, "command": None,
            "syscalls": 0, "first_ms": None, "last_ms": None, "exited": False,
        })

    for e in events:
        pid = e.get("pid")
        if pid is None:
            continue
        r = rec(pid)
        ts = e.get("timestamp_ms")
        if ts is not None:
            if r["first_ms"] is None or ts < r["first_ms"]:
                r["first_ms"] = ts
            if r["last_ms"] is None or ts > r["last_ms"]:
                r["last_ms"] = ts
        et = e.get("event_type")
        sc = e.get("syscall")
        if et == "syscall":
            r["syscalls"] += 1
        if sc in ("execve", "execveat") and e.get("path"):
            r["command"] = e["path"]
        elif sc in _FORK_SYSCALLS:
            child = _int(e.get("retval"))
            if child and child > 0:
                rec(child)["parent_pid"] = pid
        if et == "exit":
            r["exited"] = True

    rows: list[dict] = []
    for r in info.values():
        dur = (
            r["last_ms"] - r["first_ms"]
            if r["first_ms"] is not None and r["last_ms"] is not None
            else None
        )
        rows.append({
            **r,
            "duration_ms": round(dur, 1) if dur is not None else None,
            "ephemeral": dur is not None and dur <= _EPHEMERAL_MS,
        })
    rows.sort(key=lambda x: x["syscalls"], reverse=True)
    return rows


_FAM = re.compile(r"sa_family=(AF_\w+)")
_INET_ADDR = re.compile(r'inet_addr\("([^"]+)"\)')
_INET6_ADDR = re.compile(r'inet_pton\(AF_INET6,\s*"([^"]+)"')
_PORT = re.compile(r"sin6?_port=htons\((\d+)\)")
_UNIX_PATH = re.compile(r'sun_path="([^"]+)"')
_NET_POLL = {"poll", "ppoll", "select", "pselect6", "epoll_wait", "epoll_pwait"}


def network_stats(events: Iterable[dict]) -> list[dict]:
    """Outbound connections parsed from `connect()` syscalls.

    Resolves the destination (IPv4/IPv6/unix) from strace's sockaddr dump and the
    connection outcome. For the non-blocking pattern (connect→EINPROGRESS, then a
    poll/select waits), the wait latency and timeout/success are folded back onto
    the connect, so a stalled connection shows its true duration.

    Note: DNS resolution (`getaddrinfo`) is a libc call, invisible to strace —
    surfacing it needs ltrace (Phase 6).
    """
    pending: dict[int, int] = {}  # pid -> index of in-flight connect
    conns: list[dict] = []

    for e in events:
        if e.get("event_type") != "syscall":
            continue
        sc = e.get("syscall")
        pid = e.get("pid")
        if sc == "connect":
            args = e.get("args") or ""
            fam_m = _FAM.search(args)
            fam = fam_m.group(1) if fam_m else "AF_?"
            address = port = None
            if fam == "AF_INET":
                m = _INET_ADDR.search(args)
                address = m.group(1) if m else None
                pm = _PORT.search(args)
                port = int(pm.group(1)) if pm else None
            elif fam == "AF_INET6":
                m = _INET6_ADDR.search(args)
                address = m.group(1) if m else None
                pm = _PORT.search(args)
                port = int(pm.group(1)) if pm else None
            elif fam == "AF_UNIX":
                m = _UNIX_PATH.search(args)
                address = m.group(1) if m else None
            err = e.get("error")
            lat = e.get("latency_ms")
            result = "connecting" if err in ("EINPROGRESS", "EALREADY") else (err or "ok")
            conn = {
                "family": fam, "address": address, "port": port,
                "result": result,
                "latency_ms": round(lat, 2) if lat is not None else None,
                "pid": pid,
            }
            conns.append(conn)
            if result == "connecting":
                pending[pid] = len(conns) - 1
            else:
                pending.pop(pid, None)
        elif sc in _NET_POLL and pid in pending:
            idx = pending.pop(pid)
            lat = e.get("latency_ms")
            if lat is not None:
                base = conns[idx]["latency_ms"] or 0.0
                conns[idx]["latency_ms"] = round(base + lat, 2)
            pret = _int(e.get("retval"))
            if conns[idx]["result"] == "connecting":
                conns[idx]["result"] = "timed out" if pret == 0 else "ok"

    return conns
