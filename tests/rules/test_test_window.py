"""Test windows (--smoke or VOICETOOL_TEST_WINDOW=1): the screen picker, the placement and the focus guard.

Pure unit tests on made-up screen layouts and fake windows: no window opens and no real window is
touched. The layouts are generic (sizes and positions invented here), so the rules hold on any desk.
The one read of the real desktop at the end only checks the probe's shape, never its numbers.
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ui import entry
from ui.contract.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(ROOT / "ui" / "themes" / "emiya")
SIZE = MANIFEST.window.size


# ---- made-up desks ------------------------------------------------------------------------------------

def _screen(number: int, *, primary: bool, left: int, width: int, height: int, top: int = 0,
            dpi: int = 96, taskbar: int = 0) -> entry.Screen:
    """A screen of `width` x `height` physical px at (left, top); the work area loses `taskbar` px."""
    bounds = entry.Rect(left, top, left + width, top + height)
    work = entry.Rect(left, top, left + width, top + height - taskbar)
    return entry.Screen(device=f"\\\\.\\DISPLAY{number}", primary=primary, bounds=bounds, work=work, dpi=dpi,
                        logical_work=work.scaled(96 / dpi))


PRIMARY = _screen(1, primary=True, left=0, width=1600, height=900, taskbar=40)
SECONDARY = _screen(2, primary=False, left=1600, width=1280, height=1024)


def _covering(screen: entry.Screen, **changes: Any) -> entry.Foreground:
    """A foreground window that covers `screen` whole (a borderless game, say)."""
    fields = {"hwnd": 4242, "visible": True, "minimized": False, "rect": screen.bounds, "monitor": screen.device,
              "shell": False, **changes}
    return entry.Foreground(**fields)


def _desk(*screens: entry.Screen, foreground: entry.Foreground | None = None,
          state: int | None = 5) -> entry.Desktop:
    return entry.Desktop(tuple(screens), foreground, state)


@pytest.fixture
def events(caplog: pytest.LogCaptureFixture) -> Iterator[list[dict[str, Any]]]:
    """The test-window events, decoded (they are JSON lines on voicetool.testwindow)."""
    caplog.set_level(logging.INFO, logger=entry.TEST_LOG.name)
    found: list[dict[str, Any]] = []

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(json.loads(record.getMessage()))

    sink = Sink()
    entry.TEST_LOG.addHandler(sink)
    yield found
    entry.TEST_LOG.removeHandler(sink)


# ---- the picker ---------------------------------------------------------------------------------------

def test_a_covering_window_on_the_primary_picks_the_secondary() -> None:
    choice = entry.choose_screen(_desk(PRIMARY, SECONDARY, foreground=_covering(PRIMARY)))
    assert choice.screen == SECONDARY and choice.reason == "free secondary screen"


def test_a_covering_window_on_the_secondary_falls_back_to_the_primary() -> None:
    choice = entry.choose_screen(_desk(PRIMARY, SECONDARY, foreground=_covering(SECONDARY)))
    assert choice.screen == PRIMARY and choice.reason == "free primary screen"


def test_one_taken_screen_gives_no_free_screen(events: list[dict[str, Any]]) -> None:
    desk = _desk(PRIMARY, foreground=_covering(PRIMARY), state=3)
    assert entry.choose_screen(desk) == entry.ScreenChoice(None, entry.NO_FREE_SCREEN)
    code = entry.place_test_window(SIZE, desk)
    assert isinstance(code, entry.ExitCode) and code == entry.EXIT_NO_FREE_SCREEN
    assert code.text == "skipped: no free screen"
    skipped = [e for e in events if e["event"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "no free screen"
    assert skipped[0]["screens"] == [{"device": PRIMARY.device, "primary": True, "dpi": 96, "taken": True}]
    assert skipped[0]["notification_state"] == 3 and {"t_wall", "t_perf", "pid"} <= set(skipped[0])


def test_one_free_screen_gives_the_primary() -> None:
    choice = entry.choose_screen(_desk(PRIMARY, foreground=_covering(PRIMARY, rect=entry.Rect(100, 100, 900, 700))))
    assert choice.screen == PRIMARY and choice.reason == "free primary screen"


def test_a_free_secondary_wins_over_a_free_primary() -> None:
    assert entry.choose_screen(_desk(PRIMARY, SECONDARY)).screen == SECONDARY


def test_the_first_free_secondary_in_device_order_wins() -> None:
    third = _screen(3, primary=False, left=-1024, width=1024, height=768)
    assert entry.choose_screen(_desk(PRIMARY, SECONDARY, third)).screen == SECONDARY
    taken = _desk(PRIMARY, SECONDARY, third, foreground=_covering(SECONDARY))
    assert entry.choose_screen(taken).screen == third


@pytest.mark.parametrize("override", ["\\\\.\\DISPLAY1", "display1", "1", " 1 "])
def test_the_override_is_honoured(override: str) -> None:
    desk = _desk(PRIMARY, SECONDARY, foreground=_covering(PRIMARY))
    assert entry.choose_screen(desk, override) == entry.ScreenChoice(PRIMARY, "override")


@pytest.mark.parametrize("override", ["\\\\.\\DISPLAY9", "7", "0", "-1", "", "  ", None])
def test_a_missing_override_falls_back_to_the_rule(override: str | None, events: list[dict[str, Any]]) -> None:
    desk = _desk(PRIMARY, SECONDARY, foreground=_covering(PRIMARY))
    assert entry.choose_screen(desk, override) == entry.ScreenChoice(SECONDARY, "free secondary screen")
    entry.place_test_window(SIZE, desk, override)
    ignored = [e for e in events if e["event"] == "override_ignored"]
    assert len(ignored) == (1 if override and override.strip() else 0)


@pytest.mark.parametrize("changes", [
    {"minimized": True}, {"visible": False}, {"shell": True}, {"rect": entry.Rect(0, 0, 1600, 860)}])
def test_these_foreground_windows_take_no_screen(changes: dict[str, Any]) -> None:
    """Minimized, hidden, the desktop or taskbar, or a maximized window short of the taskbar."""
    foreground = _covering(PRIMARY, **changes)
    assert not entry.screen_taken(PRIMARY, foreground)
    assert entry.choose_screen(_desk(PRIMARY, foreground=foreground)).screen == PRIMARY


def test_a_busy_notification_state_alone_takes_no_screen() -> None:
    assert entry.choose_screen(_desk(PRIMARY, state=2)).screen == PRIMARY


def test_covering_allows_a_little_slack() -> None:
    bounds = PRIMARY.bounds
    assert entry.Rect(bounds.left + 2, bounds.top, bounds.right - 2, bounds.bottom).covers(bounds)
    assert not entry.Rect(bounds.left + 3, bounds.top, bounds.right, bounds.bottom).covers(bounds)
    assert entry.Rect(bounds.left - 8, bounds.top - 8, bounds.right + 8, bounds.bottom + 8).covers(bounds)


# ---- the placement ------------------------------------------------------------------------------------

def test_the_window_is_centred_on_the_logical_work_area(events: list[dict[str, Any]]) -> None:
    scaled = _screen(2, primary=False, left=1600, width=2400, height=1600, dpi=144)
    args = entry.place_test_window(SIZE, _desk(PRIMARY, scaled, foreground=_covering(PRIMARY)))
    work = scaled.logical_work
    assert work.width == 1600 and work.height == 1067                  # physical px at 96 / 144
    assert args == {"x": work.left + (work.width - SIZE[0]) // 2, "y": work.top + (work.height - SIZE[1]) // 2,
                    "focus": False, "on_top": True}
    chosen = [e for e in events if e["event"] == "screen_chosen"]
    assert len(chosen) == 1 and chosen[0]["device"] == scaled.device and chosen[0]["dpi"] == 144
    assert (chosen[0]["x"], chosen[0]["y"]) == (args["x"], args["y"])


def test_a_window_larger_than_the_work_area_keeps_its_top_left() -> None:
    small = _screen(2, primary=False, left=-1000, top=50, width=1000, height=700)
    assert entry.centred(small.logical_work, SIZE) == (-1000, 50)


def test_a_normal_launch_centres_on_the_primary() -> None:
    assert entry.centred_args(MANIFEST, (SECONDARY, PRIMARY)) == dict(
        zip(("x", "y"), entry.centred(PRIMARY.logical_work, SIZE)))
    assert entry.centred_args(MANIFEST, ()) == {}


def test_each_process_places_its_window(events: list[dict[str, Any]]) -> None:
    desk = _desk(PRIMARY, SECONDARY, foreground=_covering(PRIMARY))
    test = entry.Flags(smoke="all", test_window=True)
    server = entry.window_placement(MANIFEST, test, window_process=False, simulation=False, env={},
                                    probe=lambda: desk)
    assert isinstance(server, dict) and server["focus"] is False and server["on_top"] is True
    assert entry.window_placement(MANIFEST, test, window_process=True, simulation=False, env={},
                                  probe=lambda: desk) == {}
    normal = entry.window_placement(MANIFEST, entry.Flags(), window_process=False, simulation=False, env={},
                                    probe=lambda: desk)
    assert set(normal) == {"x", "y"}
    assert entry.window_placement(MANIFEST, test, window_process=False, simulation=True, env={},
                                  probe=lambda: desk) == normal       # the user fixture never opens a window
    assert entry.window_placement(MANIFEST, entry.Flags(browser=True, test_window=True), window_process=False,
                                  simulation=False, env={}, probe=lambda: desk) == {}
    skipped = entry.window_placement(MANIFEST, test, window_process=False, simulation=False, env={},
                                     probe=lambda: _desk(PRIMARY, foreground=_covering(PRIMARY)))
    assert skipped == entry.EXIT_NO_FREE_SCREEN


def test_the_screen_override_comes_from_the_environment() -> None:
    desk = _desk(PRIMARY, SECONDARY)
    test = entry.Flags(smoke="all", test_window=True)
    args = entry.window_placement(MANIFEST, test, window_process=False, simulation=False,
                                  env={entry.TEST_SCREEN_ENV: "1"}, probe=lambda: desk)
    assert isinstance(args, dict) and (args["x"], args["y"]) == entry.centred(PRIMARY.logical_work, SIZE)


# ---- flags --------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("argv", "env", "on"), [
    (["--smoke=all"], {}, True), (["--smoke"], {}, True), (["--fake", "--smoke", "--browser"], {}, True),
    ([], {entry.TEST_WINDOW_ENV: "1"}, True), ([], {entry.TEST_WINDOW_ENV: "0"}, False),
    ([], {}, False), (["--fake=forge", "--browser"], {}, False)])
def test_test_window_mode_is_for_smoke_runs_only(argv: list[str], env: dict[str, str], on: bool) -> None:
    assert entry.read_flags(argv, env).test_window is on


def test_flags_are_read_from_the_command_line() -> None:
    flags = entry.read_flags(["--fake", "--data-root", "D:/scratch", "--smoke", "--browser", "--no-show",
                              "--fx-telemetry", "--debug-webview", "--unknown"], {})
    assert flags == entry.Flags(browser=True, fake="speak", data_root="D:/scratch", smoke="", fx_telemetry=True,
                                debug_webview=True, no_show=True, test_window=True)
    assert entry.read_flags(["--fake=forge", "--smoke=all", "--data-root=x"], {}).fake == "forge"
    assert entry.read_flags([], {}) == entry.Flags()


def test_the_user_fixture_reads_its_flags_from_the_environment() -> None:
    assert entry.simulation_flags({entry.FAKE_ENV: "forge", entry.DATA_ENV: "D:/t"}) == entry.Flags(
        fake="forge", data_root="D:/t")
    assert entry.simulation_flags({}).fake == "speak"
    assert entry.simulation_flags({entry.ENGINE_ENV: "real", entry.FAKE_ENV: "forge"}).fake is None
    assert entry.simulation_flags({entry.TEST_WINDOW_ENV: "1"}).test_window is False


@pytest.mark.parametrize(("flags", "show"), [
    (entry.Flags(browser=True), True), (entry.Flags(browser=True, no_show=True), False),
    (entry.Flags(browser=True, smoke="all", test_window=True), False)])
def test_browser_mode_binds_loopback_and_honours_no_show(flags: entry.Flags, show: bool) -> None:
    kwargs = entry.run_arguments(MANIFEST, flags)
    assert kwargs["host"] == "127.0.0.1" and kwargs["native"] is False and kwargs["show"] is show


def test_native_runs_never_open_a_browser_tab() -> None:
    kwargs = entry.run_arguments(MANIFEST, entry.Flags(no_show=True))
    assert kwargs["native"] is True and "show" not in kwargs and "host" not in kwargs


# ---- the focus guard and the window process -----------------------------------------------------------

class _Event:
    """pywebview's Event, as far as the guard uses it."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.done = threading.Event()

    def __iadd__(self, handler: Any) -> _Event:
        self.handlers.append(handler)
        return self

    def set(self, *args: Any) -> None:
        for handler in list(self.handlers):
            handler(*args)
        self.done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    def is_set(self) -> bool:
        return self.done.is_set()


