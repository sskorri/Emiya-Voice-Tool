"""Long-session soak and the event-loop freeze check (R1), both opt-in with -m soak.

test_long_session_soak: one client, at least 3,000 mixed actions over all six pages on the FakeEngine: generate,
Generate 3, switch, Free VRAM, forge and re-forge, star and unstar, pins, Remove Unstarred, delete a voice,
settings changes, search jumps, navigation, time passing and random monkey actions, every one checked against the
invariants of tests\\pages\\_rel_actions.py. The warm-up (WARMUP actions, then switches until the load history
holds the 500 events it keeps) ends in the baseline checkpoint; then every CHECK_EVERY actions and at the end a
checkpoint settles all work, expires the transient timers and normalises what the session keeps changing (no
takes, the seeded voices, the first script), so the measurements compare like with like, and checks:
- live Python memory (tracemalloc, after a full collection) at the end is within GROWTH_BUDGET of the baseline;
- each page has the same NiceGUI element count every time the user comes back to it in a lap, and the client's
  bindings, bindable properties, delete handlers and event subscriptions do not grow from lap to lap;
- timers, tasks, clients and tickets end no higher than at the baseline, worker threads too, and no thread
  outside the two worker pools ever appears;
- no *.part or *.tmp-* file is left in the data root, and the takes index matches the WAVs on disk.

test_event_loop_lag_during_engine_work: FakeEngine speed LAG_SPEED (real sleeps in the io_bound workers), an
asyncio.sleep drift sampler on the loop, and a load, an eviction, takes, a bake and an unload driven through the
UI, on the manual clock and on the real one (the job ticker, the device poller and the arms then run as in the
app); the largest lag during engine work stays under LAG_LIMIT_S. A sample a full (generation 2) garbage
collection fell into is reported apart: that pause stops the whole interpreter, whatever the app does (up to
127 ms measured here). Opening pages during a load and with a full load history is measured and reported, not
asserted (a page build is the loop's own work). On Python 3.11 under Windows
time.monotonic() ticks every 15.6 ms and a shorter asyncio.sleep returns at once, so the sampler sleeps 25 ms
and only the tail of its numbers (p95, p99, max) means anything.

Both print their numbers; REL_BENCH_DIR=<folder> also writes them there as JSON.
"""
from __future__ import annotations

import asyncio
import collections
import gc
import json
import os
import random
import statistics
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from nicegui import Client, Event, background_tasks, binding
from nicegui.testing import User

from ui.contract.routes import PAGE_ID, ROUTES
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine

from tests.pages._rel_actions import (EMIYA, SCRIPTS, Invariants, Monkey, Trace, disarm_faults, element_counts,
                                      key_of, leftovers, open_monkey, quiesce, summary, takes_consistency)
from tests.pages._rel_actions import clock_kind  # noqa: F401  (a fixture)
from tests.pages.test_rel_monkey import drop_files
from tests.pages.test_wiring import install_kit

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
SOAK_ACTIONS = int(os.environ.get("REL_SOAK_ACTIONS", "3000"))
WARMUP = 400
CHECK_EVERY = 400
GROWTH_BUDGET = 2 ** 20              # bytes of live Python memory the post-warm-up session may add (measured: -38 KiB)
POOLS = ("ThreadPoolExecutor-", "asyncio_")      # NiceGUI's io_bound pool and asyncio's default executor
LAG_SPEED = float(os.environ.get("REL_LAG_SPEED", "0.2"))
LAG_LIMIT_S = 0.1
LAG_PERIOD_S = 0.025             # above Windows' 15.6 ms monotonic tick: a shorter asyncio.sleep returns at once
TASK_WEIGHTS = {
    "generate": 13.0, "generate3": 5.0, "switch": 7.0, "free": 4.0, "forge": 6.0, "reforge": 3.0, "star": 6.0,
    "pin": 4.0, "purge": 2.0, "delete": 2.0, "settings": 8.0, "search": 8.0, "navigate": 10.0, "time": 7.0,
    "chaos": 15.0,
}
GOOD_SCRIPTS = tuple(text for text in SCRIPTS if text.strip() and len(text) < 200)


def bench_dir() -> Path | None:
    folder = os.environ.get("REL_BENCH_DIR")
    return Path(folder) if folder else None


