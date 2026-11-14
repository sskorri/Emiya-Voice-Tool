"""Win32 probes for the native runs, from outside the app (ctypes only; lifted from spikes\\forge_fx\\winprobe.py,
spikes\\theme_boot\\probe.py and proctree.py).

Importing this module changes nothing process-wide: the DLL objects are private, so their argtypes never
reach other modules, and rectangles are read under a per-thread DPI context (physical pixels), never by
setting the process's DPI awareness. The rules it is written to keep:
  * processes are found by walking ParentProcessId down from a PID the harness started, with a
    creation-time check, so a recycled PID is never adopted; nothing is ever looked up by name;
  * a window counts only when its exact title matches AND its process is in that tree;
  * images come only from PrintWindow(hwnd, PW_RENDERFULLCONTENT) of such a window, never from the screen;
  * lifecycle changes use SW_SHOWMINNOACTIVE and posted SC_RESTORE, with the app's test-only activation veto.
"""
from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
import time
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from tests.native import clock

T = TypeVar("T")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
shcore = ctypes.WinDLL("shcore")

LONG_PTR = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


def _proto(function: Any, restype: Any, *argtypes: Any) -> Any:
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


# ---- processes ----------------------------------------------------------------------------------------

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ProcessCommandLineInformation = 60


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


