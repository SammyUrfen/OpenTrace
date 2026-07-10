# OpenTrace

**A local-first observability tool for Linux — an intelligent magnifying glass for
running software.** OpenTrace watches what a program actually *does* at the system
level — syscalls, memory, file & network I/O, CPU and off-CPU time, GC, scheduler and
disk latency, and per-request HTTP/DB timing — and turns those low-level signals into
**correlated, explained findings**, so you understand behaviour instead of squinting at
raw tool output.

It runs as a self-contained Electron desktop app. Everything stays on your machine.

---

## Why

Understanding a misbehaving process on Linux normally means juggling a fistful of tools
by hand — `strace`, `ltrace`, `lsof`, `htop`, `perf`, `py-spy`, `bpftrace` — each with
its own invocation, output format, and mental model, none of which talk to each other.
You end up correlating timestamps across five terminals to answer a simple question:
*what did this thing do, and why was it slow / leaking / stuck?*

OpenTrace collapses that into one place. It captures the signals, **correlates them on a
shared timeline**, runs a **rule engine** that flags anomalies in plain English (fd
leaks, memory growth, CPU-bound loops, slow syscalls, lock contention, slow disk, slow
endpoints…), and can hand the whole picture to an LLM for a readable summary. You get
*what happened, when, where in the code, and a plausible why* — not just numbers.

## When to use it

- A service is **leaking memory or file descriptors** and you want to see the growth
  and where it comes from.
