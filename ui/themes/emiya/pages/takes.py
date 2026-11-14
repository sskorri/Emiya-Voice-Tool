"""The Takes page (BUILD_SPEC §6.10; mockup L717-852): the take list on the left, the A/B compare card on the right.

Left: the caption with the list title and the count, then one 690 px card holding the search, the voice and model
menus (D-T2), the column header, the rows, the empty line and the footer (Export Starred…, Show Folder, Remove
Unstarred and the foot line). Right: "Compare", then the compare card while two takes are pinned, else its
placeholder. The header tabs (This Session, All, Starred) are the shell's.

Everything is drawn from TakesVM through passive bindings. Rows are kept by take id (ui.kit.elements.KeyedRows):
"Show N more", a new take or a purge adds or deletes only its own rows, and a star, a pin or the playing position
updates a row in place; the compare card is updated in place too (S03 risk 14). A visit starts at one page.
Clicks call TakesController and PlaybackController intents; colours live in static/pages/takes.css (R7).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nicegui import binding, ui
from nicegui.element import Element

from ui.contract.plans import ActionState
from ui.contract.viewmodels import CompareRowVM, CompareSideVM, CompareVM, TakeRowVM

from ..fx.bars import EmiyaBars
from .call_controls import TakeActions, stop_button
from ..widgets import (
    Card,
    Disableable,
    EmiyaButton,
    IconButton,
    KeyedRows,
    ListHost,
    PickButton,
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
    from ui.contract.viewmodels import PlaybackController, ShellVM, TakesController

__all__ = ["build"]

SEARCH_PLACEHOLDER = "Search text or voice"                                   # M L727
SEARCH_ARIA = "Search takes"
VOICE_TITLE = "Filter by voice"                                               # M L728
MODEL_TITLE = "Filter by model"                                               # M L729
ALL_VOICES = "All Voices"                                                     # M L3180
ALL_MODELS = "All Models"                                                     # M L3182
HEADER: tuple[tuple[str, str], ...] = (                                       # M L733-741
    ("#", "vt-tc-no"), ("Text", "vt-tc-text"), ("Voice", "vt-tc-voice"), ("Model", "vt-tc-model"),
    ("Expr", "vt-tc-num"), ("Guid", "vt-tc-num"), ("Length", "vt-tc-len"), ("When", "vt-tc-when"),
    ("", "vt-tc-tools"))
COMPARE_LABEL = "Compare"                                                     # M L778
PLACEHOLDER_TITLE = "Pin two takes as A and B"                                # M L845-846
PLACEHOLDER_TEXT = "Hover a row and use its A and B buttons. Pins stay set as you move between pages."
AB_ARIA = "Listen to A or B"                                                  # M L827

Pick = Callable[[str], Any]
Choice = tuple[str, str, str]                                                 # (key, label, marker suffix)


def build(ctx: ThemeContext) -> None:
    """The Takes page for this page load (sync, no subscriptions: R4)."""
    _TakesPage(ctx).build()


# ---- the filter menus (D-T2) --------------------------------------------------------------------------

class _MenuItem(Disableable):
    """One choice of a filter menu; the current one is marked (aria-checked, gold)."""

    def __init__(self, key: str, label: str, *, on: bool, on_pick: Pick) -> None:
        super().__init__(tag="button")
        self.key = key
        self.choice = label
        self.props["type"] = "button"
        self.classes("em-list vt-option")
        set_prop(self, "role", "menuitemradio")
        with self:
            TextSpan(label).classes("vt-option-name")
        self.set_on(on)
        self.on("click", lambda _event: on_pick(self.key))

    def set_on(self, on: bool) -> None:
        set_class(self, "is-on", on)
        set_flag(self, "aria-checked", on)


class _FilterMenu(Element):
    """A 150 px filter button with a chevron (M L728-729) and its menu: a click opens it; a choice, a click outside
    or Escape closes it. The open state is the page's own (it never reaches the controller)."""

    caption = binding.BindableProperty(on_change=lambda sender, value: sender._set_caption(value))
    choices = binding.BindableProperty(on_change=lambda sender, value: sender._set_choices(value))
    enabled = binding.BindableProperty(on_change=lambda sender, value: sender._set_enabled(bool(value)))

    def __init__(self, *, title: str, marker: str, on_pick: Pick) -> None:
        super().__init__("div")
        self.classes("vt-tfilter")
        self._marker = marker
        self._on_pick = on_pick
        self._items: list[_MenuItem] = []
        self._open = False
        with self:
            self.button = PickButton(tip=title, on_click=self.toggle).mark(marker)
            self._backdrop = Element("div").classes("vt-tmenu-backdrop").mark(f"{marker}.backdrop")
            self._list = Element("div").classes("vt-tmenu em-scroll")
        set_prop(self.button, "aria-haspopup", "menu")
        set_prop(self._backdrop, "aria-hidden", "true")
        set_prop(self._list, "role", "menu")
        set_prop(self._list, "aria-label", title)
        self._backdrop.on("click", lambda _event: self.close())
        self._list.on("keydown.esc", lambda _event: self.close())
        self.button.on("keydown.esc", lambda _event: self.close())
        self.caption = ""
        self.choices = ()
        self.enabled = True

    def toggle(self) -> None:
        self._set_open(not self._open)

    def close(self) -> None:
        self._set_open(False)

    def _set_open(self, on: bool) -> None:
        self._open = on
        set_class(self, "is-open", on)
        self.button.expanded = on

    def _pick(self, key: str) -> None:
        self.close()
        self._on_pick(key)

    def _set_caption(self, text: str) -> None:
        self.button.text = text
        for item in self._items:
            item.set_on(item.choice == text)

    def _set_choices(self, choices: tuple[Choice, ...]) -> None:
        self._list.clear()
        self._items = []
        with self._list:
            for key, label, suffix in choices:
                item = _MenuItem(key, label, on=label == self.caption, on_pick=self._pick).mark(
                    f"{self._marker}.{suffix}")
                self._items.append(item)

    def _set_enabled(self, on: bool) -> None:
        self.button.enabled = on
        if not on:
            self.close()


