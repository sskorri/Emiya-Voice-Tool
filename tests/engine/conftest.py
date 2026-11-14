"""Shared fixtures for the engine tests: a scratch config, fake backend factories, clips.

Nothing here loads a model, imports torch or chatterbox, or touches the real cache\\,
voices\\ or outputs\\. Every engine test runs with HF_HUB_OFFLINE=1 and fails at
teardown if it initialised CUDA for real (the CUDA-free rule of BUILD_SPEC §8.2).

fake_factories (for test_engine.py and the FakeEngine parity test):

    fakes = fake_factories                  # the fixture value, a FakeFactories
    fakes.calls["chatterbox-turbo"]         # factory invocations (failed ones included)
    fakes.builds                            # [(backend, device), ...] in call order
    fakes.installs                          # progress.install() calls (stubbed: no chatterbox import)
    gate = fakes.block("chatterbox-turbo")  # the next build waits: gate.wait_entered(); ...; gate.release()
    fakes.fail("chatterbox-turbo", RuntimeError("CUDA out of memory"))   # the next build raises a fresh copy
    fakes.model_ref(name)                   # weakref to the newest build's FakeModel (failed builds too)
    fakes.model(name), fakes.backend(name)  # the live objects, or None once the engine dropped them
    model.generate_error = ValueError("x")  # the next take raises a fresh copy inside generate()
    model.bake_error = RuntimeError("x")    # the next bake raises inside load_voice()
    model.generate_gate = fakes.gate()      # the next take waits at its first sampling step
    model.tokens = model.max_tokens + 1     # no EOS within the bound: hit_token_limit (== max_tokens is
                                            # an EOS on the last step, so the take is not cut off)

A fake take runs its sampling loop through the real progress hook, so a bound Job
sees tokens, phases and cancel points. The fixture holds only weak references to
models and backends, so it never keeps one alive behind the engine's back; every
gate is released at teardown.

make_clip(name, seconds, folder=..., sr=..., channels=..., subtype=..., pauses=...)
writes a synthetic speech-like clip (write_clip) and returns its path.

Test packages outside tests\\engine cannot see these fixtures; they import the plain
helpers instead: install_fake_factories(monkeypatch), make_scratch_config(root) and
write_clip(path, seconds, ...) from tests.engine.conftest.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import threading
import time
import weakref
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import numpy as np
import pytest
import soundfile as sf

from engine import progress
from engine.backends.base import save_atomic

BACKEND_NAMES = ("chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo")
GATE_TIMEOUT_S = 10.0
FAKE_SAMPLE_RATE = 24000

# The engine's progress hook around a pass-through "tqdm": inside a bound job each
# step updates Job.status() and is a cancel point, exactly as Chatterbox's loops are.
_sampling_loop = progress._wrap(lambda iterable, *args, **kwargs: iter(iterable), "sampling")


# ---------- autouse guards ----------

def _cuda_initialized() -> bool:
    """True when the real torch (not a test's fake module) has created a CUDA context."""
    torch = sys.modules.get("torch")
    if not getattr(torch, "__file__", None):
        return False
    try:
        return bool(torch.cuda.is_initialized())
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _engine_test_env() -> Iterable[None]:
    """HF_HUB_OFFLINE=1 for the test; fail if the test created a CUDA context for real."""
    before = _cuda_initialized()
    saved = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    yield
    if saved is None:
        os.environ.pop("HF_HUB_OFFLINE", None)
    else:
        os.environ["HF_HUB_OFFLINE"] = saved
    assert before or not _cuda_initialized(), "this test initialised CUDA (only monkeypatched torch.cuda is allowed)"


# ---------- scratch config ----------

def make_scratch_config(root: Path) -> SimpleNamespace:
    """A config namespace like config.py, rooted in `root`, CPU only, one model at a time."""
    config = SimpleNamespace(
        ROOT=root,
        CACHE_DIR=root / "cache",
        VOICES_DIR=root / "voices" / "reference",
        OUTPUTS_DIR=root / "outputs",
        DEVICE="cpu",
        DEFAULT_BACKEND="chatterbox-multilingual",
        DEFAULT_LANGUAGE="en",
        ENABLED_BACKENDS=list(BACKEND_NAMES),
        MAX_RESIDENT_BACKENDS=1,
        DEFAULT_EXAGGERATION=0.5,
        DEFAULT_CFG_WEIGHT=0.5,
    )
    for folder in (config.CACHE_DIR, config.VOICES_DIR, config.OUTPUTS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def scratch_config(tmp_path: Path) -> SimpleNamespace:
    """make_scratch_config(tmp_path)."""
    return make_scratch_config(tmp_path)


# ---------- clips ----------

def write_clip(path: Path, seconds: float, *, sr: int = FAKE_SAMPLE_RATE, channels: int = 1,
               subtype: str | None = None, fmt: str | None = None, level: float = 0.3,
               pauses: Iterable[tuple[float, float]] = ()) -> Path:
    """Write a speech-like tone of `seconds` with silent (start, end) pauses; returns `path`.

    The format follows the extension; `subtype` None is soundfile's default for it
    (PCM_16 for .wav and .flac, VORBIS for .ogg, MPEG_LAYER_III for .mp3).
    """
    n = int(round(seconds * sr))
    t = np.arange(n, dtype=np.float64) / sr
    wave = level * np.sin(2 * np.pi * 220.0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t))
    for start, end in pauses:
        wave[int(round(start * sr)):int(round(end * sr))] = 0.0
    data = np.repeat(wave[:, None], channels, axis=1) if channels > 1 else wave
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data.astype(np.float32), sr, subtype=subtype, format=fmt)
    return path


