# OpenTrace Backend

FastAPI server. Runs inside the `opentrace-dev` conda env.

## Install

```bash
conda activate opentrace-dev
pip install -e .
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

Install dev/test extras (pytest) with `pip install -e ".[dev]"` and run the
suite from `backend/` with `pytest -q`.

On first run the backend creates:

- `~/.opentrace/` — base directory (override with `OPENTRACE_HOME`)
- `~/.opentrace/config.json` — defaults from `app.config.Config`
- `~/.opentrace/sessions.db` — SQLite (sessions → terminals → runs + per-run
  events / metrics / anomalies / artifacts / run_views)
- `~/.opentrace/sessions/<slug>/` — per-project folders with `terminals/` and `runs/`

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health`, `GET /info` | liveness + resolved paths / cpu cores |
| `POST/GET/PATCH/DELETE /sessions` | projects (with `/sessions/default`, `/{id}/touch`) |
| `POST /terminals`, `POST /terminals/attach` | register a shell; `attach` is used by the hook |
| `POST /runs/start`, `/runs/{id}/pid`, `/runs/{id}/end` | run lifecycle (driven by `otrace`) |
| `GET /runs`, `/runs/{id}` | run list + detail |
| `GET /runs/{id}/{events,metrics,anomalies,artifacts,summary}` | analytical detail |
| `PUT/GET /runs/{id}/views/{name}` | persisted per-view UI state |
| `GET /stream`, `GET /runs/{id}/stream` | SSE live channel (run lifecycle + metric samples) |

## Layout

```
app/
├── main.py          FastAPI app, lifespan, SSE endpoints
├── paths.py         filesystem paths + slug/run-folder naming
├── config.py        Config / LLMConfig pydantic models
├── db.py            SQLite connect + schema + migrations
├── sessions.py      projects CRUD + router
├── terminals.py     terminals CRUD + /attach + router
├── runs.py          runs CRUD + lifecycle endpoints + router
├── run_views.py     per-run view state + router
├── storage.py       events/metrics/anomalies/artifacts + ndjson.zst + meta.json
├── streaming.py     SSE pub/sub broker
├── trace/           strace_parser · metrics (psutil) · fdresolve · orchestrator · events
└── rules/           anomaly detection engine
```

## Environment variables

| Variable                  | Effect                                          |
|---------------------------|-------------------------------------------------|
| `OPENTRACE_HOME`          | Override base dir (default `~/.opentrace`).     |
| `OPENTRACE_LLM_BASE_URL`  | Runtime-only override for `config.llm.base_url`. Not persisted. |
| `OPENTRACE_LLM_MODEL`     | Runtime-only override for `config.llm.model`. Not persisted.   |
