"""The process-wide, theme-agnostic core (§4.1): Core, ForgeDraft and the build_core() factory.

Importing this package has no side effects and imports nothing but the standard library: the services
(ui.core.services.*) are imported through it by their own tests, and fake.py must load standalone.
build_core() imports the components when it is called; there are no import-time singletons.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from ui.contract.ports import AudioOutPort
    from ui.contract.records import ClipInfo
    from ui.contract.state import AppState, Session

    from .background import Runtime
    from .call import CallService
    from .clock import Scheduler
    from .edits import TakeEdits
    from .events import CoreEvents
    from .facade import EngineFacade, EventRelay
    from .jobs import JobRunner
    from .log import AppLog
    from .paths import Paths
    from .planner import Planner
    from .services.clips import ClipService
    from .services.load_history import LoadHistory
    from .services.settings import SettingsStore
    from .services.takes_store import TakesStore
    from .services.voices import VoiceLibrary
    from .tokens import TokenStore

__all__ = ["APP_NAME", "Core", "ForgeDraft", "build_core"]

APP_NAME = "Voice Tool"                  # the Log's "<app> started" when the composition root names none


@dataclass(eq=False)
class ForgeDraft:
    """The Forge page's draft: process-wide next to Session (survives reloads, not restarts; §5.9).

    ForgeController owns every write (refused while a forge job runs); the core only marks the result
    (forged) when it finishes a forge of this draft's name. rev bumps on every draft change, so the
    replace arm can be scoped to one draft revision (D-F3).
    """

    clip: ClipInfo | None = None
    clip_state: str = "empty"            # "empty" | "reading" | "ready" | "failed"
    clip_file: str = ""                  # the file being read, or the one that failed
    clip_error: str = ""                 # why the last pick failed
    name: str = ""
    backend: str = ""                    # Settings › Default Model at launch
    language: str = "en"                 # core.forge_language at launch
    exaggeration: float = 0.5
    consent: bool = False
    forged: str = ""                     # the voice this draft forged; "" = no result row
    rev: int = 0


@dataclass(eq=False, repr=False)
class Core:
    """Everything one GUI process shares: mirrors, services, the engine facade and the job slot."""

    paths: Paths
    settings: SettingsStore
    state: AppState                      # bindable mirrors (ui.contract.state)
    session: Session                     # one per process; survives reloads, not restarts
    events: CoreEvents
    log: AppLog
    scheduler: Scheduler
    facade: EngineFacade
    planner: Planner
    tokens: TokenStore
    takes: TakesStore
    history: LoadHistory
    library: VoiceLibrary
    clips: ClipService
    trash: Callable[[Path], None]        # send2trash.send2trash from launch.py; a recording fake in tests
    session_id: str                      # "This Session" = takes with this value
    app_name: str = APP_NAME             # the active theme's window title ("Emiya Voice"), for the Log
    forge_draft: ForgeDraft = field(default_factory=ForgeDraft)
    jobs: JobRunner = field(init=False)
    call: CallService = field(init=False)
    edits: TakeEdits = field(init=False)  # stars, Keep and Remove Unstarred, saved one at a time in order
    _runtime: Runtime = field(init=False)
    _staged: dict[str, int] = field(init=False, default_factory=dict)

    # ---------- lifecycle (composition root) ----------

    async def start(self) -> None:
        """app.on_startup (§4.18); never raises."""
        await self._runtime.start()

    def stop(self) -> None:
        """app.on_shutdown: never calls the engine (§4.18)."""
        self.call.close()
        self._runtime.stop()

    def client_connected(self) -> None:
        """Call on every page load (root()) or app.on_connect; only the first call does anything."""
        self._runtime.client_connected()

    async def wait_ready(self) -> None:
        """Wait until start() finished its checks (library snapshot and launch selection included)."""
        await self._runtime.wait_ready()

    # ---------- media URLs ----------

    def take_url(self, take_id: int) -> str:
        """"/media/take/7"."""
        return f"/media/take/{int(take_id)}"

    def clip_url(self, voice_id: str) -> str:
        """"/media/clip/aiko?v=<mtime_ns>": the version changes when the clip file does."""
        url = f"/media/clip/{quote(voice_id, safe='')}"
        row = next((r for r in self.state.library.rows if r.voice_id == voice_id), None)
        if row is None or row.reference_path is None:
            return url
        try:
            return f"{url}?v={os.stat(row.reference_path).st_mtime_ns}"
        except OSError:
            return url

    def staged_url(self, clip_id: str) -> str:
        """"/media/staged/<clip_id>", plus "?v=<n>" once that file was regenerated (a new test line)."""
        url = f"/media/staged/{quote(clip_id, safe='')}"
        version = self._staged.get(clip_id, 0)
        return f"{url}?v={version}" if version else url

    def bump_staged(self, clip_id: str) -> int:
        """A staged file was (re)written under the same id: give it a new URL so the Player reloads it."""
        self._staged[clip_id] = self._staged.get(clip_id, 0) + 1
        return self._staged[clip_id]

    # ---------- mirrors ----------

    def publish_takes(self) -> None:
        """Copy the takes store's published tuple into TakesState (after every store mutation)."""
        mirror = self.state.takes
        store = self.takes
        if (mirror.takes, mirror.next_id, mirror.rev) != (store.takes, store.next_id, store.rev):
            mirror.takes, mirror.next_id, mirror.rev = store.takes, store.next_id, store.rev
            self.events.notify()

    def publish_history(self) -> None:
        """Copy the load history's events into HistoryState."""
        events = self.history.events
        if self.state.history.events != events:
            self.state.history.events = events
            self.events.notify()

    async def refresh_library(self, *, changed_only: bool = False) -> None:
        """A new library snapshot; changed_only skips it while registry.json's mtime is unchanged."""
        await self._runtime.refresh_library(changed_only=changed_only)


