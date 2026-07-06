"""Tests for the eBPF collectors (app.ebpf) — off-CPU + latency (Phase D).

The capability probe + parsers + rules are deterministic and tested here; the
actual eBPF captures need root, so their run path is exercised fail-open in the
manual end-to-end verification.
"""
from __future__ import annotations

from app import ebpf


SAMPLE_HIST = """
Tracing run queue latency... Hit Ctrl-C to end.

     msecs               : count     distribution
         0 -> 1          : 120      |****************************************|
         2 -> 3          : 30       |**********                              |
         4 -> 7          : 8        |**                                      |
         8 -> 15         : 2        |                                        |
       128 -> 255        : 1        |                                        |
"""


def test_parse_log2_hist_buckets_and_percentiles():
    h = ebpf.parse_log2_hist(SAMPLE_HIST)
    assert h["unit"] == "msecs" and h["total"] == 161 and len(h["buckets"]) == 5
    # cumulative: 120@1, 150@3, 158@7, 160@15, 161@255
    assert h["p50"] == 1        # 80.5th index lands in bucket 0->1
    assert h["p90"] == 3        # 144.9 -> cum 150 at bucket 2->3
    assert h["p99"] == 15       # 159.4 -> cum 160 at bucket 8->15
    assert h["max"] == 255      # last non-zero bucket


def test_parse_log2_hist_empty():
    h = ebpf.parse_log2_hist("no histogram here\n")
    assert h["total"] == 0 and h["p50"] is None and h["max"] is None


def test_latency_anomalies_thresholds():
    # run-queue p99 50ms (>= HIGH) → high; block-io p99 25ms (>= MED) → medium
    lat = {
        "runqueue": {"unit": "msecs", "p99": 50, "buckets": [], "total": 100},
        "block_io": {"unit": "msecs", "p99": 25, "buckets": [], "total": 100},
    }
    an = ebpf.latency_anomalies(lat)
    by = {a.rule_id: a.severity for a in an}
    assert by == {"high_runqueue_latency": "high", "slow_block_io": "medium"}


def test_latency_anomalies_below_threshold_is_quiet():
    lat = {"runqueue": {"unit": "msecs", "p99": 1}, "block_io": {"unit": "msecs", "p99": 3}}
    assert ebpf.latency_anomalies(lat) == []
    # an errored histogram (denied) must not raise or fire
    assert ebpf.latency_anomalies({"runqueue": {"error": "denied"}, "block_io": None}) == []


def test_capabilities_shape():
    c = ebpf.capabilities()
    assert {"available", "reason", "btf", "unprivileged_bpf_disabled", "is_root",
            "has_caps", "sudo", "tools", "use_sudo", "bpftrace"} <= set(c)
    assert {"offcputime", "runqlat", "biolatency", "biosnoop", "pythongc"} <= set(c["tools"])
    assert isinstance(c["available"], bool) and isinstance(c["bpftrace"], bool)


def test_parse_bpftrace_hist_single_and_range_buckets():
    txt = ("@usecs: \n"
           "[0]                    3 |@@@@          |\n"
           "[1]                    8 |@@@@@@@@@@@@@@|\n"
           "[2, 4)                 5 |@@@@@@@@      |\n"
           "[16, 32)               1 |@@            |\n")
    h = ebpf.parse_bpftrace_hist(txt, "usecs")
    assert h["unit"] == "usecs" and h["total"] == 17 and len(h["buckets"]) == 4
    assert h["max"] == 32  # last non-zero bucket's hi


def test_latency_anomalies_unit_normalization():
    # bpftrace run-queue is µs: 20000µs = 20ms → medium
    an = ebpf.latency_anomalies({"runqueue": {"unit": "usecs", "p99": 20000}})
    assert [a.rule_id for a in an] == ["high_runqueue_latency"]


def test_run_tool_missing_is_fail_open():
    ok, out, reason = ebpf.run_tool("definitely-not-a-tool", ["-x"], use_sudo=False, timeout=2)
    assert ok is False and out == "" and reason and "not installed" in reason


def test_tool_cmd_sudo_prefix():
    cmd = ebpf.tool_cmd("offcputime", ["-f", "-p", "5", "3"], use_sudo=True)
    if cmd is not None:  # only if bcc tools are present on this host
        assert cmd[:2] == ["sudo", "-n"] and "-f" in cmd
