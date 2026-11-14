"""TakesController (§5.10): the Takes page's list, filters, compare card, Remove Unstarred, Export and Show Folder.

The list is the takes store's published tuple (TakesState) seen through the header tab ("This Session" is this
run's takes, D14), the search (applied 150 ms after typing stops), the voice and model menus (D-T2) and a cap of
200 rows with "Show N more" (S03 risk 14). A row click plays its take; on the current take it toggles play
(D-T3). Stars, pins and A/B belong to PlaybackController, which the page calls directly; the compare card follows
the pins A and B (Session), and its note also checks the text (D-T4).

Remove Unstarred is an arm (§5.1). The first click captures every unstarred take of every session (Q10) and the
label states how many; any takes change, a tab change or a page change voids it (D-T6); a second click within
3 s sends exactly the captured takes that are still unstarred to the Recycle Bin through the injected trash.
Export Starred… and Show Folder go through the KitPort. Nothing here loads a model.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
from collections.abc import Awaitable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.plans import ActionState, ConfirmRequest
from ui.contract.viewmodels import CompareRowVM, CompareSideVM, CompareVM, TakeRowVM, TakesVM
from ui.contract.viewmodels import TakesController as TakesControllerProtocol
from ui.core import catalog
from ui.core.facade import describe_error
from ui.core.services.settings import CORE, folder_path
from ui.core.services.takes_store import pool_peaks
from ui.core.services.voices import MONTHS, created_tip, language_name

from .confirm import ARM_PURGE, Subscriptions
from .playback import clock, time_text

if TYPE_CHECKING:
    from ui.contract.records import Take
    from ui.contract.routes import Route
    from ui.core.clock import Handle
    from ui.core.services.takes_store import ExportResult, PurgeResult

    from . import BehaviourFactory

__all__ = [
    "ALL_MODELS",
    "ALL_VOICES",
    "EMPTY_FILTERED",
    "EMPTY_NONE",
    "EMPTY_SESSION",
    "EMPTY_STARRED",
    "LIST_TITLES",
    "NEEDS_WINDOW",
    "PAGE_ROWS",
    "QUERY_DELAY_S",
    "SAME_SETTINGS",
    "TakesController",
    "compare_bars",
    "compare_note",
    "compare_rows",
    "export_text",
    "knob_text",
    "on_fixed_drive",
    "plural",
    "take_row",
    "tenths",
    "when_text",
]

Tab = Literal["session", "all", "starred"]
Side = Literal["A", "B"]

TABS: tuple[Tab, ...] = ("session", "all", "starred")                               # M L2710
LIST_TITLES: dict[str, str] = {"session": "This Session", "all": "All Takes", "starred": "Starred Takes"}  # M L3176
ALL_VOICES = "All Voices"                                                            # M L3180
ALL_MODELS = "All Models"                                                            # M L3182
QUERY_DELAY_S = 0.15                                                                 # §5.10
PAGE_ROWS = 200                                                                      # S03 risk 14
NEW_TAKE_S = 1.5                                                                     # M L1656
COMPARE_BARS = 110                                                                   # M L2773
NO_VALUE = "—"
MISSING_TEXT = "Missing"                                                             # NEW
EMPTY_NONE = "No takes yet · generate one on Speak"                                  # M L2855-2858
EMPTY_STARRED = "No starred takes · star a take to keep it"
EMPTY_SESSION = "No takes this session yet"
EMPTY_FILTERED = "No takes match these filters"
SAME_SETTINGS = "Same settings · only the random sampling differs"                   # M L2860
COMPARE_FIELDS = ("Voice", "Model", "Language", "Expressiveness", "Guidance", "Length")   # M L2753-2760
EXPORT_LABEL, EXPORTING = "Export Starred…", "Exporting…"                           # M L769
FOLDER_LABEL = "Show Folder"                                                         # M L770
PURGE_LABEL, REMOVING = "Remove Unstarred", "Removing…"                             # M L771
NEEDS_WINDOW = "Needs the app window"                                                # NEW (§6.10)
EXPORT_TITLE = "Export Starred Takes"
DRIVE_FIXED = 3                                                                      # GetDriveTypeW

log = logging.getLogger("voicetool.behaviours")


# ---------- display copy (shared with the tests; P1's session rows can use take_row too) ----------

def plural(count: int, noun: str) -> str:
    """"1 take", "4 takes"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def tenths(value: float) -> str:
    """One decimal with halves rounded up, like the mockup's toFixed(1): 4.6 → "4.6"."""
    return f"{math.floor(value * 10 + 0.5) / 10:.1f}"


