from pathlib import Path

ROOT = Path(__file__).parent
PORTABLE = (ROOT / "portable-build.json").is_file()
CACHE_DIR = ROOT / "cache"
VOICES_DIR = ROOT / "voices" / "reference"
OUTPUTS_DIR = ROOT / "outputs"

DEVICE = "cuda"
DEFAULT_BACKEND = "chatterbox-multilingual"
DEFAULT_LANGUAGE = "en"

# Backends the app may use. These are *selectable*, not preloaded: the engine
# loads a backend's model into VRAM only when first used and keeps at most
# MAX_RESIDENT_BACKENDS resident (unloading the least-recently-used one to
# free VRAM). Listing all three takes no extra VRAM.
ENABLED_BACKENDS = [
    "chatterbox-multilingual",
    "chatterbox-standard",
    "chatterbox-turbo",
]

# How many backend models may be resident in VRAM at once. 1 = strict
# single-resident for the 8 GB gaming target: switching backends frees the
# previous model first. Raise only if you've confirmed the combination fits.
MAX_RESIDENT_BACKENDS = 1

DEFAULT_EXAGGERATION = 0.5
DEFAULT_CFG_WEIGHT = 0.5
