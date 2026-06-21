"""Pure aggregations over a run's event stream (no I/O, easily unit-tested).

Currently: per-syscall statistics for the Syscall Explorer tab, computed from
the full `events.ndjson.zst` stream (decoded to dicts by the caller).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable


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
