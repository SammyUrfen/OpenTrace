# OpenTrace — Repository Structure and Responsibilities

Developer-facing map of the tree to its responsibilities, current as of the
Phase-1 data pipeline. For the product spec see `OpenTrace_Roadmap.md`.

## Data model (the spine)

Three levels, plus per-run analytical tables (`backend/app/db.py`):

```
sessions (projects)  ──<  terminals
       └──< runs  ──<  events · metrics · anomalies · artifacts
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
  A local-only guard replaces wildcard CORS: real web `Origin`s and non-localhost
  `Host` headers get 403 (drive-by/DNS-rebinding defense); Electron's `file://`
  (null origin), the Vite dev server, and plain curl all pass.
- `db.py` — SQLite connect (WAL + FK), base schema, migrations, legacy-DB rebuild.
- `paths.py` — every filesystem path + `slugify` / `command_basename` / `run_folder_name`.
- `config.py`, `secrets.py`, `util.py` — config, file-based secret store, id/time helpers.
- `sessions.py` / `terminals.py` / `runs.py` — pydantic models, CRUD,
  and an `APIRouter` each. `terminals.py` has `/attach` (used by the shell hook);
  `runs.py` exposes the lifecycle (`/start`, `/{id}/pid`, `/{id}/end`) and detail
  endpoints (`events`, `metrics`, `anomalies`, `artifacts`, `summary`, plus
  Phase-6 `profile` and `flamegraph`). `GET /runs/{id}/metrics` stride-downsamples
  to `?max_points` (default 2000; `0` = full stream); `/{id}/incidents` compacts
  legacy uncollapsed rows and downsamples each incident's embedded metric window
  (true count in `metrics_n`). Deleting a run/session tears down any live
  poller/monitor via `orchestrator.abort_run` (no ghost loops).
- `storage.py` — DB inserts for events/metrics/anomalies/artifacts, `ndjson.zst`
  read/write, `meta.json`, and severity ranking.
- `streaming.py` — thread-safe SSE pub/sub `broker` bridging the poller thread to
  `EventSource` clients (`/stream`, `/runs/{id}/stream`).
- `trace/`
  - `events.py` — `TraceEvent`, `MetricSample`, `Anomaly` dataclasses.
  - `strace_parser.py` — `strace -f -T -ttt` → `TraceEvent` (unfinished/resumed,
    signals, exit, errno, hex returns, fd/path enrichment).
  - `ltrace_parser.py` — `ltrace -S -f -ttt -T` → `TraceEvent` for the **ltrace
    collector mode**: library calls become `LIBCALL` events (malloc/free + the
    hotspot table) and `@SYS` lines become normal `SYSCALL` events, so the
    syscall/I/O/Network pipeline still works. A superset of strace's view.
  - `metrics.py` — `MetricsPoller`: psutil samples a PID subtree every 250 ms.
  - `orchestrator.py` — run lifecycle control plane: `start_run` / `report_pid`
    (launch poller; `descendants_only` whenever a wrapper — strace/ltrace/perf —
    runs) / `end_run`. `_finalize` branches on the run's collectors: strace **or**
    ltrace as the trace source, plus (ltrace) a `profile.json` + leak anomalies and
    (perf) a `flamegraph.json`. `reconcile_orphans` cleans up interrupted runs.
- `profile.py` — allocation profiling from ltrace `LIBCALL` events: a malloc/free
  ledger (bytes alloc/freed, peak live, outstanding/leaked blocks, unmatched
  frees), library-call hotspots, and `heap_leak`/`alloc_free_imbalance` anomalies.
- `perf.py` — folds profiler output into a nested `{name,value,children}` flame
  tree + self/total symbol hotspots, pruned for the UI. `_fold_stacks` is the
  shared core (weighted root→leaf stacks); `fold_perf_script` (perf script),
  `fold_collapsed` (py-spy/asprof/phpspy/bpftrace), and `fold_speedscope`
  (rbspy/dotnet) all feed it. `build_flamegraph` shells out to `perf`.
