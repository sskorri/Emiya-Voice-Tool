"""The backends as the UI names them, plus the Speak presets and the Turbo tags (M L1192-1212; S01 §2.1).

A core constant, theme-agnostic: dock rows, popover rules lines, log lines and button labels all read
their copy here. Order is the dock order (Multilingual, Standard, Turbo); a backend id this table does
not know is shown as itself. DEFAULT_BACKEND and DEFAULT_CFG_WEIGHT are config.py's values, for the
behaviours, which may not import config (R1): "Load Multilingual" after a failed load (D13) and the
Guidance a picked voice starts with. The engine's static tables and its name rule are public here for
the same reason: BACKENDS (what each backend supports), LANGUAGES (the 23 codes and their English names)
and voice_id_error (why a name cannot be a voice id, or None).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import config
from engine.backends.base import voice_id_error
from engine.backends.info import BACKENDS, LANGUAGES

__all__ = ["BACKENDS", "DEFAULT_BACKEND", "DEFAULT_CFG_WEIGHT", "LANGUAGES", "ORDER", "PRESETS", "TAGS",
           "BackendCopy", "Preset", "copy_of", "label", "rules", "tip", "voice_id_error"]

MULTILINGUAL = "chatterbox-multilingual"
STANDARD = "chatterbox-standard"
TURBO = "chatterbox-turbo"

DEFAULT_BACKEND: str = str(getattr(config, "DEFAULT_BACKEND", MULTILINGUAL))       # CFG L9
DEFAULT_CFG_WEIGHT: float = float(getattr(config, "DEFAULT_CFG_WEIGHT", 0.5))      # CFG L28


@dataclass(frozen=True)
class BackendCopy:
    """The UI copy of one backend."""

    backend: str
    label: str            # "Multilingual"
    tip: str              # dock row and Models card title
    rules: str            # the popover's rules line


@dataclass(frozen=True)
class Preset:
    """One Speak delivery preset (M L1207-1211)."""

    name: str
    exaggeration: float
    cfg_weight: float


_COPY: Mapping[str, BackendCopy] = MappingProxyType({
    MULTILINGUAL: BackendCopy(
        MULTILINGUAL, "Multilingual",
        "Multilingual · 23 languages · Expressiveness and Guidance",
        "23 languages · Expressiveness and Guidance"),
    STANDARD: BackendCopy(
        STANDARD, "Standard",
        "Standard · English only · often the most natural English",
        "English only · Expressiveness and Guidance"),
    TURBO: BackendCopy(
        TURBO, "Turbo",
        "Turbo · English only · fastest · tags · ignores Expressiveness and Guidance · needs a clip over 5 s",
        "English only · ignores Expressiveness and Guidance · understands [laugh] [chuckle] [cough] [sigh] [gasp]"),
})

ORDER: tuple[str, ...] = tuple(name for name in BACKENDS if name in _COPY)
PRESETS: tuple[Preset, ...] = (Preset("Default", 0.5, 0.5), Preset("Slower", 0.5, 0.3), Preset("Dramatic", 0.7, 0.3))
TAGS: tuple[str, ...] = tuple(BACKENDS[TURBO].tags)


def copy_of(backend: str) -> BackendCopy:
    """The copy of a backend; an unknown id gets itself as label and empty texts."""
    return _COPY.get(backend) or BackendCopy(backend, backend, "", "")


def label(backend: str) -> str:
    """"Multilingual", "Standard", "Turbo"; any other id unchanged ("" stays "")."""
    return copy_of(backend).label if backend else ""


def tip(backend: str) -> str:
    return copy_of(backend).tip


def rules(backend: str) -> str:
    return copy_of(backend).rules
