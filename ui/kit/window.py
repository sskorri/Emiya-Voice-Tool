"""The native window: window-first quit, minimize, the drag class and the lock for window requests.

Every awaiting WindowProxy call (file dialogs, evaluate_js, get_size, ...) reads its answer from the
one response queue NiceGUI shares between all of them (nicegui/native/native.py L143-154), so two
requests in flight can take each other's answers. They run one at a time behind `native_lock`.

quit_app() closes the window first: NiceGUI's native monitor then runs the on_shutdown handlers and
exits with code 0. app.shutdown() runs only as a delayed fallback, because on its own it can lose a
race with the closing websocket and exit with code 1 (spikes/theme_boot/boot.py L292-311).
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING

from nicegui import app, background_tasks
from nicegui.server import Server

if TYPE_CHECKING:
    from nicegui.native.native import WindowProxy

DRAG_CLASS = "pywebview-drag-region"   # pywebview's DRAG_REGION_SELECTOR; gui.py sets DIRECT_TARGET_ONLY
SHUTDOWN_FALLBACK_S = 5.0

log = logging.getLogger("voicetool.kit")


class NativeLock:
    """Serialises native window requests: one asyncio.Lock per event loop.

    Per loop, so tests that each run their own loop never meet a lock bound to a closed one.
    Use it as `async with native_lock:` around every awaiting WindowProxy call.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary())

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def locked(self) -> bool:
        """True while a request holds the lock on the running loop; False outside a loop."""
        try:
            return self._lock().locked()
        except RuntimeError:
            return False

    async def __aenter__(self) -> None:
        await self._lock().acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self._lock().release()


native_lock = NativeLock()


def quit_app() -> None:
    """Quit window-first; without a native window (--browser) stop the server.

    A dialog that is still open is waited for: closing the window under it would leave NiceGUI's
    shutdown waiting on the dialog's thread (nicegui/native/native_mode.py L142-146).
    """
    window = app.native.main_window
    if window is None:
        _stop_server()
    elif native_lock.locked():
        background_tasks.create(_quit_after_requests(), name="quit after the open dialog")
    else:
        _close(window)


def minimize() -> bool:
    """Minimize the native window; False when there is none (--browser, the user fixture)."""
    window = app.native.main_window
    if window is None:
        return False
    window.minimize()
    return True


async def _quit_after_requests() -> None:
    async with native_lock:
        pass
    window = app.native.main_window
    if window is None:
        _stop_server()
    else:
        _close(window)


def _close(window: WindowProxy) -> None:
    log.info("Closing the window")
    window.destroy()
    app.timer(SHUTDOWN_FALLBACK_S, _stop_server, once=True)


def _stop_server() -> None:
    """app.shutdown(), when a server runs; the NiceGUI user fixture has none, so then nothing."""
    if getattr(Server, "instance", None) is None:
        log.info("Quit requested, but no server is running")
        return
    app.shutdown()
