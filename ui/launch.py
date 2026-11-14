"""The composition root (BUILD_SPEC §6.2.12): engine or FakeEngine, core, kit, theme, routes, head HTML,
handlers and the root page, then ui.run.

gui.py calls main() in the server process only. Under NiceGUI's user fixture ui.run returns as soon as
the app is configured, so main() returns None and every test gets a fresh composition, which the page
tests reach through LAST_CORE and LAST_LAUNCH. A FakeEngine scenario's after-launch state (the Speak and
Forge drafts, the log, the board's take position and tabs, its steps) is applied here: the drafts once
the core is ready, the rest on the first page load, through that page's controllers.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import HTTPException
from nicegui import Event, app, background_tasks, context, run, ui

from ui.contract.routes import PAGE_ID, ROUTES, Route
from ui.contract.theme import Motion, ThemeContext
from ui.core import build_core
from ui.core.clock import ManualScheduler
from ui.core.media import mount_media
from ui.core.native import register as register_native
from ui.core.paths import Paths
from ui.core.services.settings import SettingsStore, theme_schema
from ui.entry import EXIT_UNCAUGHT, Flags, fail, is_simulation
from ui.kit.port import make_kit

if TYPE_CHECKING:
    from nicegui import Client

    from ui.contract.manifest import ThemeManifest
    from ui.contract.ports import AudioOutPort, HotkeyPort, KitPort
    from ui.contract.theme import Behaviours, Theme
    from ui.core import Core
    from ui.core.fake import FakeEngine, Scenario, Step

__all__ = [
    "LAST_CORE", "LAST_LAUNCH", "Launch", "ScenarioReplay", "ScratchTrash", "SettingsPrefs", "build_root",
    "build_shell", "engine_config", "font_faces", "import_theme", "main", "page_builder", "theme_settings",
]

LOG = logging.getLogger("voicetool.launch")

STATIC_CACHE_S = 3600               # /theme/<slug> max-age (0 with --fake)
DEV_FAKE_SPEED = 1.0                # a --fake dev run sleeps the mockup's load, take and bake times
DEV_FAKE_CALLS = 10_000             # a --fake dev run keeps this many recorded engine calls (the 2 s poller adds them)
PAGE_CLASS = "vt-page"              # the wrapper around every page (its marker is page.<id>)
PAGE_FAILED_TEXT = "This page failed to build."
PAGE_FAILED_MARKER = "page.error"
FALLBACK_MARKER = "shell.fallback"
FALLBACK_CLOSE_MARKER = "shell.fallback.close"
FX_PREF = "fx_on"                   # Settings › Background Effects (Motion.fx_enabled)
SCRATCH_TRASH = ".trash"            # the user fixture's Recycle Bin, inside the scratch data root
FONT_FILES = (("OpenSans-Regular.woff2", 400), ("OpenSans-SemiBold.woff2", 600))   # optional drop-ins (Q13)
SENTINEL = Path("tests") / "native" / "sentinel.js"      # --smoke only: counts page errors (I2)
SMOKE_PLAN = "tests.native.smoke_plan"                   # --smoke=PLAN: run_plan(client, plan, launch) (I2)
CONTROLLERS = "ui.behaviours"
DEFAULT_TAKES_TAB = "session"
DEFAULT_SETTINGS_TAB = "general"

LAST_CORE: Core | None = None       # the newest composition's core (tests read it)
LAST_LAUNCH: Launch | None = None   # the newest composition (tests read its engine)


# ---- main ---------------------------------------------------------------------------------------------

def main(flags: Flags, manifest: ThemeManifest, run_kwargs: Mapping[str, Any], *,
         app_root: str | os.PathLike[str], instance_lock: object = None) -> int | None:
    """Compose the app and run it. Returns None: under the user fixture at once, otherwise after ui.run."""
    simulation = is_simulation()
    _configure_logging(simulation)
    app.on_exception(LOG.exception)
    paths = Paths.for_run(Path(app_root), data_root=_data_root(flags, simulation), fake=flags.fake is not None)
    launch = compose(flags, manifest, paths, simulation=simulation, instance_lock=instance_lock)
    _mount_routes(launch)
    _add_head_html(launch)
    app.colors(**manifest.palette)
    _register_handlers(launch)
    ui.run(build_root(launch), **run_kwargs)
    return None


def compose(flags: Flags, manifest: ThemeManifest, paths: Paths, *, simulation: bool,
            instance_lock: object = None) -> Launch:
    """Engine, core, kit and the active theme (the one manifest names) for one run; LAST_CORE and LAST_LAUNCH
    point at them."""
    global LAST_CORE, LAST_LAUNCH
    theme = import_theme(manifest)                 # registers the theme's components before ui.run
    engine, relay, fake = _make_engine(flags, paths, simulation=simulation)
    core = build_core(engine=engine, paths=paths, settings=theme_settings(paths.settings, theme),
                      trash=_make_trash(flags, paths, simulation), relay=relay, app_name=manifest.window.title,
                      audio_output=_make_audio_output(flags, simulation))
    from ui.behaviours import BehaviourFactory

    launch = Launch(flags=flags, manifest=manifest, paths=paths, core=core, engine=engine, fake=fake,
                    kit=make_kit(browser=flags.browser), theme=theme, factory=BehaviourFactory,
                    simulation=simulation, instance_lock=instance_lock)
    LAST_CORE, LAST_LAUNCH = core, launch
    return launch


def _make_audio_output(flags: Flags, simulation: bool) -> AudioOutPort | None:
    """Select the hardware adapter in a real Windows run; tests may inject a recorder."""
    if (simulation or flags.fake is not None or flags.test_window
            or flags.smoke is not None or os.name != "nt"):
        return None
    from ui.kit.sound_out import WindowsAudioOut
    return WindowsAudioOut()


def import_theme(manifest: ThemeManifest) -> Theme:
    """THEME of ui.themes.<slug> for the manifest gui.py loaded; only the active theme is ever imported."""
    module = importlib.import_module(f"ui.themes.{manifest.slug}")
    theme = getattr(module, "THEME", None)
    if theme is None or getattr(theme, "manifest", None) is None:
        raise RuntimeError(f"ui.themes.{manifest.slug} defines no THEME with a manifest")
    if theme.manifest.slug != manifest.slug:
        raise RuntimeError(f"ui.themes.{manifest.slug}.THEME is the theme {theme.manifest.slug!r}")
    return theme


def theme_settings(path: Path, theme: Theme) -> SettingsStore:
    """The settings store with the theme's own section: theme.<slug>, its [prefs] defaults and its pref_checks."""
    manifest = theme.manifest
    return SettingsStore(path, theme_section=f"theme.{manifest.slug}",
                         theme_prefs=theme_schema(manifest.prefs, getattr(theme, "pref_checks", None)))


