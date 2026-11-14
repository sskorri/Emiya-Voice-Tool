"""chatterbox-turbo: Chatterbox-Turbo (350M, English only) behind the VoiceBackend interface.

Turbo ignores Expressiveness and Guidance and reads the paralinguistic tags inline; its reference clip must be
longer than 5 s (ChatterboxTurboTTS.prepare_conditionals asserts it), which _check_clip reports up front.
"""
import logging
import warnings
from pathlib import Path
from typing import Any

import soundfile as sf

from .base import ChatterboxBackend, validate_voice_id
from .info import BACKENDS

# ChatterboxTurboTTS.prepare_conditionals asserts the clip is longer than 5 s (tts_turbo.py L245).
MIN_REFERENCE_EXCLUSIVE_S = 5.0

log = logging.getLogger("engine")


class ChatterboxTurboBackend(ChatterboxBackend):
    """Chatterbox-Turbo (350M, English-only). Lower latency/VRAM than the
    other two, adds inline paralinguistic tags ([laugh], [chuckle], [cough],
    [sigh], [gasp]) directly in the text. Ignores exaggeration/cfg_weight —
    documented behavior, not a bug here.
    """

    name = "chatterbox-turbo"

    def _from_pretrained(self, device: str) -> Any:
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        return ChatterboxTurboTTS.from_pretrained(device=device)

    def _load_conds(self, path: Path) -> Any:
        from chatterbox.tts_turbo import Conditionals

        return Conditionals.load(path, map_location=self.device)

    def _text_tokens(self, text: str, language_id: str | None) -> int:
        from chatterbox.tts_turbo import punc_norm

        # Keep generate()'s special tokens, but count the tail it would silently discard.
        return len(self.model.tokenizer.encode(punc_norm(text), truncation=False, verbose=False))

    def _text_limit(self) -> int:
        return min(BACKENDS[self.name].max_text_tokens, self.model.tokenizer.model_max_length)

    def load_voice(
        self,
        voice_id: str,
        reference_wav: Path,
        exaggeration: float = 0.5,
        force: bool = False,
        **params: Any,
    ) -> str:
        validate_voice_id(voice_id)
        if exaggeration != 0.5:
            warnings.warn(
                "ChatterboxTurboBackend: exaggeration has no documented effect "
                "on Turbo and is not passed through.",
                stacklevel=2,
            )
        return super().load_voice(
            voice_id, reference_wav, exaggeration=exaggeration, force=force, **params
        )

    def _check_clip(self, reference_wav: Path) -> None:
        # Check up front so a short clip fails with a clear error instead of an
        # opaque AssertionError from inside prepare_conditionals. A clip soundfile
        # cannot read is left for prepare_conditionals to report.
        try:
            duration = sf.info(str(reference_wav)).duration
        except Exception:
            return
        if duration <= MIN_REFERENCE_EXCLUSIVE_S:
            raise ValueError(
                f"chatterbox-turbo needs a reference clip longer than "
                f"{MIN_REFERENCE_EXCLUSIVE_S:g}s (got {duration:.1f}s). "
                f"Use a longer clip or another backend."
            )

    def _prepare(self, reference_wav: Path, exaggeration: float) -> None:
        # Turbo ignores emotion (hp.emotion_adv is False), so exaggeration is
        # not passed, though prepare_conditionals accepts it.
        try:
            self.model.prepare_conditionals(str(reference_wav))
        except AssertionError as e:
            raise ValueError(f"chatterbox-turbo rejected the reference clip: {e}") from e

    def speak(
        self,
        text: str,
        voice_id: str,
        cfg_weight: float | None = None,
        **_ignored: Any,
    ) -> bytes:
        validate_voice_id(voice_id)
        if cfg_weight is not None and cfg_weight != 0.5:
            log.warning("Warning: cfg_weight has no effect on chatterbox-turbo voices.")
        with self._lock:
            self._check_text(text)
            self._activate(voice_id)
            # No audio_prompt_path here either — same caching reason as the
            # other backends. Paralinguistic tags ([laugh] etc.) go directly
            # in `text`, not as a separate parameter.
            wav = self.model.generate(text)
        return self._wav_bytes(wav)