def save_numbers(name: str, numbers: dict[str, Any]) -> None:
    folder = bench_dir()
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(numbers, indent=2, default=str), encoding="utf-8")


# ---- the long session's user --------------------------------------------------------------------------

class Session:
    """The long session's user: everyday tasks through the real controls, each UI step one checked action."""

    def __init__(self, monkey: Monkey, core: Core, rng: random.Random) -> None:
        self.m = monkey
        self.core = core
        self.rng = rng
        self.names = 0
        self.done: collections.Counter[str] = collections.Counter()

    async def task(self) -> None:
        name = self.rng.choices(list(TASK_WEIGHTS), weights=list(TASK_WEIGHTS.values()))[0]
        self.done[name] += 1
        await getattr(self, f"_{name}")()

    async def go(self, route: str) -> None:
        if self.m.route() != route:
            await self.m.click_marker(f"nav.{PAGE_ID[route]}")

    async def confirm(self, yes: float = 0.85) -> None:
        if self.m.first(EMIYA["primary"]) is not None:
            await self.m.click_marker(EMIYA["primary"] if self.rng.random() < yes else "confirm.ask.cancel")

    async def pick(self, prefix: str) -> bool:
        found = sorted(self.m.prefixed(prefix), key=key_of)
        if not found:
            return False
        await self.m.click(self.rng.choice(found))
        return True

    async def _generate(self, marker: str = "speak.generate") -> None:
        await self.go("/")
        if self.rng.random() < 0.4:
            await self.pick("speak.voice.")
        box = self.m.first("speak.text")
        if box is not None and (self.rng.random() < 0.3 or not getattr(box, "value", "")):
            await self.m.set_text(box, self.rng.choice(GOOD_SCRIPTS))
        await self.m.click_marker(marker)
        await self.confirm()

    async def _generate3(self) -> None:
        await self._generate("speak.generate3")

    async def _switch(self) -> None:
        if await self.pick("dock.row."):
            await self.confirm()

    async def _free(self) -> None:
        await self.m.click_marker("dock.free")

    async def _forge(self) -> None:
        await self.go("/forge")
        await self.m.click_marker("forge.another")
        if self.core.forge_draft.clip_state != "ready":
            await self.m.click_marker("forge.browse")
        box = self.m.first("forge.name")
        if box is not None:
            self.names += 1
            name = f"s{self.names}" if self.rng.random() < 0.8 else self.rng.choice(("aiko", "rin", "s1"))
            await self.m.set_text(box, name)
        if self.rng.random() < 0.5:
            await self.pick("forge.backend.")
        if not self.core.forge_draft.consent:
            await self.m.click_marker("forge.consent")
        await self._press_forge()

    async def _press_forge(self) -> None:
        if await self.m.click_marker("forge.go") is None:
            return
        go = self.m.first("forge.go")
        if go is not None and "Click Again" in str(getattr(go, "label", "")):
            self.m.clock.advance(0.6)
            await self.m.click(go)
        await self.confirm()

    async def _reforge(self) -> None:
        await self.go("/voices")
        if not await self.pick("voices.row."):
            return
        if await self.m.click_marker("voices.reforge") is None:
            return
        if self.m.route() == "/forge":
            if not self.core.forge_draft.consent:
                await self.m.click_marker("forge.consent")
            await self._press_forge()

    async def _star(self) -> None:
        await self.go("/takes")
        await self.pick("takes.star.")

    async def _pin(self) -> None:
        await self.go("/takes")
        await self.pick(self.rng.choice(("takes.pin_a.", "takes.pin_b.")))

    async def _purge(self) -> None:
        await self.go("/takes")
        if await self.m.click_marker("takes.purge") is not None:
            self.m.clock.advance(0.6)
            await self.m.click_marker("takes.purge")

    async def _delete(self) -> None:
        await self.go("/voices")
        if len(self.core.state.library.rows) <= 3 or not await self.pick("voices.row."):
            return
        if await self.m.click_marker("voices.delete") is not None:
            self.m.clock.advance(0.6)
            await self.m.click_marker("voices.delete")

    async def _settings(self) -> None:
        await self.go("/settings")
        await self.pick("settings.tab.")
        for _ in range(self.rng.randint(1, 3)):
            found = self.m.candidates()
            page = [item for kind in ("click", "slide", "trigger") for item in found.get(kind, ())
                    if item.region == "page"]
            if page:
                await self.m.perform(self.rng.choice(page))

    async def _search(self) -> None:
        found = self.m.candidates().get("search")
        if found:
            await self.m.perform(found[0])

    async def _navigate(self) -> None:
        await self.pick("nav.")

    async def _time(self) -> None:
        await self.m.perform(self.m.candidates()["tick"][0])

    async def _chaos(self) -> None:
        await self.m.step()

    async def fill_history(self) -> None:
        """Switch models until the load history holds as many events as it keeps, so its rows stop growing."""
        core = self.core
        keep = core.history.keep
        for _ in range(4 * keep):
            if len(core.state.history.events) >= keep:
                return
            if any(not hold.is_set() for hold in self.m.fake.holds) or core.state.job.kind:
                await self.m.act("release", "holds (history)", self.m.fake.release_all)
            resident = core.state.engine.resident
            targets = [backend for backend in core.state.engine.enabled if backend != resident]
            await self.m.click_marker(f"dock.row.{self.rng.choice(targets)}")
            await self.confirm(yes=1.0)
        raise AssertionError(f"the load history stayed at {len(core.state.history.events)} events")


