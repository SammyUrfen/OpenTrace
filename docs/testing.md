# OpenTrace — Manual Testing Playbook

Real, reproducible workflows for every major feature. Each scenario ships an **inline
workload** (a small program that exhibits the behaviour), the **exact steps**, and the
**expected result**. Runnable by a human, by Claude, or by a subagent.

> These are *known-good*: the expected outputs are what the current build actually
> produced during verification.

---

## 0. Ground rules & setup

- **Never touch the user's live app on `:8000`.** Every workflow below runs an
  **isolated backend** on a spare port (`8090+`) with its own `OPENTRACE_HOME`, so
  nothing collides with real data.
- Backend Python lives in the conda env `opentrace-dev`.
- `otrace` = the launch-trace CLI wrapper; the REST API is the ground truth for
  assertions (the Electron UI is a view over it).

### Start an isolated backend

```bash
PYBIN="$HOME/miniconda3/envs/opentrace-dev/bin/python"; ENVBIN="$(dirname "$PYBIN")"
export OT_PORT=8090
export OT_HOME="$(mktemp -d)/otrace-home"
cd ~/Codes/python/OpenTrace/backend
OPENTRACE_HOME="$OT_HOME" PATH="$ENVBIN:$PATH" "$PYBIN" -m uvicorn app.main:app \
  --port $OT_PORT --log-level warning &
sleep 3
alias otget='curl -s http://localhost:$OT_PORT'
```

Helper to grab the newest run id:

```bash
latest_run() { curl -s "http://localhost:$OT_PORT/runs?limit=1" | "$PYBIN" -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])'; }
```

Tear down when done: `kill %1` (or the uvicorn PID). Delete `$OT_HOME`.

---

## 1. Launch tracing — `otrace -- <cmd>`

### 1.1 CPU-bound program → flamegraph + hot-function finding

**Workload** (`/tmp/ot_cpu.py`):
```python
def do_sin():
    import math; s = 0.0
    for i in range(8_000_000): s += math.sin(i)
    return s
do_sin(); do_sin()
```

**Steps:** trace it with perf enabled (via the UI wizard: enable *Hardware perf*, run
`python /tmp/ot_cpu.py`), or drive the API. Open the finished run → **Flamegraph** tab.

**Expected:** a flamegraph with `do_sin` dominant; **Overview → Top Findings** shows a
`hot_function` anomaly (`do_sin` ~high self%). Native/Go show real symbols; a plain
CPython shows `do_sin` via py-spy if installed, else VM/C frames.

### 1.2 fd leak → fd-growth finding

**Workload** (`/tmp/ot_fdleak.py`):
```python
import socket, time
held = []
for i in range(300):
    held.append(socket.socket())   # never closed
    time.sleep(0.01)
```

**Expected:** **Overview** flags a file-descriptor growth anomaly; the **Timeline/Memory**
tab shows FD count climbing monotonically to ~300.

---

## 2. Attach to a running process

### 2.1 Attach to a live Python (fail-open baseline)

**Workload** — start a busy loop and note its PID:
```bash
$PYBIN -c 'def burn():
 x=0
 for i in range(3_000_000): x+=i*i
while True: burn()' & echo "target=$!"
```

**Steps:**
```bash
curl -s -X POST http://localhost:$OT_PORT/runs/attach \
  -H 'content-type: application/json' \
  -d "{\"pid\": <TARGET>, \"window_s\": 6}"
sleep 10
otget/runs/$(latest_run)/flamegraph | $PYBIN -m json.tool | head
```

**Expected:** `attach` returns 200; after ~window+finalize the run is `completed`;
flamegraph `supported: true` with real samples (py-spy → real Python frames if
installed; else perf C frames). Killing perf still yields a full psutil timeline
(fail-open).

### 2.2 Attach to a live Node → real JS symbols (no restart)

**Workload:**
```bash
node -e 'function tick(){ let s=""; for(let i=0;i<3e5;i++) s=JSON.stringify({i,r:Math.sqrt(i)}); return s.length }
setInterval(()=>{ for(let k=0;k<20;k++) tick() }, 1);' & echo "target=$!"
```

**Steps:** attach with `window_s: 6` (as 2.1). The picker labels Node as
*"V8 inspector → real JS symbols, no install needed."*

