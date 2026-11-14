"""The takes store: take WAVs plus outputs/takes/index.json (§4.15.1).

Each take is the exact WAV the engine returned, saved as <takes_dir>/<voice>_take<N>.wav
through <name>.part and os.replace. The index sits at a fixed place, <data root>/outputs/
takes/index.json, and is rewritten whole through a unique temp file, flush, fsync and
os.replace. The WAV is always written before the index, so a crash leaves at most an
unlisted WAV, never a listed take without audio. Take numbers come from next_id and are
never reused (D-T10).

The sync methods run in workers (run.io_bound). One threading.Lock serialises the
mutations; each one publishes a new immutable tuple (newest first), which the caller copies
into TakesState on the event loop. A failed index write leaves the published takes as they
were, so memory mirrors the index on disk. Stored paths are relative to the data root with
"/" when they are inside it, else absolute; relative paths passed in mean the same. An entry
whose file name is not its own <voice>_take<N>[-k].wav counts as missing: a damaged or
hand-edited index never makes a purge, an export or the media route touch another file.
"""
from __future__ import annotations

import contextlib
import json
import math
import numbers
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.audio import peaks, read_wav_bytes
from ui.contract.records import Take

__all__ = [
    "INDEX_VERSION",
    "PEAK_BUCKETS",
    "TRASH_RETRY_S",
    "TRASH_TRIES",
    "ExportResult",
    "NewTake",
    "PurgeResult",
    "TakeNotFoundError",
    "TakeNotListedError",
    "TakesLoadReport",
    "TakesStore",
    "pool_peaks",
    "resolve_stored",
    "stored_path",
    "take_file_name",
]

INDEX_VERSION = 1
PEAK_BUCKETS = 230               # the Speak take card's bars
TRASH_TRIES = 3                  # per file: a playing take can be locked for a moment
TRASH_RETRY_S = 0.15

_REPLACE_TRIES = 5
_REPLACE_DELAY_S = 0.05
_READ_TRIES = 5
_READ_RETRY_S = 0.05
_REASON_MAX = 200
_MAX_INT = 2 ** 53 - 1           # ids, counts and rates beyond this are corrupt (and unsafe in the browser)
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_TAKE_WAV = re.compile(r".*_take(\d{1,9})(?:-\d{1,9})?\.wav", re.IGNORECASE)
_REQUIRED = object()

_sleep = time.sleep              # the retry pauses; tests replace it


# ---- records -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class NewTake:
    """What the caller knows about a finished take; the store adds id, wav, seconds, peaks and created."""

    voice: str
    backend: str                    # the voice's backend when the take was made
    language: str                   # the language_id sent; "en" for Standard and Turbo
    text: str                       # the exact script sent
    exaggeration: float | None      # None for Turbo
    cfg_weight: float | None        # None for Turbo
    gen_seconds: float              # wall time of the speak call
    session: str                    # core.session_id of this run
    tokens: int | None = None       # speech tokens from the progress hook
    hit_token_limit: bool = False   # the end of the script was cut off
    created: str = ""               # local ISO-8601 with offset; "" = now


@dataclass(frozen=True)
class TakesLoadReport:
    """What load() found."""

    count: int                      # takes listed
    next_id: int
    moved_to: str = ""              # file name the corrupt index was moved (or copied) to; "" = none
    error: str = ""                 # why the index could not be read; "" = it was fine or absent
    read_only: bool = False         # the index could not be secured: the list is not rewritten this run
    cleaned: int = 0                # leftover .wav.part and index temp files deleted

    @property
    def notice(self) -> str:
        """The toast for the first client, "" when the index was fine or absent."""
        if self.moved_to:
            return f"The takes list couldn't be read · moved it to {self.moved_to} · the WAVs are untouched"
        if self.error:
            return f"The takes list couldn't be read · {self.error} · the WAVs are untouched"
        return ""


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of purge_unstarred (ids ascending)."""

    moved: tuple[int, ...] = ()                     # sent to the trash; entries removed
    missing: tuple[int, ...] = ()                   # the file was already gone; entries removed
    failed: tuple[tuple[int, str], ...] = ()        # (id, reason): the file and its entry stay


@dataclass(frozen=True)
class ExportResult:
    """Outcome of export (ids ascending)."""

    copied: tuple[Path, ...] = ()                   # the new files
    missing: tuple[int, ...] = ()                   # the WAV is gone, or the id is not listed
    failed: tuple[tuple[int, str], ...] = ()        # (id, reason)


class TakeNotFoundError(LookupError):
    """No take with this id is listed."""

    def __init__(self, take_id: int) -> None:
        super().__init__(take_id)
        self.take_id = take_id

    def __str__(self) -> str:
        return f"Take {self.take_id} is not in the takes list"


class TakeNotListedError(Exception):
    """The take's WAV was written, but the takes list could not be updated; str() is the toast."""

    def __init__(self, take_id: int, path: Path, reason: str) -> None:
        super().__init__(take_id, path, reason)
        self.take_id = take_id
        self.path = path
        self.reason = reason

    def __str__(self) -> str:
        return f"Take {self.take_id} is saved at {self.path} but couldn't be listed: {self.reason}"


