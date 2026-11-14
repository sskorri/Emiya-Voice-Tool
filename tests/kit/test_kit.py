"""Kit tests (U5): window, dialogs, Explorer, KitPort, Player, KeyDispatcher and the two node tests.

Nothing here opens a window, a browser or Explorer: app.native.main_window is a fake (or None),
os.startfile is replaced for every test, and the NiceGUI elements live on a bare Client that has
no server and no socket. The browser halves of the components run in node (vt_keys.mjs,
vt_player.mjs) against fake events and a fake <audio>.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import multiprocessing
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import webview
from nicegui import app, background_tasks, binding, core, events, ui
from nicegui.client import Client
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.native import native as native_mode_queues
from nicegui.page import page
from nicegui.server import Server
from nicegui.testing.general import nicegui_reset_globals
from webview.util import parse_file_type

from ui.contract.plans import PlayerTarget
from ui.contract.ports import KitPort
from ui.contract.viewmodels import PlaybackVM
from ui.kit import files
from ui.kit import window as kit_window
from ui.kit.audio import Player, PlayerReport, parse_report
from ui.kit.keys import KeyDispatcher, combo_map
from ui.kit.port import BrowserKit, NativeKit, make_kit
from ui.kit.window import DRAG_CLASS, SHUTDOWN_FALLBACK_S, native_lock, quit_app

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
AUDIO_FILTERS = ("Audio clips (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)")
WAV_FILTER = ("WAV audio (*.wav)",)
KIT_MEMBERS = ("minimize", "open_folder", "pick_file", "pick_folder", "quit", "reveal", "save_as")


# ---- guards and fakes ------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _kit_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """HF_HUB_OFFLINE=1, no native window, and Explorer can never open from a test."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(app.native, "main_window", None)

    def no_explorer(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"a test tried to open Explorer: {args} {kwargs}")

    monkeypatch.setattr(os, "startfile", no_explorer)


class FakeWindow:
    """Stands in for app.native.main_window (a WindowProxy) and opens nothing."""

    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.dialogs: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.active = 0
        self.max_active = 0
        self.gate: asyncio.Event | None = None

    async def create_file_dialog(self, **kwargs: Any) -> Any:
        self.dialogs.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1
        return self.answers.pop(0) if self.answers else None

    def destroy(self) -> None:
        self.events.append("destroy")

    def minimize(self) -> None:
        self.events.append("minimize")


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> FakeWindow:
    fake = FakeWindow()
    monkeypatch.setattr(app.native, "main_window", fake)
    return fake


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, Callable[[], Any], dict[str, Any]]]:
    """app.timer calls, recorded instead of scheduled."""
    calls: list[tuple[float, Callable[[], Any], dict[str, Any]]] = []
    monkeypatch.setattr(app, "timer", lambda interval, callback, **kwargs: calls.append((interval, callback, kwargs)))
    return calls


