"""PlaybackController (§5.4): every intent row, the player's reports, Save As and reveal through a fake KitPort,
the D15 launch selection and the D19 pinned card."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ui.behaviours.playback import NO_TIME, WAV_FILTERS, Loaded, clock, time_text
from ui.contract.plans import PlayerTarget

from ui.behaviours.playback import PlaybackController

from tests.behaviours import (FakeKit, factory_for, no_error_logs, offline, started, texts, tick,  # noqa: F401
                              watch_toasts)
from tests.core.test_facade import Rig, entered, request, rigs  # noqa: F401
from tests.core.test_call import Recorder, SETTINGS


def take_url(take_id: int) -> str:
    return f"/media/take/{take_id}"


async def playing(rigs: Callable[..., Rig], scenario: str = "speak",
                  **options: object) -> tuple[Rig, PlaybackController, FakeKit]:
    rig = await started(rigs, scenario, **options)
    factory, kit = factory_for(rig)
    return rig, factory.playback(), kit


# ---------- the clock ----------

@pytest.mark.parametrize(("seconds", "text"), [
    (0.0, "0:00.0"), (1.9, "0:01.9"), (4.6, "0:04.6"), (59.96, "1:00.0"), (61.25, "1:01.3"), (600.0, "10:00.0"),
    (-3.0, "0:00.0"), (float("nan"), "0:00.0"), (float("inf"), "0:00.0"),
])
def test_clock_rounds_to_tenths_first(seconds: float, text: str) -> None:
    assert clock(seconds) == text


def test_time_text_clamps_to_the_take() -> None:
    assert time_text(1.9, 4.6) == "0:01.9 / 0:04.6"
    assert time_text(9.0, 4.6) == "0:04.6 / 0:04.6"
    assert NO_TIME == "0:00.0 / 0:00.0"


# ---------- takes ----------

async def test_single_audible_monitor_and_independent_manual_preview(rigs) -> None:
    output = Recorder()
    output.block_first = True
    rig = await started(rigs, audio_output=output, settings=SETTINGS)
    first = factory_for(rig)[0].playback()
    second = factory_for(rig)[0].playback()
    first.send_take(7)
    assert await asyncio.to_thread(output.entered.wait, 2)
    await tick()
    players = sorted((first, second), key=lambda player: player.vm.target.muted)
    audible, silent = players
    assert audible.vm.playing and silent.vm.playing
    assert not audible.vm.target.muted and silent.vm.target.muted
    rig.core.state.window.visible = False
    rig.core.events.notify()
    await tick()
    rig.clock.advance(1.7)
    rig.core.state.window.visible = True
    rig.core.events.notify()
    await tick()
    assert audible.vm.target.at == silent.vm.target.at == pytest.approx(1.7)
    silent.play_take(6)
    audible.stop_sending()
    await rig.settle()
    assert silent.vm.target.src == take_url(6) and silent.vm.playing and not silent.vm.target.muted
    assert not audible.vm.playing and len(output.plays) == 1


async def test_a_take_kept_after_stop_is_selected_quietly(rigs) -> None:
    rig, playback, _ = await playing(rigs)
    rig.core.jobs.quiet_take_id = 7
    rig.core.events.take_ready.emit(7)
    assert playback.loaded == Loaded("take", 7) and not playback.vm.target.playing

async def test_play_take_makes_it_current_and_plays_from_zero(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    changes: list[int] = []
    playback.listeners.add(lambda: changes.append(playback.vm.target.seq))
    playback.play_take(6)
    assert rig.core.session.take_id == 6
    assert playback.vm.target == PlayerTarget(seq=1, src=take_url(6), playing=True, at=0.0)
    assert (playback.vm.playing, playback.vm.duration, playback.loaded) == (True, 4.9, Loaded("take", 6))
    assert changes == [1] and playback.is_playing_take(6)
    playback.play_take(99)                                # unknown: nothing changes
    assert playback.vm.target.seq == 1
    assert rig.fake.loads == []


async def test_a_take_whose_wav_is_gone_is_a_red_toast(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    seen = watch_toasts(rig)
    path = rig.core.takes.path_of(6)
    path.unlink()
    playback.play_take(6)
    assert texts(seen) == [(f"Take 6's WAV is missing: {path}", True)]
    assert rig.core.session.take_id == 7 and playback.vm.target == PlayerTarget()


async def test_toggle_pauses_plays_and_restarts_at_the_end(rigs: Callable[..., Rig]) -> None:
    _, playback, _ = await playing(rigs)
    playback.toggle()                                    # take 7 is current and not loaded yet
    assert playback.vm.target == PlayerTarget(seq=1, src=take_url(7), playing=True, at=0.0)
    playback.on_player_state(True, 1.2, 4.6, False, "")
    playback.toggle()
    assert playback.vm.target == PlayerTarget(seq=2, src=take_url(7), playing=False, at=None)
    assert playback.vm.playing is False
    playback.toggle()
    assert playback.vm.target == PlayerTarget(seq=3, src=take_url(7), playing=True, at=None)
    playback.on_player_state(False, 4.6, 4.6, True, "")
    assert (playback.vm.playing, playback.vm.position) == (False, 4.6)
    playback.toggle()
    assert playback.vm.target == PlayerTarget(seq=4, src=take_url(7), playing=True, at=0.0)


async def test_toggle_without_a_take_does_nothing(rigs: Callable[..., Rig]) -> None:
    _, playback, _ = await playing(rigs, "firstrun")
    playback.toggle()
    assert playback.current() is None and playback.vm.target == PlayerTarget()
    assert playback.time_text() == NO_TIME


async def test_seek_moves_the_current_take_and_keeps_the_play_state(rigs: Callable[..., Rig]) -> None:
    _, playback, _ = await playing(rigs)
    playback.seek(1.9)
    assert playback.vm.target == PlayerTarget(seq=1, src=take_url(7), playing=False, at=1.9)
    assert playback.time_text() == "0:01.9 / 0:04.6"
    playback.toggle()
    playback.seek(99.0)
    assert playback.vm.target == PlayerTarget(seq=3, src=take_url(7), playing=True, at=4.6)
    playback.seek(float("nan"))
    assert playback.vm.target.seq == 3


async def test_the_time_text_holds_still_while_the_wave_writes_it(rigs: Callable[..., Rig]) -> None:
    _, playback, _ = await playing(rigs)
    playback.play_take(7)
    playback.on_player_state(True, 2.0, 4.6, False, "")
    assert playback.time_text(7) == "0:00.0 / 0:04.6"   # the take wave's rAF writes the live text (§6.5)
    playback.on_player_state(False, 2.3, 4.6, False, "")
    assert playback.time_text(7) == "0:02.3 / 0:04.6"
    assert playback.time_text(6) == "0:00.0 / 0:04.9"


async def test_ab_switch_keeps_the_position_and_the_play_state(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    playback.play_take(7)
    playback.on_player_state(True, 1.9, 4.6, False, "")
    playback.ab_switch(5)
    assert rig.core.session.take_id == 5
    assert playback.vm.target == PlayerTarget(seq=2, src=take_url(5), playing=True, at=1.9)
    playback.on_player_state(False, 5.0, 5.2, False, "")
    playback.ab_switch(7)                                # 5.0 s is past the end of take 7: clamped
    assert playback.vm.target == PlayerTarget(seq=3, src=take_url(7), playing=False, at=4.6)
    assert rig.core.session.pin_a == 5 and rig.core.session.pin_b == 7


async def test_a_take_is_never_pinned_as_both_a_and_b(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    session = rig.core.session
    playback.pin(6, "A")
    assert (session.pin_a, session.pin_b) == (6, 7)
    playback.pin(6, "B")
    assert (session.pin_a, session.pin_b) == (0, 6)
    playback.pin(6, "B")
    assert (session.pin_a, session.pin_b) == (0, 0)
    playback.pin(42, "A")
    playback.pin(6, "C")  # type: ignore[arg-type]
    assert (session.pin_a, session.pin_b) == (0, 0)


async def test_a_star_is_saved_at_once(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    playback.star(5)
    await rig.settle()
    assert rig.core.takes.get(5).starred is True
    assert next(t for t in rig.state.takes.takes if t.id == 5).starred is True
    index = json.loads(rig.paths.takes_index.read_text(encoding="utf-8"))
    assert next(t for t in index["takes"] if t["id"] == 5)["starred"] is True
    playback.star(5)
    playback.star(5)                                     # two clicks before the first save: back to starred
    await rig.settle()
    assert rig.core.takes.get(5).starred is True


async def test_a_star_that_cannot_be_saved_is_a_red_toast(rigs: Callable[..., Rig],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    rig, playback, _ = await playing(rigs)
    seen = watch_toasts(rig)

    def full(take_id: int, starred: bool) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.core.takes, "set_starred", full)
    playback.star(6)
    await rig.settle()
    assert texts(seen) == [("Couldn't save the star: disk full", True)]
    assert rig.core.takes.get(6).starred is False


async def test_prev_and_next_walk_this_session(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    assert [take.id for take in playback.session_takes()] == [7, 6, 5, 4]
    playback.next()
    assert rig.core.session.take_id == 6 and playback.vm.target.src == take_url(6)
    playback.next()
    playback.prev()
    assert rig.core.session.take_id == 6 and playback.vm.target.playing
    for _ in range(4):
        playback.next()
    assert rig.core.session.take_id == 4                # the oldest row: no further
    seq = playback.vm.target.seq
    playback.next()
    assert playback.vm.target.seq == seq
    rig.core.session.take_id = 2                         # an earlier session's take: the newest row first
    playback.prev()
    assert rig.core.session.take_id == 7


# ---------- clips ----------

async def test_audition_plays_the_reference_clip_and_a_second_call_stops_it(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    playback.play_take(7)
    playback.on_player_state(True, 1.5, 4.6, False, "")
    url = rig.core.clip_url("aiko")
    assert url.startswith("/media/clip/aiko?v=")
    playback.audition("aiko")
    assert playback.vm.target == PlayerTarget(seq=2, src=url, playing=True, at=0.0)
    assert playback.is_playing_clip("aiko") and not playback.is_playing_take(7)
    assert playback.vm.duration == pytest.approx(11.2, abs=0.1)
    playback.audition("aiko")
    assert playback.vm.target == PlayerTarget(seq=3, src=url, playing=False, at=0.0)
    playback.toggle()                                    # back to the take where it was left
    assert playback.vm.target == PlayerTarget(seq=4, src=take_url(7), playing=True, at=1.5)
    assert rig.fake.loads == []


async def test_a_missing_reference_clip_is_a_red_toast(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs, "voices_missing_files")
    seen = watch_toasts(rig)
    playback.audition("aiko")
    assert texts(seen) == [("Couldn't play the reference clip of aiko: file not found", True)]
    assert playback.vm.target == PlayerTarget()


async def test_play_url_and_stop(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    url = rig.core.staged_url("test-aiko")
    playback.play_url(url)
    assert playback.vm.target == PlayerTarget(seq=1, src=url, playing=True, at=0.0) and playback.is_playing_url(url)
    playback.stop()
    assert playback.vm.target == PlayerTarget(seq=2, src=url, playing=False, at=0.0)
    playback.play_url("")
    assert playback.vm.target.seq == 2
    playback.play_take(7)
    playback.stop()                                      # a take pauses where it is
    assert playback.vm.target == PlayerTarget(seq=4, src=take_url(7), playing=False, at=None)


# ---------- the player's reports ----------

async def test_a_player_error_is_one_red_toast_per_target(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    seen = watch_toasts(rig)
    playback.play_take(7)
    error = "The audio format or file is not supported"
    playback.on_player_state(False, 0.0, 0.0, False, error)
    playback.on_player_state(False, 0.0, 0.0, False, error)
    assert texts(seen) == [(f"Couldn't play Take 7: {error}", True)] and playback.vm.playing is False
    playback.audition("aiko")
    playback.on_player_state(False, 0.0, 0.0, False, error)
    assert texts(seen)[-1] == (f"Couldn't play the reference clip of aiko: {error}", True)


async def test_reports_update_the_view_model(rigs: Callable[..., Rig]) -> None:
    _, playback, _ = await playing(rigs)
    seen: list[float] = []
    playback.listeners.add(lambda: seen.append(playback.vm.position))
    playback.play_take(7)
    playback.on_player_state(True, 0.75, 4.608, False, "")
    assert (playback.vm.playing, playback.vm.position, playback.vm.duration) == (True, 0.75, 4.608)
    playback.on_player_state(False, 4.0, 0.0, True, "")  # ended without a duration: the position stays
    assert (playback.vm.playing, playback.vm.position, playback.vm.duration) == (False, 4.0, 4.608)
    assert seen == [0.0, 0.75, 4.0]


# ---------- Save As and reveal ----------

async def test_save_as_copies_the_current_take(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    rig, playback, kit = await playing(rigs)
    seen = watch_toasts(rig)
    kit.save_result = tmp_path / "keep" / "mine.wav"
    await playback.save_as()
    assert kit.called("save_as") == [{"default_name": "aiko_take7.wav", "filters": WAV_FILTERS, "start_dir": None}]
    assert WAV_FILTERS == ("WAV audio (*.wav)",)
    assert (tmp_path / "keep" / "mine.wav").read_bytes() == rig.core.takes.path_of(7).read_bytes()
    assert texts(seen) == [("Saved mine.wav", False)]
    kit.save_result = None
    await playback.save_as()                             # Cancel: nothing happens
    assert kit.called("save_as")[-1]["start_dir"] == tmp_path / "keep" and len(seen) == 1


async def test_save_as_failures_are_red_toasts(rigs: Callable[..., Rig], tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    rig, playback, kit = await playing(rigs)
    seen = watch_toasts(rig)
    kit.save_result = tmp_path / "mine.wav"

    def full(take_id: int, dest: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(rig.core.takes, "copy_to", full)
    await playback.save_as()
    assert texts(seen) == [("Couldn't save mine.wav: disk full", True)]
    path = rig.core.takes.path_of(7)
    path.unlink()
    await playback.save_as()                             # a missing WAV never opens the dialog
    assert texts(seen)[-1] == (f"Take 7's WAV is missing: {path}", True) and len(kit.called("save_as")) == 1


async def test_reveal_shows_the_wav_in_explorer(rigs: Callable[..., Rig]) -> None:
    rig, playback, kit = await playing(rigs)
    seen = watch_toasts(rig)
    playback.reveal()
    assert kit.called("reveal") == [{"path": rig.core.takes.path_of(7)}] and seen == []
    kit.reveal_error = OSError("Explorer is not available")
    playback.reveal()
    assert texts(seen) == [("Couldn't show aiko_take7.wav in Explorer: Explorer is not available", True)]
    path = rig.core.takes.path_of(7)
    path.unlink()
    playback.reveal()
    assert texts(seen)[-1] == (f"Take 7's WAV is missing: {path}", True) and len(kit.called("reveal")) == 2


# ---------- new takes ----------

@pytest.mark.parametrize("autoplay", [True, False])
async def test_a_new_take_is_selected_and_plays_with_autoplay(rigs: Callable[..., Rig], autoplay: bool) -> None:
    rig, playback, _ = await playing(rigs, settings={"autoplay": autoplay})
    rig.core.jobs.start_generate([request()])
    await rig.settle()
    assert rig.core.session.take_id == 8
    assert playback.vm.target == PlayerTarget(seq=1, src=take_url(8), playing=autoplay, at=0.0)
    assert rig.fake.loads == []


async def test_the_launch_selection_shows_the_newest_take_without_playing(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "relaunch", session=False)
    session = rig.core.session
    assert (session.take_id, session.voice_id, session.language, session.exaggeration, session.cfg_weight) == (
        7, "aiko", "ja", 0.5, 0.5)
    factory, _ = factory_for(rig)
    playback = factory.playback()
    assert playback.current().id == 7 and playback.vm.target == PlayerTarget() and playback.vm.playing is False
    assert playback.time_text() == "0:00.0 / 0:04.6" and playback.session_takes() == ()
    session.voice_id = ""
    factory_for(rig)[0].playback()                       # a page load never selects again (D15)
    assert (session.take_id, session.voice_id) == (7, "")
    playback.toggle()
    assert playback.vm.target == PlayerTarget(seq=1, src=take_url(7), playing=True, at=0.0)
    assert rig.fake.loads == [] and rig.state.engine.resident == ""


async def test_with_no_takes_nothing_is_preselected(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun", session=False)
    assert (rig.core.session.take_id, rig.core.session.voice_id) == (0, "")
    playback = factory_for(rig)[0].playback()
    assert playback.current() is None and playback.time_text() == NO_TIME


async def test_d19_the_card_stays_on_the_take_that_plays_while_the_next_one_generates(
        rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs)
    jobs = rig.core.jobs
    first, second = rig.fake.hold("speak"), rig.fake.hold("speak")
    assert jobs.start_generate([request()] * 3).status == "started"           # Generate 3 Takes
    await entered(first)
    await tick()
    assert rig.state.job.take_no == 8 and playback.pinned() is False and playback.card_generating() is True
    first.release()
    await entered(second)
    await tick()
    assert rig.state.job.take_no == 9 and rig.core.session.take_id == 8
    assert playback.is_playing_take(8)                   # take 8 autoplayed when it was ready
    assert playback.pinned() is True and playback.card_generating() is False
    playback.toggle()                                    # Space on the pinned card pauses take 8
    assert playback.vm.target.src == take_url(8) and playback.vm.target.playing is False
    assert playback.pinned() is True                     # the card stays pinned until take 9 is ready
    second.release()
    await rig.settle()
    await tick()
    assert rig.core.session.take_id == 10 and playback.vm.target.src == take_url(10)
    assert playback.vm.target.playing is True and playback.pinned() is False
    assert rig.fake.loads == []


async def test_d19_without_playback_the_card_shows_the_generating_state(rigs: Callable[..., Rig]) -> None:
    rig, playback, _ = await playing(rigs, settings={"autoplay": False})
    first, second = rig.fake.hold("speak"), rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()] * 2)
    await entered(first)
    first.release()
    await entered(second)
    await tick()
    assert rig.state.job.take_no == 9 and playback.vm.target.src == take_url(8)
    assert playback.vm.target.playing is False and playback.card_generating() is True
    second.release()
    await rig.settle()


async def test_save_and_reveal_wait_while_the_card_shows_a_generating_take(rigs: Callable[..., Rig]) -> None:
    rig, playback, kit = await playing(rigs)
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])
    await entered(hold)
    await tick()
    assert playback.card_generating() is True
    await playback.save_as()
    playback.reveal()
    assert kit.calls == []
    hold.release()
    await rig.settle()
