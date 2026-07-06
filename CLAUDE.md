# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OpenTrace is a **local-first Linux observability desktop app** (Electron shell + FastAPI backend + React 19/TS renderer). It traces/profiles commands you run in an embedded terminal, and attaches to already-running processes, turning low-level signals (syscalls, resource metrics, CPU/off-CPU profiles, latency) into correlated, visual findings.

## Authoritative docs — read these first

- **`docs/structure.md`** — the tree→responsibility map (backend modules, electron shell + shell hooks, frontend state/components, and the end-to-end runtime flow). Start here to find where something lives.
- **`docs/OpenTrace_Roadmap.md`** — product spec + phase status (Phases A–E of the profiling roadmap: attach-to-PID, universal samplers, Node/.NET/PHP, eBPF off-CPU/latency, containers/USDT).
- **`docs/testing.md`** — copy-pasteable manual test workflows for every feature (each with an inline workload + expected result).
- **`docs/Profiling_Roadmap.md`** — per-runtime profiler research (exact tools/flags/formats).

Keep `docs/structure.md` and the roadmap updated when you change architecture — they are treated as living docs.

## Commands

The backend runs in the conda env **`opentrace-dev`** (Python 3.11) — **NOT** `base`/system `python3`, which lack `fastapi`/`uvicorn`/`psutil`/`aiosqlite`/`zstandard`/`httpx`. Use the interpreter explicitly: `~/miniconda3/envs/opentrace-dev/bin/python` (called `$PY` below).

```bash
# Backend (from backend/) — `app.main:app` is the ASGI entry
$PY -m pytest -q                                   # full suite (18 test modules)
$PY -m pytest tests/test_ebpf.py -q                # one module
$PY -m pytest tests/test_rules.py::test_name -q    # one test
$PY -m uvicorn app.main:app --port 8000            # run the API

# Frontend (from frontend/)
npm run build        # tsc -b && vite build → dist/ (start.sh serves this)
npm test             # vitest run
npm run lint         # eslint
npx tsc --noEmit     # typecheck only (fast gate)

# Full app (repo root, conda env active)
./start.sh           # builds frontend if dist/ missing, launches Electron+backend
```

`start.sh` pins `OPENTRACE_HOME=$ROOT/tmp-opentrace` (dev-local data). Electron uses `OPENTRACE_PYTHON` to spawn the backend and `OPENTRACE_DEV=1` to serve the Vite dev server instead of `dist/`.

### Testing/verification conventions (important)

- **Never touch a backend on `:8000`** — that is the user's live app. For any manual/e2e check, start an **isolated** backend on a spare port (`8090+`) with its own home so it can't collide with real data:
  ```bash
  OPENTRACE_HOME=$(mktemp -d) $PY -m uvicorn app.main:app --port 8090
  ```
  The REST API is the ground truth for assertions; the UI is a view over it. `paths.home()` reads `OPENTRACE_HOME` on every call, so setting the env var is all a test needs — no module reloads (see `backend/tests/conftest.py`).
- **UI screenshots without interacting manually:** `electron/main.js` has a smoke mode. Set `OPENTRACE_SMOKE=<out.png>` plus `OPENTRACE_BACKEND_URL=<isolated backend>`, `OPENTRACE_USERDATA=<throwaway dir>`, and optionally `OPENTRACE_SMOKE_DELAY=<ms>`, `OPENTRACE_SMOKE_CLICK=<comma-separated CSS selectors clicked in order>`, `OPENTRACE_SMOKE_JS=<js run after clicks>`, `OPENTRACE_WIN=WxH`. It renders, clicks, screenshots, and quits. (Native OS menu-bar chrome is not captured by `capturePage`.) The onboarding modal's Continue button is `.ai-btn--primary`; dismiss it with four such clicks before deeper clicks.
- **Cleanup:** kill isolated backends by port (`ss -ltnp | grep :8090`), never with broad `pkill` patterns that could match the user's editor/other tools.

## Architecture principles (cross-cutting, not obvious from one file)

**Data spine.** `sessions` (projects) → `terminals` → `runs`; every analytical table (`events`, `metrics`, `anomalies`, `artifacts`, `run_views`) hangs off `run_id` (`backend/app/db.py`). On disk under `~/.opentrace/`, a run keeps the *complete* compressed record (`events.ndjson.zst`, `metrics.ndjson.zst`) while SQLite stores all metrics but only a **curated** event subset (errors, lifecycle, slow calls, anomaly evidence) to stay small.

