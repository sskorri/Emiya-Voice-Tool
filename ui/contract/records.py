"""Plain immutable records shared by ui.core, the behaviours and the themes (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LoadEventKind = Literal["switching", "loading", "loaded", "unloaded", "load_failed", "closed", "forged"]
CheckTone = Literal["ok", "adv", "bad"]


@dataclass(frozen=True)
class VoiceRow:
    """One registered voice in a library snapshot (§4.15.4)."""

    voice_id: str
    backend: str
    backend_enabled: bool
    language: str                   # code as stored
    language_name: str              # "English" for Standard and Turbo
    exaggeration: float
    created: str | None             # forged_at, else created_at
    reference_name: str             # basename of the stored reference path
    reference_path: Path | None     # resolved clip; None = missing
    clip_s: float | None            # duration of the resolved clip
    pt: Path                        # CACHE_DIR / backend / "<voice_id>.pt"
    pt_exists: bool
    consent: bool


@dataclass(frozen=True)
class Take:
    """One take in outputs\\takes\\index.json (§4.15.1)."""

    id: int                         # take number from next_id, never reused
    voice: str
    backend: str                    # the voice's backend when the take was made
    language: str                   # the language_id sent; "en" for Standard and Turbo
    text: str                       # the exact script sent
    exaggeration: float | None      # None for Turbo
    cfg_weight: float | None        # None for Turbo
    seconds: float                  # frames / sample rate
    gen_seconds: float              # wall time of the speak call
    tokens: int | None              # speech tokens from the progress hook
    hit_token_limit: bool           # the end of the script was cut off
    created: str                    # local ISO-8601 with offset
    session: str                    # session_id of the run that made it
    starred: bool                   # star = keep
    wav: str                        # relative to the data root when inside it, else absolute
    sample_rate: int = 24000
    peaks: tuple[int, ...] = ()     # 230 buckets, round(100 * peak), one scale for all takes
    missing: bool = False           # runtime only (the WAV is gone); never saved


@dataclass(frozen=True)
class LoadEvent:
    """One line of cache\\load_history.jsonl (§4.15.3)."""

    t: str                          # local ISO-8601 with offset
    kind: LoadEventKind
    backend: str
    from_backend: str = ""          # switching only
    seconds: float | None = None    # loaded: load wall time; forged: bake seconds; unloaded with reason
                                    # "idle": the idle limit in seconds (shown as " · idle 30 min")
    gb: float | None = None         # loaded: reserved after; unloaded and closed: freed
    peak_gb: float | None = None    # unloaded and closed: peak while resident
    reason: str = ""                # unloaded: "switch" | "free" | "idle"; loading: "" | "warm-up"; load_failed: text
    device: str = "cuda"
    cold: bool = False              # loaded: the first load in this process


@dataclass(frozen=True)
class ClipInfo:
    """A probed and staged reference clip (§4.15.5)."""

    clip_id: str                    # uuid4 hex: the /media/staged key
    name: str                       # source file name shown in the UI
    source: Path | None             # where it was picked; None for an upload
    staged: Path                    # the file the forge uses: staging\<clip_id>.wav or the library file
    in_library: bool                # staged in place inside the voices folder
    duration_s: float               # the whole source
    selection_s: float              # == duration_s up to 15 s, else the auto-selection end
    samplerate: int
    channels: int
    fmt: str                        # "WAV" | "FLAC" | "OGG" | "MP3"
    subtype: str                    # for example "PCM_16"
    peak_dbfs: float
    clipped: bool
    silent: bool
    peaks: tuple[float, ...]        # window peaks 0..1 of the mono mix over the whole source


@dataclass(frozen=True)
class CheckRow:
    """One row of the Forge clip checks (Length, Turbo rule, Channels, Sample rate, Peak)."""

    label: str
    detail: str
    tone: CheckTone = "ok"          # ok: gold dot; adv: amber advisory; bad: red, blocks Turbo


@dataclass(frozen=True)
class ClipMark:
    """A Forge waveform mark ("Prompt 6 s", "Timbre 10 s") at pct of the clip's width."""

    label: str
    pct: float                      # 0..100
