"""The Settings page under NiceGUI's user fixture (§6.12, §7, §8.5): gui.py on a FakeEngine scenario in tmp_path.

Covered: the required and listed markers on the General tab, and no Theme, Window or Runtime row; every row of
§6.12 with the mockup's copy and the value the store holds; the Output Folder as an absolute path with its full
path as the tooltip, Browse… through the KitPort (saved, refused, no window); the values in the scratch
ui_settings.json and read back by a fresh store; the Effects reaching the forge field, the body class and the
logos live; key capture on the Keys tab (bind, clash, reserved, Escape, Backspace, blur, tab change) with the
key dispatcher suspended while a slot listens; the About card with the GPU detected, absent and failed; the
Settings board opening on Effects; and the page's wiring rows (tests\\pages\\wiring\\settings.py).
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import binding
from nicegui.element import Element
from nicegui.elements.query import QueryElement
from nicegui.testing import User

from ui import launch
from ui.behaviours.commands import CommandRegistry
from ui.behaviours.settings import SettingsController
from ui.contract.markers import REQUIRED_MARKERS
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.core.services.device import GPU_TIP
from ui.core.services.settings import ABOUT_TIPS, SettingsStore, engine_text
from ui.themes.emiya.shell import Shell, ShellRoot
from ui.themes.emiya.widgets import Box, Logo, TextSpan

from tests.behaviours import FakeKit
from tests.pages.wiring import Wire
from tests.pages.wiring.settings import KEYS_FOOT, WIRING

ROUTE = "/settings"
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
GIF, STILL = "/theme/emiya/img/logo.gif", "/theme/emiya/img/logo-still.png"
IDS = ("gen", "gen3", "play", "prev", "next", "ab", "save", "search", "call_send", "call_stop")
LABELS = ("Generate", "Generate 3 Takes", "Play / Pause", "Previous Take", "Next Take", "Switch A / B", "Save As…",
          "Search", "Send to Call", "Stop Sending")
KEYS = ("Ctrl+Enter", "Ctrl+Shift+Enter", "Space", "Alt+Up", "Alt+Down", "Ctrl+B", "Ctrl+S", "Ctrl+K", "—", "—")
ABOUT_LABELS = ("Engine", "GPU", "Models in VRAM", "Voice registry", "Voice cache", "Reference clips", "Takes")
ABOUT_FIELDS = ("engine", "gpu", "models_in_vram", "registry", "cache", "clips", "takes")
GENERAL_MARKERS = ("settings.output", "settings.output.path", "settings.autoplay", "settings.stop_drops_queue", "settings.device",
                   "settings.device.cuda", "settings.device.cpu", "settings.device_note", "settings.default_backend",
                   *(f"settings.default_backend.{backend}" for backend in (MTL, STD, TUR)), "settings.offline")
EFFECTS_MARKERS = ("settings.effects", "settings.fx_on", "settings.fx_speed", "settings.fx_speed_text",
                   "settings.work_glow", "settings.reduce_motion", "settings.reduce_motion.system",
                   "settings.reduce_motion.always", "settings.saber_art", "settings.saber_art.show",
                   "settings.saber_art.hide")
GENERAL_FOOT = "Settings are saved with the app, so they survive restarts."
EFFECTS_FOOT = "Changes here apply live. Reduce Motion holds the effects and the logo as still frames."


# ---- helpers ------------------------------------------------------------------------------------------

class ForgeStub:
    """The Forge controller's set_backend (P3 owns the real one): Default Model re-picks an idle draft."""

    def __init__(self) -> None:
        self.backends: list[str] = []

    def set_backend(self, backend: str) -> None:
        self.backends.append(backend)

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Navigation closes the language lists."""


def the(user: User, marker: str) -> Any:
    """The one visible element with this marker."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def shell_of(user: User) -> Shell:
    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell


def classes(element: Element) -> set[str]:
    return set(element.classes)


def texts_in(element: Element) -> list[str]:
    return [span.text for span in element.descendants() if isinstance(span, TextSpan)]


def store_of(core: Core) -> dict[str, Any]:
    """The scratch ui_settings.json after a flush."""
    core.settings.flush()
    return json.loads(core.paths.settings.read_text(encoding="utf-8"))


