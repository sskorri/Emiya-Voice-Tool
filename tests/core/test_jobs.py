"""JobRunner (§4.8-4.9) against FakeEngine on the manual clock: chains, copy, the queue of 3, the slot."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from engine.errors import BackendLoadError, EngineBusy, GenerationError
from ui.contract.plans import AfterForge, AfterGenerate, ForgeRequest, GenerateRequest, Toast
from ui.core.facade import BUSY, OOM, STALE
from ui.core.jobs import QUEUE_FULL, TEST_LINE_JA, StartResult
from ui.core.services.takes_store import TakeNotListedError

from tests.core.test_facade import (  # noqa: F401  (fixtures)
    MTL, STD, TUR, Rig, entered, request, rigs, speak, turbo_request)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@dataclass
class Seen:
    """What the core's events delivered."""

    toasts: list[Toast] = field(default_factory=list)
    flashes: list[str] = field(default_factory=list)
    sparks: list[str] = field(default_factory=list)
    ready: list[int] = field(default_factory=list)
    forging: list[str] = field(default_factory=list)
    forged: list[str] = field(default_factory=list)

    @property
    def texts(self) -> list[tuple[str, bool]]:
        return [(toast.text, toast.bad) for toast in self.toasts]


def watch(rig: Rig) -> Seen:
    seen = Seen()
    events = rig.core.events
    events.toast.subscribe(seen.toasts.append)
    events.flash.subscribe(seen.flashes.append)
    events.spark.subscribe(seen.sparks.append)
    events.take_ready.subscribe(seen.ready.append)
    events.forge_started.subscribe(seen.forging.append)
    events.forged.subscribe(seen.forged.append)
    return seen


def lines(rig: Rig) -> list[str]:
    """The log without the time stamps."""
    return [line[10:] for line in rig.core.log.lines]


def new_lines(rig: Rig, before: int) -> list[str]:
    return lines(rig)[before:]


def token(rig: Rig, backend: str, *, reason: str = "explicit", asked: bool = False, after: Any = None) -> str:
    plan = rig.core.planner.switch_plan(backend, reason=reason, after=after)  # type: ignore[arg-type]
    return rig.core.tokens.mint(plan, asked=asked)


async def tick() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def history_kinds(rig: Rig) -> list[tuple[str, str]]:
    return [(event.kind, event.backend) for event in rig.state.history.events]


# ---------- switches ----------

async def test_a_switch_unloads_then_loads_with_the_mockup_copy(speak: Rig) -> None:
    core, fake = speak.core, speak.fake
    seen = watch(speak)
    before, history_before = len(lines(speak)), len(speak.state.history.events)
    evict, load = fake.hold("evict"), fake.hold("make_resident")
    assert core.jobs.start_switch(token(speak, TUR)) == StartResult("started")
    job = speak.state.job
    assert (job.kind, job.phase, job.source, job.target, job.fill) == ("switch", "unload", MTL, TUR, None)
    await entered(evict)
    assert new_lines(speak, before) == ["Switching Multilingual → Turbo"]
    evict.release()
    await entered(load)
    await tick()
    assert (job.phase, job.source, job.fill) == ("load", "", 0.0)
    assert speak.state.engine.resident == ""
    assert new_lines(speak, before)[-1] == "Unloaded chatterbox-multilingual · freed 3.4 GB"
    speak.clock.advance(4.4)
    assert job.elapsed_s == pytest.approx(4.4) and job.fill == pytest.approx(4.4 / 8.7, abs=1e-3)
    speak.clock.advance(20.0)
    assert job.fill == 0.95
    load.release()
    await speak.settle()
    assert new_lines(speak, before) == ["Switching Multilingual → Turbo",
                                        "Unloaded chatterbox-multilingual · freed 3.4 GB",
                                        "Loaded chatterbox-turbo · 8.7 s · 3.1 GB"]
    assert seen.texts == [("Turbo ready · 8.7 s · 3.1 GB", False)]
    assert seen.flashes == ["dock-chatterbox-turbo"] and seen.sparks == ["dock-chatterbox-turbo"]
    assert (speak.state.engine.resident, speak.state.engine.failed, speak.state.job.kind) == (TUR, "", "")
    assert fake.loads == [TUR]
    events = speak.state.history.events[history_before:]
    assert [(e.kind, e.backend, e.from_backend, e.reason) for e in events] == [
        ("switching", TUR, MTL, ""), ("unloaded", MTL, "", "switch"), ("loaded", TUR, "", "")]
    assert events[1].gb == pytest.approx(3.4, abs=0.01) and events[2].seconds == 8.7


