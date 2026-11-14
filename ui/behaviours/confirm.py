"""Confirmations (§5.1) and the per-client plumbing the controllers share.

- Arm: click twice. The first press of a key changes nothing but the armed label; a second press with
  the same scope acts after a quiet half second. The arm expires after 3 s on core.scheduler.
- AskState: the one open "ask" per client, the switch popover. Its request is redrawn by
  ModelsController while it is open; "Don't ask again" is UI state until the primary (D4).
- Listeners and Subscriptions: change fan-out and core Event subscriptions that a controller can drop
  in one call (BehaviourFactory.close() does that when the client goes away).

Both Arm and AskState live once per BehaviourFactory, so arm keys and the popover are per client.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nicegui import Event

    from ui.contract.plans import ConfirmRequest
    from ui.core.clock import Handle, Scheduler

__all__ = [
    "ARM_CLEAR",
    "ARM_DELETE",
    "ARM_PURGE",
    "ARM_QUIT",
    "ARM_REPLACE",
    "ARM_WINDOW_S",
    "Arm",
    "AskState",
    "Listeners",
    "Subscriptions",
]

ARM_WINDOW_S = 3.0                       # M L1745, L1762
ARM_PAUSE_S = 0.5                        # a quiet interval between deliberate presses
ARM_QUIT = "quit"                        # Close while busy (§5.2)
ARM_CLEAR = "clear"                      # Speak's Clear (P1)
ARM_REPLACE = "replace"                  # Forge with an existing name on the same backend (P3)
ARM_DELETE = "delete"                    # Voices' Delete (P2)
ARM_PURGE = "purge"                      # Takes' Remove Unstarred (P4)

log = logging.getLogger("voicetool.behaviours")

Listener = Callable[[], Any]


class Listeners:
    """Callbacks to run after a change; a failing callback is logged as text and never stops the others."""

    def __init__(self, first: Listener | None = None) -> None:
        self._callbacks: list[Listener] = [first] if first is not None else []

    def add(self, callback: Listener) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove(self, callback: Listener) -> None:
        self._callbacks = [known for known in self._callbacks if known != callback]

    def notify(self) -> None:
        for callback in list(self._callbacks):
            try:
                callback()
            except Exception as exc:     # a broken listener must not break the intent that changed state
                log.warning("A behaviour listener failed: %s", _text(exc))


class Subscriptions:
    """Core Event subscriptions of one controller, dropped together by close()."""

    def __init__(self) -> None:
        self._made: list[tuple[Event[Any], Callable[..., Any]]] = []

    def add(self, event: Event[Any], callback: Callable[..., Any]) -> None:
        event.subscribe(callback)
        self._made.append((event, callback))

    def close(self) -> None:
        for event, callback in self._made:
            event.unsubscribe(callback)
        self._made.clear()


@dataclass(frozen=True)
class _Armed:
    scope: Hashable
    at: float
    handle: Handle
    last_press: float


class Arm:
    """Click-twice confirmations by key ("quit", "clear", "replace", "delete", "purge"), per client."""

    def __init__(self, scheduler: Scheduler, on_change: Listener | None = None, *,
                 window_s: float = ARM_WINDOW_S, pause_s: float = ARM_PAUSE_S) -> None:
        self._scheduler = scheduler
        self._window_s = float(window_s)
        self._pause_s = pause_s
        self._acted: dict[str, float] = {}
        self._armed: dict[str, _Armed] = {}
        self.listeners = Listeners(on_change)

    def press(self, key: str, *, scope: Hashable = None) -> bool | None:
        """True when confirmed, False when newly armed, None when a repeat is ignored."""
        now = self._scheduler.now()
        if key in self._acted:
            if now <= self._acted[key] + self._pause_s:
                self._acted[key] = now
                return None
            del self._acted[key]
        entry = self._armed.get(key)
        if entry is not None and entry.scope == scope and now - entry.at < self._window_s:
            if self._pause_s and now <= entry.last_press + self._pause_s:
                self._armed[key] = _Armed(scope, entry.at, entry.handle, now)
                return None
            self._drop(key)
            if self._pause_s:
                self._acted[key] = now
            self.listeners.notify()
            return True
        self._drop(key)
        self._armed[key] = _Armed(scope, now, self._expiry(key), now)
        self.listeners.notify()
        return False

    def is_armed(self, key: str) -> bool:
        return key in self._armed

    def scope(self, key: str) -> Hashable:
        """The scope the key was armed with (None when not armed)."""
        entry = self._armed.get(key)
        return None if entry is None else entry.scope

    @property
    def armed(self) -> tuple[str, ...]:
        """The keys armed now."""
        return tuple(self._armed)

    def disarm(self, key: str | None = None) -> None:
        """Disarm one key, or every key (None)."""
        keys = list(self._armed) if key is None else [key] if key in self._armed else []
        for name in keys:
            self._drop(name)
        if keys:
            self.listeners.notify()

    def _expiry(self, key: str) -> Handle:
        def expire() -> None:
            entry = self._armed.get(key)
            if entry is not None and entry.handle is handle:
                del self._armed[key]
                self.listeners.notify()

        handle = self._scheduler.later(self._window_s, expire)
        return handle

    def _drop(self, key: str) -> None:
        entry = self._armed.pop(key, None)
        if entry is not None:
            entry.handle.cancel()


class AskState:
    """The one open switch popover of a client: the request it shows and its "Don't ask again" tick."""

    def __init__(self, on_change: Listener | None = None) -> None:
        self.request: ConfirmRequest | None = None
        self.dont = False                # the tick (committed only by the primary, D4)
        self.dont_visible = False        # only on an ask caused by Confirm Model Switches (D20)
        self.listeners = Listeners(on_change)

    @property
    def is_open(self) -> bool:
        return self.request is not None

    def open(self, req: ConfirmRequest, *, dont_visible: bool = False) -> None:
        """Show req (replacing any open ask); the tick starts off."""
        self.request = req
        self.dont = False
        self.dont_visible = bool(dont_visible)
        self.listeners.notify()

    def replace(self, req: ConfirmRequest, *, dont_visible: bool | None = None) -> None:
        """Redraw the open ask (live copy or a renewed token); the tick stays.

        dont_visible, when given, shows or hides "Don't ask again" (an open ask that turned into a
        low-VRAM ask loses it, D20); hiding it also clears the tick, so the primary cannot commit it.
        """
        if self.request is None:
            return
        changed = req != self.request
        if dont_visible is not None and bool(dont_visible) != self.dont_visible:
            self.dont_visible = bool(dont_visible)
            self.dont = self.dont and self.dont_visible
            changed = True
        if changed:
            self.request = req
            self.listeners.notify()

    def set_dont(self, on: bool) -> None:
        if self.request is not None and self.dont_visible and self.dont != bool(on):
            self.dont = bool(on)
            self.listeners.notify()

    def close(self) -> None:
        if self.request is None:
            return
        self.request = None
        self.dont = False
        self.dont_visible = False
        self.listeners.notify()


def _text(exc: BaseException) -> str:
    first = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    return f"{type(exc).__name__}: {first[:200]}" if first else type(exc).__name__
