"""A precise wall clock shared by the harness and the app (lifted from spikes\\theme_boot\\clock.py).

time.time() on Python 3.11 for Windows advances in timer ticks (15.6 ms nominal);
GetSystemTimePreciseAsFileTime has sub-microsecond steps on the same epoch, so timestamps written by
the app's smoke plan and read by the harness subtract directly. Standard library only.
"""
from __future__ import annotations

import ctypes
import sys
import time

_UNIX_EPOCH_AS_FILETIME = 116_444_736_000_000_000     # 1970-01-01 in 100 ns ticks since 1601-01-01

if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32")
    _kernel32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    _kernel32.GetSystemTimePreciseAsFileTime.restype = None
else:  # pragma: no cover - the native runs are Windows only
    _kernel32 = None


def filetime_to_unix(ticks: int) -> float:
    """A FILETIME tick count (100 ns since 1601) in Unix seconds."""
    return (ticks - _UNIX_EPOCH_AS_FILETIME) / 1e7


def now() -> float:
    """Wall-clock Unix seconds with sub-microsecond resolution."""
    if _kernel32 is None:  # pragma: no cover
        return time.time()
    ft = wintypes.FILETIME()
    _kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
    return filetime_to_unix((ft.dwHighDateTime << 32) | ft.dwLowDateTime)
