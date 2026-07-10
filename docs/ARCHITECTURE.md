# OpenTrace Architecture

> **Audience:** contributors and curious engineers who want to understand how OpenTrace works internally.
> This is the conceptual narrative — *why* the pieces exist and *how* they fit together. For the exhaustive
> tree→responsibility map (every backend module, the Electron shell, the frontend component tree), read
> [`docs/structure.md`](./structure.md). For running and testing it, see [`README.md`](../README.md),
> [`docs/SETUP.md`](./SETUP.md), [`docs/USAGE.md`](./USAGE.md), [`docs/TROUBLESHOOTING.md`](./TROUBLESHOOTING.md),
> and the manual-test playbook [`docs/testing.md`](./testing.md).

OpenTrace is a **local-first Linux observability desktop app**. It runs commands you type in an embedded
terminal (or attaches to already-running processes), collects low-level signals — syscalls, resource
metrics, CPU/off-CPU profiles, latency histograms, HTTP/DB request spans — and turns them into correlated,
visual findings. Everything happens on one machine, for one user, with no cloud component.

### Scope and honest limitations (read this first)

- **Linux only.** The whole design leans on `/proc`, `ptrace`, `perf_event`, cgroups, and eBPF. There is no
  macOS/Windows story.
- **Runs from source — no packaged installer yet.** `.deb`/`.AppImage` packaging is explicitly not done;
  today you clone and run [`./start.sh`](../start.sh). See [`README.md`](../README.md).
- **Many features degrade to "resource timeline only" without external tools.** `strace`/`ltrace`/`perf`,
  the per-language samplers (`py-spy`, `rbspy`, `asprof`, `dotnet-trace`, `phpspy`), and the eBPF stack
  (`bcc` tools + `bpftrace`) are each optional. Missing tools never break a run — they just narrow what it
  can show (the **fail-open** principle, below).
- **eBPF and request tracing need elevated privilege** (root, `CAP_BPF`+`CAP_PERFMON`, or passwordless sudo
  for the tools). Everything else works unprivileged for your own processes.
- **Single machine, single user.** Attach refuses PIDs owned by another user unless the backend runs as
  root; the API accepts only local callers.
