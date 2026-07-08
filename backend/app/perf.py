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


def _fold_stacks(weighted_stacks) -> dict:
    """Fold an iterable of `(root->leaf frame list, weight)` into the flame tree +
    self/total hotspots. The shared core behind every profiler format — perf
    (weight 1/sample), collapsed (weight=count), speedscope (weight=sample weight)."""
    root: dict = {"name": "all", "value": 0, "children": {}}
    self_ct: dict[str, int] = {}
    total_ct: dict[str, int] = {}
    n_samples = 0

    for stack, weight in weighted_stacks:
        if not stack or weight <= 0:
            continue
        n_samples += weight
        root["value"] += weight
        node = root
        for depth, sym in enumerate(stack):
            if depth >= _MAX_DEPTH:
                break
            child = node["children"].get(sym)
            if child is None:
                child = {"name": sym, "value": 0, "children": {}}
                node["children"][sym] = child
            child["value"] += weight
            node = child
        # hotspots: total = weight anywhere in the stack; self = weight at the leaf
        for sym in set(stack):
            total_ct[sym] = total_ct.get(sym, 0) + weight
        leaf = stack[-1]
        self_ct[leaf] = self_ct.get(leaf, 0) + weight

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

    # A capture that parses to zero samples is "unsupported" (target idle / window
    # too short) — same downgrade build_flamegraph applies, so every profiler format
    # yields the friendly empty state instead of a degenerate 0-value flame bar.
    out = {
        "supported": n_samples > 0,
        "samples": n_samples,
        "tree": tree,
        "hotspots": hotspots[:_MAX_HOTSPOTS],
    }
    if n_samples == 0:
        out["reason"] = "no samples (target idle, or too short a window)."
    return out


def fold_perf_script(text: str) -> dict:
    """Fold `perf script` output (leaf-first stacks) — one unit per sample."""
    return _fold_stacks((stack, 1) for stack in _iter_stacks(text))


def fold_collapsed(text: str, *, count_is_usec: bool = False) -> dict:
    """Fold Brendan-Gregg collapsed/folded stacks: `root;a;b;leaf <count>` per line,
    already root->leaf. Covers py-spy `--format raw`, async-profiler `-o collapsed`,
    phpspy/stackcollapse, and bpftrace/`offcputime -f`. `count_is_usec` tags the unit
    (off-CPU time) without changing the tree shape."""
    def _stacks():
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            head, _, tail = line.rpartition(" ")
            if not head:
                continue
            try:
                count = int(tail)
            except ValueError:
                continue
            frames = [f for f in head.split(";") if f]
            if frames:
                yield frames, count

    fg = _fold_stacks(_stacks())
    fg["unit"] = "usec" if count_is_usec else "samples"
    return fg


def fold_speedscope(doc: dict) -> dict:
    """Fold a speedscope JSON (rbspy / dotnet-trace). Frames live in
    `shared.frames`; each `sampled` profile has `samples` (frame-index lists,
    root->leaf) + `weights`. Per-thread profiles are merged."""
    frames = [
        (f.get("name") or "[unknown]")
        for f in ((doc.get("shared") or {}).get("frames") or [])
    ]

    def _stacks():
        for prof in (doc.get("profiles") or []):
            if prof.get("type") != "sampled":
                continue
            samples = prof.get("samples") or []
            weights = prof.get("weights") or []
            for i, sample in enumerate(samples):
                try:
                    weight = int(weights[i]) if i < len(weights) else 1
                except (TypeError, ValueError):
                    weight = 1
                stack = [frames[idx] for idx in sample if 0 <= idx < len(frames)]
                if stack:
                    yield stack, weight

    return _fold_stacks(_stacks())


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


def fold_cpuprofile(doc: dict) -> dict:
    """Fold a V8 `.cpuprofile` (Node / Deno / Bun via the inspector, or dotnet's
    speedscope alt). `nodes[]` carry id + callFrame + children (child ids only);
    `samples[]` are leaf node ids; `timeDeltas[]` are per-sample µs. We build a
    child→parent map, walk each sample leaf→root, reverse to root→leaf, weighted by
    the time delta."""
    nodes = doc.get("nodes") or []
    by_id = {n.get("id"): n for n in nodes}
    parent: dict = {}
    for n in nodes:
        for c in (n.get("children") or []):
            parent[c] = n.get("id")

    def _name(n: dict) -> str:
        cf = n.get("callFrame") or {}
        fn = cf.get("functionName") or ""
        if not fn:
            return "(anonymous)"
        url = cf.get("url") or ""
        if url and not url.startswith("node:") and "/" in url:
            return f"{fn} ({url.rsplit('/', 1)[-1]}:{cf.get('lineNumber', 0)})"
        return fn

    def _stack(leaf_id):
        out: list[str] = []
        nid = leaf_id
        seen = set()
        while nid is not None and nid not in seen:
            seen.add(nid)
            n = by_id.get(nid)
            if n is None:
                break
            out.append(_name(n))
            nid = parent.get(nid)
        out.reverse()
        return out

    samples = doc.get("samples") or []
    deltas = doc.get("timeDeltas") or []

    def _stacks():
        for i, leaf in enumerate(samples):
            # V8's timeDeltas[j] is the gap ENDING at sample j, so sample i ran
            # during [t_i, t_{i+1}] → weight it by the NEXT delta (canonical).
            weight = deltas[i + 1] if i + 1 < len(deltas) else 1
            weight = weight if isinstance(weight, int) and weight > 0 else 1
            frames = _stack(leaf)
            # drop the V8 synthetic roots so the flame starts at real frames
            frames = [f for f in frames if f not in ("(root)", "(program)")]
            if frames:
                yield frames, weight

    fg = _fold_stacks(_stacks())
    fg["unit"] = "usec"
    return fg


def fold_phpspy(text: str) -> dict:
    """Fold phpspy's default trace output: per-sample blocks (blank-line separated),
    each line `<depth> <function> <file>:<line>` with depth 0 = leaf. We reverse each
    block to root->leaf and count identical stacks."""
    def _stacks():
        frames: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                if frames:
                    yield list(reversed(frames)), 1  # leaf-first block -> root->leaf
                    frames = []
                continue
            if s.startswith("#") or s.startswith("//"):
                continue
            toks = s.split()
            if len(toks) >= 2 and toks[0].isdigit():
                frames.append(toks[1])
            elif len(toks) >= 1:
                frames.append(toks[0])
        if frames:
            yield list(reversed(frames)), 1

    return _fold_stacks(_stacks())


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
