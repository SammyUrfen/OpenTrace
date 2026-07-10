"""Unit tests for per-syscall aggregation (pure, no I/O)."""
from __future__ import annotations

from app import aggregate
from app.aggregate import io_stats, network_stats, process_stats, syscall_stats
from app.trace.events import Span


def ev(syscall, latency_ms=None, error=None, event_type="syscall"):
    return {
        "syscall": syscall, "latency_ms": latency_ms, "error": error,
        "event_type": event_type,
    }


def test_counts_latency_and_errors():
    events = [
        ev("openat", 1.0), ev("openat", 3.0, error="ENOENT"), ev("openat", 2.0),
        ev("read", 10.0), ev("read", 30.0),
        ev("write"),  # no latency recorded
        ev("SIGCHLD", event_type="signal"),  # ignored (not a syscall)
        ev("exit", event_type="exit"),        # ignored
    ]
    rows = syscall_stats(events)
    by = {r["syscall"]: r for r in rows}

    assert set(by) == {"openat", "read", "write"}
    assert by["openat"]["count"] == 3
    assert by["openat"]["errors"] == 1
    assert by["openat"]["total_ms"] == 6.0
    assert by["openat"]["avg_ms"] == 2.0
    assert by["read"]["total_ms"] == 40.0
    assert by["write"]["count"] == 1 and by["write"]["avg_ms"] is None


def test_sorted_by_total_latency_desc():
    rows = syscall_stats([
        ev("a", 1.0), ev("b", 100.0), ev("c", 50.0),
    ])
    assert [r["syscall"] for r in rows] == ["b", "c", "a"]


def test_percentiles_and_pct_runtime():
    # 100 read calls of latency 1..100ms
    events = [ev("read", float(i)) for i in range(1, 101)]
    events += [ev("write", 100.0)]  # total runtime denominator includes this
    rows = syscall_stats(events)
    read = next(r for r in rows if r["syscall"] == "read")
    assert read["p50_ms"] == 50.5
    assert read["p95_ms"] == 95.05
    assert read["p99_ms"] == 99.01
    # read total = 5050ms, write = 100ms -> read ~98.06% of in-syscall time
    assert 97.0 < read["pct_runtime"] < 99.0


def test_empty_stream():
    assert syscall_stats([]) == []
    assert io_stats([]) == []


def ioev(syscall, retval=None, error=None, path=None, fd=None):
    return {
        "event_type": "syscall", "pid": 1, "syscall": syscall,
        "retval": retval, "error": error, "path": path, "fd": fd,
    }


def test_io_stats_resolves_bytes_to_files_and_detects_leak():
    events = [
        ioev("openat", retval="3", path="/data/a.txt"),
        ioev("read", retval="100", fd=3),
        ioev("read", retval="50", fd=3),
        ioev("write", retval="10", fd=3),
        ioev("close", retval="0", fd=3),
        # b.txt opened and never closed -> leaked
        ioev("openat", retval="4", path="/data/b.txt"),
        ioev("write", retval="200", fd=4),
        # an error open is ignored
        ioev("openat", retval="-1", error="ENOENT", path="/data/missing"),
    ]
    rows = io_stats(events)
    by = {r["path"]: r for r in rows}
    assert "/data/missing" not in by
    a = by["/data/a.txt"]
    assert a["opens"] == 1 and a["closes"] == 1
    assert a["reads"] == 2 and a["read_bytes"] == 150
    assert a["writes"] == 1 and a["write_bytes"] == 10
    assert a["leaked"] == 0
    b = by["/data/b.txt"]
    assert b["leaked"] == 1 and b["write_bytes"] == 200
    # sorted by total accesses desc -> a.txt (5) before b.txt (2)
    assert rows[0]["path"] == "/data/a.txt"


def test_network_parses_address_and_timeout():
    args = '3, {sa_family=AF_INET, sin_port=htons(12345), sin_addr=inet_addr("192.0.2.1")}, 16'
    events = [
        {"event_type": "syscall", "pid": 1, "syscall": "connect", "args": args,
         "retval": "-1", "error": "EINPROGRESS", "latency_ms": 0.18},
        {"event_type": "syscall", "pid": 1, "syscall": "poll", "args": "",
         "retval": "0", "error": None, "latency_ms": 2503.0},  # retval 0 = timeout
    ]
    conns = network_stats(events)
    assert len(conns) == 1
    c = conns[0]
    assert c["family"] == "AF_INET"
    assert c["address"] == "192.0.2.1" and c["port"] == 12345
    assert c["result"] == "timed out"
    assert c["latency_ms"] > 2500  # poll wait folded onto the connect


