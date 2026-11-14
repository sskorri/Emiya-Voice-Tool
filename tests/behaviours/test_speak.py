"""SpeakController (§5.7) on FakeEngine and the manual clock: every intent row, Tables G and S, the Voice panel,
Delivery, the Script card, the Take card and "Takes · This Session" (§6.7) in the boards of §6.13, the Clear
arm, the named Generate token, the low-VRAM ask (D1, D20), queued snapshots (D2), the Japanese gate (D3), the
40 s warning (D16), the launch selection (D15) and the pinned card (D19). Nothing here may load a model unless
a named control or a confirmed ask says so."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from ui.behaviours import BehaviourFactory
from ui.behaviours.confirm import ARM_CLEAR
from ui.behaviours.speak import (CUT_TAKE, CUT_WARNING, SpeakController, from_utf16, has_kana, sentence_count, snap,
                                 stats_text, to_utf16)
from ui.contract.plans import ActionState, ForgeRequest
from ui.contract.records import ClipInfo
from ui.contract.viewmodels import GeneratingRowVM, LangItemVM, TakeRowVM
from ui.contract.viewmodels import SpeakController as SpeakControllerProtocol
from ui.core.fake import SCRIPT_JA, SCRIPT_TURBO, Hold
from ui.core.services.estimates import TakeEstimate

from tests.behaviours import (MTL, STD, TUR, advance, factory_for, no_error_logs, offline, started,  # noqa: F401
                              texts, tick, watch_toasts)
from tests.core.test_facade import Rig, entered, rigs  # noqa: F401

TAGS = ("[laugh]", "[chuckle]", "[cough]", "[sigh]", "[gasp]")
STEADY_SUMMARY = "aiko · Multilingual (resident) · Japanese · Expr 0.50 · Guid 0.50 · conditioning cached"
LONG_JA = SCRIPT_JA * 8                    # 208 characters: about 39 s of audio by the seeded takes


async def speak_on(rigs: Callable[..., Rig], scenario: Any = "speak", *, session: bool = True,
                   **options: Any) -> tuple[Rig, SpeakController, BehaviourFactory]:
    """A started rig with the scenario's Speak draft, a factory and its Speak controller."""
    rig = await started(rigs, scenario, session=session, **options)
    factory, _ = factory_for(rig)
    speak = factory.speak()
    assert isinstance(speak, SpeakController)
    await tick()
    return rig, speak, factory


async def held(rig: Rig, point: str = "speak") -> Hold:
    """Arm a hold at point (released by the rigs fixture at teardown)."""
    return rig.fake.hold(point)


async def settle_held(rig: Rig) -> None:
    """Let everything but the held job finish."""
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    await tick()


def a_clip(rig: Rig) -> ClipInfo:
    return ClipInfo(clip_id="c" * 32, name="sample.wav", source=None, staged=rig.paths.voices / "sample.wav",
                    in_library=True, duration_s=30.0, selection_s=13.42, samplerate=22050, channels=1, fmt="WAV",
                    subtype="PCM_16", peak_dbfs=-8.4, clipped=False, silent=False, peaks=())


def group(speak: SpeakController, label: str) -> Any:
    return next(g for g in speak.vm.groups if g.label == label)


def item(speak: SpeakController, voice_id: str) -> Any:
    return next(row for g in speak.vm.groups for row in g.rows if row.voice_id == voice_id)


# ---------- the pure helpers ----------

@pytest.mark.parametrize(("text", "count"), [
    ("", 0), ("Hello there", 1), ("Hi. Bye.", 2), ("Wait... what?!", 2), (SCRIPT_JA, 3), ("a. b! c", 2)])
def test_sentences_are_runs_of_sentence_ends(text: str, count: int) -> None:
    assert sentence_count(text) == count


def test_the_stats_line() -> None:
    assert stats_text("", None) == ("Empty", "")
    assert stats_text("Hello there", None) == ("11 chars · 1 sentence", "")
    assert stats_text("Hi. Bye.", None) == ("8 chars · 2 sentences", "")
    assert stats_text(SCRIPT_JA, TakeEstimate(made_s=7.0, audio_s=4.9, count=3)) == (
        "26 chars · 3 sentences · ≈ 7 s to generate", "")
    tiny = TakeEstimate(made_s=0.2, audio_s=0.1, count=3)
    assert stats_text("x", tiny)[0] == "1 chars · 1 sentence · ≈ 1 s to generate"       # at least 1 s
    assert stats_text(LONG_JA, TakeEstimate(made_s=56.0, audio_s=39.2, count=3))[1] == CUT_WARNING


def test_kana_snapping_and_utf16_carets() -> None:
    assert has_kana(SCRIPT_JA) and has_kana("ア") and not has_kana("今日") and not has_kana("Hello")
    assert [snap(v, 0.25, 2.0) for v in (0.333, 5, 0.1, "1.27", True, "x", float("nan"))] == [
        0.35, 2.0, 0.25, 1.25, None, None, None]
    assert snap(-1, 0.0, 1.0) == 0.0 and snap(0.3, 0.0, 1.0) == 0.3
    text = "a\U0001F600b"
    assert [to_utf16(text, i) for i in range(4)] == [0, 1, 3, 4]
    assert [from_utf16(text, c) for c in (0, 1, 3, 4, 99)] == [0, 1, 2, 3, 3]


# ---------- the boards (§6.13) ----------

