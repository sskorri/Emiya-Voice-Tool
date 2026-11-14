"""The Emiya theme: the approved Twin Forge mockup as a NiceGUI shell around six pages.

THEME is a plain object with no UI at import: importing this package reads theme.toml, registers the theme's
components (the FX field, the take wave, the bars; the kit's player and key dispatcher) and adds no routes.
The composition root calls THEME.build_shell(ctx, mount_pages) once per page load (shell.py).

theme.toml [prefs] declares Emiya's own settings (theme.emiya.* in ui_settings.json) with their defaults; their
types give the store its checks, and PREF_CHECKS refines two of them: Effect Speed moves 25-200 % in steps of 5,
and the saber art is "show" or "hide".
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ui.contract.manifest import ThemeManifest, load_manifest
from ui.contract.routes import Route
from ui.contract.theme import PageBuilder, ThemeContext

from .pages import PAGES
from .shell import build_shell

FOLDER = Path(__file__).resolve().parent
FX_SPEED = (25, 200, 5)                          # Settings › Effect Speed: low, high and step, in %
SABER_ART = ("show", "hide")                     # Settings › Saber Art


def _fx_speed(value: Any) -> int:
    """A whole number of percent in FX_SPEED's range and steps (a slider may send 150.0); never a bool."""
    low, high, step = FX_SPEED
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        raise ValueError("expected a whole number")
    number = int(value)
    if low <= number <= high and (number - low) % step == 0:
        return number
    raise ValueError(f"expected {low} to {high} in steps of {step}")


def _saber_art(value: Any) -> str:
    if isinstance(value, str) and value in SABER_ART:
        return value
    raise ValueError(f"expected one of {', '.join(SABER_ART)}")


PREF_CHECKS: Mapping[str, Callable[[Any], Any]] = MappingProxyType({"fx_speed": _fx_speed, "saber_art": _saber_art})


class EmiyaTheme:
    """The theme the composition root applies: its manifest, its page builders, its shell and its pref checks."""

    def __init__(self) -> None:
        self.manifest: ThemeManifest = load_manifest(FOLDER)
        self.pages: Mapping[Route, PageBuilder] = PAGES
        self.pref_checks: Mapping[str, Callable[[Any], Any]] = PREF_CHECKS

    def build_shell(self, ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        """The shell for one page load; it calls mount_pages() exactly once, inside its content box."""
        build_shell(ctx, mount_pages, self.manifest)


THEME = EmiyaTheme()
