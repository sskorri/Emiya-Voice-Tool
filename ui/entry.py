"""Early boot for gui.py (BUILD_SPEC §6.2.11): everything that has to happen before NiceGUI is imported.

Standard library, filelock and ui.contract.manifest only: pywebview's window process re-imports gui.py
and must stay light (tests/rules/test_fresh_import.py). The helpers return values or exit codes, and
gui.py leaves through fail(), which never shows a box or exits under pytest or the NiceGUI user fixture.

Test windows (--smoke, or VOICETOOL_TEST_WINDOW=1; never a normal launch) open on a free screen without
taking the focus. A screen is taken only while a visible, non-minimized foreground window covers it
whole (SHQueryUserNotificationState can say "busy" while nothing covers a screen, so it is only logged).
A free secondary screen wins over a free primary one; with neither, the run is skipped with exit code 8.
"""
from __future__ import annotations

import contextlib
import ctypes
import faulthandler
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ui.contract.manifest import ManifestError, ThemeManifest, load_manifest, run_kwargs

if TYPE_CHECKING:
    from filelock import FileLock

__all__ = [
    "ACTIVE_THEME", "EXIT_ALREADY_RUNNING", "EXIT_BAD_THEME", "EXIT_ENGINE_LOCKED", "EXIT_NO_FREE_SCREEN",
    "EXIT_UNCAUGHT", "NO_FREE_SCREEN", "WEBVIEW_SWITCHES", "BootFailure", "Desktop", "ExitCode", "Flags",
    "FocusGuard", "Foreground", "Rect", "Screen", "ScreenChoice", "app_name", "bring_window_forward", "centred",
    "centred_args", "choose_screen", "crash_hook", "data_root", "emit_test_event", "enable_test_events",
    "environment", "fail", "find_screen", "foreground_window", "form_hwnd", "instance_lock_path",
    "is_simulation", "load_theme_manifest", "merge_switches", "place_test_window", "prepare_window_icons", "prevent_test_activation", "prime_platform_cache", "probe_desktop",
    "probe_screens", "read_flags", "read_offline", "redirect_streams", "register_font_types", "return_hwnd",
    "run_arguments", "screen_taken", "send_sized_icons", "simulation_flags", "take_instance_lock",
    "under_test", "use_app_name", "window_placement", "window_ready",
]

ACTIVE_THEME = "emiya"              # one theme per process; the theme picker is deferred (§1.2)
WINDOW_TITLE = "Emiya Voice"        # the app's name until gui.py has read the active theme's manifest
DEFAULT_SCENARIO = "speak"          # --fake without a scenario
DEV_ROOT = ".dev"                   # the --fake data root inside the install folder (ui.core.paths.DEV_ROOT)
SETTINGS_FILE = "ui_settings.json"

EXIT_UNCAUGHT = 1                   # an uncaught error: a box points at logs\gui.log
EXIT_ALREADY_RUNNING = 5            # another Emiya Voice holds cache\gui.lock (never 3, uvicorn's STARTUP_FAILURE)
EXIT_ENGINE_LOCKED = 6              # reserved for the strict engine-lock variant (Q8)
EXIT_BAD_THEME = 7                  # theme.toml is broken
EXIT_NO_FREE_SCREEN = 8             # a test window found no free screen: skipped, never a crash

SIMULATION_ENV = "NICEGUI_USER_SIMULATION"   # set by NiceGUI's user fixture before it runs gui.py
FAKE_ENV = "VOICETOOL_FAKE"                  # the user fixture's FakeEngine scenario
DATA_ENV = "VOICETOOL_DATA_DIR"              # the user fixture's scratch data root
ENGINE_ENV = "VOICETOOL_ENGINE"              # "real": the real engine under the user fixture (I3)
TEST_WINDOW_ENV = "VOICETOOL_TEST_WINDOW"    # "1": test-window mode without --smoke
TEST_SCREEN_ENV = "VOICETOOL_TEST_SCREEN"    # a \\.\DISPLAYn device name or a 1-based screen index
RETURN_HWND_ENV = "VOICETOOL_TEST_RETURN_HWND"   # the harness window that gets the focus back
WEBVIEW2_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"

WEBVIEW_SWITCHES = (
    "--disable-features=ElasticOverscroll",     # pywebview's own switch, repeated in case ours replaces it
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-pings",
    "--no-proxy-server",                        # about 0.95 s less startup (no proxy auto-detection)
    "--autoplay-policy=no-user-gesture-required",   # Autoplay New Takes
)

SERVER_LOG = "gui.log"
WINDOW_LOG = "gui-window.log"
ROTATE_BYTES = 10 * 1024 * 1024
NO_FREE_SCREEN = "no free screen"
WINDOW_WAIT_S = 30.0                # how long the window process waits for pywebview's shown and loaded
COVER_SLACK_PX = 2                  # a window this close to a screen's edges still covers it
BASE_DPI = 96

LOG = logging.getLogger("voicetool.entry")
TEST_LOG = logging.getLogger("voicetool.testwindow")

_STREAMS: list[Any] = []            # the pythonw log stream, open for the life of the process
_ICONS: list[int] = []              # icon handles the window keeps using
_APP_NAME = [WINDOW_TITLE]          # the active theme's window title once use_app_name() ran
_PASSIVE_FORMS: dict[int, Any] = {}   # keep each test form's CBT hook callback alive until FormClosed


