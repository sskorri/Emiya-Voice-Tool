"""Wiring rows (§8.5): every page's controls reach their intents, with the right confirmation and loads.

Each page package adds tests\\pages\\wiring\\<page>.py holding WIRING, a tuple of Wire rows, and OPT_OUT,
the intents deliberately left without a control. collect() gathers the rows of every module; I1's
tests\\pages\\test_wiring.py runs them all (a module that does not exist is skipped).
"""
from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

from ui.contract.routes import Route

__all__ = ["MODULES", "Act", "Confirm", "Wire", "collect", "missing", "opt_outs"]

Act = Literal["click", "type", "value", "trigger"]
Confirm = Literal["", "named", "arm", "ask"]

MODULES = ("shell", "speak", "voices", "forge", "takes", "models", "settings")


@dataclass(frozen=True)
class Wire:
    """Activating `marker` on `route` reaches `intent`, with `confirm`, loading exactly `loads`.

    confirm: "named" the control's text or aria-label names the model and nothing loads before the act;
    "arm" the first activation changes nothing and a second within 3 s acts; "ask" an element marked
    confirm.ask opens (the primary is then pressed). `session` holds a Speak draft no scenario seeds (it is
    set on core.session after the scenario's launch state); `before` opens what the act needs first (a tab).
    """

    route: Route
    marker: str                                     # the control to activate
    intent: str                                     # "<controller>.<method>", e.g. "speak.generate"
    act: Act = "click"                              # click it, type text, set a value, or trigger an event
    arg: Any = None                                 # the text, the value, or (event, args) for trigger
    scenario: str = "speak"                         # the FakeEngine scenario the row starts from
    settings: tuple[tuple[str, str, Any], ...] = ()     # (section, key, value) set before the page opens
    session: tuple[tuple[str, Any], ...] = ()       # Session (field, value) pairs set before the page opens
    before: tuple[str, ...] = ()                    # markers clicked, in order, once the route is open
    confirm: Confirm = ""
    loads: tuple[str, ...] = ()                     # the only backends the row may load, in order
    shows: tuple[str, ...] = ()                     # texts visible after the act ("Then generates take 8")
    hides: tuple[str, ...] = ()                     # texts that must not be visible ("Don't ask again")
    queued: int | None = None                       # takes waiting afterwards; None = not checked
    note: str = ""


def collect(modules: Sequence[str] = MODULES) -> tuple[Wire, ...]:
    """Every row of the wiring modules that exist, in page order."""
    return tuple(row for module in _present(modules) for row in getattr(module, "WIRING", ()))


def opt_outs(modules: Sequence[str] = MODULES) -> frozenset[str]:
    """The intents the existing modules leave without a control on purpose."""
    return frozenset(intent for module in _present(modules) for intent in getattr(module, "OPT_OUT", ()))


def missing(modules: Sequence[str] = MODULES) -> tuple[str, ...]:
    """The wiring modules not written yet."""
    return tuple(name for name in modules if _load(name) is None)


def _present(modules: Sequence[str]) -> list[ModuleType]:
    return [module for name in modules if (module := _load(name)) is not None]


def _load(name: str) -> ModuleType | None:
    """tests.pages.wiring.<name>, or None when exactly that module does not exist yet."""
    module = f"{__name__}.{name}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as error:
        if error.name != module:
            raise
        return None
