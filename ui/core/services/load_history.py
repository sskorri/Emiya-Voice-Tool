"""Load history: cache\\load_history.jsonl, load and bake estimates, and its lines (§4.15.3).

One JSON object per line, appended and flushed per event: a quit through os._exit leaves at most a
partial last line, which load() skips. load() keeps the last `keep` events and rewrites the file
atomically when it was longer or held unreadable lines. Only the app process writes the file.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from engine.backends.info import BACKENDS
from ui.contract.records import LoadEvent, LoadEventKind

from .settings import write_atomic

__all__ = [
    "KEEP",
    "LoadEstimate",
    "LoadHistory",
    "backend_label",
    "event_text",
    "history_rows",
    "now_iso",
]

log = logging.getLogger("voicetool.history")

KEEP = 500
ESTIMATE_WINDOW = 5                       # the median of the last 5 measurements
KINDS = frozenset(get_args(LoadEventKind))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_FLOATS = ("seconds", "gb", "peak_gb")
_TEXTS = ("from_backend", "reason", "device")
_RESIDENCY_ENDS = ("unloaded", "closed")


@dataclass(frozen=True)
class LoadEstimate:
    """What the history knows about loading one backend on one device."""

    about_s: float | None = None          # median of the last 5 successful loads (same cold class preferred)
    last_s: float | None = None           # the most recent successful load
    gb: float | None = None               # reserved after the most recent load that measured it
    peak_gb: float | None = None          # peak while resident, from the most recent unload or close
    count: int = 0                        # successful loads of this backend on this device


class LoadHistory:
    """The typed residency events, oldest first, and the estimates built from them."""

    def __init__(self, path: Path, keep: int = KEEP) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}")
        self.path = Path(path)
        self.keep = keep
        self._events: tuple[LoadEvent, ...] = ()
        self._warm = False                # a load ran in this process: the next one skips the imports
        self._lock = threading.Lock()

    @property
    def events(self) -> tuple[LoadEvent, ...]:
        return self._events

    def load(self) -> None:
        """Read the file (sync, at startup); skip unreadable lines, compact to the last `keep` events.

        Raises OSError only when the file cannot be read. A failed compaction (a full disk) is logged and
        leaves the file as it was: the events read stay loaded, and the next start tries again.
        """
        with self._lock:
            try:
                data = self.path.read_bytes()
            except FileNotFoundError:
                data = b""
            events, skipped = _parse_lines(data)
            kept = events[-self.keep:]
            self._events = tuple(kept)
            if skipped:
                log.warning("%s: skipped %d unreadable line(s)", self.path.name, skipped)
            if skipped or len(events) > self.keep:
                try:
                    write_atomic(self.path, "".join(_line(ev) for ev in kept))
                except OSError as exc:
                    log.warning("Couldn't compact %s: %s", self.path.name, exc.strerror or exc)

    def append(self, ev: LoadEvent) -> None:
        """Add one event (in a worker): one JSON line, flushed. ValueError when it would not read back."""
        ev = _finite(ev)
        line = _line(ev)
        if _event_from(json.loads(line)) != ev:
            raise ValueError(f"not a valid load event: {ev!r}")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab+") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":   # a partial line from a hard exit: start a new one
                        line = "\n" + line
                handle.seek(0, os.SEEK_END)
                handle.write(line.encode("utf-8"))
                handle.flush()
            self._events = (self._events + (ev,))[-self.keep:]
            if ev.kind in ("loaded", "load_failed"):
                self._warm = True

    def estimate(self, backend: str, device: str, *, cold: bool | None = None) -> LoadEstimate:
        """The load estimate for backend on device; `cold` None = the class of this process's next load.

        about_s is the median of the last 5 successful loads, taken from the loads of the same cold class
        (first load in a process, which pays the imports) when there are any; failed loads never count.
        """
        cold = (not self._warm) if cold is None else cold
        loaded = [ev for ev in self._events if ev.kind == "loaded" and ev.backend == backend and ev.device == device]
        timed = [ev.seconds for ev in loaded if ev.seconds is not None]
        same = [ev.seconds for ev in loaded if ev.seconds is not None and ev.cold == cold]
        pool = (same or timed)[-ESTIMATE_WINDOW:]
        ends = [ev for ev in self._events
                if ev.kind in _RESIDENCY_ENDS and ev.backend == backend and ev.device == device]
        return LoadEstimate(
            about_s=statistics.median(pool) if pool else None,
            last_s=timed[-1] if timed else None,
            gb=_last(ev.gb for ev in loaded),
            peak_gb=_last(ev.peak_gb for ev in ends),
            count=len(timed),
        )

    def bake_estimate(self, backend: str, device: str | None = None) -> float | None:
        """Median of the last 5 "forged" bake seconds of backend (on device, when given)."""
        bakes = [ev.seconds for ev in self._events
                 if ev.kind == "forged" and ev.backend == backend and ev.seconds is not None
                 and (device is None or ev.device == device)]
        return statistics.median(bakes[-ESTIMATE_WINDOW:]) if bakes else None

    def last_loaded(self) -> str | None:
        """The backend of the most recent successful load (Warm Up on Launch: Last Model)."""
        for ev in reversed(self._events):
            if ev.kind == "loaded":
                return ev.backend
        return None


# ---------- lines on disk ----------

def _line(ev: LoadEvent) -> str:
    return json.dumps(asdict(ev), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


def _finite(ev: LoadEvent) -> LoadEvent:
    """Non-finite measurements are stored as unknown (None)."""
    bad = {name: None for name in _FLOATS
           if isinstance(getattr(ev, name), float) and not math.isfinite(getattr(ev, name))}
    return replace(ev, **bad) if bad else ev


def _parse_lines(data: bytes) -> tuple[list[LoadEvent], int]:
    """The readable events and the number of unreadable non-empty lines."""
    events: list[LoadEvent] = []
    skipped = 0
    for raw in data.removeprefix(b"\xef\xbb\xbf").split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = _event_from(json.loads(raw.decode("utf-8")))
        except (ValueError, RecursionError):     # bad UTF-8, bad JSON, or nesting deeper than the parser goes
            ev = None
        if ev is None:
            skipped += 1
        else:
            events.append(ev)
    return events, skipped


def _event_from(obj: Any) -> LoadEvent | None:
    """A LoadEvent from one decoded line; None when a field is missing or has the wrong type."""
    if not isinstance(obj, dict):
        return None
    t, kind, backend = obj.get("t"), obj.get("kind"), obj.get("backend")
    if not (isinstance(t, str) and _parse_time(t) is not None and isinstance(kind, str) and kind in KINDS
            and isinstance(backend, str) and backend):
        return None
    values: dict[str, Any] = {}
    for name in _FLOATS:
        value = obj.get(name)
        if value is not None:
            number = _finite_number(value)
            if number is None:
                return None
            values[name] = number
    for name in _TEXTS:
        if name in obj:
            if not isinstance(obj[name], str):
                return None
            values[name] = obj[name]
    if "cold" in obj:
        if type(obj["cold"]) is not bool:
            return None
        values["cold"] = obj["cold"]
    return LoadEvent(t=t, kind=kind, backend=backend, **values)


def _finite_number(value: Any) -> float | None:
    """A JSON number as a finite float; None for a bool, any other type, NaN, an infinity or a huge integer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _last(values: Iterable[float | None]) -> float | None:
    found = None
    for value in values:
        if value is not None:
            found = value
    return found


