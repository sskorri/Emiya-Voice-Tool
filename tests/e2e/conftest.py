"""Real-engine runs (§8.9 and the GPU soak, I3): gui.py under the NiceGUI user fixture with VOICETOOL_ENGINE=real.

Every test in this folder loads real models. It is marked `gpu` (never part of the default run) and starts only when
- no visible, non-minimized foreground window covers a whole screen (a game or a full-screen app; the user's GPU
  rule: SHQueryUserNotificationState alone is not trusted), and
- no other process holds the real engine lock (cache\\engine.lock: the real GUI, cli.py or app.py has a model).
Otherwise it skips and says why.

What a test gets:
- a scratch data root: VOICETOOL_E2E_ROOT\\<test name> when that variable names a folder, else tmp_path. It is wiped
  first and gets a copy of voices\\reference\\sample.wav. The Hugging Face cache is the real one (offline) and the
  engine lock is the real one, so a run refuses to load next to a GUI that holds a model;
- the trash the test's `trash` marker names: "recycle" is send2trash (the user's Recycle Bin, recorded), anything
  else a scratch folder in the data root (launch.ScratchTrash);
- `rig`, a driver over the user fixture (markers only) with the recorders §8.9's "all" row needs: every engine
  event, a spy on the backend factories (nothing may be resident while a model is built), the residency seen from
  a sampler thread, every take and bake job, the event-loop lag (an asyncio.sleep(0.05) drift probe) and
  nvidia-smi readings.
After the test the engine is closed (every model unloaded, the lock released), the numbers and the action trace go
to VOICETOOL_E2E_RESULTS (one JSON file, one section per test; default tmp_path\\results.json), and the scratch
model files (the data root's cache: .pt conditioning and staging) are removed unless VOICETOOL_E2E_KEEP=1.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock
import pytest

from ui import entry
from ui.contract.routes import PAGE_ID, ROUTES

if TYPE_CHECKING:
    from nicegui.testing import User

    from ui.core import Core

APP_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = APP_ROOT / "voices" / "reference" / "sample.wav"
ENGINE_LOCK = APP_ROOT / "cache" / "engine.lock"
ROOT_ENV = "VOICETOOL_E2E_ROOT"
RESULTS_ENV = "VOICETOOL_E2E_RESULTS"
KEEP_ENV = "VOICETOOL_E2E_KEEP"
FULL_SCREEN = "a full-screen app is in front"
LOCK_HELD = "another process holds the engine lock (the real GUI, cli.py or app.py has a model loaded)"

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
BACKENDS = (MTL, STD, TUR)
GIB = 2 ** 30
MIB = 2 ** 20
FREED_LIMIT = int(0.2 * GIB)           # §8.9: allocated after every unload, reserved after Free VRAM
SEED = 20261114                        # torch, numpy and random, once torch is imported (sampling is random)
LAG_PROBE_S = 0.05                     # §8.9: asyncio.sleep(0.05) drift
LAG_FLAG_S = 0.5                       # a lag above this is flagged (the freeze check)
POLL_S = 0.05
LOAD_TIMEOUT_S = 300.0
TAKE_TIMEOUT_S = 300.0
BAKE_TIMEOUT_S = 180.0
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW


# ---- pre-flight ---------------------------------------------------------------------------------------

def full_screen_app() -> str | None:
    """The screen a visible, non-minimized foreground window covers (a game or a full-screen app), or None."""
    desktop = entry.probe_desktop()
    taken = [screen.device for screen in desktop.screens if entry.screen_taken(screen, desktop.foreground)]
    return taken[0] if taken else None


def engine_lock_free(path: Path = ENGINE_LOCK) -> bool:
    """True when no other process holds the real engine lock (a non-blocking try, released at once)."""
    lock = filelock.FileLock(str(path), timeout=0, thread_local=False)
    try:
        lock.acquire()
    except filelock.Timeout:
        return False
    lock.release()
    return True


def gpu_used_mib() -> int | None:
    """nvidia-smi memory.used of GPU 0 in MiB (device-wide: every process), or None without nvidia-smi."""
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(done.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def gpu_used_median(samples: int = 3, gap_s: float = 0.3) -> int | None:
    """The median of a few nvidia-smi readings (other programs' VRAM moves a little between readings)."""
    values = []
    for index in range(samples):
        if index:
            time.sleep(gap_s)
        value = gpu_used_mib()
        if value is not None:
            values.append(value)
    return int(statistics.median(values)) if values else None


async def nvsmi_used() -> int | None:
    """gpu_used_median() in a worker thread, so the readings never hold up the event loop (or its lag probe)."""
    return await asyncio.to_thread(gpu_used_median)


def cuda_initialized() -> bool:
    torch = sys.modules.get("torch")
    try:
        return bool(torch is not None and torch.cuda.is_initialized())
    except Exception:
        return False


def cuda_bytes(reading: str) -> int | None:
    """torch.cuda.<reading>(0) once CUDA is initialised in this process, else None (never creates a context)."""
    if not cuda_initialized():
        return None
    try:
        return int(getattr(sys.modules["torch"].cuda, reading)(0))
    except Exception:
        return None


def seed_everything(seed: int = SEED) -> None:
    """Fixed seeds for the sampling (torch is imported by the first device reading)."""
    random.seed(seed)
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        numpy.random.seed(seed)
    torch = sys.modules.get("torch")
    if torch is not None:
        torch.manual_seed(seed)


# ---- results ------------------------------------------------------------------------------------------

def plain(value: Any) -> Any:
    """JSON-ready: dataclasses, paths, tuples and sets become dicts, strings and lists."""
    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_section(path: Path, section: str, data: dict[str, Any]) -> None:
    """Merge one test's section into the results file (written through a temp file and os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current[section] = plain(data)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def lag_summary(samples: list[tuple[float, float, str]]) -> dict[str, Any]:
    """Max, p95 and count over LAG_FLAG_S of the loop lag, overall and per activity."""
    def stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
        return {"n": len(values), "max_ms": round(ordered[-1] * 1000, 1), "p95_ms": round(p95 * 1000, 1),
                "median_ms": round(statistics.median(ordered) * 1000, 1),
                "over_500ms": sum(1 for v in values if v > LAG_FLAG_S)}

    by_label: dict[str, list[float]] = {}
    for _, lag, label in samples:
        by_label.setdefault(label, []).append(lag)
    flagged = [{"t": round(t, 3), "lag_ms": round(lag * 1000, 1), "during": label}
               for t, lag, label in samples if lag > LAG_FLAG_S]
    return {"all": stats([lag for _, lag, _ in samples]),
            "by_activity": {label: stats(values) for label, values in sorted(by_label.items())},
            "flagged": flagged}


# ---- fixtures -----------------------------------------------------------------------------------------

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(autouse=True)
def startfile(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """os.startfile records its arguments instead of starting Explorer or a player."""
    started: list[tuple[str, ...]] = []

    def record(path: Any, *args: Any, **kwargs: Any) -> None:
        started.append((str(path), *map(str, args)))

    monkeypatch.setattr(os, "startfile", record, raising=False)
    return started


def _scratch_root(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    base = os.environ.get(ROOT_ENV, "").strip()
    root = (Path(base) / request.node.name.replace("[", "-").replace("]", "")) if base else tmp_path / "data"
    root = root.resolve()
    app = APP_ROOT.resolve()
    if root == app or app in root.parents:
        raise pytest.UsageError(f"{ROOT_ENV} must be a scratch folder outside {app}")
    return root


@pytest.fixture(autouse=True)
def e2e_root(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Pre-flight, a fresh scratch data root with sample.wav, and the environment gui.py reads (real engine)."""
    if sys.platform == "win32" and os.environ.get("VOICETOOL_TEST_BLOCK_PROCESSES"):
        from tests.native.winprobe import running_test_blocker

        blocker = running_test_blocker()
        if blocker:
            pytest.skip(f"GPU verification deferred: {blocker}")
    screen = full_screen_app()
    if screen is not None:
        pytest.skip(f"{FULL_SCREEN} ({screen})")
    if not engine_lock_free():
        pytest.skip(LOCK_HELD)
    if not SAMPLE.is_file():
        pytest.skip(f"{SAMPLE} is missing")
    root = _scratch_root(request, tmp_path)
    if root.exists():
        shutil.rmtree(root)
    (root / "voices" / "reference").mkdir(parents=True)
    shutil.copy2(SAMPLE, root / "voices" / "reference" / SAMPLE.name)
    monkeypatch.setenv("VOICETOOL_ENGINE", "real")
    monkeypatch.setenv("VOICETOOL_DATA_DIR", str(root))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.delenv("VOICETOOL_FAKE", raising=False)
    monkeypatch.delenv("VOICETOOL_CLOCK", raising=False)
    _install_trash(request, root, monkeypatch)
    yield root
    if os.environ.get(KEEP_ENV) != "1":
        shutil.rmtree(root / "cache", ignore_errors=True)


class RecycleRecorder:
    """send2trash (the user's Recycle Bin), with every path it was given."""

    def __init__(self) -> None:
        from send2trash import send2trash

        self._send = send2trash
        self.moved: list[Path] = []

    def __call__(self, path: Path) -> None:
        self._send(path)
        self.moved.append(Path(path))


def _install_trash(request: pytest.FixtureRequest, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The composition's trash, chosen by @pytest.mark.trash("recycle"); a scratch folder otherwise."""
    from ui import launch

    marker = request.node.get_closest_marker("trash")
    mode = str(marker.args[0]) if marker is not None and marker.args else "scratch"
    trash: Any = RecycleRecorder() if mode == "recycle" else launch.ScratchTrash(root / launch.SCRATCH_TRASH)
    monkeypatch.setattr(launch, "_make_trash", lambda flags, paths, simulation: trash)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "trash(mode): 'recycle' sends trashed files to the Recycle Bin (e2e only)")


@pytest.fixture
def results_file(tmp_path: Path) -> Path:
    named = os.environ.get(RESULTS_ENV, "").strip()
    return Path(named) if named else tmp_path / "results.json"


@pytest.fixture
async def rig(user: User, e2e_root: Path, request: pytest.FixtureRequest, results_file: Path,
              monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    """The driver and its recorders, on this test's real-engine composition; closed and reported afterwards."""
    from ui import launch

    run = launch.LAST_LAUNCH
    assert run is not None and run.paths.root == e2e_root, "gui.py did not compose this test's app"
    assert run.replay is None, "this composition runs a FakeEngine scenario, not the real engine"
    await run.ready()
    driver = Rig(user, run.core, run.engine, e2e_root, monkeypatch)
    driver.start()
    try:
        yield driver
    finally:
        failed = bool(getattr(getattr(request.node, "rep_call", None), "failed", False))
        driver.stop()
        driver.close_engine()
        section = request.node.name
        data = driver.report()
        data["outcome"] = "failed" if failed else "passed"
        write_section(results_file, section, data)
        if failed:
            print(f"\n[e2e] seed {SEED}; action trace of {section}:", file=sys.stderr)
            for line in driver.trace:
                print(f"  {line}", file=sys.stderr)
            print(f"[e2e] results: {results_file}", file=sys.stderr)


# ---- the driver ---------------------------------------------------------------------------------------

class Rig:
    """Drive the real app through markers, record what the engine does, check §8.9's invariants as it goes."""

    def __init__(self, user: User, core: Core, engine: Any, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.user = user
        self.core = core
        self.engine = engine
        self.root = root
        self.t0 = time.perf_counter()
        self.trace: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.violations: list[str] = []
        self.builds: list[dict[str, Any]] = []
        self.takes: list[dict[str, Any]] = []
        self.bakes: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.lag: list[tuple[float, float, str]] = []
        self.numbers: dict[str, Any] = {"seed": SEED}
        self.max_resident_seen = 0
        self._expect: list[str] = []
        self._stop = threading.Event()
        self._lag_task: asyncio.Task[None] | None = None
        self._sampler: threading.Thread | None = None
        self._monkeypatch = monkeypatch
        self._spy_factories()
        self._spy_facade()
        core.facade.listen(self._on_event)

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._sampler = threading.Thread(target=self._sample_residency, name="e2e residency sampler", daemon=True)
        self._sampler.start()
        self._lag_task = asyncio.get_running_loop().create_task(self._probe_lag(), name="e2e lag probe")

    def stop(self) -> None:
        self._stop.set()
        if self._lag_task is not None:
            self._lag_task.cancel()
        if self._sampler is not None:
            self._sampler.join(timeout=2)

    def close_engine(self) -> None:
        """Cancel a running take or bake, then unload everything and release the engine lock."""
        with contextlib.suppress(Exception):
            self.core.jobs.cancel_running()
        try:
            self.engine.close()
        except Exception as exc:                       # reported, never raised from a teardown
            self.violations.append(f"engine.close() failed: {type(exc).__name__}: {exc}")
        self.numbers["after_close"] = {"resident": list(self.engine.residency().resident),
                                       "lock_held": bool(self.engine.residency().lock_held),
                                       "reserved_bytes": cuda_bytes("memory_reserved"),
                                       "allocated_bytes": cuda_bytes("memory_allocated")}

    def now(self) -> float:
        return round(time.perf_counter() - self.t0, 3)

    def log(self, text: str) -> None:
        self.trace.append(f"{self.now():9.3f}  {text}")

    # ---------- recorders ----------

    def _spy_factories(self) -> None:
        """Every backend build: nothing may be resident then (the eviction runs first), allocated about 0."""
        from engine import engine as engine_module

        for name, build in list(engine_module._BACKEND_FACTORIES.items()):
            self._monkeypatch.setitem(engine_module._BACKEND_FACTORIES, name, self._watched(name, build))

    def _watched(self, name: str, build: Callable[..., Any]) -> Callable[..., Any]:
        def watched(cache_dir: Path, device: str) -> Any:
            resident = tuple(getattr(self.engine, "_backends", {}))
            allocated = cuda_bytes("memory_allocated")
            self.builds.append({"t": self.now(), "backend": name, "device": device,
                                "resident_at_build": list(resident), "allocated_before": allocated,
                                "reserved_before": cuda_bytes("memory_reserved")})
            if resident:
                self.violations.append(f"{name} was built while {resident} was resident")
            if allocated is not None and allocated >= FREED_LIMIT:
                self.violations.append(f"{name} was built with {allocated / GIB:.2f} GiB still allocated")
            return build(cache_dir, device)
        return watched

    def _spy_facade(self) -> None:
        """The job of every take and bake (tokens, made_s, conds_load_s, peak), timed from the loop."""
        facade = self.core.facade
        speak, bake = facade.speak, facade.bake

        async def timed_speak(req: Any, job: Any) -> Any:
            started = time.perf_counter()
            out = await speak(req, job)
            status = job.status()
            self.takes.append({
                "t": self.now(), "voice": req.voice_id, "backend": req.backend, "language": req.language,
                "chars": len(req.text), "status": out.status, "reason": out.reason,
                "wall_s": round(time.perf_counter() - started, 3), "made_s": _r(job.made_s),
                "sample_s": _r(job.sample_s), "decode_s": _r(job.decode_s), "tokens": status.tokens,
                "max_tokens": status.max_tokens, "hit_token_limit": status.hit_token_limit,
                "audio_s_tokens": round(status.audio_s, 3), "conds_load_s": _r(job.conds_load_s, 4),
                "peak_reserved_bytes": job.peak_reserved_bytes, "decode_steps": job.decode_steps})
            return out

        async def timed_bake(req: Any, path: Path, job: Any) -> Any:
            started = time.perf_counter()
            out = await bake(req, path, job)
            self.bakes.append({"t": self.now(), "voice": req.voice_id, "backend": req.backend, "status": out.status,
                               "reason": out.reason, "wall_s": round(time.perf_counter() - started, 3),
                               "bake_s": _r(job.bake_s)})
            return out

        self._monkeypatch.setattr(facade, "speak", timed_speak)
        self._monkeypatch.setattr(facade, "bake", timed_bake)

    def _on_event(self, event: Any) -> None:
        """On the loop, for every engine event: the expected-load and freed-memory rules."""
        record = {"t": self.now(), "kind": event.kind, "backend": event.backend, "reason": event.reason,
                  "seconds": _r(event.seconds), "device": event.device, "reserved_bytes": event.reserved_bytes,
                  "allocated_bytes": event.allocated_bytes, "freed_bytes": event.freed_bytes,
                  "peak_bytes": event.peak_bytes, "first_in_process": event.first_in_process, "error": event.error}
        self.events.append(record)
        self.log(f"event {event.kind} {event.backend} {event.reason or ''}".rstrip())
        if event.kind == "load_started":
            if not self._expect or self._expect[0] != event.backend:
                self.violations.append(f"load_started {event.backend} without a named intent (expected {self._expect})")
            else:
                self._expect.pop(0)
        elif event.kind == "unloaded":
            allocated = event.allocated_bytes
            if allocated is not None and allocated >= FREED_LIMIT:
                self.violations.append(f"unloaded {event.backend} ({event.reason}) left {allocated / GIB:.2f} GiB "
                                       "allocated: something kept the model alive")
        elif event.kind == "load_failed":
            self.violations.append(f"load_failed {event.backend}: {event.error}")

    def _sample_residency(self) -> None:
        while not self._stop.wait(0.01):
            try:
                count = max(len(self.engine.residency().resident), len(getattr(self.engine, "_backends", {})))
            except Exception:
                continue
            if count > self.max_resident_seen:
                self.max_resident_seen = count

    async def _probe_lag(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            at, label = self.now(), self.activity()
            started = loop.time()
            await asyncio.sleep(LAG_PROBE_S)
            lag = max(0.0, loop.time() - started - LAG_PROBE_S)
            if label == "idle":                         # a job that started during the sleep owns its lag
                label = self.activity()
            self.lag.append((at, lag, label))

    def activity(self) -> str:
        """What the app is doing now: detect, load, unload, take, bake, test line or idle."""
        state = self.core.state
        job = state.job
        if not state.device.detected:
            return "detect"
        if job.kind == "switch":
            return "load" if job.phase == "load" else "evict"
        if job.kind == "generate":
            return "take" if job.take_no else "test_line"
        if job.kind == "forge":
            return "bake"
        if job.kind == "unload":
            return "unload"
        return "idle"

    # ---------- invariants ----------

    def expect_load(self, backend: str) -> None:
        """The next load_started is this backend's, caused by the named control the test clicks next."""
        self._expect.append(backend)

    def check(self, where: str) -> None:
        resident = self.engine.residency().resident
        if len(resident) > 1:
            self.violations.append(f"{where}: {resident} resident at once")
        assert not self.violations, f"{where}: {self.violations}"
        assert self.max_resident_seen <= 1, f"{where}: {self.max_resident_seen} models were resident at once"

    def loads_so_far(self) -> int:
        return sum(1 for event in self.events if event["kind"] == "load_started")

    @contextlib.asynccontextmanager
    async def step(self, name: str) -> AsyncIterator[dict[str, Any]]:
        """One numbered step: timed, its lag window kept, the invariants checked at its end."""
        record: dict[str, Any] = {"step": name, "start": self.now()}
        first_lag = len(self.lag)
        self.log(f"step {name}")
        try:
            yield record
        finally:
            record["end"] = self.now()
            record["seconds"] = round(record["end"] - record["start"], 3)
            window = self.lag[first_lag:]
            record["lag_max_ms"] = round(max((lag for _, lag, _ in window), default=0.0) * 1000, 1)
            self.steps.append(record)
        self.check(name)

    # ---------- the UI ----------

    def the(self, marker: str) -> Any:
        elements = self.user.find(marker=marker).elements
        assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
        return next(iter(elements))

    def shown(self, marker: str) -> bool:
        try:
            self.user.find(marker=marker)
        except AssertionError:
            return False
        return True

    def click(self, marker: str) -> None:
        self.log(f"click {marker}")
        self.user.find(marker=marker).click()

    def type(self, marker: str, text: str) -> None:
        self.log(f"type {marker} {text[:40]!r}")
        found = self.user.find(marker=marker)
        found.clear()
        found.type(text)

    def trigger(self, marker: str, event: str, args: Any) -> None:
        self.log(f"trigger {marker} {event} {args!r}")
        self.user.find(marker=marker).trigger(event, args)

    async def until(self, predicate: Callable[[], bool], what: str, timeout: float = 10.0) -> float:
        """Wait in real time for predicate(); returns the seconds it took."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not predicate():
            if loop.time() - started > timeout:
                raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
            await asyncio.sleep(POLL_S)
        return round(loop.time() - started, 3)

    async def idle(self, what: str, timeout: float) -> float:
        """Wait until no job runs and no click task is still starting one."""
        for _ in range(5):
            await asyncio.sleep(0)
        took = await self.until(lambda: not self.core.state.job.kind, what, timeout)
        await asyncio.sleep(0.1)
        return took

    async def go(self, page: str) -> None:
        self.click(f"nav.{page}")
        await self.user.should_see(marker=f"page.{page}")
        await asyncio.sleep(0.05)

    async def open(self) -> None:
        self.log("open /")
        await self.user.open("/")
        await self.user.should_see(marker="page.speak")

    async def tour(self, where: str) -> None:
        """Every page by the rail; nothing may load (§8.9 "all")."""
        before = self.loads_so_far()
        for route in (*ROUTES[1:], ROUTES[0]):
            await self.go(PAGE_ID[route])
        await asyncio.sleep(0.2)
        assert self.loads_so_far() == before, f"{where}: navigating the six pages loaded a model"
        assert not self.core.state.job.kind, f"{where}: navigating started a job"

    def resident(self) -> tuple[str, ...]:
        return tuple(self.engine.residency().resident)

    # ---------- flows ----------

    async def load_from_card(self, backend: str) -> dict[str, Any]:
        """Models › the card's Load This Model / Switch to This Model (named by its aria-label)."""
        from ui.core import catalog

        await self.go("models")
        button = self.the(f"models.switch.{backend}")
        name = f"{getattr(button, 'label', '')} {button.props.get('aria-label', '')}"
        assert catalog.label(backend) in name, f"the card's control does not name {backend}: {name!r}"
        loads = self.loads_so_far()
        self.expect_load(backend)
        started = time.perf_counter()
        self.click(f"models.switch.{backend}")
        await self.until(lambda: self.loads_so_far() > loads, f"{backend} to start loading", 10)
        await self.idle(f"{backend} to load", LOAD_TIMEOUT_S)
        wall = round(time.perf_counter() - started, 3)
        assert self.resident() == (backend,), f"after the card click {self.resident()} is resident"
        assert self.core.state.engine.resident == backend
        loaded = [e for e in self.events if e["kind"] == "loaded" and e["backend"] == backend][-1]
        return {"backend": backend, "wall_s": wall, **loaded}

    async def forge(self, name: str, backend: str, *, clip: Path | None = None) -> dict[str, Any]:
        """Forge: drop the clip, name it, pick the model, consent, Forge (named when it switches)."""
        from ui.core import catalog

        core = self.core
        await self.go("forge")
        if self.shown("forge.another"):
            self.click("forge.another")
            await asyncio.sleep(0.05)
        clip = clip or self.root / "voices" / "reference" / SAMPLE.name
        self.log(f"drop {clip.name}")
        core.events.dropped.emit((str(clip),))
        await self.until(lambda: core.forge_draft.clip_state == "ready", f"{clip.name} to stage", 30)
        self.type("forge.name", name)
        self.click(f"forge.backend.{backend}")
        await asyncio.sleep(0.05)
        if not core.forge_draft.consent:
            self.click("forge.consent")
        await self.until(lambda: core.forge_draft.consent and core.forge_draft.name == name
                         and core.forge_draft.backend == backend, "the forge draft", 5)
        label = self.the("forge.go").label
        switching = self.resident() != (backend,)
        expected = f"Switch to {catalog.label(backend)} & Forge" if switching else "Forge Voice"
        assert label == expected, f"Forge reads {label!r}, expected {expected!r}"
        if switching:
            self.expect_load(backend)
        bakes = len(self.bakes)
        started = time.perf_counter()
        self.click("forge.go")
        await self.until(lambda: core.state.job.kind or len(self.bakes) > bakes, f"forging {name} to start", 10)
        await self.idle(f"forging {name}", LOAD_TIMEOUT_S + BAKE_TIMEOUT_S)
        await self.until(lambda: any(row.voice_id == name for row in core.state.library.rows),
                         f"{name} in the library", 20)
        assert len(self.bakes) == bakes + 1 and self.bakes[-1]["status"] == "done", self.bakes[-1:]
        return {"voice": name, "backend": backend, "switched": switching,
                "wall_s": round(time.perf_counter() - started, 3), **self.bakes[-1]}

    async def take(self, voice: str, text: str, *, guidance: float | None = None, japanese: bool = False,
                   timeout: float = TAKE_TIMEOUT_S) -> dict[str, Any]:
        """Speak: pick the voice, type the script, set the delivery, Generate on the resident model."""
        core = self.core
        await self.go("speak")
        self.click(f"speak.voice.{voice}")
        await self.until(lambda: core.session.voice_id == voice, f"{voice} to be picked", 5)
        self.type("speak.text", text)
        await self.until(lambda: core.session.text == text, "the script", 5)
        if japanese:
            await self.user.should_see(marker="speak.use_ja")
            self.click("speak.use_ja")
            await self.until(lambda: core.session.language == "ja", "Use Japanese", 5)
        if guidance is not None:
            self.trigger("speak.guid", "change", guidance)
            await self.until(lambda: abs(core.session.cfg_weight - guidance) < 1e-9, "the Guidance", 5)
        label = self.the("speak.generate").label
        assert label == "Generate", f"Generate reads {label!r} (the voice's model should be resident)"
        take_id = core.takes.next_id
        loads = self.loads_so_far()
        takes = len(self.takes)
        self.click("speak.generate")
        await self.until(lambda: core.state.job.kind == "generate" or len(self.takes) > takes,
                         f"take {take_id} to start", 10)
        await self.idle(f"take {take_id}", timeout)
        assert self.loads_so_far() == loads, "Generate on the resident model loaded a model"
        assert len(self.takes) == takes + 1, "no take ran"
        job = self.takes[-1]
        assert job["status"] == "done", f"take {take_id} {job['status']}: {job['reason']}"
        await self.until(lambda: any(t.id == take_id for t in core.takes.takes), f"take {take_id} in the index", 10)
        record = next(t for t in core.takes.takes if t.id == take_id)
        return {"take_id": take_id, "seconds": record.seconds, "gen_seconds": record.gen_seconds,
                "tokens": record.tokens, "cfg_weight": record.cfg_weight, "language": record.language,
                "rtf": round(record.gen_seconds / record.seconds, 4) if record.seconds else None, **job}

    async def star(self, take_id: int) -> None:
        await self.go("takes")
        self.click(f"takes.star.{take_id}")
        await self.until(lambda: any(t.id == take_id and t.starred for t in self.core.takes.takes),
                         f"take {take_id} starred", 10)

    async def purge_unstarred(self) -> list[int]:
        """Takes › Remove Unstarred: the first click arms, the second within 3 s removes."""
        core = self.core
        await self.go("takes")
        gone = [t.id for t in core.takes.takes if not t.starred]
        self.click("takes.purge")
        await asyncio.sleep(0.05)
        assert self.the("takes.purge").label.startswith("Click Again to Remove"), self.the("takes.purge").label
        assert all(any(t.id == i for t in core.takes.takes) for i in gone), "the first click removed takes"
        await asyncio.sleep(0.6)
        self.click("takes.purge")
        await self.until(lambda: not any(t.id in gone for t in core.takes.takes), "the unstarred takes to go", 30)
        return gone

    async def delete_voice(self, voice: str) -> None:
        """Voices › the row, Delete (arms), Delete again."""
        core = self.core
        await self.go("voices")
        self.click(f"voices.row.{voice}")
        await asyncio.sleep(0.05)
        self.click("voices.delete")
        await asyncio.sleep(0.05)
        assert self.the("voices.delete").label == "Click Again to Delete", self.the("voices.delete").label
        assert any(row.voice_id == voice for row in core.state.library.rows), "the first click deleted"
        await asyncio.sleep(0.6)
        self.click("voices.delete")
        await self.until(lambda: not any(row.voice_id == voice for row in core.state.library.rows),
                         f"{voice} to be deleted", 30)

    async def free(self, where: str = "dock.free") -> dict[str, Any]:
        """Free VRAM (the dock's, or the Models card's models.free)."""
        resident = self.resident()
        unloads = sum(1 for e in self.events if e["kind"] == "unloaded")
        if where == "models.free":
            await self.go("models")
        self.click(where)
        await self.until(lambda: sum(1 for e in self.events if e["kind"] == "unloaded") > unloads,
                         "the unload", 60)
        await self.idle("Free VRAM", 60)
        assert self.resident() == (), f"{self.resident()} still resident after Free VRAM"
        event = [e for e in self.events if e["kind"] == "unloaded"][-1]
        return {"was": list(resident), **event, "reserved_after": cuda_bytes("memory_reserved"),
                "allocated_after": cuda_bytes("memory_allocated")}

    # ---------- the report ----------

    def report(self) -> dict[str, Any]:
        return {
            "numbers": self.numbers,
            "steps": self.steps,
            "events": self.events,
            "builds": self.builds,
            "takes": self.takes,
            "bakes": self.bakes,
            "loop_lag": lag_summary(self.lag),
            "max_resident_seen": self.max_resident_seen,
            "violations": self.violations,
            "trace": self.trace,
            "log": list(self.core.state.log.lines),
        }


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)
