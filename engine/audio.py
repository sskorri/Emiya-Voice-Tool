"""Reference-clip facts, waveform peaks and WAV helpers (numpy and soundfile only).

Chatterbox decodes every reference clip with librosa at 24 kHz and mixes it to mono;
the first 6 s (15 s on Turbo) set the prompt and the first 10 s set the timbre, so
clips longer than 15 s are cut at the last pause before 15 s (selection_end). The
Turbo rule, "longer than 5 s", lives here so the Forge checks and the engine agree.
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .errors import first_line

__all__ = [
    "MAX_CLIP_BYTES",
    "SUPPORTED_EXTS",
    "TURBO_MIN_EXCLUSIVE_S",
    "ClipFacts",
    "clip_duration",
    "clip_problem",
    "peaks",
    "probe_clip",
    "read_wav_bytes",
    "selection_end",
    "trim_copy",
]

SUPPORTED_EXTS = (".wav", ".flac", ".ogg", ".mp3")
MAX_CLIP_BYTES = 500 * 2**20
TURBO_MIN_EXCLUSIVE_S = 5.0                              # TUB L70; CB/tts_turbo.py L245

SELECTION_MAX_S = 15.0       # a clip up to this long is used whole
SELECTION_MIN_S = 6.0        # a pause that ends the selection begins at or after this
PAUSE_MIN_S = 0.2            # a pause lasts at least this long ...
PAUSE_DB = -40.0             # ... with 20 ms RMS frames below this, relative to the clip peak
FRAME_S = 0.02
MIN_FORGE_S = 1.0
SILENT_PEAK = 0.001          # -60 dBFS
CLIP_LEVEL = 0.999
CLIP_RUN = 3                 # consecutive samples at CLIP_LEVEL that count as clipping

_TURBO = "chatterbox-turbo"
_BLOCK_FRAMES = 65536
_EPS = 1e-9
_FORMAT_NAMES = {"WAV": "WAV", "WAVEX": "WAV", "RF64": "WAV", "FLAC": "FLAC", "OGG": "OGG", "MP3": "MP3"}
_CODEC_SUBTYPES = frozenset({"MPEG_LAYER_I", "MPEG_LAYER_II", "MPEG_LAYER_III", "VORBIS", "OPUS"})
_REPLACE_TRIES = 5
_REPLACE_DELAY_S = 0.05


@dataclass(frozen=True)
class ClipFacts:
    """What the Forge checks and the engine's validation need to know about a clip."""

    path: Path
    fmt: str               # "WAV" | "FLAC" | "OGG" | "MP3"
    subtype: str           # e.g. "PCM_16"
    duration_s: float
    sample_rate: int
    channels: int
    peak: float            # max |x| over all channels (above 1.0 only for float files that clip)
    peak_dbfs: float       # -inf for an all-zero clip
    clipped: bool          # 3+ consecutive samples with |x| >= 0.999 in any channel
    silent: bool           # peak < 0.001 (-60 dBFS)
    selection_s: float     # == duration_s when <= 15.0 s, else selection_end()
    peaks: tuple[float, ...]   # n_bars window peaks of the mono mix, normalised to the loudest window


