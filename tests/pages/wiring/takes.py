"""Wiring rows of the Takes page (§8.5): the header tabs, the search and both menus, a row's play, pins and star,
Remove Unstarred (arm), Export Starred…, and the compare card's Play, A/B, Keep A and Reuse A's Settings.
None of them loads a model.

Every row opens /takes in the `speak` scenario (This Session shows takes 7-4, pins A 5 and B 7, take 7 current)
unless it names another; `takes` opens on the All tab (M L1320). The menu rows click a menu choice directly: the
menus are shown and hidden by a class, so their choices are always in the page.
"""
from __future__ import annotations

from tests.pages.wiring import Wire

WIRING: tuple[Wire, ...] = (
    # the header tabs (the shell's, wired to TakesController.set_tab)
    Wire("/takes", "takes.tab.all", "takes.set_tab", shows=("All Takes", "7 takes")),
    Wire("/takes", "takes.tab.starred", "takes.set_tab", shows=("Starred Takes", "3 takes of 7")),
    Wire("/takes", "takes.tab.session", "takes.set_tab", scenario="takes", shows=("This Session", "4 takes of 7"),
         note="the Takes board opens on All"),
    # search and the two menus (D-T2)
    Wire("/takes", "takes.search", "takes.set_query", act="type", arg="marco",
         note="the list follows 150 ms after typing stops, on the manual clock"),
    Wire("/takes", "takes.filter_voice.voice.aiko", "takes.set_voice_filter", shows=("3 takes of 7",)),
    Wire("/takes", "takes.filter_model.chatterbox-standard", "takes.set_model_filter", scenario="takes",
         shows=("1 take of 7",)),
    # a row: play, pins and star (pins and stars are PlaybackController's)
    Wire("/takes", "takes.row.6", "takes.row_click", note="plays take 6 from 0; on the current take it toggles"),
    Wire("/takes", "takes.pin_a.6", "playback.pin", shows=("A · Take 6",)),
    Wire("/takes", "takes.pin_b.6", "playback.pin", shows=("B · Take 6",)),
    Wire("/takes", "takes.star.6", "playback.star", shows=("4 starred · 27.8 s of audio",)),
    # the footer
    Wire("/takes", "takes.purge", "takes.purge_unstarred", confirm="arm", shows=("Click Again to Remove 4",),
         note="the second click sends takes 6, 5, 2 and 1 to the user fixture's scratch Recycle Bin"),
    Wire("/takes", "takes.export", "takes.export_starred",
         note="no native window under the user fixture: the folder dialog returns nothing, nothing is copied"),
    Wire("/takes", "takes.folder", "takes.show_folder",
         note="the KitPort opens the Output Folder (a fake KitPort; tests\\pages\\conftest records os.startfile)"),
    # the compare card
    Wire("/takes", "takes.compare_play", "takes.compare_play", note="take 7 (B) is current: it plays"),
    Wire("/takes", "takes.ab.a", "playback.ab_switch", note="A becomes current at the same position"),
    Wire("/takes", "takes.keep_a", "takes.keep", shows=("Kept Take 5 · starred",)),
    Wire("/takes", "takes.reuse", "takes.reuse_settings", shows=("Delivery set from Take 5",),
         note="Speak gets take 5's delivery and opens; nothing loads"),
)

# Intents without a row here, on purpose:
OPT_OUT: tuple[str, ...] = (
    "takes.show_more",      # needs more than 200 takes; test_pages_takes lowers the page size
    "takes.confirm",        # Takes raises no ask: its one confirmation is the purge arm
    "takes.dismiss",
    "takes.opened",         # the page builder calls it on every visit, before the list (test_rel_scale's revisit)
)