**Expected:** flamegraph shows the real JS function **`tick`** near the top (verified:
`tick` ~98% over millions of samples). Mechanism: SIGUSR1 opens the V8 inspector, we
drive CDP over a WebSocket and fold the `.cpuprofile`. If another Node holds `:9229`,
the target *fails safe* (clear reason) rather than profiling the wrong process.

---

## 3. Live monitor + incident feed

**Workload** — a process that periodically spikes CPU:
```bash
$PYBIN -c 'import time
def render_report():
 x=0
 for i in range(3_000_000): x+=i*i
while True:
 render_report(); time.sleep(0.2)' & echo "target=$!"
```

**Steps:** attach with `"monitor": true, "window_s": 3`. Open the run → **Incidents** tab
(only present for monitor runs) + watch the live **Monitoring** bar; press **Stop** to
finalize.

**Expected:**
- Incidents **collapse per condition** — a recurring CPU spike shows as one row
  `CPU pegged ×N · last Xm ago`, not hundreds of duplicates.
- Each incident has **where** (hot call path, e.g. `<module> → render_report`), a peak
  metric line (CPU/RSS/FD/samples), and optional AI note.
- **Overview → Top Findings == the Incidents feed** (same set; no phantom Overview-only
  finding).
- An **idle** request-driven server (only acts when called) correctly shows
  *"No incidents — looking healthy."* — that's the expected result, not a bug.

---

## 4. Per-runtime samplers (Phase B/C)

| Runtime | Needs installed | Expected "where" |
|---|---|---|
| Python | `py-spy` (`pip install py-spy`) | real Python function names |
| Ruby | `rbspy` | real Ruby frames |
| JVM | `async-profiler` (`asprof`) | real Java methods (else perf shows VM frames) |
| Node/Deno/Bun | nothing (built-in V8 inspector) | real JS (see 2.2) |
| .NET | `dotnet-trace` | real managed frames *(unverified here — needs a .NET app)* |
| PHP | `phpspy` | real PHP frames *(unverified here — needs php-fpm)* |

Check what the picker will use without attaching:
```bash
otget/runs/attach/targets | $PYBIN -c 'import sys,json
for t in json.load(sys.stdin)[:10]: print(t["pid"], t["runtime"], "->", t["sampler"], "|", t["hint"])'
```

---

## 5. eBPF off-CPU + latency (Phase D) — **needs privilege**

eBPF needs root / CAP_BPF+CAP_PERFMON, or passwordless sudo for the bcc tools:

```bash
sudo tee /etc/sudoers.d/opentrace-ebpf >/dev/null <<'EOF'
$(id -un) ALL=(root) NOPASSWD: /usr/share/bcc/tools/offcputime, /usr/share/bcc/tools/runqlat, /usr/share/bcc/tools/biolatency, /usr/share/bcc/tools/biosnoop, /usr/share/bcc/tools/pythongc, /usr/bin/bpftrace
EOF
sudo chmod 0440 /etc/sudoers.d/opentrace-ebpf
```

Confirm the probe sees it:
```bash
otget/runs/attach/ebpf-capabilities | $PYBIN -m json.tool
# expect: "available": true, "use_sudo": true (or is_root/has_caps), "bpftrace": true
```

> **Very new kernels:** bcc's bundled headers can fail to compile `runqlat`/`biolatency`
> (kernel 7.0 hits a `struct filename` `static_assert`). OpenTrace then uses **bpftrace**
> (CO-RE) for the latency histograms — hence bpftrace in the sudoers line above.
> `offcputime` compiles fine and stays on bcc.

### 5.1 Off-CPU flamegraph (where it's BLOCKED)

**Workload** — blocking fsync + sleep:
```bash
python3 -c 'import os,time
def io():
 f=open("/tmp/ot_io.dat","wb"); f.write(os.urandom(1<<20)); f.flush(); os.fsync(f.fileno()); f.close()
while True: io(); time.sleep(0.03)' & echo "target=$!"
```

**Steps:** attach with `"ebpf": true, "window_s": 8`. Open **Flamegraph** → toggle
**Off-CPU**. Also `otget/runs/<id>/offcpu-flamegraph`.