def probe_clip(path: Path, *, n_bars: int = 230) -> ClipFacts:
    """Read a clip in blocks (bounded memory) and return its facts.

    Raises ValueError with a UI-ready reason: "use WAV, FLAC, OGG or MP3",
    "Clip file is over 500 MB", "file not found", or soundfile's own message.
    """
    path = Path(path)
    _check_clip_file(path)
    try:
        with sf.SoundFile(str(path)) as source:
            scan = _scan(source, n_bars, total=source.frames)
            if scan.frames != source.frames:     # the header's length was off: bin the real length
                source.seek(0)
                scan = _scan(source, n_bars, total=scan.frames)
            sample_rate, channels = source.samplerate, source.channels
            fmt = _FORMAT_NAMES.get(source.format, source.format)
            subtype = source.subtype
    except RuntimeError as exc:                  # soundfile's errors are RuntimeErrors
        raise ValueError(_soundfile_reason(exc)) from None
    duration = scan.frames / sample_rate
    if duration <= SELECTION_MAX_S:
        selection = duration
    else:
        selection = _selection_end(scan.head, sample_rate, scan.mono_peak)
    top = float(scan.bars.max()) if scan.bars.size else 0.0
    bars = scan.bars / top if top > 0 else scan.bars
    return ClipFacts(
        path=path,
        fmt=fmt,
        subtype=subtype,
        duration_s=duration,
        sample_rate=sample_rate,
        channels=channels,
        peak=scan.peak,
        peak_dbfs=20.0 * math.log10(scan.peak) if scan.peak > 0 else -math.inf,
        clipped=scan.clipped,
        silent=scan.peak < SILENT_PEAK,
        selection_s=selection,
        peaks=tuple(float(v) for v in bars),
    )


def clip_duration(path: Path) -> float | None:
    """sf.info(path).duration; None when the file is missing or unreadable."""
    try:
        return float(sf.info(str(path)).duration)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def clip_problem(facts: ClipFacts, backend: str | None) -> str | None:
    """Why this clip cannot be forged (on `backend`, when given), or None."""
    if facts.silent:
        return "the clip is silent"
    if facts.duration_s < MIN_FORGE_S:
        return f"{facts.duration_s:.1f} s is too short to forge"
    if backend == _TURBO and facts.selection_s <= TURBO_MIN_EXCLUSIVE_S:
        return (f"{_TURBO} needs a reference clip longer than {TURBO_MIN_EXCLUSIVE_S:g}s "
                f"(got {facts.selection_s:.1f}s). Use a longer clip or another backend.")
    return None


def selection_end(mono: np.ndarray, sr: int) -> float:
    """Where a long clip's selection ends.

    The start of the last pause (>= 200 ms below -40 dB re peak, 20 ms RMS frames)
    that begins in [6.0, 15.0] s; else 15.0. The app gets the same value from
    probe_clip() (ClipFacts.selection_s); this is the rule on samples already in memory.
    """
    mono = _as_mono(mono)
    reference = float(np.max(np.abs(mono))) if mono.size else 0.0
    return _selection_end(mono, sr, reference)


def read_wav_bytes(wav: bytes) -> tuple[np.ndarray, int]:
    """Decode the engine's float32 WAV (the stdlib wave module cannot): (samples, sample rate)."""
    samples, sample_rate = sf.read(io.BytesIO(wav), dtype="float32")
    return samples, int(sample_rate)


def peaks(samples: np.ndarray, n_bins: int) -> list[float]:
    """Absolute max |x| per bin (over all channels of a (frames, channels) array), clipped to 0..1."""
    magnitude = _magnitude(samples)
    bins = _BarBins(n_bins, magnitude.size)
    bins.add(magnitude)
    return [float(v) for v in np.clip(bins.values, 0.0, 1.0)]