def knob_text(value: float | None) -> str:
    """Expressiveness or Guidance as "0.50"; "—" for a Turbo take, which sends neither (M L2605)."""
    return NO_VALUE if value is None else f"{math.floor(value * 100 + 0.5) / 100:.2f}"


def _local(created: str) -> datetime | None:
    try:
        return datetime.fromisoformat(created).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def when_text(created: str, *, now: datetime | None = None) -> str:
    """The When column: "14:31" today, "Nov 11" earlier this year, "2025" before that; "—" when unknown."""
    at = _local(created)
    if at is None:
        return NO_VALUE
    today = (now or datetime.now()).astimezone()
    if at.date() == today.date():
        return f"{at.hour:02d}:{at.minute:02d}"
    if at.year == today.year:
        return f"{MONTHS[at.month - 1]} {at.day}"
    return str(at.year)


def take_row(take: Take, *, current: bool, pin_a: int, pin_b: int, is_new: bool = False,
             now: datetime | None = None) -> TakeRowVM:
    """One list row (rowFor, M L2597-2618)."""
    return TakeRowVM(
        id=take.id, text=take.text, lang=take.language or "en", voice=take.voice,
        model_label=catalog.label(take.backend), expr_text=knob_text(take.exaggeration),
        guid_text=knob_text(take.cfg_weight),
        length_text=MISSING_TEXT if take.missing else f"{tenths(take.seconds)} s",
        when_text=when_text(take.created, now=now), when_tip=created_tip(take.created),
        current=current, pin_a=take.id == pin_a, pin_b=take.id == pin_b, starred=take.starred,
        missing=take.missing, is_new=is_new)


def _settings(take: Take) -> tuple[str, ...]:
    return (take.voice, catalog.label(take.backend), language_name(take.backend, take.language),
            knob_text(take.exaggeration), knob_text(take.cfg_weight), f"{tenths(take.seconds)} s")


def compare_rows(a: Take, b: Take) -> tuple[CompareRowVM, ...]:
    """The compare table: Voice, Model, Language, Expressiveness, Guidance and Length of A and B."""
    return tuple(CompareRowVM(label, x, y, x != y) for label, x, y in zip(COMPARE_FIELDS, _settings(a), _settings(b)))


def compare_note(rows: Sequence[CompareRowVM], a: Take, b: Take) -> tuple[str, bool]:
    """The note under the table and whether it reports a difference; the texts count too (D-T4)."""
    diffs = [row.label.lower() for row in rows[:5] if row.differs]
    if a.text != b.text:
        diffs.append("text")
    if not diffs:
        return SAME_SETTINGS, False
    if len(diffs) == 1:
        return f"Differs in {diffs[0]}", True
    return f"Differs in {', '.join(diffs[:-1])} and {diffs[-1]}", True


def compare_bars(a: Take, b: Take) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """110 bars per side, max-pooled from the 230 stored peaks and scaled to the louder take (M L2773).

    A side whose WAV is missing, or that has no peaks, draws none.
    """
    pooled = [pool_peaks(take.peaks, COMPARE_BARS) if take.peaks and not take.missing else () for take in (a, b)]
    top = max((max(bars) for bars in pooled if bars), default=0)
    scaled = [tuple(round(value / top, 3) if top > 0 else 0.0 for value in bars) for bars in pooled]
    return scaled[0], scaled[1]


def export_text(result: ExportResult, folder: Path) -> tuple[str, bool]:
    """The toast after an export, and whether it is red."""
    copied, missing, failed = len(result.copied), len(result.missing), len(result.failed)
    if failed and not copied:
        return f"Export failed: {result.failed[0][1]}", True
    text = f"Exported {plural(copied, 'starred take')} to {folder}"
    if missing:
        text += f" · {missing} missing"
    if failed:
        text += f" · {failed} could not be copied"
    return text, bool(failed)


