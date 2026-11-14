"""Background work (§4.12, §4.18, §5.4 D15): device readings, warm-up, idle auto-unload, startup, shutdown.

Everything runs on core.scheduler, never takes the engine RLock and never blocks a page build:
- DevicePoller: the first reading (probe=True) once the first client connected, then every 2 s while
  CUDA is initialised and every 0.5 s while a switch or unload runs, plus one after each job; a tick
  is skipped while the previous reading is still running.
- Warmup: once, after that first reading; only with Warm Up on Launch = Last Model, a load recorded in
  the history, nothing resident and no job started since launch. Low VRAM skips it with a toast.
- IdleUnloader: every 15 s, frees the resident model after Auto-unload After Idle minutes without a
  switch, generate or forge job (or since the setting changed); never during a job or with a queue.
- Runtime.start(): mirrors, settings, the takes index and the load history (sync, small files), the
  launch line, then the library snapshot, the launch selection (D15), the weights sizes, the staging
  sweep, the sweep of temp files a crash left and the missing-WAV check in workers. Runtime.stop()
  never calls the engine.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nicegui import run

from engine.backends.info import BACKENDS
from ui.contract.records import LoadEvent

from . import catalog
from .facade import STALE, _boxed, describe_error
from .services.device import apply_reading, gb, gpu_about
from .services.leftovers import sweep_temp_files
from .services.load_history import now_iso
from .services.settings import fill_about, folder_path
from .services.voices import corrupt_text

if TYPE_CHECKING:
    from engine.device import DeviceInfo

    from . import Core
    from .clock import Handle

__all__ = [
    "IDLE_CHECK_S",
    "LAUNCH_LINE",
    "POLL_BUSY_S",
    "POLL_IDLE_S",
    "SETTINGS_NOT_SAVED",
    "WARMUP_IN_USE",
    "WARMUP_LINE",
    "DevicePoller",
    "IdleUnloader",
    "Runtime",
    "Warmup",
    "launch_line",
]

POLL_IDLE_S = 2.0                        # while CUDA is initialised
POLL_BUSY_S = 0.5                        # while a switch or unload runs
IDLE_CHECK_S = 15.0
LAUNCH_LINE = "{app} started · no model loaded (models load on demand)"            # M L1242
WARMUP_LINE = "{app} started · warming up {model}"
WARMUP_IN_USE = "Warm-up skipped · a model is already in use"
SETTINGS_NOT_SAVED = "Couldn't save {name}: {reason} · the change lasts until the app closes"     # NEW
GIB = 2 ** 30
_EPS = 1e-6

log = logging.getLogger("voicetool.core")


def launch_line(app_name: str, warming: str | None = None) -> str:
    """The Log's first line: "Emiya Voice started · no model loaded (…)", or "… · warming up Multilingual"."""
    if warming:
        return WARMUP_LINE.format(app=app_name, model=catalog.label(warming))
    return LAUNCH_LINE.format(app=app_name)


