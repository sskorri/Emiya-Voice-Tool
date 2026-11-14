"""Plain-HTML controls of the Emiya theme (BUILD_SPEC §6.3): the mockup's markup and class names, no Quasar look.

Every control that can be disabled is a DisableableElement whose `enabled` sets the HTML `disabled` attribute
(NiceGUI's mixin would set Quasar's `disable` prop instead), so the user fixture's click() honours it. Colours
never appear here: each state is a class or an attribute, and static/emiya.css maps it to its colour tokens (R7).
State that a view model drives is a BindableProperty, so `binding.bind_from(widget, name, vm, field)` is a
passive link that dies with the element (R9). Clicks call the given handler with no arguments; an async
handler runs as a background task of the element's client.

A clickable control reports its own label and its aria-label as displayed contents, so the user fixture can
find a button by what a person reads or hears (`user.find("Free VRAM")` clicks the button, not its label).

The theme-neutral plumbing (the attribute and class helpers, ListHost, KeyedRows, Disableable) lives in
ui.kit.elements, so a second theme gets it too; it is re-exported here for the pages.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Literal, Self

from nicegui import binding, ui
from nicegui.element import Element
from nicegui.elements.mixins.text_element import TextElement

from ui.contract.plans import ActionState, ActionTone
from ui.kit.elements import Disableable, RangeInput, set_class, set_flag, set_prop, set_style, swap_classes
from ui.kit.elements import KeyedRows, ListHost  # noqa: F401  (re-exported: the pages build their lists with them)
from ui.kit.elements import clicker as _clicker

Handler = Callable[..., Any]
Asset = Callable[[str], str]
ChipTone = Literal["base", "gold", "red", "off"]
DotTone = Literal["gold", "hollow", "red"]

BUTTON_SIZES: tuple[int, ...] = (34, 30, 29, 26, 22, 20, 18)     # the mockup's button heights
ICON_BUTTON_SIZES: tuple[int, ...] = (30, 24, 22)
CHIP_SIZES: tuple[int, ...] = (24, 22, 20)
TONE_CLASSES: Mapping[str, str] = MappingProxyType({
    "primary": "em-primary",
    "grey": "em-primary is-grey",
    "plain": "em-btn",
    "danger": "em-btn is-danger",
    "link": "vt-link",
})
REDUCED_MEDIA = "(prefers-reduced-motion: reduce)"
STATIC_URL = "/theme/emiya/"
EMPTY_KEY = "—"                                                   # an unbound Keys slot (M L2837)

# The mockup's inline SVGs (M L128-130, L158, L236, L324-325, L368-369, L937), without colours and without
# xmlns: an inline <svg> needs none, and R8 keeps every http(s) address out of ui\.
_STROKE = "fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round"
_ROUND = "fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round"
ICONS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "log": (_STROKE, '<line x1="9" y1="6" x2="20" y2="6"></line><line x1="9" y1="12" x2="20" y2="12"></line>'
                     '<line x1="9" y1="18" x2="16" y2="18"></line><line x1="4" y1="6" x2="5" y2="6"></line>'
                     '<line x1="4" y1="12" x2="5" y2="12"></line><line x1="4" y1="18" x2="5" y2="18"></line>'),
    "minimize": (_STROKE, '<line x1="5" y1="12" x2="19" y2="12"></line>'),
    "close": (_STROKE, '<line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line>'),
    "play": ("fill: currentColor", '<polygon points="7 4 20 12 7 20"></polygon>'),
    "pause": ("fill: currentColor", '<rect x="6" y="5" width="4" height="14" rx="1"></rect>'
                                    '<rect x="14" y="5" width="4" height="14" rx="1"></rect>'),
    "stop": ("fill: currentColor", '<rect x="6" y="6" width="12" height="12" rx="1.5"></rect>'),   # NEW (Forge)
    "star": ("stroke-width: 1.75; stroke-linejoin: round",
             '<polygon points="12 3 14.8 8.7 21 9.6 16.5 14 17.6 20.2 12 17.3 6.4 20.2 7.5 14 3 9.6 9.2 8.7"></polygon>'),
    "folder": (_STROKE + "; stroke-linejoin: round",
               '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>'),
    "chevron": (_ROUND + "; stroke-width: 2.5", '<polyline points="6 9 12 15 18 9"></polyline>'),
    "lock": (_ROUND + "; stroke-width: 2.2",
             '<rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path>'),
})


# ---- small helpers ------------------------------------------------------------------------------------

def tone_class(tone: str | None) -> str:
    """The class of a TextTone (the §6.4 token names): "muted" → "vt-c-muted"."""
    return f"vt-c-{tone}" if tone else ""


FLASH_CLASSES = ("em-flash-b", "em-flash")        # two names for one 1.2 s flash: a swap replays it on one element


def flash_class(flash_seq: int) -> str:
    """The flash class for a VM's flash number: odd "em-flash", even "em-flash-b", so each new number replays the
    flash on the same element; "" for 0 (no flash live)."""
    return FLASH_CLASSES[flash_seq % 2] if flash_seq else ""


def svg_markup(name: str, size: int) -> str:
    """One of the mockup's icons as inline SVG markup, `size` px square."""
    style, body = ICONS[name]
    return f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" aria-hidden="true" style="{style}">{body}</svg>'