def _voice_choices(voices: tuple[str, ...]) -> tuple[Choice, ...]:
    """"All Voices", then every voice that has takes, most recent first."""
    return (("", ALL_VOICES, "all"), *((voice, voice, f"voice.{voice}") for voice in voices))


def _model_choices(models: tuple[tuple[str, str], ...]) -> tuple[Choice, ...]:
    """"All Models", then Multilingual, Standard and Turbo."""
    return (("", ALL_MODELS, "all"), *((backend, label, backend) for backend, label in models))


# ---- the rows -----------------------------------------------------------------------------------------

class _RowButton(Disableable):
    """The clickable part of a row: number, text, voice, model, Expr, Guid, Length and When."""

    def __init__(self, on_click: Callable[[], Any]) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-trow-main")
        self.on("click", lambda _event: on_click())


class _Pin(Disableable):
    """A row's A or B pin (22x22, M L757-758): washed gold with a ring while set, else shown on hover only."""

    def __init__(self, letter: str, on_click: Callable[[], Any]) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-pin vt-hov")
        with self:
            TextSpan(letter)
        self.on("click", lambda _event: on_click())

    def show(self, on: bool, label: str) -> None:
        set_flag(self, "aria-pressed", on)
        set_prop(self, "aria-label", label)
        set_class(self, "vt-hov", not on)


