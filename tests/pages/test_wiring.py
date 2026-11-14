"""Every wiring row of every page (§8.5, I1): the control reaches its intent with its confirmation and loads
exactly the backends the row lists; and every public intent of every controller has a row or an opt-out.

One runner for all rows (tests\\pages\\wiring\\<page>.py), with the rules the page packages handed over:
- the row's settings and Speak draft (Wire.session) are in place before the page opens, and every page load
  gets a fake KitPort (Save As answers with a scratch path; no dialog, Explorer or quit ever happens);
- spies sit on the controller classes before the page binds its handlers;
- the app opens like a user opens it (open_route: "/", then the rail), the scenario's replay settles (a held job
  until it is held) and Wire.before is clicked; the records are reset only then (T1);
- a confirm="ask" row waits for the first device reading (P3) and closes an ask the board replayed (P5), so the
  ask it then sees is its own; a confirm="arm" row acts a second time and the act must happen only then.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui.element import Element
from nicegui.testing import User

from ui import launch
from ui.behaviours.commands import CommandRegistry
from ui.behaviours.forge import ForgeController
from ui.behaviours.models import ModelsController
from ui.behaviours.playback import PlaybackController
from ui.behaviours.search import SearchController
from ui.behaviours.settings import SettingsController
from ui.behaviours.speak import SpeakController
from ui.behaviours.status import StatusController
from ui.behaviours.takes import TakesController
from ui.behaviours.voices import VoicesController
from ui.contract import viewmodels as vms
from ui.contract.routes import ROUTES
from ui.contract.theme import Behaviours
from ui.core import Core, catalog
from ui.core.clock import ManualScheduler
from ui.core.fake import SCENARIOS, FakeEngine

from tests.behaviours import FakeKit
from tests.pages.wiring import MODULES, Wire, collect, missing, opt_outs

CONTROLLERS: dict[str, type] = {
    "status": StatusController, "models": ModelsController, "playback": PlaybackController,
    "commands": CommandRegistry, "search": SearchController, "speak": SpeakController, "voices": VoicesController,
    "forge": ForgeController, "takes": TakesController, "settings": SettingsController,
}
PROTOCOLS: dict[str, type] = {
    "status": vms.StatusController, "models": vms.ModelsController, "playback": vms.PlaybackController,
    "commands": vms.CommandRegistry, "search": vms.SearchController, "speak": vms.SpeakController,
    "voices": vms.VoicesController, "forge": vms.ForgeController, "takes": vms.TakesController,
    "settings": vms.SettingsController,
}
MODEL_CALLS = ("make_resident", "load_backend", "unload_backend", "load_voice", "speak")
ROWS: tuple[Wire, ...] = collect()

# What these intents finally do: never before a row's confirmation (the arm's second press, the ask's primary),
# always by the end of the row.
ACTED: dict[str, Callable[[Core, FakeEngine, FakeKit], bool]] = {
    "status.request_quit": lambda core, fake, kit: bool(kit.called("quit")),
    "speak.clear_text": lambda core, fake, kit: core.session.text == "",
    "voices.delete": lambda core, fake, kit: "delete_voice" in call_names(fake),
    "takes.purge_unstarred": lambda core, fake, kit: bool(getattr(core.trash, "moved", ())),
    "forge.forge": lambda core, fake, kit: "load_voice" in call_names(fake),
}


# ---- helpers (test_one_model and test_flows use them too) ----------------------------------------------

def call_names(fake: FakeEngine) -> list[str]:
    return [call.name for call in fake.calls]


def model_calls(fake: FakeEngine) -> list[str]:
    """The engine calls that load, unload or run a model, in order."""
    return [name for name in call_names(fake) if name in MODEL_CALLS]


def shown(user: User, marker: str) -> bool:
    """An element with this marker is visible."""
    try:
        user.find(marker=marker)
    except AssertionError:
        return False
    return True


def the(user: User, marker: str) -> Any:
    """The one visible element with this marker."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker in the page, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


POLL_S = 0.02          # Windows' monotonic clock ticks every 15.6 ms: a shorter sleep returns at once and spins


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds: handlers run as tasks and the FakeEngine on worker threads."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(POLL_S)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks start their jobs, then wait for every job no hold keeps (a held job: until it is held).

    A held job records its first calls (FakeEngine.loads included) before it reaches its hold, so a test resets
    the records only after this (T1's hand-off).
    """
    for _ in range(5):
        await asyncio.sleep(0)
    holding = [hold for hold in fake.holds if not hold.is_set()]
    if holding:
        await until(lambda: not core.state.job.kind or any(hold.entered.is_set() for hold in holding),
                    "the held call")
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if holding else [])
    for _ in range(5):
        await asyncio.sleep(0)


def behaviours_of(user: User) -> Behaviours:
    """This page load's BehaviourFactory (the shell's ThemeContext)."""
    from ui.themes.emiya.shell import ShellRoot

    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell.ctx.behaviours


def install_kit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **results: Any) -> FakeKit:
    """Every page load from now on gets this KitPort; minimize is refused as without a native window."""
    run = launch.LAST_LAUNCH
    assert run is not None
    kit = FakeKit(**{"save_result": tmp_path / "saved.wav", "minimize_result": False, **results})
    monkeypatch.setattr(run, "kit", kit)
    return kit


def spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    """Record every call of intent ("<controller>.<method>") on its controller class (async stays async)."""
    owner, name = intent.split(".")
    cls = CONTROLLERS[owner]
    original = getattr(cls, name)
    if inspect.iscoroutinefunction(original):
        async def spy_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy_async)
    else:
        def spy_sync(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy_sync)


def act(user: User, row: Wire) -> None:
    """Activate the row's control: click it, type into it, set its value or fire one of its events."""
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "type":
        found.type(row.arg)
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    elif row.act == "value":
        for element in found.elements:
            element.set_value(row.arg)
    else:
        raise AssertionError(f"unknown act {row.act!r}")