def test_network_direct_error():
    args = '3, {sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("127.0.0.1")}, 16'
    events = [{"event_type": "syscall", "pid": 1, "syscall": "connect", "args": args,
               "retval": "-1", "error": "ECONNREFUSED", "latency_ms": 0.2}]
    c = network_stats(events)[0]
    assert c["result"] == "ECONNREFUSED" and c["address"] == "127.0.0.1" and c["port"] == 80


def test_network_empty():
    assert network_stats([]) == []


def pev(pid, syscall, *, event_type="syscall", retval=None, path=None, ts=0.0):
    return {"event_type": event_type, "pid": pid, "syscall": syscall,
            "retval": retval, "path": path, "timestamp_ms": ts}


def test_process_stats_tree_command_and_lifespan():
    events = [
        pev(100, "execve", retval="0", path="/usr/bin/python3", ts=0),
        pev(100, "clone", retval="200", ts=10),     # 100 forks 200
        pev(100, "read", retval="5", ts=20),
        pev(100, "wait4", retval="200", ts=500),    # 100 lives 0..500ms
        pev(200, "execve", retval="0", path="/bin/sh", ts=12),
        pev(200, "write", retval="3", ts=15),
        pev(200, "exit", event_type="exit", ts=80),  # 200 lived 12..80 = 68ms -> ephemeral
    ]
    rows = process_stats(events)
    by = {r["pid"]: r for r in rows}
    assert by[100]["command"] == "/usr/bin/python3"
    assert by[100]["parent_pid"] is None
    assert by[100]["syscalls"] == 4  # execve, clone, read, wait4
    assert by[200]["parent_pid"] == 100
    assert by[200]["command"] == "/bin/sh"
    assert by[200]["exited"] is True
    assert by[200]["ephemeral"] is True  # 68ms <= 250
    assert by[100]["ephemeral"] is False  # 500ms > 250


def test_process_stats_empty():
    assert process_stats([]) == []


def test_io_stats_fd_reuse():
    events = [
        ioev("openat", retval="3", path="/x"),
        ioev("close", retval="0", fd=3),
        ioev("openat", retval="3", path="/y"),  # fd 3 reused for /y
        ioev("read", retval="5", fd=3),
    ]
    by = {r["path"]: r for r in io_stats(events)}
    assert by["/y"]["reads"] == 1 and by["/y"]["read_bytes"] == 5
    assert by["/x"]["reads"] == 0  # the read belongs to /y, not /x


# --- request tracing: correlation + endpoint RED ----------------------------

MS = 1_000_000  # ns per ms


def hspan(tid, start_ms, dur_ms, route, method="GET", status=200):
    return Span(kind="http", tid=tid, pid=9, start_ns=start_ms * MS,
                dur_ns=dur_ms * MS, name=f"{method} {route}", method=method,
                route=route, status=status)


def dspan(tid, start_ms, dur_ms, stmt="SELECT ?"):
    return Span(kind="db", tid=tid, pid=9, start_ns=start_ms * MS,
                dur_ns=dur_ms * MS, name=stmt, attrs={"statement": stmt})


def test_normalize_route_templatizes_dynamic_segments():
    assert aggregate._normalize_route("/users/123/orders/9") == "/users/{id}/orders/{id}"
    assert aggregate._normalize_route("/i/550e8400-e29b-41d4-a716-446655440000") == "/i/{uuid}"
    assert aggregate._normalize_route("/blob/deadbeefcafef00d1234") == "/blob/{hex}"
    assert aggregate._normalize_route("/api/health") == "/api/health"  # static kept


def test_normalize_route_canonicalizes_slashes():
    # trailing slash, double slash, and leading double slash all fold to one key so an
    # endpoint isn't split into distinct RED rows / duplicate incidents (review finding).
    base = "/users/{id}"
    assert aggregate._normalize_route("/users/123") == base
    assert aggregate._normalize_route("/users/123/") == base
    assert aggregate._normalize_route("//users/123") == base
    assert aggregate._normalize_route("/users//123/") == base
    assert aggregate._normalize_route("/") == "/"


