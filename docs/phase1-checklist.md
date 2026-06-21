# Phase 1 — Data Pipeline Checklist

Tracks `OpenTrace_Roadmap.md` §10 Phase 1, plus the data-model migration that
this phase carried.

## Data model migration
- [x] New schema: sessions (projects) → terminals → runs (+ run_views, events,
      metrics, anomalies, artifacts), keyed off `run_id` (`backend/app/db.py`)
- [x] Legacy flat-`sessions` DB detected and rebuilt on `init()`
- [x] On-disk layout: `sessions/<slug>/{session.json,terminals/,runs/}`
- [x] CRUD + routers for projects, terminals, runs, run_views

## Collectors & pipeline
- [x] strace parser → `TraceEvent` (unfinished/resumed, signals, exit, errno, hex)
- [x] psutil poller → `metrics` (CPU, RSS/VMS, FDs, threads, I/O) every 250 ms
- [x] FD path resolver via procfs
- [x] Event normalization + storage (SQLite curated subset + full `*.ndjson.zst`)
- [x] Run lifecycle: `/runs/start` → `/runs/{id}/pid` → `/runs/{id}/end` → finalize
- [x] syscall-rate derived at finalize and backfilled into metrics
- [x] orphaned-run reconciliation on backend restart

## Interception
- [x] zsh `accept-line` widget rewrites a simple command to `otrace -- <cmd>`
- [x] `otrace` launcher: fail-open handshake, strace child, pid report, exit/signal fidelity
- [x] conservative classifier (skip builtins/pipelines/TUIs/bare REPLs)
- [x] bash `ot <cmd>` helper (readline can't rewrite the accept line)
- [x] `pty.js` exports env + sources the correct hook; toggle updates live

## Analysis & UI
- [x] foundational anomaly rule engine + `max_severity` per run
- [x] SSE live channel (`/stream`, `/runs/{id}/stream`)
- [x] sidebar: projects → runs with severity dots
- [x] Live Monitor: CPU/Memory/FD sparklines streamed during a run

## Tests
- [x] parser unit tests (incl. real strace log)
- [x] rule tests (deterministic, per rule)
- [x] CRUD/storage roundtrip tests
- [x] live end-to-end pipeline test (real strace run → finalized run)
- [x] keystone zsh-over-pty interception test (exit-code fidelity, builtin skip)

## Verification
```bash
cd backend && pip install -e ".[dev]" && pytest -q     # 33 tests
```

## Deferred (later phases)
- bash transparent auto-interception (zsh is the wrapped path)
- analytics tabs (Timeline, Syscall Explorer, Memory, I/O, …), diff view
- LLM summaries (Phase 4), ltrace/perf profiling (Phase 6, host lacks them)
- `--seccomp-bpf` / `-D` strace overhead tuning