class DevicePoller:
    """GPU readings into DeviceState (and the About GPU row)."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._timer: Handle | None = None
        self._reading = False
        self._again = False
        self._first_done = False
        self._initialized = False
        self._last_start = 0.0

    @property
    def reading(self) -> bool:
        return self._reading

    def start(self) -> None:
        if self._timer is None:
            self._timer = self._core.scheduler.every(POLL_BUSY_S, self._tick)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def first_reading(self) -> None:
        """The first reading, with probe=True (Q9 default: live VRAM from launch)."""
        await self._read(probe=True)
        self._first_done = True

    def after_job(self) -> None:
        """One reading right after a job ended (runs after the current one if a reading is in flight)."""
        if not self._first_done:
            return
        if self._reading:
            self._again = True
        else:
            self._spawn()

    def _tick(self) -> None:
        if not self._first_done or self._reading:
            return
        job = self._core.state.job
        moving = job.kind in ("switch", "unload")
        if moving or (self._initialized and self._core.scheduler.now() - self._last_start >= POLL_IDLE_S - _EPS):
            self._spawn()

    def _spawn(self) -> None:
        self._reading = True
        self._core.scheduler.spawn(self._read(probe=False), name="voicetool device reading")

    async def _read(self, *, probe: bool) -> None:
        core = self._core
        self._reading = True
        self._last_start = core.scheduler.now()
        try:
            out = await core.facade.device_info(probe=probe)
            if out.ok and out.value is not None:
                self._apply(out.value)
        finally:
            self._reading = False
        if self._again:
            self._again = False
            self._spawn()

    def _apply(self, info: DeviceInfo) -> None:
        core = self._core
        self._initialized = bool(info.initialized)
        changed = apply_reading(core.state.device, info)
        about = gpu_about(core.state.device)
        if core.state.about.gpu != about:
            core.state.about.gpu = about
            changed = True
        if changed:
            core.events.notify()


class Warmup:
    """Warm Up on Launch: load the last model once, after the first device reading."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._mode = "off"
        self._last: str | None = None
        self._done = False

    def prepare(self) -> None:
        """Read the setting and the history at launch (a later change applies at the next launch)."""
        core = self._core
        self._mode = str(core.settings.get("core", "warmup"))
        last = core.history.last_loaded()
        self._last = last if last in core.state.engine.enabled else None

    @property
    def intends(self) -> str | None:
        """The backend the warm-up means to load, or None."""
        return self._last if self._mode == "last" else None

    def run(self) -> None:
        """Once: start the warm-up switch, or log why it is skipped (never retried)."""
        if self._done:
            return
        self._done = True
        target = self.intends
        if target is None:
            return
        core = self._core
        jobs = core.jobs
        if core.state.engine.resident or jobs.busy or jobs.queued or jobs.started_any:
            core.log.add(WARMUP_IN_USE)
            return
        plan = core.planner.switch_plan(target, reason="warmup")
        if plan.low_vram:
            text = f"Warm-up skipped · Low VRAM · {gb(plan.free_after_bytes)} GB free"
            core.log.add(text)
            core.events.say(text)
            return
        result = jobs.start_switch(core.tokens.mint(plan))
        if result.status == "refused":
            core.log.add("Warm-up skipped" + (f" · {result.reason}" if result.reason != STALE else ""))


