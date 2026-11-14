"""ForgeController (§5.9, §6.9) against a real Core on FakeEngine and the manual clock, with a fake KitPort.

Covered: Table F in every forge seed of §6.13 and in the busy states (forging, freeing, loading at 0.5x, 1.2x
and 2x the estimate, generating); the name table and the Fix button; the clip pipeline (empty, reading,
ready, failed, silent, too short, unsupported) with its meta, marks, bars and checks; consent clearing on a
new clip (Q3); the remembered Multilingual language and the 23-language list (Q2); the replace arm (first
press, second press, expiry, voided by any draft change and by leaving Forge); the named switch token on
row 13 in forge_replace_other_backend; the low-VRAM ask; void tokens; the result row (Speak with, Play Test
Line, Forge Another); Browse, drops, uploads, "Use sample.wav" and prefill.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ui.behaviours import BehaviourFactory
from ui.behaviours.forge import (BROWSE_TITLE, CLIP_FILTERS, DROP_BUSY, DROP_ONE, ForgeController, check_name,
                                 clip_bars, dbfs_text, rate_text, suggest_name)
from ui.contract.plans import ForgeRequest
from ui.contract.records import CheckRow
from ui.core.facade import BUSY

from tests.behaviours import (MTL, STD, TUR, FakeKit, StubSpeak, advance, factory_for, no_error_logs,  # noqa: F401
                              offline, started, texts, tick, watch_toasts)
from tests.core.test_facade import Rig, entered, request, rigs  # noqa: F401
from tests.engine.conftest import write_clip

SAMPLE_META = "30.0 s · mono · 22.05 kHz · trimmed to the first 13.4 s"
SAMPLE_CHECKS = (
    ("Length", "Selection 13.4 s of 30.0 s · ideal 6–15 s", "ok"),
    ("Turbo rule", "Over 5 s", "ok"),
    ("Channels", "Mono", "ok"),
    ("Sample rate", "22.05 kHz", "ok"),
    ("Peak", "−8.4 dBFS · no clipping", "ok"),
)
TURBO_NOTE = "Forging loads Turbo · unloads Multilingual (~9 s)"


# ---------- rigs ----------

async def seed_draft(rig: Rig) -> None:
    """The scenario's Forge draft, as the composition root applies it (stage the clip, then the fields)."""
    seed = rig.fake.scenario.forge
    if seed is None:
        return
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / seed.clip)
    draft = rig.core.forge_draft
    draft.clip, draft.clip_state, draft.clip_file, draft.clip_error = clip, "ready", clip.name, ""
    draft.name, draft.backend, draft.language = seed.name, seed.backend, seed.language
    draft.exaggeration, draft.consent = seed.exaggeration, seed.consent
    draft.rev += 1
    rig.core.events.notify()


async def forge_rig(rigs: Callable[..., Rig], scenario: str = "forge_ready", *, kit: FakeKit | None = None,
                    browser: bool = False, **options: Any) -> tuple[Rig, BehaviourFactory, ForgeController, FakeKit]:
    rig = await started(rigs, scenario, **options)
    await seed_draft(rig)
    factory, kit = factory_for(rig, kit, browser=browser)
    forge = factory.forge()
    assert isinstance(forge, ForgeController)
    await tick()
    return rig, factory, forge, kit


async def settled(rig: Rig) -> None:
    await rig.settle()
    await tick()


def label(forge: ForgeController) -> str:
    return forge.vm.forge.label


def load_voice_calls(rig: Rig) -> list[Any]:
    return [call for call in rig.fake.calls if call.name == "load_voice"]


def clip_file(tmp_path: Path, name: str, seconds: float, **options: Any) -> Path:
    return write_clip(tmp_path / "clips" / name, seconds, **options)


async def picked(rig: Rig, forge: ForgeController, path: Path) -> None:
    await forge.pick_clip(path)
    await settled(rig)


# ---------- Table F in the seeds of §6.13 ----------

@pytest.mark.parametrize(("scenario", "text", "enabled", "named"), [
    ("forge", "Confirm Consent First", False, False),
    ("forge_ready", "Forge Voice", True, False),
    ("forge_other_backend", "Switch to Turbo & Forge", True, True),
    ("forge_low_vram", "Switch to Turbo & Forge", True, True),
    ("forge_replace", "Forge Voice", True, False),
    ("forge_replace_other_backend", "Switch to Turbo & Forge", True, True),
    ("forge_taken_other", "Name Taken on Multilingual", False, False),
    ("forge_turbo_short", "Clip is 4.2 s; Turbo needs over 5 s", False, False),
    ("forge_failed", "Forge Voice", True, False),
])
async def test_table_f_in_every_forge_seed(rigs: Callable[..., Rig], scenario: str, text: str, enabled: bool,
                                           named: bool) -> None:
    rig, _, forge, _ = await forge_rig(rigs, scenario)
    action = forge.vm.forge
    assert (action.label, action.enabled) == (text, enabled)
    assert action.tone == ("primary" if enabled else "grey")
    assert (action.pending is not None) == named
    if named:
        assert action.pending.style == "named" and action.pending.kind == "switch_model"
        plan = rig.core.tokens.peek(action.pending.token)
        assert plan is not None and plan.to_backend == TUR
    assert rig.fake.loads == [] and rig.state.job.kind == ""


