"""The Models page under NiceGUI's user fixture (§6.11, P5): gui.py on a FakeEngine scenario in tmp_path.

Every row of §6.11 in the boards it belongs to (models, switch, lowvram, failure, loading, freeing, cpu,
firstrun): the cards (facts, chip, dot or ring, the named action and its markers, the gold ring and the flash
after a load), the VRAM card (meter, legend, GPU line, Free VRAM), the Behavior rows and the locked "Models in
VRAM" fact (limit_tip before detection, once detected, on the CPU, and the n != 1 refusal), and the Load History
with its empty state. A card's Load / Switch loads through a redeemed token, low VRAM asks, Unload and Free VRAM
free the model, idle unload applies live, and the page's wiring rows (tests\\pages\\wiring\\models.py) reach
their intents. Each test opens /models directly, so no other page is built.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nicegui import binding, ui
from nicegui.element import Element
from nicegui.testing import User

from ui.behaviours.models import ModelsController
from ui.behaviours.status import NOT_LOADED_YET
from ui.contract.markers import REQUIRED_MARKERS
from ui.contract.routes import TITLES
from ui.core import Core, catalog
from ui.core.clock import ManualScheduler
from ui.core.facade import BUSY, ONE_MODEL
from ui.core.fake import FakeEngine
from ui.core.services.device import GPU_TIP
from ui.core.services.load_history import MONTHS
from ui.core.tokens import LoadTicket
from ui.themes.emiya.pages.models import (
    CARDS_TITLE,
    CONFIRM_LABEL,
    CONFIRM_NOTE,
    HISTORY_EMPTY,
    LIMIT_LABEL,
    LIMIT_NOTE,
    ModelCard,
    ModelCards,
)
from ui.themes.emiya.widgets import DotSwitch, EmiyaButton, Meter, Segmented, TextSpan

from tests.pages.wiring import Wire
from tests.pages.wiring.models import WIRING

ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / "ui" / "themes" / "emiya"
PAGE_PY = THEME / "pages" / "models.py"
PAGE_CSS = THEME / "static" / "pages" / "models.css"
THEME_CSS = THEME / "static" / "emiya.css"
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
ORDER = (MTL, STD, TUR)
LIMIT_BASE = ("MAX_RESIDENT_BACKENDS = 1 in config.py · switching always unloads the current model "
              "before the next one loads")
NOTHING_TO_FREE = "Nothing to free · no model loaded"
FLASH_CLASSES = {"em-flash", "em-flash-b"}


# ---- helpers ------------------------------------------------------------------------------------------

def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def the(user: User, marker: str) -> Any:
    """The one shown element with this marker."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def shown(user: User, marker: str) -> bool:
    try:
        user.find(marker=marker)
    except AssertionError:
        return False
    return True


def classes(element: Element) -> set[str]:
    return set(element.classes)


def children(element: Element) -> list[Element]:
    return list(element.default_slot.children)


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Wait (real time) until predicate() holds; the FakeEngine works on worker threads."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks start their jobs, then wait for every job the scenario does not hold (a held job only
    until it reached its hold)."""
    for _ in range(5):
        await asyncio.sleep(0)
    held = bool(fake.scenario.holds)
    if held:
        await until(lambda: not core.state.job.kind or any(hold.entered.is_set() for hold in fake.holds),
                    "the scenario's held call")
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if held else [])
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine) -> None:
    """Open /models directly (no other page builds) and let the scenario's replay and first reading finish."""
    await user.open("/models")
    await settle(core, fake)


def cards(user: User) -> dict[str, ModelCard]:
    grid = the(user, "models.cards")
    assert isinstance(grid, ModelCards)
    found = {card.backend: card for card in children(grid) if isinstance(card, ModelCard)}
    assert all(grid.card(backend) is card for backend, card in found.items())
    return found


def facts(card: ModelCard) -> list[tuple[str, str]]:
    return [(children(row)[0].text, children(row)[1].text) for row in children(card.fact_box)]


def fact_tip(card: ModelCard, label: str) -> str | None:
    row = next(row for row in children(card.fact_box) if children(row)[0].text == label)
    return children(row)[1].props.get("title")


def history_lines(user: User) -> list[tuple[str, str | None]]:
    host = the(user, "models.history.lines")
    return [(line.text, line.tone) for line in children(host)]


def segment_states(user: User, group: str, keys: tuple[str, ...]) -> list[str]:
    return [the(user, f"{group}.{key}").props["aria-pressed"] for key in keys]


