"""No-window regressions for platform boot subprocess flags and ordered native icon writes."""
from __future__ import annotations

import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ui import entry


@pytest.mark.skipif(sys.platform != "win32", reason="Windows platform probe creation flags")
def test_platform_detection_creates_no_console_and_restores_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def output(*args, **kwargs):
        calls.append((args, kwargs))
        return "Microsoft Windows [Version 10.0.26100]"

    def uname():
        assert subprocess.check_output("ver", shell=True, creationflags=8).startswith("Microsoft")

    monkeypatch.setattr(subprocess, "check_output", output)
    monkeypatch.setattr(platform, "uname", uname)
    entry.prime_platform_cache()
    assert calls == [(("ver",), {"shell": True, "creationflags": 8 | subprocess.CREATE_NO_WINDOW})]
    assert subprocess.check_output is output
    subprocess.check_output("later command")
    assert calls[-1] == (("later command",), {})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows platform probe creation flags")
def test_a_failed_platform_probe_restores_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.check_output

    def unavailable():
        raise OSError("platform detection unavailable")

    monkeypatch.setattr(platform, "uname", unavailable)
    with pytest.raises(OSError, match="platform detection unavailable"):
        entry.prime_platform_cache()
    assert subprocess.check_output is original


def test_sharp_frames_follow_the_upstream_icon_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    slots = []

    def upstream(hwnd, title, path):
        events.append(("upstream taskbar properties", hwnd, title, path))
        slots[:] = [256, 256]

    def sized(hwnd, path):
        events.append(("sized frames", hwnd, path))
        slots[:] = [32, 16]
        return [132, 116]

    monkeypatch.setattr(entry, "send_sized_icons", sized)
    monkeypatch.setattr(entry, "_ICONS", [])
    module = SimpleNamespace(apply_icon=upstream)
    entry.prepare_window_icons(module)
    entry.prepare_window_icons(module)
    module.apply_icon(55, "Emiya Voice", "icon.ico")
    assert events == [("upstream taskbar properties", 55, "Emiya Voice", "icon.ico"),
                      ("sized frames", 55, "icon.ico")]
    assert slots == [32, 16] and entry._ICONS == [132, 116]
