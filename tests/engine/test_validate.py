"""voice_id_error() and validate_voice_id(): the name rules every .pt path depends on."""
from pathlib import PureWindowsPath

import pytest

from engine.backends.base import (
    MAX_VOICE_ID_LENGTH,
    RESERVED_VOICE_IDS,
    validate_voice_id,
    voice_id_error,
)

RESERVED = (
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{d}" for d in range(10)]
    + [f"LPT{d}" for d in range(10)]
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.mark.parametrize(
    "voice_id",
    ["aiko", "Aiko-2_x", "a", "0", "_", "-", "x" * 64, "nul-voice", "con_1", "com10", "lpt",
     "COM", "console", "auxiliary", "prn2"],
)
def test_valid_ids(voice_id: str) -> None:
    assert voice_id_error(voice_id) is None
    validate_voice_id(voice_id)  # does not raise


@pytest.mark.parametrize(
    "voice_id",
    ["", "rin\n", "\nrin", "rin\r\n", "a b", " aiko", "../etc", "..", "a.b", "a/b", "a\\b", "a:b",
     "aiko.pt", "rin$", "café", "１", "aikó", "tab\there", "nul\x00"],
)
def test_bad_characters(voice_id: str) -> None:
    assert voice_id_error(voice_id) == "chars"


@pytest.mark.parametrize("voice_id", [None, 5, b"aiko", ["aiko"]])
def test_non_strings_are_bad_characters(voice_id: object) -> None:
    assert voice_id_error(voice_id) == "chars"  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid voice_id"):
        validate_voice_id(voice_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [65, 66, 255, 300])
def test_length_limit(length: int) -> None:
    assert MAX_VOICE_ID_LENGTH == 64
    assert voice_id_error("x" * length) == "length"
    assert voice_id_error("x" * 64) is None


def test_bad_characters_win_over_length() -> None:
    assert voice_id_error("a b" * 40) == "chars"


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("spell", [str.upper, str.lower, str.title, lambda s: s[0].lower() + s[1:]])
def test_reserved_device_names_in_any_case(name: str, spell) -> None:
    assert voice_id_error(spell(name)) == "reserved"


def test_reserved_set_is_exactly_the_specified_names() -> None:
    assert RESERVED_VOICE_IDS == frozenset(RESERVED)
    assert len(RESERVED_VOICE_IDS) == 24


def test_reserved_set_matches_windows_device_names() -> None:
    # Python's own table knows COM1-9 and LPT1-9; COM0 and LPT0 are refused on top, by design.
    windows_reserved = {name for name in RESERVED if PureWindowsPath(name).is_reserved()}
    assert set(RESERVED) - windows_reserved == {"COM0", "LPT0"}


@pytest.mark.parametrize(
    ("voice_id", "detail"),
    [
        ("a b", "only letters, digits, '_' and '-' are allowed (this becomes a filename on disk)."),
        ("rin\n", "only letters, digits, '_' and '-' are allowed"),
        ("x" * 65, "use at most 64 characters (this becomes a filename on disk)."),
        ("nul", "it is a reserved device name on Windows (CON, PRN, AUX, NUL, COM0-9, LPT0-9)."),
        ("Com7", "it is a reserved device name on Windows"),
    ],
)
def test_validate_voice_id_messages(voice_id: str, detail: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_voice_id(voice_id)
    message = str(caught.value)
    assert message.startswith(f"Invalid voice_id {voice_id!r}: ")
    assert detail in message


def test_validate_voice_id_returns_none_for_a_good_id() -> None:
    assert validate_voice_id("aiko") is None
