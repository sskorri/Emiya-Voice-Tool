"""The Emiya shell (BUILD_SPEC §6.2): rail, header, Model dock, VRAM block, switch popover, Log, toast, the art
and FX layers with the four fixed FX anchors, the audio player and the key dispatchers, around ui.sub_pages.

THEME.build_shell builds it once per page load. Everything it shows comes from the controllers' view models
(mostly the StatusController's ShellVM) through passive bindings, and every control calls an intent. Pages
mount inside the content box, the trace field of the forge FX (`.emiya-field`); the FX host, the anchors and
the player sit outside ui.sub_pages, so navigation never remounts them. This is the only theme module that
subscribes to core events (R4): `changed` and the Effects prefs drive the forge field, `forge_started` grows
its blade, `dropped` hands a native drop to Forge.

Layers back to front (M L21-28, L114, L1122, L1141, L1153, L1159): art 0, FX 1, main column 2, rail 3, Log 20,
toast 30, popover and search 40, frame 50. Geometry, type and colours live in static/emiya.css.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nicegui import binding, context, ui
from nicegui.element import Element
from nicegui.events import GenericEventArguments, ValueChangeEventArguments

from ui.contract.plans import ActionState, ConfirmRequest, Toast
from ui.contract.routes import PAGE_ID, Route
from ui.contract.viewmodels import DockRowVM, KeyRowVM, SearchItem
from ui.kit.audio import Player
from ui.kit.keys import KeyDispatcher
from ui.kit.window import DRAG_CLASS

from .fx.forge_field import BLADE_ANCHOR, EmiyaForgeField, add_anchors, field_state
from .widgets import (
    Chip,
    Disableable,
    DotIndicator,
    DotSwitch,
    EmiyaButton,
    IconButton,
    ListHost,
    Logo,
    Meter,
    Popover,
    Segmented,
    TextField,
    TextSpan,
    flash_class,
    set_class,
    set_flag,
    set_prop,
    swap_classes,
)

if TYPE_CHECKING:
    from ui.contract.manifest import ThemeManifest
    from ui.contract.theme import ThemeContext

__all__ = [
    "CHIP_TITLE", "DROP_ELSEWHERE", "ESCAPE_ID", "NAV", "SEARCH_ARIA", "SETTINGS_TABS", "TAKES_TABS", "Shell",
    "build_shell",
]

FX_LOG = logging.getLogger("voicetool.fx")

NAV: tuple[tuple[Route, str], ...] = (                              # M L57-59; Settings sits below the dock
    ("/", "Speak"), ("/voices", "Voices"), ("/forge", "Forge"), ("/takes", "Takes"), ("/models", "Models"))
SETTINGS: tuple[Route, str] = ("/settings", "Settings")              # M L111
TAKES_TABS = (("session", "This Session"), ("all", "All"), ("starred", "Starred"))       # M L2710
SETTINGS_TABS = (("general", "General"), ("effects", "Effects"), ("keys", "Keys"), ("voice_chat", "Voice Chat"))
SEARCH_PLACEHOLDER = "Search"                                        # M L40
SEARCH_ARIA = "Search voices, takes and actions"
CHIP_TITLE = "Open the Log"                                          # M L126
DROP_ELSEWHERE = "Open Forge to use a dropped clip"                  # NEW (§6.2.10)
ESCAPE_COMBO, ESCAPE_ID = "Escape", "escape"                         # D12: Escape closes the overlays
FX_ON = "fx_on"                                                      # Settings › Background Effects
FAIL = re.compile("fail", re.IGNORECASE)                             # red Log lines (M L3111)
TAKES_ROUTE: Route = "/takes"
SETTINGS_ROUTE: Route = "/settings"
FORGE_ROUTE: Route = "/forge"

# Browser halves of the search box (§6.2.2): Enter runs the first result, but not while an IME composes;
# Escape clears and leaves the box; a result acts on mousedown, so the box keeps focus until it ran.
ENTER_JS = ("(e) => { if (e.isComposing || e.keyCode === 229) return; e.preventDefault(); "
            "emit(e.target.value); e.target.blur(); }")
ESCAPE_JS = "(e) => { emit(); e.target.blur(); }"
PICK_JS = "(e) => { e.preventDefault(); emit(); if (document.activeElement) document.activeElement.blur(); }"


def build_shell(ctx: ThemeContext, mount_pages: Callable[[], None], manifest: ThemeManifest) -> Shell:
    """Build the shell for one page load; mount_pages() runs once, inside the content box."""
    shell = Shell(ctx, manifest)
    shell.build(mount_pages)
    return shell


# ---- shell-only elements ------------------------------------------------------------------------------

class ShellRoot(Element):
    """The 1280x800 shell box (`shell` is the Shell that built it). reduce_always follows ShellVM, so a Reduce
    Motion change reaches the shell."""

    reduce_always = binding.BindableProperty(on_change=lambda sender, value: sender.shell._on_reduce(bool(value)))

    def __init__(self, shell: Shell) -> None:
        super().__init__("div")
        self.shell = shell
        self.classes("vt-shell")
        self.reduce_always = False


class _NavButton(TextSpan):
    """A rail page button; `current` (the shell's route) gives the tint, gold text and aria-current."""

    current = binding.BindableProperty(on_change=lambda sender, value: sender._mark(bool(value)))

    def __init__(self, label: str, route: Route, on_go: Callable[[Route], Any]) -> None:
        super().__init__(label, tag="button")
        self.props["type"] = "button"
        self.classes("vt-navbtn")
        self.route = route
        self.current = False
        self.on("click", lambda _e: on_go(self.route))

    def _mark(self, on: bool) -> None:
        set_class(self, "is-current", on)
        set_prop(self, "aria-current", "page" if on else None)


class _DockRow(Disableable):
    """One radio of the Model dock (M L69-83), updated in place so its flash and spark play out."""

    def __init__(self, backend: str, on_pick: Callable[[str], Any]) -> None:
        super().__init__(tag="button")
        self.backend = backend
        self.props["type"] = "button"
        self.classes("vt-dockrow")
        set_prop(self, "role", "radio")
        with self:
            self.indicator = DotIndicator()
            self._label = TextSpan().classes("vt-dock-label")
            self._info = TextSpan().classes("vt-dock-info")
        self._flash_seq = 0
        self._spark_seq = 0
        self._flash_class = ""
        self.on("click", lambda _e: on_pick(self.backend))

    @property
    def label(self) -> str:
        return self._label.text

    @property
    def info(self) -> TextSpan:
        """The info text on the right ("3.4 GB", "~14 s", "Freeing", "7 s", "Failed")."""
        return self._info

    def show(self, row: DockRowVM) -> None:
        set_flag(self, "aria-checked", row.state == "resident")
        set_prop(self, "title", row.tip)
        self.enabled = row.enabled
        set_class(self, "is-on", row.state in ("resident", "loading"))
        set_class(self, "is-dim", row.dim)
        self.indicator.state = row.state
        self.indicator.ring = row.ring
        self._label.text = row.label
        self._info.text = row.info
        self._info.tone = row.info_tone
        if row.flash_seq != self._flash_seq:
            self._flash_seq = row.flash_seq
            self._flash_class = swap_classes(self, self._flash_class, flash_class(row.flash_seq))
        if row.spark_seq != self._spark_seq:
            self._spark_seq = row.spark_seq
            self.indicator.spark()

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._label.text]


