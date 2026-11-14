"""The Forge page under NiceGUI's user fixture (§8.5, P3): gui.py on a FakeEngine forge seed in tmp_path.

Covered: the required and listed markers in the forge seeds; Table F per seed (§6.13) as the button shows it;
the board's copy (clip meta, checks, marks, the name line, the model note, Forge Notes); the Turbo look (the
locked language, the dimmed Expressiveness row); typing a name, the Fix button, the model segments, the
language list, the slider and consent; forging on the resident model, the named switch, the replace arm on
another model (the first click loads nothing, the second loads Turbo once, then bakes) and the low-VRAM ask;
the bake and unload labels; the result row; Browse through the kit's dialog; native drops; the wiring rows of
tests\\pages\\wiring\\forge.py; no ERROR log (the user fixture checks that itself).

Pages open /forge directly, so these tests never build another page; "Speak with" lands on a stand-in Speak
page with a stub Speak controller.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import binding, ui
from nicegui.element import Element
from nicegui.testing import User

from ui import launch
from ui.behaviours.forge import CLIP_FILTERS, DROP_ONE, ForgeController
from ui.behaviours.playback import PlaybackController
from ui.contract.markers import CONFIRM_MARKERS, REQUIRED_MARKERS
from ui.contract.viewmodels import ForgeController as ForgeProtocol
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.themes.emiya.fx.bars import EmiyaBars
from ui.themes.emiya.fx.forge_field import EmiyaForgeField
from ui.themes.emiya.pages.forge import DROP_HINT, NOTES
from ui.themes.emiya.shell import ShellRoot

from tests.behaviours import StubSpeak
from tests.engine.conftest import write_clip
from tests.pages.wiring import Wire
from tests.pages.wiring.forge import OPT_OUT, WIRING

TUR = "chatterbox-turbo"
FORGE_SEEDS = ("forge", "forge_ready", "forge_other_backend", "forge_low_vram", "forge_replace",
               "forge_replace_other_backend", "forge_taken_other", "forge_turbo_short", "forged", "forge_failed")
SAMPLE_META = "30.0 s · mono · 22.05 kHz · trimmed to the first 13.4 s"
TURBO_NOTE = "Forging loads Turbo · unloads Multilingual (~9 s)"


# ---- helpers ------------------------------------------------------------------------------------------

def the(user: User, marker: str) -> Any:
    """The one shown element with this marker."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def factory_of(user: User) -> Any:
    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell.ctx.behaviours


def controller(user: User) -> ForgeController:
    forge = factory_of(user).built("forge")
    assert isinstance(forge, ForgeController)
    return forge


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks start their jobs, then wait for every job the scenario does not hold."""
    for _ in range(5):
        await asyncio.sleep(0)
    held = any(not hold.is_set() for hold in fake.holds)
    if held:
        await until(lambda: not core.state.job.kind or any(hold.entered.is_set() for hold in fake.holds),
                    "the held call")
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle(ignore=[core.jobs.task] if held else [])
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine) -> None:
    """Open /forge directly and let the first page's replay and the clip staging finish."""
    await user.open("/forge")
    await settle(core, fake)
    await user.should_see(marker="page.forge")


def load_voice_calls(fake: FakeEngine) -> list[Any]:
    return [call for call in fake.calls if call.name == "load_voice"]


