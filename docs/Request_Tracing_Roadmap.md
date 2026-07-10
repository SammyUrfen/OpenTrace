<!--
Request/event-level attribution roadmap, researched 2026-07-06, revised after
review. Answers the "deepen the WHY" question: today OpenTrace says "something
spiked" (metrics) and "here's the hot on/off-CPU stack" (sampling) but cannot
say "endpoint POST /checkout was slow because 340ms went to a single Postgres
query." This doc is the implementation-ready plan for request-boundary + DB-query
capture and their correlation, grounded in the existing eBPF attach pipeline
(ebpf.py combined bpftrace, orchestrator._capture_ebpf, storage artifacts, the
SSE broker, and the collector_config-gated frontend tabs). See
Profiling_Roadmap.md for the sampler spine this builds on and
OpenTrace_Roadmap.md Phase E for the eBPF status.

REVISION NOTES (what the review changed): (1) MVP feasibility was overstated in
places — the libpq DB-span path and the "fold everything into the one combined
program" decision are now explicitly SPIKE-GATED, not committed. (2) A hard
correctness bug is fixed: bpftrace `nsecs` is CLOCK_MONOTONIC, so every absolute
time sink (SQLite, incident ts, timeline) needs an explicit monotonic->epoch
anchor — §2.6 + §4.1. (3) The capability gate is corrected to
bpftrace_available()+privilege (NOT caps["available"], which requires BTF+bcc).
(4) A "requests-only" run (requests=True, ebpf=False) is now scoped so it does
NOT drag in the whole off-CPU/latency suite. (5) The MVP SQLite span write is
DROPPED (no reader exists yet). (6) The BPFTRACE string-length env var name is
declared unknown-pending-spike in exactly one place.
-->

# OpenTrace Request Tracing Roadmap: Endpoint → Query Attribution by Attach

## 1. Why / the gap

OpenTrace today can answer two kinds of question about a live process:

- **"Something spiked"** — the psutil timeline + the 18+ metric/threshold rules (`orchestrator._finalize` → `anomalies`, `IncidentFeed`).
- **"Here is the hot code path"** — the on-CPU flamegraph (`perf.py::_fold_stacks`) and, with eBPF, the off-CPU (blocked-time) flamegraph + run-queue / block-I/O latency histograms (`ebpf.py`, `_capture_ebpf`).

What it **cannot** answer is the question a developer actually asks about a web service:

> **"Endpoint `POST /checkout` was slow because it spent 340ms blocked on a single Postgres query, while run-queue latency was fine and the disk was idle."**

The missing layer is **request/event-level attribution**: capture request boundaries (which HTTP endpoint), capture DB/query spans (which query, how long), and *tie those to the off-CPU stacks and scheduler/block-IO latency we already collect* — so a slow endpoint decomposes into "DB-wait vs CPU-starvation vs disk vs lock." Everything must stay **attach-only** (no target changes), **fail-open**, and **bounded-window**, and be **bpftrace-first** because of the kernel-7.0 constraints below.

**Target capability (MVP):** attach to a plaintext-HTTP/1.x server and, for the bounded window, produce a per-endpoint RED table (Rate / Errors / Duration) plus a `slow_endpoint` finding that agrees between Overview and the Incident feed. The **HTTP-boundary half of the MVP is high-confidence** (syscall tracepoints, well-trodden). The **DB-query (libpq) half is spike-gated** — see §5 and §8, OQ#2 — because the concrete uprobe primitives have not yet been exercised on this box; if the spike fails, the MVP still ships as endpoint RED + wall-time-only attribution with an honest `db spans unavailable` reason.

---

## 2. Design constraints (restate the hard-won gotchas)

These are non-negotiable and already baked into `ebpf.py`. The implementer must not relearn them.

1. **Attach without modifying the target.** Request boundaries come from kernel-side syscall tracepoints and (Phase 2) uprobes/uretprobes on the target's *own* mapped `libssl`/`libpq` — never an injected APM SDK, no restart, no source change. Runtime-native inspector hooks (Node SIGUSR1→CDP) are acceptable only because they need no source change; **do NOT adopt any context-propagation-by-injection** (Beyla `sk_msg`/TC traceparent, Odigos/OBI `bpf_probe_write_user`) — those write into the target's traffic/memory and violate the ethos. DeepFlow-style thread/time correlation needs no writes and is the ethos-compatible choice.

2. **Fail-open everywhere.** A missing `libpq`, a static-libpq wheel, an absent privilege, a non-HTTP target, or an unsupported runtime must complete the run with whatever signal we got plus a friendly `reason` string. A run with no HTTP/DB signal must look **exactly like today** — the request layer is strictly additive. Mirror `http_latency`'s `{available:false, reason}` stub (runs.py:601-609) and `_ensure_flamegraph_reason`.

3. **Bounded window.** The tracer takes the same `window_s` and the same `ctx.stop_event` as the sampler + eBPF capture, self-terminates via the trailing `interval:s:{n} { exit(); }`, and is joined before `end_run`/`_finalize`. In monitor mode it repeats bounded snapshots. No always-on agent.

4. **bpftrace-first on kernel 7.0.** On this box bcc's **bundled headers fail to compile most bcc tools** (`runqlat`/`biolatency`/`biosnoop`/`pythongc` hit the `struct filename` static_assert wall); only `offcputime` survives on bcc. **All new probes are hand-written bpftrace CO-RE, which dodges that wall** (the wall only bit bcc *tool sources* doing kernel-struct CO-RE; syscall tracepoints + userspace uprobes read no kernel structs). Specifically:
   - **NEVER `-p PID`.** `run_bpftrace`'s `-p` silently applies an implicit pid filter to **all** probes, killing the system-wide sched/block tracepoints (ebpf.py:433-435, and the combined call passes no pid at orchestrator.py:481-482). Scope every new *system-wide-tracepoint* block with an in-script `/pid == {pid}/` filter, the `_BT_GC` idiom (ebpf.py:436-440). (Pure-uprobe programs are library-scoped and *could* take `-p`, but the MVP HTTP block is tracepoint-based, so the no-`-p` rule dominates — see §3.1 for the combined-vs-dedicated decision this feeds.)
   - **Temp files, never pipes.** `_run_proc` captures stdout+stderr to `tempfile.TemporaryFile` (ebpf.py:159-200). A verbose per-event stream on a busy socket would fill a 64KB PIPE and deadlock the child. This rule is load-bearing for high-rate request output.
   - **`.format()` brace-doubling.** `_BT_RUNQ`/`_BT_GC` are `.format()`-ed so every literal bpftrace brace is doubled (`{{ }}`); `_BT_BIO` is single-brace only because it is never formatted. New `_BT_HTTP`/`_BT_SQL` take `{ssl}`/`{pq}`/`{pid}` via `.format`, so every literal brace **must** be doubled or the program won't assemble.
   - **CLOCK alignment (monotonic).** Spans are emitted in bpftrace `nsecs` (CLOCK_MONOTONIC) — the same clock as `offcputime`/`_BT_RUNQ`, so the Phase-2 off-CPU join aligns with no conversion. But CLOCK_MONOTONIC is **not** epoch time; every absolute-time sink needs the explicit anchor of §2.6.
   - **Privilege.** eBPF needs root / `CAP_BPF`+`CAP_PERFMON` / the configured passwordless `sudo` for `/usr/bin/bpftrace`. `unprivileged_bpf_disabled=0` does **not** suffice for tracing programs.

5. **Kernel-side string caps.** bpftrace truncates `str()` at a build-dependent limit (default ~64B) — too short for `POST /checkout HTTP/1.1` + SQL. Raising it needs a subprocess `env` override. **The exact env-var name is NOT settled** — recent bpftrace uses `BPFTRACE_MAX_STRLEN`; older builds used `BPFTRACE_STRLEN`; and inline `str(ptr, N)` may or may not be supported on this box's build. This is the single place the doc names it, and it is explicitly **spike-gated** (§8 OQ#2): the spike confirms the name, the max value, and inline support before any template hardcodes it. Everywhere else in this doc, "raise the string cap" refers back to this note.

6. **CLOCK_MONOTONIC → epoch-ms conversion (mandatory; new).** bpftrace `nsecs` is CLOCK_MONOTONIC nanoseconds, **not** Unix epoch. Every existing bpftrace parser (`parse_bpftrace_gc`, ebpf.py:356-368) deliberately emits a *relative* timeline (seconds since the first event) precisely to sidestep this. Request spans are the first bpftrace data OpenTrace routes into **absolute** stores (the incident `ts`, and any timeline overlay), so we must convert:
   - When `_capture_ebpf` launches the bpftrace child, capture an anchor pair **once**, back-to-back:
     `mono0 = time.clock_gettime(time.CLOCK_MONOTONIC)` and `wall0 = time.time()`.
   - Convert any span monotonic timestamp to epoch ms with
     `epoch_ms(start_ns) = wall0 * 1000.0 + (start_ns * 1e-9 - mono0) * 1000.0`.
   - Keep the raw `start_ns`/`dur_ns` (monotonic) on the `Span` for the Phase-2 off-CPU interval join (same clock as `offcputime`); compute `timestamp_ms` (epoch) via the anchor **only** at the point a span enters an absolute sink.
   - Absolute sinks that MUST use this: (a) the `_make_incident(...)` `ts` for `slow_endpoint` — orchestrator.py:554 feeds `ctx.samples[-1].timestamp_ms`, which is epoch ms, so a monotonic value would land incidents decades in the past; (b) any future timeline overlay of spans onto the metric lanes (Phase 3, §6.4). The anchor travels with the parsed span batch (a small `(mono0, wall0)` tuple passed into the parser), so the correlator never sees a bare monotonic value in an absolute context.