@dataclass(frozen=True)
class _Snapshot:
    takes: tuple[Take, ...] = ()                    # newest first
    by_id: dict[int, Take] = field(default_factory=dict)
    next_id: int = 1
    rev: int = 0


@dataclass(frozen=True)
class _IndexRead:
    next_id: int = 1
    takes: tuple[Take, ...] = ()
    moved_to: str = ""
    error: str = ""
    read_only: bool = False


# ---- the store -----------------------------------------------------------------------------------

class TakesStore:
    """Takes on disk: add, stars, purge, export, Save As and the missing-file check."""

    def __init__(self, index_path: Path, root: Path) -> None:
        self._root = _absolute(root)
        self._index = _absolute(index_path)
        self._lock = threading.Lock()
        self._snap = _Snapshot()
        self._loaded = False
        self._read_only = ""            # why the index is never rewritten this run; "" = it is

    @property
    def takes(self) -> tuple[Take, ...]:
        """Every listed take, newest first (a new tuple after each change; never mutated)."""
        return self._snap.takes

    @property
    def rev(self) -> int:
        """Bumps on every published change."""
        return self._snap.rev

    @property
    def next_id(self) -> int:
        """The number the next take gets."""
        return self._snap.next_id

    def get(self, take_id: int) -> Take | None:
        """The listed take with this id, or None."""
        return self._snap.by_id.get(take_id)

    def path_of(self, take_id: int) -> Path | None:
        """The take's absolute WAV path (the file may be missing); None for an unknown id, and for an entry whose
        file name is not its own <voice>_take<N>.wav (a damaged or hand-edited index), which counts as missing."""
        take = self.get(take_id)
        return None if take is None else self._wav_of(take)

    # ---- startup ----

    def load(self, *, folders: Iterable[Path] = ()) -> TakesLoadReport:
        """Read the index (sync, startup). A corrupt one moves to index.json.corrupt-<unix>.

        Leftover <voice>_take*.wav.part and index temp files are deleted from the index folder,
        the folders of the listed takes and `folders` (pass the current Output Folder). next_id
        never drops below 1 + the highest <voice>_take<N>.wav number found there.
        """
        with self._lock:
            read = self._read_index()
            cleaned = highest = 0
            for folder in self._take_dirs(read.takes, folders):
                removed, top = _scan_folder(folder, self._index.name)
                cleaned += removed
                highest = max(highest, top)
            listed = max((take.id for take in read.takes), default=0)
            next_id = max(read.next_id, listed + 1, highest + 1)
            self._read_only = read.error if read.read_only else ""
            self._loaded = True
            self._publish(read.takes, next_id)
            return TakesLoadReport(count=len(read.takes), next_id=next_id, moved_to=read.moved_to,
                                   error=read.error, read_only=read.read_only, cleaned=cleaned)

    def check_files(self) -> tuple[int, ...]:
        """Ids whose WAV is missing (ascending); sets or clears each take's runtime `missing` flag."""
        gone = {take.id for take in self._snap.takes if _is_missing(self._wav_of(take))}
        with self._lock:
            current = self._snap.takes
            flagged = tuple(take if take.missing == (take.id in gone) else replace(take, missing=take.id in gone)
                            for take in current)
            if any(new is not old for new, old in zip(flagged, current)):
                self._publish(flagged, self._snap.next_id)
            return tuple(sorted(take.id for take in flagged if take.missing))

    # ---- mutations ----

    def add(self, wav: bytes, rec: NewTake, folder: Path) -> Take:
        """Save a finished take in folder: the WAV first, then the index. Returns the listed take.

        Raises ValueError for bytes that are not a readable WAV and for values the index cannot
        hold (nothing written), OSError when the WAV cannot be written (its number stays free),
        and TakeNotListedError when the WAV landed but the list could not be updated (the WAV
        stays and its number is never handed out again).
        """
        self._require_loaded()
        _check_new(rec)
        seconds, sample_rate, levels = _measure(wav)
        target_dir = resolve_stored(folder, self._root)
        with self._lock:
            take_id = self._snap.next_id
            target_dir.mkdir(parents=True, exist_ok=True)
            path = _write_wav(target_dir, f"{_file_stem(rec.voice)}_take{take_id}", wav)
            take = _new_take(take_id, rec, wav=stored_path(path, self._root), seconds=seconds,
                             sample_rate=sample_rate, levels=levels)
            listed = (take,) + self._snap.takes
            try:
                if self._read_only:
                    raise OSError(f"the takes list couldn't be read ({self._read_only})")
                self._write_index(take_id + 1, listed)
            except OSError as exc:
                self._publish(self._snap.takes, take_id + 1)
                raise TakeNotListedError(take_id, path, _reason(exc)) from None
            self._publish(listed, take_id + 1)
            return take

    def set_starred(self, take_id: int, starred: bool) -> Take:
        """Star (keep) or unstar a take, saved at once; on failure nothing changes in memory."""
        self._require_loaded()
        with self._lock:
            take = self._snap.by_id.get(take_id)
            if take is None:
                raise TakeNotFoundError(take_id)
            if take.starred == starred:
                return take
            self._require_writable()
            new = replace(take, starred=bool(starred))
            takes = tuple(new if old.id == take_id else old for old in self._snap.takes)
            self._write_index(self._snap.next_id, takes)
            self._publish(takes, self._snap.next_id)
            return new

    def purge_unstarred(self, ids: Collection[int], *, trash: Callable[[Path], None]) -> PurgeResult:
        """Send the WAVs of the listed, still unstarred takes among ids to trash, one file at a time.

        Each file gets up to TRASH_TRIES tries, TRASH_RETRY_S apart. Moved and already-missing
        takes leave the index in one rewrite; failed ones stay listed. Starred and unknown ids
        are ignored. If that rewrite fails, those takes stay listed, flagged missing, and
        OSError is raised.
        """
        self._require_loaded()
        wanted = set(ids)
        with self._lock:
            doomed = sorted((t for t in self._snap.takes if t.id in wanted and not t.starred),
                            key=lambda t: t.id)
            if doomed:
                self._require_writable()
            moved: list[int] = []
            missing: list[int] = []
            failed: list[tuple[int, str]] = []
            for take in doomed:
                outcome, reason = _trash_file(self._wav_of(take), trash)
                if outcome == "moved":
                    moved.append(take.id)
                elif outcome == "missing":
                    missing.append(take.id)
                else:
                    failed.append((take.id, reason))
            if moved or missing:
                self._drop(set(moved) | set(missing))
            return PurgeResult(tuple(moved), tuple(missing), tuple(failed))

    # ---- copies ----

    def export(self, ids: Collection[int], folder: Path) -> ExportResult:
        """Copy the takes' WAVs into folder as <voice>_take<N>.wav, never overwriting (" (2)", " (3)").

        Raises OSError when the folder cannot be created; per-take failures are in the result.
        """
        target = resolve_stored(folder, self._root)
        target.mkdir(parents=True, exist_ok=True)
        snap = self._snap
        copied: list[Path] = []
        missing: list[int] = []
        failed: list[tuple[int, str]] = []
        for take_id in sorted(set(ids)):
            take = snap.by_id.get(take_id)
            source = None if take is None else self._wav_of(take)
            if take is None or source is None or _is_missing(source):
                missing.append(take_id)
                continue
            try:
                copied.append(_copy_new(source, target, take_file_name(take)))
            except OSError as exc:
                failed.append((take_id, _reason(exc)))
        return ExportResult(tuple(copied), tuple(missing), tuple(failed))

    def copy_to(self, take_id: int, dest: Path) -> Path:
        """Save As: copy the take's WAV to dest, replacing it (the save dialog already asked).

        Raises TakeNotFoundError for an unknown id and FileNotFoundError when the WAV is missing.
        """
        take = self.get(take_id)
        if take is None:
            raise TakeNotFoundError(take_id)
        source = self._wav_of(take)
        if source is None or _is_missing(source):
            raise FileNotFoundError(f"Take {take_id}'s WAV is missing: {source or take.wav}")
        target = resolve_stored(dest, self._root)
        if target.exists() and os.path.samefile(source, target):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.part-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copy2(source, tmp)
            _replace(tmp, target)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return target

    # ---- internals ----

    def _wav_of(self, take: Take) -> Path | None:
        """The take's absolute WAV path, or None when the entry does not name its own <voice>_take<N>[-k].wav:
        a damaged or hand-edited index never makes a purge, an export or the media route touch another file."""
        path = resolve_stored(take.wav, self._root)
        match = _TAKE_WAV.fullmatch(path.name)
        return path if match is not None and int(match.group(1)) == take.id else None

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("TakesStore.load() must run before the takes list changes")

    def _require_writable(self) -> None:
        if self._read_only:
            raise OSError(f"the takes list couldn't be read ({self._read_only})")

    def _publish(self, takes: tuple[Take, ...], next_id: int) -> None:
        self._snap = _Snapshot(takes, {take.id: take for take in takes}, next_id, self._snap.rev + 1)

    def _write_index(self, next_id: int, takes: Sequence[Take]) -> None:
        _write_atomic(self._index, _index_text(next_id, takes))

    def _drop(self, gone: set[int]) -> None:
        """Rewrite the index without `gone`; on failure keep them listed, flagged missing, and raise."""
        snap = self._snap
        kept = tuple(take for take in snap.takes if take.id not in gone)
        try:
            self._write_index(snap.next_id, kept)
        except OSError as exc:
            flagged = tuple(replace(take, missing=True) if take.id in gone else take for take in snap.takes)
            self._publish(flagged, snap.next_id)
            raise OSError(f"the takes list couldn't be updated: {_reason(exc)}") from None
        self._publish(kept, snap.next_id)

    def _read_index(self) -> _IndexRead:
        try:
            raw = _read_bytes(self._index)
        except OSError as exc:
            return _IndexRead(error=_reason(exc), read_only=True)
        if raw is None:
            return _IndexRead()
        try:
            next_id, takes = _parse_index(raw)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:   # any corrupt content
            moved_to = _quarantine(self._index)
            return _IndexRead(moved_to=moved_to, error=_reason(exc), read_only=not moved_to)
        return _IndexRead(next_id=next_id, takes=takes)

    def _take_dirs(self, takes: Sequence[Take], folders: Iterable[Path]) -> list[Path]:
        candidates = [self._index.parent,
                      *(resolve_stored(take.wav, self._root).parent for take in takes),
                      *(resolve_stored(folder, self._root) for folder in folders)]
        unique: dict[str, Path] = {}
        for folder in candidates:
            unique.setdefault(os.path.normcase(str(folder)), folder)
        return list(unique.values())