**Two ways a run is created, one analysis pipeline.** (1) *Launch-trace:* the zsh `accept-line` widget rewrites `python app.py` → `otrace -- python app.py` before the shell parses it (native foreground job semantics preserved); `otrace` does the `/runs/start`→pid→`/runs/end` handshake. (2) *Attach:* `POST /runs/attach {pid|port, window_s, monitor, ebpf}` profiles an existing process for a bounded window. Both converge on `trace/orchestrator.py` (`_finalize`) → the same folded-flamegraph, metrics, rule-engine, anomaly pipeline.

**Three processes + SSE.** `electron/main.js` spawns the backend (`uvicorn`), waits on `/health`, and hosts the pty (`pty.js`) + shell hooks. A psutil poller thread pushes samples/incidents through the `streaming.py` pub/sub `broker` to `EventSource` clients; the frontend's single `state/useOpenTrace.ts` hook keeps runs live over `/stream`.

**Fail-open everywhere.** A missing/denied tool (perf, a sampler, an eBPF tool, an LLM key) must never break a run — it completes with a psutil timeline + a friendly `reason` string surfaced in the relevant tab. Preserve this when adding collectors.

**Collector model.** `strace` and `ltrace` are ptrace-based and **mutually exclusive**; `perf` is an independent sampler that can run alongside either. The frontend `useCollectors` and `runViews(run)` enforce/derive this; the `otrace` hook builds the actual command from the run's live `collector_config` (not hardcoded).

**Universal profiling fold.** `perf.py::_fold_stacks` is the shared core (weighted root→leaf stacks). Every sampler feeds it via a format-specific folder: `fold_perf_script`, `fold_collapsed` (py-spy/asprof/phpspy/bcc), `fold_speedscope` (rbspy/dotnet-trace), `fold_cpuprofile` (Node/V8). `attach.py`'s registry (`_SAMPLERS`/`profiler_plan`/`sampler_argv`) picks the best available per-runtime profiler; Node/Deno/Bun use the built-in V8 inspector via `node_cdp.py` (SIGUSR1→CDP over a hand-rolled WebSocket, no external tool). `_fold_profile` dispatches on the run's `profile_format`.

**Live monitor + incidents.** A `monitor` attach run repeats bounded profiling snapshots + sliding-window rule scans → an Incident feed. Incidents **collapse by rule** (one row with an occurrence count, not one per re-fire). For monitor runs the Overview "Top Findings" are **derived from the incidents** so the two always agree — preserve that invariant if you touch `orchestrator._make_incident` / `_incidents_to_anomalies` / `_finalize`.

**eBPF (Phase D/E, `backend/app/ebpf.py`).** Off-CPU flamegraphs + run-queue/block-I/O latency + Python GC (USDT), all capability-gated (`GET /runs/attach/ebpf-capabilities`) and fail-open. Hard-won specifics baked into the code:
- eBPF needs privilege (root / `CAP_BPF`+`CAP_PERFMON` / passwordless sudo for the tools) — the probe checks all paths. `unprivileged_bpf_disabled=0` does **not** suffice for tracing programs.
- On very new kernels bcc's bundled headers fail to compile most tools (`runqlat`/`biolatency`/`biosnoop`/`pythongc`); only `offcputime` survives. So **bpftrace (CO-RE) is the preferred engine** for the latency histograms + GC when available; `offcputime` (folded stacks) stays on bcc.
- The latency+GC bpftrace runs as **one combined program** (avoids concurrent CO-RE compiles wedging each other) and **without `-p PID`** (which silently pid-filters *all* probes and kills the system-wide sched/block tracepoints) — GC is scoped by an in-script `/pid==PID/` filter instead.
- `_run_proc` captures stdout+stderr to **temp files, never pipes** — an undrained PIPE fills its 64KB buffer and deadlocks a verbose eBPF child.

**Container awareness (`backend/app/container.py`).** Pure `/proc` parsing (no root): label a target's container from its cgroup (docker/podman/containerd/cri-o/k8s, cgroup v1+v2) and resolve a container-local PID → host PID via `NSpid`.

## Gotchas

- Secrets: the LLM API key lives only in the file-based secret store (`~/.opentrace/secrets/`), never in `config.json` or git.
- Don't `sudo npm install` in `electron/` (ownership breakage); packaging needs a `node-pty` native rebuild for the target Electron ABI (`npm run rebuild`).
- ltrace mode only sees the main binary's PLT calls → suits native (C/C++/Rust) programs, not interpreted ones.
- The zsh hook must keep shell history showing the command *as typed* (a `zshaddhistory` hook strips the `otrace --` wrapper); test hook changes in a real pty, not just by eye.
