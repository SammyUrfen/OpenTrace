"""Unit tests for the pure perf-script folder (app.perf.fold_perf_script).
The text mirrors real `perf script` output: a header line, then tab-indented
leaf-first frames, blank-line separated."""
from __future__ import annotations

from app import perf

SAMPLE = (
    "cpu  100 1.0:  1000 cycles:\n"
    "\taaaa work+0x1 (/bin/x)\n"
    "\tbbbb main+0x2 (/bin/x)\n"
    "\n"
    "cpu  100 2.0:  1000 cycles:\n"
    "\tcccc helper+0x3 (/bin/x)\n"
    "\taaaa work+0x10 (/bin/x)\n"
    "\tbbbb main+0x2 (/bin/x)\n"
    "\n"
)


def test_fold_counts_samples_and_builds_tree():
    fg = perf.fold_perf_script(SAMPLE)
    assert fg["supported"] is True
    assert fg["samples"] == 2
    tree = fg["tree"]
    assert tree["name"] == "all" and tree["value"] == 2
    # root -> main(2) -> work(2) -> helper(1)
    (main,) = tree["children"]
    assert main["name"] == "main" and main["value"] == 2
    (work,) = main["children"]
    assert work["name"] == "work" and work["value"] == 2
    (helper,) = work["children"]
    assert helper["name"] == "helper" and helper["value"] == 1
    assert helper["children"] == []


def test_self_and_total_hotspots():
    fg = perf.fold_perf_script(SAMPLE)
    by = {h["function"]: h for h in fg["hotspots"]}
    # leaves: work (sample 1) and helper (sample 2)
    assert by["work"]["self"] == 1
    assert by["helper"]["self"] == 1
    assert by["main"]["self"] == 0
    # totals: main in both, work in both, helper in one
    assert by["main"]["total"] == 2
    assert by["work"]["total"] == 2
    assert by["helper"]["total"] == 1
    assert by["main"]["total_pct"] == 100.0


def test_offset_is_stripped_from_symbol():
    fg = perf.fold_perf_script(SAMPLE)
    names = {h["function"] for h in fg["hotspots"]}
    assert names == {"main", "work", "helper"}  # no "+0x..." suffixes


def test_unknown_frames_and_empty_input():
    empty = perf.fold_perf_script("")
    assert empty["samples"] == 0
    text = "cpu 1 1.0: 1 cycles:\n\tdead [unknown] (/x)\n\n"
    fg = perf.fold_perf_script(text)
    assert fg["samples"] == 1
    assert fg["hotspots"][0]["function"] == "[unknown]"


def test_hot_function_anomaly_fires_for_dominant_resolved_function():
    fg = {"supported": True, "hotspots": [
        {"function": "do_sin", "self_pct": 60.0},
    ]}
    anoms = perf.perf_anomalies(fg)
    assert anoms and anoms[0].rule_id == "hot_function" and anoms[0].severity == "high"


def test_hot_function_skips_unknown_then_below_threshold():
    fg = {"supported": True, "hotspots": [
        {"function": "[unknown]", "self_pct": 47.0},  # skipped
        {"function": "foo", "self_pct": 10.0},        # top resolved, below 30%
    ]}
    assert perf.perf_anomalies(fg) == []


def test_hot_function_skips_unknown_then_flags_resolved():
    fg = {"supported": True, "hotspots": [
        {"function": "[unknown]", "self_pct": 47.0},  # skipped
        {"function": "compute", "self_pct": 35.0},    # resolved, ≥30% -> medium
    ]}
    anoms = perf.perf_anomalies(fg)
    assert anoms and anoms[0].rule_id == "hot_function" and anoms[0].severity == "medium"


def test_no_perf_anomaly_when_unsupported():
    assert perf.perf_anomalies({"supported": False}) == []