- **Some runtimes/paths are implemented but not live-validated.** Called out inline and summarized in
  [§13](#13-honesty-what-is-and-isnt-live-validated).

---

## 1. The three-process model

OpenTrace is three cooperating processes plus a renderer, wired so that a crash or a slow start in any one
of them degrades gracefully rather than taking down the app.

```
┌─────────────────────────── Electron main (electron/main.js) ───────────────────────────┐
│  • owns app lifecycle, native menu, window                                              │
│  • spawns + health-checks the backend, restarts it with backoff                         │
│  • hosts the embedded pty (electron/pty.js, node-pty) — one global login shell          │
│  • bridges everything to the renderer via contextBridge (electron/preload.js)           │
└───────────────┬───────────────────────────────────────────────┬─────────────────────────┘
                │ spawn uvicorn (child process)                  │ IPC (contextBridge)
                ▼                                                 ▼
   ┌──────────────────────────────┐                  ┌──────────────────────────────┐
   │ FastAPI backend              │  REST + SSE      │ React 19 renderer            │
   │ (uvicorn app.main:app)       │◀────────────────▶│ (frontend/, Vite build)      │
   │ • all data + analysis        │                  │ • one SSE hook = source of   │
   │ • SQLite + on-disk record    │                  │   truth (useOpenTrace)       │
   └──────────────────────────────┘                  └──────────────────────────────┘
```

**Electron main** ([`electron/main.js`](../electron/main.js)) resolves a backend port, spawns the backend as
a child, waits on `GET /health`, then opens the `BrowserWindow`. Port logic
(`resolveBackendPort`) is deliberate:

| Situation on port 8000 | Behavior |
|---|---|
| Free | Bind it and **spawn** the backend there. |
| Occupied, and `GET /info` returns `schema_version` + `sessions_dir` | It's a real OpenTrace backend — **reuse** it, don't spawn. |
| Occupied by a foreign server | Bind an ephemeral free port and **spawn** there. |
| `OPENTRACE_BACKEND_URL` set | Never spawn — point straight at that URL (used by the e2e harness). |

Note the identity check uses `/info`, **not** `/health` — a generic `{"status":"ok"}` doesn't prove it's
OpenTrace. If the backend child dies, main.js restarts it with exponential backoff
(`1000 * 2**attempt` ms, capped at 3 attempts, reset after a >60s healthy stretch); on permanent failure it
surfaces a dialog with the port and the last 15 stderr lines. A *merely slow* start opens the window anyway
and lets the renderer's reconnecting `EventSource` self-heal.

**FastAPI backend** (`app.main:app`) is where all data and analysis live. Its `lifespan()` runs
`paths.ensure_dirs()` → `config.load()` → `db.init()` → `orchestrator.reconcile_orphans()` (flips runs left
`running`/`analyzing` by a previous crashed process to `error`, since their in-memory context is gone).

> **Backend prerequisites.** The backend runs under a dedicated **Python 3.11** interpreter carrying its hard
> dependencies — `fastapi`, `uvicorn`, `psutil`, `aiosqlite`, `zstandard`, `httpx`. [`./start.sh`](../start.sh)
> probes the interpreter named by `OPENTRACE_PYTHON` (defaulting to `python`/`python3`), fails fast with a
> friendly message when those imports don't resolve, and pins `OPENTRACE_HOME` to a dev-local `tmp-opentrace/`
> directory so a dev run never touches your real `~/.opentrace`. The full setup procedure is in
> [`SETUP.md`](./SETUP.md).

**The pty** ([`electron/pty.js`](../electron/pty.js)) is a single module-level `node-pty` session running the
user's real login shell (`zsh -l`/`bash -l`), streamed to the renderer's xterm over IPC. It is **not** per
window or per tab — a second `pty:start` while a live session exists just `reattach()`es, so the shell (and
any running command) survives a renderer reload. Scrollback is mirrored to
`<userData>/terminal-scrollback.log` (debounced, best-effort) so it can be replayed after a reload or cold
start.

**The renderer** talks to the backend only through `window.opentrace` (contextBridge; `contextIsolation:true`,
`nodeIntegration:false`). It receives the backend URL and per-launch token as
`--opentrace-backend-url=`/`--opentrace-api-token=` launch args, not from its own environment.

### REST + SSE: request/response for reads and mutations, one stream for liveness

The backend exposes a plain REST surface (routers: `sessions`, `terminals`, `runs`, `llm`, `rules_api`, plus
root-level `/health`, `/info`, `/config/*`, `/stream`). Every REST call from the frontend goes through
`apiFetch` (`frontend/src/state/api.ts`), which adds `Authorization: Bearer <token>` when a token is present.

Liveness rides a **Server-Sent Events** stream. The frontend opens exactly one `EventSource` at
`GET /stream` inside a single hook, `frontend/src/state/useOpenTrace.ts` — the "single source of truth."
Background threads in the backend (the metrics poller, the orchestrator) publish synchronously into an
in-process pub/sub `Broker` (`backend/app/streaming.py`), which fans each event out to the exact `run_id`
channel and the `"*"` wildcard channel; `sse_response()` bridges that to the async endpoint with keepalive
pings. SSE message types include `run_started`, `run_analyzing`, `run_ended`, `metric`, `anomaly_alert`,
`incident`/`incident_update`/`incident_ai`, and `request_rollup`.

Two deliberate performance choices around SSE:

- **`metric` samples never touch React state.** At ~4 samples/sec per live run, routing them through a
  `useState` re-rendered the whole tree. Instead they're diverted to a module-level store
  (`frontend/src/state/liveMetrics.ts`, a `Map<runId, ring-buffer>` subscribed via `useSyncExternalStore`),
  so only the sparkline components re-render. Anything wanting live metrics must call `useLiveMetrics(runId)`.
- **`GET /runs/{id}/metrics` bypasses Pydantic** and hand-serializes a `Response(json.dumps(...))`, because a
  multi-hour monitor run's payload is big enough that model validation would stall every other endpoint. It
  stride-downsamples (every Nth real row, never averaging, so spikes and exact timestamps survive);
  `max_points=0` returns the full stream.

The stream has **no replay** — lifecycle events emitted while the stream was down are gone. So on every
`EventSource` `onopen`, `useOpenTrace` re-fetches `GET /sessions` and `GET /runs?limit=200` and reconciles.
Backend reachability in the UI is derived purely from the SSE connection state ("the stream *is* the live
connection"), not a `/health` poll; the Electron main process additionally pushes backend-process crash/restart
status over `window.opentrace.backend.onStatus` so the UI can distinguish "backend crashed" from "SSE blipped."

---

## 2. The data spine

Everything hangs off a three-level hierarchy, and every analytical table hangs off a run.

```
sessions (projects)
   └── terminals            (one per embedded shell registration)
          └── runs          (one traced command, or one attach window)
                 ├── events        (curated syscall/lifecycle subset)   ─┐
                 ├── metrics        (full-fidelity psutil timeline)       │ all FK → run_id,
                 ├── anomalies      (findings)                            │ ON DELETE CASCADE
                 ├── artifacts      (flamegraph.json, profile.json, …)   ─┘
                 └── (custom_rules is the one GLOBAL table — no run_id)
```

The database is a **single SQLite file** at `~/.opentrace/sessions.db` (schema in
[`backend/app/db.py`](../backend/app/db.py)), opened in WAL mode with `busy_timeout=5000` so the metrics
poller thread and request handlers don't collide. `custom_rules` (migration v3) is intentionally the odd one
out: it has no `run_id` because one custom ruleset applies to every run.

`init()` is idempotent and applies numbered migrations, but there is **no upgrade path from the pre-release
Phase-0 schema** — a legacy `sessions` table lacking a `slug` column is detected and *dropped and rebuilt*
(that data isn't worth preserving). A couple of columns/tables are honest dead weight, called out in the
schema: `sessions.last_opened_at` is only ever set at creation (list ordering uses `created_at DESC`), and
`runs.ui_state_json` is a leftover column no code reads.

### Two destinations: complete record on disk, curated subset in SQLite

This split is the core of why OpenTrace stays fast on a multi-hour run while never losing data
([`backend/app/storage.py`](../backend/app/storage.py), [`backend/app/paths.py`](../backend/app/paths.py)):

| Data | On disk (`~/.opentrace/sessions/<slug>/runs/<cmd>-<stamp>/`) | In SQLite |
|---|---|---|
| Syscall/library events | `events.ndjson.zst` — **complete**, zstd-streamed, source of truth | **curated subset only** (errors, lifecycle, slow calls, anomaly evidence) |
| Resource metrics | `metrics.ndjson.zst` — complete | **stored in full** (a few samples/sec is cheap) |
| Raw trace | `strace.log` / `ltrace.log` | — |
| Monitor incidents | `incidents.ndjson` (append-only) | — |
| Derived profiles | `flamegraph.json`, `profile.json`, `offcpu-flamegraph.json`, `latency.json`, `gc-timeline.json`, `requests.json` | — |
| Request spans | `requests.ndjson.zst` | curated slow/errored spans as `event_type='request'` rows |

Note the asymmetry that surprises people: **`metrics` is stored in full, only `events` is curated** — the
opposite of what symmetry would suggest, because persisting every syscall would bloat the DB while metrics
are naturally sparse. Derived JSON is written atomically (`.tmp<pid>` + `os.replace`) so a concurrent `GET`
never sees a torn file. Curated request spans live in the shared `events` table but are held out of
`read_events()` and the syscall aggregations by an explicit `event_type != 'request'` filter, so they never
pollute the Syscalls/I/O tabs.

Run directories are named `slugify(command_basename(command))-YYYYMMDD_HHMMSS` (`python app.py` →
`python-...`), with `-2`, `-3`, … appended on a same-second collision. The whole tree relocates by setting
`OPENTRACE_HOME` — `paths.home()` reads it *fresh on every call*, so test isolation needs nothing but the env
var (no module reload).

---

## 3. Two ways a run is born — one analysis pipeline

There are exactly two ways a run enters the system, and they converge on a single finalizer,
`orchestrator._finalize` ([`backend/app/trace/orchestrator.py`](../backend/app/trace/orchestrator.py)).

### 3a. Launch-trace: the transparent shell handshake

When you type `python app.py` in the embedded terminal, a **zsh line-editor widget rewrites the buffer to
`otrace -- python app.py` *before* zsh parses it** ([`electron/shell-hooks/opentrace-hook.zsh`](../electron/shell-hooks/opentrace-hook.zsh)).
Because the rewrite happens pre-parse, the shell forks `otrace` as a genuinely native foreground job —
quoting, globbing, job control, `$?`, and Ctrl-C all behave exactly as if you'd typed the command bare.
A `zshaddhistory` hook strips the `otrace --` wrapper so your history and up-arrow show the command *as you
typed it*.

The widget is conservative about *what* it wraps (`opentrace_should_trace`): it skips pipelines, redirections,
subshells, backgrounding, comments, builtins/functions/aliases, an explicit deny-list of interactive TUIs
(`vim`, `less`, `top`, `ssh`, `tmux`, …), and bare REPLs (`python` with no args is a REPL; `python app.py` is
a script). Toggling and session switches are communicated to the shell by sourcing a per-pty runtime file
(`OPENTRACE_RT`) on every prompt — never by echoing `export` lines into the terminal.

> **bash gets a weaker deal, honestly.** bash's readline can't rewrite the accepted line from a `bind -x`
> handler, so bash has **no transparent auto-wrap** — only an opt-in `ot <cmd>` helper. This is documented in
> the hook itself as "the honest Phase-1 scope for bash." Unsupported shells (fish, nushell) get a fully
> working terminal with tracing simply disabled.

The [`otrace`](../electron/shell-hooks/otrace) wrapper (bash — `#!/usr/bin/env bash`, using bash arrays to
build the trace command) then runs the `/start` → pid → `/end` handshake against the backend:

```
POST /runs/start   {command, cwd[, session_id][, terminal_id]}   → {run, strace_log_path, run_dir, collectors}
                    (otrace deliberately OMITS collector_config so live Settings/Monitor toggles win)
POST /runs/{id}/pid {pid}                                          → backend starts psutil polling on the tree
POST /runs/{id}/end {exit_code, exit_signal, ended_at}            → orchestrator._finalize
```

`otrace` builds the actual trace command from the `collectors` field of the `/start` response (see the
collector model, [§5](#5-the-collector-model)), backgrounds it, reports the child pid, `wait`s for the true
exit status, then reports `/end`. It is **fail-open at every step**: no `curl` → run untraced; backend down
or `/start` empty → `exec "$@"` untraced; whatever happens, `otrace` always `exit $rc` with the command's
real status regardless of whether `/end` succeeded.

### 3b. Attach: profile an already-running process for a bounded window

```
POST /runs/attach   {pid | port, window_s=20, session_id?, monitor=false, ebpf=false, requests=false}
```

No command is spawned. `orchestrator.start_attach_run` validates the target and enforces the guardrails:

| Gate | Rule |
|---|---|
| Existence | `psutil.pid_exists(pid)`, else 400; a `port` resolves to a `CONN_LISTEN` pid or 404s. |
| **Ownership** | If the backend isn't root and the target's uid ≠ yours → 400 (`attach requires a same-user process`). Root bypasses. |
| **Concurrency** | `_MAX_ATTACH_ACTIVE = 16` live attach/monitor contexts — each spawns profiler(+eBPF) threads, so an unbounded burst is a CPU-exhaustion hazard. |
| Window | `window_s` clamped to `[3, 120]`s — always self-terminating. |
| Cgroup limits | `container.cgroup_limits(pid)` (fail-open) stamps `cgroup_cpu_quota_cores`/`cgroup_mem_limit_bytes` onto the run for the container-aware rules. |

Attach picks a profiler (`attach.profiler_plan`, [§6](#6-the-universal-profiling-fold)), then spawns
`_run_attach_profile` (single window → finalize) or `_run_attach_monitor` (loop until Stop) as a daemon
thread.

### The convergence, and one important asymmetry

Both paths reach `end_run()` → `_finalize()`. The only difference is *who calls* it: launch calls it from
`POST /runs/{id}/end` (or from `_auto_finalize` when the process tree exhausts), while attach/monitor call it
themselves at the end of their profiler thread. This drives a subtle asymmetry: the **launch poller
auto-finalizes** when the tree disappears (`finalize_on_exhausted=True`), but the **attach poller does not** —
it only sets `stop_event`, leaving the profiler thread as the sole finalizer. That's deliberate (finalizing
while `perf.data` is still being written would corrupt the profile), but it's the opposite of launch
behavior. If an attach thread crashes, `_fail_run` guarantees the run still lands in `error` and emits
`run_ended`, so the UI never spins forever.

### The `_finalize` pipeline

`_finalize` runs synchronously on the finishing thread and does, in order:

1. **One streaming pass** over the trace log (`_tee`): every parsed event is written straight to
   `events.ndjson.zst` (the archive), while only the first `_MAX_ANALYZED_EVENTS = 1,000,000` are kept
   in memory for analysis. Full-stream counters (totals, top syscalls, errors) are computed regardless of the
   cap, so the summary stays honest even when truncated (a synthetic low-severity `analysis_truncated`
   anomaly is appended when it is).
2. **Metrics backfill**: bin syscall counts into metric timestamps to compute `syscall_rate`, write it back
   to SQLite and into `metrics.ndjson.zst`.
3. **Anomalies** — branches on `monitor` (see the monitor invariant, [§10](#10-live-monitor-and-the-incident-model)):
   non-monitor runs build a `RuleContext` and call `run_rules` + `run_custom_rules`; monitor runs derive
   anomalies from their incidents instead.
4. **Sampler-specific extensions**: fold `perf.data` into `flamegraph.json` and add `perf_anomalies`; for
   ltrace, build the malloc/leak `profile.json`; read `latency.json`/`requests.json` and add their anomalies
   — each only when the relevant collector ran and (for latency/requests/perf-anomalies) only for non-monitor
   runs, to avoid double-emitting what monitor already produced live.
5. **Curate** a bounded event subset (`_MAX_CURATED = 3000`: all anomaly evidence, all signals/exits,
   `execve*`, slow calls, non-library errors) into SQLite, back-fill each anomaly's `evidence_ids`, then
   write `meta.json` and the final `runs.finalize(... status=completed)`.

---

## 4. Reading a run: the analytics surface

The frontend derives a run's **tab set entirely client-side from `run.collector_config`** — no server
round-trip (`runViews(run)` in `frontend/src/components/RunView.tsx`). A plain launch-trace run
(strace+psutil) shows Overview, Timeline, Memory, CPU, I/O, Network, Processes, Syscalls, Logs, Files. An
attach+ebpf+requests run shows Overview, Incidents, Requests, Timeline, Memory, CPU, Flamegraph, Latency,
Files — and *skips* the syscall-derived tabs, because attach runs have no syscall stream. (Older runs with an
empty `collector_config` fall back to the full strace tab set for backward compatibility.)

Each detail tab is backed by a `GET /runs/{id}/<thing>` endpoint (`events`, `metrics`, `syscalls`, `io`,
`network`, `processes`, `logs`, `profile`, `flamegraph`, `offcpu-flamegraph`, `latency`, `gc-timeline`,
`requests`, `request-spans`, `incidents`, `ai-summary`, `files`, `file`). Every derived-artifact endpoint
returns a **fail-open stub** — `{"supported"/"available": false, "reason": "..."}` — when the artifact
doesn't exist, so the fail-open contract extends to the read side, not just capture. File reads are
path-traversal-guarded (`is_relative_to`) and capped at 256 KiB so a multi-GB `strace.log` never gets slurped
whole. The Logs tab reconstructs stdout/stderr **without teeing fds** — it parses `strace -e write=1,2` hex
dumps (`program_output.extract_output`), preserving the traced program's `isatty()`/stdio fidelity. (An
honest consequence: the Logs tab is empty for ltrace runs, which don't produce those dumps.)

### AI summaries (optional, LLM-backed)

A handful of those endpoints are backed by an optional LLM layer
([`backend/app/summarize.py`](../backend/app/summarize.py) + [`llm.py`](../backend/app/llm.py)) rather than a
deterministic aggregation. `summarize.py` builds a compact, **pre-digested** prompt — metrics peaks, a "what
happened when" event timeline, and the rule engine's anomalies, *never* the raw event stream — and
`llm.stream_chat` streams a completion from any **OpenAI-compatible chat-completions endpoint**. There are
three flavours:

- **Run summary** (`GET /runs/{id}/ai-summary`) narrates a single run; the result is **persisted to
  `ai_summary.md` and served from cache** thereafter unless it's `force`-regenerated.
- **Diff summary** compares two runs (A vs B) and states whether B is better, worse, or mixed.
- **Incident summary** is a short, synchronous per-incident explanation for monitor runs, correlating the CPU
  hot call path with the anomaly. It is gated by the **`continuous_summaries` toggle** (off by default),
  because it would otherwise fire the LLM on *every* incident.

The whole layer is **fail-open**: `stream_chat` never raises (every network/HTTP error becomes an
`{"type":"error", …}` chunk), and when no key is configured the run still completes — the rule engine's
plain-text anomaly descriptions already render in the Overview, so the LLM purely *adds* interpretation. The
API key lives only in the file-based secret store (§12), guarded by the **base_url exfiltration rule**:
changing `base_url` without simultaneously re-entering the key **clears** the stored key, so a key entered for
one host is never silently forwarded to a redirected endpoint.

---

## 5. The collector model

Three collectors, with a specific compatibility matrix baked into both the UI and `otrace`:

| Collector | What it is | Compatibility |
|---|---|---|
| `strace` | ptrace syscall tracer (`strace -f -T -ttt -e write=1,2`) | **ptrace-exclusive with ltrace** |
| `ltrace` | ptrace library-call tracer (`ltrace -S -f -ttt -T`; `-S` also logs syscalls) | **ptrace-exclusive with strace** |
| `perf` | independent CPU sampler (`perf record -g -F 999`) | runs alongside either |
| `psutil` | in-process resource poller (metrics timeline) | always available |

strace and ltrace both use ptrace and can't both attach to one process, so the frontend
(`useCollectors.ts`) enforces mutual exclusion client-side: turning one on forces the other off. **There is
no server-side guard** — `collector_config` is just a dict, and a caller hitting `/runs/start` directly could
set both. perf, when enabled, wraps *whichever* inner command, but only after a permission probe
(`perf record -o /dev/null -- true`) succeeds — a denied perf stays fail-open and the workload still runs.

The **psutil poller** (`trace/metrics.py::MetricsPoller`) samples every 250 ms on its own daemon thread,
using persistent `psutil.Process` objects so `cpu_percent()` deltas are meaningful across ticks. Summed CPU
across a tree can exceed 100% on multicore (intentional). It tracks per-pid cumulative I/O and folds a dying
child's last bytes into a monotonic scalar before retiring it, so a fork-heavy target never drops its running
I/O total to zero. A key subtlety is `descendants_only`: when *any* wrapper (strace/ltrace/perf) is present,
the reported pid is the *wrapper*, not the workload, so the root is excluded from metrics; a bare run or an
attach includes the root because it *is* the workload.

> **ltrace's real scope:** it only sees the main binary's PLT calls, which suits native C/C++/Rust programs,
> not interpreted ones (Python/Node/Java show no meaningful library calls). Choose it accordingly.

The parsers (`strace_parser.py`, `ltrace_parser.py`) are tolerant by design — an unrecognized line is
silently skipped, never raised — and handle `<unfinished ...>`/`<... resumed>` pairing, signals, and exits.
The ltrace parser is a superset: `name@SYS` becomes a `SYSCALL` event (so the syscall/I/O/Network aggregations
work unchanged) while a bare `name` becomes a `LIBCALL` event feeding the malloc/hotspot profile. `LIBCALL`
events are excluded from syscall-rate and rule analysis so they don't inflate rates or mislabel slow libcalls.

---

## 6. The universal profiling fold

Every profiler — regardless of runtime or output format — feeds a **single shared core**,
`perf.py::_fold_stacks`, which takes an iterable of `(root→leaf frame list, weight)` and produces a pruned
nested flame tree plus a hotspots table. When it sees zero samples it returns
`{"supported": false, "reason": "no samples (target idle, or too short a window)."}`. Each sampler plugs in a
format-specific folder:

| Format | Folder | Produced by |
|---|---|---|
| `perf script` text | `fold_perf_script` (via `build_flamegraph`) | native `perf` |
| Brendan-Gregg collapsed | `fold_collapsed` | py-spy raw, async-profiler, phpspy, bcc `offcputime -f` |
| speedscope JSON | `fold_speedscope` | rbspy, dotnet-trace |
| V8 `.cpuprofile` | `fold_cpuprofile` | Node/Deno/Bun via the V8 inspector |
| phpspy trace | `fold_phpspy` | phpspy |

The orchestrator's `_fold_profile(fmt, raw)` dispatches on the run's `profile_format`. (For speedscope it
even globs `*.speedscope.json` by mtime as a fallback, because `dotnet-trace` writes a sibling file whose
exact name varies.)

### The sampler registry: best available tool per runtime

`attach.py` detects the target runtime in two passes — first substring markers in `/proc/<pid>/maps`
(`libjvm.so`→jvm, `libcoreclr.so`→dotnet, `libpython`→python, `libnode`→node, …), then exe-basename markers
with strict version-suffix matching (so `node_exporter` is never misdetected as `node` and SIGUSR1-killed).
Then `_SAMPLERS` picks the profiler:

| Runtime | Tool | Format | Fallback if missing |
|---|---|---|---|
| python | `py-spy` (`--nonblocking --subprocesses`) | collapsed | perf (VM frames) |
| ruby | `rbspy` | speedscope | perf |
| jvm | `asprof` (async-profiler) | collapsed | perf |
| dotnet | `dotnet-trace` | speedscope | perf |
| php | `phpspy` | phpspy | perf |
| node | **V8 inspector, no external tool** (`node_cdp.py`) | cpuprofile | — |
| native / unknown | — | — | perf |

If the dedicated tool isn't installed, `profiler_plan` returns `None` and the run silently degrades to `perf`
(which shows VM/interpreter frames, not your app's functions — the row hint says so). `py-spy`'s
`--subprocesses` matters: attaching to a gunicorn/uWSGI *master* would otherwise fold to an idle master; this
captures the workers too.

> **Honest edges:** A `"go"` runtime id exists in the code but is unreachable — Go binaries classify as plain
> `native`. Deno/Bun are *detected* but deliberately excluded from the V8-inspector path because, unlike Node,
> they don't install a SIGUSR1→inspector handler (signaling them would terminate them) — they fall back to
> perf.

### node_cdp: the profiler that needs no tool

For Node, the target process *is* the profiler. `node_cdp.py` sends `SIGUSR1` to ask Node to open its
inspector, then speaks the Chrome DevTools Protocol over a **hand-rolled WebSocket client** (no `websockets`
dependency): `Profiler.enable` → `setSamplingInterval(200µs)` → `start` → window → `stop`, yielding a
`.cpuprofile`. Because `SIGUSR1`'s default disposition is *termination* and only Node installs the handler,
`capture()` refuses to signal anything that doesn't look like Node (exe basename or `libnode` in maps),
returning `"refusing to send SIGUSR1 (it would terminate a non-Node process)."`. It also correlates the
target's *own* listening inspector port, so it never attaches to a different Node process holding 9229.

---

## 7. The eBPF subsystem

eBPF (`backend/app/ebpf.py`) adds off-CPU flamegraphs, run-queue/block-I/O latency histograms, and Python GC
timelines. It is entirely capability-gated and fail-open.

### Capability gating

`capabilities(refresh=False)` (backs `GET /runs/attach/ebpf-capabilities`, TTL-cached 60s) requires **all
three** of:

1. **Kernel BTF** — `/sys/kernel/btf/vmlinux` exists (CO-RE needs it).
2. **Core bcc tools** — `offcputime`, `runqlat`, `biolatency` resolvable (`biosnoop`/`pythongc` are optional
   and don't gate).
3. **Privilege** — root, *or* `CAP_BPF`+`CAP_PERFMON` (bits 39/38 in `/proc/self/status` `CapEff`), *or*
   passwordless sudo — probed as `sudo -n <the actual bcc tool> -h`, not a generic `sudo -n true` (which would
   false-positive on unrelated NOPASSWD rules and false-negative on a tool-path-only sudoers).

Each unmet requirement has a precise reason string surfaced verbatim in the Attach modal. One myth is called
out explicitly in the code and the reason text:

> **`kernel.unprivileged_bpf_disabled=0` does NOT unlock eBPF tracing.** It only ever permitted
> socket-filter/cgroup program types for unprivileged users — never the kprobe/tracepoint/perf programs
> OpenTrace loads. It's reported for information but never grants `priv_ok`.

### bpftrace (CO-RE) preferred over bcc on new kernels

On very new kernels, bcc's bundled headers fail to compile most tools (`runqlat`/`biolatency`/`biosnoop`/
`pythongc` all hit a `struct filename static_assert` on kernel 7.0); only `offcputime` reliably survives. So:

- **`offcputime` always runs on bcc** (folded off-CPU stacks → `offcpu-flamegraph.json`).
- **`bpftrace` is the preferred engine** for the latency histograms and GC when available
  (`bpftrace_available()`, TTL-cached). The run-queue latency, block-I/O latency, and optional GC USDT probes
  run as **one combined bpftrace program**, not three — concurrent CO-RE compiles wedge each other, so a
  single program avoids that.

Both engines' histograms feed a shared `{unit, buckets, total, p50, p90, p99, max}` shape so
`latency_anomalies` never special-cases which engine produced the data. Percentiles are bucket-upper-bound
estimates (log2 buckets are coarse — "honest for latency").

### The no-`-p PID` rule

A hard-won invariant repeated in three places in the code:

> **Never pass `-p PID` to a bpftrace program here.** bpftrace's `-p` applies an *implicit pid filter to
> every probe in the script*, which silently kills the system-wide `sched:*`/`block:*` tracepoints that
> run-queue/block-I/O/off-CPU decomposition depend on. Per-target scoping is done with an in-script
> `/pid == PID/` filter (for USDT) or full-path uprobe symbols (already process-scoped) instead.

### Process-management hazards

Two rules keep verbose eBPF children from wedging the backend:

- `_run_proc` captures stdout/stderr to `tempfile.TemporaryFile()`, **never a `subprocess.PIPE`** — an
  undrained 64 KB pipe buffer deadlocks a chatty bpftrace child.
- `_force_kill` never bare-`SIGKILL`s a sudo frontend (that orphans the root child, which keeps tracing).
  It snapshots the child pid, `SIGTERM`s the frontend (sudo relays it), waits, then best-effort
  `sudo -n kill -KILL <child>` on any survivor — logging a warning rather than erroring if a least-privilege
  sudoers denies the kill.

Latency anomaly thresholds: run-queue p99 ≥10ms (medium)/≥50ms (high); block-I/O p99 ≥20ms/≥100ms, with the
anomaly text explicitly noting that `biolatency`/`block_io` is **host-wide, not per-PID** (only the
`biosnoop`-derived `block_io_pid` is per-target). GC tracing is Python-only and needs real USDT probes,
checked via `readelf -n` on the mapped libpython — conda/statically-linked CPython and Node ship none, and
say so.

---

## 8. The rule engine

Findings come from a registry of rules (`backend/app/rules/engine.py`), each a pure
`fn(ctx: RuleContext) -> Anomaly | None` registered by a `@_needs("events")` or `@_needs("metrics")`
decorator that stamps which signal it consumes.

### Signal gating — the reason attach/monitor runs behave sanely

`run_rules` skips any rule tagged `events` when `ctx.events` is empty, and any rule tagged `metrics` when
`ctx.metrics` is empty (and any rule in `ctx.disabled_rules`). This is the fix for a real failure mode:
attach/monitor runs carry a metrics timeline but **no syscall stream**, so an events-only rule would misfire
on absence-of-data — an events-gated rule like `connection_error` simply doesn't run on an eventless attach
run, while a metrics-gated rule like `fd_count_growing` still does. Each rule call is wrapped in
`try/except: continue` — one throwing rule can never sink the whole pass.

Signal gating is deliberately coarse, though, and it can't separate two rules that both consume `metrics` but
are meant to fire on *different* run types. `cpu_bound_no_syscalls` and `cpu_bound_metric` are a case in point:
**both are decorated `@_needs("metrics")`** — they are *not* split by the events/metrics signal. What
distinguishes them is a finer signal carried *inside* the metrics stream, `syscall_rate` (backfilled only on
launch runs, from strace). `cpu_bound_no_syscalls` filters to the rows that *have* `syscall_rate`, so it is
the launch-run variant; `cpu_bound_metric` no-ops the moment `_has_syscall_rate(rows)` is true, so it fires
only on attach/monitor runs (where `syscall_rate` is absent). That presence check — not the decorator — is
what keeps the pair from ever double-firing.

Currently ~two dozen built-in rules are registered (roughly 15 events-gated + 8 metrics-gated). Severity
scores land in `[base, base+9.99]` per band (`_SEV_BASE`: critical 90 / high 70 / medium 45 / low 20), so an
occurrence bonus never crosses into the next band.

### Thresholds and RULE_META (derived, not hand-maintained)

`RuleThresholds` is a dataclass of ~30 tunables; `config.tracing.rule_thresholds` supplies a sparse override
merged over the defaults (both `_rule_thresholds()` and `_disabled_rules()` fail open to plain defaults on a
corrupt config). Crucially, **`RULE_META` is auto-derived**: for each registered rule it reads
`fn.__name__` (the `rule_id`), the first docstring line, and regex-scrapes `ctx.thresholds.<name>` references
out of `inspect.getsource(fn)` — so the Settings→Rules UI can never drift out of sync with the code. Don't go
looking for a static registry to edit when you add a rule; annotate the function and the metadata follows.

The `/rules` REST surface (`rules_api.py`) is **read/validate/persist only — it never evaluates a rule
against real data** (that happens in the orchestrator). One nuance worth knowing: unknown threshold keys are
handled at two strictnesses — `RuleThresholds.from_overrides` silently drops them (engine layer), while
`PUT /rules/builtin/{id}` 400s on a threshold name not in that rule's own `RULE_META` list (API layer).

### Custom rules and the safe-eval sandbox

Users can add custom boolean-expression rules over per-signal field whitelists
(`EVENT_FIELDS`/`METRIC_FIELDS`). The sandbox (`rules/safe_eval.py`) is a **whitelist, not a blacklist**: only
comparisons, boolean logic, arithmetic, and `in` over the listed names are allowed. `Call`, `Attribute`,
`Subscript`, comprehensions, lambdas, f-strings — all forbidden, which closes the classic
`().__class__.__bases__...` escapes (they all need `Call`/`Attribute`). Expressions are capped at 500 chars
and evaluated with `{"__builtins__": {}}`. Events-mode rules fire once `min_count` rows match; metrics-mode
rules fire when the predicate holds over a *contiguous* run of samples spanning `duration_ms`. A rule whose
expression later stops validating (e.g. a renamed field) is silently skipped at run time — the intended catch
point is `POST /rules/custom/validate`, which the Settings UI calls (debounced) on every keystroke.

> **Two honest caveats:** custom-rule `severity` is a free-text column, not an enum — a typo silently maps to
> the "low" base. And request-tracing anomaly scores (below) use hardcoded `0.4`/`0.5`/`0.55` values, *not*
> the `_score` scale — a real mismatch the code flags as "a shared protocol deferred to the request-tracing
> phase."

---

## 9. Request tracing

The "deepen the why" feature attributes an HTTP server's per-endpoint latency to its downstream queries,
attach-only, via bpftrace ([`backend/app/ebpf.py`](../backend/app/ebpf.py) request-tracing section +
`aggregate.py`).

### A deliberately weaker capability gate

`request_capabilities()` (backs `GET /runs/attach/request-capabilities`) gates on `bpftrace_available()`
**alone** — explicitly *not* the full eBPF-suite gate. The syscall-tracepoint + libpq-uprobe path needs
neither kernel BTF nor bcc tools, so gating on the suite flag "would fail closed on exactly the boxes where
request tracing still works." It's toggled by the independent `requests` flag on the attach request.

### Capture → rollup → breakdown

1. **Capture.** `build_request_bt` assembles one dedicated bpftrace program (again, never `-p PID`): HTTP
   boundary tracepoints, TLS plaintext recovery via `SSL_read`/`SSL_write` (or the `_ex` variants — only the
   symbols the target's libssl actually exports, since probing an absent symbol aborts the whole program),
   libpq DB spans (`PQsendQuery*` only, never `PQexec`, to avoid double-counting), MySQL/SQLite spans, and an
   off-CPU decomposition scoped to threads that are actively serving a request. All library resolution is
   per-target and fail-open.
2. **Rollup.** `aggregate.request_rollup` correlates spans and writes `requests.json` with the same shape as
   `latency.json`: a per-endpoint RED table (`endpoint_stats`, routes templatized to `/users/{id}`) plus the
   slowest ~50 sampled spans with their nested DB children.
3. **Breakdown.** When off-CPU intervals are present, `correlate_breakdown` writes each request a
   `{on_cpu_ms, runq_ms, db_wait_ms, other_off_ms}` split that **sums to the request's wall duration** by
   construction (thread-per-request model). DB time is modeled as an *overlay* on off-CPU network wait, never
   a fifth additive bucket — which is why an in-process SQLite query (runs on-CPU) can show `db_ms` exceeding
   its `db_wait_ms`.

`correlate_spans` uses a **single-owner rule** (the innermost/latest HTTP span on the same tid whose window
contains the DB span) so coroutine/greenlet servers multiplexing requests onto one OS thread don't
double-attribute DB time past 100%. Curated slow/errored spans (`status ≥ 500` or `dur ≥ endpoint p95`) are
the *only* place request data becomes epoch-time-correlatable with metrics — monotonic `nsecs` are converted
via a `(mono0, wall0)` anchor captured back-to-back at bpftrace launch, then stored as `event_type='request'`
rows.

The Requests tab (`RequestsTab.tsx` / `RequestWaterfall.tsx`) renders the RED table and a waterfall of
sampled slow requests; each waterfall row can drill into that request thread's off-CPU flamegraph via
`GET /runs/{id}/offcpu-flamegraph?tid=<tid>`. A live monitor run updates the tab in place from the
`request_rollup` SSE payload without polling.

> **Honesty on request tracing:** Postgres (libpq → system `libpq.so`) and in-process SQLite are validated
> end-to-end (kernel 7.0.14, bpftrace 0.24.2). **MySQL/MariaDB spans are symbol-correct and unit-tested but
> not live-validated** (no mysqld on the dev box). Statically-bundled `psycopg2-binary`, `asyncpg`, and
> pure-wire drivers map no dynamically-linked client, so DB spans are unavailable (endpoint timings still
> work) — with an explicit reason. **HTTP/2 and gRPC are structurally unsupported** on mid-stream attach:
> HPACK is a stateful codec whose dynamic table was built by frames never observed.

---

## 10. Live monitor and the incident model

A `monitor` attach run loops: repeated bounded profiling snapshots + a metric-only sliding-window rule scan
over the trailing ~90s (`_SLIDING_N = 360` samples) + a long-horizon slow-leak check the 90s window can't
see, until you Stop (`POST /runs/{id}/stop`). Each finding becomes an **incident**.

### Incidents collapse by rule

Incidents **collapse by rule** — one row per `rule_id` with an occurrence `count`, not one row per re-fire
(`_make_incident`). A repeat within a 10s throttle window just increments the count in memory; the leading
metrics window is embedded only on first occurrence (re-embedding ~19 KB of samples every 10s would bloat
`incidents.ndjson`). Live alerts (`fd_leak_live`, `mem_spike`, `cpu_hot_live`) re-arm via hysteresis
(`alerts_fired` latch + `alert_cooldown` counter), so a genuine re-occurrence fires again instead of latching
silent forever. (Not all three are symmetric: `mem_spike` emits unconditionally and relies on the incident
collapse for de-duplication, while the other two use the `once()`/`rearm()` latch.)

### The monitor invariant

This is the load-bearing rule to preserve when touching monitor code:

> **For a monitor run, Overview "Top Findings" are derived from the incidents, so the Overview and Incidents
> tabs can never disagree.** `_finalize` explicitly does **not** run the whole-history rule engine for a
> monitor run — it derives persisted anomalies from `_incidents_to_anomalies(read_incidents(...))`. (A full-
> session scan over a long-lived process is misleading: baseline drift flags spurious growth.) The frontend
> mirrors this *during* the run, deriving live Overview findings from the incident SSE store rather than the
> not-yet-written finalized anomalies.

Legacy pre-collapse incident files (one row per re-fire, each embedding a full metrics window) are compacted
once, lazily, on read for finished runs — idempotent thereafter, and dead weight for any run created after
the collapse feature landed.

---

## 11. Container awareness

`backend/app/container.py` labels and resolves containerized targets by **pure `/proc` parsing — no root, no
docker/podman socket**. `container_info(pid)` matches `/proc/<pid>/cgroup` against ordered docker/podman/
containerd/cri-o/kubernetes patterns (cgroup v1 and v2), returning the runtime, short/full id, and pod UID —
fail-open to a non-container shape on any error. `resolve_host_pid(local_pid, container_id?)` brute-force
scans `/proc/*` for an `NSpid` whose innermost entry matches a container-local pid (O(n), no index), backing
`POST /runs/attach/resolve`. `cgroup_limits(pid)` reads CPU quota and memory limit (v2 `cpu.max`/`memory.max`
or v1 fallbacks, treating the near-2⁶³ sentinel as unlimited) and stamps them onto the run so the cgroup-aware
rules (`cpu_throttled`, `rss_near_cgroup_limit`) threshold against the *container's* quota rather than host
cores.

> One security nuance: `/runs/attach/resolve` can return target info for any container-local pid regardless of
> caller uid, but actually *attaching* to profile it is still blocked by the same-user uid gate ([§3b](#3b-attach-profile-an-already-running-process-for-a-bounded-window))
> unless the backend runs as root.

---

## 12. Two cross-cutting principles

### Fail-open, everywhere

A missing or denied tool — `perf`, a language sampler, an eBPF tool, an LLM key — **must never break a run**.
It completes with whatever it could collect (at minimum the psutil timeline) plus a friendly `reason` string
surfaced in the relevant tab. This shows up at every layer:

- `otrace` runs the command untraced if `curl`/the backend/a tracer is unavailable, and always exits with the
  command's real status.
- Parsers skip unrecognized lines rather than raising; a missing trace log is treated as zero events.
- Every rule call, custom-rule compile, and capture thread is wrapped so one failure can't sink the run;
  capture threads even re-check `runs.get(id)` before writing, so a mid-capture delete degrades cleanly.
- Every derived-artifact endpoint returns a `{"supported"/"available": false, "reason": "..."}` stub instead
  of a 404 or a 500.
- `_ensure_flamegraph_reason` guarantees the Flamegraph tab always carries *some* explanation even when no
  profile was produced.

The exact degradation reason strings are catalogued in the failure-modes reference; representative examples:
`"perf attach denied — raise privileges (sudo sysctl kernel.perf_event_paranoid=1, or grant CAP_PERFMON)."`,
`"kernel BTF missing (/sys/kernel/btf/vmlinux) — CO-RE eBPF unavailable; use a BTF-enabled kernel."`,
`"{profiler} is not installed — captured the resource timeline only."`.

### Local-only security

OpenTrace binds to localhost and defends that boundary with three layers (`backend/app/main.py`, added so
they execute **outermost-first**: token → local-only → CORS → routes):

1. **`ApiTokenMiddleware`** — a per-launch bearer token. Electron generates 32 random bytes with
   `crypto.randomBytes(32)` **only when it spawned its own backend child**, passes it via
   `OPENTRACE_API_TOKEN`, and the middleware then requires it on every request except `OPTIONS` and
   `GET /health` (accepting `Authorization: Bearer` *or* a `?token=` query param, since `EventSource` can't
   set headers; compared with `secrets.compare_digest`). **When the env var is unset — a manual `uvicorn` run,
   the test suite, an isolated dev/e2e backend, or a reused/external backend — the middleware is a complete
   no-op**, which is the documented unauthenticated local workflow.
2. **`LocalOnlyMiddleware`** — rejects (403) any request whose `Origin` isn't `null`/`file://`/localhost or
   whose `Host` isn't localhost-shaped. CORS alone is insufficient because a "simple" cross-origin request
   still executes server-side even when the browser withholds the response (CSRF-from-open-tab); the Host
   check is a DNS-rebinding guard.
3. **`CORSMiddleware`** with the same local-origin regex and `allow_credentials=False`.

The **LLM API key lives only in a file-based secret store** (`~/.opentrace/secrets/`, dir `0700`, files
`0600`, path-traversal-guarded), never in `config.json` or git — only the fixed secret *name* is persisted.
An exfiltration guard in `llm.py`: changing `base_url` without simultaneously supplying a new key **deletes**
the stored key, because a key entered for one host must never be silently forwarded to a redirected base URL
(changing `model` alone does not trigger this). `stream_chat` never raises — every network/HTTP error becomes
a `{"type":"error","message":...}` chunk (fail-open again).

---

## 13. Honesty: what is and isn't live-validated

| Area | Status |
|---|---|
| strace/ltrace/perf launch tracing, psutil metrics | Core, exercised by the backend pytest suite (20 modules) and the Electron e2e suite (175 scenarios; see [`docs/testing.md`](./testing.md)). |
| Attach + per-runtime samplers (py-spy, rbspy, async-profiler, Node V8 inspector) | Implemented; Python/Ruby/JVM/Node exercisable locally. |
| `.NET` (`dotnet-trace`) and PHP (`phpspy`) samplers | Implemented, **not live-validated** in the dev environment (need a .NET app / php-fpm). |
| eBPF off-CPU (bcc `offcputime`) | Works where BTF + bcc + privilege exist. |
| eBPF latency/GC via bpftrace | Preferred engine on new kernels; environment-dependent (documented kernel-version caveats). |
| Request tracing — Postgres/libpq + in-process SQLite | **Validated end-to-end** (kernel 7.0.14, bpftrace 0.24.2). |
| Request tracing — MySQL/MariaDB spans, TLS `_ex` recovery | Symbol-correct + unit-tested, **not live-validated**. |
| HTTP/2 / gRPC request tracing | **Structurally unsupported** on attach (stateful HPACK). |
| Go runtime detection | Falls through to `native` — the `"go"` id is effectively dead code. |
| Packaged installers (`.deb`/`.AppImage`) | **Not done** — run from source. |

---

## Where to go next

- **File-by-file map** and end-to-end runtime flow: [`docs/structure.md`](./structure.md)
- **Install, build, run:** [`README.md`](../README.md), [`docs/SETUP.md`](./SETUP.md)
- **Day-to-day usage:** [`docs/USAGE.md`](./USAGE.md)
- **When something degrades or a tool is missing:** [`docs/TROUBLESHOOTING.md`](./TROUBLESHOOTING.md)
- **Manual test playbook** (per-feature workloads + expected results): [`docs/testing.md`](./testing.md)
- **Product spec + roadmap:** [`docs/OpenTrace_Roadmap.md`](./OpenTrace_Roadmap.md),
  [`docs/Profiling_Roadmap.md`](./Profiling_Roadmap.md),
  [`docs/Request_Tracing_Roadmap.md`](./Request_Tracing_Roadmap.md)