"""In-process pub/sub broker bridging the (threaded) trace engine to SSE.

The metrics poller and orchestrator run on background threads and call
`broker.publish(...)` synchronously. SSE endpoints (async) subscribe and drain
a thread-safe `queue.Queue`, so there are no event-loop affinity problems.

Subscribers can listen to a specific `run_id` or to the wildcard `"*"` channel,
which receives every run's lifecycle/metric events (used by the global Live
Monitor and sidebar).

Public surface:
- `broker.publish(run_id, type, data)`
- `broker.subscribe(channel) -> Queue` / `broker.unsubscribe(channel, q)`
- `sse_response(channel)` — a FastAPI `StreamingResponse` over the channel
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections import defaultdict

from starlette.responses import StreamingResponse

WILDCARD = "*"
_MAX_QUEUE = 1000


class Broker:
    def __init__(self) -> None:
        self._subs: dict[str, set[queue.Queue]] = defaultdict(set)
        self._lock = threading.Lock()

    def subscribe(self, channel: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE)
        with self._lock:
            self._subs[channel].add(q)
        return q

    def unsubscribe(self, channel: str, q: queue.Queue) -> None:
        with self._lock:
            s = self._subs.get(channel)
            if s is None:
                return
            s.discard(q)
            if not s:  # prune the emptied channel; subscribe() recreates it
                del self._subs[channel]

    def publish(self, run_id: str, type: str, data: dict) -> None:
        payload = {"type": type, "run_id": run_id, "data": data}
        with self._lock:
            targets = list(self._subs.get(run_id, ())) + list(
                self._subs.get(WILDCARD, ())
            )
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Slow consumer: drop the oldest item(s) until the new one fits.
                # Looping guarantees the latest payload is enqueued even if the
                # queue briefly drains to empty between get and put.
                for _ in range(3):
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(payload)
                        break
                    except queue.Full:
                        continue


broker = Broker()


_POLL_INTERVAL = 0.1   # seconds between queue checks
_HEARTBEAT = 15.0      # seconds between idle keepalive comments


async def _event_stream(channel: str):
    q = broker.subscribe(channel)
    try:
        # Prime the connection so EventSource fires `onopen` promptly.
        yield ": connected\n\n"
        idle = 0.0
        while True:
            try:
                payload = q.get_nowait()
                idle = 0.0
                yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            except queue.Empty:
                await asyncio.sleep(_POLL_INTERVAL)
                idle += _POLL_INTERVAL
                if idle >= _HEARTBEAT:
                    idle = 0.0
                    yield ": ping\n\n"
    except asyncio.CancelledError:  # client disconnected
        raise
    finally:
        broker.unsubscribe(channel, q)


def sse_response(channel: str) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(channel),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
