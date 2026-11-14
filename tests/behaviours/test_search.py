"""SearchController (§5.6): the pool from the in-memory snapshots, matching, hints, and the four actions."""
from __future__ import annotations

from collections.abc import Callable

from ui.behaviours import BehaviourFactory
from ui.behaviours.search import FORGE_ID, FREE_ID, HINT_EMPTY, HINT_NONE, MAX_RESULTS, SearchController
from ui.contract.viewmodels import SearchItem

from tests.behaviours import (StubSpeak, factory_for, no_error_logs, offline, started, texts, tick,  # noqa: F401
                              watch_toasts)
from tests.core.test_facade import Rig, request, rigs  # noqa: F401


async def search_on(rigs: Callable[..., Rig],
                    scenario: str = "speak") -> tuple[Rig, BehaviourFactory, SearchController, StubSpeak]:
    rig = await started(rigs, scenario)
    factory, _ = factory_for(rig)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    return rig, factory, factory.search(), speak


def flashes(rig: Rig) -> list[str]:
    seen: list[str] = []
    rig.core.events.flash.subscribe(seen.append)
    return seen


async def test_the_pool_holds_voices_takes_and_two_actions(rigs: Callable[..., Rig]) -> None:
    _, _, search, _ = await search_on(rigs)
    pool = search.pool()
    assert pool[:5] == (
        SearchItem("voice:aiko", "aiko", "Voices · Multilingual · Japanese"),
        SearchItem("voice:friend", "friend", "Voices · Multilingual · English"),
        SearchItem("voice:marco", "marco", "Voices · Multilingual · Italian"),
        SearchItem("voice:narrator", "narrator", "Voices · Standard · English"),
        SearchItem("voice:host-turbo", "host-turbo", "Voices · Turbo · English"),
    )
    assert [item.route_text for item in pool[5:12]] == [f"Takes · {voice} · Take {n}" for n, voice in (
        (7, "aiko"), (6, "aiko"), (5, "aiko"), (4, "marco"), (3, "friend"), (2, "narrator"), (1, "host-turbo"))]
    assert pool[-2:] == (SearchItem(FREE_ID, "Free VRAM", "Model › Action"),
                         SearchItem(FORGE_ID, "Forge a voice", "Forge"))


async def test_matching_is_a_case_insensitive_substring_of_label_and_route(rigs: Callable[..., Rig]) -> None:
    _, factory, search, _ = await search_on(rigs)
    found = search.results("  JAPANESE ")
    assert found == (SearchItem("voice:aiko", "aiko", "Voices · Multilingual · Japanese"),)
    assert factory.status().vm.search_items == found and factory.status().vm.search_hint == ""
    assert [item.item_id for item in search.results("marco")] == ["voice:marco", "take:4"]
    assert [item.item_id for item in search.results("vram")] == [FREE_ID]


async def test_at_most_twelve_results(rigs: Callable[..., Rig]) -> None:
    _, _, search, _ = await search_on(rigs)
    matching = tuple(item for item in search.pool() if "a" in f"{item.label} {item.route_text}".lower())
    assert len(matching) > 12
    found = search.results("a")
    assert len(found) == MAX_RESULTS == 12
    assert found == matching[:12]


async def test_the_hints(rigs: Callable[..., Rig]) -> None:
    _, factory, search, _ = await search_on(rigs)
    vm = factory.status().vm
    assert search.results("") == () and (vm.search_items, vm.search_hint) == ((), HINT_EMPTY)
    assert search.results("zzz") == () and vm.search_hint == HINT_NONE == "No matching voices, takes or actions"
    search.clear()
    assert (search.query, vm.search_items, vm.search_hint) == ("", (), "Type a voice, a take or an action")


async def test_a_voice_jumps_to_speak_picks_it_and_flashes_its_row(rigs: Callable[..., Rig]) -> None:
    rig, factory, search, speak = await search_on(rigs)
    seen = flashes(rig)
    search.results("narrator")
    assert search.run("voice:narrator") == "/"
    assert speak.picked == ["narrator"] and seen == ["voice-narrator"]
    assert (search.query, factory.status().vm.search_items) == ("", ())
    assert rig.fake.loads == [] and rig.state.job.kind == ""          # picking never loads (§4.5)


async def test_a_take_jumps_to_speak_and_plays_from_zero(rigs: Callable[..., Rig]) -> None:
    rig, factory, search, _ = await search_on(rigs)
    seen = flashes(rig)
    assert search.run("take:3") == "/"
    playback = factory.playback()
    assert rig.core.session.take_id == 3 and playback.is_playing_take(3) and playback.vm.target.at == 0.0
    assert seen == ["take-3"] and rig.fake.loads == []


async def test_free_vram_from_search_frees_or_says_why_not(rigs: Callable[..., Rig]) -> None:
    rig, _, search, _ = await search_on(rigs)
    assert search.run(FREE_ID) is None
    await rig.settle()
    assert rig.state.engine.resident == ""
    empty, _, empty_search, _ = await search_on(rigs, "firstrun")
    seen = watch_toasts(empty)
    assert empty_search.run(FREE_ID) is None
    assert texts(seen) == [("Nothing to free · no model loaded", True)]


async def test_forge_a_voice_opens_forge(rigs: Callable[..., Rig]) -> None:
    _, _, search, _ = await search_on(rigs)
    assert search.run(FORGE_ID) == "/forge"


async def test_a_vanished_or_unknown_item_does_nothing(rigs: Callable[..., Rig]) -> None:
    rig, _, search, speak = await search_on(rigs)
    for item_id in ("voice:ghost", "take:99", "take:x", "action:nope", ""):
        assert search.run(item_id) is None
    assert speak.picked == [] and rig.core.session.take_id == 7


async def test_shown_results_follow_new_takes(rigs: Callable[..., Rig]) -> None:
    rig, factory, search, _ = await search_on(rigs)
    search.results("Take 8")
    assert factory.status().vm.search_items == ()
    rig.core.jobs.start_generate([request()])
    await rig.settle()
    await tick()
    assert [item.item_id for item in factory.status().vm.search_items] == ["take:8"]
