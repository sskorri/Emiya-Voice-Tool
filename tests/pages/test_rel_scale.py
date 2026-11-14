"""Scale, stress and performance of the pages (R3), under the NiceGUI user fixture on FakeEngine data roots.

Default suite:
- paging at the real limits: the Takes list shows 200 rows and "Show N more" on every tab; Speak's "Takes · This
  Session" holds to the same limit (R3-1, fixed); rows that stay listed are kept, not rebuilt, when a take starts
  or lands on Speak (R3-2), on "Show N more" (R3-3) and on a Voices pick (R3-4), all fixed;
- stress: 200 rapid Generate commands and clicks (the queue holds at 3, the rest are refused with the documented
  copy, every accepted take is saved), 500 rapid route changes, 100 switch requests while a switch runs (all
  refused, one job), and 1,000 drags of Settings › Effect Speed (the last value is the one saved).
-m bench (tests\\reliability\\bench.py runs these and merges their numbers through VOICETOOL_BENCH_OUT): every
page's first render and re-render, the controllers' refreshes and the UI paths of search, Takes tabs and filters,
Voices selection, a star on Speak and the progress tick, on a data root grown to 300 voices, 3,000 takes, 1,000
load-history events, 64-character names and 10,000-character scripts.

Every stress test logs its seed and its action trace when it fails. Times are measured with perf_counter around
the page builders (wrapped in place) and the user's actions; the manual clock drives every app timer.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib
import json
import os
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from nicegui import background_tasks, binding
from nicegui.element import Element
from nicegui.testing import User

from ui.contract.routes import PAGE_ID, ROUTES, TITLES, Route
from ui.core import Core
from ui.core import fake as fake_module
from ui.core.clock import ManualScheduler
from ui.core.facade import BUSY
from ui.core.fake import FakeEngine
from ui.core.jobs import MAX_QUEUE, QUEUE_FULL

from tests.pages.test_wiring import behaviours_of, install_kit, settle, the, until
from tests.reliability import bench
from tests.reliability.bench import (BUDGET_OP_S, BUDGET_PAGE_S, BUDGET_SEARCH_S, OUT_ENV, TARGET_PAGE_S, LoopLag,
                                     Scale)

MTL, STD, TUR = bench.MTL, bench.STD, bench.TUR
PAGE_ROWS = 200                                  # the Takes list's page (S03 risk 14)
PAGING = Scale("paging", voices=9, takes=520, history=20, session=260, starred=230, long_name_every=3,
               long_script_every=50)
SESSION = Scale("session", voices=9, takes=470, history=20, session=450, starred=20, long_name_every=3,
                long_script_every=50)
BENCH_SCALE = bench.SCALES.get(os.environ.get(bench.SCALE_ENV, "full"), bench.SCALES["full"])
BENCH_REPEAT = max(1, int(os.environ.get(bench.REPEAT_ENV, "5") or 5))
NAV_TASK = "navigate_sub_pages"
ROW_MARKERS: dict[Route, str] = {"/voices": "voices.row.", "/takes": "takes.row."}


# ---------- fixtures and helpers ----------

@dataclass
class GrownBox:
    """The scale a test grows its data root to, and what grow() wrote once the user fixture seeded it."""

    scale: Scale
    grown: bench.Grown | None = None


@pytest.fixture
def grown(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> GrownBox:
    """Grow the scenario's seed to a Scale (indirect param, else the bench scale). List it before `user`."""
    box = GrownBox(getattr(request, "param", None) or BENCH_SCALE)
    original = fake_module._seed

    def seed(scenario: Any, config: Any) -> str | None:
        run_id = original(scenario, config)
        box.grown = bench.grow(Path(config.ROOT), box.scale, session_id=run_id)
        return run_id

    monkeypatch.setattr(fake_module, "_seed", seed)
    return box


class BuildTimer:
    """Wraps every page module's build(ctx) and the theme's build_shell() to time each build in place."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.builds: list[tuple[str, float]] = []
        for route in ROUTES:
            module = importlib.import_module(f"ui.themes.emiya.pages.{PAGE_ID[route]}")
            monkeypatch.setattr(module, "build", self._timed(route, module.build))
        theme = importlib.import_module("ui.themes.emiya")
        monkeypatch.setattr(theme, "build_shell", self._timed("shell", theme.build_shell))

    def _timed(self, key: str, build: Callable[..., Any]) -> Callable[..., Any]:
        def timed(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return build(*args, **kwargs)
            finally:
                self.builds.append((key, time.perf_counter() - started))
        return timed

    def last(self, key: str) -> float:
        return next(seconds for name, seconds in reversed(self.builds) if name == key)


@pytest.fixture
def timer(monkeypatch: pytest.MonkeyPatch) -> BuildTimer:
    return BuildTimer(monkeypatch)


class BenchLog:
    """Bench numbers: appended as JSON lines to VOICETOOL_BENCH_OUT (bench.py merges them); budgets checked.

    A test marked xfail(reason=<bug id>) tags its numbers with that id, so the report shows which bug a broken
    budget belongs to.
    """

    def __init__(self, path: str | None, test: str, bug: str = "") -> None:
        self.path = path
        self.test = test
        self.bug = bug
        self.breaches: list[str] = []

    def metric(self, name: str, value: float, *, group: str, budget: float | None = BUDGET_OP_S,
               target: float | None = None, unit: str = "s", note: str = "") -> None:
        self._write({"metric": name, "value": value, "group": group, "unit": unit, "budget": budget,
                     "target": target, "note": note, "bug": self.bug, "test": self.test})
        if budget is not None and value > budget:
            self.breaches.append(f"{name} = {value:.3f} {unit} > {budget} {unit}")

    def count(self, name: str, value: Any) -> None:
        self._write({"count": name, "value": value, "test": self.test})

    def check(self) -> None:
        assert not self.breaches, "budgets broken: " + "; ".join(self.breaches)

    def _write(self, item: dict[str, Any]) -> None:
        if self.path:
            with open(self.path, "a", encoding="utf-8") as sink:
                sink.write(json.dumps(item, ensure_ascii=False) + "\n")


@pytest.fixture
def record(request: pytest.FixtureRequest) -> BenchLog:
    known = request.node.get_closest_marker("xfail")
    applies = known is not None and (not known.args or bool(known.args[0]))
    return BenchLog(os.environ.get(OUT_ENV) or None, request.node.name,
                    str(known.kwargs.get("reason", "")) if applies else "")


@dataclass
class Trace:
    """A stress test's seed and actions, printed when it fails (pytest shows the captured output)."""

    name: str
    seed: int
    steps: list[str] = field(default_factory=list)

    def add(self, step: str) -> None:
        self.steps.append(step)

    def dump(self, last: int = 80) -> str:
        shown = self.steps[-last:]
        return (f"[{self.name}] seed={self.seed}, {len(self.steps)} actions; the last {len(shown)}:\n  "
                + "\n  ".join(shown))


