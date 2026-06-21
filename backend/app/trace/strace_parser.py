"""Parse `strace -f -T -ttt` output into normalized `TraceEvent`s.

Expected line shapes (with `-f` PID prefix, `-ttt` epoch time, `-T` duration):

    PID  EPOCH.usec  syscall(args) = retval [ERRNO (desc)] <0.000123>
    PID  EPOCH.usec  syscall(args, <unfinished ...>
    PID  EPOCH.usec  <... syscall resumed> rest) = retval <0.000123>
    PID  EPOCH.usec  --- SIGSEGV {si_signo=SIGSEGV, ...} ---
    PID  EPOCH.usec  +++ exited with 0 +++
    PID  EPOCH.usec  +++ killed by SIGKILL +++

The parser is a pure generator over lines so it can be unit-tested with fixture
strings and streamed over a real log without loading it all into memory. It is
deliberately tolerant: an unrecognized line is skipped, never raised on.

Public surface:
- `parse_lines(lines) -> Iterator[TraceEvent]`
- `parse_file(path) -> Iterator[TraceEvent]`
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

from .events import EXIT, SIGNAL, SYSCALL, TraceEvent

# Leading "PID  EPOCH.usec  <body>". PID is optional (strace without -f).
_HEAD_RE = re.compile(r"^\s*(?:(?P<pid>\d+)\s+)?(?P<ts>\d+\.\d+)\s+(?P<body>.*)$")

# Return assignment, anchored at end of (latency-stripped) body:
#   = 3          = -1 ENOENT (No such file or directory)        = 0x7ffff... = ?
_RET_RE = re.compile(
    r"=\s*(?P<ret>-?\d+|0x[0-9a-fA-F]+|\?)"
    r"(?:\s+(?P<err>E[A-Z][A-Z0-9]*))?"
    r"(?:\s+\([^)]*\))?\s*$"
)

# Trailing "<0.000123>" duration from -T.
_DUR_RE = re.compile(r"<(?P<dur>\d+\.\d+)>\s*$")

# syscall(args) head, after the return part has been removed.
_CALL_RE = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\((?P<args>.*)\)\s*$", re.DOTALL)

_RESUMED_RE = re.compile(r"^<\.\.\.\s+(?P<name>\w+)\s+resumed>(?P<rest>.*)$")
_SIGNAL_RE = re.compile(r"^---\s+(?P<sig>[A-Z][A-Z0-9+]*)\b")
_EXIT_RE = re.compile(
    r"^\+\+\+\s+(?:exited with (?P<code>\d+)|killed by (?P<sig>[A-Z0-9]+))"
)
_FIRST_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FIRST_INT = re.compile(r"^\s*(-?\d+)")

# Syscalls whose first quoted string is a meaningful filesystem path.
_PATH_SYSCALLS = {
    "open", "openat", "creat", "stat", "lstat", "newfstatat", "statx",
    "access", "faccessat", "unlink", "unlinkat", "mkdir", "mkdirat",
    "rmdir", "rename", "renameat", "renameat2", "readlink", "readlinkat",
    "execve", "execveat", "chdir", "truncate", "chmod", "chown",
}
# Syscalls whose first integer arg is an fd.
_FD_SYSCALLS = {
    "read", "write", "pread64", "pwrite64", "readv", "writev", "close",
    "fstat", "fsync", "fdatasync", "lseek", "ioctl", "fcntl", "dup",
    "dup2", "dup3", "sendto", "recvfrom", "send", "recv", "epoll_wait",
    "epoll_ctl", "getdents64", "fchdir", "ftruncate", "mmap",
}


class _Pending:
    __slots__ = ("name", "args_prefix", "ts", "pid")

    def __init__(self, name: str, args_prefix: str, ts: float, pid: int):
        self.name = name
        self.args_prefix = args_prefix
        self.ts = ts
        self.pid = pid


def _ts_to_ms(ts: str) -> float:
    return float(ts) * 1000.0


def _enrich(ev: TraceEvent) -> TraceEvent:
    """Best-effort fd/path extraction from the args string."""
    if ev.syscall in _PATH_SYSCALLS:
        m = _FIRST_STR.search(ev.args)
        if m:
            ev.path = m.group(1)
    if ev.syscall in _FD_SYSCALLS:
        m = _FIRST_INT.match(ev.args)
        if m:
            try:
                ev.fd = int(m.group(1))
            except ValueError:
                pass
    return ev


def _parse_syscall_body(
    body: str, ts_ms: float, pid: int, *, dur_ms: float | None
) -> TraceEvent | None:
    m = _RET_RE.search(body)
    retval = err = None
    head = body
    if m:
        retval = m.group("ret")
        err = m.group("err")
        head = body[: m.start()].rstrip()
    call = _CALL_RE.match(head)
    if not call:
        return None
    ev = TraceEvent(
        timestamp_ms=ts_ms,
        pid=pid,
        event_type=SYSCALL,
        syscall=call.group("name"),
        args=call.group("args").strip(),
        retval=retval,
        error=err,
        latency_ms=dur_ms,
    )
    return _enrich(ev)


def parse_lines(lines: Iterable[str]) -> Iterator[TraceEvent]:
    pending: dict[int, _Pending] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        head = _HEAD_RE.match(line)
        if not head:
            continue
        pid = int(head.group("pid")) if head.group("pid") else 0
        ts_ms = _ts_to_ms(head.group("ts"))
        body = head.group("body").strip()

        # signals: --- SIG... ---
        sig = _SIGNAL_RE.match(body)
        if sig:
            yield TraceEvent(
                timestamp_ms=ts_ms, pid=pid, event_type=SIGNAL,
                syscall=sig.group("sig"), args=body,
            )
            continue

        # process exit: +++ exited with N +++ / +++ killed by SIG +++
        ex = _EXIT_RE.match(body)
        if ex:
            code, ksig = ex.group("code"), ex.group("sig")
            yield TraceEvent(
                timestamp_ms=ts_ms, pid=pid, event_type=EXIT,
                syscall="exit", args=body,
                retval=code if code is not None else None,
                error=ksig,
            )
            continue

        # resumed: <... name resumed> rest) = ret <dur>
        resumed = _RESUMED_RE.match(body)
        if resumed:
            pend = pending.pop(pid, None)
            name = resumed.group("name")
            rest = resumed.group("rest")
            prefix = pend.args_prefix if pend else ""
            start_ts = pend.ts if pend else ts_ms
            merged = f"{name}({prefix}{rest}"
            dur = _DUR_RE.search(merged)
            dur_ms = float(dur.group("dur")) * 1000.0 if dur else None
            merged_clean = _DUR_RE.sub("", merged).rstrip()
            ev = _parse_syscall_body(merged_clean, start_ts, pid, dur_ms=dur_ms)
            if ev:
                yield ev
            continue

        # unfinished: name(args, <unfinished ...>
        if body.endswith("<unfinished ...>"):
            partial = body[: body.rindex("<unfinished ...>")]
            cm = re.match(r"^(?P<name>[A-Za-z_]\w*)\((?P<args>.*)$", partial, re.DOTALL)
            if cm:
                pending[pid] = _Pending(
                    cm.group("name"), cm.group("args"), ts_ms, pid
                )
            continue

        # normal completed syscall
        dur = _DUR_RE.search(body)
        dur_ms = float(dur.group("dur")) * 1000.0 if dur else None
        body_clean = _DUR_RE.sub("", body).rstrip()
        ev = _parse_syscall_body(body_clean, ts_ms, pid, dur_ms=dur_ms)
        if ev:
            yield ev


def parse_file(path: str | Path) -> Iterator[TraceEvent]:
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        yield from parse_lines(f)
