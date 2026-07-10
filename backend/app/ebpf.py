"""eBPF collectors — off-CPU flamegraphs + latency histograms (roadmap Phase D).

On-CPU sampling (perf / py-spy / …) shows where a process BURNS cpu; it cannot see
where it's BLOCKED (waiting on I/O, a lock, the DB) or measure kernel latency. eBPF
can, in-kernel and low-overhead. This module drives the bcc tools:

- `offcputime -f -p PID N`  → folded off-CPU stacks (value = µs blocked) → an
  off-CPU flamegraph via `perf.fold_collapsed(count_is_usec=True)`.
- `runqlat -m -p PID N 1`   → scheduler run-queue latency (log2 histogram, ms).
- `biolatency -m N 1`       → block-I/O latency (log2 histogram, ms).

eBPF requires privileges (root, or CAP_BPF+CAP_PERFMON, or
`kernel.unprivileged_bpf_disabled=0`) and kernel BTF. `capabilities()` reports
exactly what's available so the UI can gate the feature and explain any gap; every
capture is fail-open (a missing/denied tool just omits that artifact).
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

_BCC_DIR = Path("/usr/share/bcc/tools")
_TOOL_NAMES = {
    "offcputime": ["offcputime", "offcputime-bpfcc"],
    "runqlat": ["runqlat", "runqlat-bpfcc"],
    "biolatency": ["biolatency", "biolatency-bpfcc"],
    # optional add-ons (do NOT gate `available`): per-PID block I/O + Python GC
    "biosnoop": ["biosnoop", "biosnoop-bpfcc"],
    "pythongc": ["pythongc"],
}
_CORE_TOOLS = ("offcputime", "runqlat", "biolatency")
# CAP_PERFMON = 38, CAP_BPF = 39 (Linux 5.8+)
_CAP_PERFMON, _CAP_BPF = 38, 39


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _find_tool(name: str) -> str | None:
    for cand in _TOOL_NAMES.get(name, [name]):
        p = shutil.which(cand)
        if p:
            return p
        bcc = _BCC_DIR / cand
        if bcc.exists():
            return str(bcc)
    return None


def _has_bpf_caps() -> bool:
    """True if the current process holds CAP_BPF and CAP_PERFMON (effective)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                bits = int(line.split()[1], 16)
                return bool(bits & (1 << _CAP_BPF)) and bool(bits & (1 << _CAP_PERFMON))
    except (OSError, ValueError, IndexError):
        pass
    return False