async def test_a_load_from_nothing_says_loading(rigs: Callable[..., Rig]) -> None:
    rig = rigs("firstrun")
    await rig.start()
    seen = watch(rig)
    before = len(lines(rig))
    assert rig.state.job.kind == "" and rig.state.engine.resident == ""
    assert rig.core.jobs.start_switch(token(rig, MTL)).status == "started"
    assert (rig.state.job.phase, rig.state.job.source) == ("load", "")
    await rig.settle()
    assert new_lines(rig, before) == ["Loading chatterbox-multilingual", "Loaded chatterbox-multilingual · 14.2 s · 3.4 GB"]
    assert seen.texts == [("Multilingual ready · 14.2 s · 3.4 GB", False)]
    loaded = rig.state.history.events[-1]
    assert (loaded.kind, loaded.cold, loaded.device) == ("loaded", True, "cuda")
    assert history_kinds(rig) == [("loading", MTL), ("loaded", MTL)]


async def test_a_failed_load_drops_the_queue_and_the_after_action(rigs: Callable[..., Rig]) -> None:
    rig = rigs("failure")
    await rig.start()
    seen = watch(rig)
    rig.fake.reset_records()
    before = len(lines(rig))
    reqs = (turbo_request(), turbo_request(), turbo_request())
    started = rig.core.jobs.start_generate(reqs, token=token(rig, TUR, reason="generate", asked=True))
    assert started == StartResult("started") and rig.core.jobs.queued == reqs[1:]
    await rig.settle()
    assert new_lines(rig, before) == ["Switching Multilingual → Turbo",
                                      "Unloaded chatterbox-multilingual · freed 3.4 GB",
                                      "Load failed: chatterbox-turbo · CUDA out of memory"]
    assert seen.texts == [("Turbo failed to load: CUDA out of memory (2.1 GB free)", True)]
    assert (rig.state.engine.resident, rig.state.engine.failed) == ("", TUR)
    assert rig.core.jobs.queued == () and rig.state.job.kind == ""
    assert rig.calls("speak") == []
    failed = rig.state.history.events[-1]
    assert (failed.kind, failed.backend, failed.reason) == ("load_failed", TUR, OOM)


async def test_a_new_switch_clears_the_failed_state(rigs: Callable[..., Rig]) -> None:
    rig = rigs("failure")
    await rig.start()
    rig.core.jobs.start_switch(token(rig, TUR, asked=True))
    await rig.settle()
    assert rig.state.engine.failed == TUR
    assert rig.core.jobs.start_switch(token(rig, MTL, reason="retry")) == StartResult("refused", STALE)  # still low
    assert rig.core.jobs.start_switch(token(rig, MTL, reason="retry", asked=True)).status == "started"
    assert rig.state.engine.failed == ""
    await rig.settle()
    assert (rig.state.engine.resident, rig.state.engine.failed) == (MTL, "")


async def test_engine_busy_refuses_the_load_without_a_failed_state(rigs: Callable[..., Rig]) -> None:
    rig = rigs("firstrun")
    await rig.start()
    seen = watch(rig)
    holder = {"pid": 4242, "program": "cli.py", "since": "14:02:11"}
    rig.fake.fail_next("make_resident", EngineBusy(holder))
    before = len(lines(rig))
    rig.core.jobs.start_switch(token(rig, MTL))
    await rig.settle()
    assert new_lines(rig, before) == ["Loading chatterbox-multilingual",
                                      "Load refused: chatterbox-multilingual · cli.py (pid 4242) holds the engine"]
    assert seen.texts == [(str(EngineBusy(holder)), True)]
    assert (rig.state.engine.resident, rig.state.engine.failed) == ("", "")
    assert rig.fake.loads == []