@contextlib.contextmanager
def traced(name: str, seed: int) -> Iterator[Trace]:
    trace = Trace(name, seed)
    try:
        yield trace
    except BaseException:
        print(trace.dump())
        raise


def marked(user: User, prefix: str) -> list[Element]:
    """Every element (shown or not) with a marker that starts with prefix."""
    return [element for element in user.current_layout.descendants()
            if any(marker.startswith(prefix) for marker in element._markers)]


def row_ids(user: User, prefix: str) -> list[int]:
    """The take ids of the rows marked <prefix><id>, in page order."""
    ids = []
    for element in user.current_layout.descendants():
        for marker in element._markers:
            if marker.startswith(prefix) and marker[len(prefix):].isdigit():
                ids.append(int(marker[len(prefix):]))
    return ids


def page_element(user: User, route: Route) -> Element:
    marker = f"page.{PAGE_ID[route]}"
    return next(element for element in user.current_layout.descendants() if marker in element._markers)


def page_size(user: User, route: Route) -> tuple[int, int]:
    """(elements under the page wrapper, bytes of their JSON as NiceGUI sends it to the browser)."""
    elements = list(page_element(user, route).descendants())
    size = 0
    for element in elements:
        with contextlib.suppress(Exception):
            size += len(json.dumps(element._to_dict(), default=str))
    return len(elements), size


async def navigations_done(timeout: float = 120.0) -> None:
    """Wait (real time) until no sub-page navigation task is left."""
    deadline = time.perf_counter() + timeout
    while any(task.get_name() == NAV_TASK and not task.done() for task in background_tasks.running_tasks):
        assert time.perf_counter() < deadline, "sub-page navigations did not finish"
        await asyncio.sleep(0.01)


async def take_cycle(user: User, core: Core, fake: FakeEngine, marker: str, count: int) -> tuple[float, float]:
    """Click a Generate control on Speak and wait until its takes are saved: (wall seconds, worst loop block)."""
    first = core.takes.next_id
    probe = LoopLag().start()
    began = time.perf_counter()
    user.find(marker=marker).click()
    await until(lambda: core.takes.next_id >= first + count and not core.state.job.kind, "the takes", timeout=60)
    await settle(core, fake)
    wall = time.perf_counter() - began
    return wall, await probe.stop()


async def visit(user: User, core: Core, fake: FakeEngine, route: Route) -> float:
    """Click the rail button of route like a user, wait for the navigation and its page; returns the wall time."""
    page = PAGE_ID[route]
    started = time.perf_counter()
    user.find(marker=f"nav.{page}").click()
    await navigations_done()
    await settle(core, fake)
    elapsed = time.perf_counter() - started
    await user.should_see(marker=f"page.{page}")
    return elapsed


# ---------- paging at scale (default suite) ----------

@pytest.mark.parametrize("grown", [PAGING], ids=["paging"], indirect=True)
async def test_takes_list_pages_at_200_rows_on_every_tab(grown: GrownBox, user: User, core: Core, fake: FakeEngine,
                                                         open_route: Any) -> None:
    """S03 risk 14: 520 takes (260 this session, 230 starred) never render more than 200 rows per page."""
    await open_route(user, "/takes")
    await settle(core, fake)
    assert grown.grown is not None and grown.grown.takes == 520
    expected = {"all": 520, "session": grown.grown.session, "starred": grown.grown.starred}
    for tab in ("all", "starred", "session"):
        user.find(marker=f"takes.tab.{tab}").click()
        await settle(core, fake)
        total = expected[tab]
        shown = row_ids(user, "takes.row.")
        assert len(shown) == PAGE_ROWS, f"{tab}: {len(shown)} rows"
        assert shown == sorted(shown, reverse=True)
        while total > len(shown):
            await user.should_see(f"Show {min(PAGE_ROWS, total - len(shown))} more")
            user.find(marker="takes.more").click()
            await settle(core, fake)
            grown_to = row_ids(user, "takes.row.")
            assert len(grown_to) == min(total, len(shown) + PAGE_ROWS), f"{tab}: Show more gave {len(grown_to)}"
            assert grown_to[:len(shown)] == shown, "Show more rebuilt or reordered the rows already shown"
            shown = grown_to
        await user.should_not_see(marker="takes.more")
    user.find(marker="takes.tab.all").click()                       # a tab change starts again at one page
    await settle(core, fake)
    assert len(row_ids(user, "takes.row.")) == PAGE_ROWS