class _TakeRow(Element):
    """One take row (M L745-761), updated in place by show()."""

    def __init__(self, take_id: int, on_play: Callable[[int], Any], playback: PlaybackController) -> None:
        super().__init__("div")
        self.take_id = take_id
        self.classes("vt-trow")
        with self:
            self._main = _RowButton(lambda: on_play(take_id)).mark(f"takes.row.{take_id}")
            with self._main:
                TextSpan(str(take_id)).classes("vt-tc-no")
                self._cells = {name: TextSpan().classes(f"vt-tc-{name}")
                               for name in ("text", "voice", "model", "expr", "guid", "len", "when")}
            for name in ("expr", "guid"):
                self._cells[name].classes("vt-tc-num")
            with TakeActions(playback, take_id, f"takes.send.{take_id}").classes("vt-trow-tools"):
                self._pin_a = _Pin("A", lambda: playback.pin(take_id, "A")).mark(f"takes.pin_a.{take_id}")
                self._pin_b = _Pin("B", lambda: playback.pin(take_id, "B")).mark(f"takes.pin_b.{take_id}")
                self._star = IconButton("star", aria=f"Star Take {take_id}", size=24, icon_size=14, pressed=False,
                                        tooltip="", on_click=lambda: playback.star(take_id)).mark(
                    f"takes.star takes.star.{take_id}")

    def show(self, row: TakeRowVM) -> None:
        set_class(self, "on", row.current)
        set_class(self, "is-missing", row.missing)
        set_class(self, "vt-row-in", row.is_new)
        set_flag(self._main, "aria-pressed", row.current)
        cells = self._cells
        for name, value in (("text", row.text), ("voice", row.voice), ("model", row.model_label),
                            ("expr", row.expr_text), ("guid", row.guid_text), ("len", row.length_text),
                            ("when", row.when_text)):
            cells[name].text = value
        set_prop(cells["text"], "lang", row.lang)
        cells["when"].tip = row.when_tip
        self._pin_a.show(row.pin_a, f"{'Unpin' if row.pin_a else 'Pin'} Take {row.id} as A")
        self._pin_b.show(row.pin_b, f"{'Unpin' if row.pin_b else 'Pin'} Take {row.id} as B")
        self._star.pressed = row.starred
        set_prop(self._star, "aria-label", f"{'Unstar' if row.starred else 'Star'} Take {row.id}")
        set_class(self._star, "vt-hov", not row.starred)


class _TakeRows(KeyedRows):
    """The rows, kept by take id: "Show N more", a new take or a purge adds or deletes only the rows it changes,
    and a star, a pin or the playing take updates its row in place (R3-3)."""

    def __init__(self, on_play: Callable[[int], Any], playback: PlaybackController) -> None:
        super().__init__(lambda row: _TakeRow(row.id, on_play, playback), key=lambda row: row.id)
        self.classes("vt-trows")


# ---- the compare card ---------------------------------------------------------------------------------

class _Side(Element):
    """One side of the compare card: "A · Take 5" (gold while current), its length and 110 bars (M L781-801)."""

    def __init__(self) -> None:
        super().__init__("div")
        self.classes("vt-tcmp-side")
        with self:
            with Element("div").classes("vt-tcmp-head"):
                self.title = TextSpan(tag="p").classes("vt-tcmp-title")
                self.length = TextSpan().classes("vt-tcmp-len")
            self.bars = EmiyaBars("compare").classes("vt-tcmp-bars")

    def show(self, side: CompareSideVM) -> None:
        self.title.text = side.title
        set_class(self.title, "is-active", side.title_active)
        self.length.text = side.length_text
        self.bars.bars = side.bars
        self.bars.progress = side.progress if side.title_active else None


def _compare_row(row: CompareRowVM) -> None:
    """One row of the compare table; a differing row is gold-dim and semibold (M L810-814)."""
    with Element("div").classes("vt-tcmp-row" + (" is-diff" if row.differs else "")):
        TextSpan(row.label).classes("vt-tcmp-label")
        TextSpan(row.a).classes("vt-tcmp-val")
        TextSpan(row.b).classes("vt-tcmp-val")


