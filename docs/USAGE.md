# OpenTrace — Usage Guide

OpenTrace is a **local-first Linux observability desktop app**. You run commands in an embedded terminal (or attach to already-running processes), and OpenTrace turns low-level signals — syscalls, resource metrics, CPU/off-CPU profiles, latency histograms, per-request timing — into correlated, visual findings.

This guide is task-oriented: for each thing you want to do, it says **what to do** and **what you'll see**. It assumes the app is already installed and running.

- New here? Install and launch first: [README.md](../README.md) · [SETUP.md](./SETUP.md)
- Want the internals? [ARCHITECTURE.md](./ARCHITECTURE.md) · [structure.md](./structure.md) (file-by-file map)
- Something not working? [TROUBLESHOOTING.md](./TROUBLESHOOTING.md)
- Want concrete, copy-pasteable workloads that reproduce each feature? [testing.md](./testing.md) — referenced throughout below.

---

## Before you start (scope & honest limits)

- **Linux only.** OpenTrace is built around Linux tracing primitives (ptrace, perf, eBPF, `/proc`). There is no macOS/Windows build.
- **Run from source.** There is no packaged `.deb`/`.AppImage` yet. Launch with `./start.sh` from a checkout (see [SETUP.md](./SETUP.md)).
- **Single machine, single user.** The API accepts only local callers, attach is restricted to **same-user** processes (unless the backend runs as root), and one embedded terminal is live at a time.
- **Many features degrade gracefully but need external tools.** strace/ltrace/perf for launch tracing; per-runtime samplers (py-spy, rbspy, async-profiler, dotnet-trace, phpspy) for attach; bcc/bpftrace **plus elevated privilege** for eBPF off-CPU/latency/GC and request tracing. When a tool or permission is missing, the run still completes with whatever it could collect and shows a plain-English `reason` in the affected tab — it never hard-fails.
- **Not everything is equally battle-tested.** The .NET and PHP attach samplers and the MySQL/MariaDB request-span probe are implemented and unit-tested but **not live-validated** in the current environment. Postgres (libpq) and SQLite request tracing *are* validated end-to-end. HTTP/2 / gRPC request tracing is structurally unsupported (it degrades to connection-level metrics).

---

## The window at a glance

| Region | What lives there |
|---|---|
| **Left sidebar** | Sessions (projects) → runs, newest first. Select, rename, compare, delete runs here. Toggle with `Ctrl+B`. |
| **Top tab bar** | One tab per open run or diff. Each remembers its last-selected analytics view. |
| **Secondary tabs** | The analytics views for the focused run (Overview, Timeline, … Files) — the set depends on which collectors ran. |
| **Bottom panel** | The **embedded terminal** (your real login shell) + the **Live Monitor** pane (live metrics, alerts, collector toggles). Toggle terminal with `Ctrl+J`. |
| **Header** | The **terminal-tracing on/off toggle**, backend-status dot, menu bar. |

Keyboard shortcuts (also on the in-app menu):

