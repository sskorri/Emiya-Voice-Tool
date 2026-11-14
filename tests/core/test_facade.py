"""EngineFacade (§4.3-4.4) against FakeEngine and a stub engine; also the shared Core test rig.

The rig (make_rig / the `rigs` fixture) builds a real Core around a FakeEngine on a tmp data root with the
manual clock and a recording trash; test_jobs, test_background, test_media, test_tokens and test_planner
import it from here.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import threading
import weakref
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import filelock
import pytest

from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError
from engine.progress import Job
from engine.types import EngineEvent, Residency
from ui.contract.plans import ForgeRequest, GenerateRequest
from ui.contract.ports import AudioOutPort
from ui.contract.state import AppState
from ui.contract.theme import CoreEventsView, CoreView
from ui.core import Core, build_core
from ui.core import facade as facade_module
from ui.core.clock import ManualScheduler
from ui.core.events import CoreEvents
from ui.core.facade import BUSY, ONE_MODEL, OOM, STALE, EngineFacade, EventRelay, Outcome, describe_error
from ui.core.fake import SCRIPT_JA, SCRIPT_TURBO, FakeEngine, Hold, Scenario
from ui.core.paths import Paths
from ui.core.services.settings import SettingsStore
from ui.core.tokens import LoadTicket

from tests.theme_settings import EMIYA_TITLE, emiya_settings

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
GIB = 2 ** 30


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


# ---------- the shared rig ----------

@dataclass
class Rig:
    """A real Core over a FakeEngine: manual clock, tmp data root, recording trash."""

    core: Core
    fake: FakeEngine
    clock: ManualScheduler
    paths: Paths
    trashed: list[Path]

    @property
    def state(self) -> AppState:
        return self.core.state

    async def start(self, *, connect: bool = True) -> None:
        """core.start(), its checks, then (by default) the first client and its device reading."""
        await self.core.start()
        await self.clock.settle()
        if connect:
            self.core.client_connected()
            await self.clock.settle()

    async def settle(self) -> None:
        await self.clock.settle()

    def calls(self, name: str) -> list[Any]:
        return [call for call in self.fake.calls if call.name == name]


def make_rig(root: Path, scenario: str | Scenario = "speak", *, settings: dict[str, Any] | None = None,
             audio_output: AudioOutPort | None = None,
             **fake_options: Any) -> Rig:
    """Build (not start) a rig; settings is the "core" section to write into ui_settings.json first."""
    paths = Paths.for_run(root / "app", data_root=root / "data", fake=True)
    fake = FakeEngine(scenario, data_dir=paths.root, **fake_options)
    if settings:
        paths.settings.write_text(json.dumps({"version": 1, "core": settings}), encoding="utf-8")
    trashed: list[Path] = []

    def trash(path: Path) -> None:
        trashed.append(Path(path))
        Path(path).unlink()

    clock = ManualScheduler()
    core = build_core(engine=fake, paths=paths, settings=emiya_settings(paths.settings), trash=trash, scheduler=clock,
                      app_name=EMIYA_TITLE, audio_output=audio_output)
    return Rig(core=core, fake=fake, clock=clock, paths=paths, trashed=trashed)


@pytest.fixture
async def rigs(tmp_path: Path) -> AsyncIterator[Callable[..., Rig]]:
    """make(scenario="speak", settings=None, **fake_options) -> Rig; holds released and work settled at teardown."""
    made: list[Rig] = []

    def make(scenario: str | Scenario = "speak", **options: Any) -> Rig:
        rig = make_rig(tmp_path / f"rig{len(made)}", scenario, **options)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.fake.release_all()
    for rig in made:
        with contextlib.suppress(Exception):
            await rig.clock.settle(timeout=10)
        rig.clock.cancel_pending()


@pytest.fixture
async def speak(rigs: Callable[..., Rig]) -> Rig:
    """The started "speak" rig: Multilingual resident, the mockup's voices and takes 1-7."""
    rig = rigs("speak")
    await rig.start()
    return rig


async def entered(hold: Hold, timeout: float = 10.0) -> None:
    """Wait (without blocking the loop) until a call reached the hold."""
    assert await asyncio.to_thread(hold.wait_entered, timeout), f"nothing reached the {hold.point} hold"


def request(voice: str = "aiko", backend: str = MTL, text: str = SCRIPT_JA, language: str = "ja",
            exaggeration: float | None = 0.5, cfg_weight: float | None = 0.5) -> GenerateRequest:
    return GenerateRequest(voice_id=voice, backend=backend, text=text, language=language,
                           exaggeration=exaggeration, cfg_weight=cfg_weight)


