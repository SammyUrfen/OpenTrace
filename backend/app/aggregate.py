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


# --- request tracing: endpoint RED + endpoint→query correlation --------------
#
# Consumes `Span` objects (events.Span) from the attach request program. All times
# are CLOCK_MONOTONIC ns (self-consistent within a run) — see Span. Pure + testable.

_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]{16,}\Z")


def _normalize_route(route: str) -> str:
    """Templatize dynamic path segments so `/users/123` and `/users/456` roll up as one
    `/users/{id}` (a per-raw-path RED table is useless). Numeric → `{id}`, UUID →
    `{uuid}`, long hex → `{hex}`; everything else is kept verbatim.

    EMPTY segments are dropped, which canonicalizes a trailing slash, a leading `//`, and
    duplicate inner slashes: `/users/123`, `/users/123/`, and `//users/123` all become
    `/users/{id}`. Otherwise those variants would split one endpoint into several RED rows
    and — in monitor mode — several `slow_endpoint:<route>` incidents that collapse-by-rule
    can't merge (breaking the one-incident-per-endpoint invariant)."""
    if not route:
        return "/"
    out = []
    for seg in route.split("/"):
        if seg == "":
            continue  # drop empties → collapse //, leading /, and trailing /
        elif seg.isdigit():
            out.append("{id}")
        elif _UUID_RE.match(seg):
            out.append("{uuid}")
        elif _HEX_RE.match(seg):
            out.append("{hex}")
        else:
            out.append(seg)
    return "/" + "/".join(out)


def correlate_spans(http_spans: list, db_spans: list) -> dict:
    """Nest each db span under the http span on the SAME tid whose [start, end] window
    contains its start (the thread-per-request join — exact for sync WSGI/native, and
    the reason the tid is a promoted field). Mutates each http span's `db_ms` to the sum
    of its nested db-span ms; returns {id(http_span): [db_span, …]} for the waterfall.

    DB time is an OVERLAY on the request's off-CPU-network wait, never a 4th additive
    bucket (the thread is blocked in recv WHILE the query runs), so `db_ms` is clamped to
    the request's own duration.

    Each db span is attributed to EXACTLY ONE http span — the innermost (latest-started)
    containing window on its tid. Without that single-owner rule, a coroutine/greenlet
    server that multiplexes concurrent requests onto one OS thread (gevent+psycopg2,
    psycopg3-async) would attribute one query to every overlapping same-tid request,
    double-counting db time and pushing db_ms_share past 100%."""
    http_by_tid: dict[int, list] = defaultdict(list)
    for h in http_spans:
        http_by_tid[h.tid].append(h)
    for hs in http_by_tid.values():
        hs.sort(key=lambda s: s.start_ns)  # ascending → last match is the innermost owner

    nested: dict[int, list] = defaultdict(list)
    for d in db_spans:
        owner = None
        for h in http_by_tid.get(d.tid, ()):
            if h.start_ns <= d.start_ns <= h.start_ns + h.dur_ns:
                owner = h
        if owner is not None:
            nested[id(owner)].append(d)

    for h in http_spans:
        kids = nested.get(id(h), [])
        h.db_ms = round(min(sum(d.dur_ns for d in kids), h.dur_ns) / 1e6, 3)
    return dict(nested)


_OFF_REASONS = ("net", "lock", "sleep", "disk", "other")


def _overlap_ns(a0: int, a1: int, b0: int, b1: int) -> int:
    """Nanoseconds where interval [a0,a1] and [b0,b1] overlap (0 if disjoint)."""
    lo, hi = max(a0, b0), min(a1, b1)
    return hi - lo if hi > lo else 0