def flashes(card: ModelCard) -> set[str]:
    return FLASH_CLASSES & classes(card)


# ---- markers and layout -------------------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_page_renders_its_markers_and_sections(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for marker in (*REQUIRED_MARKERS["/models"], "page.models", "models.cards",
                   *(f"models.card.{backend}" for backend in ORDER), "models.unload", f"models.switch.{STD}",
                   f"models.switch.{TUR}", "models.vram", "models.vram.meter", "models.legend.app",
                   "models.legend.other", "models.legend.free", "models.gpu", "models.free", "models.warmup",
                   "models.warmup.off", "models.warmup.last", "models.idle",
                   *(f"models.idle.{minutes}" for minutes in (0, 10, 30, 60)), "models.confirm", "models.limit",
                   "models.history", "models.history.lines"):
        await user.should_see(marker=marker)
    for hidden in ("models.history.empty", f"models.switch.{MTL}", "takes.tabs", "settings.tabs", "confirm.ask"):
        await user.should_not_see(marker=hidden)
    assert the(user, "shell.title").text == TITLES["/models"] == "Models"
    page = the(user, "page.models")
    captions = [element.label.text for element in page.descendants() if "vt-section-label" in element.classes]
    assert captions == [CARDS_TITLE, "VRAM", "Behavior", "Load History"]
    root = children(page)[0]
    assert classes(root) == {"vt-models"}
    lower = children(root)[1]
    assert "vt-models-lower" in classes(lower) and "vt-models-left" in classes(children(lower)[0])
    assert binding.active_links == []                       # R9: every binding is a passive link


# ---- the cards ----------------------------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_cards_on_the_models_board(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    found = cards(user)
    assert list(found) == list(ORDER)
    assert [(card.caption.text, card.props["title"]) for card in found.values()] == [
        (catalog.label(backend), catalog.tip(backend)) for backend in ORDER]
    assert all(the(user, f"models.card.{backend}") is card for backend, card in found.items())
    mtl, std, tur = found[MTL], found[STD], found[TUR]
    assert (mtl.indicator.state, mtl.chip.text, mtl.chip.tone) == ("resident", "Resident", "gold")
    assert {"vt-card", "vt-models-card", "is-ringed"} <= classes(mtl)
    assert facts(mtl) == [("Languages", "23 languages"), ("Controls", "Expressiveness · Guidance"),
                          ("Reference clip", "6–15 s ideal"), ("Measured VRAM", "3.4 GB"), ("Last load", "14.2 s"),
                          ("Voices", "3")]
    assert facts(std) == [("Languages", "English only"), ("Controls", "Expressiveness · Guidance"),
                          ("Reference clip", "6–15 s ideal"), ("Measured VRAM", "3.5 GB"), ("Last load", "12.1 s"),
                          ("Voices", "1")]
    assert facts(tur) == [("Languages", "English only"), ("Controls", "Tags · no Expr or Guid"),
                          ("Reference clip", "Over 5 s"), ("Measured VRAM", "3.1 GB"), ("Last load", "8.7 s"),
                          ("Voices", "1")]
    unload = the(user, "models.unload")
    assert isinstance(unload, EmiyaButton) and unload is mtl.button and mtl.kind == "unload"
    assert (unload.label, unload.enabled, "aria-label" in unload.props) == ("Unload · Free 3.4 GB", True, False)
    assert {"em-btn", "vt-b30", "vt-models-act"} <= classes(unload) and "em-primary" not in classes(unload)
    for backend, card in ((STD, std), (TUR, tur)):
        button = the(user, f"models.switch.{backend}")
        assert button is card.button and card.kind == "switch"
        assert (button.label, button.props["aria-label"]) == ("Switch to This Model",
                                                              f"Switch to {catalog.label(backend)}")
        assert {"em-primary", "vt-models-act"} <= classes(button) and "is-grey" not in classes(button)
        assert (card.indicator.state, card.chip.text, card.chip.tone) == ("not_loaded", "Not loaded", "off")
        assert "is-ringed" not in classes(card) and not flashes(card)
    assert {id(element) for element in marked(user, "models.switch")} == {id(std.button), id(tur.button)}
    assert all(card.chip.tag == "span" and "vt-chip-20" in classes(card.chip) for card in found.values())