@pytest.fixture
def shutdowns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """app.shutdown calls, recorded; no uvicorn server exists unless a test adds one."""
    calls: list[str] = []
    monkeypatch.setattr(app, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.delattr(Server, "instance", raising=False)
    return calls


@pytest.fixture
def startfile(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(os, "startfile", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


@pytest.fixture
async def page_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Client]:
    """A NiceGUI client on the test's loop: no server, no socket, no browser."""
    with nicegui_reset_globals():
        monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
        client = Client(page("/"))
        try:
            yield client
        finally:
            client.delete()


def fire(element: ui.element, event_type: str, args: Any) -> None:
    """Deliver a browser event to the element's listeners, as NiceGUI's user simulation does."""
    for listener in list(element._event_listeners.values()):  # noqa: SLF001
        if listener.type == event_type:
            events.handle_event(listener.handler, events.GenericEventArguments(
                sender=element, client=element.client, args=args))


async def wait_for(predicate: Callable[[], Any], *, ticks: int = 200) -> Any:
    """Let the loop run until predicate() is truthy; its value."""
    for _ in range(ticks):
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def elements_of(client: Client, kind: type) -> list[Any]:
    return [element for element in client.elements.values() if isinstance(element, kind)]


# ---- window: drag class, minimize, quit --------------------------------------------------------------

def test_drag_class_is_pywebviews_drag_selector() -> None:
    assert DRAG_CLASS == webview.settings["DRAG_REGION_SELECTOR"].lstrip(".") == "pywebview-drag-region"


def test_minimize_without_a_native_window_is_false() -> None:
    assert kit_window.minimize() is False


def test_minimize_minimizes_the_native_window(window: FakeWindow) -> None:
    assert kit_window.minimize() is True
    assert window.events == ["minimize"]


def test_quit_without_a_window_or_server_does_nothing(shutdowns: list[str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="voicetool.kit")
    quit_app()
    assert shutdowns == []
    assert "no server is running" in caplog.text


def test_quit_without_a_window_stops_the_server(shutdowns: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Server, "instance", object(), raising=False)
    quit_app()
    assert shutdowns == ["shutdown"]


def test_quit_closes_the_window_first_with_a_delayed_fallback(
        window: FakeWindow, timers: list, shutdowns: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    quit_app()
    assert window.events == ["destroy"]
    assert shutdowns == []
    assert [(interval, kwargs) for interval, _, kwargs in timers] == [(SHUTDOWN_FALLBACK_S, {"once": True})]
    assert SHUTDOWN_FALLBACK_S == 5.0
    monkeypatch.setattr(Server, "instance", object(), raising=False)
    timers[0][1]()  # the window never went away: the fallback stops the server
    assert shutdowns == ["shutdown"]


async def test_quit_waits_for_an_open_dialog(
        window: FakeWindow, timers: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
    window.gate = asyncio.Event()
    window.answers = [("C:\\clips\\aiko.wav",)]
    picking = asyncio.create_task(files.pick_file(title="Pick a clip", filters=AUDIO_FILTERS, start_dir=None))
    await wait_for(lambda: window.active == 1)
    assert native_lock.locked()
    quit_app()
    await asyncio.sleep(0.05)
    assert window.events == []                  # the window stays while its dialog is open
    window.gate.set()
    assert await picking == Path("C:\\clips\\aiko.wav")
    await wait_for(lambda: window.events)
    assert window.events == ["destroy"]
    assert len(timers) == 1
    await wait_for(lambda: not [task for task in background_tasks.running_tasks
                                if task.get_name() == "quit after the open dialog"])


def test_native_lock_serialises_on_every_event_loop() -> None:
    """The lock is per loop, so a loop that closed never leaves a lock bound to it behind."""
    async def two_requests() -> int:
        active = peak = 0

        async def request() -> None:
            nonlocal active, peak
            async with native_lock:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.005)
                active -= 1

        await asyncio.gather(request(), request(), request())
        return peak

    assert asyncio.run(two_requests()) == 1
    assert asyncio.run(two_requests()) == 1     # a new loop gets its own lock
    assert native_lock.locked() is False        # outside any loop


# ---- native file dialogs -------------------------------------------------------------------------------

FILTER_SAMPLES = [
    "WAV audio (*.wav)", "Audio clips (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)", "Ton (*.WAV)",
    "WAV audio(*.wav)", "Archives (*.tar.gz)", "Any (*)", "Audio, clips (*.wav)", "Audio (*.wav, *.mp3)",
    "(*.wav)", "Audio-clips (*.wav)", "Audio (wav)", "Audio clips", "",
]


@pytest.mark.parametrize("sample", FILTER_SAMPLES)
def test_check_filters_agrees_with_pywebview(sample: str) -> None:
    try:
        parse_file_type(sample)
        pywebview_ok = True
    except ValueError:
        pywebview_ok = False
    try:
        files.check_filters((sample,))
        kit_ok = True
    except ValueError:
        kit_ok = False
    assert kit_ok is pywebview_ok


def test_dialog_types_are_pywebviews() -> None:
    assert (files.OPEN_DIALOG, files.FOLDER_DIALOG, files.SAVE_DIALOG) == (
        int(webview.FileDialog.OPEN), int(webview.FileDialog.FOLDER), int(webview.FileDialog.SAVE))


async def test_dialog_requests(window: FakeWindow, tmp_path: Path) -> None:
    window.answers = [("E:\\clips\\aiko.wav",), ("E:\\exports",), ("E:\\exports\\aiko_take7.wav",)]
    assert await files.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=tmp_path) == Path("E:\\clips\\aiko.wav")
    assert await files.pick_folder(title="Export to", start_dir=None) == Path("E:\\exports")
    saved = await files.save_as(default_name="aiko_take7.wav", filters=WAV_FILTER, start_dir=tmp_path)
    assert saved == Path("E:\\exports\\aiko_take7.wav")
    assert window.dialogs == [
        {"dialog_type": 10, "directory": str(tmp_path), "allow_multiple": False, "save_filename": "",
         "file_types": AUDIO_FILTERS},
        {"dialog_type": 20, "directory": "", "allow_multiple": False, "save_filename": "", "file_types": ()},
        {"dialog_type": 30, "directory": str(tmp_path), "allow_multiple": False, "save_filename": "aiko_take7.wav",
         "file_types": WAV_FILTER},
    ]
    assert not native_lock.locked()


async def test_dialogs_run_one_at_a_time(window: FakeWindow) -> None:
    unguarded = FakeWindow()
    await asyncio.gather(*(unguarded.create_file_dialog() for _ in range(3)))
    assert unguarded.max_active == 3            # the fake does see overlapping requests ...

    window.answers = [("a.wav",), ("folder",), ("b.wav",)]
    got = await asyncio.gather(
        files.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None),
        files.pick_folder(title="Folder", start_dir=None),
        files.save_as(default_name="b.wav", filters=WAV_FILTER, start_dir=None))
    assert window.max_active == 1               # ... and the kit's never overlap
    assert got == [Path("a.wav"), Path("folder"), Path("b.wav")]   # each answer reaches its own caller


@pytest.mark.parametrize("answer", [None, (), "", [""]])
async def test_cancel_returns_none(window: FakeWindow, answer: Any) -> None:
    window.answers = [answer]
    assert await files.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None) is None


async def test_a_plain_string_answer_is_a_path(window: FakeWindow) -> None:
    window.answers = ["E:\\exports\\aiko_take7.wav"]
    assert await files.save_as(default_name="aiko_take7.wav", filters=WAV_FILTER, start_dir=None) == Path(
        "E:\\exports\\aiko_take7.wav")


async def test_a_bad_filter_fails_before_any_request(window: FakeWindow) -> None:
    with pytest.raises(ValueError, match="Description"):
        await files.pick_file(title="Pick", filters=("Audio, clips (*.wav)",), start_dir=None)
    with pytest.raises(ValueError):
        await files.save_as(default_name="x.wav", filters=("WAV (*.wav, *.mp3)",), start_dir=None)
    assert window.dialogs == []
    assert not native_lock.locked()


async def test_dialogs_without_a_native_window_return_none() -> None:
    assert await files.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None) is None
    assert await files.pick_folder(title="Folder", start_dir=None) is None
    assert await files.save_as(default_name="a.wav", filters=WAV_FILTER, start_dir=None) is None