def names_the_model(user: User, row: Wire) -> bool:
    """The control's visible label or accessible name names every model the row loads (§8.5)."""
    control = the(user, row.marker)
    name = f"{getattr(control, 'label', '')} {control.props.get('aria-label', '')}"
    return bool(row.loads) and all(catalog.label(backend) in name for backend in row.loads)


async def prepare_row(row: Wire, user: User, core: Core, fake: FakeEngine, open_route: Any) -> None:
    """Open the row's route as a user does, let the board settle, and click what the act needs first."""
    for name, value in row.session:
        setattr(core.session, name, value)
    await open_route(user, row.route)
    await settle(core, fake)
    if row.confirm == "ask":
        await until(lambda: core.state.device.detected, "the first device reading")
        if shown(user, "confirm.ask"):
            user.find(marker="confirm.ask.cancel").click()       # the board's own ask is not the row's
            await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    for marker in row.before:
        user.find(marker=marker).click()
        await settle(core, fake)


async def run_row(row: Wire, user: User, core: Core, fake: FakeEngine, kit: FakeKit, open_route: Any,
                  monkeypatch: pytest.MonkeyPatch) -> None:
    """One wiring row from the opened page to the last load (the rules in the module docstring)."""
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    spy(monkeypatch, row.intent, calls)
    await prepare_row(row, user, core, fake, open_route)
    fake.reset_records()
    calls.clear()
    kit.calls.clear()
    if row.confirm == "named":
        assert names_the_model(user, row), f"{row.marker} does not name {row.loads}"
    act(user, row)
    await settle(core, fake)
    await until(lambda: row.intent in calls, f"{row.marker} to reach {row.intent}")
    await settle(core, fake)
    effect = ACTED.get(row.intent)
    if row.confirm in ("arm", "ask"):
        assert fake.loads == [], "a model loaded before the confirmation"
        assert effect is None or not effect(core, fake, kit), f"{row.marker} acted before its confirmation"
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "ask":
        await user.should_see(marker="confirm.ask")
        user.find(marker="confirm.ask.primary").click()
        await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    elif row.confirm == "arm":
        core.scheduler.advance(0.6)
        act(user, row)
        await settle(core, fake)
    elif row.confirm == "named":
        await user.should_not_see(marker="confirm.ask")
    if effect is not None:
        await until(lambda: effect(core, fake, kit), f"{row.intent} to act")
        await settle(core, fake)
    if row.queued is not None:
        assert core.state.job.queued == row.queued
    assert fake.loads == list(row.loads)
    assert len(fake.residency().resident) <= 1


# ---- the rows -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(ROWS)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    kit = install_kit(monkeypatch, tmp_path)
    await run_row(row, user, core, fake, kit, open_route, monkeypatch)


# ---- the meta-test ------------------------------------------------------------------------------------

def _public_methods(protocol: type) -> set[str]:
    """The public methods a controller protocol and its protocol bases declare."""
    found: set[str] = set()
    for cls in protocol.__mro__:
        if cls is object or not getattr(cls, "_is_protocol", False):
            continue
        found |= {name for name, member in vars(cls).items() if inspect.isfunction(member) and not name.startswith("_")}
    return found


def test_every_page_has_its_wiring_module() -> None:
    assert missing() == () and MODULES == ("shell", "speak", "voices", "forge", "takes", "models", "settings")


def test_every_public_intent_is_wired_or_opted_out() -> None:
    from tests.pages.wiring.settings import CALL_WIRING as settings_calls
    from tests.pages.wiring.speak import CALL_WIRING as speak_calls

    wired, opted = {row.intent for row in ROWS}, opt_outs()
    wired.update(row.intent for row in (*settings_calls, *speak_calls))
    for owner, protocol in PROTOCOLS.items():
        declared = {f"{owner}.{name}" for name in _public_methods(protocol)}
        assert declared - wired - opted == set(), f"{owner}: intents without a control or an opt-out"
    assert set(PROTOCOLS) == set(CONTROLLERS) == set(_public_methods(Behaviours))


def test_every_row_and_opt_out_names_a_real_intent() -> None:
    for intent in {row.intent for row in ROWS} | opt_outs():
        owner, name = intent.split(".")
        assert owner in CONTROLLERS and callable(getattr(CONTROLLERS[owner], name, None)), intent
        assert name in _public_methods(PROTOCOLS[owner]), f"{intent} is not in the {owner} protocol"
    for owner, cls in CONTROLLERS.items():
        assert PROTOCOLS[owner] in cls.__mro__, f"{cls.__name__} does not implement {owner}'s protocol"


def test_every_row_starts_from_a_real_route_and_scenario() -> None:
    assert ROWS
    for row in ROWS:
        assert row.route in ROUTES and row.scenario in SCENARIOS, row
        assert row.act in ("click", "type", "value", "trigger") and row.confirm in ("", "named", "arm", "ask"), row
        assert set(row.loads) <= set(catalog.ORDER), row
        if row.act == "trigger":
            event, _ = row.arg
            assert isinstance(event, str) and event, row
        if row.confirm == "named":
            assert len(row.loads) == 1, f"a named control names one model: {row}"