def engine_config(paths: Paths) -> Any:
    """config itself for the real data root; for a scratch root a copy whose folders point into it (§4.2)."""
    import config

    if paths.root == paths.app_root:
        return config
    values = {name: getattr(config, name) for name in dir(config) if name.isupper()}
    values.update(ROOT=paths.root, CACHE_DIR=paths.cache, VOICES_DIR=paths.voices,
                  OUTPUTS_DIR=paths.root / "outputs")
    return SimpleNamespace(**values)


def _data_root(flags: Flags, simulation: bool) -> Path | None:
    """--data-root; the user fixture must name a scratch root, so a test never touches the real data."""
    if simulation and not flags.data_root:
        fail(EXIT_UNCAUGHT, "The NiceGUI user fixture needs VOICETOOL_DATA_DIR (a scratch data root).")
    return Path(flags.data_root) if flags.data_root else None


def _make_engine(flags: Flags, paths: Paths, *, simulation: bool) -> tuple[Any, Any, FakeEngine | None]:
    """(engine, relay, fake): FakeEngine for --fake and the user fixture, else VoiceEngine (no torch, no model).

    Under the user fixture the fake is instant; a --fake dev run takes the mockup's own times (a 14.2 s
    load, a 6.9 s take), so the loading, generating and forging states stay on screen.
    """
    if flags.fake is not None:
        from ui.core.fake import FakeEngine

        fake = FakeEngine(flags.fake, data_dir=paths.root, speed=0.0 if simulation else DEV_FAKE_SPEED,
                          max_calls=None if simulation else DEV_FAKE_CALLS)
        return fake, None, fake
    from engine.engine import VoiceEngine
    from ui.core.facade import EventRelay

    relay = EventRelay()
    return VoiceEngine(engine_config(paths), on_event=relay, lock_path=paths.engine_lock), relay, None


def _make_trash(flags: Flags, paths: Paths, simulation: bool) -> Callable[[Path], None]:
    """send2trash for real runs; under the user fixture a FakeEngine's files go to <data root>\\.trash."""
    if simulation and flags.fake is not None:
        return ScratchTrash(paths.root / SCRATCH_TRASH)
    from send2trash import send2trash

    return send2trash


