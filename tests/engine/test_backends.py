"""The three Chatterbox backends against a fake model (no torch, no chatterbox).

Covers the atomic .pt save, the no-active-voice window during a bake, the conditioning
load timing (last_conds_load_s), the Standard Guidance-0 clamp and the Turbo clip rules.
The tests marked slow swap in chatterbox's real Conditionals and torchaudio.
"""
import importlib
import io
import json
import logging
import os
import subprocess
import sys
import time
import types
import warnings
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from engine.backends.base import ChatterboxBackend, VoiceBackend, save_atomic
from engine.backends.info import BACKENDS as INFO
from engine.backends.multilingual import ChatterboxMultilingualBackend
from engine.backends.standard import MIN_CFG_WEIGHT, ChatterboxStandardBackend
from engine.backends.turbo import ChatterboxTurboBackend
from engine.errors import ScriptTooLong

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = (ChatterboxMultilingualBackend, ChatterboxStandardBackend, ChatterboxTurboBackend)
CHATTERBOX_MODULES = {
    "chatterbox-multilingual": "chatterbox.mtl_tts",
    "chatterbox-standard": "chatterbox.tts",
    "chatterbox-turbo": "chatterbox.tts_turbo",
}
TTS_CLASSES = {
    "chatterbox-multilingual": "ChatterboxMultilingualTTS",
    "chatterbox-standard": "ChatterboxTTS",
    "chatterbox-turbo": "ChatterboxTurboTTS",
}


class FakeConds:
    """Stands in for chatterbox's Conditionals: its label is what save() writes."""

    def __init__(self, label: str, fail_save: bool = False) -> None:
        self.label = label
        self.fail_save = fail_save
        self.saved_to: list[Path] = []

    def save(self, fpath: Path) -> None:
        self.saved_to.append(Path(fpath))
        if self.fail_save:
            Path(fpath).write_text(self.label[:3], encoding="utf-8")  # torn, like a quit mid-save
            raise OSError("disk full")
        Path(fpath).write_text(self.label, encoding="utf-8")


class FakeModel:
    """Records prepare_conditionals() and generate(); generate() says whose conds it used."""

    sr = 24000

    def __init__(self) -> None:
        self.tokenizer = types.SimpleNamespace(encode=lambda text, **kwargs: list(text), model_max_length=1024)
        self.t3 = types.SimpleNamespace(hp=types.SimpleNamespace(max_text_tokens=2048))
        self.conds: FakeConds | None = None
        self.prepared: list[tuple[str, dict]] = []
        self.generated: list[tuple[str, dict]] = []
        self.fail_save = False
        self.prepare_error: BaseException | None = None

    def prepare_conditionals(self, wav_fpath: str, **kwargs) -> None:
        self.prepared.append((wav_fpath, kwargs))
        self.conds = FakeConds(f"baked:{Path(wav_fpath).name}", fail_save=self.fail_save)
        if self.prepare_error is not None:
            raise self.prepare_error  # after conds changed: the worst case for _active_voice

    def generate(self, text: str, **kwargs) -> str:
        self.generated.append((text, kwargs))
        return f"{self.conds.label}|{text}"


def faked(cls: type[ChatterboxBackend]) -> type[ChatterboxBackend]:
    """cls with its chatterbox/torchaudio hooks replaced by fakes; everything else is real."""

    class Faked(cls):  # type: ignore[valid-type, misc]
        load_delay_s = 0.0

        def _from_pretrained(self, device: str) -> FakeModel:
            return FakeModel()

        def _load_conds(self, path: Path) -> FakeConds:
            time.sleep(self.load_delay_s)
            return FakeConds(Path(path).read_text(encoding="utf-8"))

        def _text_tokens(self, text: str, language_id: str | None) -> int:
            return len(text)

        def _wav_bytes(self, wav: str) -> bytes:
            return wav.encode("utf-8")

    Faked.__name__ = Faked.__qualname__ = f"Fake{cls.__name__}"
    return Faked


