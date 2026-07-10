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


# --- request tracing (HTTP boundary + libpq DB spans) -----------------------

# Real capture shape: REQ carries the raw request head (trailing CRLF + header lines
# land on separate, ignored physical lines); RSP carries the status; SQL is one line.
SAMPLE_REQ_OUT = (
    "Attached 12 probes\n"
    "REQ 1000000000 9 100 GET /slow HTTP/1.1\r\n"
    "Host: localhost:8899\r\n"
    "\r\n"
    "SQL 1000500000 9 100 300000000 SELECT pg_sleep(0.3), 42\n"
    "RSP 1000000000 1306000000 200\n"
    "REQ 2000000000 9 101 POST /checkout HTTP/1.1\r\n"
    "RSP 2000000000 2008000000 500\n"
    "REQ 3000000000 9 102 GET /users/42?x=1 HTTP/1.1\r\n"
    "RSP 3000000000 3003000000 404\n"
)


def test_parse_bpftrace_http_pairs_req_rsp():
    spans = ebpf.parse_bpftrace_http(SAMPLE_REQ_OUT)
    assert len(spans) == 3
    by_route = {s.route: s for s in spans}
    slow = by_route["/slow"]
    assert slow.method == "GET" and slow.status == 200 and slow.kind == "http"
    assert slow.tid == 100 and slow.start_ns == 1000000000
    assert round(slow.dur_ns / 1e6) == 306          # 1306000000 - 1000000000
    # query string is stripped from the route
    assert by_route["/users/42"].method == "GET" and by_route["/users/42"].status == 404
    assert by_route["/checkout"].status == 500


def test_parse_bpftrace_http_ignores_noise_and_unpaired():
    # a REQ with no matching RSP produces no span (fail-open); header lines ignored
    out = "REQ 5 9 7 GET /x HTTP/1.1\r\nGarbage: header\nRSP 999 1000 200\n"
    assert ebpf.parse_bpftrace_http(out) == []


def test_parse_bpftrace_sql_scrubs_literals():
    spans = ebpf.parse_bpftrace_sql(SAMPLE_REQ_OUT)
    assert len(spans) == 1
    s = spans[0]
    assert s.kind == "db" and s.tid == 100 and round(s.dur_ns / 1e6) == 300
    # literals redacted (PII): 0.3 -> ?.?, 42 -> ?
    assert s.attrs["statement"] == "SELECT pg_sleep(?.?), ?"
    assert "42" not in s.name and "0.3" not in s.name


def test_scrub_sql_redacts_strings_and_numbers():
    assert ebpf._scrub_sql("SELECT * FROM t WHERE name='bob' AND id = 12") == \
        "SELECT * FROM t WHERE name='?' AND id = ?"


def test_scrub_sql_redacts_truncated_unterminated_literal():
    # the capture is a str(arg1, 192) PREFIX — a literal can be cut off with no closing
    # quote (truncation) or lose its close to an embedded newline; the raw tail must NOT
    # survive on disk (PII / secret leak — review finding).
    for raw in ("UPDATE users SET api_key = 'sk-live-abcdefSECRETsecret",       # truncated
                "INSERT INTO t (note) VALUES ('line1_SECRET"):                   # newline-split
        scrubbed = ebpf._scrub_sql(raw)
        assert "SECRET" not in scrubbed and "sk-live" not in scrubbed, scrubbed
        assert "'?'" in scrubbed


def test_parse_bpftrace_sql_truncated_literal_not_leaked():
    out = "SQL 1000 9 100 500000 UPDATE u SET token = 'ghp_realsecrettoken_truncated\n"
    spans = ebpf.parse_bpftrace_sql(out)
    assert spans and "realsecret" not in spans[0].attrs["statement"]


def test_build_request_bt_gates_sql_on_libpq():
    http_only = ebpf.build_request_bt(4242, "20", pq_lib=None)
    assert "TARGETPID" not in http_only and "/pid == 4242/" in http_only
    assert "interval:s:20" in http_only
    assert "PQsendQuery" not in http_only            # no libpq → no DB block
    with_db = ebpf.build_request_bt(4242, "20", pq_lib="/lib64/libpq.so.5")
    assert "uprobe:/lib64/libpq.so.5:PQsendQuery" in with_db
    assert "PQgetResult" in with_db and "LIBPQPATH" not in with_db


def test_request_capabilities_shape():
    caps = ebpf.request_capabilities()
    assert set(caps) == {"available", "reason", "engine"}
    assert caps["engine"] == "bpftrace"
    assert (caps["reason"] is None) == (caps["available"] is True)


# --- Phase 2: off-CPU decomposition, TLS/MySQL/SQLite, drill ------------------