def turbo_request(text: str = SCRIPT_TURBO) -> GenerateRequest:
    return request("host-turbo", TUR, text, "en", None, None)


def ticket_for(core: Core, backend: str, *, reason: str = "explicit", asked: bool = False) -> LoadTicket:
    ticket = core.tokens.redeem(core.tokens.mint(core.planner.switch_plan(backend, reason=reason), asked=asked))
    assert ticket is not None
    return ticket


# ---------- a stub engine for the outcome table ----------

class StubEngine:
    """Enough engine for EngineFacade; voice_details() raises whatever `make_error` builds (in the worker)."""

    def __init__(self, make_error: Callable[[], BaseException] | None = None) -> None:
        self.make_error = make_error
        self.calls: list[str] = []
        self.made: list[weakref.ref[BaseException]] = []
        self.result: Any = []
        self.state = Residency(resident=(MTL,), loading=None, unloading=None, busy=None, device="cuda",
                               preferred_device="cuda", max_resident=1, last_used=None, lock_held=True)

    def residency(self) -> Residency:
        return self.state

    def list_backends(self) -> list[str]:
        return [MTL, STD, TUR]

    def voice_details(self, voice_id: str | None = None) -> Any:
        self.calls.append("voice_details")
        if self.make_error is not None:
            error = self.make_error()
            with contextlib.suppress(TypeError):        # built-in exception types take no weak references
                self.made.append(weakref.ref(error))
            raise error
        return self.result


def stub_facade(engine: StubEngine) -> EngineFacade:
    state = AppState()
    facade = EngineFacade(engine, state=state, events=CoreEvents(), trash=lambda path: None, relay=EventRelay())
    facade.sync_state()
    return facade


class OutOfMemoryError(RuntimeError):
    """Named like torch.cuda.OutOfMemoryError (ui.core never imports torch)."""


HOLDER = {"pid": 4242, "program": "cli.py", "since": "14:02:11"}
BUSY_TEXT = "The voice engine is in use by cli.py (pid 4242) since 14:02:11. Close it, or wait for it to finish, then try again."

OUTCOMES = [
    ("cancelled", lambda: Cancelled(), "cancelled", ""),
    ("not resident", lambda: BackendNotResident(TUR, (MTL,)), "refused", "Turbo is not loaded"),
    ("engine busy", lambda: EngineBusy(HOLDER), "refused", BUSY_TEXT),
    ("load oom", lambda: BackendLoadError(TUR, "out_of_memory", "CUDA out of memory. Tried to allocate 512.00 MiB",
                                          6.1), "failed", OOM),
    ("load other", lambda: BackendLoadError(TUR, "other", "the checkpoint is truncated", 1.0), "failed",
     "the checkpoint is truncated"),
    ("take oom", lambda: GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate 64.00 MiB",
                                         during="speak"), "failed", OOM),
    ("take other", lambda: GenerationError("other", "index out of range", during="speak"), "failed",
     "index out of range"),
    ("registry busy", lambda: filelock.Timeout("registry.json.lock"), "failed", "The voice registry is busy; try again."),
    ("import", lambda: ModuleNotFoundError("No module named 'soundfile'", name="soundfile"), "failed",
     "missing dependency: soundfile"),
    ("value", lambda: ValueError("Voice 'zed' is not registered.\nsecond line"), "failed",
     "Voice 'zed' is not registered."),
    ("torch oom by name", lambda: OutOfMemoryError("Tried to allocate 2.00 GiB"), "failed", OOM),
    ("oom in the text", lambda: RuntimeError("CUDA error: out of memory"), "failed", OOM),
    ("generic", lambda: RuntimeError("something broke\ntraceback noise"), "failed", "something broke"),
]


@pytest.mark.parametrize(("make_error", "status", "reason"), [case[1:] for case in OUTCOMES],
                         ids=[case[0] for case in OUTCOMES])
async def test_every_exception_maps_to_its_outcome(make_error: Callable[[], BaseException], status: str,
                                                   reason: str) -> None:
    facade = stub_facade(StubEngine(make_error))
    out = await facade.voice_details()
    assert (out.status, out.reason) == (status, reason)
    assert out.value == (HOLDER if reason == BUSY_TEXT else None)


async def test_a_done_call_carries_its_value() -> None:
    engine = StubEngine()
    engine.result = ["a", "b"]
    out = await stub_facade(engine).voice_details()
    assert out == Outcome("done", ["a", "b"]) and out.ok


