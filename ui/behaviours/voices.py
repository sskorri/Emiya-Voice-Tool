"""VoicesController (§5.8, §6.8): the Voices page's filtered library list, its details card and the actions on
one voice.

The list and the details come from the in-memory library snapshot (LibraryState), never from the files: only
the 138 reference-clip bars of the voice in the details are read, in a worker (ClipService.bars), and they show
as a flat row until they arrive. The details show the selected voice, else the first row the filter leaves,
else the first voice (M L2716); a selected voice stays shown when the filter hides its row.

- Edits change the registry only (facade.update_voice: no model, no engine lock): the language of a
  Multilingual voice and the default Expressiveness of a voice that uses it. They are allowed while busy,
  except for the voice being forged, run one at a time in click order, show the new value at once and snap
  back when the save fails (red toast "Couldn't save <id>: <reason>").
- Delete arms first ("Click Again to Delete" for 3 s; voided by a row selection, a page change and a prefill),
  is refused while any job runs and goes through facade.delete_voice, which sends the .pt to the injected
  trash; the reference WAV stays.
- Re-forge…, Forge on … and Replace… prefill the Forge draft through ForgeController.prefill (refused while a
  forge runs); Speak with picks the voice through SpeakController.pick_voice and flashes its Speak row.

Nothing here loads or frees a model (§4.5).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.plans import ActionState, ConfirmRequest
from ui.contract.routes import Route
from ui.contract.viewmodels import ChipTone, LangItemVM, VoiceDetailsVM, VoiceListRowVM, VoicesVM
from ui.contract.viewmodels import VoicesController as VoicesControllerProtocol
from ui.core import catalog
from ui.core.catalog import BACKENDS, LANGUAGES
from ui.core.facade import BUSY, describe_error
from ui.core.planner import round_half_up
from ui.core.services.settings import CORE, folder_value
from ui.core.services.voices import created_text, created_tip

from .confirm import ARM_DELETE, Subscriptions
from .status import row_state

if TYPE_CHECKING:
    from engine.types import DeleteResult
    from ui.contract.records import VoiceRow
    from ui.contract.state import LibraryState

    from . import BehaviourFactory
    from .playback import PlaybackController

__all__ = [
    "ALL",
    "BAR_COUNT",
    "CLIP_FILTERS",
    "DELETE_ARMED",
    "DELETE_LABEL",
    "DELETING",
    "EMPTY_FILTERED",
    "EMPTY_LIBRARY",
    "NO_DETAILS",
    "REGISTRY_UNREADABLE",
    "VoicesController",
]

ALL = ""                                                     # the All segment's key (VoicesVM.segments)
BAR_COUNT = 138                                              # M L2731
TIMBRE_S = 10.0                                              # the first 10 s set the timbre (M L2732)
EXPR_MIN, EXPR_MAX = 0.25, 2.0                               # the slider's range (M L674)
MAX_BAR_SETS = 32                                            # bar sets kept per client
CLIP_FILTERS: tuple[str, ...] = ("Audio clips (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)")   # as Browse…

DELETE_LABEL = "Delete"
DELETE_ARMED = "Click Again to Delete"                       # M L3172
DELETING = "Deleting…"                                       # NEW
REFORGE_LABEL = "Re-forge…"
REPLACE_LABEL = "Replace…"
NO_DETAILS = "Pick a voice to see its details."              # M L709
EMPTY_FILTERED = "No voices match the filter"                # M L3135
EMPTY_LIBRARY = "No voices yet · forge one from a reference clip"
REGISTRY_UNREADABLE = "Couldn't read cache/registry.json · see Log"   # NEW (as on Speak)
RESIDENT_TIP = "Resident in VRAM"                            # M L2723
NOT_LOADED = "Not loaded"                                    # NEW: no load history
CONSENT_OK = "Confirmed when forged"                         # M L3166
CONSENT_MISSING = "Not confirmed · re-forge to confirm"
CLIP_MISSING = "Reference clip missing"                      # NEW
PLAY_CLIP = "Play the reference clip of {voice}"             # M L3144
STOP_CLIP = "Stop the reference clip of {voice}"             # NEW (as Forge's "Stop the reference clip")
REPLACE_TITLE = "Pick a new clip · it re-forges {voice} and replaces its .pt"   # M L3161
DASH = "—"
TAGS = "tags"                                                # Turbo's Expr cell (M L2721)
KEPT_PREFIX = "kept: "                                       # DeleteResult: a casefold twin keeps its .pt (§3.10)

Field = Literal["language", "exaggeration"]
READY = ("ready", "corrupt")

log = logging.getLogger("voicetool.behaviours")


@dataclass(frozen=True)
class _Pending:
    """An edit on its way to the registry: the value shown meanwhile, and the edit it belongs to."""

    value: str | float
    seq: int


class VoicesController(VoicesControllerProtocol):
    """The Voices page (§5.8): filter, selection, registry edits, delete, and the hand-offs to Forge and Speak."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self.vm = VoicesVM()
        self._selected = ""
        self._query = ""
        self._backend = ALL
        self._lang_open = False
        self._shown = ""                                     # the voice the details showed last
        self._pending: dict[tuple[str, str], _Pending] = {}  # (voice, field) -> the value on its way
        self._seq = 0
        self._edit_lock = asyncio.Lock()                     # edits reach the registry in click order
        self._deleting = ""
        self._picking = False                                # Replace…'s file dialog is open
        self._bars: dict[str, tuple[float | None, tuple[tuple[float, str], ...]]] = {}
        self._bars_loading: set[str] = set()
        self._folders: dict[str, bool] = {}
        self._folders_rev = -1
        self._closed = False
        self._playback: PlaybackController = factory.playback()
        self._subs = Subscriptions()
        self._subs.add(self._core.events.changed, self.refresh)
        factory.arm.listeners.add(self.refresh)
        self._playback.listeners.add(self._on_playback)
        self.refresh()

    # ---------- the list ----------

    def select(self, voice_id: str) -> None:
        """Show the voice in the details; clears the delete arm (M L2725)."""
        if self._row(voice_id) is None:
            return
        self._selected = voice_id
        self._lang_open = False
        self._factory.arm.disarm(ARM_DELETE)
        self.refresh()

    def set_filter(self, *, query: str | None = None, backend: str | None = None) -> None:
        """The filter text and the backend segment ("" = All); with neither given, the backend goes back to All."""
        if query is None and backend is None:
            backend = ALL
        if backend is not None and backend != ALL and backend not in self._core.state.engine.enabled:
            return
        before = self._details_id()
        if query is not None:
            self._query = str(query)
        if backend is not None:
            self._backend = backend
        if self._details_id() != before:
            self._lang_open = False
            self._factory.arm.disarm(ARM_DELETE)
        self.refresh()

    def opened(self) -> None:
        """The page was (re)built: take a new snapshot when registry.json changed meanwhile (cli.py, §4.10)."""
        if _running_loop():
            self._core.scheduler.spawn(self._core.refresh_library(changed_only=True), name="voicetool voices open")

    # ---------- registry edits ----------

    async def edit(self, voice_id: str, *, language: str | None = None, exaggeration: float | None = None) -> None:
        """Save a new language and/or default Expressiveness to the registry (no model loads)."""
        if language is not None:
            self._lang_open = False
        row = self._row(voice_id)
        changes = self._changes(row, language, exaggeration) if row is not None else None
        if not changes:
            self.refresh()
            return
        seq = self._remember(voice_id, changes)
        self.refresh()
        try:
            async with self._edit_lock:
                await self._save(voice_id, changes)
        finally:
            self._forget(voice_id, changes, seq)
            self.refresh()

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open or close the 23-language list of a Multilingual voice (navigation and Escape close it)."""
        want = (not self._lang_open) if open is None else bool(open)
        details = self.vm.details
        if want and (details is None or details.language_mode != "pick"):
            want = False
        if want != self._lang_open:
            self._lang_open = want
            self.refresh()

    # ---------- delete ----------

    async def delete(self, voice_id: str) -> ConfirmRequest | None:
        """The first press arms "Click Again to Delete"; a second one within 3 s deletes the voice."""
        row = self._row(voice_id)
        if row is None or self._core.state.job.kind or self._deleting:
            return None
        if not self._factory.arm.press(ARM_DELETE, scope=voice_id):
            return ConfirmRequest(token="", kind="delete_voice", style="arm", primary=DELETE_ARMED, danger=True)
        await self._delete(row)
        return None

    # ---------- hand-offs to Forge and Speak ----------

    def reforge(self, voice_id: str) -> Route | None:
        """Prefill the Forge draft from the voice (the arm to replace happens on Forge) and open Forge."""
        return self._prefill(voice_id, "reforge")

    def forge_copy(self, voice_id: str) -> Route | None:
        """Prefill a copy on the other model ("Forge on Turbo…" / "Forge on Multilingual…")."""
        row = self._row(voice_id)
        if row is None or _copy_target(row.backend) not in self._core.state.engine.enabled:
            return None
        return self._prefill(voice_id, "copy")

    async def replace_clip(self, voice_id: str) -> Route | None:
        """Pick a new clip (as Browse…), then prefill a re-forge with it; None when cancelled or refused."""
        if self._row(voice_id) is None or self._forging() or self._picking:
            return None
        path = await self._pick_clip(voice_id)
        if path is None:
            return None
        if not self._factory.browser:                        # an upload lands in a scratch folder (--browser)
            self._remember_browse_dir(path)
        return self._prefill(voice_id, "reforge", path)

    def speak_with(self, voice_id: str) -> Route:
        """Speak with this voice: the Speak draft from it (pick_voice), its row flashed, "/"; nothing loads."""
        if self._row(voice_id) is None:
            self._core.events.say(f"{voice_id} no longer exists", bad=True)
            return "/voices"
        self._factory.speak().pick_voice(voice_id)
        self._core.events.flash.emit(f"voice-{voice_id}")
        return "/"

    # ---------- the files ----------

    async def reveal_cache(self, voice_id: str) -> None:
        """Show the voice's .pt in Explorer, or open its model folder when the .pt is missing."""
        row = self._row(voice_id)
        if row is None:
            return
        kit, pt = self._factory.kit, row.pt
        try:
            if pt.is_file():
                kit.reveal(pt)
            elif pt.parent.is_dir():
                kit.open_folder(pt.parent)
        except OSError as exc:
            self._core.events.say(f"Couldn't show {pt.name} in Explorer: {describe_error(exc)}", bad=True)

    def toggle_clip_playback(self, voice_id: str) -> None:
        """Play the voice's reference clip; a second press stops it (the shell's one player)."""
        row = self._row(voice_id)
        if row is not None and row.reference_path is not None:
            self._playback.audition(voice_id)

    # ---------- Confirmable ----------

    async def confirm(self, token: str) -> None:
        """The Voices page raises no ask (Delete is an arm), so there is nothing to confirm."""

    def dismiss(self) -> None:
        """Close the language list and disarm Delete."""
        self._factory.arm.disarm(ARM_DELETE)
        self.toggle_language_list(False)

    def close(self) -> None:
        """The client went away: drop every subscription (BehaviourFactory.close)."""
        self._closed = True
        self._subs.close()
        self._factory.arm.listeners.remove(self.refresh)
        self._playback.listeners.remove(self._on_playback)

    # ---------- the view model ----------

    def refresh(self) -> None:
        """Recompute the VM from the library, the job, the residency, the arm and the player."""
        if self._closed:
            return
        vm, library = self.vm, self._core.state.library
        rows = library.rows
        shown = tuple(row for row in rows if self._matches(row))
        current = self._current(rows, shown)
        if current is None or current.voice_id != self._shown:
            self._lang_open = False
            self._shown = current.voice_id if current is not None else ""
        estimates = self._estimates(rows)
        vm.query = self._query
        vm.backend_filter = self._backend or None
        vm.segments = self._segments()
        vm.count_text = _count_text(library.status, rows, shown)
        vm.rows = tuple(self._list_row(row, current, estimates) for row in shown)
        vm.empty_text = _empty_text(library, rows, shown)
        vm.details = None if current is None else self._details(current, estimates)
        vm.no_details_text = NO_DETAILS if current is None and library.status in READY else ""

    def _list_row(self, row: VoiceRow, current: VoiceRow | None, estimates: dict[str, int | None]) -> VoiceListRowVM:
        resident = self._resident(row.backend)
        return VoiceListRowVM(
            voice_id=row.voice_id,
            model_label=catalog.label(row.backend),
            model_resident=resident,
            model_tip=_model_tip(resident, estimates.get(row.backend)),
            language_name=_language_name(row, str(self._value(row, "language"))),
            expr_text=TAGS if _is_turbo(row.backend) else f"{float(self._value(row, 'exaggeration')):.2f}",
            clip_text=DASH if row.clip_s is None else f"{row.clip_s:.1f} s",
            clip_missing=row.reference_path is None,
            created_text=created_text(row.created),
            created_tip=created_tip(row.created),
            selected=current is not None and row.voice_id == current.voice_id,
        )

    def _details(self, row: VoiceRow, estimates: dict[str, int | None]) -> VoiceDetailsVM:
        vid, turbo, many = row.voice_id, _is_turbo(row.backend), _speaks_many(row.backend)
        edits = self._edits_allowed(row)
        code, expr = str(self._value(row, "language")), float(self._value(row, "exaggeration"))
        mode = ("pick" if edits else "disabled") if many else "locked"
        chip_text, chip_tone = _chip(self._resident(row.backend), estimates.get(row.backend))
        present = row.reference_path is not None
        playing = present and self._playback.is_playing_clip(vid)
        target = _copy_target(row.backend)
        forging = self._forging()
        return VoiceDetailsVM(
            voice_id=vid, title=vid, caption=_caption(row, code), chip_text=chip_text, chip_tone=chip_tone,
            play=_play_action(vid, present, playing), playing=playing,
            bars=self._bars_of(row), bars_aria=_bars_aria(row),
            language_mode=mode, language_name=_language_name(row, code), lang_open=self._lang_open and mode == "pick",
            lang_items=self._lang_items(row.language, code) if many else (),
            expr_enabled=edits and not turbo, expr=expr, expr_text=DASH if turbo else f"{expr:.2f}",
            ref_text=_ref_text(row), ref_tip=str(row.reference_path) if present else "", ref_missing=not present,
            cache_text=folder_value(row.pt, self._core.paths.root), cache_missing=not row.pt_exists,
            reveal_enabled=row.pt_exists or self._folder_exists(row.pt.parent),
            consent_ok=row.consent, consent_text=CONSENT_OK if row.consent else CONSENT_MISSING,
            edits_enabled=edits, speak_label=f"Speak with {vid}",
            reforge=_busy_action(REFORGE_LABEL, forging),
            copy=_busy_action(f"Forge on {catalog.label(target)}…", forging),
            copy_visible=target in self._core.state.engine.enabled,
            replace=_busy_action(REPLACE_LABEL, forging or self._picking),
            delete=self._delete_action(vid),
        )

    def _delete_action(self, voice_id: str) -> ActionState:
        if self._deleting == voice_id:
            return ActionState(False, DELETING, tone="plain")
        if self._core.state.job.kind or self._deleting:
            return ActionState(False, DELETE_LABEL, reason=BUSY, tone="plain")
        arm = self._factory.arm
        armed = arm.is_armed(ARM_DELETE) and arm.scope(ARM_DELETE) == voice_id
        return ActionState(True, DELETE_ARMED if armed else DELETE_LABEL, tone="plain", armed=armed)

    def _segments(self) -> tuple[tuple[str, str, bool], ...]:
        enabled = self._core.state.engine.enabled
        return ((ALL, "All", self._backend == ALL),) + tuple(
            (backend, catalog.label(backend), self._backend == backend) for backend in enabled)

    def _lang_items(self, registered: str, code: str) -> tuple[LangItemVM, ...]:
        """The 23 languages: the voice's own first, then those other voices use, then the rest, by name."""
        used = {row.language for row in self._core.state.library.rows}
        by_name = sorted(LANGUAGES, key=lambda key: LANGUAGES[key])
        first = [registered] if registered in LANGUAGES else []
        order = first + [c for c in by_name if c in used and c not in first] + [
            c for c in by_name if c not in used and c not in first]
        return tuple(LangItemVM(code=c, name=LANGUAGES[c], selected=c == code, mark="") for c in order)

    def _bars_of(self, row: VoiceRow) -> tuple[tuple[float, str], ...]:
        """The voice's bars when read (cached by clip path and length); else start the read and draw flat."""
        if row.reference_path is None:
            return ()
        key = os.path.normcase(str(row.reference_path))
        cached = self._bars.get(key)
        if cached is not None and cached[0] == row.clip_s:
            return cached[1]
        self._load_bars(key, row.reference_path, row.clip_s)
        return ()

    def _load_bars(self, key: str, path: Path, clip_s: float | None) -> None:
        if key in self._bars_loading or not _running_loop():
            return
        self._bars_loading.add(key)
        self._core.scheduler.spawn(self._fetch_bars(key, path, clip_s), name="voicetool voice bars")

    async def _fetch_bars(self, key: str, path: Path, clip_s: float | None) -> None:
        peaks: tuple[float, ...] = ()
        try:
            peaks = tuple(await asyncio.to_thread(self._core.clips.bars, path, BAR_COUNT))
        except Exception as exc:                            # cosmetic: the bars stay flat
            log.warning("Couldn't read the bars of %s: %s", path.name, describe_error(exc))
        finally:
            self._bars_loading.discard(key)
        if self._closed:
            return
        self._bars[key] = (clip_s, _zoned(peaks, clip_s))
        while len(self._bars) > MAX_BAR_SETS:
            self._bars.pop(next(iter(self._bars)))
        self.refresh()

    # ---------- plumbing ----------

    def _row(self, voice_id: str) -> VoiceRow | None:
        return next((row for row in self._core.state.library.rows if row.voice_id == voice_id), None)

    def _matches(self, row: VoiceRow) -> bool:
        """The backend segment, and the filter text in "<name> <Language name>" ignoring case (M L2715)."""
        if self._backend and row.backend != self._backend:
            return False
        needle = self._query.strip().lower()
        return not needle or needle in f"{row.voice_id} {row.language_name}".lower()

    def _current(self, rows: tuple[VoiceRow, ...], shown: tuple[VoiceRow, ...]) -> VoiceRow | None:
        """The selected voice, else the first row shown, else the first voice (M L2716)."""
        selected = next((row for row in rows if row.voice_id == self._selected), None) if self._selected else None
        return selected or (shown[0] if shown else rows[0] if rows else None)

    def _details_id(self) -> str:
        rows = self._core.state.library.rows
        current = self._current(rows, tuple(row for row in rows if self._matches(row)))
        return current.voice_id if current is not None else ""

    def _estimates(self, rows: tuple[VoiceRow, ...]) -> dict[str, int | None]:
        """Whole-second load estimates of the backends in the library (None without history)."""
        estimates: dict[str, int | None] = {}
        for backend in {row.backend for row in rows}:
            about = self._core.planner.load_estimate(backend).about_s
            estimates[backend] = round_half_up(about) if about else None
        return estimates

    def _resident(self, backend: str) -> bool:
        return row_state(self._core, backend) == "resident"

    def _forging(self) -> bool:
        return self._core.state.job.kind == "forge"

    def _edits_allowed(self, row: VoiceRow) -> bool:
        job = self._core.state.job
        return not (job.kind == "forge" and job.voice == row.voice_id) and self._deleting != row.voice_id

    def _value(self, row: VoiceRow, field: Field) -> str | float:
        """What the details show for a field: an edit on its way, else the registered value."""
        pending = self._pending.get((row.voice_id, field))
        if pending is not None:
            return pending.value
        return row.language if field == "language" else row.exaggeration

    def _changes(self, row: VoiceRow, language: str | None, exaggeration: float | None) -> dict[str, Any] | None:
        """The registry fields this edit changes; None when the edit is refused (the control is disabled)."""
        if not self._edits_allowed(row):
            return None
        changes: dict[str, Any] = {}
        if language is not None:
            code = str(language).strip().lower()
            if not _speaks_many(row.backend) or code not in LANGUAGES:
                return None
            if code != self._value(row, "language"):
                changes["language"] = code
        if exaggeration is not None:
            value = _exaggeration(exaggeration)
            if _is_turbo(row.backend) or value is None:
                return None
            if abs(value - float(self._value(row, "exaggeration"))) > 1e-9:
                changes["exaggeration"] = value
        return changes

    def _remember(self, voice_id: str, changes: dict[str, Any]) -> int:
        self._seq += 1
        for field, value in changes.items():
            self._pending[(voice_id, field)] = _Pending(value, self._seq)
        return self._seq

    def _forget(self, voice_id: str, changes: dict[str, Any], seq: int) -> None:
        for field in changes:
            key = (voice_id, field)
            pending = self._pending.get(key)
            if pending is not None and pending.seq == seq:
                del self._pending[key]

    async def _save(self, voice_id: str, changes: dict[str, Any]) -> None:
        """One registry write; the log lines and a new snapshot on success, a red toast on failure."""
        core = self._core
        out = await core.facade.update_voice(voice_id, **changes)
        if out.ok:
            for line in _edit_lines(voice_id, changes):
                core.log.add(line)
            await core.refresh_library()
        elif out.status != "cancelled":
            core.events.say(f"Couldn't save {voice_id}: {out.reason}", bad=True)

    async def _delete(self, row: VoiceRow) -> None:
        """"Deleting…" until the new snapshot is in; nothing changed when the .pt could not be trashed."""
        core, vid = self._core, row.voice_id
        self._deleting = vid
        self._lang_open = False
        self.refresh()
        try:
            out = await core.facade.delete_voice(vid)
            if out.ok and out.value is not None and out.value.removed_entry:
                await self._deleted(row, out.value)
            elif out.ok:
                await self._gone(vid)
            elif out.status != "cancelled":
                core.events.say(f"Couldn't delete {vid}: {out.reason}", bad=True)
        finally:
            self._deleting = ""
            self.refresh()

    async def _deleted(self, row: VoiceRow, result: DeleteResult) -> None:
        """After a delete: selection and Speak's voice clear, tokens void, a new snapshot, log and toast."""
        core, vid = self._core, row.voice_id
        self._selected = ""
        if self._playback.is_playing_clip(vid):
            self._playback.stop()
        if core.session.voice_id == vid:
            core.session.voice_id = ""
            core.events.notify()
        core.tokens.void_all()
        await core.refresh_library()
        root = core.paths.root
        core.log.add(f"Removed {vid} · registry entry and {folder_value(row.pt, root)} · the WAV stays")
        for path, reason in result.failed_files:
            core.log.add(_kept_line(folder_value(path, root), reason))
        core.events.say(f"Deleted {vid} · its reference WAV stays in voices/reference")

    async def _gone(self, voice_id: str) -> None:
        """The entry was already gone (another program removed it): show the library as it is now."""
        self._selected = ""
        await self._core.refresh_library()
        self._core.events.say(f"Couldn't delete {voice_id}: it is not registered", bad=True)

    def _prefill(self, voice_id: str, mode: Literal["reforge", "copy"], clip_path: Path | None = None) -> Route | None:
        """ForgeController.prefill (§5.9); refused while a forge runs, clears the delete arm (M L1895, L1902)."""
        if self._row(voice_id) is None or self._forging():
            return None
        self._lang_open = False
        self._factory.arm.disarm(ARM_DELETE)
        route = self._factory.forge().prefill(voice_id=voice_id, mode=mode, clip_path=clip_path)
        self.refresh()
        return route

    async def _pick_clip(self, voice_id: str) -> Path | None:
        """Replace…'s OPEN dialog (one at a time); None when cancelled or when the dialog failed."""
        self._picking = True
        self.refresh()
        problem = ""
        found: Path | None = None
        try:
            found = await self._factory.kit.pick_file(title=REPLACE_TITLE.format(voice=voice_id),
                                                      filters=CLIP_FILTERS, start_dir=self._browse_dir())
        except (OSError, ValueError, RuntimeError) as exc:
            problem = describe_error(exc)
        finally:
            self._picking = False
            self.refresh()
        if problem:
            self._core.events.say(f"Couldn't open the file dialog: {problem}", bad=True)
        if found is None or self._forging() or self._row(voice_id) is None:
            return None
        return Path(found)

    def _browse_dir(self) -> Path | None:
        """Where the dialog starts: the last folder a clip came from, else the voices folder."""
        stored = str(self._core.settings.get(CORE, "browse_dir") or "")
        for folder in (Path(stored) if stored else None, self._core.paths.voices):
            if folder is not None and folder.is_dir():
                return folder
        return None

    def _remember_browse_dir(self, path: Path) -> None:
        folder = path.parent
        if not folder.is_absolute():
            return
        try:
            self._core.settings.set(CORE, "browse_dir", str(folder))
        except (KeyError, ValueError) as exc:
            log.warning("Couldn't remember the clip folder: %s", exc)

    def _folder_exists(self, folder: Path) -> bool:
        """Whether a model folder exists, checked once per library snapshot."""
        rev = self._core.state.library.rev
        if rev != self._folders_rev:
            self._folders, self._folders_rev = {}, rev
        key = os.path.normcase(str(folder))
        if key not in self._folders:
            self._folders[key] = folder.is_dir()
        return self._folders[key]

    def _on_playback(self) -> None:
        details = self.vm.details
        if details is not None and details.playing != self._playback.is_playing_clip(details.voice_id):
            self.refresh()


