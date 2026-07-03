"""Parse `ltrace -S -f -ttt -T` output into normalized `TraceEvent`s.

ltrace traces *library* calls (the value of this collector mode); with `-S` it
also interleaves the raw syscalls, so a single ltrace log is a superset of what
strace sees. The line shapes mirror strace closely, with two differences:

    [pid 9] EPOCH.usec  malloc(65536)          = 0x55e0 <0.000101>   # library call
    [pid 9] EPOCH.usec  brk@SYS(nil)           = 0xec28000          # syscall (@SYS)
    [pid 9] EPOCH.usec  free(0x55e0)           = <void> <0.000089>
    [pid 9] EPOCH.usec  malloc(65536 <unfinished ...>
    [pid 9] EPOCH.usec  <... malloc resumed> ) = 0x55e0 <0.000432>
    [pid 9] EPOCH.usec  --- SIGCHLD (Child exited) ---
    [pid 9] EPOCH.usec  +++ exited (status 0) +++

  * the pid prefix is `[pid N]` (not a bare integer);
  * a syscall's name carries an `@SYS` suffix — library calls have none. We map
    `name@SYS` -> a SYSCALL event (so the normal syscall/io/network aggregations
    work on it) and a bare `name` -> a LIBCALL event (feeds malloc/free + the
    library hotspot table).

Like the strace parser this is a tolerant streaming generator: an unrecognized
line is skipped, never raised on.

Public surface:
- `parse_lines(lines) -> Iterator[TraceEvent]`
- `parse_file(path) -> Iterator[TraceEvent]`
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

from .events import EXIT, LIBCALL, SIGNAL, SYSCALL, TraceEvent

# Leading "PID  EPOCH.usec  <body>". With `-o file` ltrace prefixes a bare pid
# (like strace); on a terminal it uses "[pid N]". Accept either, both optional.
_HEAD_RE = re.compile(
    r"^\s*(?:\[pid\s+(?P<pidb>\d+)\]\s+)?(?:(?P<pid>\d+)\s+)?"
    r"(?P<ts>\d+\.\d+)\s+(?P<body>.*)$"
)

# Trailing "<0.000123>" duration from -T (also the sentinel "<unfinished ...>").
_DUR_RE = re.compile(r"<(?P<dur>\d+\.\d+)>\s*$")

# Return assignment at the end of a (duration-stripped) body. ltrace return
# values are far more varied than strace's: hex ptr, signed int, <void>, a
# quoted string, or an opaque <...> token.
_RET_RE = re.compile(
    r"=\s*(?P<ret>0x[0-9a-fA-F]+|-?\d+|<[^>]*>|\"(?:[^\"\\]|\\.)*\"|[^\s=]+)"
    r"(?:\s+(?P<err>E[A-Z][A-Z0-9]*))?"
    r"(?:\s+\([^)]*\))?\s*$"  # optional errno "(description)" — strip if present
)

# "name(args" head — name may carry the @SYS suffix.
_CALL_RE = re.compile(r"^(?P<name>[A-Za-z_][\w@]*)\((?P<args>.*)\)\s*$", re.DOTALL)
_UNFIN_RE = re.compile(r"^(?P<name>[A-Za-z_][\w@]*)\((?P<args>.*)$", re.DOTALL)
_RESUMED_RE = re.compile(r"^<\.\.\.\s+(?P<name>[\w@]+)\s+resumed>(?P<rest>.*)$")
_SIGNAL_RE = re.compile(r"^---\s+(?P<sig>[A-Z][A-Z0-9]*)\b")
# ltrace exit lines: "+++ exited (status 0) +++" / "+++ killed by SIGKILL +++"
_EXIT_RE = re.compile(
    r"^\+\+\+\s+(?:exited \(status (?P<code>\d+)\)|killed by (?P<sig>[A-Z0-9]+))"
)

_FIRST_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FIRST_INT = re.compile(r"^\s*(-?\d+)")

_SYS_SUFFIX = "@SYS"

# Syscalls whose first quoted string is a meaningful path / first int is an fd —
# same enrichment the strace parser does, so the I/O & Network tabs light up for
# ltrace runs too.
_PATH_SYSCALLS = {
    "open", "openat", "creat", "stat", "lstat", "newfstatat", "statx",
    "access", "faccessat", "unlink", "unlinkat", "execve", "execveat",
}
_FD_SYSCALLS = {
    "read", "write", "pread64", "pwrite64", "readv", "writev", "close",
    "fstat", "fsync", "lseek", "ioctl", "fcntl", "sendto", "recvfrom",
}


class _Pending:
    __slots__ = ("name", "args_prefix", "ts")

    def __init__(self, name: str, args_prefix: str, ts: float):
        self.name = name
        self.args_prefix = args_prefix
        self.ts = ts


def _enrich(ev: TraceEvent) -> TraceEvent:
    if ev.event_type != SYSCALL:
        return ev
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


def _build(name: str, args: str, retval: str | None, err: str | None,
           ts_ms: float, pid: int, dur_ms: float | None) -> TraceEvent:
    is_sys = name.endswith(_SYS_SUFFIX)
    bare = name[: -len(_SYS_SUFFIX)] if is_sys else name
    ev = TraceEvent(
        timestamp_ms=ts_ms,
        pid=pid,
        event_type=SYSCALL if is_sys else LIBCALL,
        source="ltrace",
        syscall=bare,
        args=args.strip(),
        retval=retval,
        error=err,
        latency_ms=dur_ms,
    )
    return _enrich(ev)


def _parse_call_body(body: str, ts_ms: float, pid: int,
                     *, dur_ms: float | None) -> TraceEvent | None:
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
    return _build(call.group("name"), call.group("args"), retval, err,
                  ts_ms, pid, dur_ms)


def parse_lines(lines: Iterable[str]) -> Iterator[TraceEvent]:
    pending: dict[int, _Pending] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        head = _HEAD_RE.match(line)
        if not head:
            continue
        pid_s = head.group("pid") or head.group("pidb")
        pid = int(pid_s) if pid_s else 0
        ts_ms = float(head.group("ts")) * 1000.0
        body = head.group("body").strip()

        sig = _SIGNAL_RE.match(body)
        if sig:
            yield TraceEvent(timestamp_ms=ts_ms, pid=pid, event_type=SIGNAL,
                             source="ltrace", syscall=sig.group("sig"), args=body)
            continue

        ex = _EXIT_RE.match(body)
        if ex:
            code, ksig = ex.group("code"), ex.group("sig")
            yield TraceEvent(
                timestamp_ms=ts_ms, pid=pid, event_type=EXIT, source="ltrace",
                syscall="exit", args=body,
                retval=code if code is not None else None, error=ksig,
            )
            continue

        resumed = _RESUMED_RE.match(body)
        if resumed:
            pend = pending.pop(pid, None)
            rest = resumed.group("rest")
            name = pend.name if pend else resumed.group("name")
            prefix = pend.args_prefix if pend else ""
            start_ts = pend.ts if pend else ts_ms
            merged = f"{name}({prefix}{rest}"
            dur = _DUR_RE.search(merged)
            dur_ms = float(dur.group("dur")) * 1000.0 if dur else None
            merged_clean = _DUR_RE.sub("", merged).rstrip()
            ev = _parse_call_body(merged_clean, start_ts, pid, dur_ms=dur_ms)
            if ev:
                yield ev
            continue

        if body.endswith("<unfinished ...>"):
            partial = body[: body.rindex("<unfinished ...>")].rstrip()
            cm = _UNFIN_RE.match(partial)
            if cm:
                pending[pid] = _Pending(cm.group("name"), cm.group("args"), ts_ms)
            continue

        dur = _DUR_RE.search(body)
        dur_ms = float(dur.group("dur")) * 1000.0 if dur else None
        body_clean = _DUR_RE.sub("", body).rstrip()
        ev = _parse_call_body(body_clean, ts_ms, pid, dur_ms=dur_ms)
        if ev:
            yield ev


def parse_file(path: str | Path) -> Iterator[TraceEvent]:
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        yield from parse_lines(f)
