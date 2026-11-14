"""VoiceEngine: the one object that loads models, speaks, forges and manages voices.

Backends in config.ENABLED_BACKENDS are selectable, not preloaded: constructing the
engine imports neither torch nor chatterbox and loads nothing. A model loads when a call
needs it (make_resident(), or implicitly in speak()/load_voice() unless
require_resident=True) and stays resident; at most MAX_RESIDENT_BACKENDS are resident,
and the least recently used one is unloaded first. A switch runs under one hold of the
engine RLock and of the cross-process engine lock (cache\\engine.lock), so no other
process can load a model in the gap between the eviction and the build.

Everything the GUI polls (residency(), device_info(), voice_details(), ...) is
lock-free. Model-side failures leave the engine as fresh exceptions with no
__context__, so no traceback keeps a model or its KV cache alive.
"""
from __future__ import annotations

import contextlib
import gc
import logging
import math
import os
import sys
import threading
import time
import warnings
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, NoReturn, TypeVar

from . import progress
from .backends.base import VoiceBackend, stage_conditioning, validate_voice_id
from .backends.info import BACKENDS, BackendInfo
from .backends.info import weights_status as _weights_status
from .device import DeviceInfo
from .device import device_info as _device_info
from .errors import (BackendLoadError, BackendNotResident, Cancelled, GenerationError, LoadFailure, ScriptTooLong,
                     first_line)
from .lock import EngineLock
from .progress import Job
from .registry import VoiceRegistry
from .types import DeleteResult, EngineEvent, LoadReport, Residency, VoiceDetails

_T = TypeVar("_T")
_Discard = Callable[[Path], None]
_EventKind = Literal["load_started", "loaded", "load_failed", "unloaded"]

_EMPTY_TEXT = "Nothing to say: the text is empty."
_DEVICES = ("cuda", "cpu")
_MAX_EXAGGERATION = 2.0
_ENGLISH_ONLY: Mapping[str, str] = {"en": "English"}

# Warnings go to the "engine" logger: cli.py prints them on stderr as they are, the GUI formats them into its log.
log = logging.getLogger("engine")


def _build_multilingual(cache_dir: Path, device: str) -> VoiceBackend:
    from .backends.multilingual import ChatterboxMultilingualBackend
    return ChatterboxMultilingualBackend(cache_dir=cache_dir, device=device)


def _build_standard(cache_dir: Path, device: str) -> VoiceBackend:
    from .backends.standard import ChatterboxStandardBackend
    return ChatterboxStandardBackend(cache_dir=cache_dir, device=device)


def _build_turbo(cache_dir: Path, device: str) -> VoiceBackend:
    from .backends.turbo import ChatterboxTurboBackend
    return ChatterboxTurboBackend(cache_dir=cache_dir, device=device)


# Each factory imports its backend module only when invoked, so one backend's
# missing/incompatible dependency can't take engine startup down with it, and
# constructing the engine imports neither torch nor chatterbox.
_BACKEND_FACTORIES = {
    "chatterbox-multilingual": _build_multilingual,
    "chatterbox-standard": _build_standard,
    "chatterbox-turbo": _build_turbo,
}


def _resolve_device(preferred: str) -> str:
    """The device a load runs on: `preferred`, or "cpu" (with a warning saying why) when CUDA is not usable."""
    if preferred != "cuda":
        return preferred
    reason = "torch reports no CUDA device"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception as exc:                  # a broken torch install or CUDA DLL: keep the reason
        reason = f"{type(exc).__name__}: {_error_text(exc)}"
    log.warning("Warning: CUDA not available (%s); falling back to CPU (generation will be slow).", reason)
    return "cpu"


