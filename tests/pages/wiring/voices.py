"""Wiring rows of the Voices page (§8.5): the Library filter, the rows, the Details edits, the clip and file
actions, the hand-offs to Forge and Speak, and the Delete arm reach their intents with no model load.

Every row starts from the `voices` board (the speak data on /voices) and acts on the voice in the details (aiko,
the first row). Two acts are not one click and live in tests\\pages\\test_pages_voices.py instead: picking a
language needs the list open first (the picker row below opens it), and Speak with / Re-forge… / Forge on …
navigate away, which the runner checks only as reaching the intent.

Runner rules for these rows: ui.kit.files.reveal and ui.kit.files.open_folder start Explorer, so a runner must
patch them before acting on voices.reveal (test_pages_voices.py has an autouse fixture that does). Under the
user fixture there is no native window, so voices.replace's file dialog returns None at once.
"""
from __future__ import annotations

from tests.pages.wiring import Wire

VOICES = "voices"

WIRING: tuple[Wire, ...] = (
    # the Library (§6.8)
    Wire("/voices", "voices.filter", "voices.set_filter", act="type", arg="mar", scenario=VOICES,
         shows=("1 of 5 voices",), note="a substring of '<name> <Language name>'"),
    Wire("/voices", "voices.backend_filter.chatterbox-turbo", "voices.set_filter", scenario=VOICES,
         shows=("1 of 5 voices",), note="only enabled backends get a segment"),
    Wire("/voices", "voices.row.marco", "voices.select", scenario=VOICES, shows=("Speak with marco",)),
    # the Details edits: registry only (D-F2: the 23-language list)
    Wire("/voices", "voices.edit.language", "voices.toggle_language_list", scenario=VOICES, shows=("Korean",),
         note="the list opens with all 23 languages; a pick is two acts (test_pages_voices.py)"),
    Wire("/voices", "voices.edit.expr", "voices.edit", act="trigger", arg=("change", 0.7), scenario=VOICES,
         shows=("0.70",), note="saved on release; writes cache/registry.json only"),
    # the clip and the files
    Wire("/voices", "voices.play", "voices.toggle_clip_playback", scenario=VOICES,
         note="the shell's player gets the reference clip's /media/clip URL"),
    Wire("/voices", "voices.replace", "voices.replace_clip", scenario=VOICES,
         note="no native window: the dialog returns None and nothing is prefilled"),
    Wire("/voices", "voices.reveal", "voices.reveal_cache", scenario=VOICES,
         note="patch ui.kit.files.reveal and open_folder first: they start Explorer"),
    # the hand-offs (§9: ForgeController.prefill, SpeakController.pick_voice)
    Wire("/voices", "voices.speak", "voices.speak_with", scenario=VOICES),
    Wire("/voices", "voices.reforge", "voices.reforge", scenario=VOICES),
    Wire("/voices", "voices.copy", "voices.forge_copy", scenario=VOICES),
    # Delete arms first (§5.1): "Click Again to Delete", then the .pt goes to the injected trash
    Wire("/voices", "voices.delete", "voices.delete", scenario=VOICES, confirm="arm",
         shows=("Click Again to Delete",), note="the second click deletes through facade.delete_voice"),
)

# The page raises no ask, so nothing confirms or dismisses one; Escape and navigation close the language list
# through toggle_language_list(False); the page builder calls opened() on every visit.
OPT_OUT: tuple[str, ...] = ("voices.confirm", "voices.dismiss", "voices.opened")