class _CompareView(Element):
    """The compare card while two takes are pinned, else the placeholder (M L779-848); updated in place."""

    compare = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))

    def __init__(self, *, takes: TakesController, playback: PlaybackController, shell: ShellVM) -> None:
        super().__init__("div")
        self.classes("vt-tcmp")
        self._takes = takes
        self._playback = playback
        self._shell = shell
        with self:
            self._card = Card().classes("vt-tcmp-card").mark("takes.compare.card")
            with self._card:
                self._sides = (_Side().mark("takes.compare.side.a"), _Side().mark("takes.compare.side.b"))
                self._table()
                self._note = TextSpan(tag="p", tone="muted").classes("vt-tcmp-note").mark("takes.compare.note")
                self._transport()
                self._buttons()
            self._empty = Card().classes("vt-tcmp-empty").mark("takes.compare.placeholder")
            with self._empty:
                TextSpan(PLACEHOLDER_TITLE, tag="p").classes("vt-tcmp-empty-title")
                TextSpan(PLACEHOLDER_TEXT, tag="p").classes("vt-tcmp-empty-text")
        for side in self._sides:
            binding.bind_from(side.bars, "reduce_always", shell, "reduce_always")
        self.compare = None
        self._show(None)

    def _table(self) -> None:
        with Element("div").classes("vt-tcmp-table"):
            with Element("div").classes("vt-tcmp-thead"):
                TextSpan("Setting").classes("vt-tcmp-label")
                self._head_a = TextSpan().classes("vt-tcmp-val")
                self._head_b = TextSpan().classes("vt-tcmp-val")
            self._rows = ListHost(_compare_row).classes("vt-tcmp-rows").mark("takes.compare.rows")

    def _transport(self) -> None:
        with Row().classes("vt-tcmp-transport"):
            self._play = IconButton("play", aria="Play A", filled=True, icon_size=14,
                                    on_click=self._takes.compare_play).mark("takes.compare_play")
            self._time = TextSpan().classes("vt-tcmp-time").mark("takes.compare.time")
            Element("span").classes("vt-tcmp-spacer")
            self._ab = Segmented(aria=AB_ARIA, item_marker="takes.ab", on_change=self._listen).classes(
                "vt-tcmp-ab").mark("takes.ab")

    def _buttons(self) -> None:
        takes = self._takes
        with Element("div").classes("vt-tcmp-buttons"):
            self._keep_a = EmiyaButton("Keep A", size=30, on_click=lambda: takes.keep("A")).mark("takes.keep_a")
            self._keep_b = EmiyaButton("Keep B", size=30, on_click=lambda: takes.keep("B")).mark("takes.keep_b")
            self._reuse = EmiyaButton("Reuse A's Settings", size=30, on_click=self._reuse_settings).classes(
                "vt-tcmp-reuse").mark("takes.reuse")

    def _show(self, compare: CompareVM | None) -> None:
        self._card.set_visibility(compare is not None)
        self._empty.set_visibility(compare is None)
        if compare is None:
            return
        for view, side in zip(self._sides, compare.sides):
            view.show(side)
        self._head_a.text, self._head_b.text = compare.sides[0].title, compare.sides[1].title
        self._rows.items = compare.rows
        self._note.text = compare.note
        self._note.tone = "gold-dim" if compare.note_differs else "muted"
        self._play.action = ActionState(True, compare.play_label)
        self._play.set_icon("pause" if compare.playing else "play")
        self._time.text = compare.time_text
        self._ab.options = tuple((side.side.lower(), side.title, side.title_active) for side in compare.sides)
        self._ab.set_visibility(compare.show_ab)
        self._keep_a.action, self._keep_b.action, self._reuse.action = compare.keep_a, compare.keep_b, compare.reuse

    def _listen(self, key: str) -> None:
        """The A/B segment: that side becomes current at the same point of the line (M L1665-1668)."""
        compare = self.compare
        side = next((side for side in compare.sides if side.side.lower() == key), None) if compare else None
        if side is not None:
            self._playback.ab_switch(side.take_id)

    def _reuse_settings(self) -> None:
        route = self._takes.reuse_settings()
        if route and route != self._shell.route:
            ui.navigate.to(route)


# ---- the page -----------------------------------------------------------------------------------------