@pytest.mark.scenario("firstrun")
async def test_the_first_run_loads_from_a_card_and_fills_the_history(user: User, core: Core,
                                                                    fake: FakeEngine) -> None:
    await opened(user, core, fake)
    found = cards(user)
    for backend, card in found.items():
        name = catalog.label(backend)
        assert card.props["title"] == catalog.tip(backend) + NOT_LOADED_YET
        assert (card.indicator.state, card.chip.text, card.chip.tone) == ("not_loaded", "Not loaded", "off")
        assert (card.button.label, card.button.props["aria-label"], card.kind) == (
            "Load This Model", f"Load {name}", "load")
        assert the(user, f"models.switch.{backend}") is card.button
        values = dict(facts(card))
        assert (values["Measured VRAM"], values["Last load"], values["Voices"]) == ("—", "—", "0")
        assert fact_tip(card, "Measured VRAM") == "Measured after its first load"
        assert fact_tip(card, "Languages") is None
    await user.should_not_see(marker="models.unload")
    free = the(user, "models.free")
    assert free.enabled is False and free.props["disabled"] is True and free.props["title"] == NOTHING_TO_FREE
    assert (the(user, "models.legend.app").text, the(user, "models.legend.other").text) == (
        "This app · 0.0 GB", "Other apps · 2.5 GB")
    empty = the(user, "models.history.empty")
    assert empty.text == HISTORY_EMPTY == "No models loaded yet." and empty.tag == "p"
    assert not marked(user, "models.history.lines")[0].visible
    user.find(marker=f"models.switch.{TUR}").click()
    await settle(core, fake)
    assert fake.loads == [TUR]
    await user.should_not_see(marker="models.history.empty")
    lines = history_lines(user)
    assert [text.split("  ", 1)[1] for text, _ in lines] == ["Loading chatterbox-turbo",
                                                             "Loaded chatterbox-turbo · 8.7 s · 3.1 GB"]
    assert all(text.startswith("Today ") and tone == "text-soft" for text, tone in lines)
    turbo = found[TUR]
    assert (turbo.chip.text, turbo.button.label, flashes(turbo)) == ("Resident", "Unload · Free 3.1 GB", {"em-flash"})
    assert dict(facts(turbo))["Measured VRAM"] == "3.1 GB" and fact_tip(turbo, "Measured VRAM") is None


