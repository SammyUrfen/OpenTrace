"""Allocation profiling + library-call hotspots from an ltrace event stream.

Consumes the `LIBCALL` events the ltrace parser produces (plain dicts, as read
back from `events.ndjson.zst`) and derives:

- `malloc_profile`  — a malloc/free ledger: bytes allocated/freed, peak live
  bytes, outstanding (leaked) allocations, alloc/free imbalance, the top
  allocation sizes, and the largest still-live blocks.
- `libcall_stats`   — a per-function hotspot table (calls + total/avg time),
  the "function-hotspot" view for ltrace runs.
- `profile_anomalies` — leak / imbalance anomalies derived from the ledger,
  surfaced alongside the rule-engine anomalies.

All functions are pure over an iterable of event dicts so they unit-test on
synthetic input and stream over a real log without loading it whole.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from .trace.events import LIBCALL, Anomaly

# Functions that return a freshly allocated pointer (and how to size them).
_ALLOC_FUNCS = {"malloc", "calloc", "realloc", "reallocarray", "valloc",
                "aligned_alloc", "memalign", "pvalloc"}
_FREE_FUNCS = {"free", "cfree"}

# Thresholds for the derived anomalies (conservative, to avoid false positives).
_LEAK_BYTES = 256 * 1024          # >256 KB still live at exit
_LEAK_COUNT = 64                  # or >64 un-freed blocks
_IMBALANCE_RATIO = 4.0            # allocs >= 4x frees (and enough volume)
_IMBALANCE_MIN_ALLOCS = 200
_STORM_CHURN = 20000              # malloc+free calls — high churn regardless of rate
_STORM_RATE = 10000.0            # malloc+free calls/sec sustained (§5 spec)


def _hexptr(tok: str | None) -> int | None:
    """Parse a pointer token (0x.., (nil), nil) to an int address, or None."""
    if not tok:
        return None
    tok = tok.strip().rstrip(",")
    if tok in ("(nil)", "nil", "NULL", "0"):
        return 0
    try:
        return int(tok, 16) if tok.startswith("0x") else int(tok)
    except ValueError:
        return None


def _arg_list(args: str) -> list[str]:
    """Split a flat ltrace arg string on top-level commas (good enough for the
    numeric/pointer args of the allocator functions we care about)."""
    return [a.strip() for a in args.split(",")] if args else []


def _as_int(tok: str) -> int | None:
    tok = tok.strip()
    try:
        return int(tok, 16) if tok.startswith("0x") else int(tok)
    except (ValueError, AttributeError):
        return None


def _alloc_size(name: str, argv: list[str]) -> int | None:
    if name in ("malloc", "valloc", "pvalloc") and argv:
        return _as_int(argv[0])
    if name in ("calloc", "reallocarray") and len(argv) >= 2:
        n, sz = _as_int(argv[0]), _as_int(argv[1])
        return n * sz if n is not None and sz is not None else None
    if name == "realloc" and len(argv) >= 2:
        return _as_int(argv[1])
    if name in ("aligned_alloc", "memalign") and len(argv) >= 2:
        return _as_int(argv[1])  # (alignment, size)
    return None


def malloc_profile(events: Iterable[dict]) -> dict:
    live: dict[int, int] = {}          # ptr -> size
    live_bytes = 0
    peak_bytes = 0
    bytes_alloc = bytes_freed = 0
    counts: Counter[str] = Counter()
    sizes: Counter[int] = Counter()
    free_unmatched = 0

    for e in events:
        if e.get("event_type") != LIBCALL:
            continue
        name = e.get("syscall")
        if name not in _ALLOC_FUNCS and name not in _FREE_FUNCS:
            continue
        argv = _arg_list(e.get("args", ""))

        if name in _FREE_FUNCS:
            ptr = _hexptr(argv[0]) if argv else None
            if not ptr:
                continue  # free(NULL)/free((nil)) is a no-op — don't count it
            counts[name] += 1
            sz = live.pop(ptr, None)
            if sz is None:
                free_unmatched += 1
            else:
                bytes_freed += sz
                live_bytes -= sz
            continue

        # allocation (malloc/calloc/realloc/...)
        counts[name] += 1
        if name == "realloc":
            old = _hexptr(argv[0]) if argv else None
            if old:
                sz = live.pop(old, None)
                if sz is not None:
                    bytes_freed += sz
                    live_bytes -= sz
        ptr = _hexptr(e.get("retval"))
        size = _alloc_size(name, argv)
        if ptr and size is not None:
            live[ptr] = size
            live_bytes += size
            bytes_alloc += size
            sizes[size] += 1
            peak_bytes = max(peak_bytes, live_bytes)

    n_alloc = sum(counts[f] for f in _ALLOC_FUNCS)
    n_free = sum(counts[f] for f in _FREE_FUNCS)
    largest = sorted(live.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "supported": True,
        "calls": dict(counts),
        "n_alloc": n_alloc,
        "n_free": n_free,
        "bytes_allocated": bytes_alloc,
        "bytes_freed": bytes_freed,
        "peak_live_bytes": peak_bytes,
        "outstanding_bytes": sum(live.values()),
        "outstanding_blocks": len(live),
        "free_unmatched": free_unmatched,
        "top_sizes": [{"size": s, "count": c} for s, c in sizes.most_common(8)],
        "largest_live": [{"addr": hex(a), "size": s} for a, s in largest],
    }


def libcall_stats(events: Iterable[dict], limit: int = 50) -> list[dict]:
    count: Counter[str] = Counter()
    total_ms: defaultdict[str, float] = defaultdict(float)
    errors: Counter[str] = Counter()

    for e in events:
        if e.get("event_type") != LIBCALL:
            continue
        name = e.get("syscall")
        if not name:
            continue
        count[name] += 1
        lat = e.get("latency_ms")
        if lat is not None:
            total_ms[name] += lat
        if e.get("error"):
            errors[name] += 1

    rows = [
        {
            "function": name,
            "calls": count[name],
            "total_ms": round(total_ms[name], 3),
            "avg_ms": round(total_ms[name] / count[name], 4) if count[name] else 0.0,
            "errors": errors[name],
        }
        for name in count
    ]
    rows.sort(key=lambda r: (r["total_ms"], r["calls"]), reverse=True)
    return rows[:limit]


def profile_anomalies(profile: dict, duration_ms: int | None = None) -> list[Anomaly]:
    """Leak / imbalance / storm anomalies derived from the malloc ledger."""
    out: list[Anomaly] = []
    if not profile.get("supported"):
        return out

    n_alloc = profile.get("n_alloc", 0)
    n_free = profile.get("n_free", 0)
    churn = n_alloc + n_free
    rate = churn / (duration_ms / 1000.0) if duration_ms else None
    if churn >= _STORM_CHURN or (rate is not None and rate >= _STORM_RATE and churn > 2000):
        rate_txt = f" (~{rate:.0f}/s)" if rate else ""
        out.append(Anomaly(
            rule_id="allocation_storm",
            severity="medium",
            severity_score=0.5,
            title=f"Allocation storm: {churn:,} malloc/free calls{rate_txt}",
            description=(
                f"{n_alloc:,} allocations and {n_free:,} frees{rate_txt} — heavy "
                f"allocator churn that pressures the heap and adds CPU overhead. "
                f"Reuse buffers / pool objects to cut it down."
            ),
        ))

    live_bytes = profile.get("outstanding_bytes", 0)
    live_blocks = profile.get("outstanding_blocks", 0)
    if live_bytes >= _LEAK_BYTES or live_blocks >= _LEAK_COUNT:
        kb = live_bytes / 1024
        out.append(Anomaly(
            rule_id="heap_leak",
            severity="high",
            severity_score=0.7,
            title=f"Heap memory not freed: {kb:.0f} KB in "
                  f"{live_blocks} block(s) still live at exit",
            description=(
                f"{live_blocks} allocation(s) totalling {kb:.1f} KB were never "
                f"free()d before the program exited (malloc/calloc/realloc vs "
                f"free). This is a likely heap leak."
            ),
        ))

    if (n_alloc >= _IMBALANCE_MIN_ALLOCS
            and n_free > 0 and n_alloc / n_free >= _IMBALANCE_RATIO):
        out.append(Anomaly(
            rule_id="alloc_free_imbalance",
            severity="medium",
            severity_score=0.5,
            title=f"Allocation/free imbalance: {n_alloc} allocs vs {n_free} frees",
            description=(
                f"The program made {n_alloc} allocations but only {n_free} "
                f"free()s — a {n_alloc / n_free:.1f}× imbalance that grows the "
                f"heap over time."
            ),
        ))

    if profile.get("free_unmatched", 0) >= 16:
        n = profile["free_unmatched"]
        out.append(Anomaly(
            rule_id="free_unmatched",
            severity="medium",
            severity_score=0.45,
            title=f"{n} free() calls with no matching allocation",
            description=(
                f"{n} free() calls targeted pointers not seen from a tracked "
                f"allocation — possible double-free, or frees of memory "
                f"allocated before tracing began."
            ),
        ))
    return out
