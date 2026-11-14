"""Background work (§4.12, §4.17, §4.18, §5.4 D15) on the manual clock: device readings, warm-up, idle
auto-unload, startup checks and the launch selection, shutdown, and the native window events."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.types import EngineEvent
from ui.contract.plans import Toast
from ui.core.background import WARMUP_IN_USE, launch_line
from ui.core.clock import ManualScheduler, Scheduler, default_scheduler
from ui.core.facade import EventRelay
from ui.core.fake import SCENARIOS
from ui.core.native import NativeBridge, register

from tests.core.test_facade import (  # noqa: F401  (fixtures)
    GIB, MTL, STD, TUR, Rig, entered, request, rigs, speak)
from tests.theme_settings import EMIYA_TITLE


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def readings(rig: Rig) -> list[tuple[Any, ...]]:
    return [call.args for call in rig.calls("device_info")]


def lines(rig: Rig) -> list[str]:
    return [line[10:] for line in rig.core.log.lines]


def token(rig: Rig, backend: str, *, asked: bool = False) -> str:
    return rig.core.tokens.mint(rig.core.planner.switch_plan(backend, reason="explicit"), asked=asked)


async def advance(rig: Rig, seconds: float, *, held_job: bool = False) -> None:
    """Move the manual clock, then wait for what it started (not for a job a hold keeps running)."""
    rig.clock.advance(seconds)
    await rig.clock.settle(ignore=[rig.core.jobs.task] if held_job else ())


# ---------- DevicePoller ----------

async def test_the_first_reading_waits_for_the_first_client_and_probes(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start(connect=False)
    await advance(rig, 10.0)
    assert readings(rig) == [] and not rig.state.device.detected
    assert rig.state.about.gpu == "Detecting…"
    rig.core.client_connected()
    await rig.settle()
    assert readings(rig) == [(True,)]
    device = rig.state.device
    assert (device.detected, device.cuda, device.short_name) == (True, True, "RTX 4070 Ti")
    assert device.app_bytes == round(3.4 * GIB) and device.free_bytes is not None
    assert rig.state.about.gpu == "RTX 4070 Ti · 12.0 GB · detected"
    rig.core.client_connected()
    await rig.settle()
    assert readings(rig) == [(True,)]


async def test_a_client_before_startup_gets_its_reading_once_startup_ran(rigs: Callable[..., Rig]) -> None:
    rig = rigs("relaunch", settings={"warmup": "last"})
    rig.core.client_connected()
    await rig.settle()
    assert readings(rig) == [] and rig.fake.loads == []
    await rig.core.start()
    await rig.settle()
    await rig.core.wait_ready()
    assert readings(rig)[0] == (True,) and rig.fake.loads == [MTL]


async def test_readings_every_2_s_while_cuda_is_initialised(speak: Rig) -> None:
    speak.fake.reset_records()
    await advance(speak, 1.5)
    assert readings(speak) == []
    await advance(speak, 0.5)
    assert readings(speak) == [(False,)]
    for _ in range(4):
        await advance(speak, 0.5)
    assert readings(speak) == [(False,), (False,)]


async def test_readings_every_half_second_while_a_switch_runs(speak: Rig) -> None:
    hold = speak.fake.hold("make_resident")
    speak.core.jobs.start_switch(token(speak, TUR))
    await entered(hold)
    speak.fake.reset_records()
    for _ in range(4):
        await advance(speak, 0.5, held_job=True)
    assert len(readings(speak)) == 4
    hold.release()
    await speak.settle()


async def test_a_tick_is_skipped_while_the_previous_reading_runs(speak: Rig) -> None:
    hold = speak.fake.hold("device_info")
    speak.fake.reset_records()
    speak.clock.advance(2.0)
    await entered(hold)
    for _ in range(6):
        speak.clock.advance(0.5)
        await asyncio.sleep(0.01)
    assert len(readings(speak)) == 1 and speak.core._runtime.poller.reading
    hold.release()
    await speak.settle()
    assert len(readings(speak)) == 1
    await advance(speak, 2.0)
    assert len(readings(speak)) == 2


async def test_one_reading_after_each_job(speak: Rig) -> None:
    speak.fake.reset_records()
    speak.core.jobs.start_generate([request()])
    await speak.settle()
    assert readings(speak) == [(False,)]


async def test_without_cuda_there_are_no_periodic_readings(rigs: Callable[..., Rig]) -> None:
    rig = rigs("cpu")
    await rig.start()
    assert readings(rig) == [(True,)] and rig.state.device.cuda is False
    assert rig.state.about.gpu == "No CUDA GPU · models run on the CPU"
    await advance(rig, 10.0)
    assert readings(rig) == [(True,)]


# ---------- Warmup ----------

def relaunch(rigs: Callable[..., Rig], *, warmup: str = "last", **changes: Any) -> Rig:
    scenario = replace(SCENARIOS["relaunch"], name="relaunch-test", **changes) if changes else "relaunch"
    return rigs(scenario, settings={"warmup": warmup})


async def test_warm_up_loads_the_last_model_once_after_the_first_reading(rigs: Callable[..., Rig]) -> None:
    rig = relaunch(rigs)
    await rig.start(connect=False)
    assert lines(rig) == ["Emiya Voice started · warming up Multilingual"]
    assert rig.fake.loads == []
    rig.core.client_connected()
    await rig.settle()
    assert rig.fake.loads == [MTL]
    assert lines(rig)[1:] == ["Loading chatterbox-multilingual · warm-up",
                              "Loaded chatterbox-multilingual · 14.2 s · 3.4 GB"]
    loading = [event for event in rig.state.history.events if event.kind == "loading"][-1]
    assert loading.reason == "warm-up"
    rig.core.client_connected()
    await rig.settle()
    rig.core._runtime.warmup.run()
    assert rig.fake.loads == [MTL]


async def test_warm_up_skips_on_low_vram_with_a_toast(rigs: Callable[..., Rig]) -> None:
    rig = relaunch(rigs, other_gb=11.0)
    seen: list[Toast] = []
    rig.core.events.toast.subscribe(seen.append)
    await rig.start()
    text = "Warm-up skipped · Low VRAM · 1.0 GB free"
    assert lines(rig)[-1] == text and [toast.text for toast in seen] == [text]
    assert rig.fake.loads == []


async def test_warm_up_never_follows_a_switch_the_user_started(rigs: Callable[..., Rig]) -> None:
    rig = relaunch(rigs)
    await rig.start(connect=False)
    rig.core.jobs.start_switch(token(rig, STD))
    await rig.settle()
    rig.core.client_connected()
    await rig.settle()
    assert rig.fake.loads == [STD]
    assert lines(rig)[-1] == WARMUP_IN_USE


async def test_warm_up_never_evicts_a_resident_model(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak", settings={"warmup": "last"})
    await rig.start()
    assert rig.fake.loads == [] and lines(rig)[-1] == WARMUP_IN_USE


async def test_warm_up_off_does_nothing(rigs: Callable[..., Rig]) -> None:
    rig = relaunch(rigs, warmup="off")
    await rig.start()
    assert rig.fake.loads == [] and lines(rig) == [launch_line(EMIYA_TITLE)]


# ---------- IdleUnloader ----------

async def test_idle_unload_after_the_set_minutes(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start()
    await advance(rig, 599.0)
    assert rig.state.engine.resident == MTL
    await advance(rig, 1.0)
    assert rig.state.engine.resident == ""
    assert lines(rig)[-1] == "Unloaded chatterbox-multilingual · idle 10 min · freed 3.4 GB"
    assert rig.calls("unload_backend")[-1].kwargs == {"reason": "idle"}


async def test_idle_unload_never_during_a_job_and_counts_from_its_end(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak", settings={"idle_unload_min": 10})
    await rig.start()
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request(), request()])
    await entered(hold)
    await advance(rig, 900.0, held_job=True)
    assert rig.state.engine.resident == MTL and rig.state.job.kind == "generate"
    hold.release()
    await rig.settle()
    assert rig.core.jobs.last_activity == 900.0
    await advance(rig, 585.0)
    assert rig.state.engine.resident == MTL
    await advance(rig, 15.0)
    assert rig.state.engine.resident == ""


async def test_idle_unload_counts_from_the_setting_change(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak", settings={"idle_unload_min": 0})
    await rig.start()
    await advance(rig, 1000.0)
    rig.core.settings.set("core", "idle_unload_min", 10)
    await advance(rig, 590.0)
    assert rig.state.engine.resident == MTL
    await advance(rig, 15.0)
    assert rig.state.engine.resident == ""


async def test_idle_unload_off_never_unloads(speak: Rig) -> None:
    speak.core.settings.set("core", "idle_unload_min", 0)
    await advance(speak, 4000.0)
    assert speak.state.engine.resident == MTL and speak.calls("unload_backend") == []


async def test_editing_a_resident_draft_extends_the_idle_window(speak: Rig) -> None:
    await advance(speak, 585)
    speak.core.jobs.note_activity(MTL)
    await advance(speak, 585)
    assert speak.state.engine.resident == MTL
    speak.core.jobs.note_activity("chatterbox-turbo")
    await advance(speak, 15)
    assert speak.state.engine.resident == ""


async def test_failed_idle_unload_retries_once_per_idle_window(speak: Rig) -> None:
    speak.fake.fail_next("unload_backend", RuntimeError("device busy"))
    await advance(speak, 600)
    assert len(speak.calls("unload_backend")) == 1 and speak.state.engine.resident == MTL
    await advance(speak, 585)
    assert len(speak.calls("unload_backend")) == 1
    await advance(speak, 15)
    assert len(speak.calls("unload_backend")) == 2 and speak.state.engine.resident == ""


# ---------- startup ----------

async def test_start_mirrors_the_engine_the_files_and_the_about_facts(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start(connect=False)
    state = rig.state
    assert (state.engine.resident, state.engine.device, state.engine.max_resident) == (MTL, "cuda", 1)
    assert state.engine.enabled == (MTL, STD, TUR)
    assert len(state.takes.takes) == 7 and state.takes.next_id == 8
    assert state.history.events == rig.core.history.events and state.history.events
    assert state.library.status == "ready" and len(state.library.rows) == 5
    about = state.about
    assert about.engine.startswith("chatterbox-tts") and about.models_in_vram == "One at a time"
    assert (about.registry, about.cache, about.clips, about.takes) == (
        "cache/registry.json", "cache/‹model›/‹voice›.pt", "voices/reference", "outputs/takes")
    assert lines(rig) == [launch_line(EMIYA_TITLE)]
    assert [call.args for call in rig.calls("set_preferred_device")] == [("cuda",)]
    assert rig.fake.loads == [] and rig.calls("make_resident") == []
    await rig.core.wait_ready()


async def test_device_and_output_folder_changes_apply_live(speak: Rig, tmp_path: Path) -> None:
    core = speak.core
    core.settings.set("core", "device", "cpu")
    assert speak.fake.residency().preferred_device == "cpu" and core.planner.device() == "cpu"
    elsewhere = tmp_path / "elsewhere"
    core.settings.set("core", "takes_dir", str(elsewhere))
    assert speak.state.about.takes == str(elsewhere)
    core.settings.set("core", "takes_dir", "outputs/mine")
    assert speak.state.about.takes == "outputs/mine"
    core.jobs.start_generate([request()])
    await speak.settle()
    assert speak.core.takes.path_of(8).parent == speak.paths.root / "outputs" / "mine"


async def test_the_launch_selection_picks_the_newest_take_and_its_voice(rigs: Callable[..., Rig]) -> None:
    rig = rigs("relaunch")
    await rig.start()
    session = rig.core.session
    assert (session.take_id, session.voice_id, session.language, session.exaggeration, session.cfg_weight) == (
        7, "aiko", "ja", 0.5, 0.5)
    assert (session.text, session.pin_a, session.pin_b) == ("", 0, 0)
    assert rig.fake.loads == []


async def test_the_launch_selection_skips_a_voice_that_is_gone(rigs: Callable[..., Rig]) -> None:
    rig = rigs("relaunch")
    rig.fake.registry.remove("aiko")
    await rig.start()
    assert (rig.core.session.take_id, rig.core.session.voice_id) == (7, "")


async def test_no_takes_means_no_selection(rigs: Callable[..., Rig]) -> None:
    rig = rigs("firstrun")
    await rig.start()
    assert (rig.core.session.take_id, rig.core.session.voice_id) == (0, "")
    assert rig.state.library.rows == () and rig.state.library.status == "ready"


async def test_a_corrupt_takes_index_toasts_when_the_first_client_connects(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    (rig.paths.takes_index).write_text("{not json", encoding="utf-8")
    seen: list[Toast] = []
    rig.core.events.toast.subscribe(seen.append)
    await rig.start(connect=False)
    assert seen == [] and rig.state.takes.takes == ()
    notice = lines(rig)[0]
    assert notice.startswith("The takes list couldn't be read · moved it to index.json.corrupt-")
    rig.core.client_connected()
    await rig.settle()
    assert [(toast.text, toast.bad) for toast in seen] == [(notice, True)]


async def test_a_corrupt_registry_is_reported_once(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    (rig.paths.cache / "registry.json").write_text("{broken", encoding="utf-8")
    seen: list[Toast] = []
    rig.core.events.toast.subscribe(seen.append)
    await rig.start()
    reported = [toast.text for toast in seen]
    assert len(reported) == 1 and reported[0].startswith("The voice registry was unreadable · moved it to registry.json")
    assert rig.state.library.status == "corrupt" and reported[0] in lines(rig)
    await rig.core.refresh_library()                  # the next snapshot reads an empty registry: still "corrupt"
    assert rig.state.library.status == "corrupt" and len(seen) == 1
    rig.fake.registry.register("rin", MTL, "voices/reference/rin.wav", 0.5, "en", baked=False)
    await rig.core.refresh_library()                  # a voice again: the library is ready, nothing re-reported
    assert rig.state.library.status == "ready" and len(seen) == 1


async def test_refresh_library_only_when_the_registry_changed(speak: Rig) -> None:
    speak.fake.reset_records()
    rev = speak.state.library.rev
    await speak.core.refresh_library(changed_only=True)
    assert speak.calls("voice_details") == [] and speak.state.library.rev == rev
    assert (await speak.core.facade.update_voice("narrator", exaggeration=0.8)).ok
    await speak.core.refresh_library(changed_only=True)
    assert len(speak.calls("voice_details")) == 1 and speak.state.library.rev == rev + 1


# ---------- shutdown ----------

async def test_stop_records_closed_flushes_settings_and_never_calls_the_engine(speak: Rig) -> None:
    speak.core.settings.set("core", "autoplay", False)
    speak.fake.reset_records()
    speak.core.stop()
    assert speak.fake.calls == []
    closed = speak.core.history.events[-1]
    assert (closed.kind, closed.backend, closed.device) == ("closed", MTL, "cuda")
    assert closed.gb == pytest.approx(3.4, abs=0.01) and closed.peak_gb == pytest.approx(3.6, abs=0.01)
    assert json.loads(speak.paths.settings.read_text(encoding="utf-8"))["core"]["autoplay"] is False


async def test_stop_with_nothing_resident_records_nothing(rigs: Callable[..., Rig]) -> None:
    rig = rigs("firstrun")
    await rig.start()
    count = len(rig.core.history.events)
    rig.core.stop()
    assert len(rig.core.history.events) == count


async def test_stop_cancels_the_running_take(speak: Rig) -> None:
    hold = speak.fake.hold("speak")
    speak.core.jobs.start_generate([request(), request()])
    await entered(hold)
    speak.core.stop()
    await speak.settle()
    assert speak.state.job.kind == "" and speak.state.takes.next_id == 8


# ---------- the schedulers ----------

async def test_the_manual_clock_fires_timers_in_order_at_their_due_time() -> None:
    clock = ManualScheduler()
    fired: list[tuple[str, float]] = []
    clock.later(1.0, lambda: fired.append(("once", clock.now())))
    ticker = clock.every(0.4, lambda: fired.append(("tick", clock.now())))
    cancelled = clock.later(0.5, lambda: fired.append(("cancelled", clock.now())))
    cancelled.cancel()
    clock.advance(1.2)
    assert fired == [("tick", 0.4), ("tick", 0.8), ("once", 1.0), ("tick", 1.2)]
    assert clock.now() == 1.2
    ticker.cancel()
    clock.advance(5.0)
    assert len(fired) == 4 and clock.pending == 0
    with pytest.raises(ValueError):
        clock.advance(-1.0)
    with pytest.raises(ValueError):
        clock.every(0, lambda: None)


async def test_the_manual_clock_spawns_and_settles_async_callbacks() -> None:
    clock = ManualScheduler()
    done: list[str] = []
    gate = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0)
        done.append("work")

    async def blocked() -> None:
        await gate.wait()

    clock.later(0.0, work)
    clock.advance(0.0)
    await clock.settle()
    assert done == ["work"]
    held = clock.spawn(blocked(), name="held")
    await clock.settle(ignore=[held])
    with pytest.raises(TimeoutError, match="held"):
        await clock.settle(timeout=0.05)
    assert clock.cancel_pending() == 1


async def test_the_manual_clock_logs_a_failing_callback_and_keeps_ticking(caplog: pytest.LogCaptureFixture) -> None:
    clock = ManualScheduler()
    ticks: list[float] = []

    def flaky() -> None:
        ticks.append(clock.now())
        if len(ticks) == 1:
            raise RuntimeError("boom\nsecond line")

    clock.every(1.0, flaky)
    clock.advance(2.0)
    assert ticks == [1.0, 2.0]
    assert [r.getMessage() for r in caplog.records if r.name == "voicetool.core"] == [
        "A timer callback failed: RuntimeError: boom"]
    assert all(r.exc_info is None for r in caplog.records)


async def test_the_real_scheduler_uses_the_loop() -> None:
    clock = Scheduler()
    fired: list[str] = []
    done = asyncio.Event()

    async def work() -> None:
        fired.append("spawned")
        done.set()

    clock.later(0.01, lambda: fired.append("later"))
    ticker = clock.every(0.01, lambda: fired.append("tick"))
    clock.later(0.02, work)
    skipped = clock.later(0.01, lambda: fired.append("cancelled"))
    skipped.cancel()
    await asyncio.wait_for(done.wait(), 5)
    await asyncio.sleep(0.05)
    ticker.cancel()
    count = fired.count("tick")
    await asyncio.sleep(0.05)
    assert "later" in fired and "spawned" in fired and "cancelled" not in fired
    assert count >= 1 and fired.count("tick") == count
    assert clock.now() == pytest.approx(time.monotonic(), abs=1.0)


def test_default_scheduler_follows_voicetool_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICETOOL_CLOCK", "manual")
    assert isinstance(default_scheduler(), ManualScheduler)
    monkeypatch.setenv("VOICETOOL_CLOCK", "")
    assert type(default_scheduler()) is Scheduler


async def test_the_event_relay_drops_events_it_cannot_deliver() -> None:
    relay = EventRelay()
    event = EngineEvent(kind="loaded", backend=MTL, at=0.0)
    relay(event)                                             # nothing connected yet: dropped
    seen: list[EngineEvent] = []
    closed = asyncio.new_event_loop()
    closed.close()
    relay.connect(seen.append, closed)
    relay(event)                                             # the loop is gone: dropped
    relay.connect(seen.append, asyncio.get_running_loop())
    await asyncio.to_thread(relay, event)                    # from a worker thread, onto this loop
    await asyncio.sleep(0)
    assert seen == [event]


# ---------- native window events ----------

class FakeNative:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, kind: str, handler: Callable[..., Any]) -> None:
        self.handlers.setdefault(kind, []).append(handler)


async def test_native_events_reach_the_newest_core_only(rigs: Callable[..., Rig]) -> None:
    first, second = rigs("speak"), rigs("speak")
    app = SimpleNamespace(native=FakeNative())
    bridge = NativeBridge()
    bridge.register(app, first.core)
    bridge.register(app, second.core)
    assert {kind: len(handlers) for kind, handlers in app.native.handlers.items()} == {
        "drop": 1, "minimized": 1, "restored": 1}
    dropped: list[tuple[str, ...]] = []
    second.core.events.dropped.subscribe(dropped.append)
    app.native.handlers["minimized"][0]()
    assert second.state.window.visible is False and first.state.window.visible is True
    app.native.handlers["restored"][0]()
    assert second.state.window.visible is True
    app.native.handlers["drop"][0](SimpleNamespace(args={"files": [r"C:\clips\rin.wav", ""]}))
    app.native.handlers["drop"][0](SimpleNamespace(args={"files": []}))
    assert dropped == [(r"C:\clips\rin.wav",)]


async def test_register_is_safe_to_repeat(rigs: Callable[..., Rig]) -> None:
    app = SimpleNamespace(native=FakeNative())
    rig = rigs("speak")
    register(app, rig.core)
    register(app, rig.core)
    assert len(app.native.handlers["drop"]) == 1
    app.native.handlers["minimized"][0]()
    assert rig.state.window.visible is False
