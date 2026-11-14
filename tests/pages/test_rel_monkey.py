"""Monkey tests (R1): seeded random actions over the whole app, every action checked against the invariants.

The actions and the checks live in tests\\pages\\_rel_actions.py: clicks and fast double clicks on every shown,
enabled control, the rail, typing (scripts, names, odd text), sliders, key slots, the rail search and its jumps,
the key commands, the player's reports, Back and reload, native drops, minimize and restore, time passing on the
manual clock, releasing the scenario's holds, engine and disk faults, and what other programs do behind the app's
back (cli.py editing the registry, a WAV deleted in Explorer). After every action: no exception or ERROR log, at
most one model resident, every load behind a named control, a confirmed ask or the user's warm-up, every unload
behind Free VRAM, Unload or the idle setting, at most 3 takes queued, no stuck job once the holds are released, no
page.error marker, no polled binding, no engine call on the event-loop thread. At the end of a run the takes
index, ui_settings.json, the load history and the library agree with memory, and no temp file is left.

- test_monkey_short (default suite): 3 scenarios x 1 seed x 300 actions.
- test_monkey_full (soak): every scenario x every start route x 3 seeds x 48 actions (25,056 actions).
- test_monkey_real_clock (soak): 8 scenarios with the app's timers running in real time.
- test_time_passing_loads_and_frees_nothing, test_search_jumps_load_nothing, test_odd_text_in_every_box (soak):
  the exhaustive versions of three probes the monkey samples (two hours on every route; every result of several
  queries; every odd text in every box).
- The race tests at the end pin the bugs the double clicks pointed at (R1-BUG-1, R1-BUG-2, both fixed by the
  core's ordered take edits): a held-back worker must never let a later star or purge overtake an earlier one.

A failure prints the whole trace and names the seed, the scenario and the start route; run the test id with -k to
replay it. REL_TRACE_DIR=<folder> also writes each failing trace there; REL_SEEDS, REL_ACTIONS and
REL_REAL_ACTIONS change the soak.
"""
from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from ui.contract.routes import PAGE_ID, ROUTES
from ui.core import Core
from ui.core.fake import SCENARIOS, FakeEngine

from tests.engine.conftest import write_clip
from tests.pages._rel_actions import (EMIYA, Invariants, Monkey, Trace, is_enabled, open_monkey, quiesce, scan,
                                      summary)
from tests.pages._rel_actions import clock_kind  # noqa: F401  (a fixture)
from tests.pages.test_wiring import install_kit

SHORT: tuple[tuple[str, str], ...] = (("speak", "/"), ("queued", "/takes"), ("firstrun", "/forge"))
SHORT_ACTIONS = 300
SOAK_SEEDS = tuple(range(1, int(os.environ.get("REL_SEEDS", "3")) + 1))
SOAK_ACTIONS = int(os.environ.get("REL_ACTIONS", "48"))
MODEL_WORK = ("make_resident", "load_backend", "unload_backend", "load_voice")


def drop_files(root: Path) -> list[Path]:
    """Clips a user might drop or browse to: a good 9 s one first (the Browse… answer), then odd ones."""
    folder = root / "drops"
    files = [write_clip(folder / "voice_9s.wav", 9.0), write_clip(folder / "long_31s.wav", 31.0),
             write_clip(folder / "short_2s.wav", 2.0)]
    notes = folder / "notes.txt"
    notes.write_text("not audio", encoding="utf-8")
    return [*files, notes, folder / "gone.wav"]


def monkey_id(scenario: str, route: str, seed: int) -> str:
    return f"{scenario}-{PAGE_ID[route]}-{seed}"


async def run_monkey(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                     monkeypatch: pytest.MonkeyPatch, *, scenario: str, route: str, seed: int, actions: int,
                     test: str) -> Monkey:
    drops = drop_files(tmp_path)
    install_kit(monkeypatch, tmp_path, file_result=drops[0])
    inv = Invariants(user, core, fake, monkeypatch)
    trace = Trace(f"R1 monkey {monkey_id(scenario, route, seed)} (scenario={scenario} route={route} seed={seed} "
                  f"actions={actions})\nreplay: pytest tests/pages/test_rel_monkey.py::{test} "
                  f"-k {monkey_id(scenario, route, seed)} -p no:cacheprovider")
    monkey = Monkey(user, core, fake, inv, trace, rng=random.Random(f"{scenario}|{route}|{seed}"), drops=drops)
    try:
        await open_monkey(user, core, fake, open_route, route, inv)
        await monkey.run(actions)
        await monkey.finish()
    finally:
        inv.close()
    print(f"\n[{monkey_id(scenario, route, seed)}] {summary(monkey)}")
    return monkey


