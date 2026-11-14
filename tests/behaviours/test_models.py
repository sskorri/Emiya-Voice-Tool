"""ModelsController (§5.3, §6.11): the request_switch table per origin, the popover's primary, Cancel and
"Don't ask again" (D4, D20), Free VRAM, the Behavior settings and the Models page view model."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ui.behaviours import BehaviourFactory
from ui.contract.plans import AfterGenerate
from ui.core.facade import BUSY, ONE_MODEL

from tests.behaviours import (MTL, STD, TUR, advance, factory_for, no_error_logs, offline, started,  # noqa: F401
                              texts, tick, watch_toasts)
from tests.core.test_facade import Rig, entered, request, rigs, turbo_request  # noqa: F401


def cards(factory: BehaviourFactory) -> dict[str, Any]:
    return {card.backend: card for card in factory.models().vm.cards}


async def busy_with_a_take(rig: Rig) -> Any:
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])
    await entered(hold)
    await tick()
    return hold


async def release(rig: Rig, hold: Any) -> None:
    hold.release()
    await rig.settle()
    await tick()


# ---------- request_switch (§5.3 table) ----------

@pytest.mark.parametrize("origin", ["dock", "card", "retry", "default"])
async def test_a_busy_slot_or_the_resident_model_does_nothing(rigs: Callable[..., Rig], origin: str) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    assert models.request_switch(MTL, origin=origin) is None          # already resident
    hold = await busy_with_a_take(rig)
    assert models.request_switch(TUR, origin=origin) is None
    assert factory.ask.request is None
    await release(rig, hold)
    assert rig.fake.loads == []


@pytest.mark.parametrize("origin", ["dock", "card", "retry", "default"])
async def test_low_vram_always_asks_load_anyway(rigs: Callable[..., Rig], origin: str) -> None:
    rig = await started(rigs, "lowvram", settings={"confirm_switches": False})
    factory, _ = factory_for(rig)
    req = factory.models().request_switch(TUR, origin=origin)
    assert req is not None and req.primary == "Load Anyway" and factory.ask.request == req
    assert factory.ask.dont_visible is False
    assert rig.core.tokens.peek(req.token) is not None and rig.fake.loads == []


async def test_a_dock_row_asks_while_confirm_model_switches_is_on(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    req = factory.models().request_switch(TUR, origin="dock")
    assert req is not None and (req.primary, req.title) == ("Switch Model", "Switch to Turbo")
    assert factory.ask.dont_visible is True and rig.fake.loads == []


@pytest.mark.parametrize("origin", ["card", "retry", "default"])
async def test_named_controls_never_ask_without_low_vram(rigs: Callable[..., Rig], origin: str) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    assert rig.core.settings.get("core", "confirm_switches") is True
    assert factory.models().request_switch(STD, origin=origin) is None
    assert factory.ask.request is None and rig.state.job.kind == "switch"
    await rig.settle()
    assert rig.fake.loads == [STD] and rig.state.engine.resident == STD


async def test_a_dock_row_switches_at_once_with_confirm_off(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, settings={"confirm_switches": False})
    factory, _ = factory_for(rig)
    assert factory.models().request_switch(TUR, origin="dock") is None
    await rig.settle()
    assert rig.fake.loads == [TUR] and rig.state.engine.resident == TUR


async def test_unknown_backends_and_origins_are_refused(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    assert models.request_switch("chatterbox-nope", origin="card") is None
    assert models.request_switch(TUR, origin="menu") is None  # type: ignore[arg-type]
    assert rig.state.job.kind == "" and rig.fake.loads == []


async def test_the_same_row_again_closes_the_ask_and_another_row_retargets(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    models.request_switch(TUR, origin="dock")
    assert models.request_switch(TUR, origin="dock") is None and factory.ask.request is None
    models.request_switch(TUR, origin="dock")
    standard = models.request_switch(STD, origin="dock")
    assert standard is not None and factory.ask.request.title == "Switch to Standard"
    assert rig.fake.loads == []


# ---------- the popover ----------

async def test_the_primary_switches_and_closes_the_popover(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    req = models.request_switch(TUR, origin="dock")
    await models.confirm(req.token)
    assert factory.ask.request is None and rig.state.job.kind == "switch"
    await rig.settle()
    assert rig.fake.loads == [TUR]
    assert rig.core.settings.get("core", "confirm_switches") is True


async def test_dont_ask_again_commits_only_on_the_primary(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    models.request_switch(TUR, origin="dock")
    models.set_dont_ask(True)
    models.dismiss()                                     # Cancel with the tick on: nothing is saved (D4)
    assert rig.core.settings.get("core", "confirm_switches") is True and factory.ask.request is None
    req = models.request_switch(TUR, origin="dock")
    models.set_dont_ask(True)
    await models.confirm(req.token)
    assert rig.core.settings.get("core", "confirm_switches") is False
    assert models.vm.confirm_switches is False
    await rig.settle()
    assert rig.fake.loads == [TUR]


async def test_a_low_vram_primary_loads_anyway(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "lowvram")
    factory, _ = factory_for(rig)
    models = factory.models()
    req = models.request_switch(TUR, origin="dock")
    await models.confirm(req.token)
    await rig.settle()
    assert rig.fake.loads == [TUR]


async def test_the_after_action_rides_on_the_primary(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "lowvram")
    factory, _ = factory_for(rig)
    models = factory.models()
    plan = rig.core.planner.switch_plan(TUR, reason="generate", after=AfterGenerate((turbo_request(),)))
    req = models.ask(plan)
    await models.confirm(req.token)
    await rig.settle()
    assert rig.fake.loads == [TUR]
    assert rig.state.takes.takes[0].id == 8 and rig.state.takes.takes[0].voice == "host-turbo"


async def test_a_stale_click_or_an_unknown_token_loads_nothing(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    req = models.request_switch(TUR, origin="dock")
    await models.confirm("not-the-token")
    assert factory.ask.request == req and rig.state.job.kind == ""
    rig.core.tokens.void_all()
    await models.confirm(req.token)                      # void: recompute; the popover stays, renewed
    renewed = factory.ask.request
    assert renewed is not None and renewed.token != req.token
    assert rig.core.tokens.peek(renewed.token) is not None and rig.fake.loads == []
    await models.confirm(renewed.token)
    await rig.settle()
    assert rig.fake.loads == [TUR]


async def test_a_library_change_renews_the_open_ask(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    req = factory.models().request_switch(TUR, origin="dock")
    rig.fake.update_voice("marco", exaggeration=0.7)     # cli.py edits the registry: a new snapshot ...
    await rig.core.refresh_library()                     # ... voids every token
    await tick()
    renewed = factory.ask.request
    assert renewed is not None and renewed.token != req.token     # ... and the popover gets a fresh one
    assert rig.core.tokens.peek(renewed.token) is not None


async def test_a_residency_change_closes_the_ask(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    factory.models().request_switch(TUR, origin="dock")
    rig.core.facade.set_engine(failed=STD)
    await tick()
    assert factory.ask.request is None


async def test_the_popover_copy_is_live(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    factory.models().request_switch(TUR, origin="dock")
    rig.fake.set_other_gb(8.5)                           # another program takes VRAM while it is open
    rig.clock.advance(2.0)                               # the next 2 s device reading sees it
    await rig.settle()
    await tick()
    req = factory.ask.request
    assert req.primary == "Load Anyway" and req.lines[-1][1] == "warn"
    assert factory.ask.dont_visible is False             # D20: a low-VRAM ask has no "Don't ask again"


async def test_dont_ask_again_follows_the_low_vram_state_of_an_open_ask(rigs: Callable[..., Rig]) -> None:
    """A Confirm-caused ask that turns low hides the toggle and clears its tick (the primary saves
    nothing); once VRAM is back the toggle returns, unticked. An ask a low plan caused never gets it."""
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    models.request_switch(TUR, origin="dock")
    models.set_dont_ask(True)
    assert (factory.ask.dont_visible, factory.ask.dont) == (True, True)
    rig.fake.set_other_gb(8.5)
    rig.clock.advance(2.0)
    await rig.settle()
    await tick()
    assert (factory.ask.request.primary, factory.ask.dont_visible, factory.ask.dont) == ("Load Anyway", False, False)
    assert factory.status().vm.ask_dont_visible is False
    rig.fake.set_other_gb(2.5)
    rig.clock.advance(2.0)
    await rig.settle()
    await tick()
    assert (factory.ask.request.primary, factory.ask.dont_visible, factory.ask.dont) == ("Switch Model", True, False)
    rig.fake.set_other_gb(8.5)
    rig.clock.advance(2.0)
    await rig.settle()
    await tick()
    await models.confirm(factory.ask.request.token)
    await rig.settle()
    assert rig.fake.loads == [TUR] and rig.core.settings.get("core", "confirm_switches") is True


async def test_a_low_vram_ask_never_shows_dont_ask_again(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "lowvram")
    factory, _ = factory_for(rig)
    factory.models().request_switch(TUR, origin="dock")
    assert factory.ask.dont_visible is False
    rig.fake.set_other_gb(2.5)                           # VRAM frees up while it is open
    rig.clock.advance(2.0)
    await rig.settle()
    await tick()
    assert (factory.ask.request.primary, factory.ask.dont_visible) == ("Switch Model", False)


# ---------- Free VRAM ----------

async def test_free_vram_starts_the_unload(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    seen = watch_toasts(rig)
    await factory.models().free_vram()
    await rig.settle()
    assert rig.state.engine.resident == "" and texts(seen) == [("Freed 3.4 GB", False)]


async def test_free_vram_refusals_are_red_toasts(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    seen = watch_toasts(rig)
    await factory.models().free_vram()
    assert texts(seen) == [("Nothing to free · no model loaded", True)]
    busy = await started(rigs)
    busy_factory, _ = factory_for(busy)
    busy_seen = watch_toasts(busy)
    hold = await busy_with_a_take(busy)
    assert busy_factory.models().free().reason == BUSY
    assert texts(busy_seen) == [(BUSY, True)]
    await release(busy, hold)


# ---------- the Behavior settings ----------

async def test_behaviour_settings_are_saved_and_shown(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    assert (models.vm.warmup, models.vm.idle_min, models.vm.confirm_switches) == ("off", 10, True)
    models.set_warmup("last")
    models.set_idle_unload(30)
    models.set_confirm_switches(False)
    settings = rig.core.settings
    assert (settings.get("core", "warmup"), settings.get("core", "idle_unload_min"),
            settings.get("core", "confirm_switches")) == ("last", 30, False)
    assert (models.vm.warmup, models.vm.idle_min, models.vm.confirm_switches) == ("last", 30, False)


async def test_invalid_behaviour_values_are_refused(rigs: Callable[..., Rig], caplog: pytest.LogCaptureFixture) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    models = factory.models()
    models.set_warmup("always")  # type: ignore[arg-type]
    models.set_idle_unload(15)
    assert (rig.core.settings.get("core", "warmup"), rig.core.settings.get("core", "idle_unload_min")) == ("off", 10)
    assert "Refused the setting" in caplog.text


# ---------- the Models page ----------

async def test_cards_in_the_steady_state(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    found = cards(factory)
    mtl, std, tur = found[MTL], found[STD], found[TUR]
    assert (mtl.chip_text, mtl.chip_tone, mtl.state, mtl.ringed) == ("Resident", "gold", "resident", True)
    assert (mtl.action.label, mtl.action.tone, mtl.action_kind, mtl.action.enabled) == (
        "Unload · Free 3.4 GB", "plain", "unload", True)
    assert (std.chip_text, std.chip_tone, std.ringed) == ("Not loaded", "off", False)
    assert (std.action.label, std.action.aria, std.action.tone, std.action_kind) == (
        "Switch to This Model", "Switch to Standard", "primary", "switch")
    assert [(f.label, f.value) for f in mtl.facts] == [
        ("Languages", "23 languages"), ("Controls", "Expressiveness · Guidance"), ("Reference clip", "6–15 s ideal"),
        ("Measured VRAM", "3.4 GB"), ("Last load", "14.2 s"), ("Voices", "3")]
    assert [(f.label, f.value) for f in tur.facts] == [
        ("Languages", "English only"), ("Controls", "Tags · no Expr or Guid"), ("Reference clip", "Over 5 s"),
        ("Measured VRAM", "3.1 GB"), ("Last load", "8.7 s"), ("Voices", "1")]
    assert tur.tip == ("Turbo · English only · fastest · tags · ignores Expressiveness and Guidance · "
                       "needs a clip over 5 s")


async def test_cards_on_a_first_run(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "firstrun"))
    turbo = cards(factory)[TUR]
    assert (turbo.action.label, turbo.action.aria, turbo.action_kind) == ("Load This Model", "Load Turbo", "load")
    vram = next(f for f in turbo.facts if f.label == "Measured VRAM")
    assert (vram.value, vram.tip) == ("—", "Measured after its first load")
    assert next(f.value for f in turbo.facts if f.label == "Last load") == "—"
    assert turbo.tip.endswith(" · Not loaded yet on this PC")
    vm = factory.models().vm
    assert (vm.history, vm.history_empty) == ((), True)
    assert (vm.free.enabled, vm.free.reason) == (False, "Nothing to free · no model loaded")


async def test_cards_on_the_cpu(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "cpu"))
    found = cards(factory)
    assert all(next(f.value for f in card.facts if f.label == "Measured VRAM") == "On CPU" for card in found.values())
    assert found[MTL].action.label == "Unload"


async def test_cards_while_busy_are_grey_and_disabled(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    hold = await busy_with_a_take(rig)
    found = cards(factory)
    assert all(not card.action.enabled and card.action.reason == BUSY for card in found.values())
    assert (found[STD].action.tone, found[MTL].action.tone) == ("grey", "plain")
    assert factory.models().vm.free.enabled is False
    await release(rig, hold)


async def test_a_card_being_freed_and_a_failed_card(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    hold = rig.fake.hold("unload_backend")
    factory.models().free()
    await entered(hold)
    await tick()
    assert (cards(factory)[MTL].chip_text, cards(factory)[MTL].chip_tone) == ("Freeing…", "gold")
    await release(rig, hold)
    failure = await started(rigs, "failure")
    failed_factory, _ = factory_for(failure)
    failure.core.jobs.start_switch(failure.core.tokens.mint(
        failure.core.planner.switch_plan(TUR, reason="explicit"), asked=True))
    await failure.settle()
    await tick()
    turbo = cards(failed_factory)[TUR]
    assert (turbo.chip_text, turbo.chip_tone, turbo.state) == ("Failed", "red", "failed")
    assert cards(failed_factory)[MTL].action.label == "Load This Model"


async def test_a_load_flashes_its_card(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    before = cards(factory)[TUR].flash_seq
    factory.models().request_switch(TUR, origin="card")
    await rig.settle()
    await tick()
    assert cards(factory)[TUR].flash_seq == before + 1 and cards(factory)[TUR].chip_text == "Resident"


async def test_the_vram_card_legend_gpu_line_and_limit(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    vm = factory.models().vm
    assert (vm.legend_app, vm.legend_other, vm.legend_free) == (
        "This app · 3.4 GB · Multilingual", "Other apps · 2.5 GB", "Free · 6.1 GB")
    assert vm.vram_tip == "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"
    assert vm.gpu_line == "NVIDIA GeForce RTX 4070 Ti · 12.0 GB · detected at startup"
    assert vm.gpu_tip.startswith("Detected: name and size from torch.cuda.get_device_properties(0)")
    assert vm.limit_text == "One at a time"
    assert vm.limit_tip == ("MAX_RESIDENT_BACKENDS = 1 in config.py · switching always unloads the current model "
                            "before the next one loads · RTX 4070 Ti · 12.0 GB")
    assert vm.history[-1] == ("Today 13:57:16  Loaded chatterbox-multilingual · 14.2 s · 3.4 GB", "today")
    assert vm.history_empty is False


async def test_the_limit_before_detection_and_on_the_cpu(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start(connect=False)
    early, _ = factory_for(rig)
    vm = early.models().vm
    assert vm.limit_tip.endswith("before the next one loads")
    assert (vm.gpu_line, vm.legend_other, vm.legend_free) == ("Detecting the GPU…", "Other apps · —", "Free · —")
    cpu, _ = factory_for(await started(rigs, "cpu"))
    assert cpu.models().vm.limit_tip.endswith(" · CPU")
    assert cpu.models().vm.gpu_line == "No CUDA GPU detected · models run on the CPU"


async def test_more_than_one_model_in_vram_is_not_supported(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, max_resident=2))
    vm = factory.models().vm
    assert vm.limit_text == ONE_MODEL and vm.limit_tip.startswith("MAX_RESIDENT_BACKENDS = 2 in config.py")


async def test_the_legend_drops_the_model_name_during_a_switch(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    hold = rig.fake.hold("evict")
    factory.models().request_switch(TUR, origin="card")
    await entered(hold)
    await advance(rig, 0.2)
    assert factory.models().vm.legend_app == "This app · 3.4 GB"
    await release(rig, hold)