# ---- measurements -------------------------------------------------------------------------------------

def pool_threads() -> tuple[int, list[str]]:
    """(worker-pool threads, the names of every other thread but the main one)."""
    pool, other = 0, []
    for thread in threading.enumerate():
        if thread is threading.main_thread():
            continue
        if thread.name.startswith(POOLS):
            pool += 1
        else:
            other.append(thread.name)
    return pool, sorted(other)


def metrics(user: User, core: Core) -> dict[str, Any]:
    client = user.client
    clock = core.scheduler
    pool, other = pool_threads()
    staging = core.paths.staging
    return {
        "client_elements": len(client.elements),
        "bindings": sum(len(items) for items in binding.bindings.values()),
        "bindable_properties": len(binding.bindable_properties),
        "delete_handlers": len(client.delete_handlers),
        "event_callbacks": sum(len(event.callbacks) for event in list(Event.instances)),
        "timers": getattr(clock, "pending", -1),
        "timer_heap": len(getattr(clock, "_heap", ())),
        "scheduler_tasks": sum(1 for task in getattr(clock, "_tasks", ()) if not task.done()),
        "background_tasks": sum(1 for task in background_tasks.running_tasks if not task.done()),
        "asyncio_tasks": sum(1 for task in asyncio.all_tasks() if not task.done()),
        "pool_threads": pool,
        "other_threads": other,
        "clients": len(Client.instances),
        "tickets": len(core.tokens._tickets),
        "tokens": len(core.tokens._tokens),
        "log_lines": len(core.state.log.lines),
        "history_events": len(core.state.history.events),
        "takes": len(core.takes.takes),
        "voices": len(core.state.library.rows),
        "staged_clips": len(core.clips._paths),
        "staging_files": len(list(staging.iterdir())) if staging.is_dir() else 0,
        "outbox_queue": len(client.outbox.updates) + len(client.outbox.messages),
    }


async def lap(monkey: Monkey, core: Core, fake: FakeEngine) -> dict[str, tuple[int, int, float]]:
    """Visit every page from the rail; per route (client elements, page elements, ms to open) on arrival."""
    counts: dict[str, tuple[int, int, float]] = {}
    for route in (*ROUTES[1:], ROUTES[0]):
        began = time.perf_counter()
        await monkey.click_marker(f"nav.{PAGE_ID[route]}")
        took = round((time.perf_counter() - began) * 1000, 1)
        assert monkey.route() == route, f"the rail did not open {route}"
        counts[route] = (*element_counts(monkey.user), took)
    return counts


def collect() -> None:
    for _ in range(3):
        gc.collect()


def live_bytes(user: User, core: Core) -> int:
    """Traced live memory once what the harness itself keeps is emptied and garbage is collected."""
    client = user.client
    client.outbox.message_history.clear()            # NiceGUI's reconnect buffer (bounded, 1000 messages)
    user.back_history[:] = user.back_history[-2:]
    user.notify.messages.clear()
    moved = getattr(core.trash, "moved", None)       # the user fixture's Recycle Bin lists every file it got
    if isinstance(moved, list):
        moved.clear()
    collect()
    return tracemalloc.get_traced_memory()[0]


