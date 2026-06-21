#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This launcher is intended to be run from the opentrace-dev conda env.
if [[ -z "${CONDA_PREFIX:-}" ]]; then
  echo "Please activate the opentrace-dev conda environment first." >&2
  exit 1
fi

export OPENTRACE_LAUNCH_CWD="${OPENTRACE_LAUNCH_CWD:-$PWD}"
export OPENTRACE_PYTHON="${OPENTRACE_PYTHON:-$(command -v python)}"
export OPENTRACE_HOME="$ROOT/tmp-opentrace"

# Build the frontend only if the dist entrypoint is missing.
if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "[opentrace] building frontend..."
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