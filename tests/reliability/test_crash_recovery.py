"""R2 · crash mid-write: real child processes killed with TerminateProcess at random moments.

For each writer (the voice registry, the takes store, the settings store, the load history, the backends' atomic
.pt save and the engine owner file) tests\\reliability\\_crash_child.py repeats its write on a scratch data root
and is killed at a random moment, KILLS times per writer: SLOTS chains run side by side, each a series of kills on
its own root, so every child starts on what the one before it left. After every kill:

- each file parses and holds the result of the last journaled operation or of the one after it (the one the kill
  interrupted), never a mix of the two (_crash_child's state helpers map a file back to its operation);
- leftover temp and partial files are ignored by every reader, and the stores that sweep them (the takes store's
  load) sweep them on the next start;
- a fresh Core (a real VoiceEngine with fake factories, the manual clock) starts cleanly on a copy of that root,
  plus a check of the writer's own engine or store on it.

Then stale locks: a killed holder of cache\\gui.lock, or of cache\\engine.lock with engine.owner.json, never blocks
the next start. No model loads (fake factories, DEVICE "cpu"), no window opens (CREATE_NO_WINDOW children), and
only the children this test started are killed, by their own process handle. Delays come from a seeded RNG per
chain; a failure reports the seed, the chain and its kill trace.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock

from engine import engine as engine_module
from engine.engine import VoiceEngine
from engine.errors import EngineBusy
from engine.lock import EngineLock
from engine.registry import VoiceRegistry
from ui.core import Core, build_core
from ui.core.clock import ManualScheduler
from ui.core.facade import EventRelay
from ui.core.paths import Paths
from ui.core.services import takes_store as takes_module
from ui.core.services.load_history import LoadHistory
from ui.core.services.settings import _read_raw, write_atomic
from ui.core.services.takes_store import TakesStore, resolve_stored
from ui.entry import EXIT_ALREADY_RUNNING, take_instance_lock

from tests.engine.conftest import install_fake_factories, make_scratch_config, write_clip
from tests.reliability import _crash_child as child

APP_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name("_crash_child.py")
SEED = 20261113
KILLS = 50                                         # per writer, at least
SLOTS = max(2, min(10, (os.cpu_count() or 4) // 2))
CYCLES = -(-KILLS // SLOTS)
DELAY_MAX_S = 0.06                                 # the kill lands 0..60 ms into the write loop
READY_TIMEOUT_S = 60.0
WRITERS = ("registry", "takes", "settings", "history", "pt", "owner")
LEFTOVER = re.compile(r"\.tmp-|\.tmp$|\.part$|\.part-", re.IGNORECASE)
TAKE_WAV = re.compile(r".*_take(\d{1,9})(?:-\d{1,9})?\.wav", re.IGNORECASE)
NUMBERS: dict[str, dict[str, Any]] = {}            # printed per writer (run with -s to see them)


# ---- one chain: a data root that children write and get killed on --------------------------------------

@dataclass
class Kill:
    cycle: int
    delay_s: float
    pid: int
    journaled: int
    leftovers: tuple[str, ...]

    def __str__(self) -> str:
        left = f", left {list(self.leftovers)}" if self.leftovers else ""
        return f"#{self.cycle} pid {self.pid} after {self.delay_s * 1000:.0f} ms at op {self.journaled}{left}"


@dataclass
class Chain:
    writer: str
    slot: int
    base: Path
    kills: list[Kill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = self.base / self.writer / f"slot{self.slot}" / "data"
        self.logs = self.base / self.writer / f"slot{self.slot}" / "logs"
        self.checks = self.base / self.writer / f"slot{self.slot}" / "checks"
        for folder in (self.root / "cache", self.root / "voices" / "reference", self.logs, self.checks):
            folder.mkdir(parents=True, exist_ok=True)
        self.rng = random.Random(f"{SEED}-{self.writer}-{self.slot}")

    def where(self) -> str:
        trace = "; ".join(str(kill) for kill in self.kills[-6:])
        return f"[{self.writer} slot {self.slot}, seed {SEED}, root {self.root}] last kills: {trace}"

    def kill_once(self, cycle: int) -> Kill:
        """Start a child, wait for READY, wait a random moment, TerminateProcess it (blocking; run in a thread)."""
        delay = self.rng.uniform(0.0, DELAY_MAX_S)
        err_path = self.logs / f"child{cycle}.err"
        with open(err_path, "wb") as err:
            proc = spawn_child(self.writer, self.root, err)
            try:
                line = proc.stdout.readline() if proc.stdout is not None else b""
                if line.strip() != child.READY.encode():
                    proc.wait(timeout=READY_TIMEOUT_S)
                    stderr = err_path.read_text("utf-8", "replace")
                    raise AssertionError(f"the child never got ready ({line!r}): {stderr} {self.where()}")
                time.sleep(delay)
                if proc.poll() is not None:
                    raise AssertionError(f"the child exited by itself ({proc.returncode}) {self.where()}")
                proc.kill()                                   # TerminateProcess on our own child's handle
                proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                if proc.stdout is not None:
                    proc.stdout.close()
        stderr = err_path.read_text("utf-8", "replace")
        assert stderr == "", f"the child wrote to stderr (a traceback?):\n{stderr}\n{self.where()}"
        kill = Kill(cycle, delay, proc.pid, child.last_journaled(self.root), tuple(leftovers(self.root)))
        self.kills.append(kill)
        return kill

    def copy(self, cycle: int | str) -> Path:
        """A copy of the root as the kill left it, for the fresh-start checks (they may sweep and rewrite)."""
        target = self.checks / f"after{cycle}"
        shutil.copytree(self.root, target, ignore=shutil.ignore_patterns(".trash", child.JOURNAL))
        return target


def spawn_child(writer: str, root: Path, err: Any) -> subprocess.Popen[bytes]:
    """_crash_child.py as ONE process: the venv's python.exe is a redirector that runs the base interpreter as its
    child (killing the redirector would only reach the writer through the job object, a moment later), so start the
    base interpreter with __PYVENV_LAUNCHER__ naming the venv, as multiprocessing does on Windows (bpo-35797)."""
    env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    executable = getattr(sys, "_base_executable", sys.executable)
    if os.path.normcase(executable) != os.path.normcase(sys.executable):
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return subprocess.Popen([executable, str(CHILD), writer, str(root)], cwd=APP_ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=err, env=env, creationflags=subprocess.CREATE_NO_WINDOW)


def leftovers(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.is_file() and LEFTOVER.search(path.name) and ".trash" not in path.parts)


def ops_near(ops: set[int], journaled: int) -> bool:
    """The file holds the last journaled operation's result or the next one's (the one the kill interrupted)."""
    return bool(ops & {journaled, journaled + 1})


# ---- what each writer's files must hold after a kill ---------------------------------------------------

def check_registry(chain: Chain, kill: Kill, copy: Path) -> None:
    path = child.registry_path(chain.root)
    entries = json.loads(path.read_text("utf-8")) if path.exists() else {}
    ops = child.registry_ops(entries, str(child.reference(chain.root)))
    assert ops_near(ops, kill.journaled), f"registry holds ops {ops}, journal says {kill.journaled} {chain.where()}"
    assert all(name.startswith("cache/registry.json.") for name in kill.leftovers), chain.where()
    lock = FileLock(str(path) + ".lock", timeout=0)                 # the killed writer's lock went with it
    lock.acquire()
    lock.release()
    fresh = VoiceRegistry(child.registry_path(copy))
    assert fresh.entries() == entries and fresh.list_ids() == sorted(entries)     # temp files are ignored
    fresh.register("check", child.MTL, "x.wav", 0.5, "en")                      # and the next write works
    assert set(fresh.entries()) == set(entries) | {"check"}


def check_takes(chain: Chain, kill: Kill, copy: Path) -> None:
    index = child.takes_dir(chain.root) / "index.json"
    next_id, takes = takes_module._parse_index(index.read_bytes()) if index.exists() else (1, ())
    ops = child.takes_ops(takes)
    assert ops_near(ops, kill.journaled), f"index holds ops {ops}, journal says {kill.journaled} {chain.where()}"
    purging = any(op % 4 == 2 for op in ops)                   # an unstar done: the purge's trash may have run
    for take in takes:
        if not resolve_stored(take.wav, chain.root).is_file():
            assert purging and not take.starred, f"listed take {take.id} has no WAV {chain.where()}"
    assert next_id > max((take.id for take in takes), default=0), chain.where()
    whole = child.tone_wav()
    for path in child.takes_dir(chain.root).glob("*.wav"):      # every WAV under its final name is whole
        assert path.read_bytes() == whole, f"{path.name} is not the WAV the child wrote {chain.where()}"
    store = TakesStore(child.takes_dir(copy) / "index.json", copy)
    report = store.load(folders=[child.takes_dir(copy)])
    assert (report.error, report.moved_to, report.read_only) == ("", "", False), f"{report} {chain.where()}"
    assert leftovers(copy) == [], f"the next start left {leftovers(copy)} {chain.where()}"
    numbers = [int(m.group(1)) for p in child.takes_dir(copy).glob("*.wav") if (m := TAKE_WAV.fullmatch(p.name))]
    assert store.next_id > max(numbers, default=0), f"next_id {store.next_id} reuses a WAV number {chain.where()}"
    missing = set(store.check_files())
    assert all(not store.get(take_id).starred for take_id in missing) and (not missing or purging), chain.where()
    take = store.add(child.tone_wav(), takes_module.NewTake(voice="aiko", backend=child.MTL, language="en",
                                                            text="check", exaggeration=0.5, cfg_weight=0.5,
                                                            gen_seconds=0.1, session="check"), child.takes_dir(copy))
    assert take.id == report.next_id and resolve_stored(take.wav, copy).is_file()


def check_settings(chain: Chain, kill: Kill, copy: Path) -> None:
    path = child.settings_path(chain.root)
    raw, problem = _read_raw(path)
    assert problem == "" and raw.get("extra") == {"kept": True}, f"{problem!r} {raw} {chain.where()}"
    value = child.settings_read_early(path)["theme.emiya"]["fx_speed"]
    allowed = {child.settings_value(kill.journaled + 1)} | (
        {child.settings_value(kill.journaled)} if kill.journaled >= 0 else {100})
    assert value in allowed, f"fx_speed {value} after op {kill.journaled} (allowed {allowed}) {chain.where()}"
    store = child.settings_store(child.settings_path(copy))
    assert store.get("theme.emiya", "fx_speed") == value                        # temp files are ignored
    store.set("theme.emiya", "work_glow", False)                               # and the next write works
    store.flush()                                                               # (a worker: set() wrote already)
    assert child.settings_read_early(child.settings_path(copy))["theme.emiya"]["work_glow"] is False
    assert json.loads(child.settings_path(copy).read_text("utf-8"))["extra"] == {"kept": True}


def check_history(chain: Chain, kill: Kill, copy: Path) -> None:
    steps, bad = child.history_steps(child.history_path(chain.root))
    assert bad <= 1, chain.where()
    assert steps == list(range(steps[0], steps[0] + len(steps))) if steps else True, f"gaps: {steps} {chain.where()}"
    last = steps[-1] if steps else -1
    assert last in (kill.journaled, kill.journaled + 1), f"last {last}, journal {kill.journaled} {chain.where()}"
    history = LoadHistory(child.history_path(copy), keep=child.HISTORY_KEEP)
    history.load()
    loaded = [int(ev.seconds) for ev in history.events]
    assert loaded == steps[-child.HISTORY_KEEP:], f"{loaded} vs {steps} {chain.where()}"
    assert child.history_steps(child.history_path(copy)) == (loaded, 0)        # compacted: only good lines


def check_pt(chain: Chain, kill: Kill, copy: Path) -> None:
    step = child.pt_step(child.pt_path(chain.root))
    allowed = {kill.journaled, kill.journaled + 1} if kill.journaled >= 0 else {-1, 0}
    assert step in allowed, f".pt holds step {step}, journal says {kill.journaled} {chain.where()}"
    assert all(re.fullmatch(rf"cache/{child.MTL}/aiko\.pt\.tmp-\d+", name) for name in kill.leftovers), chain.where()
    backend = child.crash_backend(child.pt_path(copy).parent)
    assert backend.list_voices() == (["aiko"] if step >= 0 else [])            # aiko.pt.tmp-<pid> is ignored
    if step < 0:
        return
    backend.load_voice("aiko", child.reference(copy), force=False)             # reads the .pt the kill left
    assert backend.speak("Hello.", "aiko").startswith(b"RIFF")
    engine = VoiceEngine(make_scratch_config(copy), lock_path=copy / "cache" / "engine.lock")
    try:
        engine.load_voice("aiko", child.reference(copy), backend=child.MTL, force=False)
        assert engine.speak("Hello there.", "aiko").startswith(b"RIFF")
    finally:
        engine.close()


def check_owner(chain: Chain, kill: Kill, copy: Path) -> None:
    lock_path = chain.root / "cache" / "engine.lock"
    owner = lock_path.with_name("engine.owner.json")
    if owner.exists():
        record = json.loads(owner.read_text("utf-8"))
        assert record["pid"] == kill.pid and record["program"] == child.CHILD_PROGRAM, f"{record} {chain.where()}"
        assert isinstance(record["since"], str)
    lock = EngineLock(lock_path, program="pytest")
    lock.acquire()                                     # the dead child's lock is gone: no EngineBusy
    try:
        assert lock.holder()["pid"] == os.getpid()
    finally:
        lock.release()
    assert not owner.exists()
    engine = VoiceEngine(make_scratch_config(copy), lock_path=lock_path)
    try:
        assert engine.make_resident(child.MTL).backend == child.MTL and engine.residency().lock_held
    finally:
        engine.close()
    assert not owner.exists() and not engine.residency().lock_held


CHECKS = {"registry": check_registry, "takes": check_takes, "settings": check_settings, "history": check_history,
          "pt": check_pt, "owner": check_owner}


# ---- a fresh Core on the copy --------------------------------------------------------------------------

async def fresh_core_starts(copy: Path, chain: Chain, caplog: pytest.LogCaptureFixture) -> None:
    """build_core on the crashed root's copy and core.start(): its startup checks run with no failure logged."""
    before = len(caplog.records)
    relay = EventRelay()
    engine = VoiceEngine(make_scratch_config(copy), on_event=relay, lock_path=copy / "cache" / "engine.lock")
    paths = Paths.for_run(copy / "app", data_root=copy, fake=False)
    clock = ManualScheduler()
    core = build_core(engine=engine, paths=paths, settings=child.settings_store(paths.settings), trash=lambda _p: None,
                      scheduler=clock, relay=relay)
    try:
        await core.start()
        await clock.settle()
        check_core(core, chain)
    finally:
        core.stop()
        clock.cancel_pending()
        engine.close()
    problems = [r.getMessage() for r in caplog.records[before:] if r.levelno >= logging.WARNING
                and r.name.startswith("voicetool") and not EXPECTED_WARNING.search(r.getMessage())]
    assert problems == [], f"a fresh core logged {problems} {chain.where()}"