def static_url(path: str) -> str:
    """A theme file's URL without the ?v= stamp (ThemeContext.asset adds one)."""
    return STATIC_URL + path.lstrip("/")


def _number(value: Any, fallback: float) -> float:
    """An event's numeric payload ({value: n} or n); fallback for anything else."""
    if isinstance(value, Mapping):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# ---- text and containers ------------------------------------------------------------------------------

class TextSpan(TextElement):
    """Text in a plain element (<span> by default) with a bindable colour tone and tooltip."""

    tone = binding.BindableProperty(on_change=lambda sender, value: sender._apply_tone(value))
    tip = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "title", value))

    def __init__(self, text: str = "", *, tag: str = "span", tone: str | None = None, tip: str = "") -> None:
        """
        :param text: the text
        :param tag: the HTML tag (span, p, h1, div, button, ...)
        :param tone: a TextTone name ("muted", "gold-dim", "bad", ...) or None for the inherited colour
        :param tip: the title tooltip
        """
        super().__init__(tag=tag, text=text)
        self._tone_class = ""
        self.tone = tone
        self.tip = tip
        self._apply_tone(tone)
        set_prop(self, "title", tip)

    def _apply_tone(self, tone: str | None) -> None:
        self._tone_class = swap_classes(self, self._tone_class, tone_class(tone))


class Box(Element):
    """A plain container with a bindable tooltip (title) and accessible name (aria-label)."""

    tip = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "title", value))
    aria = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "aria-label", value))

    def __init__(self, tag: str = "div", *, tip: str = "", aria: str = "", role: str = "") -> None:
        super().__init__(tag)
        self.tip = tip
        self.aria = aria
        set_prop(self, "title", tip)
        set_prop(self, "aria-label", aria)
        set_prop(self, "role", role)


class Card(Element):
    """A radius-10 panel; `dark` picks the darker background (the Script card with 0 voices, Identity)."""

    dark = binding.BindableProperty(on_change=lambda sender, value: set_class(sender, "is-dark", bool(value)))

    def __init__(self, *, dark: bool = False, tag: str = "div") -> None:
        super().__init__(tag)
        self.classes("vt-card")
        self.dark = dark
        set_class(self, "is-dark", dark)


class SectionLabel(Element):
    """The 22 px caption above a card ("Voice", "Delivery", "Model"); `right` holds a slot on its right."""

    def __init__(self, text: str, *, right: bool = False) -> None:
        super().__init__("div" if right else "p")
        self.classes("vt-section-label" + (" has-right" if right else ""))
        self.right: Element | None = None
        with self:
            if right:
                self.label = TextSpan(text, tag="p").classes("vt-section-text")
                self.right = Element("span").classes("vt-section-right")
            else:
                self.label = TextSpan(text)


class Row(Element):
    """An `em-row`: 40 px, a top hairline (unless first), an optional label column; children go after it.

    `off` dims the row (vt-off, opacity 0.45) while its control does not apply; `tip` is its tooltip. Both are
    BindableProperties, so bind_from(row, "off" | "tip", vm, field) follows a view model.
    """

    off = binding.BindableProperty(on_change=lambda sender, value: set_class(sender, "vt-off", bool(value)))
    tip = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "title", value))

    def __init__(self, label: str | None = None, *, label_width: int | None = None, first: bool = False,
                 off: bool = False, tip: str = "", tag: str = "div") -> None:
        """
        :param label: the row's label (None: no label column)
        :param label_width: a fixed label column in px (None: the label takes the free width)
        :param first: the first row of a card has no hairline
        :param off: start dimmed
        :param tip: the row's tooltip
        """
        super().__init__(tag)
        self.classes("em-row")
        self.label: TextSpan | None = None
        self.off = bool(off)
        set_class(self, "vt-off", bool(off))
        self.tip = tip
        set_prop(self, "title", tip)
        with self:
            if not first:
                set_prop(Element("span").classes("vt-hair"), "aria-hidden", "true")
            if label is not None:
                self.label = TextSpan(label).classes("vt-row-label" + (" is-fixed" if label_width else ""))
                if label_width:
                    set_style(self.label, "width", f"{label_width}px")