@pytest.mark.parametrize("scenario,route,seed", [
    pytest.param(scenario, route, 1, marks=pytest.mark.scenario(scenario), id=monkey_id(scenario, route, 1))
    for scenario, route in SHORT])
async def test_monkey_short(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch, scenario: str, route: str, seed: int) -> None:
    monkey = await run_monkey(user, core, fake, open_route, tmp_path, monkeypatch, scenario=scenario, route=route,
                              seed=seed, actions=SHORT_ACTIONS, test="test_monkey_short")
    assert monkey.index >= SHORT_ACTIONS


@pytest.mark.soak
@pytest.mark.parametrize("scenario,route,seed", [
    pytest.param(scenario, route, seed, marks=pytest.mark.scenario(scenario), id=monkey_id(scenario, route, seed))
    for scenario in SCENARIOS for route in ROUTES for seed in SOAK_SEEDS])
async def test_monkey_full(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch, scenario: str, route: str, seed: int) -> None:
    monkey = await run_monkey(user, core, fake, open_route, tmp_path, monkeypatch, scenario=scenario, route=route,
                              seed=seed, actions=SOAK_ACTIONS, test="test_monkey_full")
    assert monkey.index >= SOAK_ACTIONS


REAL_CLOCK = ("speak", "queued", "loading", "failure", "forging", "freeing", "firstrun", "switch")
REAL_CLOCK_ACTIONS = int(os.environ.get("REL_REAL_ACTIONS", "200"))


@pytest.mark.soak
@pytest.mark.parametrize("clock_kind", ["real"], indirect=True)
@pytest.mark.parametrize("scenario,route,seed", [
    pytest.param(scenario, "/", 1, marks=pytest.mark.scenario(scenario), id=monkey_id(scenario, "/", 1))
    for scenario in REAL_CLOCK])
async def test_monkey_real_clock(clock_kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                 tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, route: str,
                                 seed: int) -> None:
    """The monkey with the app's own timers running in real time (the job ticker, the device poller, arm expiry),
    between and during the actions; time passing is then no action of its own."""
    monkey = await run_monkey(user, core, fake, open_route, tmp_path, monkeypatch, scenario=scenario, route=route,
                              seed=seed, actions=REAL_CLOCK_ACTIONS, test="test_monkey_real_clock")
    assert not monkey.manual and monkey.index >= REAL_CLOCK_ACTIONS


# ---- the bugs the monkey's bursts pointed at (R1-BUG-1, R1-BUG-2; fixed by core.edits) ------------------

LATE_LIMIT_S = 2.0               # a held-back worker goes on after this, so an app that orders its writes never hangs


@pytest.mark.parametrize("route,marker,take_id", [
    pytest.param("/takes", "takes.star.5", 5, id="takes-row-unstarred"),
    pytest.param("/", "take.star", 7, id="speak-card-starred")])
async def test_a_double_click_on_a_star_leaves_it_as_it_was(user: User, core: Core, fake: FakeEngine,
                                                           open_route: Any, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch, route: str, marker: str,
                                                           take_id: int) -> None:
    """R1-BUG-1: two quick clicks toggle a star twice, so the take must end as it began. The first click's worker
    is held back here until the second click's write is done (a busy CPU or a scan of index.json can delay a thread
    that long). Before the fix each click spawned its own set_starred worker, so the second write landed first:
    Take 5 ended starred after star + unstar, and the starred Take 7 ended unstarred after unstar + star (Remove
    Unstarred then sent it to the Recycle Bin). Now every take edit goes through core.edits, one at a time in click
    order: the second write never starts while the first is held, so the first goes on after LATE_LIMIT_S and the
    order holds."""
    import threading

    from ui.core.services.takes_store import TakesStore

    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, f"star race {route}")
    try:
        await open_monkey(user, core, fake, open_route, route, monkey.inv)
        if route == "/takes":
            await monkey.click_marker("takes.tab.all")
        original = TakesStore.set_starred
        order: list[bool] = []
        mx = threading.Lock()
        second_done = threading.Event()

        def first_waits_for_second(self: TakesStore, number: int, starred: bool) -> Any:
            with mx:
                order.append(starred)
                first = len(order) == 1
            if first:
                second_done.wait(LATE_LIMIT_S)       # the first click's worker is scheduled late
                return original(self, number, starred)
            try:
                return original(self, number, starred)
            finally:
                second_done.set()

        monkeypatch.setattr(TakesStore, "set_starred", first_waits_for_second)
        before = core.takes.get(take_id).starred
        star = monkey.first(marker)
        assert star is not None
        await monkey.burst(star, times=2, yields=0)
        assert order == [not before, before], f"the clicks asked for {order}"
        after = core.takes.get(take_id).starred
        assert after == before, f"two clicks on its star left Take {take_id} starred={after} (it was {before})"
    finally:
        monkey.inv.close()