# ---------- copy and small rules ----------

def _running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _is_turbo(backend: str) -> bool:
    """The backend ignores Expressiveness (Turbo)."""
    info = BACKENDS.get(backend)
    return info is not None and not info.uses_exaggeration


def _speaks_many(backend: str) -> bool:
    """The backend speaks the 23 languages (Multilingual); Standard and Turbo are English only."""
    info = BACKENDS.get(backend)
    return info is not None and not info.english_only


def _copy_target(backend: str) -> str:
    """Forge on …: Multilingual for a Turbo voice, Turbo otherwise (M L1901)."""
    return catalog.MULTILINGUAL if _is_turbo(backend) else catalog.TURBO


def _language_name(row: VoiceRow, code: str) -> str:
    if not _speaks_many(row.backend):
        return row.language_name
    return LANGUAGES.get(code, code)


def _exaggeration(value: Any) -> float | None:
    """The slider's value rounded to 0.01 (M L3156); None outside 0.25-2.0 or not a number."""
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and EXPR_MIN <= number <= EXPR_MAX else None


def _voices(count: int) -> str:
    return f"{count} voice" if count == 1 else f"{count} voices"


def _count_text(status: str, rows: tuple[VoiceRow, ...], shown: tuple[VoiceRow, ...]) -> str:
    """"5 voices · 3 Multilingual · 1 Standard · 1 Turbo", filtered "2 of 5 voices" (M L2849-2852)."""
    if not rows and status not in READY:
        return ""
    total = _voices(len(rows))
    if len(shown) != len(rows):
        return f"{len(shown)} of {total}"
    if not rows:
        return total
    backends = list(catalog.ORDER) + list(dict.fromkeys(r.backend for r in rows if r.backend not in catalog.ORDER))
    parts = [f"{sum(1 for r in rows if r.backend == b)} {catalog.label(b)}" for b in backends]
    return " · ".join([total, *parts])


