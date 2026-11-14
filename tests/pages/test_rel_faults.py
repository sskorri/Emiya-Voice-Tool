"""R2 · faults through the real controls: engine failures, damaged and missing data, a full or read-only disk.

Every test runs gui.py under NiceGUI's user fixture on a FakeEngine scenario in tmp_path (tests\\pages\\conftest).

1. Engine faults. FakeEngine.fail_next arms one failure for an engine call the GUI makes (make_resident, speak,
   load_voice, unload_backend, device_info, update_voice, delete_voice; also voice_details), with each error type
   the engine raises: BackendLoadError (out_of_memory, missing_files, other), GenerationError (out_of_memory,
   other), EngineBusy, BackendNotResident, Cancelled and a fresh ValueError. The control that reaches the call is
   clicked like a user clicks it (the Models card, Generate, Forge, the dock, the Voices editor and Delete, the
   test line, a chain's second take, a named switch with its after-action, the warm-up); then the toast and log
   copy must match §4.3 and the §4.8 table, the chip, the dock, the Generate and Forge labels and JobState must be
   usable again, the same action must then succeed, and no ERROR log, no swallowed job failure and no traceback
   may appear. One error kind per outcome class (failed, refused, cancelled) runs by default; the rest of the
   matrix is marked soak.
2. Damaged and missing data, at startup (a hook damages the seeded scratch root right after FakeEngine seeds it,
   before the core reads anything) and mid-session: registry.json, ui_settings.json, outputs\\takes\\index.json,
   cache\\load_history.jsonl, a .pt or a reference clip deleted while the app runs, a take WAV deleted while it
   plays or is pinned, a read-only data folder and a full disk (writes that store half their data, then raise
   OSError ENOSPC). The app must start, show the documented state or toast, never crash, and never leave a
   half-written file.
3. A seeded monkey clicks every kind of control, moves the manual clock and arms random engine failures (a short
   run by default, ten long ones under soak).

Seeded audio is hard-linked from a shared cache (ui.core.fake): a test never writes into a seeded audio file in
place (it unlinks first) and never changes its attributes. A damage spec that exposes a real app bug can be
marked xfail(strict=True, reason=<bug id>) through damages(bugs=...); the six R2 bugs (R2-01 to R2-06) are fixed
and their specs run as normal tests.
"""
from __future__ import annotations

import asyncio
import builtins
import errno
import io
import json
import logging
import os
import random
import re
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from nicegui.element import Element
from nicegui.elements.mixins.disableable_element import DisableableElement
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from engine.errors import BackendLoadError, BackendNotResident, Cancelled, EngineBusy, GenerationError, first_line
from ui.behaviours.voices import VoicesController
from ui.contract.plans import Toast
from ui.contract.records import Take
from ui.contract.routes import PAGE_ID
from ui.core import Core, catalog
from ui.core import fake as fake_module
from ui.core.fake import GIB, GPU_TOTAL_BYTES, SCENARIOS, FakeEngine
from ui.core.services import settings as settings_module
from ui.core.services import takes_store as takes_module
from ui.core.services.load_history import LoadHistory

from tests.pages.test_wiring import behaviours_of, install_kit, marked, settle, until

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
SEED = 20261113                                   # every random choice in this module comes from this seed
BUSY_HOLDER = {"pid": 4242, "program": "cli.py", "since": "14:02:11"}
BUSY_TEXT = ("The voice engine is in use by cli.py (pid 4242) since 14:02:11. "
             "Close it, or wait for it to finish, then try again.")
OOM = "CUDA out of memory"
MISSING = "model files are not in the local cache (Offline Mode is on)"   # HF_HUB_OFFLINE=1 in every test
BUSY_LABELS = ("Loading", "Unloading", "Generating", "Forging", "Baking", "Busy", "Deleting")
SWALLOWED = re.compile(r"ended unexpectedly|background task failed|timer callback failed|listener failed|"
                       r"idle hook failed|callback failed|could not be applied|failed to build|Startup failed|"
                       r"roll back", re.IGNORECASE)


# ---- the error kinds (§3.1) and the copy the UI must show for each (§4.3) -------------------------------

def _load_oom(backend: str) -> BaseException:
    return BackendLoadError(backend, "out_of_memory", "CUDA out of memory. Tried to allocate 512.00 MiB", 6.1)


def _load_missing(backend: str) -> BaseException:
    return BackendLoadError(backend, "missing_files", "t3_turbo_v1.safetensors is not in the local cache", 0.4)


def _load_other(backend: str) -> BaseException:
    return BackendLoadError(backend, "other", "the model failed its self-check", 3.2)


def _gen_oom(backend: str) -> BaseException:
    return GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate 64.00 MiB", during="speak")


def _gen_other(backend: str) -> BaseException:
    return GenerationError("other", "probability tensor contains either inf, nan or element < 0", during="speak")


def _busy(backend: str) -> BaseException:
    return EngineBusy(dict(BUSY_HOLDER))


def _not_resident(backend: str) -> BaseException:
    return BackendNotResident(backend, ())


def _cancelled(backend: str) -> BaseException:
    return Cancelled()


def _value(backend: str) -> BaseException:
    return ValueError("Voice 'aiko' is not registered. Call load_voice() first.")


KINDS: dict[str, Callable[[str], BaseException]] = {
    "load_oom": _load_oom, "load_missing": _load_missing, "load_other": _load_other, "gen_oom": _gen_oom,
    "gen_other": _gen_other, "busy": _busy, "not_resident": _not_resident, "cancelled": _cancelled, "value": _value,
}


def reason(kind: str, backend: str) -> str:
    """What describe_error() must make of the error (§4.3); "" for Cancelled (no toast)."""
    return {
        "load_oom": OOM, "load_missing": MISSING, "load_other": "the model failed its self-check", "gen_oom": OOM,
        "gen_other": "probability tensor contains either inf, nan or element < 0", "busy": BUSY_TEXT,
        "not_resident": f"{catalog.label(backend)} is not loaded", "cancelled": "",
        "value": "Voice 'aiko' is not registered. Call load_voice() first.",
    }[kind]


def load_reason(kind: str, backend: str) -> str:
    """A failed build: the engine (and FakeEngine) turn any other error into BackendLoadError(kind, first line)."""
    if kind in ("load_oom", "load_missing", "load_other", "gen_oom", "gen_other", "busy"):
        return reason(kind, backend)
    return first_line(str(KINDS[kind](backend))) or type(KINDS[kind](backend)).__name__


# ---- probes shared by every test ---------------------------------------------------------------------

@dataclass
class Probe:
    """The test's action trace (printed as it goes, so a failure shows it) and what the app said."""

    name: str
    toasts: list[Toast] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def step(self, text: str) -> None:
        self.steps.append(text)
        print(f"[R2 {self.name} seed={SEED}] {text}")

    def toast_texts(self) -> list[tuple[str, bool]]:
        return [(toast.text, toast.bad) for toast in self.toasts]

    def where(self) -> str:
        return f"(seed {SEED}; trace: {' -> '.join(self.steps)})"


@pytest.fixture
def probe(request: pytest.FixtureRequest) -> Probe:
    return Probe(request.node.name)


def watch(core: Core, probe: Probe) -> None:
    core.events.toast.subscribe(probe.toasts.append)


def log_lines(core: Core) -> list[str]:
    """The Log panel's lines without their "HH:MM:SS  " stamp."""
    return [line.split("  ", 1)[1] if "  " in line else line for line in core.state.log.lines]


def visible_toast(user: User) -> str | None:
    texts = [element.text for element in marked(user, "shell.toast") if element.visible]
    assert len(texts) <= 1
    return texts[0] if texts else None


def click(user: User, marker: str) -> None:
    user.find(marker=marker).click()


def finishes(monkeypatch: pytest.MonkeyPatch, cls: type, name: str) -> list[str]:
    """Record each finished call of an async intent (install before the page binds its handlers).

    Intents such as VoicesController.edit run as client tasks, outside core.scheduler, so settle() alone does
    not wait for them.
    """
    done: list[str] = []
    original = getattr(cls, name)

    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original(self, *args, **kwargs)
        finally:
            done.append(name)

    monkeypatch.setattr(cls, name, wrapped)
    return done


async def finished(done: list[str], count: int, core: Core, fake: FakeEngine) -> None:
    await until(lambda: len(done) >= count, f"{count} finished intent(s)")
    await settle(core, fake)


async def opened(user: User, core: Core, fake: FakeEngine, open_route: Any, route: str) -> None:
    await open_route(user, route)
    await settle(core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    await settle(core, fake)


def assert_clean(caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str], probe: Probe) -> None:
    """No ERROR record, no record with a traceback, no swallowed job failure and no traceback on stdout or stderr."""
    records = caplog.get_records("call")
    errors = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
    with_trace = [r.getMessage() for r in records if r.exc_info]
    swallowed = [r.getMessage() for r in records if SWALLOWED.search(r.getMessage())]
    out, err = capfd.readouterr()
    print(out, end="")                              # keep the trace in pytest's report
    assert not errors, f"ERROR logs: {errors} {probe.where()}"
    assert not with_trace, f"log records with a traceback: {with_trace} {probe.where()}"
    assert not swallowed, f"a job or handler failed and was only logged: {swallowed} {probe.where()}"
    assert "Traceback" not in out and "Traceback" not in err, f"a traceback was printed:\n{err}\n{probe.where()}"


def assert_usable(user: User, core: Core, fake: FakeEngine, probe: Probe, *, chip: str, dock: str,
                  resident: str, failed: str = "") -> None:
    """The job slot is free and every busy label is gone: the app takes the next click."""
    job = core.state.job
    assert (job.kind, job.phase, job.queued) == ("", "", 0), f"JobState is not idle: {job} {probe.where()}"
    assert core.jobs.task is None and core.jobs.queued == (), probe.where()
    assert core.state.engine.resident == resident and core.state.engine.failed == failed, probe.where()
    assert fake.residency().resident == ((resident,) if resident else ()), probe.where()
    behaviours = behaviours_of(user)
    status = behaviours.status().vm
    assert (status.chip_text, status.dock_status) == (chip, dock), probe.where()
    generate = behaviours.speak().vm.generate
    forge = behaviours.forge().vm.forge
    for what, action in (("Generate", generate), ("Forge", forge)):
        assert not action.label.startswith(BUSY_LABELS), f"{what} still reads {action.label!r} {probe.where()}"
    assert all(row.enabled for row in status.dock_rows), f"dock rows still disabled {probe.where()}"
    assert status.free.enabled == bool(resident), probe.where()


# ---- 1. engine faults through the UI --------------------------------------------------------------------

def _params(*, busy_scenario: str, scenario: str, default: tuple[str, ...], kinds: tuple[str, ...] = ()) -> list[Any]:
    """One param per error kind. The `default` kinds (one per outcome class: failed, refused, cancelled) run in the
    default suite; the rest of the matrix is marked soak (`-m soak`), which keeps this package within its budget."""
    params = []
    for kind in kinds or tuple(KINDS):
        marks = [pytest.mark.scenario(busy_scenario if kind == "busy" else scenario)]
        if kind not in default:
            marks.append(pytest.mark.soak)
        params.append(pytest.param(kind, marks=marks, id=kind))
    return params


@pytest.mark.parametrize("kind", _params(busy_scenario="relaunch", scenario="models",
                                         default=("load_oom", "load_missing", "busy")))