def _configure_logging(simulation: bool) -> None:
    """voicetool.* at INFO on stderr (logs\\gui.log under pythonw); under pytest its capture stays as it is."""
    if simulation:
        return
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("voicetool").setLevel(logging.INFO)


# ---- the composition ----------------------------------------------------------------------------------

class Launch:
    """One composed run: what main() built, the per-page ThemeContext and the first-page hooks."""

    def __init__(self, *, flags: Flags, manifest: ThemeManifest, paths: Paths, core: Core, engine: Any,
                 fake: FakeEngine | None, kit: KitPort, theme: Theme, factory: Callable[..., Behaviours],
                 simulation: bool, instance_lock: object = None) -> None:
        self.flags = flags
        self.manifest = manifest
        self.paths = paths
        self.core = core
        self.engine = engine
        self.kit = kit
        self.theme = theme
        self.simulation = simulation
        self.instance_lock = instance_lock      # held for the whole run (gui.py keeps it too)
        self.prefs = SettingsPrefs(core.settings, core.settings.theme_section)
        self.replay = ScenarioReplay(core, fake, navigate=not simulation) if fake is not None else None
        self._factory = factory
        self._first_page_seen = False

    async def ready(self) -> None:
        """Wait for core.start()'s checks and the scenario's launch state (tests, before asserting on them)."""
        await self.core.wait_ready()
        await self.page_ready()

    async def page_ready(self) -> None:
        """Before a page builds: the scenario's drafts and log are in (a real run never waits here)."""
        if self.replay is not None:
            await self.replay.wait_prepared()

    def asset(self, path: str) -> str:
        """"img/logo.gif" -> "/theme/emiya/img/logo.gif?v=<mtime_ns>" (no ?v= for a missing file)."""
        name = path.lstrip("/")
        url = f"{self.manifest.static_url}/{quote(name)}"
        try:
            return f"{url}?v={(self.manifest.static_dir / name).stat().st_mtime_ns}"
        except OSError:
            return url

    def theme_context(self, client: Client) -> ThemeContext:
        """A new ThemeContext with its own BehaviourFactory for this page load."""
        motion = Motion(policy=self.core.settings.get("core", "reduce_motion"),
                        fx_enabled=bool(self.core.settings.pref(FX_PREF, False)))   # off for a theme without FX
        behaviours = self._factory(self.core, client, kit=self.kit, browser=self.flags.browser)
        return ThemeContext(core=self.core, behaviours=behaviours, prefs=self.prefs, motion=motion,
                            asset=self.asset, browser=self.flags.browser, fx_telemetry=self.flags.fx_telemetry)

    async def first_page(self, behaviours: Behaviours, client: Client) -> None:
        """Once per run: replay the scenario through this page's controllers, then start a --smoke plan."""
        if self._first_page_seen:
            return
        self._first_page_seen = True
        if self.replay is not None:
            await self.replay.replay(behaviours)
        if self.flags.smoke:
            self._start_smoke_plan(client)

    def _start_smoke_plan(self, client: Client) -> None:
        """tests\\native\\smoke_plan.run_plan(client, plan, launch) as an app-level task (I2's harness)."""
        try:
            plan = importlib.import_module(SMOKE_PLAN)
        except ModuleNotFoundError as error:
            if error.name in ("tests", "tests.native", SMOKE_PLAN):
                LOG.warning("--smoke=%s: %s is not there, so no plan runs", self.flags.smoke, SMOKE_PLAN)
            else:
                LOG.exception("--smoke=%s: the plan could not be imported", self.flags.smoke)
            return
        background_tasks.create(plan.run_plan(client, self.flags.smoke, self),
                                name=f"smoke plan {self.flags.smoke}")


class SettingsPrefs:
    """ThemePrefs over the settings store's theme.<slug> section (the store validates and holds the defaults)."""

    def __init__(self, settings: SettingsStore, section: str) -> None:
        self._settings = settings
        self._section = section
        self.changed: Event[[str]] = Event()
        settings.changed.subscribe(self._forward)

    def get(self, key: str) -> bool | int | str:
        return self._settings.get(self._section, key)

    def set(self, key: str, value: bool | int | str) -> None:
        """Event loop only: memory at once, one coalesced write soon, then `changed`."""
        self._settings.set(self._section, key, value)

    def _forward(self, section: str, key: str) -> None:
        if section == self._section:
            self.changed.emit(key)


