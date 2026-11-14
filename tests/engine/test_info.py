"""engine.backends.info: the static backend table and weights_status() (BUILD_SPEC §3.6)."""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import socket
from pathlib import Path

import pytest

from engine import audio, progress
from engine.backends.info import BACKENDS, LANGUAGES, TURBO_TAGS, BackendInfo, weights_status

GIB = 2**30
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"


def test_languages_are_the_23_multilingual_codes() -> None:
    assert len(LANGUAGES) == 23
    assert list(LANGUAGES) == sorted(LANGUAGES)
    assert (LANGUAGES["en"], LANGUAGES["ja"], LANGUAGES["zh"], LANGUAGES["he"]) == (
        "English", "Japanese", "Chinese", "Hebrew")


def test_backends_in_config_order() -> None:
    assert list(BACKENDS) == [MTL, STD, TUR]
    for name, info in BACKENDS.items():
        assert isinstance(info, BackendInfo) and info.name == name
        assert (info.sample_rate, info.max_tokens, info.timbre_s) == (24000, 1000, 10.0)


def test_multilingual() -> None:
    info = BACKENDS[MTL]
    assert info.hf_repo == "ResembleAI/chatterbox"
    assert info.weight_files == ("ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt")
    assert dict(info.languages) == dict(LANGUAGES) and info.english_only is False
    assert (info.uses_exaggeration, info.uses_cfg, info.tags) == (True, True, ())
    assert (info.ref_min_exclusive_s, info.prompt_s) == (None, 6.0)


def test_standard() -> None:
    info = BACKENDS[STD]
    assert info.hf_repo == "ResembleAI/chatterbox"
    assert info.weight_files == ("ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors")
    assert dict(info.languages) == {"en": "English"} and info.english_only is True
    assert (info.uses_exaggeration, info.uses_cfg, info.tags) == (True, True, ())
    assert (info.ref_min_exclusive_s, info.prompt_s) == (None, 6.0)


def test_turbo() -> None:
    info = BACKENDS[TUR]
    assert info.hf_repo == "ResembleAI/chatterbox-turbo"
    assert info.weight_files == ("ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors")
    assert dict(info.languages) == {"en": "English"} and info.english_only is True
    assert (info.uses_exaggeration, info.uses_cfg) == (False, False)
    assert info.tags == TURBO_TAGS == ("[laugh]", "[chuckle]", "[cough]", "[sigh]", "[gasp]")
    assert info.ref_min_exclusive_s == audio.TURBO_MIN_EXCLUSIVE_S == 5.0
    assert info.prompt_s == 15.0


def test_backend_info_is_frozen_and_tables_are_not_shared() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        BACKENDS[TUR].max_tokens = 2000  # type: ignore[misc]
    assert BACKENDS[MTL].languages is not LANGUAGES
    assert BACKENDS[STD].languages is not BACKENDS[TUR].languages


# ---------- weights_status ----------

def _fake_snapshot(cache: Path, repo: str, files: dict[str, int], commit: str = "0123abcd") -> Path:
    repo_dir = cache / ("models--" + repo.replace("/", "--"))
    (repo_dir / "refs").mkdir(parents=True)
    (repo_dir / "refs" / "main").write_text(commit, encoding="utf-8")
    snapshot = repo_dir / "snapshots" / commit
    snapshot.mkdir(parents=True)
    for name, size in files.items():
        (snapshot / name).write_bytes(b"\0" * size)
    return snapshot


@pytest.fixture
def hf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from huggingface_hub import constants

    cache = tmp_path / "hf-cache"
    cache.mkdir()
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    return cache


def test_weights_status_with_every_file_cached(hf_cache: Path) -> None:
    _fake_snapshot(hf_cache, "ResembleAI/chatterbox-turbo",
                   {"ve.safetensors": 10, "t3_turbo_v1.safetensors": 20, "s3gen_meanflow.safetensors": 30,
                    "added_tokens.json": 5})
    assert weights_status(TUR) == (True, 60)


def test_weights_status_with_a_file_missing(hf_cache: Path) -> None:
    snapshot = _fake_snapshot(hf_cache, "ResembleAI/chatterbox",
                              {"ve.pt": 1, "t3_mtl23ls_v2.safetensors": 2, "s3gen.pt": 3})
    assert weights_status(MTL) == (True, 6)
    assert weights_status(STD) == (False, None)            # same repo, other files
    (snapshot / "s3gen.pt").unlink()
    assert weights_status(MTL) == (False, None)