def _empty_text(library: LibraryState, rows: tuple[VoiceRow, ...], shown: tuple[VoiceRow, ...]) -> str:
    if shown:
        return ""
    if rows:
        return EMPTY_FILTERED
    if library.status == "failed":
        return REGISTRY_UNREADABLE
    return EMPTY_LIBRARY if library.status in READY else ""


def _model_tip(resident: bool, about: int | None) -> str:
    if resident:
        return RESIDENT_TIP
    return f"Not loaded · about {about} s to load" if about else NOT_LOADED


def _chip(resident: bool, about: int | None) -> tuple[str, ChipTone]:
    """"Resident" (gold, ring) or "Loads in ~14 s"; "Not loaded" without history (M L3140-3143)."""
    if resident:
        return "Resident", "gold"
    return (f"Loads in ~{about} s" if about else NOT_LOADED), "off"


def _caption(row: VoiceRow, code: str) -> str:
    """"Multilingual · Japanese · forged today" / "… · forged Nov 8" (M L3139)."""
    parts = [catalog.label(row.backend), _language_name(row, code)]
    when = created_text(row.created)
    if when != DASH:
        parts.append("forged " + ("today" if when == "Today" else when))
    return " · ".join(parts)


def _ref_text(row: VoiceRow) -> str:
    """"aiko_ref.wav · 11.2 s used" (M L3160); a missing clip: "aiko_ref.wav · missing"."""
    name = row.reference_name or "reference clip"
    if row.reference_path is None:
        return f"{name} · missing"
    return name if row.clip_s is None else f"{name} · {row.clip_s:.1f} s used"