class ScratchTrash:
    """The user fixture's Recycle Bin: a trashed file moves into <data root>\\.trash; `moved` lists them."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.moved: list[Path] = []
        self._lock = threading.Lock()

    def __call__(self, path: Path) -> None:
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"{source} does not exist")
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            shutil.move(os.fspath(source), os.fspath(self.folder / f"{len(self.moved) + 1}-{source.name}"))
            self.moved.append(source)


# ---- routes, head HTML, handlers ----------------------------------------------------------------------

def _mount_routes(launch: Launch) -> None:
    """The active theme's static files, the /theme 404 guard after them, and the media routes (§4.16)."""
    manifest = launch.manifest
    cache_age = 0 if launch.flags.fake is not None else STATIC_CACHE_S
    app.add_static_files(manifest.static_url, manifest.static_dir, max_cache_age=cache_age)
    app.add_api_route("/theme/{rest:path}", _theme_guard, methods=["GET"], include_in_schema=False)
    mount_media(app, launch.core)


def _theme_guard(rest: str) -> None:
    """Any other /theme path is a 404: with ui.run(root) an unknown path would get the root page."""
    raise HTTPException(status_code=404, detail="Not Found")


def _add_head_html(launch: Launch) -> None:
    """The theme's stylesheets and fonts on every page, and its body class before Vue mounts."""
    for name in launch.manifest.css:
        ui.add_head_html(f'<link rel="stylesheet" href="{launch.asset(name)}">', shared=True)
    fonts = font_faces(launch.manifest.static_dir, launch.asset)
    if fonts:
        ui.add_head_html(f"<style>{fonts}</style>", shared=True)
    body_class = json.dumps(f"theme-{launch.manifest.slug}")
    ui.add_body_html(f"<script>document.body.classList.add({body_class})</script>", shared=True)
    if launch.flags.smoke is not None:
        _add_sentinel(launch.paths.app_root / SENTINEL)


def font_faces(static_dir: Path, asset: Callable[[str], str]) -> str:
    """@font-face rules for the optional Open Sans files present in static\\fonts (none: no rule, no 404)."""
    rules = []
    for name, weight in FONT_FILES:
        if (static_dir / "fonts" / name).is_file():
            rules.append(f"@font-face{{font-family:'Open Sans';src:url('{asset('fonts/' + name)}') "
                         f"format('woff2');font-weight:{weight};font-style:normal;font-display:block}}")
    return "".join(rules)


def _add_sentinel(path: Path) -> None:
    try:
        code = path.read_text(encoding="utf-8")
    except OSError:
        LOG.warning("--smoke: %s is not there, so page errors are not counted", path)
        return
    ui.add_head_html(f"<script>{code}</script>", shared=True)


def _register_handlers(launch: Launch) -> None:
    """Startup and shutdown order: the loop handler, core.start, the scenario; holds, core.stop, timers."""
    core = launch.core
    if not launch.simulation:
        app.on_startup(_install_loop_handler)
    app.on_startup(core.start)
    if launch.replay is not None:
        app.on_startup(launch.replay.prepare)
        app.on_shutdown(launch.replay.release)
    if (os.name == "nt" and not launch.simulation and launch.flags.fake is None
            and not launch.flags.browser and not launch.flags.test_window and launch.flags.smoke is None):
        from ui.kit.hotkeys import GlobalHotkeys
        keys = _CallKeys(core, GlobalHotkeys)
        app.on_startup(keys.start)
        app.on_shutdown(keys.close)
    app.on_shutdown(core.stop)
    app.on_shutdown(core.call.shutdown)
    if isinstance(core.scheduler, ManualScheduler):
        app.on_shutdown(core.scheduler.cancel_pending)
    register_native(app, core)


