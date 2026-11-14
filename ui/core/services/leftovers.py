"""Leftover temp files from a crash mid-write (§4.15): found and deleted at startup (R2 finding).

Every writer of the data root writes a unique temp file next to its target and replaces the target with it, so a
reader never sees a torn file. A process killed in the middle leaves that temp file behind, and nothing else ever
deletes it: the settings store's .ui_settings.json.<pid>.<hex>.tmp (and the load history's), the registry's
registry.json.tmp-<pid>-<hex>, and a voice's <voice>.pt.tmp-<pid>, several MB each. The takes store sweeps its own
(TakesStore.load). sweep_temp_files() removes exactly these names, and only when they are older than a few
minutes, so a cli.py or app.py writing right now is never disturbed. Runs in a worker.
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = ["TEMP_AGE_S", "TEMP_NAMES", "sweep_temp_files"]

TEMP_AGE_S = 10 * 60             # a writer finishes in seconds; anything older is a crash's leftover
TEMP_NAMES = (
    re.compile(r"\.[^\\/]+\.\d+\.[0-9a-f]{8}\.tmp"),          # settings.write_atomic: .ui_settings.json.<pid>.<hex>.tmp
    re.compile(r"registry\.json\.tmp-\d+-[0-9a-f]{32}"),      # VoiceRegistry._write
    re.compile(r"[^\\/]+\.pt\.tmp-\d+"),                     # engine.backends.base.save_atomic
    re.compile(r"[A-Za-z0-9_-]+\.pt\.stage-\d+-[0-9a-f]{32}"),  # a bake killed before publication
)


def sweep_temp_files(root: Path, cache: Path, backends: Iterable[str], *,
                     older_than_s: float = TEMP_AGE_S) -> int:
    """Delete the leftover temp files in root (the settings), cache (registry and load history) and each
    cache/<backend> folder (the .pt files) that are older than older_than_s; returns how many went."""
    cutoff = time.time() - older_than_s
    folders = [Path(root), Path(cache), *(Path(cache) / backend for backend in backends)]
    removed = 0
    for folder in dict.fromkeys(os.path.normcase(os.path.abspath(f)) for f in folders):
        removed += _sweep(Path(folder), cutoff)
    return removed


def _sweep(folder: Path, cutoff: float) -> int:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if not any(pattern.fullmatch(entry.name) for pattern in TEMP_NAMES):
            continue
        with contextlib.suppress(OSError):
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
    return removed
