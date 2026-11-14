"""SpeakController (§5.7): the Speak page's voice pick, delivery, script, Generate and its view model (§6.7).

The controller owns SpeakVM and recomputes it whenever the core changes (core.events.changed), a setting
changes (the key hints), the client's arms change (Clear) or the player reports (the Take card). The draft
itself lives in the process-wide Session; the language list's open state belongs to this client.

Nothing here loads a model by itself. Generate builds immutable request snapshots (D2) and hands them to the
job runner: queued behind a running take or a switch toward the same backend, started at once on the resident
backend, or, with another backend needed, started only with the named token the Generate button carries (the
button's label names the switch). A low-VRAM plan always becomes the switch popover (D1), raised through
ModelsController.ask() with the takes as its after-action ("Then generates take 8", D20).

Flashes after a search jump and the enter animation of a new take are time-limited view-model values: a row
carries a non-zero flash number for 1.2 s, so a page that is built during that time plays the flash once and a
later rebuild does not replay it.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.plans import ActionState, AfterGenerate, ConfirmRequest, GenerateRequest, SwitchPlan
from ui.contract.viewmodels import GeneratingRowVM, LangItemVM, SpeakVM, TakeCardVM, TakeRowVM, TextTone
from ui.contract.viewmodels import SpeakController as SpeakControllerProtocol
from ui.contract.viewmodels import VoiceGroupVM, VoiceItemVM, WaveVM
from ui.core import catalog
from ui.core.catalog import LANGUAGES
from ui.core.facade import STALE
from ui.core.planner import round_half_up
from ui.core.services.estimates import TakeEstimate, estimate_take
from ui.core.services.settings import folder_value
from ui.core.services.voices import created_tip

from .confirm import ARM_CLEAR, Subscriptions
from .playback import NO_TIME
from .status import app_gb, is_unloading, load_progress, load_text, row_state

if TYPE_CHECKING:
    from ui.contract.records import Take, VoiceRow
    from ui.contract.viewmodels import ChipTone
    from ui.core import Core
    from ui.core.clock import Handle, Scheduler
    from ui.core.jobs import StartResult

    from . import BehaviourFactory

__all__ = [
    "CLEAR",
    "CLEAR_ARMED",
    "CUT_TAKE",
    "CUT_WARNING",
    "EXPR_RANGE",
    "FLASH_S",
    "GUID_RANGE",
    "NEW_ROW_S",
    "SESSION_ROWS",
    "SpeakController",
    "from_utf16",
    "has_kana",
    "sentence_count",
    "snap",
    "stats_text",
    "to_utf16",
]

EXPR_RANGE = (0.25, 2.0)                  # Expressiveness (M L213)
GUID_RANGE = (0.0, 1.0)                   # Guidance (M L224)
STEP = 0.05
EXPR_HIGH = 1.5                           # above this the value turns amber (M L2950)
SAME = 0.001                              # the mockup's tolerance for "the same value" (M L2501, L2508)
FLASH_S = 1.2                             # em-flash (emiya-voice.css L36-37)
NEW_ROW_S = 1.5                           # a new take's row stays "new" (M L1656)
SESSION_ROWS = 200                        # "Takes · This Session" lists the newest 200, one Takes page (R3-1)
DASH = "—"

GENERATE = "Generate"
GENERATE_3 = "Generate 3 Takes"
PICK_A_VOICE = "Pick a Voice"
FORGE_FIRST = "Forge a Voice First"
TYPE_A_SCRIPT = "Type a Script"
FORGING = "Forging…"
BUSY_TEST_LINE = "Busy · test line"                                    # NEW (§6.7 Table G)
PLAY, PAUSE, SAVE_AS = "Play", "Pause", "Save As…"
CLEAR, CLEAR_ARMED = "Clear", "Click Again to Clear"                   # M L2981
NOTE_DEFAULT = "Applies to the next take."                             # M L2509-2514
NOTE_PICK = "Pick a voice to shape its delivery."
NOTE_FORGE = "Forge a voice to shape its delivery."
NOTE_TURBO = "Turbo ignores Expressiveness and Guidance. Shape delivery with tags."
SUMMARY_PICK = "Pick a voice to speak with."                           # M L2558
SUMMARY_FORGE = "Forge a voice first, then type what it should say."
EXPR_TIP = "How much emotion the voice puts into a line"               # M L2951
EXPR_TIP_HIGH = "High values speed delivery up; lower Guidance to compensate"
FIX_JAPANESE = "Text looks Japanese · Use Japanese"                    # M L2977
ENGLISH_ONLY_CHIP = "English only · use a Multilingual voice"          # M L2530
MATCHES_TEXT = " · matches text"
NO_PT = "no .pt · re-forge"                                            # NEW
NOT_ENABLED = "Not enabled"                                            # NEW
REGISTRY_FAILED = "Couldn't read cache/registry.json · see Log"        # NEW
VOICE_DEFAULT = "voice default"
CUT_WARNING = " · may be cut at 40 s"                                  # NEW (D16)
CUT_TAKE = " · cut at 40 s"                                            # NEW (D16); the theme adds its tooltip
MISSING = "Missing"
SAMPLE = "sample.wav"

_KANA = re.compile("[぀-ヿ]")       # hiragana and katakana (M L1420-1422)
_SENTENCES = re.compile("[。！？.!?]+")       # runs of sentence ends (M L2519)

LibraryShape = Literal["loading", "empty", "ready", "failed", "corrupt"]


# ---------- pure helpers (shared with the tests) ----------

def has_kana(text: str) -> bool:
    """The script looks Japanese: it holds hiragana or katakana (U+3040-U+30FF)."""
    return bool(_KANA.search(text or ""))


def sentence_count(text: str) -> int:
    """Runs of 。！？.!?; a non-empty text without any counts as one sentence; "" has none."""
    if not text:
        return 0
    return len(_SENTENCES.findall(text)) or 1


def stats_text(text: str, estimate: TakeEstimate | None) -> tuple[str, str]:
    """("26 chars · 3 sentences · ≈ 7 s to generate", " · may be cut at 40 s" or ""); ("Empty", "") for no text."""
    if not text:
        return "Empty", ""
    sentences = sentence_count(text)
    parts = [f"{len(text)} chars", f"{sentences} sentence" + ("" if sentences == 1 else "s")]
    if estimate is None:
        return " · ".join(parts), ""
    parts.append(f"≈ {_whole(estimate.made_s)} s to generate")
    return " · ".join(parts), CUT_WARNING if estimate.may_be_cut else ""


def snap(value: Any, low: float, high: float) -> float | None:
    """value clamped to low..high on the 0.05 grid (two decimals); None when it is not a finite number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    number = min(high, max(low, number))
    return round(round(number / STEP) * STEP, 2)