class VoiceEngine:
    """Orchestrates voice backends with on-demand, single-resident loading.

    Backends in config.ENABLED_BACKENDS are *selectable*, not preloaded:
    constructing the engine loads no models into VRAM (and imports neither
    torch nor chatterbox). A backend's model is loaded the first time it is
    needed and kept resident; when a different backend is needed and the
    resident count would exceed MAX_RESIDENT_BACKENDS (default 1), the
    least-recently-used backend is unloaded and its VRAM freed first. That
    keeps a 12 GB card from ever holding every model at once.

    on_event receives an EngineEvent for every load_started, loaded,
    load_failed and unloaded, on the engine thread with the RLock held; it
    must only hand the event on. The cross-process engine lock (lock_path,
    default CACHE_DIR/engine.lock) is held while a model is resident, or for
    the engine's whole life with hold_engine_lock=True (until close()).
    """

    def __init__(self, config: Any, *, on_event: Callable[[EngineEvent], None] | None = None,
                 hold_engine_lock: bool = False, lock_path: Path | None = None) -> None:
        self.config = config
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.registry = VoiceRegistry(config.CACHE_DIR / "registry.json")

        wanted = getattr(config, "ENABLED_BACKENDS", None) or list(_BACKEND_FACTORIES)
        self._available = [n for n in wanted if n in _BACKEND_FACTORIES]
        for n in wanted:
            if n not in _BACKEND_FACTORIES:
                log.warning("Warning: unknown backend '%s' in ENABLED_BACKENDS, ignoring.", n)
        if not self._available:
            raise RuntimeError("No known backends in config.ENABLED_BACKENDS.")

        self._max_resident = max(1, int(getattr(config, "MAX_RESIDENT_BACKENDS", 1)))
        self._preferred_device: str = getattr(config, "DEVICE", "cuda")
        self._root = Path(getattr(config, "ROOT", config.CACHE_DIR.parent))
        self._voices_dir = Path(getattr(config, "VOICES_DIR", self._root / "voices" / "reference"))
        # Resident models, most-recently-used last (LRU order), and the device each runs on.
        self._backends: OrderedDict[str, VoiceBackend] = OrderedDict()
        self._devices: dict[str, str] = {}
        # Serializes load/evict, takes, bakes and deletes so a backend can't be unloaded
        # out from under an in-flight speak(); reentrant so speak()/load_voice() can call
        # _get_backend() while holding it.
        self._lock = threading.RLock()
        # Guards only the swap of the published Residency; never held while working.
        self._state_mx = threading.Lock()
        self._on_event = on_event
        self._hold_lock = hold_engine_lock
        self._engine_lock = EngineLock(Path(lock_path) if lock_path is not None
                                       else config.CACHE_DIR / "engine.lock")
        self._residency = Residency(
            resident=(), loading=None, unloading=None, busy=None, device=None,
            preferred_device=self._preferred_device, max_resident=self._max_resident,
            last_used=None, lock_held=False,
        )
        if hold_engine_lock:
            self._take_engine_lock()

    # ---------- residency (lock-free) ----------

    def residency(self) -> Residency:
        """What is resident, loading or unloading now. Never waits for a load or a take."""
        return self._residency

    def is_resident(self, name: str) -> bool:
        return name in self._residency.resident

    def list_loaded_backends(self) -> list[str]:
        """Resident backends, least recently used first. Lock-free."""
        return list(self._residency.resident)

    # ---------- models ----------

    def make_resident(self, name: str) -> LoadReport:
        """Make `name` resident, evicting the least recently used model first when needed.

        The only call the GUI uses to load. Raises EngineBusy (another process holds a
        model; nothing evicted) or BackendLoadError (the build failed; nothing of it stays).
        """
        self._require_enabled(name)
        with self._lock:
            try:
                if name in self._backends:
                    self._touch(name)
                    return self._resident_report(name)
                return self._load(name)[1]
            finally:
                self._publish(last_used=time.monotonic())

    def load_backend(self, name: str) -> str:
        """Make `name` resident (loading it, evicting others as needed) and return its name."""
        return self.make_resident(name).backend

    def unload_backend(self, name: str, *, reason: str = "requested") -> bool:
        """Free `name`'s model. False when it was not resident (or is unknown)."""
        with self._lock:
            if name not in self._backends:
                return False
            self._publish(busy="unload")
            try:
                self._release(name, reason)
            finally:
                self._publish(busy=None)
            if not self._backends:
                self._drop_engine_lock()
            return True

    def close(self) -> None:
        """Unload every model (reason "close") and release the engine lock, also a held one."""
        with self._lock:
            self._publish(busy="unload")
            try:
                for name in list(self._backends):
                    self._release(name, "close")
            finally:
                self._publish(busy=None)
            self._hold_lock = False
            self._drop_engine_lock()

    def set_preferred_device(self, device: Literal["cuda", "cpu"]) -> None:
        """Where the next model load runs. Lock-free; never unloads the resident model."""
        if device not in _DEVICES:
            raise ValueError(f"Unknown device {device!r}: use 'cuda' or 'cpu'.")
        with self._state_mx:
            self._preferred_device = device
            self._residency = replace(self._residency, preferred_device=device)

    def engine_lock_holder(self) -> dict | None:
        """{"pid", "program", "since"} of the engine lock's holder, or None."""
        return self._engine_lock.holder()

    # ---------- voices ----------

    def speak(self, text: str, voice_id: str, *, require_resident: bool = False,
              job: Job | None = None, **params: Any) -> bytes:
        """Generate WAV bytes for `text` in `voice_id`'s voice.

        Unset language_id and exaggeration come from the voice's registration. With
        require_resident=True a non-resident backend raises BackendNotResident instead of
        loading. A model-side failure raises a fresh ValueError (the backend's own
        ValueError) or GenerationError; a cancelled job raises Cancelled.
        """
        with _JobOutcome(job):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(_EMPTY_TEXT)
            with self._lock:
                info = self.registry.get(voice_id)
                if info is None:
                    raise ValueError(
                        f"Voice '{voice_id}' is not registered. Call load_voice() first."
                    )
                if params.get("language_id") is None:
                    params["language_id"] = info.get("language") or self.config.DEFAULT_LANGUAGE
                if params.get("exaggeration") is None:
                    params["exaggeration"] = info.get(
                        "exaggeration", self.config.DEFAULT_EXAGGERATION
                    )
                try:
                    return self._take(info["backend"], text, voice_id, require_resident, job, params)
                finally:
                    self._publish(busy=None, last_used=time.monotonic())

    def load_voice(
        self,
        voice_id: str,
        reference_wav: Path,
        backend: str | None = None,
        exaggeration: float | None = None,
        language: str | None = None,
        force: bool = False,
        *,
        require_resident: bool = False,
        meta: dict | None = None,
        job: Job | None = None,
        **params: Any,
    ) -> str:
        """Bake (or, with a cached .pt and force=False, reuse) a voice's conditioning and register it.

        Every input is checked before any model loads. On a cache hit the entry
        keeps the clip its .pt was baked from.
        """
        with _JobOutcome(job):
            backend = backend or self.config.DEFAULT_BACKEND
            exaggeration = _new_exaggeration(
                self.config.DEFAULT_EXAGGERATION if exaggeration is None else exaggeration
            )
            language = self.config.DEFAULT_LANGUAGE if language is None else language
            reference = Path(reference_wav).resolve()
            language = self._check_new_voice(voice_id, reference, backend, language)
            with self._lock:
                try:
                    if job is not None:
                        job.check()
                    target = self._get_backend(backend, allow_load=not require_resident)
                    path = self._conds_path(backend, voice_id)
                    with self.registry.cached_reference(voice_id, backend, path, force=force) as source:
                        cached = source is not None
                        with stage_conditioning(path, cached=cached,
                                                invalidate=lambda: self._forget_active_voice(voice_id)) as staged:
                            self._bake(backend, job, cached, lambda: target.load_voice(
                                voice_id, reference, exaggeration=exaggeration, force=not cached, **params))
                            stored = self._kept_reference(voice_id, backend, reference, source)
                            self.registry.register(
                                voice_id,
                                backend=backend,
                                reference_wav=stored,
                                exaggeration=exaggeration,
                                language=language,
                                meta=meta,
                                baked=not cached,
                                staged=staged,
                            )
                    return voice_id
                finally:
                    self._publish(busy=None, last_used=time.monotonic())

    def update_voice(self, voice_id: str, *, language: str | None = None,
                     exaggeration: float | None = None, meta: dict | None = None) -> dict:
        """Edit a voice's language, default Expressiveness or meta; returns the new entry.

        Registry only: no model, no RLock, no torch. The next take reads the new values.
        """
        entry = self.registry.get(voice_id)
        if not isinstance(entry, dict):
            raise ValueError(f"Voice '{voice_id}' is not registered.")
        backend = str(entry.get("backend", ""))
        info = BACKENDS.get(backend)
        if language is not None:
            language = _edited_language(backend, info, language)
        if exaggeration is not None:
            exaggeration = _edited_exaggeration(backend, info, exaggeration)
        updated = self.registry.update(voice_id, language=language, exaggeration=exaggeration, meta=meta)
        if updated is None:                          # removed by another process meanwhile
            raise ValueError(f"Voice '{voice_id}' is not registered.")
        return updated

    def delete_voice(self, voice_id: str, *, discard: Callable[[Path], None] | None = None) -> DeleteResult:
        """Delete a voice: its .pt first, then its registry entry, then orphan .pt files.

        Waits for a running job. `discard` (default Path.unlink; the GUI passes
        send2trash) runs on the primary .pt first, and if it raises nothing has changed;
        a primary .pt that is already missing counts as removed. Orphans in the other
        backend folders are best effort and land in failed_files with their reason. A .pt
        that a remaining voice, spelled differently, shares on NTFS is kept and reported
        as "kept: belongs to <id>". The reference WAV is never touched.
        """
        validate_voice_id(voice_id)
        discard = discard or Path.unlink
        with self._lock, self.registry.voice_files_lock():
            entries = self.registry.entries()
            if voice_id not in entries:
                return DeleteResult(removed_entry=False, removed_files=(), failed_files=())
            entry = entries[voice_id]
            backend = entry.get("backend") if isinstance(entry, dict) else None
            twins = _casefold_twins(entries, voice_id)
            removed: list[Path] = []
            failed: list[tuple[Path, str]] = []
            if isinstance(backend, str):
                _discard_primary(self._conds_path(backend, voice_id), twins.get(backend),
                                 discard, removed, failed)
            self._forget_active_voice(voice_id)
            self.registry.remove(voice_id)
            for other in _BACKEND_FACTORIES:
                if other != backend:
                    _discard_orphan(self._conds_path(other, voice_id), twins.get(other),
                                    discard, removed, failed)
            return DeleteResult(removed_entry=True, removed_files=tuple(removed),
                                failed_files=tuple(failed))

    def voice_details(self, voice_id: str | None = None) -> list[VoiceDetails]:
        """Registry entries plus file facts, sorted by id (one voice, or [] if unknown).

        One registry read and a few stat calls; no torch, no RLock.
        """
        entries = self.registry.entries()
        ids = sorted(entries) if voice_id is None else [voice_id]
        return [self._details(vid, entries[vid]) for vid in ids if isinstance(entries.get(vid), dict)]

    def voice_cache_path(self, voice_id: str) -> Path | None:
        """CACHE_DIR/<backend>/<voice_id>.pt for a registered voice, else None."""
        entry = self.registry.get(voice_id)
        backend = entry.get("backend") if isinstance(entry, dict) else None
        return self._conds_path(backend, voice_id) if isinstance(backend, str) else None

    # ---------- facts (no torch unless probe) ----------

    def device_info(self, probe: bool = False) -> DeviceInfo:
        """GPU readings for the VRAM meter; lock-free (see engine.device)."""
        return _device_info(probe)

    def backend_info(self, name: str) -> BackendInfo:
        """Static facts about a backend; imports nothing."""
        info = BACKENDS.get(name)
        if info is None:
            raise ValueError(f"Unknown backend '{name}'. Known: {list(BACKENDS)}")
        return info

    def weights_status(self, name: str) -> tuple[bool, int | None]:
        """(all weight files in the local Hugging Face cache?, total bytes); never the network."""
        return _weights_status(name)

    # ---------- introspection ----------

    def list_voices(self) -> list[str]:
        return self.registry.list_ids()

    def list_backends(self) -> list[str]:
        """Selectable backends (config.ENABLED_BACKENDS), whether or not their
        model is currently resident."""
        return list(self._available)

    def voice_info(self, voice_id: str) -> dict | None:
        return self.registry.get(voice_id)

    # ---------- loading and unloading (call with the RLock held) ----------

    def _require_enabled(self, name: str) -> None:
        if name not in self._available:
            raise ValueError(
                f"Backend '{name}' isn't enabled. Available: {self._available}"
            )

    def _get_backend(self, name: str, *, allow_load: bool = True) -> VoiceBackend:
        """The resident backend `name`; loads it (steps 4-9 in _load) unless allow_load is False."""
        self._require_enabled(name)
        with self._lock:
            existing = self._backends.get(name)
            if existing is not None:
                self._touch(name)
                return existing
            if not allow_load:
                raise BackendNotResident(name, tuple(self._backends))
            return self._load(name)[0]

    def _load(self, name: str) -> tuple[VoiceBackend, LoadReport]:
        """Engine lock, eviction, device, build, then bookkeeping; a failed build cleans up."""
        self._take_engine_lock()                  # EngineBusy propagates: nothing evicted yet
        self._publish(loading=name, busy="load")
        self._emit("load_started", name)
        evicted, freed = self._evict()
        device = _resolve_device(self._preferred_device)
        first = "chatterbox" not in sys.modules  # this build pays the chatterbox import
        started = time.perf_counter()
        try:
            backend = _BACKEND_FACTORIES[name](self.config.CACHE_DIR / name, device)
        except Exception as exc:
            kind, detail = _load_failure_kind(exc), _error_text(exc)
        else:
            seconds = time.perf_counter() - started
            return backend, self._finish_load(name, backend, device, seconds, evicted, freed, first)
        # Outside the except block: the traceback holding the half-built model is gone.
        self._fail_load(name, device, time.perf_counter() - started, kind, detail)

    def _evict(self) -> tuple[tuple[str, ...], int | None]:
        """Unload least recently used models until one more fits; (evicted, freed bytes)."""
        evicted: list[str] = []
        freed: list[int | None] = []
        while len(self._backends) >= self._max_resident:
            oldest = next(iter(self._backends))
            log.info("Unloading backend '%s' to free VRAM.", oldest)
            freed.append(self._release(oldest, "evicted"))
            evicted.append(oldest)
        known = [b for b in freed if b is not None]
        total = sum(known) if evicted and len(known) == len(freed) else None
        return tuple(evicted), total

    def _finish_load(self, name: str, backend: VoiceBackend, device: str, seconds: float,
                     evicted: tuple[str, ...], freed: int | None, first: bool) -> LoadReport:
        self._backends[name] = backend
        self._devices[name] = device
        self._install_progress()
        cuda = _cuda_for(device)
        reserved = _cuda_bytes(cuda, "memory_reserved")
        allocated = _cuda_bytes(cuda, "memory_allocated")
        _reset_peak(cuda)
        self._publish_resident(loading=None, busy=None)
        self._emit("loaded", name, seconds=seconds, device=device, reserved_bytes=reserved,
                   allocated_bytes=allocated, first_in_process=first)
        return LoadReport(backend=name, device=device, seconds=seconds, already_resident=False,
                          evicted=evicted, freed_bytes=freed, reserved_bytes=reserved,
                          allocated_bytes=allocated, first_in_process=first)

    def _fail_load(self, name: str, device: str, seconds: float, kind: LoadFailure, detail: str) -> NoReturn:
        gc.collect()
        _empty_cache(device)
        self._publish_resident(loading=None, busy=None)
        if not self._backends:
            self._drop_engine_lock()
        self._emit("load_failed", name, seconds=seconds, device=device, error=f"{kind}: {detail}")
        raise BackendLoadError(name, kind, detail, seconds)

    def _install_progress(self) -> None:
        """Hook take progress and cancel into Chatterbox's loops (idempotent).

        A failure only costs the progress readout; the model is loaded and takes work.
        """
        try:
            progress.install()
        except Exception as exc:
            log.warning("Warning: take progress is unavailable (%s).", _error_text(exc))

    def _release(self, name: str, reason: str) -> int | None:
        """Unload one model, measured; emits `unloaded` and returns the freed bytes (None on CPU)."""
        device = self._devices.get(name)
        self._publish(unloading=name)
        cuda = _cuda_for(device)
        peak = _cuda_bytes(cuda, "max_memory_reserved")
        before = _cuda_bytes(cuda, "memory_reserved")
        self._unload(name)
        cuda = _cuda_for(device)
        after = _cuda_bytes(cuda, "memory_reserved")
        allocated = _cuda_bytes(cuda, "memory_allocated")
        freed = max(0, before - after) if before is not None and after is not None else None
        self._publish_resident(unloading=None)
        self._emit("unloaded", name, device=device, reserved_bytes=after, allocated_bytes=allocated,
                   freed_bytes=freed, peak_bytes=peak, reason=reason)
        return freed

    def _unload(self, name: str) -> None:
        """Drop the model and free its memory. Never touches the engine lock."""
        backend = self._backends.pop(name, None)
        device = self._devices.pop(name, None)
        if backend is None:
            return
        with contextlib.suppress(Exception):
            setattr(backend, "model", None)
        del backend
        gc.collect()
        _empty_cache(device)

    def _touch(self, name: str) -> None:
        self._backends.move_to_end(name)
        self._publish_resident()

    def _resident_report(self, name: str) -> LoadReport:
        device = self._devices[name]
        cuda = _cuda_for(device)
        return LoadReport(backend=name, device=device, seconds=0.0, already_resident=True,
                          evicted=(), freed_bytes=None,
                          reserved_bytes=_cuda_bytes(cuda, "memory_reserved"),
                          allocated_bytes=_cuda_bytes(cuda, "memory_allocated"),
                          first_in_process=False)

    def _take_engine_lock(self) -> None:
        self._engine_lock.acquire()
        self._publish(lock_held=True)

    def _drop_engine_lock(self) -> None:
        """Release the cross-process lock, unless hold_engine_lock keeps it until close()."""
        if self._hold_lock:
            return
        self._engine_lock.release()
        self._publish(lock_held=False)

    # ---------- takes and bakes (call with the RLock held) ----------

    def _take(self, name: str, text: str, voice_id: str, require_resident: bool,
              job: Job | None, params: dict[str, Any]) -> bytes:
        if job is not None:
            job.check()
        backend = self._get_backend(name, allow_load=not require_resident)
        self._publish(busy="generate")
        _set_phase(job, "preparing")
        started = time.perf_counter()
        with progress.bind(job):
            wav = self._call_model("speak", name, lambda: backend.speak(text, voice_id, **params))
        if job is not None:
            job.made_s = time.perf_counter() - started
            job.conds_load_s = getattr(backend, "last_conds_load_s", None)
            # Never reset per take: the peak covers the whole residency (reset only at load).
            job.peak_reserved_bytes = _cuda_bytes(_cuda_for(self._devices.get(name)), "max_memory_reserved")
        return wav

    def _bake(self, name: str, job: Job | None, cached: bool, work: Callable[[], object]) -> None:
        """Run the backend's load_voice: a bake, or on a cache hit a load of the existing .pt."""
        self._publish(busy="bake")
        _set_phase(job, "preparing" if cached else "baking")
        started = time.perf_counter()
        with progress.bind(job):
            self._call_model("bake", name, work)
        if job is not None and not cached:
            job.bake_s = time.perf_counter() - started

    def _call_model(self, during: Literal["speak", "bake"], name: str, work: Callable[[], _T]) -> _T:
        """Run a take or a bake; any failure comes back as a fresh exception with no __context__.

        Raising inside the except block would chain the original exception, whose
        traceback holds the model's frames (on a take, T3's KV cache too), so an unload
        could never free them. Only the type and the first line of the message survive.
        """
        failure: tuple[bool, str, bool] | None = None
        too_long: tuple[int, int] | None = None
        try:
            return work()
        except Cancelled:
            pass
        except ScriptTooLong as exc:
            too_long = exc.count, exc.limit
        except Exception as exc:
            failure = (isinstance(exc, ValueError), _error_text(exc), _is_out_of_memory(exc))
        if too_long is not None:
            raise ScriptTooLong(*too_long)
        if failure is None:
            raise Cancelled()
        value_error, text, out_of_memory = failure
        if value_error:
            raise ValueError(text)                     # app.py keeps its 400/404 mapping
        if out_of_memory:
            gc.collect()
            _empty_cache(self._devices.get(name))
        raise GenerationError("out_of_memory" if out_of_memory else "other", text, during=during)

    # ---------- voice checks and records ----------

    def _check_new_voice(self, voice_id: str, reference: Path, backend: str, language: str) -> str:
        """Every check load_voice() makes before it may load a model; returns the language to store."""
        validate_voice_id(voice_id)
        self._require_enabled(backend)
        _check_reference(reference, backend)
        stored = _stored_language(backend, language)
        existing = self.registry.find_casefold(voice_id)
        if existing is not None:
            raise ValueError(f"{voice_id} and {existing} would share one file")
        return stored

    def _kept_reference(self, voice_id: str, backend: str, reference: Path, stored: str | None) -> str:
        """Keep a cache hit's known reference, warning when the requested clip differs."""
        if stored is None:
            return _stored_reference(self.config, self._root, reference)
        if not self._same_file(stored, reference):
            warnings.warn(
                f"'{voice_id}' already has cached conditioning on {backend}, baked from {stored}; "
                f"kept it and ignored {reference} (pass force=True to bake the new clip).",
                stacklevel=3,                          # load_voice()'s caller
            )
        return stored

    def _same_file(self, stored: str, reference: Path) -> bool:
        resolved = (self._root / stored).resolve()
        return os.path.normcase(str(resolved)) == os.path.normcase(str(reference))

    def _forget_active_voice(self, voice_id: str) -> None:
        """A resident backend that has voice_id active reloads from disk at its next take."""
        for backend in self._backends.values():
            if getattr(backend, "_active_voice", None) == voice_id:
                setattr(backend, "_active_voice", None)

    def _conds_path(self, backend: str, voice_id: str) -> Path:
        return self.config.CACHE_DIR / backend / f"{voice_id}.pt"

    def _details(self, voice_id: str, entry: dict) -> VoiceDetails:
        backend = str(entry.get("backend", ""))
        meta = dict(entry["meta"]) if isinstance(entry.get("meta"), dict) else {}
        stored = entry.get("reference_wav")
        stored = stored if isinstance(stored, str) else ""
        conds = self._conds_path(backend, voice_id)
        consent = meta.get("consent")
        return VoiceDetails(
            voice_id=voice_id,
            backend=backend,
            backend_enabled=backend in self._available,
            language=str(entry.get("language") or self.config.DEFAULT_LANGUAGE),
            exaggeration=_as_float(entry.get("exaggeration"), self.config.DEFAULT_EXAGGERATION),
            reference_wav=stored,
            reference_path=self._find_reference(stored),
            conds_path=conds,
            conds_exists=conds.is_file(),
            created_at=_optional_str(entry.get("created_at")),
            forged_at=_optional_str(entry.get("forged_at")),
            consent=None if consent is None else bool(consent),
            meta=meta,
        )

    def _find_reference(self, stored: str) -> Path | None:
        """The stored clip if it exists, else ROOT/<relative>, else VOICES_DIR/<name>; None = missing."""
        if not stored:
            return None
        path = Path(stored)
        candidates = (path if path.is_absolute() else self._root / path, self._voices_dir / path.name)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    # ---------- published state and events ----------

    def _publish(self, **changes: Any) -> None:
        """Replace (never mutate) the Residency that residency() hands out."""
        with self._state_mx:
            self._residency = replace(self._residency, **changes)

    def _publish_resident(self, **changes: Any) -> None:
        """_publish with the current resident tuple and device (call with the RLock held)."""
        resident = tuple(self._backends)
        device = self._devices.get(resident[-1]) if resident else None
        self._publish(resident=resident, device=device, **changes)

    def _emit(self, kind: _EventKind, backend: str, **fields: Any) -> None:
        """Deliver one EngineEvent; a failing handler is reported, never raised into the engine."""
        if self._on_event is None:
            return
        event = EngineEvent(kind=kind, backend=backend, at=time.time(), **fields)
        try:
            self._on_event(event)
        except Exception as exc:
            log.warning("Warning: the engine's on_event handler failed on '%s': %s", kind, _error_text(exc),
                        exc_info=True)