@pytest.fixture
def stand_in_speak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speak ("/") becomes a bare page, so a navigation there never builds the real Speak page."""
    run = launch.LAST_LAUNCH
    assert run is not None
    theme = SimpleNamespace(manifest=run.theme.manifest, build_shell=run.theme.build_shell,
                            pages={**run.theme.pages, "/": lambda _ctx: ui.label("Speak stand-in")})
    monkeypatch.setattr(run, "theme", theme)


# ---- markers ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", [pytest.param(name, marks=pytest.mark.scenario(name)) for name in FORGE_SEEDS])
async def test_the_markers_render_in_every_forge_seed(user: User, core: Core, fake: FakeEngine, scenario: str) -> None:
    await opened(user, core, fake)
    shown = [marker for marker in REQUIRED_MARKERS["/forge"] if marker != "forge.go" or scenario != "forged"]
    for marker in (*shown, "forge.play", "forge.browse", "forge.expr", "forge.notes"):
        await user.should_see(marker=marker)
    for backend in core.state.engine.enabled:
        await user.should_see(marker=f"forge.backend.{backend}")
    go = marked(user, "forge.go")
    assert len(go) == 1 and CONFIRM_MARKERS["arm"] in go[0]._markers
    if scenario == "forged":
        for marker in ("forge.speak_with", "forge.test_line", "forge.another"):
            await user.should_see(marker=marker)
        await user.should_not_see(marker="forge.go")
    else:
        await user.should_not_see(marker="forge.speak_with")
    assert fake.loads == []


# ---- Table F per seed (§6.13) -------------------------------------------------------------------------

@pytest.mark.parametrize(("scenario", "text", "enabled", "named"), [
    pytest.param("forge", "Confirm Consent First", False, False, marks=pytest.mark.scenario("forge")),
    pytest.param("forge_ready", "Forge Voice", True, False, marks=pytest.mark.scenario("forge_ready")),
    pytest.param("forge_other_backend", "Switch to Turbo & Forge", True, True,
                 marks=pytest.mark.scenario("forge_other_backend")),
    pytest.param("forge_low_vram", "Switch to Turbo & Forge", True, True, marks=pytest.mark.scenario("forge_low_vram")),
    pytest.param("forge_replace", "Forge Voice", True, False, marks=pytest.mark.scenario("forge_replace")),
    pytest.param("forge_replace_other_backend", "Switch to Turbo & Forge", True, True,
                 marks=pytest.mark.scenario("forge_replace_other_backend")),
    pytest.param("forge_taken_other", "Name Taken on Multilingual", False, False,
                 marks=pytest.mark.scenario("forge_taken_other")),
    pytest.param("forge_turbo_short", "Clip is 4.2 s; Turbo needs over 5 s", False, False,
                 marks=pytest.mark.scenario("forge_turbo_short")),
    pytest.param("forge_failed", "Forge Voice", True, False, marks=pytest.mark.scenario("forge_failed")),
])
async def test_the_forge_button_per_seed(user: User, core: Core, fake: FakeEngine, scenario: str, text: str,
                                         enabled: bool, named: bool) -> None:
    await opened(user, core, fake)
    button = the(user, "forge.go")
    assert (button.label, button.enabled, button.action.pending is not None) == (text, enabled, named)
    assert ("disabled" in button.props) == (not enabled)
    if not enabled:
        assert "is-grey" in button.classes
    if named:
        assert core.tokens.peek(button.action.pending.token).to_backend == TUR
    assert fake.loads == []


@pytest.mark.scenario("forge")
async def test_the_forge_board_shows_the_mockups_copy(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for text in ("Reference Clip", "Identity", "Forge Notes", "sample.wav", SAMPLE_META, DROP_HINT, "Browse…",
                 "Identity uses the whole selection · the first 10 s set the timbre",
                 "Selection 13.4 s of 30.0 s · ideal 6–15 s", "Over 5 s", "Mono", "22.05 kHz",
                 "−8.4 dBFS · no clipping", "Name", "cache/chatterbox-multilingual/rin.pt", "Model", "Multilingual",
                 "Standard", "Turbo", "Multilingual is resident · forging takes about 2 s", "Language", "Japanese",
                 "Default Expressiveness", "0.50", "Starting value on Speak",
                 "The speaker agreed to having this voice cloned", "Required", "Confirm Consent First", *NOTES):
        await user.should_see(text)
    assert the(user, "forge.name").value == "rin"
    bars = next(iter(user.find(kind=EmiyaBars).elements))
    assert bars._props["kind"] == "forge" and len(bars._props["bars"]) == 230
    assert [mark["label"] for mark in bars._props["marks"]] == ["Prompt 6 s", "Timbre 10 s"]
    clip = core.forge_draft.clip
    assert bars._props["sel-pct"] == pytest.approx(clip.selection_s / clip.duration_s * 100)
    assert bars._props["aria"] == "Waveform of sample.wav, 13.4 s selected"          # M L3039
    note = next(e for e in user.current_layout.descendants() if "vt-forge-model-note" in e.classes)
    assert "vt-c-muted" in note.classes
    consent = the(user, "forge.consent")
    assert consent.props["aria-pressed"] == "false" and "vt-c-gold-dim" in consent.note.classes
    assert the(user, "forge.play").props["aria-label"] == "Play the reference clip"


@pytest.mark.scenario("forge_other_backend")
async def test_turbo_locks_english_and_dims_expressiveness(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_see("English only")
    await user.should_not_see(marker="forge.language")
    await user.should_see(TURBO_NOTE)
    note = next(e for e in user.current_layout.descendants() if "vt-forge-model-note" in e.classes)
    assert "vt-c-gold-dim" in note.classes
    slider = the(user, "forge.expr")
    assert slider.enabled is False
    row = next(e for e in user.current_layout.descendants() if "vt-forge-expr" in e.classes)
    assert "vt-off" in row.classes
    await user.should_see("Ignored by Turbo")
    await user.should_see("—")
    marks = next(iter(user.find(kind=EmiyaBars).elements))._props["marks"]
    assert [mark["label"] for mark in marks] == ["Timbre 10 s"]


@pytest.mark.scenario("forge_turbo_short")
async def test_a_short_clip_on_turbo_reads_red(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for text in ("short_hello.wav", "4.2 s · mono · 24 kHz", "4.2 s · ideal 6–15 s", "Turbo needs over 5 s",
                 "Clip is 4.2 s; Turbo needs over 5 s"):
        await user.should_see(text)
    details = [e for e in user.current_layout.descendants() if "vt-forge-check-detail" in e.classes]
    assert [sorted(c for c in e.classes if c.startswith("is-")) for e in details] == [
        ["is-adv"], ["is-bad"], ["is-ok"], ["is-ok"], ["is-ok"]]


# ---- the identity controls ----------------------------------------------------------------------------

@pytest.mark.scenario("forge")
async def test_typing_a_bad_name_offers_the_fix(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.name").type(" 2!")
    await user.should_see("Letters, digits, _ and - only (it becomes a file name)")
    name = the(user, "forge.name")
    assert "is-bad" in name.classes and the(user, "forge.go").label == "Fix the Name First"
    user.find(marker="forge.fix_name").click()
    await user.should_see("cache/chatterbox-multilingual/rin-2.pt")
    assert the(user, "forge.name").value == "rin-2" and "is-bad" not in name.classes
    await user.should_not_see(marker="forge.fix_name")


@pytest.mark.scenario("forge")
async def test_consent_model_language_and_expressiveness(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.consent").click()
    await user.should_see("Forge Voice")
    assert the(user, "forge.consent").props["aria-pressed"] == "true"
    user.find(marker="forge.language").click()
    await user.should_see(marker="forge.language.item.ko")
    user.find(marker="forge.language.item.ko").click()
    await user.should_not_see(marker="forge.language.item.ko")
    assert the(user, "forge.language").text == "Korean"
    user.find(marker="forge.expr").trigger("change", 1.25)
    await user.should_see("1.25")
    assert core.forge_draft.exaggeration == 1.25
    user.find(marker="forge.backend.chatterbox-turbo").click()
    await user.should_see(TURBO_NOTE)
    await user.should_see("Switch to Turbo & Forge")
    user.find(marker="forge.backend.chatterbox-multilingual").click()
    assert the(user, "forge.language").text == "Korean"
    assert fake.loads == []


@pytest.mark.scenario("forge_ready")
async def test_the_play_button_plays_the_staged_clip(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.play").click()
    playback = factory_of(user).playback()
    assert playback.vm.target.src == core.staged_url(core.forge_draft.clip.clip_id)
    assert the(user, "forge.play").props["aria-label"] == "Stop the reference clip"
    user.find(marker="forge.play").click()
    assert the(user, "forge.play").props["aria-label"] == "Play the reference clip"


# ---- forging ------------------------------------------------------------------------------------------

@pytest.mark.scenario("forge_ready")
async def test_forging_on_the_resident_model_shows_the_result_row(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.go").click()
    await settle(core, fake)
    for text in ("Speak with rin", "Play Test Line", "Forge Another", "rin already exists · forging replaces it"):
        await user.should_see(text)
    await user.should_not_see(marker="forge.go")
    assert fake.loads == [] and len(load_voice_calls(fake)) == 1


@pytest.mark.scenario("forge_ready")
async def test_a_forge_start_grows_the_forge_blade(user: User, core: Core, fake: FakeEngine,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    blades: list[str] = []
    monkeypatch.setattr(EmiyaForgeField, "forge_blade", lambda self, anchor="forge-blade": blades.append(anchor))
    await opened(user, core, fake)
    user.find(marker="forge.go").click()
    await settle(core, fake)
    assert blades == ["forge-blade"]


@pytest.mark.scenario("forge")
async def test_the_page_binds_without_polled_links(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.language").click()
    user.find(marker="forge.consent").click()
    await settle(core, fake)
    assert binding.active_links == []


@pytest.mark.scenario("forge_replace_other_backend")
async def test_replacing_on_another_model_arms_then_switches_once(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    fake.reset_records()
    user.find(marker="forge.go").click()
    await settle(core, fake)
    await user.should_see("Click Again to Replace")
    await user.should_see(TURBO_NOTE)
    assert fake.loads == [] and core.state.job.kind == ""
    core.scheduler.advance(0.6)
    user.find(marker="forge.go").click()
    await settle(core, fake)
    assert fake.loads == [TUR] and core.state.engine.resident == TUR
    calls = load_voice_calls(fake)
    assert len(calls) == 1 and calls[0].args[0] == "host-turbo"
    await user.should_see("Speak with host-turbo")


@pytest.mark.scenario("forge_low_vram")
async def test_low_vram_asks_before_the_named_switch(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await until(lambda: core.state.device.detected, "the first device reading")
    fake.reset_records()
    user.find(marker="forge.go").click()
    await settle(core, fake)
    await user.should_see(marker="confirm.ask")
    for text in ("Switch to Turbo", "Load Anyway", "Then forges rin"):
        await user.should_see(text)
    await user.should_not_see("Don't ask again")
    assert fake.loads == []
    user.find(marker="confirm.ask.primary").click()
    await settle(core, fake)
    assert fake.loads == [TUR]
    await user.should_see("Speak with rin")


@pytest.mark.scenario("forging")
async def test_a_bake_in_progress(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    assert core.state.job.kind == "forge"
    await user.should_see("Baking conditioning… 0.0 s")
    clock.advance(0.4)
    await settle(core, fake)
    await user.should_see("Baking conditioning… 0.4 s")
    bars = next(iter(user.find(kind=EmiyaBars).elements))
    assert bars._props["tracing"] is True and bars._props["trace-progress"] == pytest.approx(0.4 / 1.9, abs=1e-3)
    for marker in ("forge.name", "forge.consent", "forge.language", "forge.browse"):
        assert the(user, marker).enabled is False
    assert the(user, "forge.play").enabled is True
    await user.should_see("Forging rin…")                       # the header chip
    fake.release_all()
    await settle(core, fake)
    await user.should_see("Speak with rin")


@pytest.mark.scenario("freeing")
async def test_freeing_reads_unloading(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_see("Unloading Multilingual…")
    assert the(user, "forge.go").enabled is False


@pytest.mark.scenario("forge_failed")
async def test_a_failed_forge_toasts_and_returns_the_button(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.go").click()
    await settle(core, fake)
    await user.should_see("Forge failed: CUDA out of memory")
    assert the(user, "forge.go").label == "Forge Voice"


# ---- the result row -----------------------------------------------------------------------------------

@pytest.mark.scenario("forged")
async def test_the_test_line_plays_without_a_load(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    fake.reset_records()
    user.find(marker="forge.test_line").click()
    await settle(core, fake)
    playback = factory_of(user).playback()
    assert playback.vm.target.src == "/media/staged/test-rin?v=1" and playback.vm.target.playing
    assert fake.loads == [] and [call.name for call in fake.calls if call.name == "speak"] == ["speak"]


@pytest.mark.scenario("forged")
async def test_speak_with_opens_speak_on_the_new_voice(user: User, core: Core, fake: FakeEngine,
                                                        stand_in_speak: None) -> None:
    await opened(user, core, fake)
    factory = factory_of(user)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    user.find(marker="forge.speak_with").click()
    await user.should_see("Speak stand-in")
    assert speak.picked == ["rin"] and fake.loads == []


@pytest.mark.scenario("forged")
async def test_forge_another_brings_the_button_back(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="forge.another").click()
    await user.should_see("Name the Voice")
    assert the(user, "forge.name").value == "" and the(user, "forge.consent").props["aria-pressed"] == "false"
    await user.should_see("sample.wav")


# ---- clips: Browse and drops --------------------------------------------------------------------------

@pytest.mark.scenario("forge")
async def test_browse_stages_the_picked_clip(user: User, core: Core, fake: FakeEngine, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    clip = write_clip(tmp_path / "picks" / "picked.wav", 9.0)
    asked: list[dict[str, Any]] = []

    async def pick_file(*, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        asked.append({"filters": filters, "start_dir": start_dir})
        return clip

    monkeypatch.setattr("ui.kit.files.pick_file", pick_file)
    await opened(user, core, fake)
    user.find(marker="forge.browse").click()
    await settle(core, fake)
    await user.should_see("picked.wav")
    await user.should_see("9.0 s · mono · 24 kHz")
    assert asked == [{"filters": CLIP_FILTERS, "start_dir": core.paths.voices}]
    assert the(user, "forge.go").label == "Confirm Consent First"


@pytest.mark.scenario("forge")
async def test_a_native_drop_stages_one_clip(user: User, core: Core, fake: FakeEngine, tmp_path: Path) -> None:
    await opened(user, core, fake)
    one = write_clip(tmp_path / "drops" / "dropped.wav", 7.0)
    core.events.dropped.emit((str(one), str(one)))
    await settle(core, fake)
    await user.should_see(DROP_ONE)
    core.events.dropped.emit((str(one),))
    await settle(core, fake)
    await user.should_see("dropped.wav")
    await user.should_see("7.0 s · mono · 24 kHz")
    assert core.forge_draft.clip_state == "ready" and core.forge_draft.consent is False


# ---- the wiring rows ----------------------------------------------------------------------------------

def test_every_forge_intent_is_wired_or_opted_out() -> None:
    intents = {f"forge.{name}" for name, member in vars(ForgeProtocol).items()
               if inspect.isfunction(member) and not name.startswith("_")}
    intents |= {"forge.confirm", "forge.dismiss"}                     # from the Confirmable base
    covered = {row.intent for row in WIRING} | set(OPT_OUT)
    assert intents - covered == set()
    assert {intent.split(".", 1)[1] for intent in covered if intent.startswith("forge.")} <= set(dir(ForgeController))


CONTROLLERS: dict[str, type] = {"forge": ForgeController, "playback": PlaybackController}


def _spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    owner, name = intent.split(".", 1)
    cls = CONTROLLERS[owner]
    original = getattr(cls, name)
    if inspect.iscoroutinefunction(original):
        async def spy_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy_async)
    else:
        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(intent)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, name, spy)


def _act(user: User, row: Wire) -> None:
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "type":
        found.type(row.arg)
    elif row.act == "trigger":
        event, args = row.arg
        found.trigger(event, args)
    else:
        raise AssertionError(f"the Forge page has no {row.act!r} rows")


def _names_its_model(user: User, row: Wire) -> bool:
    control = the(user, row.marker)
    text = " ".join(str(part) for part in (getattr(control, "label", ""), control.props.get("aria-label", "")))
    return all(load.removeprefix("chatterbox-").capitalize() in text for load in row.loads)


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch,
                           stand_in_speak: None, row: Wire) -> None:
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    await opened(user, core, fake)
    if row.confirm == "ask":
        await until(lambda: core.state.device.detected, "the first device reading")
    for marker in row.before:
        user.find(marker=marker).click()
    await settle(core, fake)
    fake.reset_records()
    calls.clear()
    if row.confirm == "named":
        assert _names_its_model(user, row), f"{row.marker} does not name {row.loads}"
    _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    if row.confirm in ("arm", "ask"):
        assert fake.loads == []
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "ask":
        await user.should_see(marker="confirm.ask")
        user.find(marker="confirm.ask.primary").click()
        await settle(core, fake)
        await user.should_not_see(marker="confirm.ask")
    elif row.confirm == "arm":
        core.scheduler.advance(0.6)
        _act(user, row)
        await settle(core, fake)
    assert fake.loads == list(row.loads)