@pytest.mark.scenario("models")
async def test_a_card_switches_through_a_redeemed_token(user: User, core: Core, fake: FakeEngine,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    await opened(user, core, fake)
    redeemed: list[LoadTicket | None] = []
    redeem = core.tokens.redeem

    def spy(token: str, *, expect_to: str | None = None) -> LoadTicket | None:
        ticket = redeem(token, expect_to=expect_to)
        redeemed.append(ticket)
        return ticket

    monkeypatch.setattr(core.tokens, "redeem", spy)
    fake.reset_records()
    turbo = cards(user)[TUR]
    assert core.settings.get("core", "confirm_switches") is True
    assert turbo.button.props["aria-label"] == "Switch to Turbo"          # the card names the model
    user.find("Switch to Turbo").click()
    await user.should_not_see(marker="confirm.ask")                       # named: it never asks
    await settle(core, fake)
    assert fake.loads == [TUR] and core.state.engine.resident == TUR
    assert len(redeemed) == 1 and isinstance(redeemed[0], LoadTicket)
    plan = redeemed[0].plan
    assert (plan.to_backend, plan.from_backend, plan.reason, plan.low_vram) == (TUR, MTL, "explicit", False)
    after = cards(user)
    assert after[TUR] is turbo                                            # redrawn in place
    assert (turbo.indicator.state, turbo.chip.text, turbo.chip.tone) == ("resident", "Resident", "gold")
    assert "is-ringed" in classes(turbo) and flashes(turbo) == {"em-flash"}
    assert the(user, "models.unload") is turbo.button and turbo.button.label == "Unload · Free 3.1 GB"
    mtl = after[MTL]
    assert the(user, f"models.switch.{MTL}") is mtl.button and mtl.button.label == "Switch to This Model"
    assert mtl.button.props["aria-label"] == "Switch to Multilingual"
    assert "is-ringed" not in classes(mtl) and not flashes(mtl)
    assert the(user, "shell.toast").text.startswith("Turbo ready · ")
    assert [text.split("  ", 1)[1] for text, _ in history_lines(user)[-3:]] == [
        "Switching Multilingual → Turbo", "Unloaded chatterbox-multilingual · freed 3.4 GB",
        "Loaded chatterbox-turbo · 8.7 s · 3.1 GB"]


@pytest.mark.scenario("models")
async def test_the_old_card_shows_freeing_while_a_switch_unloads_it(user: User, core: Core,
                                                                   fake: FakeEngine) -> None:
    await opened(user, core, fake)
    hold = fake.hold("evict")
    user.find(marker=f"models.switch.{STD}").click()
    await until(hold.entered.is_set, "the held eviction")
    await core.scheduler.settle(ignore=[core.jobs.task])
    found = cards(user)
    mtl = found[MTL]
    assert (mtl.chip.text, mtl.chip.tone, mtl.indicator.state) == ("Freeing…", "gold", "unloading")   # D-T5
    assert "is-unloading" in classes(mtl.indicator._dot)
    assert found[STD].chip.text == "Not loaded"
    assert all(not card.button.enabled and card.button.props["title"] == BUSY for card in found.values())
    assert {"em-primary", "is-grey"} <= classes(found[TUR].button)
    assert the(user, "models.legend.app").text == "This app · 3.4 GB"     # no model name during a switch
    assert the(user, "models.free").enabled is False
    hold.release()
    await settle(core, fake)
    assert fake.loads == [STD] and cards(user)[STD].chip.text == "Resident"


@pytest.mark.scenario("switch")
async def test_the_switch_board_ask_gives_way_to_a_named_card(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "confirm.ask").props["aria-label"] == "Switch to Turbo"      # the board's dock ask
    assert the(user, "confirm.ask.primary").label == "Switch Model"
    await user.should_see(marker="confirm.ask.dont_ask")
    turbo = cards(user)[TUR]
    assert (turbo.chip.text, turbo.button.label, turbo.button.enabled) == ("Not loaded", "Switch to This Model", True)
    fake.reset_records()
    user.find(marker=f"models.switch.{TUR}").click()
    await user.should_not_see(marker="confirm.ask")
    await settle(core, fake)
    assert fake.loads == [TUR] and core.state.engine.resident == TUR


@pytest.mark.scenario("lowvram")
async def test_low_vram_makes_a_card_ask_even_with_confirm_off(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert core.state.device.detected
    user.find(marker="confirm.ask.cancel").click()                    # the board's own dock ask
    await user.should_not_see(marker="confirm.ask")
    user.find(marker="models.confirm").click()                       # "low VRAM still asks"
    assert core.settings.get("core", "confirm_switches") is False
    fake.reset_records()
    user.find(marker=f"models.switch.{TUR}").click()
    pop = the(user, "confirm.ask")
    assert pop.props["aria-label"] == "Switch to Turbo" and the(user, "confirm.ask.primary").label == "Load Anyway"
    warn = [line for line in pop.descendants() if "vt-ask-line" in line.classes and line.tone == "warn"]
    assert len(warn) == 1 and warn[0].text.startswith("Low VRAM · ")
    await user.should_not_see(marker="confirm.ask.dont_ask")          # D20
    assert fake.loads == []
    user.find(marker="confirm.ask.primary").click()
    await settle(core, fake)
    assert fake.loads == [TUR]


@pytest.mark.scenario("loading")
async def test_the_load_progress_copy_on_the_card(user: User, core: Core, fake: FakeEngine,
                                                  clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    await until(lambda: core.state.job.phase == "load", "the held load")
    await settle(core, fake)
    found = cards(user)
    turbo = found[TUR]
    assert turbo.indicator.state == "loading" and turbo.indicator._ring.visible
    assert all(not card.button.enabled and card.button.props["disabled"] is True for card in found.values())
    assert all(card.button.props["title"] == BUSY for card in found.values())
    assert all({"em-primary", "is-grey"} <= classes(card.button) for card in found.values())
    assert the(user, "models.free").enabled is False
    assert the(user, "models.legend.app").text.count("·") == 1
    start = core.state.job.started_at
    for share, text in ((0.5, "Loading · 4 s of ~9 s"), (1.2, "Loading · 10 s, usually ~9 s"),
                        (2.0, "Loading · 17 s, usually ~9 s")):
        clock.advance(round(start + share * 8.7 - clock.now(), 9))
        await settle(core, fake)
        assert (turbo.chip.text, turbo.chip.tone) == (text, "gold"), share
        assert turbo.indicator._ring.style["--ring"].endswith("deg") and "is-spin" not in classes(turbo.indicator._ring)
    user.find(marker=f"models.switch.{STD}").click()                 # disabled while busy: nothing happens
    assert core.state.job.target == TUR


@pytest.mark.scenario("firstrun")
async def test_a_first_load_without_history_shows_elapsed_seconds_and_the_turning_arc(
        user: User, core: Core, fake: FakeEngine, clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    hold = fake.hold("make_resident")
    user.find(marker=f"models.switch.{TUR}").click()
    await until(hold.entered.is_set, "the held load")
    await core.scheduler.settle(ignore=[core.jobs.task])
    turbo = cards(user)[TUR]
    ring = turbo.indicator._ring
    assert (turbo.chip.text, turbo.indicator.state) == ("Loading · 1 s", "loading")
    assert "is-spin" in classes(ring) and "--ring" not in ring.style      # no estimate: the turning arc
    clock.advance(round(core.state.job.started_at + 6.0 - clock.now(), 9))
    await core.scheduler.settle(ignore=[core.jobs.task])
    assert turbo.chip.text == "Loading · 6 s"
    hold.release()
    await settle(core, fake)
    assert fake.loads == [TUR] and turbo.chip.text == "Resident"


@pytest.mark.scenario("freeing")
async def test_free_vram_shows_the_freeing_card(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    found = cards(user)
    mtl = found[MTL]
    assert (mtl.chip.text, mtl.chip.tone, mtl.indicator.state) == ("Freeing…", "gold", "unloading")
    assert "is-unloading" in classes(mtl.indicator._dot)
    assert all(not card.button.enabled for card in found.values())
    assert (mtl.button.label, mtl.kind) == ("Unload · Free 3.4 GB", "unload")
    free = the(user, "models.free")
    assert free.enabled is False and free.props["title"] == BUSY


@pytest.mark.scenario("failure")
async def test_a_failed_load(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    found = cards(user)
    turbo = found[TUR]
    assert (turbo.chip.text, turbo.chip.tone, turbo.indicator.state) == ("Failed", "red", "failed")
    assert "is-failed" in classes(turbo.indicator._dot)
    assert (turbo.button.label, turbo.button.props["aria-label"], turbo.button.enabled) == (
        "Load This Model", "Load Turbo", True)
    assert (found[MTL].chip.text, found[MTL].button.label) == ("Not loaded", "Load This Model")
    await user.should_not_see(marker="models.unload")
    free = the(user, "models.free")
    assert free.enabled is False and free.props["title"] == NOTHING_TO_FREE
    lines = history_lines(user)
    assert lines[-1] == (lines[-1][0], "bad") and lines[-1][0].startswith("Today ")
    assert [text.split("  ", 1)[1] for text, _ in lines[-3:]] == [
        "Switching Multilingual → Turbo", "Unloaded chatterbox-multilingual · freed 3.4 GB",
        "Load failed: chatterbox-turbo · CUDA out of memory"]
    assert [tone for _, tone in lines[-3:-1]] == ["text-soft", "text-soft"]
    fake.reset_records()
    user.find(marker=f"models.switch.{TUR}").click()               # the card retries; 9.9 GB held elsewhere: ask
    assert the(user, "confirm.ask.primary").label == "Load Anyway"
    assert fake.loads == []


# ---- the VRAM card ------------------------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_vram_card_on_the_models_board(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    card = the(user, "models.vram")
    assert {"vt-card", "vt-models-vram"} <= classes(card)
    meter = the(user, "models.vram.meter")
    assert isinstance(meter, Meter) and "is-tall" in classes(meter) and meter.props["role"] == "img"
    tip = "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"
    assert meter.props["aria-label"] == tip and "title" not in meter.props
    assert meter._app.style["width"] == "28.35%" and meter._other.style["width"].startswith("20.8")
    assert [the(user, f"models.legend.{key}").text for key in ("app", "other", "free")] == [
        "This app · 3.4 GB · Multilingual", "Other apps · 2.5 GB", "Free · 6.1 GB"]
    swatches = [element for element in card.descendants() if "vt-models-swatch" in element.classes]
    assert [sorted(classes(s) - {"vt-models-swatch"}) for s in swatches] == [["is-app"], ["is-other"], ["is-free"]]
    assert all(s.props["aria-hidden"] == "true" for s in swatches)
    gpu = the(user, "models.gpu")
    assert (gpu.text, gpu.props["title"]) == ("NVIDIA GeForce RTX 4070 Ti · 12.0 GB · detected at startup", GPU_TIP)
    free = the(user, "models.free")
    assert (free.label, free.enabled, "disabled" in free.props) == ("Free VRAM", True, False)
    assert {"em-btn", "vt-b26", "vt-models-free"} <= classes(free)


@pytest.mark.scenario("models")
async def test_unload_frees_the_resident_model(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="models.unload").click()
    await settle(core, fake)
    assert core.state.engine.resident == "" and fake.loads == []
    assert the(user, "shell.toast").text == "Freed 3.4 GB"
    mtl = cards(user)[MTL]
    assert the(user, f"models.switch.{MTL}") is mtl.button and mtl.button.label == "Load This Model"
    assert (mtl.chip.text, "is-ringed" in classes(mtl)) == ("Not loaded", False)
    free = the(user, "models.free")
    assert free.enabled is False and free.props["title"] == NOTHING_TO_FREE
    assert history_lines(user)[-1][0].endswith("  Unloaded chatterbox-multilingual · freed 3.4 GB")


@pytest.mark.scenario("models")
async def test_free_vram_frees_the_resident_model(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="models.free").click()
    await settle(core, fake)
    assert core.state.engine.resident == "" and fake.loads == []
    assert the(user, "shell.toast").text == "Freed 3.4 GB"
    assert the(user, "models.legend.app").text == "This app · 0.0 GB"
    assert the(user, "models.free").enabled is False
    await user.should_not_see(marker="models.unload")


# ---- Behavior and the locked fact ---------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_behavior_rows_reach_their_settings(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    settings = core.settings
    warmup = the(user, "models.warmup")
    assert isinstance(warmup, Segmented) and "vt-models-warmup" in classes(warmup)
    assert (warmup.props["role"], warmup.props["aria-label"]) == ("group", "Warm up on launch")
    assert [segment._face.text for segment in warmup._segments] == ["Off", "Last Model"]
    assert segment_states(user, "models.warmup", ("off", "last")) == ["true", "false"]
    user.find(marker="models.warmup.last").click()
    assert settings.get("core", "warmup") == "last"
    assert segment_states(user, "models.warmup", ("off", "last")) == ["false", "true"]
    idle = the(user, "models.idle")
    assert (idle.props["aria-label"], "vt-models-idle" in classes(idle)) == ("Auto-unload after idle", True)
    assert [segment._face.text for segment in idle._segments] == ["Never", "10 min", "30 min", "1 h"]
    keys = ("0", "10", "30", "60")
    assert segment_states(user, "models.idle", keys) == ["false", "true", "false", "false"]
    user.find(marker="models.idle.30").click()
    assert settings.get("core", "idle_unload_min") == 30
    assert segment_states(user, "models.idle", keys) == ["false", "false", "true", "false"]
    user.find(marker="models.idle.0").click()
    assert settings.get("core", "idle_unload_min") == 0
    confirm = the(user, "models.confirm")
    assert isinstance(confirm, DotSwitch) and confirm.props["aria-pressed"] == "true"
    assert (confirm._label.text, confirm.note.text) == (CONFIRM_LABEL, CONFIRM_NOTE)
    user.find(marker="models.confirm").click()
    assert settings.get("core", "confirm_switches") is False
    assert confirm.props["aria-pressed"] == "false" and "em-dot-off" in classes(confirm._dot)
    user.find(marker="models.confirm").click()
    assert settings.get("core", "confirm_switches") is True
    assert fake.loads == []                                          # warm-up applies at the next launch


@pytest.mark.scenario("models")
async def test_the_locked_models_in_vram_fact(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    limit = the(user, "models.limit")
    assert {"em-row", "vt-off", "vt-models-limit"} <= classes(limit)
    assert limit.props["title"] == LIMIT_BASE + " · RTX 4070 Ti · 12.0 GB"
    texts = [element.text for element in limit.descendants() if isinstance(element, TextSpan)]
    assert texts == [LIMIT_LABEL, "One at a time", LIMIT_NOTE]
    icons = [element for element in limit.descendants() if isinstance(element, ui.html)]
    assert len(icons) == 1 and 'width="12"' in icons[0].content and "<rect" in icons[0].content
    assert [element for element in limit.descendants() if "vt-hair" in element.classes]   # a hairline row


@pytest.mark.scenario("models")
async def test_the_limit_tip_waits_for_the_gpu_detection(user: User, core: Core, fake: FakeEngine) -> None:
    hold = fake.hold("device_info")                                   # the first reading waits
    await user.open("/models")
    await until(hold.entered.is_set, "the first device reading")
    for _ in range(5):
        await asyncio.sleep(0)
    assert the(user, "models.limit").props["title"] == LIMIT_BASE       # nothing added before detection
    gpu = the(user, "models.gpu")
    assert gpu.text == "Detecting the GPU…" and "title" not in gpu.props
    assert (the(user, "models.legend.other").text, the(user, "models.legend.free").text) == (
        "Other apps · —", "Free · —")
    assert "aria-label" not in the(user, "models.vram.meter").props
    hold.release()
    await settle(core, fake)
    assert the(user, "models.limit").props["title"] == LIMIT_BASE + " · RTX 4070 Ti · 12.0 GB"
    assert the(user, "models.gpu").text == "NVIDIA GeForce RTX 4070 Ti · 12.0 GB · detected at startup"


@pytest.mark.scenario("cpu")
async def test_the_limit_and_the_cards_on_the_cpu(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "models.limit").props["title"] == LIMIT_BASE + " · CPU"
    gpu = the(user, "models.gpu")
    assert gpu.text == "No CUDA GPU detected · models run on the CPU" and "title" not in gpu.props
    for hidden in ("models.vram.meter", "models.legend.app", "models.legend.other", "models.legend.free"):
        await user.should_not_see(marker=hidden)                     # no meter, so no key to it
    assert the(user, "models.free").label == "Free VRAM"
    found = cards(user)
    assert all(dict(facts(card))["Measured VRAM"] == "On CPU" for card in found.values())
    assert dict(facts(found[MTL]))["Last load"] == "14.2 s"
    assert (found[MTL].button.label, the(user, "models.unload") is found[MTL].button) == ("Unload", True)


@pytest.mark.scenario("models")
async def test_more_than_one_model_in_vram_is_refused(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    core.facade.set_engine(max_resident=2)
    await settle(core, fake)
    limit = the(user, "models.limit")
    assert limit.props["title"].startswith("MAX_RESIDENT_BACKENDS = 2 in config.py")
    text = next(element for element in limit.descendants() if "vt-models-limit-text" in element.classes)
    assert text.text == ONE_MODEL
    fake.reset_records()
    user.find(marker=f"models.switch.{TUR}").click()
    await settle(core, fake)
    assert fake.loads == [] and core.state.engine.resident == MTL
    toast = the(user, "shell.toast")
    assert toast.text == ONE_MODEL and "is-bad" in classes(toast)


@pytest.mark.scenario("models")
async def test_auto_unload_after_idle_applies_live(user: User, core: Core, fake: FakeEngine,
                                                   clock: ManualScheduler) -> None:
    core.settings.set("core", "idle_unload_min", 0)
    await opened(user, core, fake)
    user.find(marker="models.idle.10").click()
    assert core.settings.get("core", "idle_unload_min") == 10
    clock.advance(9 * 60)
    await settle(core, fake)
    assert core.state.engine.resident == MTL
    clock.advance(60 + 15)
    await settle(core, fake)
    assert core.state.engine.resident == "" and fake.loads == []
    assert the(user, "shell.toast").text == "Freed 3.4 GB · idle 10 min"
    assert history_lines(user)[-1][0].endswith("  Unloaded chatterbox-multilingual · idle 10 min · freed 3.4 GB")


# ---- Load History -------------------------------------------------------------------------------------

@pytest.mark.scenario("models")
async def test_the_load_history_on_the_models_board(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    box = the(user, "models.history")
    assert {"vt-card", "vt-models-history", "em-scroll"} <= classes(box)
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    day = f"{MONTHS[yesterday.month - 1]} {yesterday.day}" + ("" if yesterday.year == now.year else f" {yesterday.year}")
    assert history_lines(user) == [                                   # the typed events only (D-T8), oldest first
        (f"{day} 19:20:41  Loaded chatterbox-turbo · 8.7 s · 3.1 GB", "faint"),
        (f"{day} 19:34:10  Switching Turbo → Standard", "faint"),
        (f"{day} 19:34:11  Unloaded chatterbox-turbo · freed 3.1 GB", "faint"),
        (f"{day} 19:34:23  Loaded chatterbox-standard · 12.1 s · 3.5 GB", "faint"),
        (f"{day} 19:52:40  Switching Standard → Multilingual", "faint"),
        (f"{day} 19:52:41  Unloaded chatterbox-standard · freed 3.5 GB", "faint"),
        (f"{day} 19:52:55  Loaded chatterbox-multilingual · 14.2 s · 3.4 GB", "faint"),
        (f"{day} 20:31:55  Closed · freed 3.4 GB", "faint"),
        ("Today 13:57:02  Loading chatterbox-multilingual", "text-soft"),
        ("Today 13:57:16  Loaded chatterbox-multilingual · 14.2 s · 3.4 GB", "text-soft"),
    ]
    lines = children(the(user, "models.history.lines"))
    assert all(line.tag == "p" and "vt-models-line" in line.classes for line in lines)
    assert [f"vt-c-{tone}" in classes(line) for line, (_, tone) in zip(lines, history_lines(user))] == [True] * 10


# ---- the wiring rows ----------------------------------------------------------------------------------

def _spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    owner, name = intent.split(".")
    assert owner == "models", intent
    original = getattr(ModelsController, name)
    if inspect.iscoroutinefunction(original):
        async def spy_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(ModelsController, name, spy_async)
    else:
        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(ModelsController, name, spy)


def _assert_named(user: User, row: Wire) -> None:
    """The control's visible label or aria-label names every model the row loads (§8.5)."""
    element = the(user, row.marker)
    name = f"{getattr(element, 'label', '')} {element.props.get('aria-label', '')}"
    assert row.loads and all(catalog.label(backend) in name for backend in row.loads), name


async def _act(user: User, row: Wire) -> None:
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    else:
        raise AssertionError(f"the Models page has no {row.act!r} rows")


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch,
                           row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)                             # before the page binds its handlers
    await user.open(row.route)
    await settle(core, fake)
    if row.confirm == "ask" and shown(user, "confirm.ask"):
        user.find(marker="confirm.ask.cancel").click()               # an ask the board replayed is not the row's
    await user.should_not_see(marker="confirm.ask")
    fake.reset_records()
    calls.clear()
    if row.confirm == "named":
        _assert_named(user, row)
    await _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "ask":
        assert fake.loads == []
        await user.should_see(marker="confirm.ask")
        user.find(marker="confirm.ask.primary").click()
        await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    elif row.confirm == "named":
        await user.should_not_see(marker="confirm.ask")
    assert fake.loads == list(row.loads)


# ---- the stylesheet -----------------------------------------------------------------------------------

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def _css(path: Path) -> str:
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_the_page_stylesheet_keeps_colour_values_in_tokens() -> None:
    """A colour literal in models.css is only ever the value of a custom property; rules use var(--token)."""
    stray = [part.strip() for part in re.split(r"[;{}]", _css(PAGE_CSS))
             if COLOUR.search(part) and not part.strip().startswith("--")]
    assert stray == []


def test_the_page_stylesheet_uses_declared_tokens_only() -> None:
    page = _css(PAGE_CSS)
    declared = set(re.findall(r"(--[\w-]+)\s*:", _css(THEME_CSS) + page))
    assert set(re.findall(r"var\((--[\w-]+)", page)) - declared == set()
    assert page.count("{") == page.count("}") and "@import" not in page and "url(" not in page


def test_every_class_the_page_uses_is_styled() -> None:
    used = set(re.findall(r"vt-models[\w-]*", PAGE_PY.read_text(encoding="utf-8")))
    styled = set(re.findall(r"\.(vt-models[\w-]*)", _css(PAGE_CSS)))
    assert used and used - styled == set()


def test_the_stylesheet_carries_the_mockup_geometry() -> None:
    """The boxes of M L855-953: the card grid, card padding, rows, the two columns, the boxes' sizes."""
    page = _css(PAGE_CSS)
    for rule in ("gap: 14px", "grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px", "padding: 4px 14px 14px",
                 "height: 44px", "height: 30px; border-top: 1px solid var(--hairline); font-size: 13px",
                 "width: 100%; height: 32px; margin-top: 10px; padding: 0 14px",
                 "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); column-gap: 16px",
                 "padding: 14px", "gap: 6px 18px; margin: 12px 0", "width: 8px; height: 8px; border-radius: 2px",
                 "padding: 4px 14px", "width: 200px", "width: 264px", "height: 310px", "padding: 12px 14px",
                 "gap: 6px", ".is-ringed { box-shadow: var(--gold-ring-soft); }"):
        assert rule in page, rule
    assert "--gold-ring-soft: inset 0 0 0 1px rgba(252, 219, 127, 0.35);" in _css(THEME_CSS)    # M L2817
