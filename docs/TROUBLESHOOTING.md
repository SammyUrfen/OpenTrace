# Troubleshooting & Requirements

OpenTrace is a **local-first Linux observability desktop app** (Electron shell + FastAPI backend + React renderer). This page is the requirements-and-failure matrix: for each capability it lists **what it needs**, **what you see when the need is unmet** (quoting the actual reason strings the code surfaces), and **how to fix or interpret it**.

> **The core guarantee: a run is never lost.** Every collector is *fail-open*. A missing tool, denied privilege, absent kernel feature, or unconfigured LLM key never breaks a run — it completes with whatever signals *are* available (at minimum a psutil resource timeline) and a human-readable `reason` string in the relevant tab. If you see an empty tab, look for the muted explanation text next to it; that text is quoted verbatim from the backend below.

**Related docs:** [README.md](../README.md) · [docs/SETUP.md](SETUP.md) · [docs/USAGE.md](USAGE.md) · [docs/ARCHITECTURE.md](ARCHITECTURE.md) · [docs/structure.md](structure.md) (file-by-file map) · [docs/testing.md](testing.md) (manual test playbook).

---

## Contents

1. [Where your data and logs live](#1-where-your-data-and-logs-live)
2. [Setup and launch failures](#2-setup-and-launch-failures)
3. [Backend reachability, the local-only guard, and the bearer token](#3-backend-reachability-the-local-only-guard-and-the-bearer-token)
4. [Launch-trace collectors: strace / ltrace / perf](#4-launch-trace-collectors-strace--ltrace--perf)
5. [`perf` and `perf_event_paranoid`](#5-perf-and-perf_event_paranoid)
6. [Attach-to-PID: uid gate, concurrency cap, port-with-no-listener](#6-attach-to-pid-uid-gate-concurrency-cap-port-with-no-listener)
7. [Per-runtime attach samplers](#7-per-runtime-attach-samplers)
8. [eBPF: kernel, bcc/bpftrace, privilege](#8-ebpf-kernel-bccbpftrace-privilege)
9. [Request tracing (HTTP + DB spans)](#9-request-tracing-http--db-spans)
10. [Container-attach caveat](#10-container-attach-caveat)
11. [LLM key / base_url exfiltration guard](#11-llm-key--base_url-exfiltration-guard)
12. [TTL caches — re-check with `refresh=true`](#12-ttl-caches--re-check-with-refreshtrue)
13. [Implemented but not live-validated](#13-implemented-but-not-live-validated)
14. [Known limitations](#14-known-limitations)
15. [Appendix: reason-string index](#15-appendix-reason-string-index)

---

## 1. Where your data and logs live

Almost every diagnostic starts here.

| Thing | Location | Notes |
|---|---|---|
| All app data (default) | `~/.opentrace/` | Override with `OPENTRACE_HOME` (read fresh on every access — no restart needed). |
| Dev data (via `./start.sh`) | `<repo>/tmp-opentrace/` | `start.sh` **pins** `OPENTRACE_HOME` here unconditionally — exporting your own `OPENTRACE_HOME` before `./start.sh` has **no effect** (only works for a manual `uvicorn`/CLI run). |
| Config | `<home>/config.json` | LLM `base_url`/`model`, tracing collectors, rule thresholds, disabled rules. The API key is **never** here. |
| Secrets (LLM key) | `<home>/secrets/` | Dir `0700`, files `0600`. |
| SQLite index | `<home>/sessions.db` (+ `-wal`/`-shm`) | Curated queryable views; WAL mode. |
| Per-run record | `<home>/sessions/<slug>/runs/<cmd>-<YYYYMMDD>_<HHMMSS>/` | See below. |
| Backend stdout/stderr | Terminal that launched Electron, prefixed `[backend]` | Electron keeps the last 40 stderr lines; crash dialogs show the last 15. |

Inside a run directory:

```
meta.json                 # run summary (totals, peaks, anomalies, max_severity)
events.ndjson.zst         # COMPLETE compressed event archive (source of truth)
metrics.ndjson.zst        # complete metrics archive
strace.log | ltrace.log   # raw trace (launch runs)
flamegraph.json           # on-CPU profile (perf/sampler)
offcpu-flamegraph.json    # off-CPU (eBPF)
latency.json              # runqueue + block-I/O histograms (eBPF)
gc-timeline.json          # Python GC (eBPF + USDT)
requests.json             # request-tracing rollup (RED table + sample spans)
incidents.ndjson          # monitor-mode incident feed
profile.json              # malloc/leak ledger (ltrace runs)
artifacts/
```

The **REST API is ground truth** for any assertion — the UI is a view over it. Read a run's raw files without the UI (every example in this doc targets an **isolated** backend on `:8090`, not the live `:8000` app — see the isolation rule below for why, and how to start one):

```bash
# List files in a run, then read one (256 KiB cap, path-traversal guarded):
curl -s localhost:8090/runs/<RID>/files
curl -s "localhost:8090/runs/<RID>/file?name=strace.log"
curl -s localhost:8090/runs/<RID>/summary        # meta.json, or {pending:true}
```

> **Isolation rule for any manual/e2e check:** never touch a backend on `:8000` (that is your live app). Start a throwaway backend on a spare port with its own home:
> ```bash
> OPENTRACE_HOME=$(mktemp -d) ~/miniconda3/envs/opentrace-dev/bin/python \
>   -m uvicorn app.main:app --port 8090          # run from backend/
> ```
> **Why not just curl `:8000`?** Beyond the "it's your live data" rule, the Electron-spawned `:8000` backend is **token-gated**: Electron mints a random per-launch `OPENTRACE_API_TOKEN` (32 bytes), injects it into the backend child and renderer, and `ApiTokenMiddleware` **401s any request** lacking `Authorization: Bearer <token>` / `?token=<token>` (only `OPTIONS` and `GET /health` are exempt — see [§3](#3-backend-reachability-the-local-only-guard-and-the-bearer-token)). That token is random and is **never written to any file you can read**, so don't try to fish it out. A manual `uvicorn` run, the pytest suite, and any isolated backend set **no** `OPENTRACE_API_TOKEN` and are therefore **unauthenticated** (the documented local workflow) — which is exactly why the isolated `:8090` backend, not the live app, is the right target for API inspection.

---

## 2. Setup and launch failures

`./start.sh` runs a fixed probe/build/launch sequence and **hard-fails with an exact message** when a prerequisite is missing. Match the message you got:

| Symptom / message | Cause | Fix |
|---|---|---|
| `[opentrace] no python found on PATH — install Python 3.11+ or set OPENTRACE_PYTHON.` | Neither `python` nor `python3` on PATH. | Install Python **≥ 3.11**, or `export OPENTRACE_PYTHON=/path/to/python`. |
| `[opentrace] $OPENTRACE_PYTHON lacks backend deps — activate the opentrace-dev env, run 'pip install -e backend' in your venv, or set OPENTRACE_PYTHON.` | The interpreter can't `import fastapi, uvicorn, psutil, zstandard, aiosqlite, httpx`. | `conda activate opentrace-dev` **before** `./start.sh`, or `pip install -e backend` into whatever env you point at. |
| `[opentrace] npm not found — install Node.js >= 22.12 (and gcc-c++/make for node-pty). See README Quick start.` | No `npm` on PATH. | Install **Node.js ≥ 22.12** (required by Electron 42 + `@electron/rebuild` 4). |
| Native build error mentioning `node-pty` / `node-gyp` during `electron/` install | node-pty **ships no Linux prebuilds** — it always compiles from source; missing C/C++ toolchain. | Fedora: `sudo dnf install gcc-c++ make` · Debian/Ubuntu: `sudo apt install build-essential`. Then re-run `./start.sh`. |

**Do not `sudo npm install` in `electron/`** — it breaks ownership of `node_modules` and the subsequent native rebuild.

### Backend deps, the exact set

Backend hard deps (from `backend/pyproject.toml`): `fastapi>=0.115`, `uvicorn>=0.32`, `psutil>=5.9`, `zstandard>=0.22`, `aiosqlite>=0.20`, `httpx>=0.27`. Install editable so the `opentrace` CLI registers:

```bash
conda create -n opentrace-dev python=3.11 && conda activate opentrace-dev
pip install -e backend        # editable, from a source checkout
./start.sh
```

The `opentrace` CLI **must run from an editable source checkout** — it hard-fails with `opentrace must be run from an editable install of a source checkout (pip install -e backend); see the README.` if it can't find `electron/package.json` two parents up.

### The build is idempotent

`start.sh` rebuilds the frontend **only if `frontend/dist/index.html` is missing**. After a code change to the renderer, either delete `frontend/dist/` or run `cd frontend && npm run build` yourself — a plain `./start.sh` re-run will serve the stale `dist/`. For live renderer development, use the dev path instead:

```bash
cd frontend && npm run dev &            # Vite on :5173
OPENTRACE_DEV=1 ./start.sh              # loads :5173, auto-opens DevTools
```

### The backend spawned but then died

Electron spawns `uvicorn app.main:app` and health-checks `/health`. On repeated crashes it retries with exponential backoff, capped at **3 attempts** (a >60 s healthy stretch resets the counter). When it gives up you get a dialog:

- **`OpenTrace backend failed` / "The backend on port `<PORT>` exited and could not be restarted."** followed by the **last 15 stderr lines** — read those lines; they are the real error (usually a Python import/traceback).
- **`OpenTrace backend failed to start`** — died before `/health` ever answered.

The in-app **`.backend-banner`** mirrors this live: *"Backend crashed — restarting (attempt N/M)…"*, *"Backend could not be restarted — restart OpenTrace (see logs)."*, or *"Backend unreachable — showing cached data; live updates paused."* A merely-*slow* start is not fatal — Electron opens the window anyway and the frontend's reconnecting `EventSource` self-heals.

### "It reused a backend I didn't expect"

Electron does **not** always spawn its own backend. `resolveBackendPort()`:
1. Port 8000 free → spawn there.
2. Port 8000 occupied by a **genuine OpenTrace backend** (verified via `GET /info` returning both `schema_version` and `sessions_dir` — *not* `/health`, which is a generic `{"status":"ok"}`) → **reuse it**, no spawn.
3. Port 8000 occupied by a foreign server → bind a random free port and spawn there.

Setting `OPENTRACE_BACKEND_URL` skips spawning entirely and points Electron at that URL.

---

## 3. Backend reachability, the local-only guard, and the bearer token

Two independent gates sit in front of the API (`backend/app/main.py`). Understanding which one bit you tells you the fix.

### 403 — foreign Origin/Host (`LocalOnlyMiddleware`)

Any request whose `Origin` isn't `null` / `file://…` / `http(s)://localhost|127.0.0.1|[::1]`, **or** whose `Host` isn't localhost-shaped, is rejected:

```
403  forbidden: OpenTrace accepts local clients only
```

This is a deliberate anti-CSRF / anti-DNS-rebinding guard (a "simple" cross-origin request still executes server-side even if the browser withholds the response). Electron (`file://` → `Origin: null`), the Vite dev server, `curl` (no Origin), and the `otrace` shell hook all pass. **Do not reopen wildcard CORS to work around this.** If you're scripting the API, call it from `localhost`/`127.0.0.1` with no cross-origin `Origin` header.

### 401 — missing/wrong bearer token (`ApiTokenMiddleware`)

```
401  unauthorized
```

Key fact: **this middleware is a complete no-op unless `OPENTRACE_API_TOKEN` is set.** Electron generates a per-launch 32-byte token *only when it spawned its own backend*, and injects it into both the backend child and the renderer/`otrace` hook. A **reused** backend (port-8000 case above) or an **external** backend (`OPENTRACE_BACKEND_URL`) gets **no token** — the check is skipped entirely.

That means: a **manual `uvicorn` run, the pytest suite, and every isolated dev/e2e backend run open** (unauthenticated) — this is the documented local workflow, not a bug. Only the Electron-spawned production backend requires the token. When it does, present it as `Authorization: Bearer <token>` **or** `?token=<token>` (SSE/`EventSource` can't set headers, so the query param exists for the stream).

`OPTIONS` and `GET /health` are always exempt from the token check.

### "Backend unreachable" in the UI

The UI's reachability state is derived purely from the SSE connection (`/stream`), not a `/health` poll — "the stream *is* the live connection." If the sidebar's connection dot is off and the banner says *"Backend unreachable — showing cached data,"* the SSE dropped. Check the `[backend]` terminal output; the frontend will reconnect and re-fetch on its own once the backend answers again.

---

## 4. Launch-trace collectors: strace / ltrace / perf

Launch-tracing wraps a command you run in the embedded terminal. The zsh hook rewrites `python app.py` → `otrace -- python app.py`; `otrace` performs the `/runs/start` → `/runs/{id}/pid` → `/runs/{id}/end` handshake. **Every step of `otrace` is fail-open** — a broken step just runs your command untraced but still exits with its real status:

| `otrace` step | If it fails | Result |
|---|---|---|
| `curl` present? | absent | `exec "$@"` — runs untraced |
| `POST /runs/start` | backend down / timeout / non-2xx | `exec "$@"` — untraced |
| response has `run_id` + `log`? | missing | `exec "$@"` — untraced |
| `strace`/`ltrace` on PATH? | absent | runs the **bare** command (still gets a `run_id` + psutil metrics) |
| `perf` enabled but denied? | probed via `perf record -o /dev/null -- true` first | drops the perf layer, keeps tracing |

So a launch run with **no strace installed** still finalizes — `_finalize` sees an empty/absent `strace.log`, yields zero events, and produces a run with **only the psutil resource timeline** (Timeline / Memory / CPU tabs populated; Syscalls / I/O / Network / Logs empty). This specific path surfaces no dedicated reason string — the empty tabs *are* the signal. Install the tool to get syscalls:

```bash
curl -s "localhost:8090/info/tools?refresh=true"   # strace/ltrace/perf presence + version + install hint
```

The `/info/tools` payload carries a per-distro install hint (Fedora → `dnf`, Debian/Ubuntu → `apt`, Arch → `pacman`). Note `perf` is packaged oddly on Debian (`linux-perf`) / Ubuntu (`linux-tools-generic`).

### strace vs ltrace are mutually exclusive (ptrace)

`strace` and `ltrace` are both ptrace-based and **cannot attach to one process at once**. The mutual exclusion is enforced **client-side only** (in `useCollectors.toggle()` — turning `ltrace` on forces `strace` off and vice-versa). There is **no server-side guard**; a caller hitting `/runs/start` directly could set both, which would misbehave. `perf` and `psutil` are independent and run alongside either.

If both are somehow set, `otrace` resolves it: **ltrace wins**, and it runs `ltrace -S -f -ttt -T -o <run>/ltrace.log`.

### ltrace only sees the main binary's PLT calls

ltrace suits **native** (C/C++/Rust) programs. For interpreted programs (Python/Node/Java) it shows little of value — the interesting calls live inside the interpreter, not the main binary's PLT. Use strace (+ perf, or attach with a sampler) for those.

### Two ltrace gotchas that look like bugs but aren't

- **The Logs tab is empty for ltrace runs.** `GET /runs/{id}/logs` unconditionally reads `strace.log`, and stdout/stderr reconstruction relies on strace's `-e write=1,2` hex dump, which ltrace doesn't produce. This is silent fail-open (`[]`), not an error. Use a strace run if you need reconstructed program output.
- **`strace_log_path` in the start response is always `<run_dir>/strace.log`**, even for an ltrace run (which actually logs to `ltrace.log`). The field name is misleading; don't trust it as "the trace log" for ltrace runs.

---

## 5. `perf` and `perf_event_paranoid`

`perf` needs the binary on PATH **and** a low enough `kernel.perf_event_paranoid` to profile your own process tree. The value is surfaced in two independent places:

- **Static detection** (`/info/tools`): if `perf` is installed and `perf_event_paranoid > 2`, a warning is attached: *"perf_event_paranoid={N}; lower it to ≤2 (sudo sysctl kernel.perf_event_paranoid=1) to capture profiles"*. This is a **UI warning only, not a hard gate** — attach still tries perf.
- **At capture time** (attach), if perf is actually denied, the Flamegraph tab shows: **`perf attach denied — raise privileges (sudo sysctl kernel.perf_event_paranoid=1, or grant CAP_PERFMON).`**

Fix:

```bash
sudo sysctl kernel.perf_event_paranoid=1     # temporary
# persist across reboots:
echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf
```

Other perf/profiler fail strings you may see on the Flamegraph tab:

| Reason string | Meaning |
|---|---|
| `{profiler} is not installed — captured the resource timeline only.` | The sampler binary isn't on PATH; run kept its psutil timeline. |
| `{profiler} captured no samples (target idle, or too short a window).` | Target did nothing during the window, or the window was too short. Widen it (up to 120 s) and profile while the target is doing work. |
| `the target exited before profiling could finish.` | The process died mid-window. |
| `could not start {profiler}.` | `Popen` itself threw. |

When no usable profile is produced, `_ensure_flamegraph_reason` guarantees the tab still shows *some* explanation rather than a blank panel.

---

## 6. Attach-to-PID: uid gate, concurrency cap, port-with-no-listener

`POST /runs/attach {pid|port, window_s=20, monitor, ebpf, requests}` profiles an already-running process for a bounded window. Rejections:

| Condition | Response | Interpretation / fix |
|---|---|---|
| Neither `pid` nor `port` | `400 pid or port required` | Supply one. |
| `port` given, nothing listening on it | `404 no listening process on port {port}` | The port isn't held by a `LISTEN` socket you can see; attach by PID instead. |
| `pid` doesn't exist | `400 no such process: {pid}` | Process already gone. |
| **Process owned by another user** (and backend not root) | `400 pid {pid} belongs to another user — attach requires a same-user process` | Attach only your own processes, **or** run the backend as root to profile any process. |
| **17th concurrent attach/monitor run** | `400 too many concurrent attach/monitor runs ({N}) — stop one first` | The cap is `_MAX_ATTACH_ACTIVE = 16` (each spawns profiler + eBPF threads — an unbounded burst is a CPU-exhaustion hazard). Stop a monitor run and retry. |

`window_s` is clamped to **[3, 120] s** — attach runs are always self-terminating.

> **Attach runs have no syscall stream.** They show Overview / Timeline / Memory / CPU / Flamegraph / (Latency, Requests if enabled) / Files — and deliberately **skip** I/O / Network / Processes / Syscalls / Logs / Profiling, because there's no strace behind them. Only **metric-signal rules** fire (`cpu_bound_metric`, `io_wait_metric`, `cpu_throttled`, `rss_near_cgroup_limit`, RSS/CPU trends); event-signal rules are gated off so they can't misfire on absent data. That's expected, not a missing feature.

---

## 7. Per-runtime attach samplers

Attach picks the best available profiler for the detected runtime. A **missing sampler silently degrades to `perf`**, which shows VM/interpreter frames, not your app's functions.

| Runtime | Sampler | Install hint (from the code) |
|---|---|---|
| Python | `py-spy` | `pip install py-spy` |
| Ruby | `rbspy` | `cargo install rbspy` (or download a release) |
| JVM | `asprof` (async-profiler) | install async-profiler (asprof) |
| .NET | `dotnet-trace` | `dotnet tool install -g dotnet-trace` |
| PHP | `phpspy` | install phpspy (github.com/adsr/phpspy) |
| Node | **built-in V8 inspector** (SIGUSR1 → CDP) | none needed |
| native / Go | `perf` | — |

When a known runtime's sampler is missing, the Attach modal row hint reads:

> **`perf shows {label} VM frames, not your app functions — install {tool} ({install}) for real symbols.`**

Install the tool, then re-open the Attach modal (or hit the rescan `↻`) so detection re-probes.

### Runtime-specific caveats

- **Node**: profiled with **no external tool** — OpenTrace signals SIGUSR1 to open Node's inspector and speaks CDP over a hand-rolled WebSocket. Because SIGUSR1's default disposition is *terminate*, it refuses to signal a process that doesn't look like Node: **`target does not look like a Node process — refusing to send SIGUSR1 (it would terminate a non-Node process).`** (This is also why `node_exporter`/`node-agent` are never misdetected as Node.) Very old Node needs `--inspect` at launch: **`Node inspector for pid {pid} didn't open — is this a Node process that accepts SIGUSR1? (very old Node needs --inspect).`**
- **Deno / Bun**: detected as distinct runtimes but **excluded from the inspector path on purpose** — they don't install a SIGUSR1 handler, so signaling would kill them. They fall back to `perf` (VM frames). To profile app frames you'd need `--inspect` at launch — "not an attach-any story."
- **Go**: there is a `"go"` id in the code but **no detector ever produces it** — Go binaries are classified as plain `native` and profiled with `perf`. Treat any claim of "Go gets its own runtime" as false.
- **The attach target list only shows the top ~60 processes by RSS.** A small or short-lived process may never appear. Use the modal's **manual PID/port escape hatch**: type a bare integer, then click the "PID {n}" or "port {n}" button.

Runtime-specific denial reasons (Flamegraph tab): `{profiler} attach denied — needs same-user access (sudo sysctl kernel.yama.ptrace_scope=0, or run as the target's user).` and, for py-spy/rbspy, `{profiler} version mismatch with the target runtime — update {profiler}.`

---

## 8. eBPF: kernel, bcc/bpftrace, privilege

eBPF adds off-CPU flamegraphs, run-queue/block-I/O latency histograms, and Python GC timelines. It is **capability-gated and fail-open**. Probe capabilities:

```bash
curl -s "localhost:8090/runs/attach/ebpf-capabilities?refresh=true"
```

The gate requires **all three** of the following; the first unmet one wins the `reason`:

| Requirement | Check | Reason string when missing |
|---|---|---|
| **Kernel BTF** | `/sys/kernel/btf/vmlinux` exists | `kernel BTF missing (/sys/kernel/btf/vmlinux) — CO-RE eBPF unavailable; use a BTF-enabled kernel.` |
| **Core bcc tools** | `offcputime`, `runqlat`, `biolatency` resolvable | `bcc tools not found ({missing}) — install bcc/bcc-tools.` |
| **Privilege** | root **or** `CAP_BPF`+`CAP_PERFMON` **or** passwordless sudo for the tools | `eBPF needs privileges — run OpenTrace as root, grant it CAP_BPF+CAP_PERFMON, or enable passwordless sudo for the bcc tools (loading kprobe/tracepoint programs needs CAP_BPF+CAP_PERFMON — unprivileged_bpf_disabled does not grant that).` |

`biosnoop` and `pythongc` are optional add-ons and **do not** gate availability.

### The `unprivileged_bpf_disabled=0` myth

Setting `kernel.unprivileged_bpf_disabled=0` does **NOT** unlock eBPF tracing. It only ever permitted socket-filter/cgroup program types for unprivileged users — **never** the kprobe/tracepoint/perf programs OpenTrace loads. The value is reported (informationally) in the capabilities response but is never used to grant privilege. You genuinely need one of: root, `CAP_BPF`+`CAP_PERFMON` (Linux 5.8+), or passwordless sudo.

### The three ways to grant privilege

```bash
# (a) run the whole app as root — simplest, broadest
sudo ./start.sh

# (b) grant CAP_BPF+CAP_PERFMON to the OpenTrace BACKEND process itself (no root)
#     The probe reads /proc/self/status CapEff of the *running backend* — NOT the
#     bcc tool binaries — and requires CAP_BPF (bit 39) + CAP_PERFMON (bit 38) there.
#     So `setcap` on /usr/share/bcc/tools/* alone does NOTHING for the gate: the
#     ebpf-capabilities probe still reports unavailable (and the Attach modal keeps
#     the eBPF toggle disabled). The caps must be *effective in the backend*, e.g.:
sudo setcap cap_bpf,cap_perfmon+eip "$(readlink -f ~/miniconda3/envs/opentrace-dev/bin/python)"
#     (setcap the interpreter that runs `uvicorn app.main:app`), or launch the
#     backend under a wrapper granted ambient CAP_BPF+CAP_PERFMON.
#     unprivileged_bpf_disabled is NOT enough.

# (c) passwordless sudo for exactly the bcc/bpftrace tools (least privilege)
#     see docs/testing.md §5 for the exact sudoers snippet
```

The privilege probe checks these in order: root (`geteuid()==0`), then `CAP_BPF`+`CAP_PERFMON` **held by the backend process** (`_has_bpf_caps()` reads its own `/proc/self/status` `CapEff`), then passwordless sudo. That last check, `_sudo_ok()`, runs `sudo -n <tool> -h` against the *actual* tool binary — not a generic `sudo -n true` — so it neither false-positives on unrelated NOPASSWD rules nor false-negatives on a least-privilege sudoers that whitelists only the tool paths.

### Why bpftrace is preferred, and why `-p PID` is never used

On very new kernels, bcc's bundled headers fail to compile most tools (`runqlat`/`biolatency`/`pythongc` hit a `struct filename` assert) — **only `offcputime` reliably survives on bcc**. So when `bpftrace` is available, OpenTrace runs the latency histograms + GC as **one combined bpftrace (CO-RE) program** instead of three separate bcc tools (one program avoids concurrent CO-RE compiles wedging each other). `offcputime` always stays on bcc.

Critically, that program is run **without `-p PID`**: bpftrace's `-p` silently applies an implicit pid filter to **every** probe, which would kill the system-wide `sched:*`/`block:*` tracepoints the run-queue/block-I/O/off-CPU decomposition depends on. Per-target scoping uses an in-script `/pid == PID/` filter (for USDT) instead. If you're extending the eBPF code, **do not add `-p PID`** to these programs.

### After installing bcc/bpftrace, re-check

Capabilities are TTL-cached (60 s), and the Attach modal caches the probe per-backend on the client. After installing tools, force a fresh probe:

```bash
curl -s "localhost:8090/runs/attach/ebpf-capabilities?refresh=true"
```

In the Attach modal, click the **`↻` rescan** button next to the eBPF checkbox — it bypasses both the client cache and the backend TTL.

### eBPF capture reasons and thresholds

Capture-time fail-open strings (per artifact): `eBPF denied — needs root / CAP_BPF+CAP_PERFMON (Operation not permitted).`, `bcc tool not found — install bcc-tools.`, `eBPF capture produced no output.`. Static stubs when eBPF was never requested for a run: *"off-CPU profiling not enabled for this run (attach with eBPF)."*, *"latency profiling not enabled (attach with eBPF)."*

GC timelines are Python-only and need USDT probes: **`no USDT probes on this interpreter — conda/statically-linked python and Node don't ship them; use a --enable-dtrace python build.`** and **`GC tracing is Python-only.`**

Latency-anomaly thresholds (p99): run-queue medium ≥ 10 ms / high ≥ 50 ms; block-I/O medium ≥ 20 ms / high ≥ 100 ms. **`biolatency` (block-I/O) is host-wide, not per-PID** — the anomaly text says so explicitly. Only the biosnoop-derived `block_io_pid` is scoped to your target.

### A stuck sudo-wrapped eBPF tool

The code kills a wedged sudo-wrapped tool by relaying SIGTERM then `sudo -n kill` of the snapshotted child — never a bare SIGKILL of the sudo frontend (that would orphan the root child, which keeps tracing). If a least-privilege sudoers denies `sudo kill`, you'll see a warning: *"eBPF tool child pid … survived kill (sudo kill denied?) — it may keep tracing until it exits on its own."* — it self-terminates when its window elapses.

---

## 9. Request tracing (HTTP + DB spans)

Request tracing (attach only, `requests: true`) adds a per-endpoint RED latency table and attributes each request's time to its DB queries. It has a **deliberately weaker capability gate than the eBPF suite** — it needs only **bpftrace + privilege**, not kernel BTF or bcc tools (the syscall-tracepoint + libpq-uprobe path needs neither):

```bash
curl -s "localhost:8090/runs/attach/request-capabilities?refresh=true"
```

Unavailable → **`request tracing needs bpftrace + privilege — run OpenTrace as root, grant CAP_BPF+CAP_PERFMON, or enable passwordless sudo for /usr/bin/bpftrace.`**

> This is why the Attach modal has **two independent capability probes** (`ebpf-capabilities` vs `request-capabilities`) with separate caches. Request tracing can work on a box where the full eBPF suite is unavailable — don't assume one gates the other.

### Interpreting the empty / partial cases (all fail-open)

| Reason string (Requests tab) | Meaning | What to do |
|---|---|---|
| `No HTTP/1.x requests were observed on the target during the window (idle server, HTTP/2 endpoint, or a non-HTTP process).` | Valid **empty** result — not a failure. | Send traffic during the window; confirm it's plaintext HTTP/1.x. |
| `DB spans unavailable — the target maps no dynamically-linked libpq / libmysqlclient / libsqlite3 (a statically-bundled psycopg2-binary, or an asyncpg/pure-wire driver). Endpoint timings are still shown.` | The DB client is statically bundled or pure-wire, so uprobes can't attach. | Endpoint RED metrics still work; DB attribution just won't appear. Use a dynamically-linked DB client if you need query spans. |

DB-client resolution is per-target and independent per engine: Postgres via `libpq` (exports `PQexec`), MySQL/MariaDB via `libmariadb`/`libmysqlclient` (`mysql_real_query`), SQLite via `libsqlite3` (`sqlite3_step`). TLS plaintext is recovered via `libssl` — the program emits only the SSL symbol variant the target actually exports (`SSL_read` vs `SSL_read_ex`), because probing an absent symbol aborts the whole bpftrace program.

**Structurally unsupported: HTTP/2 and gRPC.** They can't be recovered via mid-stream attach — HPACK is a stateful codec whose dynamic table was built by frames you never observed. Such endpoints degrade to connection-level RED. SQL text is PII-scrubbed (literals → `'?'`, numbers → `?`); raw literal values are never persisted.

---

## 10. Container-attach caveat

Container awareness is **pure `/proc` parsing — no root, no docker/podman socket.** `POST /runs/attach/resolve {container_pid, container_id?}` maps a container-local PID to a host PID by scanning `/proc/*/status` `NSpid:` lines.

**Caveat worth knowing:** `resolve` (and the `target_info` it returns) is **not uid-filtered** — it can return cmdline/runtime/container labels for *any* container-local PID on the host. But **attaching to profile it is still blocked by the same-uid gate** in §6 unless the backend runs as root. So you can *see* a foreign container's process metadata, but you can't attach to it as a non-root user.

---

## 11. LLM key / base_url exfiltration guard

AI summaries need an OpenAI-compatible endpoint. `is_configured()` requires **all three**: `base_url`, `model`, **and** a non-empty key from the file-based secret store. The key lives **only** in `~/.opentrace/secrets/llm_api_key` (mode `0600`) — never in `config.json` or git.

- **Not configured** → `stream_chat` yields `{"type":"error","message":"LLM is not configured"}` immediately and makes no HTTP call. The AI summary panels just stay empty; everything else (rule engine, flamegraphs) works fully without a key.
- **HTTP failures** are surfaced, never raised: `HTTP {status}: {detail[:300]}`, `request failed: {e}`.
- **Test without generating tokens:** `POST /config/llm/test` GETs `<base>/models` and reports whether your configured model name matches.

### The exfiltration guard (this surprises people)

**Changing `base_url` to a different value without supplying a new `api_key` in the same request deletes the stored key.** Rationale: the stored key is *bound* to the host it was entered for — a redirected `base_url` must never silently receive a key entered for the old host. So:

- Change `base_url` **and** paste the key together → key saved for the new host. ✅
- Change `base_url` alone → **stored key is cleared**; you must re-enter it. ⚠️
- Change **`model` alone** → key is **not** cleared (only `base_url` changes trigger the guard). ✅

If your AI summaries suddenly stop working after you edited the endpoint URL in Settings, re-enter the API key.

---

## 12. TTL caches — re-check with `refresh=true`

Several probes are cached, so an install-then-recheck within the TTL can serve stale "not installed." Bypass with `refresh=true`:

| Cache | TTL | Refresh |
|---|---|---|
| Tool detection (strace/ltrace/perf) | 30 s | `GET /info/tools?refresh=true` |
| eBPF capabilities | 60 s | `GET /runs/attach/ebpf-capabilities?refresh=true` |
| bpftrace availability | 60 s | folded into the above `refresh=true` |
| Request-tracing capability | delegates to bpftrace | `GET /runs/attach/request-capabilities?refresh=true` |

In the UI, the Attach modal's eBPF/requests checkboxes have a **`↻`** button that forces both the client-side and backend refresh. Whenever you install a tool or change a sysctl/capability, re-probe before assuming it "still doesn't work."

---

## 13. Implemented but not live-validated

Be honest about what has and hasn't been proven end-to-end on real workloads:

- **MySQL/MariaDB request DB spans** (`mysql_real_query` uprobe): symbol-correct and unit-tested, but **not live-validated** (no `mysqld` on the development box). Postgres (libpq via psycopg2 → system `libpq.so`) and SQLite (in-process) *are* validated end-to-end.
- **TLS recovery** (`SSL_read`/`SSL_read_ex` uprobes) and the **MySQL/SQLite** span probes carry design rationale but no equivalent "validated e2e" claim beyond Postgres.
- **.NET (`dotnet-trace`) and PHP (`phpspy`) attach samplers**: implemented but flagged unverified in `docs/testing.md` (need a real .NET app / php-fpm to exercise).
- The `dotnet-trace` fold source is matched by globbing the newest `*.speedscope.json` in the run dir — a filename-matching fragility if multiple such artifacts ever coexist.

None of these break the guarantee: if a probe finds nothing, the run finalizes with a reason string and the psutil timeline.

---

## 14. Known limitations

- **Linux only.** The tracing/profiling stack (strace/ltrace/perf, ptrace, eBPF, `/proc` parsing) is Linux-specific.
- **No packaged installer yet.** `.deb`/`.AppImage` are not done — run from source via `./start.sh`.
- **Single machine, single user.** The API accepts only local callers (403 on foreign Origin/Host). Attach is limited to your own processes unless the backend runs as root.
- **Bash is not auto-traced.** Only zsh gets transparent auto-wrap (its `accept-line` widget can rewrite the command before parse). Bash's readline can't rewrite the accepted line from a `bind -x` handler, so bash users opt in per-command with `ot <cmd>`. This is the honest Phase-1 scope for bash, not a bug.
- **Unsupported shells** (fish/nushell/etc.) get a plain working terminal with tracing disabled — a banner says so: *"auto-tracing hooks support zsh/bash — terminal works, tracing disabled for <shell>."*
- Several deeper features are **optional and gated**: per-runtime samplers need their tool installed; eBPF and request tracing need elevated privilege. All fail open.

For the full manual verification matrix (each feature with an inline workload + expected result, plus the eBPF sudoers snippet), see [docs/testing.md](testing.md).

---

## 15. Appendix: reason-string index

Quick lookup — paste a string you saw in the UI to find its cause and the section above.

| Reason string (verbatim) | Where | Section |
|---|---|---|
| `[opentrace] no python found on PATH …` | `start.sh` | [2](#2-setup-and-launch-failures) |
| `[opentrace] $OPENTRACE_PYTHON lacks backend deps …` | `start.sh` | [2](#2-setup-and-launch-failures) |
| `[opentrace] npm not found — install Node.js >= 22.12 …` | `start.sh` | [2](#2-setup-and-launch-failures) |
| `forbidden: OpenTrace accepts local clients only` | 403, `main.py` | [3](#3-backend-reachability-the-local-only-guard-and-the-bearer-token) |
| `unauthorized` | 401, `main.py` | [3](#3-backend-reachability-the-local-only-guard-and-the-bearer-token) |
| `{profiler} is not installed — captured the resource timeline only.` | attach | [5](#5-perf-and-perf_event_paranoid) / [7](#7-per-runtime-attach-samplers) |
| `perf attach denied — raise privileges (sudo sysctl kernel.perf_event_paranoid=1, or grant CAP_PERFMON).` | attach | [5](#5-perf-and-perf_event_paranoid) |
| `{profiler} attach denied — needs same-user access …` | attach | [7](#7-per-runtime-attach-samplers) |
| `{profiler} captured no samples (target idle, or too short a window).` | attach | [5](#5-perf-and-perf_event_paranoid) |
| `perf shows {label} VM frames, not your app functions — install {tool} …` | Attach modal | [7](#7-per-runtime-attach-samplers) |
| `target does not look like a Node process — refusing to send SIGUSR1 …` | Node attach | [7](#7-per-runtime-attach-samplers) |
| `pid {pid} belongs to another user — attach requires a same-user process` | 400, attach | [6](#6-attach-to-pid-uid-gate-concurrency-cap-port-with-no-listener) |
| `too many concurrent attach/monitor runs ({N}) — stop one first` | 400, attach | [6](#6-attach-to-pid-uid-gate-concurrency-cap-port-with-no-listener) |
| `no listening process on port {port}` | 404, attach | [6](#6-attach-to-pid-uid-gate-concurrency-cap-port-with-no-listener) |
| `kernel BTF missing (/sys/kernel/btf/vmlinux) — CO-RE eBPF unavailable …` | eBPF gate | [8](#8-ebpf-kernel-bccbpftrace-privilege) |
| `bcc tools not found ({missing}) — install bcc/bcc-tools.` | eBPF gate | [8](#8-ebpf-kernel-bccbpftrace-privilege) |
| `eBPF needs privileges — run OpenTrace as root, grant it CAP_BPF+CAP_PERFMON …` | eBPF gate | [8](#8-ebpf-kernel-bccbpftrace-privilege) |
| `eBPF denied — needs root / CAP_BPF+CAP_PERFMON (Operation not permitted).` | eBPF capture | [8](#8-ebpf-kernel-bccbpftrace-privilege) |
| `no USDT probes on this interpreter — conda/statically-linked python and Node don't ship them …` | GC timeline | [8](#8-ebpf-kernel-bccbpftrace-privilege) |
| `request tracing needs bpftrace + privilege …` | request gate | [9](#9-request-tracing-http--db-spans) |
| `DB spans unavailable — the target maps no dynamically-linked libpq / libmysqlclient / libsqlite3 …` | Requests tab | [9](#9-request-tracing-http--db-spans) |
| `No HTTP/1.x requests were observed on the target during the window …` | Requests tab | [9](#9-request-tracing-http--db-spans) |
| `LLM is not configured` | AI summary | [11](#11-llm-key--base_url-exfiltration-guard) |

---

*Doc map: [docs/structure.md](structure.md) is the module-by-module source map; [docs/testing.md](testing.md) is the manual test playbook (with the eBPF sudoers snippet and expected capability responses). Architecture rationale lives in [docs/ARCHITECTURE.md](ARCHITECTURE.md); setup and usage walkthroughs in [docs/SETUP.md](SETUP.md) and [docs/USAGE.md](USAGE.md).*