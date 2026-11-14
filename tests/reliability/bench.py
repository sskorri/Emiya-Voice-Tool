"""Scale, stress and performance bench for the Emiya GUI (R3), over FakeEngine data roots grown to scale.

    venv\\Scripts\\python.exe tests\\reliability\\bench.py --out DIR            the full bench (about 8 minutes)
    venv\\Scripts\\python.exe tests\\reliability\\bench.py --toy --out DIR      a tiny dataset (seconds)
    options: --repeat N (default 5), --core-only, --select EXPR (pytest -k), --keep (keep the scratch roots)

The measurements are the bench-marked tests of tests\\pages\\test_rel_scale.py (`pytest -m bench` runs them on
their own and checks the budgets); this script runs them in a child pytest, collects the numbers they append to
VOICETOOL_BENCH_OUT and writes DIR\\bench.json and DIR\\bench.md (median and max of every metric against its
budget; DIR defaults to %TEMP%\\voicetool-bench). It exits 1 when a budget does not hold or a test fails.
- core part (test_bench_core, core_repeat() here): a real Core over FakeEngine on the manual clock, no client:
  core start and how long it blocks the event loop, the takes index, history and library loads, search, Takes
  tabs and filters, star and unstar, Remove Unstarred of every unstarred take through an injected trash, the
  controllers' refreshes, the progress tick while a take generates, and Python memory;
- page part (the user fixture): every page's first render and re-render, the UI paths of the same operations,
  and two curves (Speak against the session's size, Takes against "Show N more").

Data roots: grow() writes on top of a FakeEngine scenario's seed with the services' own formats (the voice
registry's writer, the takes store's index writer and peaks, the load history's lines): voices across the three
backends with real clips and .pt files, takes with real tiny WAVs and their peaks, load-history events, 64-
character names and 10,000-character scripts. Everything lives in scratch folders; nothing is written into the
install folder, and no window, browser or model is ever opened. Seeds are fixed, so two runs build the same data.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import gc
import io
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import wave
from array import array
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

APP_ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":                                 # a script run: the app's packages, offline
    sys.path.insert(0, str(APP_ROOT))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

if TYPE_CHECKING:
    from ui.contract.records import LoadEvent, Take
    from ui.core import Core
    from ui.core.clock import ManualScheduler
    from ui.core.fake import FakeEngine

__all__ = [
    "BUDGET_OP_S", "BUDGET_PAGE_S", "BUDGET_SEARCH_S", "OUT_ENV", "REPEAT_ENV", "SCALES", "SCALE_ENV",
    "TARGET_PAGE_S", "Grown", "LoopLag", "Metric", "NullKit", "RecordingTrash", "Recorder", "Scale", "core_repeat",
    "grow", "growing", "long_script", "main", "rss_mb",
]

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
BACKENDS = (MTL, STD, TUR)
SAMPLE_RATE = 24_000
NAME_MAX = 64                         # validate_voice_id's limit
LONG_SCRIPT = 10_000                  # characters
TAKE_POOL = 24                        # distinct tiny take WAVs (and their peaks) the takes cycle through
CLIP_POOL = 12

BUDGET_OP_S = 5.0                     # no operation takes over 5 s
BUDGET_SEARCH_S = 0.2                 # a search stays under 200 ms
BUDGET_PAGE_S = 2.0                   # a page builder stays well under NiceGUI's 3 s response_timeout
TARGET_PAGE_S = 1.0                   # the target median of a page builder

OUT_ENV = "VOICETOOL_BENCH_OUT"       # the JSON-lines file the page tests append their numbers to
SCALE_ENV = "VOICETOOL_BENCH_SCALE"   # "full" (default) or "toy"
REPEAT_ENV = "VOICETOOL_BENCH_REPEAT"
BENCH_TESTS = "tests/pages/test_rel_scale.py"   # the bench-marked tests (the user fixture needs tests\pages)
RUN_TIMEOUT_S = 1800

PHRASES = (
    "Hello there, how are you today?",
    "The forge had been cold for a hundred years.",
    "Okay, okay, I'll explain everything later.",
    "Call me back when you get this.",
    "Chapter two. The road ran north into the hills.",
    "Please hold the line, someone will be with you shortly.",
    "Thank you for listening, and good night.",
)
PHRASES_JA = ("おはよう！今日はいい天気ですね。", "こんにちは。テストです。", "散歩に行きませんか？")


# ---------- scale ----------

@dataclass(frozen=True)
class Scale:
    """How big a grown data root is. Counts are totals: the scenario's own voices, takes and events count."""

    name: str
    voices: int                       # registered voices, across the three backends
    takes: int                        # takes in the index, each with a tiny WAV
    history: int                      # load-history events in the file (the app keeps the last 500 at startup)
    session: int                      # takes of this run ("This Session")
    starred: int                      # starred takes (every other take is Remove Unstarred's work)
    long_name_every: int = 10         # every Nth new voice has a 64-character id
    long_script_every: int = 100      # every Nth new take holds a 10,000-character script
    take_s: float = 0.25              # seconds of each new take's WAV
    clip_s: float = 0.6               # seconds of each new voice's reference clip
    seed: int = 20261113