# ---- public helpers ------------------------------------------------------------------------------

def take_file_name(take: Take) -> str:
    """<voice>_take<N>.wav: the Save As default and the export name (M L3032)."""
    return f"{_file_stem(take.voice)}_take{take.id}.wav"


def stored_path(path: str | os.PathLike[str], root: Path) -> str:
    """path relative to the data root with "/" when it is inside it, else absolute."""
    absolute = _absolute(path)
    try:
        return absolute.relative_to(_absolute(root)).as_posix()
    except ValueError:
        return str(absolute)


def resolve_stored(value: str | os.PathLike[str], root: Path) -> Path:
    """The absolute path of a stored or configured path; relative ones are relative to the data root."""
    path = Path(value)
    return _absolute(path if path.is_absolute() else Path(root) / path)


def pool_peaks(values: Sequence[float], n: int) -> tuple[float, ...]:
    """Max-pool stored peaks into n bars; bar b covers [floor(b*N/n), floor((b+1)*N/n)), empty bars are 0.

    For Compare's 110 bars from a take's 230 peaks: every peak counts, the tail included.
    """
    if n < 1:
        raise ValueError(f"need at least one bar, got {n}")
    size = len(values)
    return tuple(max(values[b * size // n:(b + 1) * size // n], default=0) for b in range(n))


# ---- files ---------------------------------------------------------------------------------------

def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(path))


def _file_stem(voice: str) -> str:
    """The voice id as a file name part (registered ids already are; anything else is made safe)."""
    return _UNSAFE_CHARS.sub("_", voice) or "take"


def _is_missing(path: Path | None) -> bool:
    """True when no regular file is at path (None: no usable path); a path that cannot be checked is not missing."""
    if path is None:
        return True
    try:
        return not stat.S_ISREG(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return True
    except OSError:
        return False


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried: another program's open handle can block it on Windows for a moment."""
    for _ in range(_REPLACE_TRIES - 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            _sleep(_REPLACE_DELAY_S)
    os.replace(src, dst)


def _write_atomic(path: Path, text: str) -> None:
    """Rewrite path whole: a unique temp file next to it, flush, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        _replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _free_wav(folder: Path, stem: str) -> Path:
    """<stem>.wav, or <stem>-2.wav, -3, ... when that name is taken."""
    n = 1
    while True:
        target = folder / (f"{stem}.wav" if n == 1 else f"{stem}-{n}.wav")
        if not os.path.lexists(target):
            return target
        n += 1


def _write_wav(folder: Path, stem: str, data: bytes) -> Path:
    """Write data as <stem>.wav (or the next free -N name) through <name>.part and os.replace."""
    target = _free_wav(folder, stem)
    part = target.with_name(target.name + ".part")
    try:
        with open(part, "wb") as sink:
            sink.write(data)
            sink.flush()
            os.fsync(sink.fileno())
        _replace(part, target)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    return target


def _copy_new(source: Path, folder: Path, name: str) -> Path:
    """Copy source into folder as name, or "<stem> (2)<suffix>", ...; never replaces a file."""
    stem, suffix = os.path.splitext(name)
    n = 1
    while True:
        target = folder / (name if n == 1 else f"{stem} ({n}){suffix}")
        try:
            sink = open(target, "xb")       # exclusive create: an existing file is never touched
        except FileExistsError:
            n += 1
            continue
        try:
            with sink, open(source, "rb") as src:
                shutil.copyfileobj(src, sink)
            shutil.copystat(source, target)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise
        return target


def _trash_file(path: Path | None, trash: Callable[[Path], None]) -> tuple[str, str]:
    """("moved" | "missing" | "failed", reason) for one take WAV (None: no usable path, never trashed)."""
    if path is None or _is_missing(path):
        return "missing", ""
    reason = ""
    for attempt in range(TRASH_TRIES):
        if attempt:
            _sleep(TRASH_RETRY_S)
            if _is_missing(path):
                return "moved", ""
        try:
            trash(path)
        except Exception as exc:        # an injected trash may raise anything; the other files go on
            reason = _reason(exc)
        else:
            return "moved", ""
    return "failed", reason


def _read_bytes(path: Path) -> bytes | None:
    """The file's bytes, None when it does not exist; an OSError is retried, then raised."""
    attempt = 1
    while True:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            if attempt >= _READ_TRIES:
                raise
            attempt += 1
            _sleep(_READ_RETRY_S)


def _free_backup(path: Path) -> Path:
    """<name>.corrupt-<unix>, or <name>.corrupt-<unix>-2, ... when that name is taken."""
    stamp = int(time.time())
    n = 1
    while True:
        backup = path.with_name(f"{path.name}.corrupt-{stamp}" + (f"-{n}" if n > 1 else ""))
        if not os.path.lexists(backup):
            return backup
        n += 1


def _quarantine(path: Path) -> str:
    """Move a corrupt index aside as <name>.corrupt-<unix> (copy it if it cannot move); "" on failure."""
    backup = _free_backup(path)
    try:
        os.replace(path, backup)
    except OSError:
        try:
            shutil.copy2(path, backup)
        except OSError:
            return ""
    return backup.name


def _scan_folder(folder: Path, index_name: str) -> tuple[int, int]:
    """(leftover files deleted, highest <voice>_take<N>.wav number) for one folder."""
    try:
        with os.scandir(folder) as found:
            entries = [entry for entry in found if entry.is_file(follow_symlinks=False)]
    except OSError:
        return 0, 0
    temp_prefix = f"{index_name}.tmp-".casefold()
    cleaned = highest = 0
    for entry in entries:
        name = entry.name.casefold()
        if (name.endswith(".wav.part") and "_take" in name) or name.startswith(temp_prefix):
            with contextlib.suppress(OSError):
                os.unlink(entry.path)
                cleaned += 1
            continue
        match = _TAKE_WAV.fullmatch(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return cleaned, highest


# ---- take records --------------------------------------------------------------------------------

def _measure(wav: bytes) -> tuple[float, int, tuple[int, ...]]:
    """(seconds, sample rate, PEAK_BUCKETS absolute peaks as round(100 * peak)) of the engine's WAV."""
    if not isinstance(wav, (bytes, bytearray)) or wav[:4] not in (b"RIFF", b"RF64") or wav[8:12] != b"WAVE":
        raise ValueError("the take's audio is not a WAV")
    try:
        samples, sample_rate = read_wav_bytes(bytes(wav))
    except Exception as exc:            # soundfile raises its RuntimeErrors, and others for garbage
        raise ValueError(f"the take's audio is not a readable WAV: {_reason(exc)}") from None
    levels = tuple(round(100 * value) for value in peaks(samples, PEAK_BUCKETS))
    return len(samples) / sample_rate, sample_rate, levels


def _check_new(rec: NewTake) -> None:
    """Refuse values the index could not hold, before anything is written."""
    for name in ("voice", "backend", "language", "text", "session", "created"):
        value = getattr(rec, name)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text, got {type(value).__name__}")
        if not _utf8(value):
            raise ValueError(f"{name} holds characters that cannot be saved")
    for name in ("exaggeration", "cfg_weight"):
        value = getattr(rec, name)
        if value is not None and not _finite(value):
            raise ValueError(f"{name} must be a finite number or None, got {value!r}")
    if not _finite(rec.gen_seconds) or rec.gen_seconds < 0:
        raise ValueError(f"gen_seconds must be a finite number of seconds, got {rec.gen_seconds!r}")
    if rec.tokens is not None and (isinstance(rec.tokens, bool) or not isinstance(rec.tokens, numbers.Integral)
                                   or rec.tokens < 0):
        raise ValueError(f"tokens must be a count or None, got {rec.tokens!r}")


def _finite(value: Any) -> bool:
    """A real, finite number (numpy scalars included); never a bool."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value)


def _utf8(text: str) -> bool:
    """False for text UTF-8 cannot encode (an unpaired surrogate), which the index could not hold."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _new_take(take_id: int, rec: NewTake, *, wav: str, seconds: float, sample_rate: int,
              levels: tuple[int, ...]) -> Take:
    return Take(
        id=take_id,
        voice=rec.voice,
        backend=rec.backend,
        language=rec.language,
        text=rec.text,
        exaggeration=None if rec.exaggeration is None else float(rec.exaggeration),
        cfg_weight=None if rec.cfg_weight is None else float(rec.cfg_weight),
        seconds=seconds,
        gen_seconds=float(rec.gen_seconds),
        tokens=None if rec.tokens is None else int(rec.tokens),
        hit_token_limit=bool(rec.hit_token_limit),
        created=rec.created or datetime.now().astimezone().isoformat(timespec="seconds"),
        session=rec.session,
        starred=False,
        wav=wav,
        sample_rate=sample_rate,
        peaks=levels,
    )


def _index_text(next_id: int, takes: Sequence[Take]) -> str:
    """The index JSON, one take per line (the 230 peaks make indented takes very long)."""
    rows = ",\n".join("    " + json.dumps(_take_json(take), ensure_ascii=False, allow_nan=False)
                      for take in takes)
    body = f"\n{rows}\n  " if rows else ""
    return f'{{\n  "version": {INDEX_VERSION},\n  "next_id": {next_id},\n  "takes": [{body}]\n}}\n'


def _take_json(take: Take) -> dict[str, Any]:
    data = asdict(take)
    del data["missing"]                 # runtime only
    return data


def _parse_index(raw: bytes) -> tuple[int, tuple[Take, ...]]:
    """(next_id, takes newest first); ValueError when the index is corrupt."""
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    version = data.get("version")
    if type(version) is not int or version != INDEX_VERSION:
        raise ValueError(f"unsupported index version {version!r}")
    entries = data.get("takes")
    if not isinstance(entries, list):
        raise ValueError("the takes entry is not a list")
    takes = tuple(_take_from_json(entry) for entry in entries)
    if len({take.id for take in takes}) != len(takes):
        raise ValueError("two takes share one id")
    next_id = _integer(data, "next_id", 1, minimum=1)
    return next_id, tuple(sorted(takes, key=lambda take: take.id, reverse=True))


def _take_from_json(entry: Any) -> Take:
    if not isinstance(entry, dict):
        raise ValueError(f"a take is {type(entry).__name__}, not an object")
    try:
        return Take(
            id=_integer(entry, "id", minimum=1),
            voice=_field(entry, "voice", (str,)),
            backend=_field(entry, "backend", (str,)),
            language=_field(entry, "language", (str,), "en"),
            text=_field(entry, "text", (str,)),
            exaggeration=_number(entry, "exaggeration", None),
            cfg_weight=_number(entry, "cfg_weight", None),
            seconds=_number(entry, "seconds", 0.0, minimum=0.0),
            gen_seconds=_number(entry, "gen_seconds", 0.0, minimum=0.0),
            tokens=_integer(entry, "tokens", None),
            hit_token_limit=_field(entry, "hit_token_limit", (bool,), False),
            created=_field(entry, "created", (str,), ""),
            session=_field(entry, "session", (str,), ""),
            starred=_field(entry, "starred", (bool,), False),
            wav=_wav_field(entry),
            sample_rate=_integer(entry, "sample_rate", 24000, minimum=1),
            peaks=_levels(entry),
        )
    except ValueError as exc:
        raise ValueError(f"take {entry.get('id')!r}: {exc}") from None


def _field(entry: dict[str, Any], key: str, kinds: tuple[type, ...], default: Any = _REQUIRED) -> Any:
    """entry[key] when it has one of kinds (a bool is never a number); absent or null gives default."""
    value = entry.get(key)
    if value is None:
        if default is _REQUIRED:
            raise ValueError(f"{key} is missing")
        return default
    if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
        raise ValueError(f"{key} is {type(value).__name__}")
    if isinstance(value, str) and not _utf8(value):
        raise ValueError(f"{key} holds an unpaired surrogate")
    return value


def _number(entry: dict[str, Any], key: str, default: Any = _REQUIRED, *,
            minimum: float | None = None) -> float | None:
    value = _field(entry, key, (int, float), default)
    if value is None:
        return None
    try:
        value = float(value)
    except OverflowError:               # a JSON integer beyond float range
        raise ValueError(f"{key} is too large") from None
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{key} is {value!r}")
    return value


def _integer(entry: dict[str, Any], key: str, default: Any = _REQUIRED, *, minimum: int = 0) -> int | None:
    value = _field(entry, key, (int,), default)
    if value is not None and not minimum <= value <= _MAX_INT:
        raise ValueError(f"{key} is {value!r}" if value < minimum else f"{key} is too large")
    return value


def _wav_field(entry: dict[str, Any]) -> str:
    value = _field(entry, "wav", (str,))
    if not value or "\x00" in value:
        raise ValueError("wav is not a path")
    return value


def _levels(entry: dict[str, Any]) -> tuple[int, ...]:
    """The stored peaks as whole numbers 0..100. The store writes exactly that, which is checked in one fast
    pass (an index of 3,000 takes holds 690,000 peaks, parsed while the app starts); anything else is clamped,
    and a value that is not a finite number (a bool included) makes the index corrupt."""
    values = _field(entry, "peaks", (list,), [])
    if not values:
        return ()
    kinds = set(map(type, values))
    if kinds == {int}:
        if 0 <= min(values) and max(values) <= 100:
            return tuple(values)
        return tuple(100 if value > 100 else 0 if value < 0 else value for value in values)
    if not kinds <= {int, float}:
        raise ValueError("peaks holds a value that is not a number")
    try:
        return tuple(min(100, max(0, round(value))) for value in values)
    except (ValueError, OverflowError):  # round() of NaN or of an infinity
        raise ValueError("peaks holds a value that is not a finite number") from None


def _reason(exc: BaseException) -> str:
    """A short, UI-ready reason: the OS or libsndfile message, else the first line of str(exc)."""
    text = getattr(exc, "error_string", None) or getattr(exc, "strerror", None) or str(exc)
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), "")
    return line[:_REASON_MAX] or type(exc).__name__