class _JobOutcome:
    """Gives a job its terminal phase when an engine call ends: done, cancelled or failed."""

    def __init__(self, job: Job | None) -> None:
        self._job = job

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: Any) -> bool:
        if self._job is not None:
            if exc_type is None:
                self._job._set_phase("done")
            elif issubclass(exc_type, Cancelled):
                self._job._set_phase("cancelled")
            else:
                self._job._set_phase("failed")
        return False


def _set_phase(job: Job | None, phase: str) -> None:
    if job is not None:
        job._set_phase(phase)


# ---------- exception text and classification ----------

def _message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return ""


def _error_text(exc: BaseException) -> str:
    """The first line of an exception's message (at most 200 characters), else its type name."""
    return first_line(_message(exc)) or type(exc).__name__


def _is_out_of_memory(exc: BaseException) -> bool:
    """torch.cuda.OutOfMemoryError (checked only when torch is loaded) or "out of memory" in the text."""
    oom_type = getattr(getattr(sys.modules.get("torch"), "cuda", None), "OutOfMemoryError", None)
    if isinstance(oom_type, type) and isinstance(exc, oom_type):
        return True
    return "out of memory" in _message(exc).lower()


def _load_failure_kind(exc: BaseException) -> LoadFailure:
    """BackendLoadError.kind for an exception raised while building a backend."""
    if _is_out_of_memory(exc):
        return "out_of_memory"
    # FileNotFoundError includes huggingface_hub's LocalEntryNotFoundError; OfflineModeIsEnabled
    # (a ConnectionError) is matched by name so this module never imports huggingface_hub.
    if isinstance(exc, FileNotFoundError) or any(
            cls.__name__ == "OfflineModeIsEnabled" for cls in type(exc).__mro__):
        return "missing_files"
    return "other"


