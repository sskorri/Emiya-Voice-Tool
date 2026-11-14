"""EngineFacade (§4.3-4.4): the only holder of the engine object, and the outcomes of its calls.

Every engine call runs in a worker through run.io_bound with _boxed, so a bare None can only mean the
call was cancelled (NiceGUI returns None for a cancelled task or a stopping app). Each method returns
an Outcome whose reason is UI-ready text; exceptions are reduced to text inside the except block and
never stored, logged with exc_info or passed on (their frames would keep model memory alive, §3.0).
The facade refuses what the mirrors say cannot be done (no engine call is made then), loads only with
a LoadTicket its TokenStore issued, and keeps EngineState in step with the engine's events, which the
EventRelay moves from the engine thread onto the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

import filelock
from nicegui import run

from engine.backends.info import BACKENDS
from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError
from engine.types import DeleteResult, EngineEvent, LoadReport, Residency

from . import catalog

if TYPE_CHECKING:
    from engine.device import DeviceInfo
    from engine.progress import Job
    from engine.types import VoiceDetails
    from ui.contract.plans import ForgeRequest, GenerateRequest
    from ui.contract.state import AppState

    from .events import CoreEvents
    from .tokens import LoadTicket, TokenStore

__all__ = [
    "BUSY",
    "ONE_MODEL",
    "OOM",
    "STALE",
    "EngineFacade",
    "EventRelay",
    "Outcome",
    "describe_error",
    "not_loaded",
]

T = TypeVar("T")
Status = Literal["done", "refused", "cancelled", "failed"]

REASON_MAX = 80
OOM = "CUDA out of memory"
MISSING_FILES = "model files are not in the local cache"
OFFLINE_SUFFIX = " (Offline Mode is on)"
REGISTRY_BUSY = "The voice registry is busy; try again."
ONE_MODEL = "This app supports one model in VRAM; set MAX_RESIDENT_BACKENDS = 1 in config.py"
EMPTY_TEXT = "Nothing to say: the text is empty."
STALE = "stale"                                      # a void token or ticket: no toast, recompute the label
BUSY = "Busy · wait for the current job"

log = logging.getLogger("voicetool.core")


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """What an engine call came to: done (with its value), refused, cancelled or failed (with a reason)."""

    status: Status
    value: T | None = None
    reason: str = ""                         # UI-ready text; never an exception object

    @property
    def ok(self) -> bool:
        return self.status == "done"


def _boxed(fn: Callable[..., T], *args: Any, **kwargs: Any) -> tuple[T]:
    """Runs in the worker: a result is always a 1-tuple, so None from run.io_bound means cancelled."""
    return (fn(*args, **kwargs),)


def not_loaded(backend: str) -> str:
    """"Turbo is not loaded"."""
    return f"{catalog.label(backend)} is not loaded"


def describe_error(e: BaseException) -> str:
    """The UI text for an exception (§4.3); "" for Cancelled."""
    if isinstance(e, Cancelled):
        return ""
    if isinstance(e, BackendNotResident):
        return not_loaded(e.backend)
    if isinstance(e, EngineBusy):
        return str(e)
    if isinstance(e, BackendLoadError):
        if e.kind == "out_of_memory":
            return OOM
        if e.kind == "missing_files":
            return MISSING_FILES + (OFFLINE_SUFFIX if os.environ.get("HF_HUB_OFFLINE") == "1" else "")
        return _short(e.detail) or _short(_message(e))
    if isinstance(e, GenerationError):
        return OOM if e.kind == "out_of_memory" else _short(_message(e))
    if isinstance(e, filelock.Timeout):
        return REGISTRY_BUSY
    if isinstance(e, ImportError):
        return f"missing dependency: {e.name or _first_line(_message(e)) or type(e).__name__}"
    text = _first_line(_message(e))
    if type(e).__name__ == "OutOfMemoryError" or "out of memory" in text.lower():
        return OOM
    return _short(text) or type(e).__name__


def _status_of(e: BaseException) -> Status:
    if isinstance(e, Cancelled):
        return "cancelled"
    if isinstance(e, (BackendNotResident, EngineBusy)):
        return "refused"
    return "failed"


def _holder(e: BaseException) -> dict | None:
    """EngineBusy's holder record (plain data), for the "Load refused" log line."""
    return dict(e.holder) if isinstance(e, EngineBusy) and e.holder else None


