"""Normalized in-memory records produced by the collectors.

These are the hot-path internal shapes (plain dataclasses, cheap to create in
the thousands). They are serialized to `events.ndjson.zst` / `metrics.ndjson.zst`
and selectively persisted to SQLite by `app.storage`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


# event_type values
SYSCALL = "syscall"
SIGNAL = "signal"
EXIT = "exit"
LIBCALL = "libcall"  # a library-function call from ltrace (e.g. malloc/free)


@dataclass(slots=True)
class TraceEvent:
    """One normalized strace line (a syscall, signal, or process-exit)."""

    timestamp_ms: float
    pid: int
    event_type: str = SYSCALL
    source: str = "strace"
    syscall: str | None = None
    args: str = ""
    retval: str | None = None
    error: str | None = None  # errno name, e.g. "ENOENT"
    latency_ms: float | None = None  # from strace -T "<...>" duration
    fd: int | None = None  # resolved fd argument when meaningful
    path: str | None = None  # resolved path for file syscalls

    def to_payload(self) -> dict:
        """JSON-able payload for the SQLite `events.payload` blob.

        Excludes the columns that live on the row itself (timestamp/pid/source/
        event_type) to avoid duplicating them inside the blob.
        """
        return {
            "syscall": self.syscall,
            "args": self.args,
            "retval": self.retval,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "fd": self.fd,
            "path": self.path,
        }

    def to_ndjson(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class MetricSample:
    """One psutil sample of the traced process tree."""

    timestamp_ms: float
    cpu_pct: float | None = None
    rss_mb: float | None = None
    vms_mb: float | None = None
    open_fds: int | None = None
    threads: int | None = None
    syscall_rate: float | None = None  # backfilled at finalize from events
    io_read_bps: float | None = None
    io_write_bps: float | None = None

    def to_ndjson(self) -> dict:
        return asdict(self)

    def as_row(self) -> tuple:
        return (
            self.timestamp_ms, self.cpu_pct, self.rss_mb, self.vms_mb,
            self.open_fds, self.threads, self.syscall_rate,
            self.io_read_bps, self.io_write_bps,
        )


@dataclass(slots=True)
class Anomaly:
    """A detected anomaly, ready to persist.

    `evidence` holds the actual `TraceEvent` objects a rule fired on; the
    orchestrator persists them and fills `evidence_ids` with their stored ids
    just before writing the anomaly row. `evidence` itself is not persisted.
    """

    rule_id: str
    severity: str
    severity_score: float
    title: str
    description: str
    evidence_ids: list[str] = field(default_factory=list)
    first_seen_ms: float | None = None
    last_seen_ms: float | None = None
    occurrence_count: int = 1
    evidence: list = field(default_factory=list, repr=False)
