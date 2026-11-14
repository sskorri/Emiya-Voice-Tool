"""The one-model rule through the whole app on the fake engine (W3, W5, W6; §1.4, §4.5, §8.5 I1).

A model loads or unloads only through a named control, a confirmed ask, Free VRAM, the warm-up or the idle
auto-unload (the last two only when the user turned them on), and never two are resident:
- rendering every route in every scenario calls no make_resident, load_backend, unload_backend, load_voice or
  speak, and each route renders its required markers (in the element tree: some are hidden in some states, for
  example Voices' details without voices; a take's star only exists on a listed take) and its header title (W5);
- every wiring row that loads goes through a named control (a dock row is named by its model), an ask (or its
  primary) or an arm, and loads exactly its backends (the runner of test_wiring.py, which runs every row; this
  file runs again the rows a load is at stake in, and checks each make_resident call);
- with Warm Up on Launch "Off" nothing loads at launch and the chip reads "No model loaded" (W3); with "Last
  Model" exactly the saved model loads at startup, once;
- the idle unload frees the model after the limit on the manual clock, never while a job runs;
- no dock row, Models card, Generate key or Forge button loads anything while a job runs;
- a named token is void after a residency, job or library change: redeeming it loads nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nicegui.testing import User

from ui.contract.markers import REQUIRED_MARKERS
from ui.contract.routes import PAGE_ID, ROUTES, TITLES
from ui.core import Core
from ui.core.background import WARMUP_IN_USE, launch_line
from ui.core.clock import ManualScheduler
from ui.core.fake import SCENARIOS, FakeEngine

from tests.pages.test_wiring import (ROWS, behaviours_of, install_kit, marked, model_calls, run_row, settle, shown,
                                     the, until)
from tests.pages.wiring import Wire

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
HELD = ("loading", "generating", "queued", "forging", "freeing")


def scenario(name: str) -> Any:
    return pytest.param(name, marks=pytest.mark.scenario(name), id=name)


def calls_of(fake: FakeEngine, name: str) -> list[Any]:
    return [call for call in fake.calls if call.name == name]


async def first_page(user: User, core: Core, fake: FakeEngine, route: str = "/") -> None:
    """The first page load (the scenario's replay), its held job held, and the first device reading in."""
    await user.open(route)
    await settle(core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    await settle(core, fake)


def required(user: User, route: str) -> tuple[str, ...]:
    """REQUIRED_MARKERS[route]; a take row's star only when the Takes list shows a row (none in an empty list)."""
    has_rows = any(marker.startswith("takes.row.") for element in user.current_layout.descendants()
                   for marker in element._markers)
    return tuple(marker for marker in REQUIRED_MARKERS[route] if marker != "takes.star" or has_rows)


async def visit(user: User, core: Core, fake: FakeEngine, route: str, *, via_rail: bool) -> None:
    """Open route: as a fresh page load, or by its rail button inside the current page."""
    if via_rail:
        user.find(marker=f"nav.{PAGE_ID[route]}").click()
    else:
        await user.open(route)
    await user.should_see(marker=f"page.{PAGE_ID[route]}")
    await settle(core, fake)


# ---- rendering never loads (W5, W6) -------------------------------------------------------------------

@pytest.mark.parametrize("name", [scenario(name) for name in SCENARIOS])
async def test_rendering_every_route_loads_no_model(user: User, core: Core, fake: FakeEngine, name: str) -> None:
    await first_page(user, core, fake)
    fake.reset_records()
    for route in ROUTES:                                                # a fresh page load per route
        await visit(user, core, fake, route, via_rail=False)
        for marker in required(user, route):
            assert marked(user, marker), f"{route} lacks {marker} in {name}"
        assert the(user, "shell.title").text == TITLES[route]
        assert not marked(user, "page.error"), f"{route} failed to build in {name}"
    for route in (*ROUTES[1:], "/"):                                    # and every route from the rail
        await visit(user, core, fake, route, via_rail=True)
    assert model_calls(fake) == [] and fake.loads == []
    assert len(fake.residency().resident) <= 1


# ---- every row that may load --------------------------------------------------------------------------

def _may_load(row: Wire) -> bool:
    """W6: a named control, an ask, an arm, the ask's primary, or a dock row whose label is the model."""
    if row.confirm in ("named", "ask", "arm") or row.marker == "confirm.ask.primary":
        return True
    return row.marker.startswith("dock.row.") and row.loads == (row.marker.removeprefix("dock.row."),)


def test_every_row_that_loads_goes_through_a_named_control_an_ask_or_an_arm() -> None:
    loading = [row for row in ROWS if row.loads]
    assert loading and all(_may_load(row) for row in loading), [row for row in loading if not _may_load(row)]
    assert all(len(row.loads) == 1 for row in loading)                  # one model per action, never two


AT_STAKE = [row for row in ROWS if row.loads or row.confirm]


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(AT_STAKE)])
async def test_a_row_loads_exactly_its_backends(user: User, core: Core, fake: FakeEngine, open_route: Any,
                                                tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    kit = install_kit(monkeypatch, tmp_path)
    await run_row(row, user, core, fake, kit, open_route, monkeypatch)
    assert [call.args[0] for call in calls_of(fake, "make_resident")] == list(row.loads)


# ---- launch (W3) and the warm-up ----------------------------------------------------------------------

@pytest.fixture
def warm_up_last(fake_env: str, tmp_path: Path) -> None:
    """Warm Up on Launch "Last Model" in the scratch ui_settings.json, before gui.py composes the app."""
    (tmp_path / "ui_settings.json").write_text(json.dumps({"core": {"warmup": "last"}}), encoding="utf-8")


@pytest.mark.parametrize("name", [scenario("firstrun"), scenario("relaunch")])
async def test_nothing_loads_at_launch_with_warm_up_off(user: User, core: Core, fake: FakeEngine,
                                                       clock: ManualScheduler, name: str) -> None:
    assert core.settings.get("core", "warmup") == "off"
    await first_page(user, core, fake)
    clock.advance(120)
    await settle(core, fake)
    for route in ROUTES[1:]:
        await visit(user, core, fake, route, via_rail=True)
    assert model_calls(fake) == [] and fake.loads == []
    assert fake.residency().resident == () and core.state.engine.resident == ""
    assert the(user, "status").text == "No model loaded"
    assert any(line.endswith(launch_line("Emiya Voice")) for line in core.state.log.lines)
    assert not any("Loading" in line for line in core.state.log.lines)


@pytest.mark.scenario("relaunch")
async def test_warm_up_last_model_loads_it_once_at_startup(warm_up_last: None, user: User, core: Core,
                                                          fake: FakeEngine, clock: ManualScheduler) -> None:
    assert core.settings.get("core", "warmup") == "last" and core.history.last_loaded() == MTL
    await first_page(user, core, fake)
    await until(lambda: core.state.engine.resident == MTL, "the warm-up load")
    await settle(core, fake)
    assert fake.loads == [MTL] and [call.args[0] for call in calls_of(fake, "make_resident")] == [MTL]
    assert any(line.endswith("Emiya Voice started · warming up Multilingual") for line in core.state.log.lines)
    for route in ROUTES:                                                # other pages and page loads: no second load
        await visit(user, core, fake, route, via_rail=False)
    clock.advance(120)
    await settle(core, fake)
    assert fake.loads == [MTL] and fake.residency().resident == (MTL,)


@pytest.mark.scenario("speak")
async def test_warm_up_skips_when_a_model_is_already_resident(warm_up_last: None, user: User, core: Core,
                                                             fake: FakeEngine) -> None:
    await first_page(user, core, fake)
    await until(lambda: any(line.endswith(WARMUP_IN_USE) for line in core.state.log.lines), "the warm-up skip")
    assert fake.loads == [] and model_calls(fake) == [] and fake.residency().resident == (MTL,)


# ---- idle auto-unload ---------------------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_idle_unload_frees_the_model_after_the_limit(user: User, core: Core, fake: FakeEngine,
                                                              open_route: Any, clock: ManualScheduler) -> None:
    core.settings.set("core", "idle_unload_min", 0)
    await open_route(user, "/models")
    await settle(core, fake)
    user.find(marker="models.idle.10").click()                         # the user turns it on
    fake.reset_records()
    clock.advance(9 * 60)
    await settle(core, fake)
    assert calls_of(fake, "unload_backend") == [] and core.state.engine.resident == MTL
    clock.advance(75)
    await settle(core, fake)
    unloads = calls_of(fake, "unload_backend")
    assert [(call.args, call.kwargs.get("reason")) for call in unloads] == [((MTL,), "idle")]
    assert fake.loads == [] and core.state.engine.resident == "" and fake.residency().resident == ()
    assert the(user, "shell.toast").text == "Freed 3.4 GB · idle 10 min"


