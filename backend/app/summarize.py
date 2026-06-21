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
    "You are OpenTrace's analysis assistant. You read a structured summary of a "
    "single Linux program run (captured with strace + psutil) and explain it to "
    "the developer who ran it. Interpret patterns — do not just restate numbers.\n\n"
    "Respond in GitHub-flavoured markdown using EXACTLY these section headers, in "
    "this order:\n"
    "## What's Wrong\n## Why It Matters\n## What to Investigate\n"
    "## What Looks Fine\n## Confidence\n\n"
    "Guidelines: be concise and concrete; quantify impact where you can; point at "
    "likely files/functions/call-patterns to check; if nothing is wrong, say so "
    "plainly in 'What's Wrong'. Note that metrics are measured under strace, which "
    "adds overhead. End 'Confidence' with a one-line honest uncertainty note. Do "
    "not invent data beyond what is given."
)


def build_messages(run: runs.Run, summary: dict | None, anomalies: list[dict]) -> list[dict]:
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

    messages = build_messages(run, summary, anomalies)
    acc: list[str] = []
    async for ev in llm.stream_chat(messages):
        if ev["type"] == "content":
            acc.append(ev["text"])
        yield ev
        if ev["type"] == "error":
            return
    if acc:
        _persist(run, "".join(acc).strip())
