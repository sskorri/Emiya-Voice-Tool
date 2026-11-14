"""FakeEngine: the engine's public surface over seeded scratch data, for --fake runs and the tests.

FakeEngine(scenario, data_dir=...) first writes one scenario's data under data_dir: the voice registry,
cached .pt files, reference clips, takes with their index and the load history (§6.13; the mockup data
of M L1189-1267). It then answers every public method of the real engine with the same signature, the
same events in the same order on the calling thread, the same residency and engine-lock rules, the same
validation and the same exception types, raised fresh with no __context__ (§3.10). Models are simulated:
a load, a take or a bake reports its mockup time (and sleeps that time scaled by `speed`; 0 = instant),
VRAM comes from a stand-in RTX 4070 Ti, and takes are speech-like PCM_16 WAVs built with the standard
library only (no numpy, soundfile or torch; validating a new voice's clip goes through the engine's own
check, which imports engine.audio when load_voice runs).

Test controls: hold(method) parks the next call at its work point until the hold is released (or the
call's job is cancelled); fail_next(method, error) makes the next call fail the way the engine would;
`calls` and `loads` record what ran. A Scenario also describes what its user did after launch (the
Speak draft, the Forge draft, the steps), which the composition root or the page harness replays
through the controllers, so every board comes from the real state that produces it.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import functools
import hashlib
import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import uuid
import warnings
import wave
import zlib
from array import array
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Literal, NoReturn, TypeVar

import config as _app_config
from engine import progress
from engine.backends.base import save_atomic, stage_conditioning, validate_voice_id
from engine.backends.info import BACKENDS, BackendInfo
from engine.device import DeviceInfo
from engine.engine import (_DEVICES, _EMPTY_TEXT, _JobOutcome, _as_float, _casefold_twins, _check_reference,
                           _discard_orphan, _discard_primary, _edited_exaggeration, _edited_language, _error_text,
                           _is_out_of_memory, _load_failure_kind, _new_exaggeration, _optional_str, _set_phase,
                           _stored_language, _stored_reference)
from engine.engine import log as _engine_log
from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError, ScriptTooLong
from engine.progress import Job
from engine.registry import VoiceRegistry
from engine.types import DeleteResult, EngineEvent, LoadReport, Residency, VoiceDetails
from ui.contract.records import LoadEvent, Take
from ui.contract.routes import Route

__all__ = [
    "GPU_NAME",
    "GPU_TOTAL_BYTES",
    "HOLD_TIMEOUT_S",
    "MODEL_SPECS",
    "PUBLIC_METHODS",
    "SCENARIOS",
    "SCRIPT_JA",
    "SCRIPT_TURBO",
    "SEED_MARKER",
    "SPEAK_LOG",
    "SPEAK_SESSION",
    "TURBO_SESSION",
    "Call",
    "FakeEngine",
    "ForgeSeed",
    "Hold",
    "ModelSpec",
    "Scenario",
    "SessionSeed",
    "Step",
    "scenario_named",
]

MTL = "chatterbox-multilingual"
STD = "chatterbox-standard"
TUR = "chatterbox-turbo"

GIB = 2 ** 30
GPU_NAME = "NVIDIA GeForce RTX 4070 Ti"      # the mockup's stand-in (M L1400-1407); the real app detects its GPU
GPU_TOTAL_BYTES = 12_878_086_144             # "12.0 GB" (§0.3)
TORCH_VERSION = "fake"
CUDA_VERSION = "12.4"
SAMPLE_RATE = 24_000                         # S3GEN_SR: every take, and the synthetic clips
TOKENS_PER_SECOND = progress.TOKENS_PER_SECOND
MIN_TOKENS = 10                              # the shortest fake take: 0.4 s
TURBO_FLOW_STEPS = 2                         # Turbo's decoding loop (n_cfm_timesteps=2)
CONDS_LOAD_S = 0.03                          # "Voice marco · conditioning cached · 0.03 s" (M L1245)
UNLOAD_S = 0.4                               # an unload's nominal time (D10's short "Freeing" phase)
FAILED_LOAD_SHARE = 0.7                      # a failed load fails this far into its usual time
PEAK_EXTRA_BYTES = round(0.2 * GIB)          # a take's working memory on top of the weights
ALLOCATED_SHARE = 0.93                       # memory_allocated as a share of memory_reserved
HOLD_TIMEOUT_S = 30.0                        # a hold nobody releases lets its call go on after this
HOLD_POLL_S = 0.02
SEED_MARKER = ".fake_seed.json"              # marks a data root this module seeded (and may wipe and reseed)
AUDIO_CACHE_DIR = "voicetool-fake-audio"     # seeded audio by SHA-256, hard-linked into each data root
_TOO_MANY_LINKS = 1142                       # ERROR_TOO_MANY_LINKS: an NTFS file has at most 1024 names

TAKE_PEAK = 0.7                              # speech-like level: peaks spread over 0..70 of the 0..100 scale
CLIP_PEAK = 0.7
SAMPLE_PEAK = 10 ** (-8.42 / 20)             # the real sample.wav peaks at -8.42 dBFS (§3.5)
SHORT_HELLO_PEAK = 10 ** (-6.4 / 20)         # the mockup's short_hello.wav check row (M L1264)

_APP_ROOT = Path(__file__).resolve().parents[2]
_REAL_SAMPLE = _APP_ROOT / "voices" / "reference" / "sample.wav"    # read only, never written
_SEEDED_FOLDERS = ("cache", "voices", "outputs")
_FOREIGN_DATA = ("cache/registry.json", "outputs/takes/index.json", "cache/load_history.jsonl")
_T = TypeVar("_T")


# ---------- simulated models ----------

@dataclass(frozen=True)
class ModelSpec:
    """Nominal numbers for one simulated model (M L1193-1195, M L1252-1259; weights §3.6)."""

    load_s: float                # load seconds reported and (times speed) slept
    vram_bytes: int              # memory_reserved after the load
    made_per_audio_s: float      # take wall time per second of audio
    bake_s: float                # bake seconds
    weights_bytes: int           # weights_status() total


MODEL_SPECS: Mapping[str, ModelSpec] = MappingProxyType({
    MTL: ModelSpec(14.2, round(3.4 * GIB), 1.5, 2.1, 3_206_854_222),
    STD: ModelSpec(12.1, round(3.5 * GIB), 1.44, 2.3, 3_191_834_148),
    TUR: ModelSpec(8.7, round(3.1 * GIB), 0.57, 1.6, 2_986_050_872),
})


# ---------- scenarios ----------

StepKind = Literal["ask", "switch", "free", "generate", "forge"]


@dataclass(frozen=True)
class SessionSeed:
    """The Speak draft a scenario starts with: the fields of ui.contract.state.Session."""

    voice_id: str = ""
    text: str = ""
    language: str = "en"
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    take_id: int = 0
    pin_a: int = 0
    pin_b: int = 0
    call_switch: bool = False


@dataclass(frozen=True)
class ForgeSeed:
    """The Forge draft of the §6.13 seed table: stage voices/reference/<clip>, then set these fields."""

    clip: str                    # a file in the data root's voices/reference
    name: str
    backend: str
    language: str
    consent: bool
    exaggeration: float = 0.5


@dataclass(frozen=True)
class Step:
    """One thing the scenario's user did after launch, replayed through the controllers in order.

    ask: a dock click on `backend` with Confirm Model Switches on (the popover opens). switch: a
    confirmed switch to `backend` (asked=True when it came through an ask, so a low-VRAM plan redeems).
    free: Free VRAM. generate: Generate (count 1) or Generate 3 Takes (count 3) with the Speak draft.
    forge: Forge with the Forge draft.
    """

    kind: StepKind
    backend: str = ""
    count: int = 1
    asked: bool = False


SCRIPT_JA = "おはよう！今日はいい天気ですね。散歩に行きませんか？"                                     # M L1190
SCRIPT_TURBO = "Okay, okay [chuckle] I'll explain everything later. [sigh] It's a long story."   # M L1214
RUN_STARTED = "13:40:12"           # today's launch (M L1242): this run's takes carry its stamp
EARLIER_RUN_STARTED = "19:18:30"   # yesterday's run, before its first load (M L1253)

SPEAK_SESSION = SessionSeed(voice_id="aiko", text=SCRIPT_JA, language="ja", exaggeration=0.5, cfg_weight=0.5,
                            take_id=7, pin_a=5, pin_b=7)                                         # M L1272-1276
TURBO_SESSION = replace(SPEAK_SESSION, voice_id="host-turbo", text=SCRIPT_TURBO, language="en")  # M L1287-1290

SPEAK_LOG = (                                                                                     # M L1241-1251
    "13:40:12  Emiya Voice started · no model loaded (models load on demand)",
    "13:57:02  Loading chatterbox-multilingual",
    "13:57:16  Loaded chatterbox-multilingual · 14.2 s · 3.4 GB",
    "13:57:16  Voice marco · conditioning cached · 0.03 s",
    "13:58:09  Take 4 · 3.2 s audio in 4.1 s",
    "14:02:12  Voice aiko · conditioning cached · 0.03 s",
    "14:29:40  Take 5 · 5.2 s audio in 7.0 s",
    "14:30:52  Take 6 · 4.9 s audio in 7.2 s",
    "14:31:58  Take 7 · 4.6 s audio in 6.9 s",
)
CPU_LOG = tuple(line.replace("14.2 s · 3.4 GB", "14.2 s · CPU") for line in SPEAK_LOG)


@dataclass(frozen=True)
class Scenario:
    """One board or test state: the data to seed, the engine's state, and what the user did after launch.

    The defaults are the "Speak · steady state" board. `session`, `player_at`, `forge`, `takes_tab`,
    `settings_tab`, `log` and `steps` are for the composition root and the page harness: copy `session`
    into core.session after the launch selection (None: keep what it selected), show `log` instead of the
    launch line (empty: write the launch line as usual), then replay `steps`.
    """

    name: str
    board: str = ""                                  # the mockup board it reproduces ("" = a test state)
    route: Route = "/"                               # the page the scenario opens on
    data: Literal["base", "firstrun"] = "base"       # firstrun: today's PC (empty registry, sample.wav only)
    this_session: bool = True                        # takes 4-7 belong to this run (FakeEngine.session_id)
    forge_files: bool = False                        # real sample.wav, short_hello.wav, a 1.9 s forged event
    missing: tuple[str, ...] = ()                    # data-root-relative files removed after seeding
    unconfirmed: tuple[str, ...] = ()                # voices registered without meta.consent (cli.py style)
    resident: str = MTL                              # the model in VRAM at launch ("" = none)
    cuda: bool = True                                # False: no CUDA GPU, models run on the CPU
    other_gb: float = 2.5                            # VRAM other programs want (M L1266)
    session: SessionSeed | None = SPEAK_SESSION
    player_at: float | None = 1.9                    # the take card's paused position (M L1276)
    forge: ForgeSeed | None = None
    takes_tab: Literal["session", "all", "starred"] = "session"
    settings_tab: Literal["general", "effects", "keys", "voice_chat"] = "general"
    log: tuple[str, ...] = SPEAK_LOG
    holds: tuple[str, ...] = ()                      # engine methods held from the start (FakeEngine.holds)
    fails: tuple[tuple[str, BaseException], ...] = ()   # (method, error) armed from the start
    steps: tuple[Step, ...] = ()


_SPEAK = Scenario(name="speak", board="Speak · steady state")
_FORGE = replace(_SPEAK, name="forge", board="Forge a voice", route="/forge", forge_files=True,
                 forge=ForgeSeed(clip="sample.wav", name="rin", backend=MTL, language="ja", consent=False))
_FORGE_READY = replace(_FORGE, name="forge_ready", board="",
                       forge=ForgeSeed(clip="sample.wav", name="rin", backend=MTL, language="ja", consent=True))
_TURBO_RIN = ForgeSeed(clip="sample.wav", name="rin", backend=TUR, language="en", consent=True)
_TURBO_OOM = BackendLoadError(TUR, "out_of_memory", "CUDA out of memory. Tried to allocate 512.00 MiB", 6.1)
_BAKE_OOM = GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate 64.00 MiB", during="bake")

SCENARIOS: Mapping[str, Scenario] = MappingProxyType({scenario.name: scenario for scenario in (
    _SPEAK,
    replace(_SPEAK, name="switch", board="Model switch · one model in VRAM", session=TURBO_SESSION,
            steps=(Step("ask", TUR),)),
    replace(_SPEAK, name="lowvram", board="", session=TURBO_SESSION, other_gb=8.5, steps=(Step("ask", TUR),)),
    replace(_SPEAK, name="failure", board="", session=TURBO_SESSION, other_gb=9.9,
            fails=(("make_resident", _TURBO_OOM),), steps=(Step("switch", TUR, asked=True),)),
    _FORGE,
    Scenario(name="firstrun", board="First run · nothing loaded", data="firstrun", resident="", session=None,
             player_at=None, log=()),
    replace(_SPEAK, name="voices", board="Voices", route="/voices"),
    replace(_SPEAK, name="takes", board="Takes", route="/takes", takes_tab="all"),
    replace(_SPEAK, name="models", board="Models", route="/models"),
    replace(_SPEAK, name="settings", board="Settings", route="/settings", settings_tab="effects"),
    replace(_SPEAK, name="other_backend", board="", session=TURBO_SESSION),
    replace(_SPEAK, name="loading", board="", session=TURBO_SESSION, holds=("make_resident",),
            steps=(Step("switch", TUR),)),
    replace(_SPEAK, name="generating", board="", holds=("speak",), steps=(Step("generate"),)),
    replace(_SPEAK, name="queued", board="", holds=("speak",), steps=(Step("generate", count=3),)),
    replace(_FORGE_READY, name="forging", holds=("load_voice",), steps=(Step("forge"),)),
    replace(_SPEAK, name="freeing", board="", holds=("unload_backend",), steps=(Step("free"),)),
    replace(_SPEAK, name="relaunch", board="", this_session=False, resident="", session=None, player_at=None,
            log=()),
    _FORGE_READY,
    replace(_FORGE_READY, name="forge_other_backend", forge=_TURBO_RIN),
    replace(_FORGE_READY, name="forge_low_vram", other_gb=8.5, forge=_TURBO_RIN),
    replace(_FORGE_READY, name="forge_replace", forge=replace(_FORGE_READY.forge, name="aiko")),
    replace(_FORGE_READY, name="forge_replace_other_backend", forge=replace(_TURBO_RIN, name="host-turbo")),
    replace(_FORGE_READY, name="forge_taken_other", forge=replace(_TURBO_RIN, name="aiko")),
    replace(_FORGE_READY, name="forge_turbo_short", forge=replace(_TURBO_RIN, clip="short_hello.wav")),
    replace(_FORGE_READY, name="forged", steps=(Step("forge"),)),
    replace(_FORGE_READY, name="forge_failed", fails=(("load_voice", _BAKE_OOM),)),
    replace(_SPEAK, name="voices_missing_files", board="", route="/voices",
            missing=("voices/reference/aiko_ref.wav", f"cache/{MTL}/aiko.pt")),
    replace(_SPEAK, name="voices_unconfirmed", board="", route="/voices", unconfirmed=("narrator",)),
    replace(_SPEAK, name="cpu", board="", cuda=False, log=CPU_LOG),
)})


def scenario_named(name: str) -> Scenario:
    """The scenario called `name`; ValueError lists the known names."""
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(f"Unknown FakeEngine scenario {name!r}; use one of: {', '.join(SCENARIOS)}") from None


# ---------- seed data (M L1215-1265) ----------

@dataclass(frozen=True)
class _VoiceSeed:
    voice_id: str
    backend: str
    language: str
    exaggeration: float
    clip: str                    # in voices/reference
    clip_s: float
    days_ago: int                # forged this many days before today
    at: str                      # local HH:MM:SS


@dataclass(frozen=True)
class _TakeSeed:
    take_id: int
    voice: str
    backend: str
    language: str
    text: str
    exaggeration: float | None
    cfg_weight: float | None
    seconds: float
    gen_seconds: float
    starred: bool
    days_ago: int                # 0 = this run
    at: str


@dataclass(frozen=True)
class _HistorySeed:
    days_ago: int
    at: str
    kind: str
    backend: str
    from_backend: str = ""
    seconds: float | None = None
    gb: float | None = None
    peak_gb: float | None = None
    reason: str = ""
    cold: bool = False


SEED_VOICES = (
    _VoiceSeed("aiko", MTL, "ja", 0.5, "aiko_ref.wav", 11.2, 4, "21:04:10"),
    _VoiceSeed("marco", MTL, "it", 0.5, "marco_ref.wav", 9.8, 3, "20:15:42"),
    _VoiceSeed("friend", MTL, "en", 0.5, "sample.wav", 12.0, 2, "19:47:03"),
    _VoiceSeed("narrator", STD, "en", 0.5, "narrator_ref.wav", 14.5, 6, "18:30:20"),
    _VoiceSeed("host-turbo", TUR, "en", 0.5, "host_ref.wav", 8.3, 1, "19:22:30"),
)

SEED_TAKES = (                                                       # newest first; stars on 7, 4 and 3
    _TakeSeed(7, "aiko", MTL, "ja", SCRIPT_JA, 0.5, 0.5, 4.6, 6.9, True, 0, "14:31:58"),
    _TakeSeed(6, "aiko", MTL, "ja", SCRIPT_JA, 0.8, 0.5, 4.9, 7.2, False, 0, "14:30:52"),
    _TakeSeed(5, "aiko", MTL, "ja", SCRIPT_JA, 0.5, 0.3, 5.2, 7.0, False, 0, "14:29:40"),
    _TakeSeed(4, "marco", MTL, "it", "Buongiorno! Che bella giornata, andiamo a fare una passeggiata?",
              0.5, 0.5, 3.2, 4.1, True, 0, "13:58:09"),
    _TakeSeed(3, "friend", MTL, "en", "Hey, it's me. Call me back when you get this.",
              0.5, 0.5, 2.8, 4.0, True, 1, "20:05:44"),
    _TakeSeed(2, "narrator", STD, "en", "Chapter one. The forge had been cold for a hundred years.",
              0.5, 0.4, 4.1, 5.9, False, 1, "19:41:05"),
    _TakeSeed(1, "host-turbo", TUR, "en", "Okay, okay [chuckle] I'll explain everything later.",
              None, None, 3.0, 1.7, False, 1, "19:25:12"),
)

EARLIER_RUN_HISTORY = (                                              # M L1253-1260
    _HistorySeed(1, "19:20:41", "loaded", TUR, seconds=8.7, gb=3.1, cold=True),
    _HistorySeed(1, "19:34:10", "switching", STD, from_backend=TUR),
    _HistorySeed(1, "19:34:11", "unloaded", TUR, gb=3.1, peak_gb=3.3, reason="switch"),
    _HistorySeed(1, "19:34:23", "loaded", STD, seconds=12.1, gb=3.5),
    _HistorySeed(1, "19:52:40", "switching", MTL, from_backend=STD),
    _HistorySeed(1, "19:52:41", "unloaded", STD, gb=3.5, peak_gb=3.7, reason="switch"),
    _HistorySeed(1, "19:52:55", "loaded", MTL, seconds=14.2, gb=3.4),
    _HistorySeed(1, "20:31:55", "closed", MTL, gb=3.4, peak_gb=3.6),
)
THIS_RUN_HISTORY = (                                                 # M L1243-1244
    _HistorySeed(0, "13:57:02", "loading", MTL),
    _HistorySeed(0, "13:57:16", "loaded", MTL, seconds=14.2, gb=3.4, cold=True),
)
THIS_RUN_CLOSED = _HistorySeed(0, "14:45:10", "closed", MTL, gb=3.4, peak_gb=3.6)   # relaunch: that run ended
FORGED_EVENT = _HistorySeed(1, "20:12:30", "forged", MTL, seconds=1.9)            # "forging takes about 2 s"


# ---------- test controls ----------

PUBLIC_METHODS = (
    "residency", "is_resident", "list_loaded_backends", "make_resident", "load_backend", "unload_backend",
    "close", "set_preferred_device", "engine_lock_holder", "speak", "load_voice", "update_voice", "delete_voice",
    "voice_details", "voice_cache_path", "device_info", "backend_info", "weights_status", "list_voices",
    "list_backends", "voice_info",
)
_HOLD_POINTS = frozenset(PUBLIC_METHODS) | {"evict", "decode"}
_FAIL_POINTS = frozenset(PUBLIC_METHODS)
_ALIASES = {"load_backend": "make_resident"}
_INNER_HOLDS = frozenset({"make_resident", "load_backend", "unload_backend", "speak", "load_voice"})
_INNER_FAILS = _INNER_HOLDS | {"device_info"}
_PASSTHROUGH = (GenerationError, BackendNotResident, BackendLoadError, EngineBusy, ScriptTooLong)


class Hold(threading.Event):
    """An armed hold: the call that reaches its point waits until set() (release()) or its job is cancelled."""

    def __init__(self, point: str) -> None:
        super().__init__()
        self.point = point
        self.entered = threading.Event()

    def release(self) -> None:
        self.set()

    def wait_entered(self, timeout: float = HOLD_TIMEOUT_S) -> bool:
        """Wait until a call reached this hold."""
        return self.entered.wait(timeout)


@dataclass
class Call:
    """One public call as FakeEngine.calls records it; an exception is kept as text only."""

    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    thread: int                  # threading.get_ident() of the caller
    started: float               # time.monotonic()
    ended: float | None = None
    outcome: str = "running"     # "running" | "returned" | "raised"
    error: str = ""              # "<Type>: <first line>"

    def finish(self, exc: BaseException | None) -> None:
        self.ended = time.monotonic()
        if exc is None:
            self.outcome = "returned"
        else:
            self.outcome = "raised"
            self.error = f"{type(exc).__name__}: {_error_text(exc)}"


def _recorded(method: Callable[..., _T]) -> Callable[..., _T]:
    """Record the call; plain methods also meet their entry hold and armed failure here."""
    name = method.__name__

    @functools.wraps(method)
    def wrapper(self: FakeEngine, *args: Any, **kwargs: Any) -> _T:
        call = self._begin(name, args, kwargs)
        try:
            if name not in _INNER_HOLDS:
                self._wait_at(name)
            if name not in _INNER_FAILS:
                self._raise_armed(name)
            result = method(self, *args, **kwargs)
        except BaseException as exc:
            call.finish(exc)
            raise
        call.finish(None)
        return result

    return wrapper


@dataclass
class _Model:
    """A simulated resident model and its backend's voice state."""

    backend: str
    device: str
    reserved: int | None                         # VRAM it holds; None on the CPU
    active_voice: str | None = None
    last_conds_load_s: float | None = None


