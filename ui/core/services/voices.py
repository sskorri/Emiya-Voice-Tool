"""The voice library: snapshot rows from the engine's voice details, corrupt-registry detection,
created dates as the Voices page shows them, and reveal in Explorer (§4.15.4).

snapshot() runs in a worker: it adds clip durations (engine.audio.clip_duration, cached by path,
mtime and size) and never touches a model. The engine's registry moves an unreadable registry.json
aside as registry.json.corrupt-<unix>; every snapshot reports the copies that are new since the
previous snapshot (or since the library was created), so the core can say so instead of showing an
empty library as if it were the first run.
"""
from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from engine.audio import clip_duration
from engine.backends.info import BACKENDS, LANGUAGES
from engine.types import VoiceDetails
from ui.contract.records import VoiceRow

__all__ = [
    "BACKEND_ORDER",
    "LibrarySnapshot",
    "VoiceLibrary",
    "corrupt_text",
    "created_text",
    "created_tip",
    "language_name",
]

BACKEND_ORDER: tuple[str, ...] = tuple(BACKENDS)     # Multilingual, Standard, Turbo
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NO_DATE = "—"


@dataclass(frozen=True)
class LibrarySnapshot:
    """One read of the library, for LibraryState."""

    rows: tuple[VoiceRow, ...]            # by backend (Multilingual, Standard, Turbo), then name ignoring case
    corrupt_files: tuple[str, ...]        # every registry.json.corrupt-* name next to the registry, sorted
    new_corrupt: tuple[str, ...]          # those that appeared since the previous snapshot: toast and log them


class VoiceLibrary:
    """Builds library snapshots for the registry at `registry` (cache\\registry.json)."""

    def __init__(self, registry: Path) -> None:
        self.registry = Path(registry)
        self._lock = threading.Lock()
        self._durations: dict[str, tuple[int, int, float | None]] = {}
        self._seen_corrupt = set(self.corrupt_files())

    def snapshot(self, details: Sequence[VoiceDetails]) -> LibrarySnapshot:
        """Rows for the engine's voice_details(), plus the corrupt-registry copies (worker)."""
        rows = tuple(sorted((self._row(d) for d in details), key=_sort_key))
        corrupt = self.corrupt_files()
        with self._lock:
            new = tuple(name for name in corrupt if name not in self._seen_corrupt)
            self._seen_corrupt.update(corrupt)
        return LibrarySnapshot(rows=rows, corrupt_files=corrupt, new_corrupt=new)

    def registry_mtime(self) -> int | None:
        """mtime_ns of registry.json, or None when it is missing; read it before voice_details()."""
        try:
            return self.registry.stat().st_mtime_ns
        except OSError:
            return None

    def corrupt_files(self) -> tuple[str, ...]:
        """The registry.json.corrupt-* names next to the registry (REG L61-77), sorted."""
        prefix = f"{self.registry.name}.corrupt-".casefold()
        try:
            with os.scandir(self.registry.parent) as entries:
                return tuple(sorted(e.name for e in entries if e.name.casefold().startswith(prefix) and e.is_file()))
        except OSError:
            return ()

    def reveal(self, path: Path) -> None:
        """Show path selected in Explorer, or open its folder when the file is gone.

        Raises FileNotFoundError when the folder is missing too. Explorer's exit code is not checked
        (it returns 1 even on success).
        """
        path = Path(path)
        if path.exists():
            _popen(f'"{_explorer()}" /select,"{path}"')
        elif path.parent.is_dir():
            _startfile(path.parent)
        else:
            raise FileNotFoundError(f"{path.parent} is missing")

    def _row(self, d: VoiceDetails) -> VoiceRow:
        return VoiceRow(
            voice_id=d.voice_id,
            backend=d.backend,
            backend_enabled=d.backend_enabled,
            language=d.language,
            language_name=language_name(d.backend, d.language),
            exaggeration=d.exaggeration,
            created=d.forged_at or d.created_at,
            reference_name=Path(d.reference_wav).name if d.reference_wav else "",
            reference_path=d.reference_path,
            clip_s=self._duration(d.reference_path),
            pt=d.conds_path,
            pt_exists=d.conds_exists,
            consent=d.consent is True,
        )

    def _duration(self, path: Path | None) -> float | None:
        """The clip's duration, read again only when its mtime or size changed."""
        if path is None:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = os.path.normcase(os.path.abspath(path))
        with self._lock:
            cached = self._durations.get(key)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        seconds = clip_duration(Path(path))
        with self._lock:
            self._durations[key] = (stat.st_mtime_ns, stat.st_size, seconds)
        return seconds


def _sort_key(row: VoiceRow) -> tuple[int, str, str, str]:
    known = row.backend in BACKEND_ORDER
    rank = BACKEND_ORDER.index(row.backend) if known else len(BACKEND_ORDER)
    return rank, "" if known else row.backend, row.voice_id.casefold(), row.voice_id


def _explorer() -> str:
    """explorer.exe by full path: a bare "explorer" would also be looked up in the working folder."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


def _popen(command: str) -> None:
    """Start a program from a full command line (explorer parses /select,"<path>" itself)."""
    subprocess.Popen(command)


def _startfile(folder: Path) -> None:
    """Open a folder in Explorer (Windows only)."""
    os.startfile(folder)


# ---------- display copy ----------

def language_name(backend: str, code: str) -> str:
    """"English" for the English-only backends, else the language's English name, else the raw code."""
    info = BACKENDS.get(backend)
    if info is not None and info.english_only:
        return "English"
    return LANGUAGES.get(code.lower(), code) if isinstance(code, str) else str(code)


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def created_text(iso: str | None, *, now: datetime | None = None) -> str:
    """The Created column: "Today", "Nov 8" this year, "2025" for older years, "—" when unknown."""
    at = _local(iso)
    if at is None:
        return NO_DATE
    today = (now or datetime.now()).astimezone()
    if at.date() == today.date():
        return "Today"
    if at.year == today.year:
        return f"{MONTHS[at.month - 1]} {at.day}"
    return str(at.year)


def created_tip(iso: str | None) -> str:
    """The full local date and time for the Created tooltip: "Nov 8, 2026 · 14:31"; "" when unknown."""
    at = _local(iso)
    if at is None:
        return ""
    return f"{MONTHS[at.month - 1]} {at.day}, {at.year} · {at.hour:02d}:{at.minute:02d}"


def corrupt_text(name: str) -> str:
    """Toast and log copy for a newly quarantined registry (NEW; S01 R6)."""
    return f"The voice registry was unreadable · moved it to {name} · voices are missing until it is restored"
