"""Static facts about the three Chatterbox backends, readable without torch or chatterbox.

The Models cards, the Forge marks and the engine's validation read these tables, so
none of them has to import a model package. Drift tests compare them with the
installed Chatterbox (tests/engine/test_info.py, marked slow).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

__all__ = ["BACKENDS", "LANGUAGES", "TURBO_TAGS", "BackendInfo", "weights_status"]

# A copy of chatterbox.mtl_tts.SUPPORTED_LANGUAGES (CB/mtl_tts.py L31-55): code -> English name.
LANGUAGES: Mapping[str, str] = {
    "ar": "Arabic",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "sw": "Swahili",
    "tr": "Turkish",
    "zh": "Chinese",
}

TURBO_TAGS = ("[laugh]", "[chuckle]", "[cough]", "[sigh]", "[gasp]")   # M L1212


@dataclass(frozen=True)
class BackendInfo:
    """What one backend supports and which weight files it loads."""

    name: str
    hf_repo: str
    weight_files: tuple[str, ...]
    languages: Mapping[str, str]   # LANGUAGES for Multilingual; {"en": "English"} otherwise
    english_only: bool
    uses_exaggeration: bool        # False for Turbo (TUB L15-19; CB/tts_turbo.py L159)
    uses_cfg: bool                 # False for Turbo
    tags: tuple[str, ...]          # inline tags the UI offers (Turbo only)
    ref_min_exclusive_s: float | None   # the reference clip must be longer than this (Turbo: 5.0)
    prompt_s: float                # seconds of the clip that set the T3 prompt (ENC_COND_LEN)
    timbre_s: float                # seconds of the clip that set the timbre (DEC_COND_LEN)
    sample_rate: int               # output rate (S3GEN_SR)
    max_tokens: int                # speech tokens per take (1000, about 40 s of audio)
    max_text_tokens: int           # input tokens before the model adds any start/stop padding


BACKENDS: Mapping[str, BackendInfo] = {
    "chatterbox-multilingual": BackendInfo(
        name="chatterbox-multilingual",
        hf_repo="ResembleAI/chatterbox",                                  # CB/mtl_tts.py L21
        weight_files=("ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt"),  # CB/mtl_tts.py L198-215
        languages=dict(LANGUAGES),
        english_only=False,
        uses_exaggeration=True,
        uses_cfg=True,
        tags=(),
        ref_min_exclusive_s=None,
        prompt_s=6.0,
        timbre_s=10.0,
        sample_rate=24000,
        max_tokens=1000,
        max_text_tokens=2048,
    ),
    "chatterbox-standard": BackendInfo(
        name="chatterbox-standard",
        hf_repo="ResembleAI/chatterbox",                                  # CB/tts.py L19
        weight_files=("ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors"),  # CB/tts.py L138-155
        languages={"en": "English"},
        english_only=True,
        uses_exaggeration=True,
        uses_cfg=True,
        tags=(),
        ref_min_exclusive_s=None,
        prompt_s=6.0,
        timbre_s=10.0,
        sample_rate=24000,
        max_tokens=1000,
        max_text_tokens=2048,
    ),
    "chatterbox-turbo": BackendInfo(
        name="chatterbox-turbo",
        hf_repo="ResembleAI/chatterbox-turbo",                            # CB/tts_turbo.py L26
        weight_files=("ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors"),  # L145-175
        languages={"en": "English"},
        english_only=True,
        uses_exaggeration=False,
        uses_cfg=False,
        tags=TURBO_TAGS,
        ref_min_exclusive_s=5.0,                                          # = engine.audio.TURBO_MIN_EXCLUSIVE_S
        prompt_s=15.0,
        timbre_s=10.0,
        sample_rate=24000,
        max_tokens=1000,
        max_text_tokens=1024,
    ),
}


def weights_status(name: str) -> tuple[bool, int | None]:
    """(all weight files in the local Hugging Face cache?, their total bytes or None).

    Uses huggingface_hub.try_to_load_from_cache, which only reads the cache folder and
    never touches the network. The total is None unless every file is cached.
    """
    info = BACKENDS.get(name)
    if info is None:
        raise ValueError(f"Unknown backend '{name}'. Known: {list(BACKENDS)}")
    from huggingface_hub import try_to_load_from_cache

    total = 0
    for filename in info.weight_files:
        path = try_to_load_from_cache(info.hf_repo, filename)
        if not isinstance(path, str) or not os.path.isfile(path):
            return False, None
        total += os.path.getsize(path)
    return True, total