# ---- Explorer ------------------------------------------------------------------------------------------

def _explorer() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


def test_reveal_selects_the_file_in_explorer(tmp_path: Path, startfile: list, monkeypatch: pytest.MonkeyPatch) -> None:
    take = tmp_path / "aiko take7.wav"
    take.write_bytes(b"RIFF")
    files.reveal(take)
    monkeypatch.chdir(tmp_path)
    files.reveal(Path("aiko take7.wav"))       # relative paths are made absolute
    expected = ((_explorer(),), {"arguments": f'/select,"{take}"'})
    assert startfile == [expected, expected]


def test_reveal_refuses_a_missing_file(tmp_path: Path, startfile: list) -> None:
    with pytest.raises(FileNotFoundError):
        files.reveal(tmp_path / "gone.wav")
    assert startfile == []


def test_open_folder(tmp_path: Path, startfile: list) -> None:
    files.open_folder(tmp_path)
    assert startfile == [((str(tmp_path),), {})]
    (tmp_path / "take.wav").write_bytes(b"RIFF")
    for not_a_folder in (tmp_path / "take.wav", tmp_path / "missing"):
        with pytest.raises(NotADirectoryError):
            files.open_folder(not_a_folder)    # a file would open in its player, not in Explorer
    assert len(startfile) == 1