# ---------- CUDA caching-allocator readings (BUILD_SPEC §3.0) ----------

def _cuda_for(device: str | None) -> Any | None:
    """torch.cuda when an allocator call is safe, else None.

    Safe means torch is already imported, the device is "cuda" and CUDA is already
    initialised. Anything else (CPU runs, fake factories) must not touch the allocator:
    reset_peak_memory_stats() alone would create a CUDA context.
    """
    if device != "cuda":
        return None
    cuda = getattr(sys.modules.get("torch"), "cuda", None)
    try:
        return cuda if cuda is not None and cuda.is_initialized() else None
    except Exception:
        return None


def _cuda_bytes(cuda: Any | None, reading: str) -> int | None:
    """One allocator reading for device 0 ("memory_reserved", ...), or None."""
    if cuda is None:
        return None
    try:
        return int(getattr(cuda, reading)(0))
    except Exception:
        return None


def _empty_cache(device: str | None) -> None:
    cuda = _cuda_for(device)
    if cuda is not None:
        with contextlib.suppress(Exception):
            cuda.empty_cache()


def _reset_peak(cuda: Any | None) -> None:
    """The only reset: max_memory_reserved() then covers this residency until the next load."""
    if cuda is not None:
        with contextlib.suppress(Exception):
            cuda.reset_peak_memory_stats(0)


