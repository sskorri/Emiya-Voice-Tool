"""R2 · lock contention between the GUI and cli.py, with the real VoiceEngine on a scratch config.

The GUI is the real gui.py composition under NiceGUI's user fixture, with VOICETOOL_ENGINE=real: a VoiceEngine
(fake factories, DEVICE "cpu", the engine lock in the scratch data root's cache) instead of the FakeEngine. GPU
readings and weights sizes are stubbed, so nothing imports torch or reads the Hugging Face cache; no model loads.

1. cli.py holds the engine (a real `cli.py load` child whose fake model build waits for the test's go-ahead, so
   it holds cache\\engine.lock as program "cli.py"): the GUI's named load (the Models card "Load This Model")
   shows the EngineBusy copy as a red toast and its "Load refused" log line, leaves no failed state, and every
   page, the chip, the dock and Generate stay usable. Once cli.py finishes and exits, the same card loads the
   model, and the voice cli.py registered shows up in the library.
2. The reverse: while the GUI holds a model, `cli.py speak` and `cli.py load` exit 1 with the one-line
   EngineBusy message naming gui.py (list, info and backends still work); after Free VRAM, cli.py speaks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from nicegui.testing import User

from engine import engine as engine_module
from engine.device import DeviceInfo
from engine.engine import VoiceEngine
from engine.errors import EngineBusy
from engine.lock import EngineLock
from engine.registry import VoiceRegistry
from ui import launch
from ui.contract.routes import PAGE_ID
from ui.core import Core
from ui.core.clock import CLOCK_ENV, ManualScheduler
from ui.core.facade import EventRelay

from tests.engine.conftest import install_fake_factories, write_clip
from tests.engine.test_cli_messages import run_cli
from tests.pages.test_wiring import behaviours_of, install_kit, until

APP_ROOT = Path(__file__).resolve().parents[2]
MTL = "chatterbox-multilingual"
TIMEOUT_S = 60.0

# `cli.py <args>` on the scratch root with the fake factories, as test_compat runs it, except that every model
# build announces LOADING and then waits for argv[2] (a file the test creates) before it builds: meanwhile this
# process holds the engine lock, exactly as a cli.py loading a real model does.
BLOCKING_CLI = r"""
import sys, time
from pathlib import Path

import pytest

import config

root, release = Path(sys.argv[1]), Path(sys.argv[2])
config.ROOT = root
config.CACHE_DIR = root / "cache"
config.VOICES_DIR = root / "voices" / "reference"
config.OUTPUTS_DIR = root / "outputs"
config.DEVICE = "cpu"

from engine import engine as engine_module
from tests.engine.conftest import install_fake_factories

install_fake_factories(pytest.MonkeyPatch())


def waiting(build):
    def build_when_released(cache_dir, device):
        print("LOADING", flush=True)
        deadline = time.monotonic() + 60
        while not release.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("the test never released the build")
            time.sleep(0.02)
        return build(cache_dir, device)
    return build_when_released


for name in list(engine_module._BACKEND_FACTORIES):
    engine_module._BACKEND_FACTORIES[name] = waiting(engine_module._BACKEND_FACTORIES[name])

import cli