def _sudo_ok() -> bool:
    """Passwordless sudo available FOR THE BCC TOOLS specifically (a generic
    `sudo -n true` would both false-positive on unrelated NOPASSWD rules and
    false-negative on a least-privilege setup that only allows the tool paths).
    We probe the real tool with `-h`, which prints usage and exits 0 without
    loading any BPF program."""
    if not shutil.which("sudo"):
        return False
    tool = _find_tool("offcputime") or _find_tool("runqlat") or _find_tool("biolatency")
    if not tool:
        return False
    try:
        return subprocess.run(
            ["sudo", "-n", tool, "-h"], capture_output=True, timeout=4
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Host eBPF capability (BTF, tools, privileges) barely changes while the app runs,
# but probing it spawns `sudo -n` subprocesses (~0.3-0.5s) — and the Attach modal
# re-fetches on every open, the monitor loop on every iteration. A short TTL keeps
# it fresh across a sudoers/tool install without an app restart; `refresh=True`
# bypasses. A stale read under a racing refresh is fine (worst case: one re-probe).
_CAPS_TTL_S = 60.0
_caps_cache: tuple[float, dict] | None = None
_bt_cache: tuple[float, bool] | None = None


def capabilities(refresh: bool = False) -> dict:
    """What eBPF profiling this host can do, and why not if it can't.
    TTL-cached (see _CAPS_TTL_S); `refresh=True` re-probes."""
    global _caps_cache
    now = time.monotonic()
    if not refresh and _caps_cache is not None and now - _caps_cache[0] < _CAPS_TTL_S:
        return _caps_cache[1]
    btf = Path("/sys/kernel/btf/vmlinux").exists()
    unpriv = _read_int("/proc/sys/kernel/unprivileged_bpf_disabled")
    tools = {k: _find_tool(k) for k in _TOOL_NAMES}
    tools_ok = all(tools[k] for k in _CORE_TOOLS)  # add-ons don't gate availability
    is_root = os.geteuid() == 0
    caps = _has_bpf_caps()
    sudo = _sudo_ok()
    # Real priv paths for the kprobe/tracepoint/perf programs these tools load:
    # root, CAP_BPF+CAP_PERFMON, or passwordless-sudo the tools. NOTE:
    # unprivileged_bpf_disabled=0 does NOT help — it only ever permitted
    # socket-filter/cgroup program types for unprivileged users, never tracing.
    priv_ok = is_root or caps or sudo

    reason: str | None = None
    if not btf:
        reason = "kernel BTF missing (/sys/kernel/btf/vmlinux) — CO-RE eBPF unavailable; use a BTF-enabled kernel."
    elif not tools_ok:
        missing = ", ".join(k for k, v in tools.items() if not v)
        reason = f"bcc tools not found ({missing}) — install bcc/bcc-tools."
    elif not priv_ok:
        reason = ("eBPF needs privileges — run OpenTrace as root, grant it "
                  "CAP_BPF+CAP_PERFMON, or enable passwordless sudo for the bcc tools "
                  "(loading kprobe/tracepoint programs needs CAP_BPF+CAP_PERFMON — "
                  "unprivileged_bpf_disabled does not grant that).")

    result = {
        "available": bool(btf and tools_ok and priv_ok),
        "reason": reason,
        "kernel": platform.release(),
        "btf": btf,
        "unprivileged_bpf_disabled": unpriv,
        "perf_event_paranoid": _read_int("/proc/sys/kernel/perf_event_paranoid"),
        "is_root": is_root,
        "has_caps": caps,
        "sudo": sudo,
        "tools": {k: bool(v) for k, v in tools.items()},
        "use_sudo": bool(sudo and not (is_root or caps)),
        # bpftrace (CO-RE) is the preferred engine for the latency histograms —
        # it compiles where bcc's bundled headers won't (e.g. very new kernels).
        "bpftrace": bpftrace_available(refresh=refresh),
    }
    _caps_cache = (time.monotonic(), result)
    return result


def tool_cmd(name: str, args: list[str], *, use_sudo: bool) -> list[str] | None:
    """Build the argv for a bcc tool (optionally via `sudo -n`), or None if absent."""
    path = _find_tool(name)
    if not path:
        return None
    cmd = [path, *args]
    return (["sudo", "-n", *cmd]) if use_sudo else cmd


def _reason_from_stderr(stderr: str) -> str:
    s = (stderr or "").lower()
    if "not permitted" in s or "permission" in s or "eperm" in s or "cap" in s:
        return "eBPF denied — needs root / CAP_BPF+CAP_PERFMON (Operation not permitted)."
    if "no such file" in s or "command not found" in s:
        return "bcc tool not found — install bcc-tools."
    tail = (stderr or "").strip().splitlines()
    return tail[-1] if tail else "eBPF capture produced no output."


def _child_pids(pid: int) -> list[int]:
    """Direct children of `pid`, from /proc (cheaper and race-safer than pgrep)."""
    try:
        return [int(p) for p in
                Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except (OSError, ValueError):
        return []


def _force_kill(proc: subprocess.Popen, is_sudo: bool) -> None:
    """Last-resort stop for a tool that ignored/never got SIGINT. A plain SIGKILL
    is fine for a directly-spawned tool, but sudo CANNOT relay SIGKILL — killing
    only the frontend orphans its root child, which keeps running with probes
    attached. So for sudo: snapshot the child pid, escalate SIGTERM first (sudo
    relays it, and the bcc/bpftrace tools install no handler so it terminates them
    even mid-wedged-compile), then SIGKILL the frontend and best-effort
    `sudo -n kill -KILL` any survivor."""
    if not is_sudo:
        proc.kill()
        proc.wait()
        return
    children = _child_pids(proc.pid)
    proc.terminate()
    end = time.monotonic() + 4
    while proc.poll() is None and time.monotonic() < end:
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for cpid in children:
        if not Path(f"/proc/{cpid}").exists():
            continue
        try:
            subprocess.run(["sudo", "-n", "kill", "-KILL", str(cpid)],
                           capture_output=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            pass
        if Path(f"/proc/{cpid}").exists():
            # least-privilege sudoers that whitelist only the tool paths deny
            # `sudo kill` — at least make the orphan observable.
            log.warning("eBPF tool child pid %s survived kill (sudo kill denied?) — "
                        "it may keep tracing until it exits on its own.", cpid)


def _run_proc(cmd: list[str], *, timeout: float, stop=None,
              duration: float | None = None) -> tuple[str, str]:
    """Run a command, capturing stdout+stderr to TEMP FILES (never pipes). This is
    essential for verbose eBPF tools: a PIPE'd stream that isn't drained during the
    run fills its 64KB buffer and DEADLOCKS the child (bpftrace emits per-event
    warnings that would otherwise hang it). `stop`/`duration` SIGINT it gracefully
    (so it flushes buffered output); `timeout` SIGKILLs as a last resort. Returns
    (stdout, stderr)."""
    outf = tempfile.TemporaryFile()
    errf = tempfile.TemporaryFile()

    def _read() -> tuple[str, str]:
        outf.seek(0); errf.seek(0)
        o = outf.read().decode(errors="replace"); e = errf.read().decode(errors="replace")
        outf.close(); errf.close()
        return o, e

    try:
        proc = subprocess.Popen(cmd, stdout=outf, stderr=errf)
    except Exception as e:  # noqa: BLE001
        outf.close(); errf.close()
        return "", str(e)

    is_sudo = "sudo" in cmd[:3]  # ["sudo","-n",…] or stdbuf-wrapped ["stdbuf","-oL","sudo",…]
    hard = time.monotonic() + timeout
    soft = (time.monotonic() + duration) if duration is not None else None
    while proc.poll() is None:
        now = time.monotonic()
        if now > hard:
            _force_kill(proc, is_sudo)
            return _read()
        if (stop is not None and stop.is_set()) or (soft is not None and now > soft):
            proc.send_signal(signal.SIGINT)  # sudo forwards SIGINT to the child
            end = time.monotonic() + 8
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.1)
            if proc.poll() is None:
                _force_kill(proc, is_sudo)
            proc.wait()
            return _read()
        time.sleep(0.2)
    proc.wait()
    return _read()


def run_tool(name: str, args: list[str], *, use_sudo: bool, timeout: float,
             stop=None, duration: float | None = None,
             line_buffered: bool = False) -> tuple[bool, str, str | None]:
    """Run a bcc tool to completion. Returns (ok, stdout, failure_reason). Fail-open."""
    cmd = tool_cmd(name, args, use_sudo=use_sudo)
    if cmd is None:
        return False, "", f"{name} is not installed (bcc-tools)."
    if line_buffered and shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", *cmd]
    out, err = _run_proc(cmd, timeout=timeout, stop=stop, duration=duration)
    if out.strip():
        return True, out, None
    return False, "", _reason_from_stderr(err)


# --- latency rules ----------------------------------------------------------

# A healthy system has sub-millisecond scheduler run-queue latency and (on SSDs)
# sub-ms block I/O; these p99 thresholds flag oversubscription / slow storage.
_RUNQ_MED_MS, _RUNQ_HIGH_MS = 10, 50
_BIO_MED_MS, _BIO_HIGH_MS = 20, 100


_UNIT_TO_MS = {"msecs": 1.0, "usecs": 1e-3, "nsecs": 1e-6, "secs": 1000.0}


def latency_anomalies(latency: dict) -> list:
    """Anomalies from the latency histograms (imported lazily to avoid a cycle).
    Normalizes to ms across engines (bpftrace run-queue is µs, bcc is ms)."""
    from .trace.events import Anomaly

    out: list = []

    def _ms(h: dict | None) -> float | None:
        if not h:
            return None
        p99, scale = h.get("p99"), _UNIT_TO_MS.get(h.get("unit"))
        return round(p99 * scale) if (p99 is not None and scale) else None

    rq = _ms(latency.get("runqueue"))
    if rq is not None and rq >= _RUNQ_MED_MS:
        sev = "high" if rq >= _RUNQ_HIGH_MS else "medium"
        out.append(Anomaly(
            rule_id="high_runqueue_latency", severity=sev,
            severity_score=0.55 if sev == "high" else 0.4,
            title=f"Scheduler run-queue p99 {rq}ms — threads waiting for CPU",
            description=(f"Runnable threads waited up to ~{rq}ms for a core (p99). "
                         "That's CPU oversubscription / a noisy neighbor — the process "
                         "is ready but the scheduler can't place it. Off-CPU flamegraph "
                         "confirms where; more cores / less contention is the fix."),
        ))
    bio = _ms(latency.get("block_io"))
    if bio is not None and bio >= _BIO_MED_MS:
        sev = "high" if bio >= _BIO_HIGH_MS else "medium"
        out.append(Anomaly(
            rule_id="slow_block_io", severity=sev,
            severity_score=0.55 if sev == "high" else 0.4,
            title=f"Block-I/O p99 {bio}ms (host-wide) — slow disk / storage contention",
            description=(f"Block-device I/O took up to ~{bio}ms (p99). Well past SSD "
                         "latency — a slow/spinning disk, a saturated volume, or fsync "
                         "pressure. NOTE: biolatency is host-wide (no per-PID filter), so "
                         "this reflects all processes' disk I/O, not just this one. If the "
                         "app blocks on it, it shows in the off-CPU flamegraph."),
        ))
    return out


def parse_biosnoop(text: str, pids: set[int]) -> dict:
    """Parse bcc biosnoop rows (one per block I/O) filtered to `pids` → real
    percentiles (biosnoop has an exact float LAT(ms) per event, so we compute true
    p50/p99, not log2 buckets). Columns: TIME COMM PID DISK T SECTOR BYTES LAT(ms)
    (with -Q an extra QUE(ms) column shifts the middle, so we anchor on the ends:
    PID = field[2], LAT = last field, T = 'R'/'W' a few fields from the right)."""
    lats: list[float] = []
    reads = writes = 0
    bytes_total = 0
    for line in text.splitlines():
        f = line.split()
        if len(f) < 8 or f[0] == "TIME(s)" or not f[2].isdigit():
            continue
        if int(f[2]) not in pids:
            continue
        try:
            lats.append(float(f[-1]))
        except ValueError:
            continue
        # T (R/W) is field 4 from the left in the default layout
        rw = f[4] if len(f) > 4 else ""
        if rw.startswith("W"):
            writes += 1
        elif rw.startswith("R"):
            reads += 1
        try:
            bytes_total += int(f[6])
        except (ValueError, IndexError):
            pass
    if not lats:
        return {"count": 0, "total_ms": 0.0, "p50_ms": None, "p99_ms": None,
                "max_ms": None, "read_count": 0, "write_count": 0, "bytes_total": 0}
    lats.sort()
    n = len(lats)

    def _q(q: float) -> float:
        return round(lats[min(n - 1, int(round(q * (n - 1))))], 3)

    return {
        "count": n, "total_ms": round(sum(lats), 2),
        "p50_ms": _q(0.5), "p99_ms": _q(0.99), "max_ms": round(lats[-1], 3),
        "read_count": reads, "write_count": writes, "bytes_total": bytes_total,
    }


def libpython_path(pid: int) -> str | None:
    """The host-accessible path to `pid`'s mapped libpython .so (direct, or through
    /proc/<pid>/root for a namespaced/containerized target), or None."""
    lib = None
    try:
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            path = line.split()[-1] if line.split() else ""
            if "libpython3" in path and ".so" in path:
                lib = path
                break
    except OSError:
        return None
    if not lib:
        return None
    if Path(lib).exists():
        return lib
    rooted = f"/proc/{pid}/root{lib}"
    return rooted if Path(rooted).exists() else None


def usdt_probes(pid: int) -> set[str]:
    """USDT probe names statically defined in `pid`'s mapped libpython (via readelf
    on the .note.stapsdt section — NO root, NO bcc). Empty when the interpreter has
    no USDT (conda/statically-linked python + node don't ship them)."""
    if not shutil.which("readelf"):
        return set()
    lib_path = libpython_path(pid)
    if not lib_path:
        return set()
    lib_path = Path(lib_path)
    try:
        out = subprocess.run(["readelf", "-n", str(lib_path)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"Name:\s+([a-z_][a-z0-9_]*)", out)) & {
        "gc__start", "gc__done", "function__entry", "function__return",
        "import__find__load__start", "import__find__load__done", "line", "audit",
    }


def parse_bpftrace_gc(text: str) -> list[dict]:
    """Parse combined-bpftrace GC lines (`GC <nsecs_abs> <duration_us>`) → timeline
    events [{start_s (relative to the first GC), duration_ms}]."""
    rows: list[tuple[int, int]] = []
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] == "GC" and f[1].isdigit() and f[2].isdigit():
            rows.append((int(f[1]), int(f[2])))
    if not rows:
        return []
    t0 = rows[0][0]
    return [{"start_s": round((ns - t0) / 1e9, 3), "duration_ms": round(us / 1000, 3)}
            for ns, us in rows][-2000:]


def parse_ugc(text: str) -> list[dict]:
    """Parse bcc `ugc`/`pythongc` output (one row per GC) → GC timeline events.
    Rows are `START(s)  DURATION  DESCRIPTION`; the header carries the unit (ms/us).
    Returns [{start_s, duration_ms}] (description is 'None' on Fedora → dropped)."""
    events: list[dict] = []
    to_ms = 1.0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("tracing") or low.startswith("start"):
            if "(us)" in low or "usec" in low:
                to_ms = 0.001
            continue
        f = s.split()
        if len(f) < 2:
            continue
        try:
            start_s = float(f[0])
            dur = float(f[1]) * to_ms
        except ValueError:
            continue
        events.append({"start_s": round(start_s, 3), "duration_ms": round(dur, 3)})
    return events[-2000:]  # cap so a GC-heavy target can't blow the artifact


# --- bpftrace path (CO-RE; works where bcc's bundled headers won't compile, e.g.
#     very new kernels) --------------------------------------------------------

def bpftrace_available(refresh: bool = False) -> bool:
    """bpftrace present AND runnable with privilege (root / caps / passwordless sudo).
    TTL-cached like capabilities() — the sudo probe is a subprocess per call."""
    global _bt_cache
    now = time.monotonic()
    if not refresh and _bt_cache is not None and now - _bt_cache[0] < _CAPS_TTL_S:
        return _bt_cache[1]

    def _probe() -> bool:
        if not shutil.which("bpftrace"):
            return False
        if os.geteuid() == 0 or _has_bpf_caps():
            return True
        try:
            return subprocess.run(["sudo", "-n", "bpftrace", "--version"],
                                  capture_output=True, timeout=4).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    ok = _probe()
    _bt_cache = (time.monotonic(), ok)
    return ok


# ONE combined bpftrace program (run-queue + block-I/O + optional GC). Running a
# single bpftrace instead of three avoids multiple concurrent CO-RE compiles (which
# saturate the CPU and can wedge each other on very new kernels). `@runq_us`/`@bio_ms`
# are named so their map dumps can be extracted separately; GC events print inline.
_BT_RUNQ = """tracepoint:sched:sched_wakeup,
tracepoint:sched:sched_wakeup_new {{ @qt[args.pid] = nsecs; }}
tracepoint:sched:sched_switch {{
  if (args.prev_state == 0) {{ @qt[args.prev_pid] = nsecs; }}
  $np = args.next_pid;
  if (@qt[$np] != 0) {{
    if ($np == {pid}) {{ @runq_us = hist((nsecs - @qt[$np]) / 1000); }}
    delete(@qt[$np]);
  }}
}}"""
_BT_BIO = """tracepoint:block:block_rq_issue { @s[args.dev, args.sector] = nsecs; }
tracepoint:block:block_rq_complete /@s[args.dev, args.sector] != 0/ {
  @bio_ms = hist((nsecs - @s[args.dev, args.sector]) / 1000000);
  delete(@s[args.dev, args.sector]);
}"""
# Scope GC to the target via a /pid == PID/ filter rather than bpftrace's `-p PID`
# — the latter silently applies an implicit pid filter to ALL probes, which kills
# the system-wide sched/block tracepoints in the same program.
_BT_GC = """usdt:{lib}:python:gc__start /pid == {pid}/ {{ @g[tid] = nsecs; }}
usdt:{lib}:python:gc__done /@g[tid] != 0/ {{
  printf("GC %llu %llu\\n", nsecs, (nsecs - @g[tid]) / 1000);
  delete(@g[tid]);
}}"""


def build_combined_bt(pid: int, n: str, gc_lib: str | None = None) -> str:
    """Assemble the combined latency (+GC) bpftrace program for a run. Run it WITHOUT
    `-p PID` (see _BT_GC) — GC is scoped by the pid filter instead."""
    parts = [_BT_RUNQ.format(pid=pid), _BT_BIO]
    if gc_lib:
        parts.append(_BT_GC.format(lib=gc_lib, pid=pid))
    parts.append(f"interval:s:{n} {{ exit(); }}")
    return "\n".join(parts)


def run_bpftrace(script: str, *, timeout: float, stop=None) -> tuple[bool, str, str | None]:
    """Run a bpftrace program (self-terminating via its own interval/exit). Fail-open.
    Never add `-p PID` here — it silently pid-filters ALL probes (see _BT_GC /
    build_combined_bt); scope with an in-script /pid==PID/ filter and full-path
    USDT probes instead."""
    if not shutil.which("bpftrace"):
        return False, "", "bpftrace not installed."
    use_sudo = not (os.geteuid() == 0 or _has_bpf_caps())
    cmd = (["sudo", "-n"] if use_sudo else []) + ["bpftrace", "-e", script]
    out, err = _run_proc(cmd, timeout=timeout, stop=stop)
    if out.strip():
        return True, out, None
    return False, "", _reason_from_stderr(err)


def extract_bt_map(text: str, name: str) -> str:
    """Isolate one named map's histogram rows from a multi-map bpftrace dump."""
    lines, out, cap = text.splitlines(), [], False
    for line in lines:
        s = line.strip()
        if s.startswith("@"):
            cap = s.startswith(f"@{name}:")
            continue
        if cap:
            out.append(line)
    return "\n".join(out)


# matches single-value `[1]  5 |…` and range `[2, 4)  3 |…`; bpftrace abbreviates
# large log2 bounds with K/M/G (=1024^n), e.g. `[2K, 4K)  681 |…`.
_BT_HIST_RE = re.compile(r"\[(\d+[KMGT]?)(?:,\s*(\d+[KMGT]?))?[\])]\s+(\d+)\s*\|")
_BT_SUFFIX = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _bt_num(s: str) -> int:
    return int(s[:-1]) * _BT_SUFFIX[s[-1]] if s and s[-1] in _BT_SUFFIX else int(s)


def _hist_summary(buckets: list[dict], unit: str) -> dict:
    """Shared summary shape for BOTH histogram engines (bpftrace `hist()` dumps
    and bcc log2 rows), so their latency.json outputs — which latency_anomalies
    compares against the same thresholds — can never diverge. Percentiles are the
    upper bound of the bucket where the cumulative count crosses the fraction
    (log2 buckets are coarse, so these are estimates — honest for latency)."""
    total = sum(b["count"] for b in buckets)

    def _pct(frac: float) -> int | None:
        if not total:
            return None
        target, cum = frac * total, 0
        for b in buckets:
            cum += b["count"]
            if cum >= target:
                return b["hi"]
        return buckets[-1]["hi"] if buckets else None

    hi_nz = [b["hi"] for b in buckets if b["count"] > 0]
    return {"unit": unit, "buckets": buckets, "total": total,
            "p50": _pct(0.5), "p90": _pct(0.9), "p99": _pct(0.99),
            "max": hi_nz[-1] if hi_nz else None}


def parse_bpftrace_hist(text: str, unit: str) -> dict:
    """Parse a bpftrace `hist()` map dump into the same shape as parse_log2_hist.
    Rows look like `[1]  5 |@@@|` (single) or `[2K, 4K)  3 |@@|` (range)."""
    buckets: list[dict] = []
    for line in text.splitlines():
        m = _BT_HIST_RE.search(line)
        if m:
            lo = _bt_num(m.group(1))
            hi = _bt_num(m.group(2)) if m.group(2) else lo
            buckets.append({"lo": lo, "hi": hi, "count": int(m.group(3))})
    return _hist_summary(buckets, unit)


# --- log2 histogram parsing (runqlat / biolatency) --------------------------

_HDR_RE = re.compile(r"\s*(nsecs|usecs|msecs|secs)\s*:\s*count", re.I)
_ROW_RE = re.compile(r"\s*(\d+)\s*->\s*(\d+)\s*:\s*(\d+)")


def parse_log2_hist(text: str) -> dict:
    """Parse a bcc power-of-2 histogram into buckets + percentile estimates.

    bcc prints:  `     msecs               : count     distribution`
    then rows:   `         2 -> 3          : 5        |****        |`
    """
    unit = "usecs"
    buckets: list[dict] = []
    for line in text.splitlines():
        hm = _HDR_RE.match(line)
        if hm:
            unit = hm.group(1).lower()
            continue
        m = _ROW_RE.match(line)
        if m:
            buckets.append({"lo": int(m.group(1)), "hi": int(m.group(2)), "count": int(m.group(3))})
    return _hist_summary(buckets, unit)


# --- request tracing (attach): HTTP boundary + libpq DB spans ----------------
#
# One DEDICATED bpftrace program (never folded into the combined latency/GC one, and
# never with `-p` — the spikes showed `-p` is a global pid filter with no bystander
# relief, and it would kill any system-wide probe; system-wide-tracepoint scoping is
# the in-script `/pid == PID/` filter). Validated end-to-end against Flask+psycopg2 →
# system libpq. The templates encode several bpftrace-0.24 quirks that each cost a
# debug cycle; DO NOT "simplify" them without re-testing on a real server:
#   - void* args (sendto `args.buff`, recvfrom `args.ubuf`) must be cast to a sized
#     pointer BEFORE arithmetic: `(uint8*)args.buff + 9`, never `(uint8*)(args.buff+9)`
#     (void has size 0 → a codegen "0 is not a valid type size" abort).
#   - Exactly ONE `%s` per printf — two string args in one printf hit the same abort.
#     So a request emits a REQ line (head) and a RSP line (status) joined in Python.
#   - `str(ptr, N)` yields N-1 chars + NUL: a 3-digit status needs `str(buf,4)`, and a
#     method compare needs `str(buf, len+1) == "GET "` (`str(buf,4)=="GET "` is false).
#   - `nsecs` read twice gives two values → capture once into `$now` for the join key.
#   - fd types differ per syscall (recvfrom `int` vs read `unsigned int`) → cast every
#     fd to `int64` so the shared maps keep one key/value type.
#   - `has_key(...)` guards the close-time deletes (else a warning per close floods out).
# Substitution uses .replace() with the distinctive tokens TARGETPID / LIBPQPATH rather
# than str.format (this program's brace count makes .format brace-doubling error-prone).

_MAX_SPANS = 4000  # per-parse cap on retained spans (like the GC/incident [-N:] caps)

_BT_HTTP = r"""
tracepoint:syscalls:sys_exit_accept4 /pid == TARGETPID/ { if (args.ret >= 0) { @srv[pid, (int64) args.ret] = 1; } }
tracepoint:syscalls:sys_exit_accept  /pid == TARGETPID/ { if (args.ret >= 0) { @srv[pid, (int64) args.ret] = 1; } }
tracepoint:syscalls:sys_enter_recvfrom /pid == TARGETPID/ { @rb[tid] = (uint64) args.ubuf; @rfd[tid] = (int64) args.fd; if (@active[tid] != 0) { @insc[tid] = 1; } }
tracepoint:syscalls:sys_enter_read     /pid == TARGETPID/ { @rb[tid] = (uint64) args.buf;  @rfd[tid] = (int64) args.fd; if (@active[tid] != 0) { @insc[tid] = 1; } }
tracepoint:syscalls:sys_exit_recvfrom /pid == TARGETPID/ {
  $fd = @rfd[tid]; $n = args.ret;
  if ($n > 4 && @srv[pid, $fd] != 0 &&
      (str((uint8 *)@rb[tid], 5) == "GET "     || str((uint8 *)@rb[tid], 6) == "POST "  ||
       str((uint8 *)@rb[tid], 5) == "PUT "     || str((uint8 *)@rb[tid], 6) == "HEAD "  ||
       str((uint8 *)@rb[tid], 8) == "DELETE "  || str((uint8 *)@rb[tid], 7) == "PATCH " ||
       str((uint8 *)@rb[tid], 9) == "OPTIONS ")) {
    $now = nsecs; @t0[pid, $fd] = $now; @active[tid] = 1; @insc[tid] = 0;
    printf("REQ %llu %d %d %s\n", $now, pid, tid, str((uint8 *)@rb[tid], 96));
  }
  delete(@rb[tid]); delete(@rfd[tid]);
}
tracepoint:syscalls:sys_exit_read /pid == TARGETPID/ {
  $fd = @rfd[tid]; $n = args.ret;
  if ($n > 4 && @srv[pid, $fd] != 0 &&
      (str((uint8 *)@rb[tid], 5) == "GET "     || str((uint8 *)@rb[tid], 6) == "POST "  ||
       str((uint8 *)@rb[tid], 5) == "PUT "     || str((uint8 *)@rb[tid], 6) == "HEAD "  ||
       str((uint8 *)@rb[tid], 8) == "DELETE "  || str((uint8 *)@rb[tid], 7) == "PATCH " ||
       str((uint8 *)@rb[tid], 9) == "OPTIONS ")) {
    $now = nsecs; @t0[pid, $fd] = $now; @active[tid] = 1; @insc[tid] = 0;
    printf("REQ %llu %d %d %s\n", $now, pid, tid, str((uint8 *)@rb[tid], 96));
  }
  delete(@rb[tid]); delete(@rfd[tid]);
}
tracepoint:syscalls:sys_enter_sendto /pid == TARGETPID && @t0[pid, (int64) args.fd] != 0/ {
  $fd = (int64) args.fd;
  printf("RSP %llu %llu %s\n", @t0[pid, $fd], nsecs, str((uint8 *) args.buff + 9, 4));
  delete(@t0[pid, $fd]);
  if (has_key(@active, tid)) { delete(@active[tid]); }
  if (has_key(@insc, tid)) { delete(@insc[tid]); }
}
tracepoint:syscalls:sys_enter_write /pid == TARGETPID && @t0[pid, (int64) args.fd] != 0/ {
  $fd = (int64) args.fd;
  printf("RSP %llu %llu %s\n", @t0[pid, $fd], nsecs, str((uint8 *) args.buf + 9, 4));
  delete(@t0[pid, $fd]);
  if (has_key(@active, tid)) { delete(@active[tid]); }
  if (has_key(@insc, tid)) { delete(@insc[tid]); }
}
tracepoint:syscalls:sys_enter_close /pid == TARGETPID/ {
  $fd = (int64) args.fd;
  $is_srv = has_key(@srv, (pid, $fd));
  if ($is_srv) { delete(@srv, (pid, $fd)); }
  if (has_key(@t0, (pid, $fd))) { delete(@t0, (pid, $fd)); }
  // On SERVER-connection teardown, clear the worker's tid-keyed request state — else a
  // request whose response is NOT a probed sendto/write (writev/sendfile, or an aborted /
  // reset connection with no response) never clears @active[tid], so the off-CPU sched
  // probes keep doing full work for that worker AND its idle inter-request epoll_wait/accept
  // blocking pollutes the per-tid @ostk drill flame. Gated on $is_srv (NOT any close) so an
  // intermediate close mid-request (e.g. the worker closing its DB socket) can't truncate a
  // live request's tracking. close() runs on the worker's own tid (thread-per-request).
  if ($is_srv) {
    if (has_key(@active, tid)) { delete(@active[tid]); }
    if (has_key(@insc, tid))   { delete(@insc[tid]); }
  }
}
"""

# Off-CPU + run-queue decomposition of each request (Phase 2 "why"). Scoped to threads
# ACTIVELY serving a request (@active[tid], set at REQ / cleared at RSP) so the system-wide
# sched tracepoints (NO -p) do almost no work outside a request window. Emits per-interval:
#   OFF <tid> <start_ns> <dur_ns> <reason>   — thread was blocked (off-CPU) then woken
#   RQ  <tid> <start_ns> <dur_ns>            — thread was runnable, waiting for a core
# The correlator sums these within each request's window on its tid → on-CPU vs off-CPU vs
# run-queue split, and labels off-CPU overlapping a DB span as "db". `reason` is a coarse
# blocking-syscall class captured while active (@insc): 1 net, 3 lock(futex), 4 sleep/wait,
# 5 disk(fsync); 0 unknown. prev_state==0 means preempted-while-runnable (→ run-queue), any
# other state means it went to sleep (→ off-CPU). All sched pids are cast to uint64 so the
# @active/@off0/@rq0 maps keep one key type (tid from the syscall probes is uint64).
_BT_OFFCPU = r"""
tracepoint:syscalls:sys_enter_futex /pid == TARGETPID/ { if (@active[tid] != 0) { @insc[tid] = 3; } }
tracepoint:syscalls:sys_enter_nanosleep,tracepoint:syscalls:sys_enter_clock_nanosleep,tracepoint:syscalls:sys_enter_select,tracepoint:syscalls:sys_enter_pselect6,tracepoint:syscalls:sys_enter_poll,tracepoint:syscalls:sys_enter_ppoll,tracepoint:syscalls:sys_enter_epoll_wait,tracepoint:syscalls:sys_enter_epoll_pwait /pid == TARGETPID/ { if (@active[tid] != 0) { @insc[tid] = 4; } }
tracepoint:syscalls:sys_enter_fsync,tracepoint:syscalls:sys_enter_fdatasync,tracepoint:syscalls:sys_enter_sync_file_range /pid == TARGETPID/ { if (@active[tid] != 0) { @insc[tid] = 5; } }
tracepoint:sched:sched_switch {
  $pp = (uint64) args.prev_pid; $np = (uint64) args.next_pid;
  if (@active[$pp] != 0) {
    if (args.prev_state == 0) { @rq0[$pp] = nsecs; }
    else { @off0[$pp] = nsecs; @offstk[$pp] = kstack(6); }
  }
  if (@active[$np] != 0 && @rq0[$np] != 0) {
    printf("RQ %d %llu %llu\n", $np, @rq0[$np], nsecs - @rq0[$np]);
    delete(@rq0[$np]);
  }
}
tracepoint:sched:sched_wakeup,tracepoint:sched:sched_wakeup_new {
  $wp = (uint64) args.pid;
  if (@active[$wp] != 0 && @off0[$wp] != 0) {
    $d = nsecs - @off0[$wp];
    printf("OFF %d %llu %llu %d\n", $wp, @off0[$wp], $d, @insc[$wp]);
    @ostk[$wp, @offstk[$wp]] = sum($d);   // per-tid off-CPU stacks → span→flamegraph drill
    delete(@off0[$wp]);
    @rq0[$wp] = nsecs;
  }
}
"""

# libpq DB spans. Probe PQsendQuery(+Params) ONLY, NOT PQexec: a sync client's PQexec
# internally calls the public PQsendQuery, so probing both double-counts the same query
# (verified: psycopg2 /slow fired both at 302ms). PQsendQuery-only catches sync clients
# (psycopg2/libpqxx via that internal send) AND async ones (psql) — one span per query.
# The span closes on the terminal PQgetResult (retval == NULL) — an entry→uretprobe span
# on PQsendQuery alone reads ~0ms because it only enqueues (spike OQ#2b).
_BT_SQL = r"""
uprobe:LIBPQPATH:PQsendQuery,uprobe:LIBPQPATH:PQsendQueryParams /pid == TARGETPID/ { @sqs[tid] = nsecs; @sqq[tid] = str(arg1, 192); }
uretprobe:LIBPQPATH:PQgetResult /pid == TARGETPID && @sqs[tid] != 0/ {
  if (retval == 0) {
    printf("SQL %llu %d %d %llu %s\n", @sqs[tid], pid, tid, nsecs - @sqs[tid], @sqq[tid]);
    delete(@sqs[tid]); delete(@sqq[tid]);
  }
}
"""


# TLS plaintext recovery (Phase 2). When a server terminates TLS, the read/write
# syscalls carry ciphertext — the plaintext HTTP head/status live one layer up, in
# libssl's SSL_read/SSL_write (plaintext valid AFTER read → uretprobe; valid at entry for
# write). Keyed by tid (not fd — the SSL* isn't an fd), exact for a thread-per-request TLS
# server. Emits the SAME REQ/RSP lines as the plaintext path, reusing the parser + the
# off-CPU decomposition (@active[tid] is set here too). OpenSSL ≥1.1.1 (and CPython's
# _ssl) use the *_ex variants (return 1/success, byte count in the *readbytes out-param);
# older/C servers use the classic ones (return = byte count). build_request_bt emits
# whichever of the two blocks the target's libssl actually exports.
_BT_TLS = r"""
uprobe:LIBSSLPATH:SSL_read /pid == TARGETPID/ { @sslrb[tid] = arg1; }
uretprobe:LIBSSLPATH:SSL_read /pid == TARGETPID && @sslrb[tid] != 0/ {
  $n = retval;
  if ($n > 4 &&
      (str((uint8 *)@sslrb[tid], 5) == "GET "     || str((uint8 *)@sslrb[tid], 6) == "POST "  ||
       str((uint8 *)@sslrb[tid], 5) == "PUT "     || str((uint8 *)@sslrb[tid], 6) == "HEAD "  ||
       str((uint8 *)@sslrb[tid], 8) == "DELETE "  || str((uint8 *)@sslrb[tid], 7) == "PATCH " ||
       str((uint8 *)@sslrb[tid], 9) == "OPTIONS ")) {
    $now = nsecs; @t0tls[tid] = $now; @active[tid] = 1; @insc[tid] = 0;
    printf("REQ %llu %d %d %s\n", $now, pid, tid, str((uint8 *)@sslrb[tid], 96));
  }
  delete(@sslrb[tid]);
}
uprobe:LIBSSLPATH:SSL_write /pid == TARGETPID && @t0tls[tid] != 0/ {
  printf("RSP %llu %llu %s\n", @t0tls[tid], nsecs, str((uint8 *) arg1 + 9, 4));
  delete(@t0tls[tid]);
  if (has_key(@active, tid)) { delete(@active[tid]); }
  if (has_key(@insc, tid)) { delete(@insc[tid]); }
}
"""
_BT_TLS_EX = r"""
uprobe:LIBSSLPATH:SSL_read_ex /pid == TARGETPID/ { @sslrb[tid] = arg1; @sslrn[tid] = arg3; }
uretprobe:LIBSSLPATH:SSL_read_ex /pid == TARGETPID && @sslrb[tid] != 0/ {
  $n = *(uint64 *) @sslrn[tid];
  if ($n > 4 &&
      (str((uint8 *)@sslrb[tid], 5) == "GET "     || str((uint8 *)@sslrb[tid], 6) == "POST "  ||
       str((uint8 *)@sslrb[tid], 5) == "PUT "     || str((uint8 *)@sslrb[tid], 6) == "HEAD "  ||
       str((uint8 *)@sslrb[tid], 8) == "DELETE "  || str((uint8 *)@sslrb[tid], 7) == "PATCH " ||
       str((uint8 *)@sslrb[tid], 9) == "OPTIONS ")) {
    $now = nsecs; @t0tls[tid] = $now; @active[tid] = 1; @insc[tid] = 0;
    printf("REQ %llu %d %d %s\n", $now, pid, tid, str((uint8 *)@sslrb[tid], 96));
  }
  delete(@sslrb[tid]); delete(@sslrn[tid]);
}
uprobe:LIBSSLPATH:SSL_write_ex /pid == TARGETPID && @t0tls[tid] != 0/ {
  printf("RSP %llu %llu %s\n", @t0tls[tid], nsecs, str((uint8 *) arg1 + 9, 4));
  delete(@t0tls[tid]);
  if (has_key(@active, tid)) { delete(@active[tid]); }
  if (has_key(@insc, tid)) { delete(@insc[tid]); }
}
"""

# MySQL/MariaDB DB spans (Phase 2). `mysql_real_query(MYSQL*, stmt, len)` is the
# synchronous-blocking entry both `mysql_query` and most drivers funnel through — probe
# it ONLY (probing mysql_query too would double-count, like libpq's PQexec/PQsendQuery).
# Prepared-statement text lives in mysql_stmt_prepare (join deferred). Same SQL line +
# parser as libpq.
_BT_MYSQL = r"""
uprobe:DBLIBPATH:mysql_real_query /pid == TARGETPID/ { @mqs[tid] = nsecs; @mqq[tid] = str(arg1, 192); }
uretprobe:DBLIBPATH:mysql_real_query /pid == TARGETPID && @mqs[tid] != 0/ {
  printf("SQL %llu %d %d %llu %s\n", @mqs[tid], pid, tid, nsecs - @mqs[tid], @mqq[tid]);
  delete(@mqs[tid]); delete(@mqq[tid]);
}
"""

# SQLite DB spans (Phase 2). SQLite is IN-PROCESS: the query text is at prepare, the work
# is in sqlite3_step (called repeatedly). We map stmt* → SQL text at prepare (deref the
# sqlite3_stmt** out-param), then emit a span for each step slower than 1ms (skips the
# trivial per-row steps; a heavy query's work lands in one long step). Reuses the SQL line
# + parser. Note: SQLite db time is typically ON-CPU (no socket wait), so it shows as an
# overlay on the on-CPU bucket, not off-CPU — the breakdown handles that (db is an overlay).
_BT_SQLITE = r"""
uprobe:DBLIBPATH:sqlite3_prepare_v2,uprobe:DBLIBPATH:sqlite3_prepare_v3 /pid == TARGETPID/ {
  if (@sqld[tid] == 0) { @spz[tid] = arg1; @spp[tid] = arg3; }
  @sqld[tid] = @sqld[tid] + 1;
}
uretprobe:DBLIBPATH:sqlite3_prepare_v2,uretprobe:DBLIBPATH:sqlite3_prepare_v3 /pid == TARGETPID/ {
  if (@sqld[tid] > 0) { @sqld[tid] = @sqld[tid] - 1; }
  if (@sqld[tid] == 0 && @spp[tid] != 0) {
    $stmt = *(uint64 *) @spp[tid];
    @stext[$stmt] = str((uint8 *) @spz[tid], 192);
    delete(@spz[tid]); delete(@spp[tid]);
  }
}
uprobe:DBLIBPATH:sqlite3_step /pid == TARGETPID/ { @sst[tid] = nsecs; @ssp[tid] = (uint64) arg0; }
uretprobe:DBLIBPATH:sqlite3_step /pid == TARGETPID && @sst[tid] != 0/ {
  $dur = nsecs - @sst[tid];
  if ($dur > 1000000) {
    printf("SQL %llu %d %d %llu %s\n", @sst[tid], pid, tid, $dur, @stext[@ssp[tid]]);
  }
  delete(@sst[tid]);
}
uprobe:DBLIBPATH:sqlite3_finalize /pid == TARGETPID/ {
  if (has_key(@stext, (uint64) arg0)) { delete(@stext[(uint64) arg0]); }
}
"""


def build_request_bt(pid: int, n: str, pq_lib: str | None = None,
                     db_libs: "list[tuple[str, str]] | None" = None,
                     ssl_lib: str | None = None, off_cpu: bool = True) -> str:
    """Assemble the dedicated request-tracing bpftrace program: the HTTP-boundary
    tracepoints (always) + off-CPU/run-queue decomposition (`off_cpu`) + DB-span uprobes
    (libpq via `pq_lib`, and any MySQL/SQLite libs in `db_libs` as (engine, path) pairs)
    + optional TLS plaintext recovery (`ssl_lib` → SSL_read/SSL_write) + the trailing
    self-terminating interval. Run WITHOUT `-p` (see _BT_HTTP)."""
    parts = [_BT_HTTP.replace("TARGETPID", str(pid))]
    if ssl_lib:
        # Emit only the SSL_* variants the target's libssl exports (probing an absent
        # symbol aborts the whole bpftrace program). OpenSSL ≥1.1.1 / CPython use *_ex.
        if _exports_symbol(ssl_lib, "SSL_read"):
            parts.append(_BT_TLS.replace("LIBSSLPATH", ssl_lib).replace("TARGETPID", str(pid)))
        if _exports_symbol(ssl_lib, "SSL_read_ex"):
            parts.append(_BT_TLS_EX.replace("LIBSSLPATH", ssl_lib).replace("TARGETPID", str(pid)))
    if off_cpu:
        parts.append(_BT_OFFCPU.replace("TARGETPID", str(pid)))
    if pq_lib:
        parts.append(_BT_SQL.replace("LIBPQPATH", pq_lib).replace("TARGETPID", str(pid)))
    has_sqlite = has_mysql = False
    for engine, lib in (db_libs or []):
        tmpl = _BT_MYSQL if engine == "mysql" else _BT_SQLITE if engine == "sqlite" else None
        if tmpl:
            parts.append(tmpl.replace("DBLIBPATH", lib).replace("TARGETPID", str(pid)))
            has_sqlite = has_sqlite or engine == "sqlite"
            has_mysql = has_mysql or engine == "mysql"
    # ONE END probe only (bpftrace rejects a second) — clear the maps that would otherwise
    # auto-dump at exit. RAW-SQL maps are a PII leak: @stext (sqlite), and @sqq (libpq) /
    # @mqq (mysql) which hold an in-flight query's str(arg1,192) if the window closes before
    # its uretprobe deletes it. @offstk is kernel-stack noise. Only clear a map the program
    # actually references, or bpftrace errors on an undeclared map.
    clears = []
    if off_cpu:
        clears.append("clear(@offstk);")
    if pq_lib:
        clears.append("clear(@sqq);")
    if has_mysql:
        clears.append("clear(@mqq);")
    if has_sqlite:
        clears.append("clear(@stext);")
    if clears:
        parts.append("END { " + " ".join(clears) + " }")
    parts.append(f"interval:s:{n} {{ exit(); }}")
    return "\n".join(parts)


def _exports_symbol(lib_path: str, symbol: str) -> bool:
    """True if `lib_path` exports `symbol` as a dynamic FUNC (via readelf --dyn-syms).
    Fail-open to True when readelf is absent/failing — a missing symbol just means the
    uprobe attaches to nothing, which is itself fail-open."""
    if not shutil.which("readelf"):
        return True
    try:
        out = subprocess.run(["readelf", "--dyn-syms", "-W", lib_path],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    pat = re.compile(rf"\bFUNC\b.*\b{re.escape(symbol)}\b")
    return any(pat.search(line) for line in out.splitlines())


def _mapped_lib(pid: int, substr: str, symbol: str) -> str | None:
    """Host-accessible path to `pid`'s mapped shared object whose name contains `substr`
    and whose dynamic symbols export `symbol`, or None. Unlike `libpython_path` (one
    libpython per process → first-match-and-break), a process can map MULTIPLE matching
    objects/versions and statically-bundled copies hide the symbols — so collect ALL
    matches and return the first that actually exports the target symbol. Fail-open."""
    seen: list[str] = []
    try:
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            parts = line.split()
            path = parts[-1] if parts else ""
            if substr in path and ".so" in path and path.startswith("/") and path not in seen:
                seen.append(path)
    except OSError:
        return None
    for lib in seen:
        real = lib if Path(lib).exists() else f"/proc/{pid}/root{lib}"
        if Path(real).exists() and _exports_symbol(real, symbol):
            return real
    return None


def libpq_path(pid: int) -> str | None:
    """`pid`'s mapped libpq that exports `PQexec` (dynamically linked Postgres), or None
    (statically-bundled psycopg2-binary / asyncpg / pure-wire drivers → no libpq)."""
    return _mapped_lib(pid, "libpq", "PQexec")


def libssl_path(pid: int) -> str | None:
    """`pid`'s mapped libssl that exports `SSL_read` (for TLS plaintext recovery), or None."""
    return _mapped_lib(pid, "libssl", "SSL_read")


def db_libs(pid: int) -> list[tuple[str, str]]:
    """(engine, lib_path) pairs for the non-Postgres DB clients `pid` maps: MySQL/MariaDB
    (`mysql_real_query`) and SQLite (`sqlite3_step`). libpq is resolved separately
    (`libpq_path`). Empty when the target maps neither. (Nearly every CPython maps
    libsqlite3 via the stdlib `sqlite3` module — the >1ms step filter keeps that cheap.)"""
    out: list[tuple[str, str]] = []
    mysql = (_mapped_lib(pid, "libmariadb", "mysql_real_query")
             or _mapped_lib(pid, "libmysqlclient", "mysql_real_query"))
    if mysql:
        out.append(("mysql", mysql))
    sqlite = _mapped_lib(pid, "libsqlite3", "sqlite3_step")
    if sqlite:
        out.append(("sqlite", sqlite))
    return out


# A string literal, CLOSED (`'abc'`) or UNTERMINATED (`'abc` with no closing quote).
# The trailing `'?` is load-bearing: the capture is a str(arg1, 192) PREFIX, so a value
# can be truncated mid-literal (or split by an embedded newline, which parse_bpftrace_sql
# drops after the first line) — a closing-quote-required regex would then persist the raw
# secret/PII tail verbatim. Matching the optional close scrubs that dangling tail too.
_SQL_STR_LIT = re.compile(r"'[^']*'?")
_SQL_NUM_LIT = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")


def _scrub_sql(sql: str) -> str:
    """Redact literals from a captured SQL prefix (PII guard, roadmap §8): string
    literals (closed OR truncated) → '?', standalone numbers → ?, whitespace collapsed.
    psycopg2 binds params client-side, so the raw text carries values — we keep only the
    statement shape and NEVER a raw literal, even when the prefix cuts a literal short."""
    s = _SQL_STR_LIT.sub("'?'", sql)
    s = _SQL_NUM_LIT.sub("?", s)
    return _WS.sub(" ", s).strip()


def parse_bpftrace_http(text: str) -> list:
    """Parse the request program's REQ/RSP lines into `http` Spans. REQ carries the raw
    request head (`REQ <start_ns> <pid> <tid> METHOD /path HTTP/x`), RSP the response
    status (`RSP <start_ns> <end_ns> <status>`); they pair on the start_ns join key. The
    request head's embedded CRLF splits onto ignored physical lines, so the first line
    holds exactly `METHOD /path`. Timestamps stay CLOCK_MONOTONIC (see Span)."""
    from .trace.events import Span

    pending: dict[int, tuple] = {}  # start_ns -> (pid, tid, method, route)
    out: list = []
    for line in text.splitlines():
        if line.startswith("REQ "):
            f = line.split()
            if len(f) < 6:
                continue
            try:
                start, pid, tid = int(f[1]), int(f[2]), int(f[3])
            except ValueError:
                continue
            method = f[4]
            route = f[5].split("?", 1)[0]  # drop query string
            pending[start] = (pid, tid, method, route)
        elif line.startswith("RSP "):
            f = line.split()
            if len(f) < 4:
                continue
            try:
                start, end = int(f[1]), int(f[2])
            except ValueError:
                continue
            req = pending.pop(start, None)
            if req is None:
                continue
            pid, tid, method, route = req
            try:
                status = int(f[3])
            except ValueError:
                status = None
            out.append(Span(
                kind="http", tid=tid, pid=pid, start_ns=start,
                dur_ns=max(0, end - start), name=f"{method} {route}",
                method=method, route=route, status=status,
            ))
    return out[-_MAX_SPANS:]


def parse_bpftrace_sql(text: str) -> list:
    """Parse the request program's SQL lines (`SQL <start_ns> <pid> <tid> <dur_ns>
    <statement>`) into `db` Spans. The statement is scrubbed of literals (PII). tid is
    the correlation key; timestamps stay CLOCK_MONOTONIC (same clock as the http spans)."""
    from .trace.events import Span

    out: list = []
    for line in text.splitlines():
        if not line.startswith("SQL "):
            continue
        f = line.split(maxsplit=5)
        if len(f) < 5:  # a SQLite step with no mapped prepare-text has no field[5]
            continue
        try:
            start, pid, tid, dur = int(f[1]), int(f[2]), int(f[3]), int(f[4])
        except ValueError:
            continue
        stmt = _scrub_sql(f[5]) if len(f) >= 6 else ""
        out.append(Span(
            kind="db", tid=tid, pid=pid, start_ns=start, dur_ns=dur,
            name=stmt[:60] or "query", attrs={"statement": stmt[:200]},
        ))
    return out[-_MAX_SPANS:]


_OFFCPU_REASON = {0: "other", 1: "net", 3: "lock", 4: "sleep", 5: "disk"}


def parse_bpftrace_offcpu(text: str) -> list[dict]:
    """Parse the request program's off-CPU / run-queue interval lines into per-tid
    intervals for the per-request decomposition (aggregate.correlate_breakdown):
      OFF <tid> <start_ns> <dur_ns> <reason_code>  — thread blocked (off-CPU) then woken
      RQ  <tid> <start_ns> <dur_ns>                — thread runnable, waiting for a core
    `reason` is a coarse blocking-syscall class (net/lock/sleep/disk/other). Timestamps
    stay CLOCK_MONOTONIC (same clock as the http/db spans)."""
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("OFF "):
            f = line.split()
            if len(f) < 5:
                continue
            try:
                tid, start, dur, code = int(f[1]), int(f[2]), int(f[3]), int(f[4])
            except ValueError:
                continue
            out.append({"kind": "off", "tid": tid, "start_ns": start, "dur_ns": dur,
                        "reason": _OFFCPU_REASON.get(code, "other")})
        elif line.startswith("RQ "):
            f = line.split()
            if len(f) < 4:
                continue
            try:
                tid, start, dur = int(f[1]), int(f[2]), int(f[3])
            except ValueError:
                continue
            out.append({"kind": "rq", "tid": tid, "start_ns": start, "dur_ns": dur})
    # off/rq intervals are far more numerous than spans — keep the most recent slice.
    return out[-(_MAX_SPANS * 8):]


_OSTK_HEAD = re.compile(r"^@ostk\[(\d+),\s*$")
_OSTK_END = re.compile(r"^\]:\s*(\d+)\s*$")
# bpftrace prints kernel frames as `symbol+123` (DECIMAL offset), not `+0xNN` — strip either.
_KFRAME = re.compile(r"^\s*(.+?)(?:\+(?:0x[0-9a-fA-F]+|\d+))?\s*$")
# The scheduler epilogue every off-CPU stack shares (leaf side) — dropped so the flame
# leaf is the actual blocking call (schedule_timeout / futex_wait / unix_stream_data_wait…).
_SCHED_EPILOGUE = {"perf_trace_sched_switch", "__traceiter_sched_switch", "__schedule",
                   "schedule", "schedule_idle"}


def extract_offcpu_stacks(text: str) -> dict[str, str]:
    """Parse the request program's `@ostk` map dump (per-tid off-CPU kernel stacks with
    summed blocked-ns) into {tid: collapsed-stack text} for perf.fold_collapsed — the data
    behind the span→off-CPU-flamegraph drill. bpftrace prints each entry as:
        @ostk[<tid>,
            <leaf_frame>+0x..
            ...
            <root_frame>
        ]: <ns>
    Frames are leaf-first; we reverse to root→leaf and emit `root;..;leaf <usec>` lines
    (usec to match offcputime's unit). Returns {} when no stacks were captured."""
    by_tid: dict[str, list[str]] = defaultdict(list)
    tid: str | None = None
    frames: list[str] = []
    for line in text.splitlines():
        head = _OSTK_HEAD.match(line)
        if head:
            tid, frames = head.group(1), []
            continue
        if tid is not None:
            end = _OSTK_END.match(line)
            if end:
                # drop the always-present scheduler epilogue at the leaf so the flame's
                # leaf is the real blocking reason (schedule_timeout/poll/futex/…).
                while frames and frames[0] in _SCHED_EPILOGUE:
                    frames.pop(0)
                if frames:
                    folded = ";".join(reversed(frames))
                    usec = max(1, int(end.group(1)) // 1000)
                    by_tid[tid].append(f"{folded} {usec}")
                tid, frames = None, []
                continue
            m = _KFRAME.match(line)
            if m and m.group(1):
                frames.append(m.group(1).strip())
    return {t: "\n".join(rows) for t, rows in by_tid.items() if rows}


def request_capabilities(refresh: bool = False) -> dict:
    """What request tracing this host can do. Gates on bpftrace + privilege ONLY — NOT
    `capabilities()["available"]`, which also requires kernel BTF and bcc tools that the
    syscall-tracepoint + libpq-uprobe path needs neither of (gating on the eBPF-suite
    flag would fail closed on exactly the boxes where request tracing still works).
    libpq-mappability is per-target, resolved at capture time (`libpq_path`)."""
    ok = bpftrace_available(refresh=refresh)
    return {
        "available": ok,
        "reason": None if ok else (
            "request tracing needs bpftrace + privilege — run OpenTrace as root, grant "
            "CAP_BPF+CAP_PERFMON, or enable passwordless sudo for /usr/bin/bpftrace."),
        "engine": "bpftrace",
    }