class Popover(Element):
    """An absolutely positioned panel (`vt-pop-in`); `open` shows it, Escape inside calls on_close."""

    open = binding.BindableProperty(on_change=lambda sender, value: sender.set_visibility(bool(value)))
    aria = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "aria-label", value))

    def __init__(self, *, open: bool = False, role: str = "dialog", aria: str = "",
                 on_close: Handler | None = None) -> None:
        super().__init__("div")
        self.classes("vt-popover vt-pop-in")
        set_prop(self, "role", role)
        self.open = bool(open)
        self.aria = aria
        set_prop(self, "aria-label", aria)
        self.set_visibility(bool(open))
        if on_close is not None:
            self.on("keydown.esc", _clicker(on_close))


class Logo(Element):
    """The Saber Lion logo in its slot: a <picture> whose still PNG serves "Follow Windows" (prefers-reduced-motion)
    and whose <img> shows the GIF, or the still PNG while `still` (Reduce Motion "Always").

    Used three times (MEM L20): the rail Logo(45, -2, -0.5), Speak's empty state Logo(84, -9, -6, slot=64) and
    the About card Logo(53, -6, -4). Bind `still` to ShellVM.logo_still with bind_still_from(status.vm).
    """

    still = binding.BindableProperty(on_change=lambda sender, _: sender._apply())

    def __init__(self, size: float, left: float, top: float, *, slot: int = 40, asset: Asset | None = None,
                 still: bool = False) -> None:
        """
        :param size: the image's width and height in px
        :param left: its offset in the slot (the GIF has transparent padding)
        :param top: its offset in the slot
        :param slot: the slot's width and height in px
        :param asset: ThemeContext.asset, for URLs with the ?v= stamp (default: plain /theme/emiya URLs)
        :param still: show the still frame
        """
        super().__init__("div")
        asset = asset or static_url
        self.gif_src = asset("img/logo.gif")
        self.still_src = asset("img/logo-still.png")
        self.classes("vt-logo")
        set_style(self, "width", f"{slot}px")
        set_style(self, "height", f"{slot}px")
        with self, Element("picture"):
            source = Element("source")
            set_prop(source, "srcset", self.still_src)
            set_prop(source, "media", REDUCED_MEDIA)
            self.img = Element("img").classes("vt-logo-img")
            self.img.props["alt"] = ""
            for name, value in (("left", left), ("top", top), ("width", size), ("height", size)):
                set_style(self.img, name, f"{value:g}px")
        self.still = bool(still)
        self._apply()

    @property
    def src(self) -> str:
        """The <img> source now in force."""
        return str(self.img.props.get("src", ""))

    def bind_still_from(self, target: Any, name: str = "logo_still") -> Self:
        """Follow target.<name> (ShellVM.logo_still by default)."""
        binding.bind_from(self, "still", target, name)
        return self

    def _apply(self) -> None:
        set_prop(self.img, "src", self.still_src if self.still else self.gif_src)


# ---- buttons ------------------------------------------------------------------------------------------

class EmiyaButton(Disableable):
    """A text button: `em-btn` (plain), `em-primary` (gold), grey, danger or link, drawn from an ActionState.

    `action` holds everything the button shows (label, enabled, tone, aria-label, tooltip, gold fill or sweep,
    armed, key hint chips); bind it with bind_action_from(vm, "generate"). Disabled, the reason is the tooltip
    when there is no other.
    """

    action = binding.BindableProperty(on_change=lambda sender, value: sender._apply(value))

    def __init__(self, label: str = "", *, tone: ActionTone = "plain", size: int = 30,
                 on_click: Handler | None = None, enabled: bool = True, tooltip: str = "", aria: str = "",
                 fill: float | None = None, indeterminate: bool = False, hint: Sequence[str] = (),
                 armed: bool = False) -> None:
        """
        :param label: the visible label
        :param tone: "primary", "plain", "grey", "danger" or "link"
        :param size: the height in px, one of BUTTON_SIZES (ignored for links)
        :param on_click: called on a click
        """
        if size not in BUTTON_SIZES:
            raise ValueError(f"EmiyaButton size must be one of {BUTTON_SIZES}, got {size}")
        super().__init__(tag="button")
        self.props["type"] = "button"
        self._size_class = f"vt-b{size}"
        self._tone_classes = ""
        self._hint_parts: tuple[str, ...] = ()
        with self:
            self._label = TextSpan(label).classes("vt-btn-label")
            self._hints = Element("span").classes("vt-kbds")
        set_prop(self._hints, "aria-hidden", "true")
        self._hints.set_visibility(False)
        first = ActionState(enabled=enabled, label=label, tone=tone, aria=aria, tooltip=tooltip, fill=fill,
                            indeterminate=indeterminate, armed=armed, hint=tuple(hint))
        self.action = first
        self._apply(first)
        if on_click is not None:
            self.on("click", _clicker(on_click))

    @property
    def label(self) -> str:
        return self._label.text

    def bind_action_from(self, target: Any, name: str, *, backward: Callable[[Any], ActionState] | None = None) -> Self:
        """Follow an ActionState field of a VM (backward turns another value into an ActionState)."""
        binding.bind_from(self, "action", target, name, backward=backward)
        return self

    def _apply(self, state: ActionState) -> None:
        self.enabled = bool(state.enabled)
        self._label.text = state.label
        classes = TONE_CLASSES.get(state.tone, "em-btn")
        if state.tone != "link":
            classes += " " + self._size_class
        self._tone_classes = swap_classes(self, self._tone_classes, classes)
        set_class(self, "is-armed", state.armed)
        set_class(self, "is-fill", state.fill is not None and not state.indeterminate)
        set_class(self, "is-sweep", state.indeterminate)
        fill = None if state.fill is None else f"{max(0.0, min(1.0, state.fill)) * 100:.1f}%"
        set_style(self, "--fill", fill)
        set_prop(self, "title", state.tooltip or ("" if state.enabled else state.reason))
        set_prop(self, "aria-label", state.aria)
        self._show_hints(tuple(state.hint))

    def _show_hints(self, parts: tuple[str, ...]) -> None:
        if parts == self._hint_parts:
            return
        self._hint_parts = parts
        self._hints.clear()
        with self._hints:
            for part in parts:
                TextSpan(part).classes("vt-kbd")
        self._hints.set_visibility(bool(parts))

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._label.text]