class _CallKeys:
    """Bind the Windows source to current settings and dispatch valid presses on the app loop."""

    def __init__(self, core: Core, factory: Callable[..., HotkeyPort]) -> None:
        self.core = core
        self.factory = factory
        self.source: HotkeyPort | None = None
        self.bindings: dict[str, str] = {}
        self.suspended = False

    def start(self) -> None:
        self.source = self.factory(asyncio.get_running_loop(), self._dispatch, self._error)
        self.core.settings.changed.subscribe(self._setting)
        self.core.events.changed.subscribe(self._capture)
        self._configure()

    def _setting(self, section: str, key: str) -> None:
        if section == "core" and key in {"keys", "call_enabled"}:
            self._configure()

    def _capture(self) -> None:
        if self.suspended != self.core.call.hotkeys_suspended:
            self._configure()

    def _configure(self) -> None:
        from ui.behaviours.commands import effective_bindings

        self.suspended = self.core.call.hotkeys_suspended
        self.bindings = {}
        if not self.suspended and self.core.settings.get("core", "call_enabled"):
            stored = effective_bindings(self.core.settings.get("core", "keys"))
            self.bindings = {combo: key for key in ("call_send", "call_stop") for combo in stored[key] if combo}
        if self.source is not None:
            self.source.set_bindings(self.bindings)

    def _dispatch(self, command: str) -> None:
        if (self.source is None or self.core.call.hotkeys_suspended or
                not self.core.settings.get("core", "call_enabled") or command not in self.bindings.values()):
            return
        if command == "call_send":
            self.core.call.send(self.core.session.take_id)
        elif command == "call_stop":
            self.core.call.stop()

    def _error(self, message: str) -> None:
        self.core.call.hotkey_error = message
        if message:
            self.core.events.say(message, bad=True)
        self.core.events.notify()

    def close(self) -> None:
        self.core.settings.changed.unsubscribe(self._setting)
        self.core.events.changed.unsubscribe(self._capture)
        if self.source is not None:
            self.source.close()
            self.source = None


def _install_loop_handler() -> None:
    asyncio.get_running_loop().set_exception_handler(_loop_exception)


def _loop_exception(loop: asyncio.AbstractEventLoop, info: dict[str, Any]) -> None:
    """WebView2 closing its sockets makes the Proactor raise WinError 10054 in _call_connection_lost: benign."""
    error = info.get("exception")
    if isinstance(error, ConnectionResetError) and "_call_connection_lost" in str(info.get("handle", "")):
        LOG.debug("A connection reset at shutdown was ignored")
        return
    loop.default_exception_handler(info)


# ---- the root page ------------------------------------------------------------------------------------

def build_root(launch: Launch) -> Callable[[], Awaitable[None]]:
    """The root page: a ThemeContext per page load, the shell (or the fallback panel), the first-page hooks."""

    async def root() -> None:
        await launch.page_ready()
        client = context.client
        launch.core.client_connected()          # before the shell, so held toasts reach it
        ctx = launch.theme_context(client)
        if ctx.motion.policy == "always":
            ui.query("body").classes("reduce-motion")
        if build_shell(launch, ctx):
            await launch.first_page(ctx.behaviours, client)

    return root


def build_shell(launch: Launch, ctx: ThemeContext) -> bool:
    """THEME.build_shell, which must call mount_pages() exactly once; a failure logs ERROR, shows the panel."""
    mounted = 0

    def mount_pages() -> None:
        nonlocal mounted
        mounted += 1
        if mounted > 1:
            raise RuntimeError("build_shell() called mount_pages() more than once")
        ui.sub_pages({route: page_builder(launch.theme, route, ctx) for route in ROUTES})

    try:
        launch.theme.build_shell(ctx, mount_pages)
        if mounted != 1:
            raise RuntimeError("build_shell() never called mount_pages()")
    except Exception:
        LOG.exception("The %s shell failed to build", launch.manifest.name)
        _fallback_panel(launch)
        return False
    return True


def page_builder(theme: Theme, route: Route, ctx: ThemeContext) -> Callable[[], None]:
    """One sub page: the page.<id> wrapper around the theme's builder; a failure shows only this page's text."""
    marker = f"page.{PAGE_ID[route]}"

    def page() -> None:
        with ui.element("div").classes(PAGE_CLASS).mark(marker) as box:
            try:
                theme.pages[route](ctx)
            except Exception:
                LOG.exception("The %s page failed to build", route)
                box.clear()
                ui.label(PAGE_FAILED_TEXT).mark(PAGE_FAILED_MARKER)

    return page


def _fallback_panel(launch: Launch) -> None:
    """Instead of a broken shell: "Emiya failed to build. See logs\\gui.log" and a Close button."""
    content = context.client.content
    content.clear()
    with content, ui.column().mark(FALLBACK_MARKER):
        ui.label(f"{launch.manifest.name} failed to build. See logs\\gui.log")
        ui.button("Close", on_click=launch.kit.quit).mark(FALLBACK_CLOSE_MARKER)


# ---- FakeEngine scenarios -----------------------------------------------------------------------------

