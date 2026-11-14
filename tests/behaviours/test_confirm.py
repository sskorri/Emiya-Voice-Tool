"""Arm and AskState (§5.1), the per-client plumbing, and the BehaviourFactory (§5.0)."""
from __future__ import annotations

import importlib.abc
import inspect
import logging
import sys
from collections.abc import Callable
from typing import Any

import pytest

from ui import behaviours
from ui.behaviours import PAGE_CONTROLLERS, BehaviourFactory, NotBuilt, PageController
from ui.behaviours.commands import CommandRegistry
from ui.behaviours.confirm import ARM_WINDOW_S, Arm, AskState, Listeners, Subscriptions
from ui.behaviours.models import ModelsController
from ui.behaviours.playback import PlaybackController
from ui.behaviours.search import SearchController
from ui.behaviours.status import StatusController
from ui.contract import viewmodels as vms
from ui.contract.plans import ConfirmRequest
from ui.contract.ports import KitPort
from ui.contract.theme import Behaviours
from ui.core.clock import ManualScheduler

from tests.behaviours import FakeKit, StubSpeak, factory_for, no_error_logs, offline, started  # noqa: F401
from tests.core.test_facade import Rig, rigs  # noqa: F401


def ask_request(token: str = "t1", title: str = "Switch to Turbo") -> ConfirmRequest:
    return ConfirmRequest(token=token, kind="switch_model", style="ask", title=title, primary="Switch Model")


def shape(func: Callable[..., Any]) -> str:
    """'async (a, *, b=)': the parameter shape after self, as test_contract writes it."""
    out, keyword_only = [], False
    for param in list(inspect.signature(func).parameters.values())[1:]:
        if param.kind is param.KEYWORD_ONLY and not keyword_only:
            out.append("*")
            keyword_only = True
        out.append(param.name if param.default is param.empty else f"{param.name}=")
    return ("async " if inspect.iscoroutinefunction(func) else "") + "(" + ", ".join(out) + ")"


def protocol_shapes(protocol: type) -> dict[str, str]:
    found: dict[str, str] = {}
    for cls in reversed(protocol.__mro__):
        if cls is object or not getattr(cls, "_is_protocol", False):
            continue
        found.update({name: shape(member) for name, member in vars(cls).items()
                      if inspect.isfunction(member) and not name.startswith("_")})
    return found


# ---------- Arm ----------

def test_the_first_press_only_arms() -> None:
    clock = ManualScheduler()
    seen: list[tuple[str, ...]] = []
    arm = Arm(clock, lambda: seen.append(arm.armed))
    assert arm.press("delete") is False
    assert arm.is_armed("delete") and seen == [("delete",)]


def test_a_second_press_within_three_seconds_acts_once() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    arm.press("delete")
    clock.advance(2.9)
    assert arm.press("delete") is True
    assert not arm.is_armed("delete")
    assert arm.press("delete") is None                  # a burst after the action cannot arm the next row
    assert not arm.is_armed("delete")
    clock.advance(0.6)
    assert arm.press("delete") is False


def test_confirmation_needs_a_quiet_half_second_since_the_latest_press() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    assert arm.press("clear") is False
    for gap in (0.15, 0.15, 0.15, 0.15, 0.5):
        clock.advance(gap)
        assert arm.press("clear") is None
        assert arm.is_armed("clear")
    clock.advance(0.51)
    assert arm.press("clear") is True


def test_a_held_burst_does_not_extend_the_original_expiry() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    assert arm.press("delete", scope="aiko") is False
    for _ in range(7):
        clock.advance(0.4)
        assert arm.press("delete", scope="aiko") is None
    clock.advance(0.2)
    assert not arm.is_armed("delete")


@pytest.mark.parametrize("wait", [3.0, 3.1])
def test_the_arm_expires_after_three_seconds(wait: float) -> None:
    clock = ManualScheduler()
    seen: list[int] = []
    arm = Arm(clock, lambda: seen.append(1))
    arm.press("purge")
    clock.advance(wait)
    assert not arm.is_armed("purge") and len(seen) == 2
    assert arm.press("purge") is False
    assert ARM_WINDOW_S == 3.0