async def test_a_failing_model_load_from_the_models_card(kind: str, user: User, core: Core, fake: FakeEngine,
                                                        open_route: Any, tmp_path: Path, probe: Probe,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        caplog: pytest.LogCaptureFixture,
                                                        capfd: pytest.CaptureFixture[str]) -> None:
    """make_resident fails (§4.8 switch rows): the failed-load copy, or the EngineBusy copy with no failed state;
    the card loads Turbo on the next click. EngineBusy can only happen while nothing is resident (the lock is ours
    while a model is), so that case starts from `relaunch`."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/models")
    start = core.state.engine.resident
    fake.reset_records()
    fake.fail_next("make_resident", KINDS[kind](TUR))
    probe.step(f"armed make_resident {kind}; resident {start or 'none'}; click models.switch.{TUR}")
    click(user, f"models.switch.{TUR}")
    await settle(core, fake)
    assert fake.armed_failures("make_resident") == (), f"the armed failure never fired {probe.where()}"
    text = load_reason(kind, TUR)
    lines = log_lines(core)
    if kind == "busy":
        assert fake.loads == [], probe.where()
        assert probe.toast_texts()[-1:] == [(BUSY_TEXT, True)], probe.toast_texts()
        assert "Load refused: chatterbox-turbo · cli.py (pid 4242) holds the engine" in lines, lines
        assert not any(line.startswith("Load failed") for line in lines), lines
        assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
        assert not behaviours_of(user).status().vm.show_failed_actions
    else:
        free = f" ({(GPU_TOTAL_BYTES - round(SCENARIOS['models'].other_gb * GIB)) / GIB:.1f} GB free)"
        expected = f"Turbo failed to load: {text}" + (free if text == OOM else "")
        assert probe.toast_texts()[-1:] == [(expected, True)], f"{probe.toast_texts()} {probe.where()}"
        assert visible_toast(user) == expected
        assert f"Load failed: chatterbox-turbo · {text}" in lines, lines
        assert_usable(user, core, fake, probe, chip="Turbo failed to load", dock="Load failed · see Log",
                      resident="", failed=TUR)
        status = behaviours_of(user).status().vm
        assert status.show_failed_actions and status.retry.aria == "Retry loading Turbo" and status.retry.enabled
        assert status.load_default.label == "Load Multilingual" and status.load_default.enabled
        assert core.state.history.events[-1].kind == "load_failed"
        assert core.state.history.events[-1].reason == text
    assert behaviours_of(user).speak().vm.generate.label.startswith(("Load Multilingual & Generate", "Type a Script"))
    probe.step("retry: click the same card")
    click(user, f"models.switch.{TUR}")
    await settle(core, fake)
    assert fake.loads[-1:] == [TUR] and core.state.engine.resident == TUR, probe.where()
    assert probe.toast_texts()[-1][0].startswith("Turbo ready · "), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="Turbo ready · 3.1 GB", dock="Ready", resident=TUR)
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="speak", scenario="speak",
                                         default=("gen_oom", "busy", "cancelled")))
async def test_a_failing_take_from_generate(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                           tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture,
                                           capfd: pytest.CaptureFixture[str]) -> None:
    """speak fails at its first sampling step (§4.8 "speak failed"): "Take 8 failed: <reason>", nothing saved, the
    model stays resident; Cancelled ends the take silently. Generate then makes take 8."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert core.takes.next_id == 8
    wav = core.paths.takes_default / "aiko_take8.wav"
    fake.reset_records()
    fake.fail_next("speak", KINDS[kind](MTL))
    probe.step(f"armed speak {kind}; click speak.generate")
    click(user, "speak.generate")
    await settle(core, fake)
    assert fake.armed_failures("speak") == (), probe.where()
    text = reason(kind, MTL)
    lines = log_lines(core)
    if kind == "cancelled":
        assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
        assert not any(line.startswith("Take 8 failed") for line in lines), lines
    else:
        assert probe.toast_texts()[-1:] == [(f"Take 8 failed: {text}", True)], probe.toast_texts()
        assert visible_toast(user) == f"Take 8 failed: {text}"
        assert f"Take 8 failed · {text}" in lines, lines
    assert core.takes.get(8) is None and core.takes.next_id == 8 and not wav.exists(), probe.where()
    assert core.session.take_id == 7
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    generate = behaviours_of(user).speak().vm.generate
    assert (generate.label, generate.enabled) == ("Generate", True), probe.where()
    probe.step("retry: click speak.generate")
    click(user, "speak.generate")
    await settle(core, fake)
    take = core.takes.get(8)
    assert take is not None and wav.is_file() and core.session.take_id == 8, probe.where()
    assert probe.toast_texts()[-1] == (f"Take 8 ready · {take.seconds:.1f} s", False)
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="forge_ready", scenario="forge_ready",
                                         default=("gen_other", "not_resident", "cancelled")))
async def test_a_failing_bake_from_forge(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                        tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                        caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """load_voice fails in its bake (§4.8 "commit or bake failed"): "Forge failed: <reason>", the clip committed
    for it is rolled back, no entry and no .pt; Forge Voice then forges rin."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/forge")
    ref = core.paths.voices / "rin_ref.wav"
    pt = core.paths.cache / MTL / "rin.pt"
    assert behaviours_of(user).forge().vm.forge.label == "Forge Voice"
    fake.reset_records()
    fake.fail_next("load_voice", KINDS[kind](MTL))
    probe.step(f"armed load_voice {kind}; click forge.go")
    click(user, "forge.go")
    await settle(core, fake)
    assert fake.armed_failures("load_voice") == (), probe.where()
    text = reason(kind, MTL)
    lines = log_lines(core)
    if kind == "cancelled":
        assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
    else:
        assert probe.toast_texts()[-1:] == [(f"Forge failed: {text}", True)], probe.toast_texts()
        assert f"Forge failed: rin · {text}" in lines, lines
    assert not ref.exists() and not pt.exists() and fake.registry.get("rin") is None, probe.where()
    draft = core.forge_draft
    assert draft.clip is not None and draft.clip_state == "ready" and Path(draft.clip.staged).is_file()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    forge = behaviours_of(user).forge().vm.forge
    assert (forge.label, forge.enabled) == ("Forge Voice", True), probe.where()
    probe.step("retry: click forge.go")
    click(user, "forge.go")
    await settle(core, fake)
    assert fake.registry.get("rin") is not None and pt.is_file() and ref.is_file(), probe.where()
    assert probe.toast_texts()[-1][0].startswith("Forged rin · Multilingual · "), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="speak", scenario="speak",
                                         default=("load_other", "busy", "cancelled")))
async def test_a_failing_free_vram(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                  tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                  caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """unload_backend fails before it frees anything: "Couldn't free VRAM: <reason>" (the runner's own copy, §4.8
    has no row for it), the model stays resident; Free VRAM then frees it."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    fake.reset_records()
    fake.fail_next("unload_backend", KINDS[kind](MTL))
    probe.step(f"armed unload_backend {kind}; click dock.free")
    click(user, "dock.free")
    await settle(core, fake)
    assert fake.armed_failures("unload_backend") == (), probe.where()
    text = reason(kind, MTL)
    if kind == "cancelled":
        assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
    else:
        assert probe.toast_texts()[-1:] == [(f"Couldn't free VRAM: {text}", True)], probe.toast_texts()
        assert f"Free VRAM failed · {text}" in log_lines(core)
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    probe.step("retry: click dock.free")
    click(user, "dock.free")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Freed 3.4 GB", False), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="voices", scenario="voices",
                                         default=("value", "busy", "cancelled")))
async def test_a_failing_registry_edit(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                      tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                      caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """update_voice fails: "Couldn't save aiko: <reason>" and the slider snaps back (§5.8); the same edit then saves."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    edits = finishes(monkeypatch, VoicesController, "edit")
    await opened(user, core, fake, open_route, "/voices")
    voices = behaviours_of(user).voices()
    assert voices.vm.details is not None and voices.vm.details.title == "aiko"
    fake.fail_next("update_voice", KINDS[kind](MTL))
    probe.step(f"armed update_voice {kind}; voices.edit.expr change 0.7")
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await finished(edits, 1, core, fake)
    assert fake.armed_failures("update_voice") == (), probe.where()
    text = reason(kind, MTL)
    if kind == "cancelled":
        assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
    else:
        assert probe.toast_texts()[-1:] == [(f"Couldn't save aiko: {text}", True)], probe.toast_texts()
    assert not any(line.startswith("Registry · aiko") for line in log_lines(core))
    assert fake.registry.get("aiko")["exaggeration"] == 0.5
    assert (voices.vm.details.expr, voices.vm.details.expr_text) == (0.5, "0.50"), probe.where()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    probe.step("retry: voices.edit.expr change 0.7")
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await finished(edits, 2, core, fake)
    assert fake.registry.get("aiko")["exaggeration"] == 0.7, probe.where()
    assert "Registry · aiko default expressiveness set to 0.70" in log_lines(core)
    assert voices.vm.details.expr_text == "0.70"
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="voices", scenario="voices",
                                         default=("value", "busy", "cancelled")))
async def test_a_failing_voice_delete(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                     tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                     caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """delete_voice fails before it touches anything: "Couldn't delete aiko: <reason>", aiko, its entry and its .pt
    stay; the armed Delete then deletes it."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    deletes = finishes(monkeypatch, VoicesController, "delete")
    await opened(user, core, fake, open_route, "/voices")
    pt = core.paths.cache / MTL / "aiko.pt"
    fake.fail_next("delete_voice", KINDS[kind](MTL))
    probe.step(f"armed delete_voice {kind}; click voices.delete twice")
    click(user, "voices.delete")
    await finished(deletes, 1, core, fake)
    core.scheduler.advance(0.6)
    click(user, "voices.delete")
    await finished(deletes, 2, core, fake)
    assert fake.armed_failures("delete_voice") == (), probe.where()
    text = reason(kind, MTL)
    if kind == "cancelled":
        assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
    else:
        assert probe.toast_texts()[-1:] == [(f"Couldn't delete aiko: {text}", True)], probe.toast_texts()
    assert fake.registry.get("aiko") is not None and pt.is_file(), probe.where()
    assert any(row.voice_id == "aiko" for row in core.state.library.rows)
    delete = behaviours_of(user).voices().vm.details.delete
    assert (delete.label, delete.enabled) == ("Delete", True), probe.where()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    probe.step("retry: click voices.delete twice")
    core.scheduler.advance(0.6)
    click(user, "voices.delete")
    await finished(deletes, 3, core, fake)
    core.scheduler.advance(0.6)
    click(user, "voices.delete")
    await finished(deletes, 4, core, fake)
    assert fake.registry.get("aiko") is None and not pt.exists(), probe.where()
    assert probe.toast_texts()[-1] == ("Deleted aiko · its reference WAV stays in voices/reference", False)
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="speak", scenario="speak", default=("gen_oom", "cancelled")))
async def test_a_failing_gpu_reading(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                    tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """device_info carries an error on the first reading (the engine never raises from it): the VRAM block, the
    Models GPU line and About say "not detected · <error>", no toast; the next 2 s poll reads the GPU again."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    error = KINDS[kind](MTL)
    shown_error = first_line(str(error)) or type(error).__name__
    fake.fail_next("device_info", error)
    probe.step(f"armed device_info {kind}; open /")
    await opened(user, core, fake, open_route, "/")
    assert fake.armed_failures("device_info") == ()
    device = core.state.device
    assert (device.detected, device.cuda, device.error) == (True, False, shown_error), probe.where()
    status = behaviours_of(user).status().vm
    assert (status.vram_value, status.vram_tip) == ("CPU", f"GPU not detected · {shown_error}"), probe.where()
    assert core.state.about.gpu == f"Not detected · {shown_error}"
    assert behaviours_of(user).models().vm.gpu_line == f"GPU not detected · {shown_error}"
    assert not any(bad for _, bad in probe.toast_texts()), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="Multilingual ready", dock="Ready", resident=MTL)
    probe.step("advance the clock 2 s (the next poll)")
    core.scheduler.advance(2.0)
    await settle(core, fake)
    assert (device.cuda, device.error) == (True, ""), probe.where()
    assert behaviours_of(user).status().vm.vram_value == "3.4 GB"
    assert core.state.about.gpu == "RTX 4070 Ti · 12.0 GB · detected"
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("models")
async def test_an_out_of_memory_load_whose_gpu_reading_fails_drops_the_free_suffix(
        user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """The "(N GB free)" suffix comes from a fresh reading (§4.8); when that reading fails the toast is shorter."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/models")
    fake.fail_next("make_resident", _load_oom(TUR))
    fake.fail_next("device_info", RuntimeError("CUDA error: unspecified launch failure"))
    probe.step("armed make_resident OOM and device_info error; click models.switch")
    click(user, f"models.switch.{TUR}")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Turbo failed to load: CUDA out of memory", True), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="Turbo failed to load", dock="Load failed · see Log", resident="",
                  failed=TUR)
    probe.step("Retry on the dock")
    click(user, "dock.retry")
    await settle(core, fake)
    assert core.state.engine.resident == TUR and fake.loads[-1] == TUR
    assert_clean(caplog, capfd, probe)


