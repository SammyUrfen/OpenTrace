"""Unit tests for the allocation profiler + library-call hotspots (app.profile).
Events are synthetic LIBCALL dicts shaped like the ltrace parser's output."""
from __future__ import annotations

from app import profile
from app.trace.events import LIBCALL, SYSCALL


def lib(name: str, args: str, retval: str | None = None, latency_ms: float | None = None,
        error: str | None = None) -> dict:
    return {"event_type": LIBCALL, "syscall": name, "args": args,
            "retval": retval, "latency_ms": latency_ms, "error": error}


def test_malloc_free_ledger_and_leak():
    events = [
        lib("malloc", "1024", "0x1000"),
        lib("malloc", "2048", "0x2000"),
        lib("free", "0x1000", "<void>"),     # frees the 1 KB block
        lib("malloc", "4096", "0x3000"),     # never freed
    ]
    p = profile.malloc_profile(events)
    assert p["n_alloc"] == 3
    assert p["n_free"] == 1
    assert p["bytes_allocated"] == 1024 + 2048 + 4096
    assert p["bytes_freed"] == 1024
    assert p["outstanding_bytes"] == 2048 + 4096
    assert p["outstanding_blocks"] == 2
    # peak live = all three before the (single, small) free happened late? No:
    # order is malloc,malloc,free,malloc -> peak after 2nd malloc = 3072,
    # then -1024 -> 2048, then +4096 -> 6144 (the true peak).
    assert p["peak_live_bytes"] == 6144
    assert p["largest_live"][0]["size"] == 4096


def test_calloc_and_realloc_accounting():
    events = [
        lib("calloc", "10, 128", "0xa000"),       # 1280 bytes
        lib("realloc", "0xa000, 4096", "0xb000"),  # frees 0xa000, allocs 4096
        lib("free", "0xb000", "<void>"),
    ]
    p = profile.malloc_profile(events)
    assert p["outstanding_bytes"] == 0
    assert p["outstanding_blocks"] == 0
    assert p["bytes_allocated"] == 1280 + 4096
    assert p["bytes_freed"] == 1280 + 4096


def test_free_unmatched_counted():
    events = [lib("free", "0xdead", "<void>"), lib("free", "0xbeef", "<void>")]
    p = profile.malloc_profile(events)
    assert p["free_unmatched"] == 2
    assert p["n_free"] == 2


def test_free_null_is_a_noop_not_counted():
    events = [
        lib("malloc", "1024", "0x1"),
        lib("free", "(nil)", "<void>"),  # free(NULL) — no-op
        lib("free", "0", "<void>"),      # free(0)   — no-op
        lib("free", "0x1", "<void>"),    # real free
    ]
    p = profile.malloc_profile(events)
    assert p["n_free"] == 1           # only the real free is counted
    assert p["free_unmatched"] == 0
    assert p["outstanding_bytes"] == 0


def test_libcall_hotspots_sorted_by_time():
    events = [
        lib("malloc", "8", "0x1", latency_ms=0.5),
        lib("malloc", "8", "0x2", latency_ms=0.5),
        lib("memcpy", "0x1, 0x2, 8", "0x1", latency_ms=2.0),
        {"event_type": SYSCALL, "syscall": "brk", "args": "", "latency_ms": 9.0},  # ignored
    ]
    rows = profile.libcall_stats(events)
    assert rows[0]["function"] == "memcpy"   # highest total_ms first
    assert rows[0]["total_ms"] == 2.0
    malloc_row = next(r for r in rows if r["function"] == "malloc")
    assert malloc_row["calls"] == 2 and malloc_row["total_ms"] == 1.0
    assert all(r["function"] != "brk" for r in rows)  # syscalls excluded


def test_leak_anomaly_fires_above_threshold():
    # 80 unfreed 8 KB blocks = 640 KB -> heap_leak (high)
    events = [lib("malloc", "8192", hex(0x10000 + i)) for i in range(80)]
    p = profile.malloc_profile(events)
    anoms = profile.profile_anomalies(p)
    ids = {a.rule_id for a in anoms}
    assert "heap_leak" in ids
    assert next(a for a in anoms if a.rule_id == "heap_leak").severity == "high"


def test_no_leak_anomaly_when_clean():
    events = [lib("malloc", "1024", "0x1"), lib("free", "0x1", "<void>")]
    p = profile.malloc_profile(events)
    assert profile.profile_anomalies(p) == []


def test_allocation_storm_on_high_churn():
    events = [lib("malloc", "8", hex(i)) for i in range(11000)]
    events += [lib("free", hex(i), "<void>") for i in range(11000)]
    p = profile.malloc_profile(events)  # churn = 22000 >= _STORM_CHURN
    assert "allocation_storm" in {a.rule_id for a in profile.profile_anomalies(p)}


def test_allocation_storm_by_rate_in_short_run():
    # 3000 churn over 100ms -> 30k/s, above the §5 10k/s rate threshold
    events = [lib("malloc", "8", hex(i)) for i in range(1500)]
    events += [lib("free", hex(i), "<void>") for i in range(1500)]
    p = profile.malloc_profile(events)
    ids = {a.rule_id for a in profile.profile_anomalies(p, duration_ms=100)}
    assert "allocation_storm" in ids
    # the same churn over a long run is not a storm
    assert "allocation_storm" not in {a.rule_id for a in profile.profile_anomalies(p, duration_ms=60_000)}
