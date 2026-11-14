"""FX tests (§8.6): node runs of the scripts in ui\\themes\\emiya\\tests and the Python side of the FX components."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / "ui" / "themes" / "emiya"
NODE = shutil.which("node")
NODE_TIMEOUT_S = 180


def run_node(script: Path, *args: str) -> dict[str, Any]:
    """Run one node script and return its JSON report (the last stdout line), with the exit code as "exit"."""
    assert NODE is not None, "node is not on PATH"
    done = subprocess.run([NODE, str(script), *args], cwd=ROOT, env={**os.environ, "HF_HUB_OFFLINE": "1"},
                          capture_output=True, text=True, encoding="utf-8", timeout=NODE_TIMEOUT_S, check=False)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"{script.name} printed nothing (exit {done.returncode}): {done.stderr[-2000:]}")
    report = json.loads(lines[-1])
    report["exit"] = done.returncode
    return report