# ---- small helpers -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("filters", "accept"), [
    (("Audio clips (*.wav;*.flac;*.ogg;*.mp3)",), ".wav,.flac,.ogg,.mp3"),
    (AUDIO_FILTERS, ""),
    (("WAV audio (*.WAV)", "Wave (*.wav)"), ".wav"),
    (("Any (*)",), ""),
    ((), ""),
])
def test_accept_of(filters: tuple[str, ...], accept: str) -> None:
    assert files.accept_of(filters) == accept


@pytest.mark.parametrize(("name", "safe"), [
    ("clip.wav", "clip.wav"),
    ("C:\\Users\\me\\clip.wav", "clip.wav"),
    ("../../etc/clip.wav", "clip.wav"),
    ('a<b>c:d"e|f?g*.wav', "a_b_c_d_e_f_g_.wav"),
    ("con.wav", "_con.wav"),
    ("LPT1", "_LPT1"),
    ("  .  ", "upload"),
    ("", "upload"),
    ("x" * 300 + ".wav", "x" * 116 + ".wav"),
])
def test_safe_name(name: str, safe: str) -> None:
    assert files.safe_name(name) == safe


# ---- KitPort ---------------------------------------------------------------------------------------------

def _protocol_members() -> tuple[str, ...]:
    return tuple(sorted(name for name, member in vars(KitPort).items()
                        if inspect.isfunction(member) and not name.startswith("_")))


def _parameters(func: Callable[..., Any]) -> list[tuple[str, Any, Any, Any]]:
    return [(p.name, p.kind, p.default, p.annotation) for p in inspect.signature(func).parameters.values()]


def test_the_protocol_is_the_spec() -> None:
    assert _protocol_members() == KIT_MEMBERS


@pytest.mark.parametrize("browser", [False, True])
def test_make_kit_satisfies_kitport(browser: bool) -> None:
    kit = make_kit(browser=browser)
    assert isinstance(kit, KitPort)
    assert isinstance(kit, BrowserKit if browser else NativeKit)
    assert isinstance(make_kit(browser), type(kit))


@pytest.mark.parametrize("browser", [False, True])
@pytest.mark.parametrize("name", KIT_MEMBERS)
def test_kit_signatures_match_the_protocol(browser: bool, name: str) -> None:
    expected, actual = getattr(KitPort, name), getattr(type(make_kit(browser)), name)
    assert inspect.iscoroutinefunction(actual) is inspect.iscoroutinefunction(expected)
    assert _parameters(actual) == _parameters(expected)
    assert inspect.signature(actual).return_annotation == inspect.signature(expected).return_annotation


