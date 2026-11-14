"""Windowless self-check reached through 'Emiya Voice.exe --portable-check'."""
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


def check(root):
    import nicegui
    import torch
    import torchaudio
    import webview
    from huggingface_hub import constants

    build = json.loads((root / "portable-build.json").read_text(encoding="utf-8"))
    if sys.flags.isolated != 1:
        raise RuntimeError("The bundled interpreter did not isolate its module search path")
    for path in [*sys.path, sys.executable, sys.base_prefix, tempfile.gettempdir(),
                 constants.HF_HUB_CACHE, os.environ["NUMBA_CACHE_DIR"], os.environ["PKUSEG_HOME"],
                 os.environ["LIBROSA_DATA_DIR"]]:
        if not Path(path).resolve().is_relative_to(root):
            raise RuntimeError(f"Unexpected external path: {path}")
    import chatterbox.tts
    import chatterbox.tts_turbo
    import chatterbox.mtl_tts
    from engine.engine import VoiceEngine
    from engine.backends.info import BACKENDS, weights_status
    from ui.kit.sound_out import WindowsAudioOut
    from ui.launch import import_theme
    from ui import entry

    modules = [nicegui, torch, torchaudio, webview, chatterbox.tts, chatterbox.tts_turbo, chatterbox.mtl_tts]
    if not all(Path(m.__file__).resolve().is_relative_to(root) for m in modules):
        raise RuntimeError("Imported a module outside the portable folder")
    theme = entry.load_theme_manifest(root / "ui" / "themes" / entry.ACTIVE_THEME)
    if isinstance(theme, int):
        raise RuntimeError("Theme manifest failed to load")
    import_theme(theme)
    if build["model_revisions"]:
        if not constants.HF_HUB_OFFLINE:
            raise RuntimeError("The bundled snapshots are configured for network lookup")
        for backend in BACKENDS:
            if not weights_status(backend)[0]:
                raise RuntimeError(f"Missing portable model files: {backend}")
    return {"python": sys.version.split()[0], "torch": torch.__version__, "cuda_build": torch.version.cuda,
            "nicegui": nicegui.__version__, "models": build["model_revisions"],
            "private_runtime": sys.flags.isolated == 1, "engine": VoiceEngine.__name__,
            "audio_adapter": WindowsAudioOut.__name__, "offline": constants.HF_HUB_OFFLINE}


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent
    (folder / "logs").mkdir(exist_ok=True)
    try:
        report = {"passed": True, **check(folder)}
    except Exception:
        report = {"passed": False, "error": traceback.format_exc()}
    (folder / "logs" / "portable-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    raise SystemExit(0 if report["passed"] else 1)
