"""ForgeController (§5.9, §6.9): the Forge draft, its clip pipeline, the name rules, Table F and the result row.

The draft (core.forge_draft) is process-wide next to Session, so a page reload keeps it, and this controller
owns every write to it; the core only marks `forged` when a forge of the draft's name finishes. Writes from
the page's inputs are refused while any job runs (the inputs are disabled then); prefill and "Use
sample.wav" are refused while a forge runs (§5.8). Every draft change bumps draft.rev, which voids the replace
arm (D-F3).

A picked, dropped or prefilled clip is probed and staged in a worker (ClipService.stage_file) while the draft
reads "reading"; the newest pick wins. A replaced clip is discarded once no job runs (a switch running then
may still forge from it). The Forge button follows Table F
(first match wins). A model loads only through its named form ("Load Turbo & Forge", "Switch to Turbo &
Forge", or the armed "Click Again to Replace" that carries the same switch token) or through the low-VRAM ask
"Load Anyway" (D1), never from a render or a timer.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakKeyDictionary

from ui.contract.plans import ActionState, AfterForge, ConfirmRequest, ForgeRequest
from ui.contract.records import CheckRow, ClipInfo, ClipMark
from ui.contract.routes import Route
from ui.contract.viewmodels import Bars, ForgeVM, LangItemVM, TextTone
from ui.contract.viewmodels import ForgeController as ForgeControllerProtocol
from ui.core import catalog
from ui.core.catalog import BACKENDS, LANGUAGES, voice_id_error
from ui.core.facade import BUSY, STALE, describe_error, not_loaded
from ui.core.planner import round_half_up
from ui.core.services.settings import CORE

from .confirm import ARM_REPLACE, Subscriptions
from .status import is_unloading, load_progress, load_text

if TYPE_CHECKING:
    from ui.contract.records import VoiceRow
    from ui.core import ForgeDraft
    from ui.core.jobs import StartResult

    from . import BehaviourFactory

__all__ = [
    "BROWSE_TITLE",
    "CLIP_FILTERS",
    "DROP_BUSY",
    "DROP_ONE",
    "ForgeController",
    "NameCheck",
    "channels_word",
    "check_name",
    "clip_bars",
    "clip_checks",
    "clip_marks",
    "clip_meta",
    "clip_problem",
    "dbfs_text",
    "rate_text",
    "selection_pct",
    "suggest_name",
]

MULTILINGUAL, TURBO = catalog.MULTILINGUAL, catalog.TURBO

# ---- copy (M L431-585, L2621-2698, L3037-3093; NEW where the mockup has none) -----------------------------

NO_CLIP = "No clip yet"                                                     # NEW
EMPTY_META = "Browse for a WAV of one speaker, 6–15 s"                      # NEW
CLIP_NOTE = "Identity uses the whole selection · the first 10 s set the timbre"     # M L3044
PLAY_CLIP = "Play the reference clip"                                       # M L440
STOP_CLIP = "Stop the reference clip"                                       # NEW
BROWSE = "Browse…"                                                          # M L446
BROWSE_TITLE = "Pick a reference clip"                                      # NEW (only the --browser dialog shows it)
CLIP_FILTERS = ("Audio clips (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)")       # §6.9
DROP_ONE = "Drop one clip at a time"                                        # NEW
DROP_BUSY = "Busy · try again when the job finishes"                        # NEW
SAMPLE_FILE = "sample.wav"
SAMPLE_NAME = "sample"                                                      # D-F4
SAMPLE_MISSING = "voices/reference/sample.wav is missing"                   # NEW
SILENT = "the clip is silent"                                               # engine.audio.clip_problem
NAME_HINT = "Becomes the file name, for example rin"                        # M L2665
NAME_CHARS = "Letters, digits, _ and - only (it becomes a file name)"       # M L2666
NAME_LENGTH = "64 characters at most (it becomes a file name)"              # NEW
EXPR_NOTE = "Starting value on Speak"                                       # M L3072
EXPR_IGNORED = "Ignored by Turbo"                                           # M L3072
TEST_LINE = "Play Test Line"                                                # M L561
TEST_LINE_RUNNING = "Generating test line…"                                 # NEW
ANOTHER = "Forge Another"                                                   # M L562
FILE_DIALOG_FAILED = "Couldn't open the file dialog: {reason}"              # NEW
# Table F, the Forge button (M L2681-2698 plus the NEW rows 5-7)
BAKING = "Baking conditioning… {seconds:.1f} s"
BUSY_TAKE = "Busy · generating a take"
PICK_CLIP = "Pick a Clip First"
READING = "Reading the Clip…"
PICK_ANOTHER = "Pick Another Clip"
NAME_IT = "Name the Voice"
FIX_NAME = "Fix the Name First"
CONSENT_FIRST = "Confirm Consent First"
REPLACE = "Click Again to Replace"
FORGE_VOICE = "Forge Voice"

MIN_FORGE_S = 1.0                     # shorter clips cannot be forged (engine.audio.MIN_FORGE_S)
TRIM_ABOVE_S = 15.0                   # a longer clip is trimmed to its selection (engine.audio.SELECTION_MAX_S)
IDEAL_MIN_S = 6.0
DULL_BELOW_HZ = 16_000
QUIET_BELOW_DBFS = -30.0
EXPR_MIN, EXPR_MAX = 0.25, 2.0        # the Default Expressiveness range (M L538)
FIXED_EXPR = 0.5                      # what a backend without Expressiveness is sent (TUB L51-56)
MAX_NAME = 64                         # engine.backends.base.MAX_VOICE_ID_LENGTH
RECENT_LANGUAGES = 5
MINUS = "−"                      # the mockup's "−8.4 dBFS"

_WHITESPACE = re.compile(r"\s+")
_NOT_NAME = re.compile(r"[^A-Za-z0-9_-]")

Verdict = Literal["empty", "invalid", "taken", "replace", "new"]

log = logging.getLogger("voicetool.behaviours")

# The newest clip pick per draft: a pick that finishes after a newer one only discards its staged copy.
_PICKS: WeakKeyDictionary[Any, int] = WeakKeyDictionary()


# ---- the name line (§5.9 table, first match wins) --------------------------------------------------------

@dataclass(frozen=True)
class NameCheck:
    """The Identity card's name line, and what it means for the Forge button."""

    message: str
    tone: TextTone
    verdict: Verdict                  # empty, invalid (rows 8-9), taken (row 12), replace (row 13), new
    red: bool = False                 # the input text turns red
    fix: str = ""                     # what "Use <fix>" sets the name to; "" = no Fix button
    existing: VoiceRow | None = None  # the voice the name clashes with or replaces


