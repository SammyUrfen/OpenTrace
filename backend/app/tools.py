"""Detect the external tracing tools OpenTrace can drive.

Backs the first-run wizard (which prompts to install missing ones) and the
Settings "tracing tools" panel. `detect()` shells out to `--version`; the
install hint is tailored to the host's package manager.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

# name -> (human label, what it unlocks)
_TOOLS = {
    "strace": ("Syscall trace", "Syscalls · I/O · Network · Processes · Logs"),
    "ltrace": ("Library calls", "malloc/free ledger · library-call hotspots"),
    "perf": ("Hardware perf", "CPU flamegraph · function hotspots"),
}

# distro id -> (package-manager install command prefix)
_PKG_CMDS = {
    "fedora": "sudo dnf install -y {pkgs}",
    "rhel": "sudo dnf install -y {pkgs}",
    "centos": "sudo dnf install -y {pkgs}",
    "debian": "sudo apt install -y {pkgs}",
    "ubuntu": "sudo apt install -y {pkgs}",
    "arch": "sudo pacman -S --noconfirm {pkgs}",
}

# distro id -> package name overrides (perf is packaged oddly on Debian/Ubuntu)
_PKG_NAMES = {
    "debian": {"perf": "linux-perf"},
    "ubuntu": {"perf": "linux-tools-generic"},
}


def _version(path: str, name: str) -> str | None:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    # pull a "<name> ... X.Y[.Z]" style version where possible
    m = re.search(r"\b\d+\.\d+(?:\.\d+)?\b", first)
    return f"{name} {m.group(0)}" if m else first[:60]


@lru_cache(maxsize=1)
def _distro_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"').lower()
            if line.startswith("ID_LIKE=") and "=" in line:
                like = line.split("=", 1)[1].strip().strip('"').lower().split()
                for cand in like:
                    if cand in _PKG_CMDS:
                        return cand
    except OSError:
        pass
    return ""


def _install_hint(name: str) -> str | None:
    distro = _distro_id()
    cmd = _PKG_CMDS.get(distro)
    if not cmd:
        return None
    pkg = _PKG_NAMES.get(distro, {}).get(name, name)
    return cmd.format(pkgs=pkg)


def _perf_paranoid() -> int | None:
    try:
        return int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        return None


def detect() -> dict:
    paranoid = _perf_paranoid()
    out = []
    for name, (label, unlocks) in _TOOLS.items():
        path = shutil.which(name)
        info = {
            "name": name,
            "label": label,
            "unlocks": unlocks,
            "available": path is not None,
            "path": path,
            "version": _version(path, name) if path else None,
            "install_hint": None if path else _install_hint(name),
        }
        # perf needs perf_event_paranoid <= 2 (<=1 for kernel/tracepoints) to
        # profile a user's own process tree.
        if name == "perf" and path is not None and paranoid is not None and paranoid > 2:
            info["warning"] = (
                f"perf_event_paranoid={paranoid}; lower it to ≤2 "
                f"(sudo sysctl kernel.perf_event_paranoid=1) to capture profiles"
            )
        out.append(info)
    return {"tools": out, "perf_event_paranoid": paranoid, "distro": _distro_id() or None}
