"""The Speak page of the Emiya theme (BUILD_SPEC §6.7; the mockup's Speak board, M L136-429).

Left column: the Voice panel (voices grouped by model, or the empty state) and Delivery. Right column: the Script
card, the Take card and "Takes · This Session". Everything shown follows SpeakVM (behaviours.speak().vm) and the
shell's ShellVM through passive bindings; every control calls an intent, and nothing here touches the engine.

The voice groups (whose chips count load seconds while a model loads) update their rows in place and rebuild only
when the set of voices changes; this session's takes (the newest 200) are kept by take id, so a take starting or
landing adds one row and rebuilds none, and a flash or a new take's enter animation plays once.
The two delivery sliders follow the thumb in the browser while it is dragged and send their value on release.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nicegui import binding, ui
from nicegui.element import Element

from ui.contract.plans import ActionState
from ui.contract.viewmodels import GeneratingRowVM, TakeCardVM, TakeRowVM, VoiceGroupVM, VoiceItemVM

from ..fx.take_wave import EmiyaTakeWave
from .call_controls import TakeActions, send_button, stop_button
from ..widgets import (
    Card,
    Chip,
    Disableable,
    DotSwitch,
    EmiyaButton,
    GoldSlider,
    IconButton,
    KeyedRows,
    ListBox,
    ListHost,
    Logo,
    PickButton,
    PlayButton,
    Row,
    ScriptBox,
    SectionLabel,
    Segmented,
    TextSpan,
    flash_class,
    set_class,
    set_flag,
    set_prop,
    swap_classes,
)

if TYPE_CHECKING:
    from ui.contract.theme import ThemeContext
    from ui.contract.viewmodels import PlaybackController, SpeakController

__all__ = ["SpeakPage", "build"]

FORGE: str = "/forge"
TAKES: str = "/takes"
SCRIPT_PLACEHOLDER = "Type what the voice should say…"                   # M L292
EMPTY_TITLE = "No voices yet"                                            # M L177-178
EMPTY_TEXT = "Forge one from a 6–15 s WAV of one speaker."
GUIDANCE_TIP = "Lower is slower, more deliberate delivery"               # M L220
CUT_TAKE_TIP = "This take hit the 40 s limit; the end of the script was cut off."
SESSION_COLUMNS = (("#", "vt-sp-c-no"), ("Text", "vt-sp-c-text"), ("Voice", "vt-sp-c-voice"),
                   ("Expr", "vt-sp-c-num"), ("Guid", "vt-sp-c-num"), ("Length", "vt-sp-c-len"),
                   ("", "vt-sp-c-actions"))                              # M L395-403
END_OF_TEXT = 1 << 30                                                    # a caret past the end inserts at the end

# The delivery sliders while dragged (client side only, nothing goes to the server): the gold fill follows the
# thumb and the value text shows the number; the release sends the value (GoldSlider's on_release).
LIVE_JS = ("(e) => { const t = e.target; if (!t || t.type !== 'range') return; "
           "const o = document.getElementById('{target}'); const n = o && o.firstChild; "
           "if (n && n.nodeType === 3) n.nodeValue = t.valueAsNumber.toFixed(2); }")

# A tag button reads the script box's caret (the textarea of its own Script card) when it is clicked.
CARET_JS = ("(e) => { const c = e.currentTarget.closest('.vt-sp-script'); const t = c && c.querySelector('textarea'); "
            "emit(t ? t.selectionStart : null); }")


def build(ctx: ThemeContext) -> None:
    """The Speak page for one page load (sync: it reads the view models and wires the intents)."""
    SpeakPage(ctx).build()


# ---- page-local elements ------------------------------------------------------------------------------

class _VoicePick(Disableable):
    """The clickable part of a voice row: the id and its sub text ("Japanese · 0.50")."""

    def __init__(self, voice_id: str, on_click: Callable[[], Any]) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-li-pick")
        with self:
            self._id = TextSpan(voice_id).classes("vt-li-id")
            self._sub = TextSpan().classes("vt-li-sub")
        self.on("click", lambda _e: on_click())

    def show(self, item: VoiceItemVM) -> None:
        set_flag(self, "aria-pressed", item.selected)
        self.enabled = item.enabled
        self._sub.text = item.sub
        self._sub.tone = item.sub_tone

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._id.text]


class _VoiceRow(Element):
    """One voice (M L153-159): the pick button and the audition button, updated in place."""

    def __init__(self, item: VoiceItemVM, on_pick: Callable[[str], Any], on_audition: Callable[[str], Any]) -> None:
        super().__init__("div")
        self.voice_id = item.voice_id
        self.classes("vt-li")
        self._flash = ""
        with self:
            self._pick = _VoicePick(item.voice_id, lambda: on_pick(self.voice_id)).mark(f"speak.voice.{item.voice_id}")
            self._audition = IconButton("play", aria=item.audition_label, size=24, icon_size=11,
                                        on_click=lambda: on_audition(self.voice_id)).classes("vt-aud").mark(
                f"speak.audition.{item.voice_id}")
        self.show(item)

    def show(self, item: VoiceItemVM) -> None:
        set_class(self, "on", item.selected)
        self._pick.show(item)
        self._audition.enabled = item.enabled
        self._flash = swap_classes(self, self._flash, flash_class(item.flash_seq))


class _VoiceGroup(Element):
    """One backend group (M L147-162): the header with its chip, then the rows."""

    def __init__(self, group: VoiceGroupVM, on_pick: Callable[[str], Any], on_audition: Callable[[str], Any]) -> None:
        super().__init__("div")
        self.classes("vt-sp-group")
        with self:
            with Element("div").classes("vt-sp-group-head"):
                TextSpan(group.label).classes("vt-sp-group-label")
                self._chip = Chip(group.chip, tone=group.chip_tone, size=20).mark(f"speak.group.{group.backend}")
            with Element("div").classes("vt-sp-group-rows"):
                self._rows = {item.voice_id: _VoiceRow(item, on_pick, on_audition) for item in group.rows}

    def show(self, group: VoiceGroupVM) -> None:
        self._chip.text = group.chip
        self._chip.tone = group.chip_tone
        for item in group.rows:
            self._rows[item.voice_id].show(item)


class _VoiceList(Element):
    """The scrolling voice list: groups and rows are rebuilt only when the set of voices changes."""

    groups = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))

    def __init__(self, on_pick: Callable[[str], Any], on_audition: Callable[[str], Any]) -> None:
        super().__init__("div")
        self._on_pick = on_pick
        self._on_audition = on_audition
        self._shape: tuple[tuple[str, tuple[str, ...]], ...] = ()
        self._views: dict[str, _VoiceGroup] = {}
        self.groups = ()

    def _show(self, groups: tuple[VoiceGroupVM, ...]) -> None:
        shape = tuple((group.backend, tuple(item.voice_id for item in group.rows)) for group in groups)
        if shape != self._shape:
            self._shape = shape
            self.clear()
            with self:
                self._views = {group.backend: _VoiceGroup(group, self._on_pick, self._on_audition)
                               for group in groups}
        for group in groups:
            self._views[group.backend].show(group)


class _PinButton(TextSpan):
    """A 22x22 Pin A / Pin B toggle of a session row (M L416-417)."""

    def __init__(self, letter: str, on_click: Callable[[], Any]) -> None:
        super().__init__(letter, tag="button")
        self.props["type"] = "button"
        self.classes("vt-pin")
        self.on("click", lambda _e: on_click())

    def show(self, on: bool, aria: str) -> None:
        set_flag(self, "aria-pressed", on)
        set_prop(self, "aria-label", aria)
        set_class(self, "is-on", on)
        set_class(self, "vt-hov", not on)


class _TakePick(Disableable):
    """The clickable part of a session row: number, text, voice, Expr, Guid and length."""

    def __init__(self, on_click: Callable[[], Any] | None = None) -> None:
        super().__init__(tag="button")
        self.props["type"] = "button"
        self.classes("vt-trow-pick")
        with self:                                  # (never name an attribute _text: NiceGUI serialises it)
            self._no = TextSpan().classes("vt-sp-c-no vt-trow-sub")
            self._line = TextSpan().classes("vt-sp-c-text vt-trow-text")
            self._voice = TextSpan().classes("vt-sp-c-voice vt-trow-sub")
            self._expr = TextSpan().classes("vt-sp-c-num vt-trow-sub")
            self._guid = TextSpan().classes("vt-sp-c-num vt-trow-sub")
            self._length = TextSpan().classes("vt-sp-c-len vt-trow-sub")
        if on_click is not None:
            self.on("click", lambda _e: on_click())

    @property
    def texts(self) -> tuple[str, ...]:
        """What the row shows: number, text, voice, Expr, Guid, length."""
        return tuple(span.text for span in (self._no, self._line, self._voice, self._expr, self._guid, self._length))

    def show(self, number: int, text: str, lang: str, voice: str, expr: str, guid: str, length: str) -> None:
        self._no.text = str(number)
        self._line.text = text
        set_prop(self._line, "lang", lang)
        self._voice.text = voice
        self._expr.text = expr
        self._guid.text = guid
        self._length.text = length

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self._line.text]


class _TakeRow(Element):
    """One take of this session (M L406-420): row click plays it, Pin A / Pin B, the star."""

    def __init__(self, row: TakeRowVM, playback: PlaybackController) -> None:
        super().__init__("div")
        self.take_id = row.id
        self.classes("vt-trow")
        self._motion = ""
        with self:
            self._pick = _TakePick(lambda: playback.play_take(self.take_id)).mark(f"speak.session.row.{row.id}")
            with TakeActions(playback, row.id, f"speak.session.send.{row.id}").classes("vt-trow-actions"):
                self._pin_a = _PinButton("A", lambda: playback.pin(self.take_id, "A")).mark(
                    f"speak.session.pin_a.{row.id}")
                self._pin_b = _PinButton("B", lambda: playback.pin(self.take_id, "B")).mark(
                    f"speak.session.pin_b.{row.id}")
                self._star = IconButton("star", aria="Star", size=24, icon_size=14, pressed=False,
                                        on_click=lambda: playback.star(self.take_id)).mark(
                    f"speak.session.star.{row.id}")
        self.show(row)

    def show(self, row: TakeRowVM) -> None:
        set_class(self, "on", row.current)
        set_flag(self._pick, "aria-pressed", row.current)
        motion = " ".join(name for name in ("vt-row-in" if row.is_new else "", flash_class(row.flash_seq)) if name)
        self._motion = swap_classes(self, self._motion, motion)
        self._pick.show(row.id, row.text, row.lang, row.voice, row.expr_text, row.guid_text, row.length_text)
        self._pin_a.show(row.pin_a, f"{'Unpin' if row.pin_a else 'Pin'} Take {row.id} as A")
        self._pin_b.show(row.pin_b, f"{'Unpin' if row.pin_b else 'Pin'} Take {row.id} as B")
        star = f"{'Unstar' if row.starred else 'Star'} Take {row.id}"
        self._star.pressed = row.starred
        set_prop(self._star, "aria-label", star)
        set_prop(self._star, "title", star)
        set_class(self._star, "vt-hov", not row.starred)


class _GeneratingRow(Element):
    """The "Generating…" row on top of the list while a take generates (M L2589-2596): not clickable."""

    def __init__(self) -> None:
        super().__init__("div")
        self.classes("vt-trow is-generating")
        with self:
            self._pick = _TakePick().mark("speak.session.generating")
            self._pick.enabled = False
            with Element("div").classes("vt-trow-actions"):
                for letter in ("A", "B"):
                    pin = TextSpan(letter, tag="button").classes("vt-pin vt-hide")
                    set_prop(pin, "disabled", True)
                IconButton("star", aria="Star", size=24, icon_size=14, enabled=False).classes("vt-hide")

    def show(self, row: GeneratingRowVM) -> None:
        self._pick.show(row.take_no, "Generating…", "en", row.voice, row.expr_text, row.guid_text, "…")


class _SessionList(KeyedRows):
    """The takes of this session, newest first, under the "Generating…" row while a take generates. The rows are
    kept by take id and the generating row is one permanent element that is shown or hidden, so a take starting or
    landing adds one row and rebuilds none (R3-2); a star, a pin or the current take updates its row in place."""

    generating = binding.BindableProperty(on_change=lambda sender, value: sender._show_generating(value))

    def __init__(self, playback: PlaybackController) -> None:
        super().__init__(lambda row: _TakeRow(row, playback), key=lambda row: row.id)
        with self:
            self._generating_view = self.head(_GeneratingRow())
        self._generating_view.set_visibility(False)
        self.generating = None

    def _show_generating(self, row: GeneratingRowVM | None) -> None:
        self._generating_view.set_visibility(row is not None)
        if row is not None:
            self._generating_view.show(row)


# ---- the page -----------------------------------------------------------------------------------------

class SpeakPage:
    """One build of the Speak page: its elements, their bindings and the handlers that call the intents."""

    def __init__(self, ctx: ThemeContext) -> None:
        self._ctx = ctx
        behaviours = ctx.behaviours
        self._speak: SpeakController = behaviours.speak()
        self._vm = self._speak.vm
        self._shell = behaviours.status().vm
        self._playback = behaviours.playback()
        self._script: ScriptBox | None = None

    def build(self) -> None:
        with Element("div").classes("vt-speak"):
            with Element("div").classes("vt-sp-col"):
                self._voice_section()
                self._delivery_section()
            with Element("div").classes("vt-sp-col"):
                self._script_section()
                self._take_section()
                self._session_section()
        self._speak.opened()                           # registry changes made outside the app (cli.py)

    # ---------- the Voice panel (M L141-186) ----------

    def _voice_section(self) -> None:
        vm = self._vm
        with Element("section").classes("vt-sp-section"):
            SectionLabel("Voice")
            with Card().classes("vt-sp-voice").mark("speak.voice"):
                with Element("div").classes("vt-sp-voice-ready") as ready:
                    ready.bind_visibility_from(vm, "library_state", backward=lambda state: state == "ready")
                    voices = _VoiceList(self._speak.pick_voice, self._playback.audition).classes(
                        "vt-sp-voice-list em-scroll").mark("speak.voice_list")
                    binding.bind_from(voices, "groups", vm, "groups")
                    with Element("div").classes("vt-sp-voice-foot"):
                        EmiyaButton("Forge New Voice", size=30, on_click=self._go_forge).classes(
                            "vt-sp-forge-new").mark("speak.forge_new")
                        TextSpan().classes("vt-sp-count").mark("speak.count").bind_text_from(vm, "count_text")
                self._empty_state()
                message = TextSpan(tag="p").classes("vt-sp-voice-message").mark("speak.library_message")
                message.bind_text_from(vm, "library_message")
                message.bind_visibility_from(vm, "library_state", backward=lambda state: state == "failed")

    def _empty_state(self) -> None:
        """No voices yet (M L169-184); also after a corrupt registry left the library empty."""
        vm = self._vm
        with Element("div").classes("vt-sp-empty").mark("speak.empty") as empty:
            empty.bind_visibility_from(vm, "library_state", backward=lambda state: state in ("empty", "corrupt"))
            Logo(84, -9, -6, slot=64, asset=self._ctx.asset).bind_still_from(self._shell)
            TextSpan(EMPTY_TITLE, tag="p").classes("vt-sp-empty-title")
            TextSpan(EMPTY_TEXT, tag="p").classes("vt-sp-empty-text")
            with Element("div").classes("vt-sp-empty-actions"):
                EmiyaButton("Forge a Voice", tone="primary", size=34, on_click=self._go_forge).mark("speak.empty.forge")
                sample = EmiyaButton("Use sample.wav", size=30, on_click=self._use_sample).mark("speak.empty.sample")
                sample.bind_action_from(vm, "show_sample", backward=lambda shown: ActionState(
                    bool(shown), "Use sample.wav", tooltip=vm.sample_tip, tone="plain"))

    # ---------- Delivery (M L188-265) ----------

    def _delivery_section(self) -> None:
        vm, speak = self._vm, self._speak
        with Element("section").classes("vt-sp-section"):
            head = SectionLabel("Delivery", right=True)
            if head.right is not None:
                with head.right:
                    reset = EmiyaButton("Reset", size=18, on_click=speak.reset_delivery).mark("speak.reset")
                    reset.bind_visibility_from(vm, "show_reset")
            with Card().classes("vt-sp-delivery").mark("speak.delivery"):
                self._preset_row()
                self._slider_row("Expressiveness", "speak.expr", "expr", "expr_text", 0.25, 2.0,
                                 lambda value: speak.set_delivery(exaggeration=value))
                self._slider_row("Guidance", "speak.guid", "guid", "guid_text", 0.0, 1.0,
                                 lambda value: speak.set_delivery(cfg_weight=value))
                self._language_row()
            with Element("div").classes("vt-sp-note"):
                note = TextSpan(tag="p").classes("vt-sp-note-text").mark("speak.note")
                note.bind_text_from(vm, "note")
                binding.bind_from(note, "tone", vm, "note_tone")
                guid0 = EmiyaButton("Set Guidance 0", size=20, on_click=speak.set_guidance_zero).mark("speak.guid0")
                guid0.bind_visibility_from(vm, "show_guid0")

    def _preset_row(self) -> None:
        vm = self._vm
        with Row("Preset", first=True) as row:
            binding.bind_from(row, "off", vm, "delivery_enabled", backward=lambda on: not on)
            group = Segmented(aria="Delivery preset", item_marker="speak.preset",
                              on_change=self._speak.apply_preset).classes("vt-sp-presets")
            binding.bind_from(group, "options", vm, "presets",
                              backward=lambda presets: tuple((name, name, on) for name, on in presets))
            binding.bind_from(group, "enabled", vm, "delivery_enabled")

    def _slider_row(self, label: str, marker: str, value_field: str, text_field: str, low: float, high: float,
                    on_release: Callable[[float], Any]) -> None:
        """Expressiveness (with the tick at the voice's own value) or Guidance (M L208-227)."""
        vm = self._vm
        expressive = value_field == "expr"
        with Row(label, tip="" if expressive else GUIDANCE_TIP) as row:
            binding.bind_from(row, "off", vm, "delivery_enabled", backward=lambda on: not on)
            if expressive:
                binding.bind_from(row, "tip", vm, "expr_tip")
            with Element("div").classes("vt-sp-knob"):
                slider = GoldSlider(min=low, max=high, step=0.05, value=getattr(vm, value_field), aria=label,
                                    on_release=on_release).mark(marker)
                binding.bind_from(slider, "value", vm, value_field)
                binding.bind_from(slider, "enabled", vm, "delivery_enabled")
                if expressive:
                    binding.bind_from(slider, "tick", vm, "expr_tick")
                value = TextSpan().classes("vt-sp-knob-value").mark(f"{marker}_value")
                value.bind_text_from(vm, text_field)
                if expressive:
                    binding.bind_from(value, "tone", vm, "expr_tone")
                else:
                    value.tone = "text"
                slider.on("input", js_handler=LIVE_JS.replace("{target}", value.html_id))

    def _language_row(self) -> None:
        """Multilingual: the picker and its 23-language list; Standard and Turbo: "English" + "English only"."""
        vm, speak = self._vm, self._speak
        with Row("Language"):                                # never dimmed (M L229)
            with Element("div").classes("vt-pick") as picker:
                picker.bind_visibility_from(vm, "language_mode", backward=lambda mode: mode != "locked")
                button = PickButton(on_click=speak.toggle_language_list).mark("speak.language")
                binding.bind_from(button, "text", vm, "language_name")
                binding.bind_from(button, "expanded", vm, "lang_open")
                button.bind_enabled_from(vm, "language_mode", backward=lambda mode: mode == "pick")
                options = ListBox(on_pick=self._pick_language, on_close=lambda: speak.toggle_language_list(False),
                                  item_marker="speak.language.item").mark("speak.language.list")
                binding.bind_from(options, "items", vm, "lang_items")
                binding.bind_from(options, "open", vm, "lang_open")
            with Element("div").classes("vt-sp-locked").mark("speak.language.locked") as locked:
                locked.bind_visibility_from(vm, "language_mode", backward=lambda mode: mode == "locked")
                TextSpan("English").classes("vt-sp-locked-name")
                Chip("English only", tone="off", size=20)

    # ---------- the Script card (M L271-312) ----------

    def _script_section(self) -> None:
        vm, speak = self._vm, self._speak
        with Element("section").classes("vt-sp-section"):
            head = SectionLabel("Script", right=True).classes("vt-sp-head-pad")
            if head.right is not None:
                with head.right:
                    TextSpan().classes("vt-sp-stats").mark("speak.stats").bind_text_from(vm, "stats_text")
                    warn = TextSpan(tone="warn").classes("vt-sp-stats").mark("speak.stats_warn")
                    warn.bind_text_from(vm, "stats_warn")
                    warn.bind_visibility_from(vm, "stats_warn", backward=bool)
            card = Card().classes("vt-sp-script")
            binding.bind_from(card, "dark", self._shell, "script_dark")
            with card:
                self._toolbar()
                script = ScriptBox(placeholder=SCRIPT_PLACEHOLDER, value=self._ctx.core.session.text,
                                   on_change=lambda event: speak.set_text(event.value or "")).mark("speak.text")
                script.bind_value_from(self._ctx.core.session, "text")
                binding.bind_from(script, "lang", vm, "text_lang")
                self._script = script
                with Element("div").classes("vt-sp-gen"):
                    EmiyaButton(tone="primary", size=34, on_click=lambda: self._generate(1)).classes(
                        "vt-sp-generate").mark("speak.generate").bind_action_from(vm, "generate")
                    EmiyaButton("Generate 3 Takes", size=34, on_click=lambda: self._generate(3)).mark(
                        "speak.generate3").bind_action_from(vm, "generate3")
                with Element("div").classes("vt-sp-summary"):
                    summary = TextSpan().classes("vt-sp-summary-text").mark("speak.status")
                    summary.bind_text_from(vm, "summary_text")
                    binding.bind_from(summary, "tone", vm, "summary_tone")
                    cancel = EmiyaButton("Cancel Queue", tone="link", on_click=speak.cancel_queue).mark(
                        "speak.cancel_queue")
                    cancel.bind_visibility_from(vm, "show_cancel_queue")
                    switch = DotSwitch("Send new takes", on_change=speak.set_call_switch).classes(
                        "vt-call-switch").mark("speak.call_switch")
                    binding.bind_from(switch, "value", self._ctx.core.session, "call_switch")
                    switch.bind_visibility_from(self._playback.vm, "call_switch_visible")
                    job = self._ctx.core.state.job
                    stop = EmiyaButton("Stop", tone="link", on_click=speak.stop_take).mark("speak.stop")
                    stop.bind_visibility_from(job, "take_no", backward=lambda number: number > 0)
                    stop.bind_action_from(job, "phase", backward=lambda phase: ActionState(
                        phase != "stopping", "Stopping…" if phase == "stopping" else "Stop", tone="link"))

    def _toolbar(self) -> None:
        """The language chip or the Use Japanese fix, the Turbo tags, and Clear (M L277-291)."""
        vm, speak = self._vm, self._speak
        with Element("div").classes("vt-sp-toolbar"):
            chip = Chip(size=22, tone="off").mark("speak.lang_chip")
            binding.bind_from(chip, "text", vm, "lang_chip_text")
            binding.bind_from(chip, "tone", vm, "toolbar_mode", backward=lambda mode: "red" if mode == "bad" else "off")
            chip.bind_visibility_from(vm, "toolbar_mode", backward=lambda mode: mode in ("chip", "bad"))
            fix = EmiyaButton(size=22, on_click=speak.use_japanese).classes("vt-sp-fix").mark("speak.use_ja")
            fix.bind_action_from(vm, "lang_chip_text", backward=lambda text: ActionState(True, text, tone="plain"))
            fix.bind_visibility_from(vm, "toolbar_mode", backward=lambda mode: mode == "fix")
            ListHost(self._tag_button).classes("vt-sp-tags").bind_items_from(vm, "tags")
            Element("span").classes("vt-sp-fill")
            EmiyaButton("Clear", size=22, on_click=speak.clear_text).classes("vt-sp-clear").mark(
                "speak.clear confirm.arm").bind_action_from(vm, "clear")

    def _tag_button(self, tag: str) -> None:
        button = EmiyaButton(tag, size=22, tooltip=f"Insert {tag} at the caret").classes("vt-tag").mark(
            f"speak.tag.{tag}")
        button.on("click", lambda event, tag=tag: self._insert_tag(tag, event.args), js_handler=CARET_JS)

    # ---------- the Take card (M L314-386) ----------

    def _take_section(self) -> None:
        vm, playback = self._vm, self._playback
        with Element("section").classes("vt-sp-section") as section:
            section.bind_visibility_from(vm, "card", backward=lambda card: card.visible)
            SectionLabel("Take")
            with Card().classes("vt-sp-take").mark("take.player"):
                self._take_head()
                wave = EmiyaTakeWave(take_url=self._ctx.core.take_url, on_seek=playback.seek).mark("take.wave")
                binding.bind_from(wave, "wave", vm, "card", backward=lambda card: card.wave)
                binding.bind_from(wave, "reduce_always", self._shell, "reduce_always")
                with Row().classes("vt-sp-take-controls"):
                    play = PlayButton(on_click=playback.toggle).mark("take.play")
                    play.bind_action_from(vm, "card", backward=lambda card: card.play)
                    binding.bind_from(play, "playing", vm, "card", backward=lambda card: card.play.label == "Pause")
                    time = TextSpan().classes("vt-sp-time").mark("take.time")
                    time.bind_text_from(vm, "card", backward=lambda card: card.time_text)
                    wave.props["time-target"] = time.html_id    # the wave writes the live time while playing
                    Element("span").classes("vt-sp-fill")
                    ab = Segmented(aria="Compare A and B", item_marker="take.ab", on_change=self._ab).classes(
                        "vt-sp-ab")
                    binding.bind_from(ab, "options", vm, "card", backward=_ab_options)
                    ab.bind_visibility_from(vm, "card", backward=lambda card: card.ab_visible)
                    EmiyaButton("Save As…", size=26, on_click=playback.save_as).mark("take.save").bind_action_from(
                        vm, "card", backward=lambda card: card.save)
                self._call_card()

    def _call_card(self) -> None:
        playback = self._playback
        with Row().classes("vt-sp-take-controls") as row:
            row.bind_visibility_from(playback.vm, "call_enabled")
            send_button(playback, lambda: self._ctx.core.session.take_id, "take.send")
            stop_button(playback, "take.stop_sending")
            hear = DotSwitch("Hear It", on_change=lambda on: self._ctx.behaviours.settings().set_call_preference(
                "call_hear", on)).classes("vt-call-switch").mark("take.hear")
            binding.bind_from(hear, "value", playback.vm, "call_hear")
            TextSpan().classes("vt-sp-summary-text").bind_text_from(playback.vm, "call_status")

    def _take_head(self) -> None:
        vm, playback = self._vm, self._playback
        with Element("div").classes("vt-sp-take-head"):
            with Element("div").classes("vt-sp-take-text"):
                TextSpan(tag="p").classes("vt-sp-take-title").mark("take.title").bind_text_from(
                    vm, "card", backward=lambda card: card.title)
                with Element("p").classes("vt-sp-take-caption"):
                    TextSpan().mark("take.caption").bind_text_from(vm, "card", backward=lambda card: card.caption)
                    warn = TextSpan(tone="warn", tip=CUT_TAKE_TIP).mark("take.caption_warn")
                    warn.bind_text_from(vm, "card", backward=lambda card: card.caption_warn)
                    warn.bind_visibility_from(vm, "card", backward=lambda card: bool(card.caption_warn))
            star = IconButton("star", aria="Star this take", pressed=False, on_click=self._star).mark("take.star")
            binding.bind_from(star, "pressed", vm, "card", backward=lambda card: card.starred)
            star.bind_action_from(vm, "card", backward=_star_action)
            star.bind_visibility_from(vm, "card", backward=lambda card: card.star_visible)
            IconButton("folder", aria="Show in Explorer", on_click=playback.reveal).mark(
                "take.reveal").bind_visibility_from(vm, "card", backward=lambda card: card.star_visible)

    # ---------- "Takes · This Session" (M L388-425) ----------

    def _session_section(self) -> None:
        vm = self._vm
        with Element("section").classes("vt-sp-section").mark("speak.session") as section:
            section.bind_visibility_from(vm, "session_visible")
            head = SectionLabel("Takes · This Session", right=True).classes("vt-sp-head-pad")
            if head.right is not None:
                with head.right:
                    EmiyaButton("All Takes ›", tone="link", on_click=self._all_takes).classes(
                        "is-plain vt-sp-all").mark("speak.all_takes")
            with Card().classes("vt-sp-session"):
                with Element("div").classes("vt-sp-session-cols"):
                    for text, column in SESSION_COLUMNS:
                        TextSpan(text).classes(column)
                rows = _SessionList(self._playback).classes("vt-sp-session-list em-scroll").mark(
                    "speak.session.list").bind_items_from(vm, "session_rows")
                binding.bind_from(rows, "generating", vm, "generating_row")

    # ---------- handlers ----------

    def _go_forge(self) -> None:
        ui.navigate.to(FORGE)

    async def _use_sample(self) -> None:
        """"Use sample.wav": Forge stages voices\\reference\\sample.wav, then Forge opens (§5.9)."""
        route = await self._ctx.behaviours.forge().use_sample()
        if route is not None:
            ui.navigate.to(route)

    def _pick_language(self, code: str) -> None:
        self._speak.set_delivery(language=code)

    def _insert_tag(self, tag: str, caret: Any) -> None:
        at = caret if isinstance(caret, int) and not isinstance(caret, bool) and caret >= 0 else END_OF_TEXT
        caret_after = self._speak.insert_tag(tag, at)
        if self._script is not None and not self._script.is_deleted:
            self._script.set_caret(caret_after)

    async def _generate(self, count: int) -> None:
        """Generate or Generate 3 Takes, with the token its named label carries (Table G rows 12-13)."""
        action = self._vm.generate if count == 1 else self._vm.generate3
        pending = action.pending
        await self._speak.generate(count, pending.token if pending is not None and pending.token else None)

    def _star(self) -> None:
        take_id = self._ctx.core.session.take_id
        if take_id:
            self._playback.star(take_id)

    def _ab(self, side: str) -> None:
        session = self._ctx.core.session
        take_id = session.pin_a if side == "a" else session.pin_b
        if take_id:
            self._playback.ab_switch(take_id)

    def _all_takes(self) -> None:
        """"All Takes ›": the Takes page on its All tab (D-T7)."""
        self._ctx.behaviours.takes().set_tab("all")
        ui.navigate.to(TAKES)


# ---- helpers ------------------------------------------------------------------------------------------

def _ab_options(card: TakeCardVM) -> tuple[tuple[str, str, bool], ...]:
    if not card.ab_visible:
        return ()
    return (("a", card.ab_a_label, card.ab_current == "A"), ("b", card.ab_b_label, card.ab_current == "B"))


def _star_action(card: TakeCardVM) -> ActionState:
    return ActionState(True, "Unstar this take" if card.starred else "Star this take")
