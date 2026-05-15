# OpenTrace

Local-first observability tool for Linux. An Electron desktop app that collects
low-level system signals (syscalls, memory, I/O, network) and presents
correlated findings visually, so developers can understand complex software
behavior without juggling `strace`, `lsof`, `htop`, and friends by hand.

## Current State

Phase 0 — Foundation is mostly in place.

What works now:

- Electron starts the FastAPI backend as a child process and waits on `/health`.
- The renderer loads a React/Vite frontend with placeholder layout regions.
- The bottom panel hosts `xterm.js` backed by `node-pty`.
- SQLite and `config.json` are created on first run under `~/.opentrace`.
- Session rows are created and updated through the backend `/sessions` API.
- The OpenTrace toggle is wired; in Phase 0 it only writes a banner into the terminal stream.
- The repo root launcher `./start.sh` builds the frontend if needed, ensures Electron deps exist, sets the launch CWD, and opens the desktop window.

What is still incomplete:

- A packaged `opentrace` CLI binary.
- Full command interception with real `strace` + `psutil` collection when tracing is ON.
- The richer analysis / replay UI from later phases.

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
