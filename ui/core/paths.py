"""Where a run keeps its files (§4.2): the data root versus the install folder.

The data root holds the user's data (registry, voice cache, clips, takes, load history, settings): the
install folder itself for a real run, `<install>\\.dev` for --fake, --data-root for scratch runs, or a
tmp_path in tests. Locks and logs always live in the install folder, and the engine lock is always the
real one, so a scratch run can never hold a model next to the real GUI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEV_ROOT", "Paths"]

DEV_ROOT = ".dev"                       # the --fake data root inside the install folder


@dataclass(frozen=True)
class Paths:
    """Every location the core reads or writes, for one run."""

    app_root: Path        # the install folder: locks, logs, static
    root: Path            # the data root
    cache: Path           # root\cache
    voices: Path          # root\voices\reference
    takes_default: Path   # root\outputs\takes
    takes_index: Path     # root\outputs\takes\index.json (fixed; does not follow Output Folder)
    history: Path         # root\cache\load_history.jsonl
    staging: Path         # root\cache\staging
    settings: Path        # root\ui_settings.json
    logs: Path            # app_root\logs
    gui_lock: Path        # app_root\cache\gui.lock (gui-dev.lock with --fake)
    engine_lock: Path     # app_root\cache\engine.lock, always the real one

    @classmethod
    def for_run(cls, app_root: Path, *, data_root: Path | None, fake: bool) -> Paths:
        """The paths of one run; data_root wins over the --fake default. Creates nothing."""
        app = _absolute(app_root)
        root = _absolute(data_root) if data_root is not None else (app / DEV_ROOT if fake else app)
        cache = root / "cache"
        takes = root / "outputs" / "takes"
        app_cache = app / "cache"
        return cls(
            app_root=app,
            root=root,
            cache=cache,
            voices=root / "voices" / "reference",
            takes_default=takes,
            takes_index=takes / "index.json",
            history=cache / "load_history.jsonl",
            staging=cache / "staging",
            settings=root / "ui_settings.json",
            logs=app / "logs",
            gui_lock=app_cache / ("gui-dev.lock" if fake else "gui.lock"),
            engine_lock=app_cache / "engine.lock",
        )


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))
