from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, db, llm, paths, run_views, runs, sessions, terminals
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

# Renderer is loaded from a `file://` URL in packaged Electron, which Chromium
# reports as the `null` origin. Allowing all origins is fine for a local-first
# desktop app where the backend only listens on localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(terminals.router)
app.include_router(runs.router)
app.include_router(run_views.router)
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