async def test_a_take_starred_before_the_purge_is_confirmed_is_kept(user: User, core: Core, fake: FakeEngine,
                                                                   open_route: Any, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """R1-BUG-2: Remove Unstarred is armed; the user stars Take 5 to keep it and then confirms. The star's write is
    held back here until the purge is done (as an fsync of a long index.json under a virus scan can hold it).
    Before the fix the purge's own worker got to the store first: Take 5, still unstarred there, went to the
    Recycle Bin, and the star then failed with "Couldn't save the star: Take 5 is not in the takes list". Now the
    purge waits for every edit asked before it (core.edits), so the star goes on after LATE_LIMIT_S and lands
    first, and the purge keeps Take 5."""
    import threading

    from ui.core.services.takes_store import TakesStore

    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, "star before purge")
    try:
        await open_monkey(user, core, fake, open_route, "/takes", monkey.inv)
        await monkey.click_marker("takes.tab.all")
        assert core.takes.get(5) is not None and not core.takes.get(5).starred
        await monkey.click_marker("takes.purge")                    # armed: "Click Again to Remove …"
        star_write, purge_write = TakesStore.set_starred, TakesStore.purge_unstarred
        purged = threading.Event()

        def late_star(self: TakesStore, number: int, starred: bool) -> Any:
            purged.wait(LATE_LIMIT_S)
            return star_write(self, number, starred)

        def purge(self: TakesStore, *args: Any, **kwargs: Any) -> Any:
            try:
                return purge_write(self, *args, **kwargs)
            finally:
                purged.set()

        monkeypatch.setattr(TakesStore, "set_starred", late_star)
        monkeypatch.setattr(TakesStore, "purge_unstarred", purge)
        star, purge = monkey.first("takes.star.5"), monkey.first("takes.purge")
        assert star is not None and purge is not None

        def star_then_confirm() -> None:
            UserInteraction(user, {star}, None).click()
            UserInteraction(user, {purge}, None).click()

        core.scheduler.advance(0.6)
        await monkey.act("burst", "star Take 5, then confirm Remove Unstarred", star_then_confirm)
        assert purged.is_set() and core.takes.get(6) is None
        take = core.takes.get(5)
        assert take is not None and take.starred, (
            f"Take 5 was starred before the purge was confirmed, yet it is "
            f"{'gone' if take is None else 'unstarred'}; the last toast: {core.events.last_toast}")
    finally:
        monkey.inv.close()


@pytest.mark.scenario("forge_replace_other_backend")
async def test_bursts_on_replace_do_not_bake_or_load(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                     tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, "replace bursts")
    try:
        await open_monkey(user, core, fake, open_route, "/forge", monkey.inv)
        forge = monkey.first("forge.go")
        assert forge is not None and "Turbo" in forge.label
        await monkey.burst(forge)
        assert forge.label == "Click Again to Replace" and not fake.loads
        monkey.clock.advance(0.3)
        await monkey.burst(forge)
        assert not fake.loads and not any(call.name == "load_voice" for call in fake.calls)
        monkey.clock.advance(0.6)
        await monkey.click(forge)
        assert fake.loads == ["chatterbox-turbo"] and any(call.name == "load_voice" for call in fake.calls)
    finally:
        monkey.inv.close()


# ---- the exhaustive probes (soak) -----------------------------------------------------------------------

TIME_SCENARIOS = ("speak", "firstrun", "generating", "queued", "lowvram", "switch", "failure", "freeing", "loading",
                  "relaunch", "forging", "forged", "cpu", "takes", "settings")


def _probe(user: User, core: Core, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch, name: str) -> Monkey:
    inv = Invariants(user, core, fake, monkeypatch)
    return Monkey(user, core, fake, inv, Trace(f"R1 probe {name}"), rng=random.Random(name), faults=False)


