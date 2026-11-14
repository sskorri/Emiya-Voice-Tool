"""Static layer lints R1-R4 and R6-R8 of §8.7 over ui\\ (and gui.py for the import rule).

Each lint is a small checker run over the real tree; the self-tests at the bottom feed every checker
code that must and must not trip it, so a lint can never pass just because the tree is still empty.
R5 (themes never load a model) is enforced statically by R1 (a theme cannot import the engine or the
core) and dynamically by test_one_model.py; R9 (no polled bindings) is test_bindings.py, both I1.
Imports inside `if TYPE_CHECKING:` are typing-only and exempt from the layer rules.
"""
from __future__ import annotations

import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SKIP_DIRS = frozenset({"__pycache__"})

STDLIB = frozenset(sys.stdlib_module_names)
NICEGUI_BEHAVIOURS = ("nicegui.binding", "nicegui.Event", "nicegui.event")
NICEGUI_CORE = NICEGUI_BEHAVIOURS + ("nicegui.run", "nicegui.app", "nicegui.background_tasks")
SERVICES_ENGINE = ("engine.audio", "engine.backends.info", "engine.types", "engine.device")
CORE_BANNED = frozenset({"torch", "soundfile", "numpy", "send2trash"})
CORE_EXTRAS = {"media.py": ("fastapi", "starlette"), "facade.py": ("filelock",)}
CONTRACT_STDLIB_ONLY = frozenset({"__init__.py", "manifest.py", "routes.py", "markers.py", "records.py",
                                  "plans.py", "ports.py"})
VOICE_ENGINE_HOLDERS = frozenset({PurePosixPath("ui/core/facade.py"), PurePosixPath("ui/launch.py")})
APP_BANNED = re.compile(r"add_\w*files?|on_\w+|colors|storage|native")
COMPONENT_SUFFIXES = frozenset({".js", ".mjs", ".vue"})
COLOUR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d|hsla?\(\s*\d")
URL_RE = re.compile(rb"https?://", re.IGNORECASE)
LAYERS = {"contract": "contract", "core": "core", "behaviours": "behaviours", "kit": "kit", "themes": "themes"}
ROOT_FILES = {"__init__.py": "ui_init", "entry.py": "entry", "launch.py": "launch"}


# ---- the tree ------------------------------------------------------------------------------------

def _files(base: Path, suffixes: frozenset[str] | None = None) -> list[Path]:
    """Every file under base, sorted, without __pycache__."""
    if not base.is_dir():
        return []
    found = [p for p in base.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(base).parts)]
    return sorted(p for p in found if suffixes is None or p.suffix.lower() in suffixes)


def _rel(path: Path) -> PurePosixPath:
    return PurePosixPath(path.relative_to(ROOT).as_posix())


