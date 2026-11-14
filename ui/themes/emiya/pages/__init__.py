"""PAGES: every route's page builder, imported lazily so that a page not built yet shows a placeholder.

Each page lives in its own module, ui.themes.emiya.pages.<page id> (PAGE_ID: speak, voices, forge, takes,
models, settings), and defines a sync build(ctx). A builder imports its module when it is called. When
exactly that module does not exist yet, it renders "This page is not built yet." and logs a WARNING, never
an ERROR, so the user fixture's ERROR check passes before every page has landed. Any other failure,
including a page module that imports something missing, propagates to the composition root, which shows
"This page failed to build." and logs an ERROR (BUILD_SPEC section 6.2.12).
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from nicegui import ui

from ui.contract.routes import PAGE_ID, ROUTES, Route
from ui.contract.theme import PageBuilder, ThemeContext

NOT_BUILT_TEXT = "This page is not built yet."
NOT_BUILT_MARKER = "page.not_built"
LOG = logging.getLogger("voicetool.theme.emiya")


@dataclass(frozen=True)
class LazyPage:
    """A page builder that imports `module` at call time and runs its build(ctx)."""

    module: str

    def __call__(self, ctx: ThemeContext) -> None:
        try:
            page = importlib.import_module(self.module)
        except ModuleNotFoundError as error:
            if error.name != self.module:
                raise
            _not_built(self.module)
            return
        page.build(ctx)


def _not_built(module: str) -> None:
    """The placeholder for a page whose module does not exist yet."""
    LOG.warning("%s is not built yet; showing the placeholder page", module)
    ui.label(NOT_BUILT_TEXT).mark(NOT_BUILT_MARKER)


PAGES: Mapping[Route, PageBuilder] = MappingProxyType(
    {route: LazyPage(f"{__name__}.{PAGE_ID[route]}") for route in ROUTES})
