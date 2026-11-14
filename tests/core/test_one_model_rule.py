"""The one-model rule in ui\\core (§4.5), before the pages land: who may load or free a model, and that
nothing does so by itself.

- Static (layers L3 and L4): only the facade touches the engine's model methods; only the job runner calls
  facade.load() and facade.unload(); every take, test line and bake the facade sends carries
  require_resident=True; only TokenStore builds a LoadTicket; only the popover (ModelsController) mints
  an asked token, the one kind that may redeem a low-VRAM plan.
- A sweep over every FakeEngine scenario: startup, the first client, every shell controller built and
  driven through its non-loading intents (routes, search picks, playback), and a minute of timers on the
  manual clock, with Warm Up Off and Auto-unload Never. The fake must record no load, unload, take, bake,
  edit or delete, and never more than one resident model.
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ui.behaviours.search import FREE_ID
from ui.contract.routes import ROUTES
from ui.core.fake import SCENARIOS

from tests.behaviours import factory_for, no_error_logs, offline, tick  # noqa: F401
from tests.core.test_facade import Rig, rigs  # noqa: F401

UI = Path(__file__).resolve().parents[2] / "ui"
FAKE = UI / "core" / "fake.py"
FACADE = UI / "core" / "facade.py"
JOBS = UI / "core" / "jobs.py"
TOKENS = UI / "core" / "tokens.py"
MODELS = UI / "behaviours" / "models.py"

ENGINE_MODEL_METHODS = {"make_resident", "load_backend", "unload_backend"}
MODEL_CALLS = {"make_resident", "load_backend", "unload_backend", "close", "speak", "load_voice",
               "update_voice", "delete_voice"}


def _sources() -> Iterator[tuple[Path, ast.Module]]:
    """Every module under ui\\ except the FakeEngine (a stand-in engine, not a caller)."""
    for path in sorted(UI.rglob("*.py")):
        if path != FAKE and "__pycache__" not in path.parts:
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _where(found: Callable[[ast.AST], bool]) -> set[Path]:
    return {path for path, tree in _sources() if any(found(node) for node in ast.walk(tree))}


def _named(node: ast.AST, name: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == name) or (isinstance(node, ast.Name) and node.id == name)


# ---------- static: layers L3 and L4 ----------

def test_only_the_facade_touches_the_engines_model_methods() -> None:
    assert _where(lambda n: isinstance(n, ast.Attribute) and n.attr in ENGINE_MODEL_METHODS) == {FACADE}


def test_only_the_job_runner_loads_or_frees_through_the_facade() -> None:
    def facade_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Attribute) and node.attr in ("load", "unload") and _named(node.value, "facade")

    assert _where(facade_call) == {JOBS}


def test_every_take_and_bake_the_facade_sends_requires_residency() -> None:
    tree = ast.parse(FACADE.read_text(encoding="utf-8"))
    sends = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and any(isinstance(arg, ast.Attribute) and arg.attr in ("speak", "load_voice")
                     and _named(arg.value, "_engine") for arg in node.args)]
    assert len(sends) == 2                                  # speak (takes and test lines) and load_voice (bakes)
    for call in sends:
        flags = {kw.arg: kw.value for kw in call.keywords}
        assert isinstance(flags.get("require_resident"), ast.Constant) and flags["require_resident"].value is True


def test_only_the_token_store_builds_load_tickets() -> None:
    assert _where(lambda n: isinstance(n, ast.Call) and _named(n.func, "LoadTicket")) == {TOKENS}


def test_only_the_popover_mints_asked_tokens() -> None:
    def asked_mint(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and _named(node.func, "mint")
                and any(kw.arg == "asked" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                        for kw in node.keywords))

    assert _where(asked_mint) == {MODELS}


# ---------- a sweep over every scenario ----------

@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_no_scenario_loads_or_frees_a_model_by_itself(rigs: Callable[..., Rig], scenario: str) -> None:
    rig = rigs(scenario)
    rig.fake.release_all()                                  # a regression then shows as a load, not a hang
    await rig.start()
    factory, _ = factory_for(rig)
    status, models, playback, search = factory.status(), factory.models(), factory.playback(), factory.search()
    factory.commands()
    for name in ("speak", "voices", "forge", "takes", "settings"):
        getattr(factory, name)()
    for route in ROUTES:
        status.on_route(route)
        status.refresh()
        models.refresh()
    for item in search.pool():
        if item.item_id != FREE_ID:                         # Free VRAM is a named intent
            search.run(item.item_id)
    playback.toggle()
    playback.prev()
    playback.next()
    for row in rig.state.library.rows:
        playback.audition(row.voice_id)
    await tick()
    await rig.clock.run_for(60.0, step=1.0)                 # device readings, idle checks, toasts, arms
    calls = sorted({call.name for call in rig.fake.calls} & MODEL_CALLS)
    assert (rig.fake.loads, calls) == ([], [])
    resident = rig.fake.residency().resident
    assert len(resident) <= 1 and rig.state.engine.resident == (resident[-1] if resident else "")
