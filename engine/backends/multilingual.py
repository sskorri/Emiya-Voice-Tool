"""chatterbox-multilingual: Chatterbox Multilingual (23 languages, the default) behind VoiceBackend.

Every take passes language_id, cfg_weight and exaggeration explicitly, so generate()'s defaults never clobber
the voice's baked values; the cached conditionals load from cache/chatterbox-multilingual/<voice>.pt.
"""
from pathlib import Path
from typing import Any

from .base import ChatterboxBackend, validate_voice_id


class ChatterboxMultilingualBackend(ChatterboxBackend):
    """Chatterbox Multilingual (23 languages). The default backend."""

    name = "chatterbox-multilingual"

    def _from_pretrained(self, device: str) -> Any:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        return ChatterboxMultilingualTTS.from_pretrained(device=device)

    def _load_conds(self, path: Path) -> Any:
        from chatterbox.mtl_tts import Conditionals

        return Conditionals.load(path, map_location=self.device)

    def _text_tokens(self, text: str, language_id: str | None) -> int:
        from chatterbox.mtl_tts import punc_norm

        language = language_id.lower() if language_id else None
        return len(self.model.tokenizer.encode(punc_norm(text), language_id=language))

    def _missing_conds_text(self, voice_id: str) -> str:
        return f"Voice '{voice_id}' has no cached conditionals. Call load_voice() first."

    def speak(
        self,
        text: str,
        voice_id: str,
        language_id: str = "en",
        cfg_weight: float = 0.5,
        exaggeration: float = 0.5,
        **_ignored: Any,
    ) -> bytes:
        validate_voice_id(voice_id)
        with self._lock:
            self._check_text(text, language_id)
            self._activate(voice_id)
            # exaggeration is passed every call so generate()'s 0.5 default can't
            # clobber the baked value; unknown kwargs (e.g. audio_prompt_path) are dropped.
            wav = self.model.generate(
                text,
                language_id=language_id,
                cfg_weight=cfg_weight,
                exaggeration=exaggeration,
            )
        return self._wav_bytes(wav)