def correlate_breakdown(http_spans: list, nested: dict, intervals: list[dict]) -> None:
    """Decompose each request's wall time into on-CPU / run-queue / db-wait / other-off-CPU
    using the per-tid off-CPU (`OFF`) + run-queue (`RQ`) intervals from the request program
    (ebpf.parse_bpftrace_offcpu). Writes `span.attrs['breakdown']` on each http span:

      { on_cpu_ms, runq_ms, db_wait_ms, other_off_ms, off_reasons:{net,lock,sleep,disk,other} }

    These four buckets SUM to the request duration (thread-per-request). db_wait_ms is the
    off-CPU time overlapping this request's DB spans (the network wait for a remote DB like
    Postgres); other_off_ms is the rest of the off-CPU (labelled by blocking-syscall reason).
    on_cpu_ms = dur − off − runq. NOTE db_ms (the DB overlay) can exceed db_wait_ms for an
    IN-PROCESS DB (SQLite runs ON-CPU), which is why db stays a separate overlay, not a bucket."""
    # bucket intervals by tid for an O(requests·intervals_on_tid) join
    off_by_tid: dict[int, list] = defaultdict(list)
    rq_by_tid: dict[int, list] = defaultdict(list)
    for iv in intervals:
        (off_by_tid if iv["kind"] == "off" else rq_by_tid)[iv["tid"]].append(iv)

    for h in http_spans:
        h0, h1 = h.start_ns, h.start_ns + h.dur_ns
        dbs = nested.get(id(h), ())
        off_ns = 0
        db_wait_ns = 0
        reasons = {r: 0 for r in _OFF_REASONS}
        for iv in off_by_tid.get(h.tid, ()):
            ov = _overlap_ns(iv["start_ns"], iv["start_ns"] + iv["dur_ns"], h0, h1)
            if ov <= 0:
                continue
            off_ns += ov
            # portion of this off-CPU interval that overlaps a DB span → db-wait
            iv0, iv1 = iv["start_ns"], iv["start_ns"] + iv["dur_ns"]
            dbov = sum(_overlap_ns(iv0, iv1, d.start_ns, d.start_ns + d.dur_ns) for d in dbs)
            dbov = min(dbov, ov)
            db_wait_ns += dbov
            reasons[iv.get("reason", "other") if iv.get("reason") in reasons else "other"] += (ov - dbov)
        rq_ns = 0
        for iv in rq_by_tid.get(h.tid, ()):
            rq_ns += _overlap_ns(iv["start_ns"], iv["start_ns"] + iv["dur_ns"], h0, h1)
        off_ns = min(off_ns, h.dur_ns)
        rq_ns = min(rq_ns, max(0, h.dur_ns - off_ns))
        on_ns = max(0, h.dur_ns - off_ns - rq_ns)
        other_off_ns = max(0, off_ns - db_wait_ns)
        h.attrs["breakdown"] = {
            "on_cpu_ms": round(on_ns / 1e6, 3),
            "runq_ms": round(rq_ns / 1e6, 3),
            "db_wait_ms": round(db_wait_ns / 1e6, 3),
            "other_off_ms": round(other_off_ns / 1e6, 3),
            "off_reasons": {r: round(reasons[r] / 1e6, 3) for r in _OFF_REASONS if reasons[r] > 0},
        }


def _agg_breakdown(spans: list) -> dict | None:
    """Sum the per-request breakdowns across an endpoint's requests → total ms per bucket +
    the dominant non-DB off-CPU blocking reason. None when no breakdown was captured."""
    keys = ("on_cpu_ms", "runq_ms", "db_wait_ms", "other_off_ms")
    tot = {k: 0.0 for k in keys}
    reasons: dict[str, float] = defaultdict(float)
    seen = False
    for s in spans:
        bd = s.attrs.get("breakdown")
        if not bd:
            continue
        seen = True
        for k in keys:
            tot[k] += bd.get(k, 0.0)
        for r, ms in (bd.get("off_reasons") or {}).items():
            reasons[r] += ms
    if not seen:
        return None
    total = sum(tot.values()) or 1.0
    top_reason = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else None
    return {
        **{k: round(v, 3) for k, v in tot.items()},
        "on_cpu_pct": round(100 * tot["on_cpu_ms"] / total, 1),
        "runq_pct": round(100 * tot["runq_ms"] / total, 1),
        "db_wait_pct": round(100 * tot["db_wait_ms"] / total, 1),
        "other_off_pct": round(100 * tot["other_off_ms"] / total, 1),
        "top_off_reason": top_reason,
    }