@pytest.fixture
def make_clip(tmp_path: Path):
    """Factory: make_clip(name, seconds, *, folder=tmp_path/"clips", **write_clip options) -> Path."""
    def make(name: str, seconds: float, *, folder: Path | None = None, **options: Any) -> Path:
        return write_clip(Path(folder or tmp_path / "clips") / name, seconds, **options)
    return make


# ---------- fake factories ----------

class Gate:
    """Holds a fake build or take until the test releases it (never longer than the timeout)."""

    def __init__(self, timeout: float = GATE_TIMEOUT_S) -> None:
        self.entered = threading.Event()
        self._released = threading.Event()
        self._timeout = timeout

    def release(self) -> None:
        self._released.set()

    def wait_entered(self, timeout: float = GATE_TIMEOUT_S) -> bool:
        """Wait until the fake reached the gate."""
        return self.entered.wait(timeout)

    def wait_here(self) -> None:
        """Called by the fake: signal arrival, then wait for release."""
        self.entered.set()
        if not self._released.wait(self._timeout):
            raise TimeoutError("the test never released this gate")


def wav_bytes(samples: np.ndarray, sr: int = FAKE_SAMPLE_RATE) -> bytes:
    """Encode like the backends do (torchaudio's soundfile backend): float32 WAV."""
    buffer = io.BytesIO()
    sf.write(buffer, np.asarray(samples, dtype=np.float32), sr, format="WAV", subtype="FLOAT")
    return buffer.getvalue()


class FakeModel:
    """Stands in for a Chatterbox TTS model: bakes JSON "conditionals", speaks a short tone."""

    sr = FAKE_SAMPLE_RATE

    def __init__(self, backend: str, device: str) -> None:
        self.backend = backend
        self.device = device
        self.tokens = 12                  # the sampling step that is the fake EOS (> max_tokens: cut off)
        self.max_tokens = 1000            # the sampling loop's bound, as in Chatterbox
        self.seconds = 0.5                # audio per take
        self.generate_calls: list[tuple[str, dict[str, Any]]] = []
        self.bakes: list[str] = []
        self.conds_loads: list[str] = []
        self.generate_error: BaseException | None = None
        self.bake_error: BaseException | None = None
        self.generate_gate: Gate | None = None

    def generate(self, text: str, **params: Any) -> np.ndarray:
        self.generate_calls.append((text, dict(params)))
        if self.generate_error is not None:
            template, self.generate_error = self.generate_error, None
            raise copy.copy(template)
        for step in _sampling_loop(range(self.max_tokens)):
            if step == 0 and self.generate_gate is not None:
                gate, self.generate_gate = self.generate_gate, None
                gate.wait_here()
            if step + 1 >= self.tokens:
                break                     # EOS
        t = np.arange(int(self.seconds * self.sr)) / self.sr
        return (0.1 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)

    def bake(self, reference_wav: Path) -> dict[str, Any]:
        self.bakes.append(str(reference_wav))
        if self.bake_error is not None:
            template, self.bake_error = self.bake_error, None
            raise copy.copy(template)
        return {"baked_from": str(reference_wav), "device": self.device}

    def load_conds(self, path: Path) -> None:
        json.loads(Path(path).read_text(encoding="utf-8"))
        self.conds_loads.append(Path(path).stem)


