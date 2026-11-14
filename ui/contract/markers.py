"""Capability markers: the element markers every theme must render (checked by the page tests)."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ui.contract.routes import PAGE_ID, Route

SHELL_MARKERS: tuple[str, ...] = tuple(f"nav.{page}" for page in PAGE_ID.values()) + ("status",)

FRAMELESS_MARKERS: tuple[str, ...] = ("window.minimize", "window.close")  # only for frameless windows

# "settings.theme" returns to the Settings row when the theme picker lands (deferred this phase).
REQUIRED_MARKERS: Mapping[Route, tuple[str, ...]] = MappingProxyType({
    "/": ("speak.voice", "speak.text", "speak.delivery", "speak.generate", "speak.status", "take.player"),
    "/voices": ("voices.list", "voices.edit", "voices.delete", "voices.reforge"),
    "/forge": ("forge.clip", "forge.name", "forge.backend", "forge.consent", "forge.go"),
    "/takes": ("takes.list", "takes.star", "takes.compare", "takes.purge"),
    "/models": ("models.switch", "models.free", "models.limit", "models.history"),
    "/settings": ("settings.core", "settings.about"),
})

# A "named" confirmation needs no marker: the control's own label or accessible name names the model.
CONFIRM_MARKERS: Mapping[str, str] = MappingProxyType({"arm": "confirm.arm", "ask": "confirm.ask"})