def to_utf16(text: str, index: int) -> int:
    """A Python index into text as a browser caret (UTF-16 code units)."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text[:max(0, index)])


def from_utf16(text: str, caret: int) -> int:
    """A browser caret (UTF-16 code units) as a Python index into text, clamped to the text."""
    units = 0
    for index, char in enumerate(text):
        if units >= caret:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _whole(seconds: float) -> int:
    """An estimate in whole seconds, at least 1 (M L2520)."""
    return max(1, round_half_up(seconds))


def _same(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < SAME


def _multilingual(backend: str) -> bool:
    """The one backend with 23 languages; Standard and Turbo read English only (engine.backends.info)."""
    return backend == catalog.MULTILINGUAL


def _turbo(backend: str) -> bool:
    """Turbo ignores Expressiveness and Guidance and understands the tags (TUB L87-113)."""
    return backend == catalog.TURBO


def _language_name(code: str) -> str:
    return LANGUAGES.get(str(code).lower(), str(code))


def _knobs(exaggeration: float | None, cfg_weight: float | None) -> str:
    """"Expr 0.50 · Guid 0.50"; Turbo (None) "Expr — · Guid —" (M L2575)."""
    if exaggeration is None or cfg_weight is None:
        return f"Expr {DASH} · Guid {DASH}"
    return f"Expr {exaggeration:.2f} · Guid {cfg_weight:.2f}"


def _number(value: float | None) -> str:
    return DASH if value is None else f"{value:.2f}"


def _bars(peaks: tuple[int, ...]) -> tuple[float, ...]:
    """The take's 230 stored peaks as 0..1, normalised to the take's own maximum."""
    top = max(peaks, default=0)
    if top <= 0:
        return tuple(0.0 for _ in peaks)
    return tuple(round(peak / top, 3) for peak in peaks)


def _clock_time(iso: str) -> str:
    """"14:31" for a local ISO time; "" when it cannot be read."""
    try:
        at = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return f"{at.hour:02d}:{at.minute:02d}"


# ---------- view-model pieces the contract leaves to the page controller ----------

@dataclass(frozen=True)
class _Draft:
    """What one recompute reads: the usable selected voice, the script and its estimate."""

    row: VoiceRow | None              # the selected voice when it exists on an enabled backend
    has_voices: bool
    loading: bool                     # no library snapshot yet: never show first-run copy
    text: str
    kana: bool
    estimate: TakeEstimate | None


@dataclass
class _Flashes:
    """Live flash numbers by key: non-zero for `seconds` after a flash, then 0 again."""

    scheduler: Scheduler
    on_change: Callable[[], None]
    seconds: float = FLASH_S
    live: dict[Hashable, int] = field(default_factory=dict)
    timers: dict[Hashable, Handle] = field(default_factory=dict)
    count: int = 0

    def start(self, key: Hashable) -> None:
        self.count += 1
        number = self.count
        self.live[key] = number
        old = self.timers.pop(key, None)
        if old is not None:
            old.cancel()

        def expire() -> None:
            if self.live.get(key) == number:
                del self.live[key]
                self.timers.pop(key, None)
                self.on_change()

        self.timers[key] = self.scheduler.later(self.seconds, expire)
        self.on_change()

    def get(self, key: Hashable) -> int:
        return self.live.get(key, 0)

    def close(self) -> None:
        for handle in self.timers.values():
            handle.cancel()
        self.timers.clear()
        self.live.clear()


# ---------- the controller ----------

class SpeakController(SpeakControllerProtocol):
    """The Speak page (§5.7): voice pick, delivery, script, Generate, and SpeakVM (§6.7)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core: Core = factory.core
        self.vm = SpeakVM()
        self._playback = factory.playback()
        self._commands = factory.commands()
        self._lang_open = False
        self._token = ""                          # the named Generate's token, reused while still valid
        self._chain: list[GenerateRequest] = []   # the takes this client handed to the runner, in order
        self._voice_flash = _Flashes(self._core.scheduler, self.refresh)
        self._take_flash = _Flashes(self._core.scheduler, self.refresh)
        self._new_rows = _Flashes(self._core.scheduler, self.refresh, seconds=NEW_ROW_S)
        self._rows_key: tuple[Any, ...] | None = None    # what the session rows were built from
        self._rows: tuple[TakeRowVM, ...] = ()
        self._new_seq = 0
        self._spark_seq = 0
        self._subs = Subscriptions()
        events = self._core.events
        self._subs.add(events.changed, self.refresh)
        self._subs.add(events.flash, self._on_flash)
        self._subs.add(events.spark, self._on_spark)
        self._subs.add(events.take_ready, self._on_take_ready)
        self._subs.add(self._core.settings.changed, self._on_setting)
        factory.arm.listeners.add(self.refresh)
        self._playback.listeners.add(self.refresh)
        self.refresh()

    # ---------- voice and delivery ----------

    def pick_voice(self, voice_id: str) -> None:
        """Select a voice with its own language and Expressiveness, Guidance 0.5; loads nothing (M L1696-1700)."""
        row = self._usable(voice_id)
        if row is None:
            return
        session = self._core.session
        session.voice_id = row.voice_id
        session.language = row.language if _multilingual(row.backend) else "en"
        session.exaggeration = float(row.exaggeration)
        session.cfg_weight = float(catalog.DEFAULT_CFG_WEIGHT)
        self._lang_open = False
        self._changed()

    def set_delivery(self, *, exaggeration: float | None = None, cfg_weight: float | None = None,
                     language: str | None = None) -> None:
        """Expressiveness 0.25-2 and Guidance 0-1 on the 0.05 grid, or one of the 23 languages (Multilingual)."""
        row = self._voice()
        if row is None or _turbo(row.backend):
            return
        session = self._core.session
        if exaggeration is not None:
            value = snap(exaggeration, *EXPR_RANGE)
            if value is not None:
                self._core.jobs.note_activity(row.backend)
                session.exaggeration = value
        if cfg_weight is not None:
            value = snap(cfg_weight, *GUID_RANGE)
            if value is not None:
                self._core.jobs.note_activity(row.backend)
                session.cfg_weight = value
        if language is not None:
            code = str(language).lower()
            if _multilingual(row.backend) and code in LANGUAGES:
                session.language = code
            self._lang_open = False
        self._changed()

    def apply_preset(self, name: Literal["Default", "Slower", "Dramatic"]) -> None:
        """Default (0.5, 0.5), Slower (0.5, 0.3) or Dramatic (0.7, 0.3) (M L1207-1211)."""
        preset = next((p for p in catalog.PRESETS if p.name == name), None)
        row = self._voice()
        if preset is None or row is None or _turbo(row.backend):
            return
        session = self._core.session
        session.exaggeration, session.cfg_weight = float(preset.exaggeration), float(preset.cfg_weight)
        self._changed()

    def reset_delivery(self) -> None:
        """The voice's own Expressiveness and language, Guidance 0.5 (M L1714-1717)."""
        row = self._voice()
        if row is None:
            return
        session = self._core.session
        session.exaggeration = float(row.exaggeration)
        session.cfg_weight = float(catalog.DEFAULT_CFG_WEIGHT)
        session.language = row.language if _multilingual(row.backend) else "en"
        self._changed()

    def set_guidance_zero(self) -> None:
        """Guidance 0, offered while the language differs from the clip's and Guidance is above 0 (M L2967)."""
        row = self._voice()
        session = self._core.session
        if row is None or not self._mismatch(row) or session.cfg_weight <= 0:
            return
        session.cfg_weight = 0.0
        self._changed()

    def use_japanese(self) -> None:
        """Japanese for a Multilingual voice whose script holds kana (M L2532, L2978)."""
        row = self._voice()
        session = self._core.session
        if (row is None or not _multilingual(row.backend) or not has_kana(session.text)
                or session.language.lower() == "ja"):
            return
        session.language = "ja"
        self._changed()

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open or close the language list; it opens only for a Multilingual voice."""
        row = self._voice()
        wanted = (not self._lang_open) if open is None else bool(open)
        wanted = wanted and row is not None and _multilingual(row.backend)
        if wanted != self._lang_open:
            self._lang_open = wanted
            self.refresh()

    # ---------- the script ----------

    def set_text(self, text: str) -> None:
        """The script as typed (Session.text); typing disarms Clear (M L2987)."""
        value = text if isinstance(text, str) else ""
        self._factory.arm.disarm(ARM_CLEAR)
        if self._core.session.text != value:
            row = self._voice()
            if row is not None:
                self._core.jobs.note_activity(row.backend)
            self._core.session.text = value
            self._core.events.notify()
        self.refresh()

    def insert_tag(self, tag: str, caret: int) -> int:
        """Insert a Turbo tag at the caret with a space on either side where none is (M L1727-1738).

        caret and the returned caret are browser carets (UTF-16 code units); a caret past the end inserts at
        the end. Nothing changes unless a Turbo voice is selected and the tag is one of its five.
        """
        text = self._core.session.text
        at = from_utf16(text, caret if isinstance(caret, int) and not isinstance(caret, bool) else len(text) * 2)
        row = self._voice()
        if row is None or not _turbo(row.backend) or tag not in catalog.TAGS:
            return to_utf16(text, at)
        before, after = text[:at], text[at:]
        pre = " " if before and not before[-1].isspace() else ""
        post = " " if after and not after[0].isspace() else ""
        head = before + pre + tag + post
        self._factory.arm.disarm(ARM_CLEAR)
        self._core.session.text = head + after
        self._changed()
        return to_utf16(head, len(head))

    def clear_text(self) -> ConfirmRequest | None:
        """Clear: the first press arms "Click Again to Clear" for 3 s, a second press empties the script."""
        session = self._core.session
        if not session.text:
            return None
        if self._factory.arm.press(ARM_CLEAR):
            session.text = ""
            self._changed()
            return None
        return ConfirmRequest(token="", kind="clear_script", style="arm", primary=CLEAR_ARMED, danger=True)

    # ---------- Generate ----------

    async def generate(self, n: int = 1, token: str | None = None) -> ConfirmRequest | None:
        """n takes of the script with this delivery: queue, start, switch with the named token, or ask.

        Returns the switch popover's request when VRAM is low (the ask "Load Anyway"), else None. A click whose
        token is void loads nothing: the label is recomputed instead.
        """
        draft = self._draft()
        row = draft.row
        if row is None or self._blocked(draft):
            return None
        core = self._core
        reqs = core.call.capture(tuple(self._request(row) for _ in range(max(1, int(n)))),
                                 switch=core.session.call_switch)
        if core.jobs.busy or core.state.engine.resident == row.backend:
            self._started(core.jobs.start_generate(reqs), reqs)
            return None
        plan = core.planner.switch_plan(row.backend, reason="generate", after=AfterGenerate(reqs))
        if plan.low_vram:
            return self._ask(plan)
        known = core.tokens.peek(token) if token else None
        if known is None or known.to_backend != row.backend:
            self.refresh()
            return None
        result = core.jobs.start_generate(reqs, token=token)
        if result.status == "refused" and result.reason == STALE:
            fresh = core.planner.switch_plan(row.backend, reason="generate", after=AfterGenerate(reqs))
            if fresh.low_vram:
                return self._ask(fresh)
            self.refresh()
            return None
        self._started(result, reqs)
        return None

    def stop_take(self) -> None:
        self._core.jobs.stop_take(drop_queue=self._core.settings.get("core", "stop_drops_queue"))

    def set_call_switch(self, on: bool) -> None:
        self._core.session.call_switch = bool(on)

    def cancel_queue(self) -> None:
        """Drop the takes waiting behind the running one (the running take finishes)."""
        core = self._core
        job = core.state.job
        waiting = core.jobs.queued
        if not waiting or job.kind != "generate" or job.take_no <= 0:
            return
        core.jobs.cancel_queue()
        dropped = {id(req) for req in waiting}
        self._chain = [req for req in self._chain if id(req) not in dropped]
        self.refresh()

    # ---------- the popover (Confirmable) ----------

    async def confirm(self, token: str) -> None:
        """The switch popover's primary; ModelsController runs every ask, the ones Generate raises included."""
        await self._factory.models().confirm(token)

    def dismiss(self) -> None:
        self._factory.models().dismiss()

    # ---------- the page ----------

    def opened(self) -> None:
        """The Speak page opened: read the registry again if it changed outside the app (cli.py)."""
        core = self._core
        core.scheduler.spawn(core.refresh_library(changed_only=True), name="voicetool speak library")

    def close(self) -> None:
        """Drop every subscription and timer (BehaviourFactory.close)."""
        self._subs.close()
        self._factory.arm.listeners.remove(self.refresh)
        self._playback.listeners.remove(self.refresh)
        for flashes in (self._voice_flash, self._take_flash, self._new_rows):
            flashes.close()

    # ---------- recompute ----------

    def refresh(self) -> None:
        """Recompute SpeakVM from the mirrors, the Session and this client's state."""
        draft = self._draft()
        self._library(draft)
        self._delivery(draft)
        self._script(draft)
        self._buttons(draft)
        self._summary(draft)
        self._take_card()
        self._session_list()

    def _draft(self) -> _Draft:
        core = self._core
        library = core.state.library
        text = core.session.text
        row = self._voice()
        estimate = estimate_take(core.state.takes.takes, row.backend, text) if row is not None else None
        return _Draft(row=row, has_voices=bool(library.rows), loading=library.status == "loading", text=text,
                      kana=has_kana(text), estimate=estimate)

    def _usable(self, voice_id: str) -> VoiceRow | None:
        """The library row of voice_id when its backend is enabled (else it cannot speak, E L110-113)."""
        if not voice_id:
            return None
        row = next((r for r in self._core.state.library.rows if r.voice_id == voice_id), None)
        if row is None or not row.backend_enabled or row.backend not in self._core.state.engine.enabled:
            return None
        return row

    def _voice(self) -> VoiceRow | None:
        return self._usable(self._core.session.voice_id)

    def _mismatch(self, row: VoiceRow) -> bool:
        """A Multilingual voice speaking another language than its clip's (M L2514)."""
        return _multilingual(row.backend) and self._core.session.language.lower() != row.language.lower()

    def _changed(self) -> None:
        self._core.events.notify()
        self.refresh()

    # ---------- the Voice panel ----------

    def _library(self, draft: _Draft) -> None:
        vm, library = self.vm, self._core.state.library
        state, message = _library_shape(library.status, bool(library.rows))
        count = len(library.rows)
        vm.library_message = message
        vm.library_state = state
        vm.groups = self._groups(draft)
        vm.count_text = f"{count} voice" if count == 1 else f"{count} voices"
        sample = self._sample_tip() if state in ("empty", "corrupt") else ""
        vm.sample_tip = sample                       # before show_sample: the page reads both on its change
        vm.show_sample = not sample

    def _sample_tip(self) -> str:
        """"" when voices/reference/sample.wav is there, else why "Use sample.wav" is disabled (NEW)."""
        core = self._core
        path = core.paths.voices / SAMPLE
        return "" if path.is_file() else f"{folder_value(path, core.paths.root)} is missing"

    def _groups(self, draft: _Draft) -> tuple[VoiceGroupVM, ...]:
        by_backend: dict[str, list[VoiceRow]] = {}
        for row in self._core.state.library.rows:
            by_backend.setdefault(row.backend, []).append(row)
        order = sorted(by_backend, key=_backend_rank)
        return tuple(self._group(backend, by_backend[backend], draft) for backend in order)

    def _group(self, backend: str, rows: list[VoiceRow], draft: _Draft) -> VoiceGroupVM:
        enabled = backend in self._core.state.engine.enabled and all(r.backend_enabled for r in rows)
        chip, tone = self._group_chip(backend) if enabled else (NOT_ENABLED, "off")
        items = tuple(self._item(row, enabled, draft) for row in rows)
        return VoiceGroupVM(backend=backend, label=catalog.label(backend), chip=chip, chip_tone=tone,
                            enabled=enabled, rows=items)

    def _group_chip(self, backend: str) -> tuple[str, ChipTone]:
        """Resident, Freeing…, Loading · 7 s, Failed or Not loaded · ~14 s (M L2486-2494)."""
        core = self._core
        state = row_state(core, backend)
        if state == "resident":
            return "Resident", "gold"
        if state == "unloading":
            return "Freeing…", "gold"
        if state == "loading":
            progress = load_progress(core)
            return f"Loading · {progress.n if progress is not None else 1} s", "gold"
        if state == "failed":
            return "Failed", "red"
        about = core.planner.load_estimate(backend).about_s
        return (f"Not loaded · ~{round_half_up(about)} s" if about else "Not loaded"), "off"

    def _item(self, row: VoiceRow, enabled: bool, draft: _Draft) -> VoiceItemVM:
        selected = draft.row is not None and draft.row.voice_id == row.voice_id
        tone: TextTone = "gold-dim" if selected else "muted"
        if not row.pt_exists:
            sub, tone = NO_PT, "warn"
        else:
            sub = f"{row.language_name} · " + ("tags" if _turbo(row.backend) else f"{row.exaggeration:.2f}")
        return VoiceItemVM(voice_id=row.voice_id, sub=sub, sub_tone=tone, selected=selected,
                           flash_seq=self._voice_flash.get(row.voice_id),
                           audition_label=f"Audition the reference clip of {row.voice_id}", enabled=enabled)

    # ---------- Delivery ----------

    def _delivery(self, draft: _Draft) -> None:
        vm, session, row = self.vm, self._core.session, draft.row
        on = row is not None and not _turbo(row.backend)
        high = on and session.exaggeration > EXPR_HIGH
        vm.delivery_enabled = on
        vm.presets = tuple((p.name, on and _same(session.exaggeration, p.exaggeration)
                            and _same(session.cfg_weight, p.cfg_weight)) for p in catalog.PRESETS)
        vm.expr, vm.guid = float(session.exaggeration), float(session.cfg_weight)
        vm.expr_text = f"{session.exaggeration:.2f}" if on else DASH
        vm.expr_tone = "warn" if high else "text"
        vm.expr_tip = EXPR_TIP_HIGH if high else EXPR_TIP
        vm.expr_tick = float(row.exaggeration) if on and row is not None else None
        vm.guid_text = f"{session.cfg_weight:.2f}" if on else DASH
        mode = "disabled" if row is None else "pick" if _multilingual(row.backend) else "locked"
        if mode != "pick":
            self._lang_open = False
        vm.language_mode = mode
        vm.language_name = "English" if mode == "locked" else _language_name(session.language)
        vm.lang_items = self._lang_items(row) if mode == "pick" and row is not None else ()
        vm.lang_open = self._lang_open
        vm.show_reset = self._differs(row)
        vm.note, vm.note_tone, vm.show_guid0 = self._note(draft)

    def _lang_items(self, row: VoiceRow) -> tuple[LangItemVM, ...]:
        """The 23 languages: the voice's own, the other registered voices' by name, then the rest by name."""
        own = row.language.lower()
        used = {r.language.lower() for r in self._core.state.library.rows} & set(LANGUAGES)
        first = [own] if own in LANGUAGES else []
        others = sorted(used - {own}, key=LANGUAGES.__getitem__)
        rest = sorted(set(LANGUAGES) - used - {own}, key=LANGUAGES.__getitem__)
        current = self._core.session.language.lower()
        return tuple(LangItemVM(code=code, name=LANGUAGES[code], selected=code == current,
                                mark=VOICE_DEFAULT if code == own else "") for code in (*first, *others, *rest))

    def _differs(self, row: VoiceRow | None) -> bool:
        """Reset shows when a non-Turbo voice's values differ from its own (M L2508)."""
        if row is None or _turbo(row.backend):
            return False
        session = self._core.session
        if not _same(session.exaggeration, row.exaggeration) or not _same(session.cfg_weight,
                                                                          catalog.DEFAULT_CFG_WEIGHT):
            return True
        return self._mismatch(row)

    def _note(self, draft: _Draft) -> tuple[str, TextTone, bool]:
        row = draft.row
        if row is None:
            return (NOTE_PICK if draft.has_voices or draft.loading else NOTE_FORGE), "muted", False
        if _turbo(row.backend):
            return NOTE_TURBO, "muted", False
        if self._mismatch(row):
            text = f"The clip of {row.voice_id} is {row.language_name}; Guidance 0 reduces accent carry-over."
            return text, "gold-dim", self._core.session.cfg_weight > 0
        return NOTE_DEFAULT, "muted", False

    # ---------- the Script card ----------

    def _script(self, draft: _Draft) -> None:
        vm, row = self.vm, draft.row
        vm.stats_text, vm.stats_warn = stats_text(draft.text, draft.estimate)
        mode, chip = self._toolbar(draft)
        vm.toolbar_mode, vm.lang_chip_text = mode, chip
        vm.tags = tuple(catalog.TAGS) if mode == "tags" else ()
        armed = self._factory.arm.is_armed(ARM_CLEAR)
        vm.clear = ActionState(enabled=bool(draft.text), label=CLEAR_ARMED if armed else CLEAR, tone="plain",
                               armed=armed)
        vm.text_lang = "en" if row is not None and not _multilingual(row.backend) else self._core.session.language

    def _toolbar(self, draft: _Draft) -> tuple[str, str]:
        """The toolbar's mode and chip text (M L2522-2538; D3 for Turbo)."""
        row = draft.row
        if row is None:
            return "none", ""
        if not _multilingual(row.backend):
            if draft.kana:
                return "bad", ENGLISH_ONLY_CHIP
            return ("tags", "") if _turbo(row.backend) else ("chip", "English")
        language = self._core.session.language.lower()
        if draft.kana and language != "ja":
            return "fix", FIX_JAPANESE
        return "chip", _language_name(language) + (MATCHES_TEXT if draft.kana else "")

    # ---------- Generate, Generate 3 Takes, Cancel Queue (Table G) ----------

    def _blocked(self, draft: _Draft) -> str:
        """Why no take of this draft can start or queue now ("" when one can)."""
        row = draft.row
        job = self._core.state.job
        if row is None:
            return "no voice"
        if job.kind == "forge":
            return "forging"
        if job.kind == "generate" and job.take_no <= 0:
            return "test line"
        if not draft.text.strip():
            return "no script"
        if draft.kana and not _multilingual(row.backend):
            return "English only"
        if not row.pt_exists:
            return "no .pt"
        return ""

    def _buttons(self, draft: _Draft) -> None:
        vm, job = self.vm, self._core.state.job
        plan = self._named_plan(draft)
        pending = self._pending(plan)
        vm.generate = self._generate_state(draft, plan, pending)
        vm.generate3 = self._generate3_state(draft, plan, pending)
        vm.show_cancel_queue = job.kind == "generate" and job.take_no > 0 and job.queued > 0

    def _named_plan(self, draft: _Draft) -> SwitchPlan | None:
        """The switch a click would start (Table G rows 12-13): idle, a usable draft, its backend not resident."""
        core, row = self._core, draft.row
        if row is None or self._blocked(draft) or core.state.job.kind or core.state.engine.resident == row.backend:
            return None
        return core.planner.switch_plan(row.backend, reason="generate")

    def _pending(self, plan: SwitchPlan | None) -> ConfirmRequest | None:
        """The named request on Generate: one token per plan, minted again only once it is void."""
        if plan is None:
            return None
        tokens = self._core.tokens
        known = tokens.peek(self._token) if self._token else None
        if known is None or known.to_backend != plan.to_backend or known.label != plan.label:
            self._token = tokens.mint(plan)
        return ConfirmRequest(token=self._token, kind="switch_model", style="named", switch=plan)

    def _generate_state(self, draft: _Draft, plan: SwitchPlan | None, pending: ConfirmRequest | None) -> ActionState:
        """Table G: the first matching row wins (M L2541-2556)."""
        core, row, job = self._core, draft.row, self._core.state.job
        if row is None:
            return _grey(FORGE_FIRST if not draft.has_voices and not draft.loading else PICK_A_VOICE)
        if is_unloading(job):
            return ActionState(False, f"Unloading {catalog.label(job.source)}…", fill=0.0)
        progress = load_progress(core)
        if progress is not None:
            return ActionState(False, load_text(progress, "button"), fill=progress.fill,
                               indeterminate=progress.fill is None)
        if job.kind == "generate" and job.take_no > 0:
            queued = f" · {job.queued} queued" if job.queued else ""
            return ActionState(False, f"Generating · {job.elapsed_s:.1f} s{queued}", fill=job.fill,
                               indeterminate=job.fill is None)
        if job.kind == "generate":
            return _grey(BUSY_TEST_LINE)
        if job.kind == "forge":
            return _grey(FORGING)
        blocked = self._blocked(draft)
        if blocked == "no script":
            return _grey(TYPE_A_SCRIPT)
        if blocked == "English only":
            return _grey(f"{catalog.label(row.backend)} Is English Only")
        if blocked == "no .pt":
            return _grey(f"Re-forge {row.voice_id} First")
        hint = self._commands.hint_parts("gen")
        if plan is not None:
            return ActionState(True, plan.label, pending=pending, hint=hint)
        return ActionState(True, GENERATE, hint=hint)

    def _generate3_state(self, draft: _Draft, plan: SwitchPlan | None,
                         pending: ConfirmRequest | None) -> ActionState:
        """Enabled while a take runs or a switch loads (it queues); named by its tooltip and aria-label (D18)."""
        if self._blocked(draft):
            return ActionState(False, GENERATE_3, tone="plain")
        if plan is None:
            return ActionState(True, GENERATE_3, tone="plain")
        name = catalog.label(plan.to_backend)
        named = f"Switch to {name} & Generate 3 Takes" if plan.from_backend else f"Load {name} & Generate 3 Takes"
        return ActionState(True, GENERATE_3, pending=pending, aria=named, tooltip=named, tone="plain")

    # ---------- the summary line (Table S) ----------

    def _summary(self, draft: _Draft) -> None:
        vm, core, row = self.vm, self._core, draft.row
        if row is None:
            vm.summary_text = SUMMARY_FORGE if not draft.has_voices and not draft.loading else SUMMARY_PICK
            vm.summary_tone = "muted"
            return
        label = catalog.label(row.backend)
        tone: TextTone = "muted"
        if draft.kana and not _multilingual(row.backend):
            text, tone = f"{label} reads English only · pick a Multilingual voice for Japanese", "bad"
        elif not row.pt_exists:
            text = f"{folder_value(row.pt, core.paths.root)} is missing · re-forge {row.voice_id} on Voices"
            tone = "bad"
        elif not core.state.job.kind and core.state.engine.resident != row.backend:
            text, tone = self._plan_summary(row, draft), "gold-dim"
        else:
            text = self._voice_summary(row)
        vm.summary_text, vm.summary_tone = text, tone

    def _voice_summary(self, row: VoiceRow) -> str:
        """"aiko · Multilingual (resident) · Japanese · Expr 0.50 · Guid 0.50 · conditioning cached"."""
        core, session = self._core, self._core.session
        resident = " (resident)" if core.state.engine.resident == row.backend else ""
        language = _language_name(session.language) if _multilingual(row.backend) else "English"
        parts = [row.voice_id, catalog.label(row.backend) + resident, language]
        if _turbo(row.backend):
            parts.append("tags on")
        else:
            parts += [f"Expr {session.exaggeration:.2f}", f"Guid {session.cfg_weight:.2f}"]
        if row.pt_exists:
            parts.append("conditioning cached")
        return " · ".join(parts)

    def _plan_summary(self, row: VoiceRow, draft: _Draft) -> str:
        """"Unloads Multilingual (3.4 GB) · loads Turbo, about 9 s · then ≈ 3 s" (parts drop without history)."""
        core = self._core
        about = core.planner.load_estimate(row.backend).about_s
        loads = catalog.label(row.backend) + (f", about {round_half_up(about)} s" if about else "")
        then = f" · then ≈ {_whole(draft.estimate.made_s)} s" if draft.estimate is not None else ""
        resident = core.state.engine.resident
        if not resident:
            return f"Loads {loads}{then}"
        size = app_gb(core)
        unloads = catalog.label(resident) + (f" ({size} GB)" if size else "")
        return f"Unloads {unloads} · loads {loads}{then}"

    # ---------- the Take card ----------

    def _take_card(self) -> None:
        core, playback, job = self._core, self._playback, self._core.state.job
        taking = job.kind == "generate" and job.take_no > 0
        visible = bool(core.state.takes.takes) or taking
        take = playback.current()
        if playback.card_generating():
            card = self._generating_card()
        elif take is not None:
            card = self._card_for(take, pinned=taking)
        else:
            card = TakeCardVM(visible=visible)
        self.vm.card = card

    def _generating_card(self) -> TakeCardVM:
        """Take 8 generating: the trace, its caption from the running request (M L2576)."""
        job = self._core.state.job
        req = self._running()
        caption = f"Generating · {_request_caption(req)}" if req is not None else f"Generating · {job.voice}"
        wave = WaveVM(generating=True, fill=job.fill, new_seq=self._new_seq, spark_seq=self._spark_seq)
        return TakeCardVM(visible=True, generating=True, title=f"Take {job.take_no}", caption=caption, wave=wave,
                          play=ActionState(False, PLAY, tone="plain"), time_text=NO_TIME,
                          save=ActionState(False, SAVE_AS, tone="plain"))

    def _card_for(self, take: Take, *, pinned: bool) -> TakeCardVM:
        """The current take: "Take 7" and "aiko · Japanese · Expr 0.50 · Guid 0.50 · 4.6 s · made in 6.9 s"."""
        playback, session = self._playback, self._core.session
        caption = (f"{take.voice} · {_language_name(take.language)} · {_knobs(take.exaggeration, take.cfg_weight)}"
                   f" · {take.seconds:.1f} s · made in {take.gen_seconds:.1f} s")
        wave = WaveVM(take_id=take.id, bars=_bars(take.peaks), length_s=float(take.seconds),
                      new_seq=self._new_seq, spark_seq=self._spark_seq)
        both = bool(session.pin_a and session.pin_b)
        playing = playback.is_playing_take(take.id)
        return TakeCardVM(
            visible=True, generating=False, pinned=pinned, title=f"Take {take.id}", caption=caption,
            caption_warn=CUT_TAKE if take.hit_token_limit else "", star_visible=True, starred=take.starred,
            wave=wave, play=ActionState(True, PAUSE if playing else PLAY, tone="plain"),
            time_text=playback.time_text(take.id), ab_visible=both,
            ab_a_label=f"A · Take {session.pin_a}" if both else "",
            ab_b_label=f"B · Take {session.pin_b}" if both else "",
            ab_current=_ab_side(session.take_id, session.pin_a, session.pin_b) if both else "",
            save=ActionState(True, SAVE_AS, tone="plain"))

    def _running(self) -> GenerateRequest | None:
        """The take being generated, among the requests this client handed over (by identity), else None."""
        chain = self._chain
        if not chain:
            return None
        queued = self._core.jobs.queued
        if not queued:
            return chain[-1]
        head = queued[0]
        index = next((i for i, req in enumerate(chain) if req is head), -1)
        if index <= 0:
            return None
        del chain[:index - 1]                      # the takes before the running one are done
        return chain[0]

    def _started(self, result: StartResult, reqs: tuple[GenerateRequest, ...]) -> None:
        """Remember the takes the runner took (for the generating caption) and toast its reason."""
        if result.status != "refused":
            taken = list(reqs[:len(reqs) - result.dropped])
            if result.status == "started":
                self._chain = taken
            else:
                self._chain.extend(taken)
        if result.reason and result.reason != STALE:
            self._core.events.say(result.reason, bad=result.status == "refused")
        self.refresh()

    def _ask(self, plan: SwitchPlan) -> ConfirmRequest:
        """A low-VRAM named Generate: the popover with "Load Anyway" and "Then generates take 8" (D1, D20)."""
        after = plan.after
        if isinstance(after, AfterGenerate):
            self._chain = list(after.reqs)
        return self._factory.models().ask(plan)

    def _request(self, row: VoiceRow) -> GenerateRequest:
        """One take's snapshot of the draft (D2): Turbo sends no Expressiveness or Guidance, English-only "en"."""
        session = self._core.session
        turbo = _turbo(row.backend)
        return GenerateRequest(voice_id=row.voice_id, backend=row.backend, text=session.text,
                               language=session.language if _multilingual(row.backend) else "en",
                               exaggeration=None if turbo else float(session.exaggeration),
                               cfg_weight=None if turbo else float(session.cfg_weight))

    # ---------- "Takes · This Session" ----------

    def _session_list(self) -> None:
        """The newest SESSION_ROWS takes of this run (R3-1: the box is 186 px; "All Takes ›" lists every take),
        rebuilt only when the takes, the pins, the current take or a row's flash change (refresh() also runs on
        every 0.2 s progress tick), and the "Generating…" row."""
        vm, core, playback, job = self.vm, self._core, self._playback, self._core.state.job
        on_card = not playback.card_generating()
        session, mirror = core.session, core.state.takes
        key = (mirror.rev, id(mirror.takes), session.take_id, session.pin_a, session.pin_b, on_card,
               tuple(self._new_rows.live.items()), tuple(self._take_flash.live.items()))
        if key != self._rows_key:
            self._rows_key = key
            current = session.take_id
            self._rows = tuple(self._row(take, current=on_card and take.id == current)
                               for take in playback.session_takes()[:SESSION_ROWS])
        rows = self._rows
        generating = None
        if job.kind == "generate" and job.take_no > 0:
            req = self._running()
            generating = GeneratingRowVM(
                take_no=job.take_no, voice=req.voice_id if req is not None else job.voice,
                expr_text=_number(req.exaggeration) if req is not None else DASH,
                guid_text=_number(req.cfg_weight) if req is not None else DASH)
        vm.session_rows = rows
        vm.generating_row = generating
        vm.session_visible = bool(rows) or generating is not None

    def _row(self, take: Take, *, current: bool) -> TakeRowVM:
        session = self._core.session
        return TakeRowVM(
            id=take.id, text=take.text, lang=take.language or "en", voice=take.voice,
            model_label=catalog.label(take.backend), expr_text=_number(take.exaggeration),
            guid_text=_number(take.cfg_weight), length_text=MISSING if take.missing else f"{take.seconds:.1f} s",
            when_text=_clock_time(take.created), when_tip=created_tip(take.created), current=current,
            pin_a=session.pin_a == take.id, pin_b=session.pin_b == take.id, starred=take.starred,
            missing=take.missing, is_new=bool(self._new_rows.get(take.id)), flash_seq=self._take_flash.get(take.id))

    # ---------- events ----------

    def _on_flash(self, key: str) -> None:
        if key.startswith("voice-"):
            self._voice_flash.start(key.removeprefix("voice-"))
        elif key.startswith("take-") and key.removeprefix("take-").isdigit():
            self._take_flash.start(int(key.removeprefix("take-")))

    def _on_spark(self, key: str) -> None:
        if key == "take":
            self._spark_seq += 1
            self.refresh()

    def _on_take_ready(self, take_id: int) -> None:
        self._new_seq += 1
        self._new_rows.start(take_id)

    def _on_setting(self, section: str, key: str) -> None:
        self.refresh()


# ---------- module helpers ----------

def _library_shape(status: str, has_rows: bool) -> tuple[LibraryShape, str]:
    """SpeakVM.library_state and its message from LibraryState.status (§6.7 "Other states")."""
    if status == "loading":
        return "loading", ""
    if status == "failed":
        return "failed", REGISTRY_FAILED
    if has_rows:
        return "ready", ""
    return ("corrupt" if status == "corrupt" else "empty"), ""


def _backend_rank(backend: str) -> tuple[int, str]:
    order = catalog.ORDER
    return (order.index(backend), "") if backend in order else (len(order), backend)


def _grey(label: str) -> ActionState:
    return ActionState(False, label, tone="grey")


def _request_caption(req: GenerateRequest) -> str:
    """"aiko · Japanese · Expr 0.50 · Guid 0.50" for a generating take (M L2576)."""
    return f"{req.voice_id} · {_language_name(req.language)} · {_knobs(req.exaggeration, req.cfg_weight)}"


def _ab_side(take_id: int, pin_a: int, pin_b: int) -> str:
    """"A" or "B" when the current take is that pin, else ""."""
    return "A" if take_id == pin_a else "B" if take_id == pin_b else ""
