#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export OPENTRACE_LAUNCH_CWD="${OPENTRACE_LAUNCH_CWD:-$PWD}"
export OPENTRACE_PYTHON="${OPENTRACE_PYTHON:-$(command -v python || command -v python3 || true)}"
export OPENTRACE_HOME="$ROOT/tmp-opentrace"

if [[ -z "$OPENTRACE_PYTHON" ]]; then
  echo "[opentrace] no python found on PATH — install Python 3.11+ or set OPENTRACE_PYTHON." >&2
  exit 1
fi

# Functional probe: the interpreter must have all backend hard deps, whether it
# comes from the opentrace-dev conda env, a venv, or OPENTRACE_PYTHON.
if ! "$OPENTRACE_PYTHON" -c 'import fastapi, uvicorn, psutil, zstandard, aiosqlite, httpx' 2>/dev/null; then
  echo "[opentrace] $OPENTRACE_PYTHON lacks backend deps — activate the opentrace-dev env, run 'pip install -e backend' in your venv, or set OPENTRACE_PYTHON." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[opentrace] npm not found — install Node.js >= 22.12 (and gcc-c++/make for node-pty). See README Quick start." >&2
  exit 1
fi

# Build the frontend only if the dist entrypoint is missing.
if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "[opentrace] building frontend..."
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "[opentrace] installing frontend dependencies..."
    (cd "$ROOT/frontend" && npm ci)
  fi
  (cd "$ROOT/frontend" && npm run build)
fi

# Ensure Electron deps exist if the folder has not been installed yet.
if [[ ! -d "$ROOT/electron/node_modules" ]]; then
  echo "[opentrace] installing electron dependencies..."
  (cd "$ROOT/electron" && npm install)
fi

echo "[opentrace] launching Electron..."
cd "$ROOT/electron"
npm start