| Key | Action |
|---|---|
| `Ctrl+K` | Command palette (jump to any session/run/action) |
| `Ctrl+N` | New session |
| `Ctrl+,` | Settings |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+J` | Toggle terminal panel |
| `Ctrl+Shift+T` | Toggle terminal tracing on/off |
| `Delete` / `Backspace` | Delete the focused run row (with confirmation) |
| `Esc` | Close the active modal/menu |

---

## 1. Trace a command you run in the terminal

**When you want:** to profile a program you launch yourself (a script, a build, a one-off job).

**Do:**

1. Turn on tracing — flip the **tracing toggle** in the header (or `Ctrl+Shift+T`, or the command palette "Turn terminal tracing ON"). It's off by default.
2. In the embedded terminal (open it with `Ctrl+J`), type the command as you normally would and press Enter:
   ```bash
   python app.py
   ```
3. That's it. In **zsh**, the shell hook transparently rewrites the line to `otrace -- python app.py` *before* zsh parses it, so quoting, globbing, job control, `$?` and Ctrl-C all behave natively. Your shell history and up-arrow still show the command exactly as you typed it. **This fully hands-off auto-tracing requires zsh to be the terminal's actual shell** — the transparent `accept-line` rewrite is zsh-only; under bash you get only the explicit `ot <cmd>` helper (see the Bash caveat below).

**You see:** while it runs, the Live Monitor pane shows a live CPU/RSS/fd timeline. When the command exits, a finished run appears in the sidebar and opens a tab (it only steals focus if you weren't reading something else). Open its **Overview** to see top findings; other tabs (Timeline, Memory, CPU, I/O, Network, Processes, Syscalls, Logs, …) hold the detail.

**What the hook auto-traces (and what it skips).** In zsh, only a *single simple external command* is wrapped. It deliberately skips:
- pipelines, redirections, subshells, backgrounding, `VAR=val` prefixes;
- interactive/TUI tools (`vim`, `less`, `top`, `ssh`, `tmux`, `gdb`, `psql`, …);
- bare REPLs (`python`, `node`, `irb`, `bash`, … with no arguments) — but `python app.py` (has an argument) *is* traced;
- builtins/functions/aliases.

**Force-trace something the classifier skips:** prefix it with `ot`:
```bash
ot mytool --flag        # runs otrace -- mytool --flag regardless of the classifier
```

**Bash caveat (important).** Bash's readline can't rewrite an accepted line, so **bash does not get transparent auto-wrapping** — the only interception is the `ot <cmd>` helper. This is a real limitation, not a bug. Use zsh for hands-off tracing.

**Reproducible workloads:** [testing.md §1.1](./testing.md) (CPU-bound → flamegraph + hot-function finding) and [§1.2](./testing.md) (fd leak → fd-growth finding).

### Choosing collectors for launch traces

Collectors decide what gets recorded. Toggle them in the **Live Monitor pane** or **Settings ▸ Collectors** — the change takes effect on your *next* traced command (the `otrace` handshake reads the live config each run).

| Collector | Default | What it gives you | Notes |
|---|---|---|---|
| **psutil** | on | Resource metrics timeline (CPU %, RSS, fds, threads, I/O bps) | Always on; the fail-open baseline. |
| **strace** | on | Syscalls → I/O, Network, Processes, Syscalls, and **Logs** (stdout/stderr reconstructed from `-e write=1,2`) | ptrace-based. |
| **ltrace** | off | Library-call hotspots + a malloc/free ledger → the **Profiling** tab | **Mutually exclusive with strace** (both use ptrace). Only meaningful for **native** C/C++/Rust binaries — it sees the main binary's PLT calls, so interpreted programs (Python/Node/Java) show little. |
| **perf** | off | On-CPU **Flamegraph** + function hotspots | Independent sampler — runs *alongside* strace or ltrace. Needs `perf` installed and `kernel.perf_event_paranoid ≤ 2`. |

The strace/ltrace mutual exclusion is enforced in the UI: turning one on turns the other off. `perf` layers on only after a permissions probe succeeds — if it can't, the workload still runs, just without a flamegraph, and the Flamegraph tab explains why.

---

## 2. Attach to a process that's already running

**When you want:** to profile a live server/daemon you didn't launch through OpenTrace — without restarting it.

**Do:**

1. Open **"Attach to running process…"** — it's on the **Run menu** and in the `Ctrl+K` command palette (there is no dedicated keyboard accelerator for it). This lists same-user processes, **sorted by memory (RSS) descending** — servers float to the top. A small/short-lived process may not appear.
2. Pick a target, or use the escape hatch: type a bare number in the filter and click **"PID {n}"** or **"port {n}"** (a port is resolved to the process listening on it).
3. Set the **Sample** window (seconds, clamped to **3–120**, default **20**).
4. Optionally tick **Keep monitoring (live)** (§3), **Off-CPU + latency (eBPF)** (§3), and/or **Request tracing** (§4).
5. Submit.

**You see:** OpenTrace samples the process for the window, then builds a flamegraph and a resource timeline. Each target row shows a **hint** telling you exactly what you'll get for that runtime.

**Which profiler runs, per runtime.** OpenTrace picks the best available per-runtime sampler; if it isn't installed it falls back to `perf`, which shows VM/interpreter frames rather than your app's functions.

| Runtime (auto-detected) | Sampler | You get | Install |
|---|---|---|---|
| Python | `py-spy` | Real Python frames (samples children too) | `pip install py-spy` |
| Ruby | `rbspy` | Real Ruby frames | `cargo install rbspy` (or a release) |
| JVM | `asprof` (async-profiler) | Real JVM frames | install async-profiler |
| .NET | `dotnet-trace` | Real .NET frames *(implemented, not live-validated)* | `dotnet tool install -g dotnet-trace` |
| PHP | `phpspy` | Real PHP frames *(implemented, not live-validated)* | phpspy (github.com/adsr/phpspy) |
| Node | **built-in V8 inspector** (SIGUSR1 → CDP) | Real JS frames, **no external tool** | — |
| Deno / Bun | *(none)* → `perf` | VM frames only — they don't accept SIGUSR1→inspector | needs `--inspect` at launch |
| Native / Go | `perf` | Native symbols | perf |
| Unknown | — | "Can't inspect this process — attach may be denied." | — |

**Guardrails you may hit:**
- **Same-user only:** attaching to another user's PID returns *"pid … belongs to another user — attach requires a same-user process."* Run the backend as root to bypass.
- **Port with no listener:** *"no listening process on port {n}."*
- **Concurrency cap:** at most **16** concurrent attach/monitor runs — *"too many concurrent attach/monitor runs … stop one first."*
- **Node safety:** OpenTrace refuses to send SIGUSR1 unless the target really looks like Node (SIGUSR1 would terminate a non-Node process). `node_exporter`/`node-agent` are never misdetected as Node.

**Reproducible workloads:** [testing.md §2.1](./testing.md) (live Python, fail-open baseline), [§2.2](./testing.md) (live Node → real JS symbols, no restart), [§4](./testing.md) (per-runtime samplers).

Under the hood this is `GET /runs/attach/targets` (the list) and `POST /runs/attach` (`{pid|port, window_s, monitor, ebpf, requests, session_id}`).

---

## 3. Keep watching a process (monitor mode) + the Incident feed

**When you want:** to watch a live service over time and catch problems as they happen, instead of a one-shot snapshot.

**Do:** in the Attach modal, tick **"Keep monitoring (live)"**. The window field relabels to **Snapshot** — OpenTrace repeats a profiling snapshot every *N* seconds and keeps a sliding-window rule scan running until you press **Stop**.

**You see:**
- A **"Monitoring live — capturing incidents"** banner with a **Stop** button on the run.
- An **Incidents** tab: a live feed where each finding **collapses by rule** — one row per rule with an occurrence count, not one row per re-fire. Each incident records **when** it fired, **where** (the dominant hot stack, or "off-CPU (not attributable to a CPU hot path)"), and a **leading-metrics window**.
- Overview "Top Findings" for a monitor run are **derived from the incidents**, so the two tabs always agree — during the run and after it finalizes.

**What the live scan catches:** metric-only rules over the trailing ~90s window (CPU-bound, I/O-wait, cgroup CPU-throttled, RSS-near-cgroup-limit, …), plus live alerts (fd-leak, RSS spike, sustained CPU-hot) with hysteresis so a genuine re-occurrence re-fires. A separate **long-horizon slow-leak check** watches the full metric history for a steady RSS climb the 90s window can't see.

**Opt into eBPF off-CPU + latency (attach/monitor only).** Tick **"Off-CPU + latency (eBPF)"** in the Attach modal. This adds:
- an **off-CPU flamegraph** (where the process *blocks* — I/O, locks, DB waits — which on-CPU sampling can't see), and
- **run-queue** and **block-I/O** latency histograms → the **Latency** tab, and
- for a USDT-enabled Python, a **GC timeline**.

The checkbox is **disabled unless the environment supports eBPF**; when unavailable it shows the backend's exact reason. eBPF needs **privilege** — run OpenTrace as root, grant `CAP_BPF`+`CAP_PERFMON`, or set up passwordless sudo for the bcc tools. Note: `kernel.unprivileged_bpf_disabled=0` does **not** grant this — it never enabled tracing programs. There's a rescan (`↻`) button that bypasses the capability cache after you install tools or change privileges.

**Reproducible workloads:** [testing.md §3](./testing.md) (monitor + incident feed) and [§5](./testing.md) (eBPF off-CPU + latency — needs privilege; includes the expected `ebpf-capabilities` response and a sudoers snippet).

---

## 4. Trace HTTP requests and the queries behind them

**When you want:** to see per-endpoint latency on a live plaintext HTTP/1.x server, and attribute each request's time to its database queries and to on-CPU vs blocked time.

**Do:** in the Attach modal, tick **"Request tracing (HTTP endpoints + DB)"**. This is gated by a **weaker, separate** check than eBPF — it needs only **bpftrace + privilege** (not kernel BTF or bcc), because it works via syscall tracepoints and libpq/libssl uprobes. If unavailable you'll see: *"request tracing needs bpftrace + privilege — run OpenTrace as root, grant CAP_BPF+CAP_PERFMON, or enable passwordless sudo for /usr/bin/bpftrace."*

**You see — the Requests tab, two views:**

- **Endpoints (RED table):** one row per normalized endpoint (`/users/{id}` etc.), columns **count · p50 · p95 · p99 · err% · %DB**, sorted by p95 descending. Click a row to expand its breakdown. The "% DB" column shows `—` (not a misleading `0%`) when no DB spans were captured.
- **Requests (waterfall):** appears once individual slow/errored requests were sampled (slowest first). Each row is a duration track with nested **DB-span markers**. Click a request to expand:
  - a **100%-stacked breakdown bar** over four buckets that sum to the wall duration — **on-CPU · run-queue · db-wait · other-off-CPU** (the "other" segment is annotated with its top off-CPU reason, e.g. "off-CPU (futex_wait)");
  - the captured **SQL statements** with durations (PII-scrubbed — literals/numbers replaced with `?`);
  - a **span → flamegraph drill**: it fetches that request thread's blocked stacks (`GET /runs/{id}/offcpu-flamegraph?tid={tid}`) so you see exactly where *that* request was stuck.

**Honest limits & fail-open reasons:**
- **DB spans need a dynamically-linked client.** With Postgres via system `libpq`, or in-process SQLite, spans appear. A statically-bundled `psycopg2-binary`, `asyncpg`, or a pure-wire driver yields *"DB spans unavailable … Endpoint timings are still shown."* SQLite shows as an on-CPU overlay; remote Postgres shows as off-CPU db-wait.
- **MySQL/MariaDB** spans are implemented but **not live-validated** here.
- **No HTTP seen** is a valid result, not an error: *"No HTTP/1.x requests were observed … (idle server, HTTP/2 endpoint, or a non-HTTP process)."* TLS traffic *is* recovered (via libssl uprobes), so HTTPS is not a blind spot; **HTTP/2 / gRPC is not** (HPACK state can't be recovered mid-stream).

For a running monitor run, the endpoint table updates live over SSE without a poll. Backing endpoint: `GET /runs/{id}/requests`.

---

## 5. Read the analytics tabs

The secondary-tab set is derived entirely from which collectors ran, so you only ever see tabs backed by real data. (Older runs with no recorded collector config fall back to the full strace-derived set.)

| Tab | Shown when | What it shows |
|---|---|---|
| **Overview** | always | Headline stats, **Top Findings** (from the rule engine, or from incidents for a monitor run), and the **AI summary** (§7). Live for a running run. |
| **Incidents** | `monitor` | The collapse-by-rule live incident feed (§3), with async AI explanations. |
| **Requests** | `requests` | The RED table + per-request waterfall + breakdown + drill (§4). |
| **Timeline** | always (psutil) | The combined resource timeline over the run. |
| **Memory** | always | RSS/VMS over time, peaks. |
| **CPU** | always | CPU % over time (can exceed 100% across cores). |
| **I/O** | strace or ltrace | Read/write syscall aggregation and throughput. |
| **Network** | strace or ltrace | Connections parsed from `connect()` (note: DNS `getaddrinfo` is a libc call, invisible to strace). |
| **Processes** | strace or ltrace | Process/subprocess stats. |
| **Syscalls** | strace or ltrace | Syscall explorer (counts, latencies). |
| **Logs** | **strace only** | Reconstructed stdout/stderr (from strace's write-data dump — ltrace can't do this, so ltrace runs have no Logs). |
| **Profiling** | `ltrace` | malloc/free ledger + library-call hotspots (native binaries). |
| **Flamegraph** | `perf` (or an attach sampler) | Interactive flamegraph. Click a frame to zoom. |
| **Latency** | `ebpf` | Run-queue + block-I/O latency histograms (p50/p90/p99). |
| **Files** | always | Every file in the run directory, viewable inline (text files, capped at 256 KiB). |

**On-CPU vs off-CPU flamegraph.** The Flamegraph tab shows an **On / Off** toggle only when the run has eBPF data. On-CPU cells are measured in **samples**; off-CPU cells in **µs** (time *blocked*). Empty-state text is context-aware — it distinguishes a launch run that needs the perf collector from an attach run that needs a longer window or a busier target.

**Latency caveat.** Run-queue latency is per-target; **block-I/O (biolatency) is host-wide** — the anomaly text says so explicitly. Per-target block I/O comes from `biosnoop` when available.

**Reproducible workload:** [testing.md §7](./testing.md) (analytics, diff, AI).

---

## 6. Compare two runs

**When you want:** to see what changed between two runs — a regression, a before/after, a good-vs-bad.

**Do:** right-click a run in the sidebar → **"Compare with… ▸"** → pick another run (needs at least two runs to exist). A **diff tab** opens.

**You see:** side-by-side **Memory**, **CPU**, **Syscall**, and **Anomaly** diff panels, plus a **"what changed"** AI summary you generate on demand. The diff summary streams live and is **regenerated every time** (not cached) via `GET /diff/{a}/{b}/ai-summary/stream` — so it needs the LLM configured (§7).

---

## 7. AI summaries and configuring the LLM

**When you want:** plain-English run summaries and diff explanations. This is **optional** — every run is fully analyzed by the rule engine without it.

**Configure it:** open **Settings ▸ AI / LLM** and set:
- **Base URL** — any OpenAI-compatible endpoint (`/chat/completions` + `/models`). The default example is Google's `https://generativelanguage.googleapis.com/v1beta/openai`.
- **Model**
- **API key** — stored **only** in the file-based secret store (`~/.opentrace/secrets/`, mode 0600), never in `config.json` or git.

