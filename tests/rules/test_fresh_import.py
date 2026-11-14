"""§8.7 fresh import: pywebview's window process imports gui.py light, and importing the theme adds nothing.

Each check runs in a fresh interpreter (no window: the process is named SpawnProcess-1 like the real
window process, so ui.run returns before NiceGUI's native mode would start WebView2, and before its
pytest port lookup). The window process must import NiceGUI and set the window chrome, but never torch,
chatterbox, engine, config, ui.core, ui.launch or a theme, and it must leave no child process behind.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ui.contract.manifest import load_manifest, window_args

ROOT = Path(__file__).resolve().parents[2]
PROBE = "U7-PROBE "
TIMEOUT_S = 20
CREATE_NO_WINDOW = 0x08000000                   # never a console window, whatever starts pytest
HEAVY = ("torch", "chatterbox", "engine", "config", "ui.core", "ui.launch", "ui.themes")
PLACEMENT_KEYS = ("x", "y", "focus", "on_top")

WINDOW_PROCESS = """
import json, multiprocessing, runpy, sys
multiprocessing.current_process().name = "SpawnProcess-1"   # what pywebview's window process is called
sys.argv = ["gui.py", *ARGS]
runpy.run_path("gui.py", run_name="__mp_main__")
from nicegui import app
from ui.entry import probe_screens
print(PROBE + json.dumps({
    "modules": sorted(sys.modules),
    "window_args": app.native.window_args,
    "drag_direct": app.native.settings.get("DRAG_REGION_DIRECT_TARGET_ONLY"),
    "start_args": sorted(app.native.start_args),
    "children": len(multiprocessing.active_children()),
    "screens": len(probe_screens()),
    "storage": __import__("os").environ.get("NICEGUI_STORAGE_PATH"),
}, default=list))
"""

THEME_IMPORT = """
import json, sys
from nicegui import app
before = len(app.routes)
import ui.themes.emiya
print(PROBE + json.dumps({"added_routes": len(app.routes) - before, "modules": sorted(sys.modules)}))
"""


def _probe(code: str, **names: Any) -> dict[str, Any]:
    """Run code in a fresh interpreter in the app folder (offline, no bytecode, no window); return its JSON."""
    prelude = "".join(f"{name} = {value!r}\n" for name, value in {"PROBE": PROBE, **names}.items())
    env = {key: value for key, value in os.environ.items() if key != "NICEGUI_USER_SIMULATION"}
    env.update(HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", prelude + code], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=TIMEOUT_S, check=False, creationflags=CREATE_NO_WINDOW)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith(PROBE)]
    assert lines, done.stdout + done.stderr
    return json.loads(lines[-1][len(PROBE):])


def _heavy(modules: list[str]) -> list[str]:
    return [m for m in modules if any(m == name or m.startswith(name + ".") for name in HEAVY)]


@pytest.fixture(scope="module")
def manifest_args() -> dict[str, Any]:
    """The manifest's window args as JSON would carry them."""
    return json.loads(json.dumps(window_args(load_manifest(ROOT / "ui" / "themes" / "emiya")), default=list))


def test_the_window_process_imports_nothing_heavy(manifest_args: dict[str, Any]) -> None:
    storage_existed = (ROOT / ".nicegui-dev").exists()
    result = _probe(WINDOW_PROCESS, ARGS=["--fake"])
    assert "nicegui" in result["modules"] and "ui.entry" in result["modules"]
    assert _heavy(result["modules"]) == []
    assert {key: result["window_args"][key] for key in manifest_args} == manifest_args
    assert ({"x", "y"} <= set(result["window_args"])) is (result["screens"] > 0)    # centred when screens exist
    assert not {"focus", "on_top"} & set(result["window_args"])                      # a normal window
    assert result["drag_direct"] is True and "func" in result["start_args"]
    assert result["children"] == 0
    assert result["storage"] == str(ROOT / ".nicegui-dev")
    assert (ROOT / ".nicegui-dev").exists() is storage_existed                       # nothing was written


def test_a_test_window_process_leaves_the_placement_to_the_server(manifest_args: dict[str, Any]) -> None:
    result = _probe(WINDOW_PROCESS, ARGS=["--fake", "--smoke=all"])
    assert _heavy(result["modules"]) == []
    assert {key: result["window_args"][key] for key in manifest_args} == manifest_args
    assert not set(PLACEMENT_KEYS) & set(result["window_args"])     # the server's forwarded values win
    assert result["children"] == 0


def test_importing_the_theme_adds_no_route_and_nothing_heavy() -> None:
    result = _probe(THEME_IMPORT)
    assert result["added_routes"] == 0
    assert [m for m in _heavy(result["modules"]) if not m.startswith("ui.themes")] == []
