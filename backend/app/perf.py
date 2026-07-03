"""Turn a `perf record -g` capture into a flame tree + symbol hotspots.

`perf script` prints one sample as a header line followed by tab-indented stack
frames (leaf first), blank-line separated:

    cpu  712799 22377.456075:  124999 cpu_atom/cycles/Pu:
            7ff31dab1d91 search_cache+0x71 (/usr/lib64/ld-linux-x86-64.so.2)
            7ff31dab2441 _dl_load_cache_lookup+0x61 (/usr/lib64/ld-linux...)
            ...
            7ff31dabb5c8 _dl_start_user+0x0 (/usr/lib64/ld-linux-x86-64.so.2)

We fold those stacks into:
- a nested {name, value, children} tree for an inline flame chart, pruned so the
  JSON stays small;
- a `hotspots` table with self (leaf) and total (anywhere-in-stack) sample
  counts per symbol — the CPU "function hotspot" view.

`fold_perf_script(text)` is pure (unit-tested on captured text); `build_flamegraph`
shells out to `perf script` and folds the result.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .trace.events import Anomaly

log = logging.getLogger(__name__)

# A single resolved function consuming this share of CPU samples is a hotspot
# worth surfacing (§5 "Hot function").
_HOT_MEDIUM = 30.0
_HOT_HIGH = 50.0

# "  <hexaddr> <symbol>[+0xoffset] (<dso>)"  — offset is stripped.
_FRAME_RE = re.compile(
    r"^\s+(?P<addr>[0-9a-fA-F]+)\s+(?P<sym>.+?)(?:\+0x[0-9a-fA-F]+)?\s+\((?P<dso>[^)]*)\)\s*$"
)

# Fraction of total samples below which a flame node is pruned, and a hard depth
# cap — together they keep the tree a few hundred nodes even for big captures.
_PRUNE_FRACTION = 0.004
_MAX_DEPTH = 64
_MAX_HOTSPOTS = 60


def _clean_sym(sym: str) -> str:
    sym = sym.strip()
    # perf sometimes prints a bare address for unresolved frames.
    if not sym or sym == "[unknown]":
        return "[unknown]"
    return sym


def _iter_stacks(text: str):
    """Yield each sample's stack as a root->leaf list of symbol names."""
    cur: list[str] = []

    def finish():
        nonlocal cur
        if cur:
            stack = list(reversed(cur))
            cur = []
            return stack
        cur = []
        return None

    for line in text.splitlines():
        if not line.strip():
            s = finish()
            if s:
                yield s
            continue
        if line[0].isspace():
            m = _FRAME_RE.match(line)
            if m:
                cur.append(_clean_sym(m.group("sym")))
        else:
            # a new sample header: flush the previous stack
            s = finish()
            if s:
                yield s
    s = finish()
    if s:
        yield s


def fold_perf_script(text: str) -> dict:
    root: dict = {"name": "all", "value": 0, "children": {}}
    self_ct: dict[str, int] = {}
    total_ct: dict[str, int] = {}
    n_samples = 0

    for stack in _iter_stacks(text):
        n_samples += 1
        root["value"] += 1
        node = root
        for depth, sym in enumerate(stack):
            if depth >= _MAX_DEPTH:
                break
            child = node["children"].get(sym)
            if child is None:
                child = {"name": sym, "value": 0, "children": {}}
                node["children"][sym] = child
            child["value"] += 1
            node = child
        # hotspots
        for sym in set(stack):
            total_ct[sym] = total_ct.get(sym, 0) + 1
        if stack:
            leaf = stack[-1]
            self_ct[leaf] = self_ct.get(leaf, 0) + 1

    min_value = max(1, round(n_samples * _PRUNE_FRACTION))
    tree = _to_list(root, min_value)

    hotspots = [
        {
            "function": sym,
            "self": self_ct.get(sym, 0),
            "total": total,
            "self_pct": round(100 * self_ct.get(sym, 0) / n_samples, 2) if n_samples else 0.0,
            "total_pct": round(100 * total / n_samples, 2) if n_samples else 0.0,
        }
        for sym, total in total_ct.items()
    ]
    hotspots.sort(key=lambda r: (r["self"], r["total"]), reverse=True)

    return {
        "supported": True,
        "samples": n_samples,
        "tree": tree,
        "hotspots": hotspots[:_MAX_HOTSPOTS],
    }


def _to_list(node: dict, min_value: int) -> dict:
    """Convert the dict-keyed children to a pruned, value-sorted list."""
    kids = [
        _to_list(c, min_value)
        for c in node["children"].values()
        if c["value"] >= min_value
    ]
    kids.sort(key=lambda c: c["value"], reverse=True)
    return {"name": node["name"], "value": node["value"], "children": kids}


def perf_anomalies(flamegraph: dict) -> list[Anomaly]:
    """Surface a dominant CPU function from the folded profile (§5 hot function).
    Skips unresolved frames so we don't flag `[unknown]`."""
    if not flamegraph.get("supported"):
        return []
    for h in flamegraph.get("hotspots") or []:
        fn = h.get("function")
        pct = h.get("self_pct", 0)
        if fn in (None, "[unknown]"):
            continue
        if pct >= _HOT_MEDIUM:
            sev = "high" if pct >= _HOT_HIGH else "medium"
            return [Anomaly(
                rule_id="hot_function",
                severity=sev,
                severity_score=0.6 if sev == "high" else 0.45,
                title=f"Hot function: {fn} is {pct:.0f}% of CPU samples",
                description=(
                    f"`{fn}` accounts for {pct:.0f}% of sampled CPU time "
                    f"(self) — the dominant compute cost. Optimizing or calling "
                    f"it less is the highest-leverage CPU win for this run."
                ),
            )]
        break  # hotspots are sorted by self desc; the top is below threshold
    return []


def build_flamegraph(perf_data: str | Path) -> dict:
    """Run `perf script` over a capture and fold it. Fail-soft: returns an
    `unsupported` stub if perf is missing or the capture is unreadable."""
    p = Path(perf_data)
    if not p.exists():
        return {"supported": False, "reason": "no perf capture", "samples": 0}
    try:
        proc = subprocess.run(
            ["perf", "script", "-i", str(p)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.warning("perf script failed for %s: %s", p, e)
        return {"supported": False, "reason": "perf script unavailable", "samples": 0}
    if proc.returncode != 0 and not proc.stdout:
        return {"supported": False, "reason": "perf script error", "samples": 0}
    folded = fold_perf_script(proc.stdout)
    if folded["samples"] == 0:
        folded["supported"] = False
        folded["reason"] = "no samples (workload too short?)"
    return folded
