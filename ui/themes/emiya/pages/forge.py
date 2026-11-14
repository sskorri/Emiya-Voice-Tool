"""The Forge page (BUILD_SPEC §6.9; M L431-585): Reference Clip, Identity and Forge Notes.

A sync builder: every element is bound passively to the ForgeController's view model (ForgeVM) and every
control calls one of its intents, so nothing here decides or loads anything (R4, R5). The waveform is the
theme's EmiyaBars ("forge": 230 bars, the Prompt and Timbre marks, the selection edges and the Trace On
overlay). Geometry and colours live in static/pages/forge.css; the saber art behind the right column and
the forge blade on a forge start are the shell's.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nicegui import binding, ui
from nicegui.element import Element
from nicegui.events import ValueChangeEventArguments

from ui.contract.plans import ActionState
from ui.contract.records import CheckRow

from ..fx.bars import EmiyaBars
from ..widgets import (
    Card,
    Chip,
    DotSwitch,
    EmiyaButton,
    GoldSlider,
    ListBox,
    ListHost,
    PickButton,
    PlayButton,
    Row,
    SectionLabel,
    Segmented,
    TextField,
    TextSpan,
    set_class,
    set_prop,
    set_style,
)

if TYPE_CHECKING:
    from ui.contract.theme import ThemeContext
    from ui.contract.viewmodels import ForgeController, ForgeVM, ShellVM

__all__ = ["DROP_HINT", "NOTES", "build"]

DROP_HINT = "or drop a WAV anywhere"                                        # M L445
LANGUAGE_TIP = "Pick a language"                                            # NEW (the mockup cycled, M L523)
CONSENT_TEXT = "The speaker agreed to having this voice cloned"             # M L547
NOTES: tuple[str, ...] = (                                                  # M L575-579
    "One speaker, 6–15 s, no music or background noise.",
    "The first 6 s set the prompt (15 s on Turbo); the first 10 s set the timbre.",
    "Turbo needs a clip longer than 5 s.",
    "Reusing a name replaces that voice.",
    "Forged voices are cached, so they load in about 0.03 s.",
)
LABEL_PX = 170                                                              # the Identity label column (M L498)
EXPR_RANGE = (0.25, 2.0, 0.05)                                              # M L538


def build(ctx: ThemeContext) -> None:
    """The Forge page for one visit: two columns, 720 | 16 | 320 (M L432)."""
    forge = ctx.behaviours.forge()
    forge.opened()                                     # a registry.json changed outside the app is re-read
    vm = forge.vm
    shell = ctx.behaviours.status().vm
    with Element("div").classes("vt-forge"):
        with Element("div").classes("vt-forge-col"):
            _reference_clip(forge, vm, shell)
            _identity(forge, vm)
        with Element("div").classes("vt-forge-col"):
            _notes()


# ---- Reference Clip (M L436-491) -------------------------------------------------------------------------

def _reference_clip(forge: ForgeController, vm: ForgeVM, shell: ShellVM) -> None:
    with Element("section").classes("vt-forge-section"):
        SectionLabel("Reference Clip")
        with Card(dark=True).classes("vt-forge-clip").mark("forge.clip"):
            _clip_head(forge, vm)
            _waveform(vm, shell)
            note = TextSpan(tag="p").classes("vt-forge-note")
            note.bind_text_from(vm, "clip_note")
            note.bind_visibility_from(vm, "clip_note", backward=bool)
            checks = ListHost(_check_row).classes("vt-forge-checks").bind_items_from(vm, "checks")
            checks.bind_visibility_from(vm, "checks", backward=bool)


def _clip_head(forge: ForgeController, vm: ForgeVM) -> None:
    """Play, the file name and its facts, the drop hint and Browse… (M L439-447)."""
    with Element("div").classes("vt-forge-clip-head"):
        play = PlayButton(aria="Play the reference clip", stop_icon="stop",
                          on_click=forge.toggle_clip_playback).mark("forge.play")
        play.bind_action_from(vm, "play")
        binding.bind_from(play, "playing", vm, "playing")
        with Element("div").classes("vt-forge-clip-text"):
            TextSpan(tag="p").classes("vt-forge-file").bind_text_from(vm, "clip_file")
            meta = TextSpan(tag="p").classes("vt-forge-meta").bind_text_from(vm, "clip_meta")
            binding.bind_from(meta, "tone", vm, "clip_meta_tone")
        TextSpan(DROP_HINT).classes("vt-forge-drop")
        browse = EmiyaButton("Browse…", size=26, on_click=forge.browse).classes("vt-forge-browse").mark("forge.browse")
        browse.bind_action_from(vm, "browse")


def _waveform(vm: ForgeVM, shell: ShellVM) -> None:
    """230 real peaks with the marks, the selection edges and the Trace On overlay (M L448-478)."""
    bars = EmiyaBars("forge")
    for name in ("bars", "marks", "sel_pct", "tracing", "trace_progress"):
        binding.bind_from(bars, name, vm, name)
    binding.bind_from(bars, "reduce_always", shell, "reduce_always")
    binding.bind_from(bars, "aria", vm, "bars_aria")


def _check_row(check: CheckRow) -> None:
    """Length, Turbo rule, Channels, Sample rate or Peak: a dot, the label and the detail (M L482-487)."""
    with Row().classes("vt-forge-check"):
        dot = Element("span").classes(f"vt-forge-dot is-{check.tone}")
        set_prop(dot, "aria-hidden", "true")
        TextSpan(check.label).classes("vt-forge-check-label")
        TextSpan(check.detail).classes(f"vt-forge-check-detail is-{check.tone}")


# ---- Identity (M L493-567) -------------------------------------------------------------------------------

def _identity(forge: ForgeController, vm: ForgeVM) -> None:
    with Element("section").classes("vt-forge-section"):
        SectionLabel("Identity")
        with Card(dark=True).classes("vt-forge-identity"):
            _name_row(forge, vm)
            _model_row(forge, vm)
            _language_row(forge, vm)
            _expr_row(forge, vm)
            _consent_row(forge, vm)
            _actions(forge, vm)


def _name_row(forge: ForgeController, vm: ForgeVM) -> None:
    """Name: the input, its message and "Use <suggested>" (M L497-504)."""
    with Row(first=True):
        label = TextSpan("Name", tag="label").classes("vt-row-label is-fixed")
        set_style(label, "width", f"{LABEL_PX}px")
        field = _NameField(placeholder="voice name", aria="Name",
                           on_change=lambda event: forge.set_name(_text(event))).classes("vt-forge-name").mark(
            "forge.name")
        set_prop(label, "for", field.html_id)
        binding.bind_from(field, "value", vm, "name")
        binding.bind_from(field, "bad", vm, "name_bad")
        field.bind_enabled_from(vm, "inputs_enabled")
        message = TextSpan().classes("vt-forge-name-msg").bind_text_from(vm, "name_msg")
        binding.bind_from(message, "tone", vm, "name_tone")
        set_prop(field, "aria-describedby", message.html_id)
        fix = EmiyaButton(size=22, on_click=forge.fix_name).classes("vt-forge-fix").mark("forge.fix_name")
        fix.bind_action_from(vm, "fix_label", backward=lambda text: ActionState(True, text, tone="plain"))
        fix.bind_visibility_from(vm, "fix_label", backward=bool)


def _model_row(forge: ForgeController, vm: ForgeVM) -> None:
    """Model: one segment per enabled backend, and the note on what forging loads (M L506-517)."""
    with Row("Model", label_width=LABEL_PX):
        models = Segmented(aria="Model for this voice", item_marker="forge.backend",
                           on_change=forge.set_backend).classes("vt-forge-models").mark("forge.backend")
        binding.bind_from(models, "options", vm, "segments")
        binding.bind_from(models, "enabled", vm, "inputs_enabled")
    note = TextSpan(tag="p").classes("vt-forge-model-note").bind_text_from(vm, "model_note")
    binding.bind_from(note, "tone", vm, "model_note_tone")


def _language_row(forge: ForgeController, vm: ForgeVM) -> None:
    """Language: the 23-language list on Multilingual (Q2), "English" + "English only" otherwise (M L519-532)."""
    with Row("Language", label_width=LABEL_PX):
        with Element("div").classes("vt-pick") as picker:
            button = PickButton(tip=LANGUAGE_TIP, on_click=forge.toggle_language_list).mark("forge.language")
            binding.bind_from(button, "text", vm, "language_name")
            binding.bind_from(button, "expanded", vm, "lang_open")
            button.bind_enabled_from(vm, "inputs_enabled")
            options = ListBox(on_pick=forge.set_language, on_close=lambda: forge.toggle_language_list(False),
                              item_marker="forge.language.item")
            binding.bind_from(options, "items", vm, "lang_items")
            binding.bind_from(options, "open", vm, "lang_open")
        picker.bind_visibility_from(vm, "lang_locked", backward=lambda locked: not locked)
        with Element("div").classes("vt-forge-locked") as locked:
            TextSpan("English").classes("vt-forge-locked-name")
            Chip("English only", tone="off", size=20)
        locked.bind_visibility_from(vm, "lang_locked")


def _expr_row(forge: ForgeController, vm: ForgeVM) -> None:
    """Default Expressiveness: the slider and its value, live while dragging; Turbo dims the row (M L534-542)."""
    row = Row("Default Expressiveness", label_width=LABEL_PX).classes("vt-forge-expr")
    binding.bind_from(row, "off", vm, "expr_enabled", backward=lambda applies: not applies)
    with row:
        with Element("div").classes("vt-forge-expr-ctl"):
            low, high, step = EXPR_RANGE
            slider = _ExprSlider(min=low, max=high, step=step, value=0.5, aria="Default Expressiveness",
                                 on_input=lambda value: setattr(shown, "text", f"{value:.2f}"),
                                 on_release=forge.set_exaggeration).mark("forge.expr")
            binding.bind_from(slider, "value", vm, "expr")
            binding.bind_from(slider, "applies", vm, "expr_enabled")
            binding.bind_from(slider, "idle", vm, "inputs_enabled")
            shown = TextSpan().classes("vt-forge-expr-value").bind_text_from(vm, "expr_text")
        TextSpan().classes("vt-forge-expr-note").bind_text_from(vm, "expr_note")


def _consent_row(forge: ForgeController, vm: ForgeVM) -> None:
    """The consent toggle with "Required", gold-dim until it is on (M L544-551)."""
    with Row():
        consent = DotSwitch(CONSENT_TEXT, note="Required", on_change=forge.set_consent).classes(
            "vt-forge-consent").mark("forge.consent")
        binding.bind_from(consent, "value", vm, "consent")
        consent.bind_enabled_from(vm, "inputs_enabled")
        binding.bind_from(consent.note, "tone", vm, "consent_req_tone")


def _actions(forge: ForgeController, vm: ForgeVM) -> None:
    """The Forge button (Table F), or after a forge the result row (M L553-564)."""
    with Element("div").classes("vt-forge-actions") as main:
        go = EmiyaButton(tone="primary", size=34, on_click=lambda: _forge(forge)).classes("vt-forge-go").mark(
            "forge.go confirm.arm")
        go.bind_action_from(vm, "forge")
    main.bind_visibility_from(vm, "forged", backward=lambda forged: not forged)
    with Element("div").classes("vt-forge-actions") as result:
        speak = EmiyaButton(tone="primary", size=34, on_click=lambda: _speak_with(forge)).classes(
            "vt-forge-speak").mark("forge.speak_with")
        speak.bind_action_from(vm, "speak_label", backward=lambda label: ActionState(True, label))
        EmiyaButton(size=34, on_click=forge.test_line).mark("forge.test_line").bind_action_from(vm, "test_line")
        EmiyaButton(size=34, on_click=forge.forge_another).mark("forge.another").bind_action_from(vm, "another")
    result.bind_visibility_from(vm, "forged", backward=bool)


# ---- Forge Notes (M L571-582) ----------------------------------------------------------------------------

def _notes() -> None:
    with Element("section").classes("vt-forge-section"):
        SectionLabel("Forge Notes")
        with Card(dark=True).classes("vt-forge-notes").mark("forge.notes"):
            for text in NOTES:
                TextSpan(text, tag="p")


# ---- intents that need more than one call ----------------------------------------------------------------

async def _forge(forge: ForgeController) -> None:
    """The Forge button hands back the switch token its label carries (rows 13-15), or none."""
    pending = forge.vm.forge.pending
    await forge.forge(pending.token if pending is not None and pending.token else None)


def _speak_with(forge: ForgeController) -> None:
    route = forge.speak_with()
    if route is not None:
        ui.navigate.to(route)


def _text(event: ValueChangeEventArguments) -> str:
    return "" if event.value is None else str(event.value)


# ---- page-local widgets ----------------------------------------------------------------------------------

class _NameField(TextField):
    """The 200x30 name input; `bad` turns its text red (M L3055)."""

    bad = binding.BindableProperty(on_change=lambda sender, value: set_class(sender, "is-bad", bool(value)))

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bad = False                 # a first value, so a bound True runs on_change


class _ExprSlider(GoldSlider):
    """The Expressiveness slider: movable only while it `applies` (not Turbo) and the app is `idle`."""

    applies = binding.BindableProperty(on_change=lambda sender, _: sender._gate())
    idle = binding.BindableProperty(on_change=lambda sender, _: sender._gate())

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.applies = True
        self.idle = True

    def _gate(self) -> None:
        self.enabled = bool(self.applies) and bool(self.idle)