class IconButton(Disableable):
    """A square icon button: `em-icon` (`vt-filled` for the filled look) or `em-danger` (Close).

    `pressed` (None: not a toggle) sets aria-pressed; `action` (optional) drives the accessible name, tooltip,
    armed look and enabled state from an ActionState, as for Close.
    """

    pressed = binding.BindableProperty(on_change=lambda sender, value: sender._apply_pressed(value))
    action = binding.BindableProperty(on_change=lambda sender, value: sender._apply_action(value))

    def __init__(self, icon: str, *, aria: str, size: int = 30, icon_size: int = 16, filled: bool = False,
                 danger: bool = False, pressed: bool | None = None, tooltip: str | None = None,
                 enabled: bool = True, on_click: Handler | None = None) -> None:
        """
        :param icon: a name in ICONS
        :param aria: the accessible name (also the tooltip unless `tooltip` is given; "" for none)
        :param size: 30, 24 or 22 px
        :param icon_size: the SVG's size in px
        """
        if size not in ICON_BUTTON_SIZES:
            raise ValueError(f"IconButton size must be one of {ICON_BUTTON_SIZES}, got {size}")
        super().__init__(tag="button")
        self.props["type"] = "button"
        self._icon_size = icon_size
        self.classes(("em-danger" if danger else "em-icon" + (" vt-filled" if filled else ""))
                     + f" vt-i{size}" + (" vt-star" if icon == "star" else ""))
        with self:
            self._svg = ui.html(svg_markup(icon, icon_size), sanitize=False, tag="span").classes("vt-ico")
        self.icon = icon
        set_prop(self, "aria-label", aria)
        set_prop(self, "title", aria if tooltip is None else tooltip)
        self.pressed = pressed
        self._apply_pressed(pressed)
        self.action = None
        self.enabled = enabled
        if on_click is not None:
            self.on("click", _clicker(on_click))

    def set_icon(self, icon: str) -> None:
        """Show another icon (play ↔ pause)."""
        if icon != self.icon:
            self.icon = icon
            self._svg.set_content(svg_markup(icon, self._icon_size))

    def bind_action_from(self, target: Any, name: str, *, backward: Callable[[Any], ActionState] | None = None) -> Self:
        binding.bind_from(self, "action", target, name, backward=backward)
        return self

    def _apply_pressed(self, pressed: bool | None) -> None:
        set_prop(self, "aria-pressed", None if pressed is None else ("true" if pressed else "false"))

    def _apply_action(self, state: ActionState | None) -> None:
        if state is None:
            return
        self.enabled = bool(state.enabled)
        name = state.aria or state.label
        set_prop(self, "aria-label", name)
        set_prop(self, "title", state.tooltip or name)
        set_class(self, "is-armed", state.armed)


class PlayButton(IconButton):
    """The filled 30 px play button of a take or a reference clip: `playing` (a BindableProperty) swaps the icon to
    pause (Speak, Voices) or stop (Forge); the label and enabled state come from an ActionState (bind_action_from).
    """

    playing = binding.BindableProperty(
        on_change=lambda sender, value: sender.set_icon(sender.stop_icon if value else "play"))

    def __init__(self, *, aria: str = "Play", stop_icon: Literal["pause", "stop"] = "pause",
                 on_click: Handler | None = None) -> None:
        """
        :param aria: the accessible name until an ActionState gives one
        :param stop_icon: the face while playing
        """
        super().__init__("play", aria=aria, filled=True, size=30, icon_size=14, on_click=on_click)
        self.stop_icon = stop_icon
        self.playing = False


