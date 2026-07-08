from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    electron_dir = repo_root / "electron"
    if not (electron_dir / "package.json").exists():
        print(
            "opentrace must be run from an editable install of a source "
            "checkout (pip install -e backend); see the README.",
            file=sys.stderr,
        )
        return 1
    if shutil.which("npm") is None:
        print("Node.js/npm not found on PATH; install Node.js first.", file=sys.stderr)
        return 1
    if not (electron_dir / "node_modules").is_dir():
        print(
            "electron/node_modules is missing — run ./start.sh once "
            "(or `npm install` in electron/).",
            file=sys.stderr,
        )
        return 1
    if not os.environ.get("OPENTRACE_DEV") and not (
        repo_root / "frontend" / "dist" / "index.html"
    ).exists():
        print(
            "frontend/dist is missing — run ./start.sh once to build the "
            "frontend, or set OPENTRACE_DEV=1 with the Vite dev server running.",
            file=sys.stderr,
        )
        return 1

    env = os.environ.copy()
    env["OPENTRACE_LAUNCH_CWD"] = os.getcwd()
    env.setdefault("OPENTRACE_PYTHON", sys.executable)
    try:
        proc = subprocess.run(["npm", "start"], cwd=electron_dir, env=env)
    except OSError as e:
        print(f"failed to launch electron via npm: {e}", file=sys.stderr)
        return 1
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