class _DockRows(Element):
    """The radiogroup "Model in VRAM": one _DockRow per enabled backend, reused while the backends stay."""

    rows = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))

    def __init__(self, on_pick: Callable[[str], Any]) -> None:
        super().__init__("div")
        self.classes("vt-dock-rows")
        set_prop(self, "role", "radiogroup")
        set_prop(self, "aria-label", "Model in VRAM")
        self._on_pick = on_pick
        self._rows: dict[str, _DockRow] = {}
        self.rows = ()

    def _show(self, rows: tuple[DockRowVM, ...]) -> None:
        backends = [row.backend for row in rows]
        if backends != list(self._rows):
            self.clear()
            self._rows = {}
            with self:
                for backend in backends:
                    self._rows[backend] = _DockRow(backend, self._on_pick).mark(f"dock.row.{backend}")
        for row in rows:
            self._rows[row.backend].show(row)


class _Art(Element):
    """The saber art (895x893 PNG at 0.32): left 729, 620 high on Forge; left 796, 540 high on an empty Speak."""

    page = binding.BindableProperty(on_change=lambda sender, value: sender._place(value))

    def __init__(self, src: str) -> None:
        super().__init__("img")
        self.classes("vt-art")
        self.props["alt"] = ""
        self.props["src"] = src
        self._page_class = ""
        self.page = ""

    def _place(self, page: str) -> None:
        self._page_class = swap_classes(self, self._page_class, f"is-{page}" if page else "")


