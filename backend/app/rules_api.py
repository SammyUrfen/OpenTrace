"""REST surface for Settings -> Rules: toggle/tune the built-in rule engine and
CRUD the user's custom (safe-expression) rules.

`RULE_META` (built-ins) and `storage.custom_rules` (user-authored) are the two
halves of the ruleset `trace.orchestrator` actually evaluates — see
`rules.engine.run_rules` / `rules.custom.run_custom_rules`. This module is pure
read/validate/persist; it never evaluates a rule against real event/metric data.

Public surface:
- `router` — FastAPI APIRouter at `/rules`
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config, storage
from .rules import RULE_META, CustomRuleDef, RuleExpressionError, RuleThresholds, validate_expression
from .rules.custom import FIELDS_BY_SIGNAL

router = APIRouter(prefix="/rules", tags=["rules"])


class BuiltinRule(BaseModel):
    id: str
    signal: str
    label: str
    description: str
    enabled: bool
    thresholds: dict[str, float]


class BuiltinRuleUpdate(BaseModel):
    enabled: bool | None = None
    # Sparse {threshold_name: value}; only names in this rule's own
    # RULE_META["thresholds"] are accepted (checked below).
    thresholds: dict[str, float] | None = None


class CustomRule(BaseModel):
    id: str
    name: str
    description: str
    signal: str
    expression: str
    severity: str
    enabled: bool
    min_count: int
    duration_ms: int
    created_at: int


class CustomRuleCreate(BaseModel):
    name: str
    description: str = ""
    signal: str
    expression: str
    severity: str = "medium"
    enabled: bool = True
    min_count: int = 5
    duration_ms: int = 5000


class CustomRuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    signal: str | None = None
    expression: str | None = None
    severity: str | None = None
    enabled: bool | None = None
    min_count: int | None = None
    duration_ms: int | None = None


class ExpressionCheck(BaseModel):
    signal: str
    expression: str


def _builtin_from_meta(meta: dict, thresholds: RuleThresholds, disabled: set[str]) -> BuiltinRule:
    return BuiltinRule(
        id=meta["id"], signal=meta["signal"], label=meta["label"],
        description=meta["description"], enabled=meta["id"] not in disabled,
        thresholds={name: getattr(thresholds, name) for name in meta["thresholds"]},
    )


def _custom_view(d: CustomRuleDef) -> CustomRule:
    return CustomRule(
        id=d.id, name=d.name, description=d.description, signal=d.signal,
        expression=d.expression, severity=d.severity, enabled=d.enabled,
        min_count=d.min_count, duration_ms=d.duration_ms, created_at=d.created_at,
    )


@router.get("")
def http_list() -> dict:
    cfg = config.load()
    thresholds = RuleThresholds.from_overrides(cfg.tracing.rule_thresholds)
    disabled = set(cfg.tracing.disabled_rules)
    return {
        "builtin": [_builtin_from_meta(m, thresholds, disabled).model_dump() for m in RULE_META],
        "custom": [_custom_view(d).model_dump() for d in storage.list_custom_rules()],
    }


@router.put("/builtin/{rule_id}", response_model=BuiltinRule)
def http_update_builtin(rule_id: str, data: BuiltinRuleUpdate) -> BuiltinRule:
    meta = next((m for m in RULE_META if m["id"] == rule_id), None)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown rule id")
    cfg = config.load()
    if data.enabled is not None:
        disabled = set(cfg.tracing.disabled_rules)
        disabled.discard(rule_id) if data.enabled else disabled.add(rule_id)
        cfg.tracing.disabled_rules = sorted(disabled)
    if data.thresholds:
        unknown = set(data.thresholds) - set(meta["thresholds"])
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"'{rule_id}' has no threshold(s) named: {', '.join(sorted(unknown))}",
            )
        merged = dict(cfg.tracing.rule_thresholds)
        merged.update(data.thresholds)
        cfg.tracing.rule_thresholds = merged
    config.save(cfg)
    return _builtin_from_meta(
        meta,
        RuleThresholds.from_overrides(cfg.tracing.rule_thresholds),
        set(cfg.tracing.disabled_rules),
    )


def _validate_or_400(signal: str, expression: str) -> None:
    fields = FIELDS_BY_SIGNAL.get(signal)
    if fields is None:
        raise HTTPException(status_code=400, detail="signal must be 'events' or 'metrics'")
    try:
        validate_expression(expression, fields)
    except RuleExpressionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/custom/validate")
def http_validate(data: ExpressionCheck) -> dict:
    """Dry-run check the settings UI calls on every keystroke (debounced) so a
    bad expression is caught before it's ever saved, let alone evaluated
    against a real run. Never raises — the error is the payload."""
    fields = FIELDS_BY_SIGNAL.get(data.signal)
    if fields is None:
        return {"ok": False, "error": "signal must be 'events' or 'metrics'", "fields": []}
    try:
        validate_expression(data.expression, fields)
    except RuleExpressionError as e:
        return {"ok": False, "error": str(e), "fields": sorted(fields)}
    return {"ok": True, "error": None, "fields": sorted(fields)}


@router.post("/custom", response_model=CustomRule)
def http_create_custom(data: CustomRuleCreate) -> CustomRule:
    _validate_or_400(data.signal, data.expression)
    d = storage.create_custom_rule(
        name=data.name.strip() or "Untitled rule", description=data.description,
        signal=data.signal, expression=data.expression, severity=data.severity,
        enabled=data.enabled, min_count=data.min_count, duration_ms=data.duration_ms,
    )
    return _custom_view(d)


@router.put("/custom/{rule_id}", response_model=CustomRule)
def http_update_custom(rule_id: str, data: CustomRuleUpdate) -> CustomRule:
    existing = storage.get_custom_rule(rule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="custom rule not found")
    if data.signal is not None or data.expression is not None:
        _validate_or_400(data.signal or existing.signal, data.expression if data.expression is not None else existing.expression)
    updated = storage.update_custom_rule(rule_id, **data.model_dump(exclude_unset=True))
    return _custom_view(updated)


@router.delete("/custom/{rule_id}")
def http_delete_custom(rule_id: str) -> dict:
    if not storage.delete_custom_rule(rule_id):
        raise HTTPException(status_code=404, detail="custom rule not found")
    return {"deleted": True}