SEEDED_VOICES = ("aiko", "marco", "friend", "narrator", "host-turbo")


async def normalize(monkey: Monkey, core: Core) -> None:
    """Bring back the shape the session's state keeps changing, so two measurements compare like with like:
    no takes (unstarred, then removed), only the seeded voices, the Speak draft on its first script."""

    async def run() -> None:
        disarm_faults(monkey.fake, core)
        for take in core.takes.takes:
            if take.starred:
                await asyncio.to_thread(core.takes.set_starred, take.id, False)
        ids = [take.id for take in core.takes.takes]
        if ids:
            await asyncio.to_thread(core.takes.purge_unstarred, ids, trash=core.trash)
        core.publish_takes()
        for row in list(core.state.library.rows):
            if row.voice_id not in SEEDED_VOICES:
                out = await core.facade.delete_voice(row.voice_id)
                assert out.ok, f"could not delete {row.voice_id}: {out.reason}"
        await core.refresh_library()
        session = core.session
        session.take_id = session.pin_a = session.pin_b = 0
        session.text = SCRIPTS[0]
        if any(row.voice_id == "aiko" for row in core.state.library.rows):
            session.voice_id = "aiko"
        core.events.notify()

    await monkey.act("normalize", "takes, voices, draft", run)


async def checkpoint(name: str, monkey: Monkey, core: Core, fake: FakeEngine, inv: Invariants) -> dict[str, Any]:
    """Settle everything, expire the transient timers, normalise the state, run two laps and measure."""
    await monkey.act("release", f"holds ({name})", fake.release_all)
    await monkey.act("tick", f"+5 s ({name})", lambda: monkey.clock.advance(5.0))
    problems = takes_consistency(core, deleted=inv.deleted) + [f"left behind: {path}"
                                                               for path in leftovers(core.paths.root)]
    await normalize(monkey, core)
    first = await lap(monkey, core, fake)
    collect()
    after_first = metrics(monkey.user, core)
    second = await lap(monkey, core, fake)
    collect()
    after_second = metrics(monkey.user, core)
    problems += [f"{route}: {first[route][:2]} elements the first time, {second[route][:2]} the second"
                 for route in ROUTES if first[route][:2] != second[route][:2]]
    for key in ("bindings", "bindable_properties", "delete_handlers", "event_callbacks", "client_elements"):
        if after_second[key] != after_first[key]:
            problems.append(f"{key} grew from {after_first[key]} to {after_second[key]} over one lap")
    problems += takes_consistency(core, deleted=inv.deleted)
    problems += [f"left behind: {path}" for path in leftovers(core.paths.root)]
    inv.trim()
    return {"name": name, "action": monkey.index, "pages": {PAGE_ID[r]: second[r] for r in ROUTES},
            "first_lap_ms": {PAGE_ID[r]: first[r][2] for r in ROUTES}, "metrics": after_second,
            "live_bytes": live_bytes(monkey.user, core), "problems": problems}


