"""gui.py under NiceGUI's user fixture with T0's placeholder theme, and the ui.entry exit-code helpers (§8.5).

Every user-fixture test here runs the real gui.py composition on a FakeEngine scenario in tmp_path: routes,
media mounts, head HTML, LAST_CORE, the scenario's launch state and replay, the fallback panel and the
page placeholder. The instance lock is held by a helper process on a scratch path (tests never write
under the app's cache folder); the exit-code helpers are called directly, so no box and no sys.exit.
Tests about the root page itself put a stand-in theme on the composition (and, where they record the
intents, a stand-in BehaviourFactory), so they keep testing the composition root once the real shell,
pages and controllers land.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import Client, app, ui
from nicegui.testing import User

from ui import entry, launch
from ui.contract.manifest import window_args
from ui.contract.routes import PAGE_ID, ROUTES
from ui.contract.theme import Motion, ThemeContext
from ui.core import Core
from ui.core.background import launch_line
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.core.media import ROUTES as MEDIA_ROUTES
from ui.core.paths import Paths

ROOT = Path(__file__).resolve().parents[2]
THEME_DIR = ROOT / "ui" / "themes" / "emiya"
CREATE_NO_WINDOW = 0x08000000
TURBO = "chatterbox-turbo"
HOLDER = (
    "import sys\n"
    "from filelock import FileLock\n"
    "lock = FileLock(sys.argv[1])\n"
    "lock.acquire(timeout=10)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


# ---- fixtures -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def held_lock(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A helper process holding a scratch gui.lock for the whole module (stopped by its own pid at the end)."""
    path = tmp_path_factory.mktemp("lock") / "cache" / "gui-dev.lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, creationflags=CREATE_NO_WINDOW)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        yield path
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=10)
        if holder.stdout is not None:
            holder.stdout.close()                         # else a ResourceWarning at the module's end


