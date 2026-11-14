"""The Settings page (BUILD_SPEC §6.12; M L959-1116): the General, Effects and Keys sections and the About card.

The header's tabs belong to the shell and set SettingsVM.tab; the page shows that tab's section in the left
column (720 px) and the About card in the right column (320 px) on every tab. Every control calls one
SettingsController intent, and everything it shows comes from SettingsVM (and ShellVM for the still logo)
through passive bindings. The Keys slots are built once and updated in place, so the focused slot keeps its
focus while it listens for a combination. There is no Theme row in this phase, and About has no Window or
Runtime rows (MEM L20, L27).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from nicegui import binding
from nicegui.element import Element

from ..widgets import (
    EMPTY_KEY,
    Box,
    Card,
    DotSwitch,
    EmiyaButton,
    GoldSlider,
    KeyButton,
    Logo,
    Row,
    SectionLabel,
    Segmented,
    TextSpan,
    set_prop,
)

if TYPE_CHECKING:
    from ui.contract.theme import ThemeContext
    from ui.contract.viewmodels import KeyRowVM, SettingsController, SettingsVM

__all__ = ["ABOUT_ROWS", "ART_OPTIONS", "DEVICE_OPTIONS", "LABEL_WIDTH", "MOTION_OPTIONS", "build"]

LABEL_WIDTH = 210                                                       # the row labels (M L968)
DEVICE_OPTIONS = (("cuda", "GPU · CUDA"), ("cpu", "CPU"))               # M L3232
MOTION_OPTIONS = (("system", "Follow Windows"), ("always", "Always"))   # M L3259
ART_OPTIONS = (("show", "Show"), ("hide", "Hide"))                      # M L3260
SPEED_RANGE = (25, 200, 5)                                              # min, max, step (M L1027)
SLOTS = ("key", "alt")

GENERAL_FOOT = "Settings are saved with the app, so they survive restarts."                              # M L1008
EFFECTS_FOOT = "Changes here apply live. Reduce Motion holds the effects and the logo as still frames."   # M L1059
KEYS_FOOT = "Click a key, then press the new combination. Esc cancels; Backspace clears it."              # M L1082
ART_NOTE = "On the Forge page and the first run"                                                         # M L1056

# The About rows (M L1104-1110): label, AboutVM field, AboutVM field of its tooltip ("" = none).
ABOUT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Engine", "engine", ""),
    ("GPU", "gpu", "gpu_tip"),
    ("Models in VRAM", "models_in_vram", ""),
    ("Voice registry", "registry", "registry_tip"),
    ("Voice cache", "cache", "cache_tip"),
    ("Reference clips", "clips", "clips_tip"),
    ("Takes", "takes", ""),
)


def build(ctx: ThemeContext) -> None:
    """The Settings page of one page load (sync: elements and bindings only)."""
    settings = ctx.behaviours.settings()
    vm = settings.vm
    with Element("div").classes("vt-settings"):
        with Element("div").classes("vt-settings-col"):
            _general(settings, vm)
            _effects(settings, vm)
            _keys(settings, vm)
            _voice_chat(settings, vm)
        with Element("div").classes("vt-settings-col"):
            _about(ctx, vm)


# ---- sections -----------------------------------------------------------------------------------------

def _tab_section(vm: SettingsVM, tab: str) -> Element:
    """A left-column section, shown while its header tab is the current one."""
    section = Element("section").classes("vt-settings-section")
    section.bind_visibility_from(vm, "tab", backward=lambda current: current == tab)
    return section


def _general(settings: SettingsController, vm: SettingsVM) -> None:
    """Output Folder, Autoplay New Takes, Run Models On, Default Model for New Voices, Offline Mode."""
    with _tab_section(vm, "general"):
        SectionLabel("General")
        with Card().classes("vt-settings-card").mark("settings.core"):
            _output_folder(settings, vm)
            with Row():
                _switch("Autoplay New Takes", "Plays each take as it arrives", settings.set_autoplay,
                        vm, "autoplay", "settings.autoplay")
            with Row():
                _switch("Stop also drops queued takes", "Cancel waiting takes when you stop the current one",
                        settings.set_stop_drops_queue, vm, "stop_drops_queue", "settings.stop_drops_queue")
            with Row("Run Models On", label_width=LABEL_WIDTH):
                _segments(DEVICE_OPTIONS, "Run models on", settings.set_device, vm, "device", "is-device",
                          "settings.device")
                note = TextSpan().classes("vt-set-note").mark("settings.device_note")
                note.bind_text_from(vm, "device_note")
            with Row("Default Model for New Voices", label_width=LABEL_WIDTH):
                models = _segments(vm.backend_options, "Default model for new voices", settings.set_default_backend,
                                   vm, "default_backend", "is-backend", "settings.default_backend")
                binding.bind_from(models, "options", vm, "backend_options")
            with Row():
                _switch("Offline Mode", "No network checks when a model loads", settings.set_offline,
                        vm, "offline", "settings.offline")
        TextSpan(GENERAL_FOOT, tag="p").classes("vt-settings-foot")


def _output_folder(settings: SettingsController, vm: SettingsVM) -> None:
    """The absolute Output Folder (ellipsis, the full path as its tooltip) and Browse… (M L967-971)."""
    with Row("Output Folder", label_width=LABEL_WIDTH, first=True):
        path = TextSpan().classes("vt-set-path").mark("settings.output.path")
        path.bind_text_from(vm, "out_dir_text")
        binding.bind_from(path, "tip", vm, "out_tip")
        browse = EmiyaButton("Browse…", size=30, on_click=settings.browse_output_folder).classes(
            "vt-set-browse").mark("settings.output")
        browse.bind_action_from(vm, "browse")


def _effects(settings: SettingsController, vm: SettingsVM) -> None:
    """Background Effects, Effect Speed, Work Glow, Reduce Motion, Saber Art (all live)."""
    with _tab_section(vm, "effects"):
        SectionLabel("Effects")
        with Card().classes("vt-settings-card").mark("settings.effects"):
            with Row(first=True):
                _switch("Background Effects", "Embers, traces and sparks behind the window",
                        _pref(settings, "fx_on"), vm, "fx_on", "settings.fx_on")
            with Row("Effect Speed", label_width=LABEL_WIDTH):
                _speed(settings, vm)
            with Row():
                _switch("Work Glow", "Embers brighten behind the cards while you load a model, generate a take or forge a voice",
                        _pref(settings, "work_glow"), vm, "work_glow", "settings.work_glow")
            with Row("Reduce Motion", label_width=LABEL_WIDTH):
                _segments(MOTION_OPTIONS, "Reduce motion", settings.set_reduce_motion, vm, "reduce_motion",
                          "is-motion", "settings.reduce_motion")
            with Row("Saber Art", label_width=LABEL_WIDTH):
                _segments(ART_OPTIONS, "Saber art", _pref(settings, "saber_art"), vm, "saber_art", "is-art",
                          "settings.saber_art")
                TextSpan(ART_NOTE).classes("vt-set-note")
        TextSpan(EFFECTS_FOOT, tag="p").classes("vt-settings-foot")


def _speed(settings: SettingsController, vm: SettingsVM) -> None:
    """The Effect Speed range and its "100%" (M L1026-1029): pushed live while it moves, saved on release."""
    low, high, step = SPEED_RANGE
    with Element("div").classes("vt-set-speed"):
        slider = GoldSlider(min=low, max=high, step=step, value=vm.fx_speed, aria="Effect speed",
                            on_input=lambda value: settings.preview_pref("fx_speed", value),
                            on_release=_pref(settings, "fx_speed")).mark("settings.fx_speed")
        binding.bind_from(slider, "value", vm, "fx_speed")
        text = TextSpan().classes("vt-set-speed-text").mark("settings.fx_speed_text")
        text.bind_text_from(vm, "fx_speed_text")


def _keys(settings: SettingsController, vm: SettingsVM) -> None:
    """The Keys table: "Command", "Key", "Alternate" and one row per command (M L1063-1084)."""
    with _tab_section(vm, "keys"):
        SectionLabel("Keys")
        with Card().classes("vt-settings-card is-keys").mark("settings.keys"):
            with Element("div").classes("vt-keys-head"):
                TextSpan("Command").classes("vt-keys-command")
                TextSpan("Key").classes("vt-keys-slot")
                TextSpan("Alternate").classes("vt-keys-slot")
            for row in vm.key_rows:
                with Row(row.label, first=True), Element("div").classes("vt-key-slots"):
                    for slot in SLOTS:
                        _key_slot(settings, vm, row.id, slot)
        TextSpan(KEYS_FOOT, tag="p").classes("vt-settings-foot")


class _CallDeviceSelect(Element):
    choices = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))
    selected = binding.BindableProperty(on_change=lambda sender, value: set_prop(sender, "value", value))

    def __init__(self, pick: Callable[[str], None]) -> None:
        super().__init__("select")
        self.classes("em-btn vt-call-device")
        self.props["aria-label"] = "Call output device"
        # Native option type-ahead and Alt+Arrow navigation take precedence over page shortcuts.
        self.on("keydown", js_handler="""e => {
            if (e.target.tagName === 'OPTION' || (e.altKey && ['ArrowDown', 'ArrowUp'].includes(e.key))) {
                e.stopPropagation();
            }
        }""")
        self.on("change", lambda event: pick(event.args), js_handler="e => emit(e.target.value)")
        self.choices = ()
        self.selected = ""

    def _show(self, devices: Sequence[tuple[str, str]]) -> None:
        self.clear()
        with self:
            for device_id, label in (("", "Choose a device…"), *devices):
                option = TextSpan(label, tag="option")
                option.props["value"] = device_id


def _voice_chat(settings: SettingsController, vm: SettingsVM) -> None:
    def pref(field: str) -> Callable[[Any], None]:
        return lambda value: settings.set_call_preference(field, value)

    with _tab_section(vm, "voice_chat"):
        SectionLabel("Voice Chat")
        with Card().classes("vt-settings-card").mark("settings.voice_chat"):
            with Row(first=True):
                _switch("Send speech to calls", "Use your own voice or a voice you have permission to use.",
                        pref("call_enabled"), vm, "call_enabled", "call.enable")
            with Row("Call output device", label_width=LABEL_WIDTH):
                device = _CallDeviceSelect(settings.select_call_device).mark("call.device")
                binding.bind_from(device, "choices", vm, "call_devices")
                binding.bind_from(device, "selected", vm, "call_device")
                EmiyaButton("Refresh", on_click=settings.refresh_call_devices).mark("call.refresh")
            with Row():
                TextSpan("Choose CABLE Input here and CABLE Output as Discord’s input device.").classes("vt-set-note")
            with Row("New takes", label_width=LABEL_WIDTH):
                _segments((("preview", "Preview first"), ("switch", "Switch on Speak"), ("auto", "Send at once")),
                          "When to send", pref("call_send"), vm, "call_send", "is-call-choice", "call.mode")
            with Row():
                _switch("Hear It", "Play outgoing takes in your headphones", pref("call_hear"), vm,
                        "call_hear", "call.hear")
            with Row("When already sending", label_width=LABEL_WIDTH):
                _segments((("wait", "Wait its turn"), ("cut", "Cut to new take")), "Overlapping sends",
                          pref("call_overlap"), vm, "call_overlap", "is-call-choice", "call.overlap")
            with Row("Start after", label_width=LABEL_WIDTH):
                _segments(tuple((str(i), f"{i} s") for i in range(4)), "Start after",
                          lambda value: settings.set_call_preference("call_delay_s", int(value)), vm,
                          "call_delay", "", "call.delay")
            with Row("Global shortcuts", label_width=LABEL_WIDTH):
                TextSpan().classes("vt-set-note").bind_text_from(vm, "call_shortcuts")
                EmiyaButton("Edit Keys", tone="link", on_click=lambda: settings.set_tab("keys"))
        note = TextSpan(tag="p", tone="warn").classes("vt-settings-foot").mark("call.note")
        note.bind_text_from(vm, "call_note")
        note.bind_visibility_from(vm, "call_note", backward=bool)
        TextSpan("After the call, restore your usual input device and input mode in Discord.").classes(
            "vt-settings-foot")


def _key_slot(settings: SettingsController, vm: SettingsVM, command_id: str, slot: str) -> None:
    """One 116x26 slot: a click listens, a keydown is captured, leaving it (blur) stops listening."""
    button = KeyButton(on_click=lambda: settings.listen(command_id, slot),
                       on_capture=lambda **combo: settings.capture(command_id, slot, **combo),
                       on_stop=settings.stop_listening).mark(f"settings.key.{command_id}.{slot}")
    for name in ("text", "listening", "aria"):
        binding.bind_from(button, name, vm, "key_rows", backward=_slot_value(command_id, f"{slot}_{name}"))


def _about(ctx: ThemeContext, vm: SettingsVM) -> None:
    """The About card (§7.3): the logo, the name, and the read-only facts."""
    status = ctx.behaviours.status()
    with Element("section").classes("vt-settings-section"):
        SectionLabel("About")
        with Card().classes("vt-settings-card is-about").mark("settings.about"):
            with Element("div").classes("vt-about-head"):
                Logo(53, -6, -4, asset=ctx.asset).bind_still_from(status.vm).mark("settings.about.logo")
                with Element("div").classes("vt-about-titles"):
                    TextSpan("Emiya Voice", tag="p").classes("vt-about-name")
                    TextSpan("Local voice cloning in the Emiya family", tag="p").classes("vt-about-sub")
            for label, field, tip in ABOUT_ROWS:
                _about_row(vm, label, field, tip)


def _about_row(vm: SettingsVM, label: str, field: str, tip: str) -> None:
    with Box().classes("vt-about-row").mark(f"settings.about.{field}") as row:
        if tip:
            binding.bind_from(row, "tip", vm, "about", backward=lambda about: getattr(about, tip))
        TextSpan(label).classes("vt-about-key")
        value = TextSpan().classes("vt-about-value")
        value.bind_text_from(vm, "about", backward=lambda about: getattr(about, field))


# ---- controls -----------------------------------------------------------------------------------------

def _switch(label: str, note: str, on_change: Callable[[bool], Any], vm: SettingsVM, field: str,
            marker: str) -> DotSwitch:
    """A dot toggle row whose dot follows the view model."""
    switch = DotSwitch(label, note=note, value=bool(getattr(vm, field)), on_change=on_change).mark(marker)
    binding.bind_from(switch, "value", vm, field)
    return switch


def _segments(options: Sequence[tuple[str, str]], aria: str, on_change: Callable[[str], Any], vm: SettingsVM,
              field: str, width: str, marker: str) -> Segmented:
    """A segmented group (segments marked <marker>.<key>) whose current segment follows the view model."""
    group = Segmented(options, value=getattr(vm, field), aria=aria, item_marker=marker,
                      on_change=on_change).classes(f"vt-set-segs {width}").mark(marker)
    binding.bind_from(group, "value", vm, field)
    return group


def _pref(settings: SettingsController, key: str) -> Callable[[Any], Any]:
    """An Effects control's handler: set_pref(key, value)."""
    return lambda value: settings.set_pref(key, value)


_SLOT_DEFAULTS: dict[str, Any] = {"text": EMPTY_KEY, "listening": False, "aria": ""}


def _slot_value(command_id: str, field: str) -> Callable[[Sequence[KeyRowVM]], Any]:
    """The binding's backward: one field (key_text, alt_listening, ...) of one command's row."""
    default = _SLOT_DEFAULTS[field.partition("_")[2]]

    def value(rows: Sequence[KeyRowVM]) -> Any:
        row = next((row for row in rows if row.id == command_id), None)
        return default if row is None else getattr(row, field)

    return value