async def test_native_kit_uses_window_and_files(
        window: FakeWindow, timers: list, startfile: list, tmp_path: Path) -> None:
    kit = make_kit(browser=False)
    window.answers = [("E:\\clips\\a.wav",), ("E:\\exports",), ("E:\\exports\\b.wav",)]
    assert await kit.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None) == Path("E:\\clips\\a.wav")
    assert await kit.pick_folder(title="Folder", start_dir=None) == Path("E:\\exports")
    assert await kit.save_as(default_name="b.wav", filters=WAV_FILTER, start_dir=None) == Path("E:\\exports\\b.wav")
    assert [d["dialog_type"] for d in window.dialogs] == [10, 20, 30]
    kit.open_folder(tmp_path)
    (tmp_path / "t.wav").write_bytes(b"RIFF")
    kit.reveal(tmp_path / "t.wav")
    assert [args[0] for args, _ in startfile] == [str(tmp_path), _explorer()]
    assert kit.minimize() is True
    kit.quit()
    assert window.events == ["minimize", "destroy"]
    assert len(timers) == 1


async def test_native_kit_is_safe_without_a_window(shutdowns: list[str]) -> None:
    kit = make_kit(browser=False)
    assert kit.minimize() is False
    assert await kit.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None) is None
    kit.quit()
    assert shutdowns == []


