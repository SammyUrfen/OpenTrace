"""User-authored custom rules: the safe expression sandbox, event/metric
evaluation modes, RULE_META derivation, disabled_rules gating, the storage CRUD,
and the /rules REST surface."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app
from app.rules.custom import CustomRuleDef, run_custom_rules
from app.rules.engine import RULE_META, RuleContext, run_rules
from app.rules.safe_eval import RuleExpressionError, compile_expression, validate_expression
from app.trace.events import SYSCALL, TraceEvent


def ev(ts, syscall, *, error=None, path=None, retval="0"):
    return TraceEvent(timestamp_ms=ts, pid=1, event_type=SYSCALL, syscall=syscall, error=error, path=path, retval=retval)


def metric(ts, **kw):
    base = dict(timestamp_ms=ts, cpu_pct=None, rss_mb=None, vms_mb=None,
                open_fds=None, threads=None, syscall_rate=None, io_read_bps=None, io_write_bps=None)
    base.update(kw)
    return base


# --- safe_eval ----------------------------------------------------------------

def test_valid_expression_passes():
    validate_expression("cpu_pct > 90 and syscall_rate < 5", {"cpu_pct", "syscall_rate"})


def test_unknown_field_rejected():
    with pytest.raises(RuleExpressionError, match="unknown field"):
        validate_expression("nope > 1", {"cpu_pct"})


def test_syntax_error_rejected():
    with pytest.raises(RuleExpressionError, match="syntax error"):
        validate_expression("cpu_pct >", {"cpu_pct"})


@pytest.mark.parametrize("expr", [
    "().__class__",
    "__import__('os')",
    "(1).bit_length()",
    "[x for x in ()]",
    "lambda: 1",
    "cpu_pct if True else 0",
    "open('/etc/passwd')",
])
def test_sandbox_escapes_rejected(expr):
    with pytest.raises(RuleExpressionError):
        validate_expression(expr, {"cpu_pct"})


def test_compiled_expression_evaluates():
    pred = compile_expression("cpu_pct > 90 and syscall_rate < 5", {"cpu_pct", "syscall_rate"})
    assert pred({"cpu_pct": 95, "syscall_rate": 1}) is True
    assert pred({"cpu_pct": 50, "syscall_rate": 1}) is False


def test_in_operator_allowed_for_substring_and_membership():
    pred = compile_expression("syscall in ('open', 'openat')", {"syscall"})
    assert pred({"syscall": "openat"}) is True
    assert pred({"syscall": "read"}) is False


def test_too_long_expression_rejected():
    with pytest.raises(RuleExpressionError, match="too long"):
        validate_expression("cpu_pct > 1" + " or cpu_pct > 1" * 100, {"cpu_pct"})


# --- custom.py: events mode ----------------------------------------------------

def test_events_rule_fires_on_min_count():
    d = CustomRuleDef(
        id="x", name="ENOENT storm", description="", signal="events",
        expression="error == 'ENOENT'", min_count=3,
    )
    events = [ev(i, "openat", error="ENOENT", path="/x") for i in range(5)]
    out = run_custom_rules(RuleContext(events=events), [d])
    assert len(out) == 1
    assert out[0].rule_id == "custom:x"
    assert out[0].occurrence_count == 5


def test_events_rule_below_min_count_is_silent():
    d = CustomRuleDef(id="x", name="n", description="", signal="events",
                       expression="error == 'ENOENT'", min_count=10)
    events = [ev(i, "openat", error="ENOENT") for i in range(3)]
    assert run_custom_rules(RuleContext(events=events), [d]) == []


def test_events_rule_retval_int_comparison():
    d = CustomRuleDef(id="x", name="n", description="", signal="events",
                       expression="retval_int < 0", min_count=2)
    events = [ev(i, "read", retval="-1") for i in range(3)] + [ev(i, "read", retval="4") for i in range(3)]
    out = run_custom_rules(RuleContext(events=events), [d])
    assert out[0].occurrence_count == 3


def test_disabled_custom_rule_is_skipped():
    d = CustomRuleDef(id="x", name="n", description="", signal="events",
                       expression="error == 'ENOENT'", min_count=1, enabled=False)
    events = [ev(0, "openat", error="ENOENT")]
    assert run_custom_rules(RuleContext(events=events), [d]) == []


def test_invalid_expression_fails_open_not_raises():
    d = CustomRuleDef(id="x", name="n", description="", signal="events",
                       expression="().__class__", min_count=1)
    events = [ev(0, "openat", error="ENOENT")]
    assert run_custom_rules(RuleContext(events=events), [d]) == []


# --- custom.py: metrics mode ----------------------------------------------------

def test_metrics_rule_fires_on_sustained_streak():
    d = CustomRuleDef(id="y", name="hot", description="", signal="metrics",
                       expression="cpu_pct > 90", duration_ms=2000)
    metrics = [metric(i * 1000, cpu_pct=95) for i in range(5)]  # 0..4000ms, contiguous
    out = run_custom_rules(RuleContext(events=[], metrics=metrics), [d])
    assert len(out) == 1 and out[0].rule_id == "custom:y"


def test_metrics_rule_short_streak_does_not_fire():
    d = CustomRuleDef(id="y", name="hot", description="", signal="metrics",
                       expression="cpu_pct > 90", duration_ms=5000)
    metrics = [metric(0, cpu_pct=95), metric(1000, cpu_pct=95)]  # only 1000ms span
    assert run_custom_rules(RuleContext(events=[], metrics=metrics), [d]) == []


def test_metrics_rule_streak_resets_on_gap():
    d = CustomRuleDef(id="y", name="hot", description="", signal="metrics",
                       expression="cpu_pct > 90", duration_ms=3000)
    metrics = [
        metric(0, cpu_pct=95), metric(1000, cpu_pct=95),  # 1000ms streak
        metric(2000, cpu_pct=10),                          # breaks it
        metric(3000, cpu_pct=95), metric(4000, cpu_pct=95),  # new 1000ms streak
    ]
    assert run_custom_rules(RuleContext(events=[], metrics=metrics), [d]) == []


def test_metrics_rule_none_field_does_not_crash():
    d = CustomRuleDef(id="y", name="hot", description="", signal="metrics",
                       expression="cpu_pct > 90", duration_ms=1000)
    metrics = [metric(0, cpu_pct=None), metric(1000, cpu_pct=None)]
    assert run_custom_rules(RuleContext(events=[], metrics=metrics), [d]) == []


def test_wrong_signal_data_present_is_noop():
    # an events-mode rule against a metrics-only context (and vice versa) never fires
    d_events = CustomRuleDef(id="a", name="n", description="", signal="events", expression="pid > 0")
    d_metrics = CustomRuleDef(id="b", name="n", description="", signal="metrics", expression="cpu_pct > 0")
    ctx_metrics_only = RuleContext(events=[], metrics=[metric(0, cpu_pct=99)])
    ctx_events_only = RuleContext(events=[ev(0, "read")], metrics=[])
    assert run_custom_rules(ctx_metrics_only, [d_events]) == []
    assert run_custom_rules(ctx_events_only, [d_metrics]) == []


# --- RULE_META + disabled_rules gating on the built-in engine -----------------

def test_rule_meta_covers_every_registered_rule():
    ids = {m["id"] for m in RULE_META}
    assert "repeated_open_same_file" in ids
    assert "cpu_bound_metric" in ids
    assert len(ids) >= 20  # every rule.__name__ is unique; a real drop would shrink this


def test_rule_meta_signal_matches_needs_tag():
    for m in RULE_META:
        assert m["signal"] in ("events", "metrics")


def test_rule_meta_thresholds_are_real_fields():
    from app.rules.engine import RuleThresholds
    defaults = RuleThresholds()
    for m in RULE_META:
        for name in m["thresholds"]:
            assert hasattr(defaults, name)


def test_disabled_rules_skips_the_rule():
    events = [ev(i * 10, "openat", path="/app/data.txt", retval="3") for i in range(12)]
    out = run_rules(RuleContext(events=events))
    assert any(a.rule_id == "repeated_open_same_file" for a in out)
    out2 = run_rules(RuleContext(events=events, disabled_rules=frozenset({"repeated_open_same_file"})))
    assert not any(a.rule_id == "repeated_open_same_file" for a in out2)


# --- storage CRUD ---------------------------------------------------------------

def test_custom_rule_storage_roundtrip(ot_home):
    d = storage.create_custom_rule(
        name="test", description="desc", signal="metrics",
        expression="cpu_pct > 1", severity="high", min_count=1, duration_ms=1000,
    )
    assert d.id
    fetched = storage.get_custom_rule(d.id)
    assert fetched == d

    updated = storage.update_custom_rule(d.id, enabled=False, name="renamed")
    assert updated.enabled is False and updated.name == "renamed"

    assert len(storage.list_custom_rules()) == 1
    assert storage.delete_custom_rule(d.id) is True
    assert storage.get_custom_rule(d.id) is None
    assert storage.delete_custom_rule(d.id) is False


# --- /rules REST surface --------------------------------------------------------

@pytest.fixture()
def client(ot_home):
    return TestClient(app, base_url="http://localhost")


def test_list_rules_includes_builtins(client):
    r = client.get("/rules")
    assert r.status_code == 200
    body = r.json()
    assert len(body["builtin"]) >= 20
    assert body["custom"] == []


def test_disable_and_enable_builtin_rule(client):
    r = client.put("/rules/builtin/failed_file_opens", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.get("/rules")
    row = next(x for x in r.json()["builtin"] if x["id"] == "failed_file_opens")
    assert row["enabled"] is False

    r = client.put("/rules/builtin/failed_file_opens", json={"enabled": True})
    assert r.json()["enabled"] is True


def test_tune_builtin_threshold(client):
    r = client.put("/rules/builtin/slow_syscall", json={"thresholds": {"slow_syscall_ms": 2500}})
    assert r.status_code == 200
    assert r.json()["thresholds"]["slow_syscall_ms"] == 2500


def test_tune_unknown_threshold_rejected(client):
    r = client.put("/rules/builtin/slow_syscall", json={"thresholds": {"not_a_real_field": 1}})
    assert r.status_code == 400


def test_update_unknown_builtin_404s(client):
    assert client.put("/rules/builtin/does_not_exist", json={"enabled": False}).status_code == 404


def test_validate_endpoint_ok_and_rejected(client):
    r = client.post("/rules/custom/validate", json={"signal": "metrics", "expression": "cpu_pct > 1"})
    assert r.json()["ok"] is True

    r = client.post("/rules/custom/validate", json={"signal": "metrics", "expression": "().__class__"})
    assert r.json()["ok"] is False


def test_create_rejects_invalid_expression(client):
    r = client.post("/rules/custom", json={
        "name": "bad", "signal": "metrics", "expression": "().__class__",
    })
    assert r.status_code == 400


def test_create_update_delete_custom_rule_via_api(client):
    r = client.post("/rules/custom", json={
        "name": "high cpu", "signal": "metrics", "expression": "cpu_pct > 90", "duration_ms": 2000,
    })
    assert r.status_code == 200
    rid = r.json()["id"]

    r = client.get("/rules")
    assert len(r.json()["custom"]) == 1

    r = client.put(f"/rules/custom/{rid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = client.put(f"/rules/custom/{rid}", json={"expression": "().__class__"})
    assert r.status_code == 400  # re-validated on update

    r = client.delete(f"/rules/custom/{rid}")
    assert r.status_code == 200
    assert client.get("/rules").json()["custom"] == []

    assert client.delete(f"/rules/custom/{rid}").status_code == 404
