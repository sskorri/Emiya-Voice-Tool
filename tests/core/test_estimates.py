"""Take estimates (§4.15.2): nothing below three comparable takes, script classes kept apart,
medians of seconds per character scaled to the script."""
from __future__ import annotations

import random

import pytest

from ui.contract.records import Take
from ui.core.services.estimates import (
    CUT_WARNING_S,
    HISTORY_TAKES,
    MIN_TAKES,
    TakeEstimate,
    estimate_take,
    script_class,
)

MTL = "chatterbox-multilingual"
TUR = "chatterbox-turbo"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def take(take_id: int, text: str = "x" * 10, *, backend: str = MTL, gen_seconds: float = 1.0,
         seconds: float = 0.5, hit_token_limit: bool = False) -> Take:
    return Take(id=take_id, voice="aiko", backend=backend, language="en", text=text, exaggeration=0.5,
                cfg_weight=0.5, seconds=seconds, gen_seconds=gen_seconds, tokens=None,
                hit_token_limit=hit_token_limit, created="2026-11-13T14:31:58+01:00", session="s",
                starred=False, wav=f"outputs/takes/aiko_take{take_id}.wav")


@pytest.mark.parametrize(("text", "cls"), [
    ("Hello there.", "latin"),
    ("", "latin"),
    ("おはよう！", "cjk"),                     # hiragana
    ("カタカナ", "cjk"),                       # katakana
    ("今日", "cjk"),                           # kanji
    ("Buongiorno, 世界", "cjk"),               # any one character decides
    ("぀", "cjk"), ("ヿ", "cjk"), ("一", "cjk"), ("鿿", "cjk"),
    ("〿", "latin"), ("㄀", "latin"), ("䷿", "latin"), ("ꀀ", "latin"),
    ("안녕하세요", "latin"),                    # Hangul is outside both ranges
    ("ＡＢＣ", "latin"),                       # full-width Latin too
])
def test_script_class(text: str, cls: str) -> None:
    assert script_class(text) == cls


def test_no_estimate_below_three_comparable_takes() -> None:
    assert MIN_TAKES == 3
    two = [take(1), take(2)]
    assert estimate_take([], MTL, "Hello") is None
    assert estimate_take(two, MTL, "Hello") is None
    assert estimate_take(two + [take(3)], MTL, "Hello") is not None


def test_medians_of_seconds_per_character_scale_to_the_script() -> None:
    takes = [take(1, "a" * 10, gen_seconds=1.0, seconds=0.5),      # 0.10 s and 0.05 s per character
             take(2, "b" * 20, gen_seconds=6.0, seconds=1.4),      # 0.30 and 0.07
             take(3, "c" * 40, gen_seconds=8.0, seconds=2.4)]      # 0.20 and 0.06
    estimate = estimate_take(takes, MTL, "d" * 50)
    assert estimate is not None and estimate.count == 3
    assert estimate.made_s == pytest.approx(50 * 0.20)
    assert estimate.audio_s == pytest.approx(50 * 0.06)
    even = estimate_take(takes + [take(4, "e" * 10, gen_seconds=4.0, seconds=0.8)], MTL, "d" * 50)
    assert even is not None and even.made_s == pytest.approx(50 * 0.25)   # mean of the middle two
    assert even.audio_s == pytest.approx(50 * 0.065)


def test_only_takes_of_the_same_backend_count() -> None:
    takes = [take(1, backend=TUR, gen_seconds=0.5), take(2, backend=TUR, gen_seconds=0.5),
             take(3, backend=TUR, gen_seconds=0.5), take(4), take(5)]
    assert estimate_take(takes, MTL, "x" * 10) is None
    turbo = estimate_take(takes, TUR, "x" * 10)
    assert turbo is not None and turbo.count == 3 and turbo.made_s == pytest.approx(0.5)


def test_script_classes_never_mix() -> None:
    japanese = [take(i, "こんにちは。", gen_seconds=3.0) for i in (1, 2, 3)]
    latin = [take(4), take(5)]
    assert estimate_take(japanese + latin, MTL, "Hello there") is None
    estimate = estimate_take(japanese + latin, MTL, "今日はいい天気")
    assert estimate is not None and estimate.count == 3
    assert estimate.made_s == pytest.approx(7 * 3.0 / 6)


def test_only_the_newest_twenty_comparable_takes_count() -> None:
    old = [take(i, gen_seconds=100.0) for i in range(1, 21)]         # 10 s per character
    new = [take(i, gen_seconds=1.0) for i in range(21, 41)]          # 0.1 s per character
    mixed = old + new
    random.Random(7).shuffle(mixed)                                  # the order of the input does not matter
    estimate = estimate_take(mixed, MTL, "x" * 10)
    assert estimate is not None and estimate.count == HISTORY_TAKES == 20
    assert estimate.made_s == pytest.approx(1.0)


def test_cut_empty_and_untimed_takes_are_not_comparable() -> None:
    takes = [take(1), take(2), take(3, hit_token_limit=True), take(4, text=""), take(5, seconds=0.0),
             take(6, gen_seconds=float("nan"))]
    assert estimate_take(takes, MTL, "Hello") is None
    estimate = estimate_take(takes + [take(7)], MTL, "Hello")
    assert estimate is not None and estimate.count == 3


def test_an_empty_script_has_no_estimate() -> None:
    assert estimate_take([take(1), take(2), take(3)], MTL, "") is None


def test_the_40_s_warning_starts_above_35_s() -> None:
    assert CUT_WARNING_S == 35.0
    assert TakeEstimate(made_s=1.0, audio_s=35.1, count=3).may_be_cut
    assert not TakeEstimate(made_s=1.0, audio_s=35.0, count=3).may_be_cut
