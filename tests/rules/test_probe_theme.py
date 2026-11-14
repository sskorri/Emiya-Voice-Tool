"""Second-theme readiness (R5): a throwaway theme, built in tmp_path, boots through the composition root.

The probe is the opposite of Emiya wherever the manifest allows (light, framed, resizable, its own palette, its
own pref, no FX) and is made only of stock NiceGUI, ui.kit and ui.contract: a shell and six plain pages that
render the contract's markers and call intents. It is never shipped. Each test writes it under tmp_path, makes it
importable as ui.themes.probe (tmp_path joins ui.themes.__path__ for the test) and runs a main file that takes
gui.py's steps with the probe's manifest and calls ui.launch.main(), under NiceGUI's user simulation on a
FakeEngine scenario's scratch data. If any of these fail, a second theme would need a core edit:

- every route renders SHELL_MARKERS and REQUIRED_MARKERS (a framed window needs no FRAMELESS_MARKERS);
- the composition root imports the theme the manifest names, serves only its static files, names the app after
  its window title and builds the settings store's theme section from its [prefs];
- Emiya's own prefs (fx_on, saber_art) are not there, and nothing fails for want of them;
- the one-model rule holds through the probe's controls (a named Models card loads once; an asked switch loads
  only on its primary), and the contract's escape() closes what Escape must close (D12);
- the probe's sources pass the layer lints R1-R6 that every theme obeys.
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from ui import entry
from ui.contract.manifest import load_manifest
from ui.contract.markers import FRAMELESS_MARKERS, REQUIRED_MARKERS, SHELL_MARKERS
from ui.contract.routes import PAGE_ID, ROUTES, Route
from ui.core.clock import CLOCK_ENV

from tests.rules.test_layers import AST_CHECKERS

ROOT = Path(__file__).resolve().parents[2]
EMIYA_ICON = ROOT / "ui" / "themes" / "emiya" / "static" / "icon.ico"
SLUG = "probe"
TITLE = "Probe Voice"
TURBO = "chatterbox-turbo"

# ---- the probe theme's files -------------------------------------------------------------------------------

THEME_TOML = f"""\
# Probe: a throwaway second theme that only proves the theme contract (tests\\rules\\test_probe_theme.py).
slug = "{SLUG}"
name = "Probe"
contract = [1, 1]
dark = false
css = ["probe.css"]

[window]
title = "{TITLE}"
frameless = false
size = [1024, 720]
min_size = [800, 600]
resizable = true
background_color = "#FFFFFF"
text_select = true
center = true
icon = "static/icon.ico"

[palette]
primary = "#2255AA"

[prefs]
density = "comfortable"
"""

INIT_PY = '''\
"""Probe: a throwaway theme of stock NiceGUI, ui.kit and ui.contract that proves the theme contract."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from ui.contract.manifest import ThemeManifest, load_manifest
from ui.contract.routes import Route
from ui.contract.theme import PageBuilder, ThemeContext

from .pages import PAGES
from .shell import build_shell

FOLDER = Path(__file__).resolve().parent