# ---- 2. damaged and missing data ------------------------------------------------------------------------

GARBAGE = random.Random(SEED).randbytes(4096)
LEFTOVER = re.compile(r"\.part$|\.part-|\.tmp$|\.tmp-|\.upload\.", re.IGNORECASE)


@dataclass(frozen=True)
class Damage:
    """Damage to a scratch data root. `after` runs right after FakeEngine seeded the root, before the core reads
    anything; `before` runs before gui.py (for files the seeding leaves alone, such as ui_settings.json). Both get
    the root and a dict for facts the test checks later (the bytes written, say)."""

    name: str
    after: Callable[[Path, dict[str, Any]], None] | None = None
    before: Callable[[Path, dict[str, Any]], None] | None = None


@dataclass
class Damaged:
    spec: Damage
    facts: dict[str, Any]
    started: float


@pytest.fixture
def damaged(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            caplog: pytest.LogCaptureFixture) -> Damaged:
    """Indirect: request.param is a Damage, applied around the seeding (list this fixture before `user`).

    The app log's INFO lines are captured from here on: a scenario's board log replaces the Log panel's lines
    right after startup, so startup notices are checked in the `voicetool` logger (app_lines()).
    """
    caplog.set_level(logging.INFO, logger="voicetool")
    spec: Damage = request.param
    facts: dict[str, Any] = {}
    if spec.before is not None:
        spec.before(tmp_path, facts)
    if spec.after is not None:
        seed = fake_module._seed

        def seed_then_damage(scenario: Any, config: Any) -> Any:
            run_id = seed(scenario, config)
            spec.after(Path(config.ROOT), facts)
            return run_id

        monkeypatch.setattr(fake_module, "_seed", seed_then_damage)
    return Damaged(spec, facts, time.monotonic())


def damages(*specs: Damage, bugs: dict[str, str] | None = None) -> list[Any]:
    """Params for parametrize("damaged", ..., indirect=True); a spec named in bugs is xfail(strict) with that id."""
    bugs = bugs or {}
    return [pytest.param(spec, id=spec.name, marks=[pytest.mark.xfail(strict=True, reason=bugs[spec.name])]
                         if spec.name in bugs else []) for spec in specs]


def registry_of(root: Path) -> Path:
    return root / "cache" / "registry.json"


def index_of(root: Path) -> Path:
    return root / "outputs" / "takes" / "index.json"


def history_of(root: Path) -> Path:
    return root / "cache" / "load_history.jsonl"


def settings_of(root: Path) -> Path:
    return root / "ui_settings.json"


def put(path: Path, data: bytes) -> None:
    """New content for path without writing into the old file (seeded audio is hard-linked from a shared cache)."""
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def put_recorded(path: Path, data: bytes, facts: dict[str, Any], key: str) -> None:
    facts[key] = data
    put(path, data)


def leftovers(root: Path) -> list[str]:
    """Temp and partial files anywhere under the data root (the scratch Recycle Bin excluded)."""
    found = []
    for path in root.rglob("*"):
        if ".trash" in path.relative_to(root).parts:
            continue
        if path.is_file() and LEFTOVER.search(path.name):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every data file under the root and its bytes (the scratch Recycle Bin and filelock's empty lock files,
    which hold no data, excluded)."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and ".trash" not in path.relative_to(root).parts and path.suffix != ".lock"}


def app_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every app log line (the `voicetool` logger) of the setup and call phases."""
    return [record.getMessage() for phase in ("setup", "call") for record in caplog.get_records(phase)
            if record.name == "voicetool"]


def corrupt_copies(folder: Path, name: str) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.name.startswith(f"{name}.corrupt-"))


def short(text: str) -> str:
    """§4.3: the first line, at most 80 characters (an ellipsis marks the cut)."""
    line = first_line(text, 10_000)
    return line if len(line) <= 80 else line[:79].rstrip() + "…"


async def go(user: User, core: Core, fake: FakeEngine, route: str) -> None:
    """Navigate with the rail like a user (no new page load) and let the page's own refresh finish."""
    click(user, f"nav.{PAGE_ID[route]}")
    await user.should_see(marker=f"page.{PAGE_ID[route]}")
    await settle(core, fake)
    for _ in range(20):
        await asyncio.sleep(0)
    await settle(core, fake)


async def generate(user: User, core: Core, fake: FakeEngine) -> None:
    click(user, "speak.generate")
    await settle(core, fake)


# ---- registry.json ----