def use_app_name(title: str) -> None:
    """Name the app after the active theme's window title (gui.py, once the manifest is read): the fail and
    crash texts, the "already running" text and the running-window search use it from then on."""
    if isinstance(title, str) and title.strip():
        _APP_NAME[0] = title.strip()


def app_name() -> str:
    """The app's name: the active theme's window title, or WINDOW_TITLE before the manifest was read."""
    return _APP_NAME[0]


# ---- exit codes ---------------------------------------------------------------------------------------

class ExitCode(int):
    """An exit code that carries its reason, for fail()."""

    text: str

    def __new__(cls, code: int, text: str) -> ExitCode:
        value = super().__new__(cls, code)
        value.text = text
        return value


class BootFailure(RuntimeError):
    """fail() under pytest or the user fixture: a modal box would block the test run forever."""

    def __init__(self, code: int, text: str) -> None:
        super().__init__(f"exit code {code}: {text}")
        self.code = code
        self.text = text


def is_simulation(env: Mapping[str, str] = os.environ) -> bool:
    """True while NiceGUI's user fixture runs gui.py (it sets NICEGUI_USER_SIMULATION before runpy)."""
    return env.get(SIMULATION_ENV) == "true"


def under_test(env: Mapping[str, str] = os.environ) -> bool:
    """True under the user fixture or anywhere in pytest."""
    return is_simulation(env) or "PYTEST_CURRENT_TEST" in env


def fail(code: int, text: str, *, quiet: bool = False) -> NoReturn:
    """Leave with `code`: a message box (unless quiet) and sys.exit; under pytest, raise BootFailure."""
    _stderr_line(f"{app_name()}: {text} (exit code {int(code)})")
    if under_test():
        raise BootFailure(int(code), text)
    if not quiet:
        _message_box(text)
    sys.exit(int(code))


# ---- flags --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Flags:
    """gui.py's command line (both processes see the same argv)."""

    browser: bool = False           # serve on 127.0.0.1 instead of opening the native window
    fake: str | None = None         # the FakeEngine scenario; None = the real engine
    data_root: str | None = None    # --data-root=PATH
    smoke: str | None = None        # --smoke[=PLAN]; "" = no plan
    fx_telemetry: bool = False
    debug_webview: bool = False
    no_show: bool = False           # --browser never opens a tab
    test_window: bool = False       # --smoke or VOICETOOL_TEST_WINDOW=1


def read_flags(argv: Sequence[str], env: Mapping[str, str] = os.environ) -> Flags:
    """The flags of a normal run; unknown arguments are ignored."""
    args = list(argv)
    fake = _option(args, "--fake")
    smoke = _option(args, "--smoke")
    return Flags(
        browser="--browser" in args,
        fake=None if fake is None else (fake or DEFAULT_SCENARIO),
        data_root=_option(args, "--data-root") or None,
        smoke=smoke,
        fx_telemetry="--fx-telemetry" in args,
        debug_webview="--debug-webview" in args,
        no_show="--no-show" in args,
        test_window=smoke is not None or env.get(TEST_WINDOW_ENV, "").strip() == "1",
    )


def simulation_flags(env: Mapping[str, str] = os.environ) -> Flags:
    """The user fixture's flags come from the environment (pytest's argv is not gui.py's)."""
    real = env.get(ENGINE_ENV, "").strip().lower() == "real"
    return Flags(fake=None if real else (env.get(FAKE_ENV, "").strip() or DEFAULT_SCENARIO),
                 data_root=env.get(DATA_ENV, "").strip() or None)


def _option(args: Sequence[str], name: str) -> str | None:
    """None when absent; "" for a bare flag; the value of --name=value or --name value."""
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
        if arg == name:
            following = args[index + 1] if index + 1 < len(args) else ""
            return "" if not following or following.startswith("--") else following
    return None


def run_arguments(manifest: ThemeManifest, flags: Flags) -> dict[str, Any]:
    """ui.run's keyword arguments; --browser opens no tab with --no-show or in test-window mode."""
    kwargs = run_kwargs(manifest, browser=flags.browser)
    if flags.browser and (flags.no_show or flags.test_window):
        kwargs["show"] = False
    return kwargs


# ---- streams ------------------------------------------------------------------------------------------