sys.argv = ["cli.py", *sys.argv[3:]]
cli.main()
"""


def cpu_reading(probe: bool = False) -> DeviceInfo:
    """No CUDA GPU (and no torch import): models run on the CPU."""
    return DeviceInfo(initialized=False, cuda_available=False if probe else None, name=None, total_bytes=None,
                      free_bytes=None, reserved_bytes=0, allocated_bytes=0, other_bytes=None, peak_reserved_bytes=0,
                      torch_version="", cuda_version=None, error=None, measured_at=time.monotonic())


def seed(root: Path) -> None:
    """aiko on Multilingual (its reference clip and a fake-factory .pt), and Run Models On = CPU."""
    clip = write_clip(root / "voices" / "reference" / "aiko_ref.wav", 6.0)
    pt = root / "cache" / MTL / "aiko.pt"
    pt.parent.mkdir(parents=True, exist_ok=True)
    pt.write_text(json.dumps({"voice_id": "aiko", "exaggeration": 0.5}), encoding="utf-8")
    VoiceRegistry(root / "cache" / "registry.json").register("aiko", MTL, str(clip), 0.5, "en",
                                                             meta={"consent": True})
    (root / "outputs" / "takes").mkdir(parents=True, exist_ok=True)
    (root / "ui_settings.json").write_text(json.dumps({"version": 1, "core": {"device": "cpu"}}), encoding="utf-8")


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The environment gui.py reads under the user fixture, for the real engine on a scratch root (list it before
    `user`): VoiceEngine with fake factories and its engine lock in <root>\\cache, no torch, no Recycle Bin."""
    root = tmp_path / "data"
    seed(root)
    monkeypatch.setenv("VOICETOOL_DATA_DIR", str(root))
    monkeypatch.setenv("VOICETOOL_ENGINE", "real")
    monkeypatch.delenv("VOICETOOL_FAKE", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv(CLOCK_ENV, "manual")
    fakes = install_fake_factories(monkeypatch)
    monkeypatch.setattr(engine_module, "_weights_status", lambda name: (True, 3_000_000_000))
    monkeypatch.setattr(engine_module, "_device_info", cpu_reading)

    def make_engine(flags: Any, paths: Any, *, simulation: bool) -> tuple[Any, Any, Any]:
        config = launch.engine_config(paths)
        assert paths.root == root and config.CACHE_DIR == root / "cache"      # never the install folder's data
        config.DEVICE = "cpu"
        relay = EventRelay()
        return VoiceEngine(config, on_event=relay, lock_path=paths.cache / "engine.lock"), relay, None

    monkeypatch.setattr(launch, "_make_engine", make_engine)
    monkeypatch.setattr(launch, "_make_trash", lambda flags, paths, simulation: launch.ScratchTrash(
        paths.root / launch.SCRATCH_TRASH))
    monkeypatch.setattr(os, "startfile", lambda *args, **kwargs: None, raising=False)
    yield root
    fakes.release_all()


@pytest.fixture
async def core(user: User, scratch: Path) -> AsyncIterator[Core]:
    run = launch.LAST_LAUNCH
    assert run is not None and run.paths.root == scratch and isinstance(run.engine, VoiceEngine)
    await run.ready()
    yield run.core
    run.engine.close()                                 # never leave the scratch engine lock held


async def settled(core: Core) -> None:
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    for _ in range(5):
        await asyncio.sleep(0)
    await core.jobs.wait_idle()
    await clock.settle()
    for _ in range(5):
        await asyncio.sleep(0)


async def go(user: User, core: Core, route: str) -> None:
    user.find(marker=f"nav.{PAGE_ID[route]}").click()
    await user.should_see(marker=f"page.{PAGE_ID[route]}")
    await settled(core)


def toasts(core: Core) -> list[tuple[str, bool]]:
    seen: list[tuple[str, bool]] = []
    core.events.toast.subscribe(lambda toast: seen.append((toast.text, toast.bad)))
    return seen


def lines(core: Core) -> list[str]:
    return [line.split("  ", 1)[1] for line in core.state.log.lines]


def spawn_blocking_cli(root: Path, release: Path, *args: str, err: Any) -> subprocess.Popen[bytes]:
    """The blocking cli.py runner as one process (the venv redirector would put the real one a level down)."""
    env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    executable = getattr(sys, "_base_executable", sys.executable)
    if os.path.normcase(executable) != os.path.normcase(sys.executable):
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return subprocess.Popen([executable, "-c", BLOCKING_CLI, str(root), str(release), *args], cwd=APP_ROOT,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def assert_idle_and_usable(user: User, core: Core, *, chip: str, resident: str) -> None:
    job = core.state.job
    assert (job.kind, core.jobs.queued, core.state.engine.failed, core.state.engine.resident) == ("", (), "", resident)
    status = behaviours_of(user).status().vm
    assert status.chip_text == chip and not status.show_failed_actions
    assert all(row.enabled for row in status.dock_rows)


async def test_the_gui_refuses_to_load_while_cli_holds_the_engine_and_loads_once_it_exits(
        scratch: Path, user: User, core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    install_kit(monkeypatch, tmp_path)
    seen = toasts(core)
    await user.open("/models")
    await settled(core)
    await until(lambda: core.state.device.detected, "the first device reading")
    release = tmp_path / "release-cli"
    lock_path = scratch / "cache" / "engine.lock"
    with open(tmp_path / "cli.err", "wb") as err:
        proc = spawn_blocking_cli(scratch, release, "load", "rin", str(scratch / "voices" / "reference" /
                                                                       "aiko_ref.wav"), err=err)
        try:
            line = await asyncio.to_thread(proc.stdout.readline)
            assert line.strip() == b"LOADING", (tmp_path / "cli.err").read_text("utf-8", "replace")
            holder = EngineLock(lock_path).holder()
            assert holder is not None and holder["program"] == "cli.py" and holder["pid"] == proc.pid, holder
            busy = str(EngineBusy(holder))
            card = user.find(marker=f"models.switch.{MTL}")
            assert next(iter(card.elements)).props.get("aria-label") == "Load Multilingual"   # named: no ask
            card.click()
            await settled(core)
            assert seen[-1:] == [(busy, True)], seen
            assert f"Load refused: {MTL} · cli.py (pid {proc.pid}) holds the engine" in lines(core)
            assert_idle_and_usable(user, core, chip="No model loaded", resident="")
            assert behaviours_of(user).status().vm.dock_status == "No model loaded"
            for route in ("/voices", "/forge", "/takes", "/settings", "/models", "/"):
                await go(user, core, route)
            user.find(marker="speak.voice.aiko").click()
            behaviours_of(user).speak().set_text("Hello there.")
            await settled(core)
            generate = behaviours_of(user).speak().vm.generate
            assert generate.enabled and generate.label.startswith("Load Multilingual & Generate"), generate
            assert proc.poll() is None                                   # cli.py still holds the engine
        finally:
            release.write_text("go", encoding="utf-8")
            out, _ = await asyncio.to_thread(proc.communicate, timeout=TIMEOUT_S)
    assert proc.returncode == 0, (tmp_path / "cli.err").read_text("utf-8", "replace")
    assert b"Registered 'rin' on backend 'chatterbox-multilingual'" in out
    lock = EngineLock(lock_path, program="pytest")
    lock.acquire()                                                 # cli.py exited: nobody holds the engine
    lock.release()
    await go(user, core, "/models")
    user.find(marker=f"models.switch.{MTL}").click()
    await settled(core)
    assert core.state.engine.resident == MTL and seen[-1][0].startswith("Multilingual ready · "), seen
    assert_idle_and_usable(user, core, chip="Multilingual ready · CPU", resident=MTL)
    assert EngineLock(lock_path).holder()["pid"] == os.getpid()
    await go(user, core, "/")
    user.find(marker="speak.generate").click()
    await settled(core)
    assert core.takes.get(1) is not None and seen[-1][0].startswith("Take 1 ready · "), seen
    await go(user, core, "/voices")                                # registry.json changed: a new snapshot
    assert {row.voice_id for row in core.state.library.rows} == {"aiko", "rin"}
    assert not [r for r in caplog.get_records("call") if r.levelno >= logging.ERROR or r.exc_info]


async def test_cli_gets_the_one_line_busy_message_while_the_gui_holds_a_model(
        scratch: Path, user: User, core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    install_kit(monkeypatch, tmp_path)
    seen = toasts(core)
    await user.open("/models")
    await settled(core)
    await until(lambda: core.state.device.detected, "the first device reading")
    user.find(marker=f"models.switch.{MTL}").click()
    await settled(core)
    assert core.state.engine.resident == MTL, seen
    holder = EngineLock(scratch / "cache" / "engine.lock").holder()
    assert holder is not None and holder["program"] == "gui.py" and holder["pid"] == os.getpid(), holder
    busy = str(EngineBusy(holder))
    target = scratch / "outputs" / "cli.wav"
    for args in (("speak", "aiko", "Hello there.", "--out", str(target)),
                 ("load", "rin", str(scratch / "voices" / "reference" / "aiko_ref.wav"))):
        code, out, err, builds = await asyncio.to_thread(run_cli, scratch, *args)
        assert (code, out, err, builds) == (1, "", busy + "\n", []), (args, code, out, err)
    assert not target.exists() and VoiceRegistry(scratch / "cache" / "registry.json").list_ids() == ["aiko"]
    for args in (("list",), ("info", "aiko"), ("backends",)):
        code, out, err, builds = await asyncio.to_thread(run_cli, scratch, *args)
        assert (code, err, builds) == (0, "", []), (args, err)
    assert core.state.engine.resident == MTL and not core.state.job.kind        # the GUI never noticed
    user.find(marker="dock.free").click()
    await settled(core)
    assert core.state.engine.resident == "" and seen[-1] == ("Unloaded Multilingual", False), seen
    code, out, err, builds = await asyncio.to_thread(run_cli, scratch, "speak", "aiko", "Hello there.", "--out",
                                                     str(target))
    assert (code, err, builds) == (0, "", [[MTL, "cpu"]]) and target.read_bytes().startswith(b"RIFF"), err
    assert not [r for r in caplog.get_records("call") if r.levelno >= logging.ERROR or r.exc_info]
