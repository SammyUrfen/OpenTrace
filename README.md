# OpenTrace

Local-first observability tool for Linux. An Electron desktop app that collects
low-level system signals (syscalls, memory, I/O, network) and presents
correlated findings visually, so developers can understand complex software
behavior without juggling `strace`, `lsof`, `htop`, and friends by hand.

## Current State

Phase 0 (Foundation) is complete and the **Phase 1 data pipeline works end to end**:
type a normal command in the embedded terminal with OpenTrace ON and it is
transparently traced, measured, analyzed, and saved as a *run*.

What works now:

- **Three-level data model** — `sessions` (projects) → `terminals` → `runs`, with
  per-run `events`, `metrics`, `anomalies`, `artifacts`, and `run_views`
  (`backend/app/db.py`). On-disk runs hold `meta.json`, `events.ndjson.zst`,
  `metrics.ndjson.zst`, `strace.log`, and `artifacts/`.
- **Transparent command interception (zsh)** — a line-editor `accept-line` widget
  rewrites a simple foreground command to `otrace -- <cmd>` *before* the shell
  parses it, so the wrapper runs as a native foreground job (exit codes, Ctrl-C,
  job control, quoting all behave normally). Builtins, pipelines, TUIs, and bare
  REPLs run untraced. Bash gets an explicit `ot <cmd>` helper.
- **Trace engine** — `strace -f -T -ttt` parsed into normalized events; a `psutil`
  poller samples the process tree every 250 ms (CPU, RSS/VMS, FDs, threads, I/O);
  syscall-rate is derived; a rule engine flags anomalies (FD growth, memory
  growth, slow syscalls, repeated opens, failed opens, CPU-bound loops).
- **Live + persisted UI** — the sidebar groups runs under projects with severity
  dots; a Live Monitor streams CPU/Memory/FD sparklines over SSE during a run.
- **Analytics tabs** — clicking a run opens it as a tab (and a finished run
  auto-opens) with **Overview** (snapshot + ranked anomaly cards + streaming AI
  summary), **Memory** & **CPU** (time-series with leak banners + 50/90% threshold
  lines), **I/O** (per-file reads/writes/bytes + ⊘ fd-leak markers), **Network**
  (connections + timeouts), **Syscalls** (sortable P50/P95/P99 table), and **Logs**
  (program stdout/stderr with stderr + anomaly-window highlighting).
- **AI summaries** — a configurable OpenAI-compatible LLM (default Google
  Gemini/Gemma) writes a sectioned analysis that streams into the Overview;
  configured in Settings, API key kept in the OS-local secret store.
- **Diff view** — right-click a run → "Compare with…" opens an A ↔ B tab:
  Overview Δ (a ∆ table with better/worse colouring), Memory/CPU Δ (overlaid
  charts), Syscalls Δ, Anomalies Δ (only-A / both / only-B), and a streaming
  **AI diff summary** ("what changed, better or worse?").
- **18 detection rules + live alerts** — covering file I/O, memory, CPU/spin
  loops, network, locks, and more; real-time alerts (FD>200, memory spikes,
  pegged CPU) stream into the Live Monitor *during* a run.
- **Profiling (Phase 6)** — a collector-mode choice between **strace** and
  **ltrace** (both ptrace, mutually exclusive), plus an independent **perf**
  sampler. ltrace runs get a **Profiling tab** — a malloc/free ledger (bytes
  allocated/freed, peak live, leaked blocks) + a library-call hotspot table, and
  a `heap_leak` anomaly. perf runs get a **Flamegraph tab** — an inline,
  click-to-zoom flame chart + self/total CPU symbol hotspots. (`test-files/alloc_demo.c`
  is a ready leak demo for ltrace mode.)
- **Sessions, collectors, theme, resizable panels** — create/switch sessions
  (projects); pick which collectors run (Resource metrics / Syscall trace /
  Library calls / Hardware perf); an **espresso (dark) / warm-paper (light)** theme
  with a toggle (the terminal re-themes too); drag-to-resize sidebar & bottom
  panel; a first-run wizard. Paired `test-files/workload_*_v1/v2.py` fixtures
  demo the diff viewer on a small code change.
- SQLite and `config.json` are created on first run under `~/.opentrace`.

What is still incomplete:

- A packaged `opentrace` CLI binary (the `app.cli` launcher works in dev).
- **Phase 7** — `.deb`/`.AppImage` packaging, libsecret keyring, session export.
- Smaller gaps: bash transparent auto-interception (zsh is the fully-wrapped
  path); ltrace mode only sees calls from the main binary's PLT, so it suits
  native (C/C++/Rust) programs rather than interpreted ones like Python.

## Repository layout

```
backend/    FastAPI server and tracing engine (Python 3.11+)
frontend/   React 19 + Vite + TypeScript renderer
electron/   Electron main process (the desktop shell)
docs/       Internal notes and phase checklists
prompts/    LLM prompt templates (used from Phase 4 onward)
```

See `docs/layout.md` for details and `docs/phase0-checklist.md` for current
build progress.

## Run

Use the root launcher from an activated `opentrace-dev` conda environment:

```bash
conda activate opentrace-dev
./start.sh
```

To force the dev frontend instead of the built `dist/` assets, set `OPENTRACE_DEV=1` before launching Electron.

## Notes

- Backend config and data live under `~/.opentrace/` unless `OPENTRACE_HOME` is set.
- Electron uses `OPENTRACE_PYTHON` to start the backend; the launcher sets it automatically from the active environment.