async def test_the_forge_board_reads_like_the_mockup(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge")
    vm = forge.vm
    assert (vm.clip_state, vm.clip_file, vm.clip_meta, vm.clip_meta_tone) == ("ready", "sample.wav", SAMPLE_META, "muted")
    assert tuple((c.label, c.detail, c.tone) for c in vm.checks) == SAMPLE_CHECKS
    assert vm.clip_note == "Identity uses the whole selection · the first 10 s set the timbre"
    clip = rig.core.forge_draft.clip
    assert [(m.label, round(m.pct, 2)) for m in vm.marks] == [
        ("Prompt 6 s", round(6 / clip.duration_s * 100, 2)), ("Timbre 10 s", round(10 / clip.duration_s * 100, 2))]
    assert vm.sel_pct == pytest.approx(clip.selection_s / clip.duration_s * 100)
    assert len(vm.bars) == 230 and {zone for _, zone in vm.bars} == {"timbre", "selection", "outside"}
    assert (vm.name, vm.name_msg, vm.name_tone, vm.name_bad, vm.fix_label) == (
        "rin", "cache/chatterbox-multilingual/rin.pt", "muted", False, "")
    assert vm.segments == ((MTL, "Multilingual", True), (STD, "Standard", False), (TUR, "Turbo", False))
    assert (vm.model_note, vm.model_note_tone) == ("Multilingual is resident · forging takes about 2 s", "muted")
    assert (vm.lang_locked, vm.language_name, vm.lang_open) == (False, "Japanese", False)
    assert (vm.expr, vm.expr_text, vm.expr_enabled, vm.expr_note) == (0.5, "0.50", True, "Starting value on Speak")
    assert (vm.consent, vm.consent_req_tone, vm.inputs_enabled) == (False, "gold-dim", True)
    assert (vm.play.enabled, vm.play.label, vm.playing) == (True, "Play the reference clip", False)
    assert (vm.browse.enabled, vm.browse.label) == (True, "Browse…")
    assert (vm.forged, vm.tracing, vm.trace_progress) == ("", False, 0.0)


async def test_turbo_hides_the_prompt_mark_locks_english_and_ignores_expressiveness(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_other_backend")
    vm = forge.vm
    assert [m.label for m in vm.marks] == ["Timbre 10 s"]          # Prompt 15 s lies past the 13.4 s selection
    assert (vm.model_note, vm.model_note_tone) == (TURBO_NOTE, "gold-dim")
    assert (vm.lang_locked, vm.language_name, vm.lang_items) == (True, "English", ())
    assert (vm.expr_text, vm.expr_enabled, vm.expr_note) == ("—", False, "Ignored by Turbo")
    assert vm.checks[1] == CheckRow("Turbo rule", "Over 5 s")


async def test_a_short_clip_blocks_turbo_only(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_turbo_short")
    vm = forge.vm
    assert vm.clip_meta == "4.2 s · mono · 24 kHz"
    assert vm.checks[0] == CheckRow("Length", "4.2 s · ideal 6–15 s", "adv")
    assert vm.checks[1] == CheckRow("Turbo rule", "Turbo needs over 5 s", "bad")
    assert vm.checks[4] == CheckRow("Peak", "−6.4 dBFS · no clipping")
    forge.set_backend(MTL)
    assert vm.checks[1] == CheckRow("Turbo rule", "Too short for Turbo", "adv")
    assert label(forge) == "Forge Voice"


async def test_the_name_lines_of_the_seeds(rigs: Callable[..., Rig]) -> None:
    _, _, taken, _ = await forge_rig(rigs, "forge_taken_other")
    assert (taken.vm.name_msg, taken.vm.name_tone) == ("aiko is a Multilingual voice · try aiko-turbo", "warn")
    _, _, replace, _ = await forge_rig(rigs, "forge_replace")
    assert (replace.vm.name_msg, replace.vm.name_tone) == ("aiko already exists · forging replaces it", "warn")


# ---------- rows 1-4: the job slot is busy ----------

async def test_a_bake_shows_its_elapsed_fill_and_trace(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forging")
    await forge.forge()
    await entered(rig.fake.holds[0])
    await tick()
    vm = forge.vm
    assert (label(forge), vm.forge.enabled, vm.forge.fill) == ("Baking conditioning… 0.0 s", False, 0.0)
    await advance(rig, 0.4)
    assert label(forge) == "Baking conditioning… 0.4 s"
    assert vm.forge.fill == pytest.approx(0.4 / 1.9, abs=1e-3) and not vm.forge.indeterminate
    assert vm.tracing is True and vm.trace_progress == pytest.approx(vm.forge.fill)
    assert vm.inputs_enabled is False and vm.browse.enabled is False and vm.browse.reason == BUSY
    rig.fake.release_all()
    await settled(rig)
    assert vm.forged == "rin" and vm.tracing is False


async def test_freeing_reads_unloading(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge, _ = await forge_rig(rigs, "freeing")
    await factory.models().free_vram()
    await entered(rig.fake.holds[0])
    await tick()
    assert (label(forge), forge.vm.forge.enabled, forge.vm.forge.fill) == ("Unloading Multilingual…", False, 0.0)
    rig.fake.release_all()
    await settled(rig)
    assert label(forge) == "Pick a Clip First"


@pytest.mark.parametrize(("share", "text"), [
    (0.5, "Loading Turbo… 4 s of ~9 s"),
    (1.2, "Loading Turbo… 10 s, usually ~9 s"),
    (2.0, "Loading Turbo… 17 s, usually ~9 s"),
])
async def test_a_load_shows_the_shared_progress_copy(rigs: Callable[..., Rig], share: float, text: str) -> None:
    rig, factory, forge, _ = await forge_rig(rigs, "loading")
    factory.models().request_switch(TUR, origin="card")
    await entered(rig.fake.holds[0])
    await advance(rig, round(share * 8.7, 2))
    action = forge.vm.forge
    assert (action.label, action.enabled, action.indeterminate) == (text, False, False)
    assert action.fill == pytest.approx(min(0.95, rig.state.job.elapsed_s / 8.7), abs=1e-3)


async def test_a_take_reads_busy_generating(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "generating")
    rig.core.jobs.start_generate([request()])
    await entered(rig.fake.holds[0])
    await tick()
    assert (label(forge), forge.vm.forge.tone) == ("Busy · generating a take", "grey")


# ---------- the name (§5.9 table) ----------

@pytest.mark.parametrize(("name", "message", "tone", "red", "fix", "button"), [
    ("", "Becomes the file name, for example rin", "muted", False, "", "Name the Voice"),
    ("rin 2!", "Letters, digits, _ and - only (it becomes a file name)", "bad", True, "rin-2", "Fix the Name First"),
    ("a" * 65, "64 characters at most (it becomes a file name)", "bad", False, "a" * 64, "Fix the Name First"),
    ("con", "con is reserved by Windows · pick another name", "bad", False, "", "Fix the Name First"),
    ("LPT9", "LPT9 is reserved by Windows · pick another name", "bad", False, "", "Fix the Name First"),
    ("Aiko", "Aiko and aiko would share one file · use aiko or another name", "warn", False, "aiko",
     "Fix the Name First"),
    ("host-turbo", "host-turbo is a Turbo voice · try host-turbo-multilingual", "warn", False, "",
     "Name Taken on Turbo"),
    ("marco", "marco already exists · forging replaces it", "warn", False, "", "Forge Voice"),
    ("rin", "cache/chatterbox-multilingual/rin.pt", "muted", False, "", "Forge Voice"),
])
async def test_the_name_table(rigs: Callable[..., Rig], name: str, message: str, tone: str, red: bool, fix: str,
                              button: str) -> None:
    _, _, forge, _ = await forge_rig(rigs)
    forge.set_name(name)
    vm = forge.vm
    assert (vm.name, vm.name_msg, vm.name_tone, vm.name_bad) == (name, message, tone, red)
    assert vm.fix_label == (f"Use {fix}" if fix else "")
    assert label(forge) == button


async def test_the_fix_button_sets_the_suggestion(rigs: Callable[..., Rig]) -> None:
    _, _, forge, _ = await forge_rig(rigs)
    forge.set_name("  rin  two!  ")
    forge.fix_name()
    assert forge.vm.name == "rin-two" and forge.vm.name_msg == "cache/chatterbox-multilingual/rin-two.pt"
    forge.set_name("Aiko")
    forge.fix_name()
    assert forge.vm.name == "aiko" and forge.vm.name_msg == "aiko already exists · forging replaces it"
    forge.fix_name()                                                   # nothing to fix: no change
    assert forge.vm.name == "aiko"


def test_the_name_helpers() -> None:
    assert suggest_name(" Rin\tthe  2nd ") == "Rin-the-2nd" and suggest_name("ü") == ""
    assert suggest_name("x" * 80) == "x" * 64
    assert check_name("aux", MTL, ()).verdict == "invalid" and check_name("rin", MTL, ()).verdict == "new"
    assert check_name("nul.pt", MTL, ()).red is True


# ---------- the clip pipeline ----------

async def test_an_empty_draft_asks_for_a_clip(rigs: Callable[..., Rig]) -> None:
    _, _, forge, _ = await forge_rig(rigs, "speak")
    vm = forge.vm
    assert (vm.clip_state, vm.clip_file, vm.clip_meta) == ("empty", "No clip yet",
                                                           "Browse for a WAV of one speaker, 6–15 s")
    assert (vm.bars, vm.marks, vm.checks, vm.clip_note, vm.sel_pct) == ((), (), (), "", 100.0)
    assert vm.play.enabled is False and label(forge) == "Pick a Clip First"
    forge.toggle_clip_playback()
    assert forge._playback.vm.target.src == ""


async def test_use_sample_reads_then_stages_the_selection(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "firstrun")
    assert await forge.use_sample() == "/forge"
    vm = forge.vm
    assert (vm.clip_state, vm.clip_file, vm.clip_meta, vm.clip_meta_tone) == (
        "reading", "sample.wav", "Reading sample.wav…", "muted")
    assert label(forge) == "Reading the Clip…" and vm.play.enabled is False
    await settled(rig)
    clip = rig.core.forge_draft.clip
    assert vm.clip_state == "ready" and vm.clip_meta == SAMPLE_META and clip.selection_s == pytest.approx(13.42, abs=0.02)
    assert (vm.name, vm.consent) == ("sample", False)
    assert (vm.model_note, vm.model_note_tone) == ("Forging loads Multilingual", "gold-dim")   # no load history
    forge.set_consent(True)
    assert label(forge) == "Load Multilingual & Forge" and forge.vm.forge.pending is not None


async def test_use_sample_keeps_a_name_and_is_refused_when_missing_or_forging(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forging")
    toasts = watch_toasts(rig)
    forge.set_name("rin-sample")
    assert await forge.use_sample() == "/forge"
    await settled(rig)
    assert forge.vm.name == "rin-sample"
    forge.set_consent(True)
    await forge.forge()
    await entered(rig.fake.holds[0])
    assert await forge.use_sample() is None
    rig.fake.release_all()
    await settled(rig)
    (rig.paths.voices / "sample.wav").unlink()
    assert await forge.use_sample() is None
    shown = texts(toasts)
    assert shown[0] == (BUSY, True) and shown[-1] == ("voices/reference/sample.wav is missing", True)


@pytest.mark.parametrize(("name", "seconds", "options", "reason"), [
    ("silent.wav", 2.0, {"level": 0.0}, "the clip is silent"),
    ("blip.wav", 0.6, {}, "0.6 s is too short to forge"),
])
async def test_a_clip_that_cannot_be_forged_fails(rigs: Callable[..., Rig], tmp_path: Path, name: str,
                                                  seconds: float, options: dict[str, Any], reason: str) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    await picked(rig, forge, clip_file(tmp_path, name, seconds, **options))
    vm = forge.vm
    assert (vm.clip_state, vm.clip_file, vm.clip_meta, vm.clip_meta_tone) == (
        "failed", name, f"Can't read {name} · {reason}", "bad")
    assert (vm.bars, vm.checks, vm.marks, vm.play.enabled) == ((), (), (), False)
    assert label(forge) == "Pick Another Clip"


async def test_an_unreadable_or_missing_file_fails_with_the_reason(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    notes = tmp_path / "notes.txt"
    notes.write_text("not audio", encoding="utf-8")
    await picked(rig, forge, notes)
    assert forge.vm.clip_meta == "Can't read notes.txt · use WAV, FLAC, OGG or MP3"
    await picked(rig, forge, tmp_path / "gone.wav")
    assert forge.vm.clip_meta == "Can't read gone.wav · file not found"
    assert forge.vm.forge.label == "Pick Another Clip"


async def test_the_checks_of_other_clips(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    await picked(rig, forge, clip_file(tmp_path, "duet.wav", 8.0, sr=44100, channels=2))
    vm = forge.vm
    assert vm.clip_meta == "8.0 s · stereo · 44.1 kHz"
    assert [c.detail for c in vm.checks[:4]] == ["8.0 s · ideal", "Over 5 s", "Stereo · mixed to mono", "44.1 kHz"]
    await picked(rig, forge, clip_file(tmp_path, "phone.wav", 7.0, sr=8000, channels=3))
    assert vm.checks[2] == CheckRow("Channels", "3 channels · mixed to mono")
    assert vm.checks[3] == CheckRow("Sample rate", "8 kHz · below 16 kHz, expect a duller voice", "adv")
    await picked(rig, forge, clip_file(tmp_path, "quiet.wav", 7.0, level=0.0158))
    peak = dbfs_text(rig.core.forge_draft.clip.peak_dbfs)
    assert peak.startswith("−36") and vm.checks[4] == CheckRow("Peak", f"{peak} dBFS · very quiet", "adv")
    await picked(rig, forge, clip_file(tmp_path, "loud.wav", 7.0, level=1.5))
    assert vm.checks[4].detail.endswith("dBFS · clipping") and vm.checks[4].tone == "adv"


def test_the_clip_helpers() -> None:
    assert rate_text(22050) == "22.05 kHz" and rate_text(48000) == "48 kHz" and rate_text(11025) in (
        "11.03 kHz", "11.02 kHz")
    assert dbfs_text(-8.42) == "−8.4" and dbfs_text(-0.04) == "0.0" and dbfs_text(float("-inf")) == "−∞"


async def test_the_bars_follow_the_selection_and_the_timbre(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    clip = rig.core.forge_draft.clip
    bars = clip_bars(clip)
    for index, (value, zone) in enumerate(bars):
        at = (index + 0.5) / len(bars) * clip.duration_s
        expected = "timbre" if at <= 10.0 else "selection" if at <= clip.selection_s else "outside"
        assert zone == expected and value == round(clip.peaks[index], 3)
    assert forge.vm.bars == bars


async def test_the_newest_pick_wins_and_a_replaced_clip_is_discarded(rigs: Callable[..., Rig],
                                                                     tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    old = rig.core.forge_draft.clip
    first = asyncio.ensure_future(forge.pick_clip(clip_file(tmp_path, "first.wav", 7.0)))
    await asyncio.sleep(0)
    second = clip_file(tmp_path, "second.wav", 8.0)
    await forge.pick_clip(second)
    await first
    await settled(rig)
    draft = rig.core.forge_draft
    assert (draft.clip_state, draft.clip.name) == ("ready", "second.wav")
    assert not old.staged.exists() and rig.core.clips.staged_path(old.clip_id) is None
    staged = sorted(path.name for path in rig.paths.staging.glob("*.wav"))
    assert staged == [draft.clip.staged.name]


async def test_a_new_clip_clears_consent_and_the_result(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forged")
    await forge.forge()
    await settled(rig)
    assert forge.vm.forged == "rin" and forge.vm.consent is True
    await picked(rig, forge, rig.paths.voices / "sample.wav")         # the same file: consent stays
    assert forge.vm.consent is True and forge.vm.forged == ""
    await picked(rig, forge, clip_file(tmp_path, "other.wav", 8.0))
    assert forge.vm.consent is False and label(forge) == "Confirm Consent First"


# ---------- model, language, expressiveness, consent ----------

async def test_a_model_pick_never_loads_and_restores_the_multilingual_language(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge")
    forge.set_backend(TUR)
    vm = forge.vm
    assert (vm.lang_locked, vm.language_name, rig.core.forge_draft.language) == (True, "English", "en")
    assert rig.core.settings.get("core", "forge_language") == "ja"            # remembered on leaving Multilingual
    assert vm.segments[2] == (TUR, "Turbo", True) and vm.model_note == TURBO_NOTE
    forge.set_backend(STD)
    forge.set_backend(MTL)
    assert (vm.lang_locked, vm.language_name) == (False, "Japanese")
    forge.set_backend("chatterbox-nope")
    assert rig.core.forge_draft.backend == MTL and rig.fake.loads == [] and rig.state.job.kind == ""


async def test_the_language_list_holds_23_languages_recent_first(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge")
    items = forge.vm.lang_items
    assert len(items) == 23 and [item.code for item in items[:3]] == ["en", "it", "ja"]
    rest = [item.name for item in items[3:]]
    assert rest == sorted(rest) and [item.code for item in items if item.selected] == ["ja"]
    forge.toggle_language_list()
    assert forge.vm.lang_open is True
    forge.set_language("ko")
    assert (forge.vm.lang_open, forge.vm.language_name) == (False, "Korean")
    assert rig.core.settings.get("core", "forge_language") == "ko"
    assert [item.code for item in forge.vm.lang_items[:1]] == ["ko"]
    forge.set_language("xx")
    assert rig.core.forge_draft.language == "ko"
    forge.set_backend(TUR)
    forge.toggle_language_list(True)
    forge.set_language("fr")
    assert forge.vm.lang_open is False and rig.core.forge_draft.language == "en"


async def test_expressiveness_rounds_clamps_and_ignores_turbo(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    forge.set_exaggeration(0.734)
    assert (forge.vm.expr, forge.vm.expr_text) == (0.73, "0.73")
    forge.set_exaggeration(5)
    assert forge.vm.expr == 2.0
    forge.set_exaggeration(float("nan"))
    assert forge.vm.expr == 2.0
    forge.set_backend(TUR)
    forge.set_exaggeration(1.0)
    assert rig.core.forge_draft.exaggeration == 2.0 and forge.vm.expr_text == "—"


async def test_consent_unlocks_the_forge_button(rigs: Callable[..., Rig]) -> None:
    _, _, forge, _ = await forge_rig(rigs, "forge")
    forge.set_consent(True)
    assert (forge.vm.consent, forge.vm.consent_req_tone, label(forge)) == (True, "faint", "Forge Voice")


async def test_inputs_are_refused_while_a_job_runs(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, kit = await forge_rig(rigs, "generating", kit=FakeKit(file_result=Path("x.wav")))
    toasts = watch_toasts(rig)
    rig.core.jobs.start_generate([request()])
    await entered(rig.fake.holds[0])
    await tick()
    before = rig.core.forge_draft.rev
    forge.set_name("busy")
    forge.set_backend(TUR)
    forge.set_consent(True)
    forge.set_exaggeration(1.0)
    forge.toggle_language_list(True)
    await forge.browse()
    await forge.pick_clip(clip_file(tmp_path, "late.wav", 7.0))
    assert rig.core.forge_draft.rev == before and kit.called("pick_file") == []
    assert forge.vm.inputs_enabled is False and forge.vm.lang_open is False
    assert texts(toasts) == [(DROP_BUSY, False)]


# ---------- the replace arm (§5.1, D-F3) ----------

async def test_replacing_arms_first_then_forges(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_replace")
    await forge.forge()
    action = forge.vm.forge
    assert (action.label, action.enabled, action.armed, action.pending) == ("Click Again to Replace", True, True, None)
    assert rig.state.job.kind == "" and load_voice_calls(rig) == []
    await advance(rig, 2.9)
    await forge.forge()
    await settled(rig)
    calls = load_voice_calls(rig)
    assert len(calls) == 1 and calls[0].args[0] == "aiko" and calls[0].kwargs["force"] is True
    assert rig.fake.loads == [] and forge.vm.forged == "aiko"


async def test_the_arm_expires_after_3_s(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_replace")
    await forge.forge()
    await advance(rig, 3.1)
    assert label(forge) == "Forge Voice"
    await forge.forge()
    assert label(forge) == "Click Again to Replace" and rig.state.job.kind == ""


@pytest.mark.parametrize("change", ["expr", "language", "consent", "route"])
async def test_any_draft_change_or_leaving_forge_voids_the_arm(rigs: Callable[..., Rig], change: str) -> None:
    rig, factory, forge, _ = await forge_rig(rigs, "forge_replace")
    await forge.forge()
    if change == "expr":
        forge.set_exaggeration(0.6)
    elif change == "language":
        forge.set_language("it")
    elif change == "consent":
        forge.set_consent(False)
        forge.set_consent(True)
    else:
        factory.status().on_route("/voices")
    assert label(forge) == "Forge Voice"
    await forge.forge()
    assert label(forge) == "Click Again to Replace" and rig.state.job.kind == "" and load_voice_calls(rig) == []


async def test_replacing_on_another_backend_carries_the_switch_token(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_replace_other_backend")
    named = forge.vm.forge
    assert named.label == "Switch to Turbo & Forge"
    await forge.forge(named.pending.token)
    armed = forge.vm.forge
    assert (armed.label, armed.armed, armed.enabled) == ("Click Again to Replace", True, True)
    assert armed.pending is not None and armed.pending.token == named.pending.token
    assert armed.pending.switch.to_backend == TUR and forge.vm.model_note == TURBO_NOTE
    assert rig.fake.loads == [] and rig.state.job.kind == ""
    rig.clock.advance(0.6)
    await forge.forge(armed.pending.token)
    await settled(rig)
    assert rig.fake.loads == [TUR] and rig.state.engine.resident == TUR
    calls = load_voice_calls(rig)
    assert len(calls) == 1 and calls[0].args[0] == "host-turbo" and calls[0].kwargs["backend"] == TUR
    assert forge.vm.forged == "host-turbo"


# ---------- named switches, low VRAM, void tokens ----------

async def test_the_named_button_switches_then_forges(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_other_backend")
    assert await forge.forge(forge.vm.forge.pending.token) is None
    await settled(rig)
    assert rig.fake.loads == [TUR]
    call = load_voice_calls(rig)[0]
    assert (call.args[0], call.kwargs["language"], call.kwargs["exaggeration"], call.kwargs["force"]) == (
        "rin", "en", 0.5, True)
    assert call.kwargs["meta"]["consent"] is True and forge.vm.forged == "rin"


async def test_without_the_named_token_nothing_loads(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_other_backend")
    old = forge.vm.forge.pending.token
    rig.core.tokens.void_all()
    assert await forge.forge(old) is None
    assert await forge.forge(None) is None
    await settled(rig)
    assert rig.fake.loads == [] and rig.state.job.kind == "" and load_voice_calls(rig) == []
    fresh = forge.vm.forge.pending.token
    assert fresh != old and rig.core.tokens.peek(fresh) is not None


async def test_low_vram_asks_load_anyway_then_forges(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge, _ = await forge_rig(rigs, "forge_low_vram")
    status = factory.status()
    req = await forge.forge(forge.vm.forge.pending.token)
    assert req is not None and (req.style, req.primary, req.title) == ("ask", "Load Anyway", "Switch to Turbo")
    assert factory.ask.request == req and factory.ask.dont_visible is False
    assert status.vm.ask_then == "Then forges rin" and status.vm.ask_dont_visible is False
    assert rig.fake.loads == []
    await forge.confirm(req.token)
    await settled(rig)
    assert rig.fake.loads == [TUR] and forge.vm.forged == "rin" and factory.ask.request is None


async def test_dismiss_closes_the_ask(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge, _ = await forge_rig(rigs, "forge_low_vram")
    await forge.forge(forge.vm.forge.pending.token)
    forge.dismiss()
    assert factory.ask.request is None and rig.fake.loads == []


async def test_a_failed_bake_toasts_and_rolls_the_clip_back(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_failed")
    toasts = watch_toasts(rig)
    await forge.forge()
    await settled(rig)
    assert ("Forge failed: CUDA out of memory", True) in texts(toasts)
    assert (label(forge), forge.vm.forged) == ("Forge Voice", "")
    assert not (rig.paths.voices / "rin_ref.wav").exists() and rig.core.forge_draft.clip.staged.exists()
    await forge.forge()
    await settled(rig)
    assert forge.vm.forged == "rin" and (rig.paths.voices / "rin_ref.wav").exists()


async def test_a_forge_reports_a_busy_refusal(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    toasts = watch_toasts(rig)
    draft = rig.core.forge_draft
    rig.core.jobs.start_forge(ForgeRequest("other", draft.clip, MTL, "ja", 0.5, True))
    await forge.forge()
    assert texts(toasts) == [] and rig.state.job.voice == "other"     # busy: the button is disabled (row 1)
    await settled(rig)


# ---------- the result row ----------

async def forged_rig(rigs: Callable[..., Rig]) -> tuple[Rig, BehaviourFactory, ForgeController]:
    rig, factory, forge, _ = await forge_rig(rigs, "forged")
    await forge.forge()
    await settled(rig)
    return rig, factory, forge


async def test_the_result_row_after_a_forge(rigs: Callable[..., Rig]) -> None:
    rig, _, forge = await forged_rig(rigs)
    vm = forge.vm
    assert (vm.forged, vm.speak_label) == ("rin", "Speak with rin")
    assert (vm.test_line.label, vm.test_line.enabled) == ("Play Test Line", True)
    assert (vm.another.label, vm.another.enabled) == ("Forge Another", True)
    assert vm.name_msg == "rin already exists · forging replaces it"


async def test_speak_with_picks_the_voice_and_goes_to_speak(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge = await forged_rig(rigs)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    flashes: list[str] = []
    rig.core.events.flash.subscribe(flashes.append)
    assert forge.speak_with() == "/"
    assert speak.picked == ["rin"] and flashes == ["voice-rin"] and rig.fake.loads == []


async def test_speak_with_a_deleted_voice_is_refused(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge = await forged_rig(rigs)
    toasts = watch_toasts(rig)
    assert (await rig.core.facade.delete_voice("rin")).ok
    await rig.core.refresh_library()
    assert forge.speak_with() is None
    assert texts(toasts) == [("rin no longer exists", True)] and forge.vm.forged == ""


async def test_the_test_line_plays_at_once_then_replays(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge = await forged_rig(rigs)
    playback = factory.playback()
    hold = rig.fake.hold("speak")
    await forge.test_line()
    await entered(hold)
    await tick()
    assert (forge.vm.test_line.label, forge.vm.test_line.enabled) == ("Generating test line…", False)
    hold.release()
    await settled(rig)
    url = "/media/staged/test-rin?v=1"
    assert (playback.vm.target.src, playback.vm.target.playing) == (url, True)
    speaks = len([call for call in rig.fake.calls if call.name == "speak"])
    playback.stop()
    await forge.test_line()
    assert playback.vm.target.src == url and playback.vm.target.playing is True
    assert len([call for call in rig.fake.calls if call.name == "speak"]) == speaks and rig.fake.loads == []
    assert rig.core.takes.next_id == 8                                    # never saved to Takes


async def test_a_new_forge_of_the_name_retires_its_test_line(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge = await forged_rig(rigs)
    await forge.test_line()
    await settled(rig)
    rig.core.events.forged.emit("rin")
    await forge.test_line()
    await settled(rig)
    assert factory.playback().vm.target.src == "/media/staged/test-rin?v=2"


async def test_the_test_line_needs_the_model_resident(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge = await forged_rig(rigs)
    await factory.models().free_vram()
    await settled(rig)
    action = forge.vm.test_line
    assert (action.enabled, action.tooltip) == (False, "Multilingual is not loaded")
    await forge.test_line()
    await settled(rig)
    assert rig.state.job.kind == "" and rig.fake.loads == []


async def test_forge_another_clears_the_draft_but_keeps_the_clip(rigs: Callable[..., Rig]) -> None:
    rig, _, forge = await forged_rig(rigs)
    rig.core.settings.set("core", "default_backend", STD)
    forge.forge_another()
    draft, vm = rig.core.forge_draft, forge.vm
    assert (draft.forged, draft.name, draft.consent, draft.backend) == ("", "", False, STD)
    assert (vm.clip_state, vm.clip_file) == ("ready", "sample.wav") and label(forge) == "Name the Voice"
    forge.set_backend(MTL)
    assert draft.language == "ja"
    forge.set_name("rin2")
    forge.set_consent(True)
    await forge.forge()
    await settled(rig)
    call = load_voice_calls(rig)[-1]
    assert Path(call.args[1]).name == "rin_ref.wav" and vm.forged == "rin2"     # in place, from the library


async def test_forge_another_keeps_the_multilingual_language(rigs: Callable[..., Rig]) -> None:
    rig, _, forge = await forged_rig(rigs)
    assert rig.core.settings.get("core", "default_backend") == MTL
    forge.forge_another()
    draft = rig.core.forge_draft
    assert (draft.backend, draft.language, draft.exaggeration, forge.vm.language_name) == (MTL, "ja", 0.5, "Japanese")


async def test_the_default_model_repicks_an_idle_draft_without_a_result(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    rig.core.settings.set("core", "default_backend", TUR)
    assert rig.core.forge_draft.backend == TUR and forge.vm.lang_locked is True
    _, _, done = await forged_rig(rigs)
    done._core.settings.set("core", "default_backend", STD)
    assert done._core.forge_draft.backend == MTL


# ---------- Browse, drops, uploads ----------

async def test_browse_opens_the_clip_dialog_and_remembers_the_folder(rigs: Callable[..., Rig],
                                                                    tmp_path: Path) -> None:
    clip = clip_file(tmp_path, "picked.wav", 9.0)
    rig, _, forge, kit = await forge_rig(rigs, "forge", kit=FakeKit(file_result=clip))
    await forge.browse()
    await settled(rig)
    call = kit.called("pick_file")[0]
    assert (call["title"], call["filters"], call["start_dir"]) == (BROWSE_TITLE, CLIP_FILTERS, rig.paths.voices)
    assert forge.vm.clip_file == "picked.wav" and forge.vm.clip_meta == "9.0 s · mono · 24 kHz"
    assert rig.core.settings.get("core", "browse_dir") == str(clip.parent)
    kit.file_result = None
    await forge.browse()
    assert kit.called("pick_file")[1]["start_dir"] == clip.parent and forge.vm.clip_file == "picked.wav"


async def test_browse_in_the_browser_does_not_remember_the_upload_folder(rigs: Callable[..., Rig],
                                                                         tmp_path: Path) -> None:
    clip = clip_file(tmp_path, "uploaded.wav", 9.0)
    rig, _, forge, _ = await forge_rig(rigs, kit=FakeKit(file_result=clip), browser=True)
    await forge.browse()
    await settled(rig)
    assert forge.vm.clip_file == "uploaded.wav" and rig.core.settings.get("core", "browse_dir") == ""


class SlowKit(FakeKit):
    """A dialog that stays open until `answer` is set."""

    def __init__(self, path: Path) -> None:
        super().__init__(file_result=path)
        self.answer = asyncio.Event()

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        found = await super().pick_file(title=title, filters=filters, start_dir=start_dir)
        await self.answer.wait()
        return found


async def test_browse_is_disabled_while_its_dialog_is_open(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    kit = SlowKit(clip_file(tmp_path, "slow.wav", 9.0))
    rig, _, forge, _ = await forge_rig(rigs, kit=kit)
    first = asyncio.ensure_future(forge.browse())
    await tick()
    assert forge.vm.browse.enabled is False
    await forge.browse()
    assert len(kit.called("pick_file")) == 1
    kit.answer.set()
    await first
    await settled(rig)
    assert forge.vm.browse.enabled is True and forge.vm.clip_file == "slow.wav"


class BrokenKit(FakeKit):
    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        raise RuntimeError("no dialog today")


async def test_a_dialog_that_fails_says_so(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, kit=BrokenKit())
    toasts = watch_toasts(rig)
    await forge.browse()
    assert texts(toasts) == [("Couldn't open the file dialog: no dialog today", True)]
    assert forge.vm.browse.enabled is True and forge.vm.clip_file == "sample.wav"


async def test_a_drop_stages_one_clip_at_a_time(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    toasts = watch_toasts(rig)
    one, two = clip_file(tmp_path, "one.wav", 7.0), clip_file(tmp_path, "two.wav", 7.0)
    await forge.drop_paths((str(one), str(two)))
    await forge.drop_paths(())
    assert texts(toasts) == [(DROP_ONE, False)] and forge.vm.clip_file == "sample.wav"
    await forge.drop_paths((str(one),))
    await settled(rig)
    assert (forge.vm.clip_state, forge.vm.clip_file) == ("ready", "one.wav")


async def test_a_drop_while_busy_waits_for_the_job(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forging")
    toasts = watch_toasts(rig)
    await forge.forge()
    await entered(rig.fake.holds[0])
    await forge.drop_paths((str(clip_file(tmp_path, "late.wav", 7.0)),))
    assert texts(toasts) == [(DROP_BUSY, False)] and rig.core.forge_draft.clip.name == "sample.wav"


async def test_an_upload_stages_its_bytes(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    data = clip_file(tmp_path, "up.wav", 9.0).read_bytes()
    await forge.upload("C:\\fake\\up.wav", data)
    await settled(rig)
    assert (forge.vm.clip_state, forge.vm.clip_file, rig.core.forge_draft.clip.source) == ("ready", "up.wav", None)
    await forge.upload("up.m4a", data)
    await settled(rig)
    assert forge.vm.clip_meta == "Can't read up.m4a · use WAV, FLAC, OGG or MP3"


async def test_the_play_button_plays_and_stops_the_staged_clip(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge, _ = await forge_rig(rigs)
    playback = factory.playback()
    clip = rig.core.forge_draft.clip
    forge.toggle_clip_playback()
    assert playback.vm.target.src == rig.core.staged_url(clip.clip_id) and playback.vm.target.playing
    assert (forge.vm.playing, forge.vm.play.label) == (True, "Stop the reference clip")
    forge.toggle_clip_playback()
    assert playback.vm.target.playing is False and forge.vm.play.label == "Play the reference clip"


# ---------- prefill (Voices) ----------

async def test_reforge_prefills_from_the_voice(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "speak")
    assert forge.prefill(voice_id="aiko", mode="reforge") == "/forge"
    draft, vm = rig.core.forge_draft, forge.vm
    assert (draft.name, draft.backend, draft.language, draft.exaggeration, draft.consent) == (
        "aiko", MTL, "ja", 0.5, True)
    assert vm.clip_state == "reading" and vm.clip_file == "aiko_ref.wav"
    await settled(rig)
    assert vm.clip_state == "ready" and draft.clip.in_library is True
    assert vm.model_note == "Multilingual is resident" and vm.name_msg == "aiko already exists · forging replaces it"


async def test_a_copy_goes_to_the_other_model(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "speak")
    forge.prefill(voice_id="aiko", mode="copy")
    draft = rig.core.forge_draft
    assert (draft.name, draft.backend, draft.language) == ("aiko-turbo", TUR, "en")
    forge.prefill(voice_id="host-turbo", mode="copy")
    assert (draft.name, draft.backend, draft.language) == ("host-turbo-multilingual", MTL, "en")
    await settled(rig)


async def test_replace_takes_the_new_clip_without_consent(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "speak")
    forge.prefill(voice_id="marco", mode="reforge", clip_path=clip_file(tmp_path, "marco_new.wav", 9.0))
    await settled(rig)
    draft = rig.core.forge_draft
    assert (draft.name, draft.clip.name, draft.consent) == ("marco", "marco_new.wav", False)


async def test_prefill_refusals(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forging")
    toasts = watch_toasts(rig)
    assert forge.prefill(voice_id="nobody", mode="reforge") is None
    await forge.forge()
    await entered(rig.fake.holds[0])
    assert forge.prefill(voice_id="aiko", mode="reforge") is None
    assert texts(toasts) == [("nobody no longer exists", True), (BUSY, True)]
    assert rig.core.forge_draft.name == "rin"


async def test_a_prefill_during_the_switch_keeps_the_clip_the_forge_needs(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "forge_other_backend")
    old = rig.core.forge_draft.clip
    hold = rig.fake.hold("make_resident")
    await forge.forge(forge.vm.forge.pending.token)
    await entered(hold)
    assert rig.state.job.kind == "switch"
    assert forge.prefill(voice_id="marco", mode="reforge") == "/forge"     # only a forge job refuses it
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    assert old.staged.exists() and rig.core.forge_draft.clip.name == "marco_ref.wav"
    hold.release()
    await settled(rig)
    call = load_voice_calls(rig)[0]
    assert (call.args[0], Path(call.args[1]).name, rig.fake.loads) == ("rin", "rin_ref.wav", [TUR])
    assert rig.core.clips.staged_path(old.clip_id) is None and rig.core.forge_draft.forged == ""


async def test_a_client_that_goes_away_mid_switch_still_discards_the_clip_it_replaced(
        rigs: Callable[..., Rig]) -> None:
    """R1 finding: close() used to drop the retired clips with the subscriptions, so the staged copy stayed in
    cache\\staging (and in ClipService) until the 24 h startup sweep. Now it is discarded when the job ends."""
    rig, _, forge, _ = await forge_rig(rigs, "forge_other_backend")
    old = rig.core.forge_draft.clip
    hold = rig.fake.hold("make_resident")
    await forge.forge(forge.vm.forge.pending.token)
    await entered(hold)
    assert forge.prefill(voice_id="marco", mode="reforge") == "/forge"
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    forge.close()                                                          # the tab closes during the switch
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    assert old.staged.exists()                                             # the forge still needs it
    hold.release()
    await settled(rig)
    assert rig.core.clips.staged_path(old.clip_id) is None and not old.staged.exists()
    assert [Path(call.args[1]).name for call in load_voice_calls(rig)] == ["rin_ref.wav"]


async def test_a_voice_whose_clip_is_missing_prefills_a_failed_clip(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs, "voices_missing_files")
    forge.prefill(voice_id="aiko", mode="reforge")
    vm = forge.vm
    assert (vm.clip_state, vm.clip_meta) == ("failed", "Can't read aiko_ref.wav · file not found")
    assert label(forge) == "Pick Another Clip"


# ---------- plumbing ----------

async def test_opening_the_page_rereads_a_registry_changed_outside(rigs: Callable[..., Rig]) -> None:
    rig, _, forge, _ = await forge_rig(rigs)
    assert forge.vm.name_msg == "cache/chatterbox-multilingual/rin.pt"
    rig.fake.registry.register("rin", MTL, str(rig.paths.voices / "sample.wav"), 0.5, "ja")   # as cli.py would
    forge.opened()
    await settled(rig)
    assert forge.vm.name_msg == "rin already exists · forging replaces it"


async def test_close_drops_the_listeners(rigs: Callable[..., Rig]) -> None:
    rig, factory, forge, _ = await forge_rig(rigs)
    factory.close()
    forge.set_name("after")
    assert forge.vm.name == "rin" and rig.core.forge_draft.name == "after"


async def test_no_render_loads_or_bakes_in_any_forge_seed(rigs: Callable[..., Rig]) -> None:
    for scenario in ("forge", "forge_ready", "forge_other_backend", "forge_low_vram", "forge_replace",
                     "forge_replace_other_backend", "forge_taken_other", "forge_turbo_short", "forge_failed"):
        rig, factory, forge, _ = await forge_rig(rigs, scenario)
        factory.status().on_route("/forge")
        forge.opened()
        forge.toggle_language_list()
        forge.refresh()
        await rig.clock.run_for(5.0, step=1.0)
        assert rig.fake.loads == [] and load_voice_calls(rig) == [], scenario
