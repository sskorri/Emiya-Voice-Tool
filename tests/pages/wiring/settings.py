"""Wiring rows of the Settings page (§6.12, §8.5): every control reaches its SettingsController intent.

Settings never loads or frees a model, so no row loads anything. The header tabs are the shell's segmented
groups, but their rows live here (T1's hand-off). The Effects rows start from the `settings` board, which opens
on the Effects tab (M L1321). The Keys rows need the Keys tab, which no scenario opens, so they click it first
(Wire.before). Effect Speed is pushed live while the range moves (preview_pref) and saved on release (set_pref).
"""
from __future__ import annotations

from tests.pages.wiring import Wire

ROUTE = "/settings"
EFFECTS_BOARD = "settings"                  # the Settings board scenario: it opens on the Effects tab
KEYS_TAB = "settings.tab.keys"
KEYS_FOOT = "Click a key, then press the new combination. Esc cancels; Backspace clears it."
CTRL_G = {"key": "g", "ctrl": True, "shift": False, "alt": False, "composing": False}

WIRING: tuple[Wire, ...] = (
    # the header tabs (M L2711): set_tab, which also stops a key capture
    Wire(ROUTE, "settings.tab.effects", "settings.set_tab", shows=("Embers, traces and sparks behind the window",),
         hides=("Output Folder",)),
    Wire(ROUTE, "settings.tab.keys", "settings.set_tab", shows=(KEYS_FOOT,)),
    Wire(ROUTE, "settings.tab.general", "settings.set_tab", scenario=EFFECTS_BOARD,
         shows=("Settings are saved with the app, so they survive restarts.",)),
    # General (M L963-1010)
    Wire(ROUTE, "settings.output", "settings.browse_output_folder",
         note="the native folder dialog; the user fixture has no window, so it is a Cancel"),
    Wire(ROUTE, "settings.autoplay", "settings.set_autoplay"),
    Wire(ROUTE, "settings.stop_drops_queue", "settings.set_stop_drops_queue"),
    Wire(ROUTE, "settings.device.cpu", "settings.set_device",
         shows=("CPU applies the next time a model loads", "Much slower than the GPU")),
    Wire(ROUTE, "settings.default_backend.chatterbox-turbo", "settings.set_default_backend",
         note="also moves an idle Forge draft to Turbo (ForgeController.set_backend); loads nothing"),
    Wire(ROUTE, "settings.offline", "settings.set_offline", shows=("Offline Mode off · applies after a restart",)),
    # Effects (M L1012-1061): all live
    Wire(ROUTE, "settings.fx_on", "settings.set_pref", scenario=EFFECTS_BOARD, note="the field is removed"),
    Wire(ROUTE, "settings.fx_speed", "settings.preview_pref", act="trigger", arg=("input", 150),
         scenario=EFFECTS_BOARD, shows=("150%",), note="while the range moves: live, not saved"),
    Wire(ROUTE, "settings.fx_speed", "settings.set_pref", act="trigger", arg=("change", 50),
         scenario=EFFECTS_BOARD, shows=("50%",), note="the release"),
    Wire(ROUTE, "settings.work_glow", "settings.set_pref", scenario=EFFECTS_BOARD),
    Wire(ROUTE, "settings.reduce_motion.always", "settings.set_reduce_motion", scenario=EFFECTS_BOARD),
    Wire(ROUTE, "settings.saber_art.hide", "settings.set_pref", scenario=EFFECTS_BOARD),
    # Keys (M L1063-1084), on the Keys tab
    Wire(ROUTE, "settings.key.gen.key", "settings.listen", before=(KEYS_TAB,), shows=("Press a Key…",)),
    Wire(ROUTE, "settings.key.search.alt", "settings.listen", before=(KEYS_TAB,), shows=("Press a Key…",)),
    Wire(ROUTE, "settings.key.gen.alt", "settings.capture", act="trigger", arg=("keydown", CTRL_G),
         before=(KEYS_TAB,),
         note="a keydown on a slot; it binds only while that slot listens (test_pages_settings covers the flow)"),
    Wire(ROUTE, "settings.key.gen.key", "settings.stop_listening", act="trigger", arg=("blur", {}),
         before=(KEYS_TAB,)),
    Wire(ROUTE, "settings.key.gen.alt", "commands.bind", act="trigger", arg=("keydown", CTRL_G),
         before=(KEYS_TAB, "settings.key.gen.alt"), shows=("Bound Ctrl+G",),
         note="the listening slot binds through the CommandRegistry; BindResult.text is the toast"),
    Wire(ROUTE, "settings.tab.general", "settings.set_tab", before=(KEYS_TAB, "settings.key.gen.key"),
         shows=("Output Folder",), note="a tab change stops the capture the slot click started (§8.5)"),
)

# Settings raises no asks, so the Confirmable intents have no control.
OPT_OUT: tuple[str, ...] = ("settings.confirm", "settings.dismiss")

CALL_WIRING = (
    Wire(ROUTE, "call.refresh", "settings.refresh_call_devices"),
    Wire(ROUTE, "call.device", "settings.select_call_device", act="trigger", arg=("change", "")),
    Wire(ROUTE, "call.enable", "settings.set_call_preference"),
)
