"""Unit tests for per-syscall aggregation (pure, no I/O)."""
from __future__ import annotations

from app.aggregate import io_stats, network_stats, process_stats, syscall_stats


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
