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
REQUEST = "request"  # an HTTP request / DB query span (attach request-tracing)


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
class Span:
    """One request-tracing span — an HTTP request (`kind='http'`) or a DB query
    (`kind='db'`) — captured by the attach-mode bpftrace request program.

    Timestamps are CLOCK_MONOTONIC nanoseconds (bpftrace `nsecs`), the SAME clock
    as offcputime/runqueue, so a future off-CPU join needs no conversion. They are
    **not** Unix epoch: durations here are self-consistent within the run, but any
    absolute-time sink (an incident `ts`, a timeline overlay) must convert via a
    (mono0, wall0) anchor captured at child launch (roadmap §2.6). The MVP has no
    such sink — endpoint durations are relative — so spans stay monotonic.

    `tid` is the worker thread and the correlation join key: a `db` span nests
    under the `http` span with the same tid whose [start_ns, start_ns+dur_ns]
    window contains it (thread-per-request). `db_ms` on an http span is the sum of
    its nested db-span durations, filled by the correlator.
    """

    kind: str  # 'http' | 'db'
    tid: int
    pid: int
    start_ns: int  # CLOCK_MONOTONIC (bpftrace nsecs)
    dur_ns: int
    name: str = ""  # 'GET /users/{id}' | 'SELECT …' (normalized rollup key)
    method: str | None = None
    route: str | None = None
    status: int | None = None
    db_ms: float = 0.0  # http spans only: Σ nested db-span ms (correlator-filled)
    attrs: dict = field(default_factory=dict)

    @property
    def dur_ms(self) -> float:
        return self.dur_ns / 1e6

    def to_ndjson(self) -> dict:
        return asdict(self)


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
