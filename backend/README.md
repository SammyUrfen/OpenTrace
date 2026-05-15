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

On first run the backend creates:

- `~/.opentrace/` — base directory (override with `OPENTRACE_HOME`)
- `~/.opentrace/config.json` — defaults from `app.config.Config`
- `~/.opentrace/sessions.db` — SQLite schema from `OpenTrace_Roadmap.md` §8
- `~/.opentrace/sessions/` — per-session data lives here in later phases

## Endpoints

| Endpoint  | Returns                                                       |
|-----------|---------------------------------------------------------------|
| `GET /health` | `{"status":"ok"}` — process is up.                         |
| `GET /info`   | Resolved version + paths. Useful for verifying bootstrap.  |

## Layout

```
app/
├── main.py     FastAPI app, lifespan, endpoints
├── paths.py    one place for every filesystem path
├── config.py   Config / LLMConfig pydantic models + load / save
└── db.py       SQLite connect + first-run schema init
```

## Environment variables

| Variable                  | Effect                                          |
|---------------------------|-------------------------------------------------|
| `OPENTRACE_HOME`          | Override base dir (default `~/.opentrace`).     |
| `OPENTRACE_LLM_BASE_URL`  | Runtime-only override for `config.llm.base_url`. Not persisted. |
| `OPENTRACE_LLM_MODEL`     | Runtime-only override for `config.llm.model`. Not persisted.   |