def _registry_garbage(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(registry_of(root), GARBAGE, facts, "written")


def _registry_truncated(root: Path, facts: dict[str, Any]) -> None:
    data = registry_of(root).read_bytes()
    put_recorded(registry_of(root), data[: len(data) // 2], facts, "written")


def _registry_list(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(registry_of(root), b'[{"aiko": {"backend": "chatterbox-multilingual"}}]', facts, "written")


def _registry_big_garbage(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(registry_of(root), random.Random(SEED + 1).randbytes(5 * 2 ** 20), facts, "written")


def _registry_deep(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(registry_of(root), b"[" * 100_000, facts, "written")


@pytest.mark.scenario("forge_ready")
@pytest.mark.parametrize("damaged", damages(
    Damage("garbage-bytes", after=_registry_garbage), Damage("truncated-json", after=_registry_truncated),
    Damage("top-level-list", after=_registry_list), Damage("5-mb-garbage", after=_registry_big_garbage),
    Damage("deeply-nested", after=_registry_deep)),       # deeply-nested: R2-04 (RecursionError), fixed
    indirect=True)
async def test_a_corrupt_registry_at_startup_is_moved_aside(damaged: Damaged, user: User, core: Core,
                                                            fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                            probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture,
                                                            capfd: pytest.CaptureFixture[str]) -> None:
    """§4.15.4: the unreadable registry moves to registry.json.corrupt-<t> untouched, a red toast says so, the
    library is empty (the Speak empty state, not a crash), and forging a voice starts a new registry."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    assert core.state.library.status == "corrupt" and core.state.library.rows == ()     # the startup snapshot
    await opened(user, core, fake, open_route, "/")
    copies = corrupt_copies(tmp_path / "cache", "registry.json")
    assert len(copies) == 1 and copies[0].read_bytes() == damaged.facts["written"], probe.where()
    assert not registry_of(tmp_path).exists()
    text = f"The voice registry was unreadable · moved it to {copies[0].name} · voices are missing until it is restored"
    assert (text, True) in probe.toast_texts() and text in app_lines(caplog), probe.toast_texts()
    # Opening Speak takes a new snapshot (registry.json's mtime changed when it moved), which reads an empty
    # registry: the library stays "corrupt" until it has voices again (an R2 finding, fixed).
    assert core.state.library.status == "corrupt" and core.state.library.rows == ()
    await user.should_see(marker="speak.empty")
    assert behaviours_of(user).speak().vm.generate.label == "Forge a Voice First"
    probe.step("forge rin on the empty library")
    await go(user, core, fake, "/forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forged rin · Multilingual · "), probe.toast_texts()
    assert set(json.loads(registry_of(tmp_path).read_text("utf-8"))) == {"rin"}
    assert [row.voice_id for row in core.state.library.rows] == ["rin"]
    assert core.state.library.status == "ready" and leftovers(tmp_path) == []
    assert_clean(caplog, capfd, probe)


def _registry_directory(root: Path, facts: dict[str, Any]) -> None:
    path = registry_of(root)
    path.unlink()
    path.mkdir()


@pytest.mark.scenario("forge_ready")
@pytest.mark.parametrize("damaged", damages(Damage("directory", after=_registry_directory)), indirect=True)
async def test_a_directory_in_place_of_the_registry(damaged: Damaged, user: User, core: Core, fake: FakeEngine,
                                                    open_route: Any, tmp_path: Path, probe: Probe,
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture,
                                                    capfd: pytest.CaptureFixture[str]) -> None:
    """An unreadable registry path (not corrupt content): the library reads "failed" with the Speak message
    "Couldn't read cache/registry.json · see Log", a forge fails with a toast and rolls its clip back, and once the
    folder is gone the library reads again and a forge works."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert core.state.library.status == "failed", probe.where()
    await user.should_see("Couldn't read cache/registry.json · see Log")
    assert any(line.startswith("Couldn't read the voice registry · [Errno 13] Permission denied")
               for line in app_lines(caplog)), app_lines(caplog)
    await go(user, core, fake, "/forge")
    probe.step("forge rin while registry.json is a folder")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forge failed: [Errno 13] Permission denied"), probe.toast_texts()
    assert not (tmp_path / "voices" / "reference" / "rin_ref.wav").exists()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    probe.step("remove the folder; open Speak, then forge again")
    registry_of(tmp_path).rmdir()
    await go(user, core, fake, "/")
    assert core.state.library.status == "ready" and core.state.library.rows == ()
    await go(user, core, fake, "/forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forged rin · Multilingual · "), probe.toast_texts()
    assert [row.voice_id for row in core.state.library.rows] == ["rin"]
    assert_clean(caplog, capfd, probe)


def _registry_padded(root: Path, facts: dict[str, Any]) -> None:
    entries = json.loads(registry_of(root).read_text("utf-8"))
    entries["aiko"]["meta"]["notes"] = "x" * (5 * 2 ** 20)
    put_recorded(registry_of(root), json.dumps(entries, indent=2, sort_keys=True).encode("utf-8"), facts, "written")


@pytest.mark.scenario("forge_ready")
@pytest.mark.parametrize("damaged", damages(Damage("5-mb-valid", after=_registry_padded)), indirect=True)
async def test_a_five_megabyte_registry_loads_and_takes_a_new_voice(damaged: Damaged, user: User, core: Core,
                                                                   fake: FakeEngine, open_route: Any,
                                                                   tmp_path: Path, probe: Probe,
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   caplog: pytest.LogCaptureFixture,
                                                                   capfd: pytest.CaptureFixture[str]) -> None:
    """A valid 5 MB registry (one voice carries 5 MB of notes in its meta): the app starts within a generous budget,
    lists the five voices, and a forge rewrites the file with every byte of the old entries kept."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    boot_s = time.monotonic() - damaged.started
    print(f"[R2 numbers] 5 MB registry: boot to first page {boot_s:.2f} s")
    assert boot_s < 30.0, f"booting on a 5 MB registry took {boot_s:.1f} s"
    assert len(core.state.library.rows) == 5 and core.state.library.status == "ready"
    await go(user, core, fake, "/forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forged rin · Multilingual · "), probe.toast_texts()
    entries = json.loads(registry_of(tmp_path).read_text("utf-8"))
    assert set(entries) == {"aiko", "marco", "friend", "narrator", "host-turbo", "rin"}
    assert len(entries["aiko"]["meta"]["notes"]) == 5 * 2 ** 20
    assert_clean(caplog, capfd, probe)


def _registry_odd_entries(root: Path, facts: dict[str, Any]) -> None:
    entries = json.loads(registry_of(root).read_text("utf-8"))
    entries["aiko"] = "not an entry"
    entries["marco"] = {"backend": 7, "reference_wav": 3, "exaggeration": "high", "language": None,
                        "created_at": 12, "meta": "x"}
    entries["friend"]["forged_at"] = "yesterday"
    entries["friend"]["created_at"] = "2026-13-45"
    entries["friend"]["meta"] = ["consent"]
    entries["a b"] = dict(entries["host-turbo"])           # a key that is not a valid voice id
    put_recorded(registry_of(root), json.dumps(entries).encode("utf-8"), facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("wrong-types", after=_registry_odd_entries)), indirect=True)
async def test_registry_entries_of_the_wrong_type(damaged: Damaged, user: User, core: Core, fake: FakeEngine,
                                                  open_route: Any, tmp_path: Path, probe: Probe,
                                                  monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                                  capfd: pytest.CaptureFixture[str]) -> None:
    """Entries that are not objects are left out, fields of the wrong type fall back, an id that is not a valid
    file name is listed but refused with a clear toast; every page renders and a good voice still speaks."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    rows = {row.voice_id: row for row in core.state.library.rows}
    assert set(rows) == {"marco", "friend", "narrator", "host-turbo", "a b"}, probe.where()
    marco = rows["marco"]
    assert (marco.backend, marco.backend_enabled, marco.exaggeration, marco.reference_path, marco.pt_exists) == (
        "7", False, 0.5, None, False)
    assert rows["friend"].consent is False
    for route in ("/voices", "/forge", "/takes", "/models", "/settings", "/"):
        probe.step(f"open {route}")
        await go(user, core, fake, route)
    await go(user, core, fake, "/voices")
    click(user, "voices.row.marco")
    await settle(core, fake)
    await user.should_see("Speak with marco")
    await go(user, core, fake, "/")
    probe.step("pick friend, type English, Generate")
    click(user, "speak.voice.friend")
    await settle(core, fake)
    behaviours_of(user).speak().set_text("Hello there, friend.")
    await settle(core, fake)
    await generate(user, core, fake)
    assert core.takes.get(8) is not None and probe.toast_texts()[-1][0].startswith("Take 8 ready"), probe.toast_texts()
    assert_clean(caplog, capfd, probe)


def _registry_expressiveness(value: float) -> Callable[[Path, dict[str, Any]], None]:
    """friend's registered Expressiveness set to value: json writes NaN / Infinity and reads them back (a hand
    edit, or a registry from before load_voice refused non-finite values)."""
    def damage(root: Path, facts: dict[str, Any]) -> None:
        entries = json.loads(registry_of(root).read_text("utf-8"))
        entries["friend"]["exaggeration"] = value
        put_recorded(registry_of(root), json.dumps(entries).encode("utf-8"), facts, "written")
    return damage


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(                        # R2-06, fixed
    Damage("infinite-expressiveness", after=_registry_expressiveness(float("inf"))),
    Damage("nan-expressiveness", after=_registry_expressiveness(float("nan")))),
    indirect=True)
async def test_a_non_finite_registered_expressiveness_falls_back(damaged: Damaged, user: User, core: Core,
                                                                 fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                                 probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                                 caplog: pytest.LogCaptureFixture,
                                                                 capfd: pytest.CaptureFixture[str]) -> None:
    """A registered Expressiveness of Infinity (a hand edit; json reads it) is not a usable value: like any other
    unusable field it must fall back to the default, so picking the voice and generating saves a take."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    friend = next(row for row in core.state.library.rows if row.voice_id == "friend")
    probe.step(f"friend's exaggeration reads {friend.exaggeration!r}; pick friend and Generate")
    click(user, "speak.voice.friend")
    await settle(core, fake)
    behaviours_of(user).speak().set_text("Hello there, friend.")
    await settle(core, fake)
    await generate(user, core, fake)
    assert core.takes.get(8) is not None, f"{probe.toast_texts()} {probe.where()}"
    assert core.takes.get(8).exaggeration == 0.5
    assert_clean(caplog, capfd, probe)


def _registry_bom(root: Path, facts: dict[str, Any]) -> None:
    data = registry_of(root).read_bytes()
    put_recorded(registry_of(root), b"\xef\xbb\xbf" + data, facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("utf8-bom", after=_registry_bom)), indirect=True)   # R2-05, fixed
async def test_a_registry_with_a_byte_order_mark_still_reads(damaged: Damaged, user: User, core: Core,
                                                             fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                             probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                             caplog: pytest.LogCaptureFixture,
                                                             capfd: pytest.CaptureFixture[str]) -> None:
    """An editor that saves UTF-8 with a BOM leaves valid JSON; the takes index, the settings and the load history
    all read it (utf-8-sig), so the registry must too instead of moving every voice aside."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert corrupt_copies(tmp_path / "cache", "registry.json") == [], probe.toast_texts()
    assert len(core.state.library.rows) == 5 and core.state.library.status == "ready"
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("speak")
async def test_a_registry_damaged_mid_session_is_moved_aside_when_a_page_opens(
        user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """registry.json replaced by garbage while the app runs (cli.py crashed, say): opening Voices reads it again
    (its mtime changed), moves it aside with the toast, and the library, Speak and the dock stay usable."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert len(core.state.library.rows) == 5
    probe.step("garbage into registry.json, then open Voices")
    put(registry_of(tmp_path), GARBAGE)
    await go(user, core, fake, "/voices")
    copies = corrupt_copies(tmp_path / "cache", "registry.json")
    assert len(copies) == 1 and copies[0].read_bytes() == GARBAGE, probe.where()
    assert probe.toast_texts()[-1] == (
        f"The voice registry was unreadable · moved it to {copies[0].name} · voices are missing until it is "
        f"restored", True)
    assert core.state.library.rows == () and core.state.library.status == "corrupt"
    await go(user, core, fake, "/")
    await user.should_see(marker="speak.empty")
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    click(user, "dock.free")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Freed 3.4 GB", False)
    assert_clean(caplog, capfd, probe)


# ---- ui_settings.json ----

def _settings_garbage(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(settings_of(root), GARBAGE, facts, "written")


def _settings_deep(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(settings_of(root), b'{"core": ' + b"[" * 100_000, facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(
    Damage("garbage-bytes", before=_settings_garbage),
    Damage("deeply-nested", before=_settings_deep)),                   # deeply-nested: R2-01 (RecursionError), fixed
    indirect=True)
async def test_a_corrupt_settings_file_falls_back_and_is_moved_aside_at_the_first_save(
        damaged: Damaged, user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """§7.2: a corrupt file means the defaults (one warning); it is moved to ui_settings.json.corrupt-<unix> only at
    the first write, which then saves a valid file."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/settings")
    assert core.settings.get("core", "autoplay") is True and core.settings.get("theme.emiya", "fx_speed") == 100
    assert any("ui_settings.json is corrupt" in r.getMessage() for r in caplog.get_records("call")) or \
        any("ui_settings.json is corrupt" in r.getMessage() for r in caplog.get_records("setup"))
    assert settings_of(tmp_path).read_bytes() == damaged.facts["written"]     # never moved during a read
    probe.step("toggle Autoplay New Takes")
    click(user, "settings.autoplay")
    await until(lambda: corrupt_copies(tmp_path, "ui_settings.json") != [] and settings_of(tmp_path).exists(),
                "the first save")
    await until(lambda: settings_module.SettingsStore.read_early(settings_of(tmp_path))["core"]["autoplay"] is False,
                "the new file")
    copies = corrupt_copies(tmp_path, "ui_settings.json")
    assert len(copies) == 1 and copies[0].read_bytes() == damaged.facts["written"]
    json.loads(settings_of(tmp_path).read_text("utf-8"))
    assert leftovers(tmp_path) == []
    assert_clean(caplog, capfd, probe)


def _settings_wrong_types(root: Path, facts: dict[str, Any]) -> None:
    data = {"version": 1, "core": {"autoplay": "yes", "idle_unload_min": 7, "takes_dir": 5, "device": "tpu",
                                   "keys": {"gen": "Ctrl+Enter", "play": ["Space", ""], "bogus": ["X", ""]},
                                   "confirm_switches": False},
            "theme.emiya": {"fx_speed": 1000, "work_glow": "on", "saber_art": "hide"},
            "extra": {"kept": True}}
    put_recorded(settings_of(root), json.dumps(data).encode("utf-8"), facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("wrong-types", before=_settings_wrong_types)), indirect=True)
async def test_settings_of_the_wrong_type_fall_back_one_by_one(damaged: Damaged, user: User, core: Core,
                                                               fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                               probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture,
                                                               capfd: pytest.CaptureFixture[str]) -> None:
    """§4.14: each invalid value falls back to its default with a warning, valid neighbours are honoured, unknown
    sections and keys survive the next write."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/settings")
    settings = core.settings
    assert (settings.get("core", "autoplay"), settings.get("core", "idle_unload_min"), settings.get("core", "device"),
            settings.get("core", "takes_dir")) == (True, 10, "cuda", "outputs/takes")
    assert settings.get("core", "confirm_switches") is False                    # valid: honoured
    assert settings.get("core", "keys")["gen"] == ("Ctrl+Enter", "")
    assert (settings.get("theme.emiya", "fx_speed"), settings.get("theme.emiya", "work_glow"),
            settings.get("theme.emiya", "saber_art")) == (100, True, "hide")
    probe.step("toggle Autoplay New Takes; wait for the save")
    click(user, "settings.autoplay")
    await until(lambda: json.loads(settings_of(tmp_path).read_text("utf-8")).get("core", {}).get("autoplay") is False,
                "the save")
    saved = json.loads(settings_of(tmp_path).read_text("utf-8"))
    assert saved["extra"] == {"kept": True} and saved["core"]["keys"]["bogus"] == ["X", ""]
    assert leftovers(tmp_path) == []
    assert_clean(caplog, capfd, probe)


def _settings_bom(root: Path, facts: dict[str, Any]) -> None:
    data = json.dumps({"version": 1, "core": {"autoplay": False, "confirm_switches": False}}).encode("utf-8")
    put_recorded(settings_of(root), b"\xef\xbb\xbf" + data, facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("utf8-bom", before=_settings_bom)), indirect=True)
async def test_a_settings_file_with_a_byte_order_mark_is_honoured(damaged: Damaged, user: User, core: Core,
                                                                  fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                                  probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                                  caplog: pytest.LogCaptureFixture,
                                                                  capfd: pytest.CaptureFixture[str]) -> None:
    install_kit(monkeypatch, tmp_path)
    await opened(user, core, fake, open_route, "/settings")
    assert core.settings.get("core", "autoplay") is False and core.settings.get("core", "confirm_switches") is False
    assert corrupt_copies(tmp_path, "ui_settings.json") == []
    assert_clean(caplog, capfd, probe)


def _settings_read_only(root: Path, facts: dict[str, Any]) -> None:
    path = settings_of(root)
    put_recorded(path, json.dumps({"version": 1, "core": {"autoplay": True}}).encode("utf-8"), facts, "written")
    os.chmod(path, stat.S_IREAD)


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("read-only", before=_settings_read_only)), indirect=True)
async def test_a_read_only_settings_file_keeps_the_change_in_memory(damaged: Damaged, user: User, core: Core,
                                                                    fake: FakeEngine, open_route: Any,
                                                                    tmp_path: Path, probe: Probe,
                                                                    monkeypatch: pytest.MonkeyPatch,
                                                                    caplog: pytest.LogCaptureFixture,
                                                                    capfd: pytest.CaptureFixture[str]) -> None:
    """The file reads; a change applies at once and its save fails with a warning (no ERROR, no temp file left);
    the file keeps its old bytes."""
    path = settings_of(tmp_path)
    try:
        install_kit(monkeypatch, tmp_path)
        watch(core, probe)
        await opened(user, core, fake, open_route, "/settings")
        probe.step("toggle Autoplay New Takes on a read-only file")
        click(user, "settings.autoplay")
        await until(lambda: any("Couldn't save ui_settings.json" in r.getMessage()
                                for r in caplog.get_records("call")), "the failed save", timeout=10)
        assert core.settings.get("core", "autoplay") is False                      # applied in memory
        assert path.read_bytes() == damaged.facts["written"] and leftovers(tmp_path) == []
        assert_clean(caplog, capfd, probe)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


# ---- outputs\takes\index.json ----

def _index_garbage(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(index_of(root), GARBAGE, facts, "written")


def _index_partial(root: Path, facts: dict[str, Any]) -> None:
    lines = index_of(root).read_bytes().splitlines(keepends=True)
    data = b"".join(lines[:4]) + lines[4][: len(lines[4]) // 2]          # a take line cut in the middle
    put_recorded(index_of(root), data, facts, "written")


def _index_big_number(root: Path, facts: dict[str, Any]) -> None:
    text = index_of(root).read_text("utf-8")
    data = re.sub(r'"seconds": [0-9.]+', '"seconds": 1' + "0" * 400, text, count=1).encode("utf-8")
    put_recorded(index_of(root), data, facts, "written")


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(
    Damage("garbage-bytes", after=_index_garbage), Damage("partial-lines", after=_index_partial),
    Damage("number-too-big", after=_index_big_number)),            # number-too-big: R2-02 (OverflowError), fixed
    indirect=True)
async def test_a_corrupt_takes_index_is_moved_aside_and_numbering_goes_on(damaged: Damaged, user: User, core: Core,
                                                                          fake: FakeEngine, open_route: Any,
                                                                          tmp_path: Path, probe: Probe,
                                                                          monkeypatch: pytest.MonkeyPatch,
                                                                          caplog: pytest.LogCaptureFixture,
                                                                          capfd: pytest.CaptureFixture[str]) -> None:
    """§4.15.1: the index moves to index.json.corrupt-<unix>, the toast says the WAVs are untouched, the list is
    empty, and the next take is number 8 (one past the highest <voice>_take<N>.wav), saved and listed."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    folder = tmp_path / "outputs" / "takes"
    wavs = {path.name: path.read_bytes() for path in folder.glob("*.wav")}
    await opened(user, core, fake, open_route, "/")
    copies = corrupt_copies(folder, "index.json")
    assert len(copies) == 1 and copies[0].read_bytes() == damaged.facts["written"], probe.toast_texts()
    text = f"The takes list couldn't be read · moved it to {copies[0].name} · the WAVs are untouched"
    assert (text, True) in probe.toast_texts() and text in app_lines(caplog)
    assert core.takes.takes == () and core.takes.next_id == 8
    probe.step("Generate")
    await generate(user, core, fake)
    assert core.takes.get(8) is not None and (folder / "aiko_take8.wav").is_file(), probe.toast_texts()
    assert {path.name: path.read_bytes() for path in folder.glob("*.wav") if path.name != "aiko_take8.wav"} == wavs
    assert [take.id for take in json_takes(index_of(tmp_path))] == [8]
    assert_clean(caplog, capfd, probe)


def json_takes(path: Path) -> list[Take]:
    data = json.loads(path.read_text("utf-8"))
    return [Take(**{k: v for k, v in take.items() if k in Take.__dataclass_fields__}) for take in data["takes"]]


def _index_missing_wavs(root: Path, facts: dict[str, Any]) -> None:
    for name in ("aiko_take5.wav", "marco_take4.wav"):
        (root / "outputs" / "takes" / name).unlink()


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("listed-wavs-missing", after=_index_missing_wavs)), indirect=True)
async def test_listed_takes_whose_wav_is_missing(damaged: Damaged, user: User, core: Core, fake: FakeEngine,
                                                 open_route: Any, tmp_path: Path, probe: Probe,
                                                 monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                                 capfd: pytest.CaptureFixture[str]) -> None:
    """The startup check flags them missing; playing one is the red "Take 5's WAV is missing" toast and its media
    URL is a 404; Remove Unstarred drops the unstarred missing one from the list, the starred one stays."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/takes")
    assert {take.id for take in core.takes.takes if take.missing} == {4, 5}, probe.where()
    path5 = tmp_path / "outputs" / "takes" / "aiko_take5.wav"
    probe.step("play take 5 from its row")
    click(user, "takes.row.5")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == (f"Take 5's WAV is missing: {path5}", True)
    assert core.session.take_id == 7
    response = await user.http_client.get("/media/take/5")
    assert response.status_code == 404
    probe.step("Remove Unstarred (twice)")
    click(user, "takes.purge")
    await settle(core, fake)
    core.scheduler.advance(0.6)
    click(user, "takes.purge")
    await until(lambda: core.takes.get(5) is None, "the purge")
    await settle(core, fake)
    assert core.takes.get(4) is not None and core.takes.get(4).missing           # starred: kept
    assert probe.toast_texts()[-1] == ("Moved 4 unstarred takes to the Recycle Bin", False), probe.toast_texts()
    assert {take.id for take in json_takes(index_of(tmp_path))} == {7, 4, 3}
    assert_clean(caplog, capfd, probe)


def _index_stray_wavs(root: Path, facts: dict[str, Any]) -> None:
    folder = root / "outputs" / "takes"
    data = (folder / "aiko_take7.wav").read_bytes()
    for name in ("aiko_take42.wav", "rin_take41-2.wav"):
        (folder / name).write_bytes(data)                         # new files (never written into a seeded one)


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("damaged", damages(Damage("unlisted-wavs", after=_index_stray_wavs)), indirect=True)
async def test_wavs_without_an_entry_are_never_overwritten(damaged: Damaged, user: User, core: Core,
                                                           fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                           probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture,
                                                           capfd: pytest.CaptureFixture[str]) -> None:
    """WAVs a crash left unlisted are not shown, and numbering skips past them: the next take is 43."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert core.takes.next_id == 43 and {take.id for take in core.takes.takes} == {1, 2, 3, 4, 5, 6, 7}
    stray = (tmp_path / "outputs" / "takes" / "aiko_take42.wav").read_bytes()
    await generate(user, core, fake)
    assert core.takes.get(43) is not None and (tmp_path / "outputs" / "takes" / "aiko_take43.wav").is_file()
    assert (tmp_path / "outputs" / "takes" / "aiko_take42.wav").read_bytes() == stray
    assert_clean(caplog, capfd, probe)


# ---- cache\load_history.jsonl ----

def _history_with(lines: list[bytes], *, tail: bytes = b"") -> Callable[[Path, dict[str, Any]], None]:
    def damage(root: Path, facts: dict[str, Any]) -> None:
        path = history_of(root)
        good = path.read_bytes().splitlines(keepends=True)
        facts["good"] = [json.loads(line) for line in good]
        data = b"".join(good[:3]) + b"".join(line + b"\n" for line in lines) + b"".join(good[3:]) + tail
        put_recorded(path, data, facts, "written")
    return damage


HISTORY_GARBAGE = [random.Random(SEED + 2).randbytes(300).replace(b"\n", b"?"), b'{"t": "2026-11-13T10:',
                   b'{"kind": "loaded"}', b"[1, 2, 3]", b'{"t":"not a time","kind":"loaded","backend":"x"}']
HISTORY_PARTIAL_TAIL = b'{"t":"2026-11-13T21:00:00+01:00","kind":"loaded","backend":"chatterbox-tur'


@pytest.mark.scenario("models")
@pytest.mark.parametrize("damaged", damages(
    Damage("garbage-and-partial-lines", after=_history_with(HISTORY_GARBAGE, tail=HISTORY_PARTIAL_TAIL)),
    Damage("kind-of-the-wrong-type",
           after=_history_with([b'{"t":"2026-11-13T10:00:00+01:00","kind":["loaded"],"backend":"x"}'])),
    Damage("number-too-big",
           after=_history_with([b'{"t":"2026-11-13T10:00:00+01:00","kind":"loaded","backend":"x","seconds":1'
                                + b"0" * 400 + b"}"])),
    Damage("deeply-nested-line", after=_history_with([b"[" * 100_000]))),   # the last three: R2-03, fixed
    indirect=True)
async def test_bad_load_history_lines_are_skipped_and_compacted(damaged: Damaged, user: User, core: Core,
                                                                fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                                probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                                caplog: pytest.LogCaptureFixture,
                                                                capfd: pytest.CaptureFixture[str]) -> None:
    """§4.15.3: unreadable lines are skipped (one warning), every good line still counts (the Models history, the
    dock's "~9 s" for Turbo), the file is rewritten without them, and the next load appends cleanly."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/models")
    good = damaged.facts["good"]
    assert len(core.state.history.events) == len(good), probe.where()
    rows = {row.backend: row for row in behaviours_of(user).status().vm.dock_rows}
    assert rows[TUR].info == "~9 s"
    lines = history_of(tmp_path).read_bytes().splitlines()
    assert [json.loads(line) for line in lines] == good, "the file was not rewritten to its good lines"
    probe.step("switch to Turbo from the card")
    click(user, f"models.switch.{TUR}")
    await settle(core, fake)
    assert core.state.engine.resident == TUR
    for line in history_of(tmp_path).read_bytes().splitlines():
        json.loads(line)
    assert leftovers(tmp_path) == []
    assert_clean(caplog, capfd, probe)


# ---- files deleted while the app runs ----

@pytest.mark.scenario("speak")
async def test_a_voice_file_deleted_while_the_app_runs(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                       tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture,
                                                       capfd: pytest.CaptureFixture[str]) -> None:
    """marco's .pt deleted: a take with marco fails with the engine's message and nothing is saved; aiko's
    reference clip deleted: auditioning it is the red toast, its media URL a 404, and Re-forge… shows the clip as
    unreadable on Forge instead of staging nothing. Takes with aiko still work."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    (tmp_path / "cache" / MTL / "marco.pt").unlink()
    (tmp_path / "voices" / "reference" / "aiko_ref.wav").unlink()
    probe.step("pick marco and Generate")
    click(user, "speak.voice.marco")
    await settle(core, fake)
    await generate(user, core, fake)
    engine_text = (f"Voice 'marco' has no cached conditionals for {MTL}. Call load_voice(backend='{MTL}') first.")
    assert probe.toast_texts()[-1] == (f"Take 8 failed: {short(engine_text)}", True), probe.toast_texts()
    assert core.takes.get(8) is None
    probe.step("audition aiko")
    click(user, "speak.audition.aiko")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Couldn't play the reference clip of aiko: file not found", True)
    assert (await user.http_client.get("/media/clip/aiko")).status_code == 404
    probe.step("pick aiko and Generate")
    click(user, "speak.voice.aiko")
    await settle(core, fake)
    await generate(user, core, fake)
    assert core.takes.get(8) is not None
    probe.step("Voices: Re-forge… aiko")
    await go(user, core, fake, "/voices")
    click(user, "voices.reforge")
    await user.should_see(marker="page.forge")
    await settle(core, fake)
    forge = behaviours_of(user).forge().vm
    assert forge.clip_state == "failed" and forge.clip_meta.startswith("Can't read aiko_ref.wav · "), probe.where()
    assert forge.forge.label == "Pick Another Clip" and not forge.forge.enabled
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("speak")
async def test_a_take_deleted_while_it_plays_or_is_pinned(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                          tmp_path: Path, probe: Probe,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          caplog: pytest.LogCaptureFixture,
                                                          capfd: pytest.CaptureFixture[str]) -> None:
    """Take 7 plays when its WAV goes: the player's error is the red "Couldn't play Take 7: …" toast, the media URL
    is a 404, and Play again says the WAV is missing. Pinned take 5 goes too: A/B refuses with the same toast and
    the card stays on take 7; nothing crashes and Generate still works."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    playback = behaviours_of(user).playback()
    folder = tmp_path / "outputs" / "takes"
    probe.step("play take 7, then delete its WAV")
    click(user, "take.play")
    await settle(core, fake)
    assert playback.vm.playing and playback.vm.target.src == core.take_url(7)
    (folder / "aiko_take7.wav").unlink()
    user.find(marker="shell.player").trigger("state", {"seq": playback.vm.target.seq, "playing": False,
                                                       "position": 2.0, "duration": 4.6, "ended": False,
                                                       "error": "MEDIA_ERR_SRC_NOT_SUPPORTED"})
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Couldn't play Take 7: MEDIA_ERR_SRC_NOT_SUPPORTED", True)
    assert not playback.vm.playing
    assert (await user.http_client.get("/media/take/7")).status_code == 404
    probe.step("play take 6, then take 7 again from the session list")
    click(user, "speak.session.row.6")
    await settle(core, fake)
    assert playback.vm.target.src == core.take_url(6) and core.session.take_id == 6
    click(user, "speak.session.row.7")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == (f"Take 7's WAV is missing: {folder / 'aiko_take7.wav'}", True)
    assert core.session.take_id == 6
    probe.step("delete pinned take 5, then A/B to it")
    (folder / "aiko_take5.wav").unlink()
    click(user, "take.ab.b")                                         # B is take 7, whose WAV is gone too
    await settle(core, fake)
    click(user, "take.ab.a")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == (f"Take 5's WAV is missing: {folder / 'aiko_take5.wav'}", True)
    assert core.session.take_id == 6
    await go(user, core, fake, "/takes")
    click(user, "takes.compare_play")
    await settle(core, fake)
    click(user, "takes.ab.a")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == (f"Take 5's WAV is missing: {folder / 'aiko_take5.wav'}", True)
    assert core.session.take_id in (6, 7)
    await go(user, core, fake, "/")
    await generate(user, core, fake)
    assert core.takes.get(8) is not None and core.session.take_id == 8
    assert_clean(caplog, capfd, probe)


# ---- a read-only data folder and a full disk ----

class Writes:
    """Every write under one folder fails, like a read-only folder (PermissionError at open, replace, rename, link,
    unlink, remove, mkdir and rmdir) or a full disk (a write stores half its data, then OSError ENOSPC).

    The patches sit on builtins.open, io.open (pathlib) and the os functions, and act only on paths under the
    folder, so pytest and NiceGUI write as usual. os.open (filelock's lock files) is left alone: a real
    read-only folder would make filelock spin until its timeout, which this test does not wait for.
    """

    def __init__(self, root: Path, mode: str) -> None:
        self.root = os.path.normcase(os.path.abspath(root))
        self.mode = mode
        self.failed: list[str] = []
        self.active = False

    def _under(self, *paths: Any) -> bool:
        for path in paths:
            if isinstance(path, int) or path is None:
                continue
            text = os.path.normcase(os.path.abspath(os.fsdecode(path)))
            if text == self.root or text.startswith(self.root + os.sep):
                return True
        return False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open = builtins.open
        writes = self

        def patched_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if writes.active and any(flag in mode for flag in "wax+") and writes._under(file):
                writes.failed.append(f"open {os.fsdecode(file)}")
                if writes.mode == "readonly":
                    raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(file))
                return _HalfWrites(real_open(file, mode, *args, **kwargs))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", patched_open)
        monkeypatch.setattr(io, "open", patched_open)
        if self.mode != "readonly":
            return
        for name in ("replace", "rename", "link", "unlink", "remove", "rmdir", "mkdir"):
            monkeypatch.setattr(os, name, self._refusing(name, getattr(os, name)))

    def _refusing(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        writes = self

        def refuse(*args: Any, **kwargs: Any) -> Any:
            paths = [arg for arg in args[:2] if isinstance(arg, (str, bytes, os.PathLike))]
            if writes.active and writes._under(*paths):
                if name == "mkdir" and os.path.exists(paths[0]):
                    raise FileExistsError(errno.EEXIST, "Cannot create a file when that file already exists",
                                          os.fsdecode(paths[0]))
                writes.failed.append(f"{name} {paths}")
                raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(paths[0]))
            return real(*args, **kwargs)

        return refuse


class _HalfWrites:
    """A file whose writes store half the data, flush it, then fail as a full disk does."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def write(self, data: Any) -> int:
        self._real.write(data[: len(data) // 2])
        self._real.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def __enter__(self) -> _HalfWrites:
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._real.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def failing_writes(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Writes:
    """Indirect: (mode, when). mode "readonly" or "full" (see Writes); "from-start" makes every write fail right
    after FakeEngine seeded the root (the core then starts on it, after any `damaged` hook listed earlier),
    "mid-session" only once the test sets `active`. List it before `user`."""
    mode, when = request.param
    writes = Writes(tmp_path, mode)
    writes.install(monkeypatch)
    monkeypatch.setattr(takes_module, "_sleep", lambda _s: None)        # the purge retries: no real waits
    if when == "from-start":
        seed = fake_module._seed

        def seed_then_fail(scenario: Any, config: Any) -> Any:
            run_id = seed(scenario, config)
            writes.active = True
            return run_id

        monkeypatch.setattr(fake_module, "_seed", seed_then_fail)
    return writes


@pytest.mark.scenario("speak")
@pytest.mark.parametrize("failing_writes", [("readonly", "from-start"), ("readonly", "mid-session")],
                         indirect=True, ids=["from-start", "mid-session"])
async def test_a_read_only_data_folder(failing_writes: Writes, user: User, core: Core, fake: FakeEngine,
                                       open_route: Any, tmp_path: Path, probe: Probe,
                                       monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                       capfd: pytest.CaptureFixture[str]) -> None:
    """Every write under the data root raises PermissionError: the app starts on it, each action says so in a toast
    (or, for settings and the load history, a warning), nothing on disk changes, no temp file is left, the app
    stays usable; once the folder is writable again the same actions work."""
    read_only_root = failing_writes
    kit = install_kit(monkeypatch, tmp_path, file_result=tmp_path / "voices" / "reference" / "aiko_ref.wav")
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    assert leftovers(tmp_path) == []
    before = snapshot(tmp_path)
    read_only_root.active = True
    probe.step("read-only: Generate")
    await generate(user, core, fake)
    assert probe.toast_texts()[-1][0].startswith("Couldn't save Take 8: [Errno 13] Permission denied"), \
        probe.toast_texts()
    assert any(line.startswith("Take 8 couldn't be saved · [Errno 13] Permission denied") for line in log_lines(core))
    probe.step("star take 7")
    click(user, "take.star")
    await until(lambda: probe.toast_texts()[-1][0].startswith("Couldn't save the star"), "the star's toast")
    assert core.takes.get(7).starred is True and behaviours_of(user).speak().vm.card.starred is True
    probe.step("Settings: Autoplay")
    await go(user, core, fake, "/settings")
    click(user, "settings.autoplay")
    await until(lambda: any("Couldn't save ui_settings.json" in r.getMessage() for r in caplog.get_records("call")),
                "the failed settings save")
    assert core.settings.get("core", "autoplay") is False
    probe.step("Voices: edit and delete")
    await go(user, core, fake, "/voices")
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await until(lambda: probe.toast_texts()[-1][0].startswith("Couldn't save aiko: [Errno 13]"), "the edit's toast")
    click(user, "voices.delete")
    await settle(core, fake)
    core.scheduler.advance(0.6)
    click(user, "voices.delete")
    await until(lambda: probe.toast_texts()[-1][0].startswith("Couldn't delete aiko: [Errno 13]"), "the delete's toast")
    assert fake.registry.get("aiko") is not None
    probe.step("Takes: Remove Unstarred (twice)")
    await go(user, core, fake, "/takes")
    click(user, "takes.purge")
    await settle(core, fake)
    core.scheduler.advance(0.6)
    click(user, "takes.purge")
    await until(lambda: "could not be moved" in probe.toast_texts()[-1][0], "the purge's toast")
    assert probe.toast_texts()[-1] == ("Moved 0 unstarred takes to the Recycle Bin · 4 could not be moved", True)
    probe.step("Forge: Browse… aiko_ref.wav, name it rin, consent, Forge")
    await go(user, core, fake, "/forge")
    forge = behaviours_of(user).forge()
    click(user, "forge.browse")
    await until(lambda: core.forge_draft.clip_state == "ready", "the staged clip")
    forge.set_name("rin")
    forge.set_consent(True)
    await settle(core, fake)
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forge failed: [Errno 13] Permission denied"), probe.toast_texts()
    probe.step("Free VRAM")
    click(user, "dock.free")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Freed 3.4 GB", False)
    assert any("Couldn't write the load history" in r.getMessage() for r in caplog.get_records("call"))
    assert snapshot(tmp_path) == before, "a read-only data folder changed on disk"
    assert leftovers(tmp_path) == [] and read_only_root.failed
    assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
    probe.step("writable again: Load Multilingual from Models, Generate")
    read_only_root.active = False
    await go(user, core, fake, "/models")
    click(user, f"models.switch.{MTL}")
    await settle(core, fake)
    await go(user, core, fake, "/")
    await generate(user, core, fake)
    assert core.takes.get(8) is not None, probe.toast_texts()
    assert_clean(caplog, capfd, probe)
    assert kit.called("pick_file")


@pytest.mark.scenario("forge_ready")
@pytest.mark.parametrize("failing_writes", [("full", "mid-session")], indirect=True, ids=["mid-session"])
async def test_a_full_disk(failing_writes: Writes, user: User, core: Core, fake: FakeEngine, open_route: Any,
                           tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                           caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """Every write under the data root stores half its data, then fails with ENOSPC: each action says so, no
    half-written file is left behind (the load history may end in a partial line, which its reader skips), the
    index, registry and settings keep their bytes; once there is room again everything works."""
    full_disk = failing_writes
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    keep = ("outputs/takes/index.json", "cache/registry.json")
    before = {name: (tmp_path / name).read_bytes() for name in keep}
    history_before = history_of(tmp_path).read_bytes()
    full_disk.active = True
    probe.step("full: Generate")
    await generate(user, core, fake)
    assert probe.toast_texts()[-1] == ("Couldn't save Take 8: [Errno 28] No space left on device", True), \
        probe.toast_texts()
    probe.step("star take 7")
    click(user, "take.star")
    await until(lambda: probe.toast_texts()[-1][0].startswith("Couldn't save the star"), "the star's toast")
    assert probe.toast_texts()[-1] == ("Couldn't save the star: [Errno 28] No space left on device", True)
    probe.step("Settings: Autoplay")
    await go(user, core, fake, "/settings")
    click(user, "settings.autoplay")
    await until(lambda: any("Couldn't save ui_settings.json" in r.getMessage() for r in caplog.get_records("call")),
                "the failed settings save")
    assert not settings_of(tmp_path).exists()
    probe.step("Voices: edit")
    await go(user, core, fake, "/voices")
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await until(lambda: probe.toast_texts()[-1][0].startswith("Couldn't save aiko"), "the edit's toast")
    assert probe.toast_texts()[-1] == ("Couldn't save aiko: [Errno 28] No space left on device", True)
    probe.step("Forge rin")
    await go(user, core, fake, "/forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Forge failed: [Errno 28] No space left on device", True), probe.toast_texts()
    assert not (tmp_path / "voices" / "reference" / "rin_ref.wav").exists()
    probe.step("Takes: Remove Unstarred (twice)")
    await go(user, core, fake, "/takes")
    click(user, "takes.purge")
    await settle(core, fake)
    core.scheduler.advance(0.6)
    click(user, "takes.purge")
    await until(lambda: "unstarred" in probe.toast_texts()[-1][0], "the purge's toast")
    assert probe.toast_texts()[-1][0].startswith("Couldn't remove the unstarred takes: "), probe.toast_texts()
    probe.step("Free VRAM")
    click(user, "dock.free")
    await settle(core, fake)
    assert probe.toast_texts()[-1] == ("Freed 3.4 GB", False)
    assert {name: (tmp_path / name).read_bytes() for name in keep} == before
    assert leftovers(tmp_path) == [], leftovers(tmp_path)
    assert history_of(tmp_path).read_bytes().startswith(history_before)     # appends only: old lines intact
    assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
    probe.step("room again: Load Multilingual, Generate, Forge, Remove Unstarred")
    full_disk.active = False
    await go(user, core, fake, "/models")
    click(user, f"models.switch.{MTL}")
    await settle(core, fake)
    await go(user, core, fake, "/")
    await generate(user, core, fake)
    assert core.takes.get(8) is not None, probe.toast_texts()
    await go(user, core, fake, "/forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert probe.toast_texts()[-1][0].startswith("Forged rin"), probe.toast_texts()
    await go(user, core, fake, "/takes")
    core.scheduler.advance(0.6)                              # the failed purge consumed its confirmation
    click(user, "takes.purge")
    await settle(core, fake)
    core.scheduler.advance(0.6)
    click(user, "takes.purge")
    await until(lambda: probe.toast_texts()[-1][0].startswith("Moved "), "the purge")
    assert leftovers(tmp_path) == [] and full_disk.failed
    history = LoadHistory(history_of(tmp_path))
    history.load()                                           # skips the partial lines and compacts the file
    assert len(history.events) >= len(history_before.splitlines())
    for line in history_of(tmp_path).read_bytes().splitlines():
        json.loads(line)
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("models")
@pytest.mark.parametrize("damaged", damages(Damage("history-to-compact", after=_history_with(HISTORY_GARBAGE))),
                         indirect=True)
@pytest.mark.parametrize("failing_writes", [("full", "from-start")], indirect=True, ids=["full-from-start"])
async def test_a_full_disk_at_startup(damaged: Damaged, failing_writes: Writes, user: User, core: Core,
                                      fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
                                      monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                      capfd: pytest.CaptureFixture[str]) -> None:
    """The disk is full before the app starts and the load history needs compacting: the compaction's write fails
    (a warning), the good lines still show, no temp file is left, and a switch works (its history line is a
    warning too)."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/models")
    assert len(core.state.history.events) == len(damaged.facts["good"])
    assert history_of(tmp_path).read_bytes() == damaged.facts["written"]          # the compaction never landed
    assert any("Couldn't read the load history" in m or "unreadable line" in m for m in
               (r.getMessage() for r in caplog.get_records("setup") + caplog.get_records("call")))
    assert leftovers(tmp_path) == [] and failing_writes.failed
    click(user, f"models.switch.{TUR}")
    await settle(core, fake)
    assert core.state.engine.resident == TUR and probe.toast_texts()[-1][0].startswith("Turbo ready · ")
    assert leftovers(tmp_path) == []
    assert_clean(caplog, capfd, probe)


# ---- more engine faults: chains, the test line, the warm-up, the library, the takes list ----------------

@pytest.mark.parametrize("kind", _params(busy_scenario="speak", scenario="speak", default=("gen_other",)))
async def test_a_failing_second_take_drops_the_rest_of_the_queue(kind: str, user: User, core: Core,
                                                                  fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                                  probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                                  caplog: pytest.LogCaptureFixture,
                                                                  capfd: pytest.CaptureFixture[str]) -> None:
    """Generate 3 Takes, the second take fails (§4.8 "speak failed": the waiting queue is cleared, the model stays):
    take 8 is saved, take 9 fails, take 10 never runs; Generate then makes take 9."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    first, second = fake.hold("speak"), fake.hold("speak")
    probe.step("Generate 3 Takes; release take 8; arm the failure; release take 9")
    click(user, "speak.generate3")
    await until(first.entered.is_set, "take 8 to start")
    first.release()
    await until(second.entered.is_set, "take 9 to start")
    assert core.takes.get(8) is not None and core.state.job.take_no == 9 and core.jobs.queued
    fake.fail_next("speak", KINDS[kind](MTL))
    second.release()
    await until(lambda: not core.state.job.kind, "the chain to end")
    await settle(core, fake)
    text = reason(kind, MTL)
    if kind != "cancelled":
        assert probe.toast_texts()[-1] == (f"Take 9 failed: {text}", True), probe.toast_texts()
    assert core.takes.get(9) is None and core.takes.get(10) is None, probe.where()
    assert [call.name for call in fake.calls].count("speak") == 2
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    await generate(user, core, fake)
    assert core.takes.get(9) is not None, probe.toast_texts()
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="other_backend", scenario="other_backend",
                                         default=("load_oom", "busy"), kinds=("load_oom", "busy", "value")))
async def test_a_failed_switch_drops_the_take_it_was_for(kind: str, user: User, core: Core, fake: FakeEngine,
                                                         open_route: Any, tmp_path: Path, probe: Probe,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture,
                                                         capfd: pytest.CaptureFixture[str]) -> None:
    """"Switch to Turbo & Generate" whose load fails (§4.8: the queue and the after-action are dropped): no take
    runs, the failed state shows; the named button then loads Turbo and makes the take. EngineBusy needs nothing
    resident, so that case frees VRAM first."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    if kind == "busy":
        click(user, "dock.free")
        await settle(core, fake)
    generate_vm = behaviours_of(user).speak().vm.generate
    assert generate_vm.label.startswith(("Switch to Turbo & Generate", "Load Turbo & Generate")), generate_vm
    fake.reset_records()
    fake.fail_next("make_resident", KINDS[kind](TUR))
    probe.step(f"armed make_resident {kind}; click {generate_vm.label!r}")
    await generate(user, core, fake)
    assert "speak" not in [call.name for call in fake.calls] and core.takes.get(8) is None, probe.where()
    if kind == "busy":
        assert probe.toast_texts()[-1] == (BUSY_TEXT, True)
        assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
    else:
        assert probe.toast_texts()[-1][0].startswith(f"Turbo failed to load: {load_reason(kind, TUR)}")
        assert_usable(user, core, fake, probe, chip="Turbo failed to load", dock="Load failed · see Log",
                      resident="", failed=TUR)
    label = behaviours_of(user).speak().vm.generate.label
    assert label.startswith("Load Turbo & Generate"), label
    probe.step(f"retry: click {label!r}")
    await generate(user, core, fake)
    assert core.state.engine.resident == TUR and core.takes.get(8) is not None, probe.toast_texts()
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("forge_other_backend")
async def test_a_forge_after_a_switch_fails_at_the_load_then_at_the_bake(
        user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """"Switch to Turbo & Forge": the load fails (the forge does not run, nothing is committed); then the load works
    and the bake fails (the clip is rolled back, Turbo stays); then it forges."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/forge")
    ref = core.paths.voices / "rin_ref.wav"
    assert behaviours_of(user).forge().vm.forge.label == "Switch to Turbo & Forge"
    fake.reset_records()
    fake.fail_next("make_resident", _load_oom(TUR))
    probe.step("load fails")
    click(user, "forge.go")
    await settle(core, fake)
    assert "load_voice" not in [call.name for call in fake.calls] and not ref.exists()
    assert probe.toast_texts()[-1][0] == "Turbo failed to load: CUDA out of memory (9.5 GB free)", probe.toast_texts()
    assert behaviours_of(user).forge().vm.forge.label == "Load Turbo & Forge"
    probe.step("bake fails")
    fake.fail_next("load_voice", _gen_oom(TUR))
    click(user, "forge.go")
    await settle(core, fake)
    assert core.state.engine.resident == TUR and not ref.exists() and fake.registry.get("rin") is None
    assert probe.toast_texts()[-1] == ("Forge failed: CUDA out of memory", True), probe.toast_texts()
    assert_usable(user, core, fake, probe, chip="Turbo ready · 3.1 GB", dock="Ready", resident=TUR)
    probe.step("forge")
    click(user, "forge.go")
    await settle(core, fake)
    assert fake.registry.get("rin")["backend"] == TUR and ref.is_file(), probe.toast_texts()
    assert_clean(caplog, capfd, probe)


@pytest.mark.parametrize("kind", _params(busy_scenario="forged", scenario="forged", default=("gen_oom", "cancelled")))
async def test_a_failing_test_line(kind: str, user: User, core: Core, fake: FakeEngine, open_route: Any,
                                   tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                   caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """Forge's "Play Test Line" reaches speak too (§4.8 "test line failed"): "Test line failed: <reason>", nothing
    plays, the slot frees; the next click plays the line."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/forge")
    playback = behaviours_of(user).playback()
    fake.fail_next("speak", KINDS[kind](MTL))
    probe.step(f"armed speak {kind}; click forge.test_line")
    click(user, "forge.test_line")
    await settle(core, fake)
    text = reason(kind, MTL)
    if kind != "cancelled":
        assert probe.toast_texts()[-1] == (f"Test line failed: {text}", True), probe.toast_texts()
        assert f"Test line failed · {text}" in log_lines(core)
    assert not playback.vm.playing and not core.clips.test_line_path("rin").exists()
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    click(user, "forge.test_line")
    await settle(core, fake)
    assert playback.vm.playing and "test-rin" in playback.vm.target.src, probe.where()
    assert_clean(caplog, capfd, probe)


def _warm_up_last(root: Path, facts: dict[str, Any]) -> None:
    put_recorded(settings_of(root), json.dumps({"version": 1, "core": {"warmup": "last"}}).encode("utf-8"), facts,
                 "written")


@pytest.mark.parametrize("damaged", damages(Damage("warm-up-last", before=_warm_up_last)), indirect=True)
@pytest.mark.parametrize("kind", _params(busy_scenario="relaunch", scenario="relaunch", default=("load_oom",),
                                         kinds=("load_oom", "load_missing", "busy")))
async def test_a_warm_up_that_fails_at_launch(kind: str, damaged: Damaged, user: User, core: Core, fake: FakeEngine,
                                              open_route: Any, tmp_path: Path, probe: Probe,
                                              monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                              capfd: pytest.CaptureFixture[str]) -> None:
    """Warm Up on Launch = Last Model, and that load fails before the user did anything: the same failed-load (or
    EngineBusy) copy, then Retry or the card loads it."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    fake.fail_next("make_resident", KINDS[kind](MTL))
    probe.step(f"armed make_resident {kind}; open /")
    await opened(user, core, fake, open_route, "/")
    await settle(core, fake)
    assert fake.armed_failures("make_resident") == () and "Emiya Voice started · warming up Multilingual" in \
        app_lines(caplog), app_lines(caplog)
    if kind == "busy":
        assert probe.toast_texts()[-1] == (BUSY_TEXT, True)
        assert_usable(user, core, fake, probe, chip="No model loaded", dock="No model loaded", resident="")
        await go(user, core, fake, "/models")
        click(user, f"models.switch.{MTL}")
    else:
        expected = f"Multilingual failed to load: {reason(kind, MTL)}" + (" (9.5 GB free)" if kind == "load_oom" else "")
        assert probe.toast_texts()[-1] == (expected, True), probe.toast_texts()
        assert_usable(user, core, fake, probe, chip="Multilingual failed to load", dock="Load failed · see Log",
                      resident="", failed=MTL)
        assert not behaviours_of(user).status().vm.load_default.enabled        # D13: Retry loads the default
        click(user, "dock.retry")
    await settle(core, fake)
    assert core.state.engine.resident == MTL and probe.toast_texts()[-1][0].startswith("Multilingual ready · ")
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("voices")
async def test_a_failing_library_read_shows_the_failed_state_and_recovers(
        user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path, probe: Probe,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    """voice_details fails in the snapshot after an edit: the log says why, Speak's voice panel would say "Couldn't
    read cache/registry.json · see Log" (§6.7), Voices keeps the last good list; the next page open reads the
    library again."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    edits = finishes(monkeypatch, VoicesController, "edit")
    await opened(user, core, fake, open_route, "/voices")
    fake.fail_next("voice_details", OSError(errno.EIO, "Input/output error"))
    probe.step("edit aiko's expressiveness; the snapshot after it fails")
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await finished(edits, 1, core, fake)
    assert core.state.library.status == "failed", probe.where()
    assert "Couldn't read the voice registry · [Errno 5] Input/output error" in log_lines(core)
    speak = behaviours_of(user).speak().vm
    assert (speak.library_state, speak.library_message) == ("failed", "Couldn't read cache/registry.json · see Log")
    await user.should_see(marker="voices.row.marco")                             # the last good list stays
    await go(user, core, fake, "/")
    assert core.state.library.status == "ready" and len(core.state.library.rows) == 5
    assert next(row for row in core.state.library.rows if row.voice_id == "aiko").exaggeration == 0.7
    assert_clean(caplog, capfd, probe)


@pytest.mark.scenario("speak")
async def test_a_take_saved_but_not_listed(user: User, core: Core, fake: FakeEngine, open_route: Any, tmp_path: Path,
                                           probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture,
                                           capfd: pytest.CaptureFixture[str]) -> None:
    """The WAV lands but the index cannot be rewritten (§4.8 "save failed", S03 §1.2): "Take 8 is saved at <path> but
    couldn't be listed: <reason>", number 8 is never handed out again, and the next take is listed as 9."""
    install_kit(monkeypatch, tmp_path)
    watch(core, probe)
    await opened(user, core, fake, open_route, "/")
    write_atomic = takes_module._write_atomic

    def full(path: Path, text: str) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(takes_module, "_write_atomic", full)
    await generate(user, core, fake)
    wav = tmp_path / "outputs" / "takes" / "aiko_take8.wav"
    assert probe.toast_texts()[-1] == (f"Take 8 is saved at {wav} but couldn't be listed: No space left on device",
                                       True), probe.toast_texts()
    assert "Take 8 couldn't be saved · No space left on device" in log_lines(core)
    assert wav.is_file() and core.takes.get(8) is None and core.takes.next_id == 9
    assert_usable(user, core, fake, probe, chip="Multilingual ready · 3.4 GB", dock="Ready", resident=MTL)
    monkeypatch.setattr(takes_module, "_write_atomic", write_atomic)
    await generate(user, core, fake)
    assert core.takes.get(9) is not None and core.takes.get(8) is None, probe.toast_texts()
    assert fresh_next_id(tmp_path) == 10                     # a restart never hands out 8 either
    assert_clean(caplog, capfd, probe)


def fresh_next_id(root: Path) -> int:
    """The next take number a fresh takes store on this root would give."""
    store = takes_module.TakesStore(index_of(root), root)
    store.load(folders=[root / "outputs" / "takes"])
    return store.next_id


# ---- 3. a monkey that clicks while engine calls fail at random -----------------------------------------

FAULT_METHODS = ("make_resident", "speak", "load_voice", "unload_backend", "update_voice", "delete_voice",
                 "device_info", "voice_details")
CLOCK_STEPS = (0.5, 2.0, 3.5, 15.0, 120.0)
MONKEY_DEFAULT = (("speak", 80), ("forge_ready", 80))
MONKEY_SOAK = tuple((scenario, 250) for scenario in ("speak", "models", "forge_ready", "failure", "other_backend",
                                                     "relaunch", "voices", "takes", "forged", "firstrun"))


def _clickable(element: Element) -> bool:
    return any(listener.type == "click" for listener in element._event_listeners.values())


def _shown(element: Element) -> bool:
    return all(e.visible for e in (element, *element.ancestors()))


def _enabled(element: Element) -> bool:
    if isinstance(element, DisableableElement) and not element.enabled:
        return False
    return not element.props.get("disabled")


def _key(element: Element) -> str:
    if element._markers:
        return " ".join(element._markers)
    text = " ".join(str(c) for c in element._displayed_contents(only_visible=False))[:40]
    return f"{element.tag}:{text}"


async def quiet(core: Core, fake: FakeEngine) -> None:
    """Every job, task and engine call the last action started has finished, and no new engine call starts: client
    tasks (a Voices edit, say) chain several calls outside core.scheduler, so wait until the count holds still."""
    for _ in range(50):
        count = len(fake.calls)
        await settle(core, fake)
        await until(lambda: not any(call.outcome == "running" for call in list(fake.calls)), "the engine calls", 10)
        for _ in range(10):
            await asyncio.sleep(0)
        await settle(core, fake)
        if len(fake.calls) == count:
            return


def invariants(core: Core, fake: FakeEngine, probe: Probe) -> None:
    resident = fake.residency().resident
    assert len(resident) <= 1, f"two models resident {probe.where()}"
    if not core.state.job.kind:
        assert core.jobs.task is None and core.jobs.queued == (), f"an idle slot holds a task {probe.where()}"
        assert core.state.engine.resident == (resident[-1] if resident else ""), (
            f"EngineState says {core.state.engine.resident!r}, the engine {resident} {probe.where()}")


async def run_monkey(scenario: str, actions: int, user: User, core: Core, fake: FakeEngine, open_route: Any,
                     tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    rng = random.Random(f"{SEED}-{scenario}")

    def pick(options: Any) -> Any:
        """One draw per choice whatever the number of options, so the stream stays aligned with the seed."""
        return options[int(rng.random() * len(options))]

    install_kit(monkeypatch, tmp_path, file_result=tmp_path / "voices" / "reference" / "sample.wav")
    await opened(user, core, fake, open_route, "/")
    counts = {"clicks": 0, "faults": 0, "clock": 0}
    for _ in range(actions):
        roll = rng.random()
        if roll < 0.25:
            method, kind, backend = pick(FAULT_METHODS), pick(tuple(KINDS)), pick((MTL, STD, TUR))
            fake.fail_next(method, KINDS[kind](backend))
            counts["faults"] += 1
            probe.step(f"arm {method} {kind}")
            continue
        if roll < 0.35:
            seconds = pick(CLOCK_STEPS)
            probe.step(f"clock +{seconds} s")
            core.scheduler.advance(seconds)
            counts["clock"] += 1
        else:
            candidates = sorted((e for e in user.current_layout.descendants()        # by a stable key, not by
                                 if _clickable(e) and _shown(e) and _enabled(e)),   # creation order (ids move
                                key=lambda e: (_key(e), e.id))                      # when a list re-renders)
            if not candidates:
                break
            element = pick(candidates)
            probe.step(f"click {_key(element)}")
            with user.client:
                UserInteraction(user, {element}, None).click()
            counts["clicks"] += 1
        await asyncio.wait_for(quiet(core, fake), 15)                     # never a freeze
        invariants(core, fake, probe)
    fake.release_all()
    await quiet(core, fake)
    invariants(core, fake, probe)
    counts["fired"] = counts["faults"] - sum(len(fake.armed_failures(method)) for method in FAULT_METHODS)
    return counts


@pytest.mark.parametrize("scenario,actions", [
    pytest.param(scenario, actions, marks=pytest.mark.scenario(scenario), id=scenario)
    for scenario, actions in MONKEY_DEFAULT])
async def test_a_monkey_clicks_while_engine_calls_fail(scenario: str, actions: int, user: User, core: Core,
                                                       fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                       probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture,
                                                       capfd: pytest.CaptureFixture[str]) -> None:
    """Seeded random clicks on every visible, enabled control, the manual clock moving now and then, and an engine
    failure (any method the GUI calls, any error kind) armed before a quarter of the steps: every action settles
    within 15 s, never two models, an idle slot holds no task, EngineState matches the engine, and no ERROR, no
    swallowed failure and no traceback. The trace (printed as it goes) replays the run for this seed."""
    counts = await run_monkey(scenario, actions, user, core, fake, open_route, tmp_path, probe, monkeypatch)
    print(f"\n[R2 numbers] fault monkey {scenario}: {counts}")
    assert counts["clicks"] > actions // 3
    assert_clean(caplog, capfd, probe)


@pytest.mark.soak
@pytest.mark.parametrize("scenario,actions", [
    pytest.param(scenario, actions, marks=pytest.mark.scenario(scenario), id=scenario)
    for scenario, actions in MONKEY_SOAK])
async def test_a_long_monkey_clicks_while_engine_calls_fail(scenario: str, actions: int, user: User, core: Core,
                                                            fake: FakeEngine, open_route: Any, tmp_path: Path,
                                                            probe: Probe, monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture,
                                                            capfd: pytest.CaptureFixture[str]) -> None:
    """The same monkey, ten scenarios, 250 steps each (opt-in: -m soak)."""
    counts = await run_monkey(scenario, actions, user, core, fake, open_route, tmp_path, probe, monkeypatch)
    print(f"\n[R2 numbers] fault monkey {scenario}: {counts}")
    assert_clean(caplog, capfd, probe)