---

## 3. Architecture

### 3.1 The pipeline (and the one-program-vs-dedicated-program OPEN decision)

The original draft asserted that folding the HTTP/SQL blocks into the existing combined bpftrace was "the whole point" and carried "no blast radius." **The review correctly flags this as an open decision, not a settled one.** Two things are unresolved and gate the pipeline shape:

- **System-wide firing cost (§8 OQ#1, must-spike).** The combined program runs **without** `-p` and scopes with an in-script `/pid==PID/` filter. That filter gates the *action/output*, not the *probe firing*: adding `sys_enter/exit_read/write/sendto/recvfrom` means the BPF handler fires on **every** such syscall on the **whole box**, a materially higher firing rate than today's per-context-switch / per-block-IO tracepoints. On a busy box this can be a real CPU tax even when almost every hit is filtered out. This must be **measured** before committing to the fold.
- **Combined-program blast radius (§8 OQ, must-spike).** Adding more probes to the single CO-RE program may slow/enlarge its one compile or drop probes on kernel 7.0.

**Consequently the primary plan is a DEDICATED, still-single request-tracing bpftrace program** (one compile, run alongside — never concurrent with — the existing combined latency/GC program, exactly as `offcputime` bcc already runs alongside). Folding into `build_combined_bt` is the *optimization to attempt only if the spike shows the firing cost and blast radius are both acceptable*. Either way it is **one request-tracing compile**, never N concurrent CO-RE compiles. The diagram below shows the dedicated-program shape (the fold variant simply moves the HTTP/SQL blocks into `build_combined_bt`):

```
                    ┌──────── existing combined bpftrace (build_combined_bt, unchanged for requests-only) ────────┐
attach window_s ──► │  _BT_RUNQ (sched) │ _BT_BIO (block) │ [_BT_GC]        (spawned ONLY when collector.ebpf)     │
                    └────────────────────────────────────────────────────────────────────────────────────────────┘
                    ┌──────── dedicated request bpftrace (build_request_bt, spawned when collector.requests) ──────┐
                    │  [_BT_HTTP syscall tp, /pid==PID/]  │  [_BT_SQL libpq uprobe — SPIKE-GATED]                   │
                    └───────────────────────────────────────────────────────────────┬────────────────────────────┘
   anchor (mono0,wall0) captured at child launch  ──────────────────────────────────┤ temp-file stdout (SPAN/SQL lines)
                                                                                     ▼
                       parse_bpftrace_http / parse_bpftrace_sql (clone parse_bpftrace_gc, carry the anchor)
                                                                                     │
                                     pure-Python correlator (tid + nested time-window join)
                                     nest each SQL span under the HTTP span on the same tid  →  endpoint_stats()
                                                                                     │
                    ┌────────────────────────────────────────────────────────────────┼──────────────┐
                    ▼ full stream                    ▼ rollup (atomic)                              ▼ live
        requests.ndjson.zst              requests.json + record_artifact                broker.publish 'request_rollup'
        (write_ndjson_zst)               (write_json, temp+os.replace)                  (throttled)
```

Note there is **no MVP SQLite span write** — see §3.5 (dropped, no reader exists).

### 3.2 Request-boundary capture — plaintext HTTP/1.x (MVP, high confidence)

A new module-level template `_BT_HTTP` in `ebpf.py`, appended by `build_request_bt` (or `build_combined_bt` in the fold variant), scoped by `/pid == {pid}/`. Mechanism (validated against Pixie/Beyla/Coroot):

- **Entry/exit buffer asymmetry is the #1 trap.** On `read`/`recv` the buffer is empty at `sys_enter` and only filled at `sys_exit` — stash the pointer on `tracepoint:syscalls:sys_enter_read` (`@rbuf[tid]=args.buf; @rfd[tid]=args.fd`) and read it on `sys_exit_read` when `args.ret>0` via `str(@rbuf[tid], args.ret)`. On `write`/`send` the buffer is valid at `sys_enter` — read directly.
- **Per-tid single-slot stash race (gap, must call out).** The `@rbuf[tid]`/`@rfd[tid]` stash holds exactly one in-flight read per thread. A single tid doing `readv`/`writev`, or a second `read` on a different fd between the request read and its `sys_exit`, or keep-alive interleaving, will **clobber** the slot and mis-pair the buffer with the wrong fd/length. The MVP mitigations: (a) only stash on server fds seeded from `sys_exit_accept4` and clear the slot on `sys_exit_read`, narrowing the window; (b) treat this stash race as part of the request/response **state-machine spike** (§8 OQ#4), not a solved detail — the pairing state machine and the read-buffer stash must be designed together. Key by `(tid, fd)` for the stash if the spike shows single-`tid` clobbering in practice.
- **fd role:** seed inbound server fds from `sys_exit_accept4` (retval = server-side fd) vs outbound from `sys_enter_connect`; tear down on `sys_enter_close`.
- **Request line:** match `^(GET|POST|PUT|DELETE|PATCH|HEAD) ` on the first bytes of a server read → derive `method`, `path`. **Status:** parse the response first line `HTTP/1.1 <code>`.
- **Pair request→response** on `(pid,fd)`: `@inflight[pid,fd] = (nsecs, method, path)` on the request read; on the response write compute `dur = nsecs - t0` and emit `printf("SPAN %d %d %d %llu %llu %d %s %s\n", pid, tid, fd, t0, nsecs, status, method, path)`, then `delete`. The `tid` of the read/write **is the worker thread** = the join key.
- **String cap:** covering the request line + status line needs the raised string cap of §2.5 (spike-gated name/size); long URLs truncate — acceptable for attribution.

**Verify before writing (spike):** `bpftrace -lv tracepoint:syscalls:sys_enter_read/_exit_read/_enter_write` on kernel 7.0 to confirm field names (`fd`/`buf`/`count`, `ret` on exit; `recvfrom` uses `ubuf`).

### 3.3 DB-query capture — libpq uprobes (SPIKE-GATED, not committed MVP)

**Feasibility downgrade (overstatement fix).** On this box, only USDT `gc__start` + sched/block tracepoints are *demonstrably* working. A uprobe on a mapped `.so` by absolute path, a `str(argN)` C-string read, multi-symbol comma-attach, and the configured NOPASSWD sudo for uprobe attach have **never been exercised here**. The original matrix/plan presented libpq DB spans as committed HIGH-confidence MVP scope; that is not warranted. **DB spans are gated behind the §8 OQ#2 spike.** If the spike passes, they land in the MVP; if not, the MVP ships without them (endpoint RED + wall-time attribution only, `db spans unavailable` reason) and DB spans move to Phase 2.

Design (pending the spike). A new `_BT_SQL` template, uprobe/uretprobe on the target's mapped `libpq.so.5`, resolved by a new `libpq_path(pid)` helper (see §3.4 for the multi-match caveat). Symbols + arg holding the SQL (bpftrace `argN` is 0-based; `str(argN)` reads the C string):

| Symbol | SQL text | Timing |
|---|---|---|
| `PQexec(PGconn*, const char *command)` | `str(arg1)` | synchronous+blocking → span = entry→uretprobe |
| `PQexecParams(PGconn*, const char *command, int nParams, ...)` | template `str(arg1)` ($1/$2, **not** the separate `paramValues=arg4` bound values) | same |
| `PQprepare` / `PQexecPrepared` | prepare carries text (`str(arg2)`); prepared-exec carries only `stmtName` → join on name (out of MVP) | — |

`_BT_SQL` skeleton (braces shown singly for readability — **double them** in the `.format` template). **Every primitive in this skeleton is what OQ#2 must confirm** (absolute-path uprobe, `str(argN)`, comma-attach):

```
uprobe:{pq}:PQexec,uprobe:{pq}:PQexecParams /pid == {pid}/ { @q[tid]=str(arg1); @st[tid]=nsecs; }
uretprobe:{pq}:PQexec,uretprobe:{pq}:PQexecParams /@st[tid] != 0/ {
  printf("SQL %d %llu %llu %s\n", tid, @st[tid], nsecs - @st[tid], @q[tid]);
  delete(@q[tid]); delete(@st[tid]);
}
```

The `@q[tid]`/`@st[tid]` single-slot stash has the same per-tid clobber exposure as §3.2; for synchronous blocking `PQexec`/`PQexecParams` there is exactly one in-flight query per tid, so it is safe — but the async `PQsendQuery`→`PQgetResult` path (out of MVP) would clobber and is explicitly deferred.

**Preflight (fail-open):** `readelf --dyn-syms` on the mapped `libpq` to confirm `PQexec` is exported. `psycopg2-binary` wheels statically bundle libpq with hidden symbols (uprobe won't attach); `asyncpg`/`pgx`/pure-Python speak the wire directly with no libpq. In every such case emit `{available:false, reason}` and never raise.

### 3.4 Extending `ebpf.py` and `_capture_ebpf`

- **`build_request_bt(pid, n, pq_lib=None)`** (new; or widen `build_combined_bt` in the fold variant): assemble the `_BT_HTTP` block (always, for request capture) + `_BT_SQL` block (only if `pq_lib` and the OQ#2 spike passed) + the trailing `interval:s:{n} { exit(); }`.
- **New parsers `parse_bpftrace_http(text, anchor)` / `parse_bpftrace_sql(text, anchor)`** modeled on `parse_bpftrace_gc` (ebpf.py:356-368): match the prefix token (`SPAN`/`SQL`), parse numeric fields + monotonic `nsecs`, `[-N:]` cap. **They take the `(mono0, wall0)` anchor** (§2.6) so they can attach an epoch `timestamp_ms` to each span while retaining the raw monotonic `start_ns`/`dur_ns`.
- **New resolver `libpq_path(pid)` (and Phase-2 `libssl_path`).** Clone `libpython_path` (ebpf.py:315-332) but **do not** inherit its first-match-and-break behavior (gap). `libpython_path` breaks on the first `libpython3` hit because a process maps one libpython; a process can map **multiple** libpq/TLS libraries/versions. `libpq_path` should collect all `libpq`-matching mapped objects and return the one whose `readelf --dyn-syms` preflight actually exports `PQexec` (skip static/hidden-symbol bundles); `libssl_path` (Phase 2) must likewise handle multiple mapped TLS libs rather than a blind substring-swap.
- **Harness change (string cap):** thread an optional `env` dict through `run_bpftrace` (ebpf.py:453) into `_run_proc`'s `Popen` (ebpf.py:177) as `env=os.environ | {<STRLEN_VAR>:'256'}`, where `<STRLEN_VAR>` is resolved by the §2.5 spike (name + max value + whether inline `str(ptr,N)` is preferable). Keep the temp-file capture + SIGINT/duration bounding untouched.
- **Capability gate (must-fix, corrected).** Do **NOT** gate request capture on `caps["available"]`. `ebpf.capabilities()` computes `available = bool(btf and tools_ok and priv_ok)` (ebpf.py:123) — it requires kernel **BTF** and installed **bcc tools**. Request capture (syscall tracepoints + libpq uprobes) needs **neither**: that is the entire thesis of this feature ("uprobes/tracepoints dodge the wall"). Gating on `caps["available"]` fails **closed** on exactly the bpftrace-capable-but-no-BTF-or-bcc boxes where request capture still works — a fail-open violation. The correct gate is **`ebpf.bpftrace_available()` + privilege** (the same `priv_ok`/`use_sudo` path `_capture_ebpf` already computes). Expose it as its own capability (§3.8) rather than piggybacking on the eBPF-suite gate.
- **In `_capture_ebpf` (orchestrator.py:435-556):** if request capture is on, capture the §2.6 anchor at child launch, resolve `pq_lib = ebpf_mod.libpq_path(pid)` cheaply, build+run the (dedicated) request program, parse with the new parsers, run the correlator, write the artifacts (§3.5). Crucially, **the eBPF-suite captures stay independently gated** — see §3.6.

### 3.5 Where spans are stored (dual-write; SQLite curated write DROPPED for the MVP)

Mirroring how `_capture_ebpf` already emits `latency.json` (orchestrator.py:523-524) and `gc-timeline.json` (:543-544):

- **(a) FULL span stream → `<run>/requests.ndjson.zst`** via `storage.write_ndjson_zst` (storage.py:55) — identical on-disk record to `events`/`metrics`.
- **(b) ROLLUP → `<run>/requests.json`** via `storage.write_json` (storage.py:277, **atomic** temp+`os.replace`, so a concurrent GET never reads a torn file mid-monitor-rewrite) + `storage.record_artifact(run.id, 'requests', ...)` (storage.py:229).
- **(c) ~~CURATED slow/errored spans → SQLite `events`~~ — DROPPED for the MVP (housekeeping must-fix).** The original plan wrote curated spans into the SQLite `events` table with `event_type='request'`. **Nothing in the MVP reads them back:** `GET /runs/{rid}/requests` returns the `requests.json` rollup (not SQLite); `syscall_stats`/`io_stats` filter to `event_type=='syscall'` and ignore `'request'`. The SQLite write would be dead weight, plus it needs the epoch conversion of §2.6 to populate `events.timestamp_ms` correctly. **So the MVP does not write spans to SQLite.** The curated-SQLite write returns in Phase 2 **together with its reader** — a timeline overlay / incident-evidence linkage that actually queries `event_type='request'` rows by `(run_id, timestamp_ms)`; at that point the epoch `timestamp_ms` (§2.6) and a curation policy (§4.4) are both required and specified together.

**Critical isolation (still holds):** request spans do **not** flow into `_finalize`'s `events`/`syscall_events` lists, so `syscall_stats`/`_syscall_rate_by_sample`/`_summary` (which discriminate on `event_type == 'syscall'`/`SIGNAL`/`EXIT`, e.g. aggregate.py:46) stay untouched and `syscall_rate` isn't inflated. (The `run_views` table this section once referenced has since been removed as dead code; `runs.ui_state_json` remains the only future UI-state hook — spans never land there either.)

### 3.6 Scoping a "requests-only" run (must-fix): what actually runs

The original `_start_ebpf` gate of `(ebpf OR requests)` (orchestrator.py:428 currently returns `None` unless `collector_config['ebpf']`) would force-enable the **entire** eBPF suite for a run that opted into `requests` but not `ebpf`: `offcputime` (orchestrator.py:474), `biosnoop` (:475), the runq/bio latency histograms, the `latency.json`/`offcpu-flamegraph.json` artifacts, and — for monitor runs — `latency_anomalies` → live latency incidents (:549-556). A user who wanted request attribution would silently get off-CPU/latency artifacts and latency incidents they never asked for.

**Resolution: individually gate each capture, don't co-enable the suite.**

- `_start_ebpf` may spawn the capture *thread* when `(ebpf OR requests)`, but inside `_capture_ebpf` each capture is gated on its **own** flag:
  - `offcputime`, `biosnoop`, the runq/bio latency histograms, `latency.json` + `offcpu-flamegraph.json` artifacts, and the monitor `latency_anomalies`→`_make_incident` block: **gated strictly on `collectors.get("ebpf")`**, unchanged from today.
  - the request program (`_BT_HTTP` [+ spike-gated `_BT_SQL`]), `requests.ndjson.zst`/`requests.json`, and the monitor `slow_endpoint` incidents: **gated on `collectors.get("requests")`**.
- Because the primary plan uses a **dedicated** request bpftrace program (§3.1), a requests-only run runs **only** that program (HTTP [+SQL] blocks + `interval` exit) — it never builds or runs the combined latency/GC program, so none of the off-CPU/latency artifacts appear. In the fold variant (only if the §8 spikes green-light it), `build_combined_bt` must conditionally emit the runq/bio/GC blocks *only when `ebpf`* and the HTTP/SQL blocks *only when `requests`*, so a requests-only run still produces no latency histograms.
- A run with both `ebpf` and `requests` gets both, with the Phase-2 off-CPU↔span join available.

This preserves fail-open (a requests-only run with no bpftrace/privilege completes with the psutil timeline + a request `reason` stub) and keeps each collector's artifacts scoped to its own opt-in.

### 3.7 Correlation (pure Python)

A new `aggregate.endpoint_stats(spans) -> list[dict]` copying `syscall_stats`' shape (aggregate.py:34) and reusing `aggregate._percentile` (aggregate.py:22) for p50/p95/p99 per `(method, route)` + `count` + `err_pct` + `db_ms_share`. The correlator (runs in `_capture_ebpf` per snapshot / `_finalize` single-shot) nests each `db`-kind span under the `http` span with the **same tid** whose `[start_ns, end_ns]` window **contains** it (thread-per-request join, on the raw monotonic timestamps). That is the literal "POST /checkout, 340ms, 300ms in one Postgres query."

`requests.json` shape mirrors the `latency.json` contract:
```json
{"available": true, "reason": null, "window_s": 20, "engine": "bpftrace",
 "endpoints": [{"method":"POST","route":"/checkout","count":42,
                "p50_ms":120,"p95_ms":340,"p99_ms":410,"err_pct":0.0,
                "db_ms_share":0.88}],
 "spans": [ /* sampled slow-request waterfall rows */ ]}
```

### 3.8 Streaming (no new SSE route)

Ride `broker.publish` + `sse_response('*')` (streaming.py:45, :100). The broker's bounded `_MAX_QUEUE=1000` drops oldest on Full, so **one SSE per request would starve metrics/incidents.** Publish:
- **`request_rollup`** THROTTLED per snapshot (like `_INCIDENT_UPDATE_MS`, orchestrator.py:605).
- **`request_span`** only for slow/errored spans.

Frontend `useOpenTrace.ts` gains an `else if (type === 'request_rollup' || type === 'request_span')` branch into a new `requestsByRun: Record<runId, ...>` (copy the incidents `Record` pattern).

### 3.9 Gating (collector_config)

- `AttachRequest.requests: bool = False` (runs.py:263-269) → `start_attach_run` stashes `collector_config['requests']=True` (orchestrator.py:222-226).
- `_start_ebpf` (orchestrator.py:425-432) spawns the capture thread on `(ebpf OR requests)`; the **per-capture** gating of §3.6 keeps a requests-only run from producing eBPF-suite artifacts.
- `GET /runs/attach/request-capabilities` (new) reports **`bpftrace_available()` + privilege + libpq-mappable** — **NOT** the `caps["available"]`/BTF/bcc gate (§3.4). This is the picker gate.
- Frontend `runViews()` pushes `{key:'requests'}` only when `c.requests` — the tab never appears for a run that didn't opt in.

### 3.10 Findings (preserve the monitor invariant)

- **Monitor runs:** slow endpoints become incidents ONLY through `_make_incident(ctx, 'slow_endpoint', sev, title, ts)` (orchestrator.py:608, collapse-by-rule) — exactly like the latency block at orchestrator.py:549-556 — so `_incidents_to_anomalies` keeps Overview == Incidents. **The `ts` MUST be an epoch value from §2.6** (`epoch_ms(span.start_ns)` via the anchor, or `ctx.samples[-1].timestamp_ms`), never a raw monotonic `start_ns`.
- **Single-shot runs:** a `reqtrace_anomalies(requests_dict) -> [Anomaly]` pass in `_finalize`, guarded `and not monitor`, mirroring the ebpf-latency guard at **orchestrator.py:957** (`if collectors.get("ebpf", False) and not monitor:`) — but guarded on `collectors.get("requests")`. Adding it to monitor runs too would duplicate findings and desync Overview from the feed.

---

## 4. Data model

### 4.1 The `Span` shape (`backend/app/trace/events.py`)

Add `REQUEST = "request"` to the event_type constants (alongside `SYSCALL`/`SIGNAL`/`EXIT`/`PROCESS`/`LIBCALL`, events.py:13-17) and a slotted `Span` dataclass:

| Field | Type | Notes |
|---|---|---|
| `span_id` | `str` | monotonic within the run (run_id IS the trace scope — **no 128-bit trace_id, no W3C context**) |
| `parent_id` | `str \| None` | `None` = root HTTP span; this alone drives the waterfall |
| `tid` | `int` | **the join key** (promoted to a field, not buried in attrs) |
| `pid` | `int` | |
| `kind` | `'http' \| 'db' \| 'other'` | collapses OTel's 5 SpanKinds to 3 |
| `name` | `str` | rollup key, e.g. `'POST /checkout'` or `'query pg'` |
| `method` | `str \| None` | |
| `route` | `str \| None` | |
| `status` | `int \| None` | folds Status + http.status_code + errno into one int |
| `start_ns` | `u64` | **CLOCK_MONOTONIC** = bpftrace `nsecs` — **must** match offcputime/`_BT_RUNQ` for the Phase-2 off-CPU join |
| `dur_ns` | `u64` | monotonic duration |
| `attrs` | `dict` | `db.statement` prefix, `peer`, `err` |

**Timestamp methods (correctness fix).** `start_ns`/`dur_ns` are **monotonic** and are used verbatim for the off-CPU/runq interval join (same clock). `timestamp_ms` is **NOT** `start_ns/1e6` — that would be monotonic ms, which the SQLite `events.timestamp_ms` (REAL) and the incident/timeline layer (epoch ms) would misread by decades. Instead `timestamp_ms` is computed from the per-batch anchor of §2.6:
`timestamp_ms = wall0*1000 + (start_ns*1e-9 - mono0)*1000`.
So the `Span` either stores the resolved epoch `timestamp_ms` at parse time (parser has the anchor) or exposes a method `epoch_ms(anchor)`; a bare `start_ns/1e6` must never reach an absolute sink. `to_ndjson()` records the raw monotonic `start_ns`/`dur_ns` (self-consistent within the run); `to_payload()` (only used by the deferred Phase-2 SQLite write) uses the epoch `timestamp_ms`.

### 4.2 Storage (see §3.5)

Full stream → `requests.ndjson.zst`; rollup → `requests.json` (atomic) + `record_artifact`. **No SQLite span write and no migration for the MVP** — the curated-SQLite path (with epoch `timestamp_ms` and a curation policy) returns in Phase 2 alongside its reader.

### 4.3 Per-endpoint rollup (`aggregate.endpoint_stats`)

Per `(method, route)`: `count`, `p50/p95/p99_ms` (reuse `_percentile`), `err_pct` (status ≥ 500 or errno), `db_ms_share` (Σ child db-span dur / Σ http-span dur). **DB time is reported as a labelled OVERLAY on off-CPU-network, never a 4th additive bucket** — the thread is off-CPU in `sock_recvmsg` *while* the query runs; adding them would exceed wall time.

### 4.4 Curation policy (Phase 2, deferred with the SQLite write)

Because the MVP has no SQLite span write (§3.5), the curation policy is deferred too. When the Phase-2 curated write + reader land, a `_SLOW_MS` analog and a per-run `_MAX_CURATED` cap decide which spans persist. Default proposal: slow = dur ≥ p95 OR status ≥ 500; cap = 500 spans/run (`[-N:]` like the GC/incident code). Until then, the full stream lives only in `requests.ndjson.zst`.

---

## 5. Per-runtime feasibility matrix

Confidence is now explicit; **DB-via-libpq is SPIKE-GATED, not committed MVP** (§3.3, §8 OQ#2).

| Runtime | HTTP signal (mechanism) | DB signal (mechanism) | Phase |
|---|---|---|---|
| **Python (CPython, plaintext HTTP, psycopg2/libpq)** | syscall tracepoints `sys_enter/exit_read+write`, request-line parse (attach-only, no libssl) — **HIGH** | `libpq` `PQexec`/`PQexecParams` uprobe — **SPIKE-GATED (OQ#2)**; not committed until the uprobe/`str(argN)`/comma-attach primitives are proven on this box | **MVP** (HTTP) / **MVP-if-spike-passes** (DB) |
| **Native C/C++/Rust server + libpq** | same syscall-tracepoint plaintext HTTP boundary — **HIGH** | `libpq` uprobe — **SPIKE-GATED (OQ#2)** | **MVP** (HTTP) / **MVP-if-spike** (DB) |
| **Python/native over TLS** | `libssl` `SSL_write`(entry)/`SSL_read`(uretprobe) uprobes; `libssl_path(pid)` (multi-lib aware, §3.4) | `libpq` uprobe (driver plaintext) | Phase 2 |
| **MySQL/MariaDB clients** (mysqlclient/mysql2/PHP mysqli) | syscall-tracepoint plaintext HTTP | `libmysqlclient`/`libmariadb` `mysql_real_query` uprobe (arg1=stmt, arg2=len) | Phase 2 |
| **Ruby / PHP** (plaintext HTTP + libpq/libmysqlclient/libsqlite3) | syscall-tracepoint plaintext HTTP | client uprobe incl. `libsqlite3` `sqlite3_step` joined to `sqlite3_prepare_v2` | Phase 2 |
| **.NET (ASP.NET Core)** | `dotnet-trace` EventPipe `RequestStart(3)`/`RequestStop(4)` → **true** endpoint + latency (attach-native, not bpftrace) | `System.Data.*` EventSource / `SocketsHttpHandler` timing | later |
| **Node/Deno/Bun (async event loop)** | reuse `node_cdp` SIGUSR1→CDP for handler-stack labeling (Node only) | wire-protocol or driver uprobe; tid-join breaks (event loop multiplexes) | later |
| **JVM (thread-per-request)** | `jcmd`/JFR `jdk.ExecutionSample` or async-profiler wall via `jattach` | JFR `jdk.SocketRead`/`jdk.JavaMonitorEnter` carrying handler stack | later |
| **Go / Rust static-TLS / pure-wire drivers** (pgx, asyncpg, PyMySQL, JDBC pools) | connection-level RED only; degrade with reason | `SSL_read`/`SSL_write` + Postgres `'Q'`/MySQL `COM_QUERY` wire parse (libbpf ringbuf, **not** bpftrace) | later |

**Notes.** HTTP/2 + gRPC paths are NOT recoverable from the byte stream via mid-stream attach — HPACK is a stateful codec whose dynamic table was built by frames we never saw; degrade to connection-level RED with an honest reason. Never point a bpftrace `uretprobe` at a Go binary (goroutine stack moves corrupt the return address → target panic — a hard ethos violation).

---

## 6. UI

### 6.1 The Requests tab

- **Registration:** `RunView.tsx::runViews()` push `{key:'requests', label:'Requests'}` gated on `c.requests`, placed after Incidents / before Timeline; dispatch `activeView === 'requests' && <RequestsTab backendUrl runId offCpu={!!c.ebpf}/>` (mirror the `FlamegraphTab`/`LatencyTab` branch). Backend MUST emit `collector_config.requests=true` — optional flags are strict.
- **RequestsTab** root `<div className="overview" data-testid="requests-tab">`; fetch via `useRunObject<Requests>(backendUrl, runId, 'requests')` → `GET /runs/{id}/requests`. Fail-open to `data.reason` in an `overview__muted` empty state like `LatencyTab`. When `db_ms_share` is absent (DB spike didn't ship / static libpq), the RED table still renders; only the DB overlay column shows `—`.
- **Endpoint RED table** reusing `className="syscall-table"` (`data-testid="endpoint-table"`), rows `data-testid="endpoint-row"` `data-route=...`, columns `method+route | count | p50 | p95 | p99 | err% | %time`, default sort p95 desc (copy `SyscallTable` sortable headers). The `%time` cell is a thin inline stacked bar.
- **Per-endpoint breakdown bar** (expand a row, `data-testid="endpoint-breakdown"`): a 100%-stacked horizontal bar; MVP renders just the DB-vs-wall split (or wall-only if no DB spans), reusing `.lat-row`/`.lat-row__bar`/`.lat-row__fill` + `.lat-pcts` chips. The full on-CPU/off-CPU decomposition is Phase 2.

### 6.2 Waterfall + drill (Phase 2)

`RequestWaterfall.tsx` borrows `TimelineChart`'s `x(t)`/`invX`/`onWheel`-zoom/drag-pan math but swaps the 5 fixed lanes for a depth-indexed row layout (like `FlamegraphTab.layout()`); each span is a `<rect x={x(s.t0)} width={x(s.t1)-x(s.t0)}>` (`data-testid="span-bar"` `data-span-kind`). Click → a `.lat-card` detail panel (SQL/peer/tid/self-vs-total). Drill: extend `FlamegraphTab` with a `spanFilter` prop → fetch `offcpu-flamegraph?tid&from&to` + a dismissible `data-testid="flame-span-filter"` chip. **Backend prerequisite:** `perf._fold_stacks` currently collapses per-sample `tid`+`ts`; the drill needs the folder to retain+filter them first — until then, fail open to the unfiltered off-CPU flame.

### 6.3 Incident deep-link (Phase 2)

Extend the `Incident` type with `requests: [{req_id, method, route, dur_ms, top_segment}]` (correlate in `_make_incident` alongside the hot-stack capture, using epoch `timestamp_ms` from §2.6 to overlap the incident window); render `.incident__requests` with a `data-testid="incident-request-link"` button that switches `activeView` to `'requests'` and opens the offending endpoint row. Also add `data-testid="incident-feed"` to the `.incidents` root (12-monitor.js already probes for it).

### 6.4 e2e data-testid hooks (stable for the 165-scenario harness)

`requests-tab`, `endpoint-table`, `endpoint-row` (`data-route`), `endpoint-breakdown`, `request-waterfall`, `span-bar` (`data-span-kind`), `flame-span-filter`, `incident-request-link`. MVP scenario mirrors **13-ebpf.js** flag-gated presence: assert `.secondary-tab {hasText:/Requests/i}` count > 0 only when the run opted in, then click + `waitFor('[data-testid="requests-tab"]')`; use the **15-analytics.js** `tab(ctx,label,testid)` helper.

---

## 7. Phased plan

### Phase 1 — MVP: endpoint RED (+ endpoint→query attribution IF the libpq spike passes)

**Goal.** For a plaintext-HTTP/1.x server captured inside the existing eBPF attach window, produce a per-endpoint RED table + a `slow_endpoint` finding, fail-open and bounded. **If** the §8 OQ#2 libpq spike passes, additionally answer "endpoint X was slow because Y ms went to a single Postgres query" for a dynamically-linked-libpq target; **if not**, ship the RED table with wall-time-only attribution and a `db spans unavailable` reason, and move DB spans to Phase 2.

**Pre-work (spikes — do these BEFORE drawing the MVP boundary):**
- **OQ#1 spike:** measure system-wide `read/write/sendto/recvfrom` tracepoint *firing* cost (not just output volume) in a no-`-p` bpftrace on a busy box → decide dedicated-program (default) vs fold-into-combined.
- **OQ#2 spike:** on this kernel/bpftrace under NOPASSWD sudo — uprobe on a `/proc`-resolved `libpq.so.5` by absolute path, `str(argN)` C-string read, multi-symbol comma-attach, the string-length env-var name (`BPFTRACE_MAX_STRLEN` vs `BPFTRACE_STRLEN`) + inline `str(ptr,N)` support → decide whether DB spans are in the MVP.
- **OQ#3 spike:** `bpftrace -lv` the syscall tracepoint field names on kernel 7.0.

**Work items (file-level):**
- `trace/events.py`: add `REQUEST='request'` + the slotted `Span` dataclass (`to_ndjson` on raw monotonic `start_ns`/`dur_ns`; `epoch_ms(anchor)` / resolved `timestamp_ms` per §4.1).
- `ebpf.py`: add `_BT_HTTP` (syscall-tracepoint plaintext HTTP boundary, `(pid,fd)`-keyed, `tid`=worker; stash race handled per §3.2); add `_BT_SQL` (libpq uprobe) **only if OQ#2 passes**; add `build_request_bt(pid, n, pq_lib=None)` (dedicated program; or widen `build_combined_bt` only if OQ#1 green-lights the fold); add `libpq_path(pid)` (multi-match aware, §3.4) + a `readelf --dyn-syms` preflight; thread an optional `env` dict through `run_bpftrace`→`_run_proc` for the string cap (var name from OQ#2); add `bpftrace_available()`-based request capability helper.
- `ebpf.py`: `parse_bpftrace_http(text, anchor)` / `parse_bpftrace_sql(text, anchor)` (clone `parse_bpftrace_gc`, carry the §2.6 anchor).
- `aggregate.py`: `endpoint_stats(spans)` (reuse `_percentile`).
- `orchestrator.py::_capture_ebpf`: capture the `(mono0, wall0)` anchor at child launch; resolve `pq_lib`; run the tid+nested-window correlator; write `requests.ndjson.zst` + `requests.json` (+`record_artifact`); publish throttled `request_rollup`; for monitor runs emit slow endpoints via `_make_incident('slow_endpoint', ..., ts=epoch_ms)`. **Per-capture gating (§3.6): eBPF-suite captures stay on `ebpf`; request captures on `requests`.** No SQLite span write.
- `orchestrator.py::_finalize`: `reqtrace_anomalies` pass guarded `if collectors.get("requests") and not monitor`.
- `orchestrator.py::start_attach_run` + `_start_ebpf`: thread `requests` into `collector_config`; spawn the capture thread on `(ebpf OR requests)` with per-capture gating.
- `runs.py`: `AttachRequest.requests` flag; `GET /runs/{rid}/requests` (stub-on-missing like `http_latency`); `GET /runs/attach/request-capabilities` (bpftrace+privilege+libpq-mappable, **not** the BTF/bcc gate).
- Frontend: `runViews()` `c.requests` gate + `RequestsTab` (RED table + DB-vs-wall bar, degrading to wall-only); `useOpenTrace` `request_rollup` branch; one e2e scenario.

**Exit criteria.** Attach (monitor + single-shot) to a Flask/uvicorn app; `requests.json` shows the per-`(method, route)` RED rows and a `slow_endpoint` finding that matches between Overview and Incidents, with incident timestamps in correct epoch time. **If the libpq spike passed:** hit an endpoint whose handler runs a slow `SELECT` and see `db_ms` dominating that row's p95. A non-HTTP or static-libpq target completes unchanged with a friendly reason and no Requests tab. A requests-only run (`ebpf=False`) produces **no** `latency.json`/`offcpu-flamegraph.json` and **no** latency incidents.

### Phase 2 — decomposition, TLS, more drivers, waterfall, curated SQLite + reader

**Goal.** Turn `db_ms` into a full on-CPU / off-CPU{disk,net,lock,sleep} / run-queue breakdown per request, cover TLS + MySQL/SQLite, add the per-request waterfall + span→off-CPU drill, and land the curated SQLite span write **with its reader**.

**Work items:**
- If not already in the MVP: land libpq DB spans (post-spike) and the DB-overlay column.
- Replace the aggregate `offcputime` with a per-event timestamped off-CPU stream (`sched_switch`/`sched_wakeup`, tid+kstack, >200µs threshold) folded into the eBPF program, keeping bcc `offcputime` as the aggregate fail-open fallback.
- Correlator: interval-overlap join off-CPU + runq intervals into each span (`on_cpu = dur − off_cpu − runq`; do **not** count 99Hz perf samples); classify each off-CPU kstack into disk/net/lock/sleep via an ordered regex table.
- Add `libssl_path(pid)` (multi-lib aware) + `SSL_write`(entry)/`SSL_read`(uretprobe) uprobes for TLS + path recovery; add `libmysqlclient`/`libmariadb` + `libsqlite3` client uprobes.
- Curated SQLite span write (`event_type='request'`, epoch `timestamp_ms` from §2.6, curation policy §4.4) **plus** its reader: a timeline overlay / incident-evidence linkage that queries those rows. Add the `spans`/index migration only if per-span SQL filtering is actually needed.
- `perf._fold_stacks` change to retain `(tid,ts)` per sample; `RequestWaterfall.tsx` + span→`offcpu-flamegraph?tid&from&to` drill + incident→request deep-link.

**Exit criteria.** The endpoint breakdown bar shows on/off/db split with a named blocking reason; a TLS-terminating server yields real routes; a slow request's waterfall drills into its off-CPU flamegraph filtered to its tid+window; curated spans are queryable by a real reader.

### Phase 3 — runtime-native + async

**Goal.** Get true per-request endpoints where bpftrace can't, and stop mis-attributing async/goroutine runtimes.

**Work items:**
- .NET `dotnet-trace` EventPipe `RequestStart`/`RequestStop` collector (attach-native, exact endpoint + latency).
- Node CDP handler-stack labeling (`node_cdp` Network/Runtime domains, Node-only SIGUSR1 gate) and JVM JFR `jdk.SocketRead`/`JavaMonitorEnter` per-handler attribution.
- Async/event-loop/goroutine detection → fall back to per-connection (fd/SSL*) attribution or report DB spans "unattributed" rather than guessing; timeline↔waterfall shared-domain correlation (using epoch `timestamp_ms`).

**Exit criteria.** At least .NET delivers true per-request endpoint + latency with zero target change; Node/Go/async targets degrade to connection-level RED with an honest "attribution unavailable" reason instead of wrong parents.

---

## 8. Risks & open questions

**Risks (with mitigations):**
- **System-wide tracepoint FIRING cost (needs a measured spike; stronger than output-volume alone).** The no-`-p` combined/dedicated program's `/pid==PID/` filter gates *action*, not *probe firing*. Adding `read/write/sendto/recvfrom` tracepoints makes the BPF handler fire on **every** such syscall on the **whole box** — a materially higher firing rate than today's per-context-switch / per-block-IO tracepoints, and a real CPU tax on a busy box even when almost everything is filtered out. **Measure this firing overhead before committing to the fold; it is the primary argument for the dedicated-second-single-bpftrace as the default** (§3.1). Mitigations if high: dedicate the request program; narrow to `accept4`-seeded server fds; drop `sendmsg`/`recvmsg` variants from the MVP.
- **Combined-program blast radius (needs a spike).** Folding HTTP/libpq into the existing runq/block/GC program may enlarge/slow its one CO-RE compile or drop probes on kernel 7.0. Default: keep request tracing as its **own** single bpftrace program (never N concurrent compiles); fold only if both spikes green-light it. Treat one-combined-vs-dedicated as an **open decision**, not settled.
- **libpq uprobe primitives unproven on this box (spike-gated, not committed).** Absolute-path uprobe, `str(argN)`, comma-attach, and the string-len env var have never been exercised here; only USDT + sched/block tracepoints are demonstrably working. DB spans are gated behind OQ#2; the MVP ships without them if it fails.
- **High-frequency output flood.** Plaintext read/write tracepoints emit per-fd; without an in-kernel request-line match + the string cap + temp-file capture + per-span caps, a busy server floods bpftrace `printf` (lines drop) and could deadlock a piped child. The temp-file rule + a volume threshold are load-bearing. (Distinct from the firing-cost risk above — this is output volume, that is handler CPU.)
- **Per-tid single-slot stash race.** `@rbuf[tid]`/`@q[tid]`/`@st[tid]`/`@inflight[pid,fd]` hold one item; interleaved `readv`/`writev`, a second fd read mid-request, or keep-alive overlap can clobber the plaintext read-buffer stash. Mitigate by seeding on server fds + clearing on `sys_exit_read`, and design the stash together with the pairing state machine (OQ#4); key by `(tid,fd)` if single-`tid` clobbering shows up.
- **Monotonic→epoch conversion (correctness, must not skip).** bpftrace `nsecs` is CLOCK_MONOTONIC; routing it into the incident `ts`/timeline/SQLite without the §2.6 anchor lands data decades off. The anchor (`mono0,wall0` at child launch) travels with each parsed span batch; raw `start_ns` stays for the off-CPU join only.
- **`libpq_path`/`libssl_path` multi-match.** Do not inherit `libpython_path`'s first-match-break; a process can map multiple libpq/TLS libs/versions. Collect all matches, pick the one whose `readelf --dyn-syms` exports the target symbol.
- **tid+window join is EXACT only for thread-per-request** (sync WSGI/gunicorn-sync, native, blocking-JDBC). Async/event-loop and goroutine pools mis-attribute — label spans "attributed, not exact" and fall back to unattributed rather than a wrong parent.
- **libpq linkage.** `psycopg2-binary` wheels statically bundle libpq with hidden symbols; `asyncpg`/`pgx`/pure-Python have no libpq — verify the exported symbol and emit a reason, never raise.
- **Capability gate.** Request capture gates on `bpftrace_available()` + privilege, **not** `caps["available"]` (BTF + bcc). Getting this wrong fails closed on exactly the boxes where uprobes/tracepoints still work.
- **Requests-only scoping.** `(ebpf OR requests)` on the *thread* must not co-enable the eBPF suite; per-capture gating (§3.6) keeps off-CPU/latency artifacts + latency incidents on the `ebpf` flag only.
- **Monitor invariant.** Any finalize-time request-anomaly pass MUST be guarded `and not monitor`; monitor findings go solely through `_make_incident` (with epoch `ts`).
- **PII surface.** SQL text carries literals in non-parameterized queries — capture the template/prefix only, never reassemble bound params, never feed raw SQL to the LLM without redaction.
- **perf fold prerequisite.** `perf._fold_stacks` collapses per-sample tid+ts today; the Phase-2 span→flamegraph drill needs retain+filter before folding; until then fail open to the unfiltered off-CPU flame.

**Open questions (spikes to run first — MVP scope depends on their outcomes):**
1. **Firing cost + blast radius:** Does adding HTTP syscall tracepoints (and, if OQ#2 passes, libpq uprobes) keep a single bpftrace stable + low-drop **and** low-CPU on a busy kernel-7.0 box, or must request tracing be its own dedicated single bpftrace? **Measure both the handler firing overhead and CO-RE compile/probe-drop.** Default assumption until measured: dedicated program.
2. **libpq/string primitives (gates DB spans into the MVP):** Confirm on this box under NOPASSWD sudo — uprobe on a `/proc`-resolved `libpq.so.5` by absolute path; `str(argN)` C-string read; multi-symbol comma-attach (`uprobe:{pq}:PQexec,uprobe:{pq}:PQexecParams`); the string-length env-var name (`BPFTRACE_MAX_STRLEN` vs `BPFTRACE_STRLEN`), its max, and whether inline `str(ptr,N)` is supported.
3. **Syscall tracepoint fields:** `bpftrace -lv tracepoint:syscalls:sys_enter_read/_exit_read/_enter_write` on kernel 7.0 (`fd`/`buf`/`count` vs `ubuf`; `ret` on exit) before writing `_BT_HTTP`.
4. **Pairing + stash state machine:** HTTP keep-alive span-end (response write vs next request read), avoiding mispairing without assuming no HTTP/1.1 pipelining, **and** whether the per-tid read-buffer stash needs a `(tid,fd)` key to survive interleaved reads.
5. **Correlator home + atomic rewrite:** inside `_capture_ebpf` per monitor snapshot (needed for per-snapshot incidents) vs a single `_finalize` pass for single-shot; confirm the read side never sees a torn `requests.json` across snapshot rewrites (the `write_json` temp+`os.replace` gives this).
6. **Phase-2 curation + SQLite reader:** the slow-threshold + per-run cap for the deferred curated write, and the exact reader (timeline overlay / incident evidence) that will consume `event_type='request'` rows — the write does not land until the reader is specified.
7. **Off-CPU stream in MVP?** Ship the per-event timestamped off-CPU stream in the MVP (richer "why") or keep the MVP to endpoint RED (+ libpq span-duration if OQ#2 passes) and defer off-CPU decomposition to Phase 2?

---

## 9. References

**Zero-code eBPF APM prior art**
- Grafana Beyla / OpenTelemetry eBPF Instrumentation (OBI): https://grafana.com/oss/beyla-ebpf/ · https://opentelemetry.io/docs/zero-code/obi/ · https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/blob/main/bpf/generictracer/k_tracer.c · https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/blob/main/bpf/generictracer/libssl.c · go tracers: https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/blob/main/bpf/gotracer/go_nethttp.c · .../go_sql.c
- Pixie: https://docs.px.dev/about-pixie/pixie-ebpf/ · https://blog.px.dev/ebpf-http-tracing/ · https://blog.px.dev/ebpf-tls-tracing-past-present-future/ · https://blog.px.dev/ebpf-openssl-tracing/ · https://blog.px.dev/ebpf-http2-tracing/ · https://github.com/pixie-io/pixie/blob/main/src/stirling/source_connectors/socket_tracer/bcc_bpf/socket_trace.c · .../openssl_trace.c
- Coroot: https://docs.coroot.com/tracing/ebpf-based-tracing/ · https://coroot.com/blog/java-tls-instrumentation-with-ebpf/ · https://github.com/coroot/coroot-node-agent/blob/main/ebpftracer/ebpf/l7/l7.c · .../postgres.c · .../mysql.c · .../openssl.c
- DeepFlow AutoTracing (no-injection correlation): https://deepflow.io/docs/features/distributed-tracing/auto-tracing/ · https://github.com/deepflowio/deepflow
- Odigos: https://odigos.io/blog/mysql-psql-ebpf · https://docs.odigos.io/instrumentations/golang/ebpf

**bpftrace / eBPF technique**
- https://www.osso.nl/blog/2023/viewing-unencrypted-traffic-ltrace-bpftrace/ · https://eunomia.dev/tutorials/30-sslsniff/ · https://eunomia.dev/tutorials/23-http/ · https://eunomia.dev/tutorials/40-mysql/ · https://bpftrace.org/docs/release_024/language · https://dxuuu.xyz/big-strings.html · https://man.archlinux.org/man/extra/bpftrace/bpftrace.8.en
- Off-CPU analysis (Gregg): https://www.brendangregg.com/offcpuanalysis.html · https://www.brendangregg.com/FlameGraphs/offcpuflamegraphs.html · https://www.brendangregg.com/blog/2016-01-20/ebpf-offcpu-flame-graph.html · https://github.com/brendangregg/bpf-perf-tools-book/blob/master/originals/Ch06_CPUs/offcputime.bt
- Go uprobe challenges: https://blog.0x74696d.com/posts/challenges-of-bpf-tracing-go/ · https://dev.to/maheshrayas/04-ebpf-uprobes-decoding-go-function-arguments-registers-memory-layout-to-parse-grpc-headers-6n8
- HPACK (why HTTP/2 paths aren't recoverable mid-stream): https://datatracker.ietf.org/doc/html/rfc7541

**DB driver symbols**
- libpq: https://www.postgresql.org/docs/current/libpq-exec.html · https://www.postgresql.org/docs/current/libpq-async.html
- bcc dbslower / mysqld_qslower (server-side, off-model): https://github.com/iovisor/bcc/blob/master/tools/dbslower.py · https://github.com/iovisor/bcc/blob/master/tools/mysqld_qslower.py · https://www.brendangregg.com/blog/2016-10-04/linux-bcc-mysqld-qslower.html
- SQLite: https://sqlite.org/c3ref/prepare.html · https://sqlite.org/c3ref/step.html
- psycopg2/psycopg3 linkage: https://www.psycopg.org/docs/install.html · https://www.psycopg.org/psycopg3/docs/basic/install.html

**Runtime-native request attribution**
- .NET EventPipe / dotnet-trace / ASP.NET Core HostingEventSource: https://learn.microsoft.com/en-us/dotnet/core/diagnostics/eventpipe · https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dotnet-trace · https://github.com/dotnet/aspnetcore/blob/main/src/Hosting/Hosting/src/Internal/HostingEventSource.cs
- Node inspector / CDP Network: https://nodejs.org/api/inspector.html · https://chromedevtools.github.io/devtools-protocol/tot/Network/ · https://github.com/nodejs/node/pull/53593
- JVM async-profiler / JFR / jattach: https://github.com/async-profiler/async-profiler/blob/master/docs/ProfilingModes.md · https://docs.oracle.com/en/java/javase/13/troubleshoot/troubleshoot-performance-issues-using-jfr.html
- Samplers: https://github.com/adsr/phpspy · https://rbspy.github.io/using-rbspy/record.html · CPython USDT: https://docs.python.org/3/howto/instrumentation.html · https://github.com/python/cpython/issues/98894 · PEP 669 (in-process only, ruled out): https://peps.python.org/pep-0669/

**Data model + method**
- OpenTelemetry span model (field names borrowed, not the SDK): https://opentelemetry.io/docs/concepts/signals/traces/ · https://opentelemetry.io/docs/specs/otel/trace/api/
- RED method: https://grafana.com/blog/the-red-method-how-to-instrument-your-services/ · https://thenewstack.io/monitoring-microservices-red-method/
- Clock domains (why CLOCK_MONOTONIC ≠ epoch; the anchor pattern): https://www.brendangregg.com/blog/2016-01-20/ebpf-offcpu-flame-graph.html · https://man7.org/linux/man-pages/man2/clock_gettime.2.html

**UI prior art**
- https://github.com/jaegertracing/jaeger-ui · https://docs.datadoghq.com/tracing/services/service_page/ · https://docs.datadoghq.com/profiler/connect_traces_and_profiles/ · https://docs.datadoghq.com/tracing/guide/slowest_request_daily/ · https://docs.sentry.io/product/sentry-basics/concepts/tracing/event-detail/ · https://docs.px.dev/tutorials/pixie-101/service-performance/ · https://coroot.com/blog/instrumenting-the-node-js-event-loop-with-ebpf/

---

## 10. Spike results — MVP boundary RESOLVED (2026-07-09)

All three Phase-1 pre-work spikes (§7 pre-work; §8 OQ#1–3) were executed on the target
box. **Environment:** kernel **7.0.14-201.fc44**, **bpftrace v0.24.2**, 20 cores, BTF
present, `unprivileged_bpf_disabled=2` (**sudo mandatory** — no unprivileged BPF at all).
Passwordless sudo is scoped to **exact tool paths** (`/usr/bin/bpftrace` + the bcc tools),
with `env_reset` and a fixed `env_keep` that does **not** include `BPFTRACE_*`. System
`/lib64/libpq.so.5` present; `postgres`/`psql` installed (used as the libpq target).
Harness scripts live in the session scratchpad (`oq1_firing.sh`, `oq1b_pflag.sh`,
`oq2_libpq.sh`, `oq2b_async.sh`, `scbench.c`).

### OQ#3 — syscall tracepoint fields (RESOLVED)
`bpftrace -lv` **requires root even to list** → must go through `sudo -n`. Confirmed field
names on kernel 7.0.14:

| tracepoint | buffer ptr | length | exit |
|---|---|---|---|
| `sys_enter_read` | `buf` (`char*`) | `count` (`size_t`) | `sys_exit_read` → `ret` (`long`) |
| `sys_enter_write` | `buf` (`const char*`) | `count` | also exposes `__data_loc char[] __buf_val` (kernel-captured write bytes, truncated) |
| `sys_enter_recvfrom` | `ubuf` (`void*`) | `size` | `sys_exit_recvfrom` → `ret` |
| `sys_enter_sendto` | `buff` (`void*`) | `len` | — |
| `sys_enter_accept4` | — | — | `sys_exit_accept4` → `ret` = new connfd (server-fd seed) |
| `sys_enter_connect` | `uservaddr` | `addrlen` | outbound-fd seed |
| `sys_enter_close` | `fd` | — | fd teardown |

**String cap (supersedes §2.5 + the §3.4 `env`-dict harness change):** `BPFTRACE_MAX_STRLEN`
**default is 1024 bytes** (not ~64) — a request line / SQL prefix fits with **no override**.
The env-var name is `BPFTRACE_MAX_STRLEN` (not the old `BPFTRACE_STRLEN`). **The env-var path
is unusable under this sudo model**: `env_reset` strips a pre-set `BPFTRACE_MAX_STRLEN`, and
`sudo -n env BPFTRACE_MAX_STRLEN=… bpftrace` is **DENIED** ("sudo: a password is required" —
sudoers whitelists only `/usr/bin/bpftrace`, not `/usr/bin/env`). Both env-free alternatives
work and were verified: **inline `str(ptr, N)`** and the in-script **`config = { max_strlen = N }`**
block. → **Do NOT thread an `env` dict through `run_bpftrace`; use inline `str(ptr,N)`/`config`.**

### OQ#2 — libpq uprobe primitives (RESOLVED — DB spans ARE in the MVP, with an async correction)
Verified against a throwaway PG18 cluster driven by `psql`:
- ✅ **absolute-path uprobe** (`uprobe:/usr/lib64/libpq.so.private18-5.18:PQexec`), ✅ **multi-symbol
  comma-attach**, ✅ **`str(arg1)`** (full 95-char marker returned uncut), ✅ **`/pid==PID/`**
  scoping, ✅ **entry→uretprobe span**. Every primitive the plan gated behind OQ#2 works.
- **`libpq_path(pid)` MUST resolve from `/proc/<pid>/maps`** (confirms §3.4). Fedora's `psql`
  maps **`libpq.so.private18-5.18`**, *not* `libpq.so.5` — a filename assumption would miss it.
  A psycopg2 app built on system libpq would map `/lib64/libpq.so.5`; both are dynamic with
  `PQexec`/`PQexecParams`/`PQsendQuery`/`PQgetResult` exported as real `FUNC`s.
- ⚠️ **ASYNC CORRECTION (changes §3.3).** The default Postgres client (`psql`, and — pending
  per-driver confirmation — psycopg2) uses the **async** libpq API: `PQsendQuery`, *not* the
  synchronous `PQexec`. An entry→uretprobe span on `PQsendQuery` measured **`dur_ms=0`** — it
  only enqueues; the real wait is inside `PQgetResult`. The **correct async DB-span** is
  `PQsendQuery` entry → the `PQgetResult` uretprobe whose **`retval==0` (NULL)** terminates the
  result loop: measured **`dur_ms=201`** for a `pg_sleep(0.2)` (`getresult_calls=2`). So `_BT_SQL`
  must support the async pattern (stash on `PQsendQuery`, close on terminal `PQgetResult`), not
  only sync `PQexec`/`PQexecParams`. The single-slot `@st[tid]`/`@q[tid]` stash is still safe for
  a synchronous-blocking client (one in-flight query per connection/thread); true pipelining is
  still out of scope. **Residual:** confirm psycopg2/psycopg3/libpqxx's actual symbol during impl
  (psycopg2 not installed here; async proven via psql).

### OQ#1 — system-wide tracepoint firing cost (RESOLVED — dedicated program, no `-p`)
Tight C microbench (`N` read+write of 1 byte = `2N` syscalls), timed via `CLOCK_MONOTONIC`
around the loop only, coordinated with `bpftrace -c`:

| condition | ns/syscall | Δ vs baseline |
|---|---|---|
| baseline (no probe) | 157.6 | — |
| **bystander** (probes attached, `/pid==1/` rejects — the innocent-bystander tax) | 294.8 | **+137 ns** |
| **matched** (target's own syscalls, full stash + `str()` runs) | 599.0 | **+441 ns** |

- **Ambient box rate ≈ 4,300 read/write/send/recv syscalls/sec** (idle-ish) → bystander tax
  here ≈ **0.06% of one core**. Projections: ~1.4% of one core at 100k syscalls/s, ~14% at 1M/s.
  The tax is bounded to the attach window and only while system-wide read/write tracepoints
  are attached.
- **`bpftrace -p` is the FILTER model, not a per-task perf attach.** With a dedicated read/write
  program attached via `-p <unrelated idle pid>`, a *bystander* bench still slowed **+135.7 ns/syscall**
  (≈ the `/pid==1/` tax). So `-p` gives **zero** bystander relief and only adds the harmful
  all-probe global filter — **fully vindicating the no-`-p` rule.** The ~137 ns firing cost is
  **intrinsic** while the tracepoints are attached; it is identical for a dedicated vs folded
  program (same tracepoints).
- **Decision:** ship request tracing as its own **DEDICATED single bpftrace program** with the
  in-script **`/pid==PID/`** filter (never `-p`). Firing cost is neutral to the fold question, so
  dedicated wins on the independent grounds of §3.6 requests-only scoping and not enlarging the
  combined program's single CO-RE compile / probe-drop risk. The "fold blast-radius" test is
  moot (dedicated sidesteps it). **Minimize the tracepoint set**: `read`/`write`(+`exit_read`)
  cover read/write-based servers; add `recvfrom`/`sendto` only for socket-syscall servers
  (CPython `socket.recv` → `recvfrom`), accepting the extra firing cost per added tracepoint.

### Net MVP boundary (post-spike)
- **HTTP endpoint RED + `slow_endpoint`**: unchanged, HIGH confidence, in.
- **DB (libpq) spans: IN the MVP** — the OQ#2 primitives all work — **with the async
  `PQsendQuery`→`PQgetResult(NULL)` span** as the primary path (sync `PQexec`/`PQexecParams`
  entry→exit also supported for synchronous clients/libpqxx).
- **Dedicated bpftrace program**, `/pid==PID/`, no `-p`, inline `str`/`config` for any string
  sizing (no `env` dict), `libpq_path` from `/proc/maps`.
- **Still deferred to Phase 2:** off-CPU decomposition (OQ#7), TLS/`libssl`, MySQL/SQLite,
  curated SQLite write + reader, waterfall + drill.

---

## 11. Implementation status — Phase 1 MVP SHIPPED (2026-07-10)

The Phase-1 MVP is **fully implemented and verified end-to-end** against a real
Flask(threaded)+psycopg2→system-libpq app: attach with `requests=true` produces a
per-endpoint RED table + a `slow_endpoint` finding, with DB time attributed per request
(`GET /slow` → p95 608ms, `db_ms_share` 0.99, *"99% of that time is DB queries"*).

- **Backend** (`ebpf.py` `_BT_HTTP`/`_BT_SQL`/`build_request_bt`/`libpq_path`/parsers/
  `request_capabilities`; `aggregate.py` `correlate_spans`/`endpoint_stats`/`request_rollup`/
  `reqtrace_anomalies`; `orchestrator.py` `_capture_requests` + per-flag gating + finalize
  pass; `runs.py` flag + `GET /runs/{id}/requests` + `GET /runs/attach/request-capabilities`;
  `events.py` `Span`). **DB spans DID land in the MVP** (OQ#2 passed) via the async
  `PQsendQuery→PQgetResult(NULL)` path (§10). Adversarially reviewed — 3 fixes: unterminated-
  SQL-literal PII scrub, single-owner correlation + db-share clamp, route slash canonicalization.
- **Frontend** (`RequestsTab.tsx` RED table + DB-vs-app breakdown; `RunView` gate;
  `useOpenTrace` `request_rollup` SSE; `AttachModal` "Request tracing" checkbox + capability
  probe).
- **Verification:** backend 206 · frontend 64 · e2e 174/174 (new `21-requests.js`) · tsc/lint/
  build green.
- **Deviations from this doc, all deliberate:** the §3.4 `env`-dict string-cap harness change
  was NOT done (env-var unusable under NOPASSWD sudo — §10 OQ#3); the §2.6 monotonic→epoch
  anchor is NOT needed in the MVP (no span reaches an absolute sink — incident `ts` uses the
  latest sample's epoch); spans are dicts/`Span` with no SQLite write (§3.5, as planned).

---

## 12. Implementation status — Phase 2 SHIPPED (2026-07-10)

**All of Phase 2 (§7) is implemented and live-validated** against the multi-signal target
(`scratchpad/target_app.py`: libpq via ctypes, in-process SQLite, a TLS variant, and
sleep/CPU routes). Everything is fail-open and gated on the same `requests` flag.

- **Off-CPU decomposition of `db_ms` (OQ#7).** The request program now carries a per-request
  off-CPU / run-queue tracker (`_BT_OFFCPU`): `sched_switch`/`sched_wakeup` scoped to threads
  ACTIVELY serving a request (`@active[tid]`, set at REQ / cleared at RSP — so the system-wide
  sched tracepoints do almost no work outside a request window, no `-p`). It emits per-interval
  `OFF`/`RQ` lines (+ a coarse blocking-syscall reason: net/lock/sleep/disk via `@insc[tid]`).
  `aggregate.correlate_breakdown` splits each request's wall time into **on-CPU / run-queue /
  DB-wait (off-CPU ∩ a DB span) / other-off-CPU** — the four buckets sum to the duration.
  Validated: `/db` → 97% DB-wait, `/sqlite` → 99% on-CPU (in-process DB is an *overlay* on
  on-CPU, never a bucket), `/sleep` → 99% off-CPU(sleep), `/cpu` → 99% on-CPU.
- **TLS (`libssl`).** `_BT_TLS`/`_BT_TLS_EX` probe `SSL_read`/`SSL_write` (+ the OpenSSL-3
  `_ex` variants CPython uses — count in the `*readbytes` out-param), keyed by tid, emitting
  the same REQ/RSP lines → the whole pipeline (routes, DB spans, breakdown) works over HTTPS.
  `build_request_bt` emits only the variants the target's libssl exports; `libssl_path` resolves it.
- **MySQL / SQLite drivers.** `_BT_MYSQL` (`mysql_real_query`, unit-tested — no server here) and
  `_BT_SQLITE` (`sqlite3_prepare_v2/v3`→text, `sqlite3_step` >1ms spans, with a **reentrancy
  depth-guard** for SQLite's nested internal schema prepares + a finalize/END `clear` that also
  prevents raw-SQL map-dump PII). `db_libs(pid)` resolves both; both reuse the SQL parser.
- **Per-request waterfall + span→off-CPU-flamegraph drill.** `@ostk[tid, kstack]` aggregates
  each thread's off-CPU stacks (block-time `kstack`, scheduler epilogue stripped, decimal
  offsets handled); `extract_offcpu_stacks` → `perf.fold_collapsed` per tid → `request-offcpu.json`;
  `GET /runs/{id}/offcpu-flamegraph?tid=` serves the per-thread flame (fail-open to the aggregate).
  `RequestWaterfall.tsx` renders sampled requests as duration tracks with nested DB spans;
  expanding one shows the breakdown bar + SQL + the tid-filtered off-CPU `FlamegraphTab` drill.
- **Curated SQLite write + reader (§3.5/§4.4, with its reader this time).** `curate_request_spans`
  keeps slow (≥ endpoint p95) / errored spans (cap 200), converting each span's CLOCK_MONOTONIC
  `start_ns` → **epoch ms via the §2.6 `(mono0, wall0)` child-launch anchor** (now genuinely
  needed and captured); `storage.insert_request_spans` writes `event_type='request'` rows and
  `read_request_spans` / `GET /runs/{id}/request-spans` reads them back (time-queryable). Kept
  isolated: `read_events` excludes `event_type='request'` so the syscall aggregations + raw
  Events tab are untouched.
- **Adversarially reviewed (12-agent workflow) — 5 fixes:** (1) HIGH `@active[tid]`/`@insc[tid]`
  leaked when a request closed without a probed response write (writev/sendfile or an aborted
  connection) → sched probes kept working + idle epoll_wait polluted the drill flame; fixed by
  clearing them on SERVER-fd close (gated on `@srv` so an intermediate DB-socket close mid-request
  can't truncate live tracking). (2) MED the waterfall keyed rows by array index → a monitor
  re-sort rebound an open row (+ its drill) to another request; fixed with a stable span-identity
  key. (3) LOW `@sqq`/`@mqq` (raw SQL) weren't in the `END` clear → an in-flight query at window
  close auto-dumped unredacted SQL; added. (4) LOW documented that `request-offcpu.json` is
  latest-window-only (drill from the live rollup, not a historical curated span). (5) LOW the drill
  header claimed "thread N blocked" even on the whole-process fallback; fixed.
- **Verification:** backend 218 · frontend 68 · e2e 7/7 requests (`requests-waterfall-breakdown-drill`)
  · tsc clean · lint 24 (baseline) · build green. Live-validated on kernel 7.0.14 / bpftrace 0.24.2.
- **Not live-validated (no server on this box):** MySQL uprobes — symbol-correct + unit-tested,
  fail-open. Everything else was exercised against real Postgres / SQLite / a TLS server.