class _ToastView(TextSpan):
    """One of the two toast boxes: odd and even toasts alternate, so a new toast replays the enter animation."""

    toast = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))

    def __init__(self, parity: int, on_click: Callable[[], Any]) -> None:
        super().__init__(tag="div")
        self.classes("vt-toast")
        set_prop(self, "role", "status")
        self._parity = parity
        self.toast = None
        self._show(None)
        self.on("click", lambda _e: on_click())

    def _show(self, toast: Toast | None) -> None:
        on = toast is not None and toast.seq % 2 == self._parity
        if on and toast is not None:
            self.text = toast.text
            set_class(self, "is-bad", toast.bad)
        self.set_visibility(on)


# ---- the shell ----------------------------------------------------------------------------------------

class Shell:
    """One page load's shell: its elements, its bindings and the handlers that turn clicks into intents."""

    def __init__(self, ctx: ThemeContext, manifest: ThemeManifest) -> None:
        self._ctx = ctx
        self._core = ctx.core
        self._manifest = manifest
        behaviours = ctx.behaviours
        self._status = behaviours.status()
        self._vm = self._status.vm
        self._models = behaviours.models()
        self._playback = behaviours.playback()
        self._commands = behaviours.commands()
        self._search = behaviours.search()
        self._takes = behaviours.takes()
        self._settings = behaviours.settings()
        self._body = ui.query("body")
        self._field: EmiyaForgeField | None = None
        self._fx_host: Element | None = None
        self._search_box: TextField | None = None
        self.root: ShellRoot | None = None

    @property
    def ctx(self) -> ThemeContext:
        """The page load's ThemeContext (its Behaviours hold this client's controllers)."""
        return self._ctx

    @property
    def field(self) -> EmiyaForgeField | None:
        """The forge field, or None while Background Effects is off."""
        return self._field

    # ---------- build ----------

    def build(self, mount_pages: Callable[[], None]) -> None:
        """The whole shell; the router's initial path sets the title, tabs and nav before anything shows."""
        router = context.client.sub_pages_router
        self._status.on_route(router.current_path)
        with ShellRoot(self).mark("shell") as root:
            self.root = root
            binding.bind_from(root, "reduce_always", self._vm, "reduce_always")
            self._art()
            self._fx()
            add_anchors()
            self._rail()
            self._main(mount_pages)
            self._ask()
            self._log()
            self._toasts()
            set_prop(Element("div").classes("vt-frame"), "aria-hidden", "true")
            self._plumbing()
            self._subscribe()
        router.on_path_changed(self._on_path)

    def _art(self) -> None:
        with Element("div").classes("vt-art-layer") as layer:
            set_prop(layer, "aria-hidden", "true")
            art = _Art(self._ctx.asset("img/saber-art.png")).mark("shell.art")
            binding.bind_from(art, "page", self._vm, "art_page")
            art.bind_visibility_from(self._vm, "art_visible")

    def _fx(self) -> None:
        with Element("div").classes("vt-fx-host") as host:
            set_prop(host, "aria-hidden", "true")
            self._fx_host = host
            if self._ctx.prefs.get(FX_ON):
                self._field = self._make_field()

    # ---------- the rail ----------

    def _rail(self) -> None:
        with Element("aside").classes("vt-rail") as rail:
            set_prop(rail, "aria-label", "Emiya Voice")
            self._brand()
            self._search_area()
            with Element("nav").classes("vt-nav") as nav:
                set_prop(nav, "aria-label", "Pages")
                for route, label in NAV:
                    self._nav_button(route, label)
            Element("div").classes("vt-spacer")
            self._dock()
            self._nav_button(*SETTINGS)

    def _brand(self) -> None:
        with Element("div").classes(f"vt-brand {DRAG_CLASS}").mark("shell.brand"):
            Logo(45, -2, -0.5, asset=self._ctx.asset).bind_still_from(self._vm).mark("shell.logo")
            with Element("p").classes("vt-wordmark"):
                TextSpan("Emiya").classes("vt-wordmark-name")
                TextSpan("Voice").classes("vt-wordmark-sub")

    def _search_area(self) -> None:
        with Element("div").classes("vt-search"):
            box = TextField(placeholder=SEARCH_PLACEHOLDER, aria=SEARCH_ARIA, kind="search",
                            on_change=self._on_query).mark("shell.search")
            box.on("keydown.enter", self._on_search_enter, js_handler=ENTER_JS)
            box.on("keydown.esc", lambda _e: self._close_search(), js_handler=ESCAPE_JS)
            self._search_box = box
            badge = TextSpan().classes("vt-search-badge").mark("shell.search.badge")
            set_prop(badge, "aria-hidden", "true")
            badge.bind_text_from(self._vm, "search_badge")
            badge.bind_visibility_from(self._vm, "search_badge", backward=bool)
            with Element("div").classes("vt-search-results em-scroll vt-pop-in").mark("shell.search.results"):
                hint = TextSpan(tag="p").classes("vt-search-hint").mark("shell.search.hint")
                hint.bind_text_from(self._vm, "search_hint")
                hint.bind_visibility_from(self._vm, "search_hint", backward=bool)
                ListHost(self._search_result).bind_items_from(
                    self._vm, "search_items", backward=lambda items: tuple(enumerate(items)))

    def _search_result(self, entry: tuple[int, SearchItem]) -> None:
        index, item = entry
        with TextSpan(tag="button").classes("em-list vt-result" + (" is-first" if index == 0 else "")).mark(
                f"shell.search.result shell.search.result.{item.item_id}") as button:
            button.props["type"] = "button"
            TextSpan(item.label).classes("vt-result-label")
            TextSpan(item.route_text).classes("vt-result-route")
        button.on("mousedown", lambda _e, item_id=item.item_id: self._run_search(item_id), js_handler=PICK_JS)

    def _nav_button(self, route: Route, label: str) -> None:
        button = _NavButton(label, route, self._go).mark(f"nav.{PAGE_ID[route]}")
        binding.bind_from(button, "current", self._vm, "route", backward=lambda path, route=route: path == route)

    def _dock(self) -> None:
        vm = self._vm
        with Element("section").classes("vt-model") as section:
            set_prop(section, "aria-label", "Model")
            TextSpan("Model", tag="p").classes("vt-section-label")
            with Element("div").classes("vt-dock-panel").mark("shell.dock"):
                rows = _DockRows(self._pick_dock_row)
                binding.bind_from(rows, "rows", vm, "dock_rows")
                set_prop(Element("div").classes("vt-divider"), "aria-hidden", "true")
                self._vram()
                status = TextSpan(tag="p").classes("vt-dock-status").mark("shell.dock_status")
                set_prop(status, "role", "status")
                set_prop(status, "aria-live", "polite")
                status.bind_text_from(vm, "dock_status")
                binding.bind_from(status, "tone", vm, "dock_status_tone")
                free = EmiyaButton("Free VRAM", size=26, on_click=self._models.free_vram).classes(
                    "vt-dock-btn").mark("dock.free")
                free.bind_action_from(vm, "free")
                free.bind_visibility_from(vm, "show_failed_actions", backward=lambda failed: not failed)
                with Element("div").classes("vt-dock-failed") as failed:
                    failed.bind_visibility_from(vm, "show_failed_actions")
                    retry = EmiyaButton("Retry", size=26, on_click=self._retry).classes("vt-dock-btn").mark(
                        "dock.retry")
                    retry.bind_action_from(vm, "retry")
                    default = EmiyaButton(size=26, on_click=self._load_default).classes("vt-dock-btn").mark(
                        "dock.load_default")
                    default.bind_action_from(vm, "load_default")
                    default.bind_visibility_from(vm, "load_default", backward=lambda action: bool(action.label))

    def _vram(self) -> None:
        vm = self._vm
        with TextSpan(tag="div").classes("vt-vram").mark("shell.vram") as block:
            binding.bind_from(block, "tip", vm, "vram_tip")
            with Element("div").classes("vt-vram-head"):
                TextSpan("VRAM").classes("vt-vram-name")
                value = TextSpan().classes("vt-vram-value").mark("shell.vram.value")
                value.bind_text_from(vm, "vram_value")
                binding.bind_from(value, "tone", vm, "vram_gold", backward=lambda gold: "gold" if gold else None)
            meter = Meter().mark("shell.vram.meter")
            binding.bind_from(meter, "app_pct", vm, "vram_app_pct")
            binding.bind_from(meter, "other_pct", vm, "vram_other_pct")
            binding.bind_from(meter, "aria", vm, "vram_tip")
            meter.bind_visibility_from(vm, "meter_visible")
            detail = TextSpan().classes("vt-vram-detail").mark("shell.vram.detail")
            detail.bind_text_from(vm, "vram_detail")

    # ---------- the main column ----------

    def _main(self, mount_pages: Callable[[], None]) -> None:
        with Element("div").classes("vt-main"):
            self._header()
            with Element("div").classes("vt-content em-scroll emiya-field").mark("shell.content"):
                mount_pages()

    def _header(self) -> None:
        vm = self._vm
        with Element("header").classes(f"vt-header {DRAG_CLASS}").mark("shell.header"):
            title = TextSpan(tag="h1").classes(f"vt-title {DRAG_CLASS}").mark("shell.title")
            title.bind_text_from(vm, "title")
            self._tabs("Takes", TAKES_TABS, TAKES_ROUTE, self._takes, "takes")
            self._tabs("Settings", SETTINGS_TABS, SETTINGS_ROUTE, self._settings, "settings")
            chip = Chip(size=24, tag="button", on_click=self._status.toggle_log).mark("status")
            set_prop(chip, "title", CHIP_TITLE)
            set_prop(chip, "aria-live", "polite")
            binding.bind_from(chip, "text", vm, "chip_text")
            binding.bind_from(chip, "tone", vm, "chip_tone")
            binding.bind_from(chip, "dot", vm, "chip_dot")
            with Element("div").classes("vt-winbtns"):
                log = IconButton("log", aria="Log", pressed=False, on_click=self._status.toggle_log).classes(
                    "vt-logbtn").mark("shell.log")
                binding.bind_from(log, "pressed", vm, "log_open")
                IconButton("minimize", aria="Minimize", on_click=self._status.minimize).mark("window.minimize")
                close = IconButton("close", aria=vm.close.aria or vm.close.label, danger=True,
                                   on_click=self._status.request_quit).mark("window.close")
                close.bind_action_from(vm, "close")

    def _tabs(self, title: str, tabs: tuple[tuple[str, str], ...], route: Route, controller: Any, page: str) -> None:
        group = Segmented(tabs, aria=title, tabs=True, item_marker=f"{page}.tab",
                          on_change=controller.set_tab).classes("vt-tabs").mark(f"{page}.tabs")
        binding.bind_from(group, "value", controller.vm, "tab")
        group.bind_visibility_from(self._vm, "route", backward=lambda path: path == route)

    # ---------- overlays ----------

    def _ask(self) -> None:
        vm = self._vm
        with Popover(role="dialog", on_close=self._models.dismiss).classes("vt-ask").mark("confirm.ask") as pop:
            binding.bind_from(pop, "open", vm, "ask", backward=lambda req: req is not None)
            binding.bind_from(pop, "aria", vm, "ask", backward=_ask_title)
            title = TextSpan(tag="p").classes("vt-ask-title")
            title.bind_text_from(vm, "ask", backward=_ask_title)
            with Element("div").classes("vt-ask-lines"):
                ListHost(_ask_line).classes("vt-ask-list").bind_items_from(
                    vm, "ask", backward=lambda req: req.lines if req else ())
                then = TextSpan(tag="p", tone="text-soft").classes("vt-ask-line").mark("confirm.ask.then")
                then.bind_text_from(vm, "ask_then")
                then.bind_visibility_from(vm, "ask_then", backward=bool)
            dont = DotSwitch("Don't ask again", on_change=self._models.set_dont_ask).classes(
                "vt-ask-dont").mark("confirm.ask.dont_ask")
            binding.bind_from(dont, "value", vm, "ask_dont")
            dont.bind_visibility_from(vm, "ask_dont_visible")
            with Element("div").classes("vt-ask-buttons"):
                primary = EmiyaButton(tone="primary", size=30, on_click=self._confirm_ask).mark("confirm.ask.primary")
                primary.bind_action_from(vm, "ask", backward=_ask_primary)
                EmiyaButton("Cancel", size=30, on_click=self._models.dismiss).mark("confirm.ask.cancel")

    def _log(self) -> None:
        with Popover(role="log", aria="Log").classes("vt-log-panel").mark("shell.log_panel") as panel:
            binding.bind_from(panel, "open", self._vm, "log_open")
            TextSpan("Log", tag="p").classes("vt-log-title")
            set_prop(Element("div").classes("vt-log-rule"), "aria-hidden", "true")
            with Element("div").classes("vt-log-scroll em-scroll"):
                lines = ListHost(_log_line).classes("vt-log-lines").mark("shell.log_lines")
                lines.bind_items_from(self._core.state.log, "lines")

    def _toasts(self) -> None:
        for parity in (1, 0):
            toast = _ToastView(parity, self._status.dismiss_toast).mark("shell.toast")
            binding.bind_from(toast, "toast", self._vm, "toast")

    # ---------- player, keys, events ----------

    def _plumbing(self) -> None:
        Player(on_state=self._playback.on_player_state).bind_target_from(self._playback.vm).mark("shell.player")
        keys = KeyDispatcher(self._commands.combos(), on_command=self._on_command).mark("shell.keys")
        keys.bind_combos_from(self._settings.vm, "key_rows", backward=lambda _rows: dict(self._commands.combos()))
        keys.bind_suspended_from(self._settings.vm, "key_rows", backward=_capturing)
        KeyDispatcher({ESCAPE_COMBO: ESCAPE_ID}, on_command=lambda _command: self._escape()).mark("shell.keys.escape")
        self._commands.set_focus_search(self._focus_search)

    def _subscribe(self) -> None:
        """Inside the shell's slot: NiceGUI drops these subscriptions when this client goes away."""
        events = self._core.events
        events.changed.subscribe(self._on_changed)
        events.forge_started.subscribe(self._on_forge_started)
        events.dropped.subscribe(self._on_dropped)
        self._ctx.prefs.changed.subscribe(self._on_pref)

    # ---------- intents ----------

    def _go(self, route: Route) -> None:
        ui.navigate.to(route)

    def _pick_dock_row(self, backend: str) -> None:
        self._models.request_switch(backend, origin="dock")

    def _retry(self) -> None:
        backend = _pending_backend(self._vm.retry)
        if backend:
            self._models.request_switch(backend, origin="retry")

    def _load_default(self) -> None:
        backend = _pending_backend(self._vm.load_default)
        if backend:
            self._models.request_switch(backend, origin="default")

    async def _confirm_ask(self) -> None:
        request = self._vm.ask
        if request is not None:
            await self._models.confirm(request.token)

    def _on_command(self, command_id: str) -> None:
        self._commands.handle(command_id, self._vm.route)

    def _escape(self) -> None:
        """D12: Escape closes the Log, the popover, the search results and the language lists (the intent), and
        empties the rail search box (the shell's own)."""
        self._status.escape()
        self._clear_search_box()

    # ---------- the search box ----------

    def _on_query(self, event: ValueChangeEventArguments) -> None:
        self._search.results(event.value or "")

    def _on_search_enter(self, event: GenericEventArguments) -> None:
        box = self._search_box
        text = event.args if isinstance(event.args, str) else (box.value if box is not None else "")
        items = self._search.results(text or "")
        if items:
            self._run_search(items[0].item_id)

    def _run_search(self, item_id: str) -> None:
        route = self._search.run(item_id)
        self._clear_search_box()
        if route is not None:
            ui.navigate.to(route)

    def _close_search(self) -> None:
        self._search.clear()
        self._clear_search_box()

    def _clear_search_box(self) -> None:
        box = self._search_box
        if box is not None and box.value:
            box.value = ""

    def _focus_search(self) -> None:
        if self._search_box is not None:
            self._search_box.run_method("focus")

    # ---------- events ----------

    def _on_path(self, path: str) -> None:
        self._status.on_route(path)
        self._clear_search_box()

    def _on_changed(self) -> None:
        self._push_field()

    def _on_pref(self, key: str) -> None:
        if key == FX_ON:
            self._toggle_field()
        else:
            self._push_field()

    def _on_reduce(self, always: bool) -> None:
        if always:
            self._body.classes(add="reduce-motion")
        else:
            self._body.classes(remove="reduce-motion")
        self._push_field()

    def _on_forge_started(self, _voice_id: str) -> None:
        if self._field is not None and not self._field.is_deleted:
            self._field.forge_blade(BLADE_ANCHOR)

    async def _on_dropped(self, paths: tuple[str, ...]) -> None:
        """A native drop: Forge takes the clip; anywhere else a toast says where to go."""
        if self._vm.route == FORGE_ROUTE:
            await self._ctx.behaviours.forge().drop_paths(paths)
        else:
            self._core.events.say(DROP_ELSEWHERE)

    # ---------- the forge field ----------

    def _field_state(self) -> dict[str, Any]:
        state = self._core.state
        policy = "always" if self._vm.reduce_always else "system"
        return field_state(state.engine, state.job, self._ctx.prefs, policy, state.window.visible,
                           fps_cap=self._manifest.fps_cap)

    def _make_field(self) -> EmiyaForgeField:
        if not self._ctx.fx_telemetry:
            return EmiyaForgeField(self._field_state())
        return EmiyaForgeField(self._field_state(), telemetry=True, on_ready=_fx_logger("ready"),
                               on_telemetry=_fx_logger("telemetry"), on_ack=_fx_logger("ack"),
                               on_error=_fx_logger("jserror"))

    def _push_field(self) -> None:
        if self._field is not None and not self._field.is_deleted:
            self._field.push(self._field_state())

    def _toggle_field(self) -> None:
        """Background Effects: off deletes the field, on builds a new one (a clean remount)."""
        wanted = bool(self._ctx.prefs.get(FX_ON))
        if wanted and self._field is None and self._fx_host is not None:
            with self._fx_host:
                self._field = self._make_field()
        elif not wanted and self._field is not None:
            if not self._field.is_deleted:
                self._field.delete()
            self._field = None


