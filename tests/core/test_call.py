"""Phase 5 call routing with a real scratch take library and a recording output. No device I/O."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.core.test_facade import Rig, TUR, entered, request, rigs, turbo_request
from tests.core.test_jobs import token
from ui.contract.plans import AfterGenerate
from ui.contract.ports import AudioDefault, AudioEndpoint
from ui.core import build_core
from ui.core.clock import ManualScheduler
from ui.core.facade import Outcome
from ui.core.services.settings import SettingsStore
from ui.entry import Flags
from ui.launch import _make_audio_output

CABLE = AudioEndpoint("endpoint-7", "CABLE Input (VB-Audio Virtual Cable)")
SPEAKER = AudioEndpoint("endpoint-1", "Speakers")
SETTINGS = {"call_enabled": True, "call_device": {"id": CABLE.id, "name": CABLE.name}}


class Recorder:
    def __init__(self):
        self.devices = (SPEAKER, CABLE)
        self.listed = 0
        self.plays: list[tuple[AudioEndpoint, Path, bytes]] = []
        self.block_first = False
        self.fail_first = False
        self.release = threading.Event()
        self.entered = threading.Event()
        self.stopped: list[bool] = []

    def list_devices(self):
        self.listed += 1
        return self.devices

    def default_roles(self):
        return AudioDefault("playback", "console", SPEAKER), AudioDefault("playback", "communications", CABLE)

    def play(self, endpoint: AudioEndpoint, path: Path, stop: threading.Event, on_start: Callable[[], None]):
        self.plays.append((endpoint, path, path.read_bytes()))
        on_start()
        self.entered.set()
        if self.block_first and len(self.plays) == 1:
            # Bound the wait so a failed test cannot keep a worker alive.
            for _ in range(500):
                if self.release.is_set() or stop.wait(0.01):
                    break
            else:
                raise AssertionError("test did not release the output")
        self.stopped.append(stop.is_set())
        if self.fail_first and len(self.plays) == 1:
            raise OSError("endpoint removed mid-stream")


async def setup(rigs, **settings) -> tuple[Rig, Recorder]:
    output = Recorder()
    rig = rigs("speak", settings={**SETTINGS, **settings}, audio_output=output)
    await rig.start()
    return rig, output


async def test_old_take_after_restart_with_model_and_voice_gone(rigs):
    rig, output = await setup(rigs)
    path = rig.core.takes.path_of(7)
    samples = path.read_bytes()
    rig.core.jobs.start_free()
    await rig.settle()
    rig.fake.delete_voice(rig.core.takes.get(7).voice)
    rig.core.stop()
    clock = ManualScheduler()
    core = build_core(engine=rig.fake, paths=rig.paths, settings=SettingsStore(rig.paths.settings),
                      trash=rig.trashed.append, scheduler=clock, audio_output=output)
    await core.start()
    core.client_connected()
    core.client_connected()
    await clock.settle()
    rig.fake.reset_records()
    clock.advance(1)
    owners = [object(), object()]
    claims = []
    for owner in owners:
        core.events.call_started.subscribe(lambda take_id, owner=owner: claims.append(core.call.claim_monitor(take_id, owner)))
    assert core.call.send(7)
    assert not core.call.send(7)
    await clock.settle()
    assert output.plays == [(CABLE, path, samples)] and path.read_bytes() == samples
    assert claims == [True, False] and core.call.phase == "idle"
    assert not rig.fake.calls and core.jobs.last_activity == 0 and not core.state.engine.resident
    await core.call.shutdown()


async def test_wait_queue_cap_and_missing_wav_do_not_block_other_sends(rigs):
    rig, output = await setup(rigs)
    call = rig.core.call
    output.block_first = True
    assert all(call.send(n) for n in (7, 5, 4, 3))
    assert not call.send(5) and not call.send(2)
    assert "queue full" in call.error.lower()
    assert await asyncio.to_thread(output.entered.wait, 2)
    bad = []
    rig.core.events.toast.subscribe(lambda toast: bad.append(toast.text) if toast.bad else None)
    rig.core.takes.path_of(5).unlink()
    output.release.set()
    await rig.settle()
    assert any("5 WAV is missing" in text for text in bad)
    assert [p for _, p, _ in output.plays] == [rig.core.takes.path_of(n) for n in (7, 4, 3)]
    rig.clock.advance(0.5)
    assert not call.send(7)
    rig.clock.advance(0.51)
    assert call.send(7)
    await rig.settle()
    assert len(output.plays) == 4


async def test_cut_waits_for_previous_device_to_close(rigs):
    rig, output = await setup(rigs, call_overlap="cut")
    output.block_first = True
    assert rig.core.call.send(5)
    assert await asyncio.to_thread(output.entered.wait, 2)
    assert rig.core.call.send(7) and rig.core.call.phase == "stopping"
    await rig.settle()
    assert output.stopped == [True, False]
    assert [p for _, p, _ in output.plays] == [rig.core.takes.path_of(n) for n in (5, 7)]


async def test_delay_stop_disable_and_shutdown(rigs):
    rig, output = await setup(rigs, call_delay_s=2)
    call = rig.core.call
    assert call.available and call.send(5) and call.send(6)
    rig.clock.advance(1.9)
    call.stop()
    rig.clock.advance(3)
    await rig.settle()
    assert not output.plays and call.phase == "idle"
    assert call.send(5) and call.send(6)
    rig.core.settings.set("core", "call_enabled", False)
    rig.core.settings.set("core", "call_enabled", True)
    rig.clock.advance(3)
    await rig.settle()
    assert not output.plays and call.phase == "idle"
    output.block_first = True
    assert call.send(7)
    rig.clock.advance(2)
    assert await asyncio.to_thread(output.entered.wait, 2)
    await call.shutdown()
    assert output.stopped == [True] and call.phase == "idle"


async def test_missing_and_disconnected_device_dont_open_default_or_change_monitor(rigs):
    rig, output = await setup(rigs, call_hear=False)
    call = rig.core.call
    assert call.default_roles == (AudioDefault("playback", "communications", CABLE),)
    output.devices = (SPEAKER,)
    assert call.send(7)
    await rig.settle()
    assert "not found" in call.error and not output.plays and not call.available
    output.devices = (CABLE, SPEAKER)
    await call.refresh_devices()
    output.fail_first = True
    assert call.send(5)
    await rig.settle()
    assert "removed mid-stream" in call.error and rig.core.takes.path_of(5).is_file()
    assert not rig.core.settings.get("core", "call_hear")
    assert call.send(6)
    await rig.settle()
    assert [endpoint for endpoint, _, _ in output.plays] == [CABLE, CABLE]


@pytest.mark.parametrize(("mode", "switch", "send"), [
    ("preview", True, False), ("switch", False, False), ("switch", True, True), ("auto", False, True)])
async def test_generate_freezes_only_the_first_request_in_each_batch(rigs, mode, switch, send):
    rig, output = await setup(rigs, call_send=mode)
    call = rig.core.call
    batch = call.capture([turbo_request(), turbo_request()], switch=switch)
    after = AfterGenerate(batch)
    load = token(rig, TUR, reason="generate", asked=True, after=after)
    rig.core.settings.set("core", "call_send", "preview")
    assert rig.core.jobs.start_switch(load).status == "started"
    queued = call.capture([turbo_request()], switch=True)
    assert rig.core.jobs.start_generate(queued).status == "queued"
    readiness = []
    rig.core.events.take_ready.subscribe(lambda take: readiness.append((take, call.handles_monitor(take))))
    await rig.settle()
    assert readiness == [(8, send), (9, False), (10, False)]
    assert [p for _, p, _ in output.plays] == ([rig.core.takes.path_of(8)] if send else [])


async def test_master_disable_during_generation_invalidates_original_intent(rigs):
    rig, output = await setup(rigs, call_send="auto")
    hold = rig.fake.hold("speak")
    reqs = rig.core.call.capture([request()], switch=False)
    rig.core.jobs.start_generate(reqs)
    await entered(hold)
    rig.core.settings.set("core", "call_enabled", False)
    rig.core.settings.set("core", "call_enabled", True)
    hold.release()
    await rig.settle()
    assert not output.plays and rig.core.takes.get(8) and not rig.core.call.handles_monitor(8)
    fresh = rig.core.call.capture([request()], switch=False)[0]
    rig.core.call.route_saved(fresh, 8, truncated=True)
    assert not rig.core.call.handles_monitor(8) and not output.plays


async def test_token_limit_saves_the_truncated_wave_for_preview(rigs):
    rig, output = await setup(rigs, call_send="auto")
    reqs = rig.core.call.capture([request(text="A long script. " * 200)], switch=True)
    rig.core.jobs.start_generate(reqs)
    await rig.settle()
    assert rig.core.takes.get(8).hit_token_limit and rig.core.takes.path_of(8).is_file()
    assert not rig.core.call.handles_monitor(8) and not output.plays


async def test_slow_output_does_not_hold_generation(rigs):
    rig, output = await setup(rigs, call_send="auto")
    output.block_first = True
    reqs = rig.core.call.capture([request()] * 3, switch=True)
    rig.core.jobs.start_generate(reqs)
    try:
        await asyncio.wait_for(rig.core.jobs.wait_idle(), 5)
        assert await asyncio.to_thread(output.entered.wait, 2)
        assert rig.core.takes.next_id == 11 and len(output.plays) == 1 and not output.stopped
    finally:
        output.release.set()
    await rig.settle()


async def test_engine_failure_wins_if_stop_arrives_during_the_same_take(rigs, monkeypatch):
    rig, output = await setup(rigs, call_send="auto")
    entered, release = asyncio.Event(), asyncio.Event()

    async def failure(req, job):
        entered.set()
        await release.wait()
        return Outcome("failed", reason="GPU out of memory")

    monkeypatch.setattr(rig.core.facade, "speak", failure)
    rig.core.jobs.start_generate(rig.core.call.capture([request()], switch=True))
    await asyncio.wait_for(entered.wait(), 2)
    assert rig.core.jobs.stop_take()
    release.set()
    await rig.settle()
    assert rig.core.events.last_toast.bad and "GPU out of memory" in rig.core.events.last_toast.text
    assert rig.core.takes.next_id == 8 and not output.plays


@pytest.mark.parametrize("when", ["claimed", "sampling", "decoding", "saving", "save failure"])
async def test_stop_and_failed_commits_never_autoplay_or_send(rigs, monkeypatch, when):
    rig, output = await setup(rigs, call_send="auto")
    jobs = rig.core.jobs
    ready = []
    rig.core.events.take_ready.subscribe(ready.append)
    point = {"sampling": "speak", "decoding": "decode"}.get(when)
    hold = rig.fake.hold(point) if point else None
    saving, release = threading.Event(), threading.Event()
    original = rig.core.takes.add

    def add(*args):
        if when == "save failure":
            raise OSError("disk full")
        saving.set()
        assert release.wait(5)
        return original(*args)

    if when in ("saving", "save failure"):
        monkeypatch.setattr(rig.core.takes, "add", add)
    reqs = rig.core.call.capture([request()] * 4, switch=True)
    assert jobs.start_generate(reqs).status == "started"
    if hold:
        await entered(hold)
    if when == "saving":
        assert await asyncio.to_thread(saving.wait, 3)
    if when != "save failure":
        assert jobs.stop_take() and not jobs.queued
        assert rig.state.job.phase == "stopping"
        assert jobs.start_generate(reqs).status == "refused"
    if hold:
        hold.release()
    release.set()
    await rig.settle()
    assert ready == ([8] if when == "saving" else []) and not output.plays and not jobs.busy
    assert bool(rig.core.takes.get(8)) is (when == "saving")
    if when == "saving":
        assert "already done" in rig.core.events.last_toast.text and jobs.quiet_take_id == 8


async def test_stop_current_can_leave_next_queued_take(rigs):
    rig, output = await setup(rigs, call_send="auto")
    reqs = rig.core.call.capture([request(), request()], switch=True)
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate(reqs)
    await entered(hold)
    assert rig.core.jobs.stop_take(drop_queue=False)
    hold.release()
    await rig.settle()
    assert rig.core.takes.next_id == 9 and not output.plays


async def test_master_off_does_no_startup_audio_work(rigs):
    rig, output = await setup(rigs, call_enabled=False)
    assert output.listed == 0 and not rig.core.call.available
    assert not rig.core.call.send(7) and not output.plays


async def test_saved_auto_mode_cannot_change_playback_without_an_output(rigs):
    rig = rigs("speak", settings={**SETTINGS, "call_send": "auto"})
    await rig.start()
    reqs = rig.core.call.capture([request()], switch=True)
    assert reqs[0].call_epoch is None and not rig.core.call.available
    rig.core.jobs.start_generate(reqs)
    await rig.settle()
    assert not rig.core.call.handles_monitor(8)


@pytest.mark.parametrize(("flags", "simulation"), [
    (Flags(fake="speak"), False), (Flags(test_window=True), False),
    (Flags(smoke=""), False), (Flags(), True)])
def test_test_runs_never_select_a_hardware_adapter(flags, simulation):
    assert _make_audio_output(flags, simulation) is None


def test_regular_windows_launcher_has_a_lazy_audio_output(monkeypatch):
    from ui import launch
    from ui.kit.sound_out import WindowsAudioOut

    monkeypatch.setattr(launch, "os", SimpleNamespace(name="nt"))
    assert isinstance(_make_audio_output(Flags(), simulation=False), WindowsAudioOut)