Use **Test connection** (a fast `/models` probe, no tokens generated) before saving.

**Security behavior to know:** if you change the **Base URL** without also entering a new key in the same save, the stored key is **deleted** — a key entered for one host is never silently forwarded to a different one. Changing only the model does *not* clear the key.

**Use it:**
- **Per-run summary** streams into the **Overview** tab (`GET /runs/{id}/ai-summary/stream`; cached after first generation, re-generate with force).
- **Diff summary** — the "what changed" panel in a diff tab (§6).
- **Continuous incident summaries** — a separate Settings toggle that, in monitor mode, auto-explains each incident (one request per incident).

If the LLM isn't configured, these surfaces simply show that it's unconfigured — nothing else breaks.

---

## 8. Settings ▸ Rules — toggle, tune, and author findings

The rule engine is **signal-gated**: each rule needs either *events* (syscalls) or *metrics*, and only runs when that signal is present. That's why attach/monitor runs (no syscall stream) get the metric-only rules and not the syscall ones.

**Tune the built-ins.** Settings ▸ Rules lists built-ins split into **"Built-in — events (syscall-based)"** and **"Built-in — metrics (CPU/RSS/IO-based)"**. For each rule:
- **Toggle** it on/off.
- Edit its **thresholds** — one numeric input per threshold (labeled with an inferred unit: ms, %, MB, B/s, ratio, count). Edits commit **on blur / Enter**, not per keystroke. A threshold name that isn't valid for that rule is rejected (`400`).