# ---------- validation ----------

def _stored_reference(config: Any, root: Path, reference: Path) -> str:
    """A managed reference travels with a portable library, including when its original still exists."""
    if getattr(config, "PORTABLE", False) and reference.is_relative_to(root):
        return reference.relative_to(root).as_posix()
    return str(reference)


def _check_reference(reference: Path, backend: str) -> None:
    """Raise ValueError unless the clip exists, opens, and can be forged on `backend`."""
    from . import audio  # numpy + soundfile: imported only when a voice is forged

    if not reference.is_file():
        raise ValueError(f"Reference clip not found: {reference}")
    problem: str | None
    try:
        facts = audio.probe_clip(reference)
    except ValueError as exc:
        problem = str(exc)
    else:
        problem = audio.clip_problem(facts, backend)
    if problem is not None:
        raise ValueError(f"Reference clip {reference.name}: {problem}")


def _stored_language(backend: str, language: str) -> str:
    """The language load_voice() stores: a Multilingual code, or "en" on English-only backends."""
    info = BACKENDS.get(backend)
    if info is None:
        return language
    code = str(language).strip().lower()
    if info.english_only:
        if code != "en":
            warnings.warn(f"{backend} is English only; storing language 'en' instead of {language!r}.",
                          stacklevel=4)              # load_voice()'s caller
        return "en"
    if code not in info.languages:
        raise ValueError(_language_error(backend, language, info.languages))
    return code


