# OpenTrace — Product Roadmap & Architecture Specification

> **One-line truth:** OpenTrace is an intelligent magnifying glass for developers — a local-first observability tool that makes complex software behavior *readable*, not just measurable.

**Platform:** Linux (primary). macOS deferred. Windows not planned.  
**Distribution:** Electron desktop app. Self-contained. No browser window.

---

## Table of Contents

1. [Product Philosophy](#1-product-philosophy)
2. [UX Design & Layout](#2-ux-design--layout)
3. [App Flow](#3-app-flow)
4. [System Architecture](#4-system-architecture)
5. [Monitoring & Detection Specification](#5-monitoring--detection-specification)
6. [Analytics & Visualization Specification](#6-analytics--visualization-specification)
7. [LLM Integration Design](#7-llm-integration-design)
8. [Data Model & Storage](#8-data-model--storage)
9. [Tech Stack](#9-tech-stack)
10. [Phase Roadmap](#10-phase-roadmap)
11. [Resolved Decisions](#11-resolved-decisions)
12. [Future Expansion](#12-future-expansion)

---

## 1. Product Philosophy

OpenTrace is **not** an autopilot debugging tool. It is a productivity layer that collapses the feedback loop between "something is wrong" and "I understand what is wrong."

The moment it eliminates:

> A developer spends 3 hours cross-referencing `strace` output, `htop`, `lsof`, log files, and gut instinct — trying to understand why their program is slow, hung, or burning memory.

OpenTrace compresses that to minutes. The guiding principle behind every design decision is: **take labour away from the developer**. Data is never dumped raw. Everything is correlated, ranked, and presented with enough visual context that the developer's eye goes immediately to what matters — not to a wall of numbers.

What OpenTrace explicitly does **not** do:
- Automatically patch or fix code
- Make autonomous decisions about the system
- Replace the developer's judgment

What it does relentlessly:
- Collect low-level system signals without the developer touching a single tracing tool
- Detect patterns that are invisible when looking at any one signal in isolation
- Present findings visually: graphs over tables, highlights over dumps, timelines over logs
- Let the developer immediately act on what they see

---

## 2. UX Design & Layout

### Design Language

OpenTrace's visual design is inspired by Claude.ai — clean, modern, and readable in both dark and light mode. The aesthetic sits between a developer tool and a polished product: not the raw utilitarian look of a terminal, not the over-designed look of a SaaS dashboard. Typography is large and readable, colours are used sparingly and meaningfully (red = problem, amber = warning, green = healthy, blue = neutral data).

The default theme is dark mode. Light mode is a first-class citizen, not an afterthought. Both are defined from shared design tokens — no "light mode as an afterthought" inversion.

---

### Window Layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  OpenTrace  [File]  [View]  [Settings]  [Help]                  [●  ○  ✕]  │
├──────────────────────────────────────────────────────────────────────────────┤
│  Main Tab Bar                                                                │
│  [  python_20240315_143022  ×  ] [  server_20240315_1455  ×  ] [  +  ]     │
├──────────────────────────────────────────────────────────────────────────────┤
│  Secondary Tab Bar  (scoped to the active main tab)                          │
│  [ Overview ] [ Timeline ] [ Syscalls ] [ Memory ] [ I/O ] [ Network ] ...  │
├──────────────────────────────────────────────────────────┬───────────────────┤
│                                                          │                   │
│  Main Content Area                                       │  RIGHT SIDEBAR    │
│  (the selected analytics view)                          │                   │
│                                                          │  ▼  Sessions      │
│  e.g. Overview:                                          │  ────────────     │
│  ┌─────────────────────────────────────────────────┐    │  python_app       │
│  │  ● AI Summary  (streaming in)                   │    │  · 15 Mar 14:30 ● │
│  │                                                 │    │  · 15 Mar 12:10 ○ │
│  │  Top Anomalies                                  │    │  node_server      │
│  │  ┌────────────────────────────────────────────┐│    │  · 14 Mar 18:00 ○ │
│  │  │ ● CRITICAL  FD Leak — 43 FDs never closed ││    │  build_make       │
│  │  │ [Jump to Timeline →]                       ││    │  · 13 Mar 09:45 ● │
│  │  └────────────────────────────────────────────┘│    │                   │
│  │  CPU ▁▃▇▅▂▁  Memory ▁▂▄▆▇▇▆  Stats grid       │    │  ▶  Terminal      │
│  └─────────────────────────────────────────────────┘    │  (collapsed)      │
├──────────────────────────────────────────────────────────┤                   │
│  Bottom Panel                                            │                   │
│  ┌─────────────────────────────┬──────────────────────┐ │                   │
│  │  Terminal                   │  Live Monitor        │ │                   │
│  │                             │  ─────────────────   │ │                   │
│  │  $ python app.py            │  ☑ CPU    ▁▃▅▇▅▃▁   │ │                   │
│  │  Loading config...          │  ☑ Memory ▁▂▃▄▅▅▅   │ │                   │
│  │  Server started on :8080    │  ☑ Syscalls          │ │                   │
│  │                             │  ☑ File I/O          │ │                   │
│  │                             │  ☐ Network           │ │                   │
│  │                             │  ☐ Perf counters     │ │                   │
│  │                             │  ─────────────────   │ │                   │
│  │                             │  ⚠ FD count > 200   │ │                   │
│  │                             │  ─────────────────   │ │                   │
│  │                             │  [■ OpenTrace ON]    │ │                   │
│  │                             │  ● 00:42   134 MB    │ │                   │
│  └─────────────────────────────┴──────────────────────┘ │                   │
└──────────────────────────────────────────────────────────┴───────────────────┘
```

---

### Right Sidebar

The sidebar is on the **right by default**, position configurable by the user. Structured like VSCode's explorer — collapsible sections, not a flat list. Hide/show with `Ctrl+B`.

**Sessions section (expanded by default):**
- Lists all saved sessions grouped by process/command name (basename)
- Each group is a collapsible sub-list of individual runs, sorted newest first
- Each entry shows: timestamp, duration, and a severity dot (● red = CRITICAL/HIGH, ○ amber = MEDIUM, · green = clean)
- Right-click context menu: Open, Rename label, Compare with…, Delete
- Search/filter bar at top of the section
- Clicking an entry opens that session in the main content area (new main tab)

**Terminal section (collapsed by default):**
- Expanding reveals a toggle to show/hide the bottom panel
- Shows current state: Tracing / Idle / OpenTrace OFF
- Collapsing hides the bottom panel

---

### Main Tab Bar

Each open session gets a tab. Tabs show: session filename, severity dot, user label in parentheses if set, and a close button.

**Special tab types:**
- **Welcome tab** — shown on first launch and when no sessions are open
- **Diff tab** — `python_20240315 ↔ python_20240314` — created via "Compare with…" in the sidebar context menu

---

### Secondary Tab Bar

Below the main tab bar, scoped to the active tab.

**Regular session:**
```
[ Overview ] [ Timeline ] [ Syscalls ] [ Memory ] [ I/O ] [ Network ] [ CPU ] [ Processes ] [ Logs ]
```

**Diff tab:**
```
[ Overview Diff ] [ Memory Diff ] [ Syscall Diff ] [ I/O Diff ] [ Anomaly Diff ]
```

---

### Bottom Panel — Terminal + Live Monitor

Split horizontally into two resizable panes.

**Left pane — Terminal:**
- Full terminal emulation (xterm.js + node-pty)
- Rooted at the directory from which `opentrace` was invoked
- User types **normal commands** — no `opentrace run` prefix required
- OpenTrace intercepts the command and wraps it with selected collectors transparently
- The program's stdout/stderr appears exactly as it would in a plain terminal

**Right pane — Live Monitor:**
- Active during a trace and shows final values after session ends
- Live sparkline graphs for each enabled metric
- Collector toggles: ☑ CPU / ☑ Memory / ☑ Syscalls / ☑ File I/O / ☐ Network / ☐ Perf / ☐ ltrace
- Real-time anomaly alerts as they fire: `⚠ FD count exceeds 200 — possible leak`
- **[OpenTrace ON / OFF] master toggle** — OFF = plain terminal, zero overhead, no restart needed
- When tracing: elapsed time, current RSS, pulsing status dot

---

### Session Naming Convention

```
<command_basename>_<YYYYMMDD>_<HHMMSS>
```

Examples: `python_20240315_143022` · `node_20240315_150100` · `a.out_20240315_161500`

User can add a human-readable label on top (never replaces the filename).

---

### Diff View

Right-click a session → "Compare with…" → select second session → opens `A ↔ B` tab.

Secondary tabs for diff: Overview Diff · Memory Diff · Syscall Diff · I/O Diff · Anomaly Diff

Each diff tab is either a side-by-side comparison or overlaid graphs with a ∆ column/shading.

---

## 3. App Flow

### Launching

```bash
opentrace          # Opens the Electron window from any directory.
                   # CWD becomes the terminal root.
```

`opentrace` is a globally installed binary. Running it from anywhere opens the window.

---

### Virtual Environment Compatibility

**When packaged (AppImage/deb):** The Python backend is fully bundled via PyInstaller. The user needs no Python environment. The embedded terminal spawns a login shell (`bash -l`) which sources `~/.bashrc` — including conda init. `conda activate myenv` works normally inside the terminal. Whatever the user activates there is what their programs see. The OpenTrace backend is isolated and invisible to that.

**When running in development:** The developer has two separate concerns that don't interfere:
1. The OpenTrace backend (FastAPI) runs in its own conda env (e.g. `opentrace-dev`)
2. Programs being traced run in whatever env the user activates in the embedded terminal

These are independent. The embedded terminal spawns a new shell that sources rc files, so you can `conda activate another-project-env` inside it and trace programs from that env. The `opentrace` command itself should be invoked from the `opentrace-dev` env, but that's the only constraint.

---

### First Launch

```
opentrace
    │
    ├── Config exists? ──Yes──► Main window (Welcome tab)
    │
    └── No ──► First-Run Setup Wizard
                    │
          Step 1: LLM Configuration
          Base URL / API Key / Model ID
          [Test Connection]  [Skip — use without AI]
                    │
          Step 2: Tracing Defaults
          Collector checkboxes
          [Save & Open OpenTrace]
                    │
          Main window — Welcome tab
```

---

### Running a Trace

1. Developer opens OpenTrace (`opentrace` from any terminal)
2. Types normal run command in the terminal pane — no prefix:
   ```
   python app.py
   ./build/my_binary --config prod.yaml
   node server.js
   java -jar app.jar
   cargo run
   ```
3. OpenTrace wraps the command transparently with selected collectors
4. Live Monitor activates with real-time sparklines
5. Program's output appears in terminal exactly as normal

---

### Short-lived vs Long-running

**Short-lived (exits on its own):** Tracing ends on process exit. Analytics computed, session saved, tab opens automatically.

**Long-running (web server, daemon):** Real-time anomaly alerts fire in Live Monitor. User stops via `Ctrl+C` or `■ Stop` button. Analytics computed, session saved, tab opens.

In both cases the session is saved before AI summary is ready. AI streams in asynchronously.

---

### Session Lifecycle

| Event | Behavior |
|---|---|
| Process exits / trace stopped | Session saved, tab opened, AI summary streams in |
| Same command runs again | New session created. Previous session untouched. |
| User closes a session tab | Session stays in sidebar. Just not open. |
| User deletes a session | Permanently removed. Confirmation dialog. No undo. |
| Storage warning (> 5 GB total) | Banner in sidebar suggesting pruning |
| App restarts | All sessions intact. Previously open tabs restored. |

---

## 4. System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Electron Shell                            │
│                                                                  │
│   ┌────────────────────┐         ┌──────────────────────────┐   │
│   │   React UI         │◄───────►│   FastAPI Backend         │   │
│   │   (renderer proc)  │  REST + │   (Python, main proc)    │   │
│   └────────────────────┘  WebSocket └──────────┬────────────┘   │
│                                               │                  │
│         ┌─────────────────────────────────────┤                  │
│         ▼                   ▼                 ▼                  │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────┐            │
│  │ Trace Engine│   │Analysis Engine│   │  LLM API  │            │
│  └──────┬──────┘   └──────┬───────┘   └───────────┘            │
│         │                 │                                      │
│  ┌──────▼─────────────────▼───────────────────────────┐        │
│  │  SQLite Event Store                                 │        │
│  │  raw events · metrics · anomalies · sessions        │        │
│  └─────────────────────────────────────────────────────┘        │
│                                                                  │
│  ┌───────────────────────────────────────────────────┐         │
│  │  System Tools (subprocess / procfs)               │         │
│  │  strace · ltrace · perf · lsof · psutil · procfs  │         │
│  └───────────────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────────────┘
```

The FastAPI server starts as a child process of Electron's main process. React communicates with it over localhost HTTP (REST) and WebSocket. The developer sees only a native app.

---

### Trace Engine

```python
# User typed: python app.py
# OpenTrace executes transparently:
subprocess.Popen(
    ["strace", "-f", "-T", "-ttt", "-e", "trace=all",
     f"--output={session_dir}/strace.log", "--",
     "python", "app.py"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
# Simultaneously: psutil poll loop, procfs reader, lsof poller
```

**Collectors:**

| Collector | Data | Sampling |
|---|---|---|
| `strace -f -T` | Every syscall: name, args, return, latency, PID | Every call |
| `psutil` | CPU%, RSS, VMS, threads, FDs, IO counters | Every 250ms |
| `/proc/[pid]/maps` | Memory mappings, heap/stack regions | Every 500ms |
| `/proc/[pid]/fdinfo` + `lsof` | Open FDs with resolved paths | Every 500ms |
| `/proc/[pid]/status` | Context switches, process state | Every 250ms |
| `/proc/net/tcp` | Active TCP connections | Every 500ms |
| `ltrace -f` | Library calls: malloc, free, fopen, etc. | Every call (opt-in) |
| `perf stat -p` | Hardware counters | Aggregated (opt-in) |
| `perf record -g -p` | CPU call graph for flamegraph | Sampled (opt-in) |
| stdout/stderr | Program's own output | Streamed |

---

## 5. Monitoring & Detection Specification

### Anomaly Severity

| Level | Colour | Meaning |
|---|---|---|
| CRITICAL | Red | Almost certainly causing visible problems. |
| HIGH | Orange | Significant inefficiency or likely bug. |
| MEDIUM | Amber | Unusual pattern worth reviewing. |
| LOW | Blue | Informational. |

Every anomaly includes: rule name, severity, occurrence count, time window, total time affected, and evidence event IDs clickable to jump to in the Timeline.

---

### Detection Rules

#### CPU & Execution

| Rule | Detection | Severity |
|---|---|---|
| Spin loop / busy-wait | High CPU + `poll`/`epoll_wait` with timeout=0 in tight loop | HIGH |
| Pure CPU-bound | CPU > 90% sustained > 2s with near-zero syscall rate | MEDIUM |
| Thread starvation | N threads exist, only 1 ever runs (context switch ratio) | HIGH |
| Excessive context switches | > 10,000 voluntary/s | MEDIUM |
| Hot function | Via perf: single function > 30% of CPU samples | HIGH |
| Infinite loop (no progress) | Running > 30s with zero syscalls | CRITICAL |

#### Memory

| Rule | Detection | Severity |
|---|---|---|
| Monotonic memory growth | RSS grows every 5s without ever decreasing | HIGH |
| Memory spike | RSS increases > 100MB within 500ms | MEDIUM |
| Allocation storm | malloc rate via ltrace > 10,000/s sustained | MEDIUM |
| Malloc/free imbalance | malloc count exceeds free count by > 10% at session end | HIGH |

#### File I/O

| Rule | Detection | Severity |
|---|---|---|
| Repeated open/close on same file | Same path opened > 10 times — missing persistent handle | HIGH |
| FD never closed | openat with no matching close by session end | HIGH |
| FD count growing monotonically | Open FD count grows throughout session | CRITICAL |
| Slow file I/O | Single read/write latency > 100ms | HIGH |
| Small read storm | read() with count < 512 bytes at > 500 calls/s | MEDIUM |
| Write amplification | Many tiny writes to same file | MEDIUM |
| Failed file opens | ENOENT or EACCES on openat | MEDIUM |
| Logging inside hot loop | > 1000 writes/s to a log file | MEDIUM |

#### Network

| Rule | Detection | Severity |
|---|---|---|
| DNS not cached | getaddrinfo for same hostname > 5 times | HIGH |
| Slow DNS | getaddrinfo latency > 200ms | HIGH |
| Connection refused / reset | ECONNREFUSED or ECONNRESET | HIGH |
| No connection reuse | New TCP connection per operation to same host | HIGH |
| Blocking network on main thread | Network syscall on main thread with latency > 100ms | HIGH |

#### Processes & Syscalls

| Rule | Detection | Severity |
|---|---|---|
| Excessive subprocess spawning | > 50 execve calls | MEDIUM |
| Single slow syscall | Any syscall latency > 1s | HIGH |
| Mutex contention | futex WAIT with latency > 10ms, repeated | HIGH |
| I/O retry loop | Same syscall on same FD > 100 times within 1s | HIGH |

---

### Function-Level Resource Attribution (opt-in)

When `perf record` or `ltrace` is enabled, a **Function Hotspot Table** appears in the CPU tab: scrollable list sorted by CPU time consumed, with function name, library, CPU%, call count, memory allocated, and average duration. Clicking any row filters the Timeline to that function's events.

---

### Visual Highlighting Conventions

| Signal | Visual Treatment |
|---|---|
| CRITICAL / HIGH anomaly | Red background tint on row, card, or graph region |
| MEDIUM anomaly | Amber left border or underline |
| LOW | Blue dot or subtle label |
| Metric above threshold | Value text coloured red/amber, bolded |
| Anomaly time window on graph | Vertical shaded region |
| Anomaly callout | Small annotation pinned to graph at first occurrence |
| Log line with anomaly | Coloured left border |
| Failed syscall | Red text, strikethrough on return value |

---

## 6. Analytics & Visualization Specification

Each secondary tab is a visualization with one primary insight, supporting detail below or on click.

### Overview Tab
- **AI Summary card** (streams in)
- **Top Anomaly cards** — each with "Jump to Timeline →"
- **Execution Snapshot** — metric grid with sparklines

### Timeline Tab
- Zoomable, scrollable D3 waterfall
- Swimlanes: Syscalls · Memory · CPU · I/O · Network · Signals · Anomalies
- Anomaly windows shaded across all swimlanes simultaneously
- Click any event → full detail panel + linked events

### Syscall Explorer Tab
- Sortable table: name, count, total latency, avg, P50/P95/P99, errors, % of runtime
- Latency distribution histogram for selected syscall

### Memory Tab
- RSS + VMS time series, overlaid, with shaded area between
- Monotonic growth banner if detected
- Allocation events list (clickable timestamps)

### I/O Tab
- Bar chart: top 15 files by access count
- File access heatmap (one row per path, intensity = access rate)
- Full table with FD leak markers (⊘)

### Network Tab
- Connection timeline (bars from connect to close)
- DNS table with repeated-lookup flags
- Error list (ECONNREFUSED, ETIMEDOUT, etc.)

### CPU Tab
- CPU% over time with threshold lines at 50% and 90%
- CPU vs Syscall rate overlay (answers: CPU-bound or I/O-bound?)
- Function hotspot table + flamegraph (if perf enabled)

### Processes Tab
- Cytoscape.js force graph: node size = CPU, node colour = memory
- Process table + ephemeral processes list

### Logs Tab
- Full stdout/stderr with timestamps
- Anomaly-concurrent lines highlighted with coloured left border
- Full-text search

### Diff Tabs
- Overview: two-column summary, ∆ column, AI diff summary at top
- Memory/CPU: both time series overlaid, delta shaded
- Syscall: per-syscall ∆ count and ∆ latency
- Anomaly: three-column table — only in A / both / only in B

---

## 7. LLM Integration Design

### Provider Compatibility

Any OpenAI-compatible endpoint:

| Provider | Base URL |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| Groq | `https://api.groq.com/openai/v1` |

---

### Prompt Design

The LLM receives a carefully constructed structured summary — not raw event data. This keeps cost low and responses focused. The prompt instructs the LLM to:

1. **What's Wrong** — interpret patterns, not just list them
2. **Why It Matters** — quantify impact ("89% of your runtime")
3. **What to Investigate** — specific files, functions, call patterns
4. **What Looks Fine** — prevent wasted investigation
5. **Confidence** — explicit uncertainty acknowledgment

There is no word limit. Clarity is the goal, not brevity.

---

### Fallback (No LLM)

Every anomaly rule has a template that generates plain-English descriptions from event data. The tool is fully useful without any AI dependency. The LLM adds interpretation; the rule engine provides the findings.

---

### Diff Summary

Separate LLM call for diff view. Prompt includes the delta between anomaly lists and key metric changes. Answers: "What changed between these two runs, and is it better or worse?"

---

## 8. Data Model & Storage

### Layout

```
~/.opentrace/
├── config.json
├── sessions.db
└── sessions/
    ├── python_20240315_143022/
    │   ├── events.ndjson.zst
    │   └── strace.log
    └── ...
```

### Key Tables

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,  -- "python_20240315_143022"
  process_name TEXT NOT NULL,
  command TEXT NOT NULL,
  cwd TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  duration_ms INTEGER,
  exit_code INTEGER,
  exit_signal TEXT,
  label TEXT,
  tags TEXT,           -- JSON array
  ai_summary TEXT,     -- JSON sections
  max_severity TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  timestamp_ms REAL NOT NULL,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  pid INTEGER,
  payload BLOB NOT NULL
);
CREATE INDEX idx_events_time ON events(session_id, timestamp_ms);

CREATE TABLE metrics (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  timestamp_ms REAL NOT NULL,
  cpu_pct REAL, rss_mb REAL, vms_mb REAL,
  open_fds INTEGER, threads INTEGER,
  syscall_rate REAL, io_read_bps REAL, io_write_bps REAL,
  PRIMARY KEY (session_id, timestamp_ms)
);

CREATE TABLE anomalies (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  rule_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  severity_score REAL NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_ids TEXT,
  first_seen_ms REAL,
  last_seen_ms REAL,
  occurrence_count INTEGER
);
```

API key stored in OS keyring (libsecret / Secret Service API). Never in config file.

---

## 9. Tech Stack

### Backend (Python)

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| API server | FastAPI + uvicorn |
| Tracing | strace, psutil, lsof, procfs |
| Profiling | perf, ltrace (opt-in) |
| Database | SQLite + aiosqlite |
| Compression | zstandard |
| LLM client | httpx (async, streaming) |
| Analytics | pandas, numpy |
| Keyring | secretstorage (libsecret) |

### Frontend (Electron + React)

| Component | Technology |
|---|---|
| Shell | Electron |
| Framework | React 18 + TypeScript + Vite |
| Charts | Recharts + D3.js |
| Timeline | Custom D3 waterfall |
| Flamegraph | d3-flame-graph |
| Process graph | Cytoscape.js |
| Terminal | xterm.js + node-pty |
| Styling | Tailwind CSS |
| State | Zustand |
| Real-time | WebSocket |
| Design | Claude-inspired design tokens, dark + light |

### Packaging (Linux)

| Format | Tool |
|---|---|
| `.deb` | electron-builder |
| `.AppImage` | electron-builder |
| Auto-update | electron-updater |

---

## 10. Phase Roadmap

### Phase 0 — Foundation (Week 1)
- [x] Monorepo: `/backend`, `/frontend`, `/electron`, `/docs`
- [x] `opentrace` CLI binary: opens Electron window, passes CWD *(dev launcher `app.cli`; PyInstaller packaging deferred)*
- [x] Electron boots, starts FastAPI backend as child process
- [x] xterm.js + node-pty terminal in bottom panel
- [x] Command interception: wraps execution with strace + psutil when ON
- [x] SQLite initialized on first run
- [x] `config.json` created with defaults
- [x] OpenTrace ON/OFF toggle functional

### Phase 1 — Data Pipeline (Weeks 2–3)
- [x] strace parser → TraceEvent schema
- [x] psutil poller → metrics table
- [x] FD path resolver via procfs
- [x] Event normalization and storage (SQLite + `events.ndjson.zst` / `metrics.ndjson.zst` / `meta.json`)
- [x] Run lifecycle (start, poll, finalize, save) over `/runs/*` + `otrace` wrapper
- [x] *(bonus)* foundational anomaly rule engine + SSE live channel

> **Data model note:** the flat Phase-0 `sessions` table was replaced by the
> three-level **sessions (projects) → terminals → runs** model; events/metrics/
> anomalies/artifacts now key off `run_id`. Interception is a zsh line-rewrite
> (`accept-line` widget → `otrace -- <cmd>`), not the original preexec re-run.
> Live updates use **SSE** rather than WebSocket (no extra native dep).

### Phase 2 — Setup UX + Sidebar (Weeks 4–5)
- [x] First-run wizard (LLM config + collector defaults)
- [ ] API key in libsecret *(file-based secret store exists; keyring swap deferred)*
- [x] Right sidebar: sessions (projects) → runs with severity dots; **create + switch sessions**
- [x] Right-click context menu (Open / Delete a run; middle-click closes a tab)
- [x] Main tab bar + secondary tab bar (real: open runs as tabs, per-run views)
- [x] Overview tab: execution snapshot + top anomaly cards
- [x] Live Monitor: real-time sparklines (SSE) + **collector toggles** (strace/psutil
      functional; ltrace/perf opt-in Phase 6)
- [~] Settings (LLM modal + Test Connection; first-run wizard covers onboarding)
- [x] *(§2)* first-class light/dark theme — **espresso (dark) / warm-paper (light)**,
      shared tokens, toggle, terminal re-themes

### Phase 3 — Analytics Views (Weeks 6–9)
- [x] Secondary tabs with real data — all 9 built: **Overview, Timeline, Memory, CPU,
      I/O, Network, Processes, Syscalls, Logs** (custom SVG timeline + process table)
- [~] Detection rule engine — foundational subset live (7 rules incl. slow-network);
      full §5 set pending
- [x] Severity highlighting (dots, colored anomaly cards, threshold lines, red errors,
      ⊘ fd-leak markers, anomaly-window shading in Timeline + Logs)
- [ ] Real-time anomaly alerts in Live Monitor *(metrics stream live; anomalies at finalize)*

### Phase 4 — LLM Integration (Weeks 10–11)
- [x] Streaming LLM call → sectioned summary in Overview (OpenAI-compatible;
      default Google Gemini/Gemma; thought-chunk filtering for reasoning models)
- [x] Rule-based fallback descriptions (anomaly cards render without an LLM)
- [x] Re-analyze button, error handling, Settings modal (base/model/key + test),
      API key in the secret store (never in config)

### Phase 5 — Diff View (Week 12)
- [ ] "Compare with…" context menu
- [ ] All diff secondary tabs
- [ ] AI diff summary

### Phase 6 — Function Profiling (Weeks 13–14)
- [ ] ltrace integration (malloc/free tracking)
- [ ] perf record integration + flamegraph
- [ ] Function hotspot table

### Phase 7 — Polish & Release (Weeks 15–16)
- [ ] Session export (JSON, HTML report)
- [ ] `.deb` + `.AppImage` build pipeline
- [ ] Auto-update, documentation

### Phase 8 — Advanced (Month 4+)
- [ ] eBPF / bpftrace mode
- [ ] **CI mode:** `opentrace ci ./tests.sh --baseline <session>` — runs OpenTrace non-interactively as part of a CI pipeline (GitHub Actions, etc.) and exits non-zero if performance regressed vs a saved baseline. Useful for catching memory leaks or slowdowns before they merge. Not needed until the tool is stable.
- [ ] Plugin API for custom anomaly detectors
- [ ] VS Code extension

---

## 11. Resolved Decisions

| Decision | Resolution |
|---|---|
| Sessions on re-run | Always new. Previous untouched. |
| strace overhead | Warn if > 30%. No hard cap. |
| Terminal | Embedded bottom panel (xterm.js + node-pty). |
| Max session size | Warn at 100MB. Cap at 500MB (configurable). |
| Platform | Linux only. macOS/Windows deferred. |
| `perf` default | Off. Opt-in (requires `perf_event_paranoid ≤ 1`). |
| `ltrace` default | Off. Opt-in. |
| Network capture | `/proc/net/tcp` polling by default. `tcpdump` opt-in. |
| API key storage | libsecret (Secret Service API — GNOME Keyring / KWallet). |
| Session naming | `<command_basename>_<YYYYMMDD>_<HHMMSS>` |
| Sidebar position | Right by default, user-configurable. |
| Command prefix | None. User types normal commands. OpenTrace wraps transparently. |
| Virtual environments | Embedded terminal spawns `bash -l` (login shell), sources `~/.bashrc`, conda init runs normally. OpenTrace backend and traced programs are independent environments. |

---

## 12. Future Expansion

**Security Runtime Analysis Mode** — the existing data (syscall sequences, file access, network) is sufficient for lightweight sandboxing. Add a security rule profile.

**CI Regression Detection** (Phase 8) — `opentrace ci ./tests.sh --baseline <session>` fails CI if memory or runtime regressed. Baseline is a pinned saved session.

**Educational Trace Replay** — annotated playback for teaching OS concepts or onboarding.

**VS Code Extension** — gutter annotations from the last trace. Requires symbol resolution via perf + DWARF.

---

*This document is the living spec for OpenTrace. Decisions in Section 11 are locked. Everything else is open to revision as you build.*
