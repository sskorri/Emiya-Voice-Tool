"""Cross-file transactions through normal engine entry points and real process termination."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from engine.registry import VoiceRegistry
from tests.reliability._conditioning_child import MTL, TUR
from ui.core.services.leftovers import sweep_temp_files


def kill_at(root: Path, mode: str, prior: str, boundary: str, waiting=None) -> None:
    ready = root / "ready"
    ready.unlink(missing_ok=True)
    executable = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    env = dict(os.environ, HF_HUB_OFFLINE="1")
    if os.path.normcase(executable) != os.path.normcase(sys.executable):
        env["__PYVENV_LAUNCHER__"] = sys.executable
    cmd = [executable, "-m", "tests.reliability._conditioning_child", str(root), mode, prior, boundary]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
        try:
            timeout = time.monotonic() + 45
            while not ready.exists() and process.poll() is None and time.monotonic() < timeout:
                time.sleep(.015)
            if process.poll() is not None:
                pytest.fail(f"Worker stopped before {boundary}: {process.stdout.read()}")
            assert ready.exists(), f"Worker did not reach {boundary}"
            assert int(ready.read_text(encoding="ascii")) == process.pid, "must terminate the actual writer"
            if waiting:
                waiting()
        finally:
            process.kill()
            process.wait(timeout=15)


@pytest.mark.parametrize("mode", ["real", "fake"])
@pytest.mark.parametrize("prior,boundary", [
    ("absent", "pre-marker"), ("absent", "marker"), ("absent", "staged"),
    ("absent", "conditioning"), ("absent", "registry"), ("same", "partial-backup"),
    ("same", "conditioning"), ("same", "registry"), ("switch", "conditioning"), ("switch", "registry"),
])
def test_process_killed_during_forge(tmp_path: Path, mode: str, prior: str, boundary: str) -> None:
    kill_at(tmp_path, mode, prior, boundary)
    cache = tmp_path / "cache"
    registry = VoiceRegistry(cache / "registry.json")
    old = str(tmp_path / "old.wav")
    new = str(tmp_path / "new.wav")
    committed = boundary == "registry"
    entry = registry.get("rin")
    target = cache / MTL / "rin.pt"
    assert registry.get("other")["meta"] == {"keep": True}
    if committed or prior != "absent":
        backend = MTL if committed or prior == "same" else TUR
        expected = new if committed else old
        assert (entry["backend"], entry["reference_wav"]) == (backend, expected)
        assert entry["meta"] == ({"new": True} if committed else {"original": True})
        path = cache / backend / "rin.pt"
        assert json.loads(path.read_text(encoding="utf-8"))["baked_from"] == expected
    else:
        assert entry is None
    assert target.is_file() == (committed or prior == "same")
    if prior == "switch":
        assert json.loads((cache / TUR / "rin.pt").read_text(encoding="utf-8"))["baked_from"] == old
    # The only debris before recording intent is private stage data; the startup sweep owns it.
    sweep_temp_files(tmp_path, cache, (MTL, TUR), older_than_s=0)
    assert not [p for p in cache.rglob("*") if any(
        suffix in p.name for suffix in (".conditioning", ".stage-", ".pending-", ".rollback-"))]


def test_second_kill_during_undo_is_idempotent(tmp_path: Path) -> None:
    kill_at(tmp_path, "real", "same", "conditioning")
    kill_at(tmp_path, "recover", "same", "restored")
    cache = tmp_path / "cache"
    reopened = VoiceRegistry(cache / "registry.json")
    old = str(tmp_path / "old.wav")
    assert reopened.get("rin")["reference_wav"] == old
    assert json.loads((cache / MTL / "rin.pt").read_text(encoding="utf-8"))["baked_from"] == old
    assert not list(cache.rglob("*.rollback-*")) and not list(cache.glob("*.conditioning"))


@pytest.mark.parametrize("boundary,clip", [("conditioning", "old.wav"), ("registry", "new.wav")])
def test_metadata_writer_waits_then_recovers_before_edit(tmp_path: Path, boundary: str, clip: str) -> None:
    registry = VoiceRegistry(tmp_path / "cache" / "registry.json")
    with ThreadPoolExecutor(1) as pool:
        writes = []

        def edit() -> None:
            writes.append(pool.submit(registry.update, "rin", language="ja", meta={"edited": True}))
            time.sleep(.1)
            assert not writes[0].done()

        kill_at(tmp_path, "real", "same", boundary, waiting=edit)
        updated = writes[0].result(timeout=10)
    assert updated["reference_wav"] == str(tmp_path / clip)
    assert updated["language"] == "ja" and updated["meta"]["edited"] is True
    assert registry.get("other")["meta"] == {"keep": True}
    assert json.loads((registry.path.parent / MTL / "rin.pt").read_text())["baked_from"] == str(tmp_path / clip)


@pytest.mark.parametrize("change", [{"voice": "../outside"}, {"voice": "NUL"}, {"backend": "../cache"},
                                     {"tx": "../escape"}, {"pid": "123/../abc"}, {"version": 42}])
def test_damaged_marker_leaves_files_intact(tmp_path: Path, change: dict) -> None:
    cache = tmp_path / "cache"
    folder = cache / MTL
    folder.mkdir(parents=True)
    registry = cache / "registry.json"
    registry.write_bytes(b'{"keep": 1}')
    pt = folder / "rin.pt"
    pt.write_bytes(b"old")
    record = dict(version=1, voice="rin", backend=MTL, tx="a" * 32, had_old=True, pid=123)
    marker = cache / "registry.json.conditioning"
    marker.write_text(json.dumps({**record, **change}), encoding="utf-8")
    with pytest.raises(ValueError):
        VoiceRegistry(registry)
    assert marker.is_file() and pt.read_bytes() == b"old" and registry.read_bytes() == b'{"keep": 1}'