def redirect_streams(log_dir: str | os.PathLike[str], *, window: bool) -> bool:
    """pythonw: point None stdout/stderr at logs\\gui.log (server, rotated at 10 MB) or logs\\gui-window.log.

    Under pythonw tqdm raises on a None stream and leaves its lock held, and NiceGUI's import would swap
    the streams for devnull and hide every log line, so this runs first. False when nothing was None.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return False
    folder = Path(log_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (WINDOW_LOG if window else SERVER_LOG)
    if not window:
        _rotate(path)
    stream = open(path, "a", encoding="utf-8", buffering=1, errors="backslashreplace")  # noqa: SIM115
    _STREAMS.append(stream)
    if sys.stdout is None:
        sys.stdout = sys.__stdout__ = stream
    if sys.stderr is None:
        sys.stderr = sys.__stderr__ = stream
    faulthandler.enable(stream)
    return True


def _rotate(path: Path) -> None:
    """gui.log -> gui.log.1 past ROTATE_BYTES (another instance may hold it open: then keep appending)."""
    with contextlib.suppress(OSError):
        if path.stat().st_size > ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))


def crash_hook(*, quiet: bool) -> Callable[[type[BaseException], BaseException, Any], None]:
    """sys.excepthook for a pythonw server: the traceback into the log, then a box pointing at it."""

    def hook(kind: type[BaseException], error: BaseException, trace: Any) -> None:
        sys.__excepthook__(kind, error, trace)
        if not quiet and not issubclass(kind, KeyboardInterrupt):
            _message_box(f"{app_name()} stopped: {_first_line(error)}\n\nSee logs\\{SERVER_LOG}")

    return hook


def _stderr_line(text: str) -> None:
    stream = sys.stderr
    if stream is not None:
        with contextlib.suppress(OSError, ValueError):
            stream.write(text + "\n")
            stream.flush()


def _first_line(error: BaseException) -> str:
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return lines[0][:300] if lines else type(error).__name__


# ---- settings and environment -------------------------------------------------------------------------

def data_root(app_root: str | os.PathLike[str], flags: Flags) -> Path:
    """The run's data root, by Paths.for_run's rule: --data-root, else <app>\\.dev for --fake, else <app>."""
    if flags.data_root:
        return Path(os.path.abspath(flags.data_root))
    root = Path(os.path.abspath(app_root))
    return root / DEV_ROOT if flags.fake is not None else root


def read_offline(path: str | os.PathLike[str]) -> bool:
    """Settings › Offline Mode, read-only: a missing or odd file, section or value means on."""
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return True
    section = data.get("core") if isinstance(data, dict) else None
    value = section.get("offline") if isinstance(section, dict) else None
    return value if isinstance(value, bool) else True


def merge_switches(existing: str, switches: Sequence[str] = WEBVIEW_SWITCHES) -> str:
    """The existing WebView2 arguments plus each switch not already among them."""
    tokens = existing.split()
    tokens += [switch for switch in switches if switch not in tokens]
    return " ".join(tokens)


def environment(flags: Flags, app_root: str | os.PathLike[str], *, offline: bool, simulation: bool,
                env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The variables to set before NiceGUI and huggingface_hub are imported (§6.2.11 step 3).

    Under the user fixture only HF_HUB_OFFLINE applies: NiceGUI is already imported (the plugin owns its
    storage path) and no window or model starts, so nothing else is written into pytest's environment.
    """
    updates = {"HF_HUB_OFFLINE": "1" if offline else "0"}
    if simulation:
        return updates
    storage = ".nicegui-dev" if flags.fake is not None else ".nicegui"
    updates["NICEGUI_STORAGE_PATH"] = os.path.join(os.path.abspath(app_root), storage)
    updates[WEBVIEW2_ENV] = merge_switches(env.get(WEBVIEW2_ENV, ""))
    if "TQDM_MININTERVAL" not in env:
        updates["TQDM_MININTERVAL"] = "5"
    return updates


def register_font_types() -> None:
    """This venv maps .woff2 and .woff to no MIME type."""
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("font/woff", ".woff")


def prime_platform_cache() -> None:
    """Before dependency imports or threads, prime platform.uname without a Windows console host.

    Python 3.11's first platform.system() calls win32_ver(), which runs `ver` through cmd.exe.
    Keep its own version detection/parsing, adding CREATE_NO_WINDOW to that boot-only query.
    The subprocess function is restored even if the probe fails; later commands are unchanged.
    """
    if sys.platform != "win32":
        return
    import platform
    import subprocess

    original = subprocess.check_output

    def quiet_output(*args: Any, **kwargs: Any) -> Any:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return original(*args, **kwargs)

    subprocess.check_output = quiet_output
    try:
        platform.uname()
    finally:
        subprocess.check_output = original


# ---- single instance and manifest ---------------------------------------------------------------------

def instance_lock_path(app_root: str | os.PathLike[str], *, fake: bool) -> Path:
    """cache\\gui.lock in the install folder (gui-dev.lock for --fake), as Paths.for_run names it."""
    return Path(os.path.abspath(app_root)) / "cache" / ("gui-dev.lock" if fake else "gui.lock")