class _Handle:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def ToInt64(self) -> int:  # noqa: N802 - the .NET name
        return self.hwnd


class _Form:
    """A WinForms form: a Handle and an Activated event. Until `created`, reading Handle fails the test:
    on a real form that read would create the window on the reading thread."""

    def __init__(self, hwnd: int, *, created: bool = True) -> None:
        self._handle = _Handle(hwnd)
        self.created = created
        self.Activated = _Event()

    @property
    def Handle(self) -> _Handle:  # noqa: N802 - the .NET name
        if not self.created:
            raise AssertionError("Handle read before the form's window exists")
        return self._handle

    def activate(self) -> None:
        self.Activated.set(self, None)


class _Window:
    """A pywebview window that fails the test if anything shows or restores it (both call Activate())."""

    def __init__(self, form: _Form | None = None) -> None:
        self.native = form
        self.events = type("Events", (), {})()
        for name in ("before_show", "shown", "loaded"):
            setattr(self.events, name, _Event())

    def show(self) -> None:
        raise AssertionError("show() activates the window")

    def restore(self) -> None:
        raise AssertionError("restore() activates the window")


@pytest.mark.parametrize("focus", [False, True], ids=["test-window", "normal-window"])
def test_the_activation_veto_precedes_native_construction_only_for_test_windows(
        focus: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hidden constructor can activate before Show: installing in the ready callback is too late."""
    calls: list[str] = []

    def install() -> Any:
        calls.append("veto installed")
        return lambda: calls.append("veto released")

    monkeypatch.setattr(entry, "_block_test_activation", install)

    class Form:
        def __init__(self, window: Any) -> None:
            calls.append("native constructor")
            self.Enabled = True
            self.pywebview_window = window
            self.Handle = _Handle(55)
            self.FormClosed = _Event()

    entry.prevent_test_activation(Form)
    entry.prevent_test_activation(Form)                    # install once per window process
    form = Form(type("Window", (), {"focus": focus})())
    assert calls == (["native constructor"] if focus else ["veto installed", "native constructor"])
    assert form.Enabled is True
    form.FormClosed.set()
    assert calls[-1] == ("native constructor" if focus else "veto released")


def test_a_failed_native_constructor_releases_the_activation_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[int] = []
    monkeypatch.setattr(entry, "_block_test_activation", lambda: lambda: released.append(55))
    class Form:
        def __init__(self, window: Any) -> None:
            raise OSError("native constructor failed")

    entry.prevent_test_activation(Form)
    with pytest.raises(OSError, match="native constructor failed"):
        Form(type("Window", (), {"focus": False})())
    assert released == [55]


@pytest.fixture
def desktop_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake GetForegroundWindow / SetForegroundWindow / WM_SETICON: no real window is touched."""
    calls: dict[str, Any] = {"set_foreground": [], "icons": [], "foreground": 1}
    monkeypatch.setattr(entry, "foreground_window", lambda: calls["foreground"])
    monkeypatch.setattr(entry, "_set_foreground_window", lambda hwnd: calls["set_foreground"].append(hwnd) or True)
    monkeypatch.setattr(entry, "send_sized_icons", lambda hwnd, icon: calls["icons"].append((hwnd, icon)) or [9])
    return calls


def test_the_guard_hooks_activation_before_show(desktop_calls: dict[str, Any],
                                                events: list[dict[str, Any]]) -> None:
    window = _Window()
    entry.FocusGuard(777).attach(window)
    window.native = _Form(55)
    window.events.before_show.set(window)                   # pywebview calls handlers with the window
    assert desktop_calls["set_foreground"] == []
    desktop_calls["foreground"] = 55
    window.native.activate()
    window.native.activate()
    assert desktop_calls["set_foreground"] == [777, 777]
    returned = [e for e in events if e["event"] == "focus_returned"]
    assert len(returned) == 2 and returned[0]["to"] == 777 and returned[0]["ok"] is True
    assert {"t_wall", "t_perf", "took_ms", "foreground"} <= set(returned[0])


def test_the_guard_hands_the_focus_back_at_once_if_the_form_already_has_it(
        desktop_calls: dict[str, Any]) -> None:
    desktop_calls["foreground"] = 55
    window = _Window(_Form(55))
    window.events.before_show.set(window)                   # pywebview showed the form before the guard ran
    guard = entry.FocusGuard(777)
    guard.attach(window)
    assert guard.hooked and desktop_calls["set_foreground"] == [777]
    guard.hook(window.native)                               # a second hook point changes nothing
    assert desktop_calls["set_foreground"] == [777] and len(window.native.Activated.handlers) == 1


def test_a_vetoed_activation_notice_does_not_disturb_the_foreground_app(desktop_calls: dict[str, Any]) -> None:
    window = _Window(_Form(55))
    entry.FocusGuard(777).attach(window)
    window.events.before_show.set(window)
    window.native.activate()                                # WinForms notice, Windows still has foreground 1
    assert desktop_calls["set_foreground"] == []


def test_the_guard_never_touches_a_form_before_its_window_exists(desktop_calls: dict[str, Any]) -> None:
    """pywebview sets window.native at the top of the form's constructor, before the handle exists."""
    window = _Window(_Form(55, created=False))
    guard = entry.FocusGuard(777)
    guard.attach(window)                                    # reading Handle now would fail the test
    assert not guard.hooked and window.native.Activated.handlers == []
    window.native.created = True
    window.events.before_show.set(window)                   # the UI thread, once the constructor is done
    assert guard.hooked
    desktop_calls["foreground"] = 55
    window.native.activate()
    assert desktop_calls["set_foreground"] == [777]


class _MissedEvent(_Event):
    """A before_show that ran its handlers just before the guard subscribed, flagged just after its check."""

    def __iadd__(self, handler: Any) -> _MissedEvent:
        return self


def test_window_ready_hooks_once_shown_if_before_show_was_missed(desktop_calls: dict[str, Any],
                                                                 events: list[dict[str, Any]]) -> None:
    window = _Window(_Form(55))
    window.events.before_show = _MissedEvent()
    window.events.shown.set()
    window.events.loaded.set()
    entry.window_ready(window, icon="icon.ico", test_window=True, env={entry.RETURN_HWND_ENV: "777"}, wait_s=1.0)
    assert len(window.native.Activated.handlers) == 1
    desktop_calls["foreground"] = 55
    window.native.activate()
    assert desktop_calls["set_foreground"] == [777] and desktop_calls["icons"] == [(55, "icon.ico")]
    assert [e["event"] for e in events] == ["window_shown", "focus_returned"]


def test_window_ready_guards_logs_and_fixes_the_icons(desktop_calls: dict[str, Any],
                                                      events: list[dict[str, Any]]) -> None:
    window = _Window(_Form(55))
    window.events.shown.set()
    window.events.loaded.set()
    entry.window_ready(window, icon="icon.ico", test_window=True, env={entry.RETURN_HWND_ENV: "0x309"},
                       wait_s=1.0)
    assert desktop_calls["icons"] == [(55, "icon.ico")]
    shown = [e for e in events if e["event"] == "window_shown"]
    assert len(shown) == 1 and shown[0]["hwnd"] == 55 and shown[0]["foreground"] == 1
    desktop_calls["foreground"] = 55
    window.native.activate()
    assert desktop_calls["set_foreground"] == [0x309]


def test_a_normal_window_gets_no_guard_and_no_events(desktop_calls: dict[str, Any],
                                                     events: list[dict[str, Any]]) -> None:
    window = _Window(_Form(55))
    window.events.shown.set()
    window.events.loaded.set()
    entry.window_ready(window, icon="icon.ico", test_window=False, env={entry.RETURN_HWND_ENV: "777"},
                       wait_s=1.0)
    window.native.activate()
    assert desktop_calls["set_foreground"] == [] and events == []
    assert desktop_calls["icons"] == [(55, "icon.ico")]


def test_a_window_that_never_shows_is_left_alone(desktop_calls: dict[str, Any]) -> None:
    entry.window_ready(_Window(_Form(55)), icon="icon.ico", test_window=True, env={}, wait_s=0.01)
    assert desktop_calls["icons"] == []


@pytest.mark.parametrize(("raw", "hwnd"), [("1234", 1234), ("0x1F", 31), (" 77 ", 77), ("", None), ("abc", None),
                                           ("-5", None), ("0", None)])
def test_return_hwnd_is_read_from_the_environment(raw: str, hwnd: int | None) -> None:
    assert entry.return_hwnd({entry.RETURN_HWND_ENV: raw}) == hwnd
    assert entry.return_hwnd({}) is None


def test_the_icons_go_out_as_big_and_small(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int, int]] = []
    loaded: list[tuple[str, int, int]] = []

    class FakeWin:
        def metric(self, index: int) -> int:
            return {11: 32, 12: 32, 49: 16, 50: 16}[index]

        def load_image(self, _: Any, path: str, kind: int, width: int, height: int, flags: int) -> int:
            loaded.append((path, width, height))
            return 100 + width

        def send_message(self, hwnd: int, message: int, which: int, handle: int) -> int:
            sent.append((message, which, handle))
            return 0

    monkeypatch.setattr(entry, "_win32", lambda: FakeWin())
    assert entry.send_sized_icons(55, "icon.ico") == [132, 116]
    assert loaded == [("icon.ico", 32, 32), ("icon.ico", 16, 16)]
    assert sent == [(0x80, 1, 132), (0x80, 0, 116)]                  # WM_SETICON, ICON_BIG then ICON_SMALL
    assert entry.send_sized_icons(0, "icon.ico") == []