def write_clip(path: Path, seconds: float, samplerate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(round(seconds * samplerate), dtype="float32"), samplerate)
    return path


def pt_text(backend: ChatterboxBackend, voice_id: str) -> str:
    return (backend.cache_dir / f"{voice_id}.pt").read_text(encoding="utf-8")


def temp_files(backend: ChatterboxBackend) -> list[str]:
    return sorted(p.name for p in backend.cache_dir.iterdir() if ".tmp-" in p.name)


def spoken(backend: ChatterboxBackend, voice_id: str, **params) -> str:
    """Which conditionals the take used, e.g. "baked:aiko.wav|hello"."""
    return backend.speak("hello", voice_id, **params).decode("utf-8")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def clips(tmp_path: Path) -> dict[str, Path]:
    """Real 6 s WAVs: long enough for Turbo, whose pre-check reads the duration."""
    folder = tmp_path / "voices"
    return {name: write_clip(folder / f"{name}.wav", 6.0) for name in ("aiko", "aiko_new", "rin")}


def make(cls: type[ChatterboxBackend], tmp_path: Path) -> ChatterboxBackend:
    return faked(cls)(tmp_path / "cache" / cls.name, device="cpu")


@pytest.fixture(params=BACKENDS, ids=lambda cls: cls.name)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> ChatterboxBackend:
    return make(request.param, tmp_path)


# ---------- the real classes and their model hooks ----------


@pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.name)
def test_backend_classes_are_concrete(cls) -> None:
    assert issubclass(cls, VoiceBackend)
    assert cls.__abstractmethods__ == frozenset()


def stub_libraries(monkeypatch: pytest.MonkeyPatch, backend_name: str) -> types.SimpleNamespace:
    """Put stand-ins for chatterbox's module and torchaudio into sys.modules for one test."""
    calls = types.SimpleNamespace(built=[], loaded=[], encoded=[])

    class StubTTS:
        @classmethod
        def from_pretrained(cls, device: str) -> FakeModel:
            calls.built.append(device)
            return FakeModel()

    class StubConditionals:
        @classmethod
        def load(cls, fpath: Path, map_location: str = "cpu") -> FakeConds:
            calls.loaded.append((Path(fpath), map_location))
            return FakeConds(Path(fpath).read_text(encoding="utf-8"))

    def save(buf: io.BytesIO, wav: str, sample_rate: int, format: str) -> None:
        calls.encoded.append((wav, sample_rate, format))
        buf.write(b"RIFF")

    module_name = CHATTERBOX_MODULES[backend_name]
    module = types.ModuleType(module_name)
    setattr(module, TTS_CLASSES[backend_name], StubTTS)
    module.Conditionals = StubConditionals
    module.punc_norm = lambda text: text
    torchaudio = types.ModuleType("torchaudio")
    torchaudio.save = save
    monkeypatch.setitem(sys.modules, "chatterbox", types.ModuleType("chatterbox"))
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    return calls


@pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.name)
def test_model_hooks_use_the_backends_own_chatterbox_classes(
    cls, tmp_path, clips, monkeypatch
) -> None:
    calls = stub_libraries(monkeypatch, cls.name)
    baker = cls(tmp_path / "cache" / cls.name, device="cpu")
    assert calls.built == ["cpu"]
    assert baker.cache_dir.is_dir()
    baker.load_voice("aiko", clips["aiko"])

    fresh = cls(baker.cache_dir, device="cpu")
    assert fresh.speak("hello", "aiko") == b"RIFF"
    assert calls.loaded == [(baker.cache_dir / "aiko.pt", "cpu")]
    assert calls.encoded == [("baked:aiko.wav|hello", 24000, "wav")]


# ---------- bake and cache (all three backends) ----------