def _parsed(*, with_gui: bool = False) -> tuple[list[tuple[PurePosixPath, ast.Module]], list[str]]:
    """(rel, tree) for every .py under ui\\ (plus gui.py), and the files that do not parse."""
    paths = _files(UI, frozenset({".py"}))
    if with_gui and (ROOT / "gui.py").is_file():
        paths.append(ROOT / "gui.py")
    trees, broken = [], []
    for path in paths:
        try:
            trees.append((_rel(path), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
        except (SyntaxError, UnicodeDecodeError) as error:
            broken.append(f"{_rel(path)}: {error}")
    return trees, broken


def layer_of(rel: PurePosixPath) -> str:
    """The §2 layer a ui file belongs to ("unknown" when it is in none)."""
    parts = rel.parts
    if parts == ("gui.py",):
        return "entry_script"
    if len(parts) < 2 or parts[0] != "ui":
        return "outside"
    if len(parts) == 2:
        return ROOT_FILES.get(parts[1], "unknown")
    if parts[1] == "core" and len(parts) > 3 and parts[2] == "services":
        return "services"
    return LAYERS.get(parts[1], "unknown")


def module_of(rel: PurePosixPath) -> tuple[str, bool]:
    """Dotted module name, and whether the file is a package __init__."""
    parts = list(rel.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def own_package(rel: PurePosixPath, layer: str) -> str:
    if layer == "themes":
        return f"ui.themes.{rel.parts[2]}" if len(rel.parts) > 3 else "ui.themes"
    return {"contract": "ui.contract", "core": "ui.core", "services": "ui.core.services",
            "behaviours": "ui.behaviours", "kit": "ui.kit"}.get(layer, "")


def _typing_only(tree: ast.Module) -> set[int]:
    """ids of the nodes inside `if TYPE_CHECKING:` bodies."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            ids.update(id(sub) for stmt in node.body for sub in ast.walk(stmt))
    return ids


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute module a (possibly relative) from-import reads from."""
    if not node.level:
        return node.module or ""
    parts = (module if is_package else module.rpartition(".")[0]).split(".")
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    base = ".".join(parts)
    return f"{base}.{node.module}" if node.module else base


def runtime_imports(tree: ast.Module, module: str, is_package: bool) -> list[tuple[int, str]]:
    """(line, dotted target) of every import that runs; `from a import b` gives "a.b"."""
    skip = _typing_only(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, is_package)
            found += [(node.lineno, base if a.name == "*" else f"{base}.{a.name}") for a in node.names]
    return found


def _within(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def allowed_for(rel: PurePosixPath, layer: str) -> tuple[str, ...] | None:
    """Import prefixes a layer may use besides the stdlib; None = unrestricted (composition root)."""
    own = own_package(rel, layer)
    if layer == "contract":
        return (own,) if rel.name in CONTRACT_STDLIB_ONLY else (own,) + NICEGUI_BEHAVIOURS
    if layer == "services":
        return (own, "ui.contract") + SERVICES_ENGINE + NICEGUI_CORE
    if layer == "core":
        extras = CORE_EXTRAS.get(rel.name, ()) if len(rel.parts) == 3 else ()
        return (own, "ui.contract", "engine", "config") + NICEGUI_CORE + extras
    if layer == "behaviours":
        return (own, "ui.core", "ui.contract") + NICEGUI_BEHAVIOURS
    if layer == "kit":
        return (own, "ui.contract", "nicegui")
    if layer == "themes":
        return (own, "ui.contract", "ui.kit", "nicegui")
    if layer == "entry":
        return ("filelock", "ui.contract.manifest")
    return None


# ---- checkers (rel, tree) -> problems ------------------------------------------------------------

def check_layer_imports(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R1: imports per layer (§8.7), ui\\__init__.py empty, every file in a known layer."""
    layer = layer_of(rel)
    if layer == "unknown":
        return [f"{rel}: not in a layer of the §2 file tree"]
    if layer == "ui_init":
        return [f"{rel}:{node.lineno}: ui\\__init__.py must stay empty" for node in tree.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    allowed = allowed_for(rel, layer)
    if allowed is None:
        return []
    module, is_package = module_of(rel)
    problems = []
    for line, target in runtime_imports(tree, module, is_package):
        top = target.partition(".")[0]
        if _within(target, "ui.kit.sound_out") and rel.as_posix() not in ("ui/kit/sound_out.py", "ui/launch.py"):
            problems.append(f"{rel}:{line}: select the Windows call audio adapter only in ui/launch.py")
        elif layer in ("core", "services") and top in CORE_BANNED:
            problems.append(f"{rel}:{line}: {target} is banned under ui\\core")
        elif top not in STDLIB and not any(_within(target, prefix) for prefix in allowed):
            problems.append(f"{rel}:{line}: the {layer} layer may not import {target}")
    return problems


def check_bare_ui_import(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R1: from-imports only; `import ui.x` rebinds the name ui and hides nicegui's ui."""
    return [f"{rel}:{node.lineno}: 'import {alias.name}' rebinds ui; use a from-import"
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names if alias.name == "ui" or alias.name.startswith("ui.")]


def check_voice_engine(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R1: only ui\\core\\facade.py and ui\\launch.py reference VoiceEngine."""
    if rel in VOICE_ENGINE_HOLDERS:
        return []
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "VoiceEngine":
            lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "VoiceEngine":
            lines.add(node.lineno)
        elif isinstance(node, ast.alias) and "VoiceEngine" in (node.name.rpartition(".")[2], node.asname):
            lines.add(node.lineno)
    return [f"{rel}:{line}: only facade.py and launch.py may reference VoiceEngine" for line in sorted(lines)]


def _nicegui_names(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Local names bound to nicegui's ui, nicegui's app and the nicegui package."""
    ui_names, app_names, package_names = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nicegui" and not node.level:
            for alias in node.names:
                if alias.name == "ui":
                    ui_names.add(alias.asname or "ui")
                elif alias.name == "app":
                    app_names.add(alias.asname or "app")
        elif isinstance(node, ast.Import):
            package_names.update(a.asname or "nicegui" for a in node.names if a.name == "nicegui")
    return ui_names, app_names, package_names


def _owner(node: ast.expr, names: tuple[set[str], set[str], set[str]]) -> str | None:
    """"ui" or "app" when node is nicegui's ui or app object."""
    ui_names, app_names, package_names = names
    if isinstance(node, ast.Name):
        return "ui" if node.id in ui_names else "app" if node.id in app_names else None
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in package_names and node.attr in ("ui", "app")):
        return node.attr
    return None


def check_process_wide(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R2: nothing process-wide in a theme."""
    if layer_of(rel) != "themes":
        return []
    names = _nicegui_names(tree)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = _owner(node.value, names)
            if (owner == "ui" and node.attr in ("page", "run")) or (
                    owner == "app" and APP_BANNED.fullmatch(node.attr)):
                problems.append(f"{rel}:{node.lineno}: {owner}.{node.attr} is process-wide (composition root only)")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "shared" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    problems.append(f"{rel}:{node.lineno}: shared=True is process-wide (composition root only)")
    return problems


def _component_prefix(rel: PurePosixPath) -> str | None:
    layer = layer_of(rel)
    if layer == "themes" and len(rel.parts) > 3:
        return f"{rel.parts[2]}_"
    return "vt_" if layer == "kit" else None


def check_component_names(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R3: `component=` names a <slug>_*.js file in a theme or a vt_*.js file in the kit."""
    prefix = _component_prefix(rel)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "component" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    name = PurePosixPath(value.value).name
                    if prefix is None or not name.startswith(prefix) or not name.endswith(".js"):
                        problems.append(f"{rel}:{node.lineno}: component {value.value!r} must be "
                                        f"{prefix or '<slug>_ or vt_'}*.js")
    return problems


def check_builders(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R4: builders are sync; core events are subscribed in shell.py only."""
    if layer_of(rel) != "themes":
        return []
    is_shell = len(rel.parts) == 4 and rel.parts[3] == "shell.py"
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and (node.name == "build" or node.name.startswith("build_")):
            problems.append(f"{rel}:{node.lineno}: builders are sync, not 'async def {node.name}'")
        elif (not is_shell and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "subscribe"):
            problems.append(f"{rel}:{node.lineno}: .subscribe( belongs in shell.py only")
    return problems


def _is_mark(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mark"


def _marks_in_chain(call: ast.Call) -> int:
    """How many .mark( calls the call chain ending in call contains."""
    count, node = 0, call
    while True:
        if isinstance(node, ast.Call):
            count += _is_mark(node)
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return count


def check_markers(rel: PurePosixPath, tree: ast.Module) -> list[str]:
    """R6: one .mark( per element call chain (mark() replaces the markers)."""
    lines = {node.lineno for node in ast.walk(tree) if _is_mark(node) and _marks_in_chain(node) > 1}
    return [f"{rel}:{line}: give an element all its markers in one .mark(...) call" for line in sorted(lines)]


AST_CHECKERS: dict[str, Callable[[PurePosixPath, ast.Module], list[str]]] = {
    "R1": check_layer_imports,
    "R1-bare": check_bare_ui_import,
    "R1-engine": check_voice_engine,
    "R2": check_process_wide,
    "R3-component": check_component_names,
    "R4": check_builders,
    "R6": check_markers,
}


# ---- checkers on paths, text and bytes -----------------------------------------------------------

def check_component_file(rel: PurePosixPath) -> list[str]:
    """R3: .js components are <slug>_*.js under their theme and vt_*.js under ui\\kit."""
    if rel.suffix.lower() not in COMPONENT_SUFFIXES:
        return []
    if layer_of(rel) == "themes" and len(rel.parts) > 4 and rel.parts[3] == "tests":
        return []  # node tests and reference copies, never served
    prefix = _component_prefix(rel)
    if prefix is None:
        return [f"{rel}: components live only in ui\\kit (vt_*.js) or a theme (<slug>_*.js)"]
    if rel.suffix != ".js" or not rel.name.startswith(prefix):
        return [f"{rel}: must be named {prefix}*.js"]
    return []


def check_colours(rel: PurePosixPath, text: str) -> list[str]:
    """R7: no literal colour values in .py or .js under ui\\ (ui\\themes\\*\\tests\\ is exempt)."""
    if rel.suffix.lower() not in (".py", ".js"):
        return []
    if layer_of(rel) == "themes" and len(rel.parts) > 4 and rel.parts[3] == "tests":
        return []
    return [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: colour literal {m.group(0)!r}; use a CSS token"
            for m in COLOUR_RE.finditer(text)]


def check_offline(rel: PurePosixPath, data: bytes) -> list[str]:
    """R8: no http:// or https:// anywhere in ui\\ (code, comments, CSS, static)."""
    return [f"{rel}: byte {m.start()}: {m.group(0).decode()!r} (the app is offline)"
            for m in list(URL_RE.finditer(data))[:5]]


# ---- the lints on the real tree ------------------------------------------------------------------

def test_ui_python_files_parse() -> None:
    _, broken = _parsed(with_gui=True)
    assert broken == []


@pytest.mark.parametrize("rule", sorted(AST_CHECKERS))
def test_ast_rule_holds_on_the_tree(rule: str) -> None:
    trees, _ = _parsed(with_gui=rule == "R1-bare")
    problems = [p for rel, tree in trees for p in AST_CHECKERS[rule](rel, tree)]
    assert problems == []


def test_r3_component_files_are_named_for_their_owner() -> None:
    problems = [p for path in _files(UI) for p in check_component_file(_rel(path))]
    assert problems == []


def test_r7_no_colour_literals_in_code() -> None:
    problems = [p for path in _files(UI, frozenset({".py", ".js"}))
                for p in check_colours(_rel(path), path.read_text(encoding="utf-8", errors="replace"))]
    assert problems == []


def test_r8_no_urls_anywhere_under_ui() -> None:
    problems = [p for path in _files(UI) if path.suffix != ".pyc"
                for p in check_offline(_rel(path), path.read_bytes())]
    assert problems == []


def test_the_contract_exists() -> None:
    """The lints above run on at least U1's contract, so they never pass on an empty tree."""
    names = {path.name for path in _files(UI / "contract", frozenset({".py"}))}
    assert {"__init__.py", "routes.py", "markers.py", "manifest.py", "records.py", "state.py", "plans.py",
            "viewmodels.py", "theme.py", "ports.py"} <= names


# ---- self-tests: every checker fires on violations and stays quiet on legal code -----------------

JOBS, SERVICE, SPEAK = "ui/core/jobs.py", "ui/core/services/takes_store.py", "ui/behaviours/speak.py"
SHELL, PAGE, FX = "ui/themes/emiya/shell.py", "ui/themes/emiya/pages/speak.py", "ui/themes/emiya/fx/forge_field.py"

AST_CASES = [
    # R1: core
    ("R1", JOBS, "from nicegui import ui", True),
    ("R1", JOBS, "import nicegui", True),
    ("R1", JOBS, "import torch", True),
    ("R1", JOBS, "from nicegui import app, background_tasks, binding, Event, run", False),
    ("R1", JOBS, "from nicegui.binding import BindableProperty\nfrom nicegui.event import Event", False),
    ("R1", JOBS, "import config\nfrom engine.errors import EngineBusy\nfrom ui.contract.state import JobState", False),
    ("R1", JOBS, "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import torch", False),
    ("R1", "ui/core/media.py", "from starlette.responses import FileResponse\nfrom fastapi import Request", False),
    ("R1", JOBS, "from starlette.responses import FileResponse", True),
    ("R1", "ui/core/facade.py", "import filelock", False),
    ("R1", JOBS, "import filelock", True),
    ("R1", JOBS, "from ui.behaviours.status import StatusController", True),
    ("R1", JOBS, "from ui.kit.window import quit_app", True),
    ("R1", "ui/core/fake.py", "from send2trash import send2trash", True),
    # R1: services
    ("R1", SERVICE, "from engine.audio import peaks\nfrom engine.types import Residency", False),
    ("R1", "ui/core/services/settings.py", "from nicegui import Event, run", False),
    ("R1", "ui/core/services/clips.py", "from engine.engine import VoiceEngine", True),
    ("R1", "ui/core/services/voices.py", "import config", True),
    ("R1", SERVICE, "import numpy as np", True),
    ("R1", SERVICE, "import soundfile", True),
    ("R1", "ui/core/services/estimates.py", "from .takes_store import TakesStore", False),
    ("R1", "ui/core/services/estimates.py", "from ui.core.clock import Scheduler", True),
    # R1: behaviours
    ("R1", SPEAK, "from nicegui import binding, Event\nfrom ui.core.jobs import JobRunner", False),
    ("R1", SPEAK, "from .confirm import Arm\nfrom ui.contract.plans import ActionState", False),
    ("R1", SPEAK, "from ui.kit.window import quit_app", True),
    ("R1", SPEAK, "from nicegui import ui", True),
    ("R1", SPEAK, "from nicegui import run", True),
    ("R1", SPEAK, "import engine", True),
    # R1: kit
    ("R1", "ui/kit/port.py", "from .window import quit_app\nfrom nicegui import app, ui", False),
    ("R1", "ui/kit/files.py", "from ui.contract.ports import KitPort", False),
    ("R1", "ui/kit/files.py", "from ui.kit.sound_out import WindowsAudioOut", True),
    ("R1", SHELL, "from ui.kit.sound_out import WindowsAudioOut", True),
    ("R1", "ui/kit/files.py", "from ui.core import Core", True),
    ("R1", "ui/kit/files.py", "import webview", True),
    # R1: themes
    ("R1", SHELL, "from nicegui import ui\nfrom ui.kit.audio import Player\nfrom .widgets import Card", False),
    ("R1", PAGE, "from ..widgets import Card\nfrom ui.contract.theme import ThemeContext", False),
    ("R1", SHELL, "from ui.behaviours import BehaviourFactory", True),
    ("R1", SHELL, "from ui.core.events import CoreEvents", True),
    ("R1", SHELL, "from ui.themes.paper import THEME", True),
    ("R1", FX, "import engine.audio", True),
    # R1: entry, launch, contract, ui\__init__.py, unknown files
    ("R1", "ui/entry.py", "import os\nfrom filelock import FileLock, Timeout", False),
    ("R1", "ui/entry.py", "from ui.contract.manifest import ManifestError, load_manifest", False),
    ("R1", "ui/entry.py", "from nicegui import app", True),
    ("R1", "ui/entry.py", "from ui.contract.state import EngineState", True),
    ("R1", "ui/launch.py", "import send2trash\nfrom ui.themes.emiya import THEME\nfrom ui.core import Core", False),
    ("R1", "ui/contract/manifest.py", "import tomllib\nfrom ui.contract import CONTRACT_VERSION", False),
    ("R1", "ui/contract/manifest.py", "from nicegui import binding", True),
    ("R1", "ui/contract/state.py", "from nicegui import binding\nfrom .records import Take", False),
    ("R1", "ui/contract/state.py", "from nicegui import ui", True),
    ("R1", "ui/contract/viewmodels.py", "from ui.core.jobs import StartResult", True),
    ("R1", "ui/__init__.py", "", False),
    ("R1", "ui/__init__.py", '"""Docstring only."""', False),
    ("R1", "ui/__init__.py", "import os", True),
    ("R1", "ui/helpers.py", "import os", True),
    # R1: bare `import ui.x`, VoiceEngine holders
    ("R1-bare", "ui/launch.py", "import ui.core", True),
    ("R1-bare", "gui.py", "import ui.launch", True),
    ("R1-bare", "gui.py", "from ui.launch import main\nfrom nicegui import ui", False),
    ("R1-engine", JOBS, "from engine.engine import VoiceEngine", True),
    ("R1-engine", JOBS, "import engine.engine\nx = engine.engine.VoiceEngine", True),
    ("R1-engine", "ui/core/facade.py", "from engine.engine import VoiceEngine", False),
    ("R1-engine", "ui/launch.py", "engine = VoiceEngine(config)", False),
    ("R1-engine", "ui/core/fake.py", '"""Same public surface as VoiceEngine."""', False),
    # R2
    ("R2", SHELL, "from nicegui import ui\n@ui.page('/x')\ndef f():\n    pass", True),
    ("R2", SHELL, "from nicegui import ui\nui.run()", True),
    ("R2", SHELL, "from nicegui import app\napp.add_static_files('/a', 'b')", True),
    ("R2", SHELL, "from nicegui import app\napp.add_media_file(local_file='a', url_path='/b')", True),
    ("R2", SHELL, "from nicegui import app\napp.on_startup(print)", True),
    ("R2", SHELL, "from nicegui import app\napp.colors(primary='x')", True),
    ("R2", SHELL, "from nicegui import app\napp.storage.general['k'] = 1", True),
    ("R2", SHELL, "from nicegui import app as a\nw = a.native.main_window", True),
    ("R2", SHELL, "import nicegui\nnicegui.app.colors(primary='x')", True),
    ("R2", SHELL, "from nicegui import ui\nui.add_head_html('x', shared=True)", True),
    ("R2", SHELL, "from nicegui import ui\nui.add_head_html('x')\nui.page_title('t')\nui.run_javascript('1')", False),
    ("R2", "ui/kit/window.py", "from nicegui import app\napp.native.main_window.destroy()", False),
    # R3 component keywords
    ("R3-component", FX, "class F(ui.element, component='forge_field.js'):\n    pass", True),
    ("R3-component", FX, "class F(ui.element, component='emiya_forge_field.js'):\n    pass", False),
    ("R3-component", "ui/kit/keys.py", "class K(ui.element, component='vt_keys.js'):\n    pass", False),
    ("R3-component", "ui/kit/keys.py", "class K(ui.element, component='keys.js'):\n    pass", True),
    ("R3-component", "ui/core/native.py", "class K(ui.element, component='vt_keys.js'):\n    pass", True),
    # R4
    ("R4", PAGE, "async def build(ctx):\n    pass", True),
    ("R4", SHELL, "async def build_shell(ctx, mount_pages):\n    pass", True),
    ("R4", PAGE, "def build(ctx):\n    ctx.core.events.changed.subscribe(print)", True),
    ("R4", SHELL, "def build_shell(ctx, mount_pages):\n    ctx.core.events.changed.subscribe(print)", False),
    ("R4", PAGE, "def build(ctx):\n    async def on_click():\n        pass", False),
    # R6
    ("R6", PAGE, "ui.button('x').mark('a').mark('b')", True),
    ("R6", PAGE, "ui.button('x').mark('a').classes('c').mark('b')", True),
    ("R6", PAGE, "ui.button('x').mark('a', 'b').classes('c')", False),
    ("R6", PAGE, "a = ui.button('x').mark('a')\nb = ui.label('y').mark('b')", False),
]


@pytest.mark.parametrize(("rule", "rel", "source", "flags"), AST_CASES)
def test_ast_checker_self_test(rule: str, rel: str, source: str, flags: bool) -> None:
    problems = AST_CHECKERS[rule](PurePosixPath(rel), ast.parse(source))
    assert bool(problems) is flags, problems


@pytest.mark.parametrize(("rel", "flags"), [
    ("ui/themes/emiya/fx/emiya_forge_field.js", False),
    ("ui/themes/emiya/fx/forge_field.js", True),
    ("ui/themes/emiya/widget.vue", True),
    ("ui/themes/emiya/tests/port_parity.mjs", False),
    ("ui/themes/emiya/tests/mockup_engine_ref.js", False),
    ("ui/kit/vt_keys.js", False),
    ("ui/kit/keys.js", True),
    ("ui/kit/vt_player.mjs", True),
    ("ui/core/helper.js", True),
    ("ui/themes/emiya/static/emiya.css", False),
])
def test_component_file_self_test(rel: str, flags: bool) -> None:
    assert bool(check_component_file(PurePosixPath(rel))) is flags


@pytest.mark.parametrize(("rel", "text", "flags"), [
    ("ui/themes/emiya/widgets.py", "GOLD = '#FCDB7F'", True),
    ("ui/themes/emiya/fx/emiya_forge_field.js", "ctx.fillStyle = 'rgba(252, 219, 127, 0.5)';", True),
    ("ui/kit/vt_keys.js", "el.style.color = 'hsl(10, 50%, 50%)';", True),
    ("ui/themes/emiya/fx/emiya_forge_field.js", "return 'rgba(' + c[0] + ',' + c[1] + ')';", False),
    ("ui/themes/emiya/widgets.py", "COLOUR = 'var(--gold)'  # see section 6.4", False),
    ("ui/themes/emiya/tests/mockup_engine_ref.js", "const GOLD = '#FCDB7F';", False),
    ("ui/themes/emiya/static/emiya.css", "--gold: #FCDB7F;", False),
])
def test_colour_checker_self_test(rel: str, text: str, flags: bool) -> None:
    assert bool(check_colours(PurePosixPath(rel), text)) is flags


@pytest.mark.parametrize(("data", "flags"), [
    (b"see https://example.com", True),
    (b'<svg xmlns="http://www.w3.org/2000/svg">', True),
    (b"@import url(HTTPS://fonts.example/x.css);", True),
    (b"@import url('pages/speak.css');", False),
])
def test_offline_checker_self_test(data: bytes, flags: bool) -> None:
    assert bool(check_offline(PurePosixPath("ui/themes/emiya/static/emiya.css"), data)) is flags


@pytest.mark.parametrize(("rel", "layer"), [
    ("gui.py", "entry_script"),
    ("ui/__init__.py", "ui_init"),
    ("ui/entry.py", "entry"),
    ("ui/launch.py", "launch"),
    ("ui/contract/state.py", "contract"),
    ("ui/core/facade.py", "core"),
    ("ui/core/services/clips.py", "services"),
    ("ui/core/services/__init__.py", "services"),
    ("ui/behaviours/speak.py", "behaviours"),
    ("ui/kit/port.py", "kit"),
    ("ui/themes/emiya/pages/speak.py", "themes"),
    ("ui/rescue.py", "unknown"),
])
def test_layer_of(rel: str, layer: str) -> None:
    assert layer_of(PurePosixPath(rel)) == layer