- An endpoint or job is **slow** and you need to know whether it's burning CPU, or
  *blocked* on I/O, a lock, the DB, or the scheduler (on-CPU sampling can't tell you —
  OpenTrace's off-CPU profiling can).
- An HTTP service has a **slow endpoint** and you want a per-endpoint latency table plus
  a per-request breakdown of where the time actually went — including its DB queries.
- You want to **profile a program you already have running** (a dev server, a
  Dockerized backend) **without restarting it**, and see real function names.
- You changed something and want a **before/after diff** — "better or worse?".
- You want a low-friction, GUI-driven alternative to memorising a dozen CLI tools.

## What it does

OpenTrace has several ways to get signal, all feeding one analysis + visualization
pipeline.

**1. Trace commands you run.** Flip tracing **ON** and use the embedded terminal
normally. A zsh shell hook transparently rewrites a foreground command so it runs under
`strace` (or `ltrace`) plus an optional `perf` sampler and a `psutil` resource poller —
exit codes, Ctrl-C, and quoting all behave as usual. The finished command is saved as a
**run** you can open, analyze, and compare. (Bash gets an opt-in `ot <cmd>` helper rather
than transparent rewriting — a readline limitation, documented honestly in the hook.)

**2. Attach to a running process.** Point OpenTrace at any live PID (or a listening
port) and it profiles it for a bounded window — picking the best available **per-runtime
profiler** for real application symbols:

| Runtime | Profiler | Status |
|---|---|---|
| Python | `py-spy` → real Python frames | verified |
| Node.js | built-in **V8 inspector** (SIGUSR1 → CDP, no install, no restart) | verified (Node) |
| JVM | `async-profiler` (`asprof`) | supported |
| Ruby | `rbspy` | supported |
| .NET / PHP | `dotnet-trace` / `phpspy` | implemented, not live-validated here |
| native / Go / Deno / Bun | `perf` (real symbols via frame pointers) — Deno and Bun fall back here since a SIGUSR1 inspector attach would terminate them | verified |

All of them fold into one flamegraph view. Missing a profiler? It **fails open** to a
psutil resource timeline with a clear reason — a run is never lost. Attach is same-user
by default (root can attach to anything), capped at 16 concurrent contexts, with the
window clamped to 3–120s.

**3. Live monitor + deep kernel signals (eBPF).** Attach in **monitor** mode to keep a
running service under continuous watch: repeating profiling snapshots + sliding-window
rule scans produce an **Incident feed** — each anomaly captured with *when · what · where
(hot call path) · leading metrics · optional AI note*, collapsed by rule with an
occurrence count. Opt into **eBPF** for what sampling fundamentally can't see:

- **Off-CPU flamegraph** — where the process is *blocked* (I/O, locks, DB, sleeps).
- **Latency histograms** — scheduler run-queue latency (CPU oversubscription) and
  block-I/O latency (slow/contended disk).
- **GC pauses** — Python stop-the-world times via USDT.

eBPF is capability-gated and fail-open; on very new kernels it uses **bpftrace/CO-RE**
where the bundled bcc tools won't compile. Container-aware: it labels Docker / Podman /
containerd / CRI-O / k8s targets and resolves in-container PIDs to host PIDs, all from
`/proc` (no root, no Docker socket).

*Validated on:* OpenTrace is developed and validated on Fedora with a recent kernel
(7.0.x) and bpftrace 0.24.2. The eBPF off-CPU / latency / GC features and request tracing
need a recent kernel plus privilege — `CAP_BPF`+`CAP_PERFMON` on the backend, root, or
passwordless sudo to the eBPF tools. (`unprivileged_bpf_disabled=0` alone does not suffice
for tracing programs.)

**4. Request tracing (HTTP endpoints + DB).** Tick **Request tracing** on an attach to
turn a plaintext HTTP/1.x server into a request-level view (bpftrace uprobes/tracepoints;
TLS plaintext is recovered via `libssl`). You get:

- a per-endpoint **RED table** (Rate / Errors / Duration — count, p50/p95/p99, err%, and
  the share of time spent in the DB), routes templatized so `/users/123` and
  `/users/456` collapse to `/users/{id}`;
- a per-request **waterfall** over the slowest sampled requests, each with an
  **on-CPU / run-queue / DB-wait / other-off-CPU** breakdown that sums to the wall time,
  its captured (PII-scrubbed) SQL statements, and a **span → off-CPU-flamegraph drill**
  showing exactly where *that request's thread* blocked.

DB time is attributed via `libpq` (Postgres), `libmysqlclient`/`libmariadb` (MySQL —
symbol-correct + unit-tested, not live-validated here), and `libsqlite3` (SQLite). If the
target statically bundles its driver (e.g. `psycopg2-binary`, `asyncpg`), endpoint
timings still show with an honest "DB spans unavailable" note. This gate is deliberately
*weaker* than the full eBPF suite (it needs only bpftrace + privilege, not kernel BTF or
bcc), so request tracing works on boxes where off-CPU/latency does not.

**Making it readable.** Every run opens as a tab with analytics views, derived from which
collectors actually ran: **Overview** (snapshot + ranked anomaly cards + streaming **AI
summary**), **Timeline / Memory / CPU** (with leak banners + p50/p90 lines), **I/O**,
**Network**, **Processes**, **Syscalls** (sortable P50/P95/P99), **Logs**, **Flamegraph**
(on-CPU / off-CPU), **Latency**, **Incidents** (monitor runs), **Requests** (request
tracing), and **Files**. Right-click two runs → **Compare** for an A↔B diff with a
streaming "what changed, better or worse?" summary. Runs are grouped into **sessions**
(projects). See **[docs/USAGE.md](docs/USAGE.md)** for a walkthrough of each.

**Tune the rule engine.** In **Settings → Rules** you can enable/disable and re-tune any
of the ~25 built-in rules (each threshold editable inline; rules are *signal-gated*, so
attach/monitor runs run the metric-only rules and syscall runs run the event rules). You
can also **author custom rules** as a sandboxed boolean expression over event fields
(`syscall == 'openat' and error == 'ENOENT'`) or metric fields
(`cpu_pct > 90 and syscall_rate < 5`) — validated live as you type, evaluated in a
whitelist AST sandbox (no calls, no attribute access), applied to every run.

## How it works

Three processes: an **Electron** shell (window + terminal + shell hooks), a **FastAPI**
backend (the tracing engine + storage + analysis), and a **React 19 / Vite** renderer.
They talk over REST + Server-Sent Events, so the UI is live during a run. The data model
is `sessions → terminals → runs`, with every analytical table hanging off a `run_id`;
runs keep a complete compressed record on disk (`events.ndjson.zst`, `metrics.ndjson.zst`)
plus a curated slice in SQLite.

A guiding principle is **fail-open**: a missing tool, denied privilege, or absent LLM key
degrades gracefully with an explanation, never a broken run.

**Security model — local single-user by design.** The backend accepts local callers
only: a `LocalOnlyMiddleware` returns `403` for any request whose `Origin` or `Host`
isn't localhost/`file://` (a DNS-rebinding guard beyond CORS). When Electron spawns its
own backend it also generates a **per-launch bearer token** and threads it to the
renderer and shell hooks; the backend then requires `Authorization: Bearer <token>` (or a
`?token=` query param for SSE) on every request except `OPTIONS`/`GET /health`. That token
middleware is a deliberate **no-op** when unset — a manual `uvicorn`, the test suite, and
isolated dev/e2e backends run unauthenticated over localhost. The LLM API key lives only
in a file-based secret store (`~/.opentrace/secrets/`, mode `0600`) — never in
`config.json` or git; pointing the LLM `base_url` at a new host without re-entering the key
clears the stored key (an exfiltration guard).

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the three-process design and the
capture→analysis pipeline, and **[docs/structure.md](docs/structure.md)** for the
module-by-module map.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Prerequisites, install, build/run, dev mode, environment variables |
| [docs/USAGE.md](docs/USAGE.md) | Tracing, attach, monitor, request tracing, reading each tab, authoring rules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Three-process design, data model, the analysis pipeline, fail-open principles |
| [docs/structure.md](docs/structure.md) | File-by-file responsibility map (backend, electron, frontend) |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Tool/privilege requirements, capability gates, and the fail-open reason strings |
| [docs/testing.md](docs/testing.md) | Copy-pasteable manual test playbook for every feature |
| [docs/OpenTrace_Roadmap.md](docs/OpenTrace_Roadmap.md) | Product spec + phase status |

## Quick start

OpenTrace targets **Linux**. The backend runs in a conda env (`opentrace-dev`, Python
3.11+) — any Python 3.11+ env with the backend installed works too (`start.sh` probes
the interpreter; override with `OPENTRACE_PYTHON`). The desktop shell is Electron + Node.

**Prerequisites:** Node.js **>= 22.12** with npm (required by Electron 42 and
`@electron/rebuild` 4), plus a C/C++ toolchain for the node-pty native build —
node-pty ships no Linux prebuilds, so it always compiles from source here
(Fedora: `sudo dnf install gcc-c++ make`; Debian/Ubuntu: `sudo apt install
build-essential`). node-gyp's Python requirement is covered by the activated
conda env.

```bash
# backend deps (once), in the conda env
conda create -n opentrace-dev python=3.11 && conda activate opentrace-dev
pip install -e backend

# build the renderer + electron deps, then launch
conda activate opentrace-dev
./start.sh
```

`start.sh` builds the frontend if needed and launches the app; data lives under
`~/.opentrace/` (override with `OPENTRACE_HOME` for a manual backend — note `start.sh`
itself pins a dev-local `tmp-opentrace/`). Set `OPENTRACE_DEV=1` to use the Vite dev
server instead of the built assets. Full details in **[docs/SETUP.md](docs/SETUP.md)**.

**Optional tools unlock deeper profiling** (all detected + fail-open if absent):
`py-spy` / `rbspy` / `asprof` / `dotnet-trace` / `phpspy` for per-runtime attach, and
`bcc-tools` + `bpftrace` (with `CAP_BPF`+`CAP_PERFMON`, root, or passwordless sudo) for
eBPF off-CPU + latency and for request tracing. AI summaries need an OpenAI-compatible
key (default Google Gemini/Gemma). If something is denied or missing, the relevant tab
tells you exactly why and what to install — see **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.

## Repository layout

```
backend/    FastAPI server + tracing engine (Python 3.11+; strace/ltrace/perf,
            attach + sampler registry, eBPF, request tracing, rules, aggregation, LLM)
frontend/   React 19 + Vite + TypeScript renderer (analytics tabs, diff, live monitor)
electron/   Electron main process + node-pty terminal + transparent shell hooks
e2e/        Playwright-Electron scenario harness (drives the real app; 175 scenarios)
docs/       Architecture, setup, usage, troubleshooting, structure map, testing playbook
test-files/ Demo workloads (leak/fd-leak/cpu; paired v1/v2 fixtures for the diff view)
```

## Testing

```bash
cd backend && python -m pytest -q          # backend unit + pipeline tests (20 modules)
cd frontend && npm test && npm run build   # renderer tests + typecheck/build
cd e2e && npm install && ./run-all.sh      # end-to-end UI scenarios (drives the app)
```

The `e2e/` harness launches the real Electron app in full isolation (its own backend on a
spare port + throwaway data) and runs **175 scenarios** in parallel waves; the registry is
authoritative, not a hardcoded count. See `e2e/README.md` and
**[docs/testing.md](docs/testing.md)** for the manual playbook.

## Status

The full loop works end-to-end: trace or attach → analytics + flamegraphs → AI summary →
diff, plus live monitoring with incidents, eBPF off-CPU/latency/GC, and request tracing
(per-endpoint RED + per-request waterfall/breakdown/drill). Postgres/SQLite request
tracing is live-validated; MySQL spans and the .NET/PHP attach samplers are implemented
but not yet validated in this environment. Not yet done: **packaged installers**
(`.deb`/`.AppImage`) — for now run from source via `start.sh`.

Linux only, single machine, single user. Some features need external tools and (for eBPF
and request tracing) elevated privileges — all optional and fail-open.