def on_fixed_drive(anchor: str) -> bool:
    """True when anchor ("E:\\") is a local fixed drive, whose files the Recycle Bin takes back; True off Windows.

    On a network share or a removable drive Windows deletes for good without asking (S03 risk 10).
    """
    if os.name != "nt" or not anchor:
        return True
    try:
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor))   # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return True
    return int(kind) == DRIVE_FIXED


# ---------- the controller ----------

class TakesController(TakesControllerProtocol):
    """The Takes page (§5.10); vm: TakesVM. One per page load, so filters survive page changes, not reloads."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self._playback = factory.playback()
        self.vm = TakesVM(model_options=tuple((backend, catalog.label(backend)) for backend in catalog.ORDER))
        self._tab: Tab = "session"
        self._applied = ""                       # the search the list follows (150 ms after typing stops)
        self._voice: str | None = None
        self._model: str | None = None
        self._limit = PAGE_ROWS
        self._new_take = 0                       # the take that just arrived (its row slides in)
        self._query_timer: Handle | None = None
        self._new_timer: Handle | None = None
        self._rows_key: tuple[Any, ...] | None = None
        self._totals_of: tuple[Take, ...] | None = None
        self._starred = 0
        self._unstarred: tuple[int, ...] = ()
        self._seconds = 0.0
        self._bars_key: tuple[Any, ...] | None = None
        self._bars: tuple[tuple[float, ...], tuple[float, ...]] = ((), ())
        self._purge_ids: tuple[int, ...] = ()    # what the armed Remove Unstarred removes
        self._purge_rev = -1                     # TakesState.rev when it armed
        self._purge_delete = False               # the takes are not on a local fixed drive: no Recycle Bin
        self._purging = False
        self._exporting = False
        self._picking = False
        self._export_dir: Path | None = None
        self._subs = Subscriptions()
        self._subs.add(self._core.events.changed, self.refresh)
        self._subs.add(self._core.events.take_ready, self._on_take_ready)
        factory.arm.listeners.add(self.refresh)
        self._playback.listeners.add(self.refresh)
        self.refresh()

    # ---------- the list ----------

    def set_tab(self, tab: Tab) -> None:
        """This Session, All or Starred; a tab change voids the purge arm (M L2710)."""
        if tab not in TABS:
            log.warning("Unknown Takes tab %r", tab)
            return
        self._tab = tab
        self._limit = PAGE_ROWS
        self._factory.arm.disarm(ARM_PURGE)
        self.refresh()

    def set_query(self, text: str) -> None:
        """The search box's text; the list follows 150 ms after typing stops."""
        self.vm.query = str(text or "")
        if self._query_timer is not None:
            self._query_timer.cancel()
        self._query_timer = self._core.scheduler.later(QUERY_DELAY_S, self._apply_query)

    def set_voice_filter(self, voice_id: str | None) -> None:
        """Only that voice's takes (a deleted voice still owns its takes); None = All Voices."""
        if voice_id is not None and not isinstance(voice_id, str):
            return
        self._voice = voice_id or None
        self._limit = PAGE_ROWS
        self.refresh()

    def set_model_filter(self, backend: str | None) -> None:
        """Only that model's takes; None = All Models."""
        if backend and backend not in catalog.ORDER:
            log.warning("Unknown model %r for the Takes filter", backend)
            return
        self._model = backend or None
        self._limit = PAGE_ROWS
        self.refresh()

    def show_more(self) -> None:
        """"Show N more": the next 200 rows of the filtered list."""
        if self.vm.more_count > 0:
            self._limit += PAGE_ROWS
            self.refresh()

    def opened(self) -> None:
        """The page is being built: the list starts again at one page (the tab and filters stay), so a revisit
        after "Show N more" never builds thousands of rows (R3)."""
        if self._limit != PAGE_ROWS:
            self._limit = PAGE_ROWS
            self.refresh()

    def row_click(self, take_id: int) -> None:
        """Play the take from 0; on the current take, toggle play (D-T3). A missing WAV is a red toast."""
        take = self._take(take_id)
        if take is None:
            return
        if take.missing:
            path = self._core.takes.path_of(take.id) or take.wav
            self._core.events.say(f"Take {take.id}'s WAV is missing: {path}", bad=True)
        elif take.id == self._core.session.take_id:
            self._playback.toggle()
        else:
            self._playback.play_take(take.id)

    # ---------- compare ----------

    def compare_play(self) -> None:
        """Play A from 0 when neither pinned take is current, else play or pause the current one (M L1962-1970)."""
        session = self._core.session
        a, b = self._take(session.pin_a), self._take(session.pin_b)
        if a is None or b is None:
            return
        if session.take_id in (a.id, b.id):
            self._playback.toggle()
        else:
            self._playback.play_take(a.id)

    def keep(self, side: Side) -> None:
        """Star the pinned take (never unstar it) and say "Kept Take N · starred" (M L1943-1948); the star is
        saved in click order with every other take edit (core.edits)."""
        session = self._core.session
        take = self._take(session.pin_a if side == "A" else session.pin_b if side == "B" else 0)
        if take is None:
            return
        if self._core.edits.wanted(take.id, take.starred):
            self._core.events.say(f"Kept Take {take.id} · starred")
            return
        self._core.scheduler.spawn(self._keep(take.id), name="voicetool keep")

    def reuse_settings(self) -> Route:
        """Speak's delivery from take A: its voice if still registered, the language for a Multilingual voice,
        Expressiveness and Guidance unless Turbo; never the text; nothing loads (M L1950-1960). Returns "/"."""
        session = self._core.session
        take = self._take(session.pin_a)
        if take is None:
            return "/takes"
        row = next((r for r in self._core.state.library.rows if r.voice_id == take.voice), None)
        if row is not None:
            session.voice_id = row.voice_id
            session.language = take.language if row.backend == catalog.MULTILINGUAL else "en"
        if take.exaggeration is not None:
            session.exaggeration = take.exaggeration
            if take.cfg_weight is not None:
                session.cfg_weight = take.cfg_weight
        self._core.events.notify()
        gone = "" if row is not None else f" · its voice {take.voice} was deleted"
        self._core.events.say(f"Delivery set from Take {take.id}{gone}")
        return "/"

    # ---------- footer ----------

    async def purge_unstarred(self) -> ConfirmRequest | None:
        """Remove Unstarred: the first click arms with the unstarred takes it captured, the second within 3 s
        sends exactly those that are still unstarred to the Recycle Bin (D-T6, Q10)."""
        if self._purging:
            return None
        self._totals()
        ids = self._unstarred
        arm = self._factory.arm
        if not ids:
            arm.disarm(ARM_PURGE)
            return None
        scope = (self._core.state.takes.rev, ids)
        if not (arm.is_armed(ARM_PURGE) and arm.scope(ARM_PURGE) == scope):
            self._purge_delete = not self._recycles(ids)
        self._purge_ids, self._purge_rev = ids, scope[0]
        if arm.press(ARM_PURGE, scope=scope):
            await self._spawned(self._purge(ids, delete=self._purge_delete), "voicetool purge")
            return None
        return ConfirmRequest(token="", kind="purge_takes", style="arm", primary=self._arm_label(), danger=True)

    async def export_starred(self) -> None:
        """Export Starred…: a folder dialog, then every starred take of every session is copied there."""
        self._totals()
        if self._factory.browser or self._exporting or self._picking or not self._starred:
            return
        self._picking = True
        try:
            folder = await self._factory.kit.pick_folder(title=EXPORT_TITLE, start_dir=self._export_start())
        except Exception as exc:
            self._core.events.say(f"Export failed: {describe_error(exc)}", bad=True)
            return
        finally:
            self._picking = False
        ids = tuple(sorted(take.id for take in self._core.state.takes.takes if take.starred))
        if folder is not None and ids:
            await self._spawned(self._export(ids, Path(folder)), "voicetool export")

    def show_folder(self) -> None:
        """Open the Output Folder in Explorer, creating it first; a failure is a red toast."""
        folder = folder_path(str(self._core.settings.get(CORE, "takes_dir")), self._core.paths.root)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._factory.kit.open_folder(folder)
        except Exception as exc:
            self._core.events.say(f"Couldn't open {folder}: {describe_error(exc)}", bad=True)

    # ---------- Confirmable ----------

    async def confirm(self, token: str) -> None:
        """Takes raises no ask (its one confirmation is the purge arm): nothing to confirm."""

    def dismiss(self) -> None:
        """Drop the purge arm."""
        self._factory.arm.disarm(ARM_PURGE)

    def close(self) -> None:
        """Drop every subscription, listener and timer (BehaviourFactory.close)."""
        self._subs.close()
        self._factory.arm.listeners.remove(self.refresh)
        self._playback.listeners.remove(self.refresh)
        for handle in (self._query_timer, self._new_timer):
            if handle is not None:
                handle.cancel()
        self._query_timer = self._new_timer = None

    # ---------- recompute ----------

    def refresh(self) -> None:
        """Recompute the VM from the takes, the Session, the player and this client's tab and filters."""
        if self._void_purge():
            return                              # disarming ran refresh() again through the arm's listeners
        self._totals()
        self._list()
        self._footer()
        self.vm.compare = self._compare()

    def _void_purge(self) -> bool:
        """Any takes change voids the purge arm (D-T6); True when it was voided now."""
        arm = self._factory.arm
        if arm.is_armed(ARM_PURGE) and self._core.state.takes.rev != self._purge_rev:
            arm.disarm(ARM_PURGE)
            return True
        return False

    def _totals(self) -> None:
        """Counts over every take (the foot line and the purge), recomputed when the published tuple changes."""
        takes = self._core.state.takes.takes
        if takes is self._totals_of:
            return
        self._totals_of = takes
        self._starred = sum(1 for take in takes if take.starred)
        self._unstarred = tuple(sorted(take.id for take in takes if not take.starred))
        self._seconds = sum(take.seconds for take in takes)
        self.vm.voice_options = tuple(dict.fromkeys(take.voice for take in takes))   # most recent first

    def _list(self) -> None:
        core, vm = self._core, self.vm
        session, mirror = core.session, core.state.takes
        now = datetime.now().astimezone()
        generating = self._playback.card_generating()
        key = (mirror.rev, id(mirror.takes), session.take_id, session.pin_a, session.pin_b, generating, self._tab,
               self._applied, self._voice, self._model, self._limit, self._new_take, now.date(), core.session_id)
        if key == self._rows_key:
            return
        self._rows_key = key
        takes = mirror.takes
        needle = self._applied.casefold()
        filtered = [take for take in takes if self._shows(take, needle)]
        shown = filtered[: self._limit]
        vm.tab = self._tab
        vm.list_title = LIST_TITLES[self._tab]
        vm.count_text = plural(len(filtered), "take") + (f" of {len(takes)}" if len(filtered) != len(takes) else "")
        vm.voice_label = self._voice or ALL_VOICES
        vm.model_label = catalog.label(self._model) if self._model else ALL_MODELS
        vm.rows = tuple(take_row(take, current=take.id == session.take_id and not generating, pin_a=session.pin_a,
                                 pin_b=session.pin_b, is_new=take.id == self._new_take, now=now) for take in shown)
        vm.more_count = min(PAGE_ROWS, len(filtered) - len(shown))
        vm.empty_text = "" if filtered else self._empty_text(bool(takes))

    def _shows(self, take: Take, needle: str) -> bool:
        """The tab, both filters and the search (needle, casefolded) apply together (M L2738-2742)."""
        if self._tab == "session" and take.session != self._core.session_id:
            return False
        if self._tab == "starred" and not take.starred:
            return False
        if self._voice is not None and take.voice != self._voice:
            return False
        if self._model is not None and take.backend != self._model:
            return False
        return not needle or needle in f"{take.text} {take.voice}".casefold()

    def _empty_text(self, any_takes: bool) -> str:
        if not any_takes:
            return EMPTY_NONE
        filtered = bool(self._applied) or self._voice is not None or self._model is not None
        if not filtered and self._tab == "starred":
            return EMPTY_STARRED
        if not filtered and self._tab == "session":
            return EMPTY_SESSION
        return EMPTY_FILTERED

    def _footer(self) -> None:
        vm = self.vm
        vm.foot_text = f"{self._starred} starred · {tenths(self._seconds)} s of audio"
        if self._factory.browser:
            vm.export = ActionState(False, EXPORT_LABEL, reason=NEEDS_WINDOW, tooltip=NEEDS_WINDOW, tone="plain")
        elif self._exporting:
            vm.export = ActionState(False, EXPORTING, tone="plain")
        else:
            vm.export = ActionState(self._starred > 0, EXPORT_LABEL, tone="plain")
        vm.folder = ActionState(True, FOLDER_LABEL, tone="plain")
        if self._purging:
            vm.purge = ActionState(False, REMOVING, tone="plain")
        elif self._factory.arm.is_armed(ARM_PURGE):
            vm.purge = ActionState(True, self._arm_label(), tone="plain", armed=True)
        else:
            vm.purge = ActionState(bool(self._unstarred), PURGE_LABEL, tone="plain")

    def _arm_label(self) -> str:
        """"Click Again to Remove 4", or "… to Delete 4" where no Recycle Bin takes them back (S03 risk 10)."""
        return f"Click Again to {'Delete' if self._purge_delete else 'Remove'} {len(self._purge_ids)}"

    def _compare(self) -> CompareVM | None:
        """The compare card while both pinned takes exist (M L2746-2779, L2859-2864)."""
        session, playback = self._core.session, self._playback
        a, b = self._take(session.pin_a), self._take(session.pin_b)
        if a is None or b is None:
            return None
        current = session.take_id
        active = current in (a.id, b.id)
        playing = active and playback.is_playing_take(current)
        bars = self._compare_bars(a, b)
        rows = compare_rows(a, b)
        note, differs = compare_note(rows, a, b)
        shown = a if current == a.id else b if current == b.id else None
        time = time_text(playback.position_of(shown.id), shown.seconds) if shown else f"0:00.0 / {clock(a.seconds)}"
        return CompareVM(
            sides=(self._side("A", a, bars[0], current), self._side("B", b, bars[1], current)),
            rows=rows, note=note, note_differs=differs,
            play_label="Pause" if playing else "Play" if active else "Play A",
            playing=playing, time_text=time, show_ab=not playback.card_generating(),
            keep_a=ActionState(True, "Keep A", tone="plain"), keep_b=ActionState(True, "Keep B", tone="plain"),
            reuse=ActionState(True, "Reuse A's Settings", tone="plain"))

    def _side(self, side: Side, take: Take, bars: tuple[float, ...], current: int) -> CompareSideVM:
        active = take.id == current
        progress = 0.0
        if active and take.seconds > 0:
            progress = round(min(1.0, max(0.0, self._playback.position_of(take.id) / take.seconds)), 3)
        length = f"{tenths(take.seconds)} s" + (" · missing" if take.missing else "")
        return CompareSideVM(side=side, take_id=take.id, title=f"{side} · Take {take.id}", title_active=active,
                             length_text=length, bars=bars, progress=progress, missing=take.missing)

    def _compare_bars(self, a: Take, b: Take) -> tuple[tuple[float, ...], tuple[float, ...]]:
        key = (a.id, a.missing, len(a.peaks), b.id, b.missing, len(b.peaks))
        if key != self._bars_key:
            self._bars_key, self._bars = key, compare_bars(a, b)
        return self._bars

    # ---------- workers ----------

    async def _spawned(self, work: Awaitable[None], name: str) -> None:
        """Run work as an app-level task, so it finishes even if this client goes away, and wait for it."""
        await asyncio.shield(self._core.scheduler.spawn(work, name=name))

    async def _keep(self, take_id: int) -> None:
        """The star write (its failure toast is the edits' own), then "Kept Take N · starred" once it holds."""
        if await self._core.edits.star(take_id, True):
            take = self._core.takes.get(take_id)
            if take is not None and take.starred:
                self._core.events.say(f"Kept Take {take_id} · starred")

    async def _purge(self, ids: tuple[int, ...], *, delete: bool) -> None:
        """After every star asked before the second click is saved (core.edits keeps the order), so the store
        decides "still unstarred" from what the user did last (R1-BUG-2)."""
        core = self._core
        self._purging = True
        self.refresh()
        result: PurgeResult | None = None
        problem = ""
        try:
            result = await core.edits.call(core.takes.purge_unstarred, ids, trash=core.trash)
        except Exception as exc:
            problem = describe_error(exc)
        finally:
            self._purging = False
            core.publish_takes()
        if problem:
            core.log.add(f"Couldn't remove the unstarred takes · {problem}")
            core.events.say(f"Couldn't remove the unstarred takes: {problem}", bad=True)
        elif result is not None:                     # None: the app is stopping
            self._after_purge(result)
            self._report(result, delete=delete)
        self.refresh()

    def _after_purge(self, result: PurgeResult) -> None:
        """Pins to removed takes clear; the current take stays if it survived, else the newest survivor; playback
        stops (M L1938)."""
        if not (result.moved or result.missing):
            return
        session, playback = self._core.session, self._playback
        survivors = self._core.takes.takes
        alive = {take.id for take in survivors}
        if session.pin_a not in alive:
            session.pin_a = 0
        if session.pin_b not in alive:
            session.pin_b = 0
        playback.stop()
        if session.take_id not in alive:
            session.take_id = survivors[0].id if survivors else 0
        elif playback.loaded.kind == "take" and playback.loaded.key == session.take_id:
            playback.seek(0.0)
        self._core.events.notify()

    def _report(self, result: PurgeResult, *, delete: bool) -> None:
        """"Moved 4 unstarred takes to the Recycle Bin" (M L1939-1940), with the failures counted and logged."""
        core = self._core
        removed = plural(len(result.moved) + len(result.missing), "unstarred take")
        failed = len(result.failed)
        if delete:
            text = f"Deleted {removed}" + (f" · {failed} could not be deleted" if failed else "")
        else:
            text = f"Moved {removed} to the Recycle Bin" + (f" · {failed} could not be moved" if failed else "")
        core.log.add(text)
        what = "deleted" if delete else "moved to the Recycle Bin"
        for take_id, reason in result.failed:
            core.log.add(f"Take {take_id} couldn't be {what} · {reason}")
        core.events.say(text, bad=bool(failed))

    async def _export(self, ids: tuple[int, ...], folder: Path) -> None:
        self._exporting = True
        self.refresh()
        try:
            result = await asyncio.to_thread(self._core.takes.export, ids, folder)
        except Exception as exc:
            self._core.events.say(f"Export failed: {describe_error(exc)}", bad=True)
            return
        finally:
            self._exporting = False
            self.refresh()
        self._export_dir = folder
        text, bad = export_text(result, folder)
        self._core.events.say(text, bad=bad)

    # ---------- helpers ----------

    def _take(self, take_id: Any) -> Take | None:
        if not isinstance(take_id, int) or isinstance(take_id, bool) or take_id <= 0:
            return None
        return self._core.takes.get(take_id)

    def _recycles(self, ids: Sequence[int]) -> bool:
        """True when every WAV to purge is on a local fixed drive, so the Recycle Bin can take it back."""
        anchors = {path.anchor for take_id in ids if (path := self._core.takes.path_of(take_id)) is not None}
        return all(on_fixed_drive(anchor) for anchor in anchors)

    def _export_start(self) -> Path | None:
        """The last export folder of this page load, else Documents (None: the dialog's own default)."""
        if self._export_dir is not None:
            return self._export_dir
        documents = Path.home() / "Documents"
        return documents if documents.is_dir() else None

    def _on_take_ready(self, take_id: int) -> None:
        """A new take: its row slides in and flashes (M L2608, cosmetic) for 1.5 s."""
        self._new_take = take_id
        if self._new_timer is not None:
            self._new_timer.cancel()
        self._new_timer = self._core.scheduler.later(NEW_TAKE_S, lambda: self._clear_new_take(take_id))
        self.refresh()

    def _clear_new_take(self, take_id: int) -> None:
        if self._new_take == take_id:
            self._new_take = 0
            self._new_timer = None
            self.refresh()

    def _apply_query(self) -> None:
        self._query_timer = None
        applied = self.vm.query.strip()
        if applied != self._applied:
            self._applied = applied
            self._limit = PAGE_ROWS
            self.refresh()