def suggest_name(name: str) -> str:
    """The Fix button's suggestion: trim, whitespace runs to "-", other characters dropped, 64 at most."""
    return _NOT_NAME.sub("", _WHITESPACE.sub("-", name.strip()))[:MAX_NAME]


def check_name(name: str, backend: str, rows: Sequence[VoiceRow]) -> NameCheck:
    """The name message, its tone and its verdict for a draft on `backend`, from the library rows."""
    if not name:
        return NameCheck(NAME_HINT, "muted", "empty")
    problem = voice_id_error(name)
    if problem is not None:
        text = {"chars": NAME_CHARS, "length": NAME_LENGTH}.get(problem,
                                                                 f"{name} is reserved by Windows · pick another name")
        return NameCheck(text, "bad", "invalid", red=problem == "chars", fix=_valid_suggestion(name))
    folded = name.casefold()
    twin = next((row for row in rows if row.voice_id != name and row.voice_id.casefold() == folded), None)
    if twin is not None:
        other = twin.voice_id
        return NameCheck(f"{name} and {other} would share one file · use {other} or another name", "warn",
                         "invalid", fix=other, existing=twin)
    same = next((row for row in rows if row.voice_id == name), None)
    if same is not None and same.backend != backend:
        text = f"{name} is a {catalog.label(same.backend)} voice · try {name}-{catalog.label(backend).lower()}"
        return NameCheck(text, "warn", "taken", existing=same)
    if same is not None:
        return NameCheck(f"{name} already exists · forging replaces it", "warn", "replace", existing=same)
    return NameCheck(f"cache/{backend}/{name}.pt", "muted", "new")


def _valid_suggestion(name: str) -> str:
    suggestion = suggest_name(name)
    return suggestion if suggestion and suggestion != name and voice_id_error(suggestion) is None else ""


# ---- the clip (§6.9 rows Meta, Marks, Bars, Checks) ------------------------------------------------------

def clip_problem(clip: ClipInfo) -> str | None:
    """Why a staged clip cannot be forged on any backend (silent, under 1 s), or None (row 7)."""
    if clip.silent:
        return SILENT
    if clip.duration_s < MIN_FORGE_S:
        return f"{clip.duration_s:.1f} s is too short to forge"
    return None


