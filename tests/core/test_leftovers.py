"""The startup sweep of temp files a crash mid-write left in the data root (an R2 finding)."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ui.core.services.leftovers import TEMP_AGE_S, sweep_temp_files

from tests.core.test_facade import MTL, TUR, Rig, rigs  # noqa: F401  (fixtures)

OLD = time.time() - TEMP_AGE_S - 60


def leftover(path: Path, *, old: bool = True, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if old:
        os.utime(path, (OLD, OLD))
    return path


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def test_only_old_temp_files_of_the_known_writers_go(tmp_path: Path) -> None:
    root, cache = tmp_path, tmp_path / "cache"
    gone = [
        leftover(root / ".ui_settings.json.4242.0a1b2c3d.tmp"),                 # settings.write_atomic
        leftover(cache / ".load_history.jsonl.4242.deadbeef.tmp"),               # the history's compaction
        leftover(cache / f"registry.json.tmp-4242-{'f' * 32}"),                   # VoiceRegistry._write
        leftover(cache / MTL / "aiko.pt.tmp-4242", data=b"x" * 4096),            # save_atomic
        leftover(cache / TUR / "host-turbo.pt.tmp-17"),
        leftover(cache / MTL / f"aiko.pt.stage-4242-{'a' * 32}"),
    ]
    kept = [
        leftover(root / ".ui_settings.json.4242.0a1b2c3d.tmp".replace("4242", "4243"), old=False),  # being written
        leftover(cache / f"registry.json.tmp-4242-{'e' * 32}", old=False),
        leftover(cache / MTL / f"aiko.pt.stage-4242-{'b' * 32}", old=False),
        leftover(cache / MTL / f"aiko.pt.pending-{'d' * 32}"),                    # belongs to journal recovery
        leftover(root / "ui_settings.json"),                                       # the real files
        leftover(cache / "registry.json"),
        leftover(cache / MTL / "aiko.pt"),
        leftover(cache / "registry.json.corrupt-1794582450"),                      # a quarantined copy: the user's
        leftover(root / "notes.tmp"),                                              # not a writer's name
        leftover(root / "outputs" / "takes" / ".ui_settings.json.1.0a1b2c3d.tmp"), # not a swept folder
    ]
    assert sweep_temp_files(root, cache, (MTL, TUR, "chatterbox-standard")) == len(gone)
    assert [path for path in gone if path.exists()] == []
    assert all(path.exists() for path in kept)
    assert sweep_temp_files(root, cache, (MTL,)) == 0                              # nothing left to do


async def test_core_start_sweeps_the_data_root(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    stale = leftover(rig.paths.cache / MTL / "aiko.pt.tmp-99")
    fresh = leftover(rig.paths.cache / MTL / "marco.pt.tmp-98", old=False)
    await rig.start()
    assert not stale.exists() and fresh.exists()
    assert (rig.paths.cache / MTL / "aiko.pt").exists()