def _edited_language(backend: str, info: BackendInfo | None, language: str) -> str:
    """update_voice(): a lowercased code the voice's backend speaks."""
    code = str(language).strip().lower()
    allowed = info.languages if info is not None else _ENGLISH_ONLY
    if code not in allowed:
        raise ValueError(_language_error(backend, language, allowed))
    return code


def _edited_exaggeration(backend: str, info: BackendInfo | None, value: Any) -> float:
    """update_voice(): a finite default Expressiveness in [0.0, 2.0], refused where it is ignored."""
    if info is not None and not info.uses_exaggeration:
        raise ValueError(f"{backend} ignores Expressiveness.")
    return _new_exaggeration(value)


def _new_exaggeration(value: Any) -> float:
    """load_voice() and update_voice(): a finite Expressiveness in [0.0, 2.0]; NaN, infinity or text is refused.

    The registry stores it for every backend (Turbo ignores it), and every take of the voice starts from it.
    """
    number = _as_float(value, math.nan)
    if not (math.isfinite(number) and 0.0 <= number <= _MAX_EXAGGERATION):
        raise ValueError(f"Expressiveness must be between 0.0 and {_MAX_EXAGGERATION:.1f} (got {value!r}).")
    return number


def _language_error(backend: str, language: Any, allowed: Mapping[str, str]) -> str:
    if set(allowed) == {"en"}:
        return f"{backend} is English only (got language {language!r})."
    return f"Unknown language {language!r} for {backend}; use one of: {', '.join(allowed)}."


