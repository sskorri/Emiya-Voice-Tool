"""The cross-page round trips with the real controllers on both sides (§8.5, §9, I1), the boards whose replay
needs the page controllers (generating, queued, forging, forged), and every board of §6.13 from its scenario.

The page packages tested their cross-page buttons against stubs of the other page's controller; here both pages
are real: Speak "Use sample.wav" → Forge stages the clip (13.4 s selected); Voices Re-forge… / Forge on … /
Replace… → the Forge draft; Forge and Voices "Speak with" → Speak; Takes "Reuse A's Settings" → Speak; the rail
search jumps; Settings › Default Model → an idle Forge draft; deleting the selected voice clears Speak's
selection; a forged voice shows in the Speak, Voices and Models counts. Nothing in these flows loads a model
unless a named control says so.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui.element import Element
from nicegui.testing import User

from ui.behaviours.forge import CLIP_FILTERS
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.themes.emiya.widgets import TextSpan

from tests.engine.conftest import write_clip
from tests.pages.test_wiring import behaviours_of, install_kit, marked, model_calls, settle, the, until

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
SAMPLE_META = "30.0 s · mono · 22.05 kHz · trimmed to the first 13.4 s"
FLASHES = {"em-flash", "em-flash-b"}


def scenario(name: str) -> Any:
    return pytest.param(name, marks=pytest.mark.scenario(name), id=name)


def calls_of(fake: FakeEngine, name: str) -> list[Any]:
    return [call for call in fake.calls if call.name == name]


def classes(element: Element) -> set[str]:
    return set(element.classes)


def row_of(user: User, marker: str) -> Element:
    """The list row around a row's pick button (it carries the on, flash and enter classes)."""
    return the(user, marker).parent_slot.parent