class _TakesPage:
    """One build of the page: its elements and their bindings to TakesVM."""

    def __init__(self, ctx: ThemeContext) -> None:
        behaviours = ctx.behaviours
        self._takes = behaviours.takes()
        self._playback = behaviours.playback()
        self._shell = behaviours.status().vm
        self._vm = self._takes.vm

    def build(self) -> None:
        self._takes.opened()                    # first: a revisit starts again at one page of rows
        with Element("div").classes("vt-takes"):
            with Element("section").classes("vt-tcol"):
                self._caption()
                with Card().classes("vt-tlist-card"):
                    self._search()
                    self._list()
                    self._footer()
            with Element("section").classes("vt-tcol").mark("takes.compare"):
                SectionLabel(COMPARE_LABEL)
                view = _CompareView(takes=self._takes, playback=self._playback, shell=self._shell)
                binding.bind_from(view, "compare", self._vm, "compare")

    def _caption(self) -> None:
        vm = self._vm
        with Element("div").classes("vt-tcaption"):
            TextSpan(tag="p").classes("vt-tcaption-title").mark("takes.title").bind_text_from(vm, "list_title")
            TextSpan().classes("vt-tcaption-count").mark("takes.count").bind_text_from(vm, "count_text")
            stop_button(self._playback, "takes.stop_sending")

    def _search(self) -> None:
        vm, takes = self._vm, self._takes
        with Element("div").classes("vt-tsearch"):
            TextField(placeholder=SEARCH_PLACEHOLDER, value=vm.query, aria=SEARCH_ARIA, kind="search",
                      on_change=lambda event: takes.set_query(event.value or "")).mark("takes.search")
            voices = _FilterMenu(title=VOICE_TITLE, marker="takes.filter_voice",
                                 on_pick=lambda key: takes.set_voice_filter(key or None))
            binding.bind_from(voices, "caption", vm, "voice_label")
            binding.bind_from(voices, "choices", vm, "voice_options", backward=_voice_choices)
            binding.bind_from(voices, "enabled", vm, "voice_options", backward=bool)
            models = _FilterMenu(title=MODEL_TITLE, marker="takes.filter_model",
                                 on_pick=lambda key: takes.set_model_filter(key or None))
            binding.bind_from(models, "caption", vm, "model_label")
            binding.bind_from(models, "choices", vm, "model_options", backward=_model_choices)

    def _list(self) -> None:
        vm, takes, playback = self._vm, self._takes, self._playback
        with Element("div").classes("vt-tbody"):
            with Element("div").classes("vt-thead"):
                for text, cell in HEADER:
                    TextSpan(text).classes(cell)
            with Element("div").classes("vt-tscroll em-scroll"):
                _TakeRows(takes.row_click, playback).mark("takes.list").bind_items_from(vm, "rows")
                more = EmiyaButton(tone="link", on_click=self._show_more).classes("vt-tmore").mark("takes.more")
                more.bind_action_from(vm, "more_count", backward=_more_action)
                more.bind_visibility_from(vm, "more_count", backward=bool)
                empty = TextSpan(tag="p").classes("vt-tempty").mark("takes.empty")
                empty.bind_text_from(vm, "empty_text")
                empty.bind_visibility_from(vm, "empty_text", backward=bool)

    def _footer(self) -> None:
        vm, takes = self._vm, self._takes
        with Element("div").classes("vt-tfoot"):
            EmiyaButton(size=30, on_click=takes.export_starred).mark("takes.export").bind_action_from(vm, "export")
            EmiyaButton(size=30, on_click=takes.show_folder).mark("takes.folder").bind_action_from(vm, "folder")
            EmiyaButton(size=30, on_click=takes.purge_unstarred).mark("takes.purge confirm.arm").bind_action_from(
                vm, "purge")
            TextSpan().classes("vt-tfoot-text").mark("takes.foot").bind_text_from(vm, "foot_text")

    def _show_more(self) -> None:
        """"Show N more" (S03 risk 14): the next rows of the filtered list."""
        self._takes.show_more()


def _more_action(count: int) -> ActionState:
    return ActionState(count > 0, f"Show {count} more", tone="link")