class PickButton(Disableable):
    """The 150x29 picker button with the current name and a chevron (M L234-237), opening a ListBox."""

    text = binding.BindableProperty(on_change=lambda sender, value: sender._set_text(value))
    expanded = binding.BindableProperty(on_change=lambda sender, value: set_flag(sender, "aria-expanded", bool(value)))

    def __init__(self, text: str = "", *, tip: str = "", on_click: Handler | None = None, enabled: bool = True) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("em-btn vt-pick-btn")
        set_prop(self, "aria-haspopup", "listbox")
        set_prop(self, "title", tip)
        with self:
            self._caption = TextSpan(text).classes("vt-pick-text")
            ui.html(svg_markup("chevron", 12), sanitize=False, tag="span").classes("vt-ico")
        self.text = text
        self.expanded = False
        set_flag(self, "aria-expanded", False)
        self.enabled = enabled
        if on_click is not None:
            self.on("click", _clicker(on_click))

    def _set_text(self, value: str) -> None:
        self._caption.text = value

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._caption.text]


# ---- segmented groups, switches, sliders --------------------------------------------------------------

Option = tuple[str, str] | tuple[str, str, bool]


class _Segment(Disableable):
    """One button of a Segmented group."""

    def __init__(self, key: str, label: str, *, tabs: bool, on_pick: Callable[[str], Any]) -> None:
        super().__init__(tag="button")
        self.key = key
        self._tabs = tabs
        self.props["type"] = "button"
        self.classes("em-seg")
        set_prop(self, "role", "tab" if tabs else "")
        with self:
            self._face = TextSpan(label).classes("em-seg-face")
        self.on("click", _clicker(lambda: on_pick(self.key)))

    def set_on(self, on: bool) -> None:
        set_class(self, "is-on", on)
        set_flag(self, "aria-selected" if self._tabs else "aria-pressed", on)

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._face.text]


class Segmented(Element):
    """An `em-seg` group (29 px; header tabs 30 px with `tabs=True`, a tablist).

    options are (key, label) pairs, compared with `value`, or (key, label, on) triples from a VM, which say
    themselves which segment is on. A click calls on_change(key); the value follows the VM (bind it).
    item_marker gives every segment the marker "<item_marker>.<key>".
    """

    value = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    options = binding.BindableProperty(on_change=lambda sender, _: sender._rebuild())
    enabled = binding.BindableProperty(on_change=lambda sender, _: sender._sync())

    def __init__(self, options: Sequence[Option] = (), *, value: str | None = None,
                 on_change: Callable[[str], Any] | None = None, aria: str = "", tabs: bool = False,
                 item_marker: str = "", enabled: bool = True) -> None:
        super().__init__("div")
        self._tabs = tabs
        self._on_change = on_change
        self._item_marker = item_marker
        self._segments: list[_Segment] = []
        self.classes("em-segs" + (" is-tabs" if tabs else ""))
        set_prop(self, "role", "tablist" if tabs else "group")
        set_prop(self, "aria-label", aria)
        self.value = value
        self.enabled = enabled
        self.options = tuple(options)
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear()
        self._segments = []
        with self:
            for option in self.options:
                segment = _Segment(option[0], option[1], tabs=self._tabs, on_pick=self._pick)
                if self._item_marker:
                    segment.mark(f"{self._item_marker}.{option[0]}")
                self._segments.append(segment)
        self._sync()

    def _sync(self) -> None:
        for segment, option in zip(self._segments, self.options):
            segment.set_on(bool(option[2]) if len(option) > 2 else option[0] == self.value)
            segment.enabled = bool(self.enabled)

    def _pick(self, key: str) -> Any:
        return self._on_change(key) if self._on_change is not None else None


class DotSwitch(Disableable):
    """A `vt-consent` row toggle: label, optional note, and the dot (13 px gold on, 12 px hollow off).

    A click calls on_change(not value); bind `value` to the VM so it shows what the core holds.
    """

    value = binding.BindableProperty(on_change=lambda sender, _: sender._sync())

    def __init__(self, label: str, *, value: bool = False, note: str = "",
                 on_change: Callable[[bool], Any] | None = None, enabled: bool = True) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-consent")
        with self:
            self._label = TextSpan(label).classes("vt-consent-label")
            self.note = TextSpan(note).classes("vt-consent-note")
            self.note.set_visibility(bool(note))
            with Element("span").classes("vt-dotbox") as box:
                set_prop(box, "aria-hidden", "true")
                self._dot = Element("span").classes("vt-dot")
        self.value = bool(value)
        self.enabled = enabled
        self._sync()
        if on_change is not None:
            self.on("click", _clicker(lambda: on_change(not self.value)))

    def _sync(self) -> None:
        set_flag(self, "aria-pressed", bool(self.value))
        set_class(self._dot, "em-dot-off", not self.value)

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._label.text]