@pytest.mark.soak
@pytest.mark.parametrize("scenario", [pytest.param(s, marks=pytest.mark.scenario(s), id=s) for s in TIME_SCENARIOS])
async def test_time_passing_loads_and_frees_nothing(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    scenario: str) -> None:
    """Two hours on every route (idle unload off): tickers, the device poller, arms and toasts run; no model work."""
    core.settings.set("core", "idle_unload_min", 0)
    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, f"time {scenario}")
    try:
        for route in ROUTES:
            await open_monkey(user, core, fake, open_route, route, monkey.inv)
            fake.reset_records()
            monkey.inv.baseline()
            for _ in range(24):
                await monkey.act("tick", "+300 s", lambda: monkey.clock.advance(300))
            work = [call.name for call in fake.calls if call.name in MODEL_WORK]
            assert work == [] and fake.loads == [], f"{route}: {work}"
    finally:
        monkey.inv.close()


SEARCHES = ("aiko", "marco", "host", "Take", "おはよう", "Forge", "Free", "zzz")


@pytest.mark.soak
@pytest.mark.parametrize("scenario", [pytest.param(s, marks=pytest.mark.scenario(s), id=s)
                                      for s in ("speak", "firstrun", "generating", "lowvram", "failure")])
async def test_search_jumps_load_nothing(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    """Every result of several queries, each run from a fresh Voices page: a jump loads nothing; Free VRAM only
    frees."""
    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, f"search {scenario}")
    ran = 0
    try:
        for query in SEARCHES:
            index = 0
            while True:
                await open_monkey(user, core, fake, open_route, "/voices", monkey.inv)
                box = monkey.first(EMIYA["search"])
                assert box is not None
                await monkey.set_text(box, query)
                results = sorted(monkey.shown(EMIYA["result"]), key=lambda element: element.id)
                if index >= len(results):
                    break
                fake.reset_records()
                monkey.inv.baseline()
                pick = results[index]
                label = " ".join(" ".join(map(str, child._displayed_contents(only_visible=False)))
                                 for child in pick.descendants())
                await monkey.act("search", f"{query!r} #{index}",
                                 lambda: UserInteraction(user, {pick}, None).trigger("mousedown"), names=label)
                work = [call.name for call in fake.calls if call.name in MODEL_WORK]
                assert fake.loads == [], f"{query!r} -> {label!r} loaded {fake.loads}"
                assert not work or ("Free VRAM" in label and set(work) <= {"unload_backend"}), f"{label!r}: {work}"
                ran += 1
                index += 1
    finally:
        monkey.inv.close()
    print(f"\n[{scenario}] ran {ran} search results")
    assert ran


NASTY = ("(.*[", "a" * 5000, "😀 émoji ✓", "مرحبا بالعالم", "\x00\x07\t\n", "con", "LPT9", "../../etc/passwd",
         "日本語のテキストです。", "  ", "<script>alert(1)</script>", "Ctrl+Enter", "%s %d {0}", "\\\\server\\share")


@pytest.mark.soak
@pytest.mark.parametrize("scenario,route", [
    pytest.param(s, r, marks=pytest.mark.scenario(s), id=f"{s}-{PAGE_ID[r]}")
    for s in ("speak", "forge", "firstrun", "takes", "generating") for r in ("/", "/voices", "/forge", "/takes")])
async def test_odd_text_in_every_box(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch, scenario: str, route: str) -> None:
    """Every text box of the page takes odd text (very long, control characters, RTL, emoji, paths) without an
    error, a freeze or a load; then each box is cleared again."""
    install_kit(monkeypatch, tmp_path)
    monkey = _probe(user, core, fake, monkeypatch, f"odd text {scenario} {route}")
    try:
        await open_monkey(user, core, fake, open_route, route, monkey.inv)
        boxes = [seen.element for seen in scan(user.current_layout)
                 if isinstance(seen.element, (ui.input, ui.textarea)) and is_enabled(seen.element)]
        assert boxes
        for box in boxes:
            for text in NASTY:
                if box.is_deleted:
                    break
                await monkey.set_text(box, text)
                await monkey.act("tick", "+1 s", lambda: monkey.clock.advance(1.0))   # the Takes search waits 150 ms
            if not box.is_deleted:
                await monkey.set_text(box, "", mode="clear")
        await quiesce(core, fake)
        assert fake.loads == [] and len(fake.residency().resident) <= 1
    finally:
        monkey.inv.close()