EXPECTED_WARNING = re.compile(r"load_history\.jsonl: skipped 1 unreadable line")   # a line the kill cut short


def check_core(core: Core, chain: Chain) -> None:
    state = core.state
    assert state.library.status == "ready", f"library {state.library.status}: {state.library.error} {chain.where()}"
    assert core.takes._loaded, chain.where()
    assert state.takes.takes == core.takes.takes
    if chain.writer == "registry":
        assert {row.voice_id for row in state.library.rows} == set(VoiceRegistry(
            child.registry_path(core.paths.root)).entries())
    if chain.writer == "history":
        assert len(state.history.events) == len(core.history.events) and state.history.events
    if chain.writer == "settings":
        assert core.settings.get("theme.emiya", "fx_speed") in child.SETTINGS_VALUES


# ---- the tests -----------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def frozen_heap() -> Iterator[None]:
    """The objects earlier tests left behind move to gc's permanent generation for this test (gc.freeze), so a
    long suite's heap does not make the garbage collector slow the kill loops; unfrozen afterwards."""
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Fake backend factories (no model), and no Hugging Face cache lookups for the weights sizes."""
    made = install_fake_factories(monkeypatch)
    monkeypatch.setattr(engine_module, "_weights_status", lambda name: (True, 3_000_000_000))
    yield made
    made.release_all()


def prepare(chain: Chain) -> None:
    """What a writer needs on its root before the first child: a reference clip; a settings file with extras."""
    write_clip(child.reference(chain.root), 6.0)
    if chain.writer == "settings":
        write_atomic(child.settings_path(chain.root), json.dumps({"version": 1, "extra": {"kept": True}}))


@pytest.mark.parametrize("writer", WRITERS)
async def test_a_writer_killed_mid_write_leaves_the_old_or_the_new_content(
        writer: str, tmp_path: Path, fakes: Any, caplog: pytest.LogCaptureFixture) -> None:
    """KILLS kills of one writer over SLOTS chains; after each: the file checks, the writer's own fresh store or
    engine on a copy, and a fresh Core on another copy (see the module docstring)."""
    caplog.set_level(logging.WARNING)
    chains = [Chain(writer, slot, tmp_path) for slot in range(SLOTS)]
    for chain in chains:
        prepare(chain)
    started = time.monotonic()

    timings = {"kill_s": 0.0, "store_check_s": 0.0, "core_start_s": 0.0}

    async def run(chain: Chain) -> None:
        for cycle in range(CYCLES):
            mark = time.perf_counter()
            kill = await asyncio.to_thread(chain.kill_once, cycle)
            timings["kill_s"] += time.perf_counter() - mark
            copy, store_copy = chain.copy(cycle), chain.copy(f"{cycle}-store")
            mark = time.perf_counter()
            await asyncio.to_thread(CHECKS[writer], chain, kill, store_copy)
            timings["store_check_s"] += time.perf_counter() - mark
            mark = time.perf_counter()
            await fresh_core_starts(copy, chain, caplog)
            timings["core_start_s"] += time.perf_counter() - mark
            for done in (copy, store_copy):                    # keep TEMP small: only the chain roots stay
                shutil.rmtree(done, ignore_errors=True)

    outcomes = await asyncio.gather(*(run(chain) for chain in chains), return_exceptions=True)
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if failures:
        raise failures[0]
    kills = [kill for chain in chains for kill in chain.kills]
    assert len(kills) >= KILLS
    mid_write = sum(1 for kill in kills if kill.leftovers)
    ops = [kill.journaled for kill in kills]
    NUMBERS[writer] = {"kills": len(kills), "left_temp_files": mid_write, "ops_reached_max": max(ops),
                       "seconds": round(time.monotonic() - started, 1), "slots": SLOTS,
                       **{name: round(value, 1) for name, value in timings.items()}}
    print(f"\n[R2 numbers] {writer}: {NUMBERS[writer]}")


def test_a_killed_lock_holder_never_blocks_the_next_start(tmp_path: Path, fakes: Any) -> None:
    """A child holds cache\\gui.lock (as gui.py does) and cache\\engine.lock with engine.owner.json (program
    gui.py), then is killed: the single-instance check passes at once, the stale owner record is only a record,
    and a fresh engine loads a model and takes the engine lock over."""
    chain = Chain("hold", 0, tmp_path)
    gui_lock = chain.root / "cache" / "gui.lock"
    engine_lock = chain.root / "cache" / "engine.lock"
    for cycle in range(5):
        holder: dict[str, Any] = {}

        def while_held(_pid: int) -> None:
            refused = take_instance_lock(gui_lock)
            assert isinstance(refused, int) and int(refused) == EXIT_ALREADY_RUNNING
            with pytest.raises(EngineBusy) as busy:
                EngineLock(engine_lock, program="pytest").acquire()
            holder.update(busy.value.holder or {})

        kill = kill_holder(chain, cycle, while_held)
        assert holder.get("pid") == kill.pid and holder.get("program") == "gui.py", holder
        instance = take_instance_lock(gui_lock)
        assert not isinstance(instance, int), f"a dead holder blocked the next start {chain.where()}"
        instance.release()
        assert EngineLock(engine_lock).holder()["pid"] == kill.pid      # the stale record: harmless
        engine = VoiceEngine(make_scratch_config(chain.root), lock_path=engine_lock)
        try:
            engine.make_resident(child.MTL)
            assert engine.engine_lock_holder()["pid"] == os.getpid()
        finally:
            engine.close()
        assert not engine_lock.with_name("engine.owner.json").exists()


def kill_holder(chain: Chain, cycle: int, while_held: Any) -> Kill:
    """kill_once for the hold child, running while_held(pid) between READY and the kill."""
    err_path = chain.logs / f"hold{cycle}.err"
    with open(err_path, "wb") as err:
        proc = spawn_child("hold", chain.root, err)
        try:
            line = proc.stdout.readline() if proc.stdout is not None else b""
            assert line.strip() == child.READY.encode(), err_path.read_text("utf-8", "replace")
            while_held(proc.pid)
            proc.kill()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()
    assert err_path.read_text("utf-8", "replace") == ""
    kill = Kill(cycle, 0.0, proc.pid, -1, ())
    chain.kills.append(kill)
    return kill