**Author a custom rule.** Use the custom-rule builder:
- **Name**, optional **Description**.
- **Signal** — `metrics` or `events`.
- **Severity** — low/medium/high/critical.
- A signal-dependent trigger: **"Held for at least (ms)"** for metrics (fires when the expression holds for a contiguous span ≥ that long), or **"Fires after N matches"** for events.
- An **Expression** — a sandboxed boolean expression over the fields for that signal. Examples:
  - metrics: `cpu_pct > 90 and syscall_rate < 5`
  - events: `syscall == 'openat' and error == 'ENOENT'`

**Live validation:** as you type (debounced), the expression is checked via `POST /rules/custom/validate` and you see **"✓ valid expression"** or the exact error, plus the live list of available fields. The sandbox allows only comparisons, `and`/`or`/`not`, arithmetic, and `in` — **no function calls, no attribute access**, so nothing beyond the listed fields is reachable. Save is enabled only when the name is non-empty and the expression validates.

Custom rules are **global** (one ruleset applied to every run), and their anomalies are tagged `custom:{id}` to distinguish them from built-ins.

**Reproducible reference:** the rule/threshold behavior is exercised in the backend suites (`test_rules*.py`, `test_rules_custom.py`); see [testing.md](./testing.md) for end-to-end runs.

---

