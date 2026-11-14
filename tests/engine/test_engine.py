"""engine.engine.VoiceEngine with the shared fake factories (BUILD_SPEC §3.10, §3.12).

No model loads and no real CUDA: torch.cuda appears only as a fake module, or, in the
tests marked slow, as the real one with every allocator call replaced by a spy.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import types
import warnings
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from engine import engine as engine_module
from engine import registry as registry_module
from engine.backends.info import BACKENDS
from engine.engine import VoiceEngine
from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError
from engine.lock import EngineLock
from engine.progress import Job
from engine.types import DeleteResult, EngineEvent, LoadReport, Residency, VoiceDetails

ROOT = Path(__file__).resolve().parents[2]
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
ALL = [MTL, STD, TUR]
GIB = 2**30
TIMEOUT_S = 10.0
ALLOCATOR_CALLS = ("memory_reserved", "memory_allocated", "max_memory_reserved",
                   "reset_peak_memory_stats", "empty_cache")


# ---------- fixtures and helpers ----------

@pytest.fixture
def events() -> list[EngineEvent]:
    return []


@pytest.fixture
def engine(scratch_config, fake_factories, events) -> Iterable[VoiceEngine]:
    voice_engine = VoiceEngine(scratch_config, on_event=events.append)
    yield voice_engine
    voice_engine.close()


@pytest.fixture
def pool(fake_factories) -> Iterable[concurrent.futures.ThreadPoolExecutor]:
    """Background calls; every gate is released before the workers are joined."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    yield executor
    fake_factories.release_all()
    executor.shutdown(wait=True)


def kinds(events: list[EngineEvent]) -> list[tuple[str, str, str | None]]:
    return [(event.kind, event.backend, event.reason) for event in events]


def write_pt(engine: VoiceEngine, backend: str, voice_id: str) -> Path:
    """A <voice_id>.pt in backend's cache folder; on its own (no entry) it is an orphan."""
    pt = engine.config.CACHE_DIR / backend / f"{voice_id}.pt"
    pt.parent.mkdir(parents=True, exist_ok=True)
    pt.write_text("{}", encoding="utf-8")
    return pt


def register(engine: VoiceEngine, voice_id: str, backend: str, *, clip: Path | str = "voices/reference/x.wav",
             with_pt: bool = True) -> Path:
    """A registry entry written directly (no model), plus its .pt unless with_pt is False."""
    engine.registry.register(voice_id, backend, str(clip), 0.5, "en")
    if with_pt:
        return write_pt(engine, backend, voice_id)
    return engine.config.CACHE_DIR / backend / f"{voice_id}.pt"


def lock_is_free(path: Path) -> bool:
    """True when no engine lock holder exists on `path` (this process included)."""
    probe = EngineLock(path, program="probe")
    try:
        probe.acquire()
    except EngineBusy:
        return False
    probe.release()
    return True


def first_line(error: BaseException) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


# ---------- a fake torch.cuda ----------

class FakeCuda:
    """torch.cuda's allocator surface: holds fake bytes per model and records every call."""

    def __init__(self) -> None:
        self.initialized = True
        self.allocated = 0
        self.reserved = 0
        self.peak = 0
        self.calls: list[str] = []
        self.OutOfMemoryError = type("OutOfMemoryError", (RuntimeError,), {})

    def is_available(self) -> bool:
        self.calls.append("is_available")
        return True

    def is_initialized(self) -> bool:
        self.calls.append("is_initialized")
        return self.initialized

    def memory_reserved(self, device: Any = None) -> int:
        self.calls.append(f"memory_reserved({device})")
        return self.reserved

    def memory_allocated(self, device: Any = None) -> int:
        self.calls.append(f"memory_allocated({device})")
        return self.allocated

    def max_memory_reserved(self, device: Any = None) -> int:
        self.calls.append(f"max_memory_reserved({device})")
        return self.peak

    def reset_peak_memory_stats(self, device: Any = None) -> None:
        self.calls.append(f"reset_peak_memory_stats({device})")
        self.peak = self.reserved

    def empty_cache(self) -> None:
        self.calls.append("empty_cache")
        self.reserved = self.allocated

    def hold(self, owner: object, nbytes: int) -> None:
        """Allocate nbytes until `owner` is garbage collected, as a model's weights are."""
        self.allocated += nbytes
        self.reserved += nbytes
        self.peak = max(self.peak, self.reserved)
        weakref.finalize(owner, self._free, nbytes)

    def _free(self, nbytes: int) -> None:
        self.allocated -= nbytes


@pytest.fixture
def fake_cuda(monkeypatch: pytest.MonkeyPatch) -> FakeCuda:
    """A stand-in torch module (no __file__, so the conftest guard knows it is not the real one)."""
    cuda = FakeCuda()
    torch = types.ModuleType("torch")
    torch.cuda = cuda  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    return cuda


def weigh_models(monkeypatch: pytest.MonkeyPatch, cuda: FakeCuda, sizes: dict[str, int]) -> None:
    """Each fake build of a backend holds sizes[backend] bytes of fake VRAM for its model's life."""
    for name, nbytes in sizes.items():
        build = engine_module._BACKEND_FACTORIES[name]

        def weighed(cache_dir: Path, device: str, build: Callable = build, nbytes: int = nbytes) -> Any:
            backend = build(cache_dir, device)
            cuda.hold(backend.model, nbytes)
            return backend

        monkeypatch.setitem(engine_module._BACKEND_FACTORIES, name, weighed)


