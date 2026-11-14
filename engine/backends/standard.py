"""chatterbox-standard: the English-only Chatterbox model (500M) behind the VoiceBackend interface.

Guidance is floored at MIN_CFG_WEIGHT (0 crashes this model), and the cached conditionals load from
cache/chatterbox-standard/<voice>.pt; audio_prompt_path is never passed, so the cache is never bypassed.
"""
from pathlib import Path
from typing import Any

from .base import ChatterboxBackend, validate_voice_id

# Guidance 0 crashes this model: ChatterboxTTS.generate() doubles the text tokens for CFG only
# when cfg_weight > 0 (chatterbox tts.py L237-238), but T3.inference() always doubles the BOS
# embedding and concatenates the two (models/t3/t3.py L310, L313). At 0.001 the logits are
# cond + 0.001 * (cond - uncond), the same delivery Multilingual gives at 0.
MIN_CFG_WEIGHT = 1e-3


class ChatterboxStandardBackend(ChatterboxBackend):
    """English-only Chatterbox ("Tips and Tricks" model, 500M). Generally
    more natural for English than Multilingual, at the cost of losing
    whatever character Multilingual's training-language mix adds."""

    name = "chatterbox-standard"

    def _from_pretrained(self, device: str) -> Any:
        from chatterbox.tts import ChatterboxTTS

        return ChatterboxTTS.from_pretrained(device=device)

    def _load_conds(self, path: Path) -> Any:
        from chatterbox.tts import Conditionals

        return Conditionals.load(path, map_location=self.device)

    def _text_tokens(self, text: str, language_id: str | None) -> int:
        from chatterbox.tts import punc_norm

        return len(self.model.tokenizer.encode(punc_norm(text)))

    def speak(
        self,
        text: str,
        voice_id: str,
        cfg_weight: float = 0.5,
        exaggeration: float = 0.5,
        **_ignored: Any,
    ) -> bytes:
        # _ignored absorbs params other backends use (e.g. language_id from
        # Multilingual) so callers can pass a uniform set of kwargs without
        # every backend needing to know about every other backend's knobs.
        validate_voice_id(voice_id)
        with self._lock:
            self._check_text(text)
            self._activate(voice_id)
            # IMPORTANT: never pass audio_prompt_path here. Chatterbox's
            # generate() re-runs prepare_conditionals() from scratch every
            # time audio_prompt_path is set, silently bypassing the cache.
            # exaggeration passed explicitly so generate()'s 0.5 default can't
            # clobber the baked emotion_adv.
            wav = self.model.generate(
                text,
                cfg_weight=max(float(cfg_weight), MIN_CFG_WEIGHT),
                exaggeration=exaggeration,
            )
        return self._wav_bytes(wav)