class FakeBackend:
    """The VoiceBackend interface the engine uses, backed by a FakeModel.

    Mirrors the real backends: one lock, `_active_voice`, `last_conds_load_s`, and a
    `<voice>.pt` per voice in cache_dir (JSON here), written atomically.
    """

    def __init__(self, name: str, cache_dir: Path, device: str, model: FakeModel) -> None:
        self.name = name
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.model: FakeModel | None = model
        self._active_voice: str | None = None
        self.last_conds_load_s: float | None = None
        self._lock = threading.Lock()

    def _conds_path(self, voice_id: str) -> Path:
        return self.cache_dir / f"{voice_id}.pt"

    def load_voice(self, voice_id: str, reference_wav: Path, exaggeration: float = 0.5,
                   force: bool = False, **_: Any) -> str:
        _validate_voice_id(voice_id)
        path = self._conds_path(voice_id)
        with self._lock:
            if path.exists() and not force:
                self.model.load_conds(path)
            else:
                self._active_voice = None
                conds = self.model.bake(Path(reference_wav))
                data = {"voice_id": voice_id, "exaggeration": exaggeration, **conds}
                save_atomic(lambda output: output.write_text(json.dumps(data), encoding="utf-8"), path)
            self._active_voice = voice_id
        return voice_id

    def speak(self, text: str, voice_id: str, **params: Any) -> bytes:
        from ui.core.fake import check_fake_script

        _validate_voice_id(voice_id)
        with self._lock:
            check_fake_script(text, self.name)
            if voice_id != self._active_voice:
                path = self._conds_path(voice_id)
                if not path.exists():
                    raise ValueError(
                        f"Voice '{voice_id}' has no cached conditionals for {self.name}. "
                        f"Call load_voice(backend='{self.name}') first."
                    )
                started = time.perf_counter()
                self.model.load_conds(path)
                self.last_conds_load_s = time.perf_counter() - started
                self._active_voice = voice_id
            else:
                self.last_conds_load_s = None
            samples = self.model.generate(text, **params)
        return wav_bytes(samples, self.model.sr)

    def list_voices(self) -> list[str]:
        return sorted(p.stem for p in self.cache_dir.glob("*.pt"))


class FakeFactories:
    """Counting replacements for engine._BACKEND_FACTORIES entries (see the module docstring)."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.builds: list[tuple[str, str]] = []
        self.installs = 0
        self._gates: dict[str, Gate] = {}
        self._errors: dict[str, BaseException] = {}
        self._models: dict[str, weakref.ref[FakeModel]] = {}
        self._backends: dict[str, weakref.ref[FakeBackend]] = {}
        self._all_gates: list[Gate] = []
        self._mx = threading.Lock()

    def factory(self, name: str):
        """The fake for one backend: same signature as engine._build_* (cache_dir, device)."""
        def build(cache_dir: Path, device: str) -> FakeBackend:
            with self._mx:
                self.calls[name] += 1
                self.builds.append((name, device))
                gate = self._gates.pop(name, None)
                error = self._errors.pop(name, None)
            model = FakeModel(name, device)
            self._models[name] = weakref.ref(model)
            if gate is not None:
                gate.wait_here()
            if error is not None:
                raise copy.copy(error)       # fresh each time: only its traceback holds `model`
            backend = FakeBackend(name, cache_dir, device, model)
            self._backends[name] = weakref.ref(backend)
            return backend
        return build

    def gate(self, timeout: float = GATE_TIMEOUT_S) -> Gate:
        """A new gate (for FakeModel.generate_gate), released at teardown at the latest."""
        gate = Gate(timeout)
        with self._mx:
            self._all_gates.append(gate)
        return gate

    def block(self, name: str, timeout: float = GATE_TIMEOUT_S) -> Gate:
        """The next build of `name` waits at the returned gate (after its model exists)."""
        gate = self.gate(timeout)
        with self._mx:
            self._gates[name] = gate
        return gate

    def fail(self, name: str, error: BaseException) -> None:
        """The next build of `name` raises a fresh copy of `error`."""
        with self._mx:
            self._errors[name] = error

    def model_ref(self, name: str) -> weakref.ref[FakeModel] | None:
        return self._models.get(name)

    def model(self, name: str) -> FakeModel | None:
        ref = self._models.get(name)
        return ref() if ref is not None else None

    def backend(self, name: str) -> FakeBackend | None:
        ref = self._backends.get(name)
        return ref() if ref is not None else None

    def record_install(self) -> None:
        self.installs += 1

    def release_all(self) -> None:
        for gate in self._all_gates:
            gate.release()


def install_fake_factories(monkeypatch: pytest.MonkeyPatch) -> FakeFactories:
    """Replace every engine._BACKEND_FACTORIES entry with a counting fake; stub progress.install().

    The fake_factories fixture uses this; test packages outside tests\\engine import it
    (with make_scratch_config and write_clip) from tests.engine.conftest. Call
    release_all() on the result at teardown.
    """
    from engine import engine as engine_module

    fakes = FakeFactories()
    for name in list(engine_module._BACKEND_FACTORIES):
        monkeypatch.setitem(engine_module._BACKEND_FACTORIES, name, fakes.factory(name))
    monkeypatch.setattr(progress, "install", fakes.record_install)
    if hasattr(engine_module, "install"):              # in case engine.py binds the name itself
        monkeypatch.setattr(engine_module, "install", fakes.record_install)
    return fakes


@pytest.fixture
def fake_factories(monkeypatch: pytest.MonkeyPatch) -> Iterable[FakeFactories]:
    """install_fake_factories(monkeypatch); every gate is released at teardown."""
    fakes = install_fake_factories(monkeypatch)
    yield fakes
    fakes.release_all()


def _validate_voice_id(voice_id: str) -> None:
    from engine.backends.base import validate_voice_id   # E2's rules, imported when used
    validate_voice_id(voice_id)