def a_whole_session(engine: VoiceEngine, fakes, clip: Path) -> None:
    """Switch, bake, take, a failed take, a failed load, unload: every allocator site once."""
    engine.make_resident(MTL)
    engine.load_voice("aiko", clip, backend=MTL)
    engine.speak("Hello there.", "aiko", job=Job())
    model = fakes.model(MTL)
    model.generate_error = RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
    del model
    with pytest.raises(GenerationError):
        engine.speak("Again.", "aiko")
    engine.make_resident(TUR)
    fakes.fail(STD, RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"))
    with pytest.raises(BackendLoadError):
        engine.make_resident(STD)
    engine.make_resident(TUR)
    assert engine.unload_backend(TUR)
    engine.close()


# ---------- construction ----------

NO_TORCH_SCRIPT = r"""
import json, sys
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
config = SimpleNamespace(
    ROOT=root, CACHE_DIR=root / "cache", VOICES_DIR=root / "voices" / "reference", OUTPUTS_DIR=root / "outputs",
    DEVICE="cuda", DEFAULT_BACKEND="chatterbox-multilingual", DEFAULT_LANGUAGE="en",
    ENABLED_BACKENDS=["chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"],
    MAX_RESIDENT_BACKENDS=1, DEFAULT_EXAGGERATION=0.5, DEFAULT_CFG_WEIGHT=0.5)

from engine.engine import VoiceEngine

engine = VoiceEngine(config)
engine.registry.register("aiko", "chatterbox-multilingual", "voices/reference/aiko.wav", 0.5, "en")
pt = config.CACHE_DIR / "chatterbox-multilingual" / "aiko.pt"
pt.parent.mkdir(parents=True)
pt.write_text("{}")
out = {
    "resident": list(engine.residency().resident),
    "loaded": engine.list_loaded_backends(),
    "language": engine.update_voice("aiko", language="ja", exaggeration=1.5)["language"],
    "details": [d.voice_id for d in engine.voice_details()],
    "cache_path": str(engine.voice_cache_path("aiko")),
    "cuda_available": engine.device_info().cuda_available,
    "turbo_cfg": engine.backend_info("chatterbox-turbo").uses_cfg,
    "holder": engine.engine_lock_holder(),
}
engine.set_preferred_device("cpu")
out["deleted"] = engine.delete_voice("aiko").removed_entry
out["torch"] = "torch" in sys.modules
out["chatterbox"] = "chatterbox" in sys.modules
print(json.dumps(out))
"""


def test_the_engine_and_its_registry_methods_import_no_torch(tmp_path: Path) -> None:
    """VoiceEngine(config) and every registry-only method keep torch and chatterbox out."""
    done = subprocess.run([sys.executable, "-c", NO_TORCH_SCRIPT, str(tmp_path)], cwd=ROOT,
                          env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out == {
        "resident": [], "loaded": [], "language": "ja", "details": ["aiko"],
        "cache_path": str(tmp_path / "cache" / MTL / "aiko.pt"), "cuda_available": None,
        "turbo_cfg": False, "holder": None, "deleted": True, "torch": False, "chatterbox": False,
    }


def test_a_new_engine_publishes_an_empty_residency(engine: VoiceEngine) -> None:
    assert engine.residency() == Residency(
        resident=(), loading=None, unloading=None, busy=None, device=None, preferred_device="cpu",
        max_resident=1, last_used=None, lock_held=False)
    assert engine.list_backends() == ALL and engine.list_loaded_backends() == []


def engine_warnings(caplog: pytest.LogCaptureFixture) -> str:
    """Every message the "engine" logger warned, one per line (cli.py prints them on stderr as they are)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name == "engine" and r.levelno >= logging.WARNING)


def test_unknown_backends_are_ignored_and_none_is_refused(scratch_config, fake_factories, caplog) -> None:
    scratch_config.ENABLED_BACKENDS = ["chatterbox-nano", TUR]
    assert VoiceEngine(scratch_config).list_backends() == [TUR]
    assert "unknown backend 'chatterbox-nano'" in engine_warnings(caplog)
    scratch_config.ENABLED_BACKENDS = ["chatterbox-nano"]
    with pytest.raises(RuntimeError, match="No known backends"):
        VoiceEngine(scratch_config)


# ---------- lock-free reads ----------

def test_polled_calls_never_wait_for_a_load(engine: VoiceEngine, fake_factories, pool) -> None:
    """residency() and every other polled call answer while a load holds the RLock."""
    register(engine, "aiko", MTL)
    gate = fake_factories.block(TUR)
    loading = pool.submit(engine.make_resident, TUR)
    assert gate.wait_entered(TIMEOUT_S)
    assert not engine._lock.acquire(blocking=False)          # the loader holds the RLock
    started = time.monotonic()
    residency = engine.residency()
    assert (residency.loading, residency.busy, residency.resident, residency.lock_held) == (TUR, "load", (), True)
    assert engine.list_loaded_backends() == [] and not engine.is_resident(TUR)
    assert [d.voice_id for d in engine.voice_details()] == ["aiko"]
    assert engine.voice_cache_path("aiko") == engine.config.CACHE_DIR / MTL / "aiko.pt"
    assert engine.update_voice("aiko", language="de")["language"] == "de"
    assert engine.device_info().reserved_bytes == 0
    assert engine.engine_lock_holder()["pid"] == os.getpid()
    assert engine.backend_info(TUR) is BACKENDS[TUR]
    assert engine.list_voices() == ["aiko"] and engine.voice_info("aiko")["language"] == "de"
    engine.set_preferred_device("cpu")
    assert time.monotonic() - started < 2.0
    gate.release()
    assert loading.result(TIMEOUT_S).backend == TUR
    assert engine.residency().resident == (TUR,)


def test_set_preferred_device_is_lock_free_and_applies_at_the_next_build(
        engine: VoiceEngine, fake_factories, pool, monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_resolve_device", lambda preferred: preferred)
    gate = fake_factories.block(MTL)
    loading = pool.submit(engine.make_resident, MTL)
    assert gate.wait_entered(TIMEOUT_S)
    started = time.monotonic()
    engine.set_preferred_device("cuda")
    assert time.monotonic() - started < 1.0
    assert engine.residency().preferred_device == "cuda"
    gate.release()
    assert loading.result(TIMEOUT_S).device == "cpu"          # resolved before the change
    assert engine.is_resident(MTL)                            # nothing was unloaded
    report = engine.make_resident(TUR)
    assert report.device == "cuda" and engine.residency().device == "cuda"
    assert fake_factories.builds == [(MTL, "cpu"), (TUR, "cuda")]
    with pytest.raises(ValueError, match="use 'cuda' or 'cpu'"):
        engine.set_preferred_device("mps")  # type: ignore[arg-type]


# ---------- require_resident ----------

def test_require_resident_with_nothing_loaded_raises_backend_not_resident(
        engine: VoiceEngine, fake_factories, events, make_clip) -> None:
    register(engine, "aiko", TUR)
    job = Job()
    with pytest.raises(BackendNotResident) as refused:
        engine.speak("Hello.", "aiko", require_resident=True, job=job)
    assert str(refused.value) == "chatterbox-turbo is not loaded"
    assert (refused.value.backend, refused.value.resident) == (TUR, ())
    assert refused.value.__context__ is None and job.status().phase == "failed"
    with pytest.raises(BackendNotResident):
        engine.load_voice("rin", make_clip("rin.wav", 6.0), backend=TUR, require_resident=True)
    assert fake_factories.calls == {} and events == []
    assert engine.residency().resident == () and not engine.residency().lock_held
    assert engine.voice_info("rin") is None


def test_require_resident_never_switches(engine: VoiceEngine, fake_factories, events) -> None:
    register(engine, "aiko", TUR)
    engine.make_resident(MTL)
    before = list(events)
    with pytest.raises(BackendNotResident) as refused:
        engine.speak("Hello.", "aiko", require_resident=True)
    assert str(refused.value) == "chatterbox-turbo is not loaded (chatterbox-multilingual is)"
    assert fake_factories.calls[TUR] == 0 and fake_factories.calls[MTL] == 1
    assert engine.residency().resident == (MTL,) and events == before


# ---------- loads, switches and events ----------

def test_a_switch_evicts_first_and_reports_it(engine: VoiceEngine, fake_factories, events, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "chatterbox", raising=False)
    first = engine.make_resident(MTL)
    assert first == LoadReport(backend=MTL, device="cpu", seconds=first.seconds, already_resident=False,
                               evicted=(), freed_bytes=None, reserved_bytes=None, allocated_bytes=None,
                               first_in_process=True)
    assert first.seconds >= 0

    report = engine.make_resident(TUR)
    assert (report.backend, report.evicted, report.freed_bytes) == (TUR, (MTL,), None)
    assert kinds(events) == [("load_started", MTL, None), ("loaded", MTL, None),
                             ("load_started", TUR, None), ("unloaded", MTL, "evicted"), ("loaded", TUR, None)]
    loaded = events[-1]
    assert (loaded.seconds, loaded.device, loaded.first_in_process) == (report.seconds, "cpu", True)
    assert all(isinstance(event.at, float) for event in events)
    assert fake_factories.model_ref(MTL)() is None            # the evicted model is gone
    assert engine.residency() == Residency(
        resident=(TUR,), loading=None, unloading=None, busy=None, device="cpu", preferred_device="cpu",
        max_resident=1, last_used=engine.residency().last_used, lock_held=True)
    assert isinstance(engine.residency().last_used, float)

    again = engine.make_resident(TUR)
    assert (again.already_resident, again.seconds, again.evicted, again.first_in_process) == (True, 0.0, (), False)
    assert engine.load_backend(TUR) == TUR
    assert len(events) == 5
    assert fake_factories.builds == [(MTL, "cpu"), (TUR, "cpu")] and fake_factories.installs == 2


def test_first_in_process_follows_the_chatterbox_import(engine: VoiceEngine, events, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "chatterbox", raising=False)
    assert engine.make_resident(MTL).first_in_process is True
    monkeypatch.setitem(sys.modules, "chatterbox", types.ModuleType("chatterbox"))
    assert engine.make_resident(TUR).first_in_process is False
    assert [event.first_in_process for event in events if event.kind == "loaded"] == [True, False]


def test_two_resident_models_evict_the_least_recently_used(scratch_config, fake_factories) -> None:
    scratch_config.MAX_RESIDENT_BACKENDS = 2
    events: list[EngineEvent] = []
    engine = VoiceEngine(scratch_config, on_event=events.append)
    try:
        engine.make_resident(MTL)
        engine.make_resident(STD)
        engine.make_resident(MTL)                             # now the most recent
        assert engine.residency().resident == (STD, MTL)
        assert engine.make_resident(TUR).evicted == (STD,)
        assert engine.residency().resident == (MTL, TUR)
        engine.close()
        assert kinds(events)[-2:] == [("unloaded", MTL, "close"), ("unloaded", TUR, "close")]
        assert engine.residency().resident == () and not engine.residency().lock_held
    finally:
        engine.close()


def test_events_arrive_on_the_engine_thread(scratch_config, fake_factories, pool) -> None:
    threads: list[int] = []
    engine = VoiceEngine(scratch_config, on_event=lambda event: threads.append(threading.get_ident()))
    try:
        worker = pool.submit(lambda: (engine.make_resident(MTL), threading.get_ident())[1]).result(TIMEOUT_S)
        assert len(threads) == 2 and set(threads) == {worker} and worker != threading.get_ident()
    finally:
        engine.close()


def test_a_failing_event_handler_never_breaks_the_engine(scratch_config, fake_factories, caplog) -> None:
    def handler(event: EngineEvent) -> None:
        raise RuntimeError("handler bug")

    engine = VoiceEngine(scratch_config, on_event=handler)
    try:
        assert engine.make_resident(MTL).backend == MTL
        assert engine.unload_backend(MTL)
    finally:
        engine.close()
    err = engine_warnings(caplog)
    assert "on_event handler failed on 'load_started'" in err and "handler bug" in err
    assert all(r.exc_info for r in caplog.records if "handler failed" in r.getMessage())   # the traceback is kept


def test_residency_names_the_running_operation(scratch_config, fake_factories, make_clip) -> None:
    seen: list[tuple[str, str, str | None, str | None, tuple[str, ...]]] = []
    engine: VoiceEngine

    def record(event: EngineEvent) -> None:
        state = engine.residency()
        seen.append((event.kind, event.backend, state.busy, state.loading, state.resident))

    engine = VoiceEngine(scratch_config, on_event=record)
    try:
        engine.make_resident(MTL)
        engine.make_resident(TUR)
        assert engine.unload_backend(TUR)
        assert seen == [
            ("load_started", MTL, "load", MTL, ()),
            ("loaded", MTL, None, None, (MTL,)),
            ("load_started", TUR, "load", TUR, (MTL,)),
            ("unloaded", MTL, "load", TUR, ()),
            ("loaded", TUR, None, None, (TUR,)),
            ("unloaded", TUR, "unload", None, ()),
        ]
        engine.make_resident(MTL)
        backend = fake_factories.backend(MTL)
        busy: list[str | None] = []
        real_load_voice, real_speak = backend.load_voice, backend.speak
        backend.load_voice = lambda *a, **k: (busy.append(engine.residency().busy), real_load_voice(*a, **k))[1]
        backend.speak = lambda *a, **k: (busy.append(engine.residency().busy), real_speak(*a, **k))[1]
        engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
        engine.speak("Hello.", "aiko")
        assert busy == ["bake", "generate"] and engine.residency().busy is None
    finally:
        engine.close()


def test_a_failing_progress_install_does_not_fail_the_load(engine: VoiceEngine, monkeypatch, caplog) -> None:
    def broken() -> None:
        raise AttributeError("module 'chatterbox.models.t3.t3' has no attribute 'tqdm'")

    monkeypatch.setattr(engine_module.progress, "install", broken)
    assert engine.make_resident(MTL).backend == MTL and engine.is_resident(MTL)
    assert "take progress is unavailable" in engine_warnings(caplog)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -0.1, 2.5, "loud"])
def test_load_voice_refuses_an_unusable_expressiveness_before_any_model_loads(
        engine: VoiceEngine, make_clip, fake_factories, value: Any) -> None:
    """R2-06: `cli.py load rin clip.wav --exaggeration nan` must not register NaN (every take of rin would then
    run its GPU work and be refused by the takes store); the range is update_voice()'s, 0.0 to 2.0."""
    with pytest.raises(ValueError, match="Expressiveness must be between 0.0 and 2.0"):
        engine.load_voice("rin", make_clip("rin.wav", 6.0), backend=MTL, exaggeration=value)
    assert engine.residency().resident == () and engine.registry.get("rin") is None


@pytest.mark.parametrize("stored", ["NaN", "Infinity", "-Infinity", "1" + "0" * 400, '"high"', "null"])
def test_a_non_finite_registered_expressiveness_reads_as_the_default(engine: VoiceEngine, make_clip,
                                                                     stored: str) -> None:
    """R2-06: a hand-edited or old registry value the engine cannot use falls back to DEFAULT_EXAGGERATION."""
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL, exaggeration=0.7)
    path = engine.registry.path
    text = path.read_text(encoding="utf-8").replace('"exaggeration": 0.7', f'"exaggeration": {stored}')
    path.write_text(text, encoding="utf-8")
    (details,) = engine.voice_details("aiko")
    assert details.exaggeration == engine.config.DEFAULT_EXAGGERATION


def test_an_unknown_backend_is_refused_before_anything_happens(engine: VoiceEngine, fake_factories, events) -> None:
    with pytest.raises(ValueError, match="Backend 'chatterbox-nano' isn't enabled"):
        engine.make_resident("chatterbox-nano")
    assert fake_factories.calls == {} and events == []


# ---------- failed loads ----------

@pytest.mark.parametrize(("error", "kind", "detail"), [
    (RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB\nGPU 0 has a total capacity of 11.99 GiB"),
     "out_of_memory", "CUDA out of memory. Tried to allocate 2.00 GiB"),
    (FileNotFoundError("ve.pt is not in the local cache"), "missing_files", "ve.pt is not in the local cache"),
    (ValueError("bad config"), "other", "bad config"),
    (ConnectionError("no route to host"), "other", "no route to host"),
    (KeyError(), "other", "KeyError"),
], ids=["oom", "missing", "other", "connection", "no-message"])
def test_a_failed_load_leaves_nothing_behind(engine: VoiceEngine, fake_factories, events, scratch_config,
                                             error: Exception, kind: str, detail: str) -> None:
    engine.make_resident(MTL)
    fake_factories.fail(TUR, error)
    with pytest.raises(BackendLoadError) as failed:
        engine.make_resident(TUR)
    exc = failed.value
    assert (exc.backend, exc.kind, exc.detail) == (TUR, kind, detail)
    assert str(exc) == f"chatterbox-turbo failed to load: {detail}" and exc.seconds >= 0
    assert exc.__context__ is None and exc.__cause__ is None
    assert fake_factories.model_ref(TUR)() is None and fake_factories.model_ref(MTL)() is None
    residency = engine.residency()
    assert (residency.resident, residency.loading, residency.unloading, residency.busy,
            residency.device, residency.lock_held) == ((), None, None, None, None, False)
    assert lock_is_free(scratch_config.CACHE_DIR / "engine.lock")
    assert kinds(events)[2:] == [("load_started", TUR, None), ("unloaded", MTL, "evicted"),
                                 ("load_failed", TUR, None)]
    assert (events[-1].error, events[-1].device) == (f"{kind}: {detail}", "cpu")
    assert engine.make_resident(TUR).backend == TUR           # the next try builds afresh


def test_missing_weights_and_offline_mode_are_missing_files(engine: VoiceEngine, fake_factories) -> None:
    from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled

    for error in (LocalEntryNotFoundError("Cannot find an appropriate cached snapshot folder"),
                  OfflineModeIsEnabled("Cannot reach the Hub: offline mode is enabled.")):
        fake_factories.fail(TUR, error)
        with pytest.raises(BackendLoadError) as failed:
            engine.make_resident(TUR)
        assert failed.value.kind == "missing_files"


def test_torchs_out_of_memory_type_is_out_of_memory(engine: VoiceEngine, fake_factories, fake_cuda) -> None:
    fake_factories.fail(TUR, fake_cuda.OutOfMemoryError("Tried to allocate 2.00 GiB"))
    with pytest.raises(BackendLoadError) as failed:
        engine.make_resident(TUR)
    assert (failed.value.kind, failed.value.detail) == ("out_of_memory", "Tried to allocate 2.00 GiB")


# ---------- the engine lock ----------

TRY_LOCK_SCRIPT = """
import json, sys
from pathlib import Path
from engine.errors import EngineBusy
from engine.lock import EngineLock
lock = EngineLock(Path(sys.argv[1]), program="other.py")
try:
    lock.acquire()
except EngineBusy as exc:
    print(json.dumps({"busy": True, "holder": exc.holder}))
else:
    lock.release()
    print(json.dumps({"busy": False}))
"""


def try_lock_from_another_process(path: Path) -> dict:
    done = subprocess.run([sys.executable, "-c", TRY_LOCK_SCRIPT, str(path)], cwd=ROOT,
                          env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_engine_lock_stays_held_in_the_eviction_gap(engine: VoiceEngine, fake_factories, events, pool,
                                                         scratch_config) -> None:
    """Between the eviction and the build of a switch, no other process can take the engine."""
    lock_path = scratch_config.CACHE_DIR / "engine.lock"
    engine.make_resident(MTL)
    gate = fake_factories.block(TUR)
    switching = pool.submit(engine.make_resident, TUR)
    assert gate.wait_entered(TIMEOUT_S)
    assert kinds(events)[-2:] == [("load_started", TUR, None), ("unloaded", MTL, "evicted")]
    assert engine.residency().resident == () and fake_factories.model_ref(MTL)() is None
    refused = try_lock_from_another_process(lock_path)
    assert refused["busy"] is True and refused["holder"]["pid"] == os.getpid()
    gate.release()
    assert switching.result(TIMEOUT_S).evicted == (MTL,)
    assert engine.unload_backend(TUR) and not engine.residency().lock_held
    assert try_lock_from_another_process(lock_path) == {"busy": False}


def test_engine_busy_refuses_before_anything_changes(engine: VoiceEngine, fake_factories, events,
                                                     scratch_config) -> None:
    other = EngineLock(scratch_config.CACHE_DIR / "engine.lock", program="cli.py")
    other.acquire()
    try:
        with pytest.raises(EngineBusy) as busy:
            engine.make_resident(MTL)
        assert busy.value.holder["program"] == "cli.py" and busy.value.__context__ is None
        assert fake_factories.calls == {} and events == []
        assert engine.residency().loading is None and not engine.residency().lock_held
    finally:
        other.release()
    assert engine.make_resident(MTL).backend == MTL


def test_the_lock_is_released_only_when_nothing_is_resident(engine: VoiceEngine, events, scratch_config) -> None:
    lock_path = scratch_config.CACHE_DIR / "engine.lock"
    assert lock_is_free(lock_path)
    engine.make_resident(MTL)
    assert engine.residency().lock_held and not lock_is_free(lock_path)
    assert engine.unload_backend(STD) is False and engine.unload_backend("nope") is False
    assert engine.residency().lock_held
    assert engine.unload_backend(MTL) is True
    assert kinds(events)[-1] == ("unloaded", MTL, "requested")
    assert not engine.residency().lock_held and lock_is_free(lock_path)
    engine.make_resident(STD)
    assert engine.unload_backend(STD, reason="idle") and events[-1].reason == "idle"


def test_the_lock_stays_held_while_any_model_is_resident(scratch_config, fake_factories) -> None:
    """With two resident slots, a failed load or one unload leaves the other model, so the lock stays."""
    scratch_config.MAX_RESIDENT_BACKENDS = 2
    lock_path = scratch_config.CACHE_DIR / "engine.lock"
    engine = VoiceEngine(scratch_config)
    try:
        engine.make_resident(MTL)
        fake_factories.fail(STD, RuntimeError("boom"))
        with pytest.raises(BackendLoadError):
            engine.make_resident(STD)
        assert engine.residency().resident == (MTL,)
        assert engine.residency().lock_held and not lock_is_free(lock_path)
        engine.make_resident(TUR)
        assert engine.unload_backend(MTL) and engine.residency().resident == (TUR,)
        assert engine.residency().lock_held and not lock_is_free(lock_path)
        assert engine.unload_backend(TUR)
        assert not engine.residency().lock_held and lock_is_free(lock_path)
    finally:
        engine.close()


def test_hold_engine_lock_keeps_the_lock_until_close(scratch_config, fake_factories) -> None:
    lock_path = scratch_config.CACHE_DIR / "engine.lock"
    engine = VoiceEngine(scratch_config, hold_engine_lock=True)
    try:
        assert engine.residency().lock_held and not lock_is_free(lock_path)
        engine.make_resident(MTL)
        assert engine.unload_backend(MTL) and engine.residency().lock_held
        fake_factories.fail(STD, RuntimeError("boom"))
        with pytest.raises(BackendLoadError):
            engine.make_resident(STD)
        assert engine.residency().lock_held and not lock_is_free(lock_path)
    finally:
        engine.close()
    assert not engine.residency().lock_held and lock_is_free(lock_path)


def test_hold_engine_lock_is_refused_while_another_holds_it(scratch_config, fake_factories) -> None:
    other = EngineLock(scratch_config.CACHE_DIR / "engine.lock", program="cli.py")
    other.acquire()
    try:
        with pytest.raises(EngineBusy):
            VoiceEngine(scratch_config, hold_engine_lock=True)
    finally:
        other.release()


def test_lock_path_overrides_the_cache_lock(scratch_config, fake_factories, tmp_path) -> None:
    real_lock = tmp_path / "install" / "cache" / "engine.lock"
    engine = VoiceEngine(scratch_config, lock_path=real_lock)
    try:
        engine.make_resident(MTL)
        assert not lock_is_free(real_lock) and lock_is_free(scratch_config.CACHE_DIR / "engine.lock")
        assert engine.engine_lock_holder()["pid"] == os.getpid()
    finally:
        engine.close()
    assert lock_is_free(real_lock)


# ---------- the allocator guard (§3.0) ----------

def test_a_switch_on_cuda_measures_what_it_frees(scratch_config, fake_factories, fake_cuda, monkeypatch,
                                                  make_clip) -> None:
    scratch_config.DEVICE = "cuda"
    weigh_models(monkeypatch, fake_cuda, {MTL: 3 * GIB, TUR: 2 * GIB})
    events: list[EngineEvent] = []
    engine = VoiceEngine(scratch_config, on_event=events.append)
    try:
        first = engine.make_resident(MTL)
        assert (first.device, first.reserved_bytes, first.allocated_bytes, first.freed_bytes) == (
            "cuda", 3 * GIB, 3 * GIB, None)
        engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
        job = Job()
        engine.speak("Hello there.", "aiko", job=job)
        assert job.peak_reserved_bytes == 3 * GIB
        assert fake_cuda.calls.count("reset_peak_memory_stats(0)") == 1     # at the load, never per take

        report = engine.make_resident(TUR)
        assert (report.evicted, report.freed_bytes, report.reserved_bytes) == ((MTL,), 3 * GIB, 2 * GIB)
        unloaded = events[-2]
        assert (unloaded.kind, unloaded.backend, unloaded.reason, unloaded.device) == (
            "unloaded", MTL, "evicted", "cuda")
        assert (unloaded.freed_bytes, unloaded.peak_bytes, unloaded.reserved_bytes,
                unloaded.allocated_bytes) == (3 * GIB, 3 * GIB, 0, 0)
        assert (events[-1].kind, events[-1].reserved_bytes, events[-1].allocated_bytes) == (
            "loaded", 2 * GIB, 2 * GIB)
        assert fake_cuda.calls.count("reset_peak_memory_stats(0)") == 2
        assert engine.make_resident(TUR).reserved_bytes == 2 * GIB          # already resident: a reading

        assert engine.unload_backend(TUR)
        assert (events[-1].reason, events[-1].freed_bytes, events[-1].allocated_bytes) == ("requested", 2 * GIB, 0)
    finally:
        engine.close()


def test_an_out_of_memory_take_frees_the_cache(scratch_config, fake_factories, fake_cuda, make_clip) -> None:
    scratch_config.DEVICE = "cuda"
    engine = VoiceEngine(scratch_config)
    try:
        engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
        model = fake_factories.model(MTL)
        model.generate_error = fake_cuda.OutOfMemoryError("Tried to allocate 20.00 MiB")
        del model
        fake_cuda.calls.clear()
        with pytest.raises(GenerationError) as failed:
            engine.speak("Hello.", "aiko")
        assert (failed.value.kind, str(failed.value), failed.value.during) == (
            "out_of_memory", "Tried to allocate 20.00 MiB", "speak")
        assert "empty_cache" in fake_cuda.calls
    finally:
        engine.close()


def test_cpu_runs_never_touch_the_allocator(engine: VoiceEngine, fake_factories, fake_cuda, make_clip) -> None:
    a_whole_session(engine, fake_factories, make_clip("aiko.wav", 6.0))
    assert fake_cuda.calls == []


def test_cuda_that_is_not_initialised_is_left_alone(scratch_config, fake_factories, fake_cuda, make_clip) -> None:
    fake_cuda.initialized = False
    scratch_config.DEVICE = "cuda"
    engine = VoiceEngine(scratch_config)
    a_whole_session(engine, fake_factories, make_clip("aiko.wav", 6.0))
    assert set(fake_cuda.calls) == {"is_available", "is_initialized"}
    assert fake_factories.builds[0] == (MTL, "cuda")


@pytest.mark.slow
@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_real_torch_never_gets_a_cuda_context_from_a_fake_session(
        scratch_config, fake_factories, monkeypatch, make_clip, device: str) -> None:
    torch = pytest.importorskip("torch")
    assert not torch.cuda.is_initialized()
    calls: list[str] = []
    for name in ALLOCATOR_CALLS:
        monkeypatch.setattr(torch.cuda, name, lambda *args, _name=name, **kwargs: calls.append(_name))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)   # a "cuda" build without asking the driver
    scratch_config.DEVICE = device
    engine = VoiceEngine(scratch_config)
    a_whole_session(engine, fake_factories, make_clip("aiko.wav", 6.0))
    assert fake_factories.builds[0] == (MTL, device)
    assert calls == []
    assert not torch.cuda.is_initialized()


# ---------- speak ----------

@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_empty_text_is_refused_before_anything_loads(engine: VoiceEngine, fake_factories, text: str) -> None:
    register(engine, "aiko", MTL)
    with pytest.raises(ValueError, match=r"^Nothing to say: the text is empty\.$"):
        engine.speak(text, "aiko")
    assert fake_factories.calls == {}


def test_an_unregistered_voice_is_refused(engine: VoiceEngine, fake_factories) -> None:
    with pytest.raises(ValueError, match=re.escape("Voice 'ghost' is not registered. Call load_voice() first.")):
        engine.speak("Hello.", "ghost")
    assert fake_factories.calls == {}


def test_speak_fills_knobs_from_the_registration(engine: VoiceEngine, fake_factories, make_clip) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL, language="ja", exaggeration=0.7)
    engine.speak("こんにちは。", "aiko", cfg_weight=0.3, job=Job(), require_resident=True)
    engine.speak("Hello.", "aiko", language_id="en", exaggeration=1.1)
    model = fake_factories.model(MTL)
    assert [params for _, params in model.generate_calls] == [
        {"language_id": "ja", "exaggeration": 0.7, "cfg_weight": 0.3},
        {"language_id": "en", "exaggeration": 1.1},
    ]


def test_speak_loads_implicitly_by_default(engine: VoiceEngine, fake_factories, events) -> None:
    """cli.py and app.py keep today's implicit loading."""
    register(engine, "aiko", STD)
    assert engine.speak("Hello.", "aiko")[:4] == b"RIFF"
    assert fake_factories.builds == [(STD, "cpu")] and kinds(events)[-1] == ("loaded", STD, None)


def test_a_take_records_its_job(engine: VoiceEngine, fake_factories, make_clip) -> None:
    clip = make_clip("aiko.wav", 6.0)
    engine.load_voice("aiko", clip, backend=MTL)
    engine.load_voice("rin", clip, backend=MTL)             # rin is the active voice now
    job = Job()
    assert engine.speak("Hello there.", "aiko", job=job)[:4] == b"RIFF"
    status = job.status()
    assert (status.phase, status.tokens, status.max_tokens, status.hit_token_limit) == ("done", 12, 1000, False)
    assert isinstance(job.conds_load_s, float) and job.conds_load_s >= 0    # copied from the backend
    assert job.made_s is not None and job.made_s > 0 and job.peak_reserved_bytes is None
    again = Job()
    engine.speak("Again.", "aiko", job=again)
    assert again.conds_load_s is None                         # aiko was already active
    model = fake_factories.model(MTL)
    model.tokens = model.max_tokens + 1                       # no EOS within the bound
    del model
    cut = Job()
    engine.speak("A very long script.", "aiko", job=cut)
    assert cut.status().hit_token_limit and cut.status().tokens == 1000


@pytest.mark.parametrize(("error", "raised", "kind"), [
    (RuntimeError("CUDA error: device-side assert triggered\nCompile with TORCH_USE_CUDA_DSA"),
     GenerationError, "other"),
    (RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB"), GenerationError, "out_of_memory"),
    (AssertionError(), GenerationError, "other"),
    (ValueError("Unsupported language_id 'xx'"), ValueError, None),
], ids=["runtime", "oom", "assert", "value"])
def test_a_failed_take_raises_fresh_and_its_model_frees_on_unload(
        engine: VoiceEngine, fake_factories, make_clip, error: Exception, raised: type, kind: str | None) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
    model = fake_factories.model(MTL)
    model.generate_error = error
    del model
    ref = fake_factories.model_ref(MTL)
    job = Job()
    with pytest.raises(raised) as failed:
        engine.speak("Hello.", "aiko", job=job)
    exc = failed.value
    assert type(exc) is raised and str(exc) == first_line(error)
    assert exc.__context__ is None and exc.__cause__ is None
    if kind is not None:
        assert (exc.kind, exc.during) == (kind, "speak")
    assert job.status().phase == "failed"
    assert engine.residency().busy is None and engine.is_resident(MTL)
    assert engine.unload_backend(MTL)
    assert ref() is None                                      # no traceback kept the model alive


@pytest.mark.parametrize(("error", "raised"), [
    (RuntimeError("bake failed\nsecond line"), GenerationError),
    (ValueError("chatterbox-turbo rejected the reference clip: x"), ValueError),
], ids=["runtime", "value"])
def test_a_failed_bake_raises_fresh_and_registers_nothing(engine: VoiceEngine, fake_factories, make_clip,
                                                         error: Exception, raised: type) -> None:
    engine.make_resident(MTL)
    model = fake_factories.model(MTL)
    model.bake_error = error
    del model
    job = Job()
    with pytest.raises(raised) as failed:
        engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL, job=job)
    assert type(failed.value) is raised and str(failed.value) == first_line(error)
    assert failed.value.__context__ is None
    if raised is GenerationError:
        assert (failed.value.kind, failed.value.during) == ("other", "bake")
    assert engine.voice_info("aiko") is None and job.status().phase == "failed"


def test_a_cancelled_job_never_starts(engine: VoiceEngine, fake_factories, make_clip) -> None:
    register(engine, "aiko", TUR)
    job = Job()
    job.cancel()
    with pytest.raises(Cancelled):
        engine.speak("Hello.", "aiko", job=job)
    with pytest.raises(Cancelled):
        engine.load_voice("rin", make_clip("rin.wav", 6.0), backend=TUR, job=job)
    assert fake_factories.calls == {} and job.status().phase == "cancelled"


def test_a_take_cancels_within_one_sampling_step(engine: VoiceEngine, fake_factories, make_clip, pool) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
    model = fake_factories.model(MTL)
    gate = model.generate_gate = fake_factories.gate()
    del model
    job = Job()
    take = pool.submit(engine.speak, "Hello there.", "aiko", job=job)
    assert gate.wait_entered(TIMEOUT_S)
    assert engine.residency().busy == "generate"
    job.cancel()
    gate.release()
    with pytest.raises(Cancelled) as cancelled:
        take.result(TIMEOUT_S)
    assert cancelled.value.__context__ is None
    assert (job.status().phase, job.status().tokens) == ("cancelled", 1)
    assert engine.residency().busy is None and engine.is_resident(MTL)
    assert engine.speak("Next.", "aiko")[:4] == b"RIFF"


# ---------- load_voice ----------

def _bad_load(case: str, clips: types.SimpleNamespace) -> tuple[dict[str, Any], str]:
    """(load_voice kwargs, expected message) for one invalid request."""
    cases: dict[str, tuple[dict[str, Any], str]] = {
        "characters": (dict(voice_id="bad name", reference_wav=clips.good, backend=TUR),
                       "Invalid voice_id 'bad name': only letters"),
        "reserved": (dict(voice_id="nul", reference_wav=clips.good, backend=TUR),
                     "Invalid voice_id 'nul': it is a reserved device name"),
        "length": (dict(voice_id="x" * 65, reference_wav=clips.good, backend=TUR), "use at most 64 characters"),
        "backend": (dict(voice_id="rin", reference_wav=clips.good, backend="chatterbox-nano"),
                    "Backend 'chatterbox-nano' isn't enabled"),
        "missing": (dict(voice_id="rin", reference_wav=clips.missing, backend=TUR),
                    f"Reference clip not found: {clips.missing}"),
        "format": (dict(voice_id="rin", reference_wav=clips.notes, backend=TUR),
                   "Reference clip notes.txt: use WAV, FLAC, OGG or MP3"),
        "silent": (dict(voice_id="rin", reference_wav=clips.silent, backend=TUR),
                   "Reference clip silent.wav: the clip is silent"),
        "short": (dict(voice_id="rin", reference_wav=clips.blip, backend=TUR),
                  "Reference clip blip.wav: 0.6 s is too short to forge"),
        "turbo": (dict(voice_id="rin", reference_wav=clips.four, backend=TUR),
                  "Reference clip four.wav: chatterbox-turbo needs a reference clip longer than 5s (got 4.2s). "
                  "Use a longer clip or another backend."),
        "language": (dict(voice_id="rin", reference_wav=clips.good, backend=MTL, language="xx"),
                     "Unknown language 'xx' for chatterbox-multilingual; use one of: ar, da, de"),
        "case": (dict(voice_id="aiko", reference_wav=clips.good, backend=TUR), "aiko and Aiko would share one file"),
    }
    return cases[case]


@pytest.mark.parametrize("case", ["characters", "reserved", "length", "backend", "missing", "format", "silent",
                                  "short", "turbo", "language", "case"])
def test_load_voice_checks_everything_before_a_model_loads(engine: VoiceEngine, fake_factories, events,
                                                           make_clip, tmp_path, case: str) -> None:
    good = make_clip("good.wav", 6.0)
    engine.load_voice("marco", good, backend=MTL)            # resident, with marco active
    engine.registry.register("Aiko", MTL, str(good), 0.5, "en")
    notes = tmp_path / "notes.txt"
    notes.write_text("not audio", encoding="utf-8")
    clips = types.SimpleNamespace(good=good, missing=(tmp_path / "missing.wav").resolve(), notes=notes,
                                  silent=make_clip("silent.wav", 3.0, level=0.0), blip=make_clip("blip.wav", 0.6),
                                  four=make_clip("four.wav", 4.2))
    kwargs, message = _bad_load(case, clips)
    before = list(events)
    with pytest.raises(ValueError, match=re.escape(message)):
        engine.load_voice(**kwargs)
    assert dict(fake_factories.calls) == {MTL: 1}
    assert engine.residency().resident == (MTL,) and events == before
    assert fake_factories.backend(MTL)._active_voice == "marco"
    assert engine.voice_info("rin") is None


def test_portable_library_uses_its_own_copied_reference(engine: VoiceEngine, make_clip, tmp_path) -> None:
    from shutil import copytree
    from .conftest import make_scratch_config

    engine.config.PORTABLE = True
    clip = make_clip("reference.wav", 6.0, folder=engine.config.VOICES_DIR)
    engine.load_voice("rin", clip, backend=MTL)
    assert engine.voice_info("rin")["reference_wav"] == "voices/reference/reference.wav"
    engine.close()

    copied = make_scratch_config(tmp_path / "copy")
    copytree(engine.config.VOICES_DIR, copied.VOICES_DIR, dirs_exist_ok=True)
    copytree(engine.config.CACHE_DIR, copied.CACHE_DIR, dirs_exist_ok=True)
    reopened = VoiceEngine(copied)
    try:
        assert reopened.voice_details("rin")[0].reference_path == copied.VOICES_DIR / clip.name
    finally:
        reopened.close()


def test_english_only_backends_store_en(engine: VoiceEngine, make_clip) -> None:
    clip = make_clip("aiko.wav", 6.0)
    with pytest.warns(UserWarning, match="chatterbox-standard is English only") as record:
        engine.load_voice("aiko", clip, backend=STD, language="ja")
    assert record[0].filename == __file__                     # points at load_voice()'s caller
    assert engine.voice_info("aiko")["language"] == "en"
    engine.load_voice("rin", clip, backend=MTL, language="JA")
    assert engine.voice_info("rin")["language"] == "ja"


def test_load_voice_stores_the_clip_as_an_absolute_path(engine: VoiceEngine, fake_factories, make_clip,
                                                        tmp_path, monkeypatch) -> None:
    clip = make_clip("aiko.wav", 6.0)                         # tmp_path/clips/aiko.wav
    monkeypatch.chdir(tmp_path)
    assert engine.load_voice("aiko", Path("clips") / "aiko.wav", backend=MTL) == "aiko"
    assert engine.voice_info("aiko")["reference_wav"] == str(clip.resolve())
    assert fake_factories.model(MTL).bakes == [str(clip.resolve())]


def test_load_voice_registers_while_holding_the_rlock(engine: VoiceEngine, make_clip, monkeypatch) -> None:
    held: list[bool] = []
    real_register = engine.registry.register

    def checking_register(*args: Any, **kwargs: Any) -> None:
        def probe() -> None:
            got = engine._lock.acquire(blocking=False)
            if got:
                engine._lock.release()
            held.append(not got)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(TIMEOUT_S)
        real_register(*args, **kwargs)

    monkeypatch.setattr(engine.registry, "register", checking_register)
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL, meta={"consent": True})
    assert held == [True]
    entry = engine.voice_info("aiko")
    assert entry["meta"] == {"consent": True} and "forged_at" in entry and "created_at" in entry


def test_a_cache_hit_keeps_the_clip_its_pt_was_baked_from(engine: VoiceEngine, fake_factories, make_clip,
                                                          monkeypatch) -> None:
    stamps = iter(f"2026-11-13T05:00:{second:02d}+00:00" for second in range(60))
    monkeypatch.setattr(registry_module, "_now_iso", lambda: next(stamps))
    first, second = make_clip("first.wav", 6.0), make_clip("second.wav", 7.0)
    engine.load_voice("aiko", first, backend=MTL, meta={"consent": True})
    baked = engine.voice_info("aiko")

    with pytest.warns(UserWarning, match="already has cached conditioning") as record:
        engine.load_voice("aiko", second, backend=MTL)
    assert record[0].filename == __file__
    hit = engine.voice_info("aiko")
    assert hit["reference_wav"] == str(first.resolve())
    assert (hit["created_at"], hit["forged_at"], hit["meta"]) == (baked["created_at"], baked["forged_at"],
                                                                  {"consent": True})
    model = fake_factories.model(MTL)
    assert len(model.bakes) == 1 and model.conds_loads == ["aiko"]
    del model

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        engine.load_voice("aiko", first, backend=MTL)       # the same clip: no warning

    job = Job()
    engine.load_voice("aiko", second, backend=MTL, force=True, job=job)
    forced = engine.voice_info("aiko")
    assert forced["reference_wav"] == str(second.resolve()) and forced["forged_at"] != baked["forged_at"]
    assert "meta" not in forced                               # consent travels with the clip
    assert job.status().phase == "done" and job.bake_s is not None and job.bake_s >= 0


def test_the_gui_bake_rebakes_on_the_resident_model_only(engine: VoiceEngine, fake_factories, events,
                                                         make_clip, monkeypatch) -> None:
    """facade.bake's call (§4.4): force=True, require_resident=True, meta and a job; it never loads or evicts."""
    stamps = iter(f"2026-11-13T06:00:{second:02d}+00:00" for second in range(60))
    monkeypatch.setattr(registry_module, "_now_iso", lambda: next(stamps))
    clip = make_clip("rin.wav", 6.0)
    engine.load_voice("rin", clip, backend=MTL, language="ja", meta={"consent": True})
    first = engine.voice_info("rin")
    before = list(events)
    meta = {"consent": True, "consent_at": "2026-11-13T06:00:30+00:00", "clip_seconds": 6.0,
            "clip_source": "rin.wav"}
    job = Job()
    assert engine.load_voice("rin", clip, backend=MTL, exaggeration=0.8, language="ja", force=True,
                             require_resident=True, meta=meta, job=job) == "rin"
    assert dict(fake_factories.calls) == {MTL: 1} and events == before      # no build, no eviction, no event
    model = fake_factories.model(MTL)
    assert model.bakes == [str(clip.resolve())] * 2                          # force=True baked again
    del model
    entry = engine.voice_info("rin")
    assert (entry["created_at"], entry["meta"], entry["exaggeration"]) == (first["created_at"], meta, 0.8)
    assert entry["forged_at"] != first["forged_at"]
    assert (job.status().phase, job.bake_s is not None) == ("done", True)
    assert fake_factories.backend(MTL)._active_voice == "rin" and engine.residency().busy is None

    refused = Job()
    with pytest.raises(BackendNotResident) as not_resident:
        engine.load_voice("rin", make_clip("rin6.wav", 6.0), backend=TUR, force=True, require_resident=True,
                          meta=meta, job=refused)
    assert str(not_resident.value) == "chatterbox-turbo is not loaded (chatterbox-multilingual is)"
    assert not_resident.value.__context__ is None and refused.status().phase == "failed"
    assert dict(fake_factories.calls) == {MTL: 1} and engine.residency().resident == (MTL,)
    assert engine.voice_info("rin") == entry and events == before


def test_engine_busy_from_an_implicit_load_is_fresh_and_fails_the_job(engine: VoiceEngine, fake_factories,
                                                                    events, scratch_config) -> None:
    """cli.py / app.py speak() while another process holds the engine: EngineBusy, nothing loaded."""
    register(engine, "aiko", MTL)
    other = EngineLock(scratch_config.CACHE_DIR / "engine.lock", program="cli.py")
    other.acquire()
    try:
        job = Job()
        with pytest.raises(EngineBusy) as busy:
            engine.speak("Hello.", "aiko", job=job)
        assert busy.value.__context__ is None and busy.value.holder["program"] == "cli.py"
        assert job.status().phase == "failed"
        assert fake_factories.calls == {} and events == []
        assert engine.residency() == Residency(
            resident=(), loading=None, unloading=None, busy=None, device=None, preferred_device="cpu",
            max_resident=1, last_used=engine.residency().last_used, lock_held=False)
    finally:
        other.release()


# ---------- update_voice ----------

def test_update_voice_edits_the_registry_only(engine: VoiceEngine, fake_factories, make_clip) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL, meta={"consent": True})
    before = engine.voice_info("aiko")
    calls = dict(fake_factories.calls)
    updated = engine.update_voice("aiko", language="JA", exaggeration=1.25, meta={"clip_seconds": 6.0})
    assert (updated["language"], updated["exaggeration"]) == ("ja", 1.25)
    assert updated["meta"] == {"consent": True, "clip_seconds": 6.0}
    for key in ("backend", "reference_wav", "created_at", "forged_at"):
        assert updated[key] == before[key]
    assert engine.voice_info("aiko") == updated
    assert engine.update_voice("aiko", meta={"consent": None})["meta"] == {"clip_seconds": 6.0}
    assert dict(fake_factories.calls) == calls


@pytest.mark.parametrize(("backend", "kwargs"), [
    (STD, {"language": "EN"}), (TUR, {"language": "en"}), (MTL, {"exaggeration": 0.0}),
    (STD, {"exaggeration": 2.0}),
])
def test_update_voice_accepts_valid_edits(engine: VoiceEngine, backend: str, kwargs: dict) -> None:
    register(engine, "aiko", backend)
    updated = engine.update_voice("aiko", **kwargs)
    assert all(updated[key] == (value.lower() if isinstance(value, str) else value) for key, value in kwargs.items())


@pytest.mark.parametrize(("backend", "kwargs", "message"), [
    (MTL, {"language": "xx"}, "Unknown language 'xx' for chatterbox-multilingual"),
    (STD, {"language": "ja"}, "chatterbox-standard is English only"),
    (TUR, {"language": "de"}, "chatterbox-turbo is English only"),
    (TUR, {"exaggeration": 0.5}, "chatterbox-turbo ignores Expressiveness."),
    (MTL, {"exaggeration": -0.01}, "Expressiveness must be between 0.0 and 2.0"),
    (MTL, {"exaggeration": 2.01}, "Expressiveness must be between 0.0 and 2.0"),
    (STD, {"exaggeration": math.nan}, "Expressiveness must be between 0.0 and 2.0"),
    (STD, {"exaggeration": math.inf}, "Expressiveness must be between 0.0 and 2.0"),
    (MTL, {"exaggeration": "loud"}, "Expressiveness must be between 0.0 and 2.0"),
])
def test_update_voice_refuses_invalid_edits(engine: VoiceEngine, backend: str, kwargs: dict, message: str) -> None:
    register(engine, "aiko", backend)
    before = engine.voice_info("aiko")
    with pytest.raises(ValueError, match=re.escape(message)):
        engine.update_voice("aiko", **kwargs)
    assert engine.voice_info("aiko") == before


def test_update_voice_of_an_unregistered_voice(engine: VoiceEngine) -> None:
    with pytest.raises(ValueError, match=re.escape("Voice 'ghost' is not registered.")):
        engine.update_voice("ghost", language="en")


# ---------- delete_voice ----------

def test_delete_discards_the_primary_pt_first_then_the_entry_then_orphans(engine: VoiceEngine, make_clip,
                                                                         scratch_config) -> None:
    clip = make_clip("aiko.wav", 6.0, folder=scratch_config.VOICES_DIR)
    primary = register(engine, "aiko", MTL, clip=clip)
    orphan = write_pt(engine, TUR, "aiko")
    seen: list[tuple[Path, bool]] = []

    def discard(path: Path) -> None:
        seen.append((path, engine.voice_info("aiko") is not None))
        path.unlink()

    result = engine.delete_voice("aiko", discard=discard)
    assert seen == [(primary, True), (orphan, False)]
    assert result == DeleteResult(removed_entry=True, removed_files=(primary, orphan), failed_files=())
    assert engine.voice_info("aiko") is None and not primary.exists() and not orphan.exists()
    assert clip.exists()                                      # the WAV stays


def test_a_failing_discard_of_the_primary_changes_nothing(engine: VoiceEngine, fake_factories, make_clip) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
    primary = engine.voice_cache_path("aiko")

    def discard(path: Path) -> None:
        raise PermissionError("in use")

    with pytest.raises(PermissionError):
        engine.delete_voice("aiko", discard=discard)
    assert engine.voice_info("aiko") is not None and primary.exists()
    assert fake_factories.backend(MTL)._active_voice == "aiko"


def test_an_orphan_that_cannot_be_discarded_is_reported(engine: VoiceEngine) -> None:
    primary = register(engine, "aiko", MTL)
    orphan = write_pt(engine, STD, "aiko")

    def discard(path: Path) -> None:
        if path == orphan:
            raise PermissionError("[WinError 32] The file is in use\nsecond line")
        path.unlink()

    result = engine.delete_voice("aiko", discard=discard)
    assert result == DeleteResult(removed_entry=True, removed_files=(primary,),
                                  failed_files=((orphan, "[WinError 32] The file is in use"),))
    assert engine.voice_info("aiko") is None and orphan.exists()


def test_delete_resets_the_active_voice_of_a_resident_backend(engine: VoiceEngine, fake_factories,
                                                              make_clip) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
    backend = fake_factories.backend(MTL)
    assert backend._active_voice == "aiko"
    assert engine.delete_voice("aiko").removed_entry
    assert backend._active_voice is None


def test_a_missing_primary_pt_still_deletes_and_is_never_discarded(engine: VoiceEngine) -> None:
    primary = register(engine, "aiko", MTL, with_pt=False)
    seen: list[Path] = []
    result = engine.delete_voice("aiko", discard=seen.append)
    assert seen == []
    assert result == DeleteResult(removed_entry=True, removed_files=(primary,), failed_files=())
    assert engine.voice_info("aiko") is None


def test_a_missing_reference_wav_never_blocks_a_delete(engine: VoiceEngine, scratch_config) -> None:
    primary = register(engine, "aiko", MTL, clip=scratch_config.VOICES_DIR / "gone.wav")
    result = engine.delete_voice("aiko")                     # default discard: Path.unlink
    assert result == DeleteResult(removed_entry=True, removed_files=(primary,), failed_files=())
    assert not primary.exists() and engine.voice_info("aiko") is None


def test_a_casefold_twin_on_another_backend_keeps_its_pt(engine: VoiceEngine, scratch_config) -> None:
    primary = register(engine, "aiko", MTL)
    twin = register(engine, "Aiko", TUR)                      # turbo/Aiko.pt; NTFS opens turbo/aiko.pt as it
    swept = scratch_config.CACHE_DIR / TUR / "aiko.pt"
    if not swept.exists():
        pytest.skip("needs a case-insensitive file system (NTFS)")
    seen: list[Path] = []

    def discard(path: Path) -> None:
        seen.append(path)
        path.unlink()

    result = engine.delete_voice("aiko", discard=discard)
    assert seen == [primary]
    assert result == DeleteResult(removed_entry=True, removed_files=(primary,),
                                  failed_files=((swept, "kept: belongs to Aiko"),))
    assert [p.name for p in twin.parent.iterdir()] == ["Aiko.pt"]
    assert engine.voice_info("Aiko") is not None and engine.voice_info("aiko") is None


def test_a_casefold_twin_on_the_same_backend_keeps_the_shared_pt(engine: VoiceEngine, scratch_config) -> None:
    shared = register(engine, "Aiko", MTL)
    engine.registry.register("aiko", MTL, "voices/reference/aiko.wav", 0.5, "en")   # a legacy twin
    own = scratch_config.CACHE_DIR / MTL / "aiko.pt"
    if not own.exists():
        pytest.skip("needs a case-insensitive file system (NTFS)")
    result = engine.delete_voice("aiko", discard=lambda path: pytest.fail(f"discarded {path}"))
    assert result == DeleteResult(removed_entry=True, removed_files=(),
                                  failed_files=((own, "kept: belongs to Aiko"),))
    assert shared.exists() and engine.voice_info("Aiko") is not None


def test_delete_of_an_unknown_or_invalid_voice(engine: VoiceEngine) -> None:
    assert engine.delete_voice("ghost") == DeleteResult(removed_entry=False, removed_files=(), failed_files=())
    with pytest.raises(ValueError, match="Invalid voice_id"):
        engine.delete_voice("../ghost")


def test_delete_waits_for_a_running_take(engine: VoiceEngine, fake_factories, make_clip, pool) -> None:
    engine.load_voice("aiko", make_clip("aiko.wav", 6.0), backend=MTL)
    model = fake_factories.model(MTL)
    gate = model.generate_gate = fake_factories.gate()
    del model
    take = pool.submit(engine.speak, "Hello.", "aiko")
    assert gate.wait_entered(TIMEOUT_S)
    deleting = pool.submit(engine.delete_voice, "aiko")
    time.sleep(0.2)
    assert not deleting.done()
    gate.release()
    assert take.result(TIMEOUT_S)[:4] == b"RIFF"
    assert deleting.result(TIMEOUT_S).removed_entry


# ---------- voice_details and voice_cache_path ----------

def test_voice_details_reports_the_entry_and_its_files(engine: VoiceEngine, make_clip, scratch_config) -> None:
    clip = make_clip("aiko.wav", 6.0, folder=scratch_config.VOICES_DIR)
    engine.load_voice("aiko", clip, backend=MTL, language="ja", exaggeration=0.7,
                      meta={"consent": True, "clip_seconds": 6.0})
    entry = engine.voice_info("aiko")
    assert engine.voice_details("aiko") == [VoiceDetails(
        voice_id="aiko", backend=MTL, backend_enabled=True, language="ja", exaggeration=0.7,
        reference_wav=str(clip.resolve()), reference_path=clip.resolve(),
        conds_path=scratch_config.CACHE_DIR / MTL / "aiko.pt", conds_exists=True,
        created_at=entry["created_at"], forged_at=entry["forged_at"], consent=True,
        meta={"consent": True, "clip_seconds": 6.0})]
    assert engine.voice_details("ghost") == []


def test_voice_details_resolves_stored_clips_and_old_entries(engine: VoiceEngine, make_clip,
                                                             scratch_config) -> None:
    root = scratch_config.ROOT
    relative = make_clip("rel.wav", 2.0, folder=root / "voices" / "reference")
    moved = make_clip("moved.wav", 2.0, folder=scratch_config.VOICES_DIR)
    legacy = {
        "rel": {"backend": MTL, "reference_wav": "voices/reference/rel.wav", "exaggeration": 0.5,
                "language": "en", "created_at": "2026-11-01T10:00:00+00:00"},
        "moved": {"backend": STD, "reference_wav": str(root / "old install" / "moved.wav"),
                  "exaggeration": 0.6, "language": "en", "created_at": "2026-11-02T10:00:00+00:00"},
        "lost": {"backend": TUR, "reference_wav": str(root / "nowhere" / "lost.wav"),
                 "exaggeration": 0.5, "language": "en", "created_at": "2026-11-03T10:00:00+00:00"},
    }
    (scratch_config.CACHE_DIR / "registry.json").write_text(json.dumps(legacy), encoding="utf-8")
    details = {d.voice_id: d for d in engine.voice_details()}
    assert list(details) == ["lost", "moved", "rel"]
    assert details["rel"].reference_path == relative
    assert details["moved"].reference_path == moved
    assert details["lost"].reference_path is None
    lost = details["lost"]
    assert (lost.conds_exists, lost.consent, lost.meta, lost.forged_at, lost.created_at) == (
        False, None, {}, None, "2026-11-03T10:00:00+00:00")


def test_voice_details_marks_a_disabled_backend(scratch_config, fake_factories) -> None:
    scratch_config.ENABLED_BACKENDS = [MTL]
    engine = VoiceEngine(scratch_config)
    engine.registry.register("tur", TUR, "x.wav", 0.5, "en")
    [details] = engine.voice_details()
    assert (details.backend, details.backend_enabled) == (TUR, False)


def test_voice_cache_path(engine: VoiceEngine, scratch_config) -> None:
    register(engine, "aiko", STD, with_pt=False)
    assert engine.voice_cache_path("aiko") == scratch_config.CACHE_DIR / STD / "aiko.pt"
    assert engine.voice_cache_path("ghost") is None


# ---------- facts ----------

def test_backend_facts_delegate(engine: VoiceEngine, monkeypatch) -> None:
    assert engine.backend_info(MTL) is BACKENDS[MTL]
    with pytest.raises(ValueError, match="Unknown backend"):
        engine.backend_info("chatterbox-nano")
    monkeypatch.setattr(engine_module, "_weights_status", lambda name: (True, len(name)))
    assert engine.weights_status(TUR) == (True, len(TUR))
    assert engine.device_info(probe=False).measured_at > 0
