"""Immutable records the engine hands to its callers (residency, loads, deletes, voices, events)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

__all__ = ["DeleteResult", "EngineEvent", "LoadReport", "Residency", "VoiceDetails"]


@dataclass(frozen=True)
class Residency:
    """What is in VRAM now; replaced (never mutated) by the engine, read lock-free."""

    resident: tuple[str, ...]      # LRU order, most recent last
    loading: str | None            # backend being built now
    unloading: str | None          # backend being freed now
    busy: str | None               # "load" | "bake" | "generate" | "unload" | None
    device: str | None             # where the resident model runs
    preferred_device: str
    max_resident: int              # MAX_RESIDENT_BACKENDS, read once
    last_used: float | None        # monotonic end of the last speak/load_voice/make_resident
    lock_held: bool                # this process holds the engine lock


@dataclass(frozen=True)
class LoadReport:
    """Outcome of make_resident()."""

    backend: str
    device: str
    seconds: float
    already_resident: bool
    evicted: tuple[str, ...]
    freed_bytes: int | None
    reserved_bytes: int | None     # after the load; None on CPU
    allocated_bytes: int | None    # after the load; None on CPU
    first_in_process: bool         # paid the torch/chatterbox import


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of delete_voice(): what went, and what could not be removed (path, reason)."""

    removed_entry: bool
    removed_files: tuple[Path, ...]
    failed_files: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class VoiceDetails:
    """One registry entry plus the file facts the Voices page needs."""

    voice_id: str
    backend: str
    backend_enabled: bool
    language: str
    exaggeration: float
    reference_wav: str             # as stored
    reference_path: Path | None    # resolved: absolute if it exists, else ROOT/relative, else VOICES_DIR/basename
    conds_path: Path               # CACHE_DIR / backend / f"{voice_id}.pt"
    conds_exists: bool
    created_at: str | None
    forged_at: str | None
    consent: bool | None           # meta.consent; None = never recorded (cli.py voices)
    meta: Mapping[str, Any]


@dataclass(frozen=True)
class EngineEvent:
    """A residency change, delivered to on_event on the engine thread."""

    kind: Literal["load_started", "loaded", "load_failed", "unloaded"]
    backend: str
    at: float                      # time.time()
    seconds: float | None = None
    device: str | None = None
    reserved_bytes: int | None = None     # loaded: after the load; unloaded: after _unload and empty_cache
    allocated_bytes: int | None = None    # unloaded: memory_allocated() after _unload
    freed_bytes: int | None = None        # unloaded
    peak_bytes: int | None = None         # unloaded: max_memory_reserved while it was resident
    reason: str | None = None             # unloaded: "evicted" | "requested" | "idle" | "close"
    error: str | None = None              # load_failed: f"{kind}: {detail}"
    first_in_process: bool | None = None  # loaded