@pytest.mark.parametrize(("offline", "text"), [("1", "model files are not in the local cache (Offline Mode is on)"),
                                               ("0", "model files are not in the local cache")])
def test_missing_files_names_offline_mode(monkeypatch: pytest.MonkeyPatch, offline: str, text: str) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", offline)
    assert describe_error(BackendLoadError(TUR, "missing_files", "LocalEntryNotFoundError: ...", 0.2)) == text


def test_long_texts_are_cut_to_80_characters() -> None:
    reason = describe_error(GenerationError("other", "x" * 300, during="bake"))
    assert len(reason) == 80 and reason.endswith("…")
    assert describe_error(RuntimeError("y" * 80)) == "y" * 80
    assert describe_error(RuntimeError("")) == "RuntimeError"


async def test_a_none_from_io_bound_means_cancelled_and_resyncs(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine()
    facade = stub_facade(engine)
    state = facade._state
    assert state.engine.resident == MTL

    async def cancelled(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(facade_module.run, "io_bound", cancelled)
    engine.state = replace(engine.state, resident=(), device=None, lock_held=False)
    out = await facade.voice_details()
    assert out == Outcome("cancelled")
    assert (state.engine.resident, state.engine.device) == ("", "")


async def test_no_exception_object_outlives_the_call() -> None:
    engine = StubEngine(lambda: GenerationError("other", "the model fell over", during="speak"))
    facade = stub_facade(engine)
    out = await facade.voice_details()
    assert out.status == "failed" and isinstance(out.reason, str)
    assert engine.made
    for _ in range(100):                                # the worker thread and the loop drop their own references
        await asyncio.sleep(0.01)                       # a moment later; only a reference we kept would last
        gc.collect()
        if all(ref() is None for ref in engine.made):
            break
    assert all(ref() is None for ref in engine.made)
    assert facade is not None                           # the facade itself is still alive and holds nothing


async def test_failures_are_logged_as_text_without_exc_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out = await stub_facade(StubEngine(lambda: GenerationError("out_of_memory", "CUDA out of memory", during="speak"))
                            ).voice_details()
    assert out.reason == OOM
    warnings = [r for r in caplog.records if r.name == "voicetool.core"]
    assert [r.getMessage() for r in warnings] == ["voice_details failed: CUDA out of memory"]
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)


# ---------- against FakeEngine ----------

async def test_speak_is_refused_without_an_engine_call_when_not_resident(speak: Rig) -> None:
    speak.fake.reset_records()
    out = await speak.core.facade.speak(turbo_request(), Job())
    assert out == Outcome("refused", reason="Turbo is not loaded")
    assert speak.calls("speak") == []


async def test_speak_refuses_an_empty_script(speak: Rig) -> None:
    speak.fake.reset_records()
    out = await speak.core.facade.speak(request(text="   "), Job())
    assert out == Outcome("refused", reason="Nothing to say: the text is empty.")
    assert speak.calls("speak") == []


async def test_speak_requires_residency_and_sends_the_knobs(speak: Rig) -> None:
    job = Job()
    out = await speak.core.facade.speak(request(exaggeration=0.7, cfg_weight=0.3), job)
    assert out.ok and out.value[:4] == b"RIFF"
    call = speak.calls("speak")[-1]
    assert call.args == (SCRIPT_JA, "aiko")
    assert call.kwargs == {"require_resident": True, "job": job, "language_id": "ja", "exaggeration": 0.7,
                           "cfg_weight": 0.3}
    assert job.status().phase == "done" and job.made_s == pytest.approx(6.9)


async def test_a_turbo_take_sends_only_the_language(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start()
    assert (await rig.core.facade.load(ticket_for(rig.core, TUR))).ok
    out = await rig.core.facade.speak(turbo_request(), Job())
    assert out.ok
    assert set(rig.calls("speak")[-1].kwargs) == {"require_resident", "job", "language_id"}


async def test_load_needs_a_ticket_from_its_token_store_used_once(speak: Rig) -> None:
    core, fake = speak.core, speak.fake
    fake.reset_records()
    assert await core.facade.load(None) == Outcome("refused", reason=STALE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LoadTicket(core.planner.switch_plan(TUR, reason="explicit"), 99)
    ticket = ticket_for(core, TUR)
    first = await core.facade.load(ticket)
    assert first.ok and first.value.backend == TUR and not first.value.already_resident
    assert await core.facade.load(ticket) == Outcome("refused", reason=STALE)
    assert fake.loads == [TUR] and len(speak.calls("make_resident")) == 1
    assert core.state.engine.resident == TUR


async def test_load_is_refused_unless_the_app_keeps_one_model(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak", max_resident=2)
    await rig.start()
    assert rig.state.engine.max_resident == 2
    rig.fake.reset_records()
    out = await rig.core.facade.load(ticket_for(rig.core, TUR))
    assert out == Outcome("refused", reason=ONE_MODEL)
    assert rig.fake.loads == [] and rig.calls("make_resident") == []


async def test_unload_needs_the_mirrored_resident(speak: Rig) -> None:
    speak.fake.reset_records()
    assert await speak.core.facade.unload(TUR, reason="requested") == Outcome("refused", reason="Turbo is not loaded")
    assert speak.calls("unload_backend") == []
    out = await speak.core.facade.unload(MTL, reason="requested")
    assert out == Outcome("done", True)
    assert speak.calls("unload_backend")[0].kwargs == {"reason": "requested"}
    assert speak.state.engine.resident == ""


async def test_bake_always_forces_on_the_resident_model_with_the_consent_meta(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_ready")
    await rig.start()
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / "sample.wav")
    path, created = await asyncio.to_thread(rig.core.clips.commit, clip, "rin")
    assert created
    req = ForgeRequest(voice_id="rin", clip=clip, backend=MTL, language="ja", exaggeration=0.5, consent=True)
    job = Job()
    out = await rig.core.facade.bake(req, path, job)
    assert out == Outcome("done", "rin")
    call = rig.calls("load_voice")[-1]
    assert call.args == ("rin", path)
    meta = call.kwargs.pop("meta")
    assert call.kwargs == {"backend": MTL, "exaggeration": 0.5, "language": "ja", "force": True,
                           "require_resident": True, "job": job}
    assert meta["consent"] is True and meta["consent_at"].endswith("+00:00")
    assert meta["clip_seconds"] == pytest.approx(13.42, abs=0.02) and meta["clip_source"].endswith("sample.wav")
    assert job.bake_s == pytest.approx(2.1)


async def test_bake_is_refused_for_another_backend_or_without_consent(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_ready")
    await rig.start()
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / "sample.wav")
    rig.fake.reset_records()
    turbo = ForgeRequest(voice_id="rin", clip=clip, backend=TUR, language="en", exaggeration=0.5, consent=True)
    assert await rig.core.facade.bake(turbo, clip.staged, Job()) == Outcome("refused", reason="Turbo is not loaded")
    unconfirmed = replace(turbo, backend=MTL, consent=False)
    assert (await rig.core.facade.bake(unconfirmed, clip.staged, Job())).status == "refused"
    assert rig.calls("load_voice") == []


async def test_a_cancelled_take_resyncs_the_residency(speak: Rig) -> None:
    core = speak.core
    hold = speak.fake.hold("speak")
    task = asyncio.create_task(core.facade.speak(request(), Job()))
    await entered(hold)
    core.state.engine.resident = ""                     # the mirror drifted; the cancel must re-read it
    task.cancel()
    out = await task
    assert out == Outcome("cancelled")
    assert core.state.engine.resident == MTL
    hold.release()


async def test_a_cancelled_job_comes_back_cancelled(speak: Rig) -> None:
    hold = speak.fake.hold("speak")
    job = Job()
    task = asyncio.create_task(speak.core.facade.speak(request(), job))
    await entered(hold)
    job.cancel()
    assert (await task) == Outcome("cancelled")
    assert job.status().phase == "cancelled"


async def test_generation_errors_map_to_the_spec_copy(speak: Rig) -> None:
    speak.fake.fail_next("speak", GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate", during="speak"))
    assert await speak.core.facade.speak(request(), Job()) == Outcome("failed", reason=OOM)
    speak.fake.fail_next("speak", GenerationError("other", "probability tensor contains nan", during="speak"))
    assert await speak.core.facade.speak(request(), Job()) == Outcome("failed", reason="probability tensor contains nan")


async def test_delete_passes_the_injected_trash_and_waits_for_no_job(speak: Rig) -> None:
    core = speak.core
    core.state.job.kind = "generate"
    speak.fake.reset_records()
    assert await core.facade.delete_voice("marco") == Outcome("refused", reason=BUSY)
    assert speak.calls("delete_voice") == []
    core.state.job.kind = ""
    out = await core.facade.delete_voice("marco")
    assert out.ok and out.value.removed_entry
    assert speak.calls("delete_voice")[0].kwargs["discard"] is core.trash
    assert speak.trashed == [speak.paths.cache / MTL / "marco.pt"]


async def test_update_voice_is_refused_for_the_voice_being_forged(speak: Rig) -> None:
    core = speak.core
    core.state.job.kind, core.state.job.voice = "forge", "aiko"
    assert await core.facade.update_voice("aiko", language="en") == Outcome("refused", reason="aiko is being forged now")
    out = await core.facade.update_voice("marco", language="es")
    assert out.ok and out.value["language"] == "es"


async def test_weights_come_from_the_startup_cache(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    assert rig.core.facade.weights_bytes(TUR) is None
    await rig.start()
    assert rig.core.facade.weights_bytes(TUR) == 2_986_050_872
    assert rig.core.facade.weights_bytes(MTL) == 3_206_854_222
    assert {call.args[0] for call in rig.calls("weights_status")} == {MTL, STD, TUR}


async def test_engine_events_reach_the_loop_in_order_and_update_the_mirror(speak: Rig) -> None:
    core = speak.core
    seen: list[tuple[str, str, str | None]] = []
    threads: set[int] = set()

    def listener(event: EngineEvent) -> None:
        threads.add(threading.get_ident())
        seen.append((event.kind, event.backend, event.reason))

    core.facade.listen(listener)
    assert (await core.facade.load(ticket_for(core, TUR))).ok
    await speak.settle()
    assert seen == [("load_started", TUR, None), ("unloaded", MTL, "evicted"), ("loaded", TUR, None)]
    assert threads == {threading.get_ident()}                       # the loop's thread, not the worker's
    engine_threads = {call.thread for call in speak.calls("make_resident")}
    assert threading.get_ident() not in engine_threads
    assert (core.state.engine.resident, core.state.engine.device) == (TUR, "cuda")


async def test_set_preferred_device_is_sync_and_checked(speak: Rig) -> None:
    facade = speak.core.facade
    assert facade.set_preferred_device("cpu") == Outcome("done")
    assert facade.preferred_device() == "cpu" and speak.fake.residency().preferred_device == "cpu"
    out = facade.set_preferred_device("mps")
    assert out.status == "failed" and "mps" in out.reason and facade.preferred_device() == "cpu"


def test_build_core_needs_a_relay_for_an_engine_without_on_event(tmp_path: Path) -> None:
    paths = Paths.for_run(tmp_path / "app", data_root=tmp_path / "data", fake=False)
    with pytest.raises(TypeError, match="relay"):
        build_core(engine=StubEngine(), paths=paths, settings=SettingsStore(paths.settings), trash=lambda p: None,
                   scheduler=ManualScheduler())
    core = build_core(engine=StubEngine(), paths=paths, settings=SettingsStore(paths.settings),
                      trash=lambda p: None, scheduler=ManualScheduler(), relay=EventRelay())
    assert core.state.engine.enabled == (MTL, STD, TUR) and core.state.engine.resident == MTL


def test_paths_for_a_real_a_fake_and_a_scratch_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "Voice Tool"
    real = Paths.for_run(app, data_root=None, fake=False)
    assert (real.app_root, real.root, real.cache, real.voices) == (app, app, app / "cache", app / "voices" / "reference")
    assert (real.takes_default, real.takes_index) == (app / "outputs" / "takes", app / "outputs" / "takes" / "index.json")
    assert (real.history, real.staging, real.settings, real.logs) == (
        app / "cache" / "load_history.jsonl", app / "cache" / "staging", app / "ui_settings.json", app / "logs")
    assert (real.gui_lock, real.engine_lock) == (app / "cache" / "gui.lock", app / "cache" / "engine.lock")
    fake = Paths.for_run(app, data_root=None, fake=True)
    assert fake.root == app / ".dev" and fake.settings == app / ".dev" / "ui_settings.json"
    assert (fake.gui_lock, fake.engine_lock, fake.logs) == (
        app / "cache" / "gui-dev.lock", app / "cache" / "engine.lock", app / "logs")
    monkeypatch.chdir(tmp_path)
    scratch = Paths.for_run(Path("Voice Tool"), data_root=Path("scratch"), fake=True)
    assert scratch.root == tmp_path / "scratch" and scratch.app_root == app
    assert scratch.engine_lock == app / "cache" / "engine.lock"
    assert not app.exists() and not (tmp_path / "scratch").exists()


def test_the_core_is_a_core_view_and_holds_no_engine(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert isinstance(rig.core, CoreView) and isinstance(rig.core.events, CoreEventsView)
    assert all(value is not rig.fake for value in vars(rig.core).values())
    assert rig.fake.on_event is not None and rig.core.session_id == rig.fake.session_id
    assert rig.core.forge_draft.backend == MTL and rig.core.forge_draft.language == "en"
