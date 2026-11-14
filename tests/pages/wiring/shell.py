"""Wiring rows of the shell (§8.5): the rail, the header, the Model dock, the popover, the Log, the toast,
the search box, the player and the key dispatchers reach their intents with the right confirmation and loads.

Scenario choices where §8.5's list names one whose replayed steps would get in the way: the `switch` and
`lowvram` boards open their ask on the first page load, and a second click on the same dock row closes an
open ask (§5.1), so the dock-row asks start from `other_backend` (a Turbo voice picked, nothing asked yet) and
`forge_low_vram` (8.5 GB held by other programs, nothing asked). Close arms in `generating` (a held take).
"""
from __future__ import annotations

from tests.pages.wiring import Wire

TURBO, MULTILINGUAL = "chatterbox-turbo", "chatterbox-multilingual"
CONFIRM_OFF = (("core", "confirm_switches", False),)
STEADY_LOG_LINE = "Loaded chatterbox-multilingual · 14.2 s · 3.4 GB"

WIRING: tuple[Wire, ...] = (
    # the Model dock (§6.2.4) and its buttons (§5.2)
    Wire("/", "dock.row.chatterbox-turbo", "models.request_switch", scenario="other_backend", confirm="ask",
         loads=(TURBO,), shows=("Don't ask again", "Switch Model"),
         note="Confirm Model Switches on: the dock row asks, with the toggle (D20)"),
    Wire("/", "dock.row.chatterbox-turbo", "models.request_switch", settings=CONFIRM_OFF, loads=(TURBO,),
         note="Confirm Model Switches off and enough VRAM: the switch starts at once"),
    Wire("/", "dock.row.chatterbox-turbo", "models.request_switch", scenario="forge_low_vram",
         settings=CONFIRM_OFF, confirm="ask", loads=(TURBO,), shows=("Load Anyway", "Low VRAM"),
         hides=("Don't ask again",), note="low VRAM always asks, without the toggle (D1, D20)"),
    Wire("/", "dock.free", "models.free_vram", shows=("No model loaded",)),
    Wire("/", "dock.retry", "models.request_switch", scenario="failure", confirm="ask", loads=(TURBO,),
         shows=("Load Anyway",), hides=("Don't ask again",),
         note="named by its aria-label 'Retry loading Turbo'; 9.9 GB held by other programs, so it asks (D1)"),
    Wire("/", "dock.load_default", "models.request_switch", scenario="failure", confirm="ask",
         loads=(MULTILINGUAL,), shows=("Load Anyway",), note="'Load Multilingual' names the model; low VRAM asks"),
    # the popover (§6.2.7): the switch board opens it on the first page load
    Wire("/", "confirm.ask.primary", "models.confirm", scenario="switch", loads=(TURBO,)),
    Wire("/", "confirm.ask.cancel", "models.dismiss", scenario="switch",
         hides=("Unloads Multilingual · frees 3.4 GB",)),
    Wire("/", "confirm.ask.dont_ask", "models.set_dont_ask", scenario="switch",
         note="UI state until the primary (D4)"),
    # the header (§6.2.3)
    Wire("/", "status", "status.toggle_log", shows=(STEADY_LOG_LINE,)),
    Wire("/", "shell.log", "status.toggle_log", shows=(STEADY_LOG_LINE,)),
    Wire("/", "window.minimize", "status.minimize", shows=("Minimize works in the app window",),
         note="the user fixture has no native window, so the kit refuses and the toast explains"),
    Wire("/", "window.close", "status.request_quit", note="idle: quits at once (kit.quit)"),
    Wire("/", "window.close", "status.request_quit", scenario="generating", confirm="arm",
         shows=("Click Again to Quit",), note="busy: the first click arms for 3 s, the second quits"),
    Wire("/", "shell.toast", "status.dismiss_toast", scenario="failure", hides=("Turbo failed to load:",),
         note="the failed load's toast is up after the first page load"),
    # keys: Escape closes the overlays (D12); a bound combo reaches the command registry
    Wire("/", "shell.keys.escape", "status.escape", act="trigger", arg=("command", {"id": "escape"})),
    Wire("/", "shell.keys", "commands.handle", act="trigger", arg=("command", {"id": "play", "combo": "Space"})),
    Wire("/", "shell.keys", "playback.prev", act="trigger", arg=("command", {"id": "prev", "combo": "Alt+Up"}),
         note="Previous Take (Speak only)"),
    Wire("/", "shell.keys", "playback.next", act="trigger", arg=("command", {"id": "next", "combo": "Alt+Down"}),
         note="Next Take (Speak only)"),
    # the player reports to the playback controller
    Wire("/", "shell.player", "playback.on_player_state", act="trigger",
         arg=("state", {"playing": False, "position": 1.0, "duration": 4.6, "ended": False, "error": ""})),
    # the rail search (§5.6): typing shows results, Enter runs the first, Escape closes them (D12)
    Wire("/", "shell.search", "search.results", act="type", arg="aiko", shows=("Voices · Multilingual · Japanese",)),
    Wire("/", "shell.search", "search.run", act="trigger", arg=("keydown.enter", "Free VRAM"),
         shows=("No model loaded",), note="the 'Free VRAM' action, run by Enter"),
    Wire("/", "shell.search", "search.clear", act="trigger", arg=("keydown.esc", {})),
)

# Intents of the shell's controllers without a control of their own.
OPT_OUT: tuple[str, ...] = (
    "status.on_route",              # navigation calls it (every route change)
    "status.close_log",             # Escape reaches it through status.escape (the row above)
    "commands.normalize",           # Settings' key capture normalises through it before commands.bind
    "commands.bindings",            # queries: the shell, Speak and Settings read the bindings, hints and combos
    "commands.combos",
    "commands.hint_parts",
    "commands.set_focus_search",    # the shell registers its search box once, at build
)
