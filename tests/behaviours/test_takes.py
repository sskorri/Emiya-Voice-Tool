"""TakesController (§5.10, §6.10): the list and its tab, search and menus, rows, the compare card, Keep, Reuse,
the Remove Unstarred arm (D-T6, Q10), Export Starred… and Show Folder through a fake KitPort.

Pure Python against a real Core on FakeEngine with the manual clock and the rig's recording trash, so nothing
here ever reaches the real Recycle Bin, a folder dialog or Explorer.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ui.behaviours import takes as takes_module
from ui.behaviours.confirm import ARM_PURGE
from ui.behaviours.takes import (
    ALL_MODELS,
    ALL_VOICES,
    EMPTY_FILTERED,
    EMPTY_NONE,
    EMPTY_SESSION,
    EMPTY_STARRED,
    NEEDS_WINDOW,
    SAME_SETTINGS,
    TakesController,
    compare_bars,
    compare_note,
    compare_rows,
    export_text,
    knob_text,
    on_fixed_drive,
    plural,
    take_row,
    tenths,
    when_text,
)
from ui.contract.plans import ActionState, ConfirmRequest, PlayerTarget
from ui.contract.records import Take
from ui.contract.viewmodels import TakesController as TakesControllerProtocol
from ui.core.fake import SCRIPT_JA
from ui.core.services import takes_store
from ui.core.services.takes_store import ExportResult
from ui.core.services.voices import MONTHS, created_tip

from tests.behaviours import FakeKit, factory_for, no_error_logs, offline, started, texts, tick, watch_toasts  # noqa: F401
from tests.core.test_facade import Rig, entered, request, rigs  # noqa: F401

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
FOOT = "3 starred · 27.8 s of audio"                                   # §6.13: the speak board's foot line


async def board(rigs: Callable[..., Rig], scenario: str = "speak", *, kit: FakeKit | None = None,
                browser: bool = False, **options: Any) -> tuple[Rig, TakesController, FakeKit]:
    rig = await started(rigs, scenario, **options)
    factory, kit = factory_for(rig, kit, browser=browser)
    controller = factory.takes()
    assert isinstance(controller, TakesController)
    return rig, controller, kit


def ids(controller: TakesController) -> list[int]:
    return [row.id for row in controller.vm.rows]


def row(controller: TakesController, take_id: int) -> Any:
    return next(item for item in controller.vm.rows if item.id == take_id)


def made(take_id: int = 9, **fields: Any) -> Take:
    """A take record for the pure helpers."""
    base = dict(id=take_id, voice="aiko", backend=MTL, language="ja", text=SCRIPT_JA, exaggeration=0.5,
                cfg_weight=0.5, seconds=4.6, gen_seconds=6.9, tokens=115, hit_token_limit=False,
                created="2026-11-13T14:31:58+09:00", session="run", starred=False, wav="outputs/takes/x.wav",
                peaks=tuple(range(230)))
    base.update(fields)
    return Take(**base)


def day_text(at: datetime, today: datetime) -> str:
    return f"{MONTHS[at.month - 1]} {at.day}" if at.year == today.year else str(at.year)


# ---------- the factory and the contract ----------

async def test_the_factory_builds_the_real_controller(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    assert isinstance(takes, TakesControllerProtocol)
    assert factory_for(rig)[0].takes() is not takes                      # one per page load
    assert rig.fake.loads == []


def _shape(func: Callable[..., Any]) -> tuple[bool, list[tuple[str, Any, bool]]]:
    params = list(inspect.signature(func).parameters.values())[1:]
    return inspect.iscoroutinefunction(func), [(p.name, p.kind, p.default is p.empty) for p in params]


def test_every_intent_has_the_protocols_signature() -> None:
    names = {name for name, member in vars(TakesControllerProtocol).items()
             if inspect.isfunction(member) and not name.startswith("_")} | {"confirm", "dismiss"}
    assert names >= {"set_tab", "set_query", "set_voice_filter", "set_model_filter", "row_click", "compare_play",
                     "keep", "reuse_settings", "purge_unstarred", "export_starred", "show_folder", "confirm", "dismiss"}
    for name in names:
        assert _shape(getattr(TakesController, name)) == _shape(getattr(TakesControllerProtocol, name)), name
    assert _shape(TakesController.show_more) == (False, [])                # §6.10 "Show N more" (not in the protocol yet)


# ---------- the speak board ----------

async def test_the_speak_board_lists_this_sessions_takes(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    vm = takes.vm
    assert (vm.tab, vm.list_title, vm.count_text) == ("session", "This Session", "4 takes of 7")
    assert ids(takes) == [7, 6, 5, 4] and vm.more_count == 0 and vm.empty_text == ""
    assert [r.current for r in vm.rows] == [True, False, False, False]
    assert [(r.pin_a, r.pin_b, r.starred) for r in vm.rows] == [
        (False, True, True), (False, False, False), (True, False, False), (False, False, True)]
    assert vm.foot_text == FOOT
    assert (vm.voice_label, vm.model_label) == (ALL_VOICES, ALL_MODELS)
    assert vm.voice_options == ("aiko", "marco", "friend", "narrator", "host-turbo")
    assert vm.model_options == ((MTL, "Multilingual"), (STD, "Standard"), (TUR, "Turbo"))
    assert vm.export == ActionState(True, "Export Starred…", tone="plain")
    assert vm.folder == ActionState(True, "Show Folder", tone="plain")
    assert vm.purge == ActionState(True, "Remove Unstarred", tone="plain")
    assert rig.fake.loads == []


async def test_rows_carry_the_mockups_columns(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    takes.set_tab("all")
    seven, three, one = row(takes, 7), row(takes, 3), row(takes, 1)
    assert (seven.text, seven.lang, seven.voice, seven.model_label) == (SCRIPT_JA, "ja", "aiko", "Multilingual")
    assert (seven.expr_text, seven.guid_text, seven.length_text, seven.when_text) == ("0.50", "0.50", "4.6 s", "14:31")
    stored = rig.core.takes.get(7)
    assert stored is not None and seven.when_tip == created_tip(stored.created)
    yesterday = datetime.fromisoformat(rig.core.takes.get(3).created).astimezone()
    assert three.when_text == day_text(yesterday, datetime.now().astimezone()) and three.lang == "en"
    assert (one.model_label, one.expr_text, one.guid_text) == ("Turbo", "—", "—")
    assert row(takes, 2).model_label == "Standard" and row(takes, 2).guid_text == "0.40"


# ---------- tabs, search, filters ----------

async def test_the_tabs_pick_the_takes(rigs: Callable[..., Rig]) -> None:
    _, takes, _ = await board(rigs)
    takes.set_tab("all")
    assert (takes.vm.tab, takes.vm.list_title, takes.vm.count_text) == ("all", "All Takes", "7 takes")
    assert ids(takes) == [7, 6, 5, 4, 3, 2, 1]
    takes.set_tab("starred")
    assert (takes.vm.list_title, takes.vm.count_text, ids(takes)) == ("Starred Takes", "3 takes of 7", [7, 4, 3])
    takes.set_tab("nonsense")                                           # type: ignore[arg-type]
    assert takes.vm.tab == "starred"


async def test_the_search_applies_150_ms_after_typing_stops(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    takes.set_tab("all")
    takes.set_query("mar")
    assert takes.vm.query == "mar" and ids(takes) == [7, 6, 5, 4, 3, 2, 1]
    rig.clock.advance(0.1)
    takes.set_query("  MARCO ")                                           # typing again restarts the wait
    rig.clock.advance(0.1)
    assert len(ids(takes)) == 7
    rig.clock.advance(0.05)
    assert ids(takes) == [4] and takes.vm.count_text == "1 take of 7"
    takes.set_query("AIKO")                                               # the voice id is searched too
    rig.clock.advance(0.15)
    assert ids(takes) == [7, 6, 5]
    takes.set_query("chapter one")                                        # case-insensitive text
    rig.clock.advance(0.15)
    assert ids(takes) == [2]


async def test_the_menus_filter_by_voice_and_model(rigs: Callable[..., Rig]) -> None:
    _, takes, _ = await board(rigs)
    takes.set_voice_filter("aiko")
    assert (takes.vm.voice_label, takes.vm.count_text, ids(takes)) == ("aiko", "3 takes of 7", [7, 6, 5])
    takes.set_tab("all")
    takes.set_voice_filter(None)
    takes.set_model_filter(STD)
    assert (takes.vm.model_label, ids(takes)) == ("Standard", [2])
    takes.set_model_filter("chatterbox-nonsense")                         # not a model: ignored
    assert takes.vm.model_label == "Standard"
    takes.set_voice_filter("aiko")
    assert ids(takes) == [] and takes.vm.empty_text == EMPTY_FILTERED and takes.vm.count_text == "0 takes of 7"
    takes.set_model_filter(None)
    assert (takes.vm.model_label, ids(takes)) == (ALL_MODELS, [7, 6, 5])


async def test_the_empty_lines(rigs: Callable[..., Rig]) -> None:
    _, first, _ = await board(rigs, "firstrun")
    assert (first.vm.rows, first.vm.empty_text, first.vm.count_text) == ((), EMPTY_NONE, "0 takes")
    assert first.vm.voice_options == () and first.vm.foot_text == "0 starred · 0.0 s of audio"
    assert first.vm.export.enabled is False and first.vm.purge.enabled is False and first.vm.compare is None
    rig, later, _ = await board(rigs, "relaunch", session=False)
    assert later.vm.empty_text == EMPTY_SESSION and later.vm.count_text == "0 takes of 7"
    for take_id in (7, 4, 3):
        rig.core.takes.set_starred(take_id, False)
    rig.core.publish_takes()
    await tick()
    later.set_tab("starred")
    assert later.vm.empty_text == EMPTY_STARRED
    later.set_query("zzz")
    rig.clock.advance(0.15)
    assert later.vm.empty_text == EMPTY_FILTERED


async def test_show_more_reveals_the_next_rows(rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(takes_module, "PAGE_ROWS", 3)
    _, takes, _ = await board(rigs)
    takes.set_tab("all")
    assert (ids(takes), takes.vm.more_count) == ([7, 6, 5], 3)
    takes.show_more()
    assert (ids(takes), takes.vm.more_count) == ([7, 6, 5, 4, 3, 2], 1)
    takes.show_more()
    assert (len(ids(takes)), takes.vm.more_count) == (7, 0)
    takes.show_more()
    assert len(ids(takes)) == 7
    takes.set_tab("all")                                                  # a new list starts at one page again
    assert (len(ids(takes)), takes.vm.count_text) == (3, "7 takes")


# ---------- rows ----------

async def test_a_row_click_plays_and_on_the_current_take_toggles(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    playback = takes._playback
    takes.row_click(6)
    assert rig.core.session.take_id == 6 and row(takes, 6).current and not row(takes, 7).current
    assert playback.vm.target == PlayerTarget(seq=1, src="/media/take/6", playing=True, at=0.0)
    playback.on_player_state(True, 1.0, 4.9, False, "")
    takes.row_click(6)                                                    # the current take: pause (D-T3)
    assert playback.vm.target.playing is False and playback.vm.target.src == "/media/take/6"
    takes.row_click(6)
    assert playback.vm.target.playing is True and playback.vm.target.at is None
    takes.row_click(99)
    assert rig.core.session.take_id == 6 and rig.fake.loads == []


async def test_a_missing_wav_reads_missing_and_a_click_says_so(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    path = rig.core.takes.path_of(6)
    path.unlink()
    assert await asyncio.to_thread(rig.core.takes.check_files) == (6,)
    rig.core.publish_takes()
    await tick()
    six = row(takes, 6)
    assert (six.length_text, six.missing) == ("Missing", True)
    takes.row_click(6)
    assert texts(seen) == [(f"Take 6's WAV is missing: {path}", True)]
    assert rig.core.session.take_id == 7


async def test_the_current_row_follows_the_generating_card(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs, "generating")
    hold = rig.fake.holds[0]
    assert rig.core.jobs.start_generate([request()]).status == "started"
    await entered(hold)
    await tick()
    assert ids(takes) == [7, 6, 5, 4] and not any(r.current for r in takes.vm.rows)   # the card generates
    assert takes.vm.compare is not None and takes.vm.compare.show_ab is False
    hold.release()
    await rig.clock.settle()
    await tick()
    assert ids(takes) == [8, 7, 6, 5, 4] and row(takes, 8).current and row(takes, 8).is_new
    assert takes.vm.compare.show_ab is True and takes.vm.count_text == "5 takes of 8"
    rig.clock.advance(1.5)
    assert not row(takes, 8).is_new
    assert rig.fake.loads == []


# ---------- the compare card ----------

async def test_the_compare_card_of_the_speak_board(rigs: Callable[..., Rig]) -> None:
    _, takes, _ = await board(rigs)
    compare = takes.vm.compare
    assert compare is not None
    a, b = compare.sides
    assert (a.side, a.take_id, a.title, a.title_active, a.length_text) == ("A", 5, "A · Take 5", False, "5.2 s")
    assert (b.side, b.take_id, b.title, b.title_active, b.length_text) == ("B", 7, "B · Take 7", True, "4.6 s")
    assert len(a.bars) == len(b.bars) == 110 and max(a.bars + b.bars) == 1.0 and (a.progress, b.progress) == (0, 0)
    assert [(r.label, r.a, r.b, r.differs) for r in compare.rows] == [
        ("Voice", "aiko", "aiko", False), ("Model", "Multilingual", "Multilingual", False),
        ("Language", "Japanese", "Japanese", False), ("Expressiveness", "0.50", "0.50", False),
        ("Guidance", "0.30", "0.50", True), ("Length", "5.2 s", "4.6 s", True)]
    assert (compare.note, compare.note_differs) == ("Differs in guidance", True)
    assert (compare.play_label, compare.playing, compare.time_text, compare.show_ab) == (
        "Play", False, "0:00.0 / 0:04.6", True)
    assert (compare.keep_a.label, compare.keep_b.label, compare.reuse.label) == ("Keep A", "Keep B",
                                                                                  "Reuse A's Settings")


async def test_the_compare_card_needs_two_pins(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    playback = takes._playback
    playback.pin(5, "A")                                                  # unpin A
    await tick()
    assert takes.vm.compare is None
    playback.pin(6, "A")
    await tick()
    assert takes.vm.compare is not None and takes.vm.compare.sides[0].title == "A · Take 6"
    playback.pin(6, "B")                                                  # a take is never both A and B
    await tick()
    assert rig.core.session.pin_a == 0 and takes.vm.compare is None


async def test_compare_play_starts_a_and_then_toggles(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    playback = takes._playback
    playback.pin(6, "B")                                                  # pins 5 and 6; take 7 is current
    await tick()
    compare = takes.vm.compare
    assert (compare.play_label, compare.time_text) == ("Play A", "0:00.0 / 0:05.2")
    takes.compare_play()
    assert rig.core.session.take_id == 5
    assert playback.vm.target == PlayerTarget(seq=1, src="/media/take/5", playing=True, at=0.0)
    playback.on_player_state(True, 2.6, 5.2, False, "")
    compare = takes.vm.compare
    assert (compare.play_label, compare.playing, compare.time_text) == ("Pause", True, "0:02.6 / 0:05.2")
    assert compare.sides[0].title_active and compare.sides[0].progress == 0.5 and compare.sides[1].progress == 0
    takes.compare_play()
    assert playback.vm.target.playing is False and takes.vm.compare.play_label == "Play"
    playback.ab_switch(6)                                                 # A/B keeps the position
    assert takes.vm.compare.sides[1].title_active and takes.vm.compare.time_text == "0:02.6 / 0:04.9"
    assert rig.fake.loads == []


@pytest.mark.parametrize(("b_fields", "note", "differs"), [
    ({}, SAME_SETTINGS, False),
    ({"exaggeration": 0.8}, "Differs in expressiveness", True),
    ({"text": "Hello there."}, "Differs in text", True),
    ({"voice": "marco", "backend": STD, "language": "en", "cfg_weight": 0.3},
     "Differs in voice, model, language and guidance", True),
    ({"voice": "marco", "cfg_weight": 0.3, "text": "Other."}, "Differs in voice, guidance and text", True),
    ({"seconds": 9.9}, SAME_SETTINGS, False),                             # Length is not a setting
])
def test_the_compare_note_also_checks_the_text(b_fields: dict[str, Any], note: str, differs: bool) -> None:
    a, b = made(1), made(2, **b_fields)
    assert compare_note(compare_rows(a, b), a, b) == (note, differs)


def test_compare_bars_scale_to_the_louder_take() -> None:
    loud, quiet = made(1, peaks=(100,) * 230), made(2, peaks=(50,) * 230)
    bars_a, bars_b = compare_bars(loud, quiet)
    assert len(bars_a) == len(bars_b) == 110 and set(bars_a) == {1.0} and set(bars_b) == {0.5}
    tail = made(3, peaks=(0,) * 229 + (80,))                             # max-pooling keeps the last peak
    assert compare_bars(tail, quiet)[0][-1] == round(80 / 80, 3)
    gone, _ = compare_bars(made(4, missing=True), quiet)
    assert gone == () and compare_bars(made(5, peaks=()), made(6, peaks=())) == ((), ())
    silent = compare_bars(made(7, peaks=(0,) * 230), made(8, peaks=(0,) * 230))
    assert silent[0] == (0.0,) * 110


async def test_a_missing_side_reads_missing_and_draws_no_bars(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    rig.core.takes.path_of(5).unlink()
    await asyncio.to_thread(rig.core.takes.check_files)
    rig.core.publish_takes()
    await tick()
    side = takes.vm.compare.sides[0]
    assert (side.title, side.length_text, side.missing, side.bars) == ("A · Take 5", "5.2 s · missing", True, ())


async def test_keep_stars_and_never_unstars(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    takes.keep("A")                                                       # take 5, unstarred
    await rig.settle()
    await tick()
    assert rig.core.takes.get(5).starred and row(takes, 5).starred
    takes.keep("B")                                                       # take 7, already starred
    await rig.settle()
    assert rig.core.takes.get(7).starred
    assert texts(seen) == [("Kept Take 5 · starred", False), ("Kept Take 7 · starred", False)]
    assert takes.vm.foot_text == "4 starred · 27.8 s of audio"


async def test_keep_reports_a_star_it_could_not_save(rigs: Callable[..., Rig],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)

    def refuse(take_id: int, starred: bool) -> Take:
        raise OSError("the disk is full")

    monkeypatch.setattr(rig.core.takes, "set_starred", refuse)
    takes.keep("A")
    await rig.settle()
    assert texts(seen) == [("Couldn't save the star: the disk is full", True)]
    assert not rig.core.takes.get(5).starred


# ---------- Reuse A's Settings ----------

async def test_reuse_sets_the_delivery_from_take_a_and_loads_nothing(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    session = rig.core.session
    session.voice_id, session.language, session.exaggeration, session.cfg_weight = "narrator", "en", 1.2, 0.9
    text = session.text
    assert takes.reuse_settings() == "/"
    assert (session.voice_id, session.language, session.exaggeration, session.cfg_weight) == ("aiko", "ja", 0.5, 0.3)
    assert session.text == text                                           # never the script
    assert texts(seen) == [("Delivery set from Take 5", False)] and rig.fake.loads == []


@pytest.mark.parametrize(("take_id", "voice", "language", "exaggeration", "cfg_weight"), [
    (2, "narrator", "en", 0.5, 0.4),                                      # Standard: English
    (1, "host-turbo", "en", 1.2, 0.9),                                    # Turbo: no Expr or Guid to take
    (4, "marco", "it", 0.5, 0.5),
])
async def test_reuse_per_backend(rigs: Callable[..., Rig], take_id: int, voice: str, language: str,
                                 exaggeration: float, cfg_weight: float) -> None:
    rig, takes, _ = await board(rigs)
    session = rig.core.session
    takes._playback.pin(take_id, "A")
    session.voice_id, session.language, session.exaggeration, session.cfg_weight = "aiko", "ja", 1.2, 0.9
    assert takes.reuse_settings() == "/"
    assert (session.voice_id, session.language, session.exaggeration, session.cfg_weight) == (
        voice, language, exaggeration, cfg_weight)


async def test_reuse_with_a_deleted_voice_keeps_the_voice(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    library = rig.state.library
    library.rows = tuple(r for r in library.rows if r.voice_id != "aiko")
    session = rig.core.session
    session.voice_id, session.language = "marco", "it"
    assert takes.reuse_settings() == "/"
    assert (session.voice_id, session.language, session.cfg_weight) == ("marco", "it", 0.3)
    assert texts(seen) == [("Delivery set from Take 5 · its voice aiko was deleted", False)]


async def test_reuse_without_a_pin_stays(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    takes._playback.pin(5, "A")
    assert takes.reuse_settings() == "/takes" and rig.core.session.voice_id == "aiko"


# ---------- Remove Unstarred ----------

async def test_the_first_click_arms_with_the_captured_count(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    request_ = await takes.purge_unstarred()
    assert request_ == ConfirmRequest(token="", kind="purge_takes", style="arm", primary="Click Again to Remove 4",
                                      danger=True)
    assert takes.vm.purge == ActionState(True, "Click Again to Remove 4", tone="plain", armed=True)
    assert rig.trashed == [] and len(rig.core.takes.takes) == 7


async def test_the_second_click_removes_every_unstarred_take(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    doomed = {take_id: rig.core.takes.path_of(take_id) for take_id in (6, 5, 2, 1)}
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    assert await takes.purge_unstarred() is None
    await tick()
    assert sorted(rig.trashed) == sorted(doomed.values()) and not any(path.exists() for path in doomed.values())
    assert [take.id for take in rig.core.takes.takes] == [7, 4, 3]
    assert texts(seen) == [("Moved 4 unstarred takes to the Recycle Bin", False)]
    assert rig.state.log.lines[-1].endswith("  Moved 4 unstarred takes to the Recycle Bin")
    session = rig.core.session
    assert (session.pin_a, session.pin_b, session.take_id) == (0, 7, 7)   # pin A was take 5
    assert takes.vm.compare is None and takes.vm.foot_text == "3 starred · 10.6 s of audio"
    assert takes.vm.purge == ActionState(False, "Remove Unstarred", tone="plain")
    assert ids(takes) == [7, 4] and rig.fake.loads == []


async def test_the_purge_stops_playback_and_reselects(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    playback = takes._playback
    takes.row_click(6)
    playback.on_player_state(True, 1.0, 4.9, False, "")
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert rig.core.session.take_id == 7                                  # the newest survivor
    assert playback.vm.target.playing is False and playback.vm.playing is False
    takes.row_click(7)
    playback.on_player_state(True, 2.0, 4.6, False, "")
    takes._playback.star(7)                                               # unstar 7, then purge it too
    await rig.settle()
    await tick()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert rig.core.session.take_id == 4 and rig.core.session.pin_b == 0


async def test_the_arm_lasts_3_seconds(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    await takes.purge_unstarred()
    rig.clock.advance(2.9)
    assert takes.vm.purge.label == "Click Again to Remove 4"
    rig.clock.advance(0.2)
    assert takes.vm.purge.label == "Remove Unstarred" and not takes.vm.purge.armed
    assert await takes.purge_unstarred() is not None                      # arms again, removes nothing
    assert rig.trashed == []


async def test_any_takes_change_voids_the_arm(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    await takes.purge_unstarred()
    takes._playback.star(6)                                               # a star changes the takes
    await rig.settle()
    await tick()
    assert takes.vm.purge.label == "Remove Unstarred" and not takes.vm.purge.armed
    assert (await takes.purge_unstarred()).primary == "Click Again to Remove 3"
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert sorted(path.name for path in rig.trashed) == ["aiko_take5.wav", "host-turbo_take1.wav",
                                                         "narrator_take2.wav"]
    assert rig.core.takes.get(6) is not None


async def test_a_tab_or_page_change_voids_the_arm(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    factory = takes._factory
    await takes.purge_unstarred()
    takes.set_tab("all")
    assert not factory.arm.is_armed(ARM_PURGE) and takes.vm.purge.label == "Remove Unstarred"
    await takes.purge_unstarred()
    factory.status().on_route("/")
    assert not factory.arm.is_armed(ARM_PURGE)
    await takes.purge_unstarred()
    takes.dismiss()
    assert not factory.arm.is_armed(ARM_PURGE)
    await takes.purge_unstarred()
    assert rig.trashed == []                                              # every second click armed again


async def test_the_purge_spans_every_session_whatever_the_view(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    takes.set_voice_filter("aiko")                                        # filters never narrow it (Q10)
    assert (await takes.purge_unstarred()).primary == "Click Again to Remove 4"
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert len(rig.trashed) == 4


async def test_a_partial_purge_is_red_and_logs_each_failure(rigs: Callable[..., Rig],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    rig, takes, _ = await board(rigs)
    monkeypatch.setattr(takes_store, "_sleep", lambda _s: None)
    seen = watch_toasts(rig)
    locked = rig.core.takes.path_of(6)
    moved: list[Path] = []

    def trash(path: Path) -> None:
        if Path(path) == locked:
            raise OSError("The file is in use")
        moved.append(Path(path))
        Path(path).unlink()

    rig.core.trash = trash
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert len(moved) == 3 and rig.core.takes.get(6) is not None
    text = "Moved 3 unstarred takes to the Recycle Bin · 1 could not be moved"
    assert texts(seen) == [(text, True)]
    assert rig.state.log.lines[-2].endswith(text)
    assert rig.state.log.lines[-1].endswith("Take 6 couldn't be moved to the Recycle Bin · The file is in use")
    assert takes.vm.purge == ActionState(True, "Remove Unstarred", tone="plain")


async def test_without_a_recycle_bin_the_arm_says_delete(rigs: Callable[..., Rig],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(takes_module, "on_fixed_drive", lambda _anchor: False)
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)
    assert (await takes.purge_unstarred()).primary == "Click Again to Delete 4"
    assert takes.vm.purge.label == "Click Again to Delete 4"
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    assert texts(seen) == [("Deleted 4 unstarred takes", False)]


async def test_a_purge_whose_list_cannot_be_rewritten(rigs: Callable[..., Rig],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    rig, takes, _ = await board(rigs)
    seen = watch_toasts(rig)

    def fail(ids: Any, *, trash: Any) -> Any:
        raise OSError("the takes list couldn't be updated: access denied")

    monkeypatch.setattr(rig.core.takes, "purge_unstarred", fail)
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    reason = "the takes list couldn't be updated: access denied"
    assert texts(seen) == [(f"Couldn't remove the unstarred takes: {reason}", True)]
    assert rig.state.log.lines[-1].endswith(f"Couldn't remove the unstarred takes · {reason}")
    assert takes.vm.purge.label == "Remove Unstarred"


async def test_the_purge_reads_removing_while_it_runs(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    gate = threading.Event()
    original = rig.core.trash

    def slow(path: Path) -> None:
        gate.wait(10)
        original(path)

    rig.core.trash = slow
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    running = asyncio.create_task(takes.purge_unstarred())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if takes.vm.purge.label == "Removing…":
            break
    assert takes.vm.purge == ActionState(False, "Removing…", tone="plain")
    assert await takes.purge_unstarred() is None                          # refused while it runs
    gate.set()
    await running
    assert takes.vm.purge.label == "Remove Unstarred" and len(rig.trashed) == 4


async def test_nothing_unstarred_disables_the_purge(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs, "firstrun")
    assert await takes.purge_unstarred() is None and takes.vm.purge.enabled is False


# ---------- Export Starred… ----------

async def test_export_copies_every_starred_take(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    folder = tmp_path / "export"
    rig, takes, kit = await board(rigs, kit=FakeKit(folder_result=folder))
    seen = watch_toasts(rig)
    takes.set_tab("session")                                              # every session, whatever the tab
    await takes.export_starred()
    documents = Path.home() / "Documents"
    assert kit.called("pick_folder") == [{"title": "Export Starred Takes",
                                          "start_dir": documents if documents.is_dir() else None}]
    assert sorted(p.name for p in folder.iterdir()) == ["aiko_take7.wav", "friend_take3.wav", "marco_take4.wav"]
    assert texts(seen) == [(f"Exported 3 starred takes to {folder}", False)]
    await takes.export_starred()
    assert kit.called("pick_folder")[-1]["start_dir"] == folder           # the last export folder of this run
    assert "aiko_take7 (2).wav" in {p.name for p in folder.iterdir()}
    assert takes.vm.export == ActionState(True, "Export Starred…", tone="plain") and rig.fake.loads == []


async def test_export_counts_missing_takes_and_cancel_does_nothing(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    folder = tmp_path / "export"
    rig, takes, kit = await board(rigs, kit=FakeKit(folder_result=None))
    seen = watch_toasts(rig)
    await takes.export_starred()
    assert len(kit.called("pick_folder")) == 1 and texts(seen) == [] and not folder.exists()
    kit.folder_result = folder
    rig.core.takes.path_of(4).unlink()
    await takes.export_starred()
    assert texts(seen) == [(f"Exported 2 starred takes to {folder} · 1 missing", False)]


async def test_export_failures_are_red(rigs: Callable[..., Rig], tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    rig, takes, kit = await board(rigs, kit=FakeKit(folder_result=tmp_path / "export"))
    seen = watch_toasts(rig)

    def fail(ids: Any, folder: Path) -> ExportResult:
        raise OSError("access denied")

    monkeypatch.setattr(rig.core.takes, "export", fail)
    await takes.export_starred()

    async def broken(**_kwargs: Any) -> Path | None:
        raise RuntimeError("the dialog went away")

    monkeypatch.setattr(kit, "pick_folder", broken)
    await takes.export_starred()
    assert texts(seen) == [("Export failed: access denied", True), ("Export failed: the dialog went away", True)]
    assert takes.vm.export.label == "Export Starred…"


def test_export_text() -> None:
    folder = Path("C:/exports")
    assert export_text(ExportResult(copied=(folder / "a.wav",)), folder) == (f"Exported 1 starred take to {folder}",
                                                                           False)
    assert export_text(ExportResult(failed=((7, "access denied"),)), folder) == ("Export failed: access denied", True)
    two = ExportResult(copied=(folder / "a.wav", folder / "b.wav"), missing=(3,), failed=((4, "busy"),))
    assert export_text(two, folder) == (f"Exported 2 starred takes to {folder} · 1 missing · 1 could not be copied",
                                        True)


async def test_export_reads_exporting_while_it_copies(rigs: Callable[..., Rig], tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    rig, takes, _ = await board(rigs, kit=FakeKit(folder_result=tmp_path / "export"))
    gate = threading.Event()
    original = rig.core.takes.export

    def slow(ids: Any, folder: Path) -> ExportResult:
        gate.wait(10)
        return original(ids, folder)

    monkeypatch.setattr(rig.core.takes, "export", slow)
    running = asyncio.create_task(takes.export_starred())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if takes.vm.export.label == "Exporting…":
            break
    assert takes.vm.export == ActionState(False, "Exporting…", tone="plain")
    await takes.export_starred()                                          # refused while it copies
    gate.set()
    await running
    assert takes.vm.export.label == "Export Starred…" and len(list((tmp_path / "export").iterdir())) == 3


async def test_export_needs_the_app_window_and_a_star(rigs: Callable[..., Rig]) -> None:
    _, browser, kit = await board(rigs, browser=True)
    assert browser.vm.export == ActionState(False, "Export Starred…", reason=NEEDS_WINDOW, tooltip=NEEDS_WINDOW,
                                            tone="plain")
    await browser.export_starred()
    _, first, first_kit = await board(rigs, "firstrun")
    assert first.vm.export.enabled is False
    await first.export_starred()
    assert kit.calls == [] and first_kit.calls == []


# ---------- Show Folder ----------

async def test_show_folder_opens_the_output_folder(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, takes, kit = await board(rigs)
    takes.show_folder()
    assert kit.called("open_folder") == [{"path": rig.paths.root / "outputs" / "takes"}]
    elsewhere = tmp_path / "new takes"
    rig.core.settings.set("core", "takes_dir", str(elsewhere))
    takes.show_folder()
    assert elsewhere.is_dir() and kit.called("open_folder")[-1] == {"path": elsewhere}


async def test_show_folder_failure_is_red(rigs: Callable[..., Rig]) -> None:
    rig, takes, kit = await board(rigs, kit=FakeKit(open_error=NotADirectoryError("Not a folder: X")))
    seen = watch_toasts(rig)
    takes.show_folder()
    folder = rig.paths.root / "outputs" / "takes"
    assert texts(seen) == [(f"Couldn't open {folder}: Not a folder: X", True)]


# ---------- plumbing ----------

async def test_confirm_has_nothing_to_confirm_and_close_drops_the_subscriptions(rigs: Callable[..., Rig]) -> None:
    rig, takes, _ = await board(rigs)
    assert await takes.confirm("any") is None
    takes.close()
    rig.core.takes.set_starred(6, True)
    rig.core.publish_takes()
    await tick()
    assert takes.vm.foot_text == FOOT                                     # no longer follows the core
    takes._factory.arm.press(ARM_PURGE)
    assert takes.vm.purge.label == "Remove Unstarred"


async def test_no_intent_loads_a_model(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, takes, _ = await board(rigs, kit=FakeKit(folder_result=tmp_path / "x"))
    for tab in ("all", "starred", "session"):
        takes.set_tab(tab)                                                # type: ignore[arg-type]
    takes.set_query("aiko")
    rig.clock.advance(0.2)
    takes.set_voice_filter("aiko")
    takes.set_model_filter(MTL)
    takes.row_click(6)
    takes.compare_play()
    takes.keep("A")
    takes.reuse_settings()
    await takes.export_starred()
    takes.show_folder()
    await takes.purge_unstarred()
    rig.clock.advance(0.6)
    await takes.purge_unstarred()
    await rig.settle()
    calls = {call.name for call in rig.fake.calls} & {"make_resident", "load_backend", "unload_backend", "speak",
                                                      "load_voice", "update_voice", "delete_voice"}
    assert rig.fake.loads == [] and calls == set()


# ---------- pure helpers ----------

@pytest.mark.parametrize(("count", "text"), [(0, "0 takes"), (1, "1 take"), (2, "2 takes")])
def test_plural(count: int, text: str) -> None:
    assert plural(count, "take") == text


@pytest.mark.parametrize(("value", "text"), [(4.6, "4.6"), (27.799999999, "27.8"), (2.25, "2.3"), (0.0, "0.0"),
                                             (10.599999999999998, "10.6")])
def test_tenths_round_halves_up(value: float, text: str) -> None:
    assert tenths(value) == text


@pytest.mark.parametrize(("value", "text"), [(None, "—"), (0.5, "0.50"), (0.3, "0.30"), (0.8, "0.80"), (1.25, "1.25")])
def test_knob_text(value: float | None, text: str) -> None:
    assert knob_text(value) == text


def test_when_text() -> None:
    now = datetime(2026, 11, 13, 15, 0).astimezone()
    today = now.replace(hour=14, minute=31, second=58)
    assert when_text(today.isoformat(), now=now) == "14:31"
    assert when_text((now - timedelta(days=2)).isoformat(), now=now) == "Nov 11"
    assert when_text(now.replace(year=2025).isoformat(), now=now) == "2025"
    assert when_text("", now=now) == "—" and when_text("not a date", now=now) == "—"


def test_take_row_of_a_missing_turbo_take() -> None:
    take = made(3, backend=TUR, voice="host-turbo", language="en", exaggeration=None, cfg_weight=None,
                missing=True, starred=True)
    vm = take_row(take, current=True, pin_a=3, pin_b=0, is_new=True)
    assert (vm.model_label, vm.expr_text, vm.guid_text, vm.length_text) == ("Turbo", "—", "—", "Missing")
    assert (vm.current, vm.pin_a, vm.pin_b, vm.starred, vm.missing, vm.is_new) == (True, True, False, True, True, True)


def test_on_fixed_drive(tmp_path: Path) -> None:
    assert on_fixed_drive("") is True
    assert on_fixed_drive(tmp_path.resolve().anchor) is True             # the system drive holds TEMP