_proto(kernel32.CreateToolhelp32Snapshot, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_proto(kernel32.Process32FirstW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_proto(kernel32.Process32NextW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_proto(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_proto(kernel32.GetProcessTimes, wintypes.BOOL, wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
_proto(kernel32.WaitForSingleObject, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
_proto(kernel32.TerminateProcess, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_proto(kernel32.GetExitCodeProcess, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_proto(kernel32.QueryProcessCycleTime, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong))
_proto(kernel32.QueryThreadCycleTime, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong))
_proto(kernel32.GetCurrentThread, wintypes.HANDLE)
_proto(kernel32.GetCurrentProcessId, wintypes.DWORD)
_proto(ntdll.NtQueryInformationProcess, ctypes.c_long, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
       wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))


def _ft(value: wintypes.FILETIME) -> int:
    return value.dwHighDateTime << 32 | value.dwLowDateTime


def list_processes(*, strict: bool = False) -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process, from one Toolhelp32 snapshot."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        if strict:
            raise ctypes.WinError(ctypes.get_last_error())
        return []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = []
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        if strict and (not out or ctypes.get_last_error() != 18):  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return out
    finally:
        kernel32.CloseHandle(snap)


def running_test_blocker() -> str | None:
    """Read exe names from a single snapshot without opening any external process."""
    blocked = {name.strip().casefold() for name in os.environ.get("VOICETOOL_TEST_BLOCK_PROCESSES", "").split(",")
               if name.strip()}
    if not blocked:
        return None
    try:
        processes = list_processes(strict=True)
    except OSError as exc:
        return f"cannot read process snapshot ({exc})"
    return next((name for _, _, name in processes if name.casefold() in blocked), None)


def command_line(handle: int) -> str | None:
    """A process's command line (NtQueryInformationProcess, ProcessCommandLineInformation)."""
    size = wintypes.ULONG(0)
    ntdll.NtQueryInformationProcess(handle, ProcessCommandLineInformation, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if ntdll.NtQueryInformationProcess(handle, ProcessCommandLineInformation, buf, size, ctypes.byref(size)) != 0:
        return None
    text = UNICODE_STRING.from_buffer(buf)
    return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""


def cycle_counter_hz(spin_s: float = 0.3, trials: int = 3) -> float:
    """The rate of the counter behind Query*CycleTime (the invariant TSC), from spinning this thread.

    Best of a few trials: preemption during a trial only makes that trial's rate look lower.
    """
    best = 0.0
    thread = kernel32.GetCurrentThread()
    for _ in range(trials):
        c0, c1 = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        kernel32.QueryThreadCycleTime(thread, ctypes.byref(c0))
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < spin_s:
            pass
        kernel32.QueryThreadCycleTime(thread, ctypes.byref(c1))
        best = max(best, (c1.value - c0.value) / (time.perf_counter() - t0))
    return best


@dataclass
class TrackedProcess:
    """A process of our tree, held open so its cycles and exit code stay readable after it ends."""

    pid: int
    ppid: int
    exe: str
    handle: int
    created: int                      # FILETIME of creation
    first_seen: float
    cmdline: str | None = None

    def cycles(self) -> int:
        value = ctypes.c_ulonglong()
        return int(value.value) if kernel32.QueryProcessCycleTime(self.handle, ctypes.byref(value)) else -1

    def cpu_seconds(self) -> float:
        """Kernel plus user CPU time, independent of changing processor clock frequency."""
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (_ft(times[2]) + _ft(times[3])) / 1e7

    def alive(self) -> bool:
        return kernel32.WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT

    def exit_code(self) -> int | None:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE and self.alive() else int(code.value)

    def summary(self) -> dict[str, Any]:
        return {"pid": self.pid, "ppid": self.ppid, "exe": self.exe, "alive": self.alive(),
                "exit_code": self.exit_code(),
                "webview_browser": bool(self.cmdline and "--type=" not in self.cmdline)}


def _open(pid: int) -> tuple[int, int] | None:
    access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    handle = kernel32.OpenProcess(access | PROCESS_TERMINATE, False, pid) or kernel32.OpenProcess(access, False, pid)
    if not handle:
        return None
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        kernel32.CloseHandle(handle)
        return None
    return int(handle), _ft(times[0])


class ProcessTree:
    """Every process descending from one root PID the harness started.

    refresh() walks ParentProcessId from the root; a child counts only if it was created after its
    parent, so a PID that Windows recycled is never adopted. Handles stay open until close().
    """

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self.procs: dict[int, TrackedProcess] = {}
        opened = _open(root_pid)
        if opened is None:
            raise RuntimeError(f"cannot open the root process {root_pid}")
        self.procs[root_pid] = TrackedProcess(root_pid, 0, "?", opened[0], opened[1], clock.now())

    @property
    def root(self) -> TrackedProcess:
        return self.procs[self.root_pid]

    def refresh(self) -> list[TrackedProcess]:
        """Adopt new descendants; return the ones found now."""
        children: dict[int, list[tuple[int, str]]] = {}
        for pid, ppid, exe in list_processes():
            children.setdefault(ppid, []).append((pid, exe))
            if pid == self.root_pid and self.root.exe == "?":
                self.root.exe = exe
        new: list[TrackedProcess] = []
        queue = list(self.procs)
        seen = set(queue)
        while queue:
            parent = self.procs.get(queue.pop())
            if parent is None:
                continue
            for pid, exe in children.get(parent.pid, []):
                if pid in seen:
                    continue
                seen.add(pid)
                if pid in self.procs:
                    queue.append(pid)
                    continue
                opened = _open(pid)
                if opened is None or opened[1] < parent.created:      # a recycled PID: not our child
                    if opened:
                        kernel32.CloseHandle(opened[0])
                    continue
                proc = TrackedProcess(pid, parent.pid, exe, opened[0], opened[1], clock.now())
                if exe.lower() == "msedgewebview2.exe":
                    proc.cmdline = command_line(proc.handle)
                self.procs[pid] = proc
                new.append(proc)
                queue.append(pid)
        return new

    def pids(self) -> set[int]:
        return set(self.procs)

    def alive(self) -> list[TrackedProcess]:
        return [proc for proc in self.procs.values() if proc.alive()]

    def cycles(self) -> int:
        """Cycles used so far by every process of the tree (ended ones included: their handles stay open)."""
        return sum(max(0, proc.cycles()) for proc in self.procs.values())

    def cpu_seconds(self) -> float:
        """Total CPU seconds for all owned descendants, including ones that have already exited."""
        return sum(proc.cpu_seconds() for proc in self.procs.values())

    def kill_all(self) -> list[int]:
        """TerminateProcess on every live member of this tree, leaves first. Only ever our own tree."""
        killed = []
        for proc in sorted(self.alive(), key=lambda p: -p.created):
            if kernel32.TerminateProcess(proc.handle, 1):
                killed.append(proc.pid)
        return killed

    def summary(self) -> list[dict[str, Any]]:
        return [proc.summary() for proc in sorted(self.procs.values(), key=lambda p: p.created)]

    def close(self) -> None:
        for proc in self.procs.values():
            kernel32.CloseHandle(proc.handle)
        self.procs.clear()


# ---- TCP endpoints ------------------------------------------------------------------------------------

TCP_TABLE_OWNER_PID_ALL = 5
AF_INET, AF_INET6 = 2, 23
TCP_STATES = {1: "CLOSED", 2: "LISTEN", 3: "SYN_SENT", 4: "SYN_RCVD", 5: "ESTABLISHED", 6: "FIN_WAIT1",
              7: "FIN_WAIT2", 8: "CLOSE_WAIT", 9: "CLOSING", 10: "LAST_ACK", 11: "TIME_WAIT", 12: "DELETE_TCB"}
_proto(iphlpapi.GetExtendedTcpTable, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
       wintypes.ULONG, ctypes.c_int, wintypes.ULONG)


def _tcp_table(family: int) -> bytes:
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            return buf.raw[:size.value]
    return b""


def tcp_endpoints(pids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """TCP endpoints (IPv4 and IPv6) with their owning PID; only those of `pids` when given."""
    wanted = None if pids is None else set(pids)
    out: list[dict[str, Any]] = []
    raw = _tcp_table(AF_INET)
    if raw:
        (count,) = struct.unpack_from("<I", raw, 0)
        for i in range(count):
            state, laddr, lport, raddr, rport, pid = struct.unpack_from("<6I", raw, 4 + i * 24)
            out.append({"pid": pid, "family": 4, "state": TCP_STATES.get(state, str(state)),
                        "local": socket.inet_ntoa(struct.pack("<I", laddr)), "lport": socket.ntohs(lport & 0xFFFF),
                        "remote": socket.inet_ntoa(struct.pack("<I", raddr)), "rport": socket.ntohs(rport & 0xFFFF)})
    raw = _tcp_table(AF_INET6)
    if raw:
        (count,) = struct.unpack_from("<I", raw, 0)
        for i in range(count):
            base = 4 + i * 56
            lport, = struct.unpack_from("<I", raw, base + 20)
            rport, state, pid = struct.unpack_from("<3I", raw, base + 44)
            out.append({"pid": pid, "family": 6, "state": TCP_STATES.get(state, str(state)),
                        "local": socket.inet_ntop(socket.AF_INET6, raw[base:base + 16]),
                        "lport": socket.ntohs(lport & 0xFFFF),
                        "remote": socket.inet_ntop(socket.AF_INET6, raw[base + 24:base + 40]),
                        "rport": socket.ntohs(rport & 0xFFFF)})
    return out if wanted is None else [e for e in out if e["pid"] in wanted]


def is_loopback(address: str) -> bool:
    return address.startswith("127.") or address in {"::1", "::ffff:127.0.0.1"}


def is_unspecified(address: str) -> bool:
    return address in {"0.0.0.0", "::"}


# ---- windows ------------------------------------------------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                     wintypes.LPARAM)

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX = 0x00C00000, 0x00040000, 0x00020000
WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x00000008, 0x08000000
WM_GETTEXT, WM_GETICON = 0x000D, 0x007F
ICON_SMALL, ICON_BIG = 0, 1
SMTO_ABORTIFHUNG = 0x0002
SW_SHOWMINNOACTIVE, SW_SHOWNOACTIVATE = 7, 4
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
PW_RENDERFULLCONTENT = 2
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
DI_NORMAL = 0x0003
DPI_PER_MONITOR_V2 = -4
WINFORMS_CLASS = "WindowsForms10."
CONSOLE_CLASS = "ConsoleWindowClass"


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_proto(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM)
_proto(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
_proto(user32.EnumDisplaySettingsW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW))
_proto(user32.MonitorFromWindow, wintypes.HMONITOR, wintypes.HWND, wintypes.DWORD)
_proto(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.SendMessageTimeoutW, LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ULONG_PTR))
_proto(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_proto(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
_proto(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
_proto(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
_proto(user32.GetWindowLongPtrW, LONG_PTR, wintypes.HWND, ctypes.c_int)
_proto(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.GetClientRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.ClientToScreen, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))
_proto(user32.GetDpiForWindow, wintypes.UINT, wintypes.HWND)
_proto(user32.GetForegroundWindow, wintypes.HWND)
_proto(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_proto(user32.ShowWindowAsync, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_proto(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_proto(user32.IsWindowEnabled, wintypes.BOOL, wintypes.HWND)
_proto(user32.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
_proto(user32.GetWindowDC, wintypes.HDC, wintypes.HWND)
_proto(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_proto(user32.PrintWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC, wintypes.UINT)
_proto(user32.DrawIconEx, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int,
       ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT)
_proto(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
       ctypes.c_int, wintypes.UINT)
_proto(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
_proto(shcore.GetDpiForMonitor, ctypes.c_long, wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
       ctypes.POINTER(wintypes.UINT))
_proto(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_proto(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
_proto(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
_proto(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_proto(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_proto(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_proto(gdi32.GdiFlush, wintypes.BOOL)
_proto(gdi32.GetDIBits, ctypes.c_int, wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
       ctypes.c_void_p, wintypes.UINT)


def physical(fn: Callable[[], T]) -> T:
    """Run fn with this thread per-monitor DPI aware (v2), so every rectangle is in physical pixels."""
    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_PER_MONITOR_V2))
    try:
        return fn()
    finally:
        if old:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def _rect(r: wintypes.RECT) -> list[int]:
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if user32.GetClassNameW(hwnd, buf, 256) else ""


def window_title(hwnd: int) -> str:
    """The title by WM_GETTEXT with a timeout (GetWindowTextW reads "" for another process's frameless window)."""
    buf = ctypes.create_unicode_buffer(512)
    copied = ULONG_PTR(0)
    if user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, 512, ctypes.addressof(buf), SMTO_ABORTIFHUNG, 200,
                                  ctypes.byref(copied)):
        return buf.value
    return ""


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def top_level_windows() -> list[int]:
    found: list[int] = []

    @WNDENUMPROC
    def collect(hwnd: int, _: int) -> bool:
        found.append(int(hwnd))
        return True

    user32.EnumWindows(collect, 0)
    return found


def app_windows(title: str, pids: set[int]) -> list[int]:
    """Top-level WinForms windows titled exactly `title` whose process is one of `pids`."""
    return [hwnd for hwnd in top_level_windows()
            if window_pid(hwnd) in pids and class_name(hwnd).startswith(WINFORMS_CLASS) and window_title(hwnd) == title]


def console_windows(pids: set[int]) -> list[dict[str, Any]]:
    """ConsoleWindowClass windows owned by `pids` (visible or not)."""
    return [{"hwnd": hwnd, "pid": window_pid(hwnd), "visible": bool(user32.IsWindowVisible(hwnd))}
            for hwnd in top_level_windows() if class_name(hwnd) == CONSOLE_CLASS and window_pid(hwnd) in pids]


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def show_minimized_no_activate(hwnd: int) -> bool:
    """ShowWindow(SW_SHOWMINNOACTIVE): minimize without activating anything."""
    return bool(user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE))


def show_no_activate(hwnd: int) -> bool:
    """Post a real restore system command to the GUI thread, where its CBT activation veto runs."""
    return bool(user32.PostMessageW(hwnd, 0x0112, 0xF120, 0))  # WM_SYSCOMMAND / SC_RESTORE


def keep_foreground_if_empty(hwnd: int) -> bool:
    """Keep the pre-restore foreground if Windows left none; never override another chosen window."""
    return bool(hwnd and not foreground_window() and user32.SetForegroundWindow(hwnd))


def _monitor(handle: int) -> dict[str, Any]:
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    user32.GetMonitorInfoW(handle, ctypes.byref(info))
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    user32.EnumDisplaySettingsW(info.szDevice, ENUM_CURRENT_SETTINGS, ctypes.byref(mode))
    dx, dy = wintypes.UINT(0), wintypes.UINT(0)
    dpi = int(dx.value) if shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dx),
                                                    ctypes.byref(dy)) == 0 else 96
    return {"device": info.szDevice, "primary": bool(info.dwFlags & 1), "rect": _rect(info.rcMonitor),
            "work": _rect(info.rcWork), "dpi": dpi or 96, "hz": int(mode.dmDisplayFrequency)}


def monitors() -> list[dict[str, Any]]:
    """Every display: device, primary, rectangle and work area (physical px), DPI and refresh rate."""
    handles: list[int] = []

    @MONITORENUMPROC
    def collect(handle: int, _hdc: int, _rect_ptr: Any, _: int) -> bool:
        handles.append(int(handle))
        return True

    return physical(lambda: (user32.EnumDisplayMonitors(None, None, collect, 0), [_monitor(h) for h in handles])[1])


def window_info(hwnd: int) -> dict[str, Any]:
    """Chrome, geometry (physical px and logical), DPI and the monitor (with its refresh rate) of a window."""
    def read() -> dict[str, Any]:
        rect, client = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE)) & 0xFFFFFFFF
        exstyle = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
        dpi = int(user32.GetDpiForWindow(hwnd) or 96)
        scale = dpi / 96
        client_px = [client.right - client.left, client.bottom - client.top]
        return {
            "hwnd": hwnd, "pid": window_pid(hwnd), "class": class_name(hwnd), "title": window_title(hwnd),
            "rect": _rect(rect), "client_origin": [origin.x, origin.y], "client_px": client_px,
            "client_logical": [round(v / scale, 1) for v in client_px], "dpi": dpi,
            "style": f"0x{style:08X}", "has_caption": (style & WS_CAPTION) == WS_CAPTION,
            "has_thickframe": bool(style & WS_THICKFRAME),
            "has_minimizebox": bool(style & WS_MINIMIZEBOX),
            "exstyle": f"0x{exstyle:08X}", "topmost": bool(exstyle & WS_EX_TOPMOST),
            "noactivate": bool(exstyle & WS_EX_NOACTIVATE),
            "visible": bool(user32.IsWindowVisible(hwnd)), "iconic": bool(user32.IsIconic(hwnd)),
            "enabled": bool(user32.IsWindowEnabled(hwnd)),
            "foreground": foreground_window() == hwnd,
            "monitor": _monitor(int(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))),
        }
    return physical(read)


# ---- icons --------------------------------------------------------------------------------------------

def _render_icon(hicon: int, size: int) -> bytes:
    """Draw an HICON at size x size onto magenta in a memory DIB; BGRA bytes."""
    hdc = gdi32.CreateCompatibleDC(None)
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size, biPlanes=1,
                              biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, bitmap)
    try:
        ctypes.memmove(bits, bytes([255, 0, 255, 255]) * (size * size), size * size * 4)
        user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, DI_NORMAL)
        gdi32.GdiFlush()
        return ctypes.string_at(bits, size * size * 4)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(hdc)


def _distance(a: bytes, b: bytes) -> float:
    """Mean absolute difference per colour channel (alpha skipped), 0-255."""
    total = sum(abs(a[i] - b[i]) for i in range(len(a)) if i % 4 != 3)
    return total / (len(a) * 3 / 4)


def window_icon(hwnd: int, which: int) -> int:
    result = ULONG_PTR(0)
    if user32.SendMessageTimeoutW(hwnd, WM_GETICON, which, 0, SMTO_ABORTIFHUNG, 300, ctypes.byref(result)):
        return int(result.value)
    return 0


def icon_distance(hwnd: int, which: int, ico: Path, size: int) -> float | None:
    """How far the window's ICON_BIG / ICON_SMALL, drawn at `size`, is from the .ico's `size` px frame."""
    hicon = window_icon(hwnd, which)
    if not hicon:
        return None
    frame = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
    if not frame:
        return None
    try:
        return round(_distance(_render_icon(hicon, size), _render_icon(frame, size)), 3)
    finally:
        user32.DestroyIcon(frame)


# ---- captures -----------------------------------------------------------------------------------------

def capture_window(hwnd: int) -> Any:
    """PrintWindow(hwnd, PW_RENDERFULLCONTENT) of our own window into a bitmap; a PIL RGB image."""
    from PIL import Image

    def grab() -> Any:
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        window_dc = user32.GetWindowDC(hwnd)
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            ok = user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
            header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                      biPlanes=1, biBitCount=32, biCompression=0)
            pixels = ctypes.create_string_buffer(width * height * 4)
            lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(header), 0)
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
        if not ok or lines != height:
            raise RuntimeError(f"PrintWindow failed (ok={ok}, lines={lines})")
        return Image.frombuffer("RGBA", (width, height), pixels.raw, "raw", "BGRA", 0, 1).convert("RGB")

    return physical(grab)


def image_stats(image: Any) -> dict[str, Any]:
    """Size, mean, spread and distinct colours (of a 1/8 thumbnail): a blank capture has a spread near 0."""
    from PIL import ImageStat

    stat = ImageStat.Stat(image.convert("L"))
    thumb = image.resize((max(1, image.width // 8), max(1, image.height // 8)))
    colours = thumb.getcolors(maxcolors=1 << 16)
    return {"size": list(image.size), "mean": round(stat.mean[0], 2), "stddev": round(stat.stddev[0], 2),
            "colours": len(colours) if colours is not None else 1 << 16}


def is_blank(stats: dict[str, Any]) -> bool:
    return stats["stddev"] < 2.0 or stats["colours"] < 8


def platform_ok() -> bool:
    return sys.platform == "win32"
