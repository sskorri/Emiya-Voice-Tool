"""VoicesController (§5.8, §6.8): the list, the filter, the details card, the registry edits, the delete arm and the
hand-offs to Forge and Speak, against a real Core on FakeEngine with the manual clock and a fake KitPort.

Cross-page buttons are checked against stubs of the Forge and Speak controllers injected into the factory
(§9); the real round trips are I1's test_flows.py. Nothing here may load a model: every test that acts ends
with no load recorded.
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from ui.behaviours import BehaviourFactory
from ui.behaviours.confirm import ARM_DELETE
from ui.behaviours.voices import (
    BAR_COUNT,
    CLIP_FILTERS,
    DELETE_ARMED,
    EMPTY_FILTERED,
    EMPTY_LIBRARY,
    NO_DETAILS,
    REGISTRY_UNREADABLE,
    VoicesController,
)
from ui.contract.plans import ActionState, ConfirmRequest, ForgeRequest
from ui.contract.routes import Route
from ui.contract.viewmodels import ForgeVM
from ui.contract.viewmodels import VoicesController as VoicesControllerProtocol
from ui.core.facade import BUSY
from ui.core.services.voices import created_text, created_tip

from tests.behaviours import (FakeKit, StubSpeak, advance, factory_for, no_error_logs, offline, started,  # noqa: F401
                              texts, tick, watch_toasts)
from tests.core.test_facade import MTL, STD, TUR, Rig, entered, request, rigs  # noqa: F401

LIBRARY_ORDER = ["aiko", "friend", "marco", "narrator", "host-turbo"]
MODEL_CALLS = {"make_resident", "load_backend", "unload_backend", "speak", "load_voice"}


class StubForge:
    """ForgeController.prefill as P3 builds it (§5.9): records each prefill and returns its route."""

    def __init__(self, route: Route | None = "/forge") -> None:
        self.vm = ForgeVM()
        self.route = route
        self.prefilled: list[tuple[str, str, Path | None]] = []
        self.lists: list[bool | None] = []

    def prefill(self, *, voice_id: str, mode: str, clip_path: Path | None = None) -> Route | None:
        self.prefilled.append((voice_id, mode, clip_path))
        return self.route

    def toggle_language_list(self, open: bool | None = None) -> None:
        self.lists.append(open)


async def voices_on(rigs: Callable[..., Rig], scenario: str = "voices", *, kit: FakeKit | None = None,
                    **options: object) -> tuple[Rig, BehaviourFactory, VoicesController, FakeKit]:
    """A started rig, a factory with a fake KitPort, and its Voices controller with the details bars read."""
    rig = await started(rigs, scenario, **options)
    factory, kit = factory_for(rig, kit)
    voices = factory.voices()
    assert isinstance(voices, VoicesController)
    await rig.settle()
    return rig, factory, voices, kit


def calls(rig: Rig) -> set[str]:
    return {call.name for call in rig.fake.calls}


def no_model_calls(rig: Rig) -> bool:
    return rig.fake.loads == [] and not calls(rig) & MODEL_CALLS


def flashes(rig: Rig) -> list[str]:
    seen: list[str] = []
    rig.core.events.flash.subscribe(seen.append)
    return seen


def row_of(rig: Rig, voice_id: str):  # noqa: ANN201 - a VoiceRow
    return next(row for row in rig.state.library.rows if row.voice_id == voice_id)


async def forge_of(rig: Rig, voice_id: str = "aiko") -> None:
    """A forge job of voice_id on the resident Multilingual model, held at its bake."""
    hold = rig.fake.hold("load_voice")
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / "sample.wav")
    request = ForgeRequest(voice_id=voice_id, clip=clip, backend=MTL, language="ja", exaggeration=0.5,
                           consent=True, replace=True)
    assert rig.core.jobs.start_forge(request).status == "started"
    await entered(hold)
    await tick()


# ---------- the list ----------

async def test_the_list_follows_the_library_snapshot(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    vm = voices.vm
    assert [row.voice_id for row in vm.rows] == LIBRARY_ORDER            # by backend, then name ignoring case
    assert vm.count_text == "5 voices · 3 Multilingual · 1 Standard · 1 Turbo"
    assert vm.segments == (("", "All", True), (MTL, "Multilingual", False), (STD, "Standard", False),
                           (TUR, "Turbo", False))
    assert (vm.query, vm.backend_filter, vm.empty_text) == ("", None, "")
    aiko, narrator, turbo = vm.rows[0], vm.rows[3], vm.rows[4]
    created = row_of(rig, "aiko").created
    assert (aiko.model_label, aiko.model_resident, aiko.model_tip, aiko.language_name, aiko.expr_text,
            aiko.clip_text, aiko.clip_missing, aiko.created_text, aiko.created_tip, aiko.selected) == (
        "Multilingual", True, "Resident in VRAM", "Japanese", "0.50", "11.2 s", False, created_text(created),
        created_tip(created), True)
    assert (narrator.model_label, narrator.model_resident, narrator.model_tip) == (
        "Standard", False, "Not loaded · about 12 s to load")
    assert (turbo.model_label, turbo.model_tip, turbo.language_name, turbo.expr_text, turbo.clip_text) == (
        "Turbo", "Not loaded · about 9 s to load", "English", "tags", "8.3 s")
    assert no_model_calls(rig)


async def test_the_count_line(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    voices.set_filter(query="o")
    assert voices.vm.count_text == "4 of 5 voices"                       # all but "friend English"
    voices.set_filter(query="")
    library = rig.state.library
    library.rows = library.rows[:1]
    voices.refresh()
    assert voices.vm.count_text == "1 voice · 1 Multilingual · 0 Standard · 0 Turbo"
    voices.set_filter(backend=TUR)
    assert voices.vm.count_text == "0 of 1 voice"


async def test_the_filter_matches_name_and_language_and_the_segments_the_backend(rigs: Callable[..., Rig]) -> None:
    _, _, voices, _ = await voices_on(rigs)
    vm = voices.vm
    voices.set_filter(query="  JAP ")
    assert [row.voice_id for row in vm.rows] == ["aiko"] and vm.count_text == "1 of 5 voices"
    voices.set_filter(query="")
    voices.set_filter(backend=TUR)
    assert [row.voice_id for row in vm.rows] == ["host-turbo"] and vm.backend_filter == TUR
    assert vm.segments[3] == (TUR, "Turbo", True) and vm.segments[0] == ("", "All", False)
    voices.set_filter(query="english")
    assert [row.voice_id for row in vm.rows] == ["host-turbo"]           # both filters apply
    voices.set_filter(query="zzz")
    assert (vm.rows, vm.empty_text) == ((), EMPTY_FILTERED) and vm.empty_text == "No voices match the filter"
    voices.set_filter(backend="chatterbox-nope")                         # not a segment: refused
    assert vm.backend_filter == TUR
    voices.set_filter()                                                  # no arguments: back to All
    assert vm.backend_filter is None and vm.query == "zzz"
    voices.set_filter(query="", backend="")
    assert [row.voice_id for row in vm.rows] == LIBRARY_ORDER


async def test_the_details_fall_back_and_a_selected_voice_stays(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    vm = voices.vm
    assert vm.details is not None and vm.details.voice_id == "aiko"      # no selection: the first row (M L2716)
    voices.set_filter(backend=STD)
    assert vm.details.voice_id == "narrator" and vm.rows[0].selected
    voices.set_filter(backend="")
    voices.select("marco")
    voices.set_filter(backend=TUR)                                       # the filter hides marco's row
    assert vm.details.voice_id == "marco" and [row.selected for row in vm.rows] == [False]
    voices.set_filter(query="zzz", backend="")
    assert vm.details.voice_id == "marco" and vm.rows == ()
    voices.select("ghost")                                               # unknown voices are refused
    assert vm.details.voice_id == "marco"
    library = rig.state.library
    library.rows = tuple(row for row in library.rows if row.voice_id != "marco")
    voices.set_filter(query="")
    assert vm.details.voice_id == "aiko"                                 # a vanished voice falls back


# ---------- the details card ----------

async def test_the_details_of_a_multilingual_voice(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    d = voices.vm.details
    row = row_of(rig, "aiko")
    when = created_text(row.created)
    assert (d.title, d.caption) == ("aiko", f"Multilingual · Japanese · forged {when}")
    assert (d.chip_text, d.chip_tone) == ("Resident", "gold")
    assert d.play == ActionState(True, "Play the reference clip of aiko", aria="Play the reference clip of aiko",
                                 tone="plain")
    assert d.playing is False
    assert d.bars_aria == "Reference clip aiko_ref.wav, 11.2 s · the first 10 s set the timbre"
    assert (d.language_mode, d.language_name, d.lang_open) == ("pick", "Japanese", False)
    assert (d.expr_enabled, d.expr, d.expr_text) == (True, 0.5, "0.50")
    assert (d.ref_text, d.ref_tip, d.ref_missing) == ("aiko_ref.wav · 11.2 s used", str(row.reference_path), False)
    assert (d.cache_text, d.cache_missing, d.reveal_enabled) == ("cache/chatterbox-multilingual/aiko.pt", False, True)
    assert (d.consent_ok, d.consent_text, d.edits_enabled) == (True, "Confirmed when forged", True)
    assert d.speak_label == "Speak with aiko"
    assert d.reforge == ActionState(True, "Re-forge…", tone="plain")
    assert (d.copy, d.copy_visible) == (ActionState(True, "Forge on Turbo…", tone="plain"), True)
    assert d.replace == ActionState(True, "Replace…", tone="plain")
    assert d.delete == ActionState(True, "Delete", tone="plain")
    assert voices.vm.no_details_text == ""


async def test_the_details_of_standard_and_turbo_voices(rigs: Callable[..., Rig]) -> None:
    _, _, voices, _ = await voices_on(rigs)
    voices.select("host-turbo")
    d = voices.vm.details
    assert d.caption.startswith("Turbo · English · forged ")
    assert (d.chip_text, d.chip_tone) == ("Loads in ~9 s", "off")
    assert (d.language_mode, d.language_name, d.lang_items) == ("locked", "English", ())
    assert (d.expr_enabled, d.expr_text) == (False, "—")
    assert (d.copy.label, d.copy_visible) == ("Forge on Multilingual…", True)
    voices.select("narrator")
    d = voices.vm.details
    assert (d.chip_text, d.language_mode, d.language_name) == ("Loads in ~12 s", "locked", "English")
    assert (d.expr_enabled, d.expr_text, d.copy.label) == (True, "0.50", "Forge on Turbo…")


async def test_without_load_history_the_chip_and_tips_say_not_loaded(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    rig.core.history.path.unlink()                                       # as on a PC that never loaded a model
    rig.core.history.load()
    rig.core.publish_history()
    await tick()
    voices.select("narrator")
    assert (voices.vm.details.chip_text, voices.vm.details.chip_tone) == ("Not loaded", "off")
    assert voices.vm.rows[3].model_tip == "Not loaded"
    assert voices.vm.rows[0].model_tip == "Resident in VRAM"


async def test_the_bars_are_read_in_a_worker_and_zoned_at_10_s(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "voices")
    factory, _ = factory_for(rig)
    voices = factory.voices()
    assert voices.vm.details.bars == ()                                  # a flat row while the bars load
    await rig.settle()
    bars = voices.vm.details.bars
    assert len(bars) == BAR_COUNT == 138
    assert all(0.0 <= value <= 1.0 for value, _ in bars) and max(value for value, _ in bars) == pytest.approx(1.0)
    clip_s = row_of(rig, "aiko").clip_s
    timbre = [index for index, (_, zone) in enumerate(bars) if zone == "timbre"]
    assert timbre == list(range(len(timbre))) and {zone for _, zone in bars} == {"timbre", "rest"}
    assert len(timbre) == sum(1 for i in range(138) if (i + 0.5) / 138 * clip_s <= 10.0)
    voices.select("marco")                                               # 9.8 s: every bar is timbre
    await rig.settle()
    assert {zone for _, zone in voices.vm.details.bars} == {"timbre"}
    voices.select("aiko")                                                # read once: cached by path and length
    assert voices.vm.details.bars == bars


async def test_the_language_list(rigs: Callable[..., Rig]) -> None:
    _, factory, voices, _ = await voices_on(rigs)
    items = voices.vm.details.lang_items
    assert len(items) == 23 and [item.code for item in items][:3] == ["ja", "en", "it"]   # own, then used
    rest = [item.name for item in items][3:]
    assert rest == sorted(rest) and [item.selected for item in items].count(True) == 1 and items[0].selected
    assert {item.mark for item in items} == {""}
    voices.toggle_language_list()
    assert voices.vm.details.lang_open is True
    voices.toggle_language_list()
    assert voices.vm.details.lang_open is False
    voices.toggle_language_list(True)
    factory.status().on_route("/takes")                                  # navigation closes it
    assert voices.vm.details.lang_open is False
    voices.toggle_language_list(True)
    voices.select("marco")                                               # so does another row
    assert voices.vm.details.lang_open is False
    voices.select("host-turbo")
    voices.toggle_language_list(True)                                    # locked: never opens
    assert voices.vm.details.lang_open is False


# ---------- registry edits ----------

async def test_a_language_edit_writes_the_registry_only(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    token = rig.core.tokens.mint(rig.core.planner.switch_plan(TUR, reason="explicit"))
    voices.toggle_language_list(True)
    await voices.edit("aiko", language="it")
    await rig.settle()
    assert rig.fake.registry.get("aiko")["language"] == "it"
    assert rig.state.log.lines[-1].endswith("  Registry · aiko language set to Italian")
    d = voices.vm.details
    assert (d.language_name, d.lang_open, voices.vm.rows[0].language_name) == ("Italian", False, "Italian")
    assert [call.name for call in rig.fake.calls if call.name == "update_voice"] == ["update_voice"]
    assert rig.core.tokens.peek(token) is None                           # the new snapshot voids tokens
    assert no_model_calls(rig)


async def test_an_expressiveness_edit_is_rounded_and_saved(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    await voices.edit("narrator", exaggeration=0.704)
    await rig.settle()
    assert rig.fake.registry.get("narrator")["exaggeration"] == 0.7
    assert rig.state.log.lines[-1].endswith("  Registry · narrator default expressiveness set to 0.70")
    voices.select("narrator")
    assert (voices.vm.details.expr, voices.vm.details.expr_text, voices.vm.rows[3].expr_text) == (0.7, "0.70",
                                                                                                    "0.70")
    assert no_model_calls(rig)


@pytest.mark.parametrize(("voice", "changes"), [
    ("aiko", {"language": "ja"}),                     # unchanged
    ("aiko", {"exaggeration": 0.5}),
    ("narrator", {"language": "it"}),                 # Standard is English only
    ("host-turbo", {"exaggeration": 0.8}),            # Turbo ignores Expressiveness
    ("aiko", {"language": "xx"}),                     # not one of the 23
    ("aiko", {"exaggeration": 2.5}),                  # outside the slider
    ("aiko", {"exaggeration": float("nan")}),
    ("aiko", {"language": "it", "exaggeration": 9.0}),
    ("ghost", {"language": "it"}),
])
async def test_edits_that_change_nothing_or_are_refused_write_nothing(rigs: Callable[..., Rig], voice: str,
                                                                      changes: dict[str, object]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    rig.fake.reset_records()
    await voices.edit(voice, **changes)
    await rig.settle()
    assert "update_voice" not in calls(rig) and rig.fake.registry.get("aiko")["language"] == "ja"


async def test_an_edit_shows_at_once_and_snaps_back_when_the_save_fails(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    hold = rig.fake.hold("update_voice")
    task = asyncio.create_task(voices.edit("aiko", exaggeration=0.7))
    await entered(hold)
    d = voices.vm.details
    assert (d.expr, d.expr_text, voices.vm.rows[0].expr_text) == (0.7, "0.70", "0.70")
    hold.release()
    await task
    await rig.settle()
    assert rig.fake.registry.get("aiko")["exaggeration"] == 0.7 and voices.vm.details.expr == 0.7
    seen = watch_toasts(rig)
    rig.fake.fail_next("update_voice", PermissionError("The voice registry is locked by another program"))
    await voices.edit("aiko", exaggeration=1.2, language="fr")
    await rig.settle()
    assert texts(seen) == [("Couldn't save aiko: The voice registry is locked by another program", True)]
    d = voices.vm.details
    assert (d.expr, d.expr_text, d.language_name) == (0.7, "0.70", "Japanese")   # the controls snap back
    assert rig.fake.registry.get("aiko")["language"] == "ja"


async def test_edits_run_in_click_order(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    hold = rig.fake.hold("update_voice")
    first = asyncio.create_task(voices.edit("aiko", exaggeration=0.9))
    await entered(hold)
    second = asyncio.create_task(voices.edit("aiko", exaggeration=0.5))
    await tick()
    assert voices.vm.details.expr == 0.5                                  # the latest choice shows
    hold.release()
    await asyncio.gather(first, second)
    await rig.settle()
    assert rig.fake.registry.get("aiko")["exaggeration"] == 0.5 and voices.vm.details.expr == 0.5


async def test_edits_wait_only_for_the_voice_being_forged(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    await forge_of(rig, "aiko")
    d = voices.vm.details
    assert (d.edits_enabled, d.language_mode, d.expr_enabled) == (False, "disabled", False)
    for action in (d.reforge, d.copy, d.replace, d.delete):
        assert (action.enabled, action.reason) == (False, BUSY)
    rig.fake.reset_records()
    await voices.edit("aiko", language="it")
    voices.toggle_language_list(True)
    assert "update_voice" not in calls(rig) and voices.vm.details.lang_open is False
    await voices.edit("marco", language="fr")                            # other voices: allowed while busy
    assert rig.fake.registry.get("marco")["language"] == "fr"
    assert (voices.reforge("marco"), voices.forge_copy("marco"), await voices.replace_clip("marco")) == (
        None, None, None)                                                # no prefill while a forge runs
    rig.fake.release_all()
    await rig.settle()


# ---------- delete ----------

async def test_delete_arms_first_then_trashes_the_pt_and_keeps_the_wav(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    row = row_of(rig, "aiko")
    seen = watch_toasts(rig)
    token = rig.core.tokens.mint(rig.core.planner.switch_plan(TUR, reason="explicit"))
    assert rig.core.session.voice_id == "aiko"
    request = await voices.delete("aiko")
    assert request == ConfirmRequest(token="", kind="delete_voice", style="arm", primary="Click Again to Delete",
                                     danger=True)
    assert voices.vm.details.delete == ActionState(True, DELETE_ARMED, tone="plain", armed=True)
    assert "delete_voice" not in calls(rig) and rig.trashed == []
    rig.clock.advance(0.6)
    assert await voices.delete("aiko") is None
    await rig.settle()
    assert rig.trashed == [row.pt] and not row.pt.exists()               # the .pt went to the injected trash
    assert row.reference_path is not None and row.reference_path.is_file()   # the WAV stays
    assert rig.fake.registry.get("aiko") is None
    assert "aiko" not in [r.voice_id for r in voices.vm.rows] and voices.vm.details.voice_id == "friend"
    assert rig.core.session.voice_id == ""                               # Speak's selection clears (M L1887)
    assert rig.core.tokens.peek(token) is None
    assert rig.state.log.lines[-1].endswith(
        "  Removed aiko · registry entry and cache/chatterbox-multilingual/aiko.pt · the WAV stays")
    assert texts(seen) == [("Deleted aiko · its reference WAV stays in voices/reference", False)]
    assert voices.vm.count_text == "4 voices · 2 Multilingual · 1 Standard · 1 Turbo"
    assert no_model_calls(rig)


async def test_the_delete_arm_expires_after_3_s(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    await voices.delete("aiko")
    await advance(rig, 2.9)
    assert voices.vm.details.delete.label == DELETE_ARMED
    await advance(rig, 0.2)
    assert voices.vm.details.delete == ActionState(True, "Delete", tone="plain")
    assert not factory.arm.is_armed(ARM_DELETE)
    assert await voices.delete("aiko") is not None                       # arms again, deletes nothing
    assert "delete_voice" not in calls(rig)


@pytest.mark.parametrize("void", ["select", "route", "reforge", "copy", "replace", "filter", "dismiss"])
async def test_the_delete_arm_is_voided(rigs: Callable[..., Rig], void: str) -> None:
    rig, factory, voices, _ = await voices_on(rigs, kit=FakeKit(file_result=Path("C:/clips/new.wav")))
    factory.provide("forge", StubForge())
    await voices.delete("aiko")
    if void == "select":
        voices.select("aiko")                                            # even the same row (M L2725)
    elif void == "route":
        factory.status().on_route("/takes")                              # a page change (M L1752)
    elif void == "reforge":
        voices.reforge("aiko")                                           # a prefill (M L1895)
    elif void == "copy":
        voices.forge_copy("aiko")                                        # M L1902
    elif void == "replace":
        await voices.replace_clip("aiko")
    elif void == "filter":
        voices.set_filter(backend=TUR)                                   # the details show another voice
    else:
        voices.dismiss()
    assert not factory.arm.is_armed(ARM_DELETE)
    assert await voices.delete(voices.vm.details.voice_id) is not None   # the next press arms again
    assert "delete_voice" not in calls(rig)


async def test_the_arm_belongs_to_one_voice(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    await voices.delete("aiko")
    voices.set_filter(query="marco")                                     # aiko stays armed but is not shown
    voices.select("marco")
    assert voices.vm.details.delete.label == "Delete"
    assert await voices.delete("marco") is not None                      # re-arms for marco, deletes nothing
    assert factory.arm.scope(ARM_DELETE) == "marco" and "delete_voice" not in calls(rig)


async def test_delete_is_refused_while_any_job_runs(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    rig.fake.hold("unload_backend")
    assert rig.core.jobs.start_free().status == "started"
    await tick()
    assert voices.vm.details.delete == ActionState(False, "Delete", reason=BUSY, tone="plain")
    assert BUSY == "Busy · wait for the current job"
    assert await voices.delete("aiko") is None and not factory.arm.is_armed(ARM_DELETE)
    assert voices.vm.details.reforge.enabled and voices.vm.details.edits_enabled   # only a forge blocks those
    rig.fake.release_all()
    await rig.settle()


async def test_deleting_shows_while_it_runs(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    hold = rig.fake.hold("delete_voice")
    await voices.delete("aiko")
    rig.clock.advance(0.6)
    task = asyncio.create_task(voices.delete("aiko"))
    await entered(hold)
    d = voices.vm.details
    assert d.delete == ActionState(False, "Deleting…", tone="plain") and d.edits_enabled is False
    voices.select("marco")
    assert voices.vm.details.delete == ActionState(False, "Delete", reason=BUSY, tone="plain")   # one at a time
    hold.release()
    await task
    await rig.settle()
    assert voices.vm.details.delete == ActionState(True, "Delete", tone="plain")
    assert rig.fake.registry.get("aiko") is None


async def test_a_failed_delete_changes_nothing(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    seen = watch_toasts(rig)
    rig.fake.fail_next("delete_voice", PermissionError("aiko.pt is open in another program"))
    await voices.delete("aiko")
    rig.clock.advance(0.6)
    await voices.delete("aiko")
    await rig.settle()
    assert texts(seen) == [("Couldn't delete aiko: aiko.pt is open in another program", True)]
    assert rig.fake.registry.get("aiko") is not None and rig.trashed == [] and row_of(rig, "aiko").pt_exists
    assert rig.core.session.voice_id == "aiko" and voices.vm.details.voice_id == "aiko"


async def test_a_voice_whose_files_are_missing_still_deletes(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs, "voices_missing_files")
    seen = watch_toasts(rig)
    await voices.delete("aiko")
    rig.clock.advance(0.6)
    await voices.delete("aiko")
    await rig.settle()
    assert rig.fake.registry.get("aiko") is None and rig.trashed == []   # a missing .pt counts as removed
    assert texts(seen) == [("Deleted aiko · its reference WAV stays in voices/reference", False)]


async def test_a_casefold_twin_keeps_its_pt_and_the_log_says_so(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    rig.fake.registry.register("Aiko", backend=TUR, reference_wav=str(rig.paths.voices / "host_ref.wav"),
                               exaggeration=0.5, language="en")          # cli.py registers case-sensitive keys
    twin = rig.paths.cache / TUR / "Aiko.pt"
    twin.write_text("{}", encoding="utf-8")
    await rig.core.refresh_library()
    await voices.delete("aiko")
    rig.clock.advance(0.6)
    await voices.delete("aiko")
    await rig.settle()
    assert rig.trashed == [row_of(rig, "marco").pt.parent / "aiko.pt"] and twin.is_file()   # D-F5: NTFS shares it
    removed, kept = rig.state.log.lines[-2:]
    assert removed.endswith("  Removed aiko · registry entry and cache/chatterbox-multilingual/aiko.pt · the WAV stays")
    assert kept.endswith("  Kept cache/chatterbox-turbo/aiko.pt · belongs to Aiko")
    assert [row.voice_id for row in voices.vm.rows] == ["friend", "marco", "narrator", "Aiko", "host-turbo"]


async def test_an_entry_removed_meanwhile_is_reported(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    seen = watch_toasts(rig)
    await voices.delete("marco")
    rig.fake.registry.remove("marco")                                    # cli.py removed it in between
    rig.clock.advance(0.6)
    await voices.delete("marco")
    await rig.settle()
    assert texts(seen) == [("Couldn't delete marco: it is not registered", True)]
    assert "marco" not in [row.voice_id for row in voices.vm.rows] and rig.trashed == []


# ---------- hand-offs to Forge and Speak ----------

async def test_reforge_and_copy_prefill_the_forge_draft(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    forge = StubForge()
    factory.provide("forge", forge)
    assert voices.reforge("aiko") == "/forge"
    assert voices.forge_copy("host-turbo") == "/forge"
    assert forge.prefilled == [("aiko", "reforge", None), ("host-turbo", "copy", None)]
    forge.route = None                                                   # Forge refused the prefill
    assert voices.reforge("marco") is None and voices.forge_copy("ghost") is None
    assert forge.prefilled[-1] == ("marco", "reforge", None)
    assert no_model_calls(rig)


async def test_forge_on_is_hidden_when_its_backend_is_not_enabled(rigs: Callable[..., Rig]) -> None:
    _, factory, voices, _ = await voices_on(rigs, enabled=(MTL, STD))
    forge = StubForge()
    factory.provide("forge", forge)
    d = voices.vm.details
    assert (d.copy.label, d.copy_visible) == ("Forge on Turbo…", False)
    assert [segment[0] for segment in voices.vm.segments] == ["", MTL, STD]
    assert voices.forge_copy("aiko") is None and forge.prefilled == []


async def test_replace_picks_a_clip_then_prefills_a_reforge(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, kit = await voices_on(rigs)
    clip = rig.paths.voices / "sample.wav"
    kit.file_result = clip
    forge = StubForge()
    factory.provide("forge", forge)
    assert await voices.replace_clip("aiko") == "/forge"
    assert kit.called("pick_file") == [{"title": "Pick a new clip · it re-forges aiko and replaces its .pt",
                                        "filters": CLIP_FILTERS, "start_dir": rig.paths.voices}]
    assert CLIP_FILTERS == ("Audio clips (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)")
    assert forge.prefilled == [("aiko", "reforge", clip)]
    assert rig.core.settings.get("core", "browse_dir") == str(clip.parent)
    kit.file_result = None                                               # Cancel: nothing changes
    assert await voices.replace_clip("marco") is None and forge.prefilled == [("aiko", "reforge", clip)]
    assert kit.called("pick_file")[-1]["start_dir"] == clip.parent       # the last clip's folder (§4.14)
    assert voices.vm.details.replace.enabled and no_model_calls(rig)


async def test_an_upload_in_browser_mode_is_not_remembered_as_the_clip_folder(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "voices")
    upload = rig.paths.root / "upload" / "new.wav"
    factory, kit = factory_for(rig, FakeKit(file_result=upload), browser=True)
    forge = StubForge()
    factory.provide("forge", forge)
    assert await factory.voices().replace_clip("aiko") == "/forge"
    assert forge.prefilled == [("aiko", "reforge", upload)] and kit.called("pick_file")
    assert rig.core.settings.get("core", "browse_dir") == ""


async def test_a_failing_file_dialog_is_a_red_toast(rigs: Callable[..., Rig]) -> None:
    class BrokenKit(FakeKit):
        async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
            raise ValueError("'Audio, clips' is not a file filter")

    rig, factory, voices, _ = await voices_on(rigs, kit=BrokenKit())
    factory.provide("forge", StubForge())
    seen = watch_toasts(rig)
    assert await voices.replace_clip("aiko") is None
    assert texts(seen) == [("Couldn't open the file dialog: 'Audio, clips' is not a file filter", True)]


async def test_speak_with_picks_the_voice_flashes_it_and_opens_speak(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    seen = flashes(rig)
    assert voices.speak_with("narrator") == "/"
    assert speak.picked == ["narrator"] and rig.core.session.voice_id == "narrator" and seen == ["voice-narrator"]
    toasts = watch_toasts(rig)
    assert voices.speak_with("ghost") == "/voices"
    assert texts(toasts) == [("ghost no longer exists", True)] and speak.picked == ["narrator"]
    assert no_model_calls(rig)


# ---------- files and the clip ----------

async def test_show_in_explorer_reveals_the_pt_or_opens_its_folder(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, kit = await voices_on(rigs)
    await voices.reveal_cache("aiko")
    assert kit.called("reveal") == [{"path": row_of(rig, "aiko").pt}]
    missing, _, missing_voices, missing_kit = await voices_on(rigs, "voices_missing_files")
    assert missing_voices.vm.details.reveal_enabled                      # the model folder is still there
    await missing_voices.reveal_cache("aiko")
    assert missing_kit.called("open_folder") == [{"path": row_of(missing, "aiko").pt.parent}]
    assert missing_kit.called("reveal") == []


async def test_show_in_explorer_is_off_without_the_pt_and_its_folder(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, kit = await voices_on(rigs)
    shutil.rmtree(row_of(rig, "host-turbo").pt.parent)
    await rig.core.refresh_library()
    voices.select("host-turbo")
    d = voices.vm.details
    assert (d.cache_missing, d.reveal_enabled) == (True, False)
    await voices.reveal_cache("host-turbo")
    assert kit.calls == []


async def test_an_explorer_failure_is_a_red_toast(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs, kit=FakeKit(reveal_error=OSError("explorer.exe is not there")))
    seen = watch_toasts(rig)
    await voices.reveal_cache("aiko")
    assert texts(seen) == [("Couldn't show aiko.pt in Explorer: explorer.exe is not there", True)]


async def test_the_play_button_auditions_the_reference_clip(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    playback = factory.playback()
    voices.toggle_clip_playback("aiko")
    assert playback.vm.target.src == rig.core.clip_url("aiko") and playback.is_playing_clip("aiko")
    d = voices.vm.details
    assert d.playing is True and d.play.label == d.play.aria == "Stop the reference clip of aiko"
    voices.toggle_clip_playback("aiko")
    assert voices.vm.details.playing is False and not playback.vm.target.playing
    assert voices.vm.details.play.label == "Play the reference clip of aiko"
    assert no_model_calls(rig)


async def test_missing_files_render(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs, "voices_missing_files")
    aiko = voices.vm.rows[0]
    assert (aiko.voice_id, aiko.clip_text, aiko.clip_missing) == ("aiko", "—", True)
    d = voices.vm.details
    assert (d.ref_text, d.ref_tip, d.ref_missing) == ("aiko_ref.wav · missing", "", True)
    assert (d.cache_text, d.cache_missing) == ("cache/chatterbox-multilingual/aiko.pt", True)
    assert (d.bars, d.bars_aria) == ((), "")
    assert d.play == ActionState(False, "Play the reference clip of aiko", aria="Play the reference clip of aiko",
                                 tone="plain", tooltip="Reference clip missing")
    voices.toggle_clip_playback("aiko")                                  # disabled: nothing plays
    assert factory.playback().vm.target.src == ""
    assert voices.vm.rows[1].clip_missing is False


async def test_a_voice_without_consent_says_so(rigs: Callable[..., Rig]) -> None:
    _, _, voices, _ = await voices_on(rigs, "voices_unconfirmed")
    voices.select("narrator")
    d = voices.vm.details
    assert (d.consent_ok, d.consent_text) == (False, "Not confirmed · re-forge to confirm")
    voices.select("aiko")
    assert voices.vm.details.consent_text == "Confirmed when forged"


# ---------- library states ----------

async def test_the_first_run_library_is_empty(rigs: Callable[..., Rig]) -> None:
    _, _, voices, _ = await voices_on(rigs, "firstrun")
    vm = voices.vm
    assert (vm.rows, vm.count_text, vm.empty_text, vm.details) == ((), "0 voices", EMPTY_LIBRARY, None)
    assert vm.empty_text == "No voices yet · forge one from a reference clip"
    assert vm.no_details_text == NO_DETAILS == "Pick a voice to see its details."
    assert len(vm.segments) == 4


async def test_before_the_first_snapshot_the_page_stays_blank(rigs: Callable[..., Rig]) -> None:
    rig = rigs("voices")
    factory, _ = factory_for(rig)
    vm = factory.voices().vm
    assert rig.state.library.status == "loading"
    assert (vm.rows, vm.count_text, vm.empty_text, vm.details, vm.no_details_text) == ((), "", "", None, "")


async def test_an_unreadable_registry_says_so(rigs: Callable[..., Rig]) -> None:
    rig = rigs("voices")
    rig.fake.fail_next("voice_details", OSError("registry.json is locked"))
    await rig.start()
    factory, _ = factory_for(rig)
    vm = factory.voices().vm
    assert rig.state.library.status == "failed"
    assert (vm.rows, vm.count_text, vm.empty_text, vm.no_details_text) == ((), "", REGISTRY_UNREADABLE, "")
    assert REGISTRY_UNREADABLE == "Couldn't read cache/registry.json · see Log"


async def test_opening_the_page_rereads_a_registry_changed_meanwhile(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    rig.fake.registry.update("marco", language="fr")                     # cli.py edited it
    await rig.settle()
    assert voices.vm.rows[2].language_name == "Italian"                  # no polling
    rig.fake.reset_records()
    voices.opened()
    await rig.settle()
    assert voices.vm.rows[2].language_name == "French" and calls(rig) == {"voice_details"}
    rig.fake.reset_records()
    voices.opened()                                                      # unchanged: no new read
    await rig.settle()
    assert calls(rig) == set()


async def test_edits_are_allowed_while_a_take_generates(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    hold = rig.fake.hold("speak")
    assert rig.core.jobs.start_generate([request()]).status == "started"
    await entered(hold)
    assert voices.vm.details.edits_enabled
    await voices.edit("aiko", exaggeration=0.6)
    assert rig.fake.registry.get("aiko")["exaggeration"] == 0.6
    rig.fake.release_all()
    await rig.settle()


# ---------- plumbing ----------

async def test_close_drops_the_subscriptions(rigs: Callable[..., Rig]) -> None:
    rig, factory, voices, _ = await voices_on(rigs)
    voices.close()
    rig.fake.registry.update("marco", language="fr")
    await rig.core.refresh_library()
    await tick()
    assert voices.vm.rows[2].language_name == "Italian"
    factory.arm.press(ARM_DELETE, scope="aiko")
    assert voices.vm.details.delete.label == "Delete"


async def test_confirm_is_a_no_op_on_this_page(rigs: Callable[..., Rig]) -> None:
    rig, _, voices, _ = await voices_on(rigs)
    assert await voices.confirm("any") is None and no_model_calls(rig)


def test_the_controller_implements_its_protocol() -> None:
    assert VoicesControllerProtocol in VoicesController.__mro__        # issubclass() refuses data protocols
    declared = {name: member for cls in VoicesControllerProtocol.__mro__ if getattr(cls, "_is_protocol", False)
                for name, member in vars(cls).items() if inspect.isfunction(member) and not name.startswith("_")}
    assert {"confirm", "dismiss", "select", "edit", "delete", "speak_with"} <= set(declared)
    for name, member in declared.items():
        mine = getattr(VoicesController, name)
        assert mine is not member, f"{name} is not implemented"
        assert inspect.iscoroutinefunction(mine) == inspect.iscoroutinefunction(member), name
        assert list(inspect.signature(mine).parameters) == list(inspect.signature(member).parameters), name