class GoldSlider(Disableable):
    """An `em-range` slider with the gold fill up to the value and an optional tick mark (Expressiveness).

    on_input(value) previews a moving thumb. on_release(value) commits one pointer or keyboard gesture.
    Bind `value` to the VM to settle the control on its accepted value, including a refused edit.
    """

    value = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    tick = binding.BindableProperty(on_change=lambda sender, _: sender._sync())

    def __init__(self, *, min: float, max: float, step: float, value: float, aria: str = "",
                 on_input: Callable[[float], Any] | None = None, on_release: Callable[[float], Any] | None = None,
                 tick: float | None = None, enabled: bool = True, throttle: float = 0.05) -> None:
        if max <= min:
            raise ValueError("GoldSlider needs max > min")
        super().__init__(tag="div")
        self.minimum, self.maximum = float(min), float(max)
        self.classes("vt-range")
        with self:
            self._input = RangeInput().classes("em-range")
            self._tick = Element("span").classes("vt-range-tick")
        for name, number in (("min", min), ("max", max), ("step", step)):
            self._input.props[name] = f"{number:g}"
        self._input.props["type"] = "range"
        set_prop(self._input, "aria-label", aria)
        set_prop(self._tick, "aria-hidden", "true")
        self.value = float(value)
        self.tick = tick
        self.enabled = enabled
        self._sync()
        if on_input is not None:
            self.on("input", lambda e: on_input(_number(e.args, self.value)), throttle=throttle,
                    trailing_events=False, js_handler="(e) => emit(Number(e.target.value))")

        def released(event):
            seq = event.args.get("sync", 0) if isinstance(event.args, Mapping) else 0
            try:
                result = on_release(_number(event.args, self.value)) if on_release is not None else None
            except Exception:
                self._input.acknowledge(seq)
                raise
            if inspect.isawaitable(result):
                async def complete():
                    try:
                        await result
                    finally:
                        self._input.acknowledge(seq)
                return complete()
            self._input.acknowledge(seq)

        self.on("change", released, js_handler="(e) => emit(e.detail)")

    def _handle_enabled_change(self, enabled: bool) -> None:
        set_prop(self._input, "disabled", None if enabled else True)

    def _pct(self, value: float) -> float:
        return max(0.0, min(100.0, (value - self.minimum) / (self.maximum - self.minimum) * 100.0))

    def _sync(self) -> None:
        value = float(self.value)
        set_prop(self._input, "serverValue", value)
        self._tick.set_visibility(self.tick is not None)
        if self.tick is not None:
            set_style(self._tick, "left", f"{self._pct(float(self.tick)):.2f}%")


# ---- chips, indicators, meters ------------------------------------------------------------------------

class Chip(Element):
    """A 20, 22 or 24 px chip (M L126, L147-150, L279) in tone base, gold, red or off, with an optional dot."""

    text = binding.BindableProperty(on_change=lambda sender, value: sender._set_text(value))
    tone = binding.BindableProperty(on_change=lambda sender, value: sender._set_tone(value))
    dot = binding.BindableProperty(on_change=lambda sender, value: sender._set_dot(value))

    def __init__(self, text: str = "", *, tone: ChipTone = "base", size: int = 20, dot: DotTone | None = None,
                 tag: str = "span", on_click: Handler | None = None) -> None:
        if size not in CHIP_SIZES:
            raise ValueError(f"Chip size must be one of {CHIP_SIZES}, got {size}")
        super().__init__(tag)
        if tag == "button":
            self.props["type"] = "button"
        self.classes(f"vt-chip vt-chip-{size}")
        self._tone_class = ""
        self._dot_class = ""
        with self:
            self._dot = Element("span").classes("vt-chip-dot")
            self._caption = TextSpan(text).classes("vt-chip-text")
        set_prop(self._dot, "aria-hidden", "true")
        self.text = text
        self.tone = tone
        self.dot = dot
        self._set_tone(tone)
        self._set_dot(dot)
        if on_click is not None:
            self.on("click", _clicker(on_click))

    def _set_text(self, value: str) -> None:
        self._caption.text = value

    def _set_tone(self, tone: str) -> None:
        self._tone_class = swap_classes(self, self._tone_class, f"is-{tone}")

    def _set_dot(self, dot: str | None) -> None:
        self._dot.set_visibility(dot is not None)
        self._dot_class = swap_classes(self._dot, self._dot_class, f"is-{dot}" if dot else "")

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._caption.text]