def saved_speed(core: Core) -> int | None:
    """Effect Speed as the scratch ui_settings.json holds it now (no flush), or None."""
    try:
        data = json.loads(core.paths.settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("theme.emiya", {}).get("fx_speed")


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds; dialogs, probes and readings run on worker threads."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks run, then wait for the work spawned on the manual clock (no scenario here holds a job)."""
    for _ in range(5):
        await asyncio.sleep(0)
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if fake.scenario.holds else [])
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine, open_route: Any) -> Shell:
    """Open Settings from the rail as a user does, let the first reading land, return the page's shell."""
    await open_route(user, ROUTE)
    await settle(core, fake)
    return shell_of(user)


def toast_of(shell: Shell) -> tuple[str, bool] | None:
    toast = shell.ctx.behaviours.status().vm.toast
    return None if toast is None else (toast.text, toast.bad)


def segments(group: Element) -> list[tuple[str, str]]:
    """(key, label) of a segmented group's segments, in order."""
    return [(segment.key, texts_in(segment)[0]) for segment in group.descendants() if "em-seg" in segment.classes]


def _blank_page(_ctx: Any) -> None:
    """Another page's builder in this module: nothing (its own package tests it)."""


@pytest.fixture
def run(user: User) -> launch.Launch:
    assert launch.LAST_LAUNCH is not None
    return launch.LAST_LAUNCH


