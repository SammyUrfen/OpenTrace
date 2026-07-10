# OpenTrace E2E — Playwright-Electron scenario harness

Drives the **real Electron app** to test features and hunt bugs. Two ways in:

1. **Scripted harness** (this dir) — a catalog of scenarios that subagents run in
   parallel, each in its own fully isolated app instance. Deterministic, repeatable.
2. **Playwright MCP (live)** — attach Claude to a live window over CDP for exploratory
   clicking. Single session; good for reproducing/poking at a specific bug.

Every instance is isolated: its own backend on a spare port + throwaway
`OPENTRACE_HOME` and Electron userData, so nothing touches the user's app on `:8000`.

## Prereqs

```bash
cd e2e && npm install          # playwright (uses the app's own Electron 42)
# the app's built renderer must exist:
(cd ../frontend && npm run build)
# backend deps are in the conda env opentrace-dev (harness uses that python by default;
# override with OPENTRACE_PYTHON=/path/to/python)
```

## Scripted harness

```bash
node smoke.js                       # prove the harness can launch + drive the app
node run.js all                     # run every scenario
node run.js tag:attach,ebpf         # by tag
node run.js id:attach-cpu-basic     # by id (comma-separated)
```

Results print live (`✓ pass`, `✗ fail`, `‼ passed-but-renderer-error`) and land in
`out/results-<pid>.json`. A scenario **fails** if it throws (assertion / timeout) **or**
triggers an uncaught renderer error / page crash — so bugs surface even when the steps
"work". Screenshots from `ctx.shot()` go to `out/`.

**Parallel (subagents):** give each agent a disjoint slice and let them run concurrently
— each `node run.js …` launches its own isolated instance on its own port:

```bash
node run.js tag:sessions   &   node run.js tag:attach   &   node run.js tag:ebpf   &   wait
```

### Scenario format

A scenario is `{ id, name, tags, timeout?, run: async (ctx) => {…} }` in a file under
`scenarios/` (auto-discovered; ids must be unique). It fails by throwing. See
`scenarios/00-core.js` for validated examples and `scenarios/_helpers.js` for the flows
(`newSession`, `attachPid`, `openRunByPid`, `menu`, …). The `ctx` API is documented at
the top of `lib/driver.js` (click/type/wait/api/spawnTarget/assert/shot). Prefer backend
API assertions (`ctx.api.get('/runs')`) as ground truth plus a UI check.

## Playwright MCP (live driving)

Launch the app with CDP exposed, pointed at an isolated backend:

```bash
# 1) isolated backend
OPENTRACE_HOME=$(mktemp -d) python -m uvicorn app.main:app --port 8201 &   # from backend/, conda env
# 2) app with remote debugging
OPENTRACE_BACKEND_URL=http://127.0.0.1:8201 OPENTRACE_USERDATA=$(mktemp -d) \
  OPENTRACE_REMOTE_DEBUG=9333 ./node_modules/.bin/electron .               # from electron/
```

Then add the Playwright MCP to Claude Code, connected over CDP:

```bash
claude mcp add playwright -- npx -y @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9333
```

or in `.mcp.json`:

```json
{ "mcpServers": { "playwright": {
  "command": "npx",
  "args": ["-y", "@playwright/mcp@latest", "--cdp-endpoint", "http://127.0.0.1:9333"]
} } }
```

Claude then drives the renderer (snapshot / click / type / screenshot). Caveats: it's a
single live session (not parallel), and it sees only the renderer page — no Electron
main-process / IPC / native-menu access (the app's in-window MenuBar is fine).

## Subagent bug-hunting (scripted + exploratory)

**Scripted** — split the 12 files across agents; each runs its slice in its own
isolated instance and reports the JSON:

```bash
./run-all.sh 4          # all 165 in parallel waves (~2 min)
node run.js file:13-ebpf.js   # one category
```

**Exploratory** — hand an agent a feature area + the `ctx` API (top of `lib/driver.js`)
and the helpers, and have it *write new scenarios* that improvise variations and edge
sequences (rapid toggles, interleaved modals, attach→delete races, odd inputs), run
them, and report anything that throws or logs a renderer error. New `scenarios/*.js`
files are auto-discovered by the runner. A failure = a throw **or** an uncaught console
/ page error, so bugs surface even when the steps appear to succeed.

Findings this suite already surfaced (behaviours worth a look, not necessarily bugs):
the attach modal only lists the top-60 processes by RSS (a low-RSS target is
unfindable); deleting a run opens a styled confirm modal (`H.confirmDeleteRun`), not a
native `window.confirm`; the xterm swallows `Ctrl+K`
so the palette won't open while the terminal is focused; a raw API run-delete doesn't
refresh the sidebar (only the UI delete path does); the Live-Monitor hint text
("toggle OpenTrace on to trace") makes body-text assertions on tracing state unreliable
— assert on the `.tracing-toggle--on/--off` class instead.
