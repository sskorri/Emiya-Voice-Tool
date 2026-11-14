"""The Voices page under NiceGUI's user fixture (§8.5, P2): gui.py on a FakeEngine scenario in tmp_path.

Covered: every row of §6.8 (filter, segments, count, header, rows, empty list, footer, the Details title, chip,
play, waveform, language picker titled "Pick a language", Default Expressiveness, reference clip, cache, consent,
notes, Speak with, Re-forge…, Forge on …, Delete with its arm, no-details card) in the voices, firstrun,
freeing, voices_missing_files and voices_unconfirmed scenarios; that edits write the registry only and Delete goes
through the facade with the injected trash, with no model load; a changed registry is re-read when the page opens;
the page's wiring rows (tests\\pages\\wiring\\voices.py); and that static\\pages\\voices.css uses only tokens.

Explorer is never started: ui.kit.files.reveal and open_folder are replaced for every test here. Cross-page
buttons are checked against stubs of the Forge and Speak controllers (§9) with navigation recorded, so the
other pages' own state never decides these tests.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui import ui
from nicegui.element import Element
from nicegui.testing import User

from ui import launch
from ui.behaviours import NotBuilt
from ui.behaviours.voices import VoicesController
from ui.contract.markers import CONFIRM_MARKERS, REQUIRED_MARKERS
from ui.contract.viewmodels import ForgeController as ForgeControllerProtocol
from ui.contract.viewmodels import ForgeVM, SpeakVM
from ui.contract.viewmodels import SpeakController as SpeakControllerProtocol
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.themes.emiya.fx.bars import EmiyaBars
from ui.themes.emiya.shell import ShellRoot
from ui.themes.emiya.widgets import TextSpan

from tests.behaviours import FakeKit
from tests.pages.wiring import Wire
from tests.pages.wiring.voices import OPT_OUT, WIRING

ROOT = Path(__file__).resolve().parents[2]
PAGE_CSS = ROOT / "ui" / "themes" / "emiya" / "static" / "pages" / "voices.css"
THEME_CSS = ROOT / "ui" / "themes" / "emiya" / "static" / "emiya.css"
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
ORDER = ("aiko", "friend", "marco", "narrator", "host-turbo")
MODEL_CALLS = {"make_resident", "load_backend", "unload_backend", "speak", "load_voice"}


# ---- helpers ------------------------------------------------------------------------------------------

def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def the(user: User, marker: str) -> Any:
    """The one element with this marker that is shown."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def shown(element: Element) -> bool:
    """The element and all its ancestors are visible."""
    return element.visible and all(ancestor.visible for ancestor in element.ancestors())


def texts_in(element: Element) -> list[str]:
    """The non-empty texts of the shown TextSpans inside element, in document order."""
    return [child.text for child in element.descendants()
            if isinstance(child, TextSpan) and child.text and shown(child)]


def classes(element: Element) -> set[str]:
    return set(element.classes)


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds: intents run as client tasks and the engine on workers."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks run, then wait for the work they started (a scenario's held job only to its hold)."""
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


def factory_of(user: User) -> Any:
    """This page load's BehaviourFactory (through the shell's ThemeContext)."""
    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell.ctx.behaviours


def names(fake: FakeEngine) -> set[str]:
    return {call.name for call in fake.calls}


class ForgeStub(NotBuilt):
    """ForgeController as P3 builds it (§5.9), every intent a no-op except prefill, which records the call."""

    def __init__(self) -> None:
        super().__init__(ForgeControllerProtocol, ForgeVM)
        self.prefilled: list[tuple[str, str, Path | None]] = []
        self.prefill = self._prefill

    def _prefill(self, *, voice_id: str, mode: str, clip_path: Path | None = None) -> str:
        self.prefilled.append((voice_id, mode, clip_path))
        return "/forge"


class SpeakStub(NotBuilt):
    """SpeakController as P1 builds it (§5.7), every intent a no-op except pick_voice, which records the voice."""

    def __init__(self) -> None:
        super().__init__(SpeakControllerProtocol, SpeakVM)
        self.picked: list[str] = []
        self.pick_voice = self.picked.append


@pytest.fixture(autouse=True)
def explorer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """ui.kit.files.reveal and open_folder land here instead of starting Explorer."""
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr("ui.kit.files.reveal", lambda path: seen.append(("reveal", Path(path))))
    monkeypatch.setattr("ui.kit.files.open_folder", lambda path: seen.append(("open_folder", Path(path))))
    return seen