def test_correlate_nests_db_by_tid_and_window():
    http = [hspan(1, 100, 300, "/slow")]
    db = [
        dspan(1, 150, 290),          # same tid, inside window -> nests
        dspan(2, 160, 5),            # different tid -> dropped
        dspan(1, 900, 5),            # same tid but AFTER the window -> dropped
    ]
    nested = aggregate.correlate_spans(http, db)
    assert http[0].db_ms == 290.0
    assert len(nested[id(http[0])]) == 1


def test_correlate_single_owner_no_double_count_on_overlapping_tid():
    # coroutine/greenlet server: two concurrent requests share one tid with overlapping
    # windows. A db span inside both must attach to exactly ONE (the innermost) — never
    # both — so db time isn't multiply-counted (review finding, db_ms_share > 100%).
    a = hspan(7, 0, 1000, "/report")     # outer window
    b = hspan(7, 100, 800, "/report")    # inner, starts later, same tid
    d = dspan(7, 200, 700)               # starts inside BOTH windows
    nested = aggregate.correlate_spans([a, b], [d])
    owners = [h for h in (a, b) if nested.get(id(h))]
    assert owners == [b]                 # innermost (latest-started) owner only
    assert a.db_ms == 0.0 and b.db_ms == 700.0


def test_correlate_clamps_db_ms_to_request_duration():
    h = hspan(1, 0, 100, "/x")           # 100ms request
    db = [dspan(1, 10, 90), dspan(1, 10, 90)]  # two 90ms queries "inside" (pathological)
    aggregate.correlate_spans([h], db)
    assert h.db_ms == 100.0              # clamped to wall time, never 180
    rows = aggregate.endpoint_stats([h])
    assert rows[0]["db_ms_share"] <= 1.0


def test_endpoint_stats_red_table_and_db_share():
    http = [hspan(1, 0, 300, "/slow"), hspan(2, 0, 302, "/slow"),
            hspan(3, 0, 5, "/users/123"), hspan(4, 0, 8, "/checkout", "POST", 500)]
    db = [dspan(1, 10, 297), dspan(2, 10, 299)]
    aggregate.correlate_spans(http, db)
    rows = {(r["method"], r["route"]): r for r in aggregate.endpoint_stats(http)}
    slow = rows[("GET", "/slow")]
    assert slow["count"] == 2 and slow["db_ms_share"] > 0.95
    assert rows[("POST", "/checkout")]["err_pct"] == 100.0
    assert ("GET", "/users/{id}") in rows           # normalized
    # sorted by p95 desc: /slow first
    assert aggregate.endpoint_stats(http)[0]["route"] == "/slow"


def test_request_rollup_and_reqtrace_anomalies():
    http = [hspan(1, 0, 700, "/report"), hspan(2, 0, 5, "/checkout", "POST", 500)]
    db = [dspan(1, 10, 690)]
    roll = aggregate.request_rollup(http, db, window_s=20)
    assert roll["available"] and roll["request_count"] == 2 and roll["db_span_count"] == 1
    assert roll["spans"][0]["route"] == "/report"   # slowest sampled first
    anoms = {a.rule_id: a for a in aggregate.reqtrace_anomalies(roll)}
    # /report p95 700ms >= 500 -> slow_endpoint (per-endpoint rule id); DB-dominated note
    assert "slow_endpoint:GET /report" in anoms
    assert "DB" in anoms["slow_endpoint:GET /report"].title
    # /checkout 100% 5xx -> errored_endpoint
    assert "errored_endpoint:POST /checkout" in anoms


# --- Phase 2: off-CPU decomposition + curation -------------------------------

def off_iv(tid, start_ms, dur_ms, reason="net"):
    return {"kind": "off", "tid": tid, "start_ns": start_ms * MS,
            "dur_ns": dur_ms * MS, "reason": reason}


def rq_iv(tid, start_ms, dur_ms):
    return {"kind": "rq", "tid": tid, "start_ns": start_ms * MS, "dur_ns": dur_ms * MS}