class ProbeTheme:
    """The contract's Theme: a manifest, six page builders and a shell (no pref_checks: its prefs are typed)."""

    def __init__(self) -> None:
        self.manifest: ThemeManifest = load_manifest(FOLDER)
        self.pages: Mapping[Route, PageBuilder] = PAGES

    def build_shell(self, ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        build_shell(ctx, mount_pages)


THEME = ProbeTheme()
'''

SHELL_PY = '''\
"""The probe shell: nav, the status chip, the dock, the ask, a toast line, the log, the player and the keys."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import context, ui

from ui.contract.routes import PAGE_ID, ROUTES, TITLES
from ui.contract.theme import ThemeContext
from ui.contract.viewmodels import DockRowVM, ShellVM
from ui.kit.audio import Player
from ui.kit.keys import KeyDispatcher


def build_shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
    behaviours = ctx.behaviours
    status, models, playback, commands = (behaviours.status(), behaviours.models(), behaviours.playback(),
                                          behaviours.commands())
    vm: ShellVM = status.vm
    router = context.client.sub_pages_router
    status.on_route(router.current_path)
    with ui.column().classes("probe-shell").mark("probe.shell"):
        with ui.row():
            for route in ROUTES:
                ui.button(TITLES[route], on_click=lambda route=route: ui.navigate.to(route)).props("flat").mark(
                    f"nav.{PAGE_ID[route]}")
            ui.button(on_click=status.toggle_log).bind_text_from(vm, "chip_text").props("outline").mark("status")
        with ui.row().mark("probe.dock"):
            for backend in ctx.core.state.engine.enabled:
                ui.button(on_click=lambda backend=backend: models.request_switch(backend, origin="dock")).bind_text_from(
                    vm, "dock_rows", backward=lambda rows, backend=backend: _row_text(rows, backend)).mark(
                    f"dock.row.{backend}")
            ui.button("Free VRAM", on_click=models.free_vram).mark("dock.free")
        with ui.card().mark("confirm.ask") as ask:
            ask.bind_visibility_from(vm, "ask", backward=lambda request: request is not None)
            ui.label().bind_text_from(vm, "ask", backward=lambda request: request.title if request else "")
            ui.button(on_click=lambda: _confirm(models, vm)).bind_text_from(
                vm, "ask", backward=lambda request: request.primary if request else "").mark("confirm.ask.primary")
            ui.button("Cancel", on_click=models.dismiss).mark("confirm.ask.cancel")
        ui.label().bind_text_from(vm, "toast", backward=lambda toast: toast.text if toast else "").mark("probe.toast")
        with ui.column().mark("probe.log") as log:
            log.bind_visibility_from(vm, "log_open")
            ui.label().bind_text_from(ctx.core.state.log, "lines", backward=lambda lines: " | ".join(lines[-3:]))
        Player(on_state=playback.on_player_state).bind_target_from(playback.vm)
        KeyDispatcher(dict(commands.combos()), on_command=lambda command_id: commands.handle(command_id, vm.route))
        KeyDispatcher({"Escape": "escape"}, on_command=lambda _command: status.escape()).mark("probe.escape")
        with ui.column().classes("probe-content").mark("probe.content"):
            mount_pages()
    router.on_path_changed(status.on_route)


async def _confirm(models: Any, vm: ShellVM) -> None:
    if vm.ask is not None:
        await models.confirm(vm.ask.token)


def _row_text(rows: tuple[DockRowVM, ...], backend: str) -> str:
    row = next((row for row in rows if row.backend == backend), None)
    if row is None:
        return backend
    return f"{row.label} · {row.info}" if row.info else row.label
'''

PAGES_PY = '''\
"""The six probe pages: stock NiceGUI and ui.kit bound to the contract's VMs; every control calls an intent."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from nicegui import ui

from ui.contract.routes import Route
from ui.contract.theme import PageBuilder, ThemeContext
from ui.kit.elements import ListHost                  # the kit's re-rendering list: no theme writes its own


def _go(route: Route | None) -> None:
    if route:
        ui.navigate.to(route)


def speak(ctx: ThemeContext) -> None:
    controller, playback = ctx.behaviours.speak(), ctx.behaviours.playback()
    vm = controller.vm
    with ui.column().mark("speak.voice"):
        ui.label().bind_text_from(vm, "count_text")
        ListHost(lambda group: [ui.button(item.voice_id, on_click=lambda v=item.voice_id: controller.pick_voice(v))
                             .mark(f"speak.voice.{item.voice_id}") for item in group.rows]).bind_items_from(vm, "groups")
    ui.textarea(value=ctx.core.session.text, on_change=lambda e: controller.set_text(e.value or "")).bind_value_from(
        ctx.core.session, "text").mark("speak.text")
    with ui.column().mark("speak.delivery"):
        ui.label().bind_text_from(vm, "expr_text")
    ui.button(on_click=lambda: _generate(controller)).bind_text_from(
        vm, "generate", backward=lambda action: action.label).mark("speak.generate")
    ui.label().bind_text_from(vm, "summary_text").mark("speak.status")
    with ui.column().mark("take.player"):
        ui.label().bind_text_from(vm, "card", backward=lambda card: card.title)
        ui.button(on_click=playback.toggle).bind_text_from(vm, "card", backward=lambda card: card.play.label).mark(
            "take.play")
    controller.opened()


async def _generate(controller: Any) -> None:
    pending = controller.vm.generate.pending
    await controller.generate(1, pending.token if pending is not None and pending.token else None)


def voices(ctx: ThemeContext) -> None:
    controller = ctx.behaviours.voices()
    vm = controller.vm

    def current() -> str:
        return vm.details.voice_id if vm.details is not None else ""

    with ui.column().mark("voices.list"):
        ListHost(lambda row: ui.button(row.voice_id, on_click=lambda v=row.voice_id: controller.select(v)).mark(
            f"voices.row.{row.voice_id}")).bind_items_from(vm, "rows")
    with ui.column().mark("voices.edit"):
        ui.label().bind_text_from(vm, "details", backward=lambda details: details.title if details else "")
        ui.button("Re-forge…", on_click=lambda: _go(controller.reforge(current()))).mark("voices.reforge")
        ui.button("Delete", on_click=lambda: controller.delete(current())).mark("voices.delete confirm.arm")
    controller.opened()


def forge(ctx: ThemeContext) -> None:
    controller = ctx.behaviours.forge()
    vm = controller.vm
    controller.opened()
    with ui.column().mark("forge.clip"):
        ui.label().bind_text_from(vm, "clip_file")
        ui.button("Browse…", on_click=controller.browse).mark("forge.browse")
    ui.input("Name", value=vm.name, on_change=lambda e: controller.set_name(e.value or "")).mark("forge.name")
    with ui.row().mark("forge.backend"):
        for key, label, _on in vm.segments:
            ui.button(label, on_click=lambda key=key: controller.set_backend(key)).mark(f"forge.backend.{key}")
    ui.checkbox("The speaker agreed to having this voice cloned", value=vm.consent,
                on_change=lambda e: controller.set_consent(bool(e.value))).mark("forge.consent")
    ui.button(on_click=lambda: _forge(controller)).bind_text_from(
        vm, "forge", backward=lambda action: action.label).mark("forge.go")


async def _forge(controller: Any) -> None:
    pending = controller.vm.forge.pending
    await controller.forge(pending.token if pending is not None and pending.token else None)


def takes(ctx: ThemeContext) -> None:
    controller, playback = ctx.behaviours.takes(), ctx.behaviours.playback()
    vm = controller.vm
    with ui.row():
        for key in ("session", "all", "starred"):
            ui.button(key, on_click=lambda key=key: controller.set_tab(key)).mark(f"takes.tab.{key}")
    with ui.column().mark("takes.list"):
        ListHost(lambda row: _take_row(row, controller, playback)).bind_items_from(vm, "rows")
    with ui.column().mark("takes.compare"):
        ui.label().bind_text_from(vm, "compare", backward=lambda compare: compare.note if compare else "")
    ui.button(on_click=controller.purge_unstarred).bind_text_from(
        vm, "purge", backward=lambda action: action.label).mark("takes.purge confirm.arm")


def _take_row(row: Any, controller: Any, playback: Any) -> None:
    with ui.row():
        ui.button(f"{row.id} {row.text}", on_click=lambda take=row.id: controller.row_click(take)).mark(
            f"takes.row.{row.id}")
        ui.button("unstar" if row.starred else "star", on_click=lambda take=row.id: playback.star(take)).mark(
            f"takes.star takes.star.{row.id}")


def models(ctx: ThemeContext) -> None:
    controller = ctx.behaviours.models()
    vm = controller.vm

    def label(cards: Any, backend: str) -> str:
        card = next((card for card in cards if card.backend == backend), None)
        return (card.action.aria or card.action.label) if card is not None else backend

    def act(backend: str) -> Any:
        card = next((card for card in vm.cards if card.backend == backend), None)
        if card is not None and card.action_kind == "unload":
            return controller.free_vram()
        controller.request_switch(backend, origin="card")
        return None

    with ui.row():
        for backend in ctx.core.state.engine.enabled:
            ui.button(on_click=lambda backend=backend: act(backend)).bind_text_from(
                vm, "cards", backward=lambda cards, backend=backend: label(cards, backend)).mark(
                f"models.switch models.switch.{backend}")
    ui.button("Free VRAM", on_click=controller.free_vram).mark("models.free")
    ui.label().bind_text_from(vm, "limit_text").mark("models.limit")
    with ui.column().mark("models.history"):
        ListHost(lambda line: ui.label(line[0])).bind_items_from(vm, "history")


def settings(ctx: ThemeContext) -> None:
    controller = ctx.behaviours.settings()
    vm = controller.vm
    with ui.column().mark("settings.core"):
        ui.switch("Autoplay New Takes", value=vm.autoplay,
                  on_change=lambda e: controller.set_autoplay(bool(e.value))).bind_value_from(vm, "autoplay")
        ui.label().bind_text_from(vm, "out_dir_text")
        ui.label(f"Density: {ctx.prefs.get('density')}").mark("probe.density")      # the probe's own pref
    with ui.column().mark("settings.about"):
        ui.label().bind_text_from(vm, "about", backward=lambda about: about.engine)


PAGES: Mapping[Route, PageBuilder] = MappingProxyType({
    "/": speak, "/voices": voices, "/forge": forge, "/takes": takes, "/models": models, "/settings": settings})
'''

CSS = "/* Probe theme: placeholder layout only (a contract probe, not a design). */\n.probe-shell { padding: 12px; }\n"

# gui.py's steps with the probe's manifest, then the composition root (ui.launch.main), under the user simulation.
MAIN_PY = '''\
"""The probe's main file for NiceGUI's user simulation: gui.py's steps with the probe theme's manifest."""
import os

from ui import entry
from ui.contract.manifest import window_args

APP_ROOT = os.environ["VOICETOOL_PROBE_APP_ROOT"]
FLAGS = entry.simulation_flags()
MANIFEST = entry.load_theme_manifest(os.environ["VOICETOOL_PROBE_THEME_DIR"])
if isinstance(MANIFEST, int):
    raise entry.BootFailure(MANIFEST, MANIFEST.text)
entry.use_app_name(MANIFEST.window.title)
os.environ.update(entry.environment(FLAGS, APP_ROOT, offline=True, simulation=True))
entry.register_font_types()

from nicegui import app  # noqa: E402

app.native.window_args.update(window_args(MANIFEST))
RUN_KWARGS = entry.run_arguments(MANIFEST, FLAGS)

if __name__ == "__main__":
    from ui.launch import main

    main(FLAGS, MANIFEST, RUN_KWARGS, app_root=APP_ROOT)
'''

SOURCES = {"__init__.py": INIT_PY, "shell.py": SHELL_PY, "pages.py": PAGES_PY}


# ---- fixtures ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """Where this test's probe theme and its scratch data live."""

    folder: Path                                  # tmp_path\\themes\\probe
    main: Path                                    # the main file the user simulation runs
    data: Path                                    # the scratch data root (VOICETOOL_DATA_DIR)


def write_probe(base: Path) -> Path:
    """The probe theme package under base\\probe; returns its folder."""
    folder = base / SLUG
    (folder / "static").mkdir(parents=True)
    (folder / "theme.toml").write_text(THEME_TOML, encoding="utf-8")
    for name, text in SOURCES.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "static" / "probe.css").write_text(CSS, encoding="utf-8")
    shutil.copyfile(EMIYA_ICON, folder / "static" / "icon.ico")
    return folder


@pytest.fixture
def probe(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Probe]:
    """The probe written to tmp_path, importable as ui.themes.probe, and the environment the main file reads."""
    import ui.themes

    themes = tmp_path / "themes"
    folder = write_probe(themes)
    main = tmp_path / "probe_main.py"
    main.write_text(MAIN_PY, encoding="utf-8")
    data = tmp_path / "data"
    marker = request.node.get_closest_marker("scenario")
    monkeypatch.setattr(ui.themes, "__path__", [*ui.themes.__path__, str(themes)])
    monkeypatch.setenv("VOICETOOL_PROBE_APP_ROOT", str(ROOT))
    monkeypatch.setenv("VOICETOOL_PROBE_THEME_DIR", str(folder))
    monkeypatch.setenv("VOICETOOL_FAKE", str(marker.args[0]) if marker is not None else "speak")
    monkeypatch.setenv("VOICETOOL_DATA_DIR", str(data))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv(CLOCK_ENV, "manual")
    monkeypatch.delenv("VOICETOOL_ENGINE", raising=False)
    monkeypatch.setattr(os, "startfile", lambda *args, **kwargs: None, raising=False)
    name = entry.app_name()
    try:
        yield Probe(folder=folder, main=main, data=data)
    finally:
        entry.use_app_name(name)
        for module in [module for module in sys.modules if module == f"ui.themes.{SLUG}"
                       or module.startswith(f"ui.themes.{SLUG}.")]:
            del sys.modules[module]


@pytest.fixture
async def user(probe: Probe, caplog: pytest.LogCaptureFixture) -> AsyncIterator[User]:
    """NiceGUI's user simulation of the probe's main file (this module's own `user`: like the plugin's, it fails
    a test that logged an ERROR)."""
    async with user_simulation(main_file=probe.main) as simulated:
        yield simulated
    errors = [record.getMessage() for record in caplog.get_records("call") if record.levelname == "ERROR"]
    assert errors == [], errors


def launched() -> Any:
    from ui import launch

    run = launch.LAST_LAUNCH
    assert run is not None, "the probe's main file did not compose the app"
    return run


async def opened(user: User, route: Route = "/") -> Any:
    """Open "/" and click the rail to route; returns the composed Launch once its core is ready."""
    await user.open("/")
    run = launched()
    await run.ready()
    if route != "/":
        user.find(marker=f"nav.{PAGE_ID[route]}").click()
    await user.should_see(marker="probe.shell")
    await run.core.scheduler.settle()
    return run


async def until(predicate: Any, what: str, core: Any, timeout: float = 10.0) -> None:
    """Poll in real time (the FakeEngine works in workers) while the manual clock settles what is due."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await core.scheduler.settle()
        await asyncio.sleep(0.02)


# ---- the tests --------------------------------------------------------------------------------------------

def test_the_probe_sources_pass_the_layer_lints(tmp_path: Path) -> None:
    """R1-R6 as they apply to a theme: only its own package, ui.contract, ui.kit and nicegui; nothing process-wide;
    sync builders; subscriptions in shell.py only; one .mark() per element."""
    folder = write_probe(tmp_path)
    problems = []
    for name in SOURCES:
        rel = PurePosixPath("ui", "themes", SLUG, name)
        tree = ast.parse((folder / name).read_text(encoding="utf-8"))
        problems += [problem for rule, check in AST_CHECKERS.items() for problem in check(rel, tree)]
    assert problems == []
    manifest = load_manifest(folder)
    assert (manifest.slug, manifest.window.title, manifest.window.frameless, dict(manifest.prefs)) == (
        SLUG, TITLE, False, {"density": "comfortable"})


@pytest.mark.parametrize("route", ROUTES)
async def test_every_route_renders_the_contract_markers(user: User, route: Route) -> None:
    run = await opened(user, route)
    await user.should_see(marker=f"page.{PAGE_ID[route]}")
    for marker in (*SHELL_MARKERS, *REQUIRED_MARKERS[route]):
        await user.should_see(marker=marker)
    assert not run.manifest.window.frameless                                  # so FRAMELESS_MARKERS are not needed
    for marker in FRAMELESS_MARKERS:
        await user.should_not_see(marker=marker)


@pytest.mark.scenario("firstrun")                 # a board without a seeded Log, so the launch line shows
async def test_the_composition_root_runs_the_theme_the_manifest_names(user: User, probe: Probe) -> None:
    run = await opened(user)
    assert (run.manifest.slug, run.manifest.folder) == (SLUG, probe.folder)
    assert type(run.theme).__module__ == f"ui.themes.{SLUG}" and run.theme.manifest == run.manifest
    assert run.core.app_name == TITLE and entry.app_name() == TITLE
    assert any(line.endswith(f"{TITLE} started · no model loaded (models load on demand)")
               for line in run.core.state.log.lines), run.core.state.log.lines
    assert (await user.http_client.get(f"/theme/{SLUG}/probe.css")).status_code == 200
    assert (await user.http_client.get("/theme/emiya/emiya.css")).status_code == 404
    assert run.paths.root == probe.data and not (probe.data / "ui_settings.json").exists()


async def test_the_settings_store_holds_the_probe_prefs_and_no_emiya_ones(user: User, probe: Probe) -> None:
    run = await opened(user, "/settings")
    settings = run.core.settings
    assert settings.theme_section == f"theme.{SLUG}"
    assert settings.pref("density") == "comfortable" and run.prefs.get("density") == "comfortable"
    assert settings.pref("fx_on", "absent") == "absent" and settings.pref("saber_art", "absent") == "absent"
    await user.should_see("Density: comfortable")
    behaviours = run.theme_context(user.client).behaviours
    assert behaviours.status().vm.art_visible is False                        # Emiya's saber art: off here
    assert run.theme_context(user.client).motion.fx_enabled is False          # no FX pref, no FX
    run.prefs.set("density", "compact")
    settings.flush()
    saved = (probe.data / "ui_settings.json").read_text(encoding="utf-8")
    assert '"theme.probe"' in saved and '"compact"' in saved and "theme.emiya" not in saved


@pytest.mark.scenario("speak")
async def test_a_named_models_card_loads_exactly_once(user: User) -> None:
    run = await opened(user, "/models")
    core, fake = run.core, run.engine
    fake.reset_records()
    user.find(marker=f"models.switch.{TURBO}").click()
    await until(lambda: fake.loads == [TURBO] and not core.state.job.kind, "one load of Turbo", core)
    assert core.state.engine.resident == TURBO and len(fake.residency().resident) == 1


@pytest.mark.scenario("switch")
async def test_the_asked_switch_loads_only_on_its_primary(user: User) -> None:
    run = await opened(user)
    core, fake = run.core, run.engine
    await user.should_see(marker="confirm.ask")
    assert fake.loads == []
    user.find(marker="confirm.ask.primary").click()
    await until(lambda: fake.loads == [TURBO] and not core.state.job.kind, "the asked switch", core)


@pytest.mark.scenario("switch")
async def test_escape_closes_the_ask_and_the_log_through_the_contract(user: User) -> None:
    run = await opened(user)
    await user.should_see(marker="confirm.ask")
    user.find(marker="status").click()
    await user.should_see(marker="probe.log")
    user.find(marker="probe.escape").trigger("command", {"id": "escape", "combo": "Escape"})
    await user.should_not_see(marker="confirm.ask")
    await user.should_not_see(marker="probe.log")
    assert run.engine.loads == []
