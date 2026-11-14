"""What a theme receives (ThemeContext) and what it provides (Theme)."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from nicegui import Event

from ui.contract.manifest import ThemeManifest
from ui.contract.plans import Toast
from ui.contract.routes import Route
from ui.contract.state import AppState, Session
from ui.contract.viewmodels import (
    CommandRegistry,
    ForgeController,
    ModelsController,
    PlaybackController,
    SearchController,
    SettingsController,
    SpeakController,
    StatusController,
    TakesController,
    VoicesController,
)


@runtime_checkable
class CoreEventsView(Protocol):
    """The core's events (ui.core.events.CoreEvents); themes subscribe in shell.py only (R4)."""

    changed: Event[[]]                      # any mirror changed (coalesced per loop tick)
    toast: Event[[Toast]]
    flash: Event[[str]]                     # "dock-<backend>", "voice-<id>", "take-<id>"
    spark: Event[[str]]                     # "dock-<backend>", "take"
    take_ready: Event[[int]]                # take id
    forge_started: Event[[str]]             # voice id (the FX blade)
    forged: Event[[str]]                    # voice id
    dropped: Event[[tuple[str, ...]]]       # native drop: full paths

    def say(self, text: str, *, bad: bool = False) -> Toast: ...   # a toast the shell raises itself (a drop)


@runtime_checkable
class CoreView(Protocol):
    """The read-only face of the core: themes bind to it and never assign."""

    state: AppState
    session: Session
    events: CoreEventsView

    def take_url(self, take_id: int) -> str: ...
    def clip_url(self, voice_id: str) -> str: ...
    def staged_url(self, clip_id: str) -> str: ...


@runtime_checkable
class Behaviours(Protocol):
    """One per page load, controllers memoized: the only write path from a theme into the core."""

    def status(self) -> StatusController: ...
    def models(self) -> ModelsController: ...
    def playback(self) -> PlaybackController: ...
    def commands(self) -> CommandRegistry: ...
    def search(self) -> SearchController: ...
    def speak(self) -> SpeakController: ...
    def voices(self) -> VoicesController: ...
    def forge(self) -> ForgeController: ...
    def takes(self) -> TakesController: ...
    def settings(self) -> SettingsController: ...


@runtime_checkable
class ThemePrefs(Protocol):
    """The active theme's own preferences (theme.<slug>.* in ui_settings.json; defaults from [prefs])."""

    changed: Event[[str]]                   # the key that changed

    def get(self, key: str) -> bool | int | str: ...
    def set(self, key: str, value: bool | int | str) -> None: ...   # event loop only


@dataclass(frozen=True)
class Motion:
    """Motion settings at page load; live changes arrive through the VMs and prefs.changed."""

    policy: Literal["system", "always"]     # Reduce Motion: follow Windows, or always (body.reduce-motion)
    fx_enabled: bool                        # Background Effects is on


PageBuilder = Callable[["ThemeContext"], None]


@dataclass(frozen=True)
class ThemeContext:
    """Everything a shell or page builder gets, per page load."""

    core: CoreView
    behaviours: Behaviours
    prefs: ThemePrefs
    motion: Motion
    asset: Callable[[str], str]             # asset("img/logo.gif") -> "/theme/emiya/img/logo.gif?v=<mtime>"
    browser: bool = False                   # --browser: no native window (uploads instead of file dialogs)
    fx_telemetry: bool = False              # --fx-telemetry: the FX field reports its telemetry (dev only)


@runtime_checkable
class Theme(Protocol):
    """ui\\themes\\<slug>\\__init__.py defines THEME, a plain object with no UI at import.

    The composition root imports only the theme gui.py's manifest names, serves its static\\ folder at
    /theme/<slug> (every other /theme path is a 404), links its css, and gives the page these hooks to style:
    body.theme-<slug> (set before Vue mounts), body.reduce-motion (Reduce Motion "Always"), and a div.vt-page
    with the marker page.<id> around every page builder's output.

    Its own settings are theme.toml's [prefs] (theme.<slug>.* in ui_settings.json; ThemeContext.prefs reads
    and writes them): each default's type is its check. An optional attribute `pref_checks`, a mapping of a
    [prefs] key to check(value) -> clean value (raising ValueError), refines one beyond its type.
    """

    manifest: ThemeManifest
    pages: Mapping[Route, PageBuilder]      # all six routes; several may share one builder

    def build_shell(self, ctx: ThemeContext, mount_pages: Callable[[], None]) -> None:
        """Build the shell synchronously on every page load.

        It renders SHELL_MARKERS (plus FRAMELESS_MARKERS and the drag regions when frameless), renders
        every ConfirmRequest it receives, subscribes to core events here and nowhere else, and calls
        mount_pages() exactly once where page content goes.
        """
        ...