- `rules/engine.py` — anomaly rules (file I/O, memory growth/spike, CPU/spin/
  infinite-loop, slow syscalls/file-I/O, network errors/reuse, mutex contention,
  I/O retry, read/write storms, subprocess spawning) → severity + plain-English text.
  Each rule is tagged (`@_needs('events'|'metrics')`) so `run_rules` only invokes a
  rule whose input signal is present — an events-rule can't false-fire on an
  eventless attach/monitor context. **Metric-only rules** (`cpu_bound_metric`,
  `io_wait_metric`, plus the cgroup-aware `cpu_throttled` / `rss_near_cgroup_limit`)
  fire on attach/monitor runs that have no syscall stream; `slow_downstream_peer`
  (launch runs) joins `connect(fd→peer)` with long blocking reads on that fd to
  surface "A is slow because it waits on B". Thresholds live on `RuleContext`
  (`RuleThresholds`, overridable via `config.tracing.rule_thresholds`); whole-history
  trend rules judge only a trailing window on long-lived launch runs so warmup isn't
  flagged as a leak. Real-time alerts (FD / memory-spike / sustained-CPU) are emitted
  live from `orchestrator._live_detect` and **re-arm via hysteresis** (a fired alert
  clears once its metric stays quiet, so a genuine re-occurrence re-fires). Monitor
  runs also get a **long-horizon slow-leak** check (full DB history vs a start
  baseline) that the 90s sliding window can't see. `RuleContext` carries the
  `collectors` dict + optional cgroup cpu-quota / mem-limit.
- `aggregate.py` — pure aggregations over the event stream: per-syscall stats,
  per-file I/O (fd→path resolution, leak detection), and outbound connections
  (sockaddr parsing); back `GET /runs/{id}/{syscalls,io,network}`.
- `program_output.py` — reconstructs stdout/stderr from `strace -e write=1,2`
  hex dumps (keeps tty fidelity); backs `GET /runs/{id}/logs`.
- `llm.py` — OpenAI-compatible streaming client (httpx) + `/config/llm` router;
  filters reasoning-model "thought" chunks. API key in the secret store.
- `summarize.py` — builds the run-summary + run-diff prompts and streams/persists
  AI summaries; backs `GET /runs/{id}/ai-summary[/stream]` and
  `GET /diff/{a}/{b}/ai-summary/stream`. The run prompt is token-budgeted but rich:
  an event TIMELINE + resource trajectory + I/O + network + malloc profile + perf
  hotspots (`_gather_context`), so the model can describe what happened when.
- `tools.py` — detects the external tracing tools (strace/ltrace/perf): availability,
  version, perf_event_paranoid, and a distro-tailored install hint; backs
  `GET /info/tools` (the wizard tool-check + Settings ▸ Tracing tools).
  Run files are browsable via `GET /runs/{id}/files` + `GET /runs/{id}/file?name=`
  (text-only, size-capped, path-traversal-guarded) behind the Files tab.
- `container.py` — pure `/proc` + `/sys/fs/cgroup` parsing (no root): labels a
  target's container from its cgroup, resolves container-local → host PID via
  `NSpid`, and reads cgroup **limits** (`cgroup_limits(pid)` → cpu-quota-cores +
  mem-limit-bytes, cgroup v1 & v2) that feed the cgroup-aware rules.
