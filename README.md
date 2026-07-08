# OpenTrace

**A local-first observability tool for Linux — an intelligent magnifying glass for
running software.** OpenTrace watches what a program actually *does* at the system
level — syscalls, memory, file & network I/O, CPU and off-CPU time, GC, scheduler and
disk latency — and turns those low-level signals into **correlated, explained findings**,
so you understand behaviour instead of squinting at raw tool output.

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
leaks, memory growth, CPU-bound loops, slow syscalls, lock contention, slow disk…), and
can hand the whole picture to an LLM for a readable summary. You get *what happened,
when, where in the code, and a plausible why* — not just numbers.

## When to use it

- A service is **leaking memory or file descriptors** and you want to see the growth
  and where it comes from.
- An endpoint or job is **slow** and you need to know whether it's burning CPU, or
  *blocked* on I/O, a lock, the DB, or the scheduler (on-CPU sampling can't tell you —
  OpenTrace's off-CPU profiling can).
- You want to **profile a program you already have running** (a dev server, a
  Dockerized backend) **without restarting it**, and see real function names.
- You changed something and want a **before/after diff** — "better or worse?".
- You want a low-friction, GUI-driven alternative to memorising a dozen CLI tools.

## What it does

OpenTrace has three ways to get signal, feeding one analysis + visualization pipeline.

**1. Trace commands you run.** Flip tracing **ON** and use the embedded terminal
normally. A shell hook transparently rewrites a foreground command so it runs under
`strace` (or `ltrace`) plus an optional `perf` sampler and a `psutil` resource poller —
exit codes, Ctrl-C, and quoting all behave as usual. The finished command is saved as a
**run** you can open, analyze, and compare.

**2. Attach to a running process.** Point OpenTrace at any live PID (or a port) and it
profiles it for a bounded window — picking the best available **per-runtime profiler**
for real application symbols:

| Runtime | Profiler | Status |
|---|---|---|
| Python | `py-spy` → real Python frames | verified |
| Node / Deno / Bun | built-in **V8 inspector** (SIGUSR1 → CDP, no install, no restart) | verified |
| JVM | `async-profiler` (`asprof`) | supported |
| Ruby | `rbspy` | supported |
| .NET / PHP | `dotnet-trace` / `phpspy` | implemented |
| native / Go | `perf` (real symbols via frame pointers) | verified |

All of them fold into one flamegraph view. Missing a profiler? It **fails open** to a
psutil resource timeline with a clear reason — a run is never lost.

**3. Live monitor + deep kernel signals (eBPF).** Attach in **monitor** mode to keep a
running service under continuous watch: repeating profiling snapshots + sliding-window
rule scans produce an **Incident feed** — each anomaly captured with *when · what · where
(hot call path) · leading metrics · optional AI note*. Opt into **eBPF** for what
sampling fundamentally can't see:

- **Off-CPU flamegraph** — where the process is *blocked* (I/O, locks, DB, sleeps).
- **Latency histograms** — scheduler run-queue latency (CPU oversubscription) and
  block-I/O latency (slow/contended disk).
- **GC pauses** — Python stop-the-world times via USDT.

eBPF is capability-gated and fail-open; on very new kernels it uses **bpftrace/CO-RE**
where the bundled bcc tools won't compile. Container-aware: it labels Docker/Podman/
containerd/k8s targets and resolves in-container PIDs to host PIDs, all from `/proc`.

**Making it readable.** Every run opens as a tab with analytics views — **Overview**
(snapshot + ranked anomaly cards + streaming **AI summary**), **Timeline / Memory / CPU**
(with leak banners + p50/p90 lines), **I/O**, **Network**, **Processes**, **Syscalls**
(sortable P50/P95/P99), **Logs**, **Flamegraph** (on-CPU / off-CPU), **Latency**, and
**Incidents**. Right-click two runs → **Compare** for an A↔B diff with a streaming
"what changed, better or worse?" summary. Runs are grouped into **sessions** (projects).

## How it works

Three processes: an **Electron** shell (window + terminal + shell hooks), a **FastAPI**
backend (the tracing engine + storage + analysis), and a **React 19 / Vite** renderer.
They talk over REST + Server-Sent Events, so the UI is live during a run. The data model
is `sessions → terminals → runs`, with every analytical table hanging off a `run_id`;
runs keep a complete compressed record on disk plus a curated slice in SQLite.

A guiding principle is **fail-open**: a missing tool, denied privilege, or absent LLM key
degrades gracefully with an explanation, never a broken run.

See **`docs/structure.md`** for the module-by-module map, **`docs/OpenTrace_Roadmap.md`**
for the product spec + phase status, and **`CLAUDE.md`** for a contributor orientation.

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
`~/.opentrace/` (override with `OPENTRACE_HOME`). Set `OPENTRACE_DEV=1` to use the Vite
dev server instead of the built assets.

**Optional tools unlock deeper profiling** (all detected + fail-open if absent):
`py-spy` / `rbspy` / `asprof` / `dotnet-trace` / `phpspy` for per-runtime attach, and
`bcc-tools` + `bpftrace` (with `CAP_BPF`+`CAP_PERFMON`, root, or passwordless sudo) for
eBPF off-CPU + latency. AI summaries need an OpenAI-compatible key (default Google
Gemini/Gemma), stored in the OS-local secret store — never in config or git.

## Repository layout

```
backend/    FastAPI server + tracing engine (Python 3.11+; strace/ltrace/perf,
            attach + sampler registry, eBPF, rules, aggregation, LLM)
frontend/   React 19 + Vite + TypeScript renderer (analytics tabs, diff, live monitor)
electron/   Electron main process + node-pty terminal + transparent shell hooks
e2e/        Playwright-Electron scenario harness (drives the real app; 165 scenarios)
docs/       Architecture (structure.md), roadmap, profiling research, testing playbook
test-files/ Demo workloads (leak/fd-leak/cpu; paired v1/v2 fixtures for the diff view)
```

## Testing

```bash
cd backend && python -m pytest -q          # backend unit + pipeline tests
cd frontend && npm test && npm run build   # renderer tests + typecheck/build
cd e2e && npm install && ./run-all.sh      # end-to-end UI scenarios (drives the app)
```

The `e2e/` harness launches the real Electron app in full isolation (its own backend +
throwaway data) and can run ~165 scenarios in parallel; see `e2e/README.md`.

## Status

The full loop works end-to-end: trace or attach → analytics + flamegraphs → AI summary →
diff, plus live monitoring with incidents and eBPF off-CPU/latency/GC. Not yet done:
packaged installers (`.deb`/`.AppImage`) — for now run from source via `start.sh`. See
the roadmap for what's next.

Linux only. Some features need external tools and (for eBPF) elevated privileges, all
optional and fail-open.
