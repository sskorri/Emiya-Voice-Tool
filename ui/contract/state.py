"""Bindable mirrors of the process-wide state.

ui.core writes them on the event loop only; themes and behaviours read them and bind_*_from them.
Bind to the mirror object itself (core.state.job, "kind"), never through a nested path
(core.state, ("job", "kind")): a nested path is not a bindable property and becomes a polled active link.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nicegui import binding

from ui.contract.records import LoadEvent, Take, VoiceRow


@binding.bindable_dataclass
class EngineState:
    """Residency as the core last saw it; never polled from the engine during a job."""

    resident: str = ""                      # backend in VRAM, "" = none
    failed: str = ""                        # backend whose last load failed, "" = none
    enabled: tuple[str, ...] = ()           # enabled backends in dock order
    max_resident: int = 1                   # residency().max_resident; the app refuses loads unless 1
    device: str = ""                        # where the resident model runs: "cuda" | "cpu"; "" = none
    rev: int = 0                            # bumps on every residency or failed-state change (voids tokens)


@binding.bindable_dataclass
class JobState:
    """The one job slot. kind "" = idle; anything else is busy."""

    kind: str = ""                          # "" | "switch" | "unload" | "generate" | "forge"
    phase: str = ""                         # switch: "unload" | "load"; generate: "preparing" | "sampling" |
                                            # "decoding"; forge: "bake"
    source: str = ""                        # switch or unload: the backend being freed
    target: str = ""                        # switch: the backend being loaded; generate: the voice's backend
    voice: str = ""                         # generate, test line, forge: the voice id
    take_no: int = 0                        # generate: the take number; 0 = a Forge test line
    queued: int = 0                         # takes waiting behind the running one (0..3)
    started_at: float = 0.0                 # scheduler time the job or chain step started
    elapsed_s: float = 0.0                  # scheduler.now() - started_at (0.2 s ticker)
    audio_s: float = 0.0                    # takes: sampled tokens / 25
    fill: float | None = None               # progress 0..0.95 (0.97 for a bake); None = indeterminate
    rev: int = 0                            # bumps when kind changes (voids tokens)


@binding.bindable_dataclass
class DeviceState:
    """The last device reading (DevicePoller)."""

    detected: bool = False                  # a reading has finished
    cuda: bool | None = None                # None = not checked yet; False = models run on the CPU
    name: str = ""                          # "NVIDIA GeForce RTX 4070 Ti"
    short_name: str = ""                    # "RTX 4070 Ti"
    total_bytes: int | None = None
    free_bytes: int | None = None           # device-wide
    app_bytes: int = 0                      # this process: memory_reserved
    other_bytes: int | None = None          # total - free - app, at least 0
    peak_bytes: int = 0                     # max_memory_reserved since the last load
    error: str = ""                         # why detection failed, "" = it did not
    measured_at: float = 0.0                # time.monotonic() of the reading


@binding.bindable_dataclass
class WindowState:
    """The native window, from the minimized and restored events."""

    visible: bool = True                    # False while minimized (the FX field pauses)


@binding.bindable_dataclass
class Session:
    """The draft on Speak plus the current take and pins: one per process, survives reloads, not restarts."""

    voice_id: str = ""
    text: str = ""
    language: str = "en"
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    take_id: int = 0                        # 0 = no current take
    pin_a: int = 0                          # 0 = not pinned
    pin_b: int = 0
    call_switch: bool = False               # opt in again on each launch in Speak switch mode


@binding.bindable_dataclass
class LibraryState:
    """The voice library snapshot."""

    rows: tuple[VoiceRow, ...] = ()         # sorted by backend, then name ignoring case
    status: str = "loading"                 # "loading" (no snapshot yet) | "ready" | "failed" | "corrupt"
    error: str = ""                         # failed: why the registry could not be read
    corrupt_files: tuple[str, ...] = ()     # registry.json.corrupt-* names found
    registry_mtime: int | None = None       # mtime_ns of registry.json when the snapshot was taken
    rev: int = 0                            # bumps on every snapshot change (voids tokens)


@binding.bindable_dataclass
class TakesState:
    """The takes store's published tuple."""

    takes: tuple[Take, ...] = ()            # newest first
    next_id: int = 1                        # the number the next take gets
    rev: int = 0                            # bumps on every store mutation


@binding.bindable_dataclass
class LogState:
    """The app log ring."""

    lines: tuple[str, ...] = ()             # "HH:MM:SS  text", oldest first, at most 60


@binding.bindable_dataclass
class HistoryState:
    """The load history."""

    events: tuple[LoadEvent, ...] = ()      # oldest first


@binding.bindable_dataclass
class AboutFacts:
    """The About card's facts, written by core.start() (the GPU line again after detection)."""

    engine: str = ""                        # "chatterbox-tts 0.1.7"
    gpu: str = ""                           # "RTX 4070 Ti · 12.0 GB · detected"; "" before detection
    models_in_vram: str = ""                # "One at a time"
    registry: str = ""                      # "cache/registry.json"
    cache: str = ""                         # "cache/‹model›/‹voice›.pt"
    clips: str = ""                         # "voices/reference"
    takes: str = ""                         # the Output Folder, relative to the data root when inside it


@dataclass(frozen=True)
class AppState:
    """All mirrors. Frozen: the mirror objects are never replaced, so bindings to them stay valid."""

    engine: EngineState = field(default_factory=EngineState)
    job: JobState = field(default_factory=JobState)
    device: DeviceState = field(default_factory=DeviceState)
    window: WindowState = field(default_factory=WindowState)
    library: LibraryState = field(default_factory=LibraryState)
    takes: TakesState = field(default_factory=TakesState)
    log: LogState = field(default_factory=LogState)
    history: HistoryState = field(default_factory=HistoryState)
    about: AboutFacts = field(default_factory=AboutFacts)
