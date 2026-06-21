"""Resolve a process's open file descriptors via procfs.

Used for live snapshots of long-running processes (which files/sockets a PID
currently holds open). Short-lived processes are gone by finalize time, so FD
*leak* analysis there is derived from strace open/close events instead; this
module is the live counterpart.

Public surface:
- `resolve_fds(pid) -> dict[int, str]`  (fd number -> resolved target)
- `count_fds(pid) -> int`
- `resolve_tree_fds(pid) -> dict[int, dict[int, str]]`  (per-pid)
"""
from __future__ import annotations

import os
from pathlib import Path

import psutil


def resolve_fds(pid: int) -> dict[int, str]:
    """Map open fd numbers to their target path/socket for a single pid."""
    out: dict[int, str] = {}
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return out
    for name in entries:
        try:
            fd = int(name)
        except ValueError:
            continue
        try:
            out[fd] = os.readlink(fd_dir / name)
        except OSError:
            out[fd] = "<unresolved>"
    return out


def count_fds(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0


def resolve_tree_fds(pid: int) -> dict[int, dict[int, str]]:
    """Resolve fds for `pid` and all its descendants."""
    result: dict[int, dict[int, str]] = {}
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        procs = []
    for p in procs:
        fds = resolve_fds(p.pid)
        if fds:
            result[p.pid] = fds
    return result
