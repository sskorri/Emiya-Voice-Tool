"""A real writer or recovery process, stopped at a filesystem boundary by its parent."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from engine import storage
from engine.registry import VoiceRegistry

MTL = "chatterbox-multilingual"
TUR = "chatterbox-turbo"


def main() -> None:
    root, mode, prior, stop = sys.argv[1:]
    root = Path(root)
    if mode == "recover":
        registry = root / "cache" / "registry.json"
    else:
        import pytest
        from engine.engine import VoiceEngine
        from tests.engine.conftest import install_fake_factories, make_scratch_config, write_clip
        from ui.core.fake import FakeEngine

        install_fake_factories(pytest.MonkeyPatch())
        engine = (FakeEngine("firstrun", data_dir=root, speed=0) if mode == "fake"
                  else VoiceEngine(make_scratch_config(root)))
        registry = engine.registry.path
        old = write_clip(root / "old.wav", 6.0)
        clip = write_clip(root / "new.wav", 7.0)
        engine.registry.register("other", MTL, str(old), 0.5, "en", meta={"keep": True})
        if prior != "absent":
            engine.load_voice("rin", old, backend=MTL if prior == "same" else TUR,
                              meta={"original": True})

    def reached() -> None:
        marker = root / "ready.tmp"
        marker.write_text(str(os.getpid()), encoding="ascii")
        marker.replace(root / "ready")
        time.sleep(90)
        raise RuntimeError("Parent failed to terminate writer")

    move = storage._replace
    target = registry.parent / MTL / "rin.pt"

    def watched_move(src: Path, dst: Path) -> None:
        if stop == "pre-marker" and src.name.endswith(".conditioning-preparing"):
            reached()
        move(src, dst)
        if ((stop == "marker" and dst.name.endswith(".conditioning")) or
                (stop == "staged" and ".stage-" in src.name) or
                (stop == "conditioning" and dst == target) or
                (stop == "registry" and dst == registry) or
                (stop == "restored" and ".rollback-" in src.name and dst == target)):
            reached()

    storage._replace = watched_move
    if stop == "partial-backup":
        def no_links(*args) -> None:
            raise OSError("filesystem does not support hard links")

        def partial(src, dst) -> None:
            Path(dst).write_bytes(b"partial")
            reached()

        Path.hardlink_to = no_links
        storage.shutil.copy2 = partial
    if mode == "recover":
        VoiceRegistry(registry)
    else:
        engine.load_voice("rin", clip, backend=MTL, force=True, meta={"new": True})
    raise RuntimeError(f"Missed conditioning transaction boundary {stop}")


if __name__ == "__main__":
    os.environ["HF_HUB_OFFLINE"] = "1"
    main()
