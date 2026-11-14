"""StatusController (§5.2) on FakeEngine and the manual clock: every chip, dock and popover text (§6.2.7),
the load progress copy at 0.5×, 1.2× and 2× the estimate, Retry's accessible name, the Close arm, the log,
the toast, the route and the art layer."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from ui.behaviours import PAGE_CONTROLLERS, BehaviourFactory, NotBuilt, PageController
from ui.behaviours.confirm import ARM_DELETE, ARM_PURGE, ARM_QUIT, ARM_REPLACE
from ui.behaviours.status import (CLOSE_ARMED, CLOSE_LABEL, MINIMIZE_ONLY_NATIVE, QUIT_BUSY, TOAST_S, load_progress,
                                  load_text, normalise_route, row_state, then_text)
from engine.errors import BackendLoadError
from ui.contract.plans import AfterForge, AfterGenerate, ForgeRequest, SwitchPlan
from ui.contract.records import ClipInfo
from ui.core.fake import Hold

from tests.behaviours import (MTL, STD, TUR, FakeKit, StubSpeak, advance, factory_for, no_error_logs,  # noqa: F401
                              offline, started, texts, tick, watch_toasts)
from tests.core.test_facade import Rig, entered, request, rigs, turbo_request  # noqa: F401

TURBO_RULES = "English only · ignores Expressiveness and Guidance · understands [laugh] [chuckle] [cough] [sigh] [gasp]"


def token_for(rig: Rig, backend: str, *, reason: str = "explicit", asked: bool = False, after: Any = None) -> str:
    plan = rig.core.planner.switch_plan(backend, reason=reason, after=after)  # type: ignore[arg-type]
    return rig.core.tokens.mint(plan, asked=asked)


async def held_switch(rig: Rig, target: str, point: str = "make_resident") -> Hold:
    """A switch to target, parked at point ("evict": its unload phase; "make_resident": its load phase)."""
    hold = rig.fake.hold(point)
    assert rig.core.jobs.start_switch(token_for(rig, target)).status == "started"
    await entered(hold)
    await tick()
    return hold


async def finish(rig: Rig, hold: Hold) -> None:
    hold.release()
    await rig.settle()
    await tick()


def rows(factory: BehaviourFactory) -> dict[str, Any]:
    return {row.backend: row for row in factory.status().vm.dock_rows}


def a_clip(rig: Rig) -> ClipInfo:
    return ClipInfo(clip_id="c" * 32, name="sample.wav", source=None, staged=rig.paths.voices / "sample.wav",
                    in_library=True, duration_s=30.0, selection_s=13.42, samplerate=22050, channels=1, fmt="WAV",
                    subtype="PCM_16", peak_dbfs=-8.4, clipped=False, silent=False, peaks=())


# ---------- the chip ----------

async def test_a_resident_model_reads_ready_with_its_live_gb(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    vm = factory.status().vm
    assert (vm.chip_text, vm.chip_tone, vm.chip_dot) == ("Multilingual ready · 3.4 GB", "base", "gold")


async def test_nothing_resident_reads_no_model_loaded(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "firstrun"))
    vm = factory.status().vm
    assert (vm.chip_text, vm.chip_tone, vm.chip_dot) == ("No model loaded", "base", "hollow")


async def test_a_model_on_the_cpu_reads_cpu(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "cpu"))
    assert factory.status().vm.chip_text == "Multilingual ready · CPU"
    assert rows(factory)[MTL].info == "CPU"


async def test_before_the_first_reading_the_chip_has_no_gb(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start(connect=False)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    assert vm.chip_text == "Multilingual ready"
    assert (vm.vram_value, vm.vram_detail, vm.meter_visible) == ("—", "Detecting GPU…", True)
    assert rows(factory)[MTL].info == "—"


async def test_free_vram_shows_the_unloading_texts(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status, models = factory.status(), factory.models()
    hold = rig.fake.hold("unload_backend")
    await models.free_vram()
    await entered(hold)
    await tick()
    vm = status.vm
    assert (vm.chip_text, vm.chip_tone, vm.chip_dot) == ("Unloading Multilingual · freeing 3.4 GB", "gold", "gold")
    assert (vm.dock_status, vm.dock_status_tone) == ("Freeing 3.4 GB…", "gold-dim")
    mtl, std = rows(factory)[MTL], rows(factory)[STD]
    assert (mtl.state, mtl.info, mtl.info_tone, mtl.dim, mtl.enabled) == ("unloading", "Freeing", "gold-dim", False, False)
    assert (std.state, std.dim, std.enabled) == ("not_loaded", True, False)
    assert vm.free.enabled is False
    await finish(rig, hold)
    assert (vm.chip_text, vm.dock_status) == ("No model loaded", "No model loaded")


async def test_a_switch_frees_first_then_loads(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    hold = await held_switch(rig, TUR, point="evict")
    assert vm.chip_text == "Unloading Multilingual · freeing 3.4 GB"
    assert vm.dock_status == "Freeing 3.4 GB…"
    assert rows(factory)[MTL].state == "unloading" and rows(factory)[TUR].state == "not_loaded"
    load = rig.fake.hold("make_resident")
    hold.release()
    await entered(load)
    await tick()
    assert vm.chip_text == "Loading Turbo · 1 s of ~9 s"
    assert rows(factory)[TUR].state == "loading" and rows(factory)[MTL].state == "not_loaded"
    await finish(rig, load)
    assert vm.chip_text == "Turbo ready · 3.1 GB" and vm.dock_status == "Ready"


@pytest.mark.parametrize(("share", "chip", "dock", "button", "card", "row"), [
    (0.5, "Loading Turbo · 4 s of ~9 s", "Loading · ~5 s left", "Loading Turbo… 4 s of ~9 s",
     "Loading · 4 s of ~9 s", "4 s"),
    (1.2, "Loading Turbo · 10 s, usually ~9 s", "Loading · 10 s, usually ~9 s", "Loading Turbo… 10 s, usually ~9 s",
     "Loading · 10 s, usually ~9 s", "10 s"),
    (2.0, "Loading Turbo · 17 s, usually ~9 s", "Loading · 17 s, usually ~9 s", "Loading Turbo… 17 s, usually ~9 s",
     "Loading · 17 s, usually ~9 s", "17 s"),
])
async def test_the_load_progress_copy_in_all_five_places(rigs: Callable[..., Rig], share: float, chip: str, dock: str,
                                                         button: str, card: str, row: str) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status, models = factory.status(), factory.models()
    hold = await held_switch(rig, TUR)
    assert rig.core.planner.load_estimate(TUR).about_s == pytest.approx(8.7)
    await advance(rig, round(share * 8.7, 2))
    progress = load_progress(rig.core)
    assert progress is not None
    assert (status.vm.chip_text, status.vm.chip_tone) == (chip, "gold")
    assert (status.vm.dock_status, status.vm.dock_status_tone) == (dock, "gold")
    assert load_text(progress, "button") == button
    turbo_card = next(c for c in models.vm.cards if c.backend == TUR)
    assert (turbo_card.chip_text, turbo_card.chip_tone, turbo_card.state) == (card, "gold", "loading")
    turbo_row = rows(factory)[TUR]
    assert (turbo_row.info, turbo_row.info_tone, turbo_row.dim) == (row, "gold", False)
    assert turbo_row.ring == pytest.approx(min(0.95, rig.state.job.elapsed_s / 8.7), abs=1e-3)
    assert turbo_card.ring == turbo_row.ring
    await finish(rig, hold)


async def test_all_places_change_form_at_the_same_tick(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status = factory.status()
    hold = await held_switch(rig, TUR)
    await advance(rig, 8.6)
    assert status.vm.chip_text == "Loading Turbo · 9 s of ~9 s" and status.vm.dock_status == "Loading · ~1 s left"
    await advance(rig, 0.2)                              # 8.8 s ≥ 8.7 s: every place switches together
    assert status.vm.chip_text == "Loading Turbo · 9 s, usually ~9 s"
    assert status.vm.dock_status == "Loading · 9 s, usually ~9 s"
    assert load_text(load_progress(rig.core), "card") == "Loading · 9 s, usually ~9 s"
    await finish(rig, hold)


async def test_a_load_without_history_shows_elapsed_only(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    status, models = factory.status(), factory.models()
    hold = await held_switch(rig, TUR)
    await advance(rig, 12.0)
    assert status.vm.chip_text == "Loading Turbo · 12 s"
    assert status.vm.dock_status == "Loading · 12 s"
    assert load_text(load_progress(rig.core), "button") == "Loading Turbo… 12 s"
    assert next(c for c in models.vm.cards if c.backend == TUR).chip_text == "Loading · 12 s"
    turbo_row = rows(factory)[TUR]
    assert (turbo_row.info, turbo_row.ring) == ("12 s", None)     # an indeterminate arc
    await finish(rig, hold)


async def test_a_take_reads_generating_with_its_elapsed_and_queue(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()] * 3)
    await entered(hold)
    await advance(rig, 2.2)
    assert (vm.chip_text, vm.chip_tone) == ("Generating Take 8 · 2.2 s · 2 queued", "gold")
    assert (vm.dock_status, vm.dock_status_tone) == ("Busy · generating", "gold-dim")
    assert all(not row.enabled and row.dim for row in vm.dock_rows)
    rig.core.jobs.cancel_queue()
    await tick()
    assert vm.chip_text == "Generating Take 8 · 2.2 s"
    await finish(rig, hold)


async def test_a_test_line_reads_its_voice(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    hold = rig.fake.hold("speak")
    assert rig.core.jobs.start_test_line("aiko").status == "started"
    await entered(hold)
    await advance(rig, 1.2)
    assert vm.chip_text == "Generating test line · aiko · 1.2 s"
    await finish(rig, hold)


async def test_a_forge_reads_forging(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "forge_ready")
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / "sample.wav")
    hold = rig.fake.hold("load_voice")
    assert rig.core.jobs.start_forge(ForgeRequest("rin", clip, MTL, "ja", 0.5, True)).status == "started"
    await entered(hold)
    await tick()
    assert (vm.chip_text, vm.chip_tone) == ("Forging rin…", "gold")
    assert (vm.dock_status, vm.dock_status_tone) == ("Busy · forging", "gold-dim")
    await finish(rig, hold)


async def failed_load(rigs: Callable[..., Rig], **options: Any) -> tuple[Rig, BehaviourFactory]:
    """The failure board: "Load Anyway" unloaded Multilingual, then the Turbo load ran out of memory."""
    rig = await started(rigs, "failure", **options)
    factory, _ = factory_for(rig)
    factory.status()
    rig.core.jobs.start_switch(token_for(rig, TUR, asked=True))
    await rig.settle()
    await tick()
    return rig, factory


async def test_a_failed_load_turns_red_and_offers_retry(rigs: Callable[..., Rig]) -> None:
    rig, factory = await failed_load(rigs)
    vm = factory.status().vm
    assert (vm.chip_text, vm.chip_tone, vm.chip_dot) == ("Turbo failed to load", "red", "red")
    assert (vm.dock_status, vm.dock_status_tone) == ("Load failed · see Log", "bad")
    turbo = rows(factory)[TUR]
    assert (turbo.state, turbo.info, turbo.info_tone, turbo.enabled) == ("failed", "Failed", "bad", True)
    assert vm.show_failed_actions is True and vm.free.enabled is False
    assert (vm.retry.label, vm.retry.aria, vm.retry.tooltip, vm.retry.enabled) == (
        "Retry", "Retry loading Turbo", "Retry loading Turbo", True)
    assert vm.retry.pending is not None and vm.retry.pending.switch.to_backend == TUR
    assert (vm.load_default.label, vm.load_default.enabled) == ("Load Multilingual", True)
    assert vm.load_default.pending.switch.to_backend == MTL
    assert rig.fake.loads == [TUR]


async def test_load_default_hides_when_the_default_is_the_one_that_failed(rigs: Callable[..., Rig]) -> None:
    """D13: the second button names config.DEFAULT_BACKEND (Multilingual) and hides when that one failed."""
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    rig.fake.fail_next("make_resident", BackendLoadError(MTL, "out_of_memory", "CUDA out of memory", 5.0))
    assert rig.core.jobs.start_switch(token_for(rig, MTL)).status == "started"
    await rig.settle()
    await tick()
    assert rig.state.engine.failed == MTL and vm.show_failed_actions is True
    assert (vm.retry.aria, vm.load_default.label, vm.load_default.enabled) == ("Retry loading Multilingual", "", False)


async def test_load_default_follows_config_not_the_forge_default(rigs: Callable[..., Rig]) -> None:
    """Settings › Default Model for New Voices is Forge's default; the failure button keeps config's (D13)."""
    rig, factory = await failed_load(rigs, settings={"default_backend": TUR})
    vm = factory.status().vm
    assert (vm.load_default.label, vm.load_default.pending.switch.to_backend) == ("Load Multilingual", MTL)


async def test_retry_on_low_vram_opens_the_ask(rigs: Callable[..., Rig]) -> None:
    rig, factory = await failed_load(rigs)
    req = factory.models().request_switch(TUR, origin="retry")
    vm = factory.status().vm
    assert req is not None and vm.ask == req
    assert (req.primary, vm.ask_dont_visible) == ("Load Anyway", False)
    assert rig.fake.loads == [TUR]                       # nothing more loaded


# ---------- the dock and the VRAM block ----------

async def test_dock_rows_in_the_steady_state(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    found = rows(factory)
    assert list(found) == [MTL, STD, TUR]
    assert [(r.label, r.state, r.info, r.info_tone, r.enabled, r.dim, r.ring) for r in found.values()] == [
        ("Multilingual", "resident", "3.4 GB", "gold-dim", True, False, None),
        ("Standard", "not_loaded", "~12 s", "faint", True, False, None),
        ("Turbo", "not_loaded", "~9 s", "faint", True, False, None),
    ]
    assert found[TUR].tip == ("Turbo · English only · fastest · tags · ignores Expressiveness and Guidance · "
                              "needs a clip over 5 s")
    vm = factory.status().vm
    assert (vm.dock_status, vm.dock_status_tone) == ("Ready", "muted")
    assert (vm.vram_value, vm.vram_gold, vm.vram_detail, vm.meter_visible) == ("3.4 GB", True, "2.5 other · 6.1 free", True)
    assert vm.vram_tip == "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"
    assert vm.vram_app_pct == pytest.approx(28.35, abs=0.1) and vm.vram_other_pct == pytest.approx(20.84, abs=0.1)
    assert (vm.free.label, vm.free.enabled, vm.free.tone) == ("Free VRAM", True, "plain")
    assert vm.show_failed_actions is False


async def test_without_history_the_row_is_blank_and_its_title_says_so(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "firstrun"))
    turbo = rows(factory)[TUR]
    assert (turbo.info, turbo.state) == ("", "not_loaded")
    assert turbo.tip.endswith(" · Not loaded yet on this PC")
    vm = factory.status().vm
    assert (vm.dock_status, vm.vram_value, vm.vram_gold, vm.vram_detail) == (
        "No model loaded", "0.0 GB", False, "2.5 other · 9.5 free")
    assert (vm.free.enabled, vm.free.reason) == (False, "Nothing to free · no model loaded")


async def test_without_cuda_the_meter_hides(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "cpu"))
    vm = factory.status().vm
    assert (vm.vram_value, vm.meter_visible, vm.vram_detail) == ("CPU", False, "No CUDA GPU · models run on the CPU")


async def test_a_load_flashes_and_sparks_its_row(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    before = rows(factory)[STD]
    rig.core.jobs.start_switch(token_for(rig, STD))
    await rig.settle()
    await tick()
    after = rows(factory)[STD]
    assert (after.flash_seq, after.spark_seq) == (before.flash_seq + 1, before.spark_seq + 1)
    assert after.state == "resident" and row_state(rig.core, STD) == "resident"


# ---------- the popover (§6.2.7) ----------

async def test_the_confirm_ask_from_the_dock(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status = factory.status()
    req = factory.models().request_switch(TUR, origin="dock")
    vm = status.vm
    assert req is not None and vm.ask == req
    assert (req.kind, req.style, req.title, req.primary) == ("switch_model", "ask", "Switch to Turbo", "Switch Model")
    assert req.lines == (("Unloads Multilingual · frees 3.4 GB", "text-soft"),
                         ("Loads Turbo · about 9 s (last 8.7 s) · 3.1 GB", "text-soft"),
                         (TURBO_RULES, "muted"))
    assert (vm.ask_dont_visible, vm.ask_dont, vm.ask_then) == (True, False, "")
    factory.models().set_dont_ask(True)
    assert vm.ask_dont is True
    assert rig.fake.loads == []


@pytest.mark.parametrize(("backend", "rules"), [
    (MTL, "23 languages · Expressiveness and Guidance"),
    (STD, "English only · Expressiveness and Guidance"),
    (TUR, TURBO_RULES),
])
async def test_the_rules_line_of_each_backend(rigs: Callable[..., Rig], backend: str, rules: str) -> None:
    factory, _ = factory_for(await started(rigs, "firstrun"))
    req = factory.models().request_switch(backend, origin="dock")
    assert req is not None and req.lines[-1] == (rules, "muted")
    assert req.title == f"Switch to {backend.removeprefix('chatterbox-').capitalize()}"


async def test_a_first_load_names_the_weights_size(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs, "firstrun"))
    req = factory.models().request_switch(TUR, origin="dock")
    assert req is not None
    assert req.lines[0] == ("Loads Turbo · first load on this PC · about 2.8 GB of weights", "text-soft")
    assert len(req.lines) == 2                           # nothing resident: no "Unloads" line


async def test_low_vram_asks_with_load_anyway_and_no_dont_ask(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "lowvram", settings={"confirm_switches": False})
    factory, _ = factory_for(rig)
    status = factory.status()
    req = factory.models().request_switch(TUR, origin="dock")
    assert req is not None and req.primary == "Load Anyway"
    assert req.lines[-1] == ("Low VRAM · 3.5 GB free · close other GPU apps", "warn")
    assert (status.vm.ask_dont_visible, status.vm.ask_then) == (False, "")
    factory.models().set_dont_ask(True)
    assert status.vm.ask_dont is False                   # D20: no toggle on a low-VRAM ask
    assert rig.fake.loads == []


def generate_plan(rig: Rig, count: int) -> SwitchPlan:
    return rig.core.planner.switch_plan(TUR, reason="generate", after=AfterGenerate((turbo_request(),) * count))


@pytest.mark.parametrize(("count", "line"), [(1, "Then generates take 8"), (3, "Then generates takes 8-10")])
async def test_a_named_generate_on_low_vram_names_its_takes(rigs: Callable[..., Rig], count: int, line: str) -> None:
    rig = await started(rigs, "lowvram")
    factory, _ = factory_for(rig)
    status = factory.status()
    req = factory.models().ask(generate_plan(rig, count))
    assert req.primary == "Load Anyway"
    assert (status.vm.ask_then, status.vm.ask_dont_visible) == (line, False)
    assert then_text(rig.core, req.switch) == line


async def test_a_named_forge_on_low_vram_names_the_voice(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "forge_low_vram")
    factory, _ = factory_for(rig)
    status = factory.status()
    forge = ForgeRequest("rin", a_clip(rig), TUR, "en", 0.5, True)
    factory.models().ask(rig.core.planner.switch_plan(TUR, reason="forge", after=AfterForge(forge)))
    assert (status.vm.ask_then, status.vm.ask_dont_visible) == ("Then forges rin", False)
    assert status.vm.ask.primary == "Load Anyway"


async def test_the_popover_closes_when_a_job_starts(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status = factory.status()
    factory.models().request_switch(STD, origin="dock")
    assert status.vm.ask is not None
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])
    await entered(hold)
    await tick()
    assert status.vm.ask is None and factory.ask.request is None
    await finish(rig, hold)


# ---------- Close, Minimize, the log and toasts ----------

async def test_close_while_idle_quits(rigs: Callable[..., Rig]) -> None:
    factory, kit = factory_for(await started(rigs))
    vm = factory.status().vm
    assert (vm.close.label, vm.close.aria, vm.close.tone, vm.close.armed) == (CLOSE_LABEL, CLOSE_LABEL, "danger", False)
    factory.status().request_quit()
    assert kit.called("quit") == [{}]


async def test_close_while_busy_arms_first(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, kit = factory_for(rig)
    status = factory.status()
    seen = watch_toasts(rig)
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])
    await entered(hold)
    status.request_quit()
    assert kit.called("quit") == []
    assert (status.vm.close.label, status.vm.close.tooltip, status.vm.close.armed) == (CLOSE_ARMED, CLOSE_ARMED, True)
    assert texts(seen) == [(QUIT_BUSY, False)]
    rig.clock.advance(3.1)
    assert status.vm.close.armed is False and status.vm.close.label == CLOSE_LABEL
    status.request_quit()
    rig.clock.advance(2.9)
    status.request_quit()
    assert kit.called("quit") == [{}] and not factory.arm.is_armed(ARM_QUIT)
    await finish(rig, hold)


async def test_minimize_without_a_window_explains_itself(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    seen = watch_toasts(rig)
    factory, kit = factory_for(rig, FakeKit(minimize_result=False))
    factory.status().minimize()
    assert kit.called("minimize") == [{}] and texts(seen) == [(MINIMIZE_ONLY_NATIVE, False)]
    native, native_kit = factory_for(rig)
    native.status().minimize()
    assert native_kit.called("minimize") == [{}] and len(seen) == 1


async def test_the_log_toggles_from_the_chip_and_closes(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    status = factory.status()
    status.toggle_log()
    assert status.vm.log_open is True
    status.toggle_log()
    status.toggle_log()
    status.close_log()
    assert status.vm.log_open is False


async def test_one_toast_at_a_time_for_two_point_six_seconds(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    first = rig.core.events.say("Take 8 ready · 4.6 s")
    assert vm.toast == first
    rig.clock.advance(2.0)
    second = rig.core.events.say("Couldn't save the star: disk full", bad=True)
    assert vm.toast == second and second.bad
    rig.clock.advance(2.0)
    assert vm.toast == second                            # the first one's timer no longer applies
    rig.clock.advance(TOAST_S - 2.0)
    assert vm.toast is None
    rig.core.events.say("Freed 3.4 GB")
    factory.status().dismiss_toast()
    assert vm.toast is None


# ---------- the route ----------

@pytest.mark.parametrize(("path", "route", "title"), [
    ("/", "/", "Speak"), ("/voices", "/voices", "Voices"), ("/forge", "/forge", "Forge"),
    ("/takes?tab=all", "/takes", "Takes"), ("/models/", "/models", "Models"), ("/settings#keys", "/settings", "Settings"),
    ("", "/", "Speak"),
])
async def test_the_route_sets_the_title(rigs: Callable[..., Rig], path: str, route: str, title: str) -> None:
    factory, _ = factory_for(await started(rigs))
    status = factory.status()
    status.on_route(path)
    assert (status.vm.route, status.vm.title) == (route, title)
    assert normalise_route(path) == route


async def test_navigation_closes_and_disarms_what_the_mockup_does(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    status = factory.status()
    speak = StubSpeak(factory)
    factory.provide("speak", speak)

    class Settings:
        stopped = 0

        def stop_listening(self) -> None:
            Settings.stopped += 1

    factory.provide("settings", Settings())
    search = factory.search()
    search.results("aiko")
    factory.models().request_switch(TUR, origin="dock")
    for key in (ARM_QUIT, ARM_DELETE, ARM_PURGE, ARM_REPLACE):
        factory.arm.press(key)
    status.on_route("/forge")
    assert status.vm.ask is None and factory.arm.armed == (ARM_QUIT, ARM_REPLACE)
    assert speak.lists == [False] and Settings.stopped == 1
    assert (search.query, status.vm.search_items) == ("", ())
    status.on_route("/voices")                           # leaving Forge clears its replace arm (D-F3)
    assert factory.arm.armed == (ARM_QUIT,)


async def test_navigation_works_while_the_pages_are_placeholders(rigs: Callable[..., Rig],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = factory_for(await started(rigs))
    for name in ("speak", "voices", "forge", "settings"):
        spec = PAGE_CONTROLLERS[name]
        monkeypatch.setitem(PAGE_CONTROLLERS, name, PageController(f"ui.behaviours.{name}_missing", spec.cls,
                                                                   spec.protocol, spec.vm))
        getattr(factory, name)()
    status = factory.status()
    status.on_route("/settings")
    assert status.vm.title == "Settings" and isinstance(factory.settings(), NotBuilt)


# ---------- art, motion and the search badge ----------

async def test_the_art_follows_the_page_and_the_library(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs, "firstrun")
    factory, _ = factory_for(rig)
    status = factory.status()
    status.on_route("/")
    assert (status.vm.art_page, status.vm.art_visible, status.vm.script_dark) == ("speak", True, True)
    status.on_route("/forge")
    assert (status.vm.art_page, status.vm.art_visible) == ("forge", True)
    rig.core.settings.set("theme.emiya", "saber_art", "hide")
    assert (status.vm.art_page, status.vm.art_visible, status.vm.script_dark) == ("forge", False, True)
    status.on_route("/takes")
    assert (status.vm.art_page, status.vm.art_visible) == ("", False)


async def test_with_voices_speak_has_no_art_and_a_light_script_card(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    status = factory.status()
    status.on_route("/")
    assert (status.vm.art_page, status.vm.art_visible, status.vm.script_dark) == ("", False, False)


async def test_reduce_motion_always_stills_the_logo(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig)
    vm = factory.status().vm
    assert (vm.reduce_always, vm.logo_still) == (False, False)
    rig.core.settings.set("core", "reduce_motion", "always")
    assert (vm.reduce_always, vm.logo_still) == (True, True)


async def test_the_search_badge_follows_the_binding(rigs: Callable[..., Rig]) -> None:
    factory, _ = factory_for(await started(rigs))
    vm = factory.status().vm
    assert vm.search_badge == "Ctrl K"
    assert factory.commands().bind("search", "key", "").ok
    assert vm.search_badge == ""
    assert factory.commands().bind("search", "alt", "Ctrl+Shift+F").ok
    assert vm.search_badge == "Ctrl Shift F"
