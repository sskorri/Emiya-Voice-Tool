"""Reference clips for Forge (§4.15.5): stage (probe, then use in place, byte-copy or trim into
cache\\staging), commit into the voices folder, roll back, discard, sweep, cached waveform bars and
the /media/staged lookup.

Every method does file I/O and runs in a worker. A source is never modified. Staging rules, first
match wins: (1) inside the voices folder and used whole: in place; (2) a WAV used whole: byte copy;
(3) otherwise trim_copy to a real WAV, so an MP3 or OGG from elsewhere always becomes PCM_16 WAV.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from engine.audio import MAX_CLIP_BYTES, SUPPORTED_EXTS, ClipFacts, probe_clip, trim_copy
from ui.contract.records import ClipInfo

__all__ = ["BARS_CACHE", "ClipService", "SWEEP_AGE_S"]

SWEEP_AGE_S = 86_400.0
BARS_CACHE = 64                           # cached bar sets (path, mtime, size, n)
MAX_REF_NAMES = 9999                      # <voice>_ref.wav, <voice>_ref-2.wav … <voice>_ref-9999.wav
TEST_LINE_PREFIX = "test-"
_CLIP_ID = re.compile(r"[0-9a-f]{32}")
_VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HASH_BLOCK = 1 << 20


class ClipService:
    """Clips staged for the Forge draft, in `staging`, committed into `voices` (VOICES_DIR)."""

    def __init__(self, voices: Path, staging: Path) -> None:
        self.voices = Path(os.path.abspath(voices))
        self.staging = Path(os.path.abspath(staging))
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {}         # clip_id -> the file /media/staged serves
        self._created: dict[str, Path] = {}       # clip_id -> the voices-folder file its commit created
        self._bars: OrderedDict[tuple[str, int, int, int], tuple[float, ...]] = OrderedDict()

    # ---------- staging ----------

    def stage_file(self, src: Path) -> ClipInfo:
        """Probe a picked or dropped clip and stage it (rules 1-3). ValueError with a UI-ready reason."""
        src = Path(os.path.abspath(src))
        facts = probe_clip(src)
        clip_id = uuid.uuid4().hex
        whole = facts.selection_s >= facts.duration_s
        if whole and _inside(src, self.voices):
            return self._register(_clip_info(clip_id, src.name, src, src, True, facts))
        staged = self._staged_file(clip_id)
        if whole and facts.fmt == "WAV":
            _copy_into(src, staged)
        else:
            trim_copy(src, staged, facts.selection_s)
        return self._register(_clip_info(clip_id, src.name, src, staged, False, facts))

    def stage_bytes(self, name: str, data: bytes) -> ClipInfo:
        """Stage an upload (--browser only) by rules 2-3; the clip has no source path."""
        name = Path(name).name
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_EXTS:
            raise ValueError("use WAV, FLAC, OGG or MP3")
        if len(data) > MAX_CLIP_BYTES:
            raise ValueError("Clip file is over 500 MB")
        clip_id = uuid.uuid4().hex
        staged = self._staged_file(clip_id)
        upload = self.staging / f"{clip_id}.upload{suffix}"
        try:
            upload.write_bytes(data)
            facts = probe_clip(upload)
            if facts.selection_s >= facts.duration_s and facts.fmt == "WAV":
                os.replace(upload, staged)
            else:
                trim_copy(upload, staged, facts.selection_s)
        finally:
            upload.unlink(missing_ok=True)
        return self._register(_clip_info(clip_id, name, None, staged, False, facts))

    # ---------- commit, roll back, discard ----------

    def commit(self, clip: ClipInfo, voice_id: str) -> tuple[Path, bool]:
        """The clip's file in the voices folder: (absolute path, created_now); never overwrites.

        In place: (path, False). Else the staged WAV moves to <voice>_ref.wav; an existing file with
        identical bytes (size and SHA-256) is reused, a different one moves the clip on to -2, -3, ...
        Committing the same clip again returns the file it went to.
        """
        if clip.in_library:
            return Path(os.path.abspath(clip.staged)), False
        if not _VOICE_ID.fullmatch(voice_id):
            raise ValueError(f"Invalid voice_id {voice_id!r}")
        staged = Path(clip.staged)
        if not staged.is_file():
            with self._lock:
                earlier = self._paths.get(clip.clip_id)
            if earlier is not None and earlier.is_file() and _inside(earlier, self.voices):
                return earlier, False
            raise FileNotFoundError(f"The staged copy of {clip.name} is gone · pick the clip again")
        self.voices.mkdir(parents=True, exist_ok=True)
        digest: tuple[int, str] | None = None
        for n in range(1, MAX_REF_NAMES + 1):
            target = self.voices / (f"{voice_id}_ref.wav" if n == 1 else f"{voice_id}_ref-{n}.wav")
            if target.exists():
                digest = digest or _digest(staged)
                if _same_bytes(target, digest):
                    with contextlib.suppress(OSError):
                        staged.unlink()
                    return self._remap(clip.clip_id, target), False
                continue
            try:
                _move_new(staged, target)
            except FileExistsError:        # created between the check and the move: try the next name
                continue
            with self._lock:
                self._created[clip.clip_id] = target
            return self._remap(clip.clip_id, target), True
        raise FileExistsError(f"{voice_id}_ref-{MAX_REF_NAMES}.wav: no free name in {self.voices}")

    def rollback(self, clip: ClipInfo, path: Path) -> None:
        """Undo the commit that created `path` (the forge failed): move it back to the clip's staging file.

        Only a file this clip's commit() created (created_now True) can be rolled back; anything else
        raises ValueError, so a reused or older clip in the voices folder is never touched.
        """
        if clip.in_library:
            return
        path = Path(os.path.abspath(path))
        with self._lock:
            created = self._created.get(clip.clip_id)
        if created is None or os.path.normcase(str(created)) != os.path.normcase(str(path)):
            raise ValueError(f"{path} was not created by committing {clip.name}")
        staged = Path(clip.staged)
        try:
            _move_new(path, staged)
        except FileExistsError:            # the staged copy is still there: just drop the committed one
            path.unlink(missing_ok=True)
        with self._lock:
            self._created.pop(clip.clip_id, None)
        self._remap(clip.clip_id, staged)

    def discard(self, clip: ClipInfo) -> None:
        """Forget a clip and delete its staging file; files in place are never touched."""
        with self._lock:
            self._paths.pop(clip.clip_id, None)
            self._created.pop(clip.clip_id, None)
        staged = Path(clip.staged)
        if clip.in_library or not _inside(staged, self.staging):
            return
        with contextlib.suppress(OSError):
            staged.unlink()

    def sweep(self, max_age_s: float = SWEEP_AGE_S) -> int:
        """Delete staging files older than max_age_s (startup: > 24 h); returns how many went."""
        cutoff = time.time() - max_age_s
        with self._lock:
            live = {os.path.normcase(str(path)) for path in self._paths.values()}
        removed = 0
        try:
            entries = list(os.scandir(self.staging))
        except OSError:
            return 0
        for entry in entries:
            try:
                if (entry.is_file() and os.path.normcase(entry.path) not in live
                        and entry.stat().st_mtime < cutoff):
                    os.unlink(entry.path)
                    removed += 1
            except OSError:                # locked or already gone: the next sweep tries again
                continue
        return removed

    # ---------- lookups ----------

    def bars(self, path: Path, n: int) -> tuple[float, ...]:
        """n window peaks 0..1 over the whole clip, cached by (path, mtime_ns, size, n); () if unreadable."""
        if n < 1:
            raise ValueError(f"need at least one bar, got {n}")
        path = Path(os.path.abspath(path))
        try:
            stat = path.stat()
        except OSError:
            return ()
        key = (os.path.normcase(str(path)), stat.st_mtime_ns, stat.st_size, n)
        with self._lock:
            cached = self._bars.get(key)
            if cached is not None:
                self._bars.move_to_end(key)
                return cached
        try:
            peaks = probe_clip(path, n_bars=n).peaks
        except (ValueError, OSError):
            return ()
        with self._lock:
            self._bars[key] = peaks
            while len(self._bars) > BARS_CACHE:
                self._bars.popitem(last=False)
        return peaks

    def staged_path(self, clip_id: str) -> Path | None:
        """The file /media/staged/<clip_id> may serve: a clip staged here, or test-<voice>; else None."""
        if _CLIP_ID.fullmatch(clip_id):
            with self._lock:
                path = self._paths.get(clip_id)
        elif clip_id.startswith(TEST_LINE_PREFIX) and _VOICE_ID.fullmatch(clip_id[len(TEST_LINE_PREFIX):]):
            path = self.test_line_path(clip_id[len(TEST_LINE_PREFIX):])
        else:
            return None
        return path if path is not None and path.is_file() else None

    def test_line_path(self, voice_id: str) -> Path:
        """Where a Forge test line is written: staging\\test-<voice>.wav (served as /media/staged/test-<voice>)."""
        if not _VOICE_ID.fullmatch(voice_id):
            raise ValueError(f"Invalid voice_id {voice_id!r}")
        return self.staging / f"{TEST_LINE_PREFIX}{voice_id}.wav"

    # ---------- helpers ----------

    def _staged_file(self, clip_id: str) -> Path:
        self.staging.mkdir(parents=True, exist_ok=True)
        return self.staging / f"{clip_id}.wav"

    def _register(self, info: ClipInfo) -> ClipInfo:
        self._remap(info.clip_id, info.staged)
        return info

    def _remap(self, clip_id: str, path: Path) -> Path:
        with self._lock:
            self._paths[clip_id] = path
        return path


def _clip_info(clip_id: str, name: str, source: Path | None, staged: Path, in_library: bool,
               facts: ClipFacts) -> ClipInfo:
    return ClipInfo(
        clip_id=clip_id,
        name=name,
        source=source,
        staged=staged,
        in_library=in_library,
        duration_s=facts.duration_s,
        selection_s=facts.selection_s,
        samplerate=facts.sample_rate,
        channels=facts.channels,
        fmt=facts.fmt,
        subtype=facts.subtype,
        peak_dbfs=facts.peak_dbfs,
        clipped=facts.clipped,
        silent=facts.silent,
        peaks=facts.peaks,
    )


def _inside(path: Path, folder: Path) -> bool:
    """True when path is folder or anything below it (symlinks resolved, case-insensitive on Windows)."""
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except OSError:
        return False


def _copy_into(src: Path, dst: Path) -> None:
    """Byte copy through dst.part, then os.replace: a reader never sees half a file."""
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def _move_new(src: Path, dst: Path) -> None:
    """Move src to dst, never replacing an existing dst (FileExistsError)."""
    try:
        os.link(src, dst)                  # create-if-absent, atomic on one volume
    except FileExistsError:
        raise
    except OSError:                        # another volume, or no hard links (FAT): copy into a new file
        _copy_new(src, dst)
    with contextlib.suppress(OSError):     # a leftover source is swept later; the move itself succeeded
        os.unlink(src)


def _copy_new(src: Path, dst: Path) -> None:
    created = False
    try:
        with open(src, "rb") as source, open(dst, "xb") as target:
            created = True
            shutil.copyfileobj(source, target, _HASH_BLOCK)
    except BaseException:
        if created:
            dst.unlink(missing_ok=True)
        raise


def _digest(path: Path) -> tuple[int, str]:
    """(size, SHA-256 hex) of a file."""
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
            sha.update(block)
    return path.stat().st_size, sha.hexdigest()


def _same_bytes(path: Path, digest: tuple[int, str]) -> bool:
    """True when path holds exactly the bytes `digest` describes; False when it can't be read."""
    try:
        if not path.is_file() or path.stat().st_size != digest[0]:
            return False
        return _digest(path) == digest
    except OSError:
        return False
