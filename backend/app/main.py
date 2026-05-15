from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import config, db, paths, sessions

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    cfg = config.load()
    db.init()
    app.state.config = cfg
    log.info("opentrace ready: home=%s", paths.home())
    yield


app = FastAPI(title="OpenTrace", version="0.0.1", lifespan=lifespan)

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/info")
def info() -> dict[str, object]:
    return {
        "version": app.version,
        "home": str(paths.home()),
        "config_path": str(paths.config_file()),
        "db_path": str(paths.sessions_db()),
        "sessions_dir": str(paths.sessions_dir()),
        "schema_version": db.CURRENT_VERSION,
    }


@app.post("/sessions", response_model=sessions.Session)
def create_session(data: sessions.SessionCreate) -> sessions.Session:
    return sessions.create(data)


@app.get("/sessions", response_model=list[sessions.Session])
def list_sessions(limit: int = 50, offset: int = 0) -> list[sessions.Session]:
    return sessions.list_recent(limit=limit, offset=offset)


@app.get("/sessions/{sid}", response_model=sessions.Session)
def get_session(sid: str) -> sessions.Session:
    sess = sessions.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


@app.patch("/sessions/{sid}", response_model=sessions.Session)
def update_session(sid: str, data: sessions.SessionUpdate) -> sessions.Session:
    sess = sessions.update(sid, data)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess
