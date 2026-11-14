"""FakeEngine against the real VoiceEngine (§8.3 test_fake_parity), plus its scenarios and test controls.

The real engine runs with E1's fake backend factories on a scratch config (CPU, no model, no torch);
the FakeEngine runs on its own seeded scratch data root. Calls go through a one-thread executor, as the
GUI's run.io_bound does, so the event callbacks can be checked against the worker thread.
"""
from __future__ import annotations

import concurrent.futures
import errno
import inspect
import io
import json
import logging
import os
import subprocess
import sys
import threading
import types
import wave
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from engine.audio import peaks, probe_clip, read_wav_bytes
from engine.engine import VoiceEngine
from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError, ScriptTooLong
from engine.progress import Job
from engine.registry import VoiceRegistry
from engine.types import DeleteResult, EngineEvent, Residency
from tests.engine.conftest import FakeFactories, install_fake_factories, make_scratch_config, write_clip
from ui.contract.routes import ROUTES
from ui.core import fake as fake_module
from ui.core.fake import (GPU_TOTAL_BYTES, PUBLIC_METHODS, SCENARIOS, SCRIPT_JA, SEED_MARKER, FakeEngine, ForgeSeed,
                          Hold, Scenario, Step, scenario_named)
from ui.core.services.load_history import LoadHistory, history_rows
from ui.core.services.takes_store import NewTake, TakesStore
from ui.core.services.voices import VoiceLibrary, created_text

ROOT = Path(__file__).resolve().parents[2]
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
GIB = 2 ** 30
TIMEOUT_S = 10.0
OOM = "CUDA out of memory. Tried to allocate 2.00 GiB"
SEED_VOICE_IDS = {"aiko", "marco", "friend", "narrator", "host-turbo"}
SPEC_SCENARIOS = {                                             # BUILD_SPEC §9, the U3 row
    "speak", "switch", "lowvram", "failure", "forge", "firstrun", "voices", "takes", "models", "settings",
    "other_backend", "loading", "generating", "queued", "forging", "freeing", "relaunch", "forge_ready",
    "forge_other_backend", "forge_low_vram", "forge_replace", "forge_replace_other_backend", "forge_taken_other",
    "forge_turbo_short", "forged", "forge_failed", "voices_missing_files", "voices_unconfirmed", "cpu",
}


# ---------- fixtures and helpers ----------

@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterable[FakeFactories]:
    """E1's counting backend factories for the real engine; every gate is released at teardown."""
    factories = install_fake_factories(monkeypatch)
    yield factories
    factories.release_all()


@pytest.fixture
def make_fake(tmp_path: Path) -> Iterable[Callable[..., FakeEngine]]:
    """Factory for FakeEngines, each on its own data root; their holds are released at teardown."""
    made: list[FakeEngine] = []

    def make(scenario: str | Scenario = "speak", **options: Any) -> FakeEngine:
        engine = FakeEngine(scenario, data_dir=tmp_path / f"fake-{len(made)}", **options)
        made.append(engine)
        return engine

    make.made = made  # type: ignore[attr-defined]
    yield make
    for engine in made:
        engine.release_all()


@pytest.fixture
def pool(make_fake, fakes) -> Iterable[concurrent.futures.ThreadPoolExecutor]:
    """One worker thread, like run.io_bound's; holds and gates are released before it is joined."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    yield executor
    for engine in make_fake.made:
        engine.release_all()
    fakes.release_all()
    executor.shutdown(wait=True)


@pytest.fixture
def clips(tmp_path: Path) -> types.SimpleNamespace:
    folder = tmp_path / "clips"
    notes = folder / "notes.txt"
    folder.mkdir()
    notes.write_text("not audio", encoding="utf-8")
    return types.SimpleNamespace(
        good=write_clip(folder / "good.wav", 6.0), other=write_clip(folder / "other.wav", 7.0),
        four=write_clip(folder / "four.wav", 4.2), silent=write_clip(folder / "silent.wav", 3.0, level=0.0),
        missing=(folder / "missing.wav").resolve(), notes=notes,
    )


class Recorder:
    """An on_event handler noting each event, its thread and the residency the handler sees."""

    def __init__(self) -> None:
        self.engine: Any = None
        self.seen: list[tuple[EngineEvent, int, Residency]] = []

    def __call__(self, event: EngineEvent) -> None:
        self.seen.append((event, threading.get_ident(), self.engine.residency()))

    def stream(self) -> list[tuple[str, str, str | None, str | None]]:
        return [(event.kind, event.backend, event.reason, event.error) for event, _, _ in self.seen]

    def states(self) -> list[tuple[Any, ...]]:
        return [state_of(state) for _, _, state in self.seen]

    def threads(self) -> set[int]:
        return {thread for _, thread, _ in self.seen}


def state_of(state: Residency) -> tuple[Any, ...]:
    return state.busy, state.loading, state.unloading, state.resident, state.lock_held


@pytest.fixture
def real(tmp_path: Path, fakes) -> Iterable[VoiceEngine]:
    recorder = Recorder()
    engine = VoiceEngine(make_scratch_config(tmp_path / "real"), on_event=recorder)
    recorder.engine = engine
    engine.recorder = recorder  # type: ignore[attr-defined]
    yield engine
    engine.close()


def recorded(engine: FakeEngine) -> Recorder:
    recorder = Recorder()
    recorder.engine = engine
    engine.on_event = recorder
    return recorder


def register_seed_voices(engine: VoiceEngine, clip: Path) -> None:
    """Give the real engine the two seed voices the parity checks use (aiko, host-turbo)."""
    engine.load_voice("aiko", clip, backend=MTL, language="ja")
    engine.load_voice("host-turbo", clip, backend=TUR)


def outcome(call: Callable[[], Any]) -> tuple[str, str]:
    """(type name, message) of what call raised, or ("returned", repr) when it did not raise."""
    try:
        value = call()
    except Exception as exc:  # noqa: BLE001 - every outcome is compared
        assert exc.__context__ is None
        return type(exc).__name__, str(exc)
    return "returned", repr(value)


def gb(n_bytes: int | None) -> str:
    return "—" if n_bytes is None else f"{n_bytes / GIB:.1f}"


def relative(paths: Iterable[Path], root: Path) -> list[str]:
    return [Path(path).relative_to(root).as_posix() for path in paths]


# ---------- the public surface ----------

def _public(cls: type) -> dict[str, Callable[..., Any]]:
    return {name: member for name, member in inspect.getmembers(cls, callable)
            if not name.startswith("_") and name in vars(cls)}


def _params(function: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def test_every_public_engine_method_is_on_the_fake_with_the_same_signature() -> None:
    engine_methods = _public(VoiceEngine)
    assert set(engine_methods) == set(PUBLIC_METHODS)
    for name, method in engine_methods.items():
        assert _params(getattr(FakeEngine, name)) == _params(method), name


def test_the_fake_has_the_engines_attributes(make_fake, tmp_path: Path) -> None:
    fake = make_fake()
    assert isinstance(fake.registry, VoiceRegistry)
    assert fake.registry.path == fake.data_dir / "cache" / "registry.json"
    assert set(vars(make_scratch_config(tmp_path / "scratch"))) <= set(vars(fake.config))
    assert (fake.config.ROOT, fake.config.CACHE_DIR, fake.config.VOICES_DIR, fake.config.OUTPUTS_DIR) == (
        fake.data_dir, fake.data_dir / "cache", fake.data_dir / "voices" / "reference", fake.data_dir / "outputs")
    assert fake.list_backends() == [MTL, STD, TUR] and fake.residency().max_resident == 1


# ---------- the event stream (switch, unload, failed load) ----------

EXPECTED_STREAM = [
    ("load_started", TUR, None, None),
    ("unloaded", MTL, "evicted", None),
    ("loaded", TUR, None, None),
    ("unloaded", TUR, "requested", None),
    ("load_started", STD, None, None),
    ("load_failed", STD, None, f"out_of_memory: {OOM}"),
]
EXPECTED_STATES = [                                          # (busy, loading, unloading, resident, lock_held)
    ("load", TUR, None, (MTL,), True),
    ("load", TUR, None, (), True),
    (None, None, None, (TUR,), True),
    ("unload", None, None, (), True),
    ("load", STD, None, (), True),
    (None, None, None, (), False),
]


def _script(engine: Any, recorder: Recorder, arm: Callable[[], None]) -> BaseException:
    """Multilingual resident; switch to Turbo, unload it, then a failed Standard load."""
    engine.make_resident(MTL)
    recorder.seen.clear()
    report = engine.make_resident(TUR)
    assert (report.backend, report.evicted, report.already_resident) == (TUR, (MTL,), False)
    assert engine.unload_backend(TUR) is True
    arm()
    try:
        engine.make_resident(STD)
    except BackendLoadError as exc:
        return exc
    raise AssertionError("the armed load did not fail")


@pytest.mark.parametrize("cuda", [False, True], ids=["fake-cpu", "fake-cuda"])
def test_one_script_gives_the_same_events_on_both_engines(real, fakes, make_fake, pool, cuda: bool) -> None:
    worker = pool.submit(threading.get_ident).result(TIMEOUT_S)
    assert worker != threading.get_ident()
    real_failed = pool.submit(_script, real, real.recorder,
                              lambda: fakes.fail(STD, RuntimeError(OOM))).result(TIMEOUT_S)
    fake = make_fake(replace(SCENARIOS["speak"], cuda=cuda))
    recorder = recorded(fake)
    fake_failed = pool.submit(_script, fake, recorder,
                              lambda: fake.fail_next("make_resident", RuntimeError(OOM))).result(TIMEOUT_S)

    assert real.recorder.stream() == recorder.stream() == EXPECTED_STREAM
    assert real.recorder.states() == recorder.states() == EXPECTED_STATES
    assert real.recorder.threads() == recorder.threads() == {worker}
    for failed in (real_failed, fake_failed):
        assert (type(failed), failed.backend, failed.kind, failed.detail) == (BackendLoadError, STD, "out_of_memory", OOM)
        assert failed.__context__ is None and failed.__cause__ is None
    assert state_of(real.residency()) == state_of(fake.residency()) == (None, None, None, (), False)
    if not cuda:                                             # on the CPU even the measurements agree
        def measured(rec: Recorder) -> list[tuple[Any, ...]]:
            return [(e.device, e.freed_bytes, e.peak_bytes, e.reserved_bytes, e.allocated_bytes)
                    for e, _, _ in rec.seen if e.kind != "load_started"]
        assert measured(real.recorder) == measured(recorder)
        assert {e.device for e, _, _ in recorder.seen if e.kind != "load_started"} == {"cpu"}


def test_a_cuda_switch_reports_what_it_frees(make_fake) -> None:
    fake = make_fake()
    recorder = recorded(fake)
    report = fake.make_resident(TUR)
    evicted, loaded = recorder.seen[1][0], recorder.seen[2][0]
    assert (gb(report.freed_bytes), gb(report.reserved_bytes), report.seconds, report.device) == ("3.4", "3.1", 8.7, "cuda")
    assert (gb(evicted.freed_bytes), gb(evicted.peak_bytes), evicted.reserved_bytes, evicted.allocated_bytes) == (
        "3.4", "3.6", 0, 0)
    assert (loaded.seconds, gb(loaded.reserved_bytes), loaded.first_in_process) == (8.7, "3.1", False)
    again = fake.make_resident(TUR)
    assert (again.already_resident, again.seconds, again.evicted, again.freed_bytes) == (True, 0.0, (), None)
    assert len(recorder.seen) == 3


def test_a_failing_event_handler_never_breaks_the_fake(make_fake, caplog) -> None:
    def handler(event: EngineEvent) -> None:
        raise RuntimeError("handler bug")

    caplog.set_level(logging.WARNING, logger="engine")
    fake = make_fake(on_event=handler)
    assert fake.make_resident(TUR).backend == TUR and fake.unload_backend(TUR)
    err = "\n".join(r.getMessage() for r in caplog.records if r.name == "engine")
    assert "on_event handler failed on 'load_started'" in err and "handler bug" in err


# ---------- require_resident and the refusals ----------

def _refusals(engine: Any, clip: Path) -> list[tuple[Any, ...]]:
    engine.make_resident(TUR)
    found = []
    for call in (lambda job: engine.speak("Hello.", "aiko", require_resident=True, job=job),
                 lambda job: engine.load_voice("rin", clip, backend=MTL, require_resident=True, job=job)):
        job = Job()
        with pytest.raises(BackendNotResident) as refused:
            call(job)
        found.append((str(refused.value), refused.value.__context__, engine.residency().resident,
                      job.status().phase, engine.voice_info("rin")))
    return found


def test_require_resident_refuses_like_the_engine(real, fakes, make_fake, clips) -> None:
    register_seed_voices(real, clips.good)
    builds = len(fakes.builds)
    fake = make_fake()
    assert _refusals(real, clips.good) == _refusals(fake, clips.good) == [
        ("chatterbox-multilingual is not loaded (chatterbox-turbo is)", None, (TUR,), "failed", None)] * 2
    assert len(fakes.builds) == builds and fake.loads == [TUR]


@pytest.mark.parametrize(("voice", "text", "limit"), [
    ("aiko", "日本語" * 1000, 2048), ("host-turbo", "Hello. " * 500, 1024)])
def test_over_limit_scripts_leave_both_engines_usable(real, make_fake, clips, voice, text, limit):
    register_seed_voices(real, clips.good)
    results = []
    for engine in (real, make_fake()):
        job = Job()
        with pytest.raises(ScriptTooLong) as exc:
            engine.speak(text, voice, job=job)
        assert exc.value.limit == limit and exc.value.__context__ is None
        assert job.status().phase == "failed" and engine.residency().busy is None
        assert engine.speak("OK.", voice)[:4] == b"RIFF"
        results.append(str(exc.value))
    assert results[0] == results[1]


REFUSALS: dict[str, Callable[[Any, types.SimpleNamespace], Any]] = {
    "empty-text": lambda e, c: e.speak("  \n", "aiko"),
    "unregistered": lambda e, c: e.speak("Hello.", "ghost"),
    "update-unregistered": lambda e, c: e.update_voice("ghost", language="ja"),
    "turbo-expressiveness": lambda e, c: e.update_voice("host-turbo", exaggeration=0.7),
    "english-only": lambda e, c: e.update_voice("host-turbo", language="ja"),
    "expressiveness-range": lambda e, c: e.update_voice("aiko", exaggeration=2.5),
    "unknown-language": lambda e, c: e.update_voice("aiko", language="xx"),
    "device": lambda e, c: e.set_preferred_device("mps"),
    "backend": lambda e, c: e.make_resident("chatterbox-nano"),
    "backend-info": lambda e, c: e.backend_info("chatterbox-nano"),
    "weights": lambda e, c: e.weights_status("chatterbox-nano"),
    "bad-id": lambda e, c: e.load_voice("bad name", c.good, backend=TUR),
    "reserved-id": lambda e, c: e.load_voice("nul", c.good, backend=TUR),
    "long-id": lambda e, c: e.load_voice("x" * 65, c.good, backend=TUR),
    "case-clash": lambda e, c: e.load_voice("AIKO", c.good, backend=MTL),
    "missing-clip": lambda e, c: e.load_voice("rin", c.missing, backend=MTL),
    "not-audio": lambda e, c: e.load_voice("rin", c.notes, backend=MTL),
    "silent-clip": lambda e, c: e.load_voice("rin", c.silent, backend=MTL),
    "short-for-turbo": lambda e, c: e.load_voice("rin", c.four, backend=TUR),
    "language": lambda e, c: e.load_voice("rin", c.good, backend=MTL, language="xx"),
    "delete-bad-id": lambda e, c: e.delete_voice("../x"),
    "not-resident-unload": lambda e, c: e.unload_backend(STD),
    "unknown-cache-path": lambda e, c: e.voice_cache_path("ghost"),
    "unknown-info": lambda e, c: e.voice_info("ghost"),
    "unknown-details": lambda e, c: e.voice_details("ghost"),
    "edit-language": lambda e, c: e.update_voice("aiko", language=" JA ")["language"],
    "turbo-facts": lambda e, c: (e.backend_info(TUR).uses_cfg, e.backend_info(TUR).tags),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusals_and_answers_read_the_same_on_both_engines(real, fakes, make_fake, clips, case: str) -> None:
    register_seed_voices(real, clips.good)
    fake = make_fake()
    fake_outcome = outcome(lambda: REFUSALS[case](fake, clips))
    assert outcome(lambda: REFUSALS[case](real, clips)) == fake_outcome
    assert fake_outcome[0] in ("ValueError", "returned"), fake_outcome


# ---------- voices: forge, cache hit, delete ----------

def _forge_rin(engine: Any, clips: types.SimpleNamespace) -> tuple[Any, ...]:
    root = Path(engine.config.ROOT)
    engine.make_resident(MTL)
    meta = {"consent": True, "consent_at": "2026-11-13T05:00:00+00:00", "clip_seconds": 6.0, "clip_source": "good.wav"}
    job = Job()
    assert engine.load_voice("rin", clips.good, backend=MTL, exaggeration=0.7, language="JA", force=True,
                             require_resident=True, meta=meta, job=job) == "rin"
    entry = engine.voice_info("rin")
    (details,) = engine.voice_details("rin")
    baked = (job.status().phase, job.bake_s is not None, sorted(entry), entry["meta"], entry["reference_wav"],
             entry["language"], entry["exaggeration"], details.conds_exists,
             relative([details.conds_path], root), details.reference_path, details.consent)
    hit = Job()
    with pytest.warns(UserWarning, match="already has cached conditioning"):
        engine.load_voice("rin", clips.other, backend=MTL, job=hit)
    kept = engine.voice_info("rin")
    return baked, (hit.status().phase, hit.bake_s, kept["reference_wav"], kept["created_at"] == entry["created_at"])


def test_a_forged_voice_is_registered_as_the_engine_registers_it(real, fakes, make_fake, clips) -> None:
    baked, hit = _forge_rin(make_fake(), clips)
    assert (baked, hit) == _forge_rin(real, clips)
    assert baked[0:2] == ("done", True) and baked[4] == str(clips.good) and baked[5:7] == ("ja", 0.7)
    assert hit == ("done", None, str(clips.good), True)


def _delete_aiko(engine: Any) -> tuple[Any, ...]:
    cache = Path(engine.config.CACHE_DIR)
    orphan = cache / TUR / "aiko.pt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("{}", encoding="utf-8")
    discarded: list[Path] = []
    result = engine.delete_voice("aiko", discard=discarded.append)
    again = engine.delete_voice("aiko")
    return (result.removed_entry, relative(result.removed_files, cache), result.failed_files,
            relative(discarded, cache), engine.voice_info("aiko"), again)


def test_delete_follows_the_engines_rules(real, fakes, make_fake, clips) -> None:
    register_seed_voices(real, clips.good)
    assert _delete_aiko(real) == _delete_aiko(make_fake()) == (
        True, [f"{MTL}/aiko.pt", f"{TUR}/aiko.pt"], (), [f"{MTL}/aiko.pt", f"{TUR}/aiko.pt"], None,
        DeleteResult(removed_entry=False, removed_files=(), failed_files=()))


def test_deleting_the_active_voice_resets_it(make_fake) -> None:
    """aiko is the resident model's active voice; once deleted, a new aiko without a .pt cannot speak."""
    fake = make_fake()
    assert fake.delete_voice("aiko").removed_entry and not (fake.config.CACHE_DIR / MTL / "aiko.pt").exists()
    fake.registry.register("aiko", MTL, str(fake.config.VOICES_DIR / "aiko_ref.wav"), 0.5, "ja")
    job = Job()
    with pytest.raises(ValueError, match="Voice 'aiko' has no cached conditionals"):
        fake.speak("Hello.", "aiko", job=job)
    assert job.status().phase == "failed"


# ---------- failures: the engine's classification ----------

TAKE_ERRORS = [
    RuntimeError("CUDA error: device-side assert triggered\nCompile with TORCH_USE_CUDA_DSA"),
    RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB"),
    AssertionError(),
    ValueError("Unsupported language_id 'xx'\nsecond line"),
]


def _failed_take(engine: Any, arm: Callable[[], None]) -> tuple[Any, ...]:
    arm()
    job = Job()
    with pytest.raises(Exception) as failed:
        engine.speak("Hello.", "aiko", job=job)
    exc = failed.value
    return (type(exc).__name__, str(exc), getattr(exc, "kind", None), getattr(exc, "during", None),
            exc.__context__, job.status().phase, engine.residency().busy, engine.is_resident(MTL))


@pytest.mark.parametrize("error", TAKE_ERRORS, ids=["runtime", "oom", "assert", "value"])
def test_take_failures_come_out_like_the_engines(real, fakes, make_fake, clips, error: Exception) -> None:
    real.load_voice("aiko", clips.good, backend=MTL, language="ja")

    def arm_real() -> None:
        fakes.model(MTL).generate_error = error

    fake = make_fake()
    assert _failed_take(real, arm_real) == _failed_take(fake, lambda: fake.fail_next("speak", error))


def _failed_bake(engine: Any, clip: Path, arm: Callable[[], None]) -> tuple[Any, ...]:
    engine.make_resident(MTL)
    arm()
    job = Job()
    with pytest.raises(Exception) as failed:
        engine.load_voice("rin", clip, backend=MTL, job=job)
    exc = failed.value
    return (type(exc).__name__, str(exc), getattr(exc, "kind", None), getattr(exc, "during", None),
            exc.__context__, job.status().phase, engine.voice_info("rin"))


@pytest.mark.parametrize("error", [RuntimeError("bake failed\nsecond line"),
                                   ValueError("chatterbox-turbo rejected the reference clip: x"),
                                   RuntimeError("CUDA out of memory. Tried to allocate 8.00 MiB")],
                         ids=["runtime", "value", "oom"])
def test_bake_failures_come_out_like_the_engines(real, fakes, make_fake, clips, error: Exception) -> None:
    def arm_real() -> None:
        fakes.model(MTL).bake_error = error

    fake = make_fake()
    assert _failed_bake(real, clips.good, arm_real) == _failed_bake(
        fake, clips.good, lambda: fake.fail_next("load_voice", error))


class OfflineModeIsEnabled(ConnectionError):
    """Named like huggingface_hub's error, which the engine matches by name."""


@pytest.mark.parametrize("error", [
    RuntimeError(f"{OOM}\nGPU 0 has a total capacity of 11.99 GiB"), FileNotFoundError("ve.pt is not in the local cache"),
    OfflineModeIsEnabled("Cannot reach the Hub"), ValueError("bad config"), KeyError(),
], ids=["oom", "missing", "offline", "other", "no-message"])
def test_build_failures_classify_like_the_engine(real, fakes, make_fake, error: Exception) -> None:
    def failed(engine: Any) -> tuple[Any, ...]:
        with pytest.raises(BackendLoadError) as caught:
            engine.make_resident(TUR)
        exc = caught.value
        return exc.backend, exc.kind, exc.detail, str(exc), exc.__context__

    fakes.fail(TUR, error)
    fake = make_fake()
    fake.fail_next("load_backend", error)                     # the alias arms make_resident
    assert failed(real) == failed(fake)


# ---------- fail_next: the engine's own types, fresh ----------

HOLDER = {"pid": 4242, "program": "cli.py", "since": "14:02:11"}


def test_engine_busy_refuses_before_anything_changes(make_fake) -> None:
    fake = make_fake("relaunch")
    recorder = recorded(fake)
    template = EngineBusy(HOLDER)
    fake.fail_next("make_resident", template)
    with pytest.raises(EngineBusy) as busy:
        fake.make_resident(MTL)
    assert busy.value is not template and busy.value.__context__ is None and busy.value.holder == HOLDER
    assert str(busy.value) == ("The voice engine is in use by cli.py (pid 4242) since 14:02:11. "
                               "Close it, or wait for it to finish, then try again.")
    assert recorder.seen == [] and fake.loads == [] and state_of(fake.residency()) == (None, None, None, (), False)
    assert fake.make_resident(MTL).backend == MTL


def test_engine_busy_waits_while_the_fake_holds_the_lock(make_fake) -> None:
    """With a model resident the engine lock is held, so another program cannot refuse a switch."""
    fake = make_fake()
    fake.fail_next("make_resident", EngineBusy(HOLDER))
    assert fake.make_resident(TUR).evicted == (MTL,)
    assert fake.unload_backend(TUR) and not fake.residency().lock_held
    with pytest.raises(EngineBusy):
        fake.make_resident(TUR)
    assert fake.armed_failures("make_resident") == ()


def test_an_armed_load_error_keeps_its_kind_and_cleans_up(make_fake) -> None:
    fake = make_fake()
    recorder = recorded(fake)
    template = BackendLoadError(TUR, "missing_files", "ve.safetensors is not in the local cache", 0.4)
    fake.fail_next("make_resident", template)
    with pytest.raises(BackendLoadError) as failed:
        fake.make_resident(STD)
    exc = failed.value
    assert exc is not template and exc.__context__ is None
    assert (exc.backend, exc.kind, exc.detail, exc.seconds) == (STD, "missing_files", template.detail, 0.4)
    assert recorder.stream()[-1] == ("load_failed", STD, None, f"missing_files: {template.detail}")
    assert state_of(fake.residency()) == (None, None, None, (), False) and fake.loads == [STD]


@pytest.mark.parametrize(("template", "raised", "phase"), [
    (GenerationError("out_of_memory", "Tried to allocate 2.00 GiB", during="speak"), GenerationError, "failed"),
    (BackendNotResident(MTL, (TUR,)), BackendNotResident, "failed"),
    (Cancelled(), Cancelled, "cancelled"),
    (ValueError("Unsupported language_id 'xx'\nmore"), ValueError, "failed"),
], ids=["generation", "not-resident", "cancelled", "value"])
def test_fail_next_on_speak_raises_a_fresh_engine_type(make_fake, template, raised, phase) -> None:
    fake = make_fake()
    fake.fail_next("speak", template)
    job = Job()
    with pytest.raises(raised) as failed:
        fake.speak("Hello.", "aiko", require_resident=True, job=job)
    exc = failed.value
    assert type(exc) is raised and exc is not template
    assert exc.__context__ is None and exc.__cause__ is None
    assert str(exc) == (str(template).splitlines()[0] if str(template) else "")
    if isinstance(template, GenerationError):
        assert (exc.kind, exc.during) == ("out_of_memory", "speak")
    assert job.status().phase == phase and fake.is_resident(MTL) and fake.residency().busy is None
    assert fake.speak("Hello.", "aiko")[:4] == b"RIFF"       # one-shot: the next take works


def test_fail_next_on_a_bake_registers_nothing(make_fake, clips) -> None:
    fake = make_fake("forge_failed")
    (template,) = fake.armed_failures("load_voice")
    job = Job()
    with pytest.raises(GenerationError) as failed:
        fake.load_voice("rin", clips.good, backend=MTL, force=True, require_resident=True, job=job)
    assert failed.value is not template and failed.value.__context__ is None
    assert (failed.value.kind, failed.value.during, str(failed.value)) == ("out_of_memory", "bake", template.detail)
    assert fake.voice_info("rin") is None and not (fake.config.CACHE_DIR / MTL / "rin.pt").exists()
    assert job.status().phase == "failed" and fake.armed_failures("load_voice") == ()


def test_fail_next_on_plain_methods_raises_at_their_start(make_fake) -> None:
    fake = make_fake()
    fake.fail_next("delete_voice", PermissionError(13, "The file is in use"))
    fake.fail_next("update_voice", ValueError("first\nsecond"))
    with pytest.raises(PermissionError) as denied:
        fake.delete_voice("aiko")
    assert denied.value.__context__ is None and fake.voice_info("aiko") is not None
    with pytest.raises(ValueError, match=r"^first$"):
        fake.update_voice("aiko", language="it")
    assert fake.voice_info("aiko")["language"] == "ja"


def test_a_failed_device_reading_carries_the_error(make_fake) -> None:
    fake = make_fake()
    fake.fail_next("device_info", RuntimeError("CUDA driver version is insufficient\ndetails"))
    reading = fake.device_info(probe=True)
    assert (reading.cuda_available, reading.error, reading.total_bytes) == (
        False, "CUDA driver version is insufficient", None)
    assert fake.device_info(probe=True).cuda_available is True


def test_fail_next_and_hold_refuse_what_the_engine_lacks(make_fake) -> None:
    fake = make_fake()
    with pytest.raises(ValueError, match="has no 'warp_drive'"):
        fake.fail_next("warp_drive", RuntimeError("x"))
    with pytest.raises(ValueError, match="has no 'evict'"):
        fake.fail_next("evict", RuntimeError("x"))
    with pytest.raises(TypeError):
        fake.fail_next("speak", "not an exception")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        fake.hold("_load")


# ---------- holds ----------

def test_a_held_build_looks_like_the_engines_blocked_build(real, fakes, make_fake, pool) -> None:
    real.make_resident(MTL)
    gate = fakes.block(TUR)
    switching = pool.submit(real.make_resident, TUR)
    assert gate.wait_entered(TIMEOUT_S)
    real_held = state_of(real.residency()), real.recorder.stream()[-2:]
    gate.release()
    switching.result(TIMEOUT_S)

    fake = make_fake()
    recorder = recorded(fake)
    hold = fake.hold("make_resident")
    switching = pool.submit(fake.make_resident, TUR)
    assert hold.wait_entered(TIMEOUT_S)
    fake_held = state_of(fake.residency()), recorder.stream()[-2:]
    assert fake.residency().loading == TUR and fake.device_info().reserved_bytes == 0   # never waits
    hold.release()
    assert switching.result(TIMEOUT_S).backend == TUR
    assert real_held == fake_held == (("load", TUR, None, (), True),
                                      [("load_started", TUR, None, None), ("unloaded", MTL, "evicted", None)])


def test_a_held_take_looks_like_the_engines_gated_take_and_cancels_within_a_step(
        real, fakes, make_fake, pool, clips) -> None:
    real.load_voice("aiko", clips.good, backend=MTL, language="ja")

    def gated_real() -> Any:
        """E1's Gate on the real engine's next take (wait_entered/release, like a Hold)."""
        model = fakes.model(MTL)
        model.generate_gate = fakes.gate()
        return model.generate_gate

    def cancel_a_held_take(engine: Any, gate: Any) -> tuple[Any, ...]:
        job = Job()
        take = pool.submit(engine.speak, "Hello there.", "aiko", job=job)
        assert gate.wait_entered(TIMEOUT_S)
        held = job.status().phase, job.status().tokens, engine.residency().busy
        job.cancel()
        gate.release()
        with pytest.raises(Cancelled) as cancelled:
            take.result(TIMEOUT_S)
        return held, cancelled.value.__context__, job.status().phase, job.status().tokens, engine.residency().busy

    fake = make_fake()
    assert cancel_a_held_take(real, gated_real()) == cancel_a_held_take(fake, fake.hold("speak")) == (
        ("sampling", 1, "generate"), None, "cancelled", 1, None)
    assert fake.speak("Next.", "aiko")[:4] == b"RIFF"


def test_hold_unload_backend_parks_before_the_model_is_freed(make_fake, pool) -> None:
    fake = make_fake()
    recorder = recorded(fake)
    hold = fake.hold("unload_backend")
    freeing = pool.submit(fake.unload_backend, MTL)
    assert hold.wait_entered(TIMEOUT_S)
    assert state_of(fake.residency()) == ("unload", None, MTL, (MTL,), True)
    assert recorder.seen == [] and gb(fake.device_info().reserved_bytes) == "3.4"
    hold.release()
    assert freeing.result(TIMEOUT_S) is True
    ((event, _, state),) = recorder.seen
    assert (event.kind, event.backend, event.reason, gb(event.freed_bytes)) == ("unloaded", MTL, "requested", "3.4")
    assert state_of(state) == ("unload", None, None, (), True)
    assert state_of(fake.residency()) == (None, None, None, (), False)


def test_hold_load_voice_parks_the_bake(make_fake, pool) -> None:
    fake = make_fake("forge_ready")
    hold = fake.hold("load_voice")
    job = Job()
    forging = pool.submit(fake.load_voice, "rin", fake.config.VOICES_DIR / "sample.wav", backend=MTL,
                          exaggeration=0.5, language="ja", force=True, require_resident=True,
                          meta={"consent": True}, job=job)
    assert hold.wait_entered(TIMEOUT_S)
    assert (job.status().phase, fake.residency().busy, fake.voice_info("rin")) == ("baking", "bake", None)
    hold.release()
    assert forging.result(TIMEOUT_S) == "rin"
    assert (job.status().phase, job.bake_s, fake.voice_info("rin")["meta"]) == ("done", 2.1, {"consent": True})
    assert (fake.config.CACHE_DIR / MTL / "rin.pt").is_file() and fake.loads == []


def test_hold_evict_parks_a_switch_in_its_unload_phase(make_fake, pool) -> None:
    fake = make_fake()
    hold = fake.hold("evict")
    switching = pool.submit(fake.make_resident, TUR)
    assert hold.wait_entered(TIMEOUT_S)
    assert state_of(fake.residency()) == ("load", TUR, MTL, (MTL,), True)
    hold.release()
    assert switching.result(TIMEOUT_S).evicted == (MTL,)


def test_disarm_drops_every_armed_failure_and_releases_the_holds(make_fake) -> None:
    """The public way to stop injecting faults (R1's monkey used to reach into FakeEngine._fails)."""
    fake = make_fake()
    fake.fail_next("make_resident", EngineBusy({"pid": 1, "program": "cli.py", "since": "now"}))
    fake.fail_next("speak", GenerationError("other", "boom", during="speak"))
    hold = fake.hold("delete_voice")
    assert fake.disarm() == 2 and hold.is_set()
    assert fake.armed_failures("make_resident") == () and fake.armed_failures("speak") == ()
    assert fake.make_resident(TUR).backend == TUR


def test_a_long_dev_run_keeps_only_the_newest_calls(make_fake) -> None:
    """--fake dev runs record a residency() call every 2 s for hours: max_calls bounds the list."""
    fake = make_fake()
    fake.max_calls = 50
    for _ in range(500):
        fake.residency()
    assert 50 <= len(fake.calls) <= 100 and all(call.name == "residency" for call in fake.calls[-50:])


def test_entry_holds_wait_and_release_all_frees_them(make_fake, pool) -> None:
    fake = make_fake()
    first, second = fake.hold("delete_voice"), fake.hold("load_backend")
    assert isinstance(first, Hold) and (first.point, second.point) == ("delete_voice", "make_resident")
    deleting = pool.submit(fake.delete_voice, "marco")
    assert first.wait_entered(TIMEOUT_S) and fake.voice_info("marco") is not None
    fake.release_all()
    assert deleting.result(TIMEOUT_S).removed_entry and second.is_set()
    assert fake.holds == (first, second)


# ---------- records and the one-model rule ----------

def test_calls_and_loads_record_what_ran(make_fake) -> None:
    fake = make_fake()
    assert fake.calls == [] and fake.loads == []                # the scenario's model was loaded before launch
    fake.make_resident(TUR)
    fake.fail_next("load_backend", RuntimeError("CUDA out of memory"))
    with pytest.raises(BackendLoadError):
        fake.load_backend(STD)
    assert fake.loads == [TUR, STD]
    assert [call.name for call in fake.calls] == ["make_resident", "load_backend", "make_resident"]
    last = fake.calls[-1]
    assert (last.args, last.outcome, last.thread) == ((STD,), "raised", threading.get_ident())
    assert last.error == "BackendLoadError: chatterbox-standard failed to load: CUDA out of memory"
    assert fake.calls[0].outcome == "returned" and fake.calls[0].ended >= fake.calls[0].started
    fake.reset_records()
    assert fake.calls == [] and fake.loads == []


def test_the_one_model_rule_trips_on_a_second_resident_model(make_fake) -> None:
    fake = make_fake(max_resident=2)
    assert fake.residency().max_resident == 2
    with pytest.raises(AssertionError, match="one model in VRAM"):
        fake.make_resident(TUR)
    assert state_of(fake.residency()) == (None, None, None, (MTL,), True) and fake.loads == []


# ---------- takes ----------

def test_a_take_is_a_pcm16_wav_as_long_as_its_text(make_fake) -> None:
    fake = make_fake()
    job = Job()
    wav = fake.speak(SCRIPT_JA, "aiko", require_resident=True, job=job, language_id="ja", exaggeration=0.5,
                     cfg_weight=0.5)
    with wave.open(io.BytesIO(wav)) as take:
        assert (take.getnchannels(), take.getsampwidth(), take.getframerate(), take.getnframes()) == (
            1, 2, 24000, 110400)
    status = job.status()
    assert (status.phase, status.tokens, status.max_tokens, status.hit_token_limit) == ("done", 115, 1000, False)
    assert (job.made_s, job.conds_load_s, gb(job.peak_reserved_bytes)) == (6.9, None, "3.6")   # aiko was active
    other = Job()
    fake.speak("Ciao!", "marco", job=other)
    assert other.conds_load_s == 0.03 and other.made_s is not None


def test_turbo_takes_decode_and_long_scripts_stop_at_the_token_limit(make_fake) -> None:
    fake = make_fake()
    fake.make_resident(TUR)
    job = Job()
    fake.speak("Okay, okay [chuckle] it works.", "host-turbo", require_resident=True, job=job)
    assert (job.status().phase, job.decode_steps, job.status().hit_token_limit) == ("done", 2, False)
    long = Job()
    wav = fake.speak("A long script. " * 80, "host-turbo", job=long)
    assert (long.status().tokens, long.status().hit_token_limit) == (1000, True)
    with wave.open(io.BytesIO(wav)) as take:
        assert take.getnframes() == 40 * 24000


def test_the_takes_store_lists_a_fake_take(make_fake) -> None:
    fake = make_fake()
    store = TakesStore(fake.data_dir / "outputs" / "takes" / "index.json", fake.data_dir)
    store.load()
    wav = fake.speak(SCRIPT_JA, "aiko", require_resident=True)
    take = store.add(wav, NewTake(voice="aiko", backend=MTL, language="ja", text=SCRIPT_JA, exaggeration=0.5,
                                  cfg_weight=0.5, gen_seconds=6.9, session=fake.session_id, tokens=115),
                     fake.data_dir / "outputs" / "takes")
    assert (take.id, round(take.seconds, 1), take.sample_rate, len(take.peaks)) == (8, 4.6, 24000, 230)
    assert 50 <= max(take.peaks) <= 70


