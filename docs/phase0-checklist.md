# Phase 0 — Foundation Checklist

Tracks the eight items from `OpenTrace_Roadmap.md` §10.

- [x] Monorepo: `/backend`, `/frontend`, `/electron`, `/docs`
- [ ] `opentrace` CLI binary: opens Electron window, passes CWD
- [x] Electron boots, starts FastAPI backend as child process
- [x] xterm.js + node-pty terminal in bottom panel
- [ ] Command interception: wraps execution with strace + psutil when ON
- [x] SQLite initialized on first run
- [x] `config.json` created with defaults
- [x] OpenTrace ON/OFF toggle functional (stub: writes banner to terminal, real wrapper deferred)

## Build order

1. **Workspace layout** ✅ skeleton files, package manifests, placeholders.
2. **Backend bootstrap** ✅ FastAPI `/health` runnable from `opentrace-dev`.
3. **Config + SQLite bootstrap** ✅ creates `~/.opentrace/config.json` and `sessions.db` on first run.
4. **Frontend shell** ✅ Vite demo stripped, placeholders for top tab bar, secondary tabs, content, right sidebar, bottom panel.
5. **Electron bootstrap** ✅ spawn backend as child process, wait for `/health`, load built frontend (or dev server when `OPENTRACE_DEV=1`).
6. **Terminal + ON/OFF toggle** ✅ xterm.js + node-pty in bottom panel; toggle writes banner into pty stream (real wrapper deferred to Phase 1).
7. **Persistence** ✅ `/sessions` CRUD endpoints, sidebar list, session per terminal launch (created on pty start, closed on pty exit). Restart test: launch app, see prior session in sidebar.
8. **Secrets store** ✅ `secrets.py` backed by `~/.opentrace/secrets/<name>` (0700 dir, 0600 files). Shape stays the same when we swap in OS keychain.
9. **End-to-end smoke test** — `opentrace` from any CWD opens the window with everything wired. *(next: CLI binary)*

## Restart test (Prompt 5 check)

```bash
# 1) launch
cd frontend && npm run build
cd ../electron && OPENTRACE_PYTHON=$(conda run -n opentrace-dev which python) npm start
# in the terminal pane, type a command or two; confirm one row appears in
# the right sidebar marked "running"
# 2) close the window (the row stays in DB as "running" — known gap)
# 3) relaunch the same command — the prior row is still there, plus a new one
```
