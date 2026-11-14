"""Call hotkeys on a windowless Windows message thread. The event loop owns configuration."""
from __future__ import annotations

import asyncio
import ctypes as ct
import threading
from collections.abc import Callable, Mapping
from ctypes import wintypes as wt

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_NO_REPEAT = 0x4000
_MODS = {"alt": 1, "ctrl": 2, "shift": 4}
_KEYS = {
    "Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8, "Delete": 46, "Insert": 45,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34, "Left": 37, "Up": 38, "Right": 39,
    "Down": 40, "Space": 32, **{f"F{i}": 111+i for i in range(1,25)},
}


class _MSG(ct.Structure):
    # The SDK's tail DWORD is absent from ctypes.wintypes.MSG on 32-bit Python.
    _fields_ = [*wt.MSG._fields_, ("lPrivate", wt.DWORD)]


def _hotkey(combo: str, scan: Callable[[str], int]) -> tuple[int, int]:
    """Translate the stored KeyboardEvent spelling using the user's Windows keyboard layout."""
    if combo == "+" or combo.endswith("++"):
        modifiers = combo[:-2].split("+") if len(combo) > 2 else []
        key = "+"
    else:
        *modifiers, key = combo.split("+")
    flags = 0
    for mod in modifiers:
        if mod.casefold() not in _MODS:
            raise ValueError("unsupported modifier")
        flags |= _MODS[mod.casefold()]
    if key == "F12":
        raise ValueError("F12 is reserved for the Windows debugger")
    if key in _KEYS:
        vk = _KEYS[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif len(key) == 1 and ord(key) <= 0xffff:
        code = scan(key)
        if code == -1 or (code >> 8) & ~7:
            raise ValueError("unsupported on this keyboard layout")
        vk, shift = code & 0xff, code >> 8
        flags |= ((shift & 1) << 2) | (shift & 2) | ((shift & 4) >> 2)
    else:
        raise ValueError("unsupported global key")
    return flags | _NO_REPEAT, vk


class _MessageThread:
    def __init__(self):
        self.user = ct.WinDLL("user32", use_last_error=True)
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = wt.DWORD
        self.thread_id = kernel.GetCurrentThreadId()
        self.msg = _MSG()
        self.user.PeekMessageW.argtypes = [ct.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
        self.user.GetMessageW.argtypes = [ct.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT]
        self.user.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
        self.user.RegisterHotKey.argtypes = [wt.HWND, ct.c_int, wt.UINT, wt.UINT]
        self.user.UnregisterHotKey.argtypes = [wt.HWND, ct.c_int]
        self.user.VkKeyScanW.argtypes = [wt.WCHAR]
        self.user.VkKeyScanW.restype = ct.c_short
        self.user.PeekMessageW(ct.byref(self.msg), None, 0, 0, 0)  # create this thread's queue before post()

    def register(self, ident: int, combo: str) -> None:
        flags, vk = _hotkey(combo, self.user.VkKeyScanW)
        if not self.user.RegisterHotKey(None, ident, flags, vk):
            error = ct.get_last_error()
            if error == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
                raise OSError("already registered by another application")
            raise ct.WinError(error)

    def unregister(self, ident: int) -> None:
        if not self.user.UnregisterHotKey(None, ident):
            raise ct.WinError(ct.get_last_error())

    def receive(self) -> int | None:
        while True:
            result = self.user.GetMessageW(ct.byref(self.msg), None, 0, 0)
            if result == 0:
                return None
            if result < 0:
                raise ct.WinError(ct.get_last_error())
            if self.msg.message == _WM_HOTKEY:
                return int(self.msg.wParam)

    def quit(self) -> None:
        if not self.user.PostThreadMessageW(self.thread_id, _WM_QUIT, 0, 0):
            raise ct.WinError(ct.get_last_error())


class GlobalHotkeys:
    """Use a fresh thread/queue for each configuration. Queued callbacks carry its revision."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_command: Callable[[str], None],
                 on_error: Callable[[str], None], *, driver_factory=_MessageThread):
        self._loop = loop
        self._command = on_command
        self._error = on_error
        self._driver_factory = driver_factory
        self._bindings: dict[str, str] = {}
        self._revision = 0
        self._driver = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def set_bindings(self, bindings: Mapping[str, str]) -> None:
        bindings = dict(bindings)
        if self._closed or bindings == self._bindings:
            return
        self._revision += 1
        self._stop_thread()
        self._bindings = bindings
        self._error("")
        if bindings:
            ready = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._revision, bindings, ready),
                                            name="Voice Tool call hotkeys", daemon=True)
            self._thread.start()
            ready.wait()  # Until the message queue exists. No model, registration or I/O wait.

    def _run(self, revision: int, bindings: dict[str, str], ready: threading.Event) -> None:
        active = {}
        try:
            driver = self._driver_factory()
            self._driver = driver
            ready.set()
            failures = []
            for ident, (combo, command) in enumerate(bindings.items(), 1):
                try:
                    driver.register(ident, combo)
                except (ValueError, OSError) as exc:
                    failures.append(f"{combo}: {exc}")
                else:
                    active[ident] = command
            if failures:
                self._post(revision, self._error, "Couldn't register global call hotkey · " + " · ".join(failures))
            while (key := driver.receive()) is not None:
                if key in active:
                    self._post(revision, self._command, active[key])
        except Exception as exc:
            self._post(revision, self._error, f"Global call hotkeys unavailable · {exc}")
        finally:
            ready.set()
            for ident in active:
                try:
                    driver.unregister(ident)
                except OSError as exc:
                    self._post(revision, self._error, f"Couldn't release global call hotkey · {exc}")
            # Windows also releases thread-bound hotkeys when the owning thread exits.

    def _post(self, revision: int, callback: Callable[[str], None], value: str) -> None:
        def deliver():
            if not self._closed and self._revision == revision:
                callback(value)

        self._loop.call_soon_threadsafe(deliver)

    def _stop_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._driver is not None:
                self._driver.quit()
            self._thread.join()
        self._thread = None
        self._driver = None

    def close(self) -> None:
        self._closed = True
        self._revision += 1
        self._stop_thread()
