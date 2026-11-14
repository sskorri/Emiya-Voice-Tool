"""Run with the moved folder's runtime. Bake a synthetic reference and speak offline on each backend."""
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf
import torch

import config
from engine.engine import VoiceEngine


def run() -> dict:
    assert config.PORTABLE
    torch.set_num_threads(2)
    results = {}
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        local = SimpleNamespace(**{key: getattr(config, key) for key in dir(config) if key.isupper()})
        local.ROOT, local.VOICES_DIR = root, root / "voices" / "reference"
        local.CACHE_DIR, local.OUTPUTS_DIR, local.DEVICE = root / "cache", root / "outputs", "cpu"
        local.VOICES_DIR.mkdir(parents=True)
        clip = local.VOICES_DIR / "synthetic.wav"
        t = np.arange(7 * 24000) / 24000
        sf.write(clip, .3 * np.sin(2 * np.pi * t * 220) * (.6 + .4 * np.sin(2 * np.pi * t * 3)), 24000)
        engine = VoiceEngine(local)
        try:
            for name in local.ENABLED_BACKENDS:
                engine.load_backend(name)
                engine.load_voice("synthetic", clip, backend=name, force=True, require_resident=True)
                wav = engine.speak("Ready.", "synthetic", require_resident=True)
                info = sf.info(io.BytesIO(wav))
                assert info.frames > 0 and info.samplerate == 24000 and info.channels == 1
                assert engine.voice_info("synthetic")["reference_wav"] == "voices/reference/synthetic.wav"
                assert engine.unload_backend(name) and not engine.residency().resident
                results[name] = {"frames": info.frames, "bytes": len(wav), "sample_rate": info.samplerate,
                                 "channels": info.channels, "unloaded": True}
        finally:
            engine.close()
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