async def test_browser_kit_has_no_window(window: FakeWindow, shutdowns: list[str],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    kit = make_kit(browser=True)
    assert kit.minimize() is False              # even if a window object were around
    assert await kit.pick_folder(title="Folder", start_dir=None) is None
    assert await kit.pick_file(title="Pick", filters=AUDIO_FILTERS, start_dir=None) is None   # outside a page
    assert await kit.save_as(default_name="a.wav", filters=WAV_FILTER, start_dir=None) is None
    assert window.dialogs == [] and window.events == []
    monkeypatch.setattr(app.native, "main_window", None)
    monkeypatch.setattr(Server, "instance", object(), raising=False)
    kit.quit()
    assert shutdowns == ["shutdown"]


# ---- Player ----------------------------------------------------------------------------------------------

def _target_props(target: PlayerTarget) -> dict[str, Any]:
    return {"seq": target.seq, "src": target.src, "playing": target.playing, "at": target.at, "muted": target.muted}


async def test_player_starts_from_its_target(page_client: Client) -> None:
    first = PlayerTarget(seq=3, src="/media/take/7", playing=True, at=1.5)
    with page_client:
        player = Player(first)
        empty = Player()
    assert player.props["target"] == _target_props(first)
    assert empty.props["target"] == {"seq": 0, "src": "", "playing": False, "at": None, "muted": False}
    assert Player.component.path.name == "vt_player.js"
    assert player.audio_id == player.html_id == f"c{player.id}"
    assert "state" in {listener.type for listener in player._event_listeners.values()}  # noqa: SLF001


async def test_player_pushes_every_new_target(page_client: Client) -> None:
    with page_client:
        player = Player()
    for target in (PlayerTarget(1, "/media/take/7", True, None), PlayerTarget(2, "/media/take/7", True, 2.0),
                   PlayerTarget(3, "/media/take/5", False, 2.0), PlayerTarget(4, "", False, None)):
        player.target = target
        assert player.props["target"] == _target_props(target)
    with pytest.raises(TypeError):
        player.target = {"seq": 9, "src": "/x"}
    with pytest.raises(TypeError), page_client:
        Player({"seq": 1})  # type: ignore[arg-type]


async def test_player_follows_the_playback_vm_without_polling(page_client: Client) -> None:
    vm = PlaybackVM()
    with page_client:
        player = Player().bind_target_from(vm)
    vm.target = PlayerTarget(seq=1, src="/media/take/8", playing=True, at=None)
    assert player.target == vm.target
    assert player.props["target"]["src"] == "/media/take/8"
    assert binding.active_links == []           # R9: bound to a bindable property, never polled


async def test_player_reports_reach_on_state(page_client: Client) -> None:
    calls: list[tuple[Any, ...]] = []
    with page_client:
        player = Player(PlayerTarget(seq=4, src="/media/take/7", playing=True),
                        on_state=lambda *args: calls.append(args))
    fire(player, "state", {"seq": 4, "playing": True, "position": 1.25, "duration": 4.6, "ended": False, "error": ""})
    fire(player, "state", {"seq": 3, "playing": False, "position": 0, "duration": 0, "ended": True, "error": ""})
    fire(player, "state", {"playing": False, "position": 4.6, "duration": 4.6, "ended": True})   # no seq: accepted
    fire(player, "state", "not a report")
    assert calls == [(True, 1.25, 4.6, False, ""), (False, 4.6, 4.6, True, "")]


async def test_player_awaits_an_async_on_state(page_client: Client) -> None:
    seen: list[float] = []

    async def on_state(playing: bool, position: float, duration: float, ended: bool, error: str) -> None:
        await asyncio.sleep(0)
        seen.append(position)

    with page_client:
        player = Player(PlayerTarget(seq=1, src="/media/take/7"), on_state=on_state)
    fire(player, "state", {"seq": 1, "playing": False, "position": 2.5, "duration": 4.6, "ended": False, "error": ""})
    assert await wait_for(lambda: seen) == [2.5]


@pytest.mark.parametrize(("args", "report"), [
    ({"seq": 2, "playing": True, "position": 1.5, "duration": 3, "ended": False, "error": ""},
     PlayerReport(2, True, 1.5, 3.0, False, "")),
    ({"seq": True, "playing": 1, "position": -1, "duration": None, "ended": "yes", "error": None},
     PlayerReport(None, False, 0.0, 0.0, False, "")),
    ({"position": float("inf"), "duration": float("nan"), "error": "x" * 400},
     PlayerReport(None, False, 0.0, 0.0, False, "x" * 300)),
    ({"position": "1.5", "error": {"code": 4}}, PlayerReport(None, False, 0.0, 0.0, False, "")),
])
def test_parse_report_sanitises(args: dict[str, Any], report: PlayerReport) -> None:
    assert parse_report(args) == report


@pytest.mark.parametrize("args", [None, [], "x", 3])
def test_parse_report_refuses_non_reports(args: Any) -> None:
    assert parse_report(args) is None


# ---- KeyDispatcher ---------------------------------------------------------------------------------------

DEFAULT_COMBOS = {"Ctrl+Enter": "gen", "Ctrl+Shift+Enter": "gen3", "Space": "play", "Alt+Up": "prev",
                  "Alt+Down": "next", "Ctrl+B": "ab", "Ctrl+S": "save", "Ctrl+K": "search"}


async def test_key_dispatcher_props(page_client: Client) -> None:
    with page_client:
        keys = KeyDispatcher(DEFAULT_COMBOS)
        bare = KeyDispatcher()
    assert keys.props["combos"] == DEFAULT_COMBOS and keys.props["suspended"] is False
    assert bare.props["combos"] == {} and bare.props["suspended"] is False
    assert KeyDispatcher.component.path.name == "vt_keys.js"
    assert "command" in {listener.type for listener in keys._event_listeners.values()}  # noqa: SLF001
    keys.suspended = True
    assert keys.props["suspended"] is True
    keys.set_combos({"Ctrl+G": "gen", "Ctrl+K": ""})
    assert keys.props["combos"] == {"Ctrl+G": "gen"}
    with pytest.raises(TypeError):
        keys.set_combos(["Ctrl+G"])  # type: ignore[arg-type]


async def test_key_dispatcher_sends_bound_commands_only(page_client: Client) -> None:
    sent: list[str] = []
    with page_client:
        keys = KeyDispatcher(DEFAULT_COMBOS, on_command=sent.append)
    fire(keys, "command", {"id": "gen", "combo": "Ctrl+Enter"})
    fire(keys, "command", {"id": "play"})                       # the combo is informational
    fire(keys, "command", {"id": "delete_everything", "combo": "Ctrl+D"})
    fire(keys, "command", {"id": 7})
    fire(keys, "command", "gen")
    keys.set_combos({"Ctrl+G": "gen"})
    fire(keys, "command", {"id": "play", "combo": "Space"})     # unbound since: dropped
    fire(keys, "command", {"id": "gen", "combo": "Ctrl+G"})
    assert sent == ["gen", "play", "gen"]


async def test_key_dispatcher_drops_commands_while_suspended(page_client: Client) -> None:
    sent: list[str] = []
    with page_client:
        keys = KeyDispatcher(DEFAULT_COMBOS, suspended=True, on_command=sent.append)
    assert keys.props["suspended"] is True
    fire(keys, "command", {"id": "gen", "combo": "Ctrl+Enter"})
    keys.suspended = False
    fire(keys, "command", {"id": "gen", "combo": "Ctrl+Enter"})
    assert sent == ["gen"]


async def test_key_dispatcher_binds_without_polling(page_client: Client) -> None:
    @binding.bindable_dataclass
    class Listening:
        rows: tuple[bool, ...] = (False, False)
        keys: tuple[tuple[str, str], ...] = (("Ctrl+Enter", "gen"),)

    source = Listening()
    with page_client:
        keys = KeyDispatcher()
    keys.bind_suspended_from(source, "rows", backward=any)
    keys.bind_combos_from(source, "keys", backward=dict)
    assert keys.props["suspended"] is False and keys.props["combos"] == {"Ctrl+Enter": "gen"}
    source.rows = (False, True)
    source.keys = (("Ctrl+G", "gen"),)
    assert keys.props["suspended"] is True and keys.props["combos"] == {"Ctrl+G": "gen"}
    assert binding.active_links == []


def test_combo_map() -> None:
    assert combo_map({"Ctrl+K": "search", "": "x", "Space": ""}) == {"Ctrl+K": "search"}
    with pytest.raises(TypeError):
        combo_map("Ctrl+K")  # type: ignore[arg-type]


# ---- --browser stand-ins ---------------------------------------------------------------------------------

async def test_browser_pick_file_takes_an_upload(page_client: Client, tmp_path: Path) -> None:
    browser = files.BrowserFiles(scratch=tmp_path / "scratch")

    async def pick() -> Path | None:
        with page_client:                       # as a behaviour runs inside a page event
            return await browser.pick_file(title="Pick a reference clip", filters=("Audio clips (*.wav;*.mp3)",))

    picking = asyncio.create_task(pick())
    [upload] = await wait_for(lambda: elements_of(page_client, ui.upload))
    [dialog] = elements_of(page_client, ui.dialog)
    assert dialog.value is True
    assert upload.props["accept"] == ".wav,.mp3"
    assert upload.props["auto-upload"] is True and upload.props["max-files"] == 1
    await upload.handle_uploads([SmallFileUpload(name="C:\\fakepath\\aiko.wav", content_type="audio/wav",
                                                 _data=b"RIFF-aiko")])
    path = await asyncio.wait_for(picking, 5)
    assert path is not None and path.name == "aiko.wav" and path.read_bytes() == b"RIFF-aiko"
    assert path.is_relative_to(tmp_path / "scratch")
    assert elements_of(page_client, ui.dialog) == []    # the dialog is removed after use


async def test_browser_pick_file_cancel(page_client: Client, tmp_path: Path) -> None:
    browser = files.BrowserFiles(scratch=tmp_path)

    async def pick() -> Path | None:
        with page_client:
            return await browser.pick_file(title="Pick", filters=AUDIO_FILTERS)

    picking = asyncio.create_task(pick())
    [upload] = await wait_for(lambda: elements_of(page_client, ui.upload))
    assert "accept" not in upload.props          # "All files (*.*)" allows any file
    [cancel] = [b for b in elements_of(page_client, ui.button) if b.text == "Cancel"]
    fire(cancel, "click", None)
    assert await asyncio.wait_for(picking, 5) is None

    picking = asyncio.create_task(pick())
    [dialog] = await wait_for(lambda: elements_of(page_client, ui.dialog))
    dialog.close()                              # Escape or a click outside
    assert await asyncio.wait_for(picking, 5) is None


async def test_browser_save_as_downloads_when_the_caller_is_done(page_client: Client, tmp_path: Path) -> None:
    browser = files.BrowserFiles(scratch=tmp_path / "scratch")

    async def save() -> Path | None:            # as PlaybackController.save_as: ask, then copy the take there
        with page_client:
            target = browser.save_as(default_name="aiko_take7.wav", filters=WAV_FILTER)
        assert target is not None
        target.write_bytes(b"RIFF-take-7")
        return target

    target = await asyncio.create_task(save())
    assert target is not None and target.name == "aiko_take7.wav"
    downloads = await wait_for(lambda: [m for m in page_client.outbox.messages if m[1] == "download"])
    assert downloads == [(page_client.id, "download", {
        "src": b"RIFF-take-7", "filename": "aiko_take7.wav",
        "media_type": mimetypes.guess_type("aiko_take7.wav")[0] or ""})]
    assert not target.parent.exists()           # the scratch copy is gone


async def test_browser_save_as_sends_nothing_when_nothing_was_written(page_client: Client, tmp_path: Path) -> None:
    browser = files.BrowserFiles(scratch=tmp_path / "scratch")

    async def save() -> Path | None:
        with page_client:
            return browser.save_as(default_name="aiko_take7.wav")

    target = await asyncio.create_task(save())
    await asyncio.sleep(0.01)
    assert target is not None and not target.parent.exists()
    assert [m for m in page_client.outbox.messages if m[1] == "download"] == []


async def test_browser_stand_ins_need_a_page(tmp_path: Path) -> None:
    browser = files.BrowserFiles(scratch=tmp_path)
    assert await browser.pick_file(title="Pick", filters=AUDIO_FILTERS) is None
    assert browser.save_as(default_name="a.wav") is None
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        await browser.pick_file(title="Pick", filters=("bad, filter (*.wav)",))


# ---- node tests of the browser halves -------------------------------------------------------------------

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("script", ["vt_keys.mjs", "vt_player.mjs", "vt_range.mjs"])
def test_component_in_node(script: str) -> None:
    result = subprocess.run([NODE, str(HERE / script)], cwd=ROOT, capture_output=True, text=True, timeout=60,
                            env={**os.environ, "HF_HUB_OFFLINE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["ok"] is True and report["failed"] == [] and report["passed"] > 20


# ---- importing the kit ------------------------------------------------------------------------------------

def test_importing_the_kit_adds_no_routes_and_no_app_layers() -> None:
    code = (
        "import json, sys\n"
        "from nicegui import app\n"
        "before = len(app.routes)\n"
        "import ui.kit.port, ui.kit.audio, ui.kit.keys\n"
        "banned = ('torch', 'chatterbox', 'engine', 'config', 'soundfile', 'send2trash')\n"
        "layers = ('ui.core', 'ui.behaviours', 'ui.themes', 'ui.launch', 'ui.entry')\n"
        "print(json.dumps({'routes': len(app.routes) - before, 'modules': sorted(m for m in sys.modules\n"
        "    if m.split('.')[0] in banned or m.startswith(layers))}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "HF_HUB_OFFLINE": "1"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"routes": 0, "modules": []}


@pytest.fixture(autouse=True)
def _no_native_window_was_started() -> Iterator[None]:
    """No test starts NiceGUI's window process (its queues appear only when native mode starts) or a child."""
    queues = (native_mode_queues.method_queue, native_mode_queues.response_queue)
    children = {child.pid for child in multiprocessing.active_children()}
    yield
    assert (native_mode_queues.method_queue, native_mode_queues.response_queue) == queues
    assert {child.pid for child in multiprocessing.active_children()} <= children
