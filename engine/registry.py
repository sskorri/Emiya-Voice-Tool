"""The voice registry: cache/registry.json, shared by the GUI, cli.py and app.py (VoiceRegistry).

Every read re-reads the file; every write is a locked read-modify-write through a unique temp file and
os.replace. A file whose content is corrupt is moved aside as registry.json.corrupt-<unix> (logged as a
warning on the "engine" logger) and the registry starts empty.
"""
import contextlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from filelock import FileLock
from .storage import ConditioningCommit

log = logging.getLogger("engine")


def _now_iso() -> str:
    """Current UTC time in the registry's date format (ISO 8601, "+00:00")."""
    return datetime.now(timezone.utc).isoformat()


def _without_none(meta: dict[str, Any]) -> dict[str, Any]:
    """meta minus the keys whose value is None (None means "delete this key")."""
    return {key: value for key, value in meta.items() if value is not None}


def _meta_of(entry: dict[str, Any]) -> dict[str, Any]:
    """A copy of the entry's meta dict ({} when it has none)."""
    meta = entry.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _same_clip(a: Any, b: Any) -> bool:
    """True when two stored reference_wav strings name the same file."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _casefold_twin(entries: dict, voice_id: str) -> str | None:
    folded = voice_id.casefold()
    return min((key for key in entries if key != voice_id and key.casefold() == folded), default=None)


def _registered_entry(
    prev: Any,
    *,
    backend: str,
    reference_wav: str,
    exaggeration: float,
    language: str,
    meta: dict[str, Any] | None,
    baked: bool,
) -> dict[str, Any]:
    """The entry register() writes, given the previous entry (or None)."""
    prev = prev if isinstance(prev, dict) else {}
    now = _now_iso()
    entry: dict[str, Any] = {
        "backend": backend,
        "reference_wav": reference_wav,
        "exaggeration": exaggeration,
        "language": language,
        "created_at": prev.get("created_at") or now,
    }
    forged_at = now if baked else prev.get("forged_at")
    if forged_at:
        entry["forged_at"] = forged_at
    # Consent travels with the clip: the previous meta survives only a re-register of the
    # same clip on the same backend.
    same_source = prev.get("backend") == backend and _same_clip(
        prev.get("reference_wav"), reference_wav
    )
    kept = _meta_of(prev) if same_source else {}
    merged = _without_none({**kept, **(meta or {})})
    if merged:
        entry["meta"] = merged
    return entry


def _updated_entry(
    entry: dict[str, Any],
    *,
    language: str | None,
    exaggeration: float | None,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """The entry update() writes: only language, exaggeration and meta keys change."""
    new = dict(entry)
    if language is not None:
        new["language"] = language
    if exaggeration is not None:
        new["exaggeration"] = exaggeration
    if meta is not None:
        merged = _without_none({**_meta_of(entry), **meta})
        if merged:
            new["meta"] = merged
        else:
            new.pop("meta", None)
    return new


class VoiceRegistry:
    """Reads and writes voice_id -> backend/config mappings in
    cache/registry.json. Single source of truth for which backend a cached
    voice belongs to.

    No in-memory cache: every read re-reads the file and every write is a
    locked read-modify-write, so concurrent processes (e.g. cli.py while the
    GUI or app.py is running) can't clobber each other's changes. Cross-process
    mutual exclusion is delegated to filelock; writes are atomic via a unique
    temp file plus os.replace.

    Entry keys: backend, reference_wav, exaggeration, language, created_at
    (first registered), forged_at (last bake; absent until a bake) and meta
    (consent, consent_at, clip_seconds, clip_source, ...; absent when empty).
    Readers must treat forged_at and meta as optional: entries written before
    they existed have neither.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        # filelock handles stale locks and cross-platform correctness. The
        # previous hand-rolled O_EXCL lock had a stale-break race where two
        # waiters could both acquire it and clobber each other's writes.
        self._lock = FileLock(str(self.path) + ".lock", timeout=10)
        self._conditioning = ConditioningCommit(self.path)
        self._recover_if_needed()

    def _recover_if_needed(self) -> None:
        # Lockless on the ordinary read path, including while a slow bake is preparing
        # its private file. Writers call this from inside the same reentrant file lock.
        if self._conditioning.marker.exists() or self._conditioning.preparing.exists():
            with self._lock:
                self._conditioning.recover()
                self._conditioning.preparing.unlink(missing_ok=True)

    @contextlib.contextmanager
    def voice_files_lock(self) -> Iterator[None]:
        """Serialize file-and-registry operations with other processes' brief publications."""
        with self._lock:
            self._recover_if_needed()
            yield

    @contextlib.contextmanager
    def cached_reference(self, voice_id: str, backend: str, path: Path, *, force: bool) -> Iterator[str | None]:
        """Keep a hit and its source together through reuse. A new bake runs outside the lock."""
        if not force:
            with self.voice_files_lock():
                entry = self.get(voice_id)
                if isinstance(entry, dict) and entry.get("backend") == backend and path.is_file():
                    reference = entry.get("reference_wav")
                    if isinstance(reference, str) and reference:
                        yield reference
                        return
        yield None

    # ---------- reading ----------

    def _read(self) -> dict:
        self._recover_if_needed()
        if not self.path.exists():
            return {}
        # Only genuine content corruption quarantines the file. A transient
        # OSError (on Windows, a lockless read can briefly collide with a
        # writer's atomic replace and raise PermissionError) is retried and
        # then surfaced -- never mistaken for corruption, so a valid registry
        # is never moved aside.
        last_err = None
        for _ in range(5):
            try:
                # utf-8-sig: an editor (or PowerShell 5.1's Set-Content) may save it with a byte order mark
                with self.path.open("r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (ValueError, RecursionError) as e:  # bad UTF-8 or JSON, or nesting deeper than the parser goes
                self._quarantine(e)
                return {}
            except OSError as e:
                last_err = e
                time.sleep(0.05)
                continue
            if not isinstance(data, dict):
                self._quarantine(
                    ValueError(f"expected a JSON object, got {type(data).__name__}")
                )
                return {}
            return data
        raise last_err  # all retries hit a transient OSError

    def _quarantine(self, error: Exception) -> None:
        # Content is genuinely malformed. Move it aside (don't destroy it) and
        # start fresh instead of crashing the engine.
        backup = self.path.with_suffix(f".json.corrupt-{int(time.time())}")
        try:
            self.path.replace(backup)
            log.warning("Warning: %s was malformed (%s); moved it to %s and starting with an empty registry.",
                        self.path, error, backup.name)
        except OSError:
            log.warning("Warning: %s was malformed (%s); starting with an empty registry "
                        "(could not back up the bad file).", self.path, error)

    def _write(self, data: dict, *, conditioning: tuple[str, str, Path] | None = None) -> None:
        if conditioning is not None:
            voice_id, backend, stage = conditioning
            self._conditioning.publish(voice_id, backend, stage, data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name so two writers can never collide on one temp file;
        # atomic os.replace onto the real path, retried because a concurrent
        # reader's open handle can make replace raise PermissionError on Windows.
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            for _ in range(5):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    time.sleep(0.05)
            os.replace(tmp, self.path)  # final attempt; let it raise if it still fails
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---------- public API ----------

    def register(
        self,
        voice_id: str,
        backend: str,
        reference_wav: str,
        exaggeration: float,
        language: str,
        *,
        meta: dict | None = None,
        baked: bool = True,
        staged: Path | None = None,
    ) -> None:
        """Create or replace voice_id's entry without losing its history.

        created_at keeps the first registration date; forged_at moves to now only when
        baked (a new .pt was written), else keeps its previous value. The previous meta is
        merged with `meta` only when the previous entry has the same backend and the same
        clip; otherwise `meta` alone is stored. A meta key whose value is None is dropped.
        """
        with self._lock:
            data = self._read()  # fresh, under the lock
            if staged is not None:
                twin = _casefold_twin(data, voice_id)
                if twin is not None:
                    raise ValueError(f"{voice_id} and {twin} would share one file")
            data[voice_id] = _registered_entry(
                data.get(voice_id),
                backend=backend,
                reference_wav=str(reference_wav),
                exaggeration=exaggeration,
                language=language,
                meta=meta,
                baked=baked,
            )
            if staged is None:
                self._write(data)
            else:
                self._write(data, conditioning=(voice_id, backend, staged))

    def update(
        self,
        voice_id: str,
        *,
        language: str | None = None,
        exaggeration: float | None = None,
        meta: dict | None = None,
    ) -> dict | None:
        """Change language, exaggeration and meta keys of a registered voice.

        A meta key whose new value is None is deleted. backend, reference_wav, created_at
        and forged_at never change here. Returns the new entry (a fresh dict, safe to keep),
        or None when voice_id is not registered.
        """
        with self._lock:
            data = self._read()  # fresh, under the lock
            entry = data.get(voice_id)
            if not isinstance(entry, dict):
                return None
            new = _updated_entry(
                entry, language=language, exaggeration=exaggeration, meta=meta
            )
            if new != entry:
                data[voice_id] = new
                self._write(data)
            return new

    def find_casefold(self, voice_id: str) -> str | None:
        """The registered id equal to voice_id ignoring case but spelled differently, else None.

        NTFS ignores case, so "Aiko" and "aiko" would share cache/<backend>/aiko.pt.
        Lockless read.
        """
        return _casefold_twin(self._read(), voice_id)

    def entries(self) -> dict[str, dict]:
        """Every entry from one lockless read (a fresh dict on each call)."""
        return self._read()

    def get(self, voice_id: str) -> dict | None:
        return self._read().get(voice_id)

    def backend_for(self, voice_id: str) -> str | None:
        entry = self._read().get(voice_id)
        return entry["backend"] if entry else None

    def list_ids(self) -> list[str]:
        return sorted(self._read().keys())

    def remove(self, voice_id: str) -> bool:
        with self._lock:
            data = self._read()
            if voice_id in data:
                del data[voice_id]
                self._write(data)
                return True
            return False
