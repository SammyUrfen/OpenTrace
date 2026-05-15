from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["OPENTRACE_LAUNCH_CWD"] = os.getcwd()
    env.setdefault("OPENTRACE_PYTHON", sys.executable)
    subprocess.run(["npm", 'start'], cwd=repo_root / "electron", env=env)
    return 0


if __name__ == "__main__":
    sys.exit(main())