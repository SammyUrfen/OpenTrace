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


def capabilities() -> dict:
    """What eBPF profiling this host can do, and why not if it can't."""
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

    return {
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
        "bpftrace": bpftrace_available(),
    }


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

    hard = time.monotonic() + timeout
    soft = (time.monotonic() + duration) if duration is not None else None
    while proc.poll() is None:
        now = time.monotonic()
        if now > hard:
            proc.kill(); proc.wait()
            return _read()
        if (stop is not None and stop.is_set()) or (soft is not None and now > soft):
            proc.send_signal(signal.SIGINT)  # sudo forwards SIGINT to the child
            end = time.monotonic() + 8
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
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

def bpftrace_available() -> bool:
    """bpftrace present AND runnable with privilege (root / caps / passwordless sudo)."""
    if not shutil.which("bpftrace"):
        return False
    if os.geteuid() == 0 or _has_bpf_caps():
        return True
    try:
        return subprocess.run(["sudo", "-n", "bpftrace", "--version"],
                              capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def run_bpftrace(script: str, *, timeout: float, stop=None, pid: int | None = None) -> tuple[bool, str, str | None]:
    """Run a bpftrace program (self-terminating via its own interval/exit). Fail-open.
    `pid` adds `-p PID` (required for USDT probes on a running process)."""
    if not shutil.which("bpftrace"):
        return False, "", "bpftrace not installed."
    use_sudo = not (os.geteuid() == 0 or _has_bpf_caps())
    cmd = (["sudo", "-n"] if use_sudo else []) + ["bpftrace"]
    if pid is not None:
        cmd += ["-p", str(pid)]
    cmd += ["-e", script]
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


# --- log2 histogram parsing (runqlat / biolatency) --------------------------

_HDR_RE = re.compile(r"\s*(nsecs|usecs|msecs|secs)\s*:\s*count", re.I)
_ROW_RE = re.compile(r"\s*(\d+)\s*->\s*(\d+)\s*:\s*(\d+)")


def parse_log2_hist(text: str) -> dict:
    """Parse a bcc power-of-2 histogram into buckets + percentile estimates.

    bcc prints:  `     msecs               : count     distribution`
    then rows:   `         2 -> 3          : 5        |****        |`
    Percentiles are the upper bound of the bucket where the cumulative count crosses
    the fraction (log2 buckets are coarse, so these are estimates — honest for latency).
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

    hi_nonzero = [b["hi"] for b in buckets if b["count"] > 0]
    return {
        "unit": unit,
        "buckets": buckets,
        "total": total,
        "p50": _pct(0.5),
        "p90": _pct(0.9),
        "p99": _pct(0.99),
        "max": hi_nonzero[-1] if hi_nonzero else None,
    }
