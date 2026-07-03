<!--
Strategic roadmap for production profiling + eBPF, researched 2026-07-03.
Answers two maintainer questions: (1) add an eBPF/bpftrace mode? (2) profile
production apps in any language non-intrusively? Grounded in the current code
(perf.py folding, orchestrator, tools.py, FlamegraphTab). See OpenTrace_Roadmap.md
for the overall phase plan; this is the profiling-specific deep dive.
-->

# OpenTrace Profiling Roadmap: eBPF Mode + Universal Attach-to-PID

## 1. Direct answers

**"Should we add an eBPF/bpftrace mode?"** — Yes, but not first. eBPF is the single highest-*capability* item because it is the only collector family that does something sampling/ptrace fundamentally can't: in-kernel off-CPU (blocked-time) flamegraphs and run-queue / block-I/O / syscall-latency **histograms**, plus true zero-restart attach at <1-2% overhead (`profile -F 49 -p PID`, `offcputime -p PID`, `bpftrace -p PID`). It is *not* the highest payoff-to-effort item, because (a) it needs root or `CAP_BPF+CAP_PERFMON` (5.8+) and `/sys/kernel/btf/vmlinux`, and (b) cross-language symbolization of JIT/interpreted stacks from the kernel is the genuinely hard part — a raw eBPF profile of Django shows `_PyEval_EvalFrameDefault`, not your Python functions. Ship it as a later phase once the attach spine and per-language userspace samplers (which symbolize natively) are in place. On this Fedora fc44 / kernel 7.x host with BTF present it will work; `perf_event_paranoid=1` is already fine.

**"Can we profile production apps in ANY language, non-intrusively?"** — Yes, via one architectural change: stop being launch-only and add **attach-to-a-running-PID**, then route every language's sampler through the folded-stack format OpenTrace already parses. Every mainstream runtime has a mature out-of-process or in-runtime sampler that (i) attaches to a live PID with no restart and (ii) emits Brendan-Gregg *collapsed/folded* stacks or a JSON that trivially folds to them: py-spy, rbspy, async-profiler, dotnet-trace, phpspy, perf (native/Go), Node V8 CDP. The honest asterisks: attach needs `ptrace_scope<=1`/same-UID or `CAP_SYS_PTRACE`; JIT/interpreted symbolization needs the *right* tool (an interpreter-aware sampler, not raw perf); and off-CPU/async latency (FastAPI `await`, Node event loop) is invisible to on-CPU sampling — that's what eBPF off-CPU / wall-clock modes are for. So: **yes for CPU flamegraphs across all runtimes today with per-language samplers; latency completeness comes with eBPF + wall-clock modes.**

---

## 2. The unlock: attach + auto-detect + universal folded-stack ingest

This is the linchpin. OpenTrace today only LAUNCHES (`otrace -- cmd` → `perf record -g -F 999 -- inner`). Three additions generalize to every runtime while reusing almost all existing code.

### 2a. Attach lifecycle (`POST /runs/attach`)
A new non-launch code path that reuses the run lifecycle verbatim:
- New endpoint `POST /runs/attach {pid | port | process-name, window_s, profiler_opts}` alongside the existing `/runs/start`.
- Internally calls `orchestrator.start_run()` with a new target kind `attach:PID` (no command spawn), runs the registry's profiler bounded by `window_s` + a hard SIGKILL watchdog, then `end_run()`.
- **`report_pid(run_id, pid)` already takes a bare PID** and its `descendants_only` flag is exactly what we need: pass `descendants_only=False` for a bare attach PID and the existing psutil poller gives the CPU/RSS/FD/threads timeline for the same window, no ptrace, for free.
- Fail-open: if attach is denied (ptrace/paranoid/caps) or the profiler errors, detach cleanly, never leave the target SIGSTOP'd, and fall back to the psutil-only timeline.

### 2b. Runtime auto-detection (`backend/app/attach.py::detect_runtime(pid)`)
Reads `/proc/PID/exe`, scans `/proc/PID/maps` for `libjvm.so` / `libpython*.so` / `libruby*.so` / `node` / `libcoreclr.so` / `beam.smp` / `php-fpm`, plus `/proc/PID/cmdline`, and resolves listening-port→PID via `psutil.net_connections()` / `ss -tlnp`. Returns a runtime id that selects a registry entry. `GET /attach/targets` enumerates candidate PIDs with detected runtime + cmdline for a picker UI.

