"""Wiring rows of the Speak page (§8.5): every control reaches its intent with the right confirmation and loads.

Rows that need a draft no FakeEngine scenario holds (a language other than the clip's, values that differ from
the voice's own) set Wire.session: those fields are set on core.session before the page opens. The Take
card's Save As runs against a fake KitPort (run.kit) that answers with a scratch path. Controls that only
navigate name the shell's route intent (status.on_route), which proves the page changed.
"""
from __future__ import annotations

from tests.pages.wiring import Wire

TURBO = "chatterbox-turbo"
GEN_KEY = ("command", {"id": "gen", "combo": "Ctrl+Enter"})
OTHER_LANGUAGE = (("language", "en"),)          # aiko speaking English: the Japanese clip's accent note

WIRING: tuple[Wire, ...] = (
    # the Voice panel (§6.7)
    Wire("/", "speak.voice.marco", "speak.pick_voice", shows=("Italian",), note="the voice's own language"),
    Wire("/", "speak.voice.host-turbo", "speak.pick_voice", shows=("Turbo Is English Only",),
         note="a voice on another model loads nothing; the Japanese script is blocked on Turbo (D3)"),
    Wire("/", "speak.audition.aiko", "playback.audition"),
    Wire("/", "speak.forge_new", "status.on_route", note="Forge New Voice opens Forge"),
    Wire("/", "speak.empty.forge", "status.on_route", scenario="firstrun", note="the empty state's Forge a Voice"),
    Wire("/", "speak.empty.sample", "forge.use_sample", scenario="firstrun"),
    # Delivery
    Wire("/", "speak.reset", "speak.reset_delivery", session=(("exaggeration", 0.7),), hides=("0.70",)),
    Wire("/", "speak.preset.Slower", "speak.apply_preset", shows=("0.30",)),
    Wire("/", "speak.expr", "speak.set_delivery", act="trigger", arg=("change", 1.25), shows=("1.25",)),
    Wire("/", "speak.guid", "speak.set_delivery", act="trigger", arg=("change", 0.3), shows=("0.30",)),
    Wire("/", "speak.language", "speak.toggle_language_list", shows=("voice default",)),
    Wire("/", "speak.guid0", "speak.set_guidance_zero", session=OTHER_LANGUAGE, shows=("0.00",)),
    # the Script card
    Wire("/", "speak.use_ja", "speak.use_japanese", session=OTHER_LANGUAGE, shows=("Japanese · matches text",)),
    Wire("/", "speak.tag.[laugh]", "speak.insert_tag", scenario="other_backend"),
    Wire("/", "speak.clear", "speak.clear_text", confirm="arm", shows=("Click Again to Clear",),
         note="the second click within 3 s empties the script"),
    Wire("/", "speak.text", "speak.set_text", act="type", arg=" Mata ne.", shows=("35 chars · 4 sentences",)),
    Wire("/", "speak.generate", "speak.generate", note="resident: a take starts and nothing loads"),
    Wire("/", "speak.generate", "speak.generate", scenario="other_backend", confirm="named", loads=(TURBO,),
         note="'Switch to Turbo & Generate' names the model; its token switches, then the take runs"),
    Wire("/", "speak.generate", "speak.generate", scenario="lowvram", confirm="ask", loads=(TURBO,),
         shows=("Then generates take 8", "Load Anyway"), hides=("Don't ask again",),
         note="low VRAM always asks (D1); the ask names the take that follows (D20)"),
    Wire("/", "speak.generate3", "speak.generate", scenario="other_backend", confirm="named", loads=(TURBO,),
         note="named by its aria-label 'Switch to Turbo & Generate 3 Takes' (D18)"),
    Wire("/", "speak.generate3", "speak.generate", scenario="generating", queued=3,
         note="Generate 3 Takes while a take runs: three takes wait, nothing loads"),
    Wire("/", "shell.keys", "speak.generate", act="trigger", arg=GEN_KEY, scenario="generating", queued=1,
         note="Ctrl+Enter while Generate is disabled by the running take still queues one (§5.0)"),
    Wire("/", "speak.cancel_queue", "speak.cancel_queue", scenario="queued", queued=0),
    # the Take card
    Wire("/", "take.play", "playback.toggle"),
    Wire("/", "take.wave", "playback.seek", act="trigger", arg=("seek", 2.5)),
    Wire("/", "take.star", "playback.star"),
    Wire("/", "take.reveal", "playback.reveal"),
    Wire("/", "take.ab.a", "playback.ab_switch", shows=("Take 5",)),
    Wire("/", "take.save", "playback.save_as", shows=("Saved ",), note="against a fake KitPort"),
    # "Takes · This Session"
    Wire("/", "speak.session.row.6", "playback.play_take", shows=("Take 6",)),
    Wire("/", "speak.session.pin_a.6", "playback.pin"),
    Wire("/", "speak.session.star.6", "playback.star"),
    Wire("/", "speak.all_takes", "takes.set_tab", note="the Takes page on its All tab (D-T7)"),
)

# Intents without a control of their own: the switch popover's buttons answer every ask through ModelsController,
# and the page builder calls opened() on every visit (a registry.json changed outside the app is read again).
OPT_OUT: tuple[str, ...] = ("speak.confirm", "speak.dismiss", "speak.opened")

# These controls use the call-enabled fixture.
CALL_WIRING = (
    Wire("/", "take.send", "playback.send_take"),
    Wire("/", "take.stop_sending", "playback.stop_sending"),
    Wire("/", "speak.call_switch", "speak.set_call_switch"),
    Wire("/", "speak.stop", "speak.stop_take", scenario="generating"),
)