## 9. Sessions & managing runs

**Sessions** are projects — a way to group runs.

- **Create:** `Ctrl+N` (or the sidebar's "Ctrl+K" hint / command palette).
- **Select:** click a session name. New runs land in the **active** session.
- **Rename:** double-click the session name.
- **Collapse:** the ▾ chevron on a session header (persisted locally).

**Runs** live under their session, newest first, each with a severity-colored dot, its label (or the command), start time, duration, and a status pill.

- **Open:** click it, or right-click → **Open**.
- **Rename:** right-click → **Rename…**. Clearing the label reverts to showing the command. (Tab-bar and sidebar always render the same label + color for a run by construction.)
- **Compare:** right-click → **Compare with…** (§6).
- **Delete:** right-click → **Delete**, or focus the row and press `Delete`/`Backspace`. A styled confirmation appears (*"Delete run … This permanently removes its data and cannot be undone."*). Deleting a live run aborts its capture threads first, then removes its DB rows and on-disk directory.

---

## Where things live on disk

All data is under `~/.opentrace/` (override with `OPENTRACE_HOME`; `start.sh` pins it to `tmp-opentrace/` for dev). Per run you get the complete compressed record (`events.ndjson.zst`, `metrics.ndjson.zst`, `strace.log`/`ltrace.log`) plus derived JSON artifacts (`flamegraph.json`, `offcpu-flamegraph.json`, `latency.json`, `gc-timeline.json`, `requests.json`, `incidents.ndjson`, …). The **Files** tab exposes these directly. See [structure.md](./structure.md) for the full layout and [ARCHITECTURE.md](./ARCHITECTURE.md) for how the pipeline fits together.

The REST API is the ground truth — every tab is a view over it (`GET /runs/{id}/{events,metrics,syscalls,io,network,processes,logs,profile,flamegraph,offcpu-flamegraph,latency,gc-timeline,requests,anomalies,incidents,ai-summary}`). If a capture didn't run, the corresponding endpoint returns a `{"available": false, "reason": "…"}` stub rather than an error.

**One auth caveat before you curl the live app.** When Electron spawns its own backend it enforces a **per-launch bearer token** (`OPENTRACE_API_TOKEN`, also accepted as a `?token=` query param since SSE can't set headers) on **every request except `/health`** — so hitting the running app's backend by hand needs that token. A manually-run `uvicorn` or an isolated dev/e2e backend sets no token and is **unauthenticated**, which is exactly the workflow to use when you just want to poke the API — spin one up on a spare port (see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) / [SETUP.md](./SETUP.md)) rather than curling the live app.

---

## When something doesn't work

Everything is fail-open, so the usual symptom is a tab that shows a **reason string** instead of data. The most common ones and their fixes:

- **"perf attach denied — raise privileges (sudo sysctl kernel.perf_event_paranoid=1, or grant CAP_PERFMON)."** — lower `perf_event_paranoid` or grant the capability.
- **"{profiler} is not installed — captured the resource timeline only."** — install the runtime's sampler (§2 table).
- **"eBPF needs privileges …"** / **"kernel BTF missing …"** / **"bcc tools not found …"** — see [testing.md §5](./testing.md) for the exact capability response and a sudoers snippet; remember to hit the rescan (`↻`) after installing.
- **"request tracing needs bpftrace + privilege …"** — install bpftrace and grant privilege.
- **Tool checklist stale after installing something** — the capability/tool probes are TTL-cached (30s tools, 60s eBPF); use the `↻` rescan or `?refresh=true` on the endpoint.

Full playbook: [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

### See also

- [README.md](../README.md) — overview & quick start
- [SETUP.md](./SETUP.md) — install, build, run, dev mode
- [ARCHITECTURE.md](./ARCHITECTURE.md) — how the pieces fit
- [structure.md](./structure.md) — file-by-file map
- [testing.md](./testing.md) — manual test playbook with copy-pasteable workloads for every feature