### 2c. Profiler REGISTRY (mirror the `_TOOLS` table in tools.py)
```
PROFILER_REGISTRY = {
  "native": {cmd: "perf record -p {pid} -g -F 99 -- sleep {n}", fmt: "perf-script"},
  "go":     {cmd: "perf record -p {pid} -g -F 99 -- sleep {n}", fmt: "perf-script"},
  "python": {cmd: "py-spy record --pid {pid} --format raw -d {n} --rate 99", fmt: "collapsed"},
  "ruby":   {cmd: "rbspy record --pid {pid} --format speedscope -d {n}",     fmt: "speedscope"},
  "jvm":    {cmd: "asprof -d {n} -e cpu -o collapsed -f {out} {pid}",         fmt: "collapsed"},
  "dotnet": {cmd: "dotnet-trace collect -p {pid} --duration ... --format speedscope", fmt: "speedscope"},
  "php":    {cmd: "phpspy --rate-hz=99 --limit=0 -P '{pgrep}' -o {out}",      fmt: "collapsed"},
  "node":   {via: "V8 CDP: SIGUSR1 -> 127.0.0.1:9229 -> Profiler.start/stop", fmt: "cpuprofile"},
}
```

### 2d. Universal ingest — refactor `perf.py`, don't rewrite it
The whole downstream (`_to_list` pruning, `_PRUNE_FRACTION`/`_MAX_DEPTH`/`_MAX_HOTSPOTS`, self/total `hotspots`, `perf_anomalies`'s `hot_function` rule, `flamegraph.json`, FlamegraphTab, LLM summary) is format-agnostic and **stays unchanged**. Only the front-end parser is new:

1. **Extract** the accumulator currently inside `fold_perf_script` (the tree-insertion loop + self/total counting) into a shared `_fold_stacks(iter_of_(root_to_leaf_frames, weight))`. `fold_perf_script` keeps `_iter_stacks`/`_FRAME_RE` and feeds `_fold_stacks` with weight=1.
2. Add **`fold_collapsed(text, count_is_usec=False)`** (~15 lines): for each line `rsplit(" ", 1)` → `(stack, count)`, `stack.split(";")` → root→leaf list, feed `_fold_stacks` with `weight=count`. Covers py-spy raw, async-profiler collapsed, phpspy, bpftrace, offcputime `-d -f`.
3. Add **`speedscope_to_stacks(json)`** adapter for rbspy / dotnet-trace: walk `profiles[].samples` (index lists into `shared.frames`) with `weights[]`. **Watch two gotchas:** speedscope samples are ordered *root→leaf* (do NOT apply perf.py's `reversed()`), and dotnet emits one profile per thread → merge them.
4. Add **`fold_cpuprofile(json)`** for Node/Deno/Bun: build id→node / child→parent maps, walk each `samples[]` entry's parent chain, feed `_fold_stacks`.

Tag units (samples vs µs vs bytes) on the artifact so the tab can label On-CPU / Off-CPU(us) / Alloc(bytes); the *tree shape is identical* so no frontend change is required beyond a source/unit badge.

### 2e. Orchestrator + otrace wiring
- In `_finalize` (orchestrator.py), add branches parallel to the existing `collectors.get("perf")` block: `python`/`ruby`/`jvm`/… each fold their output into `flamegraph.json` (+ `offcpu-flamegraph.json`, `latency.json`) via `record_artifact`, then `anomalies.extend(...)`.
- The `otrace` hook's launch path also benefits: when the wrapped argv is `node`/`deno` inject `--perf-basic-prof --interpreted-frames-native-stack` (allowed in `NODE_OPTIONS`) so the *current* perf pipeline symbolizes JS for free; for `java` launches inject the async-profiler `-agentpath`. Attach mode is the new path; launch-mode enrichment is a bonus.

---

## 3. Full per-runtime table

| Runtime / frameworks | Best tool | Attach? | Overhead | Output → flamegraph | Symbol pitfalls |
|---|---|---|---|---|---|
| **Native C/C++/Rust/Zig/Crystal/Nim/Swift-Linux** (nginx, envoy, redis, PG, Actix/Axum) | `perf record -p PID -g -F 99` (fp); `--call-graph dwarf,8192` if `-fomit-frame-pointer` | **yes** | fp ~0.1%; dwarf 0.8-2.8% | **perf-script — reused verbatim** (`fold_perf_script`) | Rust/C++ names mangled → add rustfilt/c++filt to `_clean_sym`; release builds omit FP → dwarf/lbr; stripped → `DEBUGINFOD_URLS` |
| **Go** (net/http, gRPC) | `perf -p PID -g -F 99`; OR `/debug/pprof/profile` (no root) | **yes** | ~0.1-2% | perf-script verbatim; **new `pprof.py`** profile.proto decoder → same tree | FP since 1.7 amd64 (just works); `-ldflags=-s -w` strips → `[unknown]`; perf misses inlined frames (pprof shows them) |
| **JVM** Java/Kotlin/Scala/Clojure (Spring/Quarkus-JVM/Micronaut/Vert.x/Akka) | **async-profiler** `asprof -d N -e cpu -o collapsed <pid>`; `-e wall` for latency; `-e alloc/lock` | **yes** (JVM attach socket, same UID) | ~1-2% at 10ms | **collapsed → `fold_collapsed`** | AGCT avoids safepoint bias & needs no perf-map; Kotlin/Scala/Clojure mangled (`$anonfun$`/`clojure.core$fn__`) → demangle. **HARD NO: GraalVM native-image** (use perf) & OpenJ9 (partial) |
| **CPython** (Django/Flask/FastAPI under gunicorn/uwsgi/uvicorn, Celery) | **py-spy** `record --pid P --format raw --subprocesses --gil` | **yes** | <5% at 99Hz | **raw = collapsed → `fold_collapsed`** | Reads PyFrameObject → real Python names, no JIT map; version-coupled (keep py-spy ≥ prod CPython); `--subprocesses` fans out prefork workers; **async `await`/IO latency invisible** (on-CPU only) |
| **Ruby** (Rails/Sinatra/Puma/Sidekiq/Unicorn) | **rbspy** `record --pid P --format speedscope`; vernier (opt-in deep) | **yes** | low single-digit at 99Hz | **speedscope → `speedscope_to_stacks`** (or its folded writer) | GVL-holder thread only; C-ext frames collapse to `[c function]`; must match Ruby version (`--force-version`); off-CPU/IO invisible → vernier for GVL/GC/all-thread |
| **Node/Deno** (Express/NestJS/Next/Fastify) | V8 CDP: `SIGUSR1`→9229→`Profiler.start/stop`; OR launch `--cpu-prof` / `--perf-basic-prof` | **yes** (SIGUSR1, no restart) | ~1-3% at 1ms | **`.cpuprofile` → `fold_cpuprofile`**; perf path reuses `fold_perf_script` | V8 gives JS names free (no map) but JS-only; perf path needs `/tmp/perf-PID.map` (stale on tier-up) + `--interpreted-frames-native-stack`; SIGUSR1 fixed 9229 **collides across cluster workers** → use `--cpu-prof-dir` per-PID |
| **Bun** (JSC) | `bun --cpu-prof`; `node:inspector` via `--inspect` | yes (inspector) | ~1-3% | `.cpuprofile` → `fold_cpuprofile` | JSC → **`--perf-basic-prof` does NOT exist**; perf can't symbolize Bun JIT — cpuprofile only |
| **.NET** C#/F# (ASP.NET Core, Blazor Server, Minimal API, EF Core) | **dotnet-trace** `collect -p PID --format speedscope` (EventPipe, no root/ptrace) | **yes** (diag IPC socket) | low single-digit | **speedscope → `speedscope_to_stacks`** (one profile per thread — merge) | Managed-only + safepoint-biased (no native/kernel); JIT names resolved free. Native frames need `.NET 10 collect-linux` (root, kernel≥6.4) or perf+`DOTNET_PerfMapEnabled=1`+`EnableWriteXorExecute=0` (**restart**). Same-user + same `TMPDIR` gotcha |
| **PHP** (Laravel/Symfony/WordPress under php-fpm/mod_php/RoadRunner) | **phpspy** `--rate-hz=99 -P 'php-fpm: pool www' -T 16` | **yes** | low single-digit | **collapsed (via `stackcollapse-phpspy.pl`) → `fold_collapsed`** | Walks Zend `current_execute_data` → real PHP frames; needs exact `-V` version; **non-ZTS only** (FrankenPHP/Swoole ZTS → use Excimer ext, needs restart); raw perf sees only `zend_execute_ex` |
| **Erlang/Elixir** (Phoenix/OTP/Ecto/Broadway, RabbitMQ) | perf `-p beam.smp -g -F 99` **iff booted `+JPperf true`**; else remsh `eflambe`/`recon_trace` | perf: **yes but needs boot flag**; remsh: yes no-restart | perf ~1-2%; erlang:trace 50-300× if unscoped | perf-script verbatim; **eflambe folded → `fold_collapsed`** | `+JPperf` is boot-only → unbooted node = `[unknown]`; single `beam.smp` PID (no per-worker); on-CPU mixes all processes (no per-request); NIFs w/o FP truncate |

### Long-tail decision matrix
- **AOT-compiled with symbols + frame pointers** (Go, Rust, C/C++, Zig, Crystal, Nim, Swift, OCaml, Julia, GraalVM native-image) → **`perf -p PID -g` → `fold_perf_script` verbatim.** No FP → `--call-graph dwarf`. Stripped → debuginfod.
- **Interpreted/managed with an interpreter-aware sampler** (Python→py-spy, Ruby→rbspy, PHP→phpspy, JVM→async-profiler, .NET→dotnet-trace, Node→V8 CDP) → **language sampler → `fold_collapsed`/`speedscope`/`cpuprofile`.**
- **Launch-time-only symbol maps** (Perl `Devel::NYTProf`, R `Rprof`, Dart VM service, Lua/OpenResty `stapxx lj-lua-stacks`) → attach degrades to native-C frames only unless started with the flag; honest fallback is **eBPF whole-system** or accept native frames.
- **Anything, zero-setup, one privileged agent** → **eBPF whole-system unwinder** (Parca/Pyroscope/OTel), a later DaemonSet-shaped phase.

---

## 4. eBPF mode — what sampling can't do

Add an `ebpf` collector alongside `{psutil, strace, ltrace, perf}`. Ship **libbpf-tools** CO-RE static binaries (no runtime clang) as the core, `bpftrace 0.20+` for USDT/ad-hoc, bcc as fallback.

**What it uniquely adds:**
- **Off-CPU (blocked-time) flamegraphs** — `offcputime -d -f -p PID N` emits collapsed stacks where value = µs blocked. Feeds `fold_collapsed(count_is_usec=True)` → new `offcpu-flamegraph.json`, surfaced as an "Off-CPU / Wall-clock" toggle in FlamegraphTab. This is the FastAPI-`await`/Node-event-loop/DB-wait latency that on-CPU sampling *cannot* see.
- **Latency histograms** — `runqlat` (scheduler run-queue), `biolatency` (block I/O), syscall latency. Power-of-2 histograms — **do NOT map to flamegraph.json**; need a new `latency.json` + a "Latency" tab. New rules: run-queue p99 high → "CPU oversubscription / noisy neighbor"; block-I/O tail → "slow disk", extending the existing rules engine.
- **Runtime USDT probes** — bundled bpftrace scripts for GC pauses (java/node), query latency (`postgresql:query__start/done`) → timeline events aligned with the existing memory/CPU/syscall timeline.
- **True attach at <1-2%** — `profile -F 49 -p PID` aggregates stacks in-kernel; only the summary crosses to userspace.

**Requirements & honest friction:**
- Root or `CAP_BPF+CAP_PERFMON` (kernel 5.8+); `/sys/kernel/btf/vmlinux` for CO-RE. Add a `GET /system/ebpf-capabilities` probe (kernel ver, BTF present, caps, tools installed) shown in the first-run wizard so the mode greys out with a clear reason — mirror the existing perf-paranoid warning in `tools.py::detect()`.
- 49/99 Hz are prime to avoid lockstep with the timer tick. **Event-driven tools scale with event rate, not sample rate** — `offcputime`/`runqlat`/`syscount` hook scheduler/block/syscall events firing millions/sec on busy hosts. Always bound duration (hard cap), scope with `-p`/cgroup not `-a`, surface a "high-frequency probe" warning.
- Symbolization from the kernel is the hard part: FP-built native + Go = trustworthy on Fedora fc44; **JVM needs a perf-map, interpreted langs show interpreter C frames** — detect and prompt, don't silently render `[unknown]` towers. This is exactly why the userspace samplers in §3 come first.
- `bcc` caps stacks at 127 frames; deep async/framework stacks clip.

**Reuse:** `offcpu.folded`/`oncpu.folded` reuse `fold_collapsed` + all of perf.py downstream. Only `latency.json` + its tab + 2 new rules are net-new.

---

## 5. Phased plan

Each phase is independently shippable; earliest = smallest useful increment reusing the most existing code.

### Phase A — Attach spine + native/Go perf attach *(smallest, highest reuse)*
- `POST /runs/attach {pid|port, window_s}`; `backend/app/attach.py::detect_runtime(pid)`; `GET /attach/targets`.
- Orchestrator branch: `perf record -p PID -g -F 99 -- sleep N` → **`build_flamegraph()` untouched** → `flamegraph.json`.
- `report_pid(pid, descendants_only=False)` piggybacks the psutil timeline.
- Frontend: "Attach to running process" picker reusing **FlamegraphTab unchanged**.
- **Reusable today:** all of perf.py, FlamegraphTab, `perf_anomalies`, run lifecycle, `record_artifact`, psutil poller. **Net-new:** ~1 endpoint + detect + picker. Unlocks non-intrusive profiling of every Go/Rust/C/C++/Zig/Swift server immediately.

### Phase B — Universal folded ingest + Python/Ruby/JVM samplers
- Refactor `fold_perf_script` → shared `_fold_stacks`; add `fold_collapsed` + `speedscope_to_stacks`.
- Registry entries + tools.py detection (`py-spy`, `rbspy`, `asprof`) with install hints + ptrace_scope/same-UID preflight.
- Wall/alloc/lock event selector on FlamegraphTab (async-profiler); source + unit badges.
- **Reusable:** `_to_list`, hotspots, `hot_function` rule, tab. **Net-new:** 2 parsers (~50 lines) + 3 registry entries.

### Phase C — .NET / PHP / Node / BEAM
- `dotnet-trace` (speedscope, per-thread merge) + `dotnet-counters`→time-series; PHP `phpspy` `-P` pool fan-out; Node V8 CDP SIGUSR1 attach + `fold_cpuprofile`; BEAM perf `+JPperf` preflight + remsh `eflambe` lane.
- Go `pprof.py` profile.proto decoder with cpu/alloc/inuse/contention value selector (heap/lock flamegraphs, no root).
- **Net-new:** `nettrace.py`, `fold_cpuprofile`, `pprof.py`, per-runtime preflights.

### Phase D — eBPF on-CPU + off-CPU
- `ebpf` collector shelling libbpf-tools `profile`/`offcputime`; `GET /system/ebpf-capabilities`; capability gating in wizard.
- `offcpu-flamegraph.json` via `fold_collapsed(count_is_usec=True)`; Off-CPU/Wall-clock toggle.
- **Reusable:** entire fold + tab pipeline. **Net-new:** capability probe + off-CPU artifact/toggle.

### Phase E — eBPF latency histograms + USDT + containers
- `runqlat`/`biolatency`/syscall-latency → `latency.json` + new Latency tab + 2 new rules.
- Bundled USDT bpftrace scripts (GC/query) → timeline events.
- Container/k8s: resolve container→host PID via `/proc`/PID-ns; DaemonSet/ephemeral-container eBPF as an explicit later sub-phase.

---

## 6. "Will it be a nuisance?" — candid risks

- **Privileges.** `ptrace_scope` (default 1 on Fedora blocks attaching to non-child same-UID) and `perf_event_paranoid` gate everything. py-spy/rbspy/perf/phpspy need same-UID or `CAP_SYS_PTRACE`. async-profiler/dotnet-trace use cooperative sockets (no ptrace) but need **same UID and same `TMPDIR`** — a real systemd/sidecar gotcha. eBPF needs root or `CAP_BPF+CAP_PERFMON`. Surface all of these as a **preflight in the wizard/Settings** (extend `tools.py::detect()` which already reads `perf_event_paranoid`) with copy-paste `sysctl`/`setcap` fixes — never a blank flamegraph.
- **Symbolization.** No-FP release builds → truncated perf stacks (dwarf is heavier, or eBPF `.eh_frame`). Stripped → debuginfod. JIT (JVM/V8/.NET) → hex unless a perf-map exists; the interpreter-aware samplers dodge this, which is why they're preferred. Prompt when symbols will be poor; don't render `[unknown]` towers silently.
- **JIT/safepoint bias.** JVMTI/JFR-legacy sample only at safepoints (hot loops under-sampled) — prefer async-profiler AGCT. V8/.NET samplers are also safepoint-ish; note it in the source badge.
- **Off-CPU / async blindness.** On-CPU sampling makes an `await`-bound FastAPI endpoint or a parked Node loop look *idle*, not slow. Be explicit in the UI that CPU flamegraphs ≠ latency until eBPF off-CPU / async-profiler `-e wall` lands (Phase B/D).
- **Bounded windows + fail-open.** Every capture MUST self-terminate: profiler `-d/--duration` AND an external SIGKILL watchdog (so a dead OpenTrace can't leave `perf`/`profile` running). On any attach failure, detach cleanly (never leave the target SIGSTOP'd), fall back to the psutil `/proc` timeline. Default 10-30s windows, 49/99Hz, never continuous in prod.
- **Containers/k8s.** v1 = same-host only; a container's `/tmp/perf-PID.map`, symbols, and PID live in its namespace — must resolve host PID and enter the mount/PID ns. Treat DaemonSet eBPF / `kubectl debug` as an explicit later phase, not an implied capability.
- **Prefork fan-out.** gunicorn/uwsgi/puma/php-fpm/Node-cluster fork N workers; attaching the master profiles an idle parent. Enumerate worker PIDs (py-spy `--subprocesses`, phpspy `-P`, or the psutil subtree the poller already walks).

---

## 7. Recommendation

**Build Phase A first: attach-to-PID + `perf record -p` for native/Go.** It is the highest payoff-to-effort item on the board because:

1. It reuses the *most* existing OpenTrace code — `fold_perf_script`, `build_flamegraph`, `perf_anomalies`, FlamegraphTab, the run lifecycle, `record_artifact`, and the psutil poller (which already accepts a bare root PID via `report_pid(..., descendants_only=False)`) all work with **zero changes**. The only net-new surface is one `/runs/attach` endpoint, a `detect_runtime`/`/attach/targets` pair, and a picker screen.
2. It directly kills OpenTrace's stated core limitation ("we launch, we can't attach to a live server") for the entire AOT-native + Go family — the runtimes that need the *least* symbolization work on this Fedora fc44 host (frame pointers on by default, perf installed, paranoid=1).
3. It establishes the attach spine that every subsequent phase (py-spy/rbspy/async-profiler, .NET/PHP/BEAM, eBPF) plugs into unchanged.

Then immediately do the **`_fold_stacks` refactor + `fold_collapsed`** (start of Phase B) — ~50 lines that turn every collapsed-stack sampler in the ecosystem into a supported OpenTrace source. eBPF, despite being the flashiest ask, should be Phase D: its unique value (off-CPU, latency histograms) is real but it carries the worst privilege and symbolization friction, so it lands *after* the userspace samplers have proven the attach flow and given users symbolized flamegraphs for the languages eBPF struggles to symbolize.

Key files to touch: `backend/app/perf.py` (extract `_fold_stacks`, add `fold_collapsed`), new `backend/app/attach.py`, `backend/app/trace/orchestrator.py` (attach branch in `_finalize`), `backend/app/runs.py` (`/runs/attach`, `/attach/targets`), `backend/app/tools.py` (register samplers + preflight), and a picker in the frontend reusing `FlamegraphTab.tsx` as-is.
