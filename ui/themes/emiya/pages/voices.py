"""The Voices page (BUILD_SPEC §6.8; M L587-715): the Library list with its filter, and the Details card of one
voice with the registry edits and the actions on it.

The builder is sync and reads only the VoicesController's VoicesVM (R4, R5). Every element is bound passively to a
VM field and every control calls an intent; the voice an action targets is read from vm.details at the click.
The Library rows are kept by voice id, so a pick or an edit updates its rows in place.
The routes the intents return (Re-forge…, Forge on …, Replace…, Speak with) are opened here. Geometry and colours
live in static/pages/voices.css.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nicegui import binding, ui
from nicegui.element import Element
from nicegui.events import ValueChangeEventArguments

from ui.contract.plans import ActionState
from ui.contract.viewmodels import VoiceDetailsVM, VoiceListRowVM

from ..fx.bars import EmiyaBars
from ..widgets import (
    Card,
    Chip,
    EmiyaButton,
    GoldSlider,
    IconButton,
    KeyedRows,
    ListBox,
    PickButton,
    PlayButton,
    Row,
    SectionLabel,
    Segmented,
    TextField,
    TextSpan,
    set_class,
    set_flag,
    set_prop,
)

if TYPE_CHECKING:
    from ui.contract.theme import ThemeContext
    from ui.contract.viewmodels import VoicesController

__all__ = ["ALL_KEY", "PICK_TITLE", "VoicesPage", "build"]

ROUTE = "/voices"
FORGE_ROUTE = "/forge"
ALL_KEY = "all"                                       # the All segment's marker key (the VM's key is "")
LABEL_W = 160                                         # the details rows' label column (M L658)
EXPR_MIN, EXPR_MAX, EXPR_STEP = 0.25, 2.0, 0.05       # M L674
HEADER: tuple[tuple[str, str], ...] = (               # M L606-611
    ("Name", "vt-vcol-name"), ("Model", "vt-vcol-model"), ("Language", "vt-vcol-lang"),
    ("Expr", "vt-vcol-expr"), ("Clip", "vt-vcol-clip"), ("Created", "vt-vcol-created"))
FOOT_NOTE = "Saved in cache/registry.json · one cached .pt per voice"                          # M L631
EDIT_NOTE = "Edits here change the registry only; no model loads until you speak."              # M L697
DELETE_NOTE = "Delete removes the registry entry and the cached .pt; the WAV stays in voices/reference."   # M L704
PICK_TITLE = "Pick a language"                        # NEW (D-F2): the list replaces the mockup's cycling
CLIP_MISSING = "Reference clip missing"               # NEW
MISSING_SUFFIX = " · missing"                         # NEW
REVEAL = "Show in Explorer"                           # M L688
OFF = ActionState(False, "", tone="plain")


def build(ctx: ThemeContext) -> None:
    """The Voices page for one visit (sync, R4)."""
    VoicesPage(ctx).build()


class VoicesPage:
    """One visit of the Voices page: its elements, their bindings, and the handlers that call the intents."""

    def __init__(self, ctx: ThemeContext) -> None:
        self._voices: VoicesController = ctx.behaviours.voices()
        self._vm = self._voices.vm
        self._expr_value: TextSpan | None = None

    def build(self) -> None:
        """Library | Details (grid 1.15fr | 1fr, M L588); a changed registry is re-read as the page opens."""
        with Element("div").classes("vt-voices"):
            with Element("section").classes("vt-voices-col"):
                SectionLabel("Library")
                self._library()
            with Element("section").classes("vt-voices-col"):
                SectionLabel("Details")
                self._details()
                self._no_details()
        self._voices.opened()

    # ---------- Library (M L590-634) ----------

    def _library(self) -> None:
        vm = self._vm
        with Card().classes("vt-vlib"):
            with Element("div").classes("vt-vlib-tools"):
                TextField(placeholder="Filter voices", value=vm.query, aria="Filter voices", kind="search",
                          on_change=self._on_query).mark("voices.filter")
                segments = Segmented(aria="Show voices for", item_marker="voices.backend_filter",
                                     on_change=self._on_segment).mark("voices.backend_filter")
                binding.bind_from(segments, "options", vm, "segments", backward=_segment_options)
            TextSpan(tag="p").classes("vt-vlib-count").mark("voices.count").bind_text_from(vm, "count_text")
            with Element("div").classes("vt-vlib-table"):
                with Element("div").classes("vt-vlib-head"):
                    for text, column in HEADER:
                        TextSpan(text).classes(column)
                with Element("div").classes("vt-vlib-list em-scroll").mark("voices.list"):
                    KeyedRows(self._row, key=lambda row: row.voice_id).bind_items_from(vm, "rows")
                    empty = TextSpan(tag="p").classes("vt-vlib-empty").mark("voices.empty")
                    empty.bind_text_from(vm, "empty_text")
                    empty.bind_visibility_from(vm, "empty_text", backward=bool)
            with Element("div").classes("vt-vlib-foot"):
                EmiyaButton("Forge New Voice", size=30, on_click=self._on_forge_new).mark("voices.new")
                TextSpan(FOOT_NOTE).classes("vt-vlib-note")

    def _row(self, row: VoiceListRowVM) -> _VoiceRow:
        """One Library row, kept by voice id: a pick or an edit updates it in place (R3-4)."""
        return _VoiceRow(row, self._voices.select).mark(f"voices.row.{row.voice_id}")

    # ---------- Details (M L636-712) ----------

    def _details(self) -> None:
        card = Card().classes("vt-vd").mark("voices.edit")
        card.bind_visibility_from(self._vm, "details", backward=lambda details: details is not None)
        with card:
            self._head()
            self._wave()
            with Element("div").classes("vt-vd-rows"):
                self._language_row()
                self._expr_row()
                self._reference_row()
                self._cache_row()
                self._consent_row()
            TextSpan(EDIT_NOTE, tag="p").classes("vt-vd-note")
            self._actions()
            TextSpan(DELETE_NOTE, tag="p").classes("vt-vd-delnote")

    def _head(self) -> None:
        with Element("div").classes("vt-vd-head"):
            with Element("div").classes("vt-vd-titles"):
                title = TextSpan(tag="p").classes("vt-vd-title").mark("voices.title")
                self._bind(title, "text", lambda d: d.title, "")
                caption = TextSpan(tag="p").classes("vt-vd-caption").mark("voices.caption")
                self._bind(caption, "text", lambda d: d.caption, "")
            chip = Chip(size=20).mark("voices.chip")
            self._bind(chip, "text", lambda d: d.chip_text, "")
            self._bind(chip, "tone", lambda d: d.chip_tone, "off")

    def _wave(self) -> None:
        with Element("div").classes("vt-vd-wave"):
            play = PlayButton(aria="Play the reference clip", on_click=self._on_play).mark("voices.play")
            self._bind(play, "action", lambda d: d.play, OFF)
            self._bind(play, "playing", lambda d: d.playing, False)
            bars = EmiyaBars("voices").mark("voices.bars")
            self._bind(bars, "bars", lambda d: d.bars, ())
            self._bind(bars, "aria", lambda d: d.bars_aria, "")

    def _language_row(self) -> None:
        with Row("Language", label_width=LABEL_W):
            with Element("div").classes("vt-pick") as pick_box:
                pick = PickButton(tip=PICK_TITLE, on_click=self._on_language).mark("voices.edit.language")
                self._bind(pick, "text", lambda d: d.language_name, "")
                self._bind(pick, "enabled", lambda d: d.language_mode == "pick", False)
                self._bind(pick, "expanded", lambda d: d.lang_open, False)
                listbox = ListBox(on_pick=self._on_pick_language, on_close=self._on_close_language, aria="Language",
                                  item_marker="voices.edit.language.item").mark("voices.edit.language.list")
                self._bind(listbox, "open", lambda d: d.lang_open, False)
                self._bind(listbox, "items", lambda d: d.lang_items, ())
            self._bind(pick_box, "visible", lambda d: d.language_mode != "locked", False)
            with Element("div").classes("vt-vd-locked").mark("voices.lang_locked") as locked:
                name = TextSpan().classes("vt-vd-locked-name")
                self._bind(name, "text", lambda d: d.language_name, "")
                Chip("English only", tone="off", size=20)
            self._bind(locked, "visible", lambda d: d.language_mode == "locked", False)

    def _expr_row(self) -> None:
        row = Row("Default Expressiveness", label_width=LABEL_W)
        self._bind(row, "off", lambda d: not d.expr_enabled, True)
        with row, Element("div").classes("vt-vd-expr"):
            slider = GoldSlider(min=EXPR_MIN, max=EXPR_MAX, step=EXPR_STEP, value=0.5, aria="Default Expressiveness",
                                on_input=self._on_expr_input, on_release=self._on_expr_release).mark("voices.edit.expr")
            self._bind(slider, "value", lambda d: d.expr, 0.5)
            self._bind(slider, "enabled", lambda d: d.expr_enabled, False)
            self._expr_value = TextSpan().classes("vt-vd-expr-value").mark("voices.edit.expr_value")
            self._bind(self._expr_value, "text", lambda d: d.expr_text, "—")

    def _reference_row(self) -> None:
        with Row("Reference clip", label_width=LABEL_W):
            ref = TextSpan().classes("vt-vd-text").mark("voices.ref")
            self._bind(ref, "text", lambda d: d.ref_text, "")
            self._bind(ref, "tone", lambda d: "bad" if d.ref_missing else None, None)
            self._bind(ref, "tip", lambda d: d.ref_tip, "")
            replace = EmiyaButton("Replace…", size=26, on_click=self._on_replace).mark("voices.replace")
            self._bind(replace, "action", lambda d: d.replace, OFF)

    def _cache_row(self) -> None:
        with Row("Cache", label_width=LABEL_W):
            with Element("span").classes("vt-vd-text").mark("voices.cache"):
                path = TextSpan()
                self._bind(path, "text", lambda d: d.cache_text, "")
                missing = TextSpan(MISSING_SUFFIX, tone="bad")
                self._bind(missing, "visible", lambda d: d.cache_missing, False)
            reveal = IconButton("folder", aria=REVEAL, on_click=self._on_reveal).mark("voices.reveal")
            self._bind(reveal, "enabled", lambda d: d.reveal_enabled, False)

    def _consent_row(self) -> None:
        with Row("Consent", label_width=LABEL_W):
            dot = TextSpan().classes("vt-vd-dot")
            set_prop(dot, "aria-hidden", "true")
            self._bind(dot, "tone", lambda d: "gold" if d.consent_ok else "bad", "bad")
            text = TextSpan().classes("vt-vd-consent").mark("voices.consent")
            self._bind(text, "text", lambda d: d.consent_text, "")
            self._bind(text, "tone", lambda d: "text" if d.consent_ok else "bad", "text")

    def _actions(self) -> None:
        with Element("div").classes("vt-vd-actions"):
            speak = EmiyaButton(tone="primary", size=34, on_click=self._on_speak).mark("voices.speak")
            self._bind(speak, "action", lambda d: ActionState(True, d.speak_label), OFF)
            reforge = EmiyaButton("Re-forge…", size=30, on_click=self._on_reforge).mark("voices.reforge")
            self._bind(reforge, "action", lambda d: d.reforge, OFF)
            copy = EmiyaButton(size=30, on_click=self._on_copy).mark("voices.copy")
            self._bind(copy, "action", lambda d: d.copy, OFF)
            self._bind(copy, "visible", lambda d: d.copy_visible, False)
            delete = EmiyaButton("Delete", size=30, on_click=self._on_delete).mark("voices.delete confirm.arm")
            self._bind(delete, "action", lambda d: d.delete, OFF)

    def _no_details(self) -> None:
        card = Card().classes("vt-vd-none").mark("voices.no_details")
        card.bind_visibility_from(self._vm, "no_details_text", backward=bool)
        with card:
            TextSpan(tag="p").classes("vt-vd-none-text").bind_text_from(self._vm, "no_details_text")

    def _bind(self, element: Any, prop: str, pick: Callable[[VoiceDetailsVM], Any], default: Any) -> None:
        """element.<prop> follows a field of vm.details (default while there are no details); a passive link."""
        binding.bind_from(element, prop, self._vm, "details",
                          backward=lambda details: default if details is None else pick(details))

    # ---------- handlers ----------

    def _current(self) -> str:
        """The voice the details show, read at the click."""
        details = self._vm.details
        return details.voice_id if details is not None else ""

    def _go(self, route: str | None) -> None:
        if route and route != ROUTE:
            ui.navigate.to(route)

    def _on_query(self, event: ValueChangeEventArguments) -> None:
        self._voices.set_filter(query=str(event.value or ""))

    def _on_segment(self, key: str) -> None:
        self._voices.set_filter(backend="" if key == ALL_KEY else key)

    def _on_forge_new(self) -> None:
        ui.navigate.to(FORGE_ROUTE)                       # the draft stays as it is (M L2935)

    def _on_play(self) -> None:
        if self._current():
            self._voices.toggle_clip_playback(self._current())

    def _on_language(self) -> None:
        self._voices.toggle_language_list()

    def _on_close_language(self) -> None:
        self._voices.toggle_language_list(False)

    async def _on_pick_language(self, code: str) -> None:
        if self._current():
            await self._voices.edit(self._current(), language=code)

    def _on_expr_input(self, value: float) -> None:
        """While dragging, the value label follows the thumb; the registry is written on release."""
        details = self._vm.details
        if details is not None and details.expr_enabled and self._expr_value is not None:
            self._expr_value.text = f"{value:.2f}"

    async def _on_expr_release(self, value: float) -> None:
        voice_id = self._current()
        if not voice_id:
            return
        self._on_expr_input(value)
        await self._voices.edit(voice_id, exaggeration=round(value, 2))
        details = self._vm.details
        if self._expr_value is not None:                  # a refused edit: the label shows the stored value again
            self._expr_value.text = details.expr_text if details is not None else "—"

    async def _on_replace(self) -> None:
        if self._current():
            self._go(await self._voices.replace_clip(self._current()))

    async def _on_reveal(self) -> None:
        if self._current():
            await self._voices.reveal_cache(self._current())

    def _on_speak(self) -> None:
        if self._current():
            self._go(self._voices.speak_with(self._current()))

    def _on_reforge(self) -> None:
        if self._current():
            self._go(self._voices.reforge(self._current()))

    def _on_copy(self) -> None:
        if self._current():
            self._go(self._voices.forge_copy(self._current()))

    async def _on_delete(self) -> None:
        if self._current():
            await self._voices.delete(self._current())


# ---- page-local elements -------------------------------------------------------------------------------

class _VoiceRow(Element):
    """One Library row (M L615-622): a button that shows its voice in the details; show() updates it in place."""

    def __init__(self, row: VoiceListRowVM, on_select: Callable[[str], Any]) -> None:
        super().__init__("button")
        self.voice_id = row.voice_id
        self.props["type"] = "button"
        self.classes("vt-li vt-vrow")
        self.on("click", lambda _event: on_select(self.voice_id))
        with self:
            TextSpan(row.voice_id).classes("vt-vrow-name vt-vcol-name")
            self._model = TextSpan(tip=row.model_tip).classes("vt-vrow-sub vt-vrow-model vt-vcol-model")
            with self._model:
                self._dot = Element("span").classes("vt-vrow-dot")
                set_prop(self._dot, "aria-hidden", "true")
                self._model_label = TextSpan(row.model_label)
            self._language = TextSpan(row.language_name).classes("vt-vrow-sub vt-vrow-lang vt-vcol-lang")
            self._expr = TextSpan(row.expr_text).classes("vt-vrow-sub vt-vcol-expr")
            self._clip = TextSpan(row.clip_text).classes("vt-vrow-sub vt-vcol-clip")
            self._created = TextSpan(row.created_text, tip=row.created_tip).classes("vt-vrow-sub vt-vcol-created")
        self.show(row)

    def show(self, row: VoiceListRowVM) -> None:
        """Draw one VoiceListRowVM: the selection, the model and its residency, language, Expr, clip, created."""
        set_class(self, "on", row.selected)
        set_flag(self, "aria-pressed", row.selected)
        self._model.tip = row.model_tip
        set_class(self._dot, "is-resident", row.model_resident)
        self._model_label.text = row.model_label
        self._language.text = row.language_name
        self._expr.text = row.expr_text
        self._clip.text = row.clip_text
        self._clip.tip = CLIP_MISSING if row.clip_missing else ""
        set_class(self._clip, "is-missing", row.clip_missing)
        self._created.text = row.created_text
        self._created.tip = row.created_tip

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self.voice_id]


def _segment_options(segments: tuple[tuple[str, str, bool], ...]) -> tuple[tuple[str, str, bool], ...]:
    """The VM's segments with the All key spelled "all", so its marker is voices.backend_filter.all."""
    return tuple((key or ALL_KEY, label, on) for key, label, on in segments)
