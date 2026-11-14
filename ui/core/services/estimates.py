"""Take time and audio estimates from earlier takes (§4.15.2).

They replace the mockup's fixed seconds per character: an estimate exists only when at
least MIN_TAKES comparable takes exist (same backend, same script class), and it scales
their median seconds per character to the new script. Speak's "≈ N s to generate", the
take fill and the 40 s warning read it.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ui.contract.records import Take

__all__ = ["CUT_WARNING_S", "HISTORY_TAKES", "MIN_TAKES", "ScriptClass", "TakeEstimate",
           "estimate_take", "script_class"]

ScriptClass = Literal["cjk", "latin"]

HISTORY_TAKES = 20               # the newest comparable takes that count
MIN_TAKES = 3                    # below this there is no estimate
CUT_WARNING_S = 35.0             # audio estimates above this warn " · may be cut at 40 s"

_CJK = re.compile("[぀-ヿ一-鿿]")      # kana, then CJK unified ideographs


@dataclass(frozen=True)
class TakeEstimate:
    """What a take of a script should cost, from the medians of comparable takes."""

    made_s: float                # wall time of the speak call
    audio_s: float               # length of the audio
    count: int                   # comparable takes the medians came from (MIN_TAKES..HISTORY_TAKES)

    @property
    def may_be_cut(self) -> bool:
        """The script may run into the 40 s token limit."""
        return self.audio_s > CUT_WARNING_S


def script_class(text: str) -> ScriptClass:
    """The script class: "cjk" when any character is in U+3040-U+30FF or U+4E00-U+9FFF, else "latin"."""
    return "cjk" if _CJK.search(text) else "latin"


def estimate_take(takes: Sequence[Take], backend: str, text: str) -> TakeEstimate | None:
    """made_s and audio_s for text on backend, or None below MIN_TAKES comparable takes.

    made_s = len(text) · median(gen_seconds / len(take.text)) and audio_s = len(text) ·
    median(seconds / len(take.text)) over the newest HISTORY_TAKES comparable takes.
    """
    if not text:
        return None
    sample = _comparable(takes, backend, script_class(text))
    if len(sample) < MIN_TAKES:
        return None
    made = median(take.gen_seconds / len(take.text) for take in sample)
    audio = median(take.seconds / len(take.text) for take in sample)
    return TakeEstimate(made_s=len(text) * made, audio_s=len(text) * audio, count=len(sample))


def _comparable(takes: Sequence[Take], backend: str, cls: ScriptClass) -> list[Take]:
    """The newest HISTORY_TAKES usable takes of backend whose script class is cls (by id, newest first)."""
    found: list[Take] = []
    for take in sorted(takes, key=lambda t: t.id, reverse=True):
        if take.backend == backend and _usable(take) and script_class(take.text) == cls:
            found.append(take)
            if len(found) == HISTORY_TAKES:
                break
    return found


def _usable(take: Take) -> bool:
    """A take whose rates mean something: it has text and timings, and was not cut at the token limit."""
    return (bool(take.text) and not take.hit_token_limit
            and _positive(take.seconds) and _positive(take.gen_seconds))


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0