class EventRelay:
    """The engine's on_event callback, made before the core: VoiceEngine(config, on_event=relay).

    It runs on the engine's worker thread and only enqueues: each event is handed to the facade on
    the event loop with loop.call_soon_threadsafe (§4.10). Events before the facade attached, or after
    the loop closed, are dropped.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sink: Callable[[EngineEvent], None] | None = None

    def connect(self, sink: Callable[[EngineEvent], None], loop: asyncio.AbstractEventLoop) -> None:
        self._sink = sink
        self._loop = loop

    def __call__(self, event: EngineEvent) -> None:
        loop, sink = self._loop, self._sink
        if loop is None or sink is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(sink, event)
        except RuntimeError:            # the loop closed between the check and the call
            pass


class EngineFacade:
    """Async, outcome-returning access to the engine; nothing else in the app holds the engine."""

    def __init__(self, engine: Any, *, state: AppState, events: CoreEvents, trash: Callable[[Path], None],
                 relay: EventRelay) -> None:
        self._engine = engine
        self._state = state
        self._events = events
        self._trash = trash
        self._relay = relay
        self._tokens: TokenStore | None = None
        self._listeners: list[Callable[[EngineEvent], None]] = []
        self._weights: dict[str, int | None] = {}
        self._attached: asyncio.AbstractEventLoop | None = None
        self._preferred = str(engine.residency().preferred_device)

    # ---------- wiring ----------

    def use_tokens(self, tokens: TokenStore) -> None:
        """The TokenStore whose tickets load() accepts (the only one)."""
        self._tokens = tokens

    def listen(self, listener: Callable[[EngineEvent], None]) -> None:
        """Call listener(event) on the event loop for every engine event, after EngineState is updated."""
        self._listeners.append(listener)

    def attach(self) -> None:
        """Deliver engine events on the running loop (idempotent; core.start() and every call do it)."""
        loop = asyncio.get_running_loop()
        if self._attached is not loop:
            self._attached = loop
            self._relay.connect(self._deliver, loop)

    def sync_state(self) -> None:
        """Mirror the engine's residency, its limit and the enabled backends into EngineState (startup)."""
        residency = self._engine.residency()
        available = set(self._engine.list_backends())
        enabled = tuple(name for name in catalog.ORDER if name in available)
        self.set_engine(enabled=enabled, max_resident=int(residency.max_resident), **_resident_fields(residency))

    def set_engine(self, **changes: Any) -> None:
        """Write EngineState fields; rev bumps when anything changed (it voids tokens)."""
        mirror = self._state.engine
        changed = False
        for name, value in changes.items():
            if getattr(mirror, name) != value:
                setattr(mirror, name, value)
                changed = True
        if changed:
            mirror.rev += 1
            self._events.notify()

    def resync(self) -> None:
        """Re-read the residency (lock-free) into EngineState, e.g. after a cancelled call."""
        self.set_engine(**_resident_fields(self._engine.residency()))

    # ---------- models ----------

    async def load(self, ticket: LoadTicket) -> Outcome[LoadReport]:
        """make_resident(ticket.plan.to_backend), only for an unused ticket of this facade's TokenStore."""
        if self._tokens is None or not self._tokens.spend(ticket):
            return Outcome("refused", reason=STALE)
        if self._state.engine.max_resident != 1:
            return Outcome("refused", reason=ONE_MODEL)
        out: Outcome[LoadReport] = await self._call("make_resident", self._engine.make_resident,
                                                    ticket.plan.to_backend)
        if out.ok and out.value is not None:
            self.set_engine(resident=out.value.backend, device=out.value.device)
        return out

    async def unload(self, backend: str, *, reason: str) -> Outcome[bool]:
        """unload_backend(backend, reason=reason) for the mirrored resident model."""
        if not backend or backend != self._state.engine.resident:
            return Outcome("refused", reason=not_loaded(backend))
        out: Outcome[bool] = await self._call("unload_backend", self._engine.unload_backend, backend, reason=reason)
        if out.ok and out.value and self._state.engine.resident == backend:
            self.set_engine(resident="", device="")
        return out

    def set_preferred_device(self, device: str) -> Outcome[None]:
        """Where the next model load runs; a lock-free attribute write, so no worker."""
        try:
            self._engine.set_preferred_device(device)
        except Exception as exc:
            reason = describe_error(exc)
            log.warning("set_preferred_device failed: %s", reason)
            return Outcome("failed", reason=reason)
        self._preferred = device
        return Outcome("done")

    def preferred_device(self) -> str:
        return self._preferred

    def residency(self) -> Outcome[Residency]:
        """The engine's residency (lock-free); the UI reads EngineState instead."""
        return Outcome("done", self._engine.residency())

    # ---------- voices ----------

    async def speak(self, req: GenerateRequest, job: Job) -> Outcome[bytes]:
        """One take of req on the resident model (require_resident=True, so it can never load)."""
        if self._state.engine.resident != req.backend:
            return Outcome("refused", reason=not_loaded(req.backend))
        if not req.text.strip():
            return Outcome("refused", reason=EMPTY_TEXT)
        return await self._call("speak", self._engine.speak, req.text, req.voice_id, require_resident=True,
                                job=job, **_speak_params(req))

    async def bake(self, req: ForgeRequest, path: Path, job: Job) -> Outcome[str]:
        """Forge req from the committed clip at path on the resident model (always a fresh bake)."""
        if self._state.engine.resident != req.backend:
            return Outcome("refused", reason=not_loaded(req.backend))
        if req.consent is not True:
            return Outcome("refused", reason="Confirm Consent First")
        return await self._call("load_voice", self._engine.load_voice, req.voice_id, Path(path),
                                backend=req.backend, exaggeration=req.exaggeration, language=req.language,
                                force=True, require_resident=True, meta=_forge_meta(req), job=job)

    async def update_voice(self, voice_id: str, *, language: str | None = None,
                           exaggeration: float | None = None) -> Outcome[dict]:
        """Edit a voice's language or default Expressiveness (registry only; never loads)."""
        job = self._state.job
        if job.kind == "forge" and job.voice == voice_id:
            return Outcome("refused", reason=f"{voice_id} is being forged now")
        return await self._call("update_voice", self._engine.update_voice, voice_id, language=language,
                                exaggeration=exaggeration)

    async def delete_voice(self, voice_id: str) -> Outcome[DeleteResult]:
        """Delete a voice, its .pt going to the injected trash; refused while any job runs."""
        if self._state.job.kind:
            return Outcome("refused", reason=BUSY)
        return await self._call("delete_voice", self._engine.delete_voice, voice_id, discard=self._trash)

    async def voice_details(self) -> Outcome[list[VoiceDetails]]:
        return await self._call("voice_details", self._engine.voice_details)

    # ---------- facts ----------

    async def device_info(self, *, probe: bool = False) -> Outcome[DeviceInfo]:
        """A GPU reading (lock-free in the engine); probe=True may create the CUDA context."""
        return await self._call("device_info", self._engine.device_info, probe)

    async def prefill_weights(self) -> Outcome[dict[str, int | None]]:
        """weights_status() of every enabled backend in one worker, cached for weights_bytes()."""
        out: Outcome[dict[str, int | None]] = await self._call("weights_status", self._weights_of_enabled)
        if out.ok and out.value is not None:
            self._weights.update(out.value)
        return out

    def weights_bytes(self, backend: str) -> int | None:
        """The cached weights size; None until prefill_weights() ran (never calls huggingface_hub)."""
        return self._weights.get(backend)

    # ---------- plumbing ----------

    async def _call(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Outcome[Any]:
        self.attach()
        failure: tuple[Status, str, dict | None] | None = None
        try:
            boxed = await run.io_bound(_boxed, fn, *args, **kwargs)
        except Exception as exc:            # reduced to text here; the exception object dies with this block
            failure = (_status_of(exc), describe_error(exc), _holder(exc))
        if failure is not None:
            status, reason, holder = failure
            if status == "failed":
                log.warning("%s failed: %s", name, reason)
            elif status == "cancelled":
                self.resync()
            return Outcome(status, holder, reason)
        if boxed is None:
            self.resync()
            return Outcome("cancelled")
        return Outcome("done", boxed[0])

    def _weights_of_enabled(self) -> dict[str, int | None]:
        sizes: dict[str, int | None] = {}
        for backend in self._engine.list_backends():
            try:
                _, total = self._engine.weights_status(backend)
            except Exception:               # an unreadable cache: the size stays unknown
                total = None
            sizes[backend] = int(total) if total else None
        return sizes

    def _deliver(self, event: EngineEvent) -> None:
        """On the loop: mirror the residency the event reports, then tell the listeners."""
        if event.kind == "loaded":
            self.set_engine(resident=event.backend, device=event.device or "")
        elif event.kind == "load_failed" or (event.kind == "unloaded"
                                              and self._state.engine.resident == event.backend):
            self.set_engine(resident="", device="")
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:
                text = describe_error(exc)
                log.warning("An engine event listener failed on %s: %s", event.kind, text)


def _resident_fields(residency: Residency) -> dict[str, Any]:
    resident = residency.resident[-1] if residency.resident else ""
    return {"resident": resident, "device": (residency.device or "") if resident else ""}


def _speak_params(req: GenerateRequest) -> dict[str, Any]:
    """What a take sends: Turbo gets only language_id (TUB L95-99); the others also the two knobs."""
    info = BACKENDS.get(req.backend)
    params: dict[str, Any] = {"language_id": req.language}
    if (info is None or info.uses_exaggeration) and req.exaggeration is not None:
        params["exaggeration"] = req.exaggeration
    if (info is None or info.uses_cfg) and req.cfg_weight is not None:
        params["cfg_weight"] = req.cfg_weight
    return params


def _forge_meta(req: ForgeRequest) -> dict[str, Any]:
    clip = req.clip
    return {
        "consent": True,
        "consent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "clip_seconds": round(float(clip.selection_s), 2),
        "clip_source": str(clip.source) if clip.source is not None else clip.name,
    }


def _message(e: BaseException) -> str:
    try:
        return str(e)
    except Exception:
        return ""


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _short(text: str) -> str:
    """The first line, at most REASON_MAX characters (an ellipsis marks a cut)."""
    line = _first_line(text)
    return line if len(line) <= REASON_MAX else line[: REASON_MAX - 1].rstrip() + "…"
