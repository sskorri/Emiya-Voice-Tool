"""Wiring rows of the Forge page (§8.5): its controls reach the ForgeController's intents, with the right
confirmation and exactly the listed model loads.

The rows start from the forge seeds of §6.13. `forge.go` covers every Forge form: the plain forge on the
resident model, the named switch (forge_other_backend), the replace arm on the resident model
(forge_replace) and on another model (forge_replace_other_backend: the first click arms and loads nothing,
the second loads Turbo once, then bakes), and the low-VRAM ask (forge_low_vram, "Then forges rin").
"""
from __future__ import annotations

from tests.pages.wiring import Wire

TURBO = "chatterbox-turbo"
TURBO_NOTE = "Forging loads Turbo · unloads Multilingual (~9 s)"

WIRING: tuple[Wire, ...] = (
    # Reference Clip (§6.9)
    Wire("/forge", "forge.play", "forge.toggle_clip_playback", scenario="forge_ready", shows=("Stop the reference clip",),
         note="plays the staged selection from /media/staged/<clip id>"),
    Wire("/forge", "forge.play", "playback.play_url", scenario="forge_ready", note="the shell's one player"),
    Wire("/forge", "forge.play", "playback.stop", scenario="forge_ready", before=("forge.play",),
         shows=("Play the reference clip",), note="a second click stops the clip the first one started"),
    Wire("/forge", "forge.browse", "forge.browse", scenario="forge_ready",
         note="the user fixture has no native window, so the dialog answers None (a cancel); "
              "test_pages_forge drives a picked file through ui.kit.files.pick_file"),
    # Identity: name, model, language, expressiveness, consent
    Wire("/forge", "forge.name", "forge.set_name", act="type", arg=" 2!", scenario="forge",
         shows=("Letters, digits, _ and - only (it becomes a file name)", "Use rin-2", "Fix the Name First")),
    Wire("/forge", "forge.backend.chatterbox-turbo", "forge.set_backend", scenario="forge",
         shows=(TURBO_NOTE, "English only"), note="picking a model never loads it"),
    Wire("/forge", "forge.language", "forge.toggle_language_list", scenario="forge", shows=("Korean",),
         note="opens the 23-language list (Q2)"),
    Wire("/forge", "forge.expr", "forge.set_exaggeration", act="trigger", arg=("change", 0.7), scenario="forge",
         shows=("0.70",)),
    Wire("/forge", "forge.consent", "forge.set_consent", scenario="forge", shows=("Forge Voice",)),
    # the Forge button (Table F)
    Wire("/forge", "forge.go", "forge.forge", scenario="forge_ready", shows=("Speak with rin",),
         note="Multilingual is resident: it bakes at once"),
    Wire("/forge", "forge.go", "forge.forge", scenario="forge_other_backend", confirm="named", loads=(TURBO,),
         shows=("Speak with rin",), note="'Switch to Turbo & Forge' names the switch"),
    Wire("/forge", "forge.go", "forge.forge", scenario="forge_replace", confirm="arm",
         shows=("Click Again to Replace",), note="aiko exists on Multilingual: the second click replaces it"),
    Wire("/forge", "forge.go", "forge.forge", scenario="forge_replace_other_backend", confirm="arm", loads=(TURBO,),
         shows=("Click Again to Replace", TURBO_NOTE),
         note="the armed button carries the switch token; the second click loads Turbo once, then bakes"),
    Wire("/forge", "forge.go", "forge.forge", scenario="forge_low_vram", confirm="ask", loads=(TURBO,),
         shows=("Load Anyway", "Then forges rin"), hides=("Don't ask again",),
         note="8.5 GB held by other programs: the named control asks (D1, D20)"),
    # the result row
    Wire("/forge", "forge.test_line", "forge.test_line", scenario="forged", note="Multilingual is resident: no loads"),
    Wire("/forge", "forge.speak_with", "forge.speak_with", scenario="forged",
         note="picks rin on Speak and navigates to /; nothing loads"),
    Wire("/forge", "forge.another", "forge.forge_another", scenario="forged", shows=("Name the Voice",)),
)

# Intents without a control of their own on this page, each covered by test_pages_forge or test_forge:
OPT_OUT: tuple[str, ...] = (
    "forge.drop_paths",        # a native drop reaches it through core.events.dropped and the shell
    "forge.pick_clip",         # the second half of Browse… and of a drop
    "forge.upload",            # --browser uploads arrive through the kit's pick_file (BrowserKit's ui.upload)
    "forge.use_sample",        # Speak's "Use sample.wav" (P1's wiring)
    "forge.prefill",           # Voices' Re-forge…, Forge on <model>… and Replace… (P2's wiring)
    "forge.set_language",      # a pick in the open list (its items show only once forge.language opened it)
    "forge.fix_name",          # "Use <suggested>" shows only for an invalid name, which no seed has
    "forge.confirm",           # the shell's popover primary (models.confirm)
    "forge.dismiss",           # the shell's popover Cancel (models.dismiss)
    "forge.opened",            # the page builder calls it on every visit
)
