"""Wiring rows of the Models page (§8.5): the cards, Free VRAM and the Behavior rows reach the ModelsController's
intents with the right confirmation and loads.

A card's Load / Switch is named by its aria-label ("Load Turbo" / "Switch to Turbo"), so it starts at once even
with Confirm Model Switches on, and loads exactly its model; on low VRAM it asks "Load Anyway" without
"Don't ask again" (D1, D20). The steady-state rows run in `models`, the Models board (the `speak` data, opened on
/models). The `lowvram` board replays a dock ask on its first page load: a runner closes an ask that is already
open before it clicks, so the ask it then sees is the card's own.
"""
from __future__ import annotations

from tests.pages.wiring import Wire

MULTILINGUAL, STANDARD, TURBO = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"

WIRING: tuple[Wire, ...] = (
    # the cards (§6.11 "Card action")
    Wire("/models", f"models.switch.{TURBO}", "models.request_switch", scenario="models", confirm="named",
         loads=(TURBO,), shows=("Turbo ready",),
         note="'Switch to This Model', named 'Switch to Turbo': no ask although Confirm Model Switches is on"),
    Wire("/models", f"models.switch.{STANDARD}", "models.request_switch", scenario="firstrun", confirm="named",
         loads=(STANDARD,), shows=("Standard ready",), note="'Load This Model', named 'Load Standard'"),
    Wire("/models", f"models.switch.{TURBO}", "models.request_switch", scenario="lowvram", confirm="ask",
         loads=(TURBO,), shows=("Load Anyway", "Low VRAM"), hides=("Don't ask again",),
         note="8.5 GB held by other programs: the named card asks (D1), without the toggle (D20)"),
    Wire("/models", "models.unload", "models.free_vram", scenario="models", shows=("Freed 3.4 GB",),
         note="the resident card's 'Unload · Free 3.4 GB'"),
    # the VRAM card
    Wire("/models", "models.free", "models.free_vram", scenario="models", shows=("Freed 3.4 GB",)),
    # Behavior
    Wire("/models", "models.warmup.last", "models.set_warmup", scenario="models",
         note="applies at the next launch: nothing loads now"),
    Wire("/models", "models.idle.30", "models.set_idle_unload", scenario="models"),
    Wire("/models", "models.confirm", "models.set_confirm_switches", scenario="models"),
)

# Every ModelsController intent the page does not wire is a shell control (the popover's confirm, dismiss and
# set_dont_ask rows in wiring\shell.py), so nothing is left out on purpose here.
OPT_OUT: tuple[str, ...] = ()