def endpoint_stats(http_spans: list) -> list[dict]:
    """Per-endpoint RED rows (Rate via count, Errors, Duration percentiles) keyed by
    (method, normalized route), sorted by p95 desc. `db_ms_share` is Σ nested-db-ms /
    Σ request-ms for the endpoint (requires `correlate_spans` to have run first)."""
    groups: dict[tuple, list] = defaultdict(list)
    for h in http_spans:
        groups[(h.method or "?", _normalize_route(h.route or "/"))].append(h)
    rows: list[dict] = []
    for (method, route), spans in groups.items():
        durs = sorted(s.dur_ns / 1e6 for s in spans)
        n = len(spans)
        errs = sum(1 for s in spans if s.status and s.status >= 500)
        total_dur = sum(durs)
        total_db = sum(s.db_ms for s in spans)
        rows.append({
            "method": method, "route": route, "count": n,
            "p50_ms": _percentile(durs, 50),
            "p95_ms": _percentile(durs, 95),
            "p99_ms": _percentile(durs, 99),
            "err_pct": round(100.0 * errs / n, 1),
            "db_ms_share": round(min(1.0, total_db / total_dur), 3) if total_dur > 0 else 0.0,
            "breakdown": _agg_breakdown(spans),  # on/off/db/runq split (None if not captured)
        })
    rows.sort(key=lambda r: (r["p95_ms"] or 0.0), reverse=True)
    return rows


def request_rollup(http_spans: list, db_spans: list, *, window_s: int,
                   engine: str = "bpftrace", reason: str | None = None,
                   available: bool = True, max_sample: int = 50,
                   off_intervals: list[dict] | None = None) -> dict:
    """Assemble the `requests.json` rollup: the endpoint RED table + a sample of the
    slowest requests (with their nested db spans + on/off/db/runq breakdown, for the
    waterfall). Mirrors the `latency.json` contract (available/reason/window_s/engine).
    `off_intervals` (ebpf.parse_bpftrace_offcpu) drive the per-request decomposition."""
    nested = correlate_spans(http_spans, db_spans)
    if off_intervals:
        correlate_breakdown(http_spans, nested, off_intervals)
    endpoints = endpoint_stats(http_spans)
    slow = sorted(http_spans, key=lambda s: s.dur_ns, reverse=True)[:max_sample]
    spans_out = []
    for h in slow:
        kids = sorted(nested.get(id(h), ()), key=lambda d: d.start_ns)
        spans_out.append({
            "kind": "http", "method": h.method, "route": h.route, "name": h.name,
            "status": h.status, "dur_ms": round(h.dur_ns / 1e6, 3), "tid": h.tid,
            "start_ns": h.start_ns,  # monotonic; for the waterfall x-axis (relative to the row)
            "db_ms": h.db_ms,
            "breakdown": h.attrs.get("breakdown"),
            "db": [{"name": d.name, "dur_ms": round(d.dur_ns / 1e6, 3),
                    "start_ns": d.start_ns, "statement": d.attrs.get("statement")}
                   for d in kids],
        })
    has_breakdown = any(s.get("breakdown") for s in spans_out)
    return {
        "available": available, "reason": reason, "window_s": window_s,
        "engine": engine, "endpoints": endpoints, "spans": spans_out,
        "request_count": len(http_spans), "db_span_count": len(db_spans),
        "has_breakdown": has_breakdown,
    }


_CURATE_CAP = 200  # max curated request spans persisted to SQLite per capture (roadmap §4.4)