NO_AUDIO_LIBRARIES = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("fake_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["fake_under_test"] = module
spec.loader.exec_module(module)
fake = module.FakeEngine("forge", data_dir=Path(sys.argv[2]))
wav = fake.speak("Hello there.", "aiko", require_resident=True)
fake.make_resident("chatterbox-turbo")
fake.device_info(probe=True)
print(json.dumps({"riff": wav[:4].decode(), "loaded": [m for m in ("numpy", "soundfile", "torch", "chatterbox")
                                                        if m in sys.modules]}))
"""


def test_the_fake_imports_no_audio_or_model_libraries(tmp_path: Path) -> None:
    """fake.py is loaded on its own (not through ui.core), so whatever ui.core imports is not counted."""
    done = subprocess.run([sys.executable, "-c", NO_AUDIO_LIBRARIES, str(ROOT / "ui" / "core" / "fake.py"),
                           str(tmp_path / "data")], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1"))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"riff": "RIFF", "loaded": []}


# ---------- scenarios: the list and every seed ----------

def test_the_scenarios_are_the_spec_list() -> None:
    assert set(SCENARIOS) == SPEC_SCENARIOS and len(SCENARIOS) == 29
    assert all(name == scenario.name for name, scenario in SCENARIOS.items())
    with pytest.raises(ValueError, match="Unknown FakeEngine scenario 'nope'; use one of: speak, switch"):
        scenario_named("nope")


@pytest.mark.parametrize("name", sorted(SPEC_SCENARIOS))
def test_every_scenario_seeds_its_data(make_fake, name: str) -> None:
    fake = make_fake(name)
    assert fake.calls == [] and fake.loads == []               # seeding and the scenario's model: no calls
    scenario, root = SCENARIOS[name], fake.data_dir
    assert json.loads((root / SEED_MARKER).read_text(encoding="utf-8")) == {"scenario": name}
    assert scenario.route in ROUTES and fake.scenario is scenario
    assert fake.residency().resident == ((scenario.resident,) if scenario.resident else ())
    assert fake.residency().lock_held is bool(scenario.resident)
    assert [hold.point for hold in fake.holds] == list(scenario.holds)
    for method, error in scenario.fails:
        assert fake.armed_failures(method) == (error,)
    entries = fake.registry.entries()
    if scenario.data == "firstrun":
        assert entries == {} and not (root / "outputs" / "takes" / "index.json").exists()
        return
    assert set(entries) == SEED_VOICE_IDS and (root / "outputs" / "takes" / "index.json").is_file()
    for voice_id, entry in entries.items():
        reference, pt = Path(entry["reference_wav"]), root / "cache" / entry["backend"] / f"{voice_id}.pt"
        assert reference.is_absolute() and reference.parent == root / "voices" / "reference"
        assert reference.is_file() is (reference.relative_to(root).as_posix() not in scenario.missing)
        assert pt.is_file() is (pt.relative_to(root).as_posix() not in scenario.missing)
        assert ("meta" in entry) is (voice_id not in scenario.unconfirmed)
    if scenario.session is not None:
        info = BACKEND_LANGUAGES[entries[scenario.session.voice_id]["backend"]]
        assert scenario.session.language in info and scenario.session.text
    if scenario.forge is not None:
        assert (root / "voices" / "reference" / scenario.forge.clip).is_file()


BACKEND_LANGUAGES = {MTL: {"ja", "it", "en"}, STD: {"en"}, TUR: {"en"}}


def test_speak_seeds_the_mockup_takes(make_fake) -> None:
    fake = make_fake()
    store = TakesStore(fake.data_dir / "outputs" / "takes" / "index.json", fake.data_dir)
    report = store.load()
    assert (report.count, report.next_id, report.notice) == (7, 8, "")
    takes = {take.id: take for take in store.takes}
    starred = sorted(take.id for take in store.takes if take.starred)
    total = sum(take.seconds for take in store.takes)
    assert f"{len(starred)} starred · {total:.1f} s of audio" == "3 starred · 27.8 s of audio"   # M L3194
    assert starred == [3, 4, 7] and store.check_files() == ()
    assert {take.id for take in store.takes if take.session == fake.session_id} == {4, 5, 6, 7}
    assert {take.session for take in store.takes if take.id <= 3} != {fake.session_id}
    assert [(t.voice, t.backend, t.language, t.exaggeration, t.cfg_weight, t.seconds, t.gen_seconds)
            for t in store.takes] == [
        ("aiko", MTL, "ja", 0.5, 0.5, 4.6, 6.9), ("aiko", MTL, "ja", 0.8, 0.5, 4.9, 7.2),
        ("aiko", MTL, "ja", 0.5, 0.3, 5.2, 7.0), ("marco", MTL, "it", 0.5, 0.5, 3.2, 4.1),
        ("friend", MTL, "en", 0.5, 0.5, 2.8, 4.0), ("narrator", STD, "en", 0.5, 0.4, 4.1, 5.9),
        ("host-turbo", TUR, "en", None, None, 3.0, 1.7)]
    assert takes[7].text == SCRIPT_JA and takes[7].wav == "outputs/takes/aiko_take7.wav"
    assert datetime.fromisoformat(takes[7].created).strftime("%H:%M") == "14:31"
    for take in store.takes:                                    # the peaks are the store's own measure
        samples, rate = read_wav_bytes(store.path_of(take.id).read_bytes())
        assert (rate, len(samples)) == (24000, round(take.seconds * 24000))
        assert list(take.peaks) == [round(100 * value) for value in peaks(samples, 230)]


def test_speak_seeds_the_mockup_voices(make_fake) -> None:
    fake = make_fake()
    snapshot = VoiceLibrary(fake.data_dir / "cache" / "registry.json").snapshot(fake.voice_details())
    rows = {row.voice_id: row for row in snapshot.rows}
    assert [row.voice_id for row in snapshot.rows] == ["aiko", "friend", "marco", "narrator", "host-turbo"]
    assert {voice: round(row.clip_s, 1) for voice, row in rows.items()} == {
        "aiko": 11.2, "marco": 9.8, "friend": 12.0, "narrator": 14.5, "host-turbo": 8.3}   # M L1216-1220
    assert [(rows[v].language_name, rows[v].reference_name) for v in ("aiko", "marco", "friend")] == [
        ("Japanese", "aiko_ref.wav"), ("Italian", "marco_ref.wav"), ("English", "sample.wav")]
    assert all(row.consent and row.pt_exists and row.backend_enabled for row in snapshot.rows)
    today = datetime.now().astimezone()
    for voice, days in {"aiko": 4, "marco": 3, "friend": 2, "narrator": 6, "host-turbo": 1}.items():
        assert created_text(rows[voice].created, now=today) == created_text(
            (today - timedelta(days=days)).isoformat(), now=today)


def test_speak_seeds_the_load_history(make_fake) -> None:
    fake = make_fake()
    history = LoadHistory(fake.data_dir / "cache" / "load_history.jsonl")
    history.load()
    assert [history.estimate(b, "cuda").about_s for b in (MTL, STD, TUR)] == [14.2, 12.1, 8.7]
    assert history.estimate(TUR, "cuda").peak_gb == 3.3 and history.last_loaded() == MTL
    texts = [line.split("  ", 1)[1] for line, _ in history_rows(history.events)]
    assert texts == [
        "Loaded chatterbox-turbo · 8.7 s · 3.1 GB", "Switching Turbo → Standard",       # M L1253-1260
        "Unloaded chatterbox-turbo · freed 3.1 GB", "Loaded chatterbox-standard · 12.1 s · 3.5 GB",
        "Switching Standard → Multilingual", "Unloaded chatterbox-standard · freed 3.5 GB",
        "Loaded chatterbox-multilingual · 14.2 s · 3.4 GB", "Closed · freed 3.4 GB",
        "Loading chatterbox-multilingual", "Loaded chatterbox-multilingual · 14.2 s · 3.4 GB"]
    assert history_rows(history.events)[0][0].endswith("19:20:41  Loaded chatterbox-turbo · 8.7 s · 3.1 GB")
    assert [tone for _, tone in history_rows(history.events)][-2:] == ["today", "today"]


@pytest.mark.parametrize(("name", "app", "other", "free"), [
    ("speak", "3.4", "2.5", "6.1"),                            # M L896-898
    ("lowvram", "3.4", "8.5", "0.1"),
    ("forge_low_vram", "3.4", "8.5", "0.1"),
    ("relaunch", "0.0", "2.5", "9.5"),
    ("firstrun", "0.0", "2.5", "9.5"),
])
def test_each_scenario_reads_its_vram(make_fake, name: str, app: str, other: str, free: str) -> None:
    fake = make_fake(name)
    before = fake.device_info()
    if fake.residency().resident:
        assert before.cuda_available is True                  # CUDA is initialised by the loaded model
    else:
        assert (before.cuda_available, before.torch_version) == (None, "")   # nothing checked yet
    reading = fake.device_info(probe=True)
    assert (reading.name, reading.total_bytes, reading.error) == ("NVIDIA GeForce RTX 4070 Ti", GPU_TOTAL_BYTES, None)
    assert (gb(reading.reserved_bytes), gb(reading.other_bytes), gb(reading.free_bytes)) == (app, other, free)


def test_the_failure_scenario_replays_to_the_failure_board(make_fake) -> None:
    fake = make_fake("failure")
    recorder = recorded(fake)
    assert gb(fake.device_info(probe=True).free_bytes) == "0.0"          # low VRAM: the switch needs an ask
    (step,) = fake.scenario.steps
    assert step == Step("switch", TUR, asked=True)
    with pytest.raises(BackendLoadError) as failed:
        fake.make_resident(step.backend)
    assert (failed.value.kind, failed.value.detail) == ("out_of_memory", "CUDA out of memory. Tried to allocate 512.00 MiB")
    assert recorder.stream() == [("load_started", TUR, None, None), ("unloaded", MTL, "evicted", None),
                                 ("load_failed", TUR, None, f"out_of_memory: {failed.value.detail}")]
    assert gb(recorder.seen[1][0].freed_bytes) == "3.4"
    assert gb(fake.device_info().free_bytes) == "2.1"          # "Turbo failed to load: CUDA out of memory (2.1 GB free)"
    assert state_of(fake.residency()) == (None, None, None, (), False)


STEPS = {
    "switch": ((), (), (Step("ask", TUR),)),
    "lowvram": ((), (), (Step("ask", TUR),)),
    "failure": ((), ("make_resident",), (Step("switch", TUR, asked=True),)),
    "loading": (("make_resident",), (), (Step("switch", TUR),)),
    "generating": (("speak",), (), (Step("generate"),)),
    "queued": (("speak",), (), (Step("generate", count=3),)),
    "freeing": (("unload_backend",), (), (Step("free"),)),
    "forging": (("load_voice",), (), (Step("forge"),)),
    "forged": ((), (), (Step("forge"),)),
    "forge_failed": ((), ("load_voice",), ()),
}


@pytest.mark.parametrize("name", sorted(SPEC_SCENARIOS))
def test_scenario_holds_failures_and_steps(name: str) -> None:
    scenario = SCENARIOS[name]
    holds, fails, steps = STEPS.get(name, ((), (), ()))
    assert (scenario.holds, tuple(method for method, _ in scenario.fails), scenario.steps) == (holds, fails, steps)


SESSIONS = {"aiko": ("ja", 7, 5, 7), "host-turbo": ("en", 7, 5, 7)}


@pytest.mark.parametrize("name", sorted(SPEC_SCENARIOS))
def test_scenario_pages_and_drafts(name: str) -> None:
    scenario = SCENARIOS[name]
    turbo_voice = name in ("switch", "lowvram", "failure", "other_backend", "loading")
    if name in ("firstrun", "relaunch"):
        assert (scenario.session, scenario.player_at, scenario.log) == (None, None, ())
    else:
        session = scenario.session
        assert session.voice_id == ("host-turbo" if turbo_voice else "aiko")
        assert (session.language, session.take_id, session.pin_a, session.pin_b) == SESSIONS[session.voice_id]
        assert scenario.player_at == 1.9 and len(scenario.log) == 9
    expected_route = {"takes": "/takes", "models": "/models", "settings": "/settings"}.get(name, "/")
    if name.startswith("forg"):
        expected_route = "/forge"
    elif name.startswith("voices"):
        expected_route = "/voices"
    assert scenario.route == expected_route
    assert (scenario.takes_tab, scenario.settings_tab) == (
        "all" if name == "takes" else "session", "effects" if name == "settings" else "general")


FORGE_TABLE = {                                                 # BUILD_SPEC §6.13, the forge seed table
    "forge": ("sample.wav", "rin", MTL, "ja", False, 2.5),
    "forge_ready": ("sample.wav", "rin", MTL, "ja", True, 2.5),
    "forge_other_backend": ("sample.wav", "rin", TUR, "en", True, 2.5),
    "forge_low_vram": ("sample.wav", "rin", TUR, "en", True, 8.5),
    "forge_replace": ("sample.wav", "aiko", MTL, "ja", True, 2.5),
    "forge_replace_other_backend": ("sample.wav", "host-turbo", TUR, "en", True, 2.5),
    "forge_taken_other": ("sample.wav", "aiko", TUR, "en", True, 2.5),
    "forge_turbo_short": ("short_hello.wav", "rin", TUR, "en", True, 2.5),
    "forging": ("sample.wav", "rin", MTL, "ja", True, 2.5),
    "forged": ("sample.wav", "rin", MTL, "ja", True, 2.5),
    "forge_failed": ("sample.wav", "rin", MTL, "ja", True, 2.5),
}


@pytest.mark.parametrize("name", sorted(FORGE_TABLE))
def test_forge_seeds_match_the_seed_table(name: str) -> None:
    scenario = SCENARIOS[name]
    draft = scenario.forge
    assert isinstance(draft, ForgeSeed) and draft.exaggeration == 0.5
    assert (draft.clip, draft.name, draft.backend, draft.language, draft.consent, scenario.other_gb) == FORGE_TABLE[name]
    assert (scenario.resident, scenario.forge_files, scenario.route) == (MTL, True, "/forge")


def test_only_the_forge_scenarios_carry_a_forge_draft() -> None:
    assert {name for name, scenario in SCENARIOS.items() if scenario.forge is not None} == set(FORGE_TABLE)
    assert {name for name, scenario in SCENARIOS.items() if scenario.forge_files} == set(FORGE_TABLE)


def test_forge_clips_probe_as_the_spec_says(make_fake) -> None:
    real_sample = ROOT / "voices" / "reference" / "sample.wav"
    before = real_sample.stat() if real_sample.exists() else None
    fake = make_fake("forge")
    folder = fake.config.VOICES_DIR
    sample = probe_clip(folder / "sample.wav")
    assert (round(sample.duration_s, 1), round(sample.selection_s, 1)) == (30.0, 13.4)   # not the mockup's 12.0
    short = probe_clip(folder / "short_hello.wav")
    assert (round(short.duration_s, 2), short.sample_rate, short.channels, short.subtype) == (4.2, 24000, 1, "PCM_16")
    assert f"{short.peak_dbfs:.1f}" == "-6.4" and not short.clipped and not short.silent
    history = LoadHistory(fake.data_dir / "cache" / "load_history.jsonl")
    history.load()
    assert history.bake_estimate(MTL) == 1.9                   # "forging takes about 2 s" (M L2675)
    if before is not None:
        after = real_sample.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_the_sample_stand_in_has_the_real_clips_selection(make_fake, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fake_module, "_REAL_SAMPLE", tmp_path / "gone" / "sample.wav")
    fake = make_fake("forge")
    sample = probe_clip(fake.config.VOICES_DIR / "sample.wav")
    assert (sample.sample_rate, sample.channels, round(sample.duration_s, 1)) == (22050, 1, 30.0)
    assert round(sample.selection_s, 2) == 13.42 and f"{sample.peak_dbfs:.1f}" == "-8.4"


def test_firstrun_is_todays_pc(make_fake) -> None:
    fake = make_fake("firstrun")
    root = fake.data_dir
    assert (root / "cache" / "registry.json").read_text(encoding="utf-8") == "{}"
    assert not (root / "cache" / "load_history.jsonl").exists() and fake.list_voices() == []
    assert sorted(path.name for path in (root / "voices" / "reference").iterdir()) == ["sample.wav"]
    assert fake.session_id is None and fake.residency().resident == ()


def test_relaunch_is_an_earlier_session(make_fake) -> None:
    fake = make_fake("relaunch")
    store = TakesStore(fake.data_dir / "outputs" / "takes" / "index.json", fake.data_dir)
    store.load()
    assert fake.session_id is None and len(store.takes) == 7 and store.takes[0].id == 7
    history = LoadHistory(fake.data_dir / "cache" / "load_history.jsonl")
    history.load()
    assert history.events[-1].kind == "closed" and fake.residency().resident == ()
    first = fake.make_resident(MTL)
    assert first.first_in_process is True and fake.loads == [MTL]


def test_missing_files_and_unconfirmed_voices(make_fake) -> None:
    missing = make_fake("voices_missing_files")
    (aiko,) = missing.voice_details("aiko")
    assert (aiko.reference_path, aiko.conds_exists, aiko.consent) == (None, False, True)
    job = Job()
    with pytest.raises(ValueError, match="Voice 'aiko' has no cached conditionals for chatterbox-multilingual"):
        missing.speak("Hello.", "aiko", job=job)
    (narrator,) = make_fake("voices_unconfirmed").voice_details("narrator")
    assert (narrator.consent, dict(narrator.meta), narrator.conds_exists) == (None, {}, True)


def test_the_cpu_scenario_runs_models_on_the_cpu(make_fake) -> None:
    fake = make_fake("cpu")
    assert (fake.residency().device, fake.residency().preferred_device) == ("cpu", "cuda")
    assert fake.device_info().cuda_available is None
    reading = fake.device_info(probe=True)
    assert (reading.cuda_available, reading.total_bytes, reading.error) == (False, None, None)
    report = fake.make_resident(TUR)
    assert (report.device, report.reserved_bytes, report.freed_bytes) == ("cpu", None, None)
    history = LoadHistory(fake.data_dir / "cache" / "load_history.jsonl")
    history.load()
    assert history.estimate(MTL, "cpu").about_s == 14.2 and history.events[-1].gb is None
    assert "14.2 s · CPU" in fake.scenario.log[2]


def test_set_preferred_device_applies_at_the_next_build(make_fake) -> None:
    fake = make_fake()
    fake.set_preferred_device("cpu")
    assert fake.residency().device == "cuda" and fake.make_resident(TUR).device == "cpu"
    fake.set_preferred_device("cuda")
    assert fake.make_resident(STD).device == "cuda"


def test_reseeding_replaces_an_earlier_seed_and_keeps_settings(tmp_path: Path) -> None:
    root = tmp_path / "dev"
    FakeEngine("speak", data_dir=root)
    (root / "outputs" / "takes" / "aiko_take8.wav").write_bytes(b"RIFF")
    (root / "cache" / "staging").mkdir()
    (root / "ui_settings.json").write_text("{}", encoding="utf-8")
    FakeEngine("firstrun", data_dir=root)
    assert not (root / "outputs" / "takes" / "aiko_take8.wav").exists()
    assert not (root / "outputs" / "takes" / "index.json").exists() and not (root / "cache" / "staging").exists()
    assert (root / "ui_settings.json").read_text(encoding="utf-8") == "{}"
    store = TakesStore(root / "outputs" / "takes" / "index.json", root)
    FakeEngine("speak", data_dir=root)
    assert store.load().next_id == 8


def test_seeded_audio_is_shared_and_a_damaged_copy_is_not_passed_on(make_fake) -> None:
    first, second = make_fake(), make_fake()
    take = Path("outputs") / "takes" / "aiko_take7.wav"
    original = (first.data_dir / take).read_bytes()
    assert (second.data_dir / take).read_bytes() == original
    if Path(fake_module.tempfile.gettempdir()).anchor.lower() == first.data_dir.anchor.lower():
        assert os.stat(first.data_dir / take).st_nlink >= 3      # one copy on disk: the cache and both roots
    (first.data_dir / take).write_bytes(b"RIFF" + bytes(40))      # a test that writes into a seeded file
    assert (make_fake().data_dir / take).read_bytes() == original


def test_a_shared_copy_with_every_link_taken_makes_way_for_a_fresh_one(make_fake,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """NTFS gives a file at most 1024 names; full runs kept by pytest reach that, and seeding must still link."""
    take = Path("outputs") / "takes" / "aiko_take7.wav"
    first = make_fake()
    if Path(fake_module.tempfile.gettempdir()).anchor.lower() != first.data_dir.anchor.lower():
        pytest.skip("the shared cache is on another volume, so the fake writes plain copies")
    original, old = (first.data_dir / take).read_bytes(), os.stat(first.data_dir / take)
    real_link, refused = os.link, []

    def full_once(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(dst).name == take.name and not refused:
            refused.append(Path(src))
            raise OSError(errno.EMLINK, "Too many links")
        real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", full_once)
    second = make_fake()
    fresh = os.stat(second.data_dir / take)
    assert refused and (second.data_dir / take).read_bytes() == original
    assert fresh.st_ino != old.st_ino and fresh.st_ino == os.stat(refused[0]).st_ino and fresh.st_nlink == 2
    assert os.stat(first.data_dir / take).st_nlink == old.st_nlink - 1    # the old copy lost only the cache's name
    assert (first.data_dir / take).read_bytes() == original


def test_the_fake_never_seeds_the_real_data(tmp_path: Path) -> None:
    for folder in (ROOT, ROOT / "cache" / "fake-run", ROOT / "voices" / "fake-run", ROOT / "outputs" / "x", ROOT.parent):
        with pytest.raises(ValueError, match="real data"):
            FakeEngine("speak", data_dir=folder)
    assert not (ROOT / "cache" / "fake-run").exists() and not (ROOT / "voices" / "fake-run").exists()
    theirs = tmp_path / "theirs"
    (theirs / "cache").mkdir(parents=True)
    (theirs / "cache" / "registry.json").write_text('{"x": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="did not seed"):
        FakeEngine("speak", data_dir=theirs)
    assert (theirs / "cache" / "registry.json").read_text(encoding="utf-8") == '{"x": {}}'


def test_custom_scenarios_and_engine_options(make_fake) -> None:
    crowded = make_fake(replace(SCENARIOS["speak"], name="crowded", other_gb=11.0))
    reading = crowded.device_info(probe=True)
    assert (gb(reading.reserved_bytes), reading.free_bytes) == ("3.4", 0)   # others never take this app's VRAM
    crowded.set_other_gb(1.0)
    assert gb(crowded.device_info().free_bytes) == "7.6"
    narrow = make_fake("speak", enabled=[MTL, TUR])
    assert narrow.list_backends() == [MTL, TUR]
    assert {d.voice_id: d.backend_enabled for d in narrow.voice_details()}["narrator"] is False
    with pytest.raises(ValueError, match="isn't enabled"):
        narrow.make_resident(STD)


def test_fields_of_the_session_seed_are_the_sessions() -> None:
    from ui.contract.state import Session

    assert [f.name for f in fields(fake_module.SessionSeed)] == [f.name for f in fields(Session)]
