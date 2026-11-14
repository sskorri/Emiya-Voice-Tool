"""The Emiya shell under NiceGUI's user fixture (§8.5, T1): gui.py on a FakeEngine scenario in tmp_path.

Covered: the shell and frameless markers; navigation to every route and the header titles, also for a direct
load of /takes and /settings (the router's initial path); the drag regions; the four FX anchors and the forge
field outside ui.sub_pages; the Logo instances under Reduce Motion; the chip, dock, VRAM block, popover, Log and
toast per scenario; Close (quits when idle, arms when busy); the search box; the player and the key
dispatchers; native drops; the art layer; the shell's wiring rows (tests\\pages\\wiring\\shell.py); the widgets
the pages build with; and that static\\emiya.css keeps every colour value in its token block.
Pages are T0 placeholders until P1-P6 land; where a page adds a Logo, the test counts it once it exists.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import ui
from nicegui.element import Element
from nicegui.elements.query import QueryElement
from nicegui.elements.sub_pages import SubPages
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from ui import launch
from ui.behaviours.commands import CommandRegistry
from ui.behaviours.models import ModelsController
from ui.behaviours.playback import PlaybackController
from ui.behaviours.search import SearchController
from ui.behaviours.status import CLOSE_ARMED, CLOSE_LABEL, MINIMIZE_ONLY_NATIVE, NOT_LOADED_YET, QUIT_BUSY
from ui.behaviours.status import StatusController
from ui.contract.markers import FRAMELESS_MARKERS, SHELL_MARKERS
from ui.contract.plans import ActionState, AfterGenerate, GenerateRequest
from ui.contract.routes import PAGE_ID, ROUTES, TITLES
from ui.contract.viewmodels import LangItemVM
from ui.core import Core, catalog
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.kit.audio import Player
from ui.kit.keys import KeyDispatcher
from ui.kit.window import DRAG_CLASS
from ui.themes.emiya.fx.forge_field import ANCHORS, EmiyaForgeField, anchor_marker
from ui.themes.emiya.shell import DROP_ELSEWHERE, SETTINGS_TABS, TAKES_TABS, Shell, ShellRoot
from ui.themes.emiya.widgets import (
    Chip,
    DotIndicator,
    DotSwitch,
    EmiyaButton,
    GoldSlider,
    IconButton,
    KeyButton,
    ListBox,
    ListHost,
    Logo,
    Meter,
    PickButton,
    Row,
    ScriptBox,
    SectionLabel,
    Segmented,
    TextField,
    TextSpan,
)

from tests.pages.wiring import Wire
from tests.pages.wiring.shell import WIRING

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "ui" / "themes" / "emiya" / "static" / "emiya.css"
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
GIF, STILL = "img/logo.gif", "img/logo-still.png"


# ---- helpers ------------------------------------------------------------------------------------------

def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def the(user: User, marker: str) -> Any:
    """The one element with this marker that is shown."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def shell_of(user: User) -> Shell:
    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell


def classes(element: Element) -> set[str]:
    return set(element.classes)


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds; the FakeEngine works on worker threads."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks start their jobs, then wait for every job that is not held by the scenario.

    A held job (the scenario's hold) is waited for until it reached its hold, so what it records first
    (FakeEngine.loads) is in before a test resets the records.
    """
    for _ in range(5):
        await asyncio.sleep(0)
    held = bool(fake.scenario.holds)
    if held:
        await until(lambda: not core.state.job.kind or any(hold.entered.is_set() for hold in fake.holds),
                    "the scenario's held call")
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if held else [])
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine, route: str = "/") -> Shell:
    """Open route (directly), let the scenario's replay finish, return the page's shell."""
    await user.open(route)
    await settle(core, fake)
    return shell_of(user)


@pytest.fixture
def quits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """kit.quit() lands here instead of ui.kit.window.quit_app."""
    seen: list[str] = []
    monkeypatch.setattr("ui.kit.window.quit_app", lambda: seen.append("quit"))
    return seen


@pytest.fixture
def run(user: User) -> launch.Launch:
    assert launch.LAST_LAUNCH is not None
    return launch.LAST_LAUNCH


# ---- markers, navigation, header ----------------------------------------------------------------------

async def test_the_shell_renders_its_markers(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for marker in (*SHELL_MARKERS, *FRAMELESS_MARKERS, "shell", "shell.brand", "shell.logo", "shell.search",
                   "shell.search.badge", "shell.header", "shell.title", "shell.log", "shell.dock", "shell.vram",
                   "shell.vram.meter", "shell.dock_status", "dock.free", "shell.content", "shell.player",
                   "shell.keys", "shell.keys.escape", *(f"dock.row.{backend}" for backend in (MTL, STD, TUR))):
        await user.should_see(marker=marker)
    for hidden in ("confirm.ask", "shell.log_panel", "shell.toast", "dock.retry", "dock.load_default",
                   "takes.tabs", "settings.tabs", "shell.art"):
        await user.should_not_see(marker=hidden)
    assert the(user, "shell.search").props["placeholder"] == "Search"
    assert the(user, "shell.search").props["aria-label"] == "Search voices, takes and actions"


async def test_nav_reaches_every_route_and_titles_the_page(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for route in ROUTES:
        page = PAGE_ID[route]
        user.find(marker=f"nav.{page}").click()
        await user.should_see(marker=f"page.{page}")
        assert the(user, "shell.title").text == TITLES[route]
        for other in ROUTES:
            button = the(user, f"nav.{PAGE_ID[other]}")
            assert ("is-current" in classes(button)) is (other == route)
            assert button.props.get("aria-current") == ("page" if other == route else None)
    assert [the(user, f"nav.{page}").text for page in PAGE_ID.values()] == list(TITLES.values())


@pytest.mark.parametrize(("route", "tabs", "hidden"), [
    ("/takes", ("takes", TAKES_TABS, "session"), "settings.tabs"),
    ("/settings", ("settings", SETTINGS_TABS, "general"), "takes.tabs"),
])
async def test_a_direct_load_gets_its_title_and_tabs(user: User, core: Core, fake: FakeEngine, route: str,
                                                     tabs: tuple[str, tuple[tuple[str, str], ...], str],
                                                     hidden: str) -> None:
    page, labels, current = tabs
    await opened(user, core, fake, route)
    await user.should_see(marker=f"page.{page}")
    assert the(user, "shell.title").text == TITLES[route]
    group = the(user, f"{page}.tabs")
    assert (group.props["role"], group.props["aria-label"]) == ("tablist", TITLES[route])
    for key, label in labels:
        tab = the(user, f"{page}.tab.{key}")
        assert tab.props["role"] == "tab" and tab._face.text == label
        assert tab.props["aria-selected"] == ("true" if key == current else "false")
    await user.should_not_see(marker=hidden)
    assert the(user, f"nav.{page}").props["aria-current"] == "page"
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    await user.should_not_see(marker=f"{page}.tabs")
    assert the(user, "shell.title").text == "Speak"


@pytest.mark.parametrize(("route", "page", "key"), [("/takes", "takes", "starred"), ("/settings", "settings", "keys")])
async def test_a_tab_reaches_its_page_controller(user: User, core: Core, fake: FakeEngine, route: str, page: str,
                                                 key: str) -> None:
    shell = await opened(user, core, fake, route)
    controller = getattr(shell.ctx.behaviours, page)()
    group = the(user, f"{page}.tabs")
    assert group._on_change == controller.set_tab              # P4 and P6 test what set_tab does
    user.find(marker=f"{page}.tab.{key}").click()
    controller.vm.tab = key                                      # what the controller does with it
    assert the(user, f"{page}.tab.{key}").props["aria-selected"] == "true"


async def test_drag_regions_are_the_header_its_title_and_the_brand_row(user: User, core: Core,
                                                                      fake: FakeEngine) -> None:
    await opened(user, core, fake)
    dragging = [element for element in user.current_layout.descendants() if DRAG_CLASS in element.classes]
    assert sorted(marker for element in dragging for marker in element._markers) == [
        "shell.brand", "shell.header", "shell.title"]
    assert sorted(element.tag for element in dragging) == ["div", "h1", "header"]


# ---- the FX layer -------------------------------------------------------------------------------------

async def test_the_four_anchors_and_the_field_stay_outside_the_pages(user: User, core: Core,
                                                                     fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    for route in ROUTES:
        if route != "/":
            user.find(marker=f"nav.{PAGE_ID[route]}").click()
            await user.should_see(marker=f"page.{PAGE_ID[route]}")
        pages = [element for element in user.current_layout.descendants() if isinstance(element, SubPages)]
        assert len(pages) == 1
        inside = set(pages[0].descendants())
        for name, (x, y) in ANCHORS.items():
            anchor = the(user, anchor_marker(name))
            assert anchor not in inside and anchor.props["aria-hidden"] == "true"
            assert (anchor.style["left"], anchor.style["top"]) == (f"{x}px", f"{y}px")
        assert shell.field is not None and shell.field not in inside
    content = the(user, "shell.content")
    assert "emiya-field" in classes(content)
    assert shell.field.props["options"] == {"uid": "forge-field", "field": ".emiya-field", "telemetry": False}


@pytest.mark.parametrize(("heat", "focus"), [
    pytest.param("idle", None, marks=pytest.mark.scenario("speak"), id="speak"),
    pytest.param("cold", None, marks=pytest.mark.scenario("firstrun"), id="firstrun"),
    pytest.param("working", "switch", marks=pytest.mark.scenario("loading"), id="loading"),
])
async def test_the_field_follows_the_core(user: User, core: Core, fake: FakeEngine, heat: str,
                                          focus: str | None) -> None:
    shell = await opened(user, core, fake)
    if fake.scenario.holds:
        await until(lambda: core.state.job.phase == "load", "the held load")
        await settle(core, fake)
    assert shell.field is not None
    state = shell.field.state
    assert (state["heat"], state["focus"], state["glow"], state["reduce"], state["paused"]) == (
        heat, focus, True, "system", False)
    assert state["fps_cap"] == 60 and state["speed"] == 1.0


async def test_the_effects_prefs_reach_the_field_live(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    prefs = shell.ctx.prefs
    prefs.set("fx_speed", 150)
    assert shell.field is not None and shell.field.state["speed"] == 1.5
    prefs.set("work_glow", False)
    assert shell.field.state["glow"] is False
    prefs.set("fx_on", False)
    assert shell.field is None
    assert not [e for e in user.current_layout.descendants() if isinstance(e, EmiyaForgeField)]
    prefs.set("fx_on", True)
    fields = [e for e in user.current_layout.descendants() if isinstance(e, EmiyaForgeField)]
    assert fields == [shell.field] and shell.field.state["speed"] == 1.5 and shell.field.state["glow"] is False
    core.state.window.visible = False
    core.events.notify()
    await settle(core, fake)
    assert shell.field.state["paused"] is True


async def test_a_forge_start_grows_the_blade(user: User, core: Core, fake: FakeEngine,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    blades: list[str] = []
    monkeypatch.setattr(EmiyaForgeField, "forge_blade", lambda self, anchor="forge-blade": blades.append(anchor))
    await opened(user, core, fake)
    core.events.forge_started.emit("rin")
    assert blades == ["forge-blade"]


async def test_reduce_motion_always_stills_the_logo_the_field_and_the_body(user: User, core: Core,
                                                                          fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    logo = the(user, "shell.logo")
    assert logo.src.startswith(f"/theme/emiya/{GIF}?v=")
    core.settings.set("core", "reduce_motion", "always")
    assert logo.src.startswith(f"/theme/emiya/{STILL}?v=")
    assert shell.field is not None and shell.field.state["reduce"] is True
    body = [e for e in user.current_layout.descendants() if isinstance(e, QueryElement) and e.props["selector"] == "body"]
    assert len(body) == 1 and "reduce-motion" in body[0].props["classes"]
    core.settings.set("core", "reduce_motion", "system")
    assert logo.src.startswith(f"/theme/emiya/{GIF}?v=") and shell.field.state["reduce"] == "system"
    assert "reduce-motion" not in body[0].props["classes"]


def _page_built(page: str) -> bool:
    return importlib.util.find_spec(f"ui.themes.emiya.pages.{page}") is not None


@pytest.mark.scenario("firstrun")
async def test_every_logo_instance_shows_the_still_frame_under_always(user: User, core: Core,
                                                                      fake: FakeEngine) -> None:
    """The rail's Logo, Speak's empty state (firstrun) and the About card (Settings) once P1 and P6 build them."""
    core.settings.set("core", "reduce_motion", "always")
    for route, page, extra in (("/", "speak", 1), ("/settings", "settings", 1)):
        await opened(user, core, fake, route)
        logos = list(user.find(kind=Logo).elements)
        assert len(logos) == 1 + (extra if _page_built(page) else 0)
        for logo in logos:
            assert logo.src.startswith(f"/theme/emiya/{STILL}")
            sources = [e for e in logo.descendants() if e.tag == "source"]
            assert len(sources) == 1 and sources[0].props["media"] == "(prefers-reduced-motion: reduce)"
            assert sources[0].props["srcset"].startswith(f"/theme/emiya/{STILL}")
            assert logo.img.props["alt"] == ""


async def test_the_rail_logo_has_the_mockup_geometry(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    logo = the(user, "shell.logo")
    assert (logo.style["width"], logo.style["height"]) == ("40px", "40px")
    assert dict(logo.img.style) == {"left": "-2px", "top": "-0.5px", "width": "45px", "height": "45px"}


# ---- the art layer ------------------------------------------------------------------------------------

async def test_the_art_shows_on_forge_and_follows_saber_art(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    await user.should_not_see(marker="shell.art")              # five voices: no art on Speak
    user.find(marker="nav.forge").click()
    await user.should_see(marker="page.forge")
    art = the(user, "shell.art")
    assert "is-forge" in classes(art) and art.props["src"].startswith("/theme/emiya/img/saber-art.png")
    shell.ctx.prefs.set("saber_art", "hide")
    await user.should_not_see(marker="shell.art")


@pytest.mark.scenario("firstrun")
async def test_an_empty_library_shows_the_art_on_speak(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    art = the(user, "shell.art")
    assert "is-speak" in classes(art) and "is-forge" not in classes(art)


# ---- chip, dock and VRAM per scenario -----------------------------------------------------------------

@pytest.mark.parametrize(("text", "tone", "dot"), [
    pytest.param("Multilingual ready · 3.4 GB", "base", "gold", marks=pytest.mark.scenario("speak"), id="speak"),
    pytest.param("No model loaded", "base", "hollow", marks=pytest.mark.scenario("firstrun"), id="firstrun"),
    pytest.param("Turbo failed to load", "red", "red", marks=pytest.mark.scenario("failure"), id="failure"),
    pytest.param("Loading Turbo · 1 s of ~9 s", "gold", "gold", marks=pytest.mark.scenario("loading"), id="loading"),
    pytest.param("Unloading Multilingual · freeing 3.4 GB", "gold", "gold", marks=pytest.mark.scenario("freeing"),
                 id="freeing"),
    pytest.param("Multilingual ready · CPU", "base", "gold", marks=pytest.mark.scenario("cpu"), id="cpu"),
])
async def test_the_status_chip(user: User, core: Core, fake: FakeEngine, text: str, tone: str, dot: str) -> None:
    await opened(user, core, fake)
    if fake.scenario.name == "loading":
        await until(lambda: core.state.job.phase == "load", "the held load")
        await settle(core, fake)
    chip = the(user, "status")
    assert isinstance(chip, Chip) and chip.tag == "button"
    assert (chip.text, chip.tone, chip.dot) == (text, tone, dot)
    assert f"is-{tone}" in classes(chip) and f"is-{dot}" in classes(chip._dot)
    assert (chip.props["title"], chip.props["aria-live"]) == ("Open the Log", "polite")


def _rows(user: User) -> dict[str, Any]:
    return {backend: the(user, f"dock.row.{backend}") for backend in (MTL, STD, TUR)}


async def test_the_dock_and_vram_in_the_steady_state(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    rows = _rows(user)
    assert [(row.label, row.indicator.state, row.info.text, row.info.tone, row.enabled) for row in rows.values()] == [
        ("Multilingual", "resident", "3.4 GB", "gold-dim", True),
        ("Standard", "not_loaded", "~12 s", "faint", True),
        ("Turbo", "not_loaded", "~9 s", "faint", True),
    ]
    assert [row.props["aria-checked"] for row in rows.values()] == ["true", "false", "false"]
    assert [row.props["role"] for row in rows.values()] == ["radio"] * 3
    assert {backend: row.props["title"] for backend, row in rows.items()} == {b: catalog.tip(b) for b in rows}
    group = rows[MTL].parent_slot.parent
    assert (group.props["role"], group.props["aria-label"]) == ("radiogroup", "Model in VRAM")
    value = the(user, "shell.vram.value")
    assert (value.text, value.tone) == ("3.4 GB", "gold")
    assert the(user, "shell.vram.detail").text == "2.5 other · 6.1 free"
    tip = "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"
    assert the(user, "shell.vram").props["title"] == tip
    meter = the(user, "shell.vram.meter")
    assert meter.props["aria-label"] == tip and meter.props["role"] == "img"
    assert meter._app.style["width"] == "28.35%" and meter._other.style["width"].startswith("20.8")
    status = the(user, "shell.dock_status")
    assert (status.text, status.tone, status.props["role"]) == ("Ready", "muted", "status")
    free = the(user, "dock.free")
    assert (free.label, free.enabled, "disabled" in free.props) == ("Free VRAM", True, False)


@pytest.mark.scenario("firstrun")
async def test_the_dock_on_the_first_run(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    rows = _rows(user)
    assert all(row.info.text == "" and row.props["title"].endswith(NOT_LOADED_YET) for row in rows.values())
    assert [row.props["aria-checked"] for row in rows.values()] == ["false"] * 3
    assert the(user, "shell.dock_status").text == "No model loaded"
    value = the(user, "shell.vram.value")
    assert (value.text, value.tone) == ("0.0 GB", None)
    free = the(user, "dock.free")
    assert free.enabled is False and free.props["disabled"] is True
    assert free.props["title"] == "Nothing to free · no model loaded"


@pytest.mark.scenario("cpu")
async def test_without_cuda_the_meter_hides(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "shell.vram.value").text == "CPU"
    assert the(user, "shell.vram.detail").text == "No CUDA GPU · models run on the CPU"
    await user.should_not_see(marker="shell.vram.meter")
    assert _rows(user)[MTL].info.text == "CPU"


@pytest.mark.scenario("loading")
async def test_a_held_load_rings_its_row_and_disables_the_dock(user: User, core: Core, fake: FakeEngine,
                                                               clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    await until(lambda: core.state.job.phase == "load", "the held load")
    await settle(core, fake)
    rows = _rows(user)
    assert all(not row.enabled and row.props["disabled"] is True for row in rows.values())
    assert ["is-dim" in classes(row) for row in rows.values()] == [True, True, False]
    turbo = rows[TUR].indicator
    assert turbo.state == "loading" and turbo._ring.visible and not turbo._dot.visible
    clock.advance(4.35)
    await settle(core, fake)
    assert the(user, "status").text == "Loading Turbo · 4 s of ~9 s"
    assert the(user, "shell.dock_status").text == "Loading · ~5 s left"
    assert rows[TUR].info.text == "4 s" and turbo._ring.style["--ring"].endswith("deg")
    assert "is-spin" not in classes(turbo._ring)
    assert the(user, "dock.free").enabled is False


@pytest.mark.scenario("freeing")
async def test_free_vram_shows_the_freeing_row(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    rows = _rows(user)
    assert rows[MTL].indicator.state == "unloading" and "is-unloading" in classes(rows[MTL].indicator._dot)
    assert rows[MTL].info.text == "Freeing"
    assert the(user, "shell.dock_status").text == "Freeing 3.4 GB…"
    assert the(user, "dock.free").enabled is False


@pytest.mark.scenario("failure")
async def test_a_failed_load_offers_retry_and_the_default(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_not_see(marker="dock.free")
    retry, default = the(user, "dock.retry"), the(user, "dock.load_default")
    assert (retry.label, retry.props["aria-label"], retry.props["title"]) == (
        "Retry", "Retry loading Turbo", "Retry loading Turbo")
    assert default.label == "Load Multilingual" and "aria-label" not in default.props
    status = the(user, "shell.dock_status")
    assert (status.text, status.tone) == ("Load failed · see Log", "bad")
    turbo = _rows(user)[TUR]
    assert turbo.indicator.state == "failed" and (turbo.info.text, turbo.info.tone) == ("Failed", "bad")
    toast = the(user, "shell.toast")
    assert toast.text.startswith("Turbo failed to load: ") and "is-bad" in classes(toast)


@pytest.mark.scenario("other_backend")
async def test_a_fresh_load_flashes_and_sparks_its_row(user: User, core: Core, fake: FakeEngine) -> None:
    core.settings.set("core", "confirm_switches", False)
    await opened(user, core, fake)
    turbo = _rows(user)[TUR]
    assert not {"em-flash", "em-flash-b"} & classes(turbo)
    user.find(marker=f"dock.row.{TUR}").click()
    await settle(core, fake)
    assert fake.loads == [TUR] and turbo.props["aria-checked"] == "true"
    assert "em-flash" in classes(turbo)
    assert [e for e in turbo.indicator.descendants() if "vt-spark" in e.classes]
    assert the(user, "status").text == "Turbo ready · 3.1 GB"
    assert the(user, "shell.toast").text.startswith("Turbo ready · ")


# ---- the popover --------------------------------------------------------------------------------------

@pytest.mark.scenario("switch")
async def test_the_confirm_ask_from_the_dock(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    pop = the(user, "confirm.ask")
    assert (pop.props["role"], pop.props["aria-label"]) == ("dialog", "Switch to Turbo")
    assert {"vt-popover", "vt-pop-in", "vt-ask"} <= classes(pop)
    lines = [(line.text, line.tone) for line in pop.descendants() if "vt-ask-line" in line.classes and line.visible]
    assert lines == [("Unloads Multilingual · frees 3.4 GB", "text-soft"),
                     ("Loads Turbo · about 9 s (last 8.7 s) · 3.1 GB", "text-soft"),
                     (catalog.rules(TUR), "muted")]
    dont = the(user, "confirm.ask.dont_ask")
    assert dont.props["aria-pressed"] == "false" and "em-dot-off" in classes(dont._dot)
    dont_click = user.find(marker="confirm.ask.dont_ask")
    dont_click.click()
    assert dont.props["aria-pressed"] == "true" and "em-dot-off" not in classes(dont._dot)
    assert the(user, "confirm.ask.primary").label == "Switch Model"
    await user.should_not_see(marker="confirm.ask.then")
    user.find(marker="confirm.ask.cancel").click()
    await user.should_not_see(marker="confirm.ask")
    assert fake.loads == []


@pytest.mark.scenario("lowvram")
async def test_low_vram_asks_load_anyway_without_the_toggle(user: User, core: Core, fake: FakeEngine) -> None:
    # The board's replayed dock click can run before the first device reading (then it is a Confirm ask whose
    # toggle stays, U6); a click with the reading in is the low-VRAM ask of D1/D20.
    await opened(user, core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    user.find(marker="confirm.ask.cancel").click()
    user.find(marker=f"dock.row.{TUR}").click()
    pop = the(user, "confirm.ask")
    warn = [line for line in pop.descendants() if "vt-ask-line" in line.classes and line.tone == "warn"]
    assert len(warn) == 1 and warn[0].text.startswith("Low VRAM · ") and "vt-c-warn" in classes(warn[0])
    assert the(user, "confirm.ask.primary").label == "Load Anyway"
    await user.should_not_see(marker="confirm.ask.dont_ask")


async def test_a_named_ask_says_what_follows(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    row = next(r for r in core.state.library.rows if r.backend == TUR)
    request = GenerateRequest(row.voice_id, TUR, "Okay.", "en", None, None)
    plan = core.planner.switch_plan(TUR, reason="generate", after=AfterGenerate((request,)))
    shell.ctx.behaviours.models().ask(plan)
    then = the(user, "confirm.ask.then")
    assert (then.text, then.tone) == (f"Then generates take {core.state.takes.next_id}", "text-soft")
    await user.should_not_see(marker="confirm.ask.dont_ask")


async def test_escape_closes_the_popover_the_log_and_the_search(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker=f"dock.row.{TUR}").click()
    await user.should_see(marker="confirm.ask")
    user.find(marker="status").click()
    await user.should_see(marker="shell.log_panel")
    user.find(marker="shell.search").type("aiko")
    await user.should_see(marker="shell.search.result.voice:aiko")
    user.find(marker="shell.keys.escape").trigger("command", {"id": "escape", "combo": "Escape"})
    await user.should_not_see(marker="confirm.ask")
    await user.should_not_see(marker="shell.log_panel")
    await user.should_not_see(marker="shell.search.result")
    assert the(user, "shell.search").value == ""


# ---- Log and toast ------------------------------------------------------------------------------------

async def test_the_log_opens_from_the_chip_and_the_button(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_not_see(marker="shell.log_panel")
    log_button = the(user, "shell.log")
    assert log_button.props["aria-pressed"] == "false"
    user.find(marker="status").click()
    panel = the(user, "shell.log_panel")
    assert (panel.props["role"], panel.props["aria-label"]) == ("log", "Log")
    assert log_button.props["aria-pressed"] == "true"
    shown = [line.text for line in the(user, "shell.log_lines").descendants() if isinstance(line, TextSpan)]
    assert shown == list(core.state.log.lines) and shown == list(fake.scenario.log)
    core.log.add("Take 8 failed · CUDA out of memory")
    await settle(core, fake)
    last = [line for line in the(user, "shell.log_lines").descendants() if isinstance(line, TextSpan)][-1]
    assert last.text.endswith("Take 8 failed · CUDA out of memory") and "is-bad" in classes(last)
    user.find(marker="shell.log").click()
    await user.should_not_see(marker="shell.log_panel")


async def test_toasts_replace_each_other_and_expire(user: User, core: Core, fake: FakeEngine,
                                                    clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    await user.should_not_see(marker="shell.toast")
    user.find(marker="window.minimize").click()
    first = the(user, "shell.toast")
    assert (first.text, first.props["role"], "is-bad" in classes(first)) == (MINIMIZE_ONLY_NATIVE, "status", False)
    user.find(marker="window.minimize").click()
    second = the(user, "shell.toast")
    assert second is not first and second.text == MINIMIZE_ONLY_NATIVE and not first.visible
    clock.advance(2.6)
    await user.should_not_see(marker="shell.toast")
    user.find(marker="window.minimize").click()
    user.find(marker="shell.toast").click()
    await user.should_not_see(marker="shell.toast")


# ---- Close --------------------------------------------------------------------------------------------

async def test_close_quits_at_once_when_idle(user: User, core: Core, fake: FakeEngine, quits: list[str]) -> None:
    await opened(user, core, fake)
    close = the(user, "window.close")
    assert (close.props["aria-label"], close.props["title"]) == (CLOSE_LABEL, CLOSE_LABEL)
    assert "em-danger" in classes(close) and "is-armed" not in classes(close)
    user.find(marker="window.close").click()
    assert quits == ["quit"]


@pytest.mark.scenario("loading")
async def test_close_arms_while_busy(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler,
                                     quits: list[str]) -> None:
    await opened(user, core, fake)
    close = the(user, "window.close")
    user.find(marker="window.close").click()
    assert quits == [] and close.props["aria-label"] == CLOSE_ARMED and "is-armed" in classes(close)
    assert the(user, "shell.toast").text == QUIT_BUSY
    clock.advance(3.1)
    await settle(core, fake)
    assert close.props["aria-label"] == CLOSE_LABEL and "is-armed" not in classes(close)
    user.find(marker="window.close").click()
    user.find(marker="window.close").click()
    assert quits == []
    clock.advance(0.6)
    user.find(marker="window.close").click()
    assert quits == ["quit"]


# ---- the search box -----------------------------------------------------------------------------------

async def test_search_results_hints_and_picks(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    assert the(user, "shell.search.badge").text == "Ctrl K"
    hint = the(user, "shell.search.hint")
    assert hint.text == "Type a voice, a take or an action"
    user.find(marker="shell.search").type("aiko")
    result = the(user, "shell.search.result.voice:aiko")
    assert "is-first" in classes(result)
    texts = [e.text for e in result.descendants() if isinstance(e, TextSpan) and e is not result]
    assert texts == ["aiko", "Voices · Multilingual · Japanese"]
    await user.should_not_see(marker="shell.search.hint")
    user.find(marker="shell.search").type("zzz")
    assert the(user, "shell.search.hint").text == "No matching voices, takes or actions"
    user.find(marker="shell.search").clear()
    user.find(marker="shell.search").type("forge a")
    user.find(marker="shell.search.result.action:forge").trigger("mousedown")
    await user.should_see(marker="page.forge")
    assert the(user, "shell.search").value == "" and the(user, "shell.title").text == "Forge"
    user.find(marker="shell.search").type("aiko")
    user.find(marker="shell.search.result.voice:aiko").trigger("mousedown")
    await user.should_see(marker="page.speak")
    assert the(user, "shell.search").value == ""
    user.find(marker="shell.search").type("free")
    user.find(marker="shell.search").trigger("keydown.esc")
    assert the(user, "shell.search").value == "" and shell._search.query == ""


async def test_enter_runs_the_first_result(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="shell.search").trigger("keydown.enter", "Free VRAM")
    await settle(core, fake)
    assert the(user, "status").text == "No model loaded" and fake.loads == []
    assert the(user, "shell.search").value == ""


async def test_ctrl_k_focuses_the_search_and_a_cleared_binding_hides_the_badge(
        user: User, core: Core, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = await opened(user, core, fake)
    box = the(user, "shell.search")
    focused: list[str] = []
    monkeypatch.setattr(box, "run_method", lambda name, *args, **kwargs: focused.append(name))
    keys = the(user, "shell.keys")
    assert keys.props["combos"]["Ctrl+K"] == "search" and keys.props["suspended"] is False
    user.find(marker="shell.keys").trigger("command", {"id": "search", "combo": "Ctrl+K"})
    assert focused == ["focus"]
    shell.ctx.behaviours.commands().bind("search", "key", "")
    await user.should_not_see(marker="shell.search.badge")


# ---- player, keys, drops ------------------------------------------------------------------------------

async def test_the_player_follows_playback_and_reports_back(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    player = the(user, "shell.player")
    playback = shell.ctx.behaviours.playback()
    assert isinstance(player, Player) and player.target == playback.vm.target
    assert player.target.src == core.take_url(core.session.take_id)        # the replay's paused position
    user.find(marker="shell.keys").trigger("command", {"id": "play", "combo": "Space"})
    assert player.target.playing is True and player.props["target"]["playing"] is True
    user.find(marker="shell.player").trigger("state", {"playing": True, "position": 2.5, "duration": 4.6,
                                                       "ended": False, "error": ""})
    assert (playback.vm.position, playback.vm.playing) == (2.5, True)


async def test_the_escape_dispatcher_binds_only_escape(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    escape = the(user, "shell.keys.escape")
    assert isinstance(escape, KeyDispatcher) and escape.props["combos"] == {"Escape": "escape"}
    assert "Escape" not in the(user, "shell.keys").props["combos"]


async def test_a_drop_goes_to_forge_or_says_where_to_go(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    core.events.dropped.emit(("C:\\clips\\rin.wav",))
    await settle(core, fake)
    assert the(user, "shell.toast").text == DROP_ELSEWHERE
    dropped: list[tuple[str, ...]] = []

    class ForgeStub:
        async def drop_paths(self, paths: tuple[str, ...]) -> None:
            dropped.append(paths)

    user.find(marker="nav.forge").click()
    await user.should_see(marker="page.forge")
    shell.ctx.behaviours.provide("forge", ForgeStub())       # after the page built its own controller
    core.events.dropped.emit(("C:\\clips\\rin.wav",))
    await settle(core, fake)
    assert dropped == [("C:\\clips\\rin.wav",)]


# ---- the shell's wiring rows --------------------------------------------------------------------------

CONTROLLERS: dict[str, type] = {"status": StatusController, "models": ModelsController,
                                "playback": PlaybackController, "commands": CommandRegistry,
                                "search": SearchController}


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
    elif row.act == "type":
        found.type(row.arg)
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    else:
        raise AssertionError(f"the shell has no {row.act!r} rows")


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, open_route: Any, quits: list[str],
                           monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)
    await open_route(user, row.route)
    await settle(core, fake)
    fake.reset_records()
    calls.clear()
    await _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    if row.confirm == "arm":
        assert quits == [] and fake.loads == []
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "ask":
        assert fake.loads == []
        await user.should_see(marker="confirm.ask")
        user.find(marker="confirm.ask.primary").click()
        await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    elif row.confirm == "arm":
        core.scheduler.advance(0.6)
        await _act(user, row)
        await settle(core, fake)
    assert fake.loads == list(row.loads)
    if row.intent == "status.request_quit":
        assert quits == ["quit"]


# ---- the widgets the pages build with -----------------------------------------------------------------

@pytest.fixture
def widget_page(run: launch.Launch, monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[Any], None]], None]:
    """Put a test page on /voices inside the real shell."""
    def install(build: Callable[[Any], None]) -> None:
        theme = SimpleNamespace(manifest=run.theme.manifest, build_shell=run.theme.build_shell,
                                pages={**run.theme.pages, "/voices": build})
        monkeypatch.setattr(run, "theme", theme)
    return install


async def test_buttons_map_the_action_state(user: User, core: Core, fake: FakeEngine,
                                            widget_page: Callable[..., None]) -> None:
    clicks: list[str] = []

    async def later() -> None:
        clicks.append("async")

    def page(_ctx: Any) -> None:
        EmiyaButton("Generate", tone="primary", size=34, hint=("Ctrl", "Enter"), on_click=lambda: clicks.append("go")
                    ).mark("w.generate")
        EmiyaButton("Busy", size=26, enabled=False, tooltip="", on_click=lambda: clicks.append("never")
                    ).mark("w.off")
        EmiyaButton("Later", tone="plain", on_click=later).mark("w.async")
        IconButton("play", aria="Play", filled=True, on_click=lambda: clicks.append("icon")).mark("w.icon")

    widget_page(page)
    await user.open("/voices")
    generate = the(user, "w.generate")
    assert generate.tag == "button" and generate.props["type"] == "button"
    assert {"em-primary", "vt-b34"} <= classes(generate) and "disabled" not in generate.props
    assert [chip.text for chip in generate._hints.descendants() if isinstance(chip, TextSpan)] == ["Ctrl", "Enter"]
    user.find(marker="w.generate").click()
    user.find("Generate").click()                              # by its label: the button, not the span
    user.find(marker="w.off").click()
    user.find(marker="w.async").click()
    user.find(marker="w.icon").click()
    await asyncio.sleep(0.05)
    assert sorted(clicks) == ["async", "go", "go", "icon"]          # the async handler runs as a task
    off = the(user, "w.off")
    assert off.props["disabled"] is True and "disable" not in off.props
    generate.action = ActionState(False, "Loading Turbo… 4 s of ~9 s", fill=0.45, reason="busy")
    assert (generate.label, generate.style["--fill"], generate.props["title"]) == (
        "Loading Turbo… 4 s of ~9 s", "45.0%", "busy")
    assert "is-fill" in classes(generate) and not generate._hints.visible
    generate.action = ActionState(False, "Pick a Voice", tone="grey")
    assert {"em-primary", "is-grey"} <= classes(generate) and "--fill" not in generate.style
    generate.action = ActionState(True, "Switch to Turbo & Generate 3 Takes", tone="plain", aria="Named", armed=True,
                                  indeterminate=True)
    assert {"em-btn", "is-armed", "is-sweep"} <= classes(generate) and generate.props["aria-label"] == "Named"
    generate.action = ActionState(True, "Cancel Queue", tone="link")
    assert "vt-link" in classes(generate) and "vt-b34" not in classes(generate)
    icon = the(user, "w.icon")
    assert {"em-icon", "vt-filled", "vt-i30"} <= classes(icon) and icon.props["aria-label"] == "Play"
    icon.set_icon("pause")
    assert "<rect" in icon._svg.content and "http" not in icon._svg.content


async def test_segments_switches_and_sliders(user: User, core: Core, fake: FakeEngine,
                                             widget_page: Callable[..., None]) -> None:
    seen: list[Any] = []

    def page(_ctx: Any) -> None:
        Segmented((("off", "Off"), ("last", "Last Model")), value="off", aria="Warm up on launch",
                  item_marker="w.warmup", on_change=seen.append).mark("w.segments")
        Segmented((("", "All", False), (MTL, "Multilingual", True)), aria="Show voices for",
                  item_marker="w.filter").mark("w.filter")
        DotSwitch("Background Effects", note="Embers, traces and sparks", value=True,
                  on_change=seen.append).mark("w.switch")
        GoldSlider(min=0.25, max=2.0, step=0.05, value=0.5, tick=0.5, aria="Expressiveness",
                   on_release=seen.append).mark("w.slider")

    widget_page(page)
    await user.open("/voices")
    group = the(user, "w.segments")
    assert (group.props["role"], group.props["aria-label"]) == ("group", "Warm up on launch")
    assert the(user, "w.warmup.off").props["aria-pressed"] == "true"
    user.find(marker="w.warmup.last").click()
    group.value = "last"
    assert the(user, "w.warmup.last").props["aria-pressed"] == "true" and "is-on" in classes(the(user, "w.warmup.last"))
    assert [the(user, f"w.filter.{key}").props["aria-pressed"] for key in ("", MTL)] == ["false", "true"]
    switch = the(user, "w.switch")
    assert switch.props["aria-pressed"] == "true" and switch.note.text == "Embers, traces and sparks"
    user.find(marker="w.switch").click()
    slider = the(user, "w.slider")
    assert slider._input.props["type"] == "range" and slider._input.props["serverValue"] == 0.5
    assert slider._tick.style["left"] == "14.29%"
    user.find(marker="w.slider").trigger("change", 1.25)
    slider.enabled = False
    assert slider._input.props["disabled"] is True
    assert seen == ["last", False, 1.25]


async def test_chips_indicators_meters_and_lists(user: User, core: Core, fake: FakeEngine,
                                                 widget_page: Callable[..., None]) -> None:
    seen: list[Any] = []

    def page(_ctx: Any) -> None:
        Chip("Resident", tone="gold").mark("w.chip")
        DotIndicator("loading").mark("w.ring")
        Meter(app_pct=28.35, other_pct=20.84, tall=True, aria="VRAM").mark("w.meter")
        with ui.element("div").classes("vt-pick"):
            PickButton("Japanese", on_click=lambda: seen.append("open")).mark("w.pick")
            ListBox(items=(LangItemVM("ja", "Japanese", True, "voice default"), ("en", "English", False, "")),
                    open=True, on_pick=seen.append, on_close=lambda: seen.append("close"),
                    item_marker="w.lang").mark("w.list")
        with Row("Name", label_width=170).mark("w.row"):
            TextSpan("rin")
        SectionLabel("Delivery", right=True).mark("w.caption")
        ListHost(lambda item: TextSpan(item).mark(f"w.item.{item}"), items=("a", "b")).mark("w.host")
        KeyButton("Ctrl+K", aria="Search key: Ctrl+K", on_click=lambda: seen.append("listen"),
                  on_capture=lambda **combo: seen.append(combo), on_stop=lambda: seen.append("stop")).mark("w.key")
        TextField(placeholder="voice name", aria="Name").mark("w.field")
        ScriptBox(placeholder="Type what the voice should say…", lang="ja").mark("w.script")

    widget_page(page)
    await user.open("/voices")
    chip = the(user, "w.chip")
    assert {"vt-chip", "vt-chip-20", "is-gold"} <= classes(chip) and not chip._dot.visible
    ring = the(user, "w.ring")
    assert ring._ring.visible and "is-spin" in classes(ring._ring)
    ring.ring = 0.5
    assert ring._ring.style["--ring"] == "180deg" and "is-spin" not in classes(ring._ring)
    ring.state = "resident"
    assert ring._dot.visible and "is-resident" in classes(ring._dot)
    meter = the(user, "w.meter")
    assert "is-tall" in classes(meter) and meter._app.style["width"] == "28.35%"
    options = [the(user, "w.lang.ja"), the(user, "w.lang.en")]
    assert [o.props["aria-selected"] for o in options] == ["true", "false"] and "is-on" in classes(options[0])
    user.find(marker="w.lang.en").click()
    listbox = the(user, "w.list")
    UserInteraction(user, {listbox._backdrop}, None).click()      # a click outside the list
    listbox.open = False
    await user.should_not_see(marker="w.lang.ja")
    user.find(marker="w.pick").click()
    assert the(user, "w.pick").props["aria-haspopup"] == "listbox"
    row = the(user, "w.row")
    assert row.label is not None and row.label.style["width"] == "170px"
    assert the(user, "w.caption").right is not None
    host = the(user, "w.host")
    host.items = ("c",)
    await user.should_see(marker="w.item.c")
    await user.should_not_see(marker="w.item.a")
    user.find(marker="w.key").click()
    user.find(marker="w.key").trigger("keydown", {"key": "k", "ctrl": True, "shift": False, "alt": False})
    user.find(marker="w.key").trigger("blur")
    assert the(user, "w.field").props["type"] == "text" and "em-input" in classes(the(user, "w.field"))
    script = the(user, "w.script")
    assert script.props["lang"] == "ja" and script.html_id in script.caret_js
    assert seen == ["en", "close", "open", "listen",
                    {"key": "k", "ctrl": True, "shift": False, "alt": False, "composing": False}, "stop"]


# ---- the stylesheet -----------------------------------------------------------------------------------

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def test_colour_values_live_only_in_the_token_block() -> None:
    """Every colour literal of emiya.css is the value of a custom property; the rules use var(--token)."""
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    stray = []
    for declaration in re.split(r"[;{}]", text):
        if COLOUR.search(declaration) and not declaration.strip().startswith("--"):
            stray.append(declaration.strip())
    assert stray == []


def test_every_token_the_rules_use_is_declared() -> None:
    """var(--x) names a token of the block (or a custom property a widget sets inline, with a fallback)."""
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    declared = set(re.findall(r"(--[\w-]+)\s*:", text))
    inline = {"--fill", "--pct", "--ring"}                     # EmiyaButton, GoldSlider, DotIndicator
    used = set(re.findall(r"var\((--[\w-]+)", text))
    assert used - declared - inline == set()
    assert all(re.search(rf"var\({name},", text) for name in inline)
    assert text.count("{") == text.count("}")


def test_the_fx_tokens_are_declared_once() -> None:
    text = CSS.read_text(encoding="utf-8")
    names = re.findall(r"(--emiya-[\w-]+)\s*:", text)
    assert len(names) == len(set(names))
    assert {f"--emiya-fx-{name}" for name in ("ember-1", "ember-2", "ember-3", "ember-hot", "trace-shadow", "trace",
                                             "trace-blade", "glow", "spark")} <= set(names)


def test_the_stylesheet_styles_every_class_the_fx_components_emit() -> None:
    text = CSS.read_text(encoding="utf-8")
    for name in ("emiya-trace-mid", "emiya-trace-shadow", "emiya-trace-line", "emiya-trace-dot",
                 "emiya-trace-glow-in", "emiya-trace-glow-out", "emiya-wave-bar", "emiya-wave-playhead",
                 "emiya-wave-glow", "emiya-bars-mark-label", "emiya-bars-mark-line", "emiya-bars-sel", "vt-scrub",
                 "vt-wavebox", "vt-rise", "vt-spark", "vt-head-glow", "vt-pop-in", "vt-toast", "em-flash", "vt-page"):
        assert f".{name}" in text, name
    assert "body.reduce-motion *" in text and "@media (prefers-reduced-motion: reduce)" in text
    for name in ("vtRise", "vtSpark", "vtHead", "vtPopIn", "vtToastIn", "emflash"):
        assert f"@keyframes {name}" in text


def test_the_mockup_blob_urls_never_reach_the_theme() -> None:
    """The mockup's /_blob/ asset URLs stay in the mockup: the logo and the art come from static\\img."""
    for module in ("shell.py", "widgets.py"):
        assert "/_blob/" not in (ROOT / "ui" / "themes" / "emiya" / module).read_text(encoding="utf-8")