SCALES: Mapping[str, Scale] = {
    "full": Scale("full", voices=300, takes=3000, history=1000, session=1000, starred=500),
    "toy": Scale("toy", voices=12, takes=60, history=40, session=24, starred=12, long_name_every=4,
                 long_script_every=10),
}


@dataclass(frozen=True)
class Grown:
    """What grow() wrote (the counts are totals in the root, seed data included)."""

    root: Path
    voices: int
    takes: int
    history: int
    session: int
    starred: int
    unstarred: int
    voice_ids: tuple[str, ...]        # every registered voice, sorted
    long_voice: str                   # a 64-character id ("" when the scale has none)
    long_take: int                    # a take holding a 10,000-character script (0 when none)
    session_ids: tuple[int, ...]      # this run's take ids, newest first
    files: int                        # files written
    seconds: float                    # how long growing took


def grow(root: Path, scale: Scale, *, session_id: str | None) -> Grown:
    """Grow a FakeEngine-seeded data root to `scale` (call it right after the scenario's seed)."""
    from engine.registry import VoiceRegistry
    from ui.contract.records import Take
    from ui.core.services import load_history
    from ui.core.services import takes_store as store
    from ui.core.services.load_history import LoadHistory

    started = time.perf_counter()
    root = Path(root)
    rng = random.Random(scale.seed)
    now = datetime.now().astimezone()
    cache, clips_dir, takes_dir = root / "cache", root / "voices" / "reference", root / "outputs" / "takes"
    for folder in (cache, clips_dir, takes_dir, *(cache / backend for backend in BACKENDS)):
        folder.mkdir(parents=True, exist_ok=True)
    files = 0

    # voices: a clip, a .pt and a registry entry each (the registry's own writer)
    registry = VoiceRegistry(cache / "registry.json")
    entries = registry.entries()
    clips = [_wav(_speech(scale.clip_s, rng, 0.7)) for _ in range(CLIP_POOL)]
    languages = _language_codes()
    long_voice = ""
    for i in range(max(0, scale.voices - len(entries))):
        backend = BACKENDS[i % len(BACKENDS)]
        long = scale.long_name_every > 0 and i % scale.long_name_every == 0
        voice_id = _voice_id(i, long=long)
        long_voice = long_voice or (voice_id if long else "")
        language = languages[(i // len(BACKENDS)) % len(languages)] if backend == MTL else "en"
        clip = clips_dir / f"{voice_id}_ref.wav"
        clip.write_bytes(clips[i % len(clips)])
        pt = cache / backend / f"{voice_id}.pt"
        pt.write_text(json.dumps({"fake_conditionals": True, "voice_id": voice_id, "backend": backend,
                                  "baked_from": str(clip)}), encoding="utf-8")
        files += 2
        forged = (now - timedelta(days=1 + i % 240, minutes=7 * i)).astimezone(timezone.utc).isoformat()
        entries[voice_id] = {
            "backend": backend, "reference_wav": str(clip), "exaggeration": (0.3, 0.5, 0.5, 0.7, 1.0, 1.6)[i % 6],
            "language": language, "created_at": forged, "forged_at": forged,
            "meta": {"consent": True, "consent_at": forged, "clip_seconds": scale.clip_s, "clip_source": clip.name},
        }
    registry._write(entries)
    files += 1
    voices = sorted(entries.items())

    # takes: a tiny WAV each, measured as the takes store measures a take, then one index write
    seeded = store.TakesStore(takes_dir / "index.json", root)
    seeded.load()
    existing = list(seeded.takes)
    first_id = seeded.next_id
    new = max(0, scale.takes - len(existing))
    in_session = max(0, min(new, scale.session - sum(1 for t in existing if session_id and t.session == session_id)))
    to_star = max(0, min(new, scale.starred - sum(1 for t in existing if t.starred)))
    stars = {round(k * new / to_star) for k in range(to_star)} if to_star else set()
    pool = []
    for _ in range(TAKE_POOL):
        data = _wav(_speech(scale.take_s, rng, 0.7))
        seconds, sample_rate, levels = store._measure(data)
        pool.append((data, seconds, sample_rate, levels))
    run_start = now.replace(hour=14, minute=40, second=0, microsecond=0)
    takes: list[Take] = []
    long_take = 0
    for k in range(new):
        take_id = first_id + k
        voice_id, entry = voices[(k * 7) % len(voices)]
        backend = str(entry.get("backend", MTL))
        language = str(entry.get("language", "en")) if backend == MTL else "en"
        long = scale.long_script_every > 0 and k % scale.long_script_every == scale.long_script_every - 1
        long_take = long_take or (take_id if long else 0)
        session = k >= new - in_session
        if session:
            created = run_start + timedelta(seconds=9 * (k - (new - in_session)))
            stamp = session_id or created.isoformat(timespec="seconds")
        else:
            created = now - timedelta(days=2 + (new - k) * 88 // max(1, new), minutes=k % 600)
            stamp = created.replace(hour=9, minute=0, second=0).isoformat(timespec="seconds")
        data, seconds, sample_rate, levels = pool[k % len(pool)]
        name = f"{store._file_stem(voice_id)}_take{take_id}.wav"
        (takes_dir / name).write_bytes(data)
        files += 1
        turbo = backend == TUR
        takes.append(Take(
            id=take_id, voice=voice_id, backend=backend, language=language,
            text=_script(k, take_id, language, long=long), exaggeration=None if turbo else (0.5, 0.7, 0.3)[k % 3],
            cfg_weight=None if turbo else (0.5, 0.3, 0.0)[k % 3], seconds=seconds,
            gen_seconds=round(seconds * 1.5 + 0.4, 2), tokens=round(seconds * 25), hit_token_limit=long,
            created=created.isoformat(timespec="seconds"), session=stamp, starred=k in stars,
            wav=f"outputs/takes/{name}", sample_rate=sample_rate, peaks=levels))
    listed = sorted(existing + takes, key=lambda take: take.id, reverse=True)
    store._write_atomic(takes_dir / "index.json", store._index_text(first_id + new, listed))
    files += 1

    # load history: older events first, then the scenario's own (its estimates stay the newest)
    history_path = cache / "load_history.jsonl"
    seed_history = LoadHistory(history_path)
    seed_history.load()
    older = _history(max(0, scale.history - len(seed_history.events)), now, rng)
    history_path.write_text("".join(load_history._line(ev) for ev in (*older, *seed_history.events)),
                            encoding="utf-8")
    files += 1

    session_takes = tuple(t.id for t in listed if session_id and t.session == session_id)
    return Grown(root=root, voices=len(voices), takes=len(listed), history=len(older) + len(seed_history.events),
                 session=len(session_takes), starred=sum(1 for t in listed if t.starred),
                 unstarred=sum(1 for t in listed if not t.starred), voice_ids=tuple(v for v, _ in voices),
                 long_voice=long_voice, long_take=long_take, session_ids=session_takes, files=files,
                 seconds=time.perf_counter() - started)


@contextlib.contextmanager
def growing(scale: Scale, grown: list[Grown] | None = None) -> Iterator[list[Grown]]:
    """While active, every FakeEngine seeds its scenario and then grows the root to `scale`.

    The pytest fixture does the same with monkeypatch; scripts use this. Each grown root is appended to `grown`.
    """
    from ui.core import fake

    found: list[Grown] = grown if grown is not None else []
    original = fake._seed

    def seed(scenario: Any, config: Any) -> str | None:
        run_id = original(scenario, config)
        found.append(grow(Path(config.ROOT), scale, session_id=run_id))
        return run_id

    fake._seed = seed
    try:
        yield found
    finally:
        fake._seed = original


def _voice_id(i: int, *, long: bool) -> str:
    base = f"bench-{i:04d}"
    return (base + "-a-very-long-voice-name" * 4)[:NAME_MAX] if long else base


def _language_codes() -> tuple[str, ...]:
    from engine.backends.info import LANGUAGES

    return tuple(sorted(LANGUAGES))


def long_script(k: int = 0) -> str:
    """A 10,000-character English script (the phrases in turn from k)."""
    return " ".join(PHRASES[(k + n) % len(PHRASES)] for n in range(LONG_SCRIPT // 20))[:LONG_SCRIPT]


def _script(k: int, take_id: int, language: str, *, long: bool) -> str:
    if long:
        return long_script(k)
    if language == "ja":
        return PHRASES_JA[k % len(PHRASES_JA)]
    return f"{PHRASES[k % len(PHRASES)]} Take {take_id}."


def _history(n: int, now: datetime, rng: random.Random) -> list[LoadEvent]:
    """n residency events over the last three months, oldest first, ending before the scenario's own."""
    from ui.contract.records import LoadEvent

    events: list[LoadEvent] = []
    start = now - timedelta(days=95)
    step = timedelta(days=92) / max(1, n)
    resident = ""
    k = 0

    def at() -> str:
        return (start + step * len(events)).isoformat(timespec="seconds")

    while len(events) < n:
        target = BACKENDS[rng.randrange(len(BACKENDS))]
        gb = {MTL: 3.4, STD: 3.5, TUR: 3.1}[target]
        seconds = {MTL: 14.2, STD: 12.1, TUR: 8.7}[target] + rng.uniform(-1.5, 1.5)
        if k % 17 == 16:
            events.append(LoadEvent(t=at(), kind="load_failed", backend=target, reason="CUDA out of memory"))
        elif k % 11 == 10 and resident:
            events.append(LoadEvent(t=at(), kind="forged", backend=resident, seconds=round(rng.uniform(1.4, 2.6), 1)))
        elif not resident:
            events.append(LoadEvent(t=at(), kind="loading", backend=target))
            events.append(LoadEvent(t=at(), kind="loaded", backend=target, seconds=round(seconds, 1), gb=gb, cold=True))
            resident = target
        elif target == resident:
            events.append(LoadEvent(t=at(), kind="closed", backend=resident, gb=gb, peak_gb=gb + 0.2))
            resident = ""
        else:
            freed = {MTL: 3.4, STD: 3.5, TUR: 3.1}[resident]
            events.append(LoadEvent(t=at(), kind="switching", backend=target, from_backend=resident))
            events.append(LoadEvent(t=at(), kind="unloaded", backend=resident, gb=freed, peak_gb=freed + 0.2,
                                    reason="switch"))
            events.append(LoadEvent(t=at(), kind="loaded", backend=target, seconds=round(seconds, 1), gb=gb))
            resident = target
        k += 1
    return events[:n]


def _speech(seconds: float, rng: random.Random, peak: float) -> bytes:
    """Mono PCM_16 bursts of a voiced tone (fixed seed), the first at full level."""
    frames = round(seconds * SAMPLE_RATE)
    pitch = rng.uniform(110.0, 190.0)
    burst = max(1, round(SAMPLE_RATE * rng.uniform(0.08, 0.14)))
    samples = array("h")
    level = 1.0
    for i in range(frames):
        if i and i % burst == 0:
            level = rng.choice((0.55, 0.7, 0.85, 1.0))
        edge = min(1.0, (i % burst) / (burst / 5), (burst - i % burst) / (burst / 3))
        value = math.sin(2 * math.pi * pitch * i / SAMPLE_RATE) + 0.4 * math.sin(4 * math.pi * pitch * i / SAMPLE_RATE)
        samples.append(round(max(-1.0, min(1.0, value / 1.4 * edge * level * peak)) * 32767))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def _wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(SAMPLE_RATE)
        sink.writeframes(pcm)
    return buffer.getvalue()


# ---------- metrics ----------

@dataclass
class Metric:
    """One measured operation: its samples (seconds, or MB for memory) and its budget."""

    name: str
    group: str
    unit: str = "s"
    budget: float | None = None       # every sample must stay at or under it
    target: float | None = None       # the median should stay at or under it
    samples: list[float] = field(default_factory=list)
    note: str = ""
    bug: str = ""                     # the known bug (an xfail's reason) a broken budget belongs to

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else math.nan

    @property
    def max(self) -> float:
        return max(self.samples) if self.samples else math.nan

    @property
    def ok(self) -> bool:
        return self.budget is None or (bool(self.samples) and self.max <= self.budget)

    @property
    def on_target(self) -> bool:
        return self.target is None or (bool(self.samples) and self.median <= self.target)

    def summary(self) -> dict[str, Any]:
        return {"group": self.group, "unit": self.unit, "n": len(self.samples), "median": _round(self.median),
                "max": _round(self.max), "budget": self.budget, "target": self.target, "ok": self.ok,
                "on_target": self.on_target, "bug": self.bug, "samples": [_round(s) for s in self.samples],
                "note": self.note}


class Recorder:
    """Metrics by name, plus counts (rows, elements, files)."""

    def __init__(self) -> None:
        self.metrics: dict[str, Metric] = {}
        self.counts: dict[str, Any] = {}

    def add(self, name: str, value: float, *, group: str, unit: str = "s", budget: float | None = BUDGET_OP_S,
            target: float | None = None, note: str = "", bug: str = "") -> None:
        metric = self.metrics.get(name)
        if metric is None:
            metric = self.metrics[name] = Metric(name, group, unit, budget, target, note=note, bug=bug)
        metric.samples.append(float(value))

    @contextlib.contextmanager
    def timed(self, name: str, *, group: str, budget: float | None = BUDGET_OP_S,
              target: float | None = None) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started, group=group, budget=budget, target=target)

    def count(self, name: str, value: Any) -> None:
        self.counts[name] = value

    def merge_lines(self, lines: Sequence[str]) -> None:
        """The page tests' JSON lines: {"metric": ...} samples and {"count": ...} values."""
        for line in lines:
            if not line.strip():
                continue
            item = json.loads(line)
            if "metric" in item:
                self.add(item["metric"], item["value"], group=item.get("group", "pages"), unit=item.get("unit", "s"),
                         budget=item.get("budget"), target=item.get("target"), note=item.get("note", ""),
                         bug=item.get("bug", ""))
            elif "count" in item:
                self.count(item["count"], item["value"])

    @property
    def failed(self) -> list[Metric]:
        """Broken budgets no known bug explains."""
        return [m for m in self.metrics.values() if not m.ok and not m.bug]

    @property
    def known(self) -> list[Metric]:
        """Broken budgets of a known bug (an xfail test's numbers)."""
        return [m for m in self.metrics.values() if not m.ok and m.bug]


def _round(value: float) -> float | None:
    return None if value is None or math.isnan(value) else round(value, 5)


class LoopLag:
    """A freeze probe: the longest the event loop went without waking a 5 ms sleep (real time, bench only)."""

    def __init__(self) -> None:
        self.worst = 0.0
        self._stop = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> LoopLag:
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    async def stop(self) -> float:
        self._stop = True
        if self._task is not None:
            await self._task
        return self.worst

    async def _run(self) -> None:
        while not self._stop:
            began = time.perf_counter()
            await asyncio.sleep(0.005)
            self.worst = max(self.worst, time.perf_counter() - began - 0.005)


def rss_mb() -> float:
    """This process's working set in MB (Windows; 0.0 elsewhere)."""
    if os.name != "nt":
        return 0.0

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    kernel32.K32GetProcessMemoryInfo.restype = ctypes.c_int
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return 0.0
    return counters.WorkingSetSize / 2 ** 20


# ---------- the core part ----------

class NullKit:
    """A KitPort with no dialogs, Explorer or window: every pick is cancelled, every call recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        self.calls.append("pick_file")
        return None

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        self.calls.append("pick_folder")
        return None

    async def save_as(self, *, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        self.calls.append("save_as")
        return None

    def reveal(self, path: Path) -> None:
        self.calls.append("reveal")

    def open_folder(self, path: Path) -> None:
        self.calls.append("open_folder")

    def quit(self) -> None:
        self.calls.append("quit")

    def minimize(self) -> bool:
        self.calls.append("minimize")
        return False


class RecordingTrash:
    """The injected Recycle Bin: moves each file into a scratch folder and records it (never send2trash)."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.moved: list[Path] = []

    def __call__(self, path: Path) -> None:
        source = Path(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        os.replace(source, self.folder / f"{len(self.moved) + 1}-{source.name}")
        self.moved.append(source)


@dataclass
class _Rig:
    core: Core
    fake: FakeEngine
    clock: ManualScheduler
    trash: RecordingTrash
    grown: Grown


def _rig(work: Path, scale: Scale, scenario: str = "speak") -> _Rig:
    """A Core over a grown FakeEngine root on the manual clock, built (not started)."""
    from tests.theme_settings import EMIYA_TITLE, emiya_settings
    from ui.core import build_core
    from ui.core.clock import ManualScheduler
    from ui.core.fake import FakeEngine
    from ui.core.paths import Paths

    paths = Paths.for_run(work / "app", data_root=work / "data", fake=True)
    with growing(scale) as grown:
        fake = FakeEngine(scenario, data_dir=paths.root)
    trash = RecordingTrash(work / "trash")
    clock = ManualScheduler()
    core = build_core(engine=fake, paths=paths, settings=emiya_settings(paths.settings), trash=trash,
                      scheduler=clock, app_name=EMIYA_TITLE)
    return _Rig(core, fake, clock, trash, grown[0])


async def _start(rig: _Rig) -> None:
    """core.start() and its checks, the first client and its device reading, then the scenario's Speak draft."""
    from dataclasses import fields

    await rig.core.start()
    await rig.clock.settle()
    rig.core.client_connected()
    await rig.clock.settle()
    seed = rig.fake.scenario.session
    if seed is not None:
        for item in fields(seed):
            setattr(rig.core.session, item.name, getattr(seed, item.name))
    rig.core.events.notify()
    await _tick()


async def _tick(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def core_repeat(rec: Recorder, work: Path, scale: Scale, index: int) -> None:
    """One repeat of every core-level measurement, on a fresh grown root under work (the purge comes last).

    tests\\pages\\test_rel_scale.py::test_bench_core runs it once per repeat under the bench marker.
    """
    from ui.behaviours import BehaviourFactory
    from ui.core.services.load_history import LoadHistory
    from ui.core.services.takes_store import TakesStore
    from ui.core.services.voices import VoiceLibrary

    gc.collect()
    rec.add("memory.rss_before", rss_mb(), group="memory", unit="MB", budget=None)
    built = time.perf_counter()
    rig = _rig(work / f"repeat{index}", scale)
    rec.add("data.grow", time.perf_counter() - built, group="data", budget=None,
            note="FakeEngine seed plus grow(): not app code")
    core, clock = rig.core, rig.clock
    paths = core.paths

    # history and takes index loads on copies, before core.start() compacts the history to 500 events
    history_copy = paths.history.with_name("history-copy.jsonl")
    history_copy.write_bytes(paths.history.read_bytes())
    with rec.timed("core.history_load", group="core"):
        LoadHistory(history_copy).load()
    with rec.timed("core.takes_index_load", group="core"):
        TakesStore(paths.takes_index, paths.root).load(folders=[paths.takes_default])
    tracemalloc.start()
    store = TakesStore(paths.takes_index, paths.root)
    store.load(folders=[paths.takes_default])
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rec.add("memory.takes_index_retained", current / 2 ** 20, group="memory", unit="MB", budget=None)
    rec.add("memory.takes_index_peak", peak / 2 ** 20, group="memory", unit="MB", budget=None)
    del store

    probe = LoopLag().start()
    await asyncio.sleep(0.01)
    with rec.timed("core.start", group="core"):
        await _start(rig)
    rec.add("core.start_loop_block", await probe.stop(), group="core",
            note="the longest the event loop was blocked during core.start()")
    rec.add("memory.rss_after_start", rss_mb(), group="memory", unit="MB", budget=2048.0)
    if index == 0:
        rec.count("data.voices", rig.grown.voices)
        rec.count("data.takes", rig.grown.takes)
        rec.count("data.session_takes", rig.grown.session)
        rec.count("data.starred", rig.grown.starred)
        rec.count("data.history_events_written", rig.grown.history)
        rec.count("data.history_events_kept", len(core.state.history.events))
        rec.count("data.files", rig.grown.files)
        rec.count("data.index_bytes", paths.takes_index.stat().st_size)
        rec.count("data.registry_bytes", (paths.cache / "registry.json").stat().st_size)

    details = await core.facade.voice_details()
    with rec.timed("core.library_snapshot_cold", group="core"):
        VoiceLibrary(paths.cache / "registry.json").snapshot(details.value or [])
    with rec.timed("core.library_refresh", group="core"):
        await core.refresh_library()
    with rec.timed("core.check_files", group="core"):
        core.takes.check_files()

    factory = BehaviourFactory(core, None, NullKit(), browser=False)
    controllers = {name: getattr(factory, name)() for name in
                   ("status", "models", "playback", "search", "speak", "voices", "forge", "takes", "settings")}
    await _tick()
    for name, controller in controllers.items():
        refresh = getattr(controller, "refresh", None)
        if callable(refresh):
            with rec.timed(f"refresh.{name}", group="refresh", budget=1.0):
                refresh()

    search = controllers["search"]
    queries = {"voice": rig.grown.voice_ids[-1], "take_text": "hundred years", "no_match": "zzzz-no-such-thing",
               "one_letter": "a", "long_name": rig.grown.long_voice or "bench"}
    for label, query in queries.items():
        with rec.timed(f"search.{label}", group="search", budget=BUDGET_SEARCH_S):
            search.results(query)
    search.clear()

    takes = controllers["takes"]
    for tab in ("all", "starred", "session"):
        with rec.timed(f"takes.tab.{tab}", group="takes"):
            takes.set_tab(tab)
    takes.set_tab("all")
    with rec.timed("takes.query", group="takes"):
        takes.set_query("forge")
        clock.advance(0.15)
    takes.set_query("")
    clock.advance(0.15)
    with rec.timed("takes.voice_filter", group="takes"):
        takes.set_voice_filter(rig.grown.voice_ids[0])
    takes.set_voice_filter(None)
    with rec.timed("takes.model_filter", group="takes"):
        takes.set_model_filter(TUR)
    takes.set_model_filter(None)
    with rec.timed("takes.show_more", group="takes"):
        takes.show_more()
    rec.count("takes.rows_after_show_more", len(takes.vm.rows))

    playback = controllers["playback"]
    target = next(t for t in core.state.takes.takes if not t.starred)
    for action in ("star", "unstar"):
        before = core.state.takes.rev
        with rec.timed(f"takes.{action}", group="takes"):
            playback.star(target.id)
            await clock.settle()
        assert core.state.takes.rev != before, f"{action} did not publish"

    # the progress tick while a take generates (Speak's controller recomputes on every tick)
    speak = controllers["speak"]
    hold = rig.fake.hold("speak")
    await speak.generate(1)
    await asyncio.to_thread(hold.wait_entered, 10.0)
    for _ in range(5):
        with rec.timed("tick.generating", group="refresh", budget=0.2):
            clock.advance(0.2)
            await _tick()
    hold.release()
    await clock.settle()

    # Remove Unstarred: the first press arms with every unstarred take, the second sends them to the trash
    unstarred = sum(1 for t in core.state.takes.takes if not t.starred)
    with rec.timed("takes.purge_arm", group="takes"):
        armed = await takes.purge_unstarred()
    assert armed is not None, "Remove Unstarred did not arm"
    clock.advance(0.6)
    with rec.timed("takes.purge_all_unstarred", group="takes"):
        await takes.purge_unstarred()
    left = core.state.takes.takes
    assert all(t.starred for t in left) and len(rig.trash.moved) == unstarred, (len(left), len(rig.trash.moved))
    rec.count("takes.purged", unstarred)
    gc.collect()
    rec.add("memory.rss_after_repeat", rss_mb(), group="memory", unit="MB", budget=2048.0)

    factory.close()
    rig.fake.release_all()
    with contextlib.suppress(Exception):
        await clock.settle(timeout=10)
    core.stop()
    clock.cancel_pending()


# ---------- the run (a child pytest) ----------

def run_bench_tests(rec: Recorder, work: Path, scale: Scale, repeat: int, *, select: str = "") -> dict[str, Any]:
    """tests\\pages\\test_rel_scale.py -m bench in a child pytest (the user fixture needs pytest); its numbers are
    merged into rec. select is a -k expression ("" = every bench test)."""
    lines = work / "bench.jsonl"
    lines.write_text("", encoding="utf-8")
    command = [sys.executable, "-m", "pytest", BENCH_TESTS, "-m", "bench", "-p", "no:cacheprovider", "-q",
               f"--basetemp={work / 'pytest'}"]
    if select:
        command += ["-k", select]
    env = {**os.environ, "HF_HUB_OFFLINE": "1", OUT_ENV: str(lines), SCALE_ENV: scale.name, REPEAT_ENV: str(repeat),
           "NICEGUI_STORAGE_PATH": str(work / "nicegui"), "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.perf_counter()
    done = subprocess.run(command, cwd=APP_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=RUN_TIMEOUT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rec.merge_lines(lines.read_text(encoding="utf-8").splitlines())
    tail = [line for line in done.stdout.splitlines() if line.strip()]
    return {"exit_code": done.returncode, "summary": tail[-1] if tail else "", "seconds": time.perf_counter() - started,
            "stdout_tail": tail[-40:], "stderr_tail": done.stderr.splitlines()[-20:]}


# ---------- the report ----------

GROUP_ORDER = ("core", "search", "takes", "refresh", "pages", "ui", "stress", "memory", "data")


def write_report(out: Path, rec: Recorder, scale: Scale, repeat: int, run: dict[str, Any] | None,
                 seconds: float) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "version": 1,
        "scale": asdict(scale),
        "repeat": repeat,
        "when": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
        "machine": {"python": sys.version.split()[0], "platform": platform.platform(), "cpus": os.cpu_count(),
                    "processor": platform.processor(),
                    "note": "provisional: other agents and programs shared the CPU while this ran"},
        "budgets": {"page_builder_max_s": BUDGET_PAGE_S, "page_builder_target_median_s": TARGET_PAGE_S,
                    "search_max_s": BUDGET_SEARCH_S, "operation_max_s": BUDGET_OP_S},
        "ok": not rec.failed,
        "failed": [m.name for m in rec.failed],
        "known": {m.name: m.bug for m in rec.known},
        "off_target": [m.name for m in rec.metrics.values() if not m.on_target],
        "metrics": {name: m.summary() for name, m in sorted(rec.metrics.items(), key=_order)},
        "counts": dict(sorted(rec.counts.items())),
        "run": run,
    }
    (out / "bench.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "bench.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _order(item: tuple[str, Metric]) -> tuple[int, str]:
    name, metric = item
    return (GROUP_ORDER.index(metric.group) if metric.group in GROUP_ORDER else len(GROUP_ORDER), name)


def _markdown(report: dict[str, Any]) -> str:
    scale = report["scale"]
    lines = [
        "# Emiya GUI · scale bench (R3)",
        "",
        f"Scale **{scale['name']}**: {scale['voices']:,} voices across the three backends, {scale['takes']:,} takes "
        f"({scale['session']:,} this session, {scale['starred']:,} starred), {scale['history']:,} load-history "
        f"events written, 64-character names, 10,000-character scripts.",
        f"Run {report['when']} · {report['repeat']} repeats · {report['seconds']} s · Python "
        f"{report['machine']['python']} · {report['machine']['cpus']} logical CPUs · {report['machine']['note']}.",
        "",
        f"Budgets: page builder max {BUDGET_PAGE_S} s (NiceGUI's response_timeout is 3 s; target median "
        f"{TARGET_PAGE_S} s), search max {BUDGET_SEARCH_S * 1000:.0f} ms, any operation max {BUDGET_OP_S} s.",
        "",
        f"**Verdict: {_verdict(report)}**"
        + (f" · off target: {', '.join(report['off_target'])}" if report["off_target"] else ""),
        "",
        "| Group | Metric | n | Median | Max | Budget | Target | |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, m in report["metrics"].items():
        unit = m["unit"]
        verdict = "ok" if m["ok"] and m["on_target"] else ("OVER BUDGET" if not m["ok"] else "off target")
        if not m["ok"] and m["bug"]:
            verdict += f" ({m['bug']})"
        lines.append(f"| {m['group']} | {name} | {m['n']} | {_fmt(m['median'], unit)} | {_fmt(m['max'], unit)} | "
                     f"{_fmt(m['budget'], unit)} | {_fmt(m['target'], unit)} | {verdict} |")
    lines += ["", "## Counts", "", "| Count | Value |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in report["counts"].items()]
    run = report.get("run")
    if run:
        lines += ["", "## The run", "", f"Child pytest exit code {run['exit_code']}: `{run['summary']}` "
                  f"({run['seconds']:.0f} s)."]
    lines.append("")
    return "\n".join(lines)


def _verdict(report: dict[str, Any]) -> str:
    parts = ["all budgets hold" if report["ok"] else "budgets broken: " + ", ".join(report["failed"])]
    bugs: dict[str, list[str]] = {}
    for name, bug in report["known"].items():
        bugs.setdefault(bug, []).append(name)
    if bugs:
        broken = "; ".join(f"{bug}: {', '.join(names)}" for bug, names in sorted(bugs.items()))
        parts.append(f"known bugs break {broken}")
    return " · ".join(parts)


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "s":
        return f"{value * 1000:.1f} ms" if value < 1 else f"{value:.2f} s"
    return f"{value:.1f} {unit}"


# ---------- main ----------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "voicetool-bench")
    parser.add_argument("--toy", action="store_true", help="a tiny dataset (the smoke test uses it)")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--core-only", action="store_true", help="only the core part (test_bench_core)")
    parser.add_argument("--select", default="", help="a pytest -k expression for the bench tests")
    parser.add_argument("--keep", action="store_true", help="keep the scratch data roots")
    args = parser.parse_args(argv)
    scale = SCALES["toy" if args.toy else "full"]
    repeat = max(1, args.repeat)
    select = args.select or ("test_bench_core" if args.core_only else "")
    started = time.perf_counter()
    work = Path(tempfile.mkdtemp(prefix="vtb-"))          # short paths: 64-character names nest deep
    rec = Recorder()
    run: dict[str, Any] | None = None
    try:
        run = run_bench_tests(rec, work, scale, repeat, select=select)
    finally:
        report = write_report(args.out, rec, scale, repeat, run, time.perf_counter() - started)
        if not args.keep:
            resolved_work = work.resolve()
            if resolved_work.parent != Path(tempfile.gettempdir()).resolve() or not resolved_work.name.startswith("vtb-"):
                raise ValueError("benchmark cleanup must stay in its own temporary directory")
            shutil.rmtree(resolved_work, ignore_errors=True)
    passed = run is not None and run["exit_code"] == 0
    print(f"bench {scale.name}: {'ok' if report['ok'] and passed else 'FAILED'} ({run and run['summary']}) -> "
          f"{args.out / 'bench.json'}")
    return 0 if report["ok"] and passed else 1


if __name__ == "__main__":
    sys.exit(main())