def build_core(*, engine: Any, paths: Paths, settings: SettingsStore, trash: Callable[[Path], None],
               scheduler: Scheduler | None = None, relay: EventRelay | None = None,
               app_name: str = APP_NAME, audio_output: AudioOutPort | None = None) -> Core:
    """Assemble a Core around engine (VoiceEngine or FakeEngine); nothing loads, nothing is read yet.

    The engine's events must reach the core: pass the EventRelay the engine was built with
    (VoiceEngine(config, on_event=relay)); an engine with a public on_event attribute (FakeEngine) gets
    a new relay assigned. scheduler None means default_scheduler() (VOICETOOL_CLOCK=manual → manual).
    app_name is what the Log calls the app (the composition root passes the theme's window title).
    """
    from ui.contract.state import AppState, Session

    from .background import Runtime
    from .call import CallService
    from .clock import default_scheduler
    from .edits import TakeEdits
    from .events import CoreEvents
    from .facade import EngineFacade, EventRelay
    from .jobs import JobRunner
    from .log import AppLog
    from .planner import Planner
    from .services.clips import ClipService
    from .services.load_history import LoadHistory
    from .services.takes_store import TakesStore
    from .services.voices import VoiceLibrary
    from .tokens import TokenStore

    if relay is None:
        if not hasattr(engine, "on_event"):
            raise TypeError("build_core needs relay=: build the engine with on_event=EventRelay() and pass it")
        relay = EventRelay()
        engine.on_event = relay
    state = AppState()
    events = CoreEvents()
    history = LoadHistory(paths.history)
    facade = EngineFacade(engine, state=state, events=events, trash=trash, relay=relay)
    facade.sync_state()
    planner = Planner(state, history, facade)
    tokens = TokenStore(state, planner)
    facade.use_tokens(tokens)
    core = Core(
        paths=paths,
        settings=settings,
        state=state,
        session=Session(),
        events=events,
        log=AppLog(state=state.log, events=events),
        scheduler=scheduler if scheduler is not None else default_scheduler(),
        facade=facade,
        planner=planner,
        tokens=tokens,
        takes=TakesStore(paths.takes_index, paths.root),
        history=history,
        library=VoiceLibrary(paths.cache / "registry.json"),
        clips=ClipService(paths.voices, paths.staging),
        trash=trash,
        session_id=_session_id(engine),
        app_name=app_name,
        forge_draft=_draft(settings, state.engine.enabled),
    )
    core.call = CallService(core, audio_output)
    core.jobs = JobRunner(core)
    core.edits = TakeEdits(core)
    core._runtime = Runtime(core)
    return core


def _session_id(engine: Any) -> str:
    """The engine's stamp when it has one (FakeEngine's seeded "This Session" takes), else now."""
    stamp = getattr(engine, "session_id", None)
    return stamp if isinstance(stamp, str) and stamp else datetime.now().astimezone().isoformat(timespec="seconds")


def _draft(settings: SettingsStore, enabled: tuple[str, ...]) -> ForgeDraft:
    backend = str(settings.get("core", "default_backend"))
    if enabled and backend not in enabled:
        backend = enabled[0]
    return ForgeDraft(backend=backend, language=str(settings.get("core", "forge_language")))
