"""Build the LLM prompt for a run and stream/persist the AI summary.

The model receives a compact, pre-digested summary (metrics peaks + the rule
engine's anomalies) — never the raw event stream — to keep cost and latency
down, exactly as the roadmap's LLM design specifies. The rule-based anomaly
descriptions are the fallback when no LLM is configured (they already render in
the Overview), so the LLM purely *adds* interpretation.

Public surface:
- `build_messages(run, summary, anomalies) -> list[dict]`
- `ai_summary_path(run) -> Path`
- `stream_summary(run, *, force) -> AsyncIterator[dict]`  (persists on completion)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from . import llm, runs, storage
from .util import now_ms

SYSTEM_PROMPT = (
    "You are OpenTrace's analysis assistant. You read a structured digest of a "
    "single Linux program run (captured with strace/ltrace + psutil, and "
    "optionally perf) — including an event TIMELINE of what happened when — and "
    "explain it to the developer who ran it. Interpret the sequence and patterns; "
    "use the timeline to describe what the program did over its lifetime, not just "
    "final totals.\n\n"
    "Respond in GitHub-flavoured markdown using EXACTLY these section headers, in "
    "this order:\n"
    "## What Happened\n## What's Wrong\n## Why It Matters\n## What to Investigate\n"
    "## Confidence\n\n"
    "'What Happened' is a 2–4 sentence narrative of the run over time (lean on the "
    "timeline + memory/CPU trajectory). Be concise and concrete elsewhere; quantify "
    "impact; point at likely files/functions/call-patterns. If nothing is wrong, "
    "say so plainly in 'What's Wrong'. Metrics are measured under a tracer, which "
    "adds overhead. End 'Confidence' with a one-line honest uncertainty note. Do "
    "not invent data beyond what is given."
)


def _hbytes(n: float | int | None) -> str:
    if not n:
        return "0B"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024 or unit == "GB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}GB"


def _timeline(events: list[dict], started_at: int, *, limit: int = 22) -> list[str]:
    """A compact 'what happened when' from curated events (errors / slow calls /
    signals / exec / exit), collapsing consecutive identical errors into one."""
    notable: list[tuple] = []
    for e in events:
        ts = e.get("timestamp_ms")
        if ts is None:
            continue
        et, sc, err, lat = e.get("event_type"), e.get("syscall"), e.get("error"), e.get("latency_ms")
        if et in ("signal", "exit"):
            notable.append((ts, et, sc, err, lat, e.get("retval")))
        elif err:
            notable.append((ts, "error", sc, err, None, e.get("path")))
        elif lat is not None and lat > 100:
            notable.append((ts, "slow", sc, None, lat, None))
        elif sc in ("execve", "execveat"):
            notable.append((ts, "exec", sc, None, None, e.get("path") or (e.get("args") or "")[:50]))
    notable.sort(key=lambda x: x[0])

    def rel(t: float) -> float:
        return (t - started_at) / 1000.0

    lines: list[str] = []
    i = 0
    while i < len(notable):
        ts, kind, sc, err, lat, extra = notable[i]
        j = i + 1
        if kind == "error":
            while (j < len(notable) and notable[j][1] == "error"
                   and notable[j][2] == sc and notable[j][3] == err):
                j += 1
        n = j - i
        if kind == "error":
            if n > 1:
                lines.append(f"+{rel(ts):.2f}–{rel(notable[j-1][0]):.2f}s {sc} → {err} ×{n}")
            else:
                lines.append(f"+{rel(ts):.2f}s {sc} → {err}")
        elif kind == "slow":
            lines.append(f"+{rel(ts):.2f}s {sc} took {lat:.0f}ms")
        elif kind == "exec":
            lines.append(f"+{rel(ts):.2f}s exec {str(extra or '').strip()}".rstrip())
        elif kind == "signal":
            lines.append(f"+{rel(ts):.2f}s signal {sc}")
        elif kind == "exit":
            lines.append(f"+{rel(ts):.2f}s exited {extra if extra is not None else (err or '')}".rstrip())
        i = j
    if len(lines) > limit:
        lines = lines[: limit - 1] + [f"… (+{len(lines) - (limit - 1)} more timeline events)"]
    return lines


def _trajectory(metrics: list[dict]) -> list[str]:
    out: list[str] = []
    rss = [m["rss_mb"] for m in metrics if m.get("rss_mb") is not None]
    cpu = [m["cpu_pct"] for m in metrics if m.get("cpu_pct") is not None]
    fds = [m["open_fds"] for m in metrics if m.get("open_fds") is not None]
    if rss:
        trend = ("rising" if rss[-1] > rss[0] * 1.2 + 1 else
                 "falling" if rss[-1] < rss[0] * 0.8 else "steady")
        out.append(f"RSS over time: {rss[0]:.0f}→{rss[-1]:.0f}MB, peak {max(rss):.0f}MB ({trend})")
    if cpu:
        out.append(f"CPU over time: avg {sum(cpu) / len(cpu):.0f}%, peak {max(cpu):.0f}%")
    if fds and max(fds) > min(fds):
        out.append(f"Open FDs: {fds[0]}→{fds[-1]}, peak {max(fds)}")
    return out


def _io_lines(io: list[dict], *, limit: int = 6) -> list[str]:
    rows = sorted(io, key=lambda r: r.get("read_bytes", 0) + r.get("write_bytes", 0), reverse=True)
    out = []
    for r in rows[:limit]:
        leak = " ⊘unclosed" if r.get("leaked") else ""
        out.append(
            f"{r.get('path')}: {r.get('reads', 0)}r/{r.get('writes', 0)}w, "
            f"{_hbytes(r.get('read_bytes'))} read / {_hbytes(r.get('write_bytes'))} written{leak}"
        )
    return out


def _net_lines(net: list[dict], *, limit: int = 6) -> list[str]:
    out = []
    for c in net[:limit]:
        res = c.get("result")
        out.append(
            f"{c.get('address')}:{c.get('port')} ({c.get('family', '')}) → {res}"
            + (f" {c.get('latency_ms'):.0f}ms" if c.get("latency_ms") else "")
        )
    return out


def _profile_lines(profile: dict | None) -> list[str]:
    m = (profile or {}).get("malloc") or {}
    if not m.get("supported"):
        return []
    out = [
        f"Allocations: {m.get('n_alloc')} vs {m.get('n_free')} frees; "
        f"{_hbytes(m.get('bytes_allocated'))} allocated, peak live {_hbytes(m.get('peak_live_bytes'))}"
    ]
    if m.get("outstanding_bytes"):
        out.append(
            f"LEAKED at exit: {_hbytes(m['outstanding_bytes'])} in "
            f"{m.get('outstanding_blocks')} un-freed block(s)"
        )
    hot = (profile or {}).get("hotspots") or []
    if hot:
        out.append("Top library calls by time: " + ", ".join(
            f"{h['function']}×{h['calls']}({h['total_ms']:.0f}ms)" for h in hot[:5]
        ))
    return out


def _flame_lines(flamegraph: dict | None) -> list[str]:
    fg = flamegraph or {}
    if not fg.get("supported"):
        return []
    hot = fg.get("hotspots") or []
    if not hot:
        return []
    return ["CPU hotspots (perf, self%): " + ", ".join(
        f"{h['function']} {h['self_pct']:.0f}%" for h in hot[:8]
    )]


def build_messages(
    run: runs.Run,
    summary: dict | None,
    anomalies: list[dict],
    *,
    timeline: list[str] | None = None,
    trajectory: list[str] | None = None,
    io: list[dict] | None = None,
    network: list[dict] | None = None,
    profile: dict | None = None,
    flamegraph: dict | None = None,
) -> list[dict]:
    s = summary or {}
    totals = s.get("totals", {})
    peaks = s.get("peaks", {})
    parts: list[str] = [
        f"Command: {run.command}",
        f"Working dir: {run.cwd}",
        f"Duration: {run.duration_ms} ms | exit code: {run.exit_code} | "
        f"signal: {run.exit_signal or 'none'}",
        f"Peak CPU: {peaks.get('cpu_pct')}% (summed per-core, may exceed 100 on "
        f"multiple cores) | Peak RSS: {peaks.get('rss_mb')} MB | "
        f"Peak open FDs: {peaks.get('open_fds')} | Peak threads: {peaks.get('threads')}",
        f"Total syscall events: {totals.get('syscall_events')} | "
        f"errors: {totals.get('errors')} | metric samples: {totals.get('metric_samples')}",
    ]
    top = totals.get("top_syscalls") or []
    if top:
        parts.append("Top syscalls (name×count): " + ", ".join(
            f"{n}×{c}" for n, c in top[:10]
        ))
    if trajectory:
        parts.append("\nResource trajectory:\n" + "\n".join(f"  {t}" for t in trajectory))
    if timeline:
        parts.append("\nEvent timeline (relative to start):\n" + "\n".join(f"  {t}" for t in timeline))
    if io:
        parts.append("\nTop files by I/O:\n" + "\n".join(f"  {x}" for x in _io_lines(io)))
    net_lines = _net_lines(network) if network else []
    if net_lines:
        parts.append("\nOutbound connections:\n" + "\n".join(f"  {x}" for x in net_lines))
    prof_lines = _profile_lines(profile)
    if prof_lines:
        parts.append("\nAllocation profile (ltrace):\n" + "\n".join(f"  {x}" for x in prof_lines))
    flame_lines = _flame_lines(flamegraph)
    if flame_lines:
        parts.append("\n" + "\n".join(flame_lines))
    if anomalies:
        parts.append("\nAnomalies detected by the rule engine:")
        for a in anomalies:
            parts.append(
                f"- [{a['severity'].upper()}] {a['title']} "
                f"(×{a.get('occurrence_count', 1)}): {a['description']}"
            )
    else:
        parts.append("\nThe rule engine detected no anomalies.")
    parts.append("\nWrite the analysis now.")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


DIFF_SYSTEM_PROMPT = (
    "You compare two runs (A and B) of usually the same program for the developer "
    "who ran them. Explain WHAT CHANGED and whether B is better, worse, or mixed "
    "versus A. Respond in markdown using EXACTLY these headers:\n"
    "## Verdict\n## What Changed\n## Likely Cause\n## What to Check\n\n"
    "'Verdict' is one line (better / worse / mixed + the headline). Quantify the "
    "deltas, focus on the biggest changes (runtime, memory, syscalls, anomalies), "
    "and note metrics are under strace overhead. Don't invent data."
)


def _run_block(tag: str, run: runs.Run, summary: dict | None, anomalies: list[dict]) -> str:
    s = summary or {}
    peaks = s.get("peaks", {})
    totals = s.get("totals", {})
    lines = [
        f"Run {tag}: {run.command}",
        f"  duration={run.duration_ms}ms exit={run.exit_code} "
        f"peakCPU={peaks.get('cpu_pct')}% peakRSS={peaks.get('rss_mb')}MB "
        f"syscalls={totals.get('syscall_events')} errors={totals.get('errors')}",
    ]
    if anomalies:
        lines.append(f"  anomalies: " + "; ".join(
            f"[{a['severity']}] {a['title']}" for a in anomalies
        ))
    else:
        lines.append("  anomalies: none")
    return "\n".join(lines)


def build_diff_messages(
    run_a: runs.Run, sum_a: dict | None, anom_a: list[dict],
    run_b: runs.Run, sum_b: dict | None, anom_b: list[dict],
) -> list[dict]:
    a_rules = {x["rule_id"] for x in anom_a}
    b_rules = {x["rule_id"] for x in anom_b}
    added = [x["title"] for x in anom_b if x["rule_id"] not in a_rules]
    removed = [x["title"] for x in anom_a if x["rule_id"] not in b_rules]

    def delta(key: str, path: str) -> str:
        pa = (sum_a or {}).get(path, {}).get(key)
        pb = (sum_b or {}).get(path, {}).get(key)
        if pa is None or pb is None:
            return f"{key}: A={pa} B={pb}"
        return f"{key}: A={pa} B={pb} (Δ {pb - pa:+})"

    user = "\n".join([
        _run_block("A", run_a, sum_a, anom_a),
        _run_block("B", run_b, sum_b, anom_b),
        "\nKey deltas (B − A):",
        f"  duration: A={run_a.duration_ms} B={run_b.duration_ms} "
        f"(Δ {(run_b.duration_ms or 0) - (run_a.duration_ms or 0):+}ms)",
        f"  {delta('rss_mb', 'peaks')}",
        f"  {delta('cpu_pct', 'peaks')}",
        f"  {delta('syscall_events', 'totals')}",
        f"  {delta('errors', 'totals')}",
        f"  anomalies added in B: {added or 'none'}",
        f"  anomalies gone in B: {removed or 'none'}",
        "\nWrite the comparison now.",
    ])
    return [
        {"role": "system", "content": DIFF_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def stream_diff_summary(run_a: runs.Run, run_b: runs.Run) -> AsyncIterator[dict]:
    if not llm.is_configured():
        yield {"type": "error", "message": "LLM is not configured"}
        return
    sum_a = _load_meta(run_a)
    sum_b = _load_meta(run_b)
    messages = build_diff_messages(
        run_a, sum_a, storage.read_anomalies(run_a.id),
        run_b, sum_b, storage.read_anomalies(run_b.id),
    )
    async for ev in llm.stream_chat(messages):
        yield ev


def _load_meta(run: runs.Run) -> dict | None:
    meta = Path(run.run_dir) / "meta.json"
    return json.loads(meta.read_text()) if meta.exists() else None


def ai_summary_path(run: runs.Run) -> Path:
    return Path(run.run_dir) / "ai_summary.md"


def read_cached(run: runs.Run) -> dict | None:
    p = ai_summary_path(run)
    if not p.exists():
        return None
    meta = p.with_suffix(".meta.json")
    generated_at = None
    if meta.exists():
        try:
            generated_at = json.loads(meta.read_text()).get("generated_at")
        except (json.JSONDecodeError, OSError):
            pass
    return {"text": p.read_text(encoding="utf-8"), "generated_at": generated_at}


def _persist(run: runs.Run, text: str) -> None:
    p = ai_summary_path(run)
    p.write_text(text, encoding="utf-8")
    p.with_suffix(".meta.json").write_text(
        json.dumps({"generated_at": now_ms(), "model": None}), encoding="utf-8"
    )
    storage.record_artifact(run.id, "ai-summary", p)


def _gather_context(run: runs.Run) -> dict:
    """Assemble the token-budgeted extra context (timeline, trajectory, I/O,
    network, profile, flamegraph) for the LLM prompt. Best-effort: any piece
    that fails to load is simply omitted."""
    from . import aggregate

    ctx: dict = {}
    run_dir = Path(run.run_dir)
    try:
        ctx["timeline"] = _timeline(storage.read_events(run.id, limit=3000), run.started_at)
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx["trajectory"] = _trajectory(storage.read_metrics(run.id))
    except Exception:  # noqa: BLE001
        pass
    events_path = run_dir / "events.ndjson.zst"
    if events_path.exists():
        try:
            events = list(storage.read_ndjson_zst(events_path))
            ctx["io"] = aggregate.io_stats(events)
            ctx["network"] = aggregate.network_stats(events)
        except Exception:  # noqa: BLE001
            pass
    for key, fname in (("profile", "profile.json"), ("flamegraph", "flamegraph.json")):
        p = run_dir / fname
        if p.exists():
            try:
                ctx[key] = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return ctx


async def stream_summary(run: runs.Run, *, force: bool = False) -> AsyncIterator[dict]:
    """Yield typed events (thinking/content/error/done). Streams the cached
    summary verbatim when present unless `force`; otherwise calls the LLM and
    persists the result on completion."""
    if not force:
        cached = read_cached(run)
        if cached:
            yield {"type": "content", "text": cached["text"]}
            yield {"type": "done", "cached": True}
            return

    if not llm.is_configured():
        yield {"type": "error", "message": "LLM is not configured"}
        return

    meta = Path(run.run_dir) / "meta.json"
    summary = json.loads(meta.read_text()) if meta.exists() else None
    anomalies = storage.read_anomalies(run.id)
    messages = build_messages(run, summary, anomalies, **_gather_context(run))
    acc: list[str] = []
    async for ev in llm.stream_chat(messages):
        if ev["type"] == "content":
            acc.append(ev["text"])
        yield ev
        if ev["type"] == "error":
            return
    if acc:
        _persist(run, "".join(acc).strip())


# --- monitor-mode incident explanations (continuous AI) ---------------------

INCIDENT_SYSTEM_PROMPT = (
    "You are OpenTrace. A live monitor of a running process just detected a "
    "performance anomaly. Given the anomaly and the CPU hot call path captured at "
    "that moment, explain in 2-3 sentences WHAT likely happened and WHERE in the "
    "code — name the function/class from the hot path, and if it points at a "
    "specific operation (an HTTP handler, a scheduled/cron task, a cache/GC/"
    "serialization routine) say so. If the cause is likely OFF-CPU (waiting on I/O, "
    "a database, or a lock), note that a CPU profile can't confirm it. Be concrete "
    "and terse — no preamble, no headers."
)


def _incident_context(incident: dict) -> str:
    hot = incident.get("hot") or {}
    stack = hot.get("stack") or []
    funcs = hot.get("functions") or []
    metrics = incident.get("metrics") or []

    def _last(key: str):
        vals = [m.get(key) for m in metrics if m.get(key) is not None]
        return vals[-1] if vals else None

    return "\n".join([
        f"Anomaly: {incident.get('title')}",
        f"Severity: {incident.get('severity')}",
        f"CPU hot call path (root->leaf): {' -> '.join(stack[:10]) or 'n/a (no profile at this moment)'}",
        f"Top functions by self time: {', '.join(funcs[:5]) or 'n/a'}",
        f"At the incident: cpu={_last('cpu_pct')}% rss={_last('rss_mb')}MB fds={_last('open_fds')} threads={_last('threads')}",
        f"Metric samples in the leading window: {len(metrics)}",
    ])


def incident_summary(incident: dict) -> str:
    """A short synchronous AI explanation of a monitor incident. Called from a
    worker thread (runs its own event loop). Returns '' on any failure."""
    import asyncio

    messages = [
        {"role": "system", "content": INCIDENT_SYSTEM_PROMPT},
        {"role": "user", "content": _incident_context(incident)},
    ]

    async def _collect() -> str:
        acc: list[str] = []
        async for ev in llm.stream_chat(messages, max_tokens=400):
            if ev.get("type") == "content":
                acc.append(ev["text"])
            elif ev.get("type") == "error":
                return ""
        return "".join(acc).strip()

    try:
        return asyncio.run(_collect())
    except Exception:  # noqa: BLE001
        return ""