- `attach.py` — attach-to-running-PID profiling (roadmap Phase A). `detect_runtime(pid)`
  infers the language runtime from `/proc/<pid>/maps` (+ exe fallback);
  `list_targets()` enumerates same-uid attachable processes; backs
  `GET /runs/attach/targets`. `POST /runs/attach {pid|port,window_s}` →
  `orchestrator.start_attach_run`, which watches the target with psutil
  (`descendants_only=False`) and runs the runtime's profiler for a bounded window,
  reusing `perf.py` → `flamegraph.json` (fail-open to a psutil-only timeline).
  Phase B/C: a sampler registry (`_SAMPLERS` / `profiler_plan` / `sampler_argv`)
  picks a dedicated profiler when available — **py-spy** (Python), **rbspy** (Ruby),
  **asprof** (JVM), **dotnet-trace** (.NET), **phpspy** (PHP), and the **V8 inspector**
  (`node_cdp.py`, SIGUSR1→CDP, no install) for **Node/Deno/Bun** — for real app
  symbols; else perf. `_finalize`/`_capture_profile` fold by format (`_fold_profile`
  → `fold_collapsed`/`fold_speedscope`/`fold_cpuprofile`/`fold_phpspy`/perf).
- `tests/` — pytest: parser, rules, CRUD/storage, syscall aggregation, a live
  end-to-end pipeline, and real-workload scenario tests (leak/fd-leak/exit-code).

## electron/ (desktop shell + interception)

- `main.js` — spawns the backend (`uvicorn`), waits on `/health`, creates the
  window, IPC for terminal + tracing, passes `BACKEND_URL` to the pty.
  Before spawning it probes `:8000`: an already-running OpenTrace backend
  (identified via `/info`) is reused; a foreign occupant pushes the spawn to an
  ephemeral free port (the dynamic URL flows to renderer + pty). A crashed
  backend is auto-restarted with backoff (3 attempts, `backend:status` IPC keeps
  the renderer informed). `OPENTRACE_BACKEND_URL` points it at an already-running
  backend (skips probing/spawning, for dev/testing); `OPENTRACE_WIN=WxH` sizes
  the window. Reload/DevTools menu roles are dev-only (`OPENTRACE_DEV`/`DEBUG`)
  so Ctrl+R reaches the shell instead of killing the live pty.
- `preload.js` — `contextBridge` exposing `backendUrl`, `terminal`, `tracing`.
- `pty.js` — node-pty session. Exports `OPENTRACE_API` / `OPENTRACE_OTRACE` /
  `OPENTRACE_ENABLE_STRACE` into the shell, sources the right hook, and toggles
  tracing by updating the env var the hook reads each command. Keeps a capped
  (256KB) rolling copy of terminal output that it **replays through the `pty:data`
  channel** into a freshly-mounted xterm (renderer reload / panel remount) and
  **mirrors to `<userData>/terminal-scrollback.log`**, so scrollback survives a
  full app restart (restored above the fresh prompt with a marker). Restart of an
  exited shell reuses the same xterm and deliberately skips the replay.
- `shell-hooks/`
  - `otrace` — the launcher. `otrace -- <cmd>` does the `/runs/start` handshake
    (fail-open), then builds the trace command from the run's collectors: an
    inner `ltrace -S` **or** `strace` wrapper (ptrace-exclusive), optionally
    wrapped by an outer `perf record -g` (probed first to stay fail-open). It
    reports the pid, waits, posts `/runs/end`, and `exit`s with the real status.
    It no longer hardcodes `collector_config`, so the backend applies the live
    config (the Live Monitor toggles take effect).
  - `opentrace-hook.zsh` — `accept-line` widget that rewrites a simple foreground
    command to `otrace -- <cmd>`; a conservative classifier skips builtins,
    pipelines, TUIs, and bare REPLs. Registers the terminal via `/attach`. A
    `zshaddhistory` hook strips the `otrace --` wrapper back out before the line
    is committed, so shell history / up-arrow show the command as typed
    (robust across HIST_IGNORE_SPACE / SHARE_HISTORY settings).
  - `opentrace-hook.sh` — bash: an explicit `ot <cmd>` helper (bash readline can't
    rewrite the accept line), plus terminal registration.

## frontend/ (React 19 + Vite + TS)

- `state/useOpenTrace.ts` — single hook: fetches projects + runs over REST and
  keeps them live via the `/stream` SSE channel (run lifecycle events). An SSE
  reconnect triggers a full sessions+runs resync, so missed lifecycle events
  can't leave runs stuck "running".