def test_a_different_scope_arms_again() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    assert arm.press("replace", scope=1) is False
    assert arm.press("replace", scope=2) is False
    assert arm.scope("replace") == 2
    clock.advance(0.6)
    assert arm.press("replace", scope=2) is True


def test_re_arming_restarts_the_window() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    arm.press("purge", scope=(1, 2))
    clock.advance(2.0)
    arm.press("purge", scope=(1, 2, 3))                 # another scope: a fresh 3 s
    clock.advance(2.0)
    assert arm.is_armed("purge")
    assert arm.press("purge", scope=(1, 2, 3)) is True


def test_keys_are_independent_and_disarm_takes_one_or_all() -> None:
    clock = ManualScheduler()
    arm = Arm(clock)
    for key in ("quit", "delete", "purge"):
        arm.press(key)
    arm.disarm("delete")
    assert arm.armed == ("quit", "purge")
    arm.disarm()
    assert arm.armed == () and arm.scope("quit") is None
    assert clock.pending == 0                            # disarming cancels the expiry timers


def test_listeners_survive_a_failing_listener(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("drawing failed")

    listeners = Listeners(broken)
    listeners.add(lambda: calls.append("second"))
    listeners.notify()
    assert calls == ["second"]
    assert "drawing failed" in caplog.text
    listeners.remove(broken)
    listeners.notify()
    assert calls == ["second", "second"]


# ---------- AskState ----------

def test_ask_state_opens_replaces_and_closes() -> None:
    seen: list[str] = []
    ask = AskState(lambda: seen.append(ask.request.title if ask.request else ""))
    ask.open(ask_request(), dont_visible=True)
    ask.set_dont(True)
    ask.replace(ask_request(token="t2"))
    assert (ask.request.token, ask.dont, ask.dont_visible, ask.is_open) == ("t2", True, True, True)
    ask.close()
    assert (ask.request, ask.dont, ask.dont_visible, ask.is_open) == (None, False, False, False)
    assert seen == ["Switch to Turbo", "Switch to Turbo", "Switch to Turbo", ""]


def test_the_tick_needs_a_visible_toggle_and_reopening_clears_it() -> None:
    ask = AskState()
    ask.open(ask_request())
    ask.set_dont(True)
    assert ask.dont is False                             # D20: no toggle on this ask
    ask.open(ask_request(), dont_visible=True)
    ask.set_dont(True)
    ask.open(ask_request("t3"), dont_visible=True)
    assert ask.dont is False
    ask.close()
    ask.close()                                          # idempotent
    ask.replace(ask_request())                           # nothing open: nothing to redraw
    assert ask.request is None


# ---------- Subscriptions ----------

async def test_subscriptions_are_dropped_together(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    events = rig.core.events
    before = len(events.changed.callbacks), len(events.toast.callbacks)
    subs = Subscriptions()
    subs.add(events.changed, lambda: None)
    subs.add(events.toast, lambda toast: None)
    assert (len(events.changed.callbacks), len(events.toast.callbacks)) == (before[0] + 1, before[1] + 1)
    subs.close()
    assert (len(events.changed.callbacks), len(events.toast.callbacks)) == before


# ---------- the factory ----------

async def test_the_factory_implements_behaviours_and_memoizes(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, kit = factory_for(rig)
    assert isinstance(factory, Behaviours) and isinstance(kit, KitPort)
    assert factory.status() is factory.status() and factory.models() is factory.models()
    assert factory.built("search") is None
    assert factory.search() is factory.built("search")
    assert (factory.core, factory.kit, factory.client, factory.browser) == (rig.core, kit, None, False)


@pytest.mark.parametrize(("protocol", "make"), [
    (vms.StatusController, BehaviourFactory.status),
    (vms.ModelsController, BehaviourFactory.models),
    (vms.PlaybackController, BehaviourFactory.playback),
    (vms.CommandRegistry, BehaviourFactory.commands),
    (vms.SearchController, BehaviourFactory.search),
], ids=lambda value: getattr(value, "__name__", ""))
async def test_each_common_controller_matches_its_protocol(rigs: Callable[..., Rig], protocol: type,
                                                            make: Callable[[BehaviourFactory], Any]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    controller = make(factory)
    assert isinstance(controller, protocol)
    shapes = protocol_shapes(protocol)
    assert {name: shape(getattr(type(controller), name)) for name in shapes} == shapes
    assert type(controller) in (StatusController, ModelsController, PlaybackController, CommandRegistry,
                                SearchController)


async def test_a_page_controller_that_is_not_built_yet_gets_a_placeholder(
        rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    rig = await started(rigs)
    monkeypatch.setitem(PAGE_CONTROLLERS, "takes", PageController(
        "ui.behaviours.not_there_yet", "TakesController", vms.TakesController, vms.TakesVM))
    monkeypatch.setattr(behaviours, "_WARNED", set())
    factory, _ = factory_for(rig)
    takes = factory.takes()
    assert isinstance(takes, NotBuilt) and isinstance(takes, vms.TakesController)
    assert takes.vm == vms.TakesVM() and factory.takes() is takes
    assert takes.set_tab("all") is None and takes.vm.tab == "session"
    assert await takes.purge_unstarred() is None
    warnings = [r for r in caplog.records if r.name == "voicetool.behaviours" and "not built yet" in r.getMessage()]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    factory_for(rig)[0].takes()                          # another page load: the warning is not repeated
    assert len([r for r in caplog.records if "not built yet" in r.getMessage()]) == 1


async def test_a_page_controller_module_is_imported_when_first_asked_for(
        rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await started(rigs)
    monkeypatch.setitem(PAGE_CONTROLLERS, "speak", PageController(
        "tests.behaviours", "StubSpeak", vms.SpeakController, vms.SpeakVM))
    factory, _ = factory_for(rig)
    speak = factory.speak()
    assert isinstance(speak, StubSpeak) and factory.speak() is speak


class _BrokenPage(importlib.abc.MetaPathFinder):
    """Finds tests.behaviours.broken_page and fails like a page module whose own import is missing."""

    def find_spec(self, name: str, path: Any, target: Any = None) -> None:
        if name == "tests.behaviours.broken_page":
            raise ModuleNotFoundError("No module named 'numpyx'", name="numpyx")
        return None


async def test_a_page_module_with_a_missing_import_still_fails(
        rigs: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await started(rigs)
    monkeypatch.setattr(sys, "meta_path", [_BrokenPage(), *sys.meta_path])
    monkeypatch.setitem(PAGE_CONTROLLERS, "voices", PageController(
        "tests.behaviours.broken_page", "VoicesController", vms.VoicesController, vms.VoicesVM))
    factory, _ = factory_for(rig)
    with pytest.raises(ModuleNotFoundError, match="numpyx"):
        factory.voices()
    assert factory.built("voices") is None


async def test_provide_injects_a_stub_before_first_use(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    stub = StubSpeak(factory)
    factory.provide("speak", stub)
    assert factory.speak() is stub and factory.built("speak") is stub


async def test_a_controller_that_needs_itself_is_refused(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    with pytest.raises(RuntimeError, match="needs itself"):
        factory._get("loop", lambda: factory._get("loop", object))
    assert factory.built("loop") is None


async def test_close_drops_every_subscription_and_the_client_triggers_it(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    events, settings = rig.core.events, rig.core.settings
    counts = (len(events.changed.callbacks), len(events.toast.callbacks), len(events.take_ready.callbacks),
              len(settings.changed.callbacks))

    class Client:
        def __init__(self) -> None:
            self.handlers: list[Callable[[], Any]] = []

        def on_delete(self, handler: Callable[[], Any]) -> None:
            self.handlers.append(handler)

    client: Any = Client()
    factory = BehaviourFactory(rig.core, client, FakeKit())
    for build in (factory.status, factory.models, factory.playback, factory.commands, factory.search):
        build()
    factory.arm.press("quit")
    factory.models().ask(rig.core.planner.switch_plan("chatterbox-turbo", reason="explicit"))
    assert len(events.changed.callbacks) > counts[0]
    assert client.handlers == [factory.close]
    client.handlers[0]()
    assert (len(events.changed.callbacks), len(events.toast.callbacks), len(events.take_ready.callbacks),
            len(settings.changed.callbacks)) == counts
    assert factory.arm.armed == () and factory.ask.request is None
    factory.close()                                      # idempotent