**Expected (verified):** off-CPU flame `supported: true`, header *"Off-CPU flamegraph —
N.NN s blocked"*; the tree descends through the scheduler (`finish_task_switch → schedule
→ …`) into the blocking syscall path (fsync/write, futex, epoll). This is the wait time
on-CPU sampling cannot show.

### 5.2 Latency histograms + rules

Same run → **Latency** tab (present when the run has eBPF). `otget/runs/<id>/latency`.

**Expected:** run-queue histogram (p50/p90/p99/max) and host-wide block-I/O histogram;
`engine: "bpftrace"` or `"bcc"`. If run-queue p99 ≥ 10 ms → a `high_runqueue_latency`
finding; block-I/O p99 ≥ 20 ms → a host-wide `slow_block_io` finding. On an idle box both
sit in the low buckets (healthy).

---

## 6. Phase E — containers, per-PID I/O, GC

### 6.1 Container labeling + host-PID resolution

**Workload** — a containerized process:
```bash
docker run -d --name ot-demo python:3-slim python -c 'import time
while True: [x*x for x in range(100000)]; time.sleep(0.05)'
```

**Steps:**
```bash
otget/runs/attach/targets | $PYBIN -c 'import sys,json
for t in json.load(sys.stdin):
  c=t.get("container") or {}
  if c.get("container"): print(t["pid"], c["runtime"], c["id"], "cpid=", c.get("container_pid"))'
# resolve a container-local PID (e.g. 1) → host pid:
curl -s -X POST http://localhost:$OT_PORT/runs/attach/resolve \
  -H 'content-type: application/json' -d '{"container_pid": 1}' | $PYBIN -m json.tool
```

**Expected:** the container's host process is listed with a `container` block
(`runtime: docker`, short id) and the attach modal shows a `🐳 docker:<id>` badge.
`/resolve` maps the in-container PID to the attachable host PID (via `/proc` NSpid — no
root). Cleanup: `docker rm -f ot-demo`.

### 6.2 Per-PID block I/O (biosnoop)

Same eBPF attach as 5.1 → **Latency** tab → *"This process's block I/O"* card, or
`otget/runs/<id>/latency` → `block_io_pid`.

**Expected:** per-process `count`, `p50_ms`/`p99_ms`, `read_count`/`write_count`,
`bytes_total` — the target's *own* disk latency (vs the host-wide `block_io`
histogram). *(Needs a bcc kernel where biosnoop compiles.)*

### 6.3 GC pauses (USDT) — Python `--enable-dtrace` build only

**Workload** — use the **system** python (has USDT), not conda:
```bash
/usr/bin/python3 -c 'import time
while True: [list(range(80)) for _ in range(80)]; time.sleep(0.02)' & echo "target=$!"
```

Confirm probes exist first (no root needed):
```bash
cd ~/Codes/python/OpenTrace/backend
$PYBIN -c "from app import ebpf; print(sorted(ebpf.usdt_probes(<TARGET>)))"
# expect: ['audit','gc__done','gc__start','import__find__load__done','import__find__load__start']
```

Attach with `"ebpf": true` → **Latency** tab → *"GC pauses (USDT)"* card, or
`otget/runs/<id>/gc-timeline`.

**Expected:** `available: true` with GC events (`duration_ms` each) → collections /
total pause / longest / avg. On a conda or statically-linked python (no USDT) the card
explains why it's empty.

---

## 7. Analytics, diff, AI

- **Analytics tabs** — every run: Timeline, Memory, CPU, and (for syscall runs) I/O,
  Network, Processes, Syscalls (P50/P95/P99 latency), Logs.
- **Diff** — right-click two runs → **Compare** for a side-by-side "better/worse?" view.
- **AI summary** — Overview → AI summary (needs an API key in the secret store, never in
  config/git). **Continuous AI** (Settings) streams per-incident summaries for monitor
  runs.

---

## 8. Quick smoke (all-green baseline)

```bash
cd ~/Codes/python/OpenTrace/backend && $PYBIN -m pytest -q          # backend units
cd ../frontend && npm test -- --run && npx tsc --noEmit && npm run build
```

Expected: backend + frontend suites green, tsc clean, build succeeds.