def take_instance_lock(path: str | os.PathLike[str]) -> FileLock | ExitCode:
    """The single-instance lock, held for the process lifetime; exit code 5 when another process holds it."""
    from filelock import FileLock, Timeout

    lock = FileLock(os.fspath(path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return ExitCode(EXIT_ALREADY_RUNNING, f"{app_name()} is already running.")
    return lock


def load_theme_manifest(folder: str | os.PathLike[str]) -> ThemeManifest | ExitCode:
    """The active theme's manifest; exit code 7 with the reason when theme.toml is broken."""
    try:
        return load_manifest(folder)
    except ManifestError as error:
        return ExitCode(EXIT_BAD_THEME, f"{app_name()} cannot start: {error}")


def bring_window_forward(title: str | None = None) -> bool:
    """Restore and raise the running app window, found by its title (default: app_name()); best effort, True
    when it came to the front."""
    win = _win32()
    if win is None:
        return False
    try:
        return _bring_forward(win, title or app_name())
    except _WIN32_ERRORS as error:
        LOG.warning("Couldn't bring the running window forward: %s", _first_line(error))
        return False


def _bring_forward(win: _Win32, title: str) -> bool:
    found: list[int] = []

    def visit(hwnd: int, _: int) -> bool:
        if _class_name(win, hwnd).startswith(_WINFORMS_CLASS) and _window_text(win, hwnd) == title:
            found.append(int(hwnd))
            return False
        return True

    callback = win.window_proc(visit)
    win.enum_windows(callback, 0)
    if not found:
        return False
    if win.is_iconic(found[0]):
        win.show_window(found[0], _SW_RESTORE)
    return bool(win.set_foreground(found[0]))


# ---- screens ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    """A rectangle, right and bottom exclusive."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def covers(self, other: Rect, slack: int = COVER_SLACK_PX) -> bool:
        """True when this rectangle covers all of `other` (within `slack` px of each edge)."""
        return (self.left <= other.left + slack and self.top <= other.top + slack
                and self.right >= other.right - slack and self.bottom >= other.bottom - slack)

    def scaled(self, factor: float) -> Rect:
        return Rect(*(round(value * factor) for value in (self.left, self.top, self.right, self.bottom)))


@dataclass(frozen=True)
class Screen:
    """One display: physical rectangles, its DPI, and its work area in the units pywebview's x/y use."""

    device: str                     # "\\.\DISPLAY1"
    primary: bool
    bounds: Rect                    # the whole screen, physical px
    work: Rect                      # the work area, physical px
    dpi: int                        # effective DPI (96 = 100 %)
    logical_work: Rect              # the work area in logical px (pywebview multiplies x/y by its scale)


@dataclass(frozen=True)
class Foreground:
    """The foreground window as far as a screen choice cares."""

    hwnd: int
    visible: bool
    minimized: bool
    rect: Rect                      # physical px
    monitor: str = ""               # MonitorFromWindow's device, "" when unknown
    shell: bool = False             # the desktop or the taskbar: never takes a screen


@dataclass(frozen=True)
class Desktop:
    """What the screen choice reads, in one probe."""

    screens: tuple[Screen, ...]     # in device order: VOICETOOL_TEST_SCREEN=1 is the first
    foreground: Foreground | None
    notification_state: int | None  # SHQueryUserNotificationState, logged only (2 "busy" alone means nothing)


@dataclass(frozen=True)
class ScreenChoice:
    screen: Screen | None
    reason: str                     # "override", "free secondary screen", "free primary screen" or NO_FREE_SCREEN


def screen_taken(screen: Screen, foreground: Foreground | None) -> bool:
    """A visible, non-minimized foreground window (not the desktop or taskbar) covers the whole screen."""
    if foreground is None or foreground.shell or not foreground.visible or foreground.minimized:
        return False
    return foreground.rect.covers(screen.bounds)


def find_screen(screens: Sequence[Screen], name: str | None) -> Screen | None:
    """The screen a device name (\\\\.\\DISPLAY2, or DISPLAY2) or a 1-based index names; None if none."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    if wanted.isdigit():
        index = int(wanted)
        return screens[index - 1] if 1 <= index <= len(screens) else None
    lowered = wanted.lower()
    return next((s for s in screens if lowered in (s.device.lower(), s.device.lower().rsplit("\\", 1)[-1])), None)


def choose_screen(desktop: Desktop, override: str | None = None) -> ScreenChoice:
    """The override when it names a screen; else a free secondary screen; else a free primary; else none."""
    named = find_screen(desktop.screens, override)
    if named is not None:
        return ScreenChoice(named, "override")
    free = [screen for screen in desktop.screens if not screen_taken(screen, desktop.foreground)]
    secondary = next((screen for screen in free if not screen.primary), None)
    if secondary is not None:
        return ScreenChoice(secondary, "free secondary screen")
    primary = next((screen for screen in free if screen.primary), None)
    if primary is not None:
        return ScreenChoice(primary, "free primary screen")
    return ScreenChoice(None, NO_FREE_SCREEN)


def centred(work: Rect, size: tuple[int, int]) -> tuple[int, int]:
    """The top-left that centres a window of `size` in `work`, never above or left of it."""
    width, height = size
    return work.left + max(0, (work.width - width) // 2), work.top + max(0, (work.height - height) // 2)


def centred_args(manifest: ThemeManifest, screens: Sequence[Screen]) -> dict[str, int]:
    """x/y centring a normal launch's window on the primary work area (pywebview does not centre)."""
    if not manifest.window.center or not screens:
        return {}
    primary = next((screen for screen in screens if screen.primary), screens[0])
    x, y = centred(primary.logical_work, manifest.window.size)
    return {"x": x, "y": y}


def place_test_window(size: tuple[int, int], desktop: Desktop,
                      override: str | None = None) -> dict[str, Any] | ExitCode:
    """A test window's window args on the chosen screen (no focus, on top), or exit code 8 without one."""
    override = (override or "").strip() or None
    choice = choose_screen(desktop, override)
    facts = _desktop_facts(desktop)
    if override and choice.reason != "override":
        emit_test_event("override_ignored", override=override, screens=facts["screens"])
    if choice.screen is None:
        emit_test_event("skipped", reason=NO_FREE_SCREEN, **facts)
        return ExitCode(EXIT_NO_FREE_SCREEN, f"skipped: {NO_FREE_SCREEN}")
    x, y = centred(choice.screen.logical_work, size)
    emit_test_event("screen_chosen", device=choice.screen.device, reason=choice.reason, dpi=choice.screen.dpi,
                    x=x, y=y, **facts)
    return {"x": x, "y": y, "focus": False, "on_top": True}


def window_placement(manifest: ThemeManifest, flags: Flags, *, window_process: bool, simulation: bool,
                     env: Mapping[str, str] = os.environ,
                     probe: Callable[[], Desktop] | None = None) -> dict[str, Any] | ExitCode:
    """The position part of app.native.window_args for this process (§6.2.11 step 8).

    A test window's screen is chosen once, by the server; the window process adds nothing, so the
    server's forwarded x, y, focus and on_top win NiceGUI's merge (its re-imported keys override).
    """
    if flags.browser:
        return {}
    if flags.test_window and not simulation:
        if window_process:
            return {}
        return place_test_window(manifest.window.size, (probe or probe_desktop)(), env.get(TEST_SCREEN_ENV))
    screens = probe().screens if probe is not None else probe_screens()
    return centred_args(manifest, screens)


def _desktop_facts(desktop: Desktop) -> dict[str, Any]:
    """What the test-window events record about the desktop."""
    fg = desktop.foreground
    return {
        "screens": [{"device": s.device, "primary": s.primary, "dpi": s.dpi,
                     "taken": screen_taken(s, fg)} for s in desktop.screens],
        "foreground": None if fg is None else {"hwnd": fg.hwnd, "visible": fg.visible, "minimized": fg.minimized,
                                               "monitor": fg.monitor, "shell": fg.shell},
        "notification_state": desktop.notification_state,
    }


def probe_screens() -> tuple[Screen, ...]:
    """Every screen, read-only (empty off Windows, without a desktop, or when Windows refuses the read)."""
    return probe_desktop(foreground=False).screens


def probe_desktop(*, foreground: bool = True) -> Desktop:
    """The screens, the foreground window and the notification state, read-only.

    A failed read gives an empty desktop: a normal window then opens where pywebview puts it, and a test
    window finds no free screen, so it is skipped rather than opened somewhere unknown.
    """
    win = _win32()
    if win is None:
        return Desktop((), None, None)
    try:
        screens, devices = _read_screens(win)
        if not foreground:
            return Desktop(screens, None, None)
        with _thread_dpi(win, _DPI_PER_MONITOR_V2):
            window = _foreground(win, devices)
        return Desktop(screens, window, _notification_state(win))
    except _WIN32_ERRORS as error:
        LOG.warning("Couldn't read the screens: %s", _first_line(error))
        return Desktop((), None, None)


def _read_screens(win: _Win32) -> tuple[tuple[Screen, ...], dict[int, str]]:
    """Physical rectangles and DPIs per monitor, plus the work areas pywebview's coordinates use.

    pywebview runs system-DPI aware and places the form at x/y times its system scale, so the logical
    work area is the system-aware rectangle divided by that scale: on every screen this equals the
    physical size at 96 / that screen's DPI.
    """
    physical: dict[int, tuple[str, Rect, Rect, bool]] = {}
    dpis: dict[int, int] = {}
    with _thread_dpi(win, _DPI_PER_MONITOR_V2):
        for handle in _monitor_handles(win):
            info = _monitor_info(win, handle)
            if info is not None:
                physical[handle] = info
                dpis[handle] = _monitor_dpi(win, handle)
    logical: dict[int, Rect] = {}
    with _thread_dpi(win, _DPI_SYSTEM_AWARE):
        scale = BASE_DPI / _system_dpi(win)
        for handle in physical:
            info = _monitor_info(win, handle)
            if info is not None:
                logical[handle] = info[2].scaled(scale)
    screens = [Screen(device=device, primary=primary, bounds=bounds, work=work, dpi=dpis[handle],
                      logical_work=logical.get(handle, work.scaled(BASE_DPI / dpis[handle])))
               for handle, (device, bounds, work, primary) in physical.items()]
    screens.sort(key=_device_order)
    return tuple(screens), {handle: info[0] for handle, info in physical.items()}


def _device_order(screen: Screen) -> tuple[int, str]:
    digits = re.search(r"(\d+)$", screen.device)
    return (int(digits.group(1)) if digits else sys.maxsize, screen.device.lower())


def _monitor_handles(win: _Win32) -> list[int]:
    found: list[int] = []

    def collect(handle: int, _hdc: int, _rect: Any, _data: int) -> bool:
        if handle:
            found.append(int(handle))
        return True

    callback = win.monitor_proc(collect)
    win.enum_monitors(None, None, callback, 0)
    return found


def _monitor_info(win: _Win32, handle: int) -> tuple[str, Rect, Rect, bool] | None:
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    if not win.monitor_info(handle, ctypes.byref(info)):
        return None
    return info.szDevice, _rect(info.rcMonitor), _rect(info.rcWork), bool(info.dwFlags & _MONITORINFOF_PRIMARY)


def _monitor_dpi(win: _Win32, handle: int) -> int:
    dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
    if win.monitor_dpi(handle, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
        return BASE_DPI
    return int(dpi_x.value) or BASE_DPI


def _system_dpi(win: _Win32) -> int:
    return int(win.system_dpi() or BASE_DPI) if win.system_dpi is not None else BASE_DPI


def _foreground(win: _Win32, devices: Mapping[int, str]) -> Foreground | None:
    hwnd = int(win.foreground() or 0)
    rect = wintypes.RECT()
    if not hwnd or not win.window_rect(hwnd, ctypes.byref(rect)):
        return None
    monitor = int(win.monitor_from_window(hwnd, _MONITOR_DEFAULTTONULL) or 0)
    return Foreground(hwnd=hwnd, visible=bool(win.is_visible(hwnd)), minimized=bool(win.is_iconic(hwnd)),
                      rect=_rect(rect), monitor=devices.get(monitor, ""), shell=_is_shell(win, hwnd))


def _is_shell(win: _Win32, hwnd: int) -> bool:
    return hwnd == int(win.shell_window() or 0) or _class_name(win, hwnd) in _SHELL_CLASSES


def _notification_state(win: _Win32) -> int | None:
    state = ctypes.c_int()
    return int(state.value) if win.notification_state(ctypes.byref(state)) == 0 else None


def _rect(rect: wintypes.RECT) -> Rect:
    return Rect(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


@contextlib.contextmanager
def _thread_dpi(win: _Win32, context: int) -> Iterator[None]:
    """Run the block with this thread at a DPI awareness (unchanged where Windows lacks the call)."""
    if win.thread_dpi is None:
        yield
        return
    previous = win.thread_dpi(ctypes.c_void_p(context))
    try:
        yield
    finally:
        if previous:
            win.thread_dpi(ctypes.c_void_p(previous))


def _class_name(win: _Win32, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    return buffer.value if win.class_name(hwnd, buffer, len(buffer)) else ""


def _window_text(win: _Win32, hwnd: int) -> str:
    """The title by WM_GETTEXT: GetWindowTextW returns "" for frameless windows of other processes."""
    buffer = ctypes.create_unicode_buffer(256)
    copied = ctypes.c_size_t()
    sent = win.send_timeout(hwnd, _WM_GETTEXT, len(buffer), ctypes.addressof(buffer), _SMTO_ABORTIFHUNG,
                            _GETTEXT_TIMEOUT_MS, ctypes.byref(copied))
    return buffer.value if sent else ""


# ---- test-window events and the window process --------------------------------------------------------

class _TestEventHandler(logging.StreamHandler):
    """Writes test-window events to stderr: the console, or logs\\gui*.log under pythonw."""


def enable_test_events() -> None:
    """Test windows: emit the voicetool.testwindow events as one JSON line each (both processes)."""
    if not any(isinstance(handler, _TestEventHandler) for handler in TEST_LOG.handlers):
        handler = _TestEventHandler()
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        TEST_LOG.addHandler(handler)
    TEST_LOG.setLevel(logging.INFO)
    TEST_LOG.propagate = False


def emit_test_event(event: str, **fields: Any) -> None:
    """One test-window event with wall-clock and performance-counter timestamps."""
    payload = {"event": event, "pid": os.getpid(), "t_wall": round(time.time(), 6),
               "t_perf": round(time.perf_counter(), 6), **fields}
    TEST_LOG.info("%s", json.dumps(payload, sort_keys=True, default=str))


def return_hwnd(env: Mapping[str, str] = os.environ) -> int | None:
    """The harness window VOICETOOL_TEST_RETURN_HWND names (decimal or 0x hex), or None."""
    raw = env.get(RETURN_HWND_ENV, "").strip()
    try:
        value = int(raw, 0) if raw else 0
    except ValueError:
        return None
    return value if value > 0 else None


def foreground_window() -> int:
    """GetForegroundWindow() (0 off Windows)."""
    win = _win32()
    return int(win.foreground() or 0) if win is not None else 0


def form_hwnd(form: Any) -> int:
    """The window handle of pywebview's WinForms form."""
    return int(form.Handle.ToInt64())


def prepare_window_icons(window_icon: Any) -> None:
    """After NiceGUI applies taskbar properties, synchronously replace both icon slots with sharp frames.

    pywebview's shown event flag is set before its asynchronous handlers finish, so waiting for it
    (or page load) cannot order a second writer against NiceGUI's apply_icon. Wrapping that writer
    preserves its taskbar properties and makes the 32/16 px frames its final writes.
    """
    if getattr(window_icon, "_voicetool_sized_icons", False):
        return
    original = window_icon.apply_icon

    def apply_icon(hwnd: int, title: str, icon_path: str) -> None:
        original(hwnd, title, icon_path)
        _ICONS[:] = send_sized_icons(hwnd, icon_path)

    window_icon.apply_icon = apply_icon
    window_icon._voicetool_sized_icons = True


def prevent_test_activation(form_type: Any) -> None:
    """Install the WinForms test-window adapter before pywebview constructs its first form.

    pywebview's focus=False sets WS_EX_NOACTIVATE, but Show(), WebView.Focus and restore can still ask
    for activation. A CBT hook on this GUI thread vetoes activation/focus before it happens. The smoke
    plans drive the app through its own intents and JavaScript, so they need no desktop input.
    Rendering, timers, native lifecycle events and shutdown run normally, with enabled controls.
    Forms with focus=True retain normal behaviour. No hook is installed on other apps or threads.
    """
    if getattr(form_type, "_voicetool_passive", False):
        return

    original_init = form_type.__init__

    def initialize(form: Any, window: Any, *args: Any, **kwargs: Any) -> None:
        release = _block_test_activation() if not window.focus else None
        try:
            original_init(form, window, *args, **kwargs)
        except BaseException:
            if release is not None:
                release()
            raise
        if release is not None:
            form.FormClosed += lambda *_: release()
        emit_test_event("passive_form", focus=bool(form.pywebview_window.focus), enabled=bool(form.Enabled))

    form_type.__init__ = initialize
    form_type._voicetool_passive = True


def _block_test_activation() -> Callable[[], None]:
    """Veto activation/focus on the test GUI thread, starting before any form/child HWND is created."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    thread_id = int(_bind(kernel32.GetCurrentThreadId, wintypes.DWORD)())
    signature = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    next_hook = _bind(user32.CallNextHookEx, wintypes.LPARAM, wintypes.HANDLE, ctypes.c_int,
                      wintypes.WPARAM, wintypes.LPARAM)
    unhook = _bind(user32.UnhookWindowsHookEx, wintypes.BOOL, wintypes.HANDLE)

    @signature
    def procedure(code: int, wparam: int, lparam: int) -> int:
        if code in (5, 9) and wparam:              # allow clearing focus, veto only gaining it
            emit_test_event("activation_blocked", code=code, hwnd=int(wparam))
            return 1
        return int(next_hook(None, code, wparam, lparam))

    install = _bind(user32.SetWindowsHookExW, wintypes.HANDLE, ctypes.c_int, signature,
                    wintypes.HINSTANCE, wintypes.DWORD)
    hook = install(5, procedure, None, thread_id)    # WH_CBT, this thread only; never a global hook
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    _PASSIVE_FORMS[thread_id] = (hook, procedure)

    def release() -> None:
        if thread_id in _PASSIVE_FORMS:
            unhook(hook)
            _PASSIVE_FORMS.pop(thread_id, None)

    return release


class FocusGuard:
    """Test windows: whenever our form is activated, the foreground goes straight back to `return_hwnd`.

    Allowed, because our process is the foreground process at that moment. The form's Activated event is
    hooked in pywebview's before_show (on the UI thread, before Show()), or straight away when before_show
    has already fired. window.native is set at the top of the form's constructor, before its handle exists,
    and reading Handle from this thread then would create the window on the wrong thread, so the form is
    never touched before before_show; window_ready() hooks again once the window is shown (a no-op when
    hooked), which covers a before_show that ran between the subscription and the check.
    """

    def __init__(self, return_to: int, *, set_foreground: Callable[[int], Any] | None = None,
                 foreground: Callable[[], int] | None = None) -> None:
        self.return_to = return_to
        self._set_foreground = set_foreground or _set_foreground_window
        self._foreground = foreground or foreground_window
        self._lock = threading.Lock()
        self._hooked = False
        self._hwnd = 0

    def attach(self, window: Any) -> None:
        """Hook `window`'s form when pywebview shows it (never calls show() or restore())."""
        window.events.before_show += self._before_show
        form = getattr(window, "native", None)
        if form is not None and window.events.before_show.is_set():
            self.hook(form)

    @property
    def hooked(self) -> bool:
        """True once the form's Activated event is hooked."""
        return self._hooked

    def _before_show(self, window: Any) -> None:
        self._guarded(lambda: self.hook(window.native))

    def hook(self, form: Any) -> None:
        """Subscribe to form.Activated once; hand the focus back now if the form already has it."""
        with self._lock:
            if self._hooked:
                return
            self._hooked = True
        form.Activated += self._on_activated
        self._hwnd = form_hwnd(form)
        if self._hwnd == self._foreground():
            self.return_focus()

    def _on_activated(self, *_: Any) -> None:
        # WinForms can report Activated even when the CBT hook vetoed the Windows operation.
        # Returning focus then would itself disturb the foreground app and create a false incident.
        if self._hwnd == self._foreground():
            self._guarded(self.return_focus)

    @staticmethod
    def _guarded(action: Callable[[], Any]) -> None:
        """These run inside WinForms and pywebview event handlers: nothing may raise into them."""
        try:
            action()
        except Exception as error:
            LOG.warning("The focus guard failed: %s", _first_line(error))

    def return_focus(self) -> bool:
        """SetForegroundWindow(return_to), recorded as a focus_returned event."""
        started = time.perf_counter()
        ok = bool(self._set_foreground(self.return_to))
        emit_test_event("focus_returned", to=self.return_to, ok=ok, foreground=self._foreground(),
                        took_ms=round((time.perf_counter() - started) * 1000, 3))
        return ok


def window_ready(window: Any, *, icon: str | os.PathLike[str], test_window: bool,
                 env: Mapping[str, str] = os.environ, wait_s: float = WINDOW_WAIT_S) -> None:
    """pywebview's start callback in the window process: the focus guard, then the sharp taskbar icons.

    The 32 px and 16 px frames are also applied after page load. prepare_window_icons orders the
    authoritative sized writes after NiceGUI's asynchronous shown handler, which may finish later.
    """
    try:
        target = return_hwnd(env) if test_window else None
        guard = FocusGuard(target) if target else None
        if guard is not None:
            guard.attach(window)
        if not window.events.shown.wait(wait_s):
            return
        if guard is not None:
            guard.hook(window.native)           # no-op once hooked; catches a before_show this thread missed
        hwnd = form_hwnd(window.native)
        if test_window:
            emit_test_event("window_shown", hwnd=hwnd, foreground=foreground_window(),
                            enabled=bool(getattr(window.native, "Enabled", True)))
        window.events.loaded.wait(wait_s)
        _ICONS[:] = send_sized_icons(hwnd, os.fspath(icon))
    except Exception as error:
        LOG.warning("Window setup failed: %s", _first_line(error))


def send_sized_icons(hwnd: int, icon: str) -> list[int]:
    """WM_SETICON with the ICON_BIG (SM_CXICON) and ICON_SMALL (SM_CXSMICON) frames; keep the handles."""
    win = _win32()
    if win is None or not hwnd:
        return []
    handles: list[int] = []
    for which, (width, height) in ((_ICON_BIG, (_SM_CXICON, _SM_CYICON)),
                                   (_ICON_SMALL, (_SM_CXSMICON, _SM_CYSMICON))):
        handle = int(win.load_image(None, icon, _IMAGE_ICON, win.metric(width), win.metric(height),
                                    _LR_LOADFROMFILE) or 0)
        if handle:
            win.send_message(hwnd, _WM_SETICON, which, handle)
            handles.append(handle)
    return handles


def _set_foreground_window(hwnd: int) -> bool:
    win = _win32()
    return bool(win.set_foreground(hwnd)) if win is not None else False


def _message_box(text: str) -> None:
    """A modal error box (never under pytest: fail() raises BootFailure there)."""
    win = _win32()
    if win is not None:
        win.message_box(None, text, app_name(), _MB_OK | _MB_ICONERROR)


# ---- Win32 --------------------------------------------------------------------------------------------

_DPI_SYSTEM_AWARE = -2              # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE: pywebview's coordinate space
_DPI_PER_MONITOR_V2 = -4            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: physical pixels
_MDT_EFFECTIVE_DPI = 0
_MONITORINFOF_PRIMARY = 1
_MONITOR_DEFAULTTONULL = 0
_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
_WINFORMS_CLASS = "WindowsForms10."
_SW_RESTORE = 9
_WM_GETTEXT = 0x000D
_WM_SETICON = 0x0080
_SMTO_ABORTIFHUNG = 0x0002
_GETTEXT_TIMEOUT_MS = 200
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_SM_CXICON, _SM_CYICON, _SM_CXSMICON, _SM_CYSMICON = 11, 12, 49, 50
_MB_OK, _MB_ICONERROR = 0x0, 0x10
_WIN32_ERRORS = (OSError, ValueError, AttributeError, ctypes.ArgumentError)   # what a refused Win32 read raises


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def _bind(function: Any, restype: Any, *argtypes: Any) -> Any:
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


def _optional(library: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    """A function newer Windows versions have, or None."""
    function = getattr(library, name, None)
    return _bind(function, restype, *argtypes) if function is not None else None


class _Win32:
    """ctypes bindings on private WinDLL instances, so the argtypes set here never reach other modules."""

    def __init__(self) -> None:
        hwnd, flag, uint, number = wintypes.HWND, wintypes.BOOL, wintypes.UINT, ctypes.c_int
        self.monitor_proc = ctypes.WINFUNCTYPE(flag, wintypes.HMONITOR, wintypes.HDC,
                                               ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        self.window_proc = ctypes.WINFUNCTYPE(flag, hwnd, wintypes.LPARAM)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shcore = ctypes.WinDLL("shcore")
        shell32 = ctypes.WinDLL("shell32")
        self.enum_monitors = _bind(user32.EnumDisplayMonitors, flag, wintypes.HDC, ctypes.c_void_p,
                                   self.monitor_proc, wintypes.LPARAM)
        self.monitor_info = _bind(user32.GetMonitorInfoW, flag, wintypes.HMONITOR, ctypes.c_void_p)
        self.monitor_dpi = _bind(shcore.GetDpiForMonitor, ctypes.c_long, wintypes.HMONITOR, number,
                                 ctypes.POINTER(uint), ctypes.POINTER(uint))
        self.thread_dpi = _optional(user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p, ctypes.c_void_p)
        self.system_dpi = _optional(user32, "GetDpiForSystem", uint)
        self.foreground = _bind(user32.GetForegroundWindow, hwnd)
        self.is_visible = _bind(user32.IsWindowVisible, flag, hwnd)
        self.is_iconic = _bind(user32.IsIconic, flag, hwnd)
        self.window_rect = _bind(user32.GetWindowRect, flag, hwnd, ctypes.POINTER(wintypes.RECT))
        self.monitor_from_window = _bind(user32.MonitorFromWindow, wintypes.HMONITOR, hwnd, wintypes.DWORD)
        self.class_name = _bind(user32.GetClassNameW, number, hwnd, wintypes.LPWSTR, number)
        self.shell_window = _bind(user32.GetShellWindow, hwnd)
        self.set_foreground = _bind(user32.SetForegroundWindow, flag, hwnd)
        self.show_window = _bind(user32.ShowWindow, flag, hwnd, number)
        self.enum_windows = _bind(user32.EnumWindows, flag, self.window_proc, wintypes.LPARAM)
        self.send_timeout = _bind(user32.SendMessageTimeoutW, wintypes.LPARAM, hwnd, uint, wintypes.WPARAM,
                                  wintypes.LPARAM, uint, uint, ctypes.POINTER(ctypes.c_size_t))
        self.send_message = _bind(user32.SendMessageW, wintypes.LPARAM, hwnd, uint, wintypes.WPARAM,
                                  wintypes.LPARAM)
        self.load_image = _bind(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, uint,
                                number, number, uint)
        self.metric = _bind(user32.GetSystemMetrics, number, number)
        self.message_box = _bind(user32.MessageBoxW, number, hwnd, wintypes.LPCWSTR, wintypes.LPCWSTR, uint)
        self.notification_state = _bind(shell32.SHQueryUserNotificationState, ctypes.c_long,
                                        ctypes.POINTER(ctypes.c_int))


@cache
def _win32() -> _Win32 | None:
    """The process's bindings (None off Windows)."""
    return _Win32() if sys.platform == "win32" else None
