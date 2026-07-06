"""Minimal V8-inspector CPU profiler for an already-running Node process — no
restart (Phase C).

Node opens its inspector on 127.0.0.1:9229 when it receives SIGUSR1. We then drive
the Chrome DevTools Protocol over a WebSocket (Profiler.start → wait → Profiler.stop)
and write the resulting `.cpuprofile`, which `perf.fold_cpuprofile` folds like any
other profiler. The backend has no `websockets` dependency, so this is a tiny
hand-rolled client (handshake + masked text frames + reassembly) — just enough for
the two CDP round-trips.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import struct
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import psutil

log = logging.getLogger(__name__)

# The V8 inspector binds 127.0.0.1:9229, or the next free port if taken.
_INSPECTOR_PORTS = range(9229, 9330)


def _target_inspector_port(pid: int) -> int | None:
    """The inspector port OUR target pid is listening on — so we never attach to a
    DIFFERENT Node's inspector that happens to hold 9229 (multiple node procs, a
    --inspect dev server, or a prior capture whose inspector is still open)."""
    try:
        conns = psutil.Process(pid).net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    for c in conns:
        if (c.status == psutil.CONN_LISTEN and c.laddr
                and c.laddr.ip in ("127.0.0.1", "::1")
                and c.laddr.port in _INSPECTOR_PORTS):
            return c.laddr.port
    return None


def _discover_ws(pid: int, timeout: float = 6.0) -> str | None:
    """Find the debugger WebSocket URL for `pid`'s OWN inspector (correlated by the
    port the target process is listening on — not just whatever holds 9229)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = _target_inspector_port(pid)
        if port is not None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1.0
                ) as r:
                    for t in json.loads(r.read().decode("utf-8")):
                        url = t.get("webSocketDebuggerUrl")
                        if url:
                            return url
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
    return None


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("websocket closed")
        buf.extend(chunk)
    return bytes(buf)


def _ws_connect(url: str) -> socket.socket:
    u = urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or 9229
    path = u.path or "/"
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(30)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    # read the handshake response headers
    resp = bytearray()
    while b"\r\n\r\n" not in resp:
        resp.extend(_recv_exact(sock, 1))
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        sock.close()
        raise ConnectionError(f"inspector upgrade failed: {resp[:80]!r}")
    return sock


def _ws_send(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])  # FIN + text opcode
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock: socket.socket) -> str:
    """Receive one (possibly fragmented) text message. Server→client is unmasked."""
    chunks = bytearray()
    while True:
        b0, b1 = _recv_exact(sock, 2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        data = _recv_exact(sock, length) if length else b""
        if opcode == 0x8:  # close
            raise ConnectionError("websocket closed by peer")
        if opcode in (0x1, 0x0, 0x2):
            chunks.extend(data)
            if fin:
                return chunks.decode("utf-8", errors="replace")
        # ping/pong (0x9/0xA) ignored


def _cdp(sock: socket.socket, msg_id: int, method: str, params: dict | None = None) -> dict:
    """Send a CDP command and return the response with the matching id (skipping
    protocol events / other ids)."""
    _ws_send(sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    for _ in range(10000):  # bounded so a chatty target can't loop us forever
        obj = json.loads(_ws_recv(sock))
        if obj.get("id") == msg_id:
            if "error" in obj:
                raise RuntimeError(f"{method}: {obj['error']}")
            return obj
    raise TimeoutError(f"no CDP response for {method}")


def capture(pid: int, window_s: int, out_path: str, stop=None) -> tuple[bool, str | None]:
    """Profile a running Node process for `window_s` via the inspector; write the
    `.cpuprofile` to `out_path`. Returns (ok, failure_reason). Fully fail-open.

    NOTE: only NODE installs a SIGUSR1→inspector handler; sending SIGUSR1 to a
    process that doesn't handle it terminates it. The caller must gate this to
    Node (see attach._CDP_RUNTIMES). `stop` (a threading.Event) cuts the window
    short on Stop / target-exit."""
    try:
        os.kill(pid, signal.SIGUSR1)  # ask Node to open its inspector
    except OSError as e:
        return False, f"couldn't signal Node (pid {pid}): {e}"

    ws_url = _discover_ws(pid)
    if not ws_url:
        return False, (f"Node inspector for pid {pid} didn't open — is this a Node "
                       "process that accepts SIGUSR1? (very old Node needs --inspect).")

    sock = None
    resp: dict = {}
    try:
        sock = _ws_connect(ws_url)
        _cdp(sock, 1, "Profiler.enable")
        _cdp(sock, 2, "Profiler.setSamplingInterval", {"interval": 200})  # µs
        _cdp(sock, 3, "Profiler.start")
        # bounded window, cut short on Stop or target death (not a bare sleep)
        deadline = time.monotonic() + max(1, window_s)
        while time.monotonic() < deadline:
            if (stop is not None and stop.is_set()) or not psutil.pid_exists(pid):
                break
            time.sleep(0.2)
        resp = _cdp(sock, 4, "Profiler.stop")  # returns whatever was sampled
    except Exception as e:  # noqa: BLE001
        log.warning("Node CDP session failed for pid %s: %s", pid, e)
        return False, f"Node CDP profiling failed: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    profile = ((resp.get("result") or {}).get("profile"))
    if not profile:
        return False, "the Node inspector returned no CPU profile."
    try:
        Path(out_path).write_text(json.dumps(profile))
    except OSError as e:
        return False, f"couldn't write the cpuprofile: {e}"
    return True, None