@pytest.fixture
def instance_lock_held(held_lock: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """gui.py's single-instance lock would be the held one, if it took the lock under the fixture."""
    monkeypatch.setattr(entry, "instance_lock_path", lambda app_root, *, fake: held_lock)
    return held_lock


@pytest.fixture
def no_box(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records what a message box would have shown (no real box ever opens)."""
    shown: list[str] = []
    monkeypatch.setattr(entry, "_message_box", shown.append)
    return shown


@pytest.fixture
def run(user: User) -> launch.Launch:
    """The composition gui.py built for this test."""
    assert launch.LAST_LAUNCH is not None
    return launch.LAST_LAUNCH


def _bare_shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
    with ui.column():
        mount_pages()


def _bare_page(ctx: ThemeContext) -> None:
    ui.label("bare page")


def _stand_in_theme(run: launch.Launch, monkeypatch: pytest.MonkeyPatch, *,
                    shell: Callable[[ThemeContext, Callable[[], None]], None] = _bare_shell,
                    pages: dict[str, Any] | None = None) -> None:
    """Put a theme with this shell and these pages (bare by default) on the composition."""
    theme = SimpleNamespace(manifest=run.theme.manifest, build_shell=shell,
                            pages=pages or {route: _bare_page for route in ROUTES})
    monkeypatch.setattr(run, "theme", theme)


# ---- recording controllers for the scenario replay ----------------------------------------------------

class _Recorder:
    """A controller that records every intent; request_switch answers with `answer`."""

    def __init__(self, name: str, calls: list[tuple[Any, ...]], answer: Any = None) -> None:
        self._name, self._calls, self._answer = name, calls, answer

    def __getattr__(self, intent: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> Any:
            self._calls.append((f"{self._name}.{intent}", *args, *sorted(kwargs.items())))
            return self._answer if intent == "request_switch" else None

        async def record_async(*args: Any, **kwargs: Any) -> None:
            record(*args, **kwargs)

        return record_async if intent in ("confirm", "free_vram", "generate", "forge") else record


def _recording_factory(calls: list[tuple[Any, ...]], *, answer: Any = None, missing: str = "") -> type:
    class Factory:
        """Records the intents the replay reaches; `missing` names a controller not built yet."""

        def __init__(self, core: Core, client: Client, kit: Any, *, browser: bool = False) -> None:
            self._controllers: dict[str, _Recorder] = {}

        def __getattr__(self, name: str) -> Any:
            def controller() -> _Recorder:
                if name == missing:
                    raise ModuleNotFoundError(f"No module named 'ui.behaviours.{name}'",
                                              name=f"ui.behaviours.{name}")
                return self._controllers.setdefault(name, _Recorder(name, calls, answer))

            return controller

    return Factory


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.get_records("call") if r.levelname == "ERROR" and r.name == "voicetool.launch"]


# ---- the composition under the user fixture -----------------------------------------------------------

async def test_every_route_renders_its_page(user: User) -> None:
    for route in ROUTES:
        await user.open(route)
        await user.should_see(marker=f"page.{PAGE_ID[route]}")
        await user.should_not_see(marker=launch.PAGE_FAILED_MARKER)
        await user.should_not_see(marker=launch.FALLBACK_MARKER)


async def test_open_route_waits_for_the_page(user: User, open_route: Any) -> None:
    await open_route(user, "/")
    await user.should_see(marker="page.speak")


async def test_theme_and_media_routes_are_mounted(user: User, core: Core) -> None:
    paths = [getattr(route, "path", "") for route in app.routes]
    static, guard = "/theme/emiya/{path:path}", "/theme/{rest:path}"
    assert static in paths and guard in paths and paths.index(static) < paths.index(guard)
    assert set(MEDIA_ROUTES) <= set(paths)
    http = user.http_client
    css = await http.get("/theme/emiya/emiya.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert (await http.get("/theme/emiya/no-such.css")).status_code == 404
    other = await http.get("/theme/elsewhere/emiya.css")
    assert other.status_code == 404 and other.json() == {"detail": "Not Found"}
    take = core.state.takes.takes[0]
    wav = await http.get(core.take_url(take.id))
    assert wav.status_code == 200 and wav.headers["content-type"] == "audio/wav"
    assert wav.headers["cache-control"] == "no-store"
    assert (await http.get(core.take_url(core.takes.next_id + 100))).status_code == 404
    voice = next(row for row in core.state.library.rows if row.reference_path is not None)
    assert (await http.get(core.clip_url(voice.voice_id))).status_code == 200
    assert (await http.get(core.staged_url("no-such-clip"))).status_code == 404


async def test_last_core_is_this_runs_composition(run: launch.Launch, core: Core, fake: FakeEngine,
                                                  clock: ManualScheduler, tmp_path: Path) -> None:
    assert launch.LAST_CORE is core and run.engine is fake and run.replay is not None
    assert core.paths.root == tmp_path and core.paths.settings == tmp_path / "ui_settings.json"
    assert core.paths.app_root == ROOT and core.scheduler is clock and fake.scenario.name == "speak"
    assert fake.speed == 0.0                              # instant under the fixture
    assert isinstance(core.trash, launch.ScratchTrash) and core.trash.folder == tmp_path / launch.SCRATCH_TRASH
    seed = fake.scenario.session
    assert seed is not None
    assert {f.name: getattr(core.session, f.name) for f in dataclasses.fields(seed)} == dataclasses.asdict(seed)
    assert core.state.log.lines == fake.scenario.log


async def test_head_html_colours_and_window_chrome(run: launch.Launch) -> None:
    manifest = run.manifest
    assert Client.shared_head_html.count('rel="stylesheet"') == len(manifest.css)
    assert f'href="{run.asset(manifest.css[0])}"' in Client.shared_head_html and "?v=" in run.asset("emiya.css")
    assert 'document.body.classList.add("theme-emiya")' in Client.shared_body_html
    assert app.config.quasar_config["brand"]["primary"] == manifest.palette["primary"]
    for key, value in window_args(manifest).items():
        assert app.native.window_args[key] == value
    assert app.native.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] is True
    assert "func" not in app.native.start_args            # no window under the user fixture


@pytest.mark.scenario("forge")
async def test_a_forge_scenario_stages_its_draft(core: Core, fake: FakeEngine) -> None:
    seed = fake.scenario.forge
    draft = core.forge_draft
    assert seed is not None and draft.clip is not None and draft.clip_state == "ready"
    assert (draft.name, draft.backend, draft.language, draft.exaggeration, draft.consent) == (
        seed.name, seed.backend, seed.language, seed.exaggeration, seed.consent)
    assert draft.clip_file == seed.clip and draft.rev >= 1
    assert draft.clip.selection_s <= draft.clip.duration_s


@pytest.mark.scenario("firstrun")
async def test_a_scenario_without_a_log_keeps_the_launch_line(core: Core, fake: FakeEngine) -> None:
    assert fake.scenario.log == () and fake.scenario.session is None
    assert core.state.log.lines and core.state.log.lines[-1].endswith(launch_line("Emiya Voice"))
    assert core.session.take_id == 0


async def test_each_page_load_gets_its_own_context(run: launch.Launch, user: User, core: Core,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ThemeContext] = []

    def shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        seen.append(ctx)
        _bare_shell(ctx, mount_pages)

    _stand_in_theme(run, monkeypatch, shell=shell)
    await user.open("/")
    await user.open("/voices")
    await user.should_see(marker="page.voices")
    first, second = seen
    assert first.behaviours is not second.behaviours and first.core is core and second.prefs is run.prefs
    assert first.motion == Motion(policy="system", fx_enabled=True) and first.browser is False
    assert first.asset("img/logo.gif").startswith("/theme/emiya/img/logo.gif?v=")
    assert first.asset("img/no-such.png") == "/theme/emiya/img/no-such.png"


async def test_reduce_motion_always_reaches_the_context(run: launch.Launch, user: User, core: Core,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ThemeContext] = []

    def shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        seen.append(ctx)
        mount_pages()

    _stand_in_theme(run, monkeypatch, shell=shell)
    core.settings.set("core", "reduce_motion", "always")
    run.prefs.set("fx_on", False)
    await user.open("/")
    assert seen[0].motion == Motion(policy="always", fx_enabled=False)


async def test_prefs_forward_only_the_theme_section(run: launch.Launch, core: Core) -> None:
    keys: list[str] = []
    run.prefs.changed.subscribe(keys.append)
    run.prefs.set("fx_speed", 150)
    core.settings.set("core", "autoplay", False)
    assert run.prefs.get("fx_speed") == 150 and keys == ["fx_speed"]


async def test_a_failing_shell_shows_the_fallback_panel(run: launch.Launch, user: User,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    quits: list[str] = []
    monkeypatch.setattr("ui.kit.window.quit_app", lambda: quits.append("quit"))

    def shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        ui.label("half a shell")
        raise RuntimeError("the shell broke")

    _stand_in_theme(run, monkeypatch, shell=shell)
    await user.open("/")
    await user.should_see("Emiya failed to build. See logs\\gui.log")
    await user.should_not_see("half a shell")
    user.find(marker=launch.FALLBACK_CLOSE_MARKER).click()
    assert quits == ["quit"]
    errors = _errors(caplog)
    assert len(errors) == 1 and "shell failed to build" in errors[0].getMessage()
    caplog.clear()                                          # the expected ERROR must not fail the test


@pytest.mark.parametrize("mounts", [0, 2])
async def test_mount_pages_must_run_exactly_once(run: launch.Launch, user: User, monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture, mounts: int) -> None:
    def shell(ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        for _ in range(mounts):
            mount_pages()

    _stand_in_theme(run, monkeypatch, shell=shell)
    await user.open("/")
    await user.should_see(marker=launch.FALLBACK_MARKER)
    assert [str(r.exc_info[1]) for r in _errors(caplog) if r.exc_info] == [
        "build_shell() never called mount_pages()" if mounts == 0
        else "build_shell() called mount_pages() more than once"]
    caplog.clear()


async def test_a_failing_page_shows_only_its_placeholder(run: launch.Launch, user: User,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    def broken(ctx: ThemeContext) -> None:
        ui.label("half a page")
        raise RuntimeError("the page broke")

    _stand_in_theme(run, monkeypatch, pages={**{route: _bare_page for route in ROUTES}, "/": broken})
    await user.open("/")
    await user.should_see(marker="page.speak")
    await user.should_see(launch.PAGE_FAILED_TEXT)
    await user.should_not_see("half a page")
    await user.should_not_see(marker=launch.FALLBACK_MARKER)
    assert len(_errors(caplog)) == 1
    caplog.clear()


# ---- the scenario replay on the first page load -------------------------------------------------------

SEEK = ("playback.seek", 1.9)
ASKED = SimpleNamespace(token="token-1")        # what request_switch answers when the switch asks
REPLAYS = [
    ("speak", None, [SEEK]),
    ("switch", ASKED, [SEEK, ("models.request_switch", TURBO, ("origin", "dock"))]),
    ("failure", ASKED, [SEEK, ("models.request_switch", TURBO, ("origin", "dock")), ("models.confirm", "token-1")]),
    ("loading", None, [SEEK, ("models.request_switch", TURBO, ("origin", "card"))]),
    ("freeing", None, [SEEK, ("models.free_vram",)]),
    ("queued", None, [SEEK, ("speak.generate", 3)]),
    ("forged", None, [SEEK, ("forge.forge",)]),
    ("takes", None, [SEEK, ("takes.set_tab", "all")]),
    ("settings", None, [SEEK, ("settings.set_tab", "effects")]),
    ("firstrun", None, []),
]


@pytest.mark.parametrize(("answer", "expected"), [
    pytest.param(answer, expected, marks=pytest.mark.scenario(name), id=name) for name, answer, expected in REPLAYS])
async def test_the_first_page_replays_the_scenario(run: launch.Launch, user: User, fake: FakeEngine,
                                                   monkeypatch: pytest.MonkeyPatch, answer: Any,
                                                   expected: list[tuple[Any, ...]]) -> None:
    calls: list[tuple[Any, ...]] = []
    resets: list[int] = []
    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", _recording_factory(calls, answer=answer))
    monkeypatch.setattr(fake, "reset_records", lambda: resets.append(1))
    await user.open("/")
    await user.open("/")                                    # a second page load replays nothing
    assert calls == expected and resets == [1]


@pytest.mark.scenario("queued")
async def test_a_controller_not_built_yet_skips_the_rest(run: launch.Launch, user: User,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    calls: list[tuple[Any, ...]] = []
    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", _recording_factory(calls, missing="speak"))
    await user.open("/")
    assert calls == [SEEK]
    warnings = [r.getMessage() for r in caplog.get_records("call") if r.name == "voicetool.launch"]
    assert any("generate step was skipped" in text and "ui.behaviours.speak" in text for text in warnings)


async def test_a_failing_replay_action_is_an_error(run: launch.Launch, user: User,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def __init__(self, core: Core, client: Client, kit: Any, *, browser: bool = False) -> None:
            pass

        def playback(self) -> Any:
            raise LookupError("no playback")

    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", Broken)
    await user.open("/")
    await user.should_see(marker="page.speak")
    errors = _errors(caplog)
    assert len(errors) == 1 and "take position failed" in errors[0].getMessage()
    caplog.clear()


@pytest.mark.scenario("forge")
async def test_a_dev_run_opens_on_the_scenarios_page(run: launch.Launch, user: User,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    assert run.replay is not None and run.replay.scenario.route == "/forge"
    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", _recording_factory([]))
    monkeypatch.setattr(run.replay, "_navigate", True)     # what --fake=forge does outside the fixture
    await user.open("/")
    await user.should_see(marker="page.forge", retries=30)


async def test_a_smoke_run_starts_its_plan_after_the_first_page(run: launch.Launch, user: User,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[Any, ...]] = []
    plan = types.ModuleType(launch.SMOKE_PLAN)

    async def run_plan(client: Client, name: str, composition: launch.Launch) -> None:
        started.append((client, name, composition))

    plan.run_plan = run_plan  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, launch.SMOKE_PLAN, plan)
    monkeypatch.setattr(run, "flags", dataclasses.replace(run.flags, smoke="all"))
    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", _recording_factory([]))
    client = await user.open("/")
    await user.open("/")
    await user.should_see(marker="page.speak")
    for _ in range(50):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == [(client, "all", run)]


async def test_a_smoke_run_without_its_plan_module_warns(run: launch.Launch, user: User,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, launch.SMOKE_PLAN, None)    # import_module raises ModuleNotFoundError
    monkeypatch.setattr(run, "flags", dataclasses.replace(run.flags, smoke="all"))
    _stand_in_theme(run, monkeypatch)
    monkeypatch.setattr(run, "_factory", _recording_factory([]))
    await user.open("/")
    assert any("no plan runs" in r.getMessage() for r in caplog.get_records("call") if r.levelname == "WARNING")


# ---- the instance lock under the fixture --------------------------------------------------------------

@pytest.mark.parametrize("run_number", [1, 2])
async def test_fixture_runs_while_another_process_holds_the_lock(instance_lock_held: Path, user: User,
                                                                 run_number: int) -> None:
    await user.open("/")
    await user.should_see(marker="page.speak")
    assert entry.take_instance_lock(instance_lock_held) == entry.EXIT_ALREADY_RUNNING   # still the helper's


# ---- ui.entry's exit-code helpers ---------------------------------------------------------------------

def test_take_instance_lock_returns_5_against_a_held_lock(held_lock: Path, tmp_path: Path) -> None:
    code = entry.take_instance_lock(held_lock)
    assert isinstance(code, entry.ExitCode) and code == entry.EXIT_ALREADY_RUNNING == 5
    assert "already running" in code.text
    free = entry.take_instance_lock(tmp_path / "cache" / "gui.lock")
    assert not isinstance(free, int) and free.is_locked
    free.release()


def test_load_theme_manifest_returns_7_for_a_broken_theme(tmp_path: Path) -> None:
    folder = tmp_path / "emiya"
    folder.mkdir()
    (folder / "theme.toml").write_text('slug = "emiya"\n[window\n', encoding="utf-8")
    code = entry.load_theme_manifest(folder)
    assert isinstance(code, entry.ExitCode) and code == entry.EXIT_BAD_THEME == 7
    assert "theme.toml" in code.text and "cannot start" in code.text
    manifest = entry.load_theme_manifest(THEME_DIR)
    assert not isinstance(manifest, int) and manifest.slug == entry.ACTIVE_THEME


def test_no_free_screen_gives_exit_code_8() -> None:
    screen = entry.Rect(0, 0, 1600, 900)
    desktop = entry.Desktop(
        (entry.Screen("\\\\.\\DISPLAY1", True, screen, screen, 96, screen),),
        entry.Foreground(hwnd=7, visible=True, minimized=False, rect=screen), 3)
    code = entry.place_test_window((1280, 800), desktop)
    assert isinstance(code, entry.ExitCode) and code == entry.EXIT_NO_FREE_SCREEN == 8
    assert code.text == "skipped: no free screen"


def test_fail_raises_boot_failure_under_pytest(monkeypatch: pytest.MonkeyPatch, no_box: list[str]) -> None:
    with pytest.raises(entry.BootFailure) as caught:
        entry.fail(5, "Emiya Voice is already running.")
    assert (caught.value.code, caught.value.text) == (5, "Emiya Voice is already running.")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(entry.SIMULATION_ENV, "true")
    with pytest.raises(entry.BootFailure):
        entry.fail(7, "broken theme")
    assert no_box == []


def test_fail_outside_tests_shows_the_box_and_exits(monkeypatch: pytest.MonkeyPatch, no_box: list[str]) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(entry.SIMULATION_ENV, raising=False)
    with pytest.raises(SystemExit) as caught:
        entry.fail(7, "broken theme")
    assert caught.value.code == 7 and no_box == ["broken theme"]
    with pytest.raises(SystemExit) as caught:
        entry.fail(8, "skipped: no free screen", quiet=True)
    assert caught.value.code == 8 and no_box == ["broken theme"]


def test_fail_text_reaches_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(entry.BootFailure):
        entry.fail(5, "Emiya Voice is already running.")
    assert "Emiya Voice is already running. (exit code 5)" in capsys.readouterr().err


def test_the_user_fixture_needs_a_scratch_data_root() -> None:
    with pytest.raises(entry.BootFailure) as caught:
        launch._data_root(entry.Flags(fake="speak"), simulation=True)
    assert caught.value.code == entry.EXIT_UNCAUGHT and "VOICETOOL_DATA_DIR" in caught.value.text


def test_the_crash_hook_logs_then_points_at_the_log(no_box: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    try:
        raise ValueError("model files are not in the local cache")
    except ValueError as error:
        entry.crash_hook(quiet=False)(ValueError, error, error.__traceback__)
        entry.crash_hook(quiet=True)(ValueError, error, error.__traceback__)
    assert "ValueError: model files are not in the local cache" in capsys.readouterr().err
    assert no_box == ["Emiya Voice stopped: model files are not in the local cache\n\nSee logs\\gui.log"]


# ---- pure helpers -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("fake", "data"), [(False, False), (True, False), (False, True), (True, True)])
def test_entry_paths_follow_paths_for_run(tmp_path: Path, fake: bool, data: bool) -> None:
    flags = entry.Flags(fake="speak" if fake else None, data_root=str(tmp_path) if data else None)
    paths = Paths.for_run(ROOT, data_root=tmp_path if data else None, fake=fake)
    assert entry.data_root(ROOT, flags) == paths.root
    assert entry.instance_lock_path(ROOT, fake=fake) == paths.gui_lock


@pytest.mark.parametrize(("text", "offline"), [
    (None, True), ('{"core": {"offline": false}}', False), ('﻿{"core": {"offline": false}}', False),
    ('{"core": {"offline": true}}', True), ('{"core": {"offline": "no"}}', True), ('{"core": []}', True),
    ("[1, 2]", True), ("{broken", True), ('{"core": {}}', True)])
def test_offline_mode_is_read_tolerantly(tmp_path: Path, text: str | None, offline: bool) -> None:
    path = tmp_path / "ui_settings.json"
    if text is not None:
        path.write_bytes(text.encode("utf-8"))
    assert entry.read_offline(path) is offline


def test_the_environment_before_nicegui(tmp_path: Path) -> None:
    env = entry.environment(entry.Flags(fake="speak"), tmp_path, offline=True, simulation=False,
                            env={entry.WEBVIEW2_ENV: "--no-pings --custom"})
    assert env["HF_HUB_OFFLINE"] == "1" and env["TQDM_MININTERVAL"] == "5"
    assert env["NICEGUI_STORAGE_PATH"] == str(tmp_path / ".nicegui-dev")
    switches = env[entry.WEBVIEW2_ENV].split()
    assert switches[:2] == ["--no-pings", "--custom"] and switches.count("--no-pings") == 1
    assert set(entry.WEBVIEW_SWITCHES) <= set(switches)
    real = entry.environment(entry.Flags(), tmp_path, offline=False, simulation=False,
                             env={"TQDM_MININTERVAL": "1"})
    assert real["HF_HUB_OFFLINE"] == "0" and "TQDM_MININTERVAL" not in real
    assert real["NICEGUI_STORAGE_PATH"] == str(tmp_path / ".nicegui")
    assert entry.environment(entry.Flags(), tmp_path, offline=True, simulation=True, env={}) == {
        "HF_HUB_OFFLINE": "1"}


def test_engine_config_points_a_scratch_root_at_its_folders(tmp_path: Path) -> None:
    import config

    assert launch.engine_config(Paths.for_run(ROOT, data_root=None, fake=False)) is config
    paths = Paths.for_run(ROOT, data_root=tmp_path, fake=False)
    scratch = launch.engine_config(paths)
    assert (scratch.ROOT, scratch.CACHE_DIR, scratch.VOICES_DIR, scratch.OUTPUTS_DIR) == (
        tmp_path, tmp_path / "cache", tmp_path / "voices" / "reference", tmp_path / "outputs")
    assert scratch.ENABLED_BACKENDS == config.ENABLED_BACKENDS
    assert scratch.MAX_RESIDENT_BACKENDS == config.MAX_RESIDENT_BACKENDS and scratch.DEVICE == config.DEVICE


def test_a_fake_dev_run_takes_the_mockups_times(tmp_path: Path) -> None:
    """--fake outside the fixture sleeps the nominal times (U3's hand-off); the fixture's fake is instant."""
    paths = Paths.for_run(ROOT, data_root=tmp_path / "data", fake=True)
    engine, relay, fake = launch._make_engine(entry.Flags(fake="speak"), paths, simulation=False)
    assert engine is fake and relay is None and fake.speed == launch.DEV_FAKE_SPEED == 1.0
    assert fake.data_dir == tmp_path / "data"
    _, _, instant = launch._make_engine(entry.Flags(fake="speak"), Paths.for_run(
        ROOT, data_root=tmp_path / "test", fake=True), simulation=True)
    assert instant.speed == 0.0


def test_scratch_trash_moves_files_and_records_them(tmp_path: Path) -> None:
    trash = launch.ScratchTrash(tmp_path / ".trash")
    victim = tmp_path / "aiko_take7.wav"
    victim.write_bytes(b"RIFF")
    trash(victim)
    assert not victim.exists() and trash.moved == [victim]
    assert [p.read_bytes() for p in (tmp_path / ".trash").iterdir()] == [b"RIFF"]
    with pytest.raises(FileNotFoundError):
        trash(victim)


def test_font_faces_only_for_files_present(tmp_path: Path) -> None:
    assert launch.font_faces(tmp_path, lambda path: f"/theme/emiya/{path}") == ""
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "OpenSans-SemiBold.woff2").write_bytes(b"wOF2")
    rules = launch.font_faces(tmp_path, lambda path: f"/theme/emiya/{path}")
    assert rules.count("@font-face") == 1 and "font-weight:600" in rules
    assert "url('/theme/emiya/fonts/OpenSans-SemiBold.woff2')" in rules and "font-display:block" in rules


def test_the_sentinel_is_inlined_only_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(Client, "shared_head_html", "")
    launch._add_sentinel(tmp_path / "sentinel.js")
    assert Client.shared_head_html == "" and "page errors are not counted" in caplog.text
    (tmp_path / "sentinel.js").write_text("window.vtErrors = 0;", encoding="utf-8")
    launch._add_sentinel(tmp_path / "sentinel.js")
    assert Client.shared_head_html.strip() == "<script>window.vtErrors = 0;</script>"


def test_the_benign_shutdown_reset_is_dropped() -> None:
    passed: list[dict[str, Any]] = []
    loop = SimpleNamespace(default_exception_handler=passed.append)
    benign = {"exception": ConnectionResetError(10054, "reset"),
              "handle": "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"}
    launch._loop_exception(loop, benign)                  # type: ignore[arg-type]
    other = {"exception": ConnectionResetError(10054, "reset"), "handle": "<Handle something_else()>"}
    launch._loop_exception(loop, other)                   # type: ignore[arg-type]
    assert passed == [other]


def test_run_arguments_carry_the_absolute_icon() -> None:
    manifest = entry.load_theme_manifest(THEME_DIR)
    assert not isinstance(manifest, int)
    assert os.path.isabs(entry.run_arguments(manifest, entry.Flags())["favicon"])
