# OpenTrace — Setup & Run Guide

OpenTrace is a **local-first Linux observability desktop app** — an Electron shell over a FastAPI backend and a React 19 renderer. It traces and profiles commands you run in an embedded terminal, and attaches to already-running processes, turning syscalls, resource metrics, CPU/off-CPU profiles, and request latency into correlated visual findings.

There is **no packaged installer yet** (`.deb`/`.AppImage` are not built). You run it **from a source checkout** via `./start.sh`. This guide is the copy-pasteable path from a clean machine to a running app.

**Related docs:** [`README.md`](../README.md) (project overview + quick start) · [`docs/USAGE.md`](USAGE.md) (day-to-day workflows) · [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (how the pieces fit) · [`docs/structure.md`](structure.md) (file-by-file map) · [`docs/testing.md`](testing.md) (manual test playbook) · [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md) (when something breaks).

---

## 1. The Linux-only reality

OpenTrace runs on **Linux only**. There is no macOS or Windows build — the whole value proposition is built on Linux kernel facilities: `strace`/`ltrace` (ptrace), `perf`, `/proc`, eBPF (bcc + bpftrace), and cgroup parsing. The Electron shell and React renderer are cross-platform in principle, but the tracing engine is not.

The **core app** (terminal tracing with the psutil resource timeline, the rule engine, flamegraphs from `perf`) works on any mainstream Linux with the base prerequisites. Everything is **fail-open**: a missing tool, a denied privilege, or an absent LLM key degrades to a friendly reason string and a still-complete run — never a crash or a broken run.

### Kernel / distro facts that actually matter

You only need to care about these if you want the **deeper profiling** features (attach samplers, eBPF, request tracing):

| Fact | Why it matters | How to check |
|---|---|---|
| **Kernel BTF** — `/sys/kernel/btf/vmlinux` must exist | Required for CO-RE eBPF (off-CPU, latency, GC). Missing → eBPF suite unavailable. | `ls /sys/kernel/btf/vmlinux` |
| **`CAP_BPF` + `CAP_PERFMON`** (Linux **5.8+**) | eBPF tracing programs need these (or root, or passwordless sudo for the tools). `unprivileged_bpf_disabled=0` does **not** grant them. | `getpcaps $$` |
| **`kernel.perf_event_paranoid`** | If `> 2`, `perf` can't profile your own process tree. Lower to `≤ 2`. | `cat /proc/sys/kernel/perf_event_paranoid` |
| **`kernel.yama.ptrace_scope`** | Affects same-user attach for language samplers (py-spy/rbspy). | `cat /proc/sys/kernel/yama/ptrace_scope` |
| **Very new kernels** (e.g. 7.x) | bcc's bundled headers fail to compile most bcc tools; only `offcputime` survives. OpenTrace prefers **bpftrace (CO-RE)** for the latency histograms when available. | (handled automatically) |

None of this blocks basic use. If a capability is absent, the relevant tab shows an explanation instead of data — see [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

---

## 2. Prerequisites (exact versions)

| Component | Requirement | Notes |
|---|---|---|
| **OS** | Linux | The only supported platform. |
| **Python** | **>= 3.11** | Backend. `requires-python = ">=3.11"` (`backend/pyproject.toml`). Use a conda env (`opentrace-dev`) **or** a venv — not the system `python3`. |
| **Node.js** | **>= 22.12** with **npm** | Required by Electron 42 and `@electron/rebuild` 4. |
| **C/C++ toolchain** | `gcc-c++` + `make` (Fedora) / `build-essential` (Debian/Ubuntu) | **Mandatory** — `node-pty` ships **no Linux prebuilds** and always compiles from source. |
| **node-gyp Python** | covered by your activated Python env | node-pty's native build uses node-gyp, which needs a Python; the activated conda/venv satisfies it. |
| **curl** | Runtime dependency for launch-trace auto-tracing | The shell hooks (`electron/shell-hooks/opentrace-hook.zsh`/`.sh`) and the `otrace` wrapper shell out to `curl` for `/terminals/attach` registration and the `/runs/start`→`/runs/end` handshake. Without `curl`, terminal auto-tracing silently does nothing (the rest of the app still works). |

Backend hard dependencies (installed for you by `pip install -e backend`, from `backend/pyproject.toml`):

```
fastapi>=0.115   uvicorn>=0.32   psutil>=5.9
zstandard>=0.22  aiosqlite>=0.20 httpx>=0.27
```

Dev extra (`[project.optional-dependencies].dev`): `pytest>=8.0`. The install also registers a console script: **`opentrace = app.cli:main`**.

Frontend/Electron toolchain (installed by npm, pinned in `frontend/package.json` / `electron/package.json`): React `^19.2.6`, Vite `^8.0.12`, TypeScript `~6.0.2`, Vitest `^3.2.6`, Electron `^42.0.1`, node-pty `^1.1.0`.

### Install the toolchain

```bash
# Fedora / RHEL / CentOS
sudo dnf install -y gcc-c++ make nodejs npm curl

# Debian / Ubuntu
sudo apt install -y build-essential nodejs npm curl

# Arch
sudo pacman -S --noconfirm base-devel nodejs npm curl
```

Verify Node is new enough (must be ≥ 22.12):

```bash
node --version
npm --version
```

> **Shell requirement for auto-tracing.** Transparent launch-tracing works only when the embedded terminal's shell is **zsh** (full transparent `otrace --` rewrite) or **bash / sh / dash** (opt-in `ot <cmd>` helper). Other shells (fish, nushell, …) get a fully working terminal but tracing is **disabled** with a banner. This is decided by `electron/pty.js` `shellType()`.

---

## 3. Get the source

```bash
git clone <your-fork-or-remote> OpenTrace
cd OpenTrace
```

Everything below assumes the repo root is your working directory unless stated otherwise.

---

## 4. Install the backend (Python)

The backend must be an **editable install from the source checkout** (`pip install -e backend`). This is not optional — the `opentrace` CLI hard-fails if it can't find `electron/package.json` two directories up from `app/cli.py`, i.e. it must run inside the repo tree.

### Option A — conda (recommended, matches the dev convention)

```bash
conda create -n opentrace-dev python=3.11
conda activate opentrace-dev
pip install -e backend            # installs deps + registers the `opentrace` CLI
```

For the test suite, add the dev extra:

```bash
pip install -e "backend[dev]"     # also installs pytest
```

### Option B — venv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e backend
```

Either way, note the interpreter path — you'll reuse it. Throughout this doc, `$PY` means "the Python in your activated env," e.g. `~/miniconda3/envs/opentrace-dev/bin/python`.

```bash
# a handy shell shorthand for the rest of this guide
PY=$(command -v python)
```

---

## 5. Launch with `start.sh` (the normal path)

From the repo root:

```bash
./start.sh
```

`start.sh` installs frontend/electron deps if missing, builds the renderer if needed, and launches the Electron app. On repeat runs it skips the build (idempotent). The first launch triggers a `node-pty` native rebuild (`electron-rebuild -f -w node-pty`) for the target Electron ABI — expect it to take a minute.

### What `start.sh` does, step by step

1. `ROOT` = the script's own directory.
2. Exports `OPENTRACE_LAUNCH_CWD` = the directory you invoked it from (so later `cd`s inside the app don't move the terminal's start dir).
3. Exports `OPENTRACE_PYTHON` by probing `python` then `python3` on `PATH` (override by exporting it yourself first).
4. **Pins** `OPENTRACE_HOME="$ROOT/tmp-opentrace"` — **unconditionally**. This is dev-local data and it **overrides any `OPENTRACE_HOME` you set** before running `./start.sh`. (To point at custom data, use a manual `uvicorn`/CLI run instead — see §7.)
5. **Functional dependency probe** (not a version check): runs `python -c 'import fastapi, uvicorn, psutil, zstandard, aiosqlite, httpx'`.
6. Requires `npm` on `PATH`.
7. Builds the frontend **only if `frontend/dist/index.html` is missing** (`npm ci` first if `node_modules` absent, then `npm run build`).
8. Installs electron deps **only if `electron/node_modules` is missing** (`npm install`, which runs the node-pty rebuild via its `postinstall`).
9. `cd electron && npm start` → `electron .` → `electron/main.js`.

### `start.sh` failure messages (and the fix)

| Message | Fix |
|---|---|
| `[opentrace] no python found on PATH — install Python 3.11+ or set OPENTRACE_PYTHON.` | Activate your env, or `export OPENTRACE_PYTHON=/path/to/python`. |
| `[opentrace] $OPENTRACE_PYTHON lacks backend deps — activate the opentrace-dev env, run 'pip install -e backend' in your venv, or set OPENTRACE_PYTHON.` | Run `pip install -e backend` in the active env (or point `OPENTRACE_PYTHON` at one that has the deps). |
| `[opentrace] npm not found — install Node.js >= 22.12 (and gcc-c++/make for node-pty). See README Quick start.` | Install Node ≥ 22.12 + the C/C++ toolchain (§2). |

> **Do not `sudo npm install` in `electron/`** — it breaks ownership of `node_modules` and the native rebuild. Run npm as your normal user.

### Alternative launcher: the `opentrace` CLI

After `pip install -e backend`, the `opentrace` command is on your `PATH` and does the equivalent checks, then `npm start` in `electron/`:

```bash
opentrace
```

Two differences from `start.sh` worth knowing:

- The CLI defaults the backend interpreter to **`sys.executable`** (the Python that ran `opentrace` — normally your active env), whereas `start.sh` probes `python`→`python3` on `PATH`.
- The CLI **respects an externally-set `OPENTRACE_HOME`** (it does not pin `tmp-opentrace`). It sets `OPENTRACE_LAUNCH_CWD` to your current dir.

Its guard messages:

| Situation | Message |
|---|---|
| Not run from a source checkout | `opentrace must be run from an editable install of a source checkout (pip install -e backend); see the README.` |
| No npm | `Node.js/npm not found on PATH; install Node.js first.` |
| Electron deps missing | `electron/node_modules is missing — run ./start.sh once (or npm install in electron/).` |
| `dist/` missing and not in dev mode | `frontend/dist is missing — run ./start.sh once to build the frontend, or set OPENTRACE_DEV=1 with the Vite dev server running.` |

### What launch actually does (Electron ↔ backend)

`electron/main.js` resolves a backend before opening the window:

- Backend port preference is **8000**. If free → spawns its own backend there.
- If **8000 is already a live OpenTrace backend** (verified via `GET /info` returning `schema_version` + `sessions_dir`) → **reuses it**, spawns nothing. (`/health` alone is not trusted — its `{"status":"ok"}` is generic.)
- If 8000 is held by a foreign server → binds a random free port and spawns there.
- If `OPENTRACE_BACKEND_URL` is set → never spawns; points at that URL.

When it spawns its own backend, main.js generates a **per-launch bearer token** (`crypto.randomBytes(32)`) and passes it to both the backend (`OPENTRACE_API_TOKEN`) and the renderer. A reused/external backend gets **no token** (see §9). The backend is started as:

```
${OPENTRACE_PYTHON:-python3} -m uvicorn app.main:app --port <PORT>   # cwd = backend/
```

Backend crashes trigger exponential-backoff restart (up to 3 attempts; a >60s healthy stretch resets the counter). A permanently-dead backend shows an error dialog with the port and the last 15 stderr lines.

---

## 6. Dev mode (Vite hot reload)

For renderer development, run the Vite dev server and tell Electron to load it instead of the built `dist/`:

```bash
# terminal 1 — Vite dev server on :5173
cd frontend
npm install          # first time only
npm run dev

# terminal 2 — Electron pointed at the dev server
cd OpenTrace
OPENTRACE_DEV=1 ./start.sh
```

`OPENTRACE_DEV=1` makes `createWindow()` load `http://localhost:5173` (Vite's default port; there's no custom `server.port` in `frontend/vite.config.ts`) and auto-opens DevTools. It also enables the dev-only Reload / Toggle DevTools menu items. The renderer receives its backend URL/token from Electron's preload arguments, **not** from Vite env — `vite.config.ts` is minimal (`base: './'` + the React plugin, no backend proxy).

---

## 7. Running the backend standalone

The backend is the ground truth — the UI is a view over its REST API. You can run it alone (for API pokes, pytest-style checks, or debugging):

```bash
cd backend
$PY -m uvicorn app.main:app --port 8000        # normal (matches Electron's spawn)
```

> **Never touch a backend on `:8000` if the desktop app is running** — that's the app's live backend. For any manual check, use an **isolated** backend on a spare port (`8090+`) with a throwaway home so it can't collide with real data:

```bash
OPENTRACE_HOME=$(mktemp -d) $PY -m uvicorn app.main:app --port 8090
```

`paths.home()` reads `OPENTRACE_HOME` fresh on every call, so the env var alone is enough for isolation — no reload needed. A standalone backend never sets `OPENTRACE_API_TOKEN`, so its auth middleware is a complete no-op (unauthenticated local workflow — see §9).

ASGI entry point: `app.main:app` (`FastAPI(title="OpenTrace", version="0.1.0")`). On startup its `lifespan()` runs `paths.ensure_dirs()` → `config.load()` → `db.init()` → `orchestrator.reconcile_orphans()` and logs `opentrace ready: home=… (reconciled N orphan run(s))`.

Kill an isolated backend by port when done (never a broad `pkill`):

```bash
ss -ltnp | grep :8090          # find the pid, then kill it
```

---

## 8. Every `OPENTRACE_*` environment variable

Most of these are set for you (by `start.sh`, `electron/main.js`, or `electron/pty.js`). The ones you'll ever set by hand are marked **[you]**.

| Variable | Meaning | Default |
|---|---|---|
| `OPENTRACE_HOME` **[you]** | Root dir for all on-disk data. Read fresh on every access. `start.sh` **pins** it to `$ROOT/tmp-opentrace`; a manual `uvicorn`/`opentrace` run honors what you set. | `~/.opentrace` |
| `OPENTRACE_PYTHON` **[you]** | Interpreter used to spawn the backend `uvicorn` child. | `start.sh`: probes `python`→`python3`; CLI: `sys.executable`; main.js: `python3` |
| `OPENTRACE_DEV` **[you]** | Load the Vite dev server (`:5173`) instead of `dist/`; open DevTools; enable dev menu items. | unset |
| `OPENTRACE_BACKEND_URL` **[you]** | Point Electron at an already-running backend instead of spawning one. | unset |
| `OPENTRACE_API` | Backend base URL the shell hooks / `otrace` curl against. | `http://localhost:8000` |
| `OPENTRACE_API_TOKEN` | Per-launch bearer token. Set **only** when Electron spawns its own backend; enforced by `ApiTokenMiddleware`. | unset (auth off) |
| `OPENTRACE_LLM_BASE_URL` **[you]** | Runtime override for `config.llm.base_url` (not persisted). | unset |
| `OPENTRACE_LLM_MODEL` **[you]** | Runtime override for `config.llm.model` (not persisted). | unset |
| `OPENTRACE_LAUNCH_CWD` | CWD the app/CLI was launched from (pty start dir). | `$PWD` at launch |
| `OPENTRACE_ENABLE_STRACE` | Master on/off toggle for terminal auto-tracing (`'1'`/`'0'`). | reflects the Settings toggle |
| `OPENTRACE_SESSION` | Active session (project) id for the current terminal. | from `/terminals/attach` |
| `OPENTRACE_TERMINAL` | Active terminal id. | from `/terminals/attach` |
| `OPENTRACE_IN_RUN` | Re-entrancy guard: the run id while `otrace` wraps a command (prevents double-wrap). | set by `otrace` |
| `OPENTRACE_OTRACE` | Absolute path to the `otrace` wrapper script. | `electron/shell-hooks/otrace` |
| `OPENTRACE_RT` | Per-pty runtime-state file the hook sources each prompt (tracing on/off, session id) — so no `export` lines echo into the terminal. | tmp file per pty |
| `OPENTRACE_REMOTE_DEBUG` **[you]** | Chrome DevTools Protocol remote-debugging port for the renderer (Playwright MCP driving). | unset |

**Smoke / test-harness only** (for headless screenshots and e2e isolation — see [`docs/testing.md`](testing.md)):

| Variable | Meaning |
|---|---|
| `OPENTRACE_SMOKE` | Output PNG path; enables headless smoke-screenshot mode, then quits. |
| `OPENTRACE_SMOKE_DELAY` | ms to wait before acting (default 4000). |
| `OPENTRACE_SMOKE_CLICK` | Comma-separated CSS selectors clicked in order before the screenshot. |
| `OPENTRACE_SMOKE_JS` | Arbitrary JS run after the clicks. |
| `OPENTRACE_USERDATA` | Throwaway Electron `userData` dir (isolated profile/localStorage). |
| `OPENTRACE_WIN` | BrowserWindow size as `"WxH"` (default `1280x800`). |

> The bearer token can also ride as a `?token=` query param — used only by `EventSource`/SSE, which can't set custom headers.

---

## 9. Access control (what's on by default)

Two independent guards live in the backend (`backend/app/main.py`), so you're not exposing anything to the network:

- **`LocalOnlyMiddleware`** (always on): rejects with `403 forbidden: OpenTrace accepts local clients only` any request whose `Origin` isn't `null` / `file://…` / `http(s)://localhost|127.0.0.1|[::1]`, or whose `Host` isn't localhost-shaped (DNS-rebinding guard). Electron's `file://`, Vite `:5173`, and plain `curl` all pass.
- **`ApiTokenMiddleware`**: a **no-op unless `OPENTRACE_API_TOKEN` is set**. Manual `uvicorn`, pytest, and isolated dev/e2e backends never set it, so they're unauthenticated (the documented local workflow). Only an Electron-spawned backend gets a token; then every request except `OPTIONS` and `GET /health` needs `Authorization: Bearer <token>` or `?token=<token>`.

This is a single-machine, single-user tool — it only ever accepts local callers. Don't reopen wildcard CORS.

---

## 10. Optional external tools (deeper profiling)

All of these are **optional and fail-open** — OpenTrace detects them at runtime and, if absent, shows an explanation and still gives you the psutil resource timeline. Install only what you need. The Settings → Tracing tools panel (backed by `GET /info/tools`) shows what's detected and a per-distro install hint; the Attach modal probes eBPF/request capabilities live.

### Launch-trace collectors

| Tool | Unlocks | Fedora / RHEL | Debian / Ubuntu | Arch |
|---|---|---|---|---|
| **strace** | Syscalls · I/O · Network · Processes · Logs tabs (default collector) | `sudo dnf install -y strace` | `sudo apt install -y strace` | `sudo pacman -S --noconfirm strace` |
| **ltrace** | malloc/free ledger · library-call hotspots (native C/C++/Rust only; mutually exclusive with strace) | `sudo dnf install -y ltrace` | `sudo apt install -y ltrace` | `sudo pacman -S --noconfirm ltrace` |
| **perf** | CPU flamegraph · function hotspots | `sudo dnf install -y perf` | `sudo apt install -y linux-perf` (Debian) / `linux-tools-generic` (Ubuntu) | `sudo pacman -S --noconfirm perf` |

`perf` also needs `perf_event_paranoid ≤ 2` to profile your own processes:

```bash
sudo sysctl kernel.perf_event_paranoid=1
```

> `strace` and `ltrace` are both ptrace-based and **mutually exclusive** (the UI enforces one at a time). `perf` is an independent sampler and can run alongside either. `ltrace` only sees the main binary's PLT calls — good for native programs, not interpreted ones.

### Per-runtime attach samplers (Phase B)

Picked automatically per detected runtime when you Attach to a PID. Missing ones silently fall back to `perf` (which shows VM/interpreter frames, not your app symbols).

| Runtime | Tool | Install |
|---|---|---|
| Python | `py-spy` | `pip install py-spy` |
| Ruby | `rbspy` | `cargo install rbspy` (or download a release) |
| JVM | `asprof` (async-profiler) | install async-profiler (`asprof`) |
| .NET | `dotnet-trace` | `dotnet tool install -g dotnet-trace` |
| PHP | `phpspy` | install phpspy (github.com/adsr/phpspy) |
| Node | *(none)* — built-in V8 inspector via SIGUSR1→CDP | no install needed |

> Deno/Bun are detected but deliberately **not** CDP-profiled (SIGUSR1 would terminate them) — they fall back to `perf`. The **.NET and PHP samplers are implemented but not live-validated** in the dev environment (need a real .NET app / php-fpm to confirm).

### eBPF suite (Phase D — off-CPU, latency, GC)

Needs `bcc-tools` + `bpftrace`, kernel BTF, **and** privilege (root, or `CAP_BPF`+`CAP_PERFMON`, or passwordless sudo for the tools). Check readiness via the Attach modal or `GET /runs/attach/ebpf-capabilities`.

```bash
# Fedora / RHEL / CentOS
sudo dnf install -y bcc-tools bpftrace

# Debian / Ubuntu
sudo apt install -y bpfcc-tools bpftrace

# Arch
sudo pacman -S --noconfirm bcc-tools bpftrace
```

Grant privilege one of these ways:

```bash
# simplest: run OpenTrace as root (single-user dev box)
# or grant caps to the tools, or add a passwordless-sudo rule for the tool paths
```

> `kernel.unprivileged_bpf_disabled=0` does **not** help — it never permitted tracing programs. See the eBPF sudoers snippet in [`docs/testing.md`](testing.md) §5. After installing tools, re-probe with `?refresh=true` (the capability checks are TTL-cached 30–60s).

### Request tracing (HTTP endpoints + DB)

A **weaker gate** than the full eBPF suite: it needs **only `bpftrace` + privilege** (no BTF, no bcc). Install `bpftrace` as above. DB spans additionally need a **dynamically-linked** `libpq` / `libmysqlclient` / `libsqlite3` in the target (a statically-bundled `psycopg2-binary` or `asyncpg` shows endpoint timings without DB spans).

> Validated end-to-end for **Postgres (libpq) and SQLite**. **MySQL/MariaDB DB spans are symbol-correct and unit-tested but not live-validated** (no mysqld on the dev box). HTTP/2 and gRPC are structurally unsupported via mid-stream attach.

---

## 11. LLM key setup (optional AI summaries)

AI run summaries are optional — every run is fully analyzed by the rule engine without a key. To enable them, configure an **OpenAI-compatible** endpoint (default target is Google's Gemini/Gemma: `https://generativelanguage.googleapis.com/v1beta/openai`).

**In the app:** Settings → **AI / LLM** → set Base URL + Model, paste the API key, **Test connection**, **Save**. The first-run wizard also has an optional AI step.

Key facts about how the key is stored and guarded:

- The API key lives **only** in the file-based secret store at `~/.opentrace/secrets/llm_api_key` (dir mode `0700`, file mode `0600`) — **never** in `config.json` or git. `config.json` stores only `base_url`, `model`, and the fixed secret *name*.
- **Exfiltration guard:** changing the `base_url` to a different host **without** supplying a new key in the same save **deletes** the stored key — a key bound to one host is never silently forwarded to a redirected base. (Changing only the `model` does not clear the key.)
- `GET /config/llm` never returns the key, only `has_key: bool`.

Runtime overrides (not persisted): `OPENTRACE_LLM_BASE_URL`, `OPENTRACE_LLM_MODEL`.

---

## 12. Verify the install works

### Health / identity check

With a backend running (the app's, or an isolated one on `:8090`):

```bash
curl -s localhost:8090/health          # → {"status":"ok"}
curl -s localhost:8090/info            # → {version, home, config_path, db_path, sessions_dir, schema_version, cpu_cores}
curl -s "localhost:8090/info/tools?refresh=true"   # detected strace/ltrace/perf + versions + install hints
```

`/info` (not `/health`) is the true "is this OpenTrace" check — it returns `schema_version` and `sessions_dir`.

### Backend tests (pytest)

```bash
cd backend
$PY -m pytest -q                                   # full suite
$PY -m pytest tests/test_ebpf.py -q                # one module
$PY -m pytest tests/test_rules.py::test_name -q    # one test
```

There are **20** `test_*.py` modules in `backend/tests/` today. (`CLAUDE.md` says "18 test modules" — that figure is **stale**.) Note `test_e2e_scenarios.py` is a *backend* pytest module that runs real workloads under real `strace` — it is unrelated to the Playwright `e2e/` suite despite the name.

### Frontend checks (vitest / tsc / eslint)

```bash
cd frontend
npm test             # vitest run
npm run build        # tsc -b && vite build → dist/
npm run lint         # eslint
npx tsc --noEmit     # fast typecheck-only gate
```

### End-to-end UI scenarios (real Electron app)

```bash
cd e2e
npm install
./run-all.sh          # optional: ./run-all.sh <wave_size>  (default 4 concurrent)
```

Each scenario runs a **fully isolated** Electron+backend instance (its own `mkdtemp` home + userData, its own port). The registry currently holds **175** scenarios across 13 files. (`README.md` says "165 scenarios" — that figure is **stale**; the registry length in `e2e/scenarios/index.js` is authoritative.)

### End-to-end smoke of the whole loop

Launch `./start.sh`, open the embedded terminal, and run a workload — e.g. `python test-files/anomaly_memory_growth.py`. The zsh hook transparently rewrites it to a traced run and it appears in the sidebar with analytics tabs. For a scripted walkthrough of every feature (with inline workloads + expected results), see [`docs/testing.md`](testing.md).

---

## 13. Where your data lives

Under `OPENTRACE_HOME` (default `~/.opentrace`; `start.sh` pins `./tmp-opentrace`):

```
~/.opentrace/
├── config.json                     # base_url, model, tracing config (never the API key)
├── sessions.db                     # SQLite (+ -wal/-shm), curated views
├── secrets/                        # 0700 dir, 0600 files — the LLM key only
└── sessions/<slug>/
    ├── session.json
    ├── terminals/<term-NN>/{history, cwd.txt}
    └── runs/<cmd>-<YYYYMMDD>_<HHMMSS>/
        ├── meta.json
        ├── events.ndjson.zst       # complete compressed event archive
        ├── metrics.ndjson.zst      # complete metric archive
        ├── strace.log | ltrace.log
        ├── flamegraph.json / profile.json / latency.json / requests.json / …
        └── artifacts/
```

The on-disk `.ndjson.zst` files are the complete source of truth; SQLite keeps all metrics but only a **curated** event subset (errors, lifecycle, slow calls, anomaly evidence) to stay small. Full layout: [`docs/structure.md`](structure.md).

---

## 14. Honest limitations

- **Linux only.** No macOS/Windows build.
- **No packaged installer yet.** No `.deb`/`.AppImage` — run from source via `./start.sh`.
- **Single machine, single user.** The API accepts local callers only; attaching to another user's PID is rejected unless the backend runs as root.
- **Deeper features need external tools + privileges.** Attach samplers need per-runtime tools; eBPF needs BTF + bcc/bpftrace + `CAP_BPF`/`CAP_PERFMON` (or root/passwordless-sudo). All fail-open with a reason string.
- **Not live-validated in the dev environment:** .NET (`dotnet-trace`) and PHP (`phpspy`) attach samplers; MySQL/MariaDB request DB spans (symbol-correct + unit-tested only). HTTP/2 and gRPC request tracing are structurally unsupported via mid-stream attach.
- **`start.sh` overrides `OPENTRACE_HOME`** — to use custom data locations, run the backend manually or via the `opentrace` CLI.

When a feature shows a "reason" instead of data, that's the fail-open path doing its job — start with [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md), which maps every reason string to its fix.

---

**Next:** [`docs/USAGE.md`](USAGE.md) walks through tracing a command, attaching to a PID, live monitoring, and reading the analytics. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) explains how the three processes, the trace pipeline, and the rule engine fit together.