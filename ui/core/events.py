"""The core's events (§4.11): what changed, toasts, flashes and sparks, finished takes and forges, drops.

Only shell.py and the behaviour controllers subscribe (R4); a controller subscribes inside its client
context, so NiceGUI drops the subscription with the client. `changed` is coalesced: any number of
notify() calls in one loop tick give one emit. Toasts raised before the first client connected (a
corrupt takes index or registry at startup) are held and shown when it does (release()).
"""
from __future__ import annotations

import asyncio

from nicegui import Event

from ui.contract.plans import Toast

__all__ = ["CoreEvents"]


class CoreEvents:
    """The eight core events plus coalesced change notification and numbered toasts."""

    def __init__(self) -> None:
        self.changed: Event[[]] = Event()                   # any mirror changed (coalesced per loop tick)
        self.toast: Event[[Toast]] = Event()                # Toast(text, bad, seq)
        self.flash: Event[[str]] = Event()                  # "dock-<backend>", "voice-<id>", "take-<id>"
        self.spark: Event[[str]] = Event()                  # "dock-<backend>", "take"
        self.take_ready: Event[[int]] = Event()             # take id
        self.call_started: Event[[int]] = Event()           # physical output accepted the first buffer
        self.call_ended: Event[[int]] = Event()             # physical output finished or was stopped
        self.forge_started: Event[[str]] = Event()          # voice id (the FX blade)
        self.forged: Event[[str]] = Event()                 # voice id
        self.dropped: Event[[tuple[str, ...]]] = Event()    # native drop: full paths
        self.last_toast: Toast | None = None
        self._seq = 0
        self._pending = False
        self._held: list[Toast] | None = []                 # None once the first client is there

    def notify(self) -> None:
        """Ask for one `changed` emit on the next loop tick (emits at once outside a loop)."""
        if self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.changed.emit()
            return
        self._pending = True
        loop.call_soon(self._flush)

    def say(self, text: str, *, bad: bool = False) -> Toast:
        """Raise a toast with the next sequence number (held until the first client connects)."""
        self._seq += 1
        toast = Toast(text=text, bad=bad, seq=self._seq)
        self.last_toast = toast
        if self._held is not None:
            self._held.append(toast)
        else:
            self.toast.emit(toast)
        return toast

    @property
    def held(self) -> tuple[Toast, ...]:
        """Toasts waiting for the first client."""
        return tuple(self._held or ())

    def release(self) -> None:
        """The first client is here: from now on toasts go out at once; held ones on the next tick."""
        held, self._held = self._held, None
        if not held:
            return
        try:
            asyncio.get_running_loop().call_soon(self._emit_all, held)
        except RuntimeError:
            self._emit_all(held)

    def _emit_all(self, toasts: list[Toast]) -> None:
        for toast in toasts:
            self.toast.emit(toast)

    def _flush(self) -> None:
        self._pending = False
        self.changed.emit()