# ---------- line copy (Models page and log) ----------

def now_iso() -> str:
    """Local ISO-8601 with offset, for LoadEvent.t: "2026-11-13T19:20:41+01:00"."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def backend_label(backend: str) -> str:
    """"Multilingual", "Standard", "Turbo" for the known backends; any other id unchanged."""
    return backend.removeprefix("chatterbox-").capitalize() if backend in BACKENDS else backend


def event_text(ev: LoadEvent, label: Callable[[str], str] = backend_label) -> str:
    """The line for one event, without the time; "" for forged events (never shown).

    For an idle unload, `seconds` holds the idle limit, shown as " · idle 30 min".
    """
    if ev.kind == "switching":
        return f"Switching {label(ev.from_backend)} → {label(ev.backend)}"
    if ev.kind == "loading":
        return f"Loading {ev.backend}" + (" · warm-up" if ev.reason == "warm-up" else "")
    if ev.kind == "loaded":
        timing = f" · {ev.seconds:.1f} s" if ev.seconds is not None else ""
        size = " · CPU" if ev.device == "cpu" else (f" · {ev.gb:.1f} GB" if ev.gb is not None else "")
        return f"Loaded {ev.backend}{timing}{size}"
    if ev.kind == "unloaded":
        idle = ""
        if ev.reason == "idle":
            idle = f" · idle {round(ev.seconds / 60)} min" if ev.seconds else " · idle"
        return f"Unloaded {ev.backend}{idle}" + (f" · freed {ev.gb:.1f} GB" if ev.gb is not None else "")
    if ev.kind == "load_failed":
        return f"Load failed: {ev.backend}" + (f" · {ev.reason}" if ev.reason else "")
    if ev.kind == "closed":
        return "Closed" + (f" · freed {ev.gb:.1f} GB" if ev.gb is not None else "")
    return ""


def history_rows(events: Iterable[LoadEvent], *, now: datetime | None = None,
                 label: Callable[[str], str] = backend_label) -> tuple[tuple[str, str], ...]:
    """Load History lines, oldest first: ("Today 19:20:41  Loaded …", tone) with tone old, today or failed.

    Older days read "Nov 11 19:20:41  …" (the year is added for another year); forged events are left out.
    """
    today = (now or datetime.now()).astimezone()
    rows = []
    for ev in events:
        at = _parse_time(ev.t)
        text = event_text(ev, label)
        if at is None or not text:
            continue
        at = at.astimezone()
        is_today = at.date() == today.date()
        tone = "failed" if ev.kind == "load_failed" else "today" if is_today else "old"
        rows.append((f"{_when(at, today, is_today)}  {text}", tone))
    return tuple(rows)


def _when(at: datetime, today: datetime, is_today: bool) -> str:
    clock = f"{at.hour:02d}:{at.minute:02d}:{at.second:02d}"
    if is_today:
        return f"Today {clock}"
    day = f"{MONTHS[at.month - 1]} {at.day}" + ("" if at.year == today.year else f" {at.year}")
    return f"{day} {clock}"
