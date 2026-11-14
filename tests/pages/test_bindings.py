"""R9 (§8.5, §8.7, I1): no binding in the app is polled.

NiceGUI keeps a binding whose source is not a BindableProperty in binding.active_links and polls it on a timer
(NG/binding.py L162-164); every binding of the shell and the six pages follows a bindable view-model field or
state mirror instead, so the list stays empty after visiting all six pages, from the rail and by page loads.
"""
from __future__ import annotations

from typing import Any

import pytest
from nicegui import binding
from nicegui.testing import User

from ui.contract.routes import PAGE_ID, ROUTES
from ui.core import Core
from ui.core.fake import FakeEngine

from tests.pages.test_wiring import settle


async def _visit_all_pages(user: User, core: Core, fake: FakeEngine) -> None:
    """Every route from the rail inside one page, then every route as a fresh page load."""
    await user.open("/")
    await settle(core, fake)
    for route in (*ROUTES[1:], "/"):
        user.find(marker=f"nav.{PAGE_ID[route]}").click()
        await user.should_see(marker=f"page.{PAGE_ID[route]}")
        await settle(core, fake)
    for route in ROUTES:
        await user.open(route)
        await user.should_see(marker=f"page.{PAGE_ID[route]}")
        await settle(core, fake)


async def test_no_binding_is_polled_after_visiting_all_six_pages(user: User, core: Core, fake: FakeEngine) -> None:
    await _visit_all_pages(user, core, fake)
    assert binding.active_links == []


@pytest.mark.parametrize("name", [pytest.param(name, marks=pytest.mark.scenario(name), id=name)
                                  for name in ("firstrun", "generating", "forged", "failure")])
async def test_no_binding_is_polled_in_other_boards(user: User, core: Core, fake: FakeEngine, name: str) -> None:
    """The boards whose pages draw other parts (the empty library, a running take, the Forge result, Retry)."""
    await _visit_all_pages(user, core, fake)
    assert binding.active_links == []


async def test_no_binding_is_polled_after_the_pages_change_state(user: User, core: Core, fake: FakeEngine,
                                                                 open_route: Any) -> None:
    """Rows rebuilt by a filter, a tab, a selection and a take keep their bindings passive too."""
    await open_route(user, "/takes")
    user.find(marker="takes.tab.all").click()
    user.find(marker="takes.pin_a.6").click()
    await settle(core, fake)
    user.find(marker="nav.voices").click()
    await user.should_see(marker="page.voices")
    user.find(marker="voices.row.marco").click()
    user.find(marker="voices.backend_filter.chatterbox-turbo").click()
    await settle(core, fake)
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    user.find(marker="speak.generate").click()
    await settle(core, fake)
    assert binding.active_links == []