def trim_copy(src: Path, dst: Path, end_s: float) -> Path:
    """Write the first `end_s` seconds of `src` to `dst` as a real WAV, same rate and channels.

    Subtype: PCM_16 for PCM sources of 16 bits or less and for compressed sources,
    PCM_24 for 24-bit, PCM_32 for 32-bit, FLOAT for float and double. Writes
    dst + ".part", then os.replace; the source is never modified.
    """
    if not math.isfinite(end_s) or end_s <= 0:
        raise ValueError(f"end_s must be a positive number of seconds, got {end_s!r}")
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    try:
        with sf.SoundFile(str(src)) as source:
            out_subtype, dtype, clip = _trim_plan(source.subtype)
            with sf.SoundFile(str(part), "w", samplerate=source.samplerate, channels=source.channels,
                              subtype=out_subtype, format="WAV") as sink:
                _copy_frames(source, sink, round(end_s * source.samplerate), dtype, clip)
        _replace(part, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    return dst


# ---------- probing ----------

@dataclass
class _Scan:
    frames: int
    peak: float
    mono_peak: float
    clipped: bool
    bars: np.ndarray
    head: np.ndarray       # the mono mix of the first ~15.2 s, for selection_end


def _check_clip_file(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_EXTS:
        raise ValueError("use WAV, FLAC, OGG or MP3")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ValueError("file not found") from None
    except OSError as exc:
        raise ValueError(first_line(exc.strerror or str(exc)) or type(exc).__name__) from None
    if size > MAX_CLIP_BYTES:
        raise ValueError("Clip file is over 500 MB")


def _scan(source: sf.SoundFile, n_bars: int, total: int) -> _Scan:
    """One pass over the file: peaks, clipping, bar peaks and the selection head."""
    channels = source.channels
    bars = _BarBins(n_bars, total)
    head = _Head(_head_samples(source.samplerate))
    tail = np.zeros((CLIP_RUN - 1, channels), dtype=bool)
    peak = mono_peak = 0.0
    clipped = False
    frames = 0
    while True:
        block = source.read(_BLOCK_FRAMES, dtype="float32", always_2d=True)
        if not len(block):
            break
        magnitude = np.abs(block)
        peak = max(peak, float(magnitude.max()))
        if not clipped:
            clipped, tail = _clip_run(magnitude >= CLIP_LEVEL, tail)
        mono = block[:, 0] if channels == 1 else block.mean(axis=1)
        mono_magnitude = np.abs(mono)
        mono_peak = max(mono_peak, float(mono_magnitude.max()))
        bars.add(mono_magnitude)
        head.add(mono)
        frames += len(block)
    return _Scan(frames, peak, mono_peak, clipped, bars.values, head.samples())


def _clip_run(hot: np.ndarray, tail: np.ndarray) -> tuple[bool, np.ndarray]:
    """Any run of CLIP_RUN hot samples in a channel, counting the previous block's last ones."""
    joined = np.concatenate((tail, hot), axis=0)
    run = joined[: len(joined) - CLIP_RUN + 1].copy()
    for offset in range(1, CLIP_RUN):
        run &= joined[offset: len(joined) - CLIP_RUN + 1 + offset]
    return bool(run.any()), joined[len(joined) - (CLIP_RUN - 1):]


class _BarBins:
    """Max |x| per bin over a known number of samples, filled block by block.

    Bin b covers samples [floor(b*N/n), floor((b+1)*N/n)); samples past N fall in
    the last bin.
    """

    def __init__(self, n_bins: int, total: int) -> None:
        if n_bins < 1:
            raise ValueError(f"need at least one bin, got {n_bins}")
        self._total = max(int(total), 0)
        self.values = np.zeros(n_bins, dtype=np.float32)
        # Python ints: a bogus header length near 2**63 must not overflow while binning.
        self._edges = np.array([b * self._total // n_bins for b in range(n_bins + 1)], dtype=np.int64)
        self._pos = 0

    def add(self, magnitude: np.ndarray) -> None:
        start = self._pos
        self._pos += magnitude.size
        if magnitude.size == 0 or self._total == 0:
            return
        last_bin = self.values.size - 1
        first = min(int(np.searchsorted(self._edges, start, side="right")) - 1, last_bin)
        last = min(int(np.searchsorted(self._edges, self._pos - 1, side="right")) - 1, last_bin)
        offsets = np.maximum(self._edges[first:last + 1], start) - start
        maxima = np.maximum.reduceat(magnitude, offsets)
        window = self.values[first:last + 1]
        np.maximum(window, maxima, out=window)


class _Head:
    """The first `limit` samples of a stream, copied out of the blocks."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[np.ndarray] = []
        self._size = 0

    def add(self, samples: np.ndarray) -> None:
        if self._size < self._limit:
            part = np.array(samples[: self._limit - self._size], dtype=np.float32)
            self._parts.append(part)
            self._size += part.size

    def samples(self) -> np.ndarray:
        return np.concatenate(self._parts) if self._parts else np.zeros(0, dtype=np.float32)


def _head_samples(sr: int) -> int:
    """Enough samples to see a whole minimum pause that begins at SELECTION_MAX_S."""
    return math.ceil((SELECTION_MAX_S + PAUSE_MIN_S) * sr) + 2 * _frame_length(sr)


def _soundfile_reason(exc: RuntimeError) -> str:
    reason = getattr(exc, "error_string", None) or str(exc)
    return first_line(reason) or type(exc).__name__


# ---------- selection ----------

def _selection_end(mono: np.ndarray, sr: int, reference: float) -> float:
    frame = _frame_length(sr)
    frame_s = frame / sr
    quiet = _frame_rms(mono, frame) < reference * 10.0 ** (PAUSE_DB / 20.0)
    end = SELECTION_MAX_S
    for start, length in _true_runs(quiet):
        begins = start * frame_s
        if begins > SELECTION_MAX_S + _EPS:
            break
        if begins >= SELECTION_MIN_S - _EPS and length * frame_s >= PAUSE_MIN_S - _EPS:
            end = begins
    return end


def _frame_length(sr: int) -> int:
    return max(1, round(sr * FRAME_S))


def _frame_rms(mono: np.ndarray, frame: int) -> np.ndarray:
    """RMS of consecutive frames; a shorter last frame counts on its own samples."""
    whole = mono.size // frame
    framed = mono[: whole * frame].astype(np.float64).reshape(whole, frame)
    rms = np.sqrt(np.mean(framed * framed, axis=1))
    rest = mono[whole * frame:].astype(np.float64)
    if rest.size:
        rms = np.append(rms, math.sqrt(float(np.mean(rest * rest))))
    return rms


def _true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, length) of every run of True values."""
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    steps = np.diff(padded)
    starts = np.flatnonzero(steps == 1)
    ends = np.flatnonzero(steps == -1)
    return [(int(s), int(e - s)) for s, e in zip(starts, ends)]


def _as_mono(samples: np.ndarray) -> np.ndarray:
    array = np.asarray(samples, dtype=np.float32)
    if array.ndim > 1:
        array = array.reshape(array.shape[0], -1).mean(axis=1)
    return array.reshape(-1)


def _magnitude(samples: np.ndarray) -> np.ndarray:
    array = np.abs(np.asarray(samples, dtype=np.float32))
    if array.ndim > 1:
        array = array.reshape(array.shape[0], -1).max(axis=1)
    return array.reshape(-1)


# ---------- trimming ----------

def _trim_plan(subtype: str) -> tuple[str, str, bool]:
    """(output subtype, read dtype, clip to +-1) for a source subtype."""
    if subtype in ("FLOAT", "DOUBLE"):
        return "FLOAT", "float32", False
    if subtype in ("PCM_24", "PCM_32"):
        return subtype, "int32", False
    if subtype in _CODEC_SUBTYPES:
        return "PCM_16", "float32", True       # decoders can overshoot full scale
    return "PCM_16", "int16", False


def _copy_frames(source: sf.SoundFile, sink: sf.SoundFile, frames: int, dtype: str, clip: bool) -> None:
    while frames > 0:
        block = source.read(min(_BLOCK_FRAMES, frames), dtype=dtype, always_2d=True)
        if not len(block):
            break
        if clip:
            np.clip(block, -1.0, 1.0, out=block)
        sink.write(block)
        frames -= len(block)


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried: a reader's open handle can make it fail on Windows for a moment."""
    for _ in range(_REPLACE_TRIES - 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(_REPLACE_DELAY_S)
    os.replace(src, dst)
