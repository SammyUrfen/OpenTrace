"""Reconstruct a program's stdout/stderr from `strace -e write=1,2` hex dumps.

Capturing output this way (rather than teeing the program's fds through a pipe)
keeps stdio fidelity: the program still writes to the real terminal and
`isatty()` is unaffected — strace merely *observes* the bytes. Each
`write(1|2, ...)` line is followed by `| OFFSET  HH HH ...  ascii |` dump lines
that hold the exact bytes.

Public surface:
- `extract_output(strace_log_path) -> list[OutputChunk]`
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

# write(fd, ...) on stdout(1)/stderr(2), with PID + epoch-ms prefix.
_WRITE = re.compile(r"^\s*(\d+)\s+(\d+\.\d+)\s+write\((1|2),")
# A hex dump line: "| 00000  48 65 ...  ascii |". Capture the hex column window
# (bounded to 49 chars = 16 bytes) so the ascii column is never misread as hex.
_DUMP = re.compile(r"^\s*\|\s+[0-9a-fA-F]+ {2}(.{1,49})")
_HEXBYTE = re.compile(r"[0-9a-fA-F]{2}")


class OutputChunk(TypedDict):
    timestamp_ms: float
    stream: str  # "stdout" | "stderr"
    text: str


def extract_output(strace_log_path: str | Path) -> list[OutputChunk]:
    path = Path(strace_log_path)
    if not path.exists():
        return []
    chunks: list[OutputChunk] = []
    pending: dict | None = None  # the write we're collecting dump bytes for
    buf = bytearray()

    def flush() -> None:
        nonlocal pending, buf
        if pending is not None:
            text = buf.decode("utf-8", errors="replace")
            if text:
                chunks.append({
                    "timestamp_ms": pending["ts"],
                    "stream": "stdout" if pending["fd"] == 1 else "stderr",
                    "text": text,
                })
        pending = None
        buf = bytearray()

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            w = _WRITE.match(line)
            if w:
                flush()
                pending = {"ts": float(w.group(2)) * 1000.0, "fd": int(w.group(3))}
                continue
            if pending is not None:
                d = _DUMP.match(line)
                if d:
                    for hb in _HEXBYTE.findall(d.group(1)):
                        buf.append(int(hb, 16))
                    continue
                # any non-dump line ends this write's data
                flush()
    flush()
    return chunks
