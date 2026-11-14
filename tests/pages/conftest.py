"""NiceGUI user-fixture page tests (§8.5): gui.py runs for every test, on a FakeEngine scenario's scratch data.

The autouse fake_env reads @pytest.mark.scenario(name) (default "speak") and points gui.py at a fresh
data root (tmp_path) with the manual clock, before the `user` fixture runs gui.py. `core`, `fake` and
`clock` reach what that run composed, once core.start() and the scenario's launch state are in;
`open_route(user, route)` opens "/", clicks nav.<page> and waits for page.<id>. The user fixture itself
fails a test that logged an ERROR. The autouse `startfile` keeps os.startfile from ever starting a program:
the real kit's Show in Explorer and Show Folder run, and the calls are only recorded.
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ui.contract.routes import PAGE_ID, Route
from ui.core.clock import CLOCK_ENV, ManualScheduler

if TYPE_CHECKING:
    from nicegui.testing import User

    from ui.core import Core
    from ui.core.fake import FakeEngine

DEFAULT_SCENARIO = "speak"

OpenRoute = Callable[["User", Route], Awaitable[None]]


@pytest.fixture(autouse=True)
def fake_env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The environment gui.py reads under the user fixture; returns the scenario name."""
    marker = request.node.get_closest_marker("scenario")
    scenario = str(marker.args[0]) if marker is not None and marker.args else DEFAULT_SCENARIO
    monkeypatch.setenv("VOICETOOL_FAKE", scenario)
    monkeypatch.setenv("VOICETOOL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv(CLOCK_ENV, "manual")
    monkeypatch.delenv("VOICETOOL_ENGINE", raising=False)
    return scenario


@pytest.fixture(autouse=True)
def startfile(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """os.startfile records its arguments instead of opening Explorer or a player (no window under pytest)."""
    started: list[tuple[str, ...]] = []

    def record(path: Any, *args: Any, **kwargs: Any) -> None:
        started.append((str(path), *map(str, args), *(f"{key}={value}" for key, value in kwargs.items())))

    monkeypatch.setattr(os, "startfile", record, raising=False)
    return started


@pytest.fixture
async def core(user: User, tmp_path: Path) -> Core:
    """This test's Core (ui.launch.LAST_CORE), after its startup checks and the scenario's launch state."""
    from ui import launch

    run = launch.LAST_LAUNCH
    assert run is not None and launch.LAST_CORE is run.core, "gui.py did not compose the app"
    assert run.paths.root == tmp_path, "LAST_CORE belongs to another test"
    await run.ready()
    return run.core


@pytest.fixture
def fake(core: Core) -> FakeEngine:
    """The FakeEngine behind this test's core (holds, failures and the recorded calls)."""
    from ui import launch
    from ui.core.fake import FakeEngine

    engine = launch.LAST_LAUNCH.engine if launch.LAST_LAUNCH is not None else None
    assert isinstance(engine, FakeEngine)
    return engine


@pytest.fixture
def clock(core: Core) -> ManualScheduler:
    """The manual clock every core and behaviour timer runs on (advance() it to pass time)."""
    assert isinstance(core.scheduler, ManualScheduler)
    return core.scheduler


@pytest.fixture
def open_route() -> OpenRoute:
    """open_route(user, route): open "/", click nav.<page> like a user, wait for page.<id>."""
    return _open_route


async def _open_route(user: User, route: Route) -> None:
    await user.open("/")
    page = PAGE_ID[route]
    if route != "/":
        user.find(marker=f"nav.{page}").click()
    await user.should_see(marker=f"page.{page}")