@pytest.mark.soak
@pytest.mark.scenario("speak")
async def test_long_session_soak(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    drops = drop_files(tmp_path)
    install_kit(monkeypatch, tmp_path, file_result=drops[0])
    seed = int(os.environ.get("REL_SOAK_SEED", "7"))
    inv = Invariants(user, core, fake, monkeypatch)
    trace = Trace(f"R1 long session (seed={seed} actions={SOAK_ACTIONS})\n"
                  f"replay: REL_SOAK_SEED={seed} pytest tests/pages/test_rel_soak.py::test_long_session_soak -m soak",
                  keep=400)
    rng = random.Random(f"soak|{seed}")
    monkey = Monkey(user, core, fake, inv, trace, rng=rng, drops=drops, reloads=False)
    session = Session(monkey, core, rng)
    tracemalloc.start(1)
    started = time.perf_counter()
    points: list[dict[str, Any]] = []
    try:
        await open_monkey(user, core, fake, open_route, "/", inv)
        while monkey.index < WARMUP:
            await session.task()
        await session.fill_history()
        points.append(await checkpoint("baseline", monkey, core, fake, inv))
        base_snapshot = tracemalloc.take_snapshot()
        next_check = monkey.index + CHECK_EVERY
        while monkey.index < SOAK_ACTIONS:
            await session.task()
            if monkey.index >= next_check:
                points.append(await checkpoint(f"at {monkey.index}", monkey, core, fake, inv))
                next_check = monkey.index + CHECK_EVERY
        await monkey.finish()
        points.append(await checkpoint("end", monkey, core, fake, inv))
        top = [str(stat) for stat in tracemalloc.take_snapshot().compare_to(base_snapshot, "lineno")[:12]]
    finally:
        tracemalloc.stop()
        inv.close()
    elapsed = time.perf_counter() - started
    base_bytes, end_bytes = points[0]["live_bytes"], points[-1]["live_bytes"]
    growth = end_bytes - base_bytes
    numbers = {
        "actions": monkey.index, "seconds": round(elapsed, 1), "tasks": dict(session.done),
        "kinds": dict(monkey.kinds), "growth_bytes": growth, "budget_bytes": GROWTH_BUDGET,
        "base_bytes": base_bytes, "end_bytes": end_bytes, "top_growth": top, "checkpoints": points,
        "loads": collections.Counter(why for _, _, why in inv.loads), "faults": len(inv.faults),
        "warnings": dict(inv.counts.most_common(15)), "slowest": monkey.slow_acts,
    }
    print(f"\n[long session] {summary(monkey)} in {elapsed:.0f} s; tasks {dict(session.done)}")
    print(f"live memory {base_bytes / 2**20:.2f} MiB at the baseline, {end_bytes / 2**20:.2f} MiB at the end "
          f"({growth / 2**10:+.0f} KiB; budget {GROWTH_BUDGET / 2**20:.1f} MiB)")
    for point in points:
        m = point["metrics"]
        print(f"  {point['name']:>9} #{point['action']:>5}: live {point['live_bytes'] / 2**20:.2f} MiB | pages "
              f"{ {page: counts[1] for page, counts in point['pages'].items()} } open ms {point['first_lap_ms']} | "
              f"bindings {m['bindings']} props {m['bindable_properties']} handlers {m['delete_handlers']} "
              f"callbacks {m['event_callbacks']} timers {m['timers']}/{m['timer_heap']} tasks {m['asyncio_tasks']} "
              f"threads {m['pool_threads']}+{m['other_threads']} history {m['history_events']} "
              f"staged {m['staged_clips']}/{m['staging_files']} tickets {m['tickets']}")
    for line in top[:8]:
        print("   growth:", line)
    for seconds, kind, key, route in monkey.slow_acts:
        print(f"   slow: {seconds * 1000:.0f} ms {kind} {key} on {route}")
    save_numbers("r1_long_session", numbers)
    assert monkey.index >= SOAK_ACTIONS
    problems = [f"{point['name']}: {problem}" for point in points for problem in point["problems"]]
    assert problems == [], "\n".join(problems)
    first, last = points[0]["metrics"], points[-1]["metrics"]
    assert last["other_threads"] == first["other_threads"] == [], (first["other_threads"], last["other_threads"])
    for key in ("pool_threads", "timers", "scheduler_tasks", "background_tasks", "asyncio_tasks", "clients",
                "tickets", "delete_handlers", "event_callbacks"):
        assert last[key] <= first[key], f"{key} grew from {first[key]} to {last[key]}"
    assert growth < GROWTH_BUDGET, f"live memory grew {growth / 2**20:.2f} MiB:\n" + "\n".join(top)


# ---- the freeze check ---------------------------------------------------------------------------------

class LagSampler:
    """How late asyncio.sleep(LAG_PERIOD_S) wakes up on the app's loop, tagged with the job and the activity."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.activity = "idle"
        self.samples: list[tuple[str, str, float]] = []
        self.full_gc: list[float] = []                          # lags of the samples a full collection fell into
        self.collections: list[tuple[int, float, str]] = []     # (generation, seconds, activity) of each gc run
        self._task: asyncio.Task[Any] | None = None
        self._stop = False
        self._gc_started = 0.0
        self._full = 0                                          # full (generation 2) collections so far

    def start(self) -> None:
        gc.callbacks.append(self._on_gc)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="R1 lag sampler")

    async def stop(self) -> None:
        self._stop = True
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        if self._task is not None:
            await self._task

    def _on_gc(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._gc_started = time.perf_counter()
        else:
            generation = int(info.get("generation", -1))
            self.collections.append((generation, time.perf_counter() - self._gc_started, self.activity))
            if generation == 2:
                self._full += 1

    def gc_table(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for generation in (0, 1, 2):
            pauses = [seconds for gen, seconds, _ in self.collections if gen == generation]
            if pauses:
                table[f"gen{generation}"] = {"count": len(pauses), "max_ms": round(max(pauses) * 1000, 2),
                                             "total_ms": round(sum(pauses) * 1000, 1)}
        return table

    async def _run(self) -> None:
        while not self._stop:
            before, full = time.perf_counter(), self._full
            await asyncio.sleep(LAG_PERIOD_S)
            lag = max(0.0, time.perf_counter() - before - LAG_PERIOD_S)
            if self._full != full:                   # a full collection paused the whole interpreter meanwhile
                self.full_gc.append(lag)
                continue
            job = self.core.state.job
            phase = f"{job.kind}/{job.phase}" if job.kind else "idle"
            self.samples.append((self.activity, phase, lag))

    def table(self) -> dict[str, dict[str, float]]:
        groups: dict[str, list[float]] = collections.defaultdict(list)
        for activity, phase, lag in self.samples:
            groups[f"{activity} · {phase}"].append(lag)
        return {name: _stats(lags) for name, lags in sorted(groups.items())}


def _stats(lags: list[float]) -> dict[str, float]:
    ordered = sorted(lags)
    return {"n": len(ordered), "p50_ms": round(statistics.median(ordered) * 1000, 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] * 1000, 2),
            "p99_ms": round(ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))] * 1000, 2),
            "max_ms": round(ordered[-1] * 1000, 2)}


@pytest.mark.soak
@pytest.mark.scenario("speak")
@pytest.mark.parametrize("clock_kind", ["manual", "real"], indirect=True)
async def test_event_loop_lag_during_engine_work(clock_kind: str, user: User, core: Core, fake: FakeEngine,
                                                 open_route: Any, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    assert (clock_kind == "real") != isinstance(core.scheduler, ManualScheduler)
    drops = drop_files(tmp_path)
    install_kit(monkeypatch, tmp_path, file_result=drops[0])
    inv = Invariants(user, core, fake, monkeypatch)
    monkey = Monkey(user, core, fake, inv, Trace("R1 lag"), rng=random.Random("lag"), faults=False, reloads=False)
    sampler = LagSampler(core)
    work: dict[str, float] = {}
    builds: dict[str, list[float]] = collections.defaultdict(list)

    async def engine_work(activity: str, marker: str, *, navigate: bool = False, wait: float = 120.0) -> None:
        """Click marker (and an ask's primary) without waiting for the job, then wait for it in real time;
        navigate opens every page from the rail, round after round, while the job runs."""
        sampler.activity = activity
        began = time.perf_counter()
        if await monkey.click_marker(marker, job=False) is None:
            raise AssertionError(f"{activity}: {marker} is not there to click")
        if monkey.first(EMIYA["primary"]) is not None:
            await monkey.click_marker(EMIYA["primary"], job=False)
        while navigate and core.state.job.kind:
            for route in (*ROUTES[1:], ROUTES[0]):
                opened = time.perf_counter()
                await monkey.click_marker(f"nav.{PAGE_ID[route]}", job=False)
                builds[f"{PAGE_ID[route]} during a load"].append(time.perf_counter() - opened)
        deadline = time.perf_counter() + wait
        while core.state.job.kind:
            assert time.perf_counter() < deadline, f"{activity} did not finish in {wait:.0f} s"
            await asyncio.sleep(LAG_PERIOD_S)
        work[activity] = round(time.perf_counter() - began, 2)
        sampler.activity = "idle"
        await quiesce(core, fake)

    try:
        await open_monkey(user, core, fake, open_route, "/", inv)
        fake.speed = LAG_SPEED
        sampler.start()
        await asyncio.sleep(0.3)
        await engine_work("switch to Turbo (evict + load)", f"dock.row.{TUR}")
        await monkey.click_marker("speak.voice.host-turbo")
        script = monkey.first("speak.text")
        assert script is not None
        await monkey.set_text(script, "Okay, okay [chuckle] it works. This is a longer line for the take.")
        await engine_work("take (Turbo)", "speak.generate")
        await engine_work("3 takes (Turbo)", "speak.generate3")
        await monkey.click_marker("nav.forge")
        if core.forge_draft.clip_state != "ready":
            await monkey.click_marker("forge.browse")
        box = monkey.first("forge.name")
        assert box is not None
        await monkey.set_text(box, "lagtest")
        await monkey.click_marker(f"forge.backend.{MTL}")
        if not core.forge_draft.consent:
            await monkey.click_marker("forge.consent")
        await engine_work("forge on Multilingual (switch + bake)", "forge.go")
        await engine_work("Free VRAM (unload)", "dock.free")
        await engine_work("load Standard, pages opened meanwhile", f"dock.row.{STD}", navigate=True)
        fake.speed = 0.0
        sampler.activity = "page builds, 500 history rows"
        await fill_history_directly(core)
        for _ in range(3):
            for route in (*ROUTES[1:], ROUTES[0]):
                opened = time.perf_counter()
                await monkey.click_marker(f"nav.{PAGE_ID[route]}")
                builds[f"{PAGE_ID[route]} with a full history"].append(time.perf_counter() - opened)
        sampler.activity = "idle"
        await asyncio.sleep(0.3)
    finally:
        fake.speed = 0.0
        await sampler.stop()
        inv.close()
    await monkey.finish()
    table = sampler.table()
    engine = [lag for activity, phase, lag in sampler.samples if phase != "idle" and "pages" not in activity]
    with_pages = [lag for activity, phase, lag in sampler.samples if "pages opened" in activity]
    full = [lag for activity, phase, lag in sampler.samples if "history rows" in activity]
    opens = {name: _stats(times) for name, times in sorted(builds.items())}
    numbers = {"clock": clock_kind, "speed": LAG_SPEED, "limit_ms": LAG_LIMIT_S * 1000,
               "period_ms": LAG_PERIOD_S * 1000, "work_seconds": work, "engine_work": _stats(engine),
               "engine_work_with_page_opens": _stats(with_pages) if with_pages else {},
               "page_opens_full_history": _stats(full) if full else {}, "page_open_seconds": opens, "table": table,
               "gc": sampler.gc_table(),
               "samples_with_a_full_gc": _stats(sampler.full_gc) if sampler.full_gc else {}}
    print(f"\n[event-loop lag, {clock_kind} clock] FakeEngine speed {LAG_SPEED}; sampler period "
          f"{LAG_PERIOD_S * 1000:.0f} ms; work seconds {work}")
    for name, row in table.items():
        print(f"  {name:62} n={row['n']:>4} p50={row['p50_ms']:>6} ms p95={row['p95_ms']:>6} ms "
              f"max={row['max_ms']:>6} ms")
    print(f"  engine work: {numbers['engine_work']}")
    print(f"  engine work with pages opened meanwhile: {numbers['engine_work_with_page_opens']}")
    print(f"  pages opened with 500 history rows: {numbers['page_opens_full_history']}")
    for name, row in opens.items():
        print(f"  open {name:32} n={row['n']:>3} p50={row['p50_ms']:>7} ms max={row['max_ms']:>7} ms")
    print(f"  garbage collections: {numbers['gc']}; samples a full collection fell into (left out above): "
          f"{numbers['samples_with_a_full_gc']}")
    save_numbers(f"r1_event_loop_lag_{clock_kind}", numbers)
    assert len(engine) > 50, "the sampler saw too little engine work"
    assert max(engine) < LAG_LIMIT_S, f"the event loop lagged {max(engine) * 1000:.0f} ms during engine work"


async def fill_history_directly(core: Core) -> None:
    """The load history at its cap (core.history.keep events), as months of use leave it; the Models page lists
    every one of them."""
    from ui.contract.records import LoadEvent
    from ui.core.services.load_history import now_iso

    missing = core.history.keep - len(core.history.events)
    kinds = ("switching", "unloaded", "loaded")
    events = [LoadEvent(t=now_iso(), kind=kinds[n % 3], backend=(MTL, STD, TUR)[n % 3],
                        from_backend=TUR if n % 3 == 0 else "", seconds=14.2 if n % 3 == 2 else None,
                        gb=3.4 if n % 3 else None, device="cuda") for n in range(max(0, missing))]

    def append_all() -> None:
        for event in events:
            core.history.append(event)

    await asyncio.to_thread(append_all)
    core.publish_history()