@pytest.fixture(autouse=True)
def only_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other five pages build nothing here, so these tests see the Settings page in the real shell
    whatever state the other page packages are in (I1 runs the pages together). Tests without the user
    fixture (the file checks) start no app at all."""
    if "user" not in request.fixturenames:
        return
    run: launch.Launch = request.getfixturevalue("run")
    pages = {route: build if route == ROUTE else _blank_page for route, build in run.theme.pages.items()}
    monkeypatch.setattr(run, "theme", SimpleNamespace(manifest=run.theme.manifest,
                                                      build_shell=run.theme.build_shell, pages=pages))


# ---- the General tab ----------------------------------------------------------------------------------

async def test_the_general_tab_renders_its_markers_and_no_theme_row(user: User, core: Core, fake: FakeEngine,
                                                                    open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    for marker in (*REQUIRED_MARKERS[ROUTE], *GENERAL_MARKERS, "settings.tabs", "settings.tab.general",
                   "settings.tab.effects", "settings.tab.keys", "settings.tab.voice_chat", "settings.about.logo",
                   *(f"settings.about.{field}" for field in ABOUT_FIELDS)):
        await user.should_see(marker=marker)
    for marker in (*EFFECTS_MARKERS, "settings.keys", "settings.key.gen.key", "settings.theme"):
        await user.should_not_see(marker=marker)
    assert marked(user, "settings.theme") == []                          # deferred this phase (§1.2)
    await user.should_not_see("Theme")
    with user.scope(marker="settings.about"):
        for text in ("Window", "Runtime", "Theme"):
            await user.should_not_see(text)
    assert the(user, "settings.tab.general").props["aria-selected"] == "true"
    assert the(user, "shell.title").text == "Settings"
    for tab in ("effects", "keys", "general"):
        user.find(marker=f"settings.tab.{tab}").click()
    assert binding.active_links == []                                    # every binding is passive (R9)


async def test_the_general_tab_has_the_mockup_copy_and_the_stored_values(user: User, core: Core, fake: FakeEngine,
                                                                         open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    for text in ("General", "Output Folder", "Run Models On", "Default Model for New Voices", GENERAL_FOOT, "About"):
        await user.should_see(text)
    browse = the(user, "settings.output")
    assert (browse.label, browse.tag, "disabled" in browse.props) == ("Browse…", "button", False)
    autoplay, offline = the(user, "settings.autoplay"), the(user, "settings.offline")
    assert (texts_in(autoplay), autoplay.props["aria-pressed"]) == (
        ["Autoplay New Takes", "Plays each take as it arrives"], "true")
    assert the(user, "settings.stop_drops_queue").props["aria-pressed"] == "true"
    assert (texts_in(offline), offline.props["aria-pressed"]) == (
        ["Offline Mode", "No network checks when a model loads"], "true")
    device = the(user, "settings.device")
    assert (device.props["role"], device.props["aria-label"]) == ("group", "Run models on")
    assert segments(device) == [("cuda", "GPU · CUDA"), ("cpu", "CPU")]
    assert [the(user, f"settings.device.{key}").props["aria-pressed"] for key in ("cuda", "cpu")] == ["true", "false"]
    assert the(user, "settings.device_note").text == "Falls back to CPU without CUDA"
    models = the(user, "settings.default_backend")
    assert models.props["aria-label"] == "Default model for new voices"
    assert segments(models) == [(MTL, "Multilingual"), (STD, "Standard"), (TUR, "Turbo")]
    assert [the(user, f"settings.default_backend.{b}").props["aria-pressed"] for b in (MTL, STD, TUR)] == [
        "true", "false", "false"]
    for marker in ("settings.device", "settings.default_backend"):
        assert "vt-set-segs" in classes(the(user, marker))


async def test_the_output_folder_is_the_absolute_path_and_browse_saves_a_new_one(
        user: User, core: Core, fake: FakeEngine, open_route: Any, run: launch.Launch,
        monkeypatch: pytest.MonkeyPatch) -> None:
    picked = core.paths.root / "outputs" / "mine"
    kit = FakeKit(folder_result=picked)
    monkeypatch.setattr(run, "kit", kit)                                 # the KitPort of the next page load
    shell = await opened(user, core, fake, open_route)
    default = str(core.paths.root / "outputs" / "takes")
    path = the(user, "settings.output.path")
    assert (path.text, path.props["title"]) == (default, default)        # E:\Voice Tool\outputs\takes on the PC
    user.find(marker="settings.output").click()
    await until(lambda: core.settings.get("core", "takes_dir") == "outputs/mine", "the new Output Folder")
    await settle(core, fake)
    assert kit.called("pick_folder") == [{"title": "Output Folder", "start_dir": core.paths.root / "outputs" / "takes"}]
    assert (path.text, path.props["title"]) == (str(picked), str(picked))
    assert toast_of(shell) == (f"New takes will be saved in {picked}", False)
    assert texts_in(the(user, "settings.about.takes")) == ["Takes", "outputs/mine"]   # the relative form (§7.3)
    assert store_of(core)["core"]["takes_dir"] == "outputs/mine" and picked.is_dir()
    assert "disabled" not in the(user, "settings.output").props


async def test_a_folder_that_cannot_be_written_is_refused(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                          run: launch.Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = core.paths.root / "a file"
    blocker.write_text("not a folder", encoding="utf-8")
    picked = blocker / "takes"
    monkeypatch.setattr(run, "kit", FakeKit(folder_result=picked))
    shell = await opened(user, core, fake, open_route)
    user.find(marker="settings.output").click()
    await until(lambda: toast_of(shell) is not None, "the refusal")
    text, bad = toast_of(shell) or ("", False)
    assert bad and text.startswith(f"Can't write to {picked}: ")
    assert "is-bad" in classes(the(user, "shell.toast"))
    assert core.settings.get("core", "takes_dir") == "outputs/takes"
    assert the(user, "settings.output.path").text == str(core.paths.root / "outputs" / "takes")


async def test_browse_needs_the_app_window(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                           run: launch.Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    kit = FakeKit(folder_result=core.paths.root / "never")
    monkeypatch.setattr(run, "kit", kit)
    monkeypatch.setattr(run, "flags", dataclasses.replace(run.flags, browser=True))
    await opened(user, core, fake, open_route)
    browse = the(user, "settings.output")
    assert (browse.props.get("disabled"), browse.props.get("title")) == (True, "Needs the app window")
    user.find(marker="settings.output").click()
    await settle(core, fake)
    assert kit.calls == [] and core.settings.get("core", "takes_dir") == "outputs/takes"


async def test_the_general_rows_save_to_ui_settings_json(user: User, core: Core, fake: FakeEngine,
                                                         open_route: Any) -> None:
    shell = await opened(user, core, fake, open_route)
    forge = ForgeStub()
    shell.ctx.behaviours.provide("forge", forge)
    fake.reset_records()
    user.find(marker="settings.autoplay").click()
    user.find(marker="settings.stop_drops_queue").click()
    user.find(marker="settings.device.cpu").click()
    assert toast_of(shell) == ("CPU applies the next time a model loads", False)
    user.find(marker="settings.default_backend.chatterbox-turbo").click()
    user.find(marker="settings.offline").click()
    assert toast_of(shell) == ("Offline Mode off · applies after a restart", False)
    await settle(core, fake)
    assert the(user, "settings.autoplay").props["aria-pressed"] == "false"
    assert [the(user, f"settings.device.{key}").props["aria-pressed"] for key in ("cuda", "cpu")] == ["false", "true"]
    assert the(user, "settings.device_note").text == "Much slower than the GPU"
    assert the(user, "settings.default_backend.chatterbox-turbo").props["aria-pressed"] == "true"
    assert the(user, "settings.offline").props["aria-pressed"] == "false"
    assert forge.backends == [TUR]                                       # an idle Forge draft follows (M L3240)
    assert [call.args for call in fake.calls if call.name == "set_preferred_device"] == [("cpu",)]
    assert fake.loads == []
    data = store_of(core)
    assert (data["version"], data["core"]["autoplay"], data["core"]["device"], data["core"]["default_backend"],
            data["core"]["offline"]) == (1, False, "cpu", TUR, False)
    fresh = SettingsStore(core.paths.settings)                           # what the next launch reads back
    assert the(user, "settings.stop_drops_queue").props["aria-pressed"] == "false"
    assert fresh.get("core", "stop_drops_queue") is False
    assert (fresh.get("core", "autoplay"), fresh.get("core", "device"), fresh.get("core", "default_backend"),
            fresh.get("core", "offline")) == (False, "cpu", TUR, False)
    assert SettingsStore.read_early(core.paths.settings)["core"]["offline"] is False


# ---- the Effects tab ----------------------------------------------------------------------------------

@pytest.mark.scenario("settings")
async def test_the_settings_board_opens_on_effects(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    assert the(user, "settings.tab.effects").props["aria-selected"] == "true"
    for marker in (*EFFECTS_MARKERS, "settings.about"):
        await user.should_see(marker=marker)
    await user.should_not_see(marker="settings.core")
    for text in ("Effects", "Background Effects", "Embers, traces and sparks behind the window", "Effect Speed",
                 "Work Glow", "Embers brighten behind the cards while you load a model, generate a take or forge a voice",
                 "Reduce Motion", "Follow Windows",
                 "Always", "Saber Art", "Show", "Hide", "On the Forge page and the first run", EFFECTS_FOOT):
        await user.should_see(text)
    slider = the(user, "settings.fx_speed")
    assert (slider._input.props["min"], slider._input.props["max"], slider._input.props["step"]) == ("25", "200", "5")
    assert (slider._input.props["serverValue"], slider._input.props["aria-label"]) == (100, "Effect speed")
    assert the(user, "settings.fx_speed_text").text == "100%"
    assert the(user, "settings.reduce_motion").props["aria-label"] == "Reduce motion"
    assert the(user, "settings.saber_art").props["aria-label"] == "Saber art"
    assert [the(user, f"settings.reduce_motion.{key}").props["aria-pressed"] for key in ("system", "always")] == [
        "true", "false"]


@pytest.mark.scenario("settings")
async def test_the_effects_reach_the_field_live(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    shell = await opened(user, core, fake, open_route)
    assert shell.field is not None and shell.field.state["speed"] == 1.0
    user.find(marker="settings.fx_speed").trigger("input", 150)          # while the range moves
    assert shell.field.state["speed"] == 1.5 and the(user, "settings.fx_speed_text").text == "150%"
    await settle(core, fake)
    assert saved_speed(core) is None                                     # live, not saved (§6.12)
    user.find(marker="settings.fx_speed").trigger("change", 150)         # the release
    await until(lambda: saved_speed(core) == 150, "the released speed in ui_settings.json")
    user.find(marker="settings.work_glow").click()
    assert shell.field.state["glow"] is False
    assert the(user, "settings.work_glow").props["aria-pressed"] == "false"
    user.find(marker="settings.fx_on").click()                           # Background Effects off: no field
    assert shell.field is None
    user.find(marker="settings.fx_on").click()
    assert shell.field is not None and (shell.field.state["speed"], shell.field.state["glow"]) == (1.5, False)
    data = store_of(core)
    assert data["theme.emiya"] == {"fx_on": True, "fx_speed": 150, "work_glow": False}
    assert fake.loads == []


@pytest.mark.scenario("settings")
async def test_reduce_motion_always_stills_the_field_the_body_and_every_logo(user: User, core: Core,
                                                                             fake: FakeEngine,
                                                                             open_route: Any) -> None:
    shell = await opened(user, core, fake, open_route)
    logo = the(user, "settings.about.logo")
    assert logo.src.startswith(GIF)
    user.find(marker="settings.reduce_motion.always").click()
    assert the(user, "settings.reduce_motion.always").props["aria-pressed"] == "true"
    assert shell.field is not None and shell.field.state["reduce"] is True
    body = [element for element in user.current_layout.descendants()
            if isinstance(element, QueryElement) and element.props["selector"] == "body"]
    assert len(body) == 1 and "reduce-motion" in body[0].props["classes"]
    assert [found.src.split("?")[0] for found in user.find(kind=Logo).elements] == [STILL, STILL]
    user.find(marker="settings.reduce_motion.system").click()
    assert shell.field.state["reduce"] == "system" and logo.src.startswith(GIF)
    assert store_of(core)["core"]["reduce_motion"] == "system"


@pytest.mark.scenario("settings")
async def test_saber_art_hide_takes_the_art_off_forge(user: User, core: Core, fake: FakeEngine,
                                                      open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="settings.saber_art.hide").click()
    assert the(user, "settings.saber_art.hide").props["aria-pressed"] == "true"
    assert core.settings.get("theme.emiya", "saber_art") == "hide"
    user.find(marker="nav.forge").click()
    await user.should_see(marker="page.forge")
    await user.should_not_see(marker="shell.art")


# ---- the Keys tab -------------------------------------------------------------------------------------

async def test_the_keys_tab_lists_the_commands(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="settings.tab.keys").click()
    await user.should_see(KEYS_FOOT)
    with user.scope(marker="settings.keys"):
        for text in ("Command", "Key", "Alternate", *LABELS):
            await user.should_see(text)
    assert [the(user, f"settings.key.{cid}.key").text for cid in IDS] == list(KEYS)
    assert [the(user, f"settings.key.{cid}.alt").text for cid in IDS] == ["—"] * len(IDS)
    assert all("is-empty" in classes(the(user, f"settings.key.{cid}.alt")) for cid in IDS)
    gen, gen_alt = the(user, "settings.key.gen.key"), the(user, "settings.key.gen.alt")
    assert (gen.props["aria-label"], gen_alt.props["aria-label"]) == (
        "Generate key: Ctrl+Enter", "Generate alternate key: none")
    assert (gen.tag, gen.props["type"], "vt-key" in classes(gen)) == ("button", "button", True)
    await user.should_not_see(marker="settings.core")


async def test_a_key_slot_captures_a_combination_while_the_dispatcher_waits(user: User, core: Core,
                                                                            fake: FakeEngine,
                                                                            open_route: Any) -> None:
    shell = await opened(user, core, fake, open_route)
    user.find(marker="settings.tab.keys").click()
    dispatcher = the(user, "shell.keys")
    slot = the(user, "settings.key.gen.alt")
    user.find(marker="settings.key.gen.alt").click()
    assert (slot.text, "listen" in classes(slot), dispatcher.props["suspended"]) == ("Press a Key…", True, True)
    user.find(marker="settings.key.gen.alt").trigger("keydown", {"key": "Shift", "shift": True})
    assert slot.text == "Press a Key…"                                   # a modifier on its own keeps listening
    user.find(marker="settings.key.gen.alt").trigger("keydown", {"key": "g", "ctrl": True})
    assert the(user, "settings.key.gen.alt") is slot                     # updated in place: the focus stays
    assert (slot.text, "listen" in classes(slot), slot.props["aria-label"]) == (
        "Ctrl+G", False, "Generate alternate key: Ctrl+G")
    assert toast_of(shell) == ("Bound Ctrl+G", False)
    assert dispatcher.props["suspended"] is False and dispatcher.props["combos"]["Ctrl+G"] == "gen"
    assert store_of(core)["core"]["keys"]["gen"] == ["Ctrl+Enter", "Ctrl+G"]


@pytest.mark.parametrize(("marker", "event", "toast"), [
    ("settings.key.search.alt", {"key": "Enter", "ctrl": True}, ("Ctrl+Enter is already used by Generate", True)),
    ("settings.key.gen.alt", {"key": "Enter", "ctrl": True}, ("Ctrl+Enter is already used by Generate", True)),
    ("settings.key.ab.alt", {"key": "F4", "alt": True}, ("Alt+F4 is reserved by Windows", True)),
    ("settings.key.ab.alt", {"key": "=", "ctrl": True}, ("Ctrl+= is the window's zoom", True)),
    ("settings.key.play.alt", {"key": "Escape"}, None),
])
async def test_a_refused_or_cancelled_capture_keeps_the_binding(user: User, core: Core, fake: FakeEngine,
                                                                open_route: Any, marker: str, event: dict[str, Any],
                                                                toast: tuple[str, bool] | None) -> None:
    shell = await opened(user, core, fake, open_route)
    user.find(marker="settings.tab.keys").click()
    user.find(marker=marker).click()
    user.find(marker=marker).trigger("keydown", event)
    slot = the(user, marker)
    assert (slot.text, "listen" in classes(slot)) == ("—", False)
    assert toast_of(shell) == toast
    if toast is not None:
        assert "is-bad" in classes(the(user, "shell.toast"))
    assert the(user, "shell.keys").props["suspended"] is False


async def test_backspace_clears_a_binding_and_its_hint(user: User, core: Core, fake: FakeEngine,
                                                       open_route: Any) -> None:
    shell = await opened(user, core, fake, open_route)
    await user.should_see(marker="shell.search.badge")
    user.find(marker="settings.tab.keys").click()
    user.find(marker="settings.key.search.key").click()
    user.find(marker="settings.key.search.key").trigger("keydown", {"key": "Backspace"})
    slot = the(user, "settings.key.search.key")
    assert (slot.text, slot.props["aria-label"], "is-empty" in classes(slot)) == ("—", "Search key: none", True)
    assert toast_of(shell) == ("Cleared the binding", False)
    assert "Ctrl+K" not in the(user, "shell.keys").props["combos"]
    await user.should_not_see(marker="shell.search.badge")               # the rail hint follows the binding
    assert store_of(core)["core"]["keys"]["search"] == ["", ""]


async def test_blur_and_a_tab_change_stop_listening(user: User, core: Core, fake: FakeEngine,
                                                    open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="settings.tab.keys").click()
    user.find(marker="settings.key.gen.key").click()
    user.find(marker="settings.key.gen.key").trigger("blur")
    assert the(user, "settings.key.gen.key").text == "Ctrl+Enter"
    assert the(user, "shell.keys").props["suspended"] is False
    user.find(marker="settings.key.next.alt").click()
    assert the(user, "shell.keys").props["suspended"] is True
    user.find(marker="settings.tab.general").click()                     # M L2711
    await user.should_see(marker="settings.core")
    assert the(user, "shell.keys").props["suspended"] is False
    user.find(marker="settings.tab.keys").click()
    assert (the(user, "settings.key.next.alt").text, "listen" in classes(the(user, "settings.key.next.alt"))) == (
        "—", False)


async def test_leaving_the_page_stops_listening(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="settings.tab.keys").click()
    user.find(marker="settings.key.save.alt").click()
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    assert the(user, "shell.keys").props["suspended"] is False


# ---- the About card -----------------------------------------------------------------------------------

def about_rows(user: User) -> list[Box]:
    card = the(user, "settings.about")
    return [element for element in card.descendants() if isinstance(element, Box) and "vt-about-row" in element.classes]


async def test_the_about_card_shows_the_facts_and_the_detected_gpu(user: User, core: Core, fake: FakeEngine,
                                                                   open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    for text in ("Emiya Voice", "Local voice cloning in the Emiya family"):
        with user.scope(marker="settings.about"):
            await user.should_see(text)
    found = about_rows(user)
    assert [texts_in(row) for row in found] == [
        ["Engine", engine_text()], ["GPU", "RTX 4070 Ti · 12.0 GB · detected"], ["Models in VRAM", "One at a time"],
        ["Voice registry", "cache/registry.json"], ["Voice cache", "cache/‹model›/‹voice›.pt"],
        ["Reference clips", "voices/reference"], ["Takes", "outputs/takes"]]
    assert [row.props.get("title") for row in found] == [
        None, GPU_TIP, None, ABOUT_TIPS["registry"], ABOUT_TIPS["cache"], ABOUT_TIPS["clips"], None]
    assert engine_text().startswith("chatterbox-tts ")
    logo = the(user, "settings.about.logo")
    assert (logo.style["width"], logo.style["height"]) == ("40px", "40px")
    assert dict(logo.img.style) == {"left": "-6px", "top": "-4px", "width": "53px", "height": "53px"}
    assert logo.src.startswith(GIF) and logo.img.props["alt"] == ""


@pytest.mark.scenario("cpu")
async def test_without_cuda_about_and_the_device_note_say_so(user: User, core: Core, fake: FakeEngine,
                                                             open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    gpu = the(user, "settings.about.gpu")
    assert (texts_in(gpu), gpu.props.get("title")) == (["GPU", "No CUDA GPU · models run on the CPU"], None)
    assert the(user, "settings.device_note").text == "No CUDA GPU detected · models run on the CPU"


async def test_a_failed_detection_shows_its_reason(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    fake.fail_next("device_info", RuntimeError("CUDA driver version is insufficient"))
    await opened(user, core, fake, open_route)
    await until(lambda: bool(core.state.device.error), "the failed reading")
    await settle(core, fake)
    gpu = the(user, "settings.about.gpu")
    assert texts_in(gpu) == ["GPU", f"Not detected · {core.state.device.error}"]
    assert "CUDA driver version is insufficient" in core.state.device.error and gpu.props.get("title") is None


@pytest.mark.scenario("firstrun")
async def test_the_page_on_the_first_run(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    assert the(user, "settings.output.path").text == str(core.paths.root / "outputs" / "takes")
    assert texts_in(the(user, "settings.about.gpu")) == ["GPU", "RTX 4070 Ti · 12.0 GB · detected"]
    assert [texts_in(row)[0] for row in about_rows(user)] == list(ABOUT_LABELS)


# ---- the stylesheet -----------------------------------------------------------------------------------

STATIC = Path(__file__).resolve().parents[2] / "ui" / "themes" / "emiya" / "static"
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def _css(path: Path) -> str:
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_the_page_stylesheet_uses_only_the_theme_tokens() -> None:
    """settings.css has no colour value of its own, every var() is a token of emiya.css, and every rule is
    scoped to the page root."""
    text = _css(STATIC / "pages" / "settings.css")
    assert COLOUR.search(text) is None
    declared = set(re.findall(r"(--[\w-]+)\s*:", _css(STATIC / "emiya.css")))
    assert set(re.findall(r"var\((--[\w-]+)", text)) - declared == set()
    selectors = [chunk.split("{")[0].strip() for chunk in text.split("}") if "{" in chunk]
    assert selectors and all(part.strip().startswith(".vt-settings") for s in selectors for part in s.split(","))
    assert text.count("{") == text.count("}") and "@import" not in text and "url(" not in text


# ---- the page's wiring rows ---------------------------------------------------------------------------

CONTROLLERS: dict[str, type] = {"settings": SettingsController, "commands": CommandRegistry}


def _spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    owner, name = intent.split(".")
    cls = CONTROLLERS[owner]
    original = getattr(cls, name)
    if inspect.iscoroutinefunction(original):
        async def spy_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy_async)
    else:
        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy)


async def _act(user: User, row: Wire) -> None:
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    else:
        raise AssertionError(f"Settings has no {row.act!r} rows")


def test_every_intent_has_a_row_or_an_opt_out() -> None:
    from tests.pages.wiring.settings import CALL_WIRING, OPT_OUT

    intents = {name for name, member in vars(SettingsController).items()
               if inspect.isfunction(member) and not name.startswith("_") and name not in ("refresh", "close")}
    own = {row.intent for row in WIRING if row.intent.startswith("settings.")}
    call_controls = {row.intent for row in CALL_WIRING}
    assert {f"settings.{name}" for name in intents} == own | set(OPT_OUT) | call_controls


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, open_route: Any,
                           monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)
    shell = await opened(user, core, fake, open_route)
    shell.ctx.behaviours.provide("forge", ForgeStub())                   # Default Model's re-pick, without P3
    for marker in row.before:
        user.find(marker=marker).click()
    fake.reset_records()
    calls.clear()
    await _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    assert fake.loads == list(row.loads) == []