async def opened(user: User, core: Core, fake: FakeEngine, open_route: Any, route: str) -> None:
    """The app opened as a user opens it, on route, with the scenario's replay and first reading in."""
    await open_route(user, route)
    await settle(core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    fake.reset_records()


async def go(user: User, core: Core, fake: FakeEngine, page: str) -> None:
    """Navigate by the rail and let the page settle."""
    user.find(marker=f"nav.{page}").click()
    await user.should_see(marker=f"page.{page}")
    await settle(core, fake)


async def landed(user: User, core: Core, fake: FakeEngine, page: str) -> None:
    """A control navigated: wait for the page, then for the work it started."""
    await user.should_see(marker=f"page.{page}")
    await settle(core, fake)


def library_ids(core: Core) -> set[str]:
    return {row.voice_id for row in core.state.library.rows}


def fact(user: User, backend: str, label: str) -> str:
    """A Models card's fact value ("Voices" → "3")."""
    card = the(user, f"models.card.{backend}")
    for row in card.descendants():
        if "vt-models-fact" in row.classes:
            spans = [child for child in row.descendants() if isinstance(child, TextSpan)]
            if spans and spans[0].text == label:
                return spans[1].text
    raise AssertionError(f"the {backend} card has no {label} fact")


# ---- Speak → Forge ------------------------------------------------------------------------------------

@pytest.mark.scenario("firstrun")
async def test_use_sample_stages_it_on_forge(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/")
    user.find(marker="speak.empty.sample").click()
    await landed(user, core, fake, "forge")
    await until(lambda: core.forge_draft.clip_state == "ready", "sample.wav to stage")
    await settle(core, fake)
    clip = core.forge_draft.clip
    assert clip is not None and clip.name == "sample.wav" and round(clip.selection_s, 1) == 13.4
    await user.should_see(SAMPLE_META)
    assert the(user, "forge.name").value == "sample"                    # D-F4
    assert the(user, "forge.go").label == "Confirm Consent First"
    assert fake.loads == [] and model_calls(fake) == []


# ---- Voices → Forge -----------------------------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_reforge_round_trip_replaces_the_voice_and_speaks_with_it(user: User, core: Core, fake: FakeEngine,
                                                                         open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/voices")
    user.find(marker="voices.row.aiko").click()
    user.find(marker="voices.reforge").click()
    await landed(user, core, fake, "forge")
    await until(lambda: core.forge_draft.clip_state == "ready", "aiko's clip to stage")
    await settle(core, fake)
    draft = core.forge_draft
    assert (draft.name, draft.backend, draft.language, draft.consent) == ("aiko", MTL, "ja", True)
    assert the(user, "forge.go").label == "Forge Voice"
    user.find(marker="forge.go").click()                                # aiko exists: the first click arms
    await settle(core, fake)
    assert the(user, "forge.go").label == "Click Again to Replace" and calls_of(fake, "load_voice") == []
    core.scheduler.advance(0.6)
    user.find(marker="forge.go").click()
    await settle(core, fake)
    await until(lambda: calls_of(fake, "load_voice") != [], "the bake")
    await settle(core, fake)
    bake = calls_of(fake, "load_voice")
    assert [(call.args[0], call.kwargs["backend"], call.kwargs["force"]) for call in bake] == [("aiko", MTL, True)]
    user.find(marker="forge.speak_with").click()                        # "Speak with aiko"
    await landed(user, core, fake, "speak")
    assert core.session.voice_id == "aiko" and the(user, "speak.voice.aiko").props["aria-pressed"] == "true"
    assert fake.loads == []


@pytest.mark.scenario("voices")
async def test_forge_on_turbo_prefills_a_copy(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/voices")
    user.find(marker="voices.row.aiko").click()
    copy = the(user, "voices.copy")
    assert copy.label == "Forge on Turbo…"
    user.find(marker="voices.copy").click()
    await landed(user, core, fake, "forge")
    await until(lambda: core.forge_draft.clip_state == "ready", "aiko's clip to stage")
    await settle(core, fake)
    draft = core.forge_draft
    assert (draft.name, draft.backend, draft.language) == ("aiko-turbo", TUR, "en")
    assert draft.clip is not None and draft.clip.name == "aiko_ref.wav"
    assert the(user, "forge.name").value == "aiko-turbo"
    assert the(user, f"forge.backend.{TUR}").props["aria-pressed"] == "true"
    assert fake.loads == [] and model_calls(fake) == []


@pytest.mark.scenario("voices")
async def test_replace_picks_a_new_clip_and_clears_consent(user: User, core: Core, fake: FakeEngine,
                                                          open_route: Any, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    clip = write_clip(tmp_path / "picked" / "new_aiko.wav", 9.0)
    kit = install_kit(monkeypatch, tmp_path, file_result=clip)
    await opened(user, core, fake, open_route, "/voices")
    user.find(marker="voices.row.aiko").click()
    user.find(marker="voices.replace").click()
    await landed(user, core, fake, "forge")
    await until(lambda: core.forge_draft.clip_state == "ready", "the picked clip to stage")
    await settle(core, fake)
    assert [call["filters"] for call in kit.called("pick_file")] == [CLIP_FILTERS]
    draft = core.forge_draft
    assert (draft.name, draft.backend, draft.consent, draft.clip_file) == ("aiko", MTL, False, "new_aiko.wav")
    await user.should_see("new_aiko.wav")
    assert the(user, "forge.go").label == "Confirm Consent First"       # Q3: a new clip clears the consent
    assert fake.loads == [] and model_calls(fake) == []


# ---- → Speak ------------------------------------------------------------------------------------------

@pytest.mark.scenario("voices")
async def test_voices_speak_with_picks_the_voice_on_speak(user: User, core: Core, fake: FakeEngine,
                                                         open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/voices")
    user.find(marker="voices.row.marco").click()
    assert the(user, "voices.speak").label == "Speak with marco"
    user.find(marker="voices.speak").click()
    await landed(user, core, fake, "speak")
    assert core.session.voice_id == "marco" and the(user, "speak.voice.marco").props["aria-pressed"] == "true"
    assert FLASHES & classes(row_of(user, "speak.voice.marco"))
    assert the(user, "speak.language")._caption.text == "Italian"
    assert fake.loads == [] and model_calls(fake) == []


@pytest.mark.scenario("forged")
async def test_forge_speak_with_picks_the_forged_voice(user: User, core: Core, fake: FakeEngine,
                                                      open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/forge")
    assert the(user, "forge.speak_with").label == "Speak with rin"
    user.find(marker="forge.speak_with").click()
    await landed(user, core, fake, "speak")
    assert core.session.voice_id == "rin" and the(user, "speak.voice.rin").props["aria-pressed"] == "true"
    assert FLASHES & classes(row_of(user, "speak.voice.rin"))
    assert fake.loads == [] and model_calls(fake) == []


async def test_reuse_a_settings_sets_the_speak_delivery(user: User, core: Core, fake: FakeEngine,
                                                       open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/takes")
    assert the(user, "takes.reuse").label == "Reuse A's Settings"
    user.find(marker="takes.reuse").click()
    await landed(user, core, fake, "speak")
    await user.should_see("Delivery set from Take 5")
    session = core.session
    assert (session.voice_id, session.language, session.exaggeration, session.cfg_weight) == ("aiko", "ja", 0.5, 0.3)
    assert the(user, "speak.guid_value").text == "0.30" and the(user, "speak.expr_value").text == "0.50"
    assert the(user, "speak.preset.Slower").props["aria-pressed"] == "true"
    assert fake.loads == [] and model_calls(fake) == []


async def test_search_jumps_from_another_page(user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/takes")
    user.find(marker="shell.search").type("marco")                     # a voice: Speak with it picked
    user.find(marker="shell.search.result.voice:marco").trigger("mousedown")
    await landed(user, core, fake, "speak")
    assert core.session.voice_id == "marco" and FLASHES & classes(row_of(user, "speak.voice.marco"))
    await go(user, core, fake, "takes")
    user.find(marker="shell.search").type("Take 6")                    # a take: Speak with it playing
    user.find(marker="shell.search.result.take:6").trigger("mousedown")
    await landed(user, core, fake, "speak")
    playback = behaviours_of(user).playback()
    assert core.session.take_id == 6 and playback.vm.target.src == core.take_url(6) and playback.vm.target.playing
    assert the(user, "take.title").text == "Take 6" and FLASHES & classes(row_of(user, "speak.session.row.6"))
    user.find(marker="shell.search").type("forge a")                   # an action: Forge
    user.find(marker="shell.search.result.action:forge").trigger("mousedown")
    await landed(user, core, fake, "forge")
    user.find(marker="shell.search").type("Free VRAM")                 # an action: frees the model, stays
    user.find(marker="shell.search.result.action:free").trigger("mousedown")
    await settle(core, fake)
    await until(lambda: not core.state.engine.resident, "Free VRAM")
    await user.should_see(marker="page.forge")
    assert [call.name for call in fake.calls if call.name in ("unload_backend", "make_resident")] == ["unload_backend"]
    assert fake.loads == []


# ---- Settings → Forge ---------------------------------------------------------------------------------

async def test_default_model_repicks_an_idle_forge_draft(user: User, core: Core, fake: FakeEngine,
                                                        open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/forge")
    assert core.forge_draft.backend == MTL and the(user, f"forge.backend.{MTL}").props["aria-pressed"] == "true"
    await go(user, core, fake, "settings")
    user.find(marker=f"settings.default_backend.{TUR}").click()
    await settle(core, fake)
    assert core.settings.get("core", "default_backend") == TUR and core.forge_draft.backend == TUR
    await go(user, core, fake, "forge")
    assert the(user, f"forge.backend.{TUR}").props["aria-pressed"] == "true"
    await user.should_see("English only")
    assert fake.loads == [] and model_calls(fake) == []


@pytest.mark.scenario("forged")
async def test_default_model_keeps_a_draft_that_shows_its_result(user: User, core: Core, fake: FakeEngine,
                                                                 open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/settings")
    user.find(marker=f"settings.default_backend.{TUR}").click()
    await settle(core, fake)
    assert core.settings.get("core", "default_backend") == TUR
    assert (core.forge_draft.forged, core.forge_draft.backend) == ("rin", MTL)      # M L3238-3241
    assert fake.loads == []


# ---- the library across pages -------------------------------------------------------------------------

async def test_deleting_the_selected_voice_clears_speaks_selection(user: User, core: Core, fake: FakeEngine,
                                                                   open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/")
    assert core.session.voice_id == "aiko" and the(user, "speak.generate").label == "Generate"
    await go(user, core, fake, "voices")
    user.find(marker="voices.row.aiko").click()
    user.find(marker="voices.delete").click()
    await settle(core, fake)
    await user.should_see("Click Again to Delete")
    core.scheduler.advance(0.6)
    user.find(marker="voices.delete").click()
    await until(lambda: "aiko" not in library_ids(core), "aiko to be deleted")
    await settle(core, fake)
    assert core.session.voice_id == ""
    assert [path.name for path in core.trash.moved] == ["aiko.pt"]       # the clip stays (§5.8)
    assert (core.paths.voices / "aiko_ref.wav").is_file()                # voices\reference keeps the WAV
    await go(user, core, fake, "speak")
    assert marked(user, "speak.voice.aiko") == [] and the(user, "speak.generate").label == "Pick a Voice"
    assert the(user, "speak.count").text == "4 voices"
    assert fake.loads == [] and model_calls(fake) == []


@pytest.mark.scenario("forge_ready")
async def test_a_forged_voice_shows_in_the_speak_voices_and_models_counts(user: User, core: Core, fake: FakeEngine,
                                                                          open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/models")
    assert fact(user, MTL, "Voices") == "3"
    await go(user, core, fake, "forge")
    user.find(marker="forge.go").click()                                # Multilingual is resident: it bakes
    await until(lambda: "rin" in library_ids(core), "rin to be forged")
    await settle(core, fake)
    await user.should_see("Speak with rin")
    await go(user, core, fake, "speak")
    assert the(user, "speak.count").text == "6 voices" and marked(user, "speak.voice.rin")
    await go(user, core, fake, "voices")
    assert the(user, "voices.count").text == "6 voices · 4 Multilingual · 1 Standard · 1 Turbo"
    assert marked(user, "voices.row.rin")
    await go(user, core, fake, "models")
    assert (fact(user, MTL, "Voices"), fact(user, STD, "Voices"), fact(user, TUR, "Voices")) == ("4", "1", "1")
    assert fake.loads == [] and [call.args[0] for call in calls_of(fake, "load_voice")] == ["rin"]


# ---- the boards the replay builds through the page controllers ------------------------------------------

@pytest.mark.scenario("generating")
async def test_the_generating_board_replays_a_held_take(user: User, core: Core, fake: FakeEngine,
                                                       open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/")
    job = core.state.job
    assert (job.kind, job.take_no, job.voice, job.queued) == ("generate", 8, "aiko", 0)
    assert any(hold.point == "speak" and hold.entered.is_set() for hold in fake.holds)
    assert the(user, "speak.generate").label == "Generating · 0.0 s"
    assert the(user, "speak.session.generating").texts[:3] == ("8", "Generating…", "aiko")
    assert the(user, "take.title").text == "Take 8" and the(user, "status").text.startswith("Generating")


@pytest.mark.scenario("queued")
async def test_the_queued_board_replays_one_take_and_two_waiting(user: User, core: Core, fake: FakeEngine,
                                                                open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/")
    job = core.state.job
    assert (job.kind, job.take_no, job.queued) == ("generate", 8, 2)
    assert the(user, "speak.generate").label == "Generating · 0.0 s · 2 queued"
    await user.should_see(marker="speak.cancel_queue")


@pytest.mark.scenario("forging")
async def test_the_forging_board_replays_a_held_bake(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                    clock: ManualScheduler) -> None:
    await opened(user, core, fake, open_route, "/forge")
    job = core.state.job
    assert (job.kind, job.phase, job.voice, job.target) == ("forge", "bake", "rin", MTL)
    assert any(hold.point == "load_voice" and hold.entered.is_set() for hold in fake.holds)
    clock.advance(0.4)
    await settle(core, fake)
    assert the(user, "forge.go").label == "Baking conditioning… 0.4 s"


@pytest.mark.scenario("forged")
async def test_the_forged_board_replays_a_finished_forge(user: User, core: Core, fake: FakeEngine,
                                                        open_route: Any) -> None:
    await opened(user, core, fake, open_route, "/forge")
    assert "rin" in library_ids(core) and core.forge_draft.forged == "rin" and not core.state.job.kind
    for marker, label in (("forge.speak_with", "Speak with rin"), ("forge.test_line", "Play Test Line"),
                          ("forge.another", "Forge Another")):
        assert the(user, marker).label == label
    await user.should_not_see(marker="forge.go")


# ---- every board of §6.13 from its scenario -------------------------------------------------------------

async def _first_run(user: User, core: Core, fake: FakeEngine) -> None:
    assert (core.state.library.rows, core.state.takes.takes, core.state.engine.resident) == ((), (), "")
    assert the(user, "status").text == "No model loaded" and the(user, "shell.dock_status").text == "No model loaded"
    assert the(user, "dock.free").enabled is False
    generate = the(user, "speak.generate")
    assert (generate.label, generate.enabled, the(user, "speak.generate3").enabled) == (
        "Forge a Voice First", False, False)
    assert the(user, "speak.status").text == "Forge a voice first, then type what it should say."
    assert the(user, "speak.stats").text == "Empty" and the(user, "speak.clear").enabled is False
    assert the(user, "speak.note").text == "Forge a voice to shape its delivery."
    assert "is-dark" in classes(the(user, "speak.text").parent_slot.parent)
    for hidden in ("take.player", "speak.session"):
        await user.should_not_see(marker=hidden)
    await user.should_see(marker="shell.art")
    assert len(core.state.log.lines) == 1


async def _steady_state(user: User, core: Core, fake: FakeEngine) -> None:
    assert core.state.engine.resident == MTL and the(user, "speak.generate").label == "Generate"
    assert [n for n in range(1, 8) if marked(user, f"speak.session.row.{n}")] == [4, 5, 6, 7]
    assert (the(user, "take.ab.a")._face.text, the(user, "take.ab.b")._face.text) == ("A · Take 5", "B · Take 7")
    assert {take.id for take in core.state.takes.takes if take.starred} == {7, 4, 3}
    await go(user, core, fake, "takes")
    assert the(user, "takes.foot").text == "3 starred · 27.8 s of audio"


async def _switch(user: User, core: Core, fake: FakeEngine) -> None:
    await user.should_see(marker="confirm.ask")
    for text in ("Switch Model", "Unloads Multilingual · frees 3.4 GB", "Don't ask again"):
        await user.should_see(text)
    assert the(user, "speak.generate").label == "Switch to Turbo & Generate" and core.session.voice_id == "host-turbo"


async def _low_vram(user: User, core: Core, fake: FakeEngine) -> None:
    await user.should_see(marker="confirm.ask")                         # the board's dock click
    for text in ("Load Anyway", "Low VRAM"):
        await user.should_see(text)
    await user.should_not_see("Don't ask again")
    user.find(marker="confirm.ask.cancel").click()
    user.find(marker="speak.generate").click()                          # a named control asks too (D1)
    await settle(core, fake)
    for text in ("Load Anyway", "Then generates take 8"):
        await user.should_see(text)
    assert fake.loads == []


async def _failure(user: User, core: Core, fake: FakeEngine) -> None:
    assert core.state.engine.resident == "" and core.state.engine.failed == TUR
    for marker in ("dock.retry", "dock.load_default"):
        await user.should_see(marker=marker)
    await user.should_not_see(marker="dock.free")
    assert the(user, "dock.load_default").label == "Load Multilingual" and the(user, "status").tone == "red"
    lines = core.state.log.lines
    assert any("Switching Multilingual → Turbo" in line for line in lines)
    assert "Load failed: chatterbox-turbo · " in lines[-1]


async def _forge_board(user: User, core: Core, fake: FakeEngine) -> None:
    draft = core.forge_draft
    assert (draft.name, draft.backend, draft.language, draft.exaggeration, draft.consent) == ("rin", MTL, "ja",
                                                                                              0.5, False)
    assert draft.clip is not None and draft.clip.name == "sample.wav"
    for text in ("cache/chatterbox-multilingual/rin.pt", "Multilingual is resident · forging takes about 2 s"):
        await user.should_see(text)
    assert the(user, "forge.go").label == "Confirm Consent First"


async def _takes_board(user: User, core: Core, fake: FakeEngine) -> None:
    assert the(user, "takes.tab.all").props["aria-selected"] == "true" and the(user, "takes.title").text == "All Takes"


async def _settings_board(user: User, core: Core, fake: FakeEngine) -> None:
    assert the(user, "settings.tab.effects").props["aria-selected"] == "true"
    await user.should_see(marker="settings.effects")


def _page_board(marker: str) -> Callable[[User, Core, FakeEngine], Awaitable[None]]:
    async def check(user: User, core: Core, fake: FakeEngine) -> None:
        await user.should_see(marker=marker)
    return check


def _forge_label(label: str, *, note: str = "", after_click: str = "",
                 shows: tuple[str, ...] = ()) -> Callable[[User, Core, FakeEngine], Awaitable[None]]:
    """A forge seed's Table F row and model note, and what one click on Forge leads to when that click does not
    load by itself (an arm, an ask or a refusal): the next row or texts, with nothing loaded."""
    async def check(user: User, core: Core, fake: FakeEngine) -> None:
        assert the(user, "forge.go").label == label
        if note:
            await user.should_see(note)
        if after_click or shows:
            user.find(marker="forge.go").click()
            await settle(core, fake)
            if after_click:
                assert the(user, "forge.go").label == after_click
            for text in shows:
                await user.should_see(text)
            assert fake.loads == []
    return check


async def _forging(user: User, core: Core, fake: FakeEngine) -> None:
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    clock.advance(0.4)
    await settle(core, fake)
    assert the(user, "forge.go").label == "Baking conditioning… 0.4 s"


async def _forged(user: User, core: Core, fake: FakeEngine) -> None:
    for text in ("Speak with rin", "Play Test Line", "Forge Another"):
        await user.should_see(text)


async def _freeing(user: User, core: Core, fake: FakeEngine) -> None:
    assert the(user, "speak.generate").label == "Unloading Multilingual…"
    assert the(user, f"speak.group.{MTL}").text == "Freeing…"
    await go(user, core, fake, "forge")
    assert the(user, "forge.go").label == "Unloading Multilingual…"


async def _relaunch(user: User, core: Core, fake: FakeEngine) -> None:
    assert (core.session.take_id, core.session.voice_id, core.state.engine.resident) == (7, "aiko", "")
    assert the(user, "speak.generate").label == "Type a Script" and the(user, "take.title").text == "Take 7"
    assert fake.loads == [] and model_calls(fake) == []


BOARDS: dict[str, tuple[str, Callable[[User, Core, FakeEngine], Awaitable[None]]]] = {
    "firstrun": ("/", _first_run),
    "speak": ("/", _steady_state),
    "switch": ("/", _switch),
    "lowvram": ("/", _low_vram),
    "failure": ("/", _failure),
    "forge": ("/forge", _forge_board),
    "voices": ("/voices", _page_board("voices.list")),
    "takes": ("/takes", _takes_board),
    "models": ("/models", _page_board("models.limit")),
    "settings": ("/settings", _settings_board),
    "forge_ready": ("/forge", _forge_label("Forge Voice")),
    "forge_other_backend": ("/forge", _forge_label("Switch to Turbo & Forge",
                                                    note="Forging loads Turbo · unloads Multilingual (~9 s)")),
    "forge_low_vram": ("/forge", _forge_label("Switch to Turbo & Forge", shows=("Load Anyway", "Then forges rin"))),
    "forge_replace": ("/forge", _forge_label("Forge Voice", after_click="Click Again to Replace")),
    "forge_replace_other_backend": ("/forge", _forge_label("Switch to Turbo & Forge",
                                                            after_click="Click Again to Replace")),
    "forge_taken_other": ("/forge", _forge_label("Name Taken on Multilingual")),
    "forge_turbo_short": ("/forge", _forge_label("Clip is 4.2 s; Turbo needs over 5 s")),
    "forging": ("/forge", _forging),
    "forged": ("/forge", _forged),
    "forge_failed": ("/forge", _forge_label("Forge Voice", shows=("Forge failed: CUDA out of memory",))),
    "freeing": ("/", _freeing),
    "relaunch": ("/", _relaunch),
}


@pytest.mark.parametrize("name", [scenario(name) for name in BOARDS])
async def test_the_board_is_reproduced_by_its_scenario(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                       name: str) -> None:
    route, check = BOARDS[name]
    await opened(user, core, fake, open_route, route)
    assert fake.scenario.route == route
    await check(user, core, fake)
