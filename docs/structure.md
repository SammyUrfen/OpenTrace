# OpenTrace — Repository Structure and Responsibilities

Developer-facing map of the tree to its responsibilities, current as of the
Phase-1 data pipeline. For the product spec see `OpenTrace_Roadmap.md`.

## Data model (the spine)

Three levels, plus per-run analytical tables (`backend/app/db.py`):

```
sessions (projects)  ──<  terminals
       └──< runs  ──<  events · metrics · anomalies · artifacts · run_views
```

- **session** = a project/workspace with a filesystem-safe `slug`.
- **terminal** = a shell instance inside a session (persists `history`, `cwd.txt`).
- **run** = one traced command execution. Everything analytical hangs off `run_id`.

On disk under `~/.opentrace/` (override with `OPENTRACE_HOME`):

```
config.json · sessions.db
sessions/<slug>/
  session.json
  terminals/term-NN/{history,cwd.txt}
  runs/<cmd>-<YYYYMMDD_HHMMSS>/
    meta.json · events.ndjson.zst · metrics.ndjson.zst · strace.log · artifacts/
```

`events.ndjson.zst` / `metrics.ndjson.zst` are the *complete* compressed record;
SQLite holds all metrics but only a **curated** subset of events (errors,
lifecycle, slow calls, anomaly evidence) so the DB stays small.

## backend/ (FastAPI, Python 3.11, env `opentrace-dev`)

- `main.py` — app, lifespan (init DB, reconcile orphaned runs), routers, SSE endpoints.
- `db.py` — SQLite connect (WAL + FK), base schema, migrations, legacy-DB rebuild.
- `paths.py` — every filesystem path + `slugify` / `command_basename` / `run_folder_name`.
- `config.py`, `secrets.py`, `util.py` — config, file-based secret store, id/time helpers.
- `sessions.py` / `terminals.py` / `runs.py` / `run_views.py` — pydantic models, CRUD,
  and an `APIRouter` each. `terminals.py` has `/attach` (used by the shell hook);
  `runs.py` exposes the lifecycle (`/start`, `/{id}/pid`, `/{id}/end`) and detail
  endpoints (`events`, `metrics`, `anomalies`, `artifacts`, `summary`).
- `storage.py` — DB inserts for events/metrics/anomalies/artifacts, `ndjson.zst`
  read/write, `meta.json`, and severity ranking.
- `streaming.py` — thread-safe SSE pub/sub `broker` bridging the poller thread to
  `EventSource` clients (`/stream`, `/runs/{id}/stream`).
- `trace/`
  - `events.py` — `TraceEvent`, `MetricSample`, `Anomaly` dataclasses.
  - `strace_parser.py` — `strace -f -T -ttt` → `TraceEvent` (unfinished/resumed,
    signals, exit, errno, hex returns, fd/path enrichment).
  - `metrics.py` — `MetricsPoller`: psutil samples a PID subtree every 250 ms.
  - `fdresolve.py` — live fd→path resolution via `/proc`.
  - `orchestrator.py` — run lifecycle control plane: `start_run` / `report_pid`
    (launch poller) / `end_run` (parse, derive syscall-rate, run rules, write
    derived files + meta.json, finalize). `reconcile_orphans` cleans up runs
    interrupted by a backend restart.
- `rules/engine.py` — anomaly rules (repeated opens, failed opens, slow syscall,
  monotonic memory growth, fd-count growth, CPU-bound) → severity + plain-English text.
- `aggregate.py` — pure per-syscall stats (count, latency P50/P95/P99, errors,
  %time) over the event stream; backs `GET /runs/{id}/syscalls`.
- `tests/` — pytest: parser, rules, CRUD/storage, syscall aggregation, a live
  end-to-end pipeline, and real-workload scenario tests (leak/fd-leak/exit-code).

## electron/ (desktop shell + interception)

- `main.js` — spawns the backend (`uvicorn`), waits on `/health`, creates the
  window, IPC for terminal + tracing, passes `BACKEND_URL` to the pty.
- `preload.js` — `contextBridge` exposing `backendUrl`, `terminal`, `tracing`.
- `pty.js` — node-pty session. Exports `OPENTRACE_API` / `OPENTRACE_OTRACE` /
  `OPENTRACE_ENABLE_STRACE` into the shell, sources the right hook, and toggles
  tracing by updating the env var the hook reads each command.
- `shell-hooks/`
  - `otrace` — the launcher. `otrace -- <cmd>` does the `/runs/start` handshake
    (fail-open), runs the command under strace as a child, reports the pid,
    waits, posts `/runs/end`, and `exit`s with the command's real status.
  - `opentrace-hook.zsh` — `accept-line` widget that rewrites a simple foreground
    command to `otrace -- <cmd>`; a conservative classifier skips builtins,
    pipelines, TUIs, and bare REPLs. Registers the terminal via `/attach`.
  - `opentrace-hook.sh` — bash: an explicit `ot <cmd>` helper (bash readline can't
    rewrite the accept line), plus terminal registration.

## frontend/ (React 19 + Vite + TS)

- `state/useOpenTrace.ts` — single hook: fetches projects + runs over REST and
  keeps them live via the `/stream` SSE channel (run lifecycle + metric samples).
- `state/useRunDetail.ts` — a run's summary/metrics/anomalies (re-fetches on finalize).
- `state/useSyscalls.ts` — lazy per-syscall stats for the Syscalls tab.
- `state/useTracing.ts`, `state/format.ts` — toggle mirror; severity/format helpers.
- `components/`
  - sidebar/live: `RunSidebar` (projects → runs + dots), `LiveMonitor`, `Sparkline`.
  - tabs: `MainTabs` (open runs), `SecondaryTabs` (per-run views), `RunView` (dispatch).
  - analytics tabs: `OverviewTab` (snapshot + anomaly cards), `MemoryTab` / `CpuTab`
    (`TimeSeriesChart`), `SyscallTab` (`SyscallTable`, sortable).
  - shell: `Terminal` (xterm), `TracingToggle`, layout placeholders.
- `App.tsx` — composition: main+secondary tabs, RunView (or welcome), sidebar, and a
  bottom panel split into Terminal + Live Monitor. Clicking a sidebar run opens it as a tab.
- Tests: `vitest` + `@testing-library/react` (`*.test.ts(x)`); `npm test`.

## Runtime flow (a traced command)

1. `./start.sh` (from `opentrace-dev`) builds the frontend, sets env, runs Electron.
2. `main.js` starts the backend, waits for `/health`, opens the window; the renderer
   requests a pty; `pty.js` spawns the shell and sources the hook.
3. The hook registers the terminal (`/terminals/attach`) → session + terminal ids.
4. With tracing ON, typing `python app.py` ⟶ the widget rewrites it to
   `otrace -- python app.py` ⟶ `otrace` POSTs `/runs/start`, runs it under strace,
   POSTs the pid (psutil poller starts), waits, POSTs `/runs/end`.
5. The backend parses `strace.log`, derives metrics + syscall-rate, runs the rule
   engine, writes the derived files + `meta.json`, and marks the run complete.
6. The renderer sees it live over SSE (Live Monitor) and in the sidebar.

## Cautions

- Don't `sudo npm install` in `electron/` (ownership breakage).
- Packaging needs native-module rebuild (`node-pty`) for the target Electron ABI.
- CSP and other hardening must land before shipping.