def test_parse_bpftrace_offcpu_intervals_and_reasons():
    out = ("OFF 100 1000 200000000 4\n"    # reason 4 = sleep
           "RQ 100 1300 10000000\n"
           "OFF 101 5 5000000 3\n"          # 3 = lock
           "OFF 102 5 5000 9\n"             # unknown code -> other
           "some noise line\n"
           "OFF x y z 1\n")                 # non-numeric -> skipped
    ivs = ebpf.parse_bpftrace_offcpu(out)
    tagged = {(i["kind"], i.get("reason")) for i in ivs}
    assert ("off", "sleep") in tagged and ("off", "lock") in tagged and ("off", "other") in tagged
    assert ("rq", None) in tagged
    assert all("start_ns" in i and "dur_ns" in i and "tid" in i for i in ivs)
    assert not any(i["tid"] == 0 for i in ivs)   # the malformed line produced nothing


def test_parse_bpftrace_sql_tolerates_missing_statement():
    # a SQLite step with no mapped prepare-text emits SQL with no statement field
    spans = ebpf.parse_bpftrace_sql("SQL 1000 9 100 250000000\n")
    assert len(spans) == 1 and spans[0].name == "query" and spans[0].dur_ns == 250000000


def test_extract_offcpu_stacks_folds_per_tid_strips_epilogue_and_offsets():
    dump = (
        "@ostk[100, \n"
        "        perf_trace_sched_switch+20\n"       # scheduler epilogue (decimal offset)
        "        __schedule+1131\n"
        "        schedule+39\n"
        "        schedule_hrtimeout_range_clock+321\n"
        "        do_nanosleep+123\n"
        "]: 200000000\n"
        "@ostk[101, \n"
        "        perf_trace_sched_switch+20\n"
        "        futex_wait_queue+70\n"
        "        futex_do_wait+200\n"
        "]: 5000000\n"
    )
    st = ebpf.extract_offcpu_stacks(dump)
    assert set(st) == {"100", "101"}
    # decimal offsets stripped, scheduler epilogue dropped, root->leaf, value in usec
    assert st["100"] == "do_nanosleep;schedule_hrtimeout_range_clock 200000"
    assert st["101"] == "futex_do_wait;futex_wait_queue 5000"


def test_build_request_bt_offcpu_tls_dbs_and_single_end(monkeypatch):
    monkeypatch.setattr(ebpf, "_exports_symbol", lambda lib, sym: True)
    s = ebpf.build_request_bt(
        7, "8", pq_lib="/l/libpq.so.5",
        db_libs=[("mysql", "/l/libmariadb.so.3"), ("sqlite", "/l/libsqlite3.so.0")],
        ssl_lib="/l/libssl.so.3", off_cpu=True)
    assert s.count("END {") == 1                       # bpftrace rejects a 2nd END probe
    assert "clear(@offstk);" in s and "clear(@stext);" in s
    assert "sched:sched_switch" in s and "@ostk[$wp," in s   # off-CPU decomposition + drill
    assert "SSL_read_ex" in s and "SSL_write_ex" in s        # TLS (both variants "exported")
    assert "mysql_real_query" in s and "sqlite3_step" in s   # MySQL + SQLite DB spans
    assert "TARGETPID" not in s and "DBLIBPATH" not in s and "LIBSSLPATH" not in s
    # off_cpu=False, no sqlite → no sched block and no END (nothing to clear)
    s2 = ebpf.build_request_bt(7, "8", off_cpu=False)
    assert "sched:sched_switch" not in s2 and "END {" not in s2


def test_build_request_bt_tls_selects_only_exported_variants(monkeypatch):
    # a libssl exporting only the classic (non-_ex) symbols → only that block emitted
    monkeypatch.setattr(ebpf, "_exports_symbol", lambda lib, sym: sym in ("SSL_read", "SSL_write"))
    s = ebpf.build_request_bt(7, "8", ssl_lib="/l/libssl.so.1.1", off_cpu=False)
    assert "SSL_read /pid == 7/" in s and "SSL_read_ex" not in s


def test_request_spans_sqlite_roundtrip_and_event_isolation(ot_home):
    from app import runs, sessions, storage
    from app.trace import orchestrator
    sess = sessions.create(sessions.SessionCreate(display_name="Req Proj"))
    run = orchestrator.start_run(runs.RunCreate(command="sleep 1", cwd="/tmp", session_id=sess.id))
    rows = [{"timestamp_ms": 1_700_000_000_000.0, "pid": 9,
             "payload": {"method": "GET", "route": "/slow", "status": 200,
                         "dur_ms": 300.0, "db_ms": 290.0, "tid": 100, "db": []}}]
    storage.insert_request_spans(run.id, rows)
    got = storage.read_request_spans(run.id)
    assert len(got) == 1 and got[0]["route"] == "/slow"
    assert got[0]["timestamp_ms"] == 1_700_000_000_000.0 and got[0]["db_ms"] == 290.0
    # curated request spans must NOT leak into the generic events reader (syscall isolation)
    assert storage.read_events(run.id) == []