@pytest.mark.parametrize("grown", [SESSION], ids=["session450"], indirect=True)
async def test_speak_session_list_holds_to_one_page_of_rows(grown: GrownBox, timer: BuildTimer, user: User,
                                                            core: Core, fake: FakeEngine, open_route: Any) -> None:
    """R3-1 (fixed): Speak's "Takes · This Session" (a 186 px box) gets the Takes list's limit: with 450 takes this
    session it renders the newest 200 rows, and "All Takes ›" leads to the rest. It used to render every take."""
    await open_route(user, "/")
    await settle(core, fake)
    assert grown.grown is not None and grown.grown.session == 450
    rows = row_ids(user, "speak.session.row.")
    elements, size = page_size(user, "/")
    assert len(rows) <= PAGE_ROWS, (f"Speak renders {len(rows)} session rows ({elements} elements, {size:,} bytes "
                                    f"of element JSON, build {timer.last('/') * 1000:.0f} ms)")
    newest = sorted((take.id for take in core.takes.takes if take.session == core.session_id), reverse=True)
    assert rows == newest[:PAGE_ROWS], "the session list must show the newest takes, newest first"


@pytest.mark.scenario("speak")
async def test_a_10000_character_script_is_preserved_and_can_be_shortened_after_refusal(
        user: User, core: Core, fake: FakeEngine, open_route: Any, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    """Keep the complete oversized draft. A shorter input can still hit the separate 40-second output limit."""
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    script = bench.long_script()
    assert len(script) == bench.LONG_SCRIPT
    with user.client:
        next(iter(user.find(marker="speak.text").elements)).set_value(script)
    await settle(core, fake)
    assert core.session.text == script
    assert the(user, "speak.stats").text.startswith(f"{bench.LONG_SCRIPT} chars · ")
    first = core.takes.next_id
    user.find(marker="speak.generate").click()
    await settle(core, fake)
    assert core.takes.next_id == first and core.session.text == script
    assert "Script too long" in the(user, "shell.toast").text
    script = script[:1000]
    with user.client:
        next(iter(user.find(marker="speak.text").elements)).set_value(script)
    await settle(core, fake)
    user.find(marker="speak.generate").click()
    await until(lambda: core.takes.next_id > first and not core.state.job.kind, "the take", timeout=30)
    await settle(core, fake)
    take = core.takes.get(first)
    assert take is not None and take.text == script and take.hit_token_limit
    assert any(f"Take {first} hit the 40 s limit" in line for line in core.state.log.lines)
    await user.should_see(" · cut at 40 s")
    with user.client:
        next(iter(user.find(marker="shell.search").elements)).set_value(script[:40])
    await settle(core, fake)
    assert f"shell.search.result.take:{first}" in {m for e in marked(user, "shell.search.result.") for m in e._markers}
    assert fake.loads == []


def row_elements(user: User, prefix: str) -> dict[str, Element]:
    """The element of each row marked <prefix><key>, by key (the same object means the row was kept)."""
    found: dict[str, Element] = {}
    for element in user.current_layout.descendants():
        for marker in element._markers:
            if marker.startswith(prefix) and len(marker) > len(prefix):
                found[marker[len(prefix):]] = element
    return found


def rebuilt(before: dict[str, Element], after: dict[str, Element]) -> list[str]:
    """Keys whose row is still listed but is a new element (rebuilt rather than updated in place)."""
    return [key for key, element in before.items() if key in after and after[key] is not element]


@pytest.mark.scenario("speak")
async def test_speak_session_rows_are_kept_while_a_take_starts_and_lands(user: User, core: Core, fake: FakeEngine,
                                                                         open_route: Any,
                                                                         monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    """R3-2 (fixed): a take starting shows the "Generating…" row and a take landing adds its own row; the rows
    already listed stay (updated in place, as the Takes list keeps its rows). Each start and each landing used to
    rebuild every row, three times per take."""
    from ui.themes.emiya.pages import speak as speak_page

    clears: list[str] = []
    original = speak_page._SessionList.clear

    def counted(self: Any) -> Any:
        clears.append(core.state.job.kind or "idle")
        return original(self)

    monkeypatch.setattr(speak_page._SessionList, "clear", counted)
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    before = row_elements(user, "speak.session.row.")
    assert len(before) == 4
    clears.clear()
    hold = fake.hold("speak")
    user.find(marker="speak.generate").click()
    await until(lambda: hold.entered.is_set(), "the take's hold")
    await settle(core, fake)
    started = rebuilt(before, row_elements(user, "speak.session.row."))
    hold.release()
    await until(lambda: not core.state.job.kind, "the take", timeout=30)
    await settle(core, fake)
    after = row_elements(user, "speak.session.row.")
    landed = rebuilt(before, after)
    assert started == [] and landed == [], (f"rebuilt at the start: {started}; when the take landed: {landed}; "
                                            f"the whole list was rebuilt {len(clears)} times for one take")
    assert clears == [] and set(after) == set(before) | {"8"}, (clears, sorted(after))
    assert row_ids(user, "speak.session.row.")[0] == 8                  # the new take's row is on top


@pytest.mark.parametrize("grown", [PAGING], ids=["paging"], indirect=True)
async def test_takes_show_more_keeps_the_rows_already_shown(grown: GrownBox, user: User, core: Core,
                                                            fake: FakeEngine, open_route: Any) -> None:
    """R3-3 (fixed): "Show 200 more" appends the next rows; the 200 already shown stay as they are. Every click
    used to rebuild every shown row, so each click cost more than the last (the bench's curve.takes_show_more)."""
    await open_route(user, "/takes")
    user.find(marker="takes.tab.all").click()
    await settle(core, fake)
    before = row_elements(user, "takes.row.")
    assert len(before) == PAGE_ROWS
    user.find(marker="takes.more").click()
    await settle(core, fake)
    after = row_elements(user, "takes.row.")
    assert len(after) == 2 * PAGE_ROWS
    gone = rebuilt(before, after)
    assert gone == [], f"Show more rebuilt {len(gone)} of the {len(before)} rows already shown"
    assert row_ids(user, "takes.row.") == sorted(row_ids(user, "takes.row."), reverse=True)    # appended in order


@pytest.mark.parametrize("grown", [PAGING], ids=["paging"], indirect=True)
async def test_a_takes_revisit_starts_again_at_one_page(grown: GrownBox, user: User, core: Core, fake: FakeEngine,
                                                        open_route: Any) -> None:
    """After "Show N more" twice (all 520 takes shown), leaving Takes and coming back builds one page of rows (the tab
    stays), not all of them: at 3,000 rows shown a revisit used to take seconds (R3-3)."""
    await open_route(user, "/takes")
    user.find(marker="takes.tab.all").click()
    await settle(core, fake)
    for _ in range(2):
        user.find(marker="takes.more").click()
        await settle(core, fake)
    assert grown.grown is not None and len(row_ids(user, "takes.row.")) == grown.grown.takes == 520
    await visit(user, core, fake, "/")
    await visit(user, core, fake, "/takes")
    assert len(row_ids(user, "takes.row.")) == PAGE_ROWS and behaviours_of(user).takes().vm.tab == "all"


@pytest.mark.scenario("voices")
async def test_voices_selection_keeps_the_library_rows(user: User, core: Core, fake: FakeEngine,
                                                       open_route: Any) -> None:
    """R3-4 (fixed): picking a voice moves the selection; the Library's rows stay (updated in place). The whole
    list used to be rebuilt on every pick (300 voices: 2,878 elements and 0.37 MB of element JSON per click)."""
    await open_route(user, "/voices")
    await settle(core, fake)
    before = row_elements(user, "voices.row.")
    assert {"aiko", "marco", "narrator"} <= set(before)
    user.find(marker="voices.row.marco").click()
    await settle(core, fake)
    after = row_elements(user, "voices.row.")
    assert "on" in after["marco"].classes
    gone = rebuilt(before, after)
    assert gone == [], f"a pick rebuilt {len(gone)} of the {len(before)} Library rows"


# ---------- stress (default suite) ----------

@pytest.mark.scenario("speak")
async def test_stress_200_rapid_generate_commands(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                  monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ctrl+Enter pressed 200 times while take 8 runs: 3 queue, 196 are refused with "Queue full · 3 takes
    waiting", and once the take is let go exactly takes 8-11 are saved, with the draft they were queued with."""
    seed = 200
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    toasts: list[Any] = []
    core.events.toast.subscribe(toasts.append)
    first = core.takes.next_id
    before = {take.id for take in core.takes.takes}
    hold = fake.hold("speak")
    keys = user.find(marker="shell.keys")
    with traced("200 generate commands", seed) as trace:
        for press in range(200):
            keys.trigger("command", {"id": "gen", "combo": "Ctrl+Enter"})
            trace.add(f"press {press + 1}: job={core.state.job.kind or '-'} take={core.state.job.take_no} "
                      f"queued={len(core.jobs.queued)}")
            if press % 10 == 9:
                await asyncio.sleep(0)                               # a few handlers run between the presses
            assert len(core.jobs.queued) <= MAX_QUEUE, "the queue grew past 3"
        await until(lambda: hold.entered.is_set(), "take 8 to reach its hold")
        await settle(core, fake)
        assert (core.state.job.kind, core.state.job.take_no) == ("generate", first)
        assert len(core.jobs.queued) == MAX_QUEUE and core.state.job.queued == MAX_QUEUE
        refused = [toast for toast in toasts if toast.bad]
        assert [toast.text for toast in refused] == [QUEUE_FULL] * (200 - 1 - MAX_QUEUE), \
            sorted({toast.text for toast in toasts})
        hold.release()
        await until(lambda: not core.state.job.kind, "the queue to drain", timeout=30)
        await settle(core, fake)
        new = sorted(take.id for take in core.takes.takes if take.id not in before)
        assert new == list(range(first, first + 1 + MAX_QUEUE)), f"saved takes {new}"
        texts = {core.takes.get(take_id).text for take_id in new}
        assert texts == {core.session.text} and fake.loads == []
        assert core.jobs.queued == () and core.state.job.queued == 0
        await user.should_see(marker=f"speak.session.row.{first + MAX_QUEUE}")


@pytest.mark.scenario("speak")
async def test_stress_200_rapid_generate_clicks(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Generate clicked 200 times in bursts of 25, the first burst before the page can disable it: take 8 runs, 3
    queue, the other 21 clicks of that burst are refused with "Queue full · 3 takes waiting", every later click
    lands on the disabled button; nothing is lost."""
    seed, burst = 201, 25
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    toasts: list[Any] = []
    core.events.toast.subscribe(toasts.append)
    first = core.takes.next_id
    before = {take.id for take in core.takes.takes}
    hold = fake.hold("speak")
    generate = the(user, "speak.generate")
    with traced("200 generate clicks", seed) as trace:
        for click in range(200):
            user.find(marker="speak.generate").click()
            trace.add(f"click {click + 1}: enabled={generate.enabled} label={generate.props.get('aria-label', '')!r} "
                      f"queued={len(core.jobs.queued)}")
            if click % burst == burst - 1:
                await asyncio.sleep(0)                               # the burst's handlers run
        await until(lambda: hold.entered.is_set(), "take 8 to reach its hold")
        await settle(core, fake)
        queued = len(core.jobs.queued)
        refused = [toast.text for toast in toasts if toast.bad]
        assert queued == MAX_QUEUE and refused == [QUEUE_FULL] * (burst - 1 - MAX_QUEUE), (queued, refused)
        assert not generate.enabled, "Generate stayed enabled while a take runs"
        hold.release()
        await until(lambda: not core.state.job.kind, "the queue to drain", timeout=30)
        await settle(core, fake)
        new = sorted(take.id for take in core.takes.takes if take.id not in before)
        assert new == list(range(first, first + 1 + queued)), f"saved takes {new}"
        assert fake.loads == [] and len(fake.residency().resident) == 1


@pytest.mark.scenario("speak")
async def test_stress_500_rapid_route_changes(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """500 rail clicks in a seeded order, many before the previous page is built: the last route wins, one page
    shows, the title and the nav follow it, nothing loads, no binding is left polling and nothing piles up."""
    seed = 500
    rng = random.Random(seed)
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    for route in (*ROUTES, "/"):                                    # every page built once, then the baseline
        await visit(user, core, fake, route)
    baseline = _footprint(user)
    clients = len(user.client.instances) if user.client is not None else 0
    fake.reset_records()
    order = [rng.choice(ROUTES) for _ in range(500)]
    started = time.perf_counter()
    with traced("500 route changes", seed) as trace:
        for index, route in enumerate(order):
            user.find(marker=f"nav.{PAGE_ID[route]}").click()
            trace.add(f"{index + 1}: {route}")
            if index % rng.choice((3, 7, 11)) == 0:
                await asyncio.sleep(0)                               # some builds run between the clicks
        await navigations_done()
        await settle(core, fake)
        final = order[-1]
        shown = [route for route in ROUTES if any(f"page.{PAGE_ID[route]}" in element._markers
                                                   for element in user.current_layout.descendants())]
        assert shown == [final], f"pages in the layout after the storm: {shown}"
        status = behaviours_of(user).status()
        assert status.vm.route == final and status.vm.title == TITLES[final]
        await user.should_see(marker=f"page.{PAGE_ID[final]}")
        await visit(user, core, fake, "/")                         # back to the baseline page
        assert binding.active_links == []
        assert fake.loads == [] and [c.name for c in fake.calls if c.name in ("make_resident", "unload_backend")] == []
        assert _footprint(user) == baseline, "elements or bindings piled up over the storm"
        assert (len(user.client.instances) if user.client is not None else 0) == clients
    print(f"\n500 route changes in {time.perf_counter() - started:.1f} s")


def _footprint(user: User) -> tuple[int, int, int]:
    """(elements in the layout, bindable properties, bindings): the same page must give the same numbers."""
    gc.collect()
    return (len(list(user.current_layout.descendants())), len(binding.bindable_properties),
            sum(len(links) for links in binding.bindings.values()))


@pytest.mark.scenario("loading")
async def test_stress_100_switch_requests_while_a_switch_runs(user: User, core: Core, fake: FakeEngine,
                                                              open_route: Any, monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    """The Turbo switch is held mid-load; 100 switch and free requests from every path (dock rows, Models cards,
    the controller, named tokens straight to the job runner) are all refused, and only that one job ever runs."""
    seed = 100
    rng = random.Random(seed)
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/models")
    await settle(core, fake)
    job = core.state.job
    assert (job.kind, job.target) == ("switch", TUR)
    task = core.jobs.task
    behaviours = behaviours_of(user)
    models = behaviours.models()
    fake.reset_records()
    refused: list[str] = []
    paths = ("dock", "card", "request", "token", "free")
    with traced("100 switch requests", seed) as trace:
        for request in range(100):
            backend = rng.choice((MTL, STD, TUR))
            path = paths[request % len(paths)]
            if path == "dock":
                user.find(marker=f"dock.row.{backend}").click()
            elif path == "card":
                with contextlib.suppress(AssertionError):           # the resident card has no switch button
                    user.find(marker=f"models.switch.{backend}").click()
            elif path == "request":
                assert models.request_switch(backend, origin=rng.choice(("dock", "card", "retry", "default"))) is None
            elif path == "token":
                plan = core.planner.switch_plan(backend, reason="explicit")
                result = core.jobs.start_switch(core.tokens.mint(plan, asked=True))
                assert (result.status, result.reason) == ("refused", BUSY), result
                refused.append(result.reason)
            else:
                result = core.jobs.start_free()
                assert (result.status, result.reason) == ("refused", BUSY), result
                refused.append(result.reason)
            trace.add(f"{request + 1}: {path} {backend} -> job={core.state.job.kind}:{core.state.job.target}")
            if request % 9 == 8:
                await asyncio.sleep(0)
            assert core.jobs.task is task and (core.state.job.kind, core.state.job.target) == ("switch", TUR)
        await settle(core, fake)
        assert fake.loads == [] and core.jobs.task is task
        assert len(refused) == 40 and core.state.engine.resident in ("", MTL)
        fake.release_all()
        await until(lambda: not core.state.job.kind, "the held switch to finish", timeout=30)
        await settle(core, fake)
        assert core.state.engine.resident == TUR and fake.residency().resident == (TUR,)
        assert [call.name for call in fake.calls if call.name == "make_resident"] == []   # no second load
        assert core.state.job.kind == "" and core.jobs.queued == ()


@pytest.mark.scenario("settings")
async def test_stress_1000_effect_speed_drags(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings › Effect Speed dragged 1,000 times (each drag a few live moves, then the release): no error, the
    label and the store follow the last release, and ui_settings.json ends with it after a handful of writes."""
    from ui.core.services import settings as settings_module

    seed = 1000
    rng = random.Random(seed)
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/settings")
    await settle(core, fake)
    user.find(marker="settings.tab.effects").click()
    await settle(core, fake)
    writes: list[float] = []
    original = settings_module.write_atomic

    def counted(path: Path, text: str) -> None:
        writes.append(time.perf_counter())
        original(path, text)

    monkeypatch.setattr(settings_module, "write_atomic", counted)
    slider = user.find(marker="settings.fx_speed")
    grid = list(range(25, 201, 5))
    last = 100
    started = time.perf_counter()
    with traced("1000 drags", seed) as trace:
        for drag in range(1000):
            path = [rng.choice(grid) for _ in range(rng.randint(1, 5))]
            for value in path[:-1]:
                slider.trigger("input", float(value))
            slider.trigger("change", float(path[-1]))
            last = path[-1]
            trace.add(f"drag {drag + 1}: {path}")
            if drag % 50 == 49:
                await asyncio.sleep(0)
        await settle(core, fake)

        def saved() -> Any:
            try:
                return json.loads(core.paths.settings.read_text(encoding="utf-8"))["theme.emiya"]["fx_speed"]
            except (OSError, ValueError, KeyError):
                return None

        await until(lambda: saved() == last, "ui_settings.json to hold the last release", timeout=30)
        assert core.settings.get("theme.emiya", "fx_speed") == last
        assert behaviours_of(user).settings().vm.fx_speed_text == f"{last}%"
        assert 1 <= len(writes) <= 100, f"{len(writes)} writes of ui_settings.json for 1,000 drags"
    print(f"\n1,000 drags in {time.perf_counter() - started:.1f} s, {len(writes)} writes")


# ---------- the bench (-m bench) ----------

@pytest.mark.bench
@pytest.mark.parametrize("repeat", range(BENCH_REPEAT))
async def test_bench_core(record: BenchLog, tmp_path: Path, repeat: int) -> None:
    """The core part at the bench scale, no client (bench.core_repeat): start, loads, search, Takes filters, star,
    Remove Unstarred through an injected trash, refreshes, the progress tick and memory."""
    rec = bench.Recorder()
    await bench.core_repeat(rec, tmp_path, BENCH_SCALE, repeat)
    for metric in rec.metrics.values():
        for value in metric.samples:
            record.metric(metric.name, value, group=metric.group, budget=metric.budget, target=metric.target,
                          unit=metric.unit, note=metric.note)
    for name, value in rec.counts.items():
        record.count(name, value)
    record.check()


@pytest.mark.bench
@pytest.mark.parametrize("repeat", range(BENCH_REPEAT))
async def test_bench_page_renders(grown: GrownBox, timer: BuildTimer, user: User, core: Core, fake: FakeEngine,
                                  clock: ManualScheduler, record: BenchLog, monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path, repeat: int) -> None:
    """Every page's first render (a fresh app: "/" through a full page load, the others on their first visit) and
    re-render (a later visit), the controllers' refreshes and the page sizes, at the bench scale."""
    install_kit(monkeypatch, tmp_path)
    gc.collect()
    rss = bench.rss_mb()
    started = time.perf_counter()
    await user.open("/")
    await user.should_see(marker="page.speak")
    await settle(core, fake)
    record.metric("ui.open_root", time.perf_counter() - started, group="ui", budget=BUDGET_OP_S)
    speak = timer.last("/")
    record.metric("page.first.speak", speak, group="pages", budget=BUDGET_PAGE_S, target=TARGET_PAGE_S)
    record.metric("page.shell", timer.last("shell") - speak, group="pages", budget=BUDGET_PAGE_S,
                  target=TARGET_PAGE_S)
    sizes: dict[str, tuple[int, int]] = {"speak": page_size(user, "/")}
    if repeat == 0:
        record.count("rows.speak.session", len(row_ids(user, "speak.session.row.")))
        record.count("rows.speak.voices", len(marked(user, "speak.voice.")))
    for route in ROUTES[1:]:
        wall = await visit(user, core, fake, route)
        page = PAGE_ID[route]
        record.metric(f"page.first.{page}", timer.last(route), group="pages", budget=BUDGET_PAGE_S,
                      target=TARGET_PAGE_S)
        record.metric(f"ui.nav_first.{page}", wall, group="ui")
        sizes[page] = page_size(user, route)
        if repeat == 0 and route in ROW_MARKERS:
            record.count(f"rows.{page}", len(marked(user, ROW_MARKERS[route])))
        if repeat == 0 and route == "/models":
            lines = marked(user, "models.history.lines")
            record.count("rows.models.history", len(list(lines[0].default_slot.children)) if lines else 0)
    for route in ROUTES:
        wall = await visit(user, core, fake, route)
        page = PAGE_ID[route]
        record.metric(f"page.rerender.{page}", timer.last(route), group="pages", budget=BUDGET_PAGE_S,
                      target=TARGET_PAGE_S)
        record.metric(f"ui.nav_again.{page}", wall, group="ui")
    behaviours = behaviours_of(user)
    for name in ("status", "models", "playback", "speak", "voices", "forge", "takes", "settings"):
        refresh = getattr(getattr(behaviours, name)(), "refresh", None)
        if callable(refresh):
            began = time.perf_counter()
            with user.client:
                refresh()
            await settle(core, fake)
            record.metric(f"refresh.ui.{name}", time.perf_counter() - began, group="refresh", budget=1.0)
    gc.collect()
    record.metric("memory.rss_growth_six_pages", bench.rss_mb() - rss, group="memory", unit="MB", budget=None,
                  note="working-set growth of the test process from before the first page to after all six")
    if repeat == 0:
        assert grown.grown is not None
        for page, (elements, size) in sizes.items():
            record.count(f"elements.{page}", elements)
            record.count(f"element_json_bytes.{page}", size)
        record.count("data.grown_files", grown.grown.files)
    record.check()


@pytest.mark.bench
@pytest.mark.parametrize("repeat", range(BENCH_REPEAT))
async def test_bench_ui_operations(grown: GrownBox, timer: BuildTimer, user: User, core: Core, fake: FakeEngine,
                                   clock: ManualScheduler, record: BenchLog, monkeypatch: pytest.MonkeyPatch,
                                   tmp_path: Path, repeat: int, open_route: Any) -> None:
    """The UI paths at the bench scale: the rail search, Takes tabs, search and filters, Show more, a Voices row
    pick, a star on Speak's session list, and the progress tick with Speak open while a take generates."""
    install_kit(monkeypatch, tmp_path)
    await open_route(user, "/")
    await settle(core, fake)
    assert grown.grown is not None
    box = user.find(marker="shell.search")
    for label, query in (("voice", grown.grown.voice_ids[-1]), ("take_text", "hundred years"),
                         ("no_match", "zzzz-no-such-thing"), ("long_name", grown.grown.long_voice or "bench")):
        began = time.perf_counter()
        with user.client:
            next(iter(box.elements)).set_value(query)
        await settle(core, fake)
        record.metric(f"ui.search.{label}", time.perf_counter() - began, group="search", budget=BUDGET_SEARCH_S)
        assert len(marked(user, "shell.search.result.")) <= 12
    with user.client:
        next(iter(box.elements)).set_value("")
    await settle(core, fake)

    # Speak: a star on a session row (the session list updates its rows in place; it lists the newest 200)
    session = row_ids(user, "speak.session.row.")
    assert len(session) == min(PAGE_ROWS, len(grown.grown.session_ids))
    if session:
        star = f"speak.session.star.{session[len(session) // 2]}"
        began = time.perf_counter()
        user.find(marker=star).click()
        await settle(core, fake)
        await until(lambda: not core.takes._lock.locked(), "the star's save")
        await settle(core, fake)
        record.metric("ui.speak.star", time.perf_counter() - began, group="ui")

    # the progress tick with Speak open: one manual 0.2 s step while a take generates
    hold = fake.hold("speak")
    user.find(marker="speak.generate").click()
    await until(lambda: hold.entered.is_set(), "the take's hold")
    await settle(core, fake)
    for _ in range(5):
        began = time.perf_counter()
        clock.advance(0.2)
        await settle(core, fake)
        record.metric("ui.tick.generating", time.perf_counter() - began, group="refresh", budget=0.2)
    hold.release()
    await until(lambda: not core.state.job.kind, "the take", timeout=30)
    await settle(core, fake)

    # Takes: tabs, search, filters and Show more through the page
    await visit(user, core, fake, "/takes")
    for tab in ("all", "starred", "session"):
        began = time.perf_counter()
        user.find(marker=f"takes.tab.{tab}").click()
        await settle(core, fake)
        record.metric(f"ui.takes.tab.{tab}", time.perf_counter() - began, group="takes")
    user.find(marker="takes.tab.all").click()
    await settle(core, fake)
    began = time.perf_counter()
    user.find(marker="takes.search").type("forge")
    clock.advance(0.15)
    await settle(core, fake)
    record.metric("ui.takes.search", time.perf_counter() - began, group="takes")
    with user.client:
        next(iter(user.find(marker="takes.search").elements)).set_value("")
    clock.advance(0.15)
    await settle(core, fake)
    for menu, choice in (("filter_voice", f"voice.{grown.grown.voice_ids[0]}"), ("filter_model", TUR)):
        user.find(marker=f"takes.{menu}").click()
        began = time.perf_counter()
        user.find(marker=f"takes.{menu}.{choice}").click()
        await settle(core, fake)
        record.metric(f"ui.takes.{menu}", time.perf_counter() - began, group="takes")
        user.find(marker=f"takes.{menu}").click()
        user.find(marker=f"takes.{menu}.all").click()
        await settle(core, fake)
    for _ in range(3):
        more = marked(user, "takes.more")
        if not more or not all(element.visible for element in (more[0], *more[0].ancestors())):
            break
        began = time.perf_counter()
        user.find(marker="takes.more").click()
        await settle(core, fake)
        record.metric("ui.takes.show_more", time.perf_counter() - began, group="takes")

    # Voices: picking rows (the library list follows the selection)
    await visit(user, core, fake, "/voices")
    ids = grown.grown.voice_ids
    for pick in (ids[1], ids[len(ids) // 2], ids[-1]):
        began = time.perf_counter()
        user.find(marker=f"voices.row.{pick}").click()
        await settle(core, fake)
        record.metric("ui.voices.select", time.perf_counter() - began, group="ui")
    record.check()


FREEZE_S = 2.0                                    # the longest the UI may stop answering during a take


@pytest.mark.bench
@pytest.mark.parametrize("repeat", range(BENCH_REPEAT))
async def test_bench_speak_take_cycles(grown: GrownBox, user: User, core: Core, fake: FakeEngine, record: BenchLog,
                                       open_route: Any, repeat: int) -> None:
    """Whole takes with Speak open at the bench scale: Generate, then Generate 3 Takes. The FakeEngine's takes are
    instant, so the time is the app's own; budget: under 5 s per click and no freeze over 2 s. The session list
    used to be rebuilt whenever a take started and landed, freezing the loop for seconds at 1,000 session takes
    (R3-2, fixed: its rows are kept, and it lists the newest 200)."""
    await open_route(user, "/")
    await settle(core, fake)
    for marker, count in (("speak.generate", 1), ("speak.generate3", 3)):
        wall, block = await take_cycle(user, core, fake, marker, count)
        name = marker.split(".")[-1]
        record.metric(f"ui.speak.{name}_cycle", wall, group="ui")
        record.metric(f"ui.speak.{name}_loop_block", block, group="ui", budget=FREEZE_S)
    record.check()


CURVE = (100, 300, 1000, 3000)


@pytest.mark.bench
@pytest.mark.parametrize("grown", [Scale(f"session{n}", voices=30, takes=max(n, 120), history=50, session=n,
                                         starred=10, long_name_every=5, long_script_every=100) for n in CURVE],
                         ids=[f"session{n}" for n in CURVE], indirect=True)
async def test_bench_speak_session_curve(grown: GrownBox, timer: BuildTimer, user: User, core: Core,
                                         fake: FakeEngine, record: BenchLog, monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    """Speak against the number of takes this session: its first render, its size, and a whole take with it open
    (the session list follows the take's start and its end). One run per size: a curve, not a repeat."""
    install_kit(monkeypatch, tmp_path)
    began = time.perf_counter()
    await user.open("/")
    await user.should_see(marker="page.speak")
    await settle(core, fake)
    size = grown.scale.session
    record.metric(f"curve.speak_open.{size}", time.perf_counter() - began, group="pages", budget=None)
    record.metric(f"curve.speak_build.{size}", timer.last("/"), group="pages", budget=None)
    elements, payload = page_size(user, "/")
    record.count(f"curve.speak_elements.{size}", elements)
    record.count(f"curve.speak_element_json_bytes.{size}", payload)
    wall, block = await take_cycle(user, core, fake, "speak.generate", 1)
    record.metric(f"curve.speak_take_loop_block.{size}", block, group="ui", budget=None)
    record.metric(f"curve.speak_take_cycle.{size}", wall, group="ui", budget=None)


@pytest.mark.bench
async def test_bench_takes_show_more_curve(grown: GrownBox, user: User, core: Core, fake: FakeEngine,
                                           record: BenchLog, open_route: Any) -> None:
    """Takes › All at the bench scale: "Show N more" clicked until every take shows; each click's time against the
    rows shown, a later visit of the page, and one more take arriving (budget: none of them over 5 s, no freeze
    over 2 s). Every click and every new take used to rebuild every shown row, and a revisit built them all again
    (R3-3, fixed: the rows are kept, and a revisit starts again at one page)."""
    await open_route(user, "/takes")
    user.find(marker="takes.tab.all").click()
    await settle(core, fake)
    assert grown.grown is not None
    shown = len(row_ids(user, "takes.row."))
    while shown < grown.grown.takes:
        began = time.perf_counter()
        user.find(marker="takes.more").click()
        await settle(core, fake)
        shown = len(row_ids(user, "takes.row."))
        record.metric(f"curve.takes_show_more.{shown}", time.perf_counter() - began, group="takes")
    await visit(user, core, fake, "/")
    record.metric(f"curve.takes_revisit.{shown}", await visit(user, core, fake, "/takes"), group="takes")
    # a take arriving while every row shows (a queued take finishing while the user browses Takes)
    first = core.takes.next_id
    probe = LoopLag().start()
    began = time.perf_counter()
    core.scheduler.spawn(behaviours_of(user).speak().generate(1), name="bench take")
    await until(lambda: core.takes.next_id > first and not core.state.job.kind, "the take", timeout=60)
    await settle(core, fake)
    record.metric(f"curve.takes_new_take.{shown}", time.perf_counter() - began, group="takes")
    record.metric(f"curve.takes_new_take_loop_block.{shown}", await probe.stop(), group="takes", budget=FREEZE_S)
    record.count("curve.takes_rows_at_end", len(row_ids(user, "takes.row.")))
    record.check()