class ScenarioReplay:
    """A FakeEngine scenario's after-launch state (ui.core.fake.Scenario), applied once per run."""

    def __init__(self, core: Core, fake: FakeEngine, *, navigate: bool) -> None:
        self._core = core
        self._fake = fake
        self._navigate = navigate               # dev runs open on the scenario's page; tests use open_route
        self._prepared = asyncio.Event()

    @property
    def scenario(self) -> Scenario:
        return self._fake.scenario

    async def prepare(self) -> None:
        """app.on_startup: after core.start()'s checks, the Speak draft, the log and the Forge draft."""
        try:
            await self._core.wait_ready()
            self._apply_session()
            self._apply_log()
            await self._apply_forge()
        except Exception:
            LOG.exception("Scenario %s: its launch state could not be applied", self.scenario.name)
        finally:
            self._prepared.set()

    async def wait_prepared(self) -> None:
        await self._prepared.wait()

    def release(self) -> None:
        """app.on_shutdown: every held engine call goes on, so the app can stop."""
        self._fake.release_all()

    async def replay(self, behaviours: Behaviours) -> None:
        """The first page load: the take position and tabs the board shows, then the steps; then forget the calls."""
        scenario = self.scenario
        if scenario.player_at is not None:
            at = scenario.player_at
            await self._run("the take position", lambda: behaviours.playback().seek(at))
        if scenario.takes_tab != DEFAULT_TAKES_TAB:
            await self._run("the Takes tab", lambda: behaviours.takes().set_tab(scenario.takes_tab))
        if scenario.settings_tab != DEFAULT_SETTINGS_TAB:
            await self._run("the Settings tab", lambda: behaviours.settings().set_tab(scenario.settings_tab))
        for step in scenario.steps:
            if not await self._run(f"the {step.kind} step", lambda step=step: self._step(step, behaviours)):
                break
        self._fake.reset_records()
        if self._navigate and scenario.route != "/":
            route = scenario.route
            ui.timer(0.1, lambda: ui.navigate.to(route), once=True)

    def _apply_session(self) -> None:
        seed = self.scenario.session
        if seed is None:
            return
        for item in dataclasses.fields(seed):
            setattr(self._core.session, item.name, getattr(seed, item.name))
        self._core.events.notify()

    def _apply_log(self) -> None:
        if self.scenario.log:
            self._core.log.restore(self.scenario.log)

    async def _apply_forge(self) -> None:
        seed = self.scenario.forge
        if seed is None:
            return
        path = self._core.paths.voices / seed.clip
        clip = await run.io_bound(self._core.clips.stage_file, path)
        if clip is None:                        # the app is stopping
            return
        draft = self._core.forge_draft
        draft.clip, draft.clip_state, draft.clip_file, draft.clip_error = clip, "ready", path.name, ""
        draft.name, draft.backend, draft.language = seed.name, seed.backend, seed.language
        draft.exaggeration, draft.consent = seed.exaggeration, seed.consent
        draft.rev += 1
        self._core.events.notify()

    async def _step(self, step: Step, behaviours: Behaviours) -> None:
        """ask: a dock click; switch: through the ask (asked) or the Models card; free, generate, forge."""
        if step.kind in ("ask", "switch"):
            models = behaviours.models()
            origin = "card" if step.kind == "switch" and not step.asked else "dock"
            request = models.request_switch(step.backend, origin=origin)
            if step.kind == "switch" and request is not None and request.token:
                await models.confirm(request.token)
        elif step.kind == "free":
            await behaviours.models().free_vram()
        elif step.kind == "generate":
            await behaviours.speak().generate(step.count)
        elif step.kind == "forge":
            await behaviours.forge().forge()
        else:
            raise ValueError(f"unknown scenario step {step.kind!r}")

    async def _run(self, what: str, action: Callable[[], Any]) -> bool:
        """Run one replay action; a controller not built yet is a WARNING, any other failure an ERROR."""
        try:
            result = action()
            if inspect.isawaitable(result):
                await result
        except ModuleNotFoundError as error:
            if (error.name or "").startswith(CONTROLLERS):
                LOG.warning("Scenario %s: %s was skipped (%s is not built yet)", self.scenario.name, what,
                            error.name)
            else:
                LOG.exception("Scenario %s: %s failed", self.scenario.name, what)
            return False
        except Exception:
            LOG.exception("Scenario %s: %s failed", self.scenario.name, what)
            return False
        return True
