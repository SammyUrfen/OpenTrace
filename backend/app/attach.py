"""Attach-to-running-process profiling — Phase A of the profiling roadmap.

OpenTrace normally *launches* a command (`otrace -- cmd`). To profile a service
that is already running (a Spring Boot / Django / Go / … server), we instead
ATTACH to its PID for a bounded window. This module supplies the two pieces the
attach flow needs:

- `detect_runtime(pid)` — infer the language runtime from `/proc/<pid>/maps` +
  the exe, so a profiler can be chosen. Phase A profiles everything with `perf`
  (best for native/Go); later phases swap in the runtime's own sampler
  (py-spy / rbspy / async-profiler / …) via a registry, but the detected id is
  surfaced now so the UI can set expectations.
- `list_targets()` — enumerate attachable candidate PIDs (same-uid, real
  processes) with their detected runtime, for the picker.

See `docs/Profiling_Roadmap.md` for the full plan.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import psutil

from . import container

log = logging.getLogger(__name__)

# Ordered runtime probes: (runtime id, substrings to look for in /proc/pid/maps).
# Order matters — check the most specific shared objects first. Everything that
# matches none of these is treated as `native` (Go, Rust, C/C++, Zig, …), which
# `perf` profiles with real symbols when frame pointers are present.
_MAPS_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("jvm", ("libjvm.so",)),
    ("dotnet", ("libcoreclr.so",)),
    ("beam", ("beam.smp",)),
    ("php", ("libphp", "php_", "/php-fpm")),
    ("ruby", ("libruby",)),
    ("python", ("libpython",)),
    ("node", ("libnode",)),
]

# Runtimes whose main binary name is the giveaway (statically-linked interpreters
# — e.g. conda CPython bakes libpython into the exe — that don't show a
# distinguishing .so in maps). Matched by exact name or `startswith`.
_EXE_MARKERS: dict[str, str] = {
    "node": "node",
    "deno": "deno",
    "bun": "bun",
    "beam": "beam.smp",
    "php": "php-fpm",
    "python": "python",   # python, python3, python3.11
    "ruby": "ruby",
}

# What Phase A can actually do per runtime, so the UI can be honest. Phase A ships
# `perf` only; the interpreter-aware samplers land in Phase B+.
RUNTIME_LABELS: dict[str, str] = {
    "native": "Native (C/C++/Rust/Zig/…)",
    "go": "Go",
    "jvm": "JVM (Java/Kotlin/Scala)",
    "python": "Python (CPython)",
    "ruby": "Ruby",
    "node": "Node.js",
    "deno": "Deno",
    "bun": "Bun",
    "dotnet": ".NET",
    "php": "PHP",
    "beam": "Erlang/Elixir (BEAM)",
    "unknown": "Unknown",
}

# Runtimes whose stacks perf symbolizes well today (native code + frame pointers).
# For the rest, perf sees interpreter/VM frames, not app functions — a dedicated
# userspace sampler (below) gives real app symbols when installed.
_PERF_NATIVE = {"native", "go"}

# Phase B — per-runtime dedicated sampler: runtime -> (tool binary, output format,
# output filename, install hint). Used only when the tool is installed; otherwise
# the attach flow falls back to perf (native/VM frames). Each format is folded by
# perf.py (`fold_collapsed` / `fold_speedscope`).
_SAMPLERS: dict[str, tuple[str, str, str, str]] = {
    "python": ("py-spy", "collapsed", "pyspy.folded", "pip install py-spy"),
    "ruby": ("rbspy", "speedscope", "rbspy.speedscope.json", "cargo install rbspy (or download a release)"),
    "jvm": ("asprof", "collapsed", "asprof.collapsed", "install async-profiler (asprof)"),
    "dotnet": ("dotnet-trace", "speedscope", "dotnet.speedscope.json", "dotnet tool install -g dotnet-trace"),
    "php": ("phpspy", "phpspy", "phpspy.raw", "install phpspy (github.com/adsr/phpspy)"),
}

# Profiled via the V8 inspector (SIGUSR1 → CDP) — no external tool, the running
# process IS the profiler. ONLY Node installs a SIGUSR1→inspector handler; Deno/Bun
# do NOT, so sending SIGUSR1 would TERMINATE them — they're intentionally excluded
# (they'd need to be launched with --inspect; not an attach-any story).
_CDP_RUNTIMES = {"node"}


def profiler_plan(runtime: str) -> dict | None:
    """The best AVAILABLE dedicated profiler for `runtime`, else None (→ use perf).
    Returns `{tool, format, out_file}`."""
    if runtime in _CDP_RUNTIMES:
        return {"tool": "node-cdp", "format": "cpuprofile", "out_file": "node.cpuprofile"}
    entry = _SAMPLERS.get(runtime)
    if not entry:
        return None
    tool, fmt, out_file, _hint = entry
    if shutil.which(tool) is None:
        return None
    return {"tool": tool, "format": fmt, "out_file": out_file}


def sampler_argv(tool: str, pid: int, window_s: int, out_path: str) -> list[str]:
    """Build the argv to sample `pid` for `window_s` seconds into `out_path`."""
    n, p = str(window_s), str(pid)
    if tool == "py-spy":
        # --nonblocking: don't pause the target (production-safe, slightly less exact)
        return ["py-spy", "record", "--pid", p, "--format", "raw", "--rate", "99",
                "--duration", n, "--nonblocking", "--output", out_path]
    if tool == "rbspy":
        return ["rbspy", "record", "--pid", p, "--format", "speedscope",
                "--file", out_path, "--duration", n]
    if tool == "asprof":  # async-profiler
        return ["asprof", "-d", n, "-e", "cpu", "-o", "collapsed", "-f", out_path, p]
    if tool == "dotnet-trace":
        # -o writes the .nettrace; --format speedscope also emits <base>.speedscope.json
        # (that sibling is what we fold — see out_file). Duration is dd:hh:mm:ss.
        nettrace = out_path[:-len(".speedscope.json")] + ".nettrace" \
            if out_path.endswith(".speedscope.json") else out_path + ".nettrace"
        secs = int(window_s)
        dur = f"00:00:{secs // 60:02d}:{secs % 60:02d}"
        return ["dotnet-trace", "collect", "-p", p, "--duration", dur,
                "--format", "speedscope", "-o", nettrace]
    if tool == "phpspy":
        # -H = sample rate (Hz); streams traces to -o. No portable duration flag
        # across versions → the orchestrator's window loop SIGINTs it at window_s.
        return ["phpspy", "-H", "99", "-p", p, "-o", out_path]
    raise ValueError(f"unknown sampler: {tool}")


def _read_maps(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/maps").read_text(errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return ""


def detect_runtime(pid: int) -> str:
    """Best-effort runtime id for a PID: one of the keys in `RUNTIME_LABELS`.

    Reads `/proc/<pid>/maps` (loaded shared objects) and the exe basename. Falls
    back to `native` (perf-friendly) for anything unrecognized, or `unknown` if
    the process can't be inspected at all.
    """
    maps = _read_maps(pid)
    if maps:
        for runtime, needles in _MAPS_MARKERS:
            if any(n in maps for n in needles):
                return runtime

    exe = ""
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        try:
            exe = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            exe = ""
    if exe:
        for runtime, base in _EXE_MARKERS.items():
            if exe == base or exe.startswith(base):
                return runtime

    # Reachable at all? If we could read maps or exe, it's a real native process.
    if maps or exe:
        return "native"
    return "unknown"


def profiler_hint(runtime: str) -> str:
    """A one-line note for the picker about what a capture will show."""
    if runtime in _CDP_RUNTIMES:
        return "V8 inspector (SIGUSR1 → CDP) → real JS symbols, no install needed."
    plan = profiler_plan(runtime)
    if plan:
        return f"{plan['tool']} → real {RUNTIME_LABELS.get(runtime, runtime)} symbols."
    if runtime in _PERF_NATIVE or runtime == "native":
        return "perf gives real symbols (frame-pointer native code)."
    if runtime == "unknown":
        return "Can't inspect this process — attach may be denied."
    if runtime in _SAMPLERS:
        tool, _fmt, _out, install = _SAMPLERS[runtime]
        return (f"perf shows {RUNTIME_LABELS.get(runtime, runtime)} VM frames, not "
                f"your app functions — install {tool} ({install}) for real symbols.")
    return (
        f"perf will show {RUNTIME_LABELS.get(runtime, runtime)} VM/interpreter "
        "frames, not your app functions."
    )


def target_info(pid: int) -> dict:
    """Detail for one attach target (used by /runs/attach validation + the run label)."""
    proc = psutil.Process(pid)  # raises NoSuchProcess if gone
    with proc.oneshot():
        try:
            # Collapse all whitespace so a multi-line `-c` script (or tabs) can't
            # smear the run's command/name across lines.
            cmdline = " ".join(" ".join(proc.cmdline()).split())
        except (psutil.Error, OSError):
            cmdline = ""
        name = proc.name()
        try:
            rss = proc.memory_info().rss
        except (psutil.Error, OSError):
            rss = 0
    runtime = detect_runtime(pid)
    plan = profiler_plan(runtime)
    return {
        "pid": pid,
        "name": name,
        "cmdline": cmdline or name,
        "runtime": runtime,
        "runtime_label": RUNTIME_LABELS.get(runtime, runtime),
        "hint": profiler_hint(runtime),
        "sampler": plan["tool"] if plan else None,
        "rss_mb": round(rss / (1024 * 1024), 1),
        "container": container.container_info(pid),
    }


def list_targets(limit: int = 60) -> list[dict]:
    """Attachable candidate processes (same uid, real userspace procs), biggest
    (by RSS) first — servers tend to be memory-heavy and float to the top. Runtime
    detection is only done for the returned subset to keep this cheap."""
    me = os.getuid()
    own_pid = os.getpid()
    candidates: list[tuple[int, int, str, str]] = []  # (rss, pid, name, cmdline)
    for proc in psutil.process_iter(["pid", "name", "cmdline", "uids", "memory_info"]):
        try:
            info = proc.info
            uids = info.get("uids")
            if uids is None or uids.real != me:
                continue
            if info["pid"] == own_pid:
                continue
            cmdline_list = info.get("cmdline") or []
            if not cmdline_list:  # kernel threads / zombies have no cmdline
                continue
            mem = info.get("memory_info")
            rss = mem.rss if mem else 0
            candidates.append((rss, info["pid"], info.get("name") or "", " ".join(cmdline_list)))
        except (psutil.Error, OSError):
            continue

    candidates.sort(reverse=True)  # by rss desc
    out: list[dict] = []
    for rss, pid, name, cmdline in candidates[:limit]:
        runtime = detect_runtime(pid)
        plan = profiler_plan(runtime)
        out.append({
            "pid": pid,
            "name": name,
            "cmdline": cmdline or name,
            "runtime": runtime,
            "runtime_label": RUNTIME_LABELS.get(runtime, runtime),
            "hint": profiler_hint(runtime),
            "sampler": plan["tool"] if plan else None,
            "rss_mb": round(rss / (1024 * 1024), 1),
            "container": container.container_info(pid),
        })
    return out
