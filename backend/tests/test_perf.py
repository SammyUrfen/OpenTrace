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


# --- Phase B: universal folded-stack ingest ---------------------------------

def test_fold_collapsed_self_and_total():
    text = "main;a;b 5\nmain;a;c 3\nmain;a 2\n"
    fg = perf.fold_collapsed(text)
    assert fg["supported"] and fg["samples"] == 10 and fg["unit"] == "samples"
    self_by = {h["function"]: h["self"] for h in fg["hotspots"]}
    total_by = {h["function"]: h["total"] for h in fg["hotspots"]}
    assert self_by["b"] == 5 and self_by["c"] == 3 and self_by["a"] == 2
    assert total_by["a"] == 10 and total_by["main"] == 10  # a & main are in every stack


def test_fold_collapsed_skips_malformed_and_tags_usec():
    text = "no_count_here\ngood;leaf 4\n;;; 2\nbad;x notanumber\n"
    fg = perf.fold_collapsed(text, count_is_usec=True)
    assert fg["samples"] == 4 and fg["unit"] == "usec"  # only the one valid line
    assert {h["function"] for h in fg["hotspots"]} == {"good", "leaf"}


def test_fold_speedscope_root_to_leaf_and_merges_profiles():
    doc = {
        "shared": {"frames": [{"name": "root"}, {"name": "foo"}, {"name": "bar"}]},
        "profiles": [
            {"type": "sampled", "samples": [[0, 1], [0, 1, 2]], "weights": [1, 1]},
            {"type": "sampled", "samples": [[0, 1]], "weights": [1]},  # 2nd thread
        ],
    }
    fg = perf.fold_speedscope(doc)
    assert fg["samples"] == 3
    self_by = {h["function"]: h["self"] for h in fg["hotspots"]}
    assert self_by["foo"] == 2 and self_by["bar"] == 1  # foo is a leaf twice (root->leaf order)


def test_fold_perf_script_unchanged_by_refactor():
    fg = perf.fold_perf_script(SAMPLE)
    assert fg["supported"] and fg["samples"] == 2


def test_fold_empty_capture_is_unsupported():
    # zero-sample captures must downgrade like build_flamegraph (friendly empty state)
    assert perf.fold_collapsed("")["supported"] is False
    g = perf.fold_collapsed("garbage with no count\n")
    assert g["supported"] is False and g["samples"] == 0 and "reason" in g
    ss = perf.fold_speedscope({"shared": {"frames": [{"name": "f"}]},
                               "profiles": [{"type": "sampled", "samples": [], "weights": []}]})
    assert ss["supported"] is False and ss["samples"] == 0 and "reason" in ss
    # a real capture stays supported
    assert perf.fold_collapsed("a;b 3\n")["supported"] is True


def test_fold_cpuprofile_walks_parents_and_drops_synthetic():
    doc = {
        "nodes": [
            {"id": 1, "callFrame": {"functionName": "(root)"}, "children": [2]},
            {"id": 2, "callFrame": {"functionName": "main", "url": "file:///a.js", "lineNumber": 3}, "children": [3]},
            {"id": 3, "callFrame": {"functionName": "hot", "url": "file:///a.js", "lineNumber": 9}, "children": []},
        ],
        "samples": [3, 3, 2],
        "timeDeltas": [100, 100, 100],
    }
    fg = perf.fold_cpuprofile(doc)
    assert fg["supported"] and fg["unit"] == "usec"
    top = fg["hotspots"][0]
    assert top["function"] == "hot (a.js:9)" and top["self"] == 200  # 2 samples × 100µs
    total_by = {h["function"]: h["total"] for h in fg["hotspots"]}
    assert total_by["main (a.js:3)"] >= 200  # main is hot's ancestor
    assert not any("(root)" in h["function"] for h in fg["hotspots"])  # synthetic dropped


def test_fold_cpuprofile_anonymous_and_bad_weights():
    doc = {
        "nodes": [{"id": 1, "callFrame": {"functionName": ""}, "children": []}],
        "samples": [1, 1], "timeDeltas": [-5, 0],  # non-positive → clamped to 1
    }
    fg = perf.fold_cpuprofile(doc)
    assert fg["supported"] and fg["samples"] == 2
    assert fg["hotspots"][0]["function"] == "(anonymous)"


def test_fold_phpspy_reverses_leaf_first_blocks():
    txt = "0 leaf /a.php:1\n1 mid /a.php:2\n2 root /a.php:3\n\n0 leaf /a.php:1\n1 mid /a.php:2\n2 root /a.php:3\n"
    fg = perf.fold_phpspy(txt)
    assert fg["supported"] and fg["samples"] == 2
    self_by = {h["function"]: h["self"] for h in fg["hotspots"]}
    total_by = {h["function"]: h["total"] for h in fg["hotspots"]}
    assert self_by["leaf"] == 2 and total_by["root"] == 2  # root->leaf after reversing