def test_weights_status_with_nothing_cached(hf_cache: Path) -> None:
    assert weights_status(TUR) == (False, None)


def test_weights_status_with_a_cached_no_exist_marker(hf_cache: Path) -> None:
    _fake_snapshot(hf_cache, "ResembleAI/chatterbox-turbo",
                   {"ve.safetensors": 10, "s3gen_meanflow.safetensors": 30})
    marker = hf_cache / "models--ResembleAI--chatterbox-turbo" / ".no_exist" / "0123abcd"
    marker.mkdir(parents=True)
    (marker / "t3_turbo_v1.safetensors").write_bytes(b"")
    assert weights_status(TUR) == (False, None)


def test_weights_status_of_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unknown backend 'chatterbox-nano'"):
        weights_status("chatterbox-nano")


def test_weights_status_on_this_pc_reads_the_cache_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Hugging Face cache, read-only; any network connection fails the test."""
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("weights_status touched the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for name in BACKENDS:
        cached, total = weights_status(name)
        if cached:
            assert 2.5 * GIB < total < 3.5 * GIB, (name, total)
        else:
            assert total is None


def test_turbo_tags_are_tokens_of_the_cached_turbo_tokenizer() -> None:
    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(BACKENDS[TUR].hf_repo, "added_tokens.json")
    if not isinstance(path, str):
        pytest.skip("the Turbo tokenizer is not in the local Hugging Face cache")
    tokens = json.loads(Path(path).read_text(encoding="utf-8"))
    assert set(TURBO_TAGS) <= set(tokens)


# ---------- drift against the installed Chatterbox (imports it, loads no model) ----------

@pytest.mark.slow
def test_tables_match_the_installed_chatterbox() -> None:
    mtl = pytest.importorskip("chatterbox.mtl_tts")
    tts = importlib.import_module("chatterbox.tts")
    turbo = importlib.import_module("chatterbox.tts_turbo")
    t3 = importlib.import_module("chatterbox.models.t3.t3")
    from chatterbox.models.s3gen import S3GEN_SR
    from chatterbox.models.s3tokenizer import S3_SR, S3_TOKEN_RATE

    assert list(LANGUAGES.items()) == list(mtl.SUPPORTED_LANGUAGES.items())
    classes = {MTL: mtl.ChatterboxMultilingualTTS, STD: tts.ChatterboxTTS, TUR: turbo.ChatterboxTurboTTS}
    for name, cls in classes.items():
        info = BACKENDS[name]
        assert cls.ENC_COND_LEN / S3_SR == info.prompt_s, name
        assert cls.DEC_COND_LEN / S3GEN_SR == info.timbre_s, name
        assert S3GEN_SR == info.sample_rate
    assert (mtl.REPO_ID, tts.REPO_ID, turbo.REPO_ID) == tuple(BACKENDS[n].hf_repo for n in (MTL, STD, TUR))
    assert mtl.DEFAULT_MULTILINGUAL_T3_MODEL in BACKENDS[MTL].weight_files
    assert S3_TOKEN_RATE == progress.TOKENS_PER_SECOND
    assert inspect.signature(t3.T3.inference_turbo).parameters["max_gen_len"].default == BACKENDS[TUR].max_tokens
    for cls in (mtl.ChatterboxMultilingualTTS, tts.ChatterboxTTS):
        assert f"max_new_tokens={BACKENDS[STD].max_tokens}," in inspect.getsource(cls.generate)


@pytest.mark.slow
def test_weight_files_are_the_ones_chatterbox_loads() -> None:
    mtl = pytest.importorskip("chatterbox.mtl_tts")
    tts = importlib.import_module("chatterbox.tts")
    turbo = importlib.import_module("chatterbox.tts_turbo")
    sources = {MTL: inspect.getsource(mtl.ChatterboxMultilingualTTS.from_local),
               STD: inspect.getsource(tts.ChatterboxTTS.from_local),
               TUR: inspect.getsource(turbo.ChatterboxTurboTTS.from_local)}
    for name, source in sources.items():
        for filename in BACKENDS[name].weight_files:
            if filename != mtl.DEFAULT_MULTILINGUAL_T3_MODEL:
                assert f'"{filename}"' in source, (name, filename)
