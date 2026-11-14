"""The behaviours (§5): every flow, validation and confirmation, one set per client, no widgets.

BehaviourFactory(core, client, kit) is made once per page load (ui.launch root()) and implements the
contract's Behaviours: each controller is built on first use and memoized, and every controller class
is constructed as Cls(factory). The factory also holds the client's Arm and AskState (confirm.py), the
KitPort and the --browser flag. The page controllers (speak, voices, forge, takes, settings) are
imported when first asked for; while a page module does not exist yet, a placeholder with the page's
default VM and intents that do nothing stands in, and a WARNING (never an ERROR) is logged once.

Behaviours import only the standard library, ui.core, ui.contract and nicegui.binding / nicegui.Event;
native dialogs, Explorer, quit and minimize go through the injected KitPort (R1, §5.0).
"""
from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ui.contract.viewmodels import ForgeController as ForgeControllerProtocol
from ui.contract.viewmodels import ForgeVM, SettingsVM, SpeakVM, TakesVM, VoicesVM
from ui.contract.viewmodels import SettingsController as SettingsControllerProtocol
from ui.contract.viewmodels import SpeakController as SpeakControllerProtocol
from ui.contract.viewmodels import TakesController as TakesControllerProtocol
from ui.contract.viewmodels import VoicesController as VoicesControllerProtocol
from ui.core.facade import describe_error

from .commands import CommandRegistry
from .confirm import Arm, AskState
from .models import ModelsController
from .playback import PlaybackController
from .search import SearchController
from .status import StatusController

if TYPE_CHECKING:
    from nicegui import Client

    from ui.contract.ports import KitPort
    from ui.core import Core

__all__ = ["PAGE_CONTROLLERS", "BehaviourFactory", "NotBuilt", "PageController"]

log = logging.getLogger("voicetool.behaviours")
_WARNED: set[str] = set()


@dataclass(frozen=True)
class PageController:
    """Where a page controller lives, the protocol it implements and its view model."""

    module: str
    cls: str
    protocol: type
    vm: type


PAGE_CONTROLLERS: dict[str, PageController] = {
    "speak": PageController("ui.behaviours.speak", "SpeakController", SpeakControllerProtocol, SpeakVM),
    "voices": PageController("ui.behaviours.voices", "VoicesController", VoicesControllerProtocol, VoicesVM),
    "forge": PageController("ui.behaviours.forge", "ForgeController", ForgeControllerProtocol, ForgeVM),
    "takes": PageController("ui.behaviours.takes", "TakesController", TakesControllerProtocol, TakesVM),
    "settings": PageController("ui.behaviours.settings", "SettingsController", SettingsControllerProtocol,
                               SettingsVM),
}


class NotBuilt:
    """A page controller whose module does not exist yet: the page's default VM, intents that do nothing."""

    def __init__(self, protocol: type, vm_type: type) -> None:
        self.protocol = protocol
        self.vm = vm_type()
        for name, member in _protocol_methods(protocol).items():
            setattr(self, name, _noop(member))

    def close(self) -> None:
        """Nothing to drop."""


class BehaviourFactory:
    """One per page load: the client's controllers (memoized), its Arm and AskState, the KitPort."""

    def __init__(self, core: Core, client: Client | None, kit: KitPort, *, browser: bool = False) -> None:
        self.core = core
        self.client = client
        self.kit = kit
        self.browser = bool(browser)
        self.arm = Arm(core.scheduler)
        self.ask = AskState()
        self._built: dict[str, Any] = {}
        self._building: set[str] = set()
        self._closed = False
        if client is not None:
            client.on_delete(self.close)

    # ---------- the contract's Behaviours ----------

    def status(self) -> StatusController:
        return self._get("status", lambda: StatusController(self))

    def models(self) -> ModelsController:
        return self._get("models", lambda: ModelsController(self))

    def playback(self) -> PlaybackController:
        return self._get("playback", lambda: PlaybackController(self))

    def commands(self) -> CommandRegistry:
        return self._get("commands", lambda: CommandRegistry(self))

    def search(self) -> SearchController:
        return self._get("search", lambda: SearchController(self))

    def speak(self) -> Any:
        return self._page("speak")

    def voices(self) -> Any:
        return self._page("voices")

    def forge(self) -> Any:
        return self._page("forge")

    def takes(self) -> Any:
        return self._page("takes")

    def settings(self) -> Any:
        return self._page("settings")

    # ---------- wiring ----------

    def provide(self, name: str, controller: Any) -> None:
        """Use controller for name (tests inject a stub of another page's controller before first use)."""
        self._built[name] = controller

    def built(self, name: str) -> Any | None:
        """The controller already built for name, or None (never builds one)."""
        return self._built.get(name)

    def close(self) -> None:
        """The client went away: every controller drops its subscriptions and timers (idempotent)."""
        if self._closed:
            return
        self._closed = True
        for name, controller in list(self._built.items()):
            close = getattr(controller, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                log.warning("Closing the %s controller failed: %s", name, describe_error(exc))
        self.arm.disarm()
        self.ask.close()

    def _get(self, name: str, build: Callable[[], Any]) -> Any:
        found = self._built.get(name)
        if found is not None:
            return found
        if name in self._building:
            raise RuntimeError(f"the {name} controller needs itself while it is being built")
        self._building.add(name)
        try:
            controller = build()
        finally:
            self._building.discard(name)
        self._built[name] = controller
        return controller

    def _page(self, name: str) -> Any:
        return self._get(name, lambda: self._construct(PAGE_CONTROLLERS[name]))

    def _construct(self, spec: PageController) -> Any:
        try:
            module = importlib.import_module(spec.module)
        except ModuleNotFoundError as error:
            if error.name != spec.module:
                raise
            if spec.module not in _WARNED:
                _WARNED.add(spec.module)
                log.warning("%s is not built yet; using a placeholder controller", spec.module)
            return NotBuilt(spec.protocol, spec.vm)
        return getattr(module, spec.cls)(self)


def _protocol_methods(protocol: type) -> dict[str, Callable[..., Any]]:
    """The public methods a protocol and its protocol bases declare."""
    found: dict[str, Callable[..., Any]] = {}
    for cls in reversed(protocol.__mro__):
        if cls is object or not getattr(cls, "_is_protocol", False):
            continue
        found.update({name: member for name, member in vars(cls).items()
                      if inspect.isfunction(member) and not name.startswith("_")})
    return found


def _noop(member: Callable[..., Any]) -> Callable[..., Any]:
    """An intent with member's name and async-ness that does nothing and returns None."""
    async def async_intent(*args: Any, **kwargs: Any) -> None:
        return None

    def sync_intent(*args: Any, **kwargs: Any) -> None:
        return None

    intent = async_intent if inspect.iscoroutinefunction(member) else sync_intent
    intent.__name__ = member.__name__
    intent.__doc__ = f"Placeholder for {member.__qualname__} (the page is not built yet)."
    return intent