class IdleUnloader:
    """Auto-unload After Idle (core.idle_unload_min minutes; 0 = never)."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._timer: Handle | None = None
        self._changed_at = float("-inf")
        self._retry_after = float("-inf")
        core.settings.changed.subscribe(self._setting_changed)

    def start(self) -> None:
        if self._timer is None:
            self._timer = self._core.scheduler.every(IDLE_CHECK_S, self.check)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def check(self) -> None:
        core = self._core
        minutes = int(core.settings.get("core", "idle_unload_min"))
        if minutes <= 0 or not core.state.engine.resident or core.jobs.busy or core.jobs.queued:
            return
        since = max(core.jobs.last_activity, self._changed_at)
        now = core.scheduler.now()
        if now >= self._retry_after and now - since >= minutes * 60 - _EPS:
            # A failed unload must not hammer the engine or repeat its toast on every 15-second check.
            self._retry_after = now + minutes * 60
            core.jobs.start_free(reason="idle")

    def _setting_changed(self, section: str, key: str) -> None:
        if (section, key) == ("core", "idle_unload_min"):
            self._changed_at = self._core.scheduler.now()
            self._retry_after = float("-inf")


class Runtime:
    """Startup, the first client, the library refresh and shutdown for one Core."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self.poller = DevicePoller(core)
        self.warmup = Warmup(core)
        self.idle = IdleUnloader(core)
        self._client_seen = False
        self._booted = False                 # start() finished its sync part
        self._ready = asyncio.Event()        # start() finished everything
        self._library_lock = asyncio.Lock()
        self._told_unsaved = False           # a failed settings save was reported this session
        core.jobs.on_idle(self.poller.after_job)
        core.settings.changed.subscribe(self._setting_changed)
        core.settings.save_failed.subscribe(self._settings_not_saved)

    def _setting_changed(self, section: str, key: str) -> None:
        """Run Models On applies at the next load; the About card follows the Output Folder."""
        if section != "core":
            return
        if key == "device":
            self._core.facade.set_preferred_device(str(self._core.settings.get("core", "device")))
        elif key == "takes_dir":
            self._fill_about()

    def _settings_not_saved(self, reason: str) -> None:
        """A settings change could not be saved (a read-only file, a full disk): one red toast per session."""
        if self._told_unsaved:
            return
        self._told_unsaved = True
        text = SETTINGS_NOT_SAVED.format(name=self._core.settings.path.name, reason=reason)
        self._core.log.add(text)
        self._core.events.say(text, bad=True)

    # ---------- startup ----------

    async def start(self) -> None:
        """app.on_startup: the quick sync part first, then the worker checks (never raises)."""
        core = self._core
        try:
            core.facade.attach()
            core.facade.sync_state()
            core.facade.set_preferred_device(str(core.settings.get("core", "device")))
            self._load_takes()
            self._load_history()
            self._fill_about()
            self.warmup.prepare()
            core.log.add(self._launch_line())
            core.jobs.last_activity = core.scheduler.now()
            self.poller.start()
            self.idle.start()
            self._booted = True
            if self._client_seen:
                self._spawn_first_client()
            await self._checks()
            if core.settings.get("core", "call_enabled"):
                await core.call.refresh_devices()
        except Exception as exc:
            text = describe_error(exc)
            log.warning("Startup failed: %s", text)
        finally:
            self._booted = True
            self._ready.set()

    async def wait_ready(self) -> None:
        """Wait until start() has finished its checks (the launch selection included)."""
        await self._ready.wait()

    def client_connected(self) -> None:
        """The first call releases the held toasts and starts the first reading, then the warm-up.

        Before start() has read the settings and the history, the reading waits for it.
        """
        if self._client_seen:
            return
        self._client_seen = True
        self._core.events.release()
        if self._booted:
            self._spawn_first_client()

    def _spawn_first_client(self) -> None:
        self._core.scheduler.spawn(self._first_client(), name="voicetool first reading")

    async def _first_client(self) -> None:
        await self.poller.first_reading()
        self.warmup.run()

    def _launch_line(self) -> str:
        return launch_line(self._core.app_name, self.warmup.intends)

    def _load_takes(self) -> None:
        core = self._core
        folder = folder_path(str(core.settings.get("core", "takes_dir")), core.paths.root)
        notice = ""
        try:
            notice = core.takes.load(folders=[folder]).notice
        except Exception as exc:
            notice = f"The takes list couldn't be read · {describe_error(exc)} · the WAVs are untouched"
        core.publish_takes()
        if notice:
            core.log.add(notice)
            core.events.say(notice, bad=True)

    def _load_history(self) -> None:
        core = self._core
        problem = ""
        try:
            core.history.load()
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            log.warning("Couldn't read the load history: %s", problem)
        core.publish_history()

    def _fill_about(self) -> None:
        core = self._core
        fill_about(core.state.about, root=core.paths.root, cache=core.paths.cache, voices=core.paths.voices,
                   takes_dir=str(core.settings.get("core", "takes_dir")),
                   max_resident=core.state.engine.max_resident)
        core.state.about.gpu = gpu_about(core.state.device)
        core.events.notify()

    async def _checks(self) -> None:
        core = self._core
        await self.refresh_library()
        self._launch_selection()
        await core.facade.prefill_weights()
        await self._in_worker("the staging sweep", core.clips.sweep)
        removed = await self._in_worker("the temp-file sweep",
                                        lambda: sweep_temp_files(core.paths.root, core.paths.cache, BACKENDS))
        if removed:
            log.info("Removed %d temp file(s) a crash left in the data root", removed)
        if await self._in_worker("the missing-WAV check", core.takes.check_files) is not None:
            core.publish_takes()

    async def _in_worker(self, what: str, fn: Any) -> Any:
        problem = ""
        try:
            done = await run.io_bound(_boxed, fn)
        except Exception as exc:
            problem = describe_error(exc)
            done = None
        if problem:
            log.warning("%s failed: %s", what.capitalize(), problem)
        return None if done is None else done[0]

    def _launch_selection(self) -> None:
        """D15: the newest take, and its voice with pick_voice values when it is still usable; no load."""
        core = self._core
        takes = core.takes.takes
        if not takes:
            return
        newest = takes[0]
        session = core.session
        session.take_id = newest.id
        row = next((r for r in core.state.library.rows
                    if r.voice_id == newest.voice and r.backend_enabled and r.backend in core.state.engine.enabled),
                   None)
        if row is not None:
            info = BACKENDS.get(row.backend)
            session.voice_id = row.voice_id
            session.language = "en" if info is not None and info.english_only else row.language
            session.exaggeration = row.exaggeration
            session.cfg_weight = catalog.DEFAULT_CFG_WEIGHT
        core.events.notify()

    # ---------- the library ----------

    async def refresh_library(self, *, changed_only: bool = False) -> None:
        """A new snapshot into LibraryState; changed_only skips it while registry.json is unchanged."""
        async with self._library_lock:
            await self._refresh(changed_only)

    async def _refresh(self, changed_only: bool) -> None:
        core = self._core
        mirror = core.state.library
        mtime = core.library.registry_mtime()
        if changed_only and mirror.status not in ("loading", "failed") and mtime == mirror.registry_mtime:
            return
        details = await core.facade.voice_details()
        if details.status == "cancelled":
            return
        if not details.ok:
            self._set_library(status="failed", error=details.reason)
            core.log.add(f"Couldn't read the voice registry · {details.reason}")
            return
        snapshot = await self._in_worker("the library snapshot", lambda: core.library.snapshot(details.value))
        if snapshot is None:
            return
        # "corrupt" lasts until the registry has voices again (R2: the snapshot a page open takes right after the
        # quarantine reads an empty registry, which is no reason to forget why the library is empty)
        corrupt = bool(snapshot.new_corrupt) or (mirror.status == "corrupt" and not snapshot.rows)
        self._set_library(rows=snapshot.rows, corrupt_files=snapshot.corrupt_files, registry_mtime=mtime,
                          status="corrupt" if corrupt else "ready", error="")
        for name in snapshot.new_corrupt:
            text = corrupt_text(name)
            core.log.add(text)
            core.events.say(text, bad=True)

    def _set_library(self, **changes: Any) -> None:
        mirror = self._core.state.library
        changed = False
        for name, value in changes.items():
            if getattr(mirror, name) != value:
                setattr(mirror, name, value)
                changed = True
        if changed:
            mirror.rev += 1
            self._core.events.notify()

    # ---------- shutdown ----------

    def stop(self) -> None:
        """app.on_shutdown: no engine call; cancel the job, record "closed", flush the settings."""
        core = self._core
        self.poller.stop()
        self.idle.stop()
        core.jobs.cancel_running()
        resident = core.state.engine.resident
        if resident:
            self._record_closed(resident)
        core.settings.flush()

    def _record_closed(self, resident: str) -> None:
        core = self._core
        device = core.state.engine.device or "cuda"
        reading = core.state.device
        measured = device == "cuda" and bool(reading.cuda)
        freed = reading.app_bytes / GIB if measured else None
        peak = reading.peak_bytes / GIB if measured and reading.peak_bytes else None
        problem = ""
        try:
            core.history.append(LoadEvent(t=now_iso(), kind="closed", backend=resident,
                                          gb=None if freed is None else round(freed, 3),
                                          peak_gb=None if peak is None else round(peak, 3), device=device))
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            log.warning("Couldn't record the close: %s", problem)