def curate_request_spans(http_spans: list, db_spans: list, endpoints: list[dict], *,
                         mono0: float, wall0: float, cap: int = _CURATE_CAP) -> list[dict]:
    """Select the curated slow/errored request spans to persist to SQLite (roadmap §4.4):
    keep a span if its duration ≥ its endpoint's p95 OR its status ≥ 500, capped at `cap`
    (slowest first). Converts each span's CLOCK_MONOTONIC start_ns → EPOCH ms via the
    (mono0, wall0) child-launch anchor (§2.6) so the persisted rows time-correlate with
    metrics/incidents. Returns row dicts for storage.insert_request_spans."""
    p95 = {(e["method"], e["route"]): (e.get("p95_ms") or 0.0) for e in endpoints}
    nested = correlate_spans(http_spans, db_spans)  # idempotent: refills db_ms + children
    keep = []
    for h in http_spans:
        thr = p95.get((h.method or "?", _normalize_route(h.route or "/")), 0.0)
        if (h.status and h.status >= 500) or (h.dur_ns / 1e6) >= thr:
            keep.append(h)
    keep.sort(key=lambda s: s.dur_ns, reverse=True)
    rows = []
    for h in keep[:cap]:
        epoch_ms = wall0 * 1000.0 + (h.start_ns * 1e-9 - mono0) * 1000.0
        kids = sorted(nested.get(id(h), ()), key=lambda d: d.start_ns)
        rows.append({
            "timestamp_ms": round(epoch_ms, 1), "pid": h.pid,
            "payload": {
                "method": h.method, "route": h.route, "name": h.name, "status": h.status,
                "dur_ms": round(h.dur_ns / 1e6, 3), "db_ms": h.db_ms, "tid": h.tid,
                "breakdown": h.attrs.get("breakdown"),
                "db": [{"name": d.name, "dur_ms": round(d.dur_ns / 1e6, 3),
                        "statement": d.attrs.get("statement")} for d in kids],
            },
        })
    return rows


_SLOW_ENDPOINT_MS = 500.0     # p95 over this → a slow_endpoint finding
_SLOW_ENDPOINT_HIGH_MS = 2000.0
_ERR_ENDPOINT_PCT = 5.0       # 5xx rate over this → an errored_endpoint finding


def reqtrace_anomalies(rollup: dict) -> list:
    """Slow/errored-endpoint findings from a `request_rollup`. The rule_id embeds the
    endpoint (`slow_endpoint:POST /checkout`) so a monitor run's collapse-by-rule keeps
    ONE incident PER endpoint (not one shared 'slow_endpoint' row), and
    `_incidents_to_anomalies` turns each back into its own Overview finding. Anomaly
    imported lazily to avoid an import cycle."""
    from .trace.events import Anomaly

    out: list = []
    for ep in rollup.get("endpoints", []):
        route = f"{ep.get('method', '?')} {ep.get('route', '/')}"
        count = ep.get("count", 0)
        p95 = ep.get("p95_ms") or 0.0
        err = ep.get("err_pct") or 0.0
        if p95 >= _SLOW_ENDPOINT_MS:
            share = ep.get("db_ms_share") or 0.0
            sev = "high" if p95 >= _SLOW_ENDPOINT_HIGH_MS else "medium"
            db_note = (f" — {round(share * 100)}% of that time is DB queries"
                       if share >= 0.3 else "")
            out.append(Anomaly(
                rule_id=f"slow_endpoint:{route}", severity=sev,
                severity_score=0.55 if sev == "high" else 0.4,
                title=f"Slow endpoint {route} — p95 {round(p95)}ms{db_note}",
                description=(
                    f"{route} took p95 {round(p95)}ms across {count} request(s) in the "
                    f"window (p50 {round(ep.get('p50_ms') or 0)}ms, p99 "
                    f"{round(ep.get('p99_ms') or 0)}ms). "
                    + (f"Most of that is database time (db share "
                       f"{round(share * 100)}%) — look at the query, not the CPU."
                       if share >= 0.3 else
                       "Little of it is DB time — check the off-CPU flamegraph / CPU.")),
            ))
        if err >= _ERR_ENDPOINT_PCT:
            out.append(Anomaly(
                rule_id=f"errored_endpoint:{route}", severity="high",
                severity_score=0.5,
                title=f"Endpoint errors on {route} — {err}% 5xx",
                description=(f"{route} returned a 5xx status on {err}% of {count} "
                             "request(s) in the window."),
            ))
    return out