def _bars_aria(row: VoiceRow) -> str:
    """"Reference clip aiko_ref.wav, 11.2 s · the first 10 s set the timbre" (M L3146)."""
    if row.reference_path is None:
        return ""
    length = f", {row.clip_s:.1f} s" if row.clip_s is not None else ""
    return f"Reference clip {row.reference_name}{length} · the first 10 s set the timbre"


def _zoned(peaks: tuple[float, ...], clip_s: float | None) -> tuple[tuple[float, str], ...]:
    """Bars as (value, zone): "timbre" up to 10 s into the clip, "rest" after (M L2732-2733)."""
    count, length = len(peaks), clip_s or TIMBRE_S
    return tuple((float(value), "timbre" if (i + 0.5) / count * length <= TIMBRE_S else "rest")
                 for i, value in enumerate(peaks))


def _kept_line(shown: str, reason: str) -> str:
    """A .pt the delete left: "Kept cache/…/aiko.pt · belongs to Aiko" (a casefold twin, D-F5) or why it failed."""
    if reason.startswith(KEPT_PREFIX):
        return f"Kept {shown} · {reason[len(KEPT_PREFIX):]}"
    return f"Couldn't remove {shown} · {reason}"


def _busy_action(label: str, busy: bool) -> ActionState:
    return ActionState(enabled=not busy, label=label, reason=BUSY if busy else "", tone="plain")


def _play_action(voice_id: str, present: bool, playing: bool) -> ActionState:
    """The play button's accessible name and title; disabled while the clip is missing (NEW)."""
    name = (STOP_CLIP if playing else PLAY_CLIP).format(voice=voice_id)
    return ActionState(enabled=present, label=name, aria=name, tone="plain", tooltip="" if present else CLIP_MISSING)


def _edit_lines(voice_id: str, changes: dict[str, Any]) -> list[str]:
    """"Registry · aiko language set to Italian" (M L1873), "… default expressiveness set to 0.70" (NEW)."""
    lines = []
    if "language" in changes:
        code = changes["language"]
        lines.append(f"Registry · {voice_id} language set to {LANGUAGES.get(code, code)}")
    if "exaggeration" in changes:
        lines.append(f"Registry · {voice_id} default expressiveness set to {changes['exaggeration']:.2f}")
    return lines
