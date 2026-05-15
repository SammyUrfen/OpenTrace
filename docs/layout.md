# Repository Layout

OpenTrace is a single repository with four runnable areas plus support
directories.

| Path        | Purpose                                                                                          |
|-------------|--------------------------------------------------------------------------------------------------|
| `backend/`  | FastAPI server, tracing engine, SQLite store (Python 3.11+). Runs in conda env `opentrace-dev`. |
| `frontend/` | React 19 + Vite + TypeScript renderer. Loaded by Electron.                                       |
| `electron/` | Electron main process. Will spawn the backend and open the renderer window.                      |
| `docs/`     | Internal notes, phase checklists, design references.                                             |
| `prompts/`  | Used to store prompt files since the project is AI code heavy                                                 |

## Reference docs

- `docs/OpenTrace_Roadmap.md` — locked product spec.
- `prompts/OpenTrace_Prompt_File_Phase0.docx` — Phase 0 brief.

## Runtime data

User-local runtime data (config, sessions, SQLite DB) lives in `~/.opentrace/`,
never inside the repo.

## Environments

- **Python / backend** — conda env `opentrace-dev` (activate with `conda activate opentrace-dev`).
- **Node / Electron / Vite** — system Node toolchain (no env activation).
- **Traced programs** — whatever the user activates in the embedded terminal at runtime. Independent of the two above.