# ---------- delete helpers ----------

def _casefold_twins(entries: Mapping[str, Any], voice_id: str) -> dict[str, str]:
    """backend -> another registered id equal to voice_id ignoring case (it shares the .pt on NTFS)."""
    folded = voice_id.casefold()
    twins: dict[str, str] = {}
    for other in sorted(entries):
        entry = entries[other]
        if other == voice_id or other.casefold() != folded or not isinstance(entry, dict):
            continue
        backend = entry.get("backend")
        if isinstance(backend, str):
            twins.setdefault(backend, other)
    return twins


def _kept_for_twin(path: Path, twin: str | None, failed: list[tuple[Path, str]]) -> bool:
    """True when a remaining voice owns this .pt; reports the kept file when it exists."""
    if twin is None:
        return False
    if path.exists():
        failed.append((path, f"kept: belongs to {twin}"))
    return True


def _discard_primary(path: Path, twin: str | None, discard: _Discard,
                     removed: list[Path], failed: list[tuple[Path, str]]) -> None:
    """The voice's own .pt. A discard failure propagates: nothing has changed yet."""
    if _kept_for_twin(path, twin, failed):
        return
    if path.exists():
        discard(path)
    removed.append(path)                              # an already-missing .pt counts as removed


def _discard_orphan(path: Path, twin: str | None, discard: _Discard,
                    removed: list[Path], failed: list[tuple[Path, str]]) -> None:
    """An orphan .pt left by an earlier cross-backend register: best effort, only if it exists."""
    if _kept_for_twin(path, twin, failed) or not path.exists():
        return
    try:
        discard(path)
    except Exception as exc:
        failed.append((path, _error_text(exc)))
    else:
        removed.append(path)


# ---------- small conversions ----------

def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_float(value: Any, default: float) -> float:
    """value as a finite float, else default: a missing, malformed, huge or non-finite registry value."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)
