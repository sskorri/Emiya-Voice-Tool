"""Real permission failures and a failed forge's conditioning/registry consistency.

All paths are temporary; fake factories exercise the real VoiceEngine transaction without
loading torch or a model. The Windows permission check changes only a fresh scratch folder's
ACL and removes its explicit deny rule in finally, including when an assertion fails.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from engine.engine import VoiceEngine
from engine.errors import GenerationError
from engine.registry import VoiceRegistry
from tests.engine.conftest import install_fake_factories, make_scratch_config, write_clip
from ui.core.fake import FakeEngine

MTL = "chatterbox-multilingual"


def _quiet_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=True,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


@contextlib.contextmanager
def _deny_writes(folder: Path):
    """Deny the current Windows user writes to one new folder, then restore its inherited ACL."""
    identity = _quiet_command("whoami", "/user", "/fo", "csv", "/nh").stdout
    sid = re.search(r"S-1-\d+(?:-\d+)+", identity)
    assert sid is not None, "whoami did not return the current user's SID"
    principal = "*" + sid.group()
    try:
        # Specific write rights: generic W also denies SYNCHRONIZE, which ordinary reads need.
        _quiet_command("icacls", str(folder), "/deny", principal + ":(OI)(CI)(WD,AD,WEA,WA)")
        yield
    finally:
        _quiet_command("icacls", str(folder), "/remove:d", principal)


@pytest.mark.skipif(os.name != "nt", reason="uses a real Windows directory ACL")
def test_a_real_read_only_registry_fails_promptly_and_recovers(tmp_path: Path) -> None:
    """A genuine denied lock-file creation is PermissionError, never a 10-second busy timeout."""
    folder = tmp_path / "denied"
    folder.mkdir()
    registry = VoiceRegistry(folder / "registry.json")
    registry.register("aiko", MTL, "original.wav", 0.5, "en")
    original = registry.path.read_bytes()
    with _deny_writes(folder):
        with pytest.raises(PermissionError):
            (folder / "write-probe").write_text("must fail", encoding="utf-8")
        started = time.perf_counter()
        with pytest.raises(PermissionError):
            registry.update("aiko", language="ja")
        assert time.perf_counter() - started < 2, "permission denial was treated as lock contention"
        assert registry.path.read_bytes() == original
        assert registry.get("aiko")["language"] == "en"
    registry.update("aiko", language="ja")
    assert registry.get("aiko")["language"] == "ja"
    assert sorted(p.name for p in folder.iterdir()) == ["registry.json"]


@pytest.mark.parametrize("simulated", [False, True], ids=["real-engine", "fake-engine"])
def test_a_bake_that_created_no_file_needs_no_rollback_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, simulated: bool,
) -> None:
    """A failed first bake on a read-only folder must not report a second, spurious rollback failure."""
    fakes = install_fake_factories(monkeypatch)
    engine = (FakeEngine("firstrun", data_dir=tmp_path / "fake", speed=0) if simulated else
              VoiceEngine(make_scratch_config(tmp_path)))
    clip = write_clip(tmp_path / "reference.wav", 6.0)
    path = engine.config.CACHE_DIR / MTL / "rin.pt"
    try:
        engine.make_resident(MTL)
        failure = PermissionError("the read-only folder refused the conditioning save")
        if simulated:
            engine.fail_next("load_voice", failure)
        else:
            fakes.model(MTL).bake_error = failure
        original_unlink = Path.unlink

        def cannot_remove_output(candidate: Path, *args, **kwargs) -> None:
            if candidate == path:
                raise PermissionError("the read-only folder also refuses unlink")
            original_unlink(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", cannot_remove_output)
        with pytest.raises(GenerationError, match="read-only folder refused"):
            engine.load_voice("rin", clip, backend=MTL)
        assert not path.exists() and engine.registry.get("rin") is None
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR or record.exc_info]
    finally:
        engine.close()
        fakes.release_all()


@pytest.mark.parametrize("replacement", [False, True], ids=["new-voice", "forced-replacement"])
@pytest.mark.parametrize("simulated", [False, True], ids=["real-engine", "fake-engine"])
def test_a_failed_registry_save_rolls_back_conditioning_and_retry_uses_its_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bool, simulated: bool,
) -> None:
    """A reported failed forge cannot relabel its .pt as a later, different reference clip."""
    fakes = install_fake_factories(monkeypatch)
    engine = (FakeEngine("firstrun", data_dir=tmp_path / "fake", speed=0) if simulated else
              VoiceEngine(make_scratch_config(tmp_path)))
    first = write_clip(tmp_path / "first.wav", 6.0)
    failed = write_clip(tmp_path / "failed.wav", 7.0)
    retry = write_clip(tmp_path / "retry.wav", 8.0)
    path = engine.config.CACHE_DIR / MTL / "aiko.pt"
    try:
        if replacement:
            engine.load_voice("aiko", first, backend=MTL, meta={"consent": True})
        old_entry = engine.registry.get("aiko")
        old_bytes = path.read_bytes() if path.exists() else None
        with monkeypatch.context() as failure:
            def cannot_write(data: dict, **kwargs) -> None:
                raise OSError("simulated registry disk failure after the conditioning save")

            failure.setattr(engine.registry, "_write", cannot_write)
            with pytest.raises(OSError, match="registry disk failure"):
                engine.load_voice("aiko", failed, backend=MTL, force=replacement)
        assert engine.registry.get("aiko") == old_entry
        assert (path.read_bytes() if path.exists() else None) == old_bytes
        assert list(path.parent.glob("*.rollback-*")) == []
        if replacement:
            assert (engine._models[MTL].active_voice if simulated else fakes.backend(MTL)._active_voice) is None
            engine.speak("The original voice still works.", "aiko")
            if not simulated:
                assert fakes.model(MTL).conds_loads[-1] == "aiko"
        engine.load_voice("aiko", retry, backend=MTL, force=replacement)
        assert json.loads(path.read_text(encoding="utf-8"))["baked_from"] == str(retry.resolve())
        assert engine.registry.get("aiko")["reference_wav"] == str(retry.resolve())
        if not simulated:
            assert fakes.model(MTL).bakes[-1] == str(retry.resolve())
    finally:
        engine.close()
        fakes.release_all()


@pytest.mark.parametrize("simulated", [False, True], ids=["real-engine", "fake-engine"])
def test_only_known_backend_and_clip_can_reuse_a_cached_conditioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, simulated: bool,
) -> None:
    fakes = install_fake_factories(monkeypatch)
    engine = (FakeEngine("firstrun", data_dir=tmp_path / "fake", speed=0) if simulated else
              VoiceEngine(make_scratch_config(tmp_path)))
    first = write_clip(tmp_path / "first.wav", 6.0)
    second = write_clip(tmp_path / "second.wav", 7.0)
    path = engine.config.CACHE_DIR / MTL / "rin.pt"
    try:
        engine.load_voice("rin", first, backend=MTL)
        # A pre-journal orphan, and then a previous backend's .pt, both lack source evidence.
        engine.registry.remove("rin")
        engine.load_voice("rin", second, backend=MTL)
        assert json.loads(path.read_text())["baked_from"] == str(second)
        engine.load_voice("rin", second, backend="chatterbox-standard")
        engine.load_voice("rin", first, backend=MTL)
        assert json.loads(path.read_text())["baked_from"] == str(first)
        # A legacy registration with no dates or meta still proves the source on a hit.
        data = engine.registry.entries()
        data["rin"].pop("forged_at")
        engine.registry._write(data)
        original = path.read_bytes()
        with pytest.warns(UserWarning, match="kept it and ignored"):
            engine.load_voice("rin", second, backend=MTL)
        assert path.read_bytes() == original and "forged_at" not in engine.registry.get("rin")
    finally:
        engine.close()
        fakes.release_all()


def test_second_engine_delete_waits_for_cache_hit_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = install_fake_factories(monkeypatch)
    config = make_scratch_config(tmp_path)
    writer, deleter = VoiceEngine(config), VoiceEngine(config)
    clip = write_clip(tmp_path / "voice.wav", 6.0)
    entered, release = threading.Event(), threading.Event()
    try:
        writer.load_voice("rin", clip, backend=MTL)
        load = fakes.backend(MTL).load_voice

        def held_load(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return load(*args, **kwargs)

        monkeypatch.setattr(fakes.backend(MTL), "load_voice", held_load)
        with ThreadPoolExecutor(2) as pool:
            hit = pool.submit(writer.load_voice, "rin", clip, backend=MTL)
            assert entered.wait(10)
            delete = pool.submit(deleter.delete_voice, "rin")
            try:
                time.sleep(.1)
                assert not delete.done()
            finally:
                release.set()
            assert hit.result(timeout=10) == "rin" and delete.result(timeout=10).removed_entry
        assert writer.registry.get("rin") is None and not (config.CACHE_DIR / MTL / "rin.pt").exists()
    finally:
        release.set()
        writer.close()
        deleter.close()
        fakes.release_all()


def test_casefold_conflict_appearing_during_bake_preserves_winning_entry(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = install_fake_factories(monkeypatch)
    config = make_scratch_config(tmp_path)
    engine = VoiceEngine(config)
    clip = write_clip(tmp_path / "voice.wav", 6.0)
    try:
        original = engine.registry.register

        def conflict(*args, **kwargs) -> None:
            other = VoiceRegistry(engine.registry.path)
            other.register("Rin", MTL, "existing.wav", 0.5, "en")
            (config.CACHE_DIR / MTL / "Rin.pt").write_bytes(b"existing")
            original(*args, **kwargs)

        monkeypatch.setattr(engine.registry, "register", conflict)
        with pytest.raises(ValueError, match="would share one file"):
            engine.load_voice("rin", clip, backend=MTL)
        assert engine.registry.list_ids() == ["Rin"]
        assert (config.CACHE_DIR / MTL / "Rin.pt").read_bytes() == b"existing"
    finally:
        engine.close()
        fakes.release_all()