def test_correlate_breakdown_splits_db_wait_runq_on_cpu():
    # /db: 300ms request, 200ms off-CPU that fully overlaps a 200ms db span (→ db-wait),
    # 10ms run-queue, rest on-CPU. Buckets sum to the wall time.
    h = hspan(1, 0, 300, "/db")
    nested = aggregate.correlate_spans([h], [dspan(1, 50, 200)])
    aggregate.correlate_breakdown([h], nested, [off_iv(1, 50, 200, "net"), rq_iv(1, 260, 10)])
    bd = h.attrs["breakdown"]
    assert bd["db_wait_ms"] == 200.0
    assert bd["other_off_ms"] == 0.0
    assert bd["runq_ms"] == 10.0
    assert bd["on_cpu_ms"] == 90.0  # 300 - 200 off - 10 runq


def test_correlate_breakdown_labels_non_db_offcpu_by_reason():
    h = hspan(2, 0, 250, "/sleep")
    nested = aggregate.correlate_spans([h], [])          # no db spans
    aggregate.correlate_breakdown([h], nested, [off_iv(2, 20, 200, "sleep")])
    bd = h.attrs["breakdown"]
    assert bd["db_wait_ms"] == 0.0
    assert bd["other_off_ms"] == 200.0
    assert bd["off_reasons"].get("sleep") == 200.0
    assert bd["on_cpu_ms"] == 50.0


def test_correlate_breakdown_ignores_other_tid_intervals():
    h = hspan(5, 0, 100, "/x")
    nested = aggregate.correlate_spans([h], [])
    # an off interval on a DIFFERENT tid must not bleed into this request
    aggregate.correlate_breakdown([h], nested, [off_iv(9, 10, 80, "sleep")])
    assert h.attrs["breakdown"]["off_reasons"] == {}
    assert h.attrs["breakdown"]["on_cpu_ms"] == 100.0


def test_endpoint_stats_aggregates_breakdown():
    http = [hspan(1, 0, 300, "/db"), hspan(2, 0, 300, "/db")]
    nested = aggregate.correlate_spans(http, [dspan(1, 50, 200), dspan(2, 50, 200)])
    aggregate.correlate_breakdown(http, nested, [off_iv(1, 50, 200), off_iv(2, 50, 200)])
    row = aggregate.endpoint_stats(http)[0]
    assert row["breakdown"] is not None
    assert row["breakdown"]["db_wait_pct"] > 60         # DB-dominated endpoint
    # endpoint with NO breakdown captured → None
    plain = aggregate.endpoint_stats([hspan(3, 0, 10, "/y")])[0]
    assert plain["breakdown"] is None


def test_request_rollup_threads_breakdown_and_start_ns():
    http = [hspan(1, 0, 300, "/db")]
    roll = aggregate.request_rollup(http, [dspan(1, 50, 200)], window_s=8,
                                    off_intervals=[off_iv(1, 50, 200)])
    assert roll["has_breakdown"] is True
    span = roll["spans"][0]
    assert span["breakdown"]["db_wait_ms"] == 200.0
    assert span["start_ns"] == 0 and span["db"][0]["start_ns"] == 50 * MS  # waterfall x-axis
    # a run with no off-CPU intervals → no breakdown (fresh span, not the mutated one above)
    assert aggregate.request_rollup([hspan(2, 0, 300, "/db")], [], window_s=8)["has_breakdown"] is False


def test_curate_request_spans_epoch_threshold_and_cap():
    # /x: two slow (300ms, >= p95 kept) + one fast (5ms, below p95 dropped); /y errored kept.
    http = [hspan(1, 100, 300, "/x"), hspan(2, 100, 300, "/x"),
            hspan(3, 100, 5, "/x"), hspan(4, 100, 8, "/y", "POST", 500)]
    aggregate.correlate_spans(http, [])
    eps = aggregate.endpoint_stats(http)
    rows = aggregate.curate_request_spans(http, [], eps, mono0=0.0, wall0=1_700_000.0)
    routes = sorted(r["payload"]["route"] for r in rows)
    assert routes == ["/x", "/x", "/y"]                 # fast /x dropped, errored /y kept
    # epoch: start_ns = 100ms = 0.1s, mono0=0 → epoch_ms = wall0*1000 + 0.1*1000
    x = next(r for r in rows if r["payload"]["route"] == "/x")
    assert abs(x["timestamp_ms"] - (1_700_000.0 * 1000 + 100)) < 1.0
    # cap honoured
    assert len(aggregate.curate_request_spans(http, [], eps, mono0=0.0, wall0=1.0, cap=1)) == 1