class DotIndicator(Element):
    """The 19x19 state box of a dock row or a Models card (M L70-82).

    A filled 13 px gold dot (resident), a hollow 12 px dot (not loaded; gold-edged while unloading, red after a
    failed load), or for "loading" a 12 px conic ring filled to `ring` (0..1), an indeterminate turning arc
    while ring is None. spark() replays the 1.1 s spark of a fresh load.
    """

    state = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    ring = binding.BindableProperty(on_change=lambda sender, _: sender._sync())

    def __init__(self, state: str = "not_loaded", *, ring: float | None = None) -> None:
        super().__init__("span")
        self.classes("vt-ind")
        set_prop(self, "aria-hidden", "true")
        self._state_class = ""
        self._spark: Element | None = None
        with self:
            self._dot = Element("span").classes("vt-ind-dot")
            self._ring = Element("span").classes("vt-ind-ring")
        self.state = state
        self.ring = ring
        self._sync()

    def spark(self) -> None:
        """Play the spark once more (a new element, so the animation starts over)."""
        if self._spark is not None and not self._spark.is_deleted:
            self._spark.delete()
        with self:
            self._spark = Element("span").classes("vt-spark")

    def _sync(self) -> None:
        loading = self.state == "loading"
        self._dot.set_visibility(not loading)
        self._ring.set_visibility(loading)
        self._state_class = swap_classes(self._dot, self._state_class, f"is-{self.state}")
        set_class(self._ring, "is-spin", loading and self.ring is None)
        degrees = None if self.ring is None else f"{round(max(0.0, min(1.0, self.ring)) * 360)}deg"
        set_style(self._ring, "--ring", degrees)


class Meter(Element):
    """A two-segment bar: this app (gold) and other programs (grey), in percent of the total.

    4 px x 132 px in the dock; tall=True is the Models page's 8 px bar.
    """

    app_pct = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    other_pct = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    aria = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "aria-label", value))

    def __init__(self, *, app_pct: float = 0.0, other_pct: float = 0.0, tall: bool = False, aria: str = "") -> None:
        super().__init__("div")
        self.classes("vt-meter" + (" is-tall" if tall else ""))
        set_prop(self, "role", "img")
        with self:
            self._app = Element("span").classes("vt-meter-seg is-app")
            self._other = Element("span").classes("vt-meter-seg is-other")
        self.app_pct = app_pct
        self.other_pct = other_pct
        self.aria = aria
        set_prop(self, "aria-label", aria)
        self._sync()

    def _sync(self) -> None:
        set_style(self._app, "width", f"{max(0.0, float(self.app_pct)):.2f}%")
        set_style(self._other, "width", f"{max(0.0, float(self.other_pct)):.2f}%")


# ---- lists, keys, text entry --------------------------------------------------------------------------

class _Option(Disableable):
    """One entry of a ListBox."""

    def __init__(self, code: str, name: str, *, selected: bool, mark: str, on_pick: Callable[[str], Any]) -> None:
        super().__init__(tag="button")
        self.code = code
        self.props["type"] = "button"
        self.classes("em-list vt-option" + (" is-on" if selected else ""))
        set_prop(self, "role", "option")
        set_flag(self, "aria-selected", selected)
        with self:
            self._name = TextSpan(name).classes("vt-option-name")
            TextSpan(mark).classes("vt-option-mark")
        self.on("click", _clicker(lambda: on_pick(self.code)))

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._name.text]


class ListBox(Element):
    """The language list: 220 px wide, opening upward from its picker, at most 300 px high (M L239-249).

    items are LangItemVM rows (code, name, selected, mark) or such tuples. A pick calls on_pick(code); a click
    outside (a transparent backdrop) or Escape calls on_close(). Put it with its PickButton in a `vt-pick` box.
    item_marker gives every option the marker "<item_marker>.<code>".
    """

    open = binding.BindableProperty(on_change=lambda sender, _: sender._sync())
    items = binding.BindableProperty(on_change=lambda sender, _: sender._rebuild())

    def __init__(self, *, items: Sequence[Any] = (), open: bool = False, on_pick: Callable[[str], Any],
                 on_close: Handler, aria: str = "Language", item_marker: str = "") -> None:
        super().__init__("span")
        self.classes("vt-listbox-host")
        self._on_pick = on_pick
        self._item_marker = item_marker
        with self:
            self._backdrop = Element("div").classes("vt-backdrop")
            self._list = Element("div").classes("vt-listbox em-scroll vt-pop-in")
        set_prop(self._backdrop, "aria-hidden", "true")
        set_prop(self._list, "role", "listbox")
        set_prop(self._list, "aria-label", aria)
        self._backdrop.on("click", _clicker(on_close))
        self._list.on("keydown.esc", _clicker(on_close))
        self.open = bool(open)
        self.items = tuple(items)
        self._rebuild()

    def _rebuild(self) -> None:
        self._list.clear()
        with self._list:
            for item in self.items:
                code, name, selected, mark = _lang_fields(item)
                option = _Option(code, name, selected=selected, mark=mark, on_pick=self._on_pick)
                if self._item_marker:
                    option.mark(f"{self._item_marker}.{code}")
        self._sync()

    def _sync(self) -> None:
        self._backdrop.set_visibility(bool(self.open))
        self._list.set_visibility(bool(self.open))


