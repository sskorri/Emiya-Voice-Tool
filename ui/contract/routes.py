"""The six routes, their page ids and their header titles."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

Route = Literal["/", "/voices", "/forge", "/takes", "/models", "/settings"]

ROUTES: tuple[Route, ...] = ("/", "/voices", "/forge", "/takes", "/models", "/settings")

PAGE_ID: Mapping[Route, str] = MappingProxyType({
    "/": "speak",
    "/voices": "voices",
    "/forge": "forge",
    "/takes": "takes",
    "/models": "models",
    "/settings": "settings",
})

TITLES: Mapping[Route, str] = MappingProxyType({
    "/": "Speak",
    "/voices": "Voices",
    "/forge": "Forge",
    "/takes": "Takes",
    "/models": "Models",
    "/settings": "Settings",
})