# ---- helpers ------------------------------------------------------------------------------------------

def _ask_title(request: ConfirmRequest | None) -> str:
    return request.title if request is not None else ""


def _ask_primary(request: ConfirmRequest | None) -> ActionState:
    return ActionState(enabled=request is not None, label=request.primary if request is not None else "",
                       tone="primary")


def _ask_line(line: tuple[str, str]) -> None:
    text, tone = line
    TextSpan(text, tag="p", tone=tone).classes("vt-ask-line")


def _log_line(line: str) -> None:
    TextSpan(line, tag="p").classes("vt-log-line" + (" is-bad" if FAIL.search(line) else ""))


def _pending_backend(action: ActionState) -> str:
    """The backend a named failure button loads (its pending request's switch plan)."""
    pending = action.pending
    return pending.switch.to_backend if pending is not None and pending.switch is not None else ""


def _capturing(rows: tuple[KeyRowVM, ...]) -> bool:
    """A Keys slot listens: the dispatcher stays quiet until it is done."""
    return any(row.key_listening or row.alt_listening for row in rows)


def _fx_logger(kind: str) -> Callable[[GenericEventArguments], None]:
    """--fx-telemetry: every field event as one JSON line on the voicetool.fx logger."""
    def log(event: GenericEventArguments) -> None:
        FX_LOG.info("fx-%s %s", kind, json.dumps(event.args, default=str, separators=(",", ":")))
    return log