def _lang_fields(item: Any) -> tuple[str, str, bool, str]:
    if isinstance(item, tuple):
        code, name, selected, mark = (tuple(item) + (False, ""))[:4]
        return str(code), str(name), bool(selected), str(mark)
    return item.code, item.name, bool(item.selected), item.mark


class KeyButton(Disableable):
    """A 116x26 `vt-key` slot of the Keys table (M L1076-1077).

    A click calls on_click() (start listening). While `listening` (class "listen"), a keydown is captured in
    the browser (no default action, no other key handler) and sent to on_capture(key=, ctrl=, shift=, alt=,
    composing=); Tab is left alone, and leaving the button (blur) calls on_stop().
    """

    text = binding.BindableProperty(on_change=lambda sender, value: sender._set_text(value))
    listening = binding.BindableProperty(on_change=lambda sender, value: set_class(sender, "listen", bool(value)))
    aria = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "aria-label", value))

    CAPTURE_JS = ("(e) => { if (!e.currentTarget.classList.contains('listen') || e.key === 'Tab') return; "
                  "e.preventDefault(); e.stopPropagation(); emit({key: e.key, ctrl: e.ctrlKey, shift: e.shiftKey, "
                  "alt: e.altKey, composing: e.isComposing || e.keyCode === 229}); }")

    def __init__(self, text: str = "—", *, listening: bool = False, aria: str = "",
                 on_click: Handler | None = None, on_capture: Callable[..., Any] | None = None,
                 on_stop: Handler | None = None) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-key")
        with self:
            self._caption = TextSpan(text)
        self.text = text
        self.listening = bool(listening)
        self.aria = aria
        self._set_text(text)
        set_class(self, "listen", bool(listening))
        set_prop(self, "aria-label", aria)
        if on_click is not None:
            self.on("click", _clicker(on_click))
        if on_capture is not None:
            self.on("keydown", lambda e: on_capture(**_capture_args(e.args)), js_handler=self.CAPTURE_JS)
        if on_stop is not None:
            self.on("blur", _clicker(on_stop))

    def _set_text(self, value: str) -> None:
        self._caption.text = value
        set_class(self, "is-empty", value in ("", EMPTY_KEY))

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._caption.text]


def _capture_args(args: Any) -> dict[str, Any]:
    data = args if isinstance(args, Mapping) else {}
    return {"key": str(data.get("key", "")), "ctrl": bool(data.get("ctrl")), "shift": bool(data.get("shift")),
            "alt": bool(data.get("alt")), "composing": bool(data.get("composing"))}


class TextField(ui.input):
    """A borderless `em-input` text box (a QInput, so the user fixture can type into it)."""

    def __init__(self, *, placeholder: str = "", value: str = "", aria: str = "", kind: str = "text",
                 on_change: Handler | None = None) -> None:
        """
        :param kind: the input type ("text", "search")
        :param on_change: called with NiceGUI's value-change arguments on every keystroke
        """
        super().__init__(placeholder=placeholder, value=value, on_change=on_change)
        self.props("borderless dense hide-bottom-space")
        self.props["type"] = kind
        self.classes("em-input")
        set_prop(self, "aria-label", aria)


class ScriptBox(ui.textarea):
    """The 150 px script textarea (M L292): gold caret, selectable text, a `lang` attribute for the voice.

    caret_js is a JavaScript expression for the caret position (a tag button's js_handler emits it);
    set_caret(index) puts the caret back after an insert.
    """

    lang = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "lang", value))

    def __init__(self, *, placeholder: str = "", value: str = "", lang: str = "en", aria: str = "Script",
                 on_change: Handler | None = None) -> None:
        super().__init__(placeholder=placeholder, value=value, on_change=on_change)
        self.props("borderless hide-bottom-space")
        self.classes("vt-script em-scroll")
        set_prop(self, "aria-label", aria)
        self.lang = lang
        set_prop(self, "lang", lang)

    @property
    def caret_js(self) -> str:
        """document.getElementById(<the textarea>).selectionStart (QInput gives the native element our id)."""
        return f"(document.getElementById('{self.html_id}') || {{selectionStart: 0}}).selectionStart"

    def set_caret(self, index: int) -> None:
        """Focus the textarea and put the caret at index."""
        position = max(0, int(index))
        self.client.run_javascript(
            f"const t = document.getElementById('{self.html_id}');"
            f" if (t) {{ t.focus(); t.setSelectionRange({position}, {position}); }}")
