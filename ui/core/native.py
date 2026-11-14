"""Native window events (§4.17): drop → events.dropped, minimized / restored → WindowState.visible.

NiceGUI keeps native event handlers process-wide and never forgets them (app.reset() leaves app.native
alone), while the user fixture builds a new Core for every test. So the handlers are registered with
app.native once per process, and register() only points them at the newest Core (held weakly, so an
old Core is never kept alive or written to). The events arrive on the event loop (NG/native/
event_manager.py L41-42); NiceGUI already prevents the browser default for a dropped file.
"""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import Core

__all__ = ["NativeBridge", "register"]


class NativeBridge:
    """The process's native window handlers, forwarding to one Core."""

    def __init__(self) -> None:
        self._target: weakref.ref[Core] | None = None
        self._registered: set[int] = set()

    def register(self, app: Any, core: Core) -> None:
        """Point the handlers at core; register them with app.native the first time."""
        self._target = weakref.ref(core)
        key = id(app.native)
        if key in self._registered:
            return
        app.native.on("drop", self.drop)
        app.native.on("minimized", self.minimized)
        app.native.on("restored", self.restored)
        self._registered.add(key)

    def core(self) -> Core | None:
        return self._target() if self._target is not None else None

    def drop(self, e: Any) -> None:
        """Dropped files: their full paths (pywebviewFullPath), empty names left out."""
        core = self.core()
        files = getattr(e, "args", {}).get("files", []) if e is not None else []
        paths = tuple(str(path) for path in files or () if path)
        if core is not None and paths:
            core.events.dropped.emit(paths)

    def minimized(self) -> None:
        self._visible(False)

    def restored(self) -> None:
        self._visible(True)

    def _visible(self, visible: bool) -> None:
        core = self.core()
        if core is not None and core.state.window.visible != visible:
            core.state.window.visible = visible
            core.events.notify()


_BRIDGE = NativeBridge()                 # mirrors NiceGUI's own process-wide native event manager


def register(app: Any, core: Core) -> None:
    """launch.py: route app.native's drop, minimized and restored events to core (safe to repeat)."""
    _BRIDGE.register(app, core)
