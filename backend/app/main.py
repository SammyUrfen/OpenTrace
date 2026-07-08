from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from . import config, db, llm, paths, runs, sessions, terminals
from .streaming import sse_response
from .trace import metrics as metrics_mod
from .trace import orchestrator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    cfg = config.load()
    db.init()
    orphaned = orchestrator.reconcile_orphans()
    app.state.config = cfg
    log.info(
        "opentrace ready: home=%s (reconciled %d orphan run(s))",
        paths.home(), orphaned,
    )
    yield


app = FastAPI(title="OpenTrace", version="0.1.0", lifespan=lifespan)

# Origins a legitimate client can carry: the packaged Electron renderer loads
# from `file://` (Chromium reports Origin `null`), dev uses the Vite server on
# localhost, and non-browser clients (curl, the otrace hook) send no Origin.
# Anything else is a random web page poking the localhost API.
_LOCAL_ORIGIN = re.compile(
    r"^(null|file://.*|https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?)$",
    re.IGNORECASE,
)
# Hosts the backend answers for; a real DNS name here is a rebinding attempt.
_LOCAL_HOST = re.compile(
    r"^(localhost|127\.0\.0\.1|\[::1\]|::1)(:\d+)?$", re.IGNORECASE
)


class LocalOnlyMiddleware:
    """Reject requests from real web origins (CSRF from a page the user has
    open) and non-local Host headers (DNS rebinding). CORS alone is not enough:
    simple cross-origin requests execute server-side even when the browser
    withholds the response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            host = headers.get("host")
            if (origin is not None and not _LOCAL_ORIGIN.match(origin)) or (
                host is not None and not _LOCAL_HOST.match(host)
            ):
                resp = PlainTextResponse(
                    "forbidden: OpenTrace accepts local clients only",
                    status_code=403,
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LOCAL_ORIGIN.pattern,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added after CORS so it runs first (outermost) and rejects before preflight.
app.add_middleware(LocalOnlyMiddleware)

app.include_router(sessions.router)
app.include_router(terminals.router)
app.include_router(runs.router)
app.include_router(llm.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config/tracing", response_model=config.TracingConfig)
def get_tracing_config() -> config.TracingConfig:
    return config.load().tracing


@app.put("/config/tracing", response_model=config.TracingConfig)
def put_tracing_config(data: config.TracingConfig) -> config.TracingConfig:
    cfg = config.load()
    cfg.tracing = data
    config.save(cfg)
    return cfg.tracing


@app.get("/info")
def info() -> dict[str, object]:
    return {
        "version": app.version,
        "home": str(paths.home()),
        "config_path": str(paths.config_file()),
        "db_path": str(paths.sessions_db()),
        "sessions_dir": str(paths.sessions_dir()),
        "schema_version": db.CURRENT_VERSION,
        "cpu_cores": metrics_mod.cpu_count(),
    }


@app.get("/info/tools")
def info_tools(refresh: bool = False) -> dict[str, object]:
    """Which tracing tools (strace/ltrace/perf) are installed, with versions and
    a distro-tailored install hint for any that are missing. `refresh=true`
    bypasses the TTL cache (the UI's re-check button, after installing a tool)."""
    from . import tools

    return tools.detect(refresh=refresh)


# --- live SSE channels ------------------------------------------------------

@app.get("/stream")
def stream_all():
    """Global live channel — every run's lifecycle + metric events."""
    return sse_response("*")


@app.get("/runs/{rid}/stream")
def stream_run(rid: str):
    """Live channel scoped to a single run."""
    return sse_response(rid)


@app.get("/diff/{a}/{b}/ai-summary/stream")
async def diff_ai_summary_stream(a: str, b: str):
    """Stream an AI comparison of two runs as SSE (no cache — regenerated)."""
    import json as _json

    from starlette.responses import StreamingResponse

    from . import summarize

    run_a, run_b = runs.get(a), runs.get(b)
    if run_a is None or run_b is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def gen():
        yield ": connected\n\n"
        async for ev in summarize.stream_diff_summary(run_a, run_b):
            yield f"data: {_json.dumps(ev, separators=(',', ':'))}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
