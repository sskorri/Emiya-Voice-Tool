"""The Emiya theme's settings section as the composition root builds it (ui.launch.theme_settings), for tests that
build a SettingsStore or a Core by hand (tests\\core, tests\\behaviours, tests\\reliability).

The core knows no theme by name: the store gets theme.<slug> from the active theme's theme.toml [prefs] and its
pref_checks. Emiya is the only theme, so these helpers give the store and the app name every Emiya run has.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ui.core.services.settings import SettingsStore, theme_schema

EMIYA_SECTION = "theme.emiya"
EMIYA_TITLE = "Emiya Voice"                      # theme.toml [window] title: the Log's "Emiya Voice started"


def emiya_prefs() -> dict[str, Any]:
    """The theme_prefs of Emiya's section (its manifest [prefs] with its PREF_CHECKS)."""
    from ui.themes.emiya import THEME

    return theme_schema(THEME.manifest.prefs, THEME.pref_checks)


def emiya_settings(path: Path) -> SettingsStore:
    """SettingsStore(path) with Emiya's theme section, as a real Emiya run builds it."""
    return SettingsStore(Path(path), theme_section=EMIYA_SECTION, theme_prefs=emiya_prefs())


def emiya_read_early(path: Path) -> dict[str, Any]:
    """SettingsStore.read_early(path) with Emiya's theme section."""
    return SettingsStore.read_early(Path(path), theme_section=EMIYA_SECTION, theme_prefs=emiya_prefs())
