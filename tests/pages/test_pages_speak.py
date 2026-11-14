"""The Speak page under NiceGUI's user fixture (§8.5, P1): gui.py on a FakeEngine scenario in tmp_path.

Covered: the required markers and the page's own markers; the boards of §6.13 element by element (firstrun,
speak, switch, lowvram, failure, loading, generating, queued, freeing, relaunch, and the test line from
`forged`); Table G and Table S as the page shows them; the Voice panel (groups without voices are not listed,
flashes after a jump); Delivery (the Language row is never dimmed, the list of 23, the live sliders); the Script
card (dark with no voices, also with Saber Art hidden; "1 sentence"; the Japanese gate; tags at the caret); the
Take card and "Takes · This Session"; Save As through a fake KitPort; that rendering never loads a model; and
the page's wiring rows (tests\\pages\\wiring\\speak.py). The user fixture fails any test that logs an ERROR.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui.element import Element
from nicegui.testing import User

from ui import launch
from ui.behaviours.commands import CommandRegistry
from ui.behaviours.models import ModelsController
from ui.behaviours.playback import PlaybackController
from ui.behaviours.speak import SpeakController
from ui.behaviours.status import StatusController
from ui.contract.markers import CONFIRM_MARKERS, REQUIRED_MARKERS
from ui.core import Core, catalog
from ui.core.clock import ManualScheduler
from ui.core.fake import SCRIPT_JA, FakeEngine
from ui.themes.emiya.fx.take_wave import EmiyaTakeWave
from ui.themes.emiya.shell import Shell, ShellRoot
from ui.themes.emiya.widgets import GoldSlider, ListBox, Logo, Row, ScriptBox, TextSpan

from tests.behaviours import FakeKit
from tests.pages.wiring import Wire
from tests.pages.wiring.speak import WIRING

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
TAGS = ("[laugh]", "[chuckle]", "[cough]", "[sigh]", "[gasp]")
STEADY_SUMMARY = "aiko · Multilingual (resident) · Japanese · Expr 0.50 · Guid 0.50 · conditioning cached"
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


def speak_of(user: User) -> SpeakController:
    controller = shell_of(user).ctx.behaviours.speak()
    assert isinstance(controller, SpeakController)
    return controller


def classes(element: Element) -> set[str]:
    return set(element.classes)


def spans(element: Element) -> list[str]:
    """The texts of the visible TextSpans inside an element, in order."""
    return [e.text for e in element.descendants() if isinstance(e, TextSpan) and e is not element and e.visible]


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds; the FakeEngine works on worker threads."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let clicks start their jobs, then wait for every job that no hold keeps (a held job: until it is held)."""
    for _ in range(5):
        await asyncio.sleep(0)
    holding = [hold for hold in fake.holds if not hold.is_set()]
    if holding:
        await until(lambda: not core.state.job.kind or any(hold.entered.is_set() for hold in holding),
                    "the held call")
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if holding else [])
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine, route: str = "/") -> Shell:
    """Open route directly, let the scenario's replay and the first device reading finish."""
    await user.open(route)
    await settle(core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    return shell_of(user)


def model_calls(fake: FakeEngine) -> list[str]:
    return [call.name for call in fake.calls if call.name in ("make_resident", "load_backend", "speak", "load_voice")]


@pytest.fixture
def run(user: User) -> launch.Launch:
    assert launch.LAST_LAUNCH is not None
    return launch.LAST_LAUNCH


@pytest.fixture
def kit(run: launch.Launch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeKit:
    """Every page load gets this KitPort: Save As answers with a scratch path."""
    fake_kit = FakeKit(save_result=tmp_path / "saved.wav")
    monkeypatch.setattr(run, "kit", fake_kit)
    return fake_kit


# ---- markers ------------------------------------------------------------------------------------------

async def test_the_required_and_listed_markers_render(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for marker in (*REQUIRED_MARKERS["/"], "speak.voice_list", "speak.voice.aiko", "speak.audition.aiko",
                   "speak.forge_new", "speak.count", "speak.preset.Default", "speak.expr", "speak.guid",
                   "speak.language", "speak.note", "speak.lang_chip", "speak.clear", "speak.generate3",
                   "speak.stats", "take.star", "take.reveal", "take.wave", "take.play", "take.time", "take.ab.a",
                   "take.ab.b", "take.save", "speak.session", "speak.session.row.7", "speak.session.pin_a.7",
                   "speak.session.pin_b.7", "speak.session.star.7", "speak.all_takes",
                   *(f"speak.group.{backend}" for backend in (MTL, STD, TUR))):
        await user.should_see(marker=marker)
    for hidden in ("speak.empty", "speak.reset", "speak.guid0", "speak.use_ja", "speak.cancel_queue",
                   "speak.language.locked", "speak.library_message", "speak.session.generating",
                   "speak.stats_warn", "take.caption_warn"):
        await user.should_not_see(marker=hidden)
    clear = the(user, "speak.clear")
    assert CONFIRM_MARKERS["arm"] in clear._markers                  # the arm control carries confirm.arm


@pytest.mark.scenario("firstrun")
async def test_the_required_markers_exist_on_the_first_run(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for marker in REQUIRED_MARKERS["/"]:
        assert marked(user, marker), marker                           # take.player exists, hidden
    await user.should_not_see(marker="take.player")


# ---- the boards (§6.13) -------------------------------------------------------------------------------

@pytest.mark.scenario("firstrun")
async def test_the_first_run_board_element_by_element(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    empty = the(user, "speak.empty")
    assert "No voices yet" in spans(empty) and "Forge one from a 6–15 s WAV of one speaker." in spans(empty)
    logo = next(e for e in empty.descendants() if isinstance(e, Logo))
    assert (logo.style["width"], logo.style["height"]) == ("64px", "64px")
    assert dict(logo.img.style) == {"left": "-9px", "top": "-6px", "width": "84px", "height": "84px"}
    assert logo.src.startswith(f"/theme/emiya/{GIF}")
    forge, sample = the(user, "speak.empty.forge"), the(user, "speak.empty.sample")
    assert (forge.label, "em-primary" in classes(forge), sample.label, sample.enabled) == (
        "Forge a Voice", True, "Use sample.wav", True)
    await user.should_not_see(marker="speak.voice_list")
    await user.should_not_see(marker="speak.forge_new")
    rows = [e for e in the(user, "speak.delivery").descendants() if isinstance(e, Row)]
    assert [("vt-off" in classes(row), row.label.text if row.label else "") for row in rows] == [
        (True, "Preset"), (True, "Expressiveness"), (True, "Guidance"), (False, "Language")]
    language = the(user, "speak.language")
    assert (language._caption.text, language.enabled, language.props.get("disabled")) == ("English", False, True)
    assert the(user, "speak.expr_value").text == "—" and the(user, "speak.guid_value").text == "—"
    assert the(user, "speak.expr").enabled is False
    assert the(user, "speak.note").text == "Forge a voice to shape its delivery."
    assert the(user, "speak.stats").text == "Empty"
    script_card = the(user, "speak.text").parent_slot.parent
    assert "is-dark" in classes(script_card)
    assert the(user, "speak.clear").enabled is False
    generate = the(user, "speak.generate")
    assert (generate.label, generate.enabled, "is-grey" in classes(generate)) == ("Forge a Voice First", False, True)
    assert not generate._hints.visible and the(user, "speak.generate3").enabled is False
    assert the(user, "speak.status").text == "Forge a voice first, then type what it should say."
    await user.should_not_see(marker="take.player")
    await user.should_not_see(marker="speak.session")
    await user.should_see(marker="shell.art")
    assert fake.loads == [] and model_calls(fake) == []


@pytest.mark.scenario("firstrun")
async def test_the_script_card_stays_dark_with_saber_art_hidden(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    shell.ctx.prefs.set("saber_art", "hide")
    await user.should_not_see(marker="shell.art")
    assert "is-dark" in classes(the(user, "speak.text").parent_slot.parent)


@pytest.mark.scenario("firstrun")
async def test_the_empty_state_logo_stills_under_reduce_motion(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    core.settings.set("core", "reduce_motion", "always")
    logo = next(e for e in the(user, "speak.empty").descendants() if isinstance(e, Logo))
    assert logo.src.startswith(f"/theme/emiya/{STILL}")


async def test_the_steady_state_board_element_by_element(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert [(the(user, f"speak.group.{b}").text, the(user, f"speak.group.{b}").tone) for b in (MTL, STD, TUR)] == [
        ("Resident", "gold"), ("Not loaded · ~12 s", "off"), ("Not loaded · ~9 s", "off")]
    aiko = the(user, "speak.voice.aiko")
    assert (aiko.props["aria-pressed"], aiko._sub.text, aiko._sub.tone) == ("true", "Japanese · 0.50", "gold-dim")
    assert "on" in classes(aiko.parent_slot.parent)
    assert the(user, "speak.voice.host-turbo")._sub.text == "English · tags"
    assert the(user, "speak.audition.aiko").props["aria-label"] == "Audition the reference clip of aiko"
    assert the(user, "speak.count").text == "5 voices"
    assert the(user, "speak.preset.Default").props["aria-pressed"] == "true"
    expr = the(user, "speak.expr")
    assert (expr.value, expr.tick, expr.enabled, the(user, "speak.expr_value").text) == (0.5, 0.5, True, "0.50")
    assert the(user, "speak.language")._caption.text == "Japanese"
    assert the(user, "speak.note").text == "Applies to the next take."
    assert the(user, "speak.stats").text == "26 chars · 3 sentences · ≈ 7 s to generate"
    chip = the(user, "speak.lang_chip")
    assert (chip.text, chip.tone) == ("Japanese · matches text", "off")
    script = the(user, "speak.text")
    assert isinstance(script, ScriptBox) and (script.value, script.props["lang"]) == (SCRIPT_JA, "ja")
    generate = the(user, "speak.generate")
    assert (generate.label, generate.enabled, "em-primary" in classes(generate)) == ("Generate", True, True)
    assert [e.text for e in generate._hints.descendants() if isinstance(e, TextSpan)] == ["Ctrl", "Enter"]
    assert the(user, "speak.status").text == STEADY_SUMMARY


async def test_the_steady_state_take_card_and_session_list(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "take.title").text == "Take 7"
    assert the(user, "take.caption").text == "aiko · Japanese · Expr 0.50 · Guid 0.50 · 4.6 s · made in 6.9 s"
    assert the(user, "take.time").text == "0:01.9 / 0:04.6"              # the board's paused position
    star = the(user, "take.star")
    assert (star.props["aria-pressed"], star.props["aria-label"]) == ("true", "Unstar this take")
    play = the(user, "take.play")
    assert (play.props["aria-label"], play.icon, play.enabled) == ("Play", "play", True)
    wave = the(user, "take.wave")
    assert isinstance(wave, EmiyaTakeWave) and wave.props["take"]["id"] == 7
    assert wave.props["time-target"] == the(user, "take.time").html_id
    assert [(the(user, f"take.ab.{s}")._face.text, the(user, f"take.ab.{s}").props["aria-pressed"]) for s in "ab"] == [
        ("A · Take 5", "false"), ("B · Take 7", "true")]
    assert the(user, "take.save").enabled is True
    rows = [the(user, f"speak.session.row.{n}") for n in (7, 6, 5, 4)]
    assert [row.texts for row in rows][0] == ("7", SCRIPT_JA, "aiko", "0.50", "0.50", "4.6 s")
    assert ["on" in classes(row.parent_slot.parent) for row in rows] == [True, False, False, False]
    assert the(user, "speak.session.pin_b.7").props["aria-pressed"] == "true"
    assert the(user, "speak.session.pin_a.5").props["aria-label"] == "Unpin Take 5 as A"
    assert the(user, "speak.session.pin_a.6").props["aria-label"] == "Pin Take 6 as A"
    assert the(user, "speak.session.star.4").props["aria-label"] == "Unstar Take 4"
    assert "vt-hov" in classes(the(user, "speak.session.star.6"))
    assert marked(user, "speak.session.row.3") == []                  # an earlier session's take
    assert fake.loads == []


@pytest.mark.scenario("switch")
async def test_the_switch_board(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_see(marker="confirm.ask")                        # the board's dock click
    assert the(user, "speak.generate").label == "Switch to Turbo & Generate"
    assert [the(user, f"speak.tag.{tag}").label for tag in TAGS] == list(TAGS)
    await user.should_not_see(marker="speak.lang_chip")
    rows = [e for e in the(user, "speak.delivery").descendants() if isinstance(e, Row)]
    assert ["vt-off" in classes(row) for row in rows] == [True, True, True, False]   # Language is never dimmed
    await user.should_see(marker="speak.language.locked")
    await user.should_not_see(marker="speak.language")
    assert the(user, "speak.note").text == "Turbo ignores Expressiveness and Guidance. Shape delivery with tags."
    summary = the(user, "speak.status")
    assert (summary.text, summary.tone) == ("Unloads Multilingual (3.4 GB) · loads Turbo, about 9 s", "gold-dim")
    assert the(user, "speak.text").props["lang"] == "en"
    assert fake.loads == []


@pytest.mark.scenario("lowvram")
async def test_low_vram_turns_generate_into_the_ask(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    fake.reset_records()
    user.find(marker="speak.generate").click()
    await settle(core, fake)
    await user.should_see("Then generates take 8")
    await user.should_see("Load Anyway")
    await user.should_not_see(marker="confirm.ask.dont_ask")
    assert fake.loads == []


@pytest.mark.scenario("failure")
async def test_the_failure_board(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    turbo = the(user, f"speak.group.{TUR}")
    assert (turbo.text, turbo.tone) == ("Failed", "red")
    assert the(user, f"speak.group.{MTL}").text == "Not loaded · ~14 s"
    assert the(user, "speak.generate").label == "Load Turbo & Generate · about 9 s"
    assert the(user, "speak.status").text == "Loads Turbo, about 9 s"


@pytest.mark.scenario("loading")
async def test_a_load_counts_seconds_on_generate(user: User, core: Core, fake: FakeEngine,
                                                 clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    await until(lambda: core.state.job.phase == "load", "the held load")
    await settle(core, fake)
    generate = the(user, "speak.generate")
    assert (generate.label, generate.enabled, "is-fill" in classes(generate)) == ("Loading Turbo… 1 s of ~9 s", False,
                                                                                  True)
    clock.advance(4.35)
    await settle(core, fake)
    assert generate.label == "Loading Turbo… 4 s of ~9 s" and generate.style["--fill"].endswith("%")
    assert the(user, f"speak.group.{TUR}").text == "Loading · 4 s"
    assert the(user, "speak.generate3").enabled is True                # it would queue behind the load


@pytest.mark.scenario("generating")
async def test_a_generating_take(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    assert the(user, "take.title").text == "Take 8"
    assert the(user, "take.caption").text == "Generating · aiko · Japanese · Expr 0.50 · Guid 0.50"
    wave = the(user, "take.wave")
    assert wave.props["generating"]["active"] is True and wave.props["take"] is None
    assert the(user, "take.play").enabled is False and the(user, "take.time").text == "0:00.0 / 0:00.0"
    for hidden in ("take.star", "take.reveal", "take.ab.a"):
        await user.should_not_see(marker=hidden)
    generating = the(user, "speak.session.generating")
    assert generating.texts == ("8", "Generating…", "aiko", "0.50", "0.50", "…") and generating.enabled is False
    assert not any("on" in classes(the(user, f"speak.session.row.{n}").parent_slot.parent) for n in (7, 6, 5, 4))
    clock.advance(2.2)
    await settle(core, fake)
    assert the(user, "speak.generate").label == "Generating · 2.2 s"
    await user.should_not_see(marker="speak.cancel_queue")


@pytest.mark.scenario("queued")
async def test_queued_takes_and_cancel_queue(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "speak.generate").label == "Generating · 0.0 s · 2 queued"
    user.find(marker="speak.cancel_queue").click()
    await settle(core, fake)
    await user.should_not_see(marker="speak.cancel_queue")
    assert core.state.job.queued == 0 and the(user, "speak.generate").label == "Generating · 0.0 s"


@pytest.mark.scenario("freeing")
async def test_free_vram_reads_unloading(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    generate = the(user, "speak.generate")
    assert (generate.label, generate.style.get("--fill")) == ("Unloading Multilingual…", "0.0%")
    group = the(user, f"speak.group.{MTL}")
    assert (group.text, group.tone) == ("Freeing…", "gold")


@pytest.mark.scenario("relaunch")
async def test_relaunch_selects_take_7_and_aiko_and_loads_nothing(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "speak.voice.aiko").props["aria-pressed"] == "true"
    assert the(user, "speak.generate").label == "Type a Script" and the(user, "take.title").text == "Take 7"
    await user.should_not_see(marker="speak.session")
    assert fake.loads == [] and model_calls(fake) == [] and core.state.engine.resident == ""


@pytest.mark.scenario("forged")
async def test_a_test_line_is_never_a_take_on_speak(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert any(row.voice_id == "rin" for row in core.state.library.rows)    # the board's replayed forge
    fake.hold("speak")
    assert core.jobs.start_test_line("rin").status == "started"
    await settle(core, fake)
    await user.open("/")                                                # Speak opened while the line plays
    await user.should_see(marker="page.speak")
    generate = the(user, "speak.generate")
    assert (generate.label, "is-grey" in classes(generate)) == ("Busy · test line", True)
    await user.should_not_see(marker="speak.session.generating")
    assert the(user, "take.title").text == "Take 7"
    assert the(user, "take.wave").props["generating"]["active"] is False


# ---- the Voice panel ----------------------------------------------------------------------------------

async def test_a_group_without_voices_is_not_listed(user: User, core: Core, fake: FakeEngine) -> None:
    registry = core.paths.cache / "registry.json"
    entries = json.loads(registry.read_text(encoding="utf-8"))
    entries.pop("narrator")
    registry.write_text(json.dumps(entries), encoding="utf-8")
    await opened(user, core, fake)                                      # opening Speak reads it again
    await user.should_not_see(marker=f"speak.group.{STD}")
    assert the(user, "speak.count").text == "4 voices"


async def test_a_search_jump_flashes_the_voice_row(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="shell.search").type("marco")
    user.find(marker="shell.search.result.voice:marco").trigger("mousedown")
    await user.should_see(marker="page.speak")
    row = the(user, "speak.voice.marco").parent_slot.parent
    assert "on" in classes(row) and {"em-flash", "em-flash-b"} & classes(row)
    assert the(user, "speak.language")._caption.text == "Italian"


async def test_picking_a_turbo_voice_gates_japanese_and_loads_nothing(user: User, core: Core,
                                                                      fake: FakeEngine) -> None:
    await opened(user, core, fake)
    fake.reset_records()
    user.find(marker="speak.voice.host-turbo").click()
    generate = the(user, "speak.generate")
    assert (generate.label, generate.enabled) == ("Turbo Is English Only", False)       # D3
    chip = the(user, "speak.lang_chip")
    assert (chip.text, chip.tone) == ("English only · use a Multilingual voice", "red")
    await user.should_not_see(marker="speak.tag.[laugh]")
    user.find(marker="speak.text").clear()
    user.find(marker="speak.text").type("Okay then.")
    assert generate.label == "Switch to Turbo & Generate" and generate.enabled
    assert the(user, "speak.generate3").props["aria-label"] == "Switch to Turbo & Generate 3 Takes"
    assert fake.loads == [] and model_calls(fake) == []


# ---- Delivery -----------------------------------------------------------------------------------------

async def test_the_language_list_and_the_accent_note(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.language").click()
    listbox = the(user, "speak.language.list")
    assert isinstance(listbox, ListBox) and the(user, "speak.language").props["aria-expanded"] == "true"
    options = [e for e in listbox.descendants() if "vt-option" in e.classes]
    assert len(options) == 23 and options[0].code == "ja" and "is-on" in classes(options[0])
    assert [e.text for e in options[0].descendants() if isinstance(e, TextSpan)] == ["Japanese", "voice default"]
    user.find(marker="speak.language.item.en").click()
    await user.should_not_see(marker="speak.language.item.ja")
    assert the(user, "speak.language")._caption.text == "English" and core.session.language == "en"
    note = the(user, "speak.note")
    assert (note.text, note.tone) == ("The clip of aiko is Japanese; Guidance 0 reduces accent carry-over.", "gold-dim")
    await user.should_see(marker="speak.guid0")
    await user.should_see(marker="speak.reset")
    fix = the(user, "speak.use_ja")
    assert fix.label == "Text looks Japanese · Use Japanese"
    user.find(marker="speak.guid0").click()
    assert the(user, "speak.guid_value").text == "0.00"
    await user.should_not_see(marker="speak.guid0")
    user.find(marker="speak.reset").click()
    assert (core.session.language, core.session.cfg_weight) == ("ja", 0.5)
    await user.should_not_see(marker="speak.reset")


async def test_the_sliders_follow_the_thumb_and_send_on_release(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    slider = the(user, "speak.expr")
    assert isinstance(slider, GoldSlider)
    live = [listener for listener in slider._event_listeners.values()
            if listener.type == "input" and listener.handler is None]
    assert len(live) == 1 and "valueAsNumber" in (live[0].js_handler or "")
    assert the(user, "speak.expr_value").html_id in (live[0].js_handler or "")
    user.find(marker="speak.expr").trigger("change", 1.6)
    value = the(user, "speak.expr_value")
    assert (core.session.exaggeration, value.text, value.tone) == (1.6, "1.60", "warn")
    row = slider.parent_slot.parent.parent_slot.parent
    assert row.props["title"] == "High values speed delivery up; lower Guidance to compensate"
    user.find(marker="speak.preset.Dramatic").click()
    assert (the(user, "speak.expr_value").text, the(user, "speak.guid_value").text) == ("0.70", "0.30")
    assert the(user, "speak.preset.Dramatic").props["aria-pressed"] == "true"


# ---- the Script card ----------------------------------------------------------------------------------

async def test_one_sentence_without_a_terminator(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.text").clear()
    user.find(marker="speak.text").type("Hello there")
    assert the(user, "speak.stats").text == "11 chars · 1 sentence"
    user.find(marker="speak.text").type(". Bye!")
    assert the(user, "speak.stats").text == "17 chars · 2 sentences"


async def test_a_long_script_warns_it_may_be_cut(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.text").type(SCRIPT_JA * 7)
    warn = the(user, "speak.stats_warn")
    assert (warn.text, warn.tone) == (" · may be cut at 40 s", "warn")


async def test_kana_on_a_multilingual_voice_offers_japanese(user: User, core: Core, fake: FakeEngine) -> None:
    core.session.language = "it"
    await opened(user, core, fake)
    fix = the(user, "speak.use_ja")
    assert "vt-sp-fix" in classes(fix)
    await user.should_not_see(marker="speak.lang_chip")
    fix_click = user.find(marker="speak.use_ja")
    fix_click.click()
    assert the(user, "speak.lang_chip").text == "Japanese · matches text" and core.session.language == "ja"


@pytest.mark.scenario("other_backend")
async def test_a_tag_goes_in_at_the_caret(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.text").clear()
    user.find(marker="speak.text").type("Okay okay")
    user.find(marker="speak.tag.[laugh]").trigger("click", 4)        # the browser's caret
    assert core.session.text == "Okay [laugh] okay" and the(user, "speak.text").value == "Okay [laugh] okay"
    user.find(marker="speak.tag.[sigh]").click()                       # no caret (no browser): the end
    assert core.session.text == "Okay [laugh] okay [sigh]"
    tag = the(user, "speak.tag.[gasp]")
    assert tag.props["title"] == "Insert [gasp] at the caret" and "vt-tag" in classes(tag)


async def test_clear_arms_and_empties(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.clear").click()
    clear = the(user, "speak.clear")
    assert (clear.label, "is-armed" in classes(clear)) == ("Click Again to Clear", True)
    clock.advance(3.1)
    await settle(core, fake)
    assert clear.label == "Clear" and core.session.text == SCRIPT_JA
    user.find(marker="speak.clear").click()
    clock.advance(0.6)
    user.find(marker="speak.clear").click()
    assert core.session.text == "" and the(user, "speak.text").value == ""
    assert the(user, "speak.clear").enabled is False and the(user, "speak.generate").label == "Type a Script"


# ---- the Take card and the session list ---------------------------------------------------------------

async def test_play_pause_follows_the_player(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="take.play").click()
    play = the(user, "take.play")
    assert (play.props["aria-label"], play.icon) == ("Pause", "pause")
    user.find(marker="shell.player").trigger("state", {"playing": False, "position": 3.0, "duration": 4.6,
                                                       "ended": False, "error": ""})
    assert (play.props["aria-label"], play.icon, the(user, "take.time").text) == ("Play", "play", "0:03.0 / 0:04.6")


async def test_save_as_goes_through_the_kit(user: User, core: Core, fake: FakeEngine, kit: FakeKit) -> None:
    await opened(user, core, fake)
    user.find(marker="take.save").click()
    await until(lambda: any(e.text == "Saved saved.wav" for e in marked(user, "shell.toast") if e.visible),
                "the Save As toast")
    assert [call["default_name"] for call in kit.called("save_as")] == ["aiko_take7.wav"]
    user.find(marker="take.reveal").click()
    assert [call["path"].name for call in kit.called("reveal")] == ["aiko_take7.wav"]


async def test_a_session_row_plays_its_take_and_pins_stars(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.session.row.6").click()
    assert the(user, "take.title").text == "Take 6"
    assert "on" in classes(the(user, "speak.session.row.6").parent_slot.parent)
    user.find(marker="speak.session.pin_a.6").click()
    await settle(core, fake)                                            # the pins arrive with the next change
    assert (core.session.pin_a, the(user, "take.ab.a")._face.text) == (6, "A · Take 6")
    user.find(marker="speak.session.star.6").click()
    await settle(core, fake)
    assert core.takes.get(6).starred and the(user, "speak.session.star.6").props["aria-pressed"] == "true"


async def test_a_new_take_lands_in_the_session_list(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="speak.generate").click()
    await settle(core, fake)
    row = the(user, "speak.session.row.8").parent_slot.parent
    assert "vt-row-in" in classes(row) and "on" in classes(row)
    assert the(user, "take.title").text == "Take 8" and fake.loads == []


async def test_all_takes_opens_takes_on_its_all_tab(user: User, core: Core, fake: FakeEngine,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    shell = await opened(user, core, fake)
    takes = shell.ctx.behaviours.takes()
    tabs: list[str] = []
    original = takes.set_tab
    monkeypatch.setattr(takes, "set_tab", lambda tab: (tabs.append(tab), original(tab))[1])
    user.find(marker="speak.all_takes").click()
    await user.should_see(marker="page.takes")
    assert tabs == ["all"]


@pytest.mark.scenario("firstrun")
async def test_use_sample_asks_forge_and_follows_its_route(user: User, core: Core, fake: FakeEngine) -> None:
    shell = await opened(user, core, fake)
    asked: list[str] = []

    class ForgeStub:
        async def use_sample(self) -> str | None:
            asked.append("use_sample")
            return None

        def toggle_language_list(self, open: bool | None = None) -> None:
            """Navigation closes the language lists."""

    shell.ctx.behaviours.provide("forge", ForgeStub())
    user.find(marker="speak.empty.sample").click()
    await settle(core, fake)
    assert asked == ["use_sample"]
    await user.should_see(marker="page.speak")


@pytest.mark.parametrize("scenario", [
    pytest.param(name, marks=pytest.mark.scenario(name), id=name) for name in ("speak", "firstrun", "generating")])
async def test_every_binding_on_the_page_is_passive(user: User, core: Core, fake: FakeEngine, scenario: str) -> None:
    """R9: every binding follows a bindable field, so nothing is polled (NG/binding.py active_links)."""
    from nicegui import binding

    await opened(user, core, fake)
    assert binding.active_links == []


# ---- one model ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", [
    pytest.param(name, marks=pytest.mark.scenario(name), id=name)
    for name in ("speak", "firstrun", "switch", "lowvram", "failure", "other_backend", "relaunch", "cpu")])
async def test_rendering_speak_loads_no_model(user: User, core: Core, fake: FakeEngine, scenario: str) -> None:
    await opened(user, core, fake)
    fake.reset_records()
    await user.open("/")                                                # a second page load builds it again
    await settle(core, fake)
    await user.should_see(marker="speak.generate")
    assert fake.loads == [] and model_calls(fake) == []


# ---- the wiring rows (tests\pages\wiring\speak.py) ----------------------------------------------------

CONTROLLERS: dict[str, type] = {"speak": SpeakController, "playback": PlaybackController, "status": StatusController,
                                "models": ModelsController, "commands": CommandRegistry}


def _record(intent: str, function: Any, calls: list[str]) -> Any:
    """function wrapped so a call first records intent (async stays async)."""
    if inspect.iscoroutinefunction(function):
        async def spy_async(*args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await function(*args, **kwargs)
        return spy_async

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(intent)
        return function(*args, **kwargs)
    return spy


def _spy_class(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> bool:
    owner, name = intent.split(".")
    cls = CONTROLLERS.get(owner)
    if cls is None:
        return False
    monkeypatch.setattr(cls, name, _record(intent, getattr(cls, name), calls))
    return True


def _spy_instance(user: User, monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    """Another page's controller (Forge, Takes): wrap the instance this page load uses, built or a placeholder."""
    owner, name = intent.split(".")
    controller = getattr(shell_of(user).ctx.behaviours, owner)()
    monkeypatch.setattr(controller, name, _record(intent, getattr(controller, name), calls))


def _names_the_model(element: Element, backend: str) -> bool:
    shown = f"{getattr(element, 'label', '')} {element.props.get('aria-label', '')}"
    return catalog.label(backend) in shown


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
        raise AssertionError(f"Speak has no {row.act!r} rows")


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, kit: FakeKit, monkeypatch: pytest.MonkeyPatch,
                           row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    for name, value in row.session:
        setattr(core.session, name, value)
    calls: list[str] = []
    spied = _spy_class(monkeypatch, row.intent, calls)
    await opened(user, core, fake, row.route)
    if not spied:
        _spy_instance(user, monkeypatch, row.intent, calls)
    fake.reset_records()
    calls.clear()
    if row.confirm == "named":
        assert _names_the_model(the(user, row.marker), row.loads[0]), f"{row.marker} does not name the model"
    await _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    if row.confirm in ("arm", "ask"):
        assert fake.loads == [], "a confirmation loaded a model before it was confirmed"
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "ask":
        await user.should_see(marker="confirm.ask")
        user.find(marker="confirm.ask.primary").click()
        await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    elif row.confirm == "arm":
        core.scheduler.advance(0.6)
        await _act(user, row)
        await settle(core, fake)
    if row.queued is not None:
        assert core.state.job.queued == row.queued
    assert fake.loads == list(row.loads)
    if row.marker == "speak.clear":
        assert core.session.text == ""
