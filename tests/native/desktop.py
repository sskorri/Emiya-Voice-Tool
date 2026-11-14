"""Create native-test processes on an unshown Win32 desktop. Never switch the input desktop."""
from __future__ import annotations

import ctypes as ct
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import Path

user32 = ct.WinDLL("user32", use_last_error=True)
kernel32 = ct.WinDLL("kernel32", use_last_error=True)
DESKTOP_ENV = "VOICETOOL_HARNESS_DESKTOP"
READ_OBJECTS = 0x0001


class StartupInfo(ct.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ct.c_void_p),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]


class ProcessInfo(ct.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD)]


def _bind(fn, result, *args):
    fn.restype, fn.argtypes = result, args
    return fn


create_desktop = _bind(user32.CreateDesktopW, wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR, ct.c_void_p, wt.DWORD,
                       wt.DWORD, ct.c_void_p)
open_input = _bind(user32.OpenInputDesktop, wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
thread_desktop = _bind(user32.GetThreadDesktop, wt.HANDLE, wt.DWORD)
thread_id = _bind(kernel32.GetCurrentThreadId, wt.DWORD)
object_info = _bind(user32.GetUserObjectInformationW, wt.BOOL, wt.HANDLE, ct.c_int, ct.c_void_p,
                    wt.DWORD, ct.POINTER(wt.DWORD))
set_desktop = _bind(user32.SetThreadDesktop, wt.BOOL, wt.HANDLE)
close_desktop = _bind(user32.CloseDesktop, wt.BOOL, wt.HANDLE)
close_handle = _bind(kernel32.CloseHandle, wt.BOOL, wt.HANDLE)
create_process = _bind(kernel32.CreateProcessW, wt.BOOL, wt.LPCWSTR, wt.LPWSTR, ct.c_void_p, ct.c_void_p,
                       wt.BOOL, wt.DWORD, ct.c_void_p, wt.LPCWSTR, ct.POINTER(StartupInfo), ct.POINTER(ProcessInfo))
wait_handle = _bind(kernel32.WaitForSingleObject, wt.DWORD, wt.HANDLE, wt.DWORD)
exit_code = _bind(kernel32.GetExitCodeProcess, wt.BOOL, wt.HANDLE, ct.POINTER(wt.DWORD))
terminate_process = _bind(kernel32.TerminateProcess, wt.BOOL, wt.HANDLE, wt.UINT)


def _required(value):
    if not value:
        raise ct.WinError(ct.get_last_error())
    return value


def _name(handle: int) -> str:
    buffer = ct.create_unicode_buffer(512)
    needed = wt.DWORD()
    _required(object_info(_required(handle), 2, buffer, ct.sizeof(buffer), ct.byref(needed)))  # UOI_NAME
    return buffer.value


def input_name() -> str:
    handle = _required(open_input(0, False, READ_OBJECTS))
    try:
        return _name(handle)
    finally:
        close_desktop(handle)


def current_name() -> str:
    return _name(thread_desktop(thread_id()))


def require_private(expected: str | None) -> None:
    """Fail before any application code can initialize WinForms or WebView."""
    if not expected or not expected.startswith("VoiceTool-test-"):
        raise RuntimeError("A private native test desktop is required")
    if current_name() != expected or input_name().casefold() == expected.casefold():
        raise RuntimeError("Native test is outside its private desktop")


class Child:
    def __init__(self, info: ProcessInfo):
        self.handle, self.pid = info.hProcess, info.dwProcessId
        self.returncode: int | None = None
        close_handle(info.hThread)

    def wait(self, timeout: float = 0) -> int:
        result = wait_handle(self.handle, int(timeout * 1000))
        if result == 258:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if result != 0:
            raise ct.WinError(ct.get_last_error())
        code = wt.DWORD()
        _required(exit_code(self.handle, ct.byref(code)))
        self.returncode = code.value
        return self.returncode

    def poll(self) -> int | None:
        try:
            return self.wait()
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        close_handle(self.handle)

    def terminate(self) -> None:
        _required(terminate_process(self.handle, 1))


class TestDesktop:
    def __init__(self):
        self.name = "VoiceTool-test-" + uuid.uuid4().hex
        self.input = input_name()
        # READOBJECTS | CREATEWINDOW | CREATEMENU | HOOKCONTROL | ENUMERATE | WRITEOBJECTS.
        # No SWITCHDESKTOP access. The handle is not inherited.
        self.handle = _required(create_desktop(self.name, None, None, 0, 0x00CF, None))

    @contextmanager
    def attached(self):
        original = _required(thread_desktop(thread_id()))
        _required(set_desktop(self.handle))
        try:
            require_private(self.name)
            yield
        finally:
            # SetThreadDesktop binds only this worker. Restore its original thread desktop before
            # returning; CloseDesktop fails with ERROR_BUSY while any local thread still uses it.
            _required(set_desktop(original))

    def start(self, args: list[str], cwd: Path, env: dict[str, str]) -> Child:
        require_private(self.name)
        # Python 3.11 subprocess.STARTUPINFO does not forward lpDesktop to CreateProcessW.
        info = StartupInfo(cb=ct.sizeof(StartupInfo), lpDesktop=self.name)
        proc = ProcessInfo()
        command = ct.create_unicode_buffer(subprocess.list2cmdline(args))
        entries = {**env, DESKTOP_ENV: self.name}
        block = ct.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in
                                                    sorted(entries.items(), key=lambda pair: pair[0].upper())) + "\0")
        _required(create_process(args[0], command, None, None, False,
                                 subprocess.CREATE_NO_WINDOW | 0x400, block, os.fspath(cwd),
                                 ct.byref(info), ct.byref(proc)))  # CREATE_UNICODE_ENVIRONMENT
        return Child(proc)

    def close(self) -> None:
        # USER32 can retain the thread binding briefly while worker teardown completes.
        deadline = time.monotonic() + 2
        while not close_desktop(self.handle):
            error = ct.get_last_error()
            if error != 170 or time.monotonic() >= deadline:  # ERROR_BUSY
                raise ct.WinError(error)
            time.sleep(.025)