def channels_word(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels")


def rate_text(hz: int) -> str:
    """"22.05 kHz", "24 kHz", "44.1 kHz"."""
    return f"{f'{hz / 1000:.2f}'.rstrip('0').rstrip('.')} kHz"


def dbfs_text(dbfs: float) -> str:
    """"−8.4" with the mockup's minus sign; "0.0" at full scale."""
    if not math.isfinite(dbfs):
        return f"{MINUS}∞"
    value = round(dbfs, 1)
    return f"{abs(value):.1f}" if value >= 0 else f"{MINUS}{abs(value):.1f}"


def clip_meta(clip: ClipInfo) -> str:
    """"30.0 s · mono · 22.05 kHz · trimmed to the first 13.4 s" (the trim only over 15 s, M L3038)."""
    text = f"{clip.duration_s:.1f} s · {channels_word(clip.channels)} · {rate_text(clip.samplerate)}"
    if clip.duration_s > TRIM_ABOVE_S:
        text += f" · trimmed to the first {clip.selection_s:.1f} s"
    return text


def clip_marks(clip: ClipInfo, backend: str) -> tuple[ClipMark, ...]:
    """"Prompt 6 s" ("Prompt 15 s" on Turbo) and "Timbre 10 s", each only inside the selection (M L2631)."""
    if clip.duration_s <= 0:
        return ()
    info = BACKENDS.get(backend)
    prompt, timbre = (info.prompt_s, info.timbre_s) if info is not None else (6.0, 10.0)
    marks = ((prompt, f"Prompt {prompt:g} s"), (timbre, f"Timbre {timbre:g} s"))
    return tuple(ClipMark(label, at / clip.duration_s * 100) for at, label in marks if at < clip.selection_s)


def clip_bars(clip: ClipInfo, timbre_s: float = 10.0) -> Bars:
    """One (peak, zone) per window: timbre up to 10 s in the selection, then selection, then outside."""
    count = len(clip.peaks)
    timbre = min(clip.selection_s, timbre_s)
    bars = []
    for index, peak in enumerate(clip.peaks):
        at = (index + 0.5) / count * clip.duration_s
        zone = "timbre" if at <= timbre else "selection" if at <= clip.selection_s else "outside"
        bars.append((round(float(peak), 3), zone))
    return tuple(bars)


def selection_pct(clip: ClipInfo) -> float:
    return clip.selection_s / clip.duration_s * 100 if clip.duration_s > 0 else 100.0


def clip_checks(clip: ClipInfo, backend: str) -> tuple[CheckRow, ...]:
    """The five check rows: Length, Turbo rule, Channels, Sample rate, Peak (M L2650-2662)."""
    return (_length_check(clip), _turbo_check(clip, backend), _channels_check(clip), _rate_check(clip),
            _peak_check(clip))


def _length_check(clip: ClipInfo) -> CheckRow:
    duration = clip.duration_s
    if duration > TRIM_ABOVE_S:
        return CheckRow("Length", f"Selection {clip.selection_s:.1f} s of {duration:.1f} s · ideal 6–15 s")
    if duration < IDEAL_MIN_S:
        return CheckRow("Length", f"{duration:.1f} s · ideal 6–15 s", "adv")
    return CheckRow("Length", f"{duration:.1f} s · ideal")


def _turbo_check(clip: ClipInfo, backend: str) -> CheckRow:
    if clip.selection_s > _turbo_floor():
        return CheckRow("Turbo rule", "Over 5 s")
    if backend == TURBO:
        return CheckRow("Turbo rule", "Turbo needs over 5 s", "bad")
    return CheckRow("Turbo rule", "Too short for Turbo", "adv")


def _channels_check(clip: ClipInfo) -> CheckRow:
    if clip.channels == 1:
        return CheckRow("Channels", "Mono")
    word = "Stereo" if clip.channels == 2 else f"{clip.channels} channels"
    return CheckRow("Channels", f"{word} · mixed to mono")


def _rate_check(clip: ClipInfo) -> CheckRow:
    text = rate_text(clip.samplerate)
    if clip.samplerate < DULL_BELOW_HZ:
        return CheckRow("Sample rate", f"{text} · below 16 kHz, expect a duller voice", "adv")
    return CheckRow("Sample rate", text)


def _peak_check(clip: ClipInfo) -> CheckRow:
    peak = f"{dbfs_text(clip.peak_dbfs)} dBFS"
    if clip.clipped:
        return CheckRow("Peak", f"{peak} · clipping", "adv")
    if clip.peak_dbfs < QUIET_BELOW_DBFS:
        return CheckRow("Peak", f"{peak} · very quiet", "adv")
    return CheckRow("Peak", f"{peak} · no clipping")


def _turbo_floor() -> float:
    """Turbo needs a clip longer than this (BackendInfo.ref_min_exclusive_s, 5.0)."""
    info = BACKENDS.get(TURBO)
    floor = info.ref_min_exclusive_s if info is not None else None
    return floor if floor is not None else 5.0


# ---- the controller -------------------------------------------------------------------------------------

class ForgeController(ForgeControllerProtocol):
    """§5.9: the Forge draft's intents, its clip pipeline, the name rules, Table F and the result row."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self._draft: ForgeDraft = factory.core.forge_draft
        self._playback = factory.playback()
        self.vm = ForgeVM()
        self._lang_open = False
        self._dialog_open = False
        self._closed = False
        self._named: tuple[str, str] | None = None        # (backend, token) the named Forge button carries
        self._test_line: tuple[str, str] | None = None    # (voice, url) of the test line to replay
        self._bars: tuple[str, Bars] | None = None         # (clip id, bars) of the clip shown
        self._retired: list[ClipInfo] = []                 # clips the draft replaced, discarded once idle
        self._subs = Subscriptions()
        events = self._core.events
        self._subs.add(events.changed, self.refresh)
        self._subs.add(events.forged, self._on_forged)
        self._subs.add(self._core.settings.changed, self._on_setting)
        factory.arm.listeners.add(self.refresh)
        self._playback.listeners.add(self._on_playback)
        self.refresh()

    # ---------- the clip ----------

    async def browse(self) -> None:
        """Browse…: the native OPEN dialog (an upload dialog in --browser), then stage the pick."""
        if self._busy() or self._dialog_open:
            return
        self._dialog_open = True
        self.refresh()
        path: Path | None = None
        try:
            path = await self._factory.kit.pick_file(title=BROWSE_TITLE, filters=CLIP_FILTERS,
                                                     start_dir=self._start_dir())
        except Exception as exc:          # a dialog that cannot open must say so, never log an ERROR
            self._core.events.say(FILE_DIALOG_FAILED.format(reason=describe_error(exc)), bad=True)
        finally:
            self._dialog_open = False
            self.refresh()
        if path is None:
            return
        self._remember_folder(Path(path).parent)
        await self.pick_clip(Path(path))

    async def pick_clip(self, path: Path) -> None:
        """Stage a picked or dropped clip in a worker; the draft reads "reading" meanwhile."""
        if self._busy():
            self._core.events.say(DROP_BUSY)
            return
        source = Path(path)
        clips = self._core.clips
        await asyncio.shield(self._read(source.name, lambda: clips.stage_file(source), source=source))

    async def drop_paths(self, paths: Sequence[str]) -> None:
        """A native drop on Forge (full paths): one clip at a time."""
        files = [str(path) for path in paths if str(path).strip()]
        if not files:
            return
        if len(files) > 1:
            self._core.events.say(DROP_ONE)
            return
        await self.pick_clip(Path(files[0]))

    async def upload(self, name: str, data: bytes) -> None:
        """An uploaded clip (--browser): staged from its bytes like a pick."""
        if self._busy():
            self._core.events.say(DROP_BUSY)
            return
        shown = Path(str(name)).name or "upload"
        clips = self._core.clips
        await asyncio.shield(self._read(shown, lambda: clips.stage_bytes(shown, data), source=None))

    async def use_sample(self) -> Route | None:
        """Speak's "Use sample.wav": stage voices\\reference\\sample.wav, name the draft "sample" if unnamed (D-F4)."""
        core = self._core
        if self._forge_locked():
            core.events.say(BUSY, bad=True)
            return None
        sample = core.paths.voices / SAMPLE_FILE
        if not sample.is_file():
            core.events.say(SAMPLE_MISSING, bad=True)
            return None
        if not self._draft.name:
            self._draft.name = SAMPLE_NAME
        self._close_list()
        clips = core.clips
        self._read(sample.name, lambda: clips.stage_file(sample), source=sample)
        return "/forge"

    def prefill(self, *, voice_id: str, mode: Literal["reforge", "copy"],
                clip_path: Path | None = None) -> Route | None:
        """Voices' Re-forge…, Forge on <model>… and Replace…: the draft from a voice, then "/forge"."""
        core = self._core
        if self._forge_locked():
            core.events.say(BUSY, bad=True)
            return None
        row = self._row(voice_id)
        if row is None:
            core.events.say(f"{voice_id} no longer exists", bad=True)
            return None
        target = self._prefill_target(row, mode)
        if target is None:
            return None
        backend, name = target
        draft = self._draft
        draft.name, draft.forged = name, ""
        draft.exaggeration = _clamp_expr(row.exaggeration)
        self._apply_backend(backend, language=row.language)
        self._close_list()
        own = clip_path is None
        consent = row.consent if own else False               # Q3: a new clip can be another speaker
        source = row.reference_path if own else Path(clip_path)
        if source is None:
            self._new_clip(row.reference_name or f"{voice_id}'s clip", source=None, consent=consent, state="failed",
                           error="file not found")
        else:
            clips = core.clips
            self._read(source.name, lambda: clips.stage_file(source), source=source, consent=consent)
        return "/forge"

    def toggle_clip_playback(self) -> None:
        """Play the staged selection from 0, or stop it."""
        _, clip, _ = self._clip_view()
        if clip is None:
            return
        url = self._core.staged_url(clip.clip_id)
        if self._playback.is_playing_url(url):
            self._playback.stop()
        else:
            self._playback.play_url(url)

    # ---------- identity ----------

    def set_name(self, name: str) -> None:
        """Typing in Name: the draft's name; it clears the result row and the replace arm (M L3056)."""
        if self._busy():
            return
        text = "" if name is None else str(name)
        draft = self._draft
        if text == draft.name:
            return
        self._core.jobs.note_activity(draft.backend)
        draft.name, draft.forged = text, ""
        self._changed()

    def fix_name(self) -> None:
        """"Use <suggested>" (or "Use <existing>" on a case clash)."""
        fix = self._check().fix
        if fix:
            self.set_name(fix)

    def set_backend(self, backend: str) -> None:
        """The Model segments: never loads. Multilingual restores its remembered language; others are English."""
        if self._busy() or backend not in self._core.state.engine.enabled:
            return
        self._close_list()
        if backend == self._draft.backend:
            return
        self._apply_backend(backend)
        self._changed()

    def set_language(self, code: str) -> None:
        """A pick in the 23-language list (Multilingual only); remembered as core.forge_language."""
        draft = self._draft
        self._close_list()
        if self._busy() or _english_only(draft.backend) or code not in LANGUAGES or code == draft.language:
            return
        draft.language = code
        self._remember_language(code)
        self._changed()

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open or close the language list (never while busy or for an English-only model)."""
        wanted = (not self._lang_open) if open is None else bool(open)
        if wanted and (self._busy() or _english_only(self._draft.backend)):
            wanted = False
        if wanted != self._lang_open:
            self._lang_open = wanted
            self.vm.lang_open = wanted

    def set_exaggeration(self, value: float) -> None:
        """Default Expressiveness on release: 0.25-2.0, rounded to 0.01 (M L3077); not for Turbo."""
        draft = self._draft
        if self._busy() or not _uses_expr(draft.backend) or not _finite(value):
            return
        rounded = _clamp_expr(value)
        if rounded == draft.exaggeration:
            return
        self._core.jobs.note_activity(draft.backend)
        draft.exaggeration = rounded
        self._changed()

    def set_consent(self, given: bool) -> None:
        """"The speaker agreed to having this voice cloned"."""
        if self._busy() or bool(given) == self._draft.consent:
            return
        self._draft.consent = bool(given)
        self._changed()

    # ---------- the Forge button ----------

    async def forge(self, token: str | None = None) -> ConfirmRequest | None:
        """Table F: arm a replace, ask on low VRAM, start the named switch and forge, or forge on the resident model."""
        core, draft = self._core, self._draft
        state, clip, _ = self._clip_view()
        check = self._check()
        if not self._forge_action(state, clip, check).enabled or clip is None:
            self.refresh()
            return None
        replace = check.verdict == "replace"
        if replace and not self._factory.arm.press(ARM_REPLACE, scope=draft.rev):
            return None                                          # the first press only arms (row 13)
        req = self._request(clip, replace=replace)
        if core.state.engine.resident == req.backend:
            self._report(core.jobs.start_forge(req))
            return None
        plan = core.planner.switch_plan(req.backend, reason="forge", after=AfterForge(req))
        if plan.low_vram:
            return self._factory.models().ask(plan)              # D1: "Load Anyway", then forges
        result = core.jobs.start_forge(req, token=token)
        if result.reason == STALE:                               # a void token loads nothing: redraw
            self._named = None
            self.refresh()
            return None
        self._report(result)
        return None

    async def confirm(self, token: str) -> None:
        """An ask this page raised lives in the shell's switch popover: its primary."""
        await self._factory.models().confirm(token)

    def dismiss(self) -> None:
        self._factory.models().dismiss()

    # ---------- the result row ----------

    def speak_with(self) -> Route | None:
        """"Speak with <name>": pick the voice on Speak and go there; nothing loads."""
        voice = self._draft.forged
        if not voice:
            return None
        if self._row(voice) is None:
            self._core.events.say(f"{voice} no longer exists", bad=True)
            self._draft.forged = ""
            self._changed()
            return None
        self._factory.speak().pick_voice(voice)
        self._core.events.flash.emit(f"voice-{voice}")
        return "/"

    async def test_line(self) -> None:
        """"Play Test Line": one unsaved take of the new voice, played at once; later clicks replay it."""
        voice = self._draft.forged
        if not voice or not self._test_action().enabled:
            return
        cached = self._test_line
        if cached is not None and cached[0] == voice:
            self._playback.play_url(cached[1])
            return
        result = self._core.jobs.start_test_line(voice, on_done=lambda url: self._test_line_ready(voice, url))
        self._report(result)
        self.refresh()

    def forge_another(self) -> None:
        """"Forge Another": name "", consent off, Default Model; the clip stays (now in the library)."""
        if self._core.state.job.kind == "forge":
            return
        draft = self._draft
        draft.forged, draft.name, draft.consent = "", "", False
        self._apply_backend(self._default_backend())
        self._close_list()
        self._changed()

    # ---------- page plumbing ----------

    def opened(self) -> None:
        """The Forge page was built: refresh the library when registry.json changed (U2), then redraw."""
        core = self._core
        core.scheduler.spawn(core.refresh_library(changed_only=True), name="voicetool forge library")
        self.refresh()

    def close(self) -> None:
        """The client went away: drop every subscription and listener. The staged clips the draft replaced are
        discarded now, or when the running job ends (a switch may still forge from one)."""
        self._closed = True
        self._subs.close()
        self._factory.arm.listeners.remove(self.refresh)
        self._playback.listeners.remove(self._on_playback)
        retired, self._retired = self._retired, []
        if retired:
            self._core.jobs.once_idle(lambda: [self._discard(clip) for clip in retired])

    # ---------- the view model ----------

    def refresh(self) -> None:
        """Recompute the whole view model from the draft and the core mirrors."""
        if self._closed:
            return
        core, draft, vm = self._core, self._draft, self.vm
        job = core.state.job
        busy = bool(job.kind)
        self._discard_retired()
        state, clip, error = self._clip_view()
        check = self._check()
        self._show_clip(state, clip, error)
        vm.name, vm.name_msg, vm.name_tone, vm.name_bad = draft.name, check.message, check.tone, check.red
        vm.fix_label = f"Use {check.fix}" if check.fix else ""
        vm.segments = tuple((backend, catalog.label(backend), backend == draft.backend)
                            for backend in core.state.engine.enabled)
        vm.model_note, vm.model_note_tone = self._model_note()
        self._show_language(busy)
        self._show_expr()
        vm.consent = draft.consent
        vm.consent_req_tone = "faint" if draft.consent else "gold-dim"
        vm.inputs_enabled = not busy
        vm.browse = ActionState(enabled=not busy and not self._dialog_open, label=BROWSE,
                                reason=BUSY if busy else "", tone="plain")
        vm.forge = self._forge_action(state, clip, check)
        vm.tracing = job.kind == "forge"
        vm.trace_progress = float(job.fill) if job.kind == "forge" and job.fill is not None else 0.0
        vm.forged = draft.forged
        vm.speak_label = f"Speak with {draft.forged}" if draft.forged else ""
        vm.test_line = self._test_action()
        vm.another = ActionState(True, ANOTHER, tone="plain")
        self._show_play(clip)

    def _clip_view(self) -> tuple[str, ClipInfo | None, str]:
        """(state, the usable clip or None, the failure reason); a ready clip that cannot be forged is failed."""
        draft = self._draft
        if draft.clip_state == "ready" and draft.clip is not None:
            problem = clip_problem(draft.clip)
            return ("failed", None, problem) if problem else ("ready", draft.clip, "")
        if draft.clip_state in ("reading", "failed"):
            return draft.clip_state, None, draft.clip_error
        return "empty", None, ""

    def _show_clip(self, state: str, clip: ClipInfo | None, error: str) -> None:
        draft, vm = self._draft, self.vm
        vm.clip_state = state
        aria = ""
        if clip is None:
            shown = draft.clip_file or "the clip"
            vm.clip_file = NO_CLIP if state == "empty" else shown
            vm.clip_meta = {"reading": f"Reading {shown}…", "failed": f"Can't read {shown} · {error}"}.get(
                state, EMPTY_META)
            vm.clip_meta_tone = "bad" if state == "failed" else "muted"
            vm.bars, vm.marks, vm.sel_pct, vm.clip_note, vm.checks = (), (), 100.0, "", ()
        else:
            vm.clip_file, vm.clip_meta, vm.clip_meta_tone = clip.name, clip_meta(clip), "muted"
            vm.bars = self._bars_of(clip)
            vm.marks = clip_marks(clip, draft.backend)
            vm.sel_pct = selection_pct(clip)
            vm.clip_note = CLIP_NOTE
            vm.checks = clip_checks(clip, draft.backend)
            aria = f"Waveform of {clip.name}, {clip.selection_s:.1f} s selected"     # M L3039
        vm.bars_aria = aria

    def _bars_of(self, clip: ClipInfo) -> Bars:
        if self._bars is None or self._bars[0] != clip.clip_id:
            info = BACKENDS.get(self._draft.backend)
            self._bars = (clip.clip_id, clip_bars(clip, info.timbre_s if info is not None else 10.0))
        return self._bars[1]

    def _show_play(self, clip: ClipInfo | None) -> None:
        playing = clip is not None and self._playback.is_playing_url(self._core.staged_url(clip.clip_id))
        self.vm.playing = playing
        self.vm.play = ActionState(enabled=clip is not None, label=STOP_CLIP if playing else PLAY_CLIP, tone="plain")

    def _model_note(self) -> tuple[str, TextTone]:
        """"Multilingual is resident · forging takes about 2 s" / "Forging loads Turbo · unloads … (~9 s)"."""
        core, backend = self._core, self._draft.backend
        engine = core.state.engine
        name = catalog.label(backend)
        if engine.resident == backend:
            bake = core.history.bake_estimate(backend, engine.device or None)
            if bake:
                return f"{name} is resident · forging takes about {max(1, round_half_up(bake))} s", "muted"
            return f"{name} is resident", "muted"
        about = core.planner.load_estimate(backend).about_s
        text = f"Forging loads {name}" + (f" · unloads {catalog.label(engine.resident)}" if engine.resident else "")
        return text + (f" (~{max(1, round_half_up(about))} s)" if about else ""), "gold-dim"

    def _show_language(self, busy: bool) -> None:
        draft, vm = self._draft, self.vm
        locked = _english_only(draft.backend)
        if locked or busy:
            self._lang_open = False
        vm.lang_locked = locked
        vm.language_name = "English" if locked else LANGUAGES.get(draft.language, draft.language)
        vm.lang_open = self._lang_open
        vm.lang_items = () if locked else tuple(
            LangItemVM(code, LANGUAGES[code], code == draft.language, "") for code in self._language_order())

    def _language_order(self) -> tuple[str, ...]:
        """Recent languages first (the remembered one, then those of the newest Multilingual voices), then A-Z."""
        rows = sorted((row for row in self._core.state.library.rows if row.backend == MULTILINGUAL),
                      key=lambda row: row.created or "", reverse=True)
        recent: list[str] = []
        for code in (self._remembered_language(), *(row.language for row in rows)):
            if code in LANGUAGES and code not in recent and len(recent) < RECENT_LANGUAGES:
                recent.append(code)
        rest = sorted((code for code in LANGUAGES if code not in recent), key=lambda code: LANGUAGES[code])
        return (*recent, *rest)

    def _show_expr(self) -> None:
        draft, vm = self._draft, self.vm
        applies = _uses_expr(draft.backend)
        vm.expr = draft.exaggeration
        vm.expr_text = f"{draft.exaggeration:.2f}" if applies else "—"
        vm.expr_enabled = applies
        vm.expr_note = EXPR_NOTE if applies else EXPR_IGNORED

    def _forge_action(self, state: str, clip: ClipInfo | None, check: NameCheck) -> ActionState:
        """Table F (first match wins)."""
        busy = self._busy_action()
        if busy is not None:
            return busy                                                       # rows 1-4
        if state != "ready" or clip is None:
            return _grey({"empty": PICK_CLIP, "reading": READING}.get(state, PICK_ANOTHER))   # rows 5-7
        if check.verdict == "empty":
            return _grey(NAME_IT)                                             # row 8
        if check.verdict == "invalid":
            return _grey(FIX_NAME)                                            # row 9
        draft = self._draft
        if draft.backend == TURBO and clip.selection_s <= _turbo_floor():
            return _grey(f"Clip is {clip.duration_s:.1f} s; Turbo needs over {_turbo_floor():g} s")   # row 10
        if not draft.consent:
            return _grey(CONSENT_FIRST)                                       # row 11
        if check.verdict == "taken" and check.existing is not None:
            return _grey(f"Name Taken on {catalog.label(check.existing.backend)}")   # row 12
        named = None if self._core.state.engine.resident == draft.backend else self._named_request(draft.backend)
        if check.verdict == "replace" and self._armed():
            return ActionState(True, REPLACE, pending=named, armed=True)      # row 13, named while not resident
        if named is not None and named.switch is not None:
            return ActionState(True, named.switch.label, pending=named)       # rows 14-15
        return ActionState(True, FORGE_VOICE)                                 # row 16

    def _busy_action(self) -> ActionState | None:
        """Rows 1-4: a bake, an unload, a load (the shared progress copy), a take or a test line."""
        core = self._core
        job = core.state.job
        if job.kind == "forge":
            return ActionState(False, BAKING.format(seconds=job.elapsed_s), fill=job.fill,
                               indeterminate=job.fill is None)
        if is_unloading(job):
            return ActionState(False, f"Unloading {catalog.label(job.source)}…", fill=0.0)
        progress = load_progress(core)
        if progress is not None:
            return ActionState(False, load_text(progress, "button"), fill=progress.fill,
                               indeterminate=progress.fill is None)
        return _grey(BUSY_TAKE) if job.kind else None

    def _named_request(self, backend: str) -> ConfirmRequest:
        """The named switch the button carries: one token per backend, reused while it stays valid."""
        core = self._core
        plan = core.planner.switch_plan(backend, reason="forge")
        token = self._named[1] if self._named is not None and self._named[0] == backend else ""
        known = core.tokens.peek(token) if token else None
        if known is None or known.to_backend != backend:
            token = core.tokens.mint(plan)
            self._named = (backend, token)
        return ConfirmRequest(token=token, kind="switch_model", style="named", switch=plan)

    def _test_action(self) -> ActionState:
        core = self._core
        job = core.state.job
        if job.kind == "generate" and job.take_no == 0:
            return ActionState(False, TEST_LINE_RUNNING, tone="plain")
        if job.kind:
            return ActionState(False, TEST_LINE, reason=BUSY, tone="plain")
        row = self._row(self._draft.forged)
        if row is not None and core.state.engine.resident != row.backend:
            return ActionState(False, TEST_LINE, tooltip=not_loaded(row.backend), tone="plain")
        return ActionState(True, TEST_LINE, tone="plain")

    # ---------- the draft ----------

    def _read(self, name: str, stage: Callable[[], ClipInfo], *, source: Path | None,
              consent: bool | None = None) -> asyncio.Task[None]:
        """Start staging a new clip: the draft reads "reading" now, the worker's result lands later."""
        serial = self._new_clip(name, source=source, consent=consent, state="reading")
        return self._core.scheduler.spawn(self._stage(serial, name, stage), name="voicetool forge clip")

    def _new_clip(self, name: str, *, source: Path | None, consent: bool | None, state: str,
                  error: str = "") -> int:
        """Replace the draft's clip by a new one in `state`; consent survives only the same file (Q3)."""
        draft = self._draft
        serial = _PICKS.get(draft, 0) + 1
        _PICKS[draft] = serial
        old = draft.clip
        same = old is not None and _same_file(old.source, source)
        draft.clip, draft.clip_state, draft.clip_file, draft.clip_error = None, state, name, error
        draft.forged = ""
        draft.consent = (draft.consent and same) if consent is None else bool(consent)
        if old is not None:
            self._retired.append(old)
        self._changed()
        return serial

    async def _stage(self, serial: int, name: str, stage: Callable[[], ClipInfo]) -> None:
        try:
            clip = await asyncio.to_thread(stage)
        except Exception as exc:          # an unreadable clip is a draft state, not an error log
            self._staged(serial, None, name, describe_error(exc) or "unreadable")
            return
        self._staged(serial, clip, name, "")

    def _staged(self, serial: int, clip: ClipInfo | None, name: str, reason: str) -> None:
        """A worker finished: the newest pick lands in the draft, a stale one is only discarded."""
        draft = self._draft
        if _PICKS.get(draft) != serial:
            if clip is not None:
                self._discard(clip)
            return
        problem = clip_problem(clip) if clip is not None else reason
        if clip is not None and problem:
            self._discard(clip)
        draft.clip = clip if not problem else None
        draft.clip_state = "failed" if problem else "ready"
        draft.clip_file = clip.name if clip is not None else name
        draft.clip_error = problem or ""
        self._changed()

    def _discard(self, clip: ClipInfo) -> None:
        clips = self._core.clips
        self._core.scheduler.spawn(asyncio.to_thread(clips.discard, clip), name="voicetool discard clip")

    def _discard_retired(self) -> None:
        """Discard the clips the draft replaced, once no job runs (a running switch may still forge from one)."""
        if self._retired and not self._busy():
            retired, self._retired = self._retired, []
            for clip in retired:
                self._discard(clip)

    def _apply_backend(self, backend: str, *, language: str | None = None) -> None:
        """Set the draft's model: English-only models speak English; Multilingual gets `language`, else keeps
        its own language when it stays Multilingual, else the remembered one (core.forge_language)."""
        draft = self._draft
        staying = draft.backend == backend
        if draft.backend == MULTILINGUAL and not staying:
            self._remember_language(draft.language)
        draft.backend = backend
        if _english_only(backend):
            draft.language = "en"
        elif language in LANGUAGES:
            draft.language = str(language)
        elif not staying or draft.language not in LANGUAGES:
            draft.language = self._remembered_language()

    def _prefill_target(self, row: VoiceRow, mode: str) -> tuple[str, str] | None:
        """(backend, name) for a re-forge or a copy (Turbo ↔ Multilingual, M L1898-1903); None when refused."""
        if mode == "reforge":
            backend, name = row.backend, row.voice_id
        elif mode == "copy":
            backend = MULTILINGUAL if row.backend == TURBO else TURBO
            name = f"{row.voice_id}-{catalog.label(backend).lower()}"
        else:
            return None
        if backend not in self._core.state.engine.enabled:
            self._core.events.say(f"{catalog.label(backend)} is not enabled", bad=True)
            return None
        return backend, name

    def _request(self, clip: ClipInfo, *, replace: bool) -> ForgeRequest:
        """The immutable snapshot a forge runs with (English and 0.5 where the model ignores them)."""
        draft = self._draft
        backend = draft.backend
        return ForgeRequest(voice_id=draft.name, clip=clip, backend=backend,
                            language="en" if _english_only(backend) else draft.language,
                            exaggeration=_clamp_expr(draft.exaggeration) if _uses_expr(backend) else FIXED_EXPR,
                            consent=draft.consent is True, replace=replace)

    def _changed(self) -> None:
        """A draft change: bump its revision, void the replace arm, tell every client, redraw."""
        self._draft.rev += 1
        self._factory.arm.disarm(ARM_REPLACE)
        self._core.events.notify()
        self.refresh()

    # ---------- small reads ----------

    def _check(self) -> NameCheck:
        draft = self._draft
        return check_name(draft.name, draft.backend, self._core.state.library.rows)

    def _armed(self) -> bool:
        arm = self._factory.arm
        return arm.is_armed(ARM_REPLACE) and arm.scope(ARM_REPLACE) == self._draft.rev

    def _busy(self) -> bool:
        return bool(self._core.state.job.kind)

    def _forge_locked(self) -> bool:
        """A forge job runs: its request is immutable, so the draft waits (prefill, "Use sample.wav", §5.8)."""
        return self._core.state.job.kind == "forge"

    def _row(self, voice_id: str) -> VoiceRow | None:
        if not voice_id:
            return None
        return next((row for row in self._core.state.library.rows if row.voice_id == voice_id), None)

    def _default_backend(self) -> str:
        """Settings › Default Model for New Voices, when enabled; else the first enabled backend."""
        enabled = self._core.state.engine.enabled
        backend = str(self._core.settings.get(CORE, "default_backend"))
        return backend if backend in enabled or not enabled else enabled[0]

    def _remembered_language(self) -> str:
        code = str(self._core.settings.get(CORE, "forge_language"))
        return code if code in LANGUAGES else "en"

    def _remember_language(self, code: str) -> None:
        if code in LANGUAGES and code != self._core.settings.get(CORE, "forge_language"):
            self._set_setting("forge_language", code)

    def _start_dir(self) -> Path | None:
        """The last Browse folder, else voices\\reference (VOICES_DIR)."""
        stored = str(self._core.settings.get(CORE, "browse_dir") or "")
        if stored and os.path.isdir(stored):
            return Path(stored)
        voices = self._core.paths.voices
        return voices if voices.is_dir() else None

    def _remember_folder(self, folder: Path) -> None:
        if not self._factory.browser:                 # an upload's scratch folder is gone after the run
            self._set_setting("browse_dir", os.path.abspath(folder))

    def _set_setting(self, key: str, value: Any) -> None:
        try:
            self._core.settings.set(CORE, key, value)
        except (KeyError, ValueError) as exc:
            log.warning("Refused the setting %s.%s: %s", CORE, key, exc)

    def _report(self, result: StartResult) -> None:
        """Toast a start result's reason, red when refused ("stale" only means: redraw)."""
        if result.reason and result.reason != STALE:
            self._core.events.say(result.reason, bad=result.status == "refused")

    def _close_list(self) -> None:
        if self._lang_open:
            self._lang_open = False
            self.vm.lang_open = False

    # ---------- events ----------

    def _test_line_ready(self, voice: str, url: str) -> None:
        self._test_line = (voice, url)
        if not self._closed:
            self._playback.play_url(url)

    def _on_forged(self, voice_id: str) -> None:
        if self._test_line is not None and self._test_line[0] == voice_id:
            self._test_line = None                    # a new forge of that name: its old test line is stale

    def _on_setting(self, section: str, key: str) -> None:
        """Settings › Default Model re-picks an idle draft without a result (M L3238-3241)."""
        if section != CORE or key not in ("default_backend", "forge_language"):
            return
        if key == "default_backend" and not self._forge_locked() and not self._draft.forged:
            backend = self._default_backend()
            if backend != self._draft.backend:
                self._apply_backend(backend)
                self._changed()
                return
        self.refresh()

    def _on_playback(self) -> None:
        if not self._closed:
            _, clip, _ = self._clip_view()
            self._show_play(clip)


# ---- helpers ----------------------------------------------------------------------------------------------

def _english_only(backend: str) -> bool:
    info = BACKENDS.get(backend)
    return info is not None and info.english_only


def _uses_expr(backend: str) -> bool:
    info = BACKENDS.get(backend)
    return info is None or info.uses_exaggeration


def _grey(label: str) -> ActionState:
    return ActionState(False, label, tone="grey")


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp_expr(value: float) -> float:
    number = float(value) if _finite(value) else FIXED_EXPR
    return round(min(EXPR_MAX, max(EXPR_MIN, number)), 2)


def _same_file(first: Path | None, second: Path | None) -> bool:
    if first is None or second is None:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))