async def test_the_steady_state_board(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    assert isinstance(speak, SpeakControllerProtocol) and factory.speak() is speak
    vm = speak.vm
    assert (vm.library_state, vm.library_message, vm.count_text) == ("ready", "", "5 voices")
    assert [(g.label, g.chip, g.chip_tone, g.enabled) for g in vm.groups] == [
        ("Multilingual", "Resident", "gold", True), ("Standard", "Not loaded · ~12 s", "off", True),
        ("Turbo", "Not loaded · ~9 s", "off", True)]
    assert [(r.voice_id, r.sub, r.sub_tone, r.selected, r.enabled) for r in group(speak, "Multilingual").rows] == [
        ("aiko", "Japanese · 0.50", "gold-dim", True, True), ("friend", "English · 0.50", "muted", False, True),
        ("marco", "Italian · 0.50", "muted", False, True)]
    assert item(speak, "narrator").sub == "English · 0.50" and item(speak, "host-turbo").sub == "English · tags"
    assert item(speak, "aiko").audition_label == "Audition the reference clip of aiko"
    assert all(row.flash_seq == 0 for g in vm.groups for row in g.rows)


async def test_the_steady_state_delivery_and_script(rigs: Callable[..., Rig]) -> None:
    _, speak, _ = await speak_on(rigs)
    vm = speak.vm
    assert vm.delivery_enabled and vm.presets == (("Default", True), ("Slower", False), ("Dramatic", False))
    assert (vm.expr, vm.expr_text, vm.expr_tone, vm.expr_tick, vm.guid, vm.guid_text) == (
        0.5, "0.50", "text", 0.5, 0.5, "0.50")
    assert vm.expr_tip == "How much emotion the voice puts into a line"
    assert (vm.language_mode, vm.language_name, vm.lang_open, vm.show_reset) == ("pick", "Japanese", False, False)
    names = [entry.name for entry in vm.lang_items]
    assert names == ["Japanese", "English", "Italian", "Arabic", "Chinese", "Danish", "Dutch", "Finnish", "French",
                     "German", "Greek", "Hebrew", "Hindi", "Korean", "Malay", "Norwegian", "Polish", "Portuguese",
                     "Russian", "Spanish", "Swahili", "Swedish", "Turkish"]       # the mockup's order (M L1205)
    assert vm.lang_items[0] == LangItemVM("ja", "Japanese", True, "voice default")
    assert [entry.mark for entry in vm.lang_items[1:]] == [""] * 22
    assert (vm.note, vm.note_tone, vm.show_guid0) == ("Applies to the next take.", "muted", False)
    assert (vm.stats_text, vm.stats_warn) == ("26 chars · 3 sentences · ≈ 7 s to generate", "")
    assert (vm.toolbar_mode, vm.lang_chip_text, vm.tags, vm.text_lang) == ("chip", "Japanese · matches text", (), "ja")
    assert vm.clear == ActionState(True, "Clear", tone="plain")
    assert vm.generate == ActionState(True, "Generate", hint=("Ctrl", "Enter"))
    assert vm.generate3 == ActionState(True, "Generate 3 Takes", tone="plain")
    assert vm.show_cancel_queue is False
    assert (vm.summary_text, vm.summary_tone) == (STEADY_SUMMARY, "muted")


async def test_the_steady_state_take_card_and_session(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    card = speak.vm.card
    assert (card.visible, card.generating, card.pinned, card.title) == (True, False, False, "Take 7")
    assert card.caption == "aiko · Japanese · Expr 0.50 · Guid 0.50 · 4.6 s · made in 6.9 s"
    assert (card.caption_warn, card.star_visible, card.starred) == ("", True, True)
    assert card.play == ActionState(True, "Play", tone="plain")
    assert card.save == ActionState(True, "Save As…", tone="plain")
    assert card.time_text == "0:00.0 / 0:04.6"
    assert (card.ab_visible, card.ab_a_label, card.ab_b_label, card.ab_current) == (
        True, "A · Take 5", "B · Take 7", "B")
    wave = card.wave
    assert (wave.take_id, wave.length_s, wave.generating, len(wave.bars), max(wave.bars)) == (7, 4.6, False, 230, 1.0)
    vm = speak.vm
    assert vm.session_visible and vm.generating_row is None
    assert [(r.id, r.current, r.pin_a, r.pin_b, r.starred) for r in vm.session_rows] == [
        (7, True, False, True, True), (6, False, False, False, False), (5, False, True, False, False),
        (4, False, False, False, True)]
    seven, six, four = vm.session_rows[0], vm.session_rows[1], vm.session_rows[3]
    assert (seven.text, seven.lang, seven.voice, seven.model_label) == (SCRIPT_JA, "ja", "aiko", "Multilingual")
    assert (seven.expr_text, seven.guid_text, seven.length_text, seven.missing, seven.is_new) == (
        "0.50", "0.50", "4.6 s", False, False)
    assert (six.expr_text, four.lang, four.voice, four.length_text) == ("0.80", "it", "marco", "3.2 s")
    assert seven.when_text == "14:31" and seven.when_tip.endswith("· 14:31")
    assert all(isinstance(row, TakeRowVM) and row.flash_seq == 0 for row in vm.session_rows)
    assert rig.fake.loads == []


async def test_the_first_run_board(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs, "firstrun")
    vm = speak.vm
    assert (vm.library_state, vm.groups, vm.count_text, vm.show_sample, vm.sample_tip) == ("empty", (), "0 voices",
                                                                                          True, "")
    assert vm.delivery_enabled is False and vm.presets == (("Default", False), ("Slower", False), ("Dramatic", False))
    assert (vm.expr_text, vm.guid_text, vm.expr_tick) == ("—", "—", None)
    assert (vm.language_mode, vm.language_name, vm.lang_items) == ("disabled", "English", ())
    assert (vm.note, vm.show_reset) == ("Forge a voice to shape its delivery.", False)
    assert (vm.stats_text, vm.toolbar_mode, vm.clear.enabled) == ("Empty", "none", False)
    assert vm.generate == ActionState(False, "Forge a Voice First", tone="grey")
    assert vm.generate3.enabled is False
    assert (vm.summary_text, vm.summary_tone) == ("Forge a voice first, then type what it should say.", "muted")
    assert vm.card.visible is False and vm.session_visible is False and vm.session_rows == ()
    assert factory.status().vm.script_dark is True
    speak.set_text("Hello")                                  # the textarea accepts typing
    assert vm.stats_text == "5 chars · 1 sentence" and vm.generate.label == "Forge a Voice First"
    assert rig.fake.loads == []


async def test_use_sample_is_disabled_without_sample_wav(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "firstrun")
    (rig.paths.voices / "sample.wav").unlink()
    speak.refresh()
    assert (speak.vm.show_sample, speak.vm.sample_tip) == (False, "voices/reference/sample.wav is missing")


async def test_a_turbo_voice_while_multilingual_is_resident(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    vm = speak.vm
    assert (vm.generate.label, vm.generate.enabled, vm.generate.hint) == ("Switch to Turbo & Generate", True,
                                                                          ("Ctrl", "Enter"))
    pending = vm.generate.pending
    assert pending is not None and (pending.kind, pending.style) == ("switch_model", "named")
    assert pending.switch is not None and pending.switch.to_backend == TUR
    assert rig.core.tokens.peek(pending.token) is not None
    assert vm.generate3.pending == pending
    assert (vm.generate3.label, vm.generate3.aria, vm.generate3.tooltip) == (
        "Generate 3 Takes", "Switch to Turbo & Generate 3 Takes", "Switch to Turbo & Generate 3 Takes")
    assert (vm.summary_text, vm.summary_tone) == ("Unloads Multilingual (3.4 GB) · loads Turbo, about 9 s", "gold-dim")
    assert (vm.toolbar_mode, vm.tags, vm.text_lang) == ("tags", TAGS, "en")
    assert (vm.delivery_enabled, vm.expr_text, vm.guid_text, vm.expr_tick) == (False, "—", "—", None)
    assert (vm.language_mode, vm.language_name, vm.show_reset) == ("locked", "English", False)
    assert vm.note == "Turbo ignores Expressiveness and Guidance. Shape delivery with tags."
    assert item(speak, "host-turbo").selected and not item(speak, "aiko").selected
    speak.refresh()
    assert speak.vm.generate.pending is not None and speak.vm.generate.pending.token == pending.token  # reused
    assert rig.core.tokens.outstanding == 1 and rig.fake.loads == []


async def test_nothing_resident_reads_load_and_generate(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "relaunch")
    vm = speak.vm
    assert rig.core.session.voice_id == "aiko" and vm.generate == ActionState(False, "Type a Script", tone="grey")
    speak.set_text("こんにちは")
    assert vm.generate.label == "Load Multilingual & Generate · about 14 s" and vm.generate.enabled
    assert (vm.generate3.aria, vm.generate3.tooltip) == ("Load Multilingual & Generate 3 Takes",) * 2
    assert (vm.summary_text, vm.summary_tone) == ("Loads Multilingual, about 14 s · then ≈ 1 s", "gold-dim")
    assert group(speak, "Multilingual").chip == "Not loaded · ~14 s"
    assert rig.fake.loads == []


async def test_without_load_history_the_estimates_drop(rigs: Callable[..., Rig]) -> None:
    rig = rigs("relaunch")
    rig.paths.history.unlink()
    await rig.start()
    factory, _ = factory_for(rig)
    speak = factory.speak()
    speak.set_text("Hello there")
    vm = speak.vm
    assert vm.generate.label == "Load Multilingual & Generate"
    assert vm.summary_text == "Loads Multilingual"                  # no "about", and only 2 Latin takes: no "then"
    assert [g.chip for g in vm.groups] == ["Not loaded", "Not loaded", "Not loaded"]


async def test_the_failure_board(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs, "failure")
    models = factory.models()
    request = models.request_switch(TUR, origin="dock")     # 9.9 GB held elsewhere: the low-VRAM ask
    assert request is not None and request.primary == "Load Anyway"
    await models.confirm(request.token)
    await rig.settle()
    await tick()
    assert (rig.state.engine.failed, rig.state.engine.resident) == (TUR, "")
    vm = speak.vm
    assert [(g.chip, g.chip_tone) for g in vm.groups] == [("Not loaded · ~14 s", "off"), ("Not loaded · ~12 s", "off"),
                                                          ("Failed", "red")]
    assert vm.generate.label == "Load Turbo & Generate · about 9 s" and vm.generate.enabled
    assert vm.summary_text == "Loads Turbo, about 9 s"


# ---------- Table G: what is running ----------

async def test_free_vram_reads_unloading_and_freeing(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    hold = await held(rig, "unload_backend")
    await factory.models().free_vram()
    await entered(hold)
    await tick()
    assert speak.vm.generate == ActionState(False, "Unloading Multilingual…", fill=0.0)
    assert (group(speak, "Multilingual").chip, group(speak, "Multilingual").chip_tone) == ("Freeing…", "gold")
    assert speak.vm.generate3.enabled is True                # enabled: a click is refused with a toast
    hold.release()
    await rig.settle()
    await tick()
    assert speak.vm.generate.label == "Load Multilingual & Generate · about 14 s"


async def test_a_switch_reads_unloading_then_loading(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    evict, load = await held(rig, "evict"), await held(rig, "make_resident")
    assert rig.core.jobs.start_switch(rig.core.tokens.mint(rig.core.planner.switch_plan(TUR, reason="explicit"))
                                      ).status == "started"
    await entered(evict)
    await tick()
    assert speak.vm.generate == ActionState(False, "Unloading Multilingual…", fill=0.0)
    evict.release()
    await entered(load)
    await tick()
    assert speak.vm.generate.label == "Loading Turbo… 1 s of ~9 s" and speak.vm.generate.fill == 0.0
    assert group(speak, "Turbo").chip == "Loading · 1 s" and group(speak, "Multilingual").chip == "Not loaded · ~14 s"
    assert speak.vm.generate3.enabled is True and speak.vm.generate3.pending is None      # it would queue
    assert speak.vm.summary_text == "host-turbo · Turbo · English · tags on · conditioning cached"
    load.release()
    await rig.settle()
    await tick()
    assert speak.vm.generate == ActionState(True, "Generate", hint=("Ctrl", "Enter"))
    assert speak.vm.summary_text == "host-turbo · Turbo (resident) · English · tags on · conditioning cached"


@pytest.mark.parametrize(("share", "label"), [
    (0.5, "Loading Turbo… 4 s of ~9 s"), (1.2, "Loading Turbo… 10 s, usually ~9 s"),
    (2.0, "Loading Turbo… 17 s, usually ~9 s")])
async def test_the_load_progress_copy_on_generate(rigs: Callable[..., Rig], share: float, label: str) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    load = await held(rig, "make_resident")
    rig.core.jobs.start_switch(rig.core.tokens.mint(rig.core.planner.switch_plan(TUR, reason="explicit")))
    await entered(load)
    await advance(rig, round(share * 8.7, 2))
    action = speak.vm.generate
    assert (action.label, action.enabled, action.indeterminate) == (label, False, False)
    assert action.fill == pytest.approx(min(0.95, rig.state.job.elapsed_s / 8.7), abs=1e-3)
    assert group(speak, "Turbo").chip == f"Loading · {label.split('… ')[1].split(' s')[0]} s"


async def test_a_load_without_history_sweeps(rigs: Callable[..., Rig]) -> None:
    rig = rigs("relaunch")
    rig.paths.history.unlink()
    await rig.start()
    factory, _ = factory_for(rig)
    speak = factory.speak()
    load = await held(rig, "make_resident")
    rig.core.jobs.start_switch(rig.core.tokens.mint(rig.core.planner.switch_plan(MTL, reason="explicit")))
    await entered(load)
    await advance(rig, 12.0)
    assert speak.vm.generate == ActionState(False, "Loading Multilingual… 12 s", indeterminate=True)


async def test_a_running_take_reads_generating_with_its_queue(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig)
    await speak.generate(1)
    await entered(hold)
    await advance(rig, 2.2)
    action = speak.vm.generate
    assert (action.label, action.enabled, action.indeterminate) == ("Generating · 2.2 s", False, False)
    assert action.fill is not None                           # three Japanese takes give an estimate
    assert speak.vm.generate3.enabled is True
    await speak.generate(3)
    await tick()
    assert speak.vm.generate.label == "Generating · 2.2 s · 3 queued" and speak.vm.show_cancel_queue
    assert rig.fake.loads == []


async def test_a_test_line_reads_busy_and_is_not_a_take(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig)
    assert rig.core.jobs.start_test_line("aiko").status == "started"
    await entered(hold)
    await tick()
    vm = speak.vm
    assert vm.generate == ActionState(False, "Busy · test line", tone="grey") and vm.generate3.enabled is False
    assert (vm.card.generating, vm.card.title, vm.generating_row) == (False, "Take 7", None)
    assert await speak.generate(1) is None and rig.core.jobs.queued == ()
    assert vm.show_cancel_queue is False


async def test_a_forge_reads_forging(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig, "load_voice")
    assert rig.core.jobs.start_forge(ForgeRequest("rin", a_clip(rig), MTL, "ja", 0.5, True)).status == "started"
    await entered(hold)
    await tick()
    assert speak.vm.generate == ActionState(False, "Forging…", tone="grey")
    assert speak.vm.generate3.enabled is False
    assert await speak.generate(1) is None and rig.core.jobs.queued == ()


# ---------- Table G and S: the draft ----------

async def test_an_empty_or_blank_script_reads_type_a_script(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    for text in ("", "   \n "):
        speak.set_text(text)
        assert speak.vm.generate == ActionState(False, "Type a Script", tone="grey")
        assert speak.vm.generate3.enabled is False
        assert await speak.generate(1) is None
    assert rig.state.job.kind == "" and not [call for call in rig.fake.calls if call.name == "speak"]


async def test_japanese_on_standard_is_blocked(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    speak.pick_voice("narrator")
    speak.set_text(SCRIPT_JA)
    vm = speak.vm
    assert vm.generate == ActionState(False, "Standard Is English Only", tone="grey") and not vm.generate3.enabled
    assert (vm.toolbar_mode, vm.lang_chip_text) == ("bad", "English only · use a Multilingual voice")
    assert (vm.summary_text, vm.summary_tone) == (
        "Standard reads English only · pick a Multilingual voice for Japanese", "bad")
    assert await speak.generate(1) is None and rig.state.job.kind == ""
    speak.set_text("Chapter two.")
    assert (vm.toolbar_mode, vm.lang_chip_text) == ("chip", "English")


async def test_d3_japanese_on_turbo_is_blocked_like_standard(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    speak.set_text("こんにちは [laugh]")
    vm = speak.vm
    assert vm.generate == ActionState(False, "Turbo Is English Only", tone="grey") and not vm.generate3.enabled
    assert (vm.toolbar_mode, vm.lang_chip_text, vm.tags) == ("bad", "English only · use a Multilingual voice", ())
    assert vm.summary_text == "Turbo reads English only · pick a Multilingual voice for Japanese"
    assert vm.generate.pending is None
    assert await speak.generate(1, token="anything") is None and rig.fake.loads == []


async def test_a_missing_pt_reads_reforge_first(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "voices_missing_files")
    vm = speak.vm
    aiko = item(speak, "aiko")
    assert (aiko.sub, aiko.sub_tone) == ("no .pt · re-forge", "warn")
    assert vm.generate == ActionState(False, "Re-forge aiko First", tone="grey") and not vm.generate3.enabled
    assert (vm.summary_text, vm.summary_tone) == (
        "cache/chatterbox-multilingual/aiko.pt is missing · re-forge aiko on Voices", "bad")
    assert await speak.generate(1) is None and rig.state.job.kind == ""


async def test_the_estimate_needs_three_comparable_takes(rigs: Callable[..., Rig]) -> None:
    _, speak, _ = await speak_on(rigs)
    speak.set_text("Hello there")                            # Latin on Multilingual: only takes 3 and 4
    assert speak.vm.stats_text == "11 chars · 1 sentence"
    speak.pick_voice("host-turbo")
    speak.set_text("Okay, okay.")                            # Turbo: one take
    assert speak.vm.stats_text == "11 chars · 1 sentence"


async def test_d16_a_long_script_warns_it_may_be_cut(rigs: Callable[..., Rig]) -> None:
    _, speak, _ = await speak_on(rigs)
    speak.set_text(LONG_JA)
    assert speak.vm.stats_text == "208 chars · 24 sentences · ≈ 56 s to generate"
    assert speak.vm.stats_warn == CUT_WARNING
    speak.set_text(SCRIPT_JA)
    assert speak.vm.stats_warn == ""


# ---------- voice and delivery intents ----------

async def test_pick_voice_takes_the_voices_own_values_and_loads_nothing(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    session = rig.core.session
    speak.set_delivery(exaggeration=1.2, cfg_weight=0.2)
    speak.toggle_language_list(True)
    assert speak.vm.lang_open
    speak.pick_voice("marco")
    assert (session.voice_id, session.language, session.exaggeration, session.cfg_weight) == ("marco", "it", 0.5, 0.5)
    assert speak.vm.lang_open is False and speak.vm.language_name == "Italian"
    assert [entry.name for entry in speak.vm.lang_items][:4] == ["Italian", "English", "Japanese", "Arabic"]
    speak.pick_voice("narrator")
    assert (session.voice_id, session.language) == ("narrator", "en")
    speak.set_text("Okay.")
    speak.pick_voice("host-turbo")                           # another backend: nothing loads
    assert session.voice_id == "host-turbo" and speak.vm.generate.label == "Switch to Turbo & Generate"
    speak.pick_voice("ghost")
    assert session.voice_id == "host-turbo"
    assert rig.fake.loads == [] and not [c for c in rig.fake.calls if c.name in ("make_resident", "speak")]


async def test_a_voice_on_a_backend_that_is_not_enabled(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, enabled=(MTL, STD))
    turbo = group(speak, "Turbo")
    assert (turbo.chip, turbo.chip_tone, turbo.enabled) == ("Not enabled", "off", False)
    assert [row.enabled for row in turbo.rows] == [False]
    speak.pick_voice("host-turbo")
    assert rig.core.session.voice_id == "aiko"
    rig.core.session.voice_id = "host-turbo"                 # a stale draft: treated as no voice
    speak.refresh()
    assert speak.vm.generate.label == "Pick a Voice" and speak.vm.summary_text == "Pick a voice to speak with."


async def test_set_delivery_clamps_snaps_and_refuses(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    session = rig.core.session
    speak.set_delivery(exaggeration=0.333, cfg_weight=1.7)
    assert (session.exaggeration, session.cfg_weight) == (0.35, 1.0)
    speak.set_delivery(exaggeration="loud", cfg_weight=float("nan"))
    assert (session.exaggeration, session.cfg_weight) == (0.35, 1.0)
    speak.set_delivery(language="EN")
    assert session.language == "en" and speak.vm.language_name == "English"
    speak.set_delivery(language="xx")
    assert session.language == "en"
    speak.set_delivery(exaggeration=1.55)
    vm = speak.vm
    assert (vm.expr_text, vm.expr_tone) == ("1.55", "warn")
    assert vm.expr_tip == "High values speed delivery up; lower Guidance to compensate"
    speak.pick_voice("host-turbo")
    speak.set_delivery(exaggeration=1.0, cfg_weight=0.1)     # Turbo ignores both: refused
    assert (session.exaggeration, session.cfg_weight) == (0.5, 0.5)
    speak.pick_voice("narrator")
    speak.set_delivery(language="ja", exaggeration=0.7)      # Standard: Expressiveness yes, language no
    assert (session.language, session.exaggeration) == ("en", 0.7)
    session.voice_id = ""
    speak.set_delivery(exaggeration=1.0)
    assert session.exaggeration == 0.7


async def test_presets_and_reset(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    session = rig.core.session
    speak.apply_preset("Dramatic")
    assert (session.exaggeration, session.cfg_weight) == (0.7, 0.3)
    assert speak.vm.presets == (("Default", False), ("Slower", False), ("Dramatic", True)) and speak.vm.show_reset
    speak.apply_preset("Slower")
    assert speak.vm.presets[1] == ("Slower", True)
    speak.set_delivery(cfg_weight=0.35)
    assert not any(on for _, on in speak.vm.presets)
    speak.set_delivery(language="it")
    speak.reset_delivery()
    assert (session.exaggeration, session.cfg_weight, session.language) == (0.5, 0.5, "ja")
    assert speak.vm.show_reset is False and speak.vm.presets[0] == ("Default", True)
    speak.apply_preset("Loud")                               # type: ignore[arg-type]
    assert (session.exaggeration, session.cfg_weight) == (0.5, 0.5)
    speak.pick_voice("host-turbo")
    speak.apply_preset("Dramatic")
    assert (session.exaggeration, session.cfg_weight) == (0.5, 0.5) and speak.vm.show_reset is False


async def test_another_language_suggests_guidance_zero(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    speak.set_guidance_zero()                                # no mismatch: refused
    assert rig.core.session.cfg_weight == 0.5
    speak.set_delivery(language="en")
    vm = speak.vm
    assert (vm.note, vm.note_tone, vm.show_guid0, vm.show_reset) == (
        "The clip of aiko is Japanese; Guidance 0 reduces accent carry-over.", "gold-dim", True, True)
    assert (vm.toolbar_mode, vm.lang_chip_text) == ("fix", "Text looks Japanese · Use Japanese")
    speak.set_guidance_zero()
    assert rig.core.session.cfg_weight == 0.0 and vm.show_guid0 is False and vm.guid_text == "0.00"
    speak.use_japanese()
    assert rig.core.session.language == "ja" and vm.toolbar_mode == "chip" and vm.note == "Applies to the next take."


async def test_use_japanese_needs_kana_and_a_multilingual_voice(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    speak.set_delivery(language="it")
    speak.set_text("Ciao a tutti")
    speak.use_japanese()
    assert rig.core.session.language == "it" and speak.vm.lang_chip_text == "Italian"
    speak.pick_voice("narrator")
    speak.set_text(SCRIPT_JA)
    speak.use_japanese()
    assert rig.core.session.language == "en"


async def test_the_language_list_opens_only_for_multilingual(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    speak.toggle_language_list()
    assert speak.vm.lang_open
    speak.toggle_language_list()
    assert not speak.vm.lang_open
    speak.toggle_language_list(True)
    speak.set_delivery(language="fr")                        # a pick closes it
    assert not speak.vm.lang_open and rig.core.session.language == "fr"
    assert next(entry for entry in speak.vm.lang_items if entry.code == "fr").selected
    speak.pick_voice("host-turbo")
    speak.toggle_language_list(True)
    assert not speak.vm.lang_open and speak.vm.lang_items == ()


# ---------- the script ----------

async def test_clear_arms_then_empties_and_typing_disarms(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    request = speak.clear_text()
    assert request is not None and (request.kind, request.style, request.primary) == (
        "clear_script", "arm", "Click Again to Clear")
    assert rig.core.session.text == SCRIPT_JA
    assert speak.vm.clear == ActionState(True, "Click Again to Clear", tone="plain", armed=True)
    await advance(rig, 2.9)
    assert speak.clear_text() is None and rig.core.session.text == ""
    assert speak.vm.clear == ActionState(False, "Clear", tone="plain")
    assert speak.clear_text() is None and not factory.arm.is_armed(ARM_CLEAR)      # nothing to clear
    speak.set_text("Hello")
    await advance(rig, 0.6)
    assert speak.clear_text() is not None and factory.arm.is_armed(ARM_CLEAR)
    await advance(rig, 3.1)
    assert speak.vm.clear.armed is False and not factory.arm.is_armed(ARM_CLEAR)
    speak.clear_text()
    speak.set_text("Hello!")                                 # typing voids the arm (M L2987)
    assert speak.vm.clear.label == "Clear"
    assert speak.clear_text() is not None and rig.core.session.text == "Hello!"


async def test_insert_tag_spaces_the_tag_and_returns_the_caret(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    session = rig.core.session
    speak.set_text("Okay okay")
    assert speak.insert_tag("[laugh]", 4) == 12 and session.text == "Okay [laugh] okay"
    assert speak.insert_tag("[sigh]", 0) == 7 and session.text == "[sigh] Okay [laugh] okay"
    assert speak.insert_tag("[gasp]", 10 ** 6) == len("[sigh] Okay [laugh] okay [gasp]")
    assert session.text == "[sigh] Okay [laugh] okay [gasp]"
    speak.set_text("\U0001F600 hi")
    assert speak.insert_tag("[cough]", 2) == 10 and session.text == "\U0001F600 [cough] hi"
    assert speak.insert_tag("[shout]", 0) == 0 and session.text == "\U0001F600 [cough] hi"
    speak.clear_text()
    speak.insert_tag("[chuckle]", 0)
    assert speak.vm.clear.armed is False                     # an insert voids the Clear arm too
    speak.pick_voice("aiko")
    speak.set_text("Hi")
    assert speak.insert_tag("[laugh]", 1) == 1 and session.text == "Hi"        # tags are Turbo only


# ---------- Generate ----------

async def test_generate_on_the_resident_model_starts_a_take(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    assert await speak.generate(1) is None
    await rig.settle()
    await tick()
    take = rig.core.takes.get(8)
    assert take is not None and (take.voice, take.backend, take.language, take.text) == ("aiko", MTL, "ja", SCRIPT_JA)
    assert (take.exaggeration, take.cfg_weight) == (0.5, 0.5)
    call = next(c for c in rig.fake.calls if c.name == "speak")
    assert call.kwargs["language_id"] == "ja" and call.kwargs["require_resident"] is True
    assert rig.fake.loads == [] and rig.core.session.take_id == 8


@pytest.mark.parametrize("drop_queue", [True, False])
async def test_stop_current_take_respects_queue_preference(rigs: Callable[..., Rig], drop_queue: bool) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig)
    await speak.generate(3)
    await entered(hold)
    assert len(rig.core.jobs.queued) == 2
    if not drop_queue:
        rig.core.settings.set("core", "stop_drops_queue", False)
    speak.stop_take()
    hold.release()
    await rig.settle()
    assert not rig.core.jobs.busy and not rig.core.jobs.queued
    assert rig.core.takes.next_id == (8 if drop_queue else 10)


async def test_d2_queued_takes_keep_the_settings_of_their_click(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig)
    await speak.generate(3)
    await entered(hold)
    speak.set_delivery(exaggeration=1.0, language="en")
    speak.set_text("Something else")
    waiting = rig.core.jobs.queued
    assert len(waiting) == 2
    assert {(r.text, r.language, r.exaggeration, r.cfg_weight) for r in waiting} == {(SCRIPT_JA, "ja", 0.5, 0.5)}
    assert speak.vm.card.caption == "Generating · aiko · Japanese · Expr 0.50 · Guid 0.50"


async def test_the_gen_key_queues_while_a_take_runs_and_loads_nothing(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    hold = await held(rig)
    await speak.generate(1)
    await entered(hold)
    await tick()
    assert speak.vm.generate.enabled is False
    rig.fake.reset_records()
    factory.commands().handle("gen", "/")
    await settle_held(rig)
    assert rig.state.job.queued == 1 and len(rig.core.jobs.queued) == 1
    factory.commands().handle("gen3", "/")
    await settle_held(rig)
    assert rig.state.job.queued == 3
    assert rig.fake.loads == [] and not [c for c in rig.fake.calls if c.name == "make_resident"]


async def test_the_queue_holds_three_takes(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    toasts = watch_toasts(rig)
    hold = await held(rig)
    await speak.generate(1)
    await entered(hold)
    await speak.generate(1)
    await speak.generate(3)                                  # one fits, two do not
    assert rig.state.job.queued == 3 and texts(toasts) == [("Queue full · 3 takes waiting", False)]
    await speak.generate(1)                                  # none fits
    assert texts(toasts)[-1] == ("Queue full · 3 takes waiting", True) and rig.state.job.queued == 3


async def test_a_take_for_another_model_is_refused_while_busy(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    toasts = watch_toasts(rig)
    hold = await held(rig)
    await speak.generate(1)
    await entered(hold)
    speak.pick_voice("host-turbo")
    speak.set_text(SCRIPT_TURBO)
    assert await speak.generate(1) is None
    assert texts(toasts) == [("Busy with Multilingual · host-turbo needs Turbo · switch after the queue", True)]
    assert rig.state.job.queued == 0 and rig.fake.loads == []


async def test_a_take_while_freeing_is_refused_with_a_toast(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    toasts = watch_toasts(rig)
    hold = await held(rig, "unload_backend")
    await factory.models().free_vram()
    await entered(hold)
    await speak.generate(3)
    assert texts(toasts)[-1] == ("Busy · wait for the current job", True) and rig.core.jobs.queued == ()


async def test_the_named_generate_switches_then_generates(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    pending = speak.vm.generate.pending
    assert pending is not None and rig.fake.loads == []
    assert await speak.generate(1, pending.token) is None
    await rig.settle()
    await tick()
    assert rig.fake.loads == [TUR] and rig.state.engine.resident == TUR
    take = rig.core.takes.get(8)
    assert take is not None and (take.voice, take.language, take.exaggeration, take.cfg_weight) == (
        "host-turbo", "en", None, None)
    assert speak.vm.generate.pending is None and speak.vm.generate.label == "Generate"


async def test_a_void_token_loads_nothing_and_renews_the_label(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "other_backend")
    first = speak.vm.generate.pending
    assert first is not None
    assert await speak.generate(1, "not-a-token") is None
    assert await speak.generate(1) is None
    rig.core.tokens.void_all()
    assert await speak.generate(1, first.token) is None
    await tick()
    assert rig.state.job.kind == "" and rig.fake.loads == []
    renewed = speak.vm.generate.pending
    assert renewed is not None and renewed.token != first.token and rig.core.tokens.peek(renewed.token) is not None


@pytest.mark.parametrize(("count", "then"), [(1, "Then generates take 8"), (3, "Then generates takes 8-10")])
async def test_low_vram_turns_the_named_generate_into_the_ask(rigs: Callable[..., Rig], count: int,
                                                              then: str) -> None:
    rig, speak, factory = await speak_on(rigs, "lowvram")
    action = speak.vm.generate if count == 1 else speak.vm.generate3
    assert action.pending is not None
    request = await speak.generate(count, action.pending.token)
    assert request is not None and (request.style, request.primary, request.title) == ("ask", "Load Anyway",
                                                                                       "Switch to Turbo")
    shell = factory.status().vm
    assert (shell.ask, shell.ask_then, shell.ask_dont_visible) == (request, then, False)
    assert rig.fake.loads == [] and rig.state.job.kind == ""
    await speak.confirm(request.token)                       # the popover's primary
    await rig.settle()
    await tick()
    assert rig.fake.loads == [TUR] and rig.core.takes.get(7 + count) is not None
    assert rig.core.takes.get(8).voice == "host-turbo"


async def test_a_token_minted_before_vram_ran_low_becomes_the_ask(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs, "other_backend")
    pending = speak.vm.generate.pending
    assert pending is not None
    rig.fake.set_other_gb(8.5)
    await advance(rig, 2.5)                                  # the next device readings see it
    await rig.settle()
    request = await speak.generate(1, pending.token)
    assert request is not None and request.primary == "Load Anyway"
    assert factory.status().vm.ask_then == "Then generates take 8" and rig.fake.loads == []
    speak.dismiss()
    assert factory.status().vm.ask is None


# ---------- the Take card and the session list ----------

async def test_a_generating_take_shows_its_own_snapshot(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    hold = await held(rig)
    await speak.generate(1)
    await entered(hold)
    await tick()
    card = speak.vm.card
    assert (card.visible, card.generating, card.pinned, card.title) == (True, True, False, "Take 8")
    assert card.caption == "Generating · aiko · Japanese · Expr 0.50 · Guid 0.50"
    assert (card.star_visible, card.play.enabled, card.save.enabled, card.time_text, card.ab_visible) == (
        False, False, False, "0:00.0 / 0:00.0", False)
    assert card.wave.generating is True and card.wave.take_id == 0
    vm = speak.vm
    assert vm.generating_row == GeneratingRowVM(8, "aiko", "0.50", "0.50")
    assert vm.session_visible and not any(row.current for row in vm.session_rows)


async def test_d19_the_card_stays_on_the_take_that_plays(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    playback = factory.playback()
    first, second = await held(rig), await held(rig)
    await speak.generate(2)
    await entered(first)
    await tick()
    assert speak.vm.card.generating is True
    first.release()
    await entered(second)
    await tick()
    card = speak.vm.card
    assert (card.generating, card.pinned, card.title, card.play.label) == (False, True, "Take 8", "Pause")
    assert speak.vm.generating_row is not None and speak.vm.generating_row.take_no == 9
    assert [row.current for row in speak.vm.session_rows][:1] == [True]
    playback.toggle()                                         # Space on the pinned card pauses take 8
    assert (speak.vm.card.play.label, speak.vm.card.pinned) == ("Play", True)
    second.release()
    await rig.settle()
    await tick()
    assert (speak.vm.card.title, speak.vm.card.pinned, speak.vm.generating_row) == ("Take 9", False, None)
    assert rig.fake.loads == []


async def test_a_new_take_rises_sparks_and_slides_in(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    before = speak.vm.card.wave
    await speak.generate(1)
    await rig.settle()
    await tick()
    wave = speak.vm.card.wave
    assert wave.take_id == 8 and wave.new_seq == before.new_seq + 1 and wave.spark_seq == before.spark_seq + 1
    assert [(row.id, row.is_new) for row in speak.vm.session_rows][:2] == [(8, True), (7, False)]
    await advance(rig, 1.5)
    assert speak.vm.session_rows[0].is_new is False


async def test_a_turbo_take_and_a_cut_take(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    factory.playback().play_take(1)
    await tick()
    assert speak.vm.card.caption == "host-turbo · English · Expr — · Guid — · 3.0 s · made in 1.7 s"
    speak.set_text("あ" * 260)                                 # about 46 s of speech: past 1000 tokens
    await speak.generate(1)
    await rig.settle()
    await tick()
    take = rig.core.takes.get(8)
    assert take is not None and take.hit_token_limit
    assert speak.vm.card.title == "Take 8" and speak.vm.card.caption_warn == CUT_TAKE


async def test_a_jump_flashes_the_voice_or_take_row_for_a_moment(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    rig.core.events.flash.emit("voice-marco")
    assert item(speak, "marco").flash_seq != 0 and item(speak, "aiko").flash_seq == 0
    rig.core.events.flash.emit("take-6")
    assert next(row for row in speak.vm.session_rows if row.id == 6).flash_seq != 0
    await advance(rig, 1.2)
    assert item(speak, "marco").flash_seq == 0
    assert all(row.flash_seq == 0 for row in speak.vm.session_rows)
    rig.core.events.flash.emit("dock-chatterbox-turbo")       # the dock's own flash is not Speak's
    assert all(row.flash_seq == 0 for g in speak.vm.groups for row in g.rows)


async def test_a_take_whose_wav_is_gone_reads_missing(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    path = rig.core.takes.path_of(6)
    assert path is not None
    path.unlink()
    rig.core.takes.check_files()
    rig.core.publish_takes()
    await tick()
    six = next(row for row in speak.vm.session_rows if row.id == 6)
    assert (six.missing, six.length_text) == (True, "Missing")


async def test_cancel_queue_drops_the_waiting_takes(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    speak.cancel_queue()                                     # nothing waits: nothing happens
    hold = await held(rig)
    await speak.generate(3)
    await entered(hold)
    await tick()
    assert speak.vm.show_cancel_queue and rig.state.job.queued == 2
    speak.cancel_queue()
    await tick()
    assert rig.core.jobs.queued == () and rig.state.job.queued == 0 and not speak.vm.show_cancel_queue
    assert speak.vm.card.caption == "Generating · aiko · Japanese · Expr 0.50 · Guid 0.50"
    assert rig.state.log.lines[-1].endswith("Cancelled 2 queued takes")


async def test_d15_relaunch_selects_the_newest_take_and_its_voice(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs, "relaunch", session=False)
    vm = speak.vm
    assert (rig.core.session.take_id, rig.core.session.voice_id) == (7, "aiko")
    assert vm.generate == ActionState(False, "Type a Script", tone="grey")
    assert (vm.card.visible, vm.card.title, vm.session_visible) == (True, "Take 7", False)
    assert vm.summary_text == "Loads Multilingual, about 14 s" and item(speak, "aiko").selected
    assert rig.fake.loads == [] and rig.state.engine.resident == ""


# ---------- the library states ----------

async def test_before_the_first_snapshot_the_panel_is_blank(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    factory, _ = factory_for(rig)
    speak = factory.speak()
    vm = speak.vm
    assert (vm.library_state, vm.groups) == ("loading", ())
    assert (vm.generate.label, vm.note, vm.summary_text) == ("Pick a Voice", "Pick a voice to shape its delivery.",
                                                             "Pick a voice to speak with.")
    await rig.start()
    await tick()
    assert vm.library_state == "ready" and len(vm.groups) == 3


async def test_an_unreadable_registry_says_so(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    rig.fake.fail_next("voice_details", OSError("the disk is gone"))
    await rig.core.refresh_library()
    await tick()
    assert (speak.vm.library_state, speak.vm.library_message) == (
        "failed", "Couldn't read cache/registry.json · see Log")


async def test_a_corrupt_registry_leaves_the_empty_state_and_a_toast(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    toasts = watch_toasts(rig)
    (rig.paths.cache / "registry.json").write_text("{not json", encoding="utf-8")
    await rig.core.refresh_library()
    await tick()
    assert (speak.vm.library_state, speak.vm.groups) == ("corrupt", ())
    assert speak.vm.show_sample is True
    assert toasts and toasts[-1].bad and "registry.json.corrupt-" in toasts[-1].text


async def test_opening_the_page_rereads_a_registry_changed_outside(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    calls = len([c for c in rig.fake.calls if c.name == "voice_details"])
    speak.opened()
    await rig.settle()
    assert len([c for c in rig.fake.calls if c.name == "voice_details"]) == calls     # unchanged: no read
    entries = json.loads((rig.paths.cache / "registry.json").read_text(encoding="utf-8"))
    entries.pop("narrator")
    (rig.paths.cache / "registry.json").write_text(json.dumps(entries), encoding="utf-8")
    speak.opened()
    await rig.settle()
    await tick()
    assert [g.label for g in speak.vm.groups] == ["Multilingual", "Turbo"]        # an empty group is not listed
    assert speak.vm.count_text == "4 voices"


# ---------- plumbing ----------

async def test_close_drops_the_subscriptions(rigs: Callable[..., Rig]) -> None:
    rig, speak, factory = await speak_on(rigs)
    factory.close()
    rig.core.session.text = "Changed"
    rig.core.events.notify()
    await tick()
    assert speak.vm.stats_text.startswith("26 chars")


async def test_one_voice_reads_one_voice(rigs: Callable[..., Rig]) -> None:
    rig, speak, _ = await speak_on(rigs)
    for voice in ("friend", "marco", "narrator", "host-turbo"):
        rig.fake.registry.remove(voice)
    await rig.core.refresh_library()
    await tick()
    assert speak.vm.count_text == "1 voice" and [g.label for g in speak.vm.groups] == ["Multilingual"]