def test_a_second_launch_brings_the_app_window_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnumWindows over fakes: only a WinForms window titled Emiya Voice is restored and raised."""
    raised: list[tuple[str, int]] = []
    windows = {10: ("SomeOtherAppWindow", "Emiya Voice"), 11: ("WindowsForms10.Window.8.app.0.1", "Other"),
               12: ("WindowsForms10.Window.8.app.0.2", "Emiya Voice")}

    class FakeWin:
        def window_proc(self, function: Any) -> Any:
            return function

        def enum_windows(self, visit: Any, _: int) -> None:
            for hwnd in windows:
                if not visit(hwnd, 0):
                    break

        def class_name(self, hwnd: int, buffer: Any, size: int) -> int:
            buffer.value = windows[hwnd][0]
            return len(buffer.value)

        def send_timeout(self, hwnd: int, message: int, size: int, address: int, flags: int, timeout: int,
                         copied: Any) -> int:
            title = windows[hwnd][1]
            ctypes.memmove(address, ctypes.create_unicode_buffer(title), (len(title) + 1) * 2)   # WM_GETTEXT
            return 1

        def is_iconic(self, hwnd: int) -> bool:
            return True

        def show_window(self, hwnd: int, command: int) -> None:
            raised.append(("show", command))

        def set_foreground(self, hwnd: int) -> bool:
            raised.append(("foreground", hwnd))
            return True

    monkeypatch.setattr(entry, "_win32", lambda: FakeWin())
    assert entry.bring_window_forward() is True
    assert raised == [("show", 9), ("foreground", 12)]                # SW_RESTORE, then the matching window


def test_nothing_raises_into_the_winforms_handlers(desktop_calls: dict[str, Any],
                                                   caplog: pytest.LogCaptureFixture) -> None:
    def refuse(hwnd: int) -> bool:
        raise OSError("access denied")

    window = _Window(_Form(55))
    entry.FocusGuard(777, set_foreground=refuse).attach(window)
    window.events.before_show.set(window)
    desktop_calls["foreground"] = 55
    window.native.activate()                                # pythonnet would carry an exception into .NET
    assert "The focus guard failed: access denied" in caplog.text


def test_a_refused_screen_read_skips_a_test_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(win: Any) -> Any:
        raise OSError("no desktop")

    monkeypatch.setattr(entry, "_win32", lambda: object())
    monkeypatch.setattr(entry, "_read_screens", refuse)
    assert entry.probe_desktop() == entry.Desktop((), None, None) and entry.probe_screens() == ()
    test = entry.Flags(smoke="all", test_window=True)
    assert entry.window_placement(MANIFEST, test, window_process=False, simulation=False,
                                  env={}) == entry.EXIT_NO_FREE_SCREEN
    assert entry.window_placement(MANIFEST, entry.Flags(), window_process=False, simulation=False, env={}) == {}


def test_a_refused_window_search_is_no_second_launch_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing:
        def window_proc(self, function: Any) -> Any:
            raise ctypes.ArgumentError("bad callback")

    monkeypatch.setattr(entry, "_win32", lambda: Refusing())
    assert entry.bring_window_forward() is False


# ---- the events channel and the real desktop ----------------------------------------------------------

def test_enable_test_events_writes_one_json_line_each(capsys: pytest.CaptureFixture[str]) -> None:
    logger = entry.TEST_LOG
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    try:
        entry.enable_test_events()
        entry.enable_test_events()                          # idempotent: one handler
        assert sum(isinstance(h, entry._TestEventHandler) for h in logger.handlers) == 1
        entry.emit_test_event("probe", value=1)
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
    line = [text for text in capsys.readouterr().err.splitlines() if text.startswith(logger.name)][-1]
    payload = json.loads(line[len(logger.name) + 1:])
    assert payload["event"] == "probe" and payload["value"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="the probe reads Win32 monitors")
def test_the_probe_reads_the_real_desktop_read_only() -> None:
    desktop = entry.probe_desktop()
    assert all(isinstance(screen, entry.Screen) for screen in desktop.screens)
    assert sum(screen.primary for screen in desktop.screens) <= 1
    for screen in desktop.screens:
        assert screen.device and screen.dpi >= 96 and screen.bounds.width > 0 and screen.logical_work.width > 0
    assert desktop.foreground is None or isinstance(desktop.foreground, entry.Foreground)
    assert entry.probe_screens() == desktop.screens