@dataclass(frozen=True)
class _TakePlan:
    tokens: int                  # the fake EOS step; more than the bound = the script is cut off
    audio_s: float
    made_s: float


def _passthrough(iterable: Any, *args: Any, **kwargs: Any) -> Any:
    return iter(iterable)


# The engine's own progress hook around a pass-through "tqdm": a bound Job sees tokens, phases and
# cancel points exactly as in Chatterbox's loops, and outside a job the loop runs plainly.
_SAMPLING_LOOP = progress._wrap(_passthrough, "sampling")
_DECODING_LOOP = progress._wrap(_passthrough, "decoding")


# ---------- the engine ----------

class FakeEngine:
    """The engine's public surface on seeded scratch data; see the module docstring.

    Beyond the engine's own methods: scenario, session_id, data_dir, speed and on_event (both may be
    assigned later), calls, loads, hold(), holds, release_all(), fail_next(), armed_failures(), disarm(),
    set_other_gb() and reset_records().
    """

    def __init__(self, scenario: str | Scenario = "speak", *, data_dir: str | os.PathLike[str],
                 speed: float = 0.0, on_event: Callable[[EngineEvent], None] | None = None,
                 max_resident: int | None = None, enabled: Sequence[str] | None = None,
                 max_calls: int | None = None) -> None:
        """
        :param max_calls: keep only about this many recorded calls (a long --fake dev run); None keeps all
        """
        self.max_calls = max_calls
        self.scenario = scenario if isinstance(scenario, Scenario) else scenario_named(scenario)
        self.data_dir = Path(os.path.abspath(data_dir))
        self.config = _fake_config(self.data_dir, max_resident=max_resident, enabled=enabled)
        self._available = _enabled_backends(self.config.ENABLED_BACKENDS)
        self._max_resident = max(1, int(self.config.MAX_RESIDENT_BACKENDS))
        self.session_id = _seed(self.scenario, self.config)
        self.registry = VoiceRegistry(self.config.CACHE_DIR / "registry.json")
        self.speed = float(speed)
        self.on_event = on_event
        self.calls: list[Call] = []
        self.loads: list[str] = []
        self._models: OrderedDict[str, _Model] = OrderedDict()
        self._lock = threading.RLock()
        self._state_mx = threading.Lock()
        self._controls_mx = threading.Lock()
        self._records_mx = threading.Lock()
        self._holds: dict[str, deque[Hold]] = {}
        self._all_holds: list[Hold] = []
        self._fails: dict[str, deque[BaseException]] = {}
        self._has_cuda = self.scenario.cuda
        self._other_bytes = round(self.scenario.other_gb * GIB)
        self._cuda_ready = False
        self._torch_loaded = False
        self._built = False
        self._peak: int | None = None
        self._takes_made = 0
        self._residency = Residency(
            resident=(), loading=None, unloading=None, busy=None, device=None,
            preferred_device=self.config.DEVICE, max_resident=self._max_resident, last_used=None,
            lock_held=False,
        )
        self._start_resident()
        for point in self.scenario.holds:
            self.hold(point)
        for method, error in self.scenario.fails:
            self.fail_next(method, error)

    # ---------- residency (lock-free) ----------

    @_recorded
    def residency(self) -> Residency:
        """What is resident, loading or unloading now. Never waits for a load or a take."""
        return self._residency

    @_recorded
    def is_resident(self, name: str) -> bool:
        return name in self._residency.resident

    @_recorded
    def list_loaded_backends(self) -> list[str]:
        """Resident backends, least recently used first. Lock-free."""
        return list(self._residency.resident)

    # ---------- models ----------

    @_recorded
    def make_resident(self, name: str) -> LoadReport:
        """Make `name` resident, evicting the least recently used model first (the engine's rules)."""
        self._require_enabled(name)
        with self._lock:
            try:
                if name in self._models:
                    self._touch(name)
                    return self._resident_report(name)
                return self._load(name)[1]
            finally:
                self._publish(last_used=time.monotonic())

    @_recorded
    def load_backend(self, name: str) -> str:
        return self.make_resident(name).backend

    @_recorded
    def unload_backend(self, name: str, *, reason: str = "requested") -> bool:
        """Free `name`'s model. False when it was not resident (or is unknown)."""
        self._raise_armed("unload_backend")
        with self._lock:
            if name not in self._models:
                return False
            self._publish(busy="unload")
            try:
                self._release(name, reason, point="unload_backend")
            finally:
                self._publish(busy=None)
            if not self._models:
                self._drop_engine_lock()
            return True

    @_recorded
    def close(self) -> None:
        """Unload every model (reason "close") and release the engine lock."""
        with self._lock:
            self._publish(busy="unload")
            try:
                for name in list(self._models):
                    self._release(name, "close", point=None)
            finally:
                self._publish(busy=None)
            self._drop_engine_lock()

    @_recorded
    def set_preferred_device(self, device: Literal["cuda", "cpu"]) -> None:
        """Where the next model load runs. Lock-free; never unloads the resident model."""
        if device not in _DEVICES:
            raise ValueError(f"Unknown device {device!r}: use 'cuda' or 'cpu'.")
        self._publish(preferred_device=device)

    @_recorded
    def engine_lock_holder(self) -> dict | None:
        """The fake never shares an engine lock with another program."""
        return None

    # ---------- voices ----------

    @_recorded
    def speak(self, text: str, voice_id: str, *, require_resident: bool = False,
              job: Job | None = None, **params: Any) -> bytes:
        """A PCM_16 take of `text` in `voice_id`'s voice, with the engine's checks and failures.

        The knobs in `params` do not change the fake audio; the caller's arguments are in `calls`.
        """
        with _JobOutcome(job):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(_EMPTY_TEXT)
            with self._lock:
                info = self.registry.get(voice_id)
                if info is None:
                    raise ValueError(f"Voice '{voice_id}' is not registered. Call load_voice() first.")
                try:
                    return self._take(info["backend"], text, voice_id, require_resident, job)
                finally:
                    self._publish(busy=None, last_used=time.monotonic())

    @_recorded
    def load_voice(
        self,
        voice_id: str,
        reference_wav: Path,
        backend: str | None = None,
        exaggeration: float | None = None,
        language: str | None = None,
        force: bool = False,
        *,
        require_resident: bool = False,
        meta: dict | None = None,
        job: Job | None = None,
        **params: Any,
    ) -> str:
        """Bake (or, with a cached .pt and force=False, reuse) a voice and register it, as the engine does."""
        with _JobOutcome(job):
            backend = backend or self.config.DEFAULT_BACKEND
            exaggeration = _new_exaggeration(
                self.config.DEFAULT_EXAGGERATION if exaggeration is None else exaggeration)
            language = self.config.DEFAULT_LANGUAGE if language is None else language
            reference = Path(reference_wav).resolve()
            language = self._check_new_voice(voice_id, reference, backend, language)
            with self._lock:
                try:
                    if job is not None:
                        job.check()
                    model = self._get_backend(backend, allow_load=not require_resident)
                    path = self._conds_path(backend, voice_id)
                    with self.registry.cached_reference(voice_id, backend, path, force=force) as source:
                        cached = source is not None
                        with stage_conditioning(path, cached=cached,
                                                invalidate=lambda: self._forget_active_voice(voice_id)) as staged:
                            self._bake(job, cached, lambda: self._run_bake(model, voice_id, reference, exaggeration, cached))
                            stored = self._kept_reference(voice_id, backend, reference, source)
                            self.registry.register(voice_id, backend=backend, reference_wav=stored,
                                                   exaggeration=exaggeration, language=language, meta=meta,
                                                   baked=not cached, staged=staged)
                    return voice_id
                finally:
                    self._publish(busy=None, last_used=time.monotonic())

    @_recorded
    def update_voice(self, voice_id: str, *, language: str | None = None,
                     exaggeration: float | None = None, meta: dict | None = None) -> dict:
        """Edit a voice's language, default Expressiveness or meta; registry only, no RLock."""
        entry = self.registry.get(voice_id)
        if not isinstance(entry, dict):
            raise ValueError(f"Voice '{voice_id}' is not registered.")
        backend = str(entry.get("backend", ""))
        info = BACKENDS.get(backend)
        if language is not None:
            language = _edited_language(backend, info, language)
        if exaggeration is not None:
            exaggeration = _edited_exaggeration(backend, info, exaggeration)
        updated = self.registry.update(voice_id, language=language, exaggeration=exaggeration, meta=meta)
        if updated is None:
            raise ValueError(f"Voice '{voice_id}' is not registered.")
        return updated

    @_recorded
    def delete_voice(self, voice_id: str, *, discard: Callable[[Path], None] | None = None) -> DeleteResult:
        """Delete a voice: its .pt first, then its entry, then orphan .pt files (the engine's rules)."""
        validate_voice_id(voice_id)
        discard = discard or Path.unlink
        with self._lock, self.registry.voice_files_lock():
            entries = self.registry.entries()
            if voice_id not in entries:
                return DeleteResult(removed_entry=False, removed_files=(), failed_files=())
            entry = entries[voice_id]
            backend = entry.get("backend") if isinstance(entry, dict) else None
            twins = _casefold_twins(entries, voice_id)
            removed: list[Path] = []
            failed: list[tuple[Path, str]] = []
            if isinstance(backend, str):
                _discard_primary(self._conds_path(backend, voice_id), twins.get(backend), discard, removed, failed)
            self._forget_active_voice(voice_id)
            self.registry.remove(voice_id)
            for other in BACKENDS:
                if other != backend:
                    _discard_orphan(self._conds_path(other, voice_id), twins.get(other), discard, removed, failed)
            return DeleteResult(removed_entry=True, removed_files=tuple(removed), failed_files=tuple(failed))

    @_recorded
    def voice_details(self, voice_id: str | None = None) -> list[VoiceDetails]:
        """Registry entries plus file facts, sorted by id (one voice, or [] if unknown)."""
        entries = self.registry.entries()
        ids = sorted(entries) if voice_id is None else [voice_id]
        return [self._details(vid, entries[vid]) for vid in ids if isinstance(entries.get(vid), dict)]

    @_recorded
    def voice_cache_path(self, voice_id: str) -> Path | None:
        """CACHE_DIR/<backend>/<voice_id>.pt for a registered voice, else None."""
        entry = self.registry.get(voice_id)
        backend = entry.get("backend") if isinstance(entry, dict) else None
        return self._conds_path(backend, voice_id) if isinstance(backend, str) else None

    # ---------- facts ----------

    @_recorded
    def device_info(self, probe: bool = False) -> DeviceInfo:
        """The stand-in GPU's readings, by the engine's rules for probing and initialised CUDA."""
        armed = self._take_armed("device_info")
        if armed is not None:
            return self._reading(cuda_available=False, error=_error_text(armed))
        if not self._has_cuda:
            self._torch_loaded = self._torch_loaded or probe
            return self._reading(cuda_available=False if probe else None)
        if not self._cuda_ready and not probe:
            return self._reading()
        self._cuda_ready = self._torch_loaded = True
        reserved = self._reserved()
        other = min(self._other_bytes, max(0, GPU_TOTAL_BYTES - reserved))
        return DeviceInfo(
            initialized=True, cuda_available=True, name=GPU_NAME, total_bytes=GPU_TOTAL_BYTES,
            free_bytes=GPU_TOTAL_BYTES - other - reserved, reserved_bytes=reserved,
            allocated_bytes=_allocated(reserved), other_bytes=other, peak_reserved_bytes=self._peak or 0,
            torch_version=TORCH_VERSION, cuda_version=CUDA_VERSION, error=None, measured_at=time.monotonic(),
        )

    @_recorded
    def backend_info(self, name: str) -> BackendInfo:
        """Static facts about a backend; imports nothing."""
        info = BACKENDS.get(name)
        if info is None:
            raise ValueError(f"Unknown backend '{name}'. Known: {list(BACKENDS)}")
        return info

    @_recorded
    def weights_status(self, name: str) -> tuple[bool, int | None]:
        """Every simulated model's weights are in the cache (the sizes of §3.6)."""
        if name not in BACKENDS:
            raise ValueError(f"Unknown backend '{name}'. Known: {list(BACKENDS)}")
        return True, MODEL_SPECS[name].weights_bytes

    @_recorded
    def list_voices(self) -> list[str]:
        return self.registry.list_ids()

    @_recorded
    def list_backends(self) -> list[str]:
        """Selectable backends, whether or not their model is resident."""
        return list(self._available)

    @_recorded
    def voice_info(self, voice_id: str) -> dict | None:
        return self.registry.get(voice_id)

    # ---------- test controls ----------

    def hold(self, method: str) -> Hold:
        """Park the next call of `method` at its work point until the returned Hold is released.

        make_resident (alias load_backend): the next model build, after the eviction (the load phase).
        unload_backend: before the model is freed (busy "unload"). evict: the next eviction of a switch,
        before the model is freed. speak: the next take, at its first sampling step. decode: the
        uninterruptible Standard or Multilingual decode after sampling. load_voice: the next bake, in
        phase "baking". Any other public method: at its start. A hold also ends at HOLD_TIMEOUT_S.
        """
        hold = Hold(_point(method, _HOLD_POINTS))
        with self._controls_mx:
            self._holds.setdefault(hold.point, deque()).append(hold)
            self._all_holds.append(hold)
        return hold

    @property
    def holds(self) -> tuple[Hold, ...]:
        """Every hold armed so far, the scenario's included, oldest first."""
        with self._controls_mx:
            return tuple(self._all_holds)

    def release_all(self) -> None:
        """Release every hold (call it at teardown)."""
        for hold in self.holds:
            hold.release()

    def fail_next(self, method: str, error: BaseException) -> None:
        """Make the next call of `method` fail with `error`, the way the engine raises it.

        make_resident (alias load_backend), for the next call that loads: an EngineBusy is raised while
        nothing is resident (as the real lock is free only then), before anything changes; any other
        error fails the build after the eviction (load_failed, then BackendLoadError: a BackendLoadError
        keeps its kind and detail, anything else is classified as the engine does). speak and load_voice:
        the take or bake fails at its first step: Cancelled, GenerationError, BackendNotResident,
        EngineBusy and BackendLoadError come out as fresh copies, a ValueError as a fresh ValueError of
        its first line, anything else as GenerationError (out_of_memory when the text says so).
        device_info: the reading carries the error. Any other method: a fresh copy at its start.
        Errors armed for one method fire in order; every raised exception has no __context__.
        """
        if not isinstance(error, BaseException):
            raise TypeError(f"fail_next needs an exception, got {type(error).__name__}")
        point = _point(method, _FAIL_POINTS)
        with self._controls_mx:
            self._fails.setdefault(point, deque()).append(error)

    def armed_failures(self, method: str) -> tuple[BaseException, ...]:
        """The errors still armed for `method` (templates, in firing order)."""
        point = _point(method, _FAIL_POINTS)
        with self._controls_mx:
            return tuple(self._fails.get(point, ()))

    def disarm(self) -> int:
        """Drop every armed failure and release every hold; returns how many failures were dropped."""
        with self._controls_mx:
            dropped = sum(len(queue) for queue in self._fails.values())
            self._fails.clear()
        self.release_all()
        return dropped

    def set_other_gb(self, gb: float) -> None:
        """VRAM other programs want from now on (they never take what this app holds)."""
        self._other_bytes = round(gb * GIB)

    def reset_records(self) -> None:
        """Forget the recorded calls and loads (for example after replaying a scenario's steps)."""
        with self._records_mx:
            self.calls.clear()
            self.loads.clear()

    # ---------- loading and unloading (call with the RLock held) ----------

    def _require_enabled(self, name: str) -> None:
        if name not in self._available:
            raise ValueError(f"Backend '{name}' isn't enabled. Available: {self._available}")

    def _get_backend(self, name: str, *, allow_load: bool = True) -> _Model:
        self._require_enabled(name)
        with self._lock:
            model = self._models.get(name)
            if model is not None:
                self._touch(name)
                return model
            if not allow_load:
                raise BackendNotResident(name, tuple(self._models))
            return self._load(name)[0]

    def _load(self, name: str) -> tuple[_Model, LoadReport]:
        """Engine lock, eviction, device, build; a failed build leaves nothing behind."""
        self._take_engine_lock()
        self._publish(loading=name, busy="load")
        self._emit("load_started", name)
        evicted, freed = self._evict()
        self._check_one_model(name)
        device = self._resolve_device()
        first = not self._built
        self._built = self._torch_loaded = True
        with self._records_mx:
            self.loads.append(name)
        self._wait_at("make_resident")
        spec = MODEL_SPECS[name]
        failure = self._build_failure(name)
        if failure is not None:
            kind, detail, seconds = failure
            self._pace(seconds)
            self._fail_load(name, device, seconds, kind, detail)
        self._pace(spec.load_s)
        model = _Model(name, device, spec.vram_bytes if device == "cuda" else None)
        return model, self._finish_load(model, spec.load_s, evicted, freed, first)

    def _evict(self) -> tuple[tuple[str, ...], int | None]:
        evicted: list[str] = []
        freed: list[int | None] = []
        while len(self._models) >= self._max_resident:
            oldest = next(iter(self._models))
            freed.append(self._release(oldest, "evicted", point="evict"))
            evicted.append(oldest)
        known = [n for n in freed if n is not None]
        total = sum(known) if evicted and len(known) == len(freed) else None
        return tuple(evicted), total

    def _check_one_model(self, name: str) -> None:
        """The tripwire of the one-model rule: a load that would leave two models resident."""
        if self._models:
            self._publish_resident(loading=None, busy=None)
            raise AssertionError(f"FakeEngine: loading {name} would leave {len(self._models) + 1} models "
                                 f"resident; the app keeps one model in VRAM")

    def _build_failure(self, name: str) -> tuple[str, str, float] | None:
        """(kind, detail, seconds) of an armed build failure, else None (an EngineBusy waits for the lock step)."""
        with self._controls_mx:
            queue = self._fails.get("make_resident")
            error = queue.popleft() if queue and not isinstance(queue[0], EngineBusy) else None
        if error is None:
            return None
        if isinstance(error, BackendLoadError):
            return error.kind, error.detail, error.seconds
        return _load_failure_kind(error), _error_text(error), round(MODEL_SPECS[name].load_s * FAILED_LOAD_SHARE, 1)

    def _finish_load(self, model: _Model, seconds: float, evicted: tuple[str, ...], freed: int | None,
                     first: bool) -> LoadReport:
        self._models[model.backend] = model
        if model.device == "cuda":
            self._cuda_ready = True
            self._peak = self._reserved()                        # the only reset: at a load
        reserved = self._allocator(model.device, "reserved")
        allocated = self._allocator(model.device, "allocated")
        self._publish_resident(loading=None, busy=None)
        self._emit("loaded", model.backend, seconds=seconds, device=model.device, reserved_bytes=reserved,
                   allocated_bytes=allocated, first_in_process=first)
        return LoadReport(backend=model.backend, device=model.device, seconds=seconds, already_resident=False,
                          evicted=evicted, freed_bytes=freed, reserved_bytes=reserved, allocated_bytes=allocated,
                          first_in_process=first)

    def _fail_load(self, name: str, device: str, seconds: float, kind: Any, detail: str) -> NoReturn:
        self._publish_resident(loading=None, busy=None)
        if not self._models:
            self._drop_engine_lock()
        self._emit("load_failed", name, seconds=seconds, device=device, error=f"{kind}: {detail}")
        raise BackendLoadError(name, kind, detail, seconds)

    def _release(self, name: str, reason: str, *, point: str | None) -> int | None:
        """Free one model, measured; emits `unloaded` and returns the freed bytes (None on the CPU)."""
        device = self._models[name].device
        self._publish(unloading=name)
        if point is not None:
            self._wait_at(point)
        peak = self._allocator(device, "peak")
        before = self._allocator(device, "reserved")
        del self._models[name]
        self._pace(UNLOAD_S)
        after = self._allocator(device, "reserved")
        allocated = self._allocator(device, "allocated")
        freed = max(0, before - after) if before is not None and after is not None else None
        self._publish_resident(unloading=None)
        self._emit("unloaded", name, device=device, reserved_bytes=after, allocated_bytes=allocated,
                   freed_bytes=freed, peak_bytes=peak, reason=reason)
        return freed

    def _touch(self, name: str) -> None:
        self._models.move_to_end(name)
        self._publish_resident()

    def _resident_report(self, name: str) -> LoadReport:
        device = self._models[name].device
        return LoadReport(backend=name, device=device, seconds=0.0, already_resident=True, evicted=(),
                          freed_bytes=None, reserved_bytes=self._allocator(device, "reserved"),
                          allocated_bytes=self._allocator(device, "allocated"), first_in_process=False)

    def _take_engine_lock(self) -> None:
        """The engine lock is free only while nothing is resident: an armed EngineBusy fires then."""
        if self._residency.lock_held:
            return
        with self._controls_mx:
            queue = self._fails.get("make_resident")
            busy = queue.popleft() if queue and isinstance(queue[0], EngineBusy) else None
        if busy is not None:
            raise _fresh(busy)
        self._publish(lock_held=True)

    def _drop_engine_lock(self) -> None:
        self._publish(lock_held=False)

    def _resolve_device(self) -> str:
        return "cuda" if self._residency.preferred_device == "cuda" and self._has_cuda else "cpu"

    def _start_resident(self) -> None:
        """The scenario's resident model: loaded before launch, so it counts as no load here."""
        name = self.scenario.resident
        if not name or name not in self._available:
            return
        device = self._resolve_device()
        spec = MODEL_SPECS[name]
        self._models[name] = _Model(name, device, spec.vram_bytes if device == "cuda" else None,
                                    active_voice=_active_voice(self.scenario))
        self._built = self._torch_loaded = True
        if device == "cuda":
            self._cuda_ready = True
            self._peak = spec.vram_bytes + (PEAK_EXTRA_BYTES if self.scenario.this_session else 0)
        self._publish_resident(lock_held=True)

    # ---------- takes and bakes (call with the RLock held) ----------

    def _take(self, name: str, text: str, voice_id: str, require_resident: bool, job: Job | None) -> bytes:
        if job is not None:
            job.check()
        model = self._get_backend(name, allow_load=not require_resident)
        self._publish(busy="generate")
        _set_phase(job, "preparing")
        with progress.bind(job):
            wav, made_s = self._call_model("speak", lambda: self._run_take(model, text, voice_id, job))
        if job is not None:
            job.made_s = made_s
            job.conds_load_s = model.last_conds_load_s
            job.peak_reserved_bytes = self._allocator(model.device, "peak")
        return wav

    def _run_take(self, model: _Model, text: str, voice_id: str, job: Job | None) -> tuple[bytes, float]:
        """The backend side of a take: activate the voice, sample, decode, render the audio."""
        validate_voice_id(voice_id)
        check_fake_script(text, model.backend)
        self._activate(model, voice_id)
        plan = _take_plan(text, model.backend)
        self._sample(plan, job, BACKENDS[model.backend].max_tokens)
        if model.backend == TUR:
            for _ in _DECODING_LOOP(range(TURBO_FLOW_STEPS)):
                pass
        else:
            _set_phase(job, "decoding")
            self._wait_at("decode")       # a Standard/Multilingual decode can finish after Stop
        self._bump_peak(model)
        self._takes_made += 1
        seed = zlib.crc32(f"{voice_id}|{text}|{self._takes_made}".encode("utf-8"))
        return _wav(_speech(round(plan.audio_s * SAMPLE_RATE), SAMPLE_RATE, seed, TAKE_PEAK), SAMPLE_RATE), plan.made_s

    def _activate(self, model: _Model, voice_id: str) -> None:
        """Load the voice's .pt unless it is already active (sets last_conds_load_s like the backends)."""
        model.last_conds_load_s = None
        if voice_id == model.active_voice:
            return
        if not self._conds_path(model.backend, voice_id).exists():
            raise ValueError(f"Voice '{voice_id}' has no cached conditionals for {model.backend}. "
                             f"Call load_voice(backend='{model.backend}') first.")
        model.last_conds_load_s = CONDS_LOAD_S
        model.active_voice = voice_id

    def _sample(self, plan: _TakePlan, job: Job | None, bound: int) -> None:
        """The sampling loop through the engine's progress hook; the speak hold and failure sit at step 1."""
        pause = plan.made_s * self.speed / max(1, min(plan.tokens, bound)) if self.speed > 0 else 0.0
        for step, _ in enumerate(_SAMPLING_LOOP(range(bound))):
            if step == 0:
                self._wait_at("speak", job)
                if job is not None:
                    job.check()
                self._raise_armed("speak")
            if step + 1 >= plan.tokens:
                break                                            # EOS
            if pause:
                time.sleep(pause)

    def _bake(self, job: Job | None, cached: bool, work: Callable[[], float]) -> None:
        self._publish(busy="bake")
        _set_phase(job, "preparing" if cached else "baking")
        with progress.bind(job):
            bake_s = self._call_model("bake", work)
        if job is not None and not cached:
            job.bake_s = bake_s

    def _run_bake(self, model: _Model, voice_id: str, reference: Path, exaggeration: float, cached: bool) -> float:
        """The backend side of load_voice: reuse the .pt, or bake and save it atomically."""
        validate_voice_id(voice_id)
        self._wait_at("load_voice")
        self._raise_armed("load_voice")
        if cached:
            model.active_voice = voice_id
            return 0.0
        model.active_voice = None
        spec = MODEL_SPECS[model.backend]
        self._pace(spec.bake_s)
        _write_conds(self._conds_path(model.backend, voice_id), voice_id, model.backend, reference, exaggeration)
        model.active_voice = voice_id
        self._bump_peak(model)
        return spec.bake_s

    def _call_model(self, during: Literal["speak", "bake"], work: Callable[[], _T]) -> _T:
        """Run a take or a bake; failures come back fresh, typed as the engine types them."""
        failure: tuple[bool, str, bool] | None = None
        passthrough: BaseException | None = None
        try:
            return work()
        except Cancelled:
            pass
        except _PASSTHROUGH as exc:
            passthrough = _fresh(exc)
        except Exception as exc:
            failure = (isinstance(exc, ValueError), _error_text(exc), _is_out_of_memory(exc))
        if passthrough is not None:
            raise passthrough
        if failure is None:
            raise Cancelled()
        value_error, text, out_of_memory = failure
        if value_error:
            raise ValueError(text)
        raise GenerationError("out_of_memory" if out_of_memory else "other", text, during=during)

    def _bump_peak(self, model: _Model) -> None:
        """A take or bake raises max_memory_reserved above the weights (it is reset only at a load)."""
        if model.device == "cuda" and self._cuda_ready:
            self._peak = max(self._peak or 0, self._reserved() + PEAK_EXTRA_BYTES)

    # ---------- voice checks and records ----------

    def _check_new_voice(self, voice_id: str, reference: Path, backend: str, language: str) -> str:
        """Every check load_voice() makes before it may load a model; returns the language to store."""
        validate_voice_id(voice_id)
        self._require_enabled(backend)
        _check_reference(reference, backend)
        stored = _stored_language(backend, language)
        existing = self.registry.find_casefold(voice_id)
        if existing is not None:
            raise ValueError(f"{voice_id} and {existing} would share one file")
        return stored

    def _kept_reference(self, voice_id: str, backend: str, reference: Path, stored: str | None) -> str:
        """Keep a cache hit's known reference, warning when the requested clip differs."""
        if stored is None:
            return _stored_reference(self.config, self.data_dir, reference)
        if not self._same_file(stored, reference):
            warnings.warn(
                f"'{voice_id}' already has cached conditioning on {backend}, baked from {stored}; "
                f"kept it and ignored {reference} (pass force=True to bake the new clip).",
                stacklevel=4,
            )
        return stored

    def _same_file(self, stored: str, reference: Path) -> bool:
        resolved = (Path(self.config.ROOT) / stored).resolve()
        return os.path.normcase(str(resolved)) == os.path.normcase(str(reference))

    def _forget_active_voice(self, voice_id: str) -> None:
        for model in self._models.values():
            if model.active_voice == voice_id:
                model.active_voice = None

    def _conds_path(self, backend: str, voice_id: str) -> Path:
        return self.config.CACHE_DIR / backend / f"{voice_id}.pt"

    def _details(self, voice_id: str, entry: dict) -> VoiceDetails:
        backend = str(entry.get("backend", ""))
        meta = dict(entry["meta"]) if isinstance(entry.get("meta"), dict) else {}
        stored = entry.get("reference_wav")
        stored = stored if isinstance(stored, str) else ""
        conds = self._conds_path(backend, voice_id)
        consent = meta.get("consent")
        return VoiceDetails(
            voice_id=voice_id,
            backend=backend,
            backend_enabled=backend in self._available,
            language=str(entry.get("language") or self.config.DEFAULT_LANGUAGE),
            exaggeration=_as_float(entry.get("exaggeration"), self.config.DEFAULT_EXAGGERATION),
            reference_wav=stored,
            reference_path=self._find_reference(stored),
            conds_path=conds,
            conds_exists=conds.is_file(),
            created_at=_optional_str(entry.get("created_at")),
            forged_at=_optional_str(entry.get("forged_at")),
            consent=None if consent is None else bool(consent),
            meta=meta,
        )

    def _find_reference(self, stored: str) -> Path | None:
        """The stored clip if it exists, else ROOT/<relative>, else VOICES_DIR/<name>; None = missing."""
        if not stored:
            return None
        path = Path(stored)
        root = Path(self.config.ROOT)
        candidates = (path if path.is_absolute() else root / path, Path(self.config.VOICES_DIR) / path.name)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    # ---------- readings ----------

    def _reserved(self) -> int:
        """VRAM the resident models hold (a snapshot: device_info reads it without the RLock)."""
        return sum(model.reserved or 0 for model in tuple(self._models.values()))

    def _allocator(self, device: str | None, reading: Literal["reserved", "allocated", "peak"]) -> int | None:
        """The engine's guarded allocator reading: None unless the device is CUDA and CUDA is initialised."""
        if device != "cuda" or not self._cuda_ready:
            return None
        reserved = self._reserved()
        if reading == "reserved":
            return reserved
        if reading == "allocated":
            return _allocated(reserved)
        return max(self._peak or 0, reserved)

    def _reading(self, *, cuda_available: bool | None = None, error: str | None = None) -> DeviceInfo:
        """A reading without CUDA numbers (not checked yet, no CUDA, or an error)."""
        return DeviceInfo(
            initialized=self._cuda_ready, cuda_available=cuda_available, name=None, total_bytes=None,
            free_bytes=None, reserved_bytes=0, allocated_bytes=0, other_bytes=None, peak_reserved_bytes=0,
            torch_version=TORCH_VERSION if self._torch_loaded else "", cuda_version=None, error=error,
            measured_at=time.monotonic(),
        )

    # ---------- published state and events ----------

    def _publish(self, **changes: Any) -> None:
        """Replace (never mutate) the Residency that residency() hands out."""
        with self._state_mx:
            self._residency = replace(self._residency, **changes)

    def _publish_resident(self, **changes: Any) -> None:
        resident = tuple(self._models)
        device = self._models[resident[-1]].device if resident else None
        self._publish(resident=resident, device=device, **changes)

    def _emit(self, kind: Literal["load_started", "loaded", "load_failed", "unloaded"], backend: str,
              **fields: Any) -> None:
        """Deliver one EngineEvent on this thread; a failing handler is reported, never raised."""
        handler = self.on_event
        if handler is None:
            return
        event = EngineEvent(kind=kind, backend=backend, at=time.time(), **fields)
        try:
            handler(event)
        except Exception as exc:
            _engine_log.warning("Warning: the engine's on_event handler failed on '%s': %s", kind, _error_text(exc),
                                exc_info=True)

    # ---------- control plumbing ----------

    def _begin(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Call:
        call = Call(name, tuple(args), dict(kwargs), threading.get_ident(), time.monotonic())
        with self._records_mx:
            self.calls.append(call)
            if self.max_calls and len(self.calls) > 2 * self.max_calls:
                del self.calls[:-self.max_calls]      # a long --fake dev run keeps the newest max_calls
        return call

    def _wait_at(self, point: str, job: Job | None = None) -> None:
        """Wait at an armed hold for `point` until it is released, the job is cancelled, or the timeout."""
        with self._controls_mx:
            queue = self._holds.get(point)
            hold = queue.popleft() if queue else None
        if hold is None:
            return
        hold.entered.set()
        deadline = time.monotonic() + HOLD_TIMEOUT_S
        while not hold.wait(HOLD_POLL_S):
            if (job is not None and job.cancelled) or time.monotonic() >= deadline:
                return

    def _take_armed(self, point: str) -> BaseException | None:
        with self._controls_mx:
            queue = self._fails.get(point)
            return queue.popleft() if queue else None

    def _raise_armed(self, point: str) -> None:
        error = self._take_armed(point)
        if error is not None:
            raise _fresh(error)

    def _pace(self, seconds: float) -> None:
        if self.speed > 0 and seconds > 0:
            time.sleep(seconds * self.speed)


# ---------- helpers ----------

def _point(method: str, allowed: frozenset[str]) -> str:
    point = _ALIASES.get(method, method)
    if point not in allowed:
        raise ValueError(f"FakeEngine has no {method!r} to hold or fail; use one of: {', '.join(sorted(allowed))}")
    return point


def _fresh(error: BaseException) -> BaseException:
    """A new exception like `error`, with no traceback, __context__ or __cause__."""
    if type(error) is ValueError:
        return ValueError(_error_text(error))
    try:
        return copy.copy(error)
    except Exception:
        return RuntimeError(_error_text(error))


def _allocated(reserved: int) -> int:
    return int(reserved * ALLOCATED_SHARE)


def _fake_config(root: Path, *, max_resident: int | None, enabled: Sequence[str] | None) -> SimpleNamespace:
    """config.py's settings (device, defaults, enabled backends, one model at a time) with paths under root."""
    def setting(name: str, default: Any) -> Any:
        return getattr(_app_config, name, default)

    return SimpleNamespace(
        ROOT=root,
        PORTABLE=setting("PORTABLE", False),
        CACHE_DIR=root / "cache",
        VOICES_DIR=root / "voices" / "reference",
        OUTPUTS_DIR=root / "outputs",
        DEVICE=setting("DEVICE", "cuda"),
        DEFAULT_BACKEND=setting("DEFAULT_BACKEND", MTL),
        DEFAULT_LANGUAGE=setting("DEFAULT_LANGUAGE", "en"),
        ENABLED_BACKENDS=list(enabled if enabled is not None else setting("ENABLED_BACKENDS", list(BACKENDS))),
        MAX_RESIDENT_BACKENDS=max_resident if max_resident is not None else setting("MAX_RESIDENT_BACKENDS", 1),
        DEFAULT_EXAGGERATION=setting("DEFAULT_EXAGGERATION", 0.5),
        DEFAULT_CFG_WEIGHT=setting("DEFAULT_CFG_WEIGHT", 0.5),
    )


def _enabled_backends(names: Sequence[str]) -> list[str]:
    available = [name for name in names if name in BACKENDS]
    for name in names:
        if name not in BACKENDS:
            _engine_log.warning("Warning: unknown backend '%s' in ENABLED_BACKENDS, ignoring.", name)
    if not available:
        raise RuntimeError("No known backends in config.ENABLED_BACKENDS.")
    return available


def _active_voice(scenario: Scenario) -> str | None:
    """The voice of this run's newest take on the resident model (its conditioning is loaded)."""
    if scenario.data != "base" or not scenario.this_session:
        return None
    voice = next((take.voice for take in SEED_TAKES if take.days_ago == 0 and take.backend == scenario.resident),
                 None)
    if voice is None or f"cache/{scenario.resident}/{voice}.pt" in scenario.missing:
        return None
    return voice


def _is_wide(char: str) -> bool:
    """Kana, CJK and full-width characters, which take longer to say."""
    return "　" <= char <= "鿿" or "＀" <= char <= "￯"


def check_fake_script(text: str, backend: str) -> None:
    """Stand-in for the live tokenizer in headless models. Approximate input IDs, independent of speech length."""
    wide = sum(_is_wide(char) for char in text)
    count = round((len(text) - wide) * 0.60 + wide * 1.42)
    limit = BACKENDS[backend].max_text_tokens
    if count > limit:
        raise ScriptTooLong(count, limit)


def _take_plan(text: str, backend: str) -> _TakePlan:
    """How long the fake take of `text` is: about 0.06 s per character, 0.177 s for a wide one."""
    seconds = sum(0.177 if _is_wide(char) else 0.06 for char in text)
    tokens = max(MIN_TOKENS, round(seconds * TOKENS_PER_SECOND))
    audio_s = min(tokens, BACKENDS[backend].max_tokens) / TOKENS_PER_SECOND
    return _TakePlan(tokens=tokens, audio_s=audio_s,
                     made_s=round(audio_s * MODEL_SPECS[backend].made_per_audio_s, 1))


# ---------- seeding ----------

@dataclass(frozen=True)
class _SeedClock:
    """Seed times: a mockup clock time on a day counted back from today, in local time."""

    today: date

    def at(self, days_ago: int, hms: str) -> datetime:
        day = self.today - timedelta(days=days_ago)
        hour, minute, second = (int(part) for part in hms.split(":"))
        return datetime(day.year, day.month, day.day, hour, minute, second).astimezone()

    def local(self, days_ago: int, hms: str) -> str:
        """Local ISO-8601 with offset (takes, load history, session ids)."""
        return self.at(days_ago, hms).isoformat(timespec="seconds")

    def utc(self, days_ago: int, hms: str) -> str:
        """UTC ISO-8601, as the voice registry stores its dates."""
        return self.at(days_ago, hms).astimezone(timezone.utc).isoformat()


def _seed(scenario: Scenario, config: SimpleNamespace) -> str | None:
    """Write the scenario's files under config.ROOT; returns the session id of this run's takes, if any."""
    root = Path(config.ROOT)
    _prepare_root(root)
    for folder in (config.CACHE_DIR, config.VOICES_DIR, config.OUTPUTS_DIR / "takes"):
        Path(folder).mkdir(parents=True, exist_ok=True)
    run_id: str | None = None
    if scenario.data == "firstrun":
        _write_bytes(config.CACHE_DIR / "registry.json", b"{}")
        _place_sample(config.VOICES_DIR / "sample.wav")
    else:
        run_id = _seed_library(scenario, config, _SeedClock(datetime.now().astimezone().date()))
    for relative in scenario.missing:
        with contextlib.suppress(FileNotFoundError):
            (root / relative).unlink()
    _write_bytes(root / SEED_MARKER, json.dumps({"scenario": scenario.name}).encode("utf-8"))
    return run_id if scenario.this_session else None


def _prepare_root(root: Path) -> None:
    """Refuse the app's real data; wipe what an earlier seed wrote; never overwrite data it did not seed."""
    resolved = root.resolve()
    guarded = [_APP_ROOT / name for name in _SEEDED_FOLDERS]
    if (resolved == _APP_ROOT or resolved in _APP_ROOT.parents
            or any(resolved == folder or folder in resolved.parents for folder in guarded)):
        raise ValueError(f"FakeEngine never seeds {root}: the app's real data lives there")
    if (root / SEED_MARKER).is_file():
        for name in _SEEDED_FOLDERS:
            if (root / name).exists():
                shutil.rmtree(root / name)
    elif any((root / relative).exists() for relative in _FOREIGN_DATA):
        raise ValueError(f"FakeEngine: {root} holds data it did not seed; give it an empty folder")
    root.mkdir(parents=True, exist_ok=True)


def _seed_library(scenario: Scenario, config: SimpleNamespace, clock: _SeedClock) -> str:
    """Voices, takes and history of the mockup (M L1215-1261); returns this run's session id."""
    _seed_voices(config, clock, unconfirmed=frozenset(scenario.unconfirmed))
    run_id = clock.local(0, RUN_STARTED)
    _seed_takes(config, clock, sessions={0: run_id, 1: clock.local(1, EARLIER_RUN_STARTED)})
    _seed_history(config, clock, scenario)
    if scenario.forge_files:
        _place_sample(config.VOICES_DIR / "sample.wav")
        _place_audio(config.VOICES_DIR / "short_hello.wav", _short_hello())
    return run_id


def _seed_voices(config: SimpleNamespace, clock: _SeedClock, *, unconfirmed: frozenset[str]) -> None:
    entries: dict[str, dict[str, Any]] = {}
    for voice in SEED_VOICES:
        clip = Path(config.VOICES_DIR) / voice.clip
        _place_audio(clip, _seeded_clip(voice))
        _write_conds(Path(config.CACHE_DIR) / voice.backend / f"{voice.voice_id}.pt", voice.voice_id,
                     voice.backend, clip, voice.exaggeration)
        entries[voice.voice_id] = _registry_entry(voice, clip, clock, consent=voice.voice_id not in unconfirmed)
    _write_bytes(config.CACHE_DIR / "registry.json", json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"))


def _registry_entry(voice: _VoiceSeed, clip: Path, clock: _SeedClock, *, consent: bool) -> dict[str, Any]:
    """The entry the engine writes for a forged voice (cli.py-style without meta when not confirmed)."""
    forged = clock.utc(voice.days_ago, voice.at)
    entry: dict[str, Any] = {
        "backend": voice.backend, "reference_wav": str(clip), "exaggeration": voice.exaggeration,
        "language": voice.language, "created_at": forged, "forged_at": forged,
    }
    if consent:
        entry["meta"] = {"consent": True, "consent_at": forged, "clip_seconds": voice.clip_s,
                         "clip_source": voice.clip}
    return entry


def _seed_takes(config: SimpleNamespace, clock: _SeedClock, *, sessions: Mapping[int, str]) -> None:
    folder = Path(config.OUTPUTS_DIR) / "takes"
    takes: list[Take] = []
    for seed in SEED_TAKES:
        audio, levels = _seeded_take(seed.take_id)
        name = f"{seed.voice}_take{seed.take_id}.wav"
        _place_audio(folder / name, audio)
        takes.append(Take(
            id=seed.take_id, voice=seed.voice, backend=seed.backend, language=seed.language, text=seed.text,
            exaggeration=seed.exaggeration, cfg_weight=seed.cfg_weight,
            seconds=_frames(seed.seconds) / SAMPLE_RATE, gen_seconds=seed.gen_seconds,
            tokens=round(seed.seconds * TOKENS_PER_SECOND), hit_token_limit=False,
            created=clock.local(seed.days_ago, seed.at), session=sessions[seed.days_ago], starred=seed.starred,
            wav=f"outputs/takes/{name}", sample_rate=SAMPLE_RATE, peaks=levels,
        ))
    next_id = max(take.id for take in takes) + 1
    _write_bytes(folder / "index.json", _index_text(takes, next_id).encode("utf-8"))


def _index_text(takes: Sequence[Take], next_id: int) -> str:
    """outputs/takes/index.json, version 1, one take per line (U4a's layout)."""
    rows = []
    for take in takes:
        data = asdict(take)
        del data["missing"]                                      # runtime only
        rows.append("    " + json.dumps(data, ensure_ascii=False, allow_nan=False))
    return '{\n  "version": 1,\n  "next_id": %d,\n  "takes": [\n%s\n  ]\n}\n' % (next_id, ",\n".join(rows))


def _seed_history(config: SimpleNamespace, clock: _SeedClock, scenario: Scenario) -> None:
    rows = [*EARLIER_RUN_HISTORY, *THIS_RUN_HISTORY]
    if not scenario.this_session:
        rows.append(THIS_RUN_CLOSED)
    if scenario.forge_files:
        rows.append(FORGED_EVENT)
    rows.sort(key=lambda row: clock.at(row.days_ago, row.at))
    lines = []
    for row in rows:
        event = LoadEvent(t=clock.local(row.days_ago, row.at), kind=row.kind, backend=row.backend,
                          from_backend=row.from_backend, seconds=row.seconds, gb=row.gb, peak_gb=row.peak_gb,
                          reason=row.reason, cold=row.cold)
        if not scenario.cuda and row.days_ago == 0 and row.kind == "loaded":
            event = replace(event, device="cpu", gb=None)            # this run loaded on the CPU
        lines.append(json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")) + "\n")
    _write_bytes(config.CACHE_DIR / "load_history.jsonl", "".join(lines).encode("utf-8"))


def _write_conds(path: Path, voice_id: str, backend: str, clip: Path, exaggeration: float) -> None:
    """A fake .pt (JSON), written atomically like the backends' save."""
    data = {"fake_conditionals": True, "voice_id": voice_id, "backend": backend, "baked_from": str(clip),
            "exaggeration": exaggeration}
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(lambda output: output.write_bytes(json.dumps(data).encode("utf-8")), path)


def _write_bytes(path: Path, data: bytes) -> None:
    """Write through a unique temp file and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _place_sample(target: Path) -> None:
    """The real voices/reference/sample.wav (read only), else a stand-in with its length and selection."""
    try:
        info = _REAL_SAMPLE.stat()
    except OSError:
        _place_audio(target, _sample_stand_in())
        return
    _place_audio(target, _file_audio(str(_REAL_SAMPLE), info.st_size, info.st_mtime_ns))


def _place_audio(target: Path, audio: _Audio) -> None:
    """Put an audio file at target: a hard link to its copy in the shared cache, else a plain file."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    try:
        _link_shared(audio, target)
    except OSError:                                              # another volume, or no cache: a plain copy
        _write_bytes(target, audio.data)


def _link_shared(audio: _Audio, target: Path) -> None:
    """Hard-link target to the shared copy. A copy that already has every name the file system allows (NTFS:
    1024, reached after a few full test runs, since pytest keeps the last three runs' folders) makes way for a
    fresh copy under the same name; the old file stays alive through the links it has."""
    cached = _cached_copy(audio)
    try:
        os.link(cached, target)
    except OSError as error:
        if not _too_many_links(error):
            raise
        _write_bytes(cached, audio.data)
        os.link(cached, target)


def _too_many_links(error: OSError) -> bool:
    return error.errno == errno.EMLINK or getattr(error, "winerror", None) == _TOO_MANY_LINKS


def _cached_copy(audio: _Audio) -> Path:
    """The shared copy, named by its SHA-256, so many seeded data roots cost one copy on disk.

    A copy whose size no longer matches (something wrote into a seeded file) is written afresh.
    """
    path = Path(tempfile.gettempdir()) / AUDIO_CACHE_DIR / f"{audio.digest[:32]}.wav"
    with contextlib.suppress(FileNotFoundError):
        if path.stat().st_size == len(audio.data):
            return path
    _write_bytes(path, audio.data)
    return path


# ---------- audio (standard library only) ----------

_SYLLABLES = 12
_LEVELS = (0.55, 0.7, 0.85, 1.0)


@dataclass(frozen=True)
class _Audio:
    """A seeded file's bytes and their SHA-256 (its name in the shared cache)."""

    data: bytes
    digest: str


def _audio(data: bytes) -> _Audio:
    return _Audio(data, hashlib.sha256(data).hexdigest())


def _frames(seconds: float, sample_rate: int = SAMPLE_RATE) -> int:
    return round(seconds * sample_rate)


@functools.lru_cache(maxsize=None)
def _seeded_take(take_id: int) -> tuple[_Audio, tuple[int, ...]]:
    """One seeded take and its 230 peaks."""
    seed = next(take for take in SEED_TAKES if take.take_id == take_id)
    pcm = _speech(_frames(seed.seconds), SAMPLE_RATE, 7000 + take_id, TAKE_PEAK)
    return _audio(_wav(pcm, SAMPLE_RATE)), _peaks(pcm)


@functools.lru_cache(maxsize=None)
def _seeded_clip(voice: _VoiceSeed) -> _Audio:
    pcm = _speech(_frames(voice.clip_s), SAMPLE_RATE, zlib.crc32(voice.voice_id.encode("utf-8")), CLIP_PEAK)
    return _audio(_wav(pcm, SAMPLE_RATE))


@functools.lru_cache(maxsize=None)
def _short_hello() -> _Audio:
    """short_hello.wav: 4.2 s, 24 kHz, mono, peak -6.4 dBFS (§6.13; M L1264)."""
    return _audio(_wav(_speech(_frames(4.2), SAMPLE_RATE, 4242, SHORT_HELLO_PEAK), SAMPLE_RATE))


@functools.lru_cache(maxsize=None)
def _sample_stand_in() -> _Audio:
    """When the real sample.wav is gone: 30.03 s at 22.05 kHz whose selection ends at a pause at 13.42 s."""
    rate = 22_050
    head = _speech(_frames(13.42, rate), rate, 1, SAMPLE_PEAK, end_loud=True)
    gap = bytes(2 * _frames(0.6, rate))
    tail = _speech(_frames(30.03, rate) - (len(head) + len(gap)) // 2, rate, 2, SAMPLE_PEAK)
    return _audio(_wav(head + gap + tail, rate))


@functools.lru_cache(maxsize=4)
def _file_audio(path: str, size: int, mtime_ns: int) -> _Audio:
    """A file's bytes, read once per version of it (size and mtime are part of the key)."""
    return _audio(Path(path).read_bytes())


def _speech(frames: int, sample_rate: int, seed: int, peak: float, *, end_loud: bool = False) -> bytes:
    """Mono PCM_16 speech-like audio: syllables at varying levels with short gaps (none of 200 ms).

    The first syllable is at full level, so the clip peaks at `peak`; with end_loud the audio never
    ends in a gap (a pause that follows starts exactly at the end).
    """
    rng = random.Random(seed)
    want = 2 * frames
    tail = 2 * _frames(0.04, sample_rate)
    out = bytearray()
    level = 1.0
    while len(out) < want:
        out += _blob(sample_rate, rng.randrange(_SYLLABLES), round(level * peak, 6))
        level = rng.choice(_LEVELS)
        gap_s = rng.uniform(0.08, 0.12) if rng.random() < 0.3 else rng.uniform(0.015, 0.05)
        gap = 2 * _frames(gap_s, sample_rate)
        if end_loud and len(out) + gap + tail >= want:
            continue
        out += bytes(gap)
    return bytes(out[:want])


@functools.lru_cache(maxsize=1024)
def _blob(sample_rate: int, index: int, amplitude: float) -> bytes:
    """One syllable as little-endian PCM_16 at `amplitude` (0..1 of full scale)."""
    scale = amplitude * 32767
    return _le_bytes(array("h", [round(value * scale) for value in _syllable_bank(sample_rate)[index]]))


@functools.lru_cache(maxsize=None)
def _syllable_bank(sample_rate: int) -> tuple[tuple[float, ...], ...]:
    rng = random.Random(sample_rate)
    return tuple(_syllable(rng, sample_rate) for _ in range(_SYLLABLES))


def _syllable(rng: random.Random, sample_rate: int) -> tuple[float, ...]:
    """A voiced syllable: three harmonics of one pitch plus breath, raised-cosine edges, peak 1."""
    size = round(sample_rate * rng.uniform(0.11, 0.26))
    step = 2 * math.pi * rng.uniform(105.0, 190.0) / sample_rate
    rise, fall = max(1, size // 5), max(1, size * 2 // 5)
    values = []
    for i in range(size):
        edge = min(1.0, i / rise, (size - i) / fall)
        tone = math.sin(step * i) + 0.45 * math.sin(2 * step * i + 0.7) + 0.2 * math.sin(3 * step * i + 1.9)
        values.append((0.5 - 0.5 * math.cos(math.pi * edge)) * (tone + rng.uniform(-0.15, 0.15)))
    top = max(abs(value) for value in values)
    return tuple(value / top for value in values)


def _le_bytes(samples: array) -> bytes:
    if sys.byteorder == "big":
        samples = array(samples.typecode, samples)
        samples.byteswap()
    return samples.tobytes()


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    """A RIFF WAVE file around mono PCM_16 samples."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(sample_rate)
        sink.writeframes(pcm)
    return buffer.getvalue()


def _peaks(pcm: bytes, bins: int = 230) -> tuple[int, ...]:
    """round(100 * max |x|) per bin, as the takes store measures a take (engine.audio.peaks' bins)."""
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    total = len(samples)
    return tuple(round(100 * max(map(abs, samples[b * total // bins:(b + 1) * total // bins]), default=0) / 32768)
                 for b in range(bins))