def test_text_limit_precedes_conditioning_and_allows_the_boundary(backend, clips) -> None:
    limit = INFO[backend.name].max_text_tokens
    with pytest.raises(ScriptTooLong) as exc:
        backend.speak("x" * (limit + 1), "unbaked")
    assert (exc.value.count, exc.value.limit) == (limit + 1, limit)
    assert not backend.model.generated and backend.model.conds is None
    backend.load_voice("aiko", clips["aiko"])
    backend.speak("x" * limit, "aiko")
    assert len(backend.model.generated) == 1


def test_bake_saves_through_a_pid_temp_file(backend, clips) -> None:
    assert backend.load_voice("aiko", clips["aiko"]) == "aiko"
    assert pt_text(backend, "aiko") == "baked:aiko.wav"
    assert backend.model.conds.saved_to == [backend.cache_dir / f"aiko.pt.tmp-{os.getpid()}"]
    assert temp_files(backend) == []
    assert backend.model.prepared[0][0] == str(clips["aiko"])
    assert backend._active_voice == "aiko"


def test_cache_hit_loads_the_pt_without_baking(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    fresh = type(backend)(backend.cache_dir, device="cpu")
    fresh.load_voice("aiko", clips["aiko_new"])  # another clip, no force: the .pt wins
    assert fresh.model.prepared == []
    assert fresh.model.conds.label == "baked:aiko.wav"
    assert fresh._active_voice == "aiko"


def test_force_rebakes_over_the_cached_pt(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    backend.load_voice("aiko", clips["aiko_new"], force=True)
    assert pt_text(backend, "aiko") == "baked:aiko_new.wav"
    assert spoken(backend, "aiko") == "baked:aiko_new.wav|hello"


def test_failed_save_keeps_the_old_pt_and_leaves_no_voice_active(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    backend.load_voice("rin", clips["rin"])
    backend.model.fail_save = True
    with pytest.raises(OSError, match="disk full"):
        backend.load_voice("aiko", clips["aiko_new"], force=True)
    assert pt_text(backend, "aiko") == "baked:aiko.wav"  # the old .pt is intact
    assert temp_files(backend) == []
    assert backend._active_voice is None
    # model.conds now holds aiko_new's conditionals; neither voice may speak with them.
    assert spoken(backend, "rin") == "baked:rin.wav|hello"
    assert spoken(backend, "aiko") == "baked:aiko.wav|hello"


def test_failed_first_bake_leaves_no_files(backend, clips) -> None:
    backend.model.fail_save = True
    with pytest.raises(OSError):
        backend.load_voice("aiko", clips["aiko"])
    assert list(backend.cache_dir.iterdir()) == []
    assert backend.list_voices() == []
    assert backend._active_voice is None


def test_failed_prepare_leaves_no_voice_active(backend, clips) -> None:
    backend.load_voice("rin", clips["rin"])
    backend.model.prepare_error = RuntimeError("bad clip")
    with pytest.raises(RuntimeError, match="bad clip"):
        backend.load_voice("aiko", clips["aiko"])
    assert backend._active_voice is None
    assert not (backend.cache_dir / "aiko.pt").exists()
    assert spoken(backend, "rin") == "baked:rin.wav|hello"


def test_bake_retries_a_replace_blocked_by_a_reader(backend, clips, monkeypatch) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def blocked_three_times(src, dst) -> None:
        calls.append(Path(dst).name)
        if len(calls) <= 3:
            raise PermissionError("the file is open in another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", blocked_three_times)
    backend.load_voice("aiko", clips["aiko"])
    assert calls == ["aiko.pt"] * 4
    assert pt_text(backend, "aiko") == "baked:aiko.wav"
    assert temp_files(backend) == []


def test_invalid_ids_are_refused_before_the_model_runs(backend, clips) -> None:
    for bad in ("nul", "COM1", "lpt0", "../x", "rin\n", "a b", "x" * 65):
        with pytest.raises(ValueError, match="Invalid voice_id"):
            backend.load_voice(bad, clips["aiko"])
        with pytest.raises(ValueError, match="Invalid voice_id"):
            backend.speak("hello", bad)
    assert backend.model.prepared == []
    assert backend.model.generated == []
    assert list(backend.cache_dir.iterdir()) == []


def test_list_voices_ignores_temp_files(backend, clips) -> None:
    backend.load_voice("rin", clips["rin"])
    backend.load_voice("aiko", clips["aiko"])
    (backend.cache_dir / "mio.pt.tmp-4242").write_text("torn", encoding="utf-8")
    assert backend.list_voices() == ["aiko", "rin"]


# ---------- conditioning load timing ----------


def test_voice_change_times_the_load_and_a_repeat_reports_none(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    backend.load_voice("rin", clips["rin"])
    backend.load_delay_s = 0.02

    assert spoken(backend, "aiko") == "baked:aiko.wav|hello"
    first = backend.last_conds_load_s
    assert isinstance(first, float)
    assert 0.015 <= first < 5.0

    assert spoken(backend, "aiko") == "baked:aiko.wav|hello"
    assert backend.last_conds_load_s is None

    assert spoken(backend, "rin") == "baked:rin.wav|hello"
    assert isinstance(backend.last_conds_load_s, float)


def test_take_right_after_a_bake_loads_nothing(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    spoken(backend, "aiko")
    assert backend.last_conds_load_s is None


def test_take_without_cached_conditionals_fails_cleanly(backend, clips) -> None:
    backend.load_voice("aiko", clips["aiko"])
    backend.load_voice("rin", clips["rin"])
    spoken(backend, "aiko")
    assert isinstance(backend.last_conds_load_s, float)
    with pytest.raises(ValueError, match="Voice 'ghost' has no cached conditionals"):
        backend.speak("hello", "ghost")
    assert backend.last_conds_load_s is None  # never a stale value from the previous take
    assert backend._active_voice == "aiko"
    assert [text for text, _ in backend.model.generated] == ["hello"]


def test_missing_conditionals_messages_are_unchanged(tmp_path) -> None:
    mtl = make(ChatterboxMultilingualBackend, tmp_path)
    with pytest.raises(ValueError) as caught:
        mtl.speak("hello", "ghost")
    assert str(caught.value) == "Voice 'ghost' has no cached conditionals. Call load_voice() first."
    for cls in (ChatterboxStandardBackend, ChatterboxTurboBackend):
        with pytest.raises(ValueError) as caught:
            make(cls, tmp_path).speak("hello", "ghost")
        assert str(caught.value) == (
            f"Voice 'ghost' has no cached conditionals for {cls.name}. "
            f"Call load_voice(backend='{cls.name}') first."
        )


def test_interface_default_for_last_conds_load_s() -> None:
    class Minimal(VoiceBackend):
        name = "minimal"

        def load_voice(self, voice_id, reference_wav, **params):
            return voice_id

        def speak(self, text, voice_id, **params):
            return b""

        def list_voices(self):
            return []

    assert Minimal().last_conds_load_s is None


# ---------- per-backend knobs ----------


def test_multilingual_passes_language_guidance_and_expressiveness(tmp_path, clips) -> None:
    mtl = make(ChatterboxMultilingualBackend, tmp_path)
    mtl.load_voice("aiko", clips["aiko"], exaggeration=0.8)
    assert mtl.model.prepared == [(str(clips["aiko"]), {"exaggeration": 0.8})]
    mtl.speak("hola", "aiko", language_id="es", cfg_weight=0.0, exaggeration=0.7,
              audio_prompt_path="ignored.wav")
    assert mtl.model.generated[-1] == (
        "hola", {"language_id": "es", "cfg_weight": 0.0, "exaggeration": 0.7}
    )


@pytest.mark.parametrize(
    ("given", "sent"),
    [(0.0, 0.001), (0, 0.001), (-0.5, 0.001), (0.0005, 0.001), (0.001, 0.001), (0.3, 0.3),
     (1, 1.0)],
)
def test_standard_guidance_zero_is_clamped(tmp_path, clips, given, sent) -> None:
    std = make(ChatterboxStandardBackend, tmp_path)
    std.load_voice("aiko", clips["aiko"])
    std.speak("hi", "aiko", cfg_weight=given, exaggeration=0.7, language_id="ja")
    text, kwargs = std.model.generated[-1]
    assert text == "hi"
    assert kwargs == {"cfg_weight": sent, "exaggeration": 0.7}
    assert type(kwargs["cfg_weight"]) is float


def test_standard_defaults(tmp_path, clips) -> None:
    assert MIN_CFG_WEIGHT == 1e-3
    std = make(ChatterboxStandardBackend, tmp_path)
    std.load_voice("aiko", clips["aiko"])
    assert std.model.prepared == [(str(clips["aiko"]), {"exaggeration": 0.5})]
    std.speak("hi", "aiko")
    assert std.model.generated[-1] == ("hi", {"cfg_weight": 0.5, "exaggeration": 0.5})


@pytest.mark.parametrize("seconds", [4.2, 5.0])
def test_turbo_refuses_a_short_clip_before_anything_changes(tmp_path, clips, seconds) -> None:
    turbo = make(ChatterboxTurboBackend, tmp_path)
    turbo.load_voice("rin", clips["rin"])
    short = write_clip(tmp_path / "voices" / "short.wav", seconds)
    message = (
        f"chatterbox-turbo needs a reference clip longer than 5s (got {seconds:.1f}s). "
        "Use a longer clip or another backend."
    )
    with pytest.raises(ValueError) as caught:
        turbo.load_voice("aiko", short)
    assert str(caught.value) == message
    assert [path for path, _ in turbo.model.prepared] == [str(clips["rin"])]
    assert turbo._active_voice == "rin"
    assert not (turbo.cache_dir / "aiko.pt").exists()


def test_turbo_accepts_a_clip_over_five_seconds(tmp_path) -> None:
    turbo = make(ChatterboxTurboBackend, tmp_path)
    clip = write_clip(tmp_path / "voices" / "ok.wav", 5.5)
    turbo.load_voice("aiko", clip)
    assert turbo.model.prepared == [(str(clip), {})]  # exaggeration is never passed
    assert pt_text(turbo, "aiko") == "baked:ok.wav"


def test_turbo_leaves_an_unreadable_clip_to_the_model(tmp_path) -> None:
    turbo = make(ChatterboxTurboBackend, tmp_path)
    clip = tmp_path / "voices" / "not_audio.wav"
    clip.parent.mkdir(parents=True)
    clip.write_text("not a wav", encoding="utf-8")
    turbo.load_voice("aiko", clip)
    assert turbo.model.prepared == [(str(clip), {})]


def test_turbo_maps_the_model_assertion_to_value_error(tmp_path, clips) -> None:
    turbo = make(ChatterboxTurboBackend, tmp_path)
    turbo.load_voice("rin", clips["rin"])
    turbo.model.prepare_error = AssertionError("Audio prompt must be longer than 5 seconds!")
    with pytest.raises(ValueError, match="chatterbox-turbo rejected the reference clip: Audio"):
        turbo.load_voice("aiko", clips["aiko"])
    assert turbo._active_voice is None
    assert spoken(turbo, "rin") == "baked:rin.wav|hello"


def test_turbo_warns_about_exaggeration_only_when_set(tmp_path, clips) -> None:
    turbo = make(ChatterboxTurboBackend, tmp_path)
    with pytest.warns(UserWarning, match="exaggeration has no documented effect"):
        turbo.load_voice("aiko", clips["aiko"], exaggeration=0.7)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        turbo.load_voice("rin", clips["rin"], exaggeration=0.5)


def test_turbo_generates_from_text_alone(tmp_path, clips, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="engine")
    turbo = make(ChatterboxTurboBackend, tmp_path)
    turbo.load_voice("aiko", clips["aiko"])
    turbo.speak("Okay [chuckle] it works.", "aiko", cfg_weight=None, language_id="en",
                exaggeration=0.9)
    turbo.speak("Again.", "aiko", cfg_weight=0.5)
    assert turbo.model.generated == [("Okay [chuckle] it works.", {}), ("Again.", {})]
    assert [r.getMessage() for r in caplog.records if r.name == "engine"] == []
    turbo.speak("Third.", "aiko", cfg_weight=0.3)
    assert any("cfg_weight has no effect on chatterbox-turbo voices" in r.getMessage()
               for r in caplog.records if r.name == "engine")


# ---------- save_atomic ----------


def test_save_atomic_replaces_the_file(tmp_path) -> None:
    target = tmp_path / "aiko.pt"
    target.write_text("old", encoding="utf-8")
    save_atomic(lambda path: path.write_text("new", encoding="utf-8"), target)
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["aiko.pt"]


class Interrupted(BaseException):
    """A BaseException, like the KeyboardInterrupt a Ctrl+C raises mid-save."""


def test_save_atomic_failure_keeps_the_old_content(tmp_path) -> None:
    target = tmp_path / "aiko.pt"
    target.write_text("old", encoding="utf-8")

    def torn(path: Path) -> None:
        path.write_text("ne", encoding="utf-8")
        raise Interrupted

    with pytest.raises(Interrupted):
        save_atomic(torn, target)
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["aiko.pt"]


def test_save_atomic_gives_up_after_five_retries(tmp_path, monkeypatch) -> None:
    target = tmp_path / "aiko.pt"
    target.write_text("old", encoding="utf-8")
    calls: list[float] = []

    def always_blocked(src, dst) -> None:
        calls.append(time.perf_counter())
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", always_blocked)
    with pytest.raises(PermissionError):
        save_atomic(lambda path: path.write_text("new", encoding="utf-8"), target)
    assert len(calls) == 6  # the first try and five retries
    assert all(b - a >= 0.04 for a, b in zip(calls, calls[1:]))  # 50 ms apart
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["aiko.pt"]


# ---------- imports ----------


def test_backend_modules_import_without_torch_or_chatterbox() -> None:
    code = (
        "import json, sys\n"
        "import engine.registry, engine.backends.base, engine.backends.multilingual\n"
        "import engine.backends.standard, engine.backends.turbo\n"
        "heavy = ('torch', 'torchaudio', 'chatterbox', 'librosa', 'transformers')\n"
        "print(json.dumps([name for name in heavy if name in sys.modules]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


# ---------- slow: chatterbox's real Conditionals and torchaudio, still no model ----------


@pytest.mark.slow
@pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.name)
def test_real_tokenizer_counts_match_the_upstream_cpu_inputs(cls) -> None:
    """Use just the local tokenizer files, testing the 2048/1024 boundary without building a model."""
    from huggingface_hub import try_to_load_from_cache
    from chatterbox.models.t3.modules.t3_config import T3Config
    from chatterbox.models.tokenizers import EnTokenizer, MTLTokenizer
    from transformers import AutoTokenizer

    info = INFO[cls.name]
    turbo, multilingual = cls is ChatterboxTurboBackend, cls is ChatterboxMultilingualBackend
    filenames = {ChatterboxTurboBackend: "tokenizer_config.json",
                 ChatterboxMultilingualBackend: "grapheme_mtl_merged_expanded_v1.json",
                 ChatterboxStandardBackend: "tokenizer.json"}
    filename = filenames[cls]
    path = try_to_load_from_cache(info.hf_repo, filename)
    if not isinstance(path, str):
        pytest.skip(f"local tokenizer files required: {info.hf_repo}/{filename}")
    if turbo:
        tokenizer = AutoTokenizer.from_pretrained(Path(path).parent, local_files_only=True)
        tokenizer.pad_token = tokenizer.pad_token or tokenizer.eos_token
    else:
        tokenizer = (MTLTokenizer if multilingual else EnTokenizer)(path)
    model = types.SimpleNamespace(tokenizer=tokenizer, t3=types.SimpleNamespace(hp=T3Config()))
    backend = object.__new__(cls)
    backend.model = model
    upstream = importlib.import_module(CHATTERBOX_MODULES[cls.name])
    assert backend._text_limit() == info.max_text_tokens
    for text, language in [("  we're …  here — [chuckle]", "en"), ("日本語で話します。カタカナも！", "ja")]:
        normalized = upstream.punc_norm(text)
        if turbo:
            ids = tokenizer(normalized, return_tensors="pt", padding=True, truncation=False).input_ids
        elif multilingual:
            ids = tokenizer.text_to_tokens(normalized, language_id=language)
        else:
            ids = tokenizer.text_to_tokens(normalized)
        assert ids.device.type == "cpu"
        assert backend._text_tokens(text, language.upper()) == ids.shape[-1]

    if turbo:
        # Each whitespace-separated 'x' and the final punctuation are single GPT-2 IDs.
        text = ("x " * (info.max_text_tokens - 1)).rstrip() + "!"
        one_more = "x " + text
    else:
        # Each additional '!' is one ID in the Standard and Multilingual vocabularies.
        text = "a!"
        count = backend._text_tokens(text, "en")
        text += "!" * (info.max_text_tokens - count)
        one_more = text + "!"
    assert backend._text_tokens(text, "en") == info.max_text_tokens
    backend._check_text(text, "en")
    with pytest.raises(ScriptTooLong) as exc:
        backend._check_text(one_more, "en")
    assert exc.value.count == info.max_text_tokens + 1


@pytest.mark.slow
@pytest.mark.parametrize("cls", BACKENDS, ids=lambda cls: cls.name)
def test_real_conditionals_round_trip_through_the_atomic_save(cls, tmp_path, clips) -> None:
    import torch

    module = importlib.import_module(CHATTERBOX_MODULES[cls.name])
    speaker = torch.arange(256, dtype=torch.float32).reshape(1, 256)
    prompt = torch.arange(8).reshape(1, 8)

    class RealCondsModel(FakeModel):
        def prepare_conditionals(self, wav_fpath: str, **kwargs) -> None:
            self.prepared.append((wav_fpath, kwargs))
            t3 = module.T3Cond(speaker_emb=speaker.clone(), emotion_adv=0.5 * torch.ones(1, 1, 1))
            self.conds = module.Conditionals(t3, {"prompt_token": prompt.clone()})

        def generate(self, text: str, **kwargs) -> "torch.Tensor":
            self.generated.append((text, kwargs))
            return torch.zeros(1, 2400)

    class RealHooks(cls):  # only the model build is faked: _load_conds and _wav_bytes are real
        def _from_pretrained(self, device: str) -> RealCondsModel:
            return RealCondsModel()

    baker = RealHooks(tmp_path / "cache" / cls.name, device="cpu")
    baker.load_voice("aiko", clips["aiko"])
    assert temp_files(baker) == []

    speaker_backend = RealHooks(baker.cache_dir, device="cpu")
    wav = speaker_backend.speak("hello", "aiko")
    conds = speaker_backend.model.conds
    assert isinstance(conds, module.Conditionals)
    assert torch.equal(conds.t3.speaker_emb, speaker)
    assert torch.equal(conds.gen["prompt_token"], prompt)
    assert isinstance(speaker_backend.last_conds_load_s, float)

    info = sf.info(io.BytesIO(wav))
    assert (info.samplerate, info.channels, info.frames) == (24000, 1, 2400)
    assert info.subtype == "FLOAT"
    assert not torch.cuda.is_initialized()