- `state/runCache.ts` — per-(run, resource) in-memory cache for finalized
  (immutable) runs' payloads, shared by all data hooks; evicted on delete/end.
- `state/liveMetrics.ts` — module-level live metric store (`useSyncExternalStore`
  per runId): the 4 Hz SSE sample stream re-renders only its subscribers
  (LiveMonitor pane, a live run's Overview), not the App tree.
- `state/useRunDetail.ts` — a run's summary/metrics/anomalies (re-fetches on finalize).
- `state/useSyscalls.ts` — lazy per-syscall stats (delegates to `useRunResource`).
- `state/useTracing.ts`, `state/format.ts` — toggle mirror; severity/format helpers.
- `components/`
  - sidebar/live: `RunSidebar` (projects → runs + dots), `LiveMonitor`, `Sparkline`.
  - shared helpers: `seriesUtils` (loop-based min/max + chart-point decimation —
    metric series can exceed 100k samples; spread-based `Math.max(...)` crashes),
    `StatCell`, `textUtils`, `collectorRows`, `LlmConfigForm`, `ToolChecklist`,
    `ErrorBoundary` (wraps each run view: one bad tab shows an inline error +
    Retry instead of blanking the app).
  - tabs: `MainTabs` (open runs), `SecondaryTabs` (per-run views), `RunView` (dispatch).
  - analytics tabs: `OverviewTab` (snapshot + anomaly cards), `MemoryTab` / `CpuTab`
    (`TimeSeriesChart`), `IoTab`, `NetworkTab`, `SyscallTab` (sortable tables),
    `LogsTab` (program output, stderr + anomaly-window highlighting). Phase-6
    profiling tabs appear per-run via `runViews(run)`: `ProfilingTab` (ltrace —
    malloc/free ledger + library hotspots) and `FlamegraphTab` (perf — an inline
    click-to-zoom HTML flame chart + self/total symbol hotspots).
    Every view has a collapsible `TabGuide` ("How to read this") footer.
  - data: `useRunDetail` (summary/metrics/anomalies), `useSyscalls`,
    `useRunResource` (lazy array fetch: io/network/logs/processes/events) +
    `useRunObject` (lazy object fetch: profile/flamegraph),
    `useAiSummary` (module-level SSE store that survives tab unmounts via
    `useSyncExternalStore`), `useTheme` (espresso/warm-paper), `useCollectors`
    (strace↔ltrace are ptrace-exclusive: enabling one disables the other).
  - diff: `DiffView` (+ `DIFF_VIEWS`) + `DiffPanels` (Memory/CPU/Syscall/Anomaly Δ);
    `useDiff` (both runs' data), AI diff card streams `/diff/{a}/{b}/ai-summary`.
  - chrome: `MenuBar` (in-app File/View/Run/Help — the native Electron menu bar
    doesn't render on KDE/Wayland; dropdown is portalled to <body>),
    `RunSidebar` (create/switch sessions + run context menu: Open /
    Rename… / Compare with… / Delete; a run shows `label ?? command`),
    `LiveMonitor` (collector toggles),
    `SettingsPage` (full sectioned page: General/Collectors/AI/Tools/Guide/About),
    `CommandPalette` (⌘K), `SessionModal` (generic create/rename dialog for
    sessions and runs), `RunNameBar` (non-blocking "name this run" prompt shown
    on a freshly-finished run; opt-out in Settings ▸ General), `FirstRunWizard`
    (onboarding), `Markdown` (safe LLM-summary renderer). Runs are renamable via
    `renameRun` (PATCH `display_name`) — double-click a tab or the sidebar
    context menu.
  - tabs: `useTabs` (unified run + diff tab model; `tabKey`), `MainTabs` (generic).
- Theme: shared CSS tokens in `index.css` — `:root` espresso (dark) +
  `:root[data-theme=light]` warm paper; `state/useTheme.ts` + a ☾/☀ toggle; the
  xterm terminal re-themes via a `data-theme` MutationObserver.
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
