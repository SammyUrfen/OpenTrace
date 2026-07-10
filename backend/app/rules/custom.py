"""User-authored anomaly rules: a safe boolean expression (`safe_eval.py`) over
event/metric fields, evaluated the same way the built-ins are (`engine.py`),
but sourced from `storage.list_custom_rules` instead of hardcoded Python.

Two modes, mirroring how the built-ins already split by signal:
- "events": the expression is tested against EACH event; fires once at least
  `min_count` events match (mirrors `failed_file_opens` / `repeated_open_same_file`).
- "metrics": the expression is tested against each metric sample; fires when it
  holds for a CONTIGUOUS run of samples spanning at least `duration_ms`
  (mirrors `cpu_bound_metric` / `io_wait_metric`).

Fail-open like the rest of the engine: a rule whose expression no longer
validates (edited config, field renamed) is skipped, never raised — the
`/rules/custom/validate` endpoint is where a bad expression is caught, at
save time, not during a run.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..trace.events import Anomaly, TraceEvent
from .engine import RuleContext, _score
from .safe_eval import RuleExpressionError, compile_expression

EVENT_FIELDS = {
    "event_type", "syscall", "args", "retval", "retval_int", "error",
    "latency_ms", "fd", "path", "pid", "timestamp_ms",
}
METRIC_FIELDS = {
    "timestamp_ms", "cpu_pct", "rss_mb", "vms_mb", "open_fds", "threads",
    "syscall_rate", "io_read_bps", "io_write_bps",
}
FIELDS_BY_SIGNAL = {"events": EVENT_FIELDS, "metrics": METRIC_FIELDS}


@dataclass
class CustomRuleDef:
    id: str
    name: str
    description: str
    signal: str  # "events" | "metrics"
    expression: str
    severity: str = "medium"
    enabled: bool = True
    min_count: int = 5        # events mode: matches needed to fire
    duration_ms: int = 5000   # metrics mode: contiguous span needed to fire
    created_at: int = 0


def _event_vars(ev: TraceEvent) -> dict:
    retval_int: int | None = None
    if ev.retval is not None:
        try:
            retval_int = int(ev.retval, 0)
        except (ValueError, TypeError):
            retval_int = None
    return {
        "event_type": ev.event_type, "syscall": ev.syscall or "",
        "args": ev.args or "", "retval": ev.retval or "",
        "retval_int": retval_int, "error": ev.error or "",
        "latency_ms": ev.latency_ms, "fd": ev.fd, "path": ev.path or "",
        "pid": ev.pid, "timestamp_ms": ev.timestamp_ms,
    }


def _metric_vars(row: dict) -> dict:
    return {k: row.get(k) for k in METRIC_FIELDS}


def _eval_events_rule(d: CustomRuleDef, ctx: RuleContext) -> Anomaly | None:
    try:
        pred = compile_expression(d.expression, EVENT_FIELDS)
    except RuleExpressionError:
        return None
    matches = []
    for ev in ctx.events:
        try:
            if pred(_event_vars(ev)):
                matches.append(ev)
        except Exception:  # noqa: BLE001 — a type mismatch on one row can't sink the rule
            continue
    if len(matches) < d.min_count:
        return None
    n = len(matches)
    return Anomaly(
        rule_id=f"custom:{d.id}",
        severity=d.severity,
        severity_score=_score(d.severity, n),
        title=f"{d.name} ({n}x)",
        description=d.description or f"Custom rule matched {n} time(s): {d.expression}",
        evidence=matches[:20],
        first_seen_ms=matches[0].timestamp_ms,
        last_seen_ms=matches[-1].timestamp_ms,
        occurrence_count=n,
    )


def _eval_metrics_rule(d: CustomRuleDef, ctx: RuleContext) -> Anomaly | None:
    try:
        pred = compile_expression(d.expression, METRIC_FIELDS)
    except RuleExpressionError:
        return None
    rows = sorted(
        (r for r in ctx.metrics if r.get("timestamp_ms") is not None),
        key=lambda r: r["timestamp_ms"],
    )
    streak_start: float | None = None
    best_span = 0.0
    best_first: float | None = None
    best_last: float | None = None
    for row in rows:
        ts = row["timestamp_ms"]
        try:
            hit = pred(_metric_vars(row))
        except Exception:  # noqa: BLE001
            hit = False
        if hit:
            if streak_start is None:
                streak_start = ts
            span = ts - streak_start
            if span >= best_span:
                best_span, best_first, best_last = span, streak_start, ts
        else:
            streak_start = None
    if best_first is None or best_span < d.duration_ms:
        return None
    return Anomaly(
        rule_id=f"custom:{d.id}",
        severity=d.severity,
        severity_score=_score(d.severity, int(best_span)),
        title=d.name,
        description=d.description or f"Custom rule held for {int(best_span / 1000)}s: {d.expression}",
        first_seen_ms=best_first,
        last_seen_ms=best_last,
        occurrence_count=1,
    )


def run_custom_rules(ctx: RuleContext, defs: list[CustomRuleDef]) -> list[Anomaly]:
    found: list[Anomaly] = []
    for d in defs:
        if not d.enabled:
            continue
        if d.signal == "events" and ctx.events:
            a = _eval_events_rule(d, ctx)
        elif d.signal == "metrics" and ctx.metrics:
            a = _eval_metrics_rule(d, ctx)
        else:
            a = None
        if a is not None:
            found.append(a)
    found.sort(key=lambda a: a.severity_score, reverse=True)
    return found