async def test_free_vram_is_a_job_with_the_mockup_copy(speak: Rig) -> None:
    core = speak.core
    seen = watch(speak)
    before = len(lines(speak))
    hold = speak.fake.hold("unload_backend")
    assert core.jobs.start_free() == StartResult("started")
    job = speak.state.job
    assert (job.kind, job.source, job.target) == ("unload", MTL, "")
    await entered(hold)
    hold.release()
    await speak.settle()
    assert new_lines(speak, before) == ["Unloaded chatterbox-multilingual · freed 3.4 GB"]
    assert seen.texts == [("Freed 3.4 GB", False)]
    assert speak.state.engine.resident == "" and job.kind == ""
    unloaded = speak.state.history.events[-1]
    assert (unloaded.kind, unloaded.reason, unloaded.seconds) == ("unloaded", "free", None)
    assert core.jobs.start_free() == StartResult("refused", "Nothing to free · no model loaded")


async def test_an_idle_unload_names_the_idle_limit(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak", settings={"idle_unload_min": 10})
    await rig.start()
    seen = watch(rig)
    rig.core.jobs.start_free(reason="idle")
    await rig.settle()
    assert lines(rig)[-1] == "Unloaded chatterbox-multilingual · idle 10 min · freed 3.4 GB"
    assert seen.texts == [("Freed 3.4 GB · idle 10 min", False)]
    unloaded = rig.state.history.events[-1]
    assert (unloaded.reason, unloaded.seconds) == ("idle", 600)
    assert rig.calls("unload_backend")[-1].kwargs == {"reason": "idle"}


async def test_switch_and_free_refuse_while_any_job_runs(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("speak")
    assert jobs.start_generate([request()]).status == "started"
    switch = token(speak, TUR)
    assert jobs.start_switch(switch) == StartResult("refused", BUSY)
    assert jobs.start_free() == StartResult("refused", BUSY)
    assert jobs.queued == () and speak.state.job.queued == 0
    assert speak.core.tokens.peek(switch) is not None            # a busy refusal never spends the token
    await entered(hold)
    hold.release()
    await speak.settle()
    assert speak.fake.loads == []


# ---------- takes ----------

async def test_a_take_on_the_resident_backend(speak: Rig) -> None:
    core = speak.core
    seen = watch(speak)
    before = len(lines(speak))
    assert core.jobs.start_generate([request()]) == StartResult("started")
    job = speak.state.job
    assert (job.kind, job.phase, job.voice, job.take_no, job.target, job.queued) == (
        "generate", "preparing", "aiko", 8, MTL, 0)
    await speak.settle()
    assert new_lines(speak, before) == ["Generating take 8 · aiko · Multilingual", "Take 8 · 4.6 s audio in 6.9 s"]
    assert seen.texts == [("Take 8 ready · 4.6 s", False)]
    assert seen.ready == [8] and seen.sparks == ["take"]
    assert core.session.take_id == 8
    take = speak.state.takes.takes[0]
    assert (take.id, take.voice, take.backend, take.language, take.session) == (8, "aiko", MTL, "ja", core.session_id)
    assert take.gen_seconds == pytest.approx(6.9) and take.tokens == 115 and not take.hit_token_limit
    assert speak.state.takes.next_id == 9 and job.kind == ""
    assert speak.fake.loads == []


async def test_the_conditioning_line_comes_before_the_take_line(speak: Rig) -> None:
    before = len(lines(speak))
    speak.core.jobs.start_generate([request("marco", MTL, "Buongiorno! Che bella giornata.", "it")])
    await speak.settle()
    take = speak.state.takes.takes[0]
    assert new_lines(speak, before) == ["Generating take 8 · marco · Multilingual",
                                        "Voice marco · conditioning cached · 0.03 s",
                                        f"Take 8 · {take.seconds:.1f} s audio in {take.gen_seconds:.1f} s"]
    assert take.voice == "marco" and take.seconds > 1.0


async def test_a_cut_take_says_so(speak: Rig) -> None:
    speak.core.jobs.start_generate([request(text="a" * 800, language="en")])
    await speak.settle()
    assert lines(speak)[-1] == "Take 8 hit the 40 s limit · the end of the script was cut off"
    assert speak.state.takes.takes[0].hit_token_limit


async def test_generate_on_another_backend_needs_a_token(speak: Rig) -> None:
    jobs = speak.core.jobs
    assert jobs.start_generate([turbo_request()]) == StartResult("refused", STALE)
    assert jobs.start_generate([turbo_request()], token=token(speak, STD)) == StartResult("refused", STALE)
    assert speak.state.job.kind == ""
    before = len(lines(speak))
    assert jobs.start_generate([turbo_request()], token=token(speak, TUR, reason="generate")).status == "started"
    await speak.settle()
    take = speak.state.takes.takes[0]
    assert new_lines(speak, before) == ["Switching Multilingual → Turbo",
                                        "Unloaded chatterbox-multilingual · freed 3.4 GB",
                                        "Loaded chatterbox-turbo · 8.7 s · 3.1 GB",
                                        "Generating take 8 · host-turbo · Turbo",
                                        "Voice host-turbo · conditioning cached · 0.03 s",
                                        f"Take 8 · {take.seconds:.1f} s audio in {take.gen_seconds:.1f} s"]
    assert take.voice == "host-turbo" and take.exaggeration is None and take.cfg_weight is None
    assert speak.fake.loads == [TUR]


async def test_takes_queue_behind_a_running_take_up_to_three(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("speak")
    rev = speak.state.job.rev
    assert jobs.start_generate([request()]) == StartResult("started")
    assert jobs.start_generate([request(), request(), request()]) == StartResult("queued")
    assert speak.state.job.queued == 3 and len(jobs.queued) == 3
    assert jobs.start_generate([request()]) == StartResult("refused", QUEUE_FULL, 1)
    assert speak.state.job.queued == 3
    await entered(hold)
    hold.release()
    await speak.settle()
    assert [take.id for take in speak.state.takes.takes[:4]] == [11, 10, 9, 8]
    assert speak.state.job.rev == rev + 2                       # busy once, idle once: the slot was held throughout
    assert speak.fake.loads == []


async def test_extras_that_do_not_fit_are_dropped(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("speak")
    jobs.start_generate([request(), request(), request()])
    assert speak.state.job.queued == 2
    assert jobs.start_generate([request(), request(), request()]) == StartResult("queued", QUEUE_FULL, 2)
    assert speak.state.job.queued == 3
    await entered(hold)
    hold.release()
    await speak.settle()
    assert speak.state.takes.next_id == 12


async def test_queued_takes_are_snapshots(speak: Rig) -> None:
    core = speak.core
    hold = speak.fake.hold("speak")
    session = core.session
    session.voice_id, session.text, session.language = "aiko", "First line.", "en"

    def snapshot() -> GenerateRequest:
        return request(session.voice_id, MTL, session.text, session.language, session.exaggeration,
                       session.cfg_weight)

    core.jobs.start_generate([snapshot()])
    session.text = "Second line."
    core.jobs.start_generate([snapshot()])
    session.text, session.exaggeration = "Edited after the click.", 1.5
    await entered(hold)
    hold.release()
    await speak.settle()
    spoken = [(call.args[0], call.kwargs["exaggeration"]) for call in speak.calls("speak")]
    assert spoken == [("First line.", 0.5), ("Second line.", 0.5)]


async def test_another_backend_is_refused_while_a_take_runs(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("speak")
    jobs.start_generate([request()])
    jobs.start_generate([request()])
    turbo = token(speak, TUR, reason="generate")
    result = jobs.start_generate([turbo_request()], token=turbo)
    assert result == StartResult("refused", "Busy with Multilingual · host-turbo needs Turbo · switch after the queue")
    assert len(jobs.queued) == 1 and speak.state.job.queued == 1
    await entered(hold)
    hold.release()
    await speak.settle()
    assert speak.fake.loads == []
    assert {call.args[1] for call in speak.calls("speak")} == {"aiko"}


async def test_takes_for_the_switch_target_queue_behind_the_switch(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("make_resident")
    jobs.start_switch(token(speak, TUR))
    assert jobs.start_generate([turbo_request()]) == StartResult("queued")
    refused = jobs.start_generate([request()])
    assert refused == StartResult("refused", "Busy with Turbo · aiko needs Multilingual · switch after the queue")
    await entered(hold)
    hold.release()
    await speak.settle()
    assert [call.args[1] for call in speak.calls("speak")] == ["host-turbo"]
    assert speak.state.takes.takes[0].voice == "host-turbo"


async def test_an_unload_forge_or_test_line_refuses_takes(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("unload_backend")
    jobs.start_free()
    assert jobs.start_generate([request()]) == StartResult("refused", BUSY)
    assert jobs.start_test_line("aiko") == StartResult("refused", BUSY)
    await entered(hold)
    hold.release()
    await speak.settle()
    assert speak.calls("speak") == []


async def test_a_failed_take_clears_the_queue_and_keeps_the_model(speak: Rig) -> None:
    seen = watch(speak)
    speak.fake.fail_next("speak", GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate",
                                                  during="speak"))
    before = len(lines(speak))
    speak.core.jobs.start_generate([request(), request(), request()])
    await speak.settle()
    assert new_lines(speak, before) == ["Generating take 8 · aiko · Multilingual", "Take 8 failed · CUDA out of memory"]
    assert seen.texts == [("Take 8 failed: CUDA out of memory", True)]
    assert len(speak.calls("speak")) == 1 and speak.core.jobs.queued == ()
    assert speak.state.engine.resident == MTL and speak.state.takes.next_id == 8


async def test_cancel_queue_lets_the_running_take_finish(speak: Rig) -> None:
    jobs = speak.core.jobs
    assert jobs.cancel_queue() == 0
    hold = speak.fake.hold("speak")
    jobs.start_generate([request(), request(), request()])
    assert jobs.cancel_queue() == 2
    assert speak.state.job.queued == 0 and lines(speak)[-1] == "Cancelled 2 queued takes"
    await entered(hold)
    hold.release()
    await speak.settle()
    assert speak.state.takes.next_id == 9


async def test_a_take_that_cannot_be_saved(speak: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = watch(speak)
    store = speak.core.takes

    def broken(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("[WinError 5] Access is denied: 'aiko_take8.wav'")

    monkeypatch.setattr(store, "add", broken)
    speak.core.jobs.start_generate([request(), request()])
    await speak.settle()
    assert lines(speak)[-1] == "Take 8 couldn't be saved · [WinError 5] Access is denied: 'aiko_take8.wav'"
    assert seen.texts == [("Couldn't save Take 8: [WinError 5] Access is denied: 'aiko_take8.wav'", True)]
    assert len(speak.calls("speak")) == 1

    def unlisted(*args: Any, **kwargs: Any) -> None:
        raise TakeNotListedError(8, Path("E:/takes/aiko_take8.wav"), "the disk is full")

    monkeypatch.setattr(store, "add", unlisted)
    speak.core.jobs.start_generate([request()])
    await speak.settle()
    assert lines(speak)[-1] == "Take 8 couldn't be saved · the disk is full"
    assert seen.texts[-1] == (str(TakeNotListedError(8, Path("E:/takes/aiko_take8.wav"), "the disk is full")), True)


async def test_elapsed_audio_and_fill_come_from_the_manual_clock_and_the_hook(speak: Rig) -> None:
    hold = speak.fake.hold("speak")
    speak.core.jobs.start_generate([request()])
    await entered(hold)
    job = speak.state.job
    assert (job.elapsed_s, job.fill) == (0.0, 0.0)
    speak.clock.advance(1.0)
    assert job.elapsed_s == 1.0 and job.phase == "sampling" and job.audio_s == pytest.approx(0.04)
    assert job.fill == pytest.approx(0.04 / 4.9, abs=1e-4)          # tokens / the median of takes 5-7
    speak.clock.advance(1.2)
    assert job.elapsed_s == pytest.approx(2.2)
    hold.release()
    await speak.settle()
    assert job.kind == "" and job.elapsed_s == 0.0


async def test_without_three_comparable_takes_the_take_fill_is_indeterminate(speak: Rig) -> None:
    hold = speak.fake.hold("speak")
    speak.core.jobs.start_generate([request(text="Hello there.", language="en")])
    await entered(hold)
    speak.clock.advance(0.4)
    assert speak.state.job.fill is None and speak.state.job.elapsed_s == pytest.approx(0.4)
    hold.release()


async def test_cancel_running_stops_a_held_take(speak: Rig) -> None:
    jobs = speak.core.jobs
    hold = speak.fake.hold("speak")
    jobs.start_generate([request(), request()])
    await entered(hold)
    jobs.cancel_running()
    await speak.settle()
    assert speak.state.job.kind == "" and jobs.queued == ()
    assert speak.state.takes.next_id == 8 and len(speak.calls("speak")) == 1
    assert speak.calls("speak")[0].kwargs["job"].status().phase == "cancelled"


async def test_wait_idle(speak: Rig) -> None:
    hold = speak.fake.hold("speak")
    speak.core.jobs.start_generate([request()])
    waiter = asyncio.create_task(speak.core.jobs.wait_idle())
    await entered(hold)
    assert not waiter.done()
    hold.release()
    await asyncio.wait_for(waiter, 10)
    assert speak.state.job.kind == ""


# ---------- forge ----------

async def stage(rig: Rig, name: str = "sample.wav") -> Any:
    return await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / name)


def forge_request(clip: Any, *, name: str = "rin", backend: str = MTL, language: str = "ja") -> ForgeRequest:
    return ForgeRequest(voice_id=name, clip=clip, backend=backend, language=language, exaggeration=0.5, consent=True)


async def test_a_forge_on_the_resident_model(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_ready")
    await rig.start()
    core = rig.core
    seen = watch(rig)
    core.forge_draft.name = "rin"
    clip = await stage(rig)
    library_rev = rig.state.library.rev
    stale = token(rig, TUR)
    before = len(lines(rig))
    assert core.jobs.start_forge(forge_request(clip)) == StartResult("started")
    job = rig.state.job
    assert (job.kind, job.phase, job.voice, job.target, job.fill) == ("forge", "bake", "rin", MTL, 0.0)
    await rig.settle()
    assert new_lines(rig, before) == ["Forging rin · Multilingual", "Cached cache/chatterbox-multilingual/rin.pt"]
    assert seen.texts == [("Forged rin · Multilingual · 2.1 s", False)]
    assert seen.forging == ["rin"] and seen.forged == ["rin"]
    assert (rig.paths.voices / "rin_ref.wav").is_file()
    assert "rin" in {row.voice_id for row in rig.state.library.rows} and rig.state.library.rev > library_rev
    assert core.tokens.redeem(stale) is None
    forged = rig.state.history.events[-1]
    assert (forged.kind, forged.backend, forged.seconds) == ("forged", MTL, 2.1)
    assert (core.forge_draft.forged, rig.fake.loads) == ("rin", [])


async def test_a_failed_forge_rolls_back_the_clip_it_committed(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_failed")
    await rig.start()
    seen = watch(rig)
    clip = await stage(rig)
    before = len(lines(rig))
    rig.core.jobs.start_forge(forge_request(clip))
    await rig.settle()
    assert new_lines(rig, before) == ["Forging rin · Multilingual", "Forge failed: rin · CUDA out of memory"]
    assert seen.texts == [("Forge failed: CUDA out of memory", True)] and seen.forged == []
    assert not (rig.paths.voices / "rin_ref.wav").exists() and Path(clip.staged).is_file()
    assert rig.state.engine.resident == MTL and "rin" not in {row.voice_id for row in rig.state.library.rows}


async def test_a_forge_on_another_backend_switches_first(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_other_backend")
    await rig.start()
    jobs = rig.core.jobs
    clip = await stage(rig)
    req = forge_request(clip, backend=TUR, language="en")
    assert jobs.start_forge(req) == StartResult("refused", STALE)
    before = len(lines(rig))
    assert jobs.start_forge(req, token=token(rig, TUR, reason="forge", after=AfterForge(req))).status == "started"
    await rig.settle()
    assert new_lines(rig, before) == ["Switching Multilingual → Turbo",
                                      "Unloaded chatterbox-multilingual · freed 3.4 GB",
                                      "Loaded chatterbox-turbo · 8.7 s · 3.1 GB",
                                      "Forging rin · Turbo",
                                      "Cached cache/chatterbox-turbo/rin.pt"]
    assert rig.fake.loads == [TUR] and len(rig.calls("load_voice")) == 1


async def test_the_popover_primary_runs_the_after_action(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_low_vram")
    await rig.start()
    clip = await stage(rig)
    req = forge_request(clip, backend=TUR, language="en")
    plan_token = token(rig, TUR, reason="forge", after=AfterForge(req))
    assert rig.core.jobs.start_switch(plan_token) == StartResult("refused", STALE)   # low VRAM: named → ask
    asked = token(rig, TUR, reason="forge", after=AfterForge(req), asked=True)
    assert rig.core.jobs.start_switch(asked).status == "started"
    await rig.settle()
    assert lines(rig)[-2:] == ["Forging rin · Turbo", "Cached cache/chatterbox-turbo/rin.pt"]


async def test_a_switch_for_takes_from_the_popover(speak: Rig) -> None:
    reqs = (turbo_request(), turbo_request())
    started = speak.core.jobs.start_switch(token(speak, TUR, reason="generate", after=AfterGenerate(reqs)))
    assert started.status == "started" and speak.state.job.queued == 1
    await speak.settle()
    assert [take.voice for take in speak.state.takes.takes[:2]] == ["host-turbo", "host-turbo"]
    assert speak.state.takes.next_id == 10


async def test_a_switch_to_the_resident_model_only_runs_its_after_action(speak: Rig) -> None:
    jobs = speak.core.jobs
    assert jobs.start_switch(token(speak, MTL)) == StartResult("refused", STALE)
    assert jobs.start_switch(token(speak, MTL, reason="generate", after=AfterGenerate((request(),)))).status == "started"
    await speak.settle()
    assert speak.fake.loads == [] and speak.calls("make_resident") == []
    assert speak.state.takes.next_id == 9


async def test_a_failed_switch_never_forges(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_other_backend")
    await rig.start()
    rig.fake.fail_next("make_resident", BackendLoadError(TUR, "missing_files", "LocalEntryNotFoundError", 0.3))
    clip = await stage(rig)
    req = forge_request(clip, backend=TUR, language="en")
    rig.core.jobs.start_forge(req, token=token(rig, TUR, reason="forge"))
    await rig.settle()
    assert lines(rig)[-1] == ("Load failed: chatterbox-turbo · model files are not in the local cache "
                              "(Offline Mode is on)")
    assert rig.calls("load_voice") == [] and not (rig.paths.voices / "rin_ref.wav").exists()


async def test_a_forge_needs_consent_and_a_free_slot(speak: Rig) -> None:
    clip = await stage(speak, "aiko_ref.wav")
    unconfirmed = replace(forge_request(clip), consent=False)
    assert speak.core.jobs.start_forge(unconfirmed) == StartResult("refused", "Confirm Consent First")
    hold = speak.fake.hold("speak")
    speak.core.jobs.start_generate([request()])
    assert speak.core.jobs.start_forge(forge_request(clip)) == StartResult("refused", BUSY)
    await entered(hold)
    hold.release()


# ---------- test lines ----------

async def test_a_test_line_is_played_and_never_saved(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_ready")
    await rig.start()
    seen = watch(rig)
    clip = await stage(rig)
    rig.core.jobs.start_forge(forge_request(clip))
    await rig.settle()
    next_id, urls = rig.state.takes.next_id, []
    assert rig.core.jobs.start_test_line("rin", on_done=urls.append) == StartResult("started")
    job = rig.state.job
    assert (job.kind, job.take_no, job.voice) == ("generate", 0, "rin")
    await rig.settle()
    assert urls == ["/media/staged/test-rin?v=1"]
    assert (rig.paths.staging / "test-rin.wav").read_bytes()[:4] == b"RIFF"
    call = rig.calls("speak")[-1]
    assert call.args == (TEST_LINE_JA, "rin") and call.kwargs["language_id"] == "ja"
    assert rig.state.takes.next_id == next_id and seen.ready == []
    rig.core.jobs.start_test_line("rin", on_done=urls.append)
    await rig.settle()
    assert urls[-1] == "/media/staged/test-rin?v=2"


async def test_a_test_line_needs_a_known_resident_voice(speak: Rig) -> None:
    jobs = speak.core.jobs
    assert jobs.start_test_line("zed") == StartResult("refused", "zed no longer exists")
    assert jobs.start_test_line("host-turbo") == StartResult("refused", "Turbo is not loaded")
    seen = watch(speak)
    speak.fake.fail_next("speak", GenerationError("other", "the decoder failed", during="speak"))
    assert jobs.start_test_line("marco").status == "started"
    await speak.settle()
    assert lines(speak)[-1] == "Test line failed · the decoder failed"
    assert seen.texts == [("Test line failed: the decoder failed", True)]
    english = speak.calls("speak")[-1]
    assert english.args == ("Hello, this is marco.", "marco") and english.kwargs["language_id"] == "en"


async def test_every_take_and_bake_requires_residency(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_other_backend")
    await rig.start()
    jobs = rig.core.jobs
    clip = await stage(rig)
    jobs.start_generate([request(), request()])
    await rig.settle()
    jobs.start_forge(forge_request(clip, backend=TUR, language="en"), token=token(rig, TUR, reason="forge"))
    await rig.settle()
    jobs.start_test_line("rin")
    await rig.settle()
    calls = rig.calls("speak") + rig.calls("load_voice")
    assert len(calls) == 4
    assert all(call.kwargs["require_resident"] is True and call.kwargs["job"] is not None for call in calls)
    assert all(call.kwargs["force"] is True for call in rig.calls("load_voice"))


async def test_failures_reach_the_log_as_text_only(speak: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    speak.fake.fail_next("speak", GenerationError("other", "the sampler blew up", during="speak"))
    speak.core.jobs.start_generate([request()])
    await speak.settle()
    speak.fake.fail_next("make_resident", BackendLoadError(TUR, "out_of_memory", "CUDA out of memory", 6.1))
    speak.core.jobs.start_switch(token(speak, TUR))
    await speak.settle()
    assert "Take 8 failed · the sampler blew up" in lines(speak)
    assert "Load failed: chatterbox-turbo · CUDA out of memory" in lines(speak)
    assert caplog.records and all(r.exc_info is None and r.exc_text is None for r in caplog.records)


async def test_no_job_ever_leaves_two_models_or_loads_without_a_token(speak: Rig) -> None:
    jobs = speak.core.jobs
    for backend in (TUR, STD, MTL):
        jobs.start_switch(token(speak, backend))
        await speak.settle()
        assert speak.fake.residency().resident == (backend,)
    assert speak.fake.loads == [TUR, STD, MTL]
    assert jobs.start_generate([turbo_request()]) == StartResult("refused", STALE)
    assert speak.fake.loads == [TUR, STD, MTL]