@pytest.mark.scenario("generating")
async def test_the_idle_unload_never_frees_while_a_job_runs(user: User, core: Core, fake: FakeEngine,
                                                           clock: ManualScheduler) -> None:
    core.settings.set("core", "idle_unload_min", 10)
    await first_page(user, core, fake)
    fake.reset_records()
    clock.advance(11 * 60)                                              # the take is still held
    await settle(core, fake)
    assert calls_of(fake, "unload_backend") == [] and core.state.job.kind == "generate"
    fake.release_all()                                                  # the take finishes
    await until(lambda: not core.state.job.kind, "the take to finish")
    await settle(core, fake)
    clock.advance(9 * 60)                                               # the limit counts from the job's end
    await settle(core, fake)
    assert calls_of(fake, "unload_backend") == []
    clock.advance(75)
    await settle(core, fake)
    assert [call.kwargs.get("reason") for call in calls_of(fake, "unload_backend")] == ["idle"]
    assert fake.loads == []


# ---- no load while a job runs -------------------------------------------------------------------------

def _click_if_shown(user: User, marker: str) -> None:
    if shown(user, marker):
        user.find(marker=marker).click()


@pytest.mark.parametrize("name", [scenario(name) for name in HELD])
async def test_no_control_loads_while_a_job_runs(user: User, core: Core, fake: FakeEngine, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    install_kit(monkeypatch, tmp_path)
    await first_page(user, core, fake)
    kind = core.state.job.kind
    assert kind, f"{name} holds no job"
    fake.reset_records()
    for backend in (MTL, STD, TUR):                                     # the Model dock
        _click_if_shown(user, f"dock.row.{backend}")
        await settle(core, fake)
        _click_if_shown(user, "confirm.ask.primary")
    _click_if_shown(user, "speak.voice.host-turbo")                     # a voice on another model, then the keys
    for command in ({"id": "gen", "combo": "Ctrl+Enter"}, {"id": "gen3", "combo": "Ctrl+Shift+Enter"}):
        user.find(marker="shell.keys").trigger("command", command)
    _click_if_shown(user, "speak.generate")
    await settle(core, fake)
    await visit(user, core, fake, "/models", via_rail=True)            # the Models cards
    for backend in (MTL, STD, TUR):
        _click_if_shown(user, f"models.switch.{backend}")
        await settle(core, fake)
        _click_if_shown(user, "confirm.ask.primary")
    await visit(user, core, fake, "/forge", via_rail=True)             # the Forge button
    _click_if_shown(user, "forge.go")
    await settle(core, fake)
    loads = [call.name for call in fake.calls if call.name in ("make_resident", "load_backend", "load_voice")]
    assert loads == [] and fake.loads == [] and core.state.job.kind == kind


# ---- tokens void after residency, job and library changes ---------------------------------------------

async def _free(user: User, core: Core, fake: FakeEngine) -> None:
    user.find(marker="dock.free").click()                               # a residency change
    await settle(core, fake)
    await until(lambda: not core.state.engine.resident, "the model to be freed")


async def _test_line(user: User, core: Core, fake: FakeEngine) -> None:
    assert core.jobs.start_test_line("aiko").status == "started"        # a job starts and ends
    await settle(core, fake)
    await until(lambda: not core.state.job.kind, "the test line")


async def _edit_a_voice(user: User, core: Core, fake: FakeEngine) -> None:
    rev = core.state.library.rev
    await visit(user, core, fake, "/voices", via_rail=True)             # a library change: a registry edit
    user.find(marker="voices.row.aiko").click()
    await settle(core, fake)
    user.find(marker="voices.edit.expr").trigger("change", 0.7)
    await until(lambda: core.state.library.rev != rev, "the new library snapshot")
    await visit(user, core, fake, "/", via_rail=True)


@pytest.mark.scenario("other_backend")
@pytest.mark.parametrize("change", [_free, _test_line, _edit_a_voice], ids=["residency", "job", "library"])
async def test_a_generate_token_is_void_after_a_change(user: User, core: Core, fake: FakeEngine,
                                                       change: Any) -> None:
    await first_page(user, core, fake)
    speak = behaviours_of(user).speak()
    pending = speak.vm.generate.pending
    assert pending is not None and core.tokens.peek(pending.token) is not None
    await change(user, core, fake)
    assert core.tokens.peek(pending.token) is None
    fake.reset_records()
    await speak.generate(1, pending.token)                              # the old token: nothing loads
    await settle(core, fake)
    assert fake.loads == [] and calls_of(fake, "make_resident") == []
    fresh = speak.vm.generate.pending
    assert fresh is not None and fresh.token != pending.token and "Turbo" in speak.vm.generate.label
    user.find(marker="speak.generate").click()                          # the fresh named control does
    await settle(core, fake)
    assert fake.loads == [TUR]


@pytest.mark.scenario("forge_other_backend")
async def test_a_forge_token_is_void_after_a_residency_change(user: User, core: Core, fake: FakeEngine,
                                                              open_route: Any) -> None:
    await open_route(user, "/forge")
    await settle(core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    forge = behaviours_of(user).forge()
    pending = forge.vm.forge.pending
    assert pending is not None and forge.vm.forge.label == "Switch to Turbo & Forge"
    await _free(user, core, fake)
    fake.reset_records()
    await forge.forge(pending.token)
    await settle(core, fake)
    assert fake.loads == [] and calls_of(fake, "load_voice") == []
    fresh = forge.vm.forge.pending
    assert forge.vm.forge.label == "Load Turbo & Forge" and fresh is not None and fresh.token != pending.token
    user.find(marker="forge.go").click()
    await settle(core, fake)
    await until(lambda: calls_of(fake, "load_voice") != [], "the bake")
    assert fake.loads == [TUR]