@pytest.fixture
def navigated(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
    """Call it after the page is open: from then on ui.navigate.to(route) records the route instead."""
    routes: list[str] = []

    def start() -> list[str]:
        monkeypatch.setattr(ui.navigate, "to", lambda target, *args, **kwargs: routes.append(str(target)))
        return routes

    return start


async def opened(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await open_route(user, "/voices")
    await settle(core, fake)


# ---- markers and the Library ------------------------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_the_page_renders_its_markers(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    listed = ("voices.list", "voices.filter", "voices.backend_filter", "voices.count", "voices.new", "voices.edit",
              "voices.play", "voices.bars", "voices.edit.language", "voices.edit.expr", "voices.replace",
              "voices.reveal", "voices.speak", "voices.reforge", "voices.copy", "voices.delete",
              CONFIRM_MARKERS["arm"], *(f"voices.row.{voice}" for voice in ORDER),
              *(f"voices.backend_filter.{key}" for key in ("all", MTL, STD, TUR)))
    for marker in (*REQUIRED_MARKERS["/voices"], *listed):
        await user.should_see(marker=marker)
    assert set(the(user, "voices.delete")._markers) == {"voices.delete", "confirm.arm"}
    await user.should_not_see(marker="voices.no_details")
    await user.should_not_see(marker="voices.empty")
    assert fake.loads == []


@pytest.mark.scenario("voices")
async def test_the_library_matches_the_mockup(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    assert the(user, "voices.count").text == "5 voices · 3 Multilingual · 1 Standard · 1 Turbo"
    for text in ("Library", "Details", "Name", "Model", "Language", "Expr", "Clip", "Created", "Forge New Voice",
                 "Saved in cache/registry.json · one cached .pt per voice"):
        await user.should_see(text)
    search = the(user, "voices.filter")
    assert (search.props["placeholder"], search.props["aria-label"]) == ("Filter voices", "Filter voices")
    group = the(user, "voices.backend_filter")
    assert group.props["aria-label"] == "Show voices for"
    assert [texts_in(the(user, f"voices.backend_filter.{key}")) for key in ("all", MTL, STD, TUR)] == [
        ["All"], ["Multilingual"], ["Standard"], ["Turbo"]]
    assert the(user, "voices.backend_filter.all").props["aria-pressed"] == "true"
    rows = core.state.library.rows
    aiko = the(user, "voices.row.aiko")
    vm_rows = factory_of(user).voices().vm.rows
    assert [row.voice_id for row in vm_rows] == list(ORDER) == [row.voice_id for row in rows]
    assert texts_in(aiko) == ["aiko", "Multilingual", "Japanese", "0.50", "11.2 s", vm_rows[0].created_text]
    assert "on" in classes(aiko) and aiko.props["aria-pressed"] == "true"
    assert texts_in(the(user, "voices.row.host-turbo"))[:5] == ["host-turbo", "Turbo", "English", "tags", "8.3 s"]
    model_cells = [child for child in aiko.descendants() if isinstance(child, TextSpan) and "vt-vrow-model" in
                   child.classes]
    assert model_cells[0].props["title"] == "Resident in VRAM"
    dot = next(child for child in aiko.descendants() if "vt-vrow-dot" in child.classes)
    assert "is-resident" in classes(dot)
    narrator = the(user, "voices.row.narrator")
    narrator_model = next(child for child in narrator.descendants() if "vt-vrow-model" in child.classes)
    assert narrator_model.props["title"] == "Not loaded · about 12 s to load"
    created_cell = next(child for child in aiko.descendants() if "vt-vcol-created" in child.classes)
    assert created_cell.props["title"] == vm_rows[0].created_tip


@pytest.mark.scenario("voices")
async def test_the_filter_and_the_segments(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.filter").type("mar")
    await user.should_see(marker="voices.row.marco")
    await user.should_not_see(marker="voices.row.aiko")
    assert the(user, "voices.count").text == "1 of 5 voices"
    assert the(user, "voices.title").text == "marco"                     # nothing selected: the first row shown
    user.find(marker="voices.filter").type("zzz")
    await user.should_see("No voices match the filter")
    user.find(marker="voices.filter").clear()
    user.find(marker=f"voices.backend_filter.{TUR}").click()
    await user.should_see(marker="voices.row.host-turbo")
    await user.should_not_see(marker="voices.row.marco")
    assert the(user, f"voices.backend_filter.{TUR}").props["aria-pressed"] == "true"
    user.find(marker="voices.backend_filter.all").click()
    await settle(core, fake)
    for voice in ORDER:
        await user.should_see(marker=f"voices.row.{voice}")


@pytest.mark.scenario("voices")
async def test_a_row_click_shows_that_voice_and_clears_the_delete_arm(user: User, core: Core, fake: FakeEngine,
                                                                     open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.delete").click()
    await user.should_see("Click Again to Delete")
    user.find(marker="voices.row.marco").click()
    await settle(core, fake)
    await user.should_see("Speak with marco")
    assert the(user, "voices.title").text == "marco"
    assert "on" in classes(the(user, "voices.row.marco")) and "on" not in classes(the(user, "voices.row.aiko"))
    await user.should_not_see("Click Again to Delete")
    assert "delete_voice" not in names(fake)


# ---- the Details card ---------------------------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_the_details_of_aiko(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    details = factory_of(user).voices().vm.details
    assert the(user, "voices.title").text == "aiko"
    assert the(user, "voices.caption").text == details.caption and details.caption.startswith(
        "Multilingual · Japanese · forged ")
    chip = the(user, "voices.chip")
    assert (chip.text, chip.tone) == ("Resident", "gold")
    play = the(user, "voices.play")
    assert play.props["aria-label"] == play.props["title"] == "Play the reference clip of aiko"
    assert {"em-icon", "vt-filled", "vt-i30"} <= classes(play)
    bars = next(element for element in marked(user, "voices.bars") if isinstance(element, EmiyaBars))
    assert bars.props["kind"] == "voices" and len(bars.props["bars"]) == 138
    assert {zone for _, zone in bars.props["bars"]} == {"timbre", "rest"}
    assert bars.props["aria"] == "Reference clip aiko_ref.wav, 11.2 s · the first 10 s set the timbre"
    pick = the(user, "voices.edit.language")
    assert (pick.props["title"], pick.text, pick.enabled) == ("Pick a language", "Japanese", True)
    await user.should_not_see(marker="voices.lang_locked")
    slider = the(user, "voices.edit.expr")
    assert (slider.value, slider.enabled, the(user, "voices.edit.expr_value").text) == (0.5, True, "0.50")
    assert slider._input.props["aria-label"] == "Default Expressiveness"
    assert (slider._input.props["min"], slider._input.props["max"], slider._input.props["step"]) == (
        "0.25", "2", "0.05")
    ref = the(user, "voices.ref")
    assert (ref.text, ref.props["title"]) == ("aiko_ref.wav · 11.2 s used", details.ref_tip)
    assert texts_in(the(user, "voices.cache")) == ["cache/chatterbox-multilingual/aiko.pt"]
    reveal = the(user, "voices.reveal")
    assert (reveal.props["aria-label"], reveal.enabled) == ("Show in Explorer", True)
    consent = the(user, "voices.consent")
    assert (consent.text, consent.tone) == ("Confirmed when forged", "text")
    for text in ("Edits here change the registry only; no model loads until you speak.",
                 "Delete removes the registry entry and the cached .pt; the WAV stays in voices/reference.",
                 "Speak with aiko", "Re-forge…", "Forge on Turbo…", "Replace…", "Delete", "Reference clip", "Cache",
                 "Consent", "Default Expressiveness"):
        await user.should_see(text)
    speak = the(user, "voices.speak")
    assert {"em-primary", "vt-b34"} <= classes(speak)
    assert fake.loads == []


@pytest.mark.scenario("voices")
async def test_a_turbo_voice_locks_the_language_and_dims_expressiveness(user: User, core: Core, fake: FakeEngine,
                                                                       open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.row.host-turbo").click()
    await user.should_see(marker="voices.lang_locked")
    await user.should_not_see(marker="voices.edit.language")
    assert texts_in(the(user, "voices.lang_locked")) == ["English", "English only"]
    slider = the(user, "voices.edit.expr")
    assert slider.enabled is False and slider._input.props.get("disabled") is True
    assert the(user, "voices.edit.expr_value").text == "—"
    row = next(element for element in slider.ancestors() if "em-row" in element.classes)
    assert "vt-off" in classes(row)                                       # the row dims (0.45, M L3153)
    assert the(user, "voices.chip").text == "Loads in ~9 s" and the(user, "voices.chip").tone == "off"
    await user.should_see("Forge on Multilingual…")
    rows_before = names(fake)
    user.find(marker="voices.edit.expr").trigger("change", 1.0)           # a disabled slider is refused
    await settle(core, fake)
    assert fake.registry.get("host-turbo")["exaggeration"] == 0.5 and "update_voice" not in names(fake) - rows_before


@pytest.mark.scenario("voices")
async def test_picking_a_language_writes_the_registry_only(user: User, core: Core, fake: FakeEngine,
                                                           open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    fake.reset_records()
    user.find(marker="voices.edit.language").click()
    await user.should_see(marker="voices.edit.language.item.ko")
    items = [element for element in marked(user, "voices.edit.language.list")[0].descendants()
             if any(marker.startswith("voices.edit.language.item.") for marker in element._markers)]
    assert len(items) == 23 and items[0].code == "ja" and items[0].props["aria-selected"] == "true"
    assert the(user, "voices.edit.language").props["aria-expanded"] == "true"
    user.find(marker="voices.edit.language.item.it").click()
    await until(lambda: fake.registry.get("aiko")["language"] == "it", "the registry write")
    await settle(core, fake)
    await user.should_not_see(marker="voices.edit.language.item.ko")      # the list closed
    assert the(user, "voices.edit.language").text == "Italian"
    assert texts_in(the(user, "voices.row.aiko"))[2] == "Italian"
    assert core.state.log.lines[-1].endswith("  Registry · aiko language set to Italian")
    assert fake.loads == [] and not names(fake) & MODEL_CALLS and "update_voice" in names(fake)


@pytest.mark.scenario("voices")
async def test_expressiveness_is_saved_on_release(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    fake.reset_records()
    user.find(marker="voices.edit.expr").trigger("change", 0.704)
    await until(lambda: fake.registry.get("aiko")["exaggeration"] == 0.7, "the registry write")
    await settle(core, fake)
    assert the(user, "voices.edit.expr").value == 0.7 and the(user, "voices.edit.expr_value").text == "0.70"
    assert texts_in(the(user, "voices.row.aiko"))[3] == "0.70"
    assert core.state.log.lines[-1].endswith("  Registry · aiko default expressiveness set to 0.70")
    assert fake.loads == [] and not names(fake) & MODEL_CALLS


@pytest.mark.scenario("voices")
async def test_a_failed_save_is_a_red_toast_and_the_slider_snaps_back(user: User, core: Core, fake: FakeEngine,
                                                                     open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    fake.fail_next("update_voice", PermissionError("The voice registry is locked by another program"))
    user.find(marker="voices.edit.expr").trigger("input", 1.2)            # the label follows the thumb
    assert the(user, "voices.edit.expr_value").text == "1.20"
    user.find(marker="voices.edit.expr").trigger("change", {"value": 1.2, "sync": 1})
    await user.should_see("Couldn't save aiko: The voice registry is locked by another program")
    await settle(core, fake)
    slider = the(user, "voices.edit.expr")
    assert slider.value == slider._input.props["serverValue"] == 0.5
    assert slider._input.props["sync"] == 1 and "value" not in slider._input.props
    assert the(user, "voices.edit.expr_value").text == "0.50"
    assert fake.registry.get("aiko")["exaggeration"] == 0.5


# ---- Delete -------------------------------------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_delete_arms_then_sends_the_pt_to_the_trash(user: User, core: Core, fake: FakeEngine,
                                                          open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    pt = core.paths.cache / MTL / "aiko.pt"
    wav = core.paths.voices / "aiko_ref.wav"
    assert pt.is_file() and wav.is_file() and core.session.voice_id == "aiko"
    fake.reset_records()
    user.find(marker="voices.delete").click()
    await user.should_see("Click Again to Delete")
    delete = the(user, "voices.delete")
    assert "is-armed" in classes(delete) and "delete_voice" not in names(fake)
    core.scheduler.advance(0.6)
    user.find(marker="voices.delete").click()
    await until(lambda: fake.registry.get("aiko") is None, "the delete")
    await settle(core, fake)
    await user.should_see("Deleted aiko · its reference WAV stays in voices/reference")
    await user.should_not_see(marker="voices.row.aiko")
    assert core.trash.moved == [pt] and not pt.exists() and wav.is_file()   # the injected trash; the WAV stays
    assert core.session.voice_id == "" and the(user, "voices.title").text == "friend"
    assert core.state.log.lines[-1].endswith(
        "  Removed aiko · registry entry and cache/chatterbox-multilingual/aiko.pt · the WAV stays")
    assert the(user, "voices.count").text == "4 voices · 2 Multilingual · 1 Standard · 1 Turbo"
    assert fake.loads == [] and not names(fake) & MODEL_CALLS


@pytest.mark.scenario("voices")
async def test_the_delete_arm_expires_after_3_s(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler,
                                                open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.delete").click()
    await user.should_see("Click Again to Delete")
    clock.advance(3.1)
    await settle(core, fake)
    await user.should_not_see("Click Again to Delete")
    assert "is-armed" not in classes(the(user, "voices.delete")) and "delete_voice" not in names(fake)


@pytest.mark.scenario("freeing")
async def test_delete_is_disabled_while_a_job_runs(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    assert core.state.job.kind == "unload"
    delete = the(user, "voices.delete")
    assert (delete.enabled, delete.props["title"], delete.props.get("disabled")) == (
        False, "Busy · wait for the current job", True)
    assert the(user, "voices.reforge").enabled and the(user, "voices.edit.language").enabled   # registry edits go on
    user.find(marker="voices.delete").trigger("click")                    # refused in the controller too
    await settle(core, fake)
    assert "delete_voice" not in names(fake) and fake.registry.get("aiko") is not None


# ---- missing files, consent, the first run --------------------------------------------------------------

@pytest.mark.scenario("voices_missing_files")
async def test_missing_files_render(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    aiko = the(user, "voices.row.aiko")
    clip = next(child for child in aiko.descendants() if "vt-vcol-clip" in child.classes)
    assert (clip.text, clip.props["title"]) == ("—", "Reference clip missing") and "is-missing" in classes(clip)
    ref = the(user, "voices.ref")
    assert (ref.text, ref.tone) == ("aiko_ref.wav · missing", "bad") and "vt-c-bad" in classes(ref)
    assert texts_in(the(user, "voices.cache")) == ["cache/chatterbox-multilingual/aiko.pt", " · missing"]
    play = the(user, "voices.play")
    assert play.enabled is False and play.props["title"] == "Reference clip missing"
    bars = next(element for element in marked(user, "voices.bars") if isinstance(element, EmiyaBars))
    assert (bars.props["bars"], bars.props["aria"]) == ([], "")
    assert the(user, "voices.reveal").enabled                              # its model folder is still there


@pytest.mark.scenario("voices_unconfirmed")
async def test_a_voice_without_consent(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.row.narrator").click()
    await settle(core, fake)
    await user.should_see("Not confirmed · re-forge to confirm")
    consent = the(user, "voices.consent")
    assert consent.tone == "bad" and "vt-c-bad" in classes(consent)
    dot = next(child for child in the(user, "voices.edit").descendants() if "vt-vd-dot" in child.classes)
    assert dot.tone == "bad"


@pytest.mark.scenario("firstrun")
async def test_the_first_run_shows_the_empty_library(user: User, core: Core, fake: FakeEngine,
                                                     open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    assert the(user, "voices.count").text == "0 voices"
    assert the(user, "voices.empty").text == "No voices yet · forge one from a reference clip"
    await user.should_see("Pick a voice to see its details.")
    await user.should_see(marker="voices.no_details")
    await user.should_not_see(marker="voices.edit")
    assert marked(user, "voices.edit") and marked(user, "voices.delete")    # rendered, hidden (M L638)


# ---- cross-page buttons, files and the clip -------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_reforge_copy_and_replace_prefill_the_forge_draft(user: User, core: Core, fake: FakeEngine,
                                                                 open_route: Any, navigated: Any,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    clip = Path(launch.LAST_LAUNCH.paths.voices) / "sample.wav"
    monkeypatch.setattr(launch.LAST_LAUNCH, "kit", FakeKit(file_result=clip))
    await opened(user, core, fake, open_route)
    forge = ForgeStub()
    factory_of(user).provide("forge", forge)
    routes = navigated()
    user.find(marker="voices.reforge").click()
    user.find(marker="voices.copy").click()
    user.find(marker="voices.replace").click()
    await until(lambda: len(forge.prefilled) == 3, "three prefills")
    await settle(core, fake)
    assert forge.prefilled == [("aiko", "reforge", None), ("aiko", "copy", None), ("aiko", "reforge", clip)]
    assert routes == ["/forge", "/forge", "/forge"] and fake.loads == []


@pytest.mark.scenario("voices")
async def test_speak_with_picks_the_voice_and_opens_speak(user: User, core: Core, fake: FakeEngine,
                                                          open_route: Any, navigated: Any) -> None:
    await opened(user, core, fake, open_route)
    speak = SpeakStub()
    factory_of(user).provide("speak", speak)
    routes = navigated()
    user.find(marker="voices.row.narrator").click()
    user.find(marker="voices.speak").click()
    await settle(core, fake)
    assert speak.picked == ["narrator"] and routes == ["/"] and fake.loads == []


@pytest.mark.scenario("voices")
async def test_forge_new_voice_opens_forge(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                           navigated: Any) -> None:
    await opened(user, core, fake, open_route)
    routes = navigated()
    user.find(marker="voices.new").click()
    await settle(core, fake)
    assert routes == ["/forge"]


@pytest.mark.scenario("voices")
async def test_show_in_explorer_reveals_the_pt(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                               explorer: list[tuple[str, Path]]) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.reveal").click()
    await until(lambda: bool(explorer), "the reveal")
    await settle(core, fake)
    assert explorer == [("reveal", core.paths.cache / MTL / "aiko.pt")]


@pytest.mark.scenario("voices")
async def test_the_play_button_auditions_the_reference_clip(user: User, core: Core, fake: FakeEngine,
                                                            open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    user.find(marker="voices.play").click()
    playback = factory_of(user).playback()
    assert playback.vm.target.src == core.clip_url("aiko") and playback.vm.target.playing
    play = the(user, "voices.play")
    assert play.icon == "pause" and play.props["aria-label"] == "Stop the reference clip of aiko"
    user.find(marker="voices.play").click()
    await settle(core, fake)
    assert the(user, "voices.play").icon == "play" and not playback.vm.target.playing
    assert fake.loads == []


@pytest.mark.scenario("voices")
async def test_opening_the_page_rereads_a_changed_registry(user: User, core: Core, fake: FakeEngine,
                                                           open_route: Any) -> None:
    await opened(user, core, fake, open_route)
    fake.registry.update("marco", language="fr")                         # cli.py edited the registry
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    user.find(marker="nav.voices").click()
    await user.should_see(marker="page.voices")
    await until(lambda: any(row.voice_id == "marco" and row.language == "fr" for row in core.state.library.rows),
                "the new snapshot")
    await settle(core, fake)
    assert texts_in(the(user, "voices.row.marco"))[2] == "French"


# ---- the wiring rows ----------------------------------------------------------------------------------

def _spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    owner, name = intent.split(".")
    assert owner == "voices"
    original = getattr(VoicesController, name)
    if inspect.iscoroutinefunction(original):
        async def spy_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(VoicesController, name, spy_async)
    else:
        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(VoicesController, name, spy)


def _act(user: User, row: Wire) -> None:
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "type":
        found.type(row.arg)
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    else:
        raise AssertionError(f"the Voices page has no {row.act!r} rows")


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, open_route: Any, navigated: Any,
                           monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)
    await opened(user, core, fake, open_route)
    factory = factory_of(user)
    factory.provide("forge", ForgeStub())
    factory.provide("speak", SpeakStub())
    navigated()
    fake.reset_records()
    calls.clear()
    _act(user, row)
    await settle(core, fake)
    await until(lambda: row.intent in calls, f"{row.marker} to reach {row.intent}")
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "arm":
        assert "delete_voice" not in names(fake)
        core.scheduler.advance(0.6)
        _act(user, row)
        await until(lambda: "delete_voice" in names(fake), "the second press to delete")
    await settle(core, fake)
    assert fake.loads == list(row.loads) and not names(fake) & MODEL_CALLS


def test_every_intent_is_wired_or_opted_out() -> None:
    from ui.contract.viewmodels import VoicesController as Protocol
    declared = {f"voices.{name}" for cls in Protocol.__mro__ if getattr(cls, "_is_protocol", False)
                for name, member in vars(cls).items() if inspect.isfunction(member) and not name.startswith("_")}
    wired = {row.intent for row in WIRING}
    assert declared - wired - set(OPT_OUT) == set()
    assert all(row.route == "/voices" for row in WIRING)


# ---- the stylesheet -----------------------------------------------------------------------------------

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def test_the_page_stylesheet_uses_only_tokens() -> None:
    text = re.sub(r"/\*.*?\*/", "", PAGE_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert not COLOUR.search(text)
    declared = set(re.findall(r"(--[\w-]+)\s*:", THEME_CSS.read_text(encoding="utf-8")))
    used = set(re.findall(r"var\((--[\w-]+)", text))
    assert used and used <= declared
    assert '@import url("pages/voices.css");' in THEME_CSS.read_text(encoding="utf-8")
