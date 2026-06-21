"""psutil-based metrics poller for a traced process tree.

Given a root PID (in OpenTrace this is the `strace` process), samples the
process tree every ~250ms and emits a `MetricSample` per tick via a callback.
Runs on its own daemon thread so it never blocks the request loop.

Design notes:
- `descendants_only=True` excludes the root (strace) itself, so the metrics
  reflect the *user's program*, not the tracer overhead.
- CPU% is measured with persistent `psutil.Process` objects (cpu_percent is a
  delta since the previous call on that object), summed across the tree. It can
  exceed 100% on multi-core machines — that's intentional; consumers normalize
  by `psutil.cpu_count()` if they want a per-core view.
- I/O throughput is derived from a *monotonic* cumulative total: each pid's last
  seen read/write byte counts are carried forward even after it exits, so a
  child exiting between samples can never make the total drop (which would mask
  real I/O as zero).
- Self-terminates after a sustained run of empty samples (the whole tree is
  gone) so a poller whose `otrace` parent was SIGKILL'd before `/runs/end` does
  not become a zombie thread writing empty metrics forever; `on_exhausted` lets
  the orchestrator finalize such a run.

Public surface:
- `MetricsPoller(root_pid, on_sample, interval=0.25, descendants_only=True,
  on_exhausted=None)` with `.start()` / `.stop(join=True)` and `.sample_now()`.
- `cpu_count()` passthrough for consumers.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import psutil

from .events import MetricSample


def cpu_count() -> int:
    return psutil.cpu_count(logical=True) or 1


# Consecutive empty samples (tree gone) before the poller self-terminates.
_EXHAUST_TICKS = 12


class MetricsPoller:
    def __init__(
        self,
        root_pid: int,
        on_sample: Callable[[MetricSample], None],
        *,
        interval: float = 0.25,
        descendants_only: bool = True,
        on_exhausted: Callable[[], None] | None = None,
    ):
        self.root_pid = root_pid
        self.on_sample = on_sample
        self.interval = interval
        self.descendants_only = descendants_only
        self.on_exhausted = on_exhausted
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # persistent Process objects so cpu_percent() deltas are meaningful
        self._procs: dict[int, psutil.Process] = {}
        # last-seen cumulative IO per pid (carried forward after exit)
        self._io_cum: dict[int, tuple[int, int]] = {}
        # previous monotonic total for throughput derivation: (t, read, write)
        self._prev_total: tuple[float, int, int] | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"metrics-{self.root_pid}", daemon=True
        )
        self._thread.start()

    def stop(self, join: bool = True, timeout: float = 2.0) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=timeout)

    # --- internals ----------------------------------------------------------

    def _resolve_tree(self) -> list[psutil.Process]:
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error:
            return []
        try:
            procs = root.children(recursive=True)
            if not self.descendants_only:
                procs = [root] + procs
        except psutil.Error:
            procs = [] if self.descendants_only else [root]
        # Maintain persistent objects so cpu_percent() keeps its baseline.
        live: dict[int, psutil.Process] = {}
        for p in procs:
            live[p.pid] = self._procs.get(p.pid, p)
        self._procs = live
        return list(live.values())

    def sample_now(self) -> MetricSample:
        now = time.time()
        procs = self._resolve_tree()
        cpu = rss = vms = 0.0
        fds = threads = 0
        any_proc = False
        for p in procs:
            try:
                with p.oneshot():
                    cpu += p.cpu_percent(None)
                    mem = p.memory_info()
                    rss += mem.rss
                    vms += mem.vms
                    threads += p.num_threads()
                    try:
                        fds += p.num_fds()
                    except (psutil.AccessDenied, NotImplementedError):
                        pass
                    try:
                        io = p.io_counters()
                        self._io_cum[p.pid] = (io.read_bytes, io.write_bytes)
                    except (psutil.AccessDenied, NotImplementedError, AttributeError):
                        pass
                any_proc = True
            except psutil.Error:
                continue

        # Monotonic totals: sum last-seen cumulative across every pid ever seen.
        total_read = sum(r for r, _ in self._io_cum.values())
        total_write = sum(w for _, w in self._io_cum.values())
        io_read_bps = io_write_bps = None
        if self._prev_total is not None:
            pt, pr, pw = self._prev_total
            dt = now - pt
            if dt > 0:
                io_read_bps = max(0.0, (total_read - pr) / dt)
                io_write_bps = max(0.0, (total_write - pw) / dt)
        self._prev_total = (now, total_read, total_write)

        return MetricSample(
            timestamp_ms=now * 1000.0,
            cpu_pct=round(cpu, 2) if any_proc else None,
            rss_mb=round(rss / (1024 * 1024), 3) if any_proc else None,
            vms_mb=round(vms / (1024 * 1024), 3) if any_proc else None,
            open_fds=fds if any_proc else None,
            threads=threads if any_proc else None,
            io_read_bps=round(io_read_bps, 1) if io_read_bps is not None else None,
            io_write_bps=round(io_write_bps, 1) if io_write_bps is not None else None,
        )

    def _run(self) -> None:
        # Prime cpu_percent baselines, then sample on the interval.
        self._resolve_tree()
        for p in self._procs.values():
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        seen_alive = False
        empties = 0
        exhausted = False
        while not self._stop.wait(self.interval):
            try:
                sample = self.sample_now()
            except Exception:  # noqa: BLE001 — a poller must never crash a run
                continue
            if sample.rss_mb is None:  # whole tree is gone this tick
                if seen_alive:
                    empties += 1
                    if empties >= _EXHAUST_TICKS:
                        exhausted = True
                        break
                continue  # don't emit empty samples
            seen_alive = True
            empties = 0
            self.on_sample(sample)
        if exhausted and self.on_exhausted is not None:
            try:
                self.on_exhausted()
            except Exception:  # noqa: BLE001
                pass
