"""The Takes page under NiceGUI's user fixture (§8.5, P4): gui.py on a FakeEngine scenario in tmp_path.

Covered: the required and page markers; the speak board as the mockup draws it (caption, count, header, rows,
foot "3 starred · 27.8 s of audio", the compare card at take 7's paused 1.9 s); the takes board on All; the
header tabs; the search after 150 ms; both menus; a row's play, pins and star, updated in place; Remove Unstarred
through its arm into the fixture's scratch Recycle Bin, and the arm voided by a tab and a page change; Export
Starred… and Show Folder through a FakeKit; the compare card's Play, A/B, Keep and Reuse; the first run; a
missing WAV; "Show N more"; the wiring rows (tests\\pages\\wiring\\takes.py); and that pages\\takes.css uses
only emiya.css tokens. The page opens directly on /takes, and Speak is a blank stand-in wherever a test lands
there, so these tests depend on the shell and the Takes page only.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import re
import wave
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nicegui import binding
from nicegui.element import Element
from nicegui.testing import User

from ui import launch
from ui.behaviours import takes as takes_module
from ui.behaviours.playback import PlaybackController
from ui.behaviours.takes import TakesController
from ui.contract.markers import REQUIRED_MARKERS
from ui.core import Core
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.core.services.takes_store import NewTake
from ui.themes.emiya.shell import Shell, ShellRoot

from tests.behaviours import FakeKit
from tests.pages.wiring import Wire
from tests.pages.wiring.takes import WIRING

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "ui" / "themes" / "emiya" / "static"
FOOT = "3 starred · 27.8 s of audio"


# ---- helpers ------------------------------------------------------------------------------------------

def the(user: User, marker: str) -> Any:
    """The one element with this marker that is shown."""
    elements = user.find(marker=marker).elements
    assert len(elements) == 1, f"{len(elements)} visible elements are marked {marker}"
    return next(iter(elements))


def marked(user: User, marker: str) -> list[Element]:
    """Every element with this marker, shown or hidden."""
    return [element for element in user.current_layout.descendants() if marker in element._markers]


def texts_of(element: Element) -> list[str]:
    return [text for child in element.descendants() if (text := getattr(child, "text", "")) and child is not element]


def shell_of(user: User) -> Shell:
    roots = [element for element in user.current_layout.descendants() if isinstance(element, ShellRoot)]
    assert len(roots) == 1
    return roots[0].shell


def controllers(user: User) -> tuple[TakesController, PlaybackController]:
    behaviours = shell_of(user).ctx.behaviours
    return behaviours.takes(), behaviours.playback()


def row_ids(user: User) -> list[int]:
    return [int(m.removeprefix("takes.row.")) for element in the(user, "takes.list").descendants()
            for m in element._markers if m.startswith("takes.row.")]


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def settle(core: Core, fake: FakeEngine) -> None:
    """Let click tasks run, then wait for the work they spawned on the manual clock."""
    for _ in range(5):
        await asyncio.sleep(0)
    clock = core.scheduler
    assert isinstance(clock, ManualScheduler)
    await clock.settle()
    for _ in range(5):
        await asyncio.sleep(0)


async def opened(user: User, core: Core, fake: FakeEngine) -> None:
    """Open /takes directly (the router's initial path) and let the scenario's replay finish."""
    await user.open("/takes")
    await user.should_see(marker="page.takes")
    await settle(core, fake)


@pytest.fixture
def run(user: User) -> launch.Launch:
    assert launch.LAST_LAUNCH is not None
    return launch.LAST_LAUNCH


@pytest.fixture
def quiet_speak(run: launch.Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    """Speak is a blank page here, so a test that lands on it depends on the Takes page only."""
    theme = SimpleNamespace(manifest=run.theme.manifest, build_shell=run.theme.build_shell,
                            pages={**run.theme.pages, "/": lambda _ctx: None})
    monkeypatch.setattr(run, "theme", theme)


@pytest.fixture
def kit(run: launch.Launch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeKit:
    """A recording KitPort for this run's page loads: no dialog and no Explorer ever opens."""
    fake_kit = FakeKit(folder_result=tmp_path / "export")
    monkeypatch.setattr(run, "kit", fake_kit)
    return fake_kit


# ---- markers and the boards ---------------------------------------------------------------------------

async def test_the_takes_page_renders_its_markers(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    for marker in (*REQUIRED_MARKERS["/takes"], "takes.title", "takes.count", "takes.search", "takes.filter_voice",
                   "takes.filter_model", "takes.export", "takes.folder", "takes.foot", "confirm.arm",
                   "takes.compare.card", "takes.compare_play", "takes.ab", "takes.ab.a", "takes.ab.b",
                   "takes.keep_a", "takes.keep_b", "takes.reuse", "takes.tab.session", "takes.tab.all",
                   "takes.tab.starred", *(f"takes.{kind}.{n}" for kind in ("row", "pin_a", "pin_b", "star")
                                          for n in (7, 6, 5, 4))):
        await user.should_see(marker=marker)
    for hidden in ("takes.compare.placeholder", "takes.more", "takes.empty", "takes.row.3"):
        await user.should_not_see(marker=hidden)
    assert "confirm.arm" in the(user, "takes.purge")._markers
    assert the(user, "takes.search").props["placeholder"] == "Search text or voice"
    assert the(user, "takes.search").props["aria-label"] == "Search takes"
    assert the(user, "takes.filter_voice").props["title"] == "Filter by voice"
    assert the(user, "takes.filter_model").props["title"] == "Filter by model"


async def test_the_speak_board_matches_the_mockup(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert (the(user, "takes.title").text, the(user, "takes.count").text) == ("This Session", "4 takes of 7")
    assert the(user, "takes.foot").text == FOOT
    for header in ("#", "Text", "Voice", "Model", "Expr", "Guid", "Length", "When"):
        await user.should_see(header)
    assert row_ids(user) == [7, 6, 5, 4]
    seven = the(user, "takes.row.7")
    assert seven.props["aria-pressed"] == "true" and "on" in seven.parent_slot.parent.classes
    assert texts_of(seven)[:5] == ["7", core.takes.get(7).text, "aiko", "Multilingual", "0.50"]
    assert "4.6 s" in texts_of(seven) and "14:31" in texts_of(seven)
    assert the(user, "takes.row.6").props["aria-pressed"] == "false"
    pin = the(user, "takes.pin_b.7")
    assert (pin.props["aria-pressed"], pin.props["aria-label"]) == ("true", "Unpin Take 7 as B")
    assert "vt-hov" not in pin.classes and "vt-hov" in the(user, "takes.pin_a.7").classes
    star = the(user, "takes.star.7")
    assert (star.props["aria-pressed"], star.props["aria-label"]) == ("true", "Unstar Take 7")
    assert the(user, "takes.star.6").props["aria-label"] == "Star Take 6" and "vt-hov" in the(user, "takes.star.6").classes
    # the compare card: pins A 5 and B 7, take 7 paused at 1.9 s (M L1272-1276)
    side_a, side_b = the(user, "takes.compare.side.a"), the(user, "takes.compare.side.b")
    assert texts_of(side_a) == ["A · Take 5", "5.2 s"] and texts_of(side_b) == ["B · Take 7", "4.6 s"]
    title_a, title_b = (next(e for e in side.descendants() if "vt-tcmp-title" in e.classes) for side in (side_a, side_b))
    assert "is-active" in title_b.classes and "is-active" not in title_a.classes
    assert the(user, "takes.compare.time").text == "0:01.9 / 0:04.6"
    note = the(user, "takes.compare.note")
    assert note.text == "Differs in guidance" and "vt-c-gold-dim" in note.classes
    assert the(user, "takes.compare_play").props["aria-label"] == "Play"
    assert [texts_of(row) for row in the(user, "takes.compare.rows")] == [
        ["Voice", "aiko", "aiko"], ["Model", "Multilingual", "Multilingual"], ["Language", "Japanese", "Japanese"],
        ["Expressiveness", "0.50", "0.50"], ["Guidance", "0.30", "0.50"], ["Length", "5.2 s", "4.6 s"]]
    assert ["is-diff" in row.classes for row in the(user, "takes.compare.rows")] == [False] * 4 + [True, True]
    assert (the(user, "takes.ab.a").props["aria-pressed"], the(user, "takes.ab.b").props["aria-pressed"]) == (
        "false", "true")
    assert the(user, "takes.ab").props["aria-label"] == "Listen to A or B"
    assert fake.loads == []


@pytest.mark.scenario("takes")
async def test_the_takes_board_opens_on_all(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "takes.tab.all").props["aria-selected"] == "true"
    assert (the(user, "takes.title").text, the(user, "takes.count").text) == ("All Takes", "7 takes")
    assert row_ids(user) == [7, 6, 5, 4, 3, 2, 1]
    one = the(user, "takes.row.1")
    assert texts_of(one)[3:6] == ["Turbo", "—", "—"]


async def test_the_header_tabs_switch_the_list(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="takes.tab.starred").click()
    await user.should_see("Starred Takes")
    assert the(user, "takes.count").text == "3 takes of 7" and row_ids(user) == [7, 4, 3]
    assert the(user, "takes.tab.starred").props["aria-selected"] == "true"
    user.find(marker="takes.tab.all").click()
    await user.should_see("All Takes")
    assert row_ids(user) == [7, 6, 5, 4, 3, 2, 1]


async def test_the_search_follows_after_150_ms(user: User, core: Core, fake: FakeEngine, clock: ManualScheduler) -> None:
    await opened(user, core, fake)
    user.find(marker="takes.tab.all").click()
    user.find(marker="takes.search").type("marco")
    clock.advance(0.1)
    assert len(row_ids(user)) == 7
    clock.advance(0.05)
    await user.should_see("1 take of 7")
    assert row_ids(user) == [4]


# ---- the menus (D-T2) ---------------------------------------------------------------------------------

async def test_the_voice_menu_opens_picks_and_closes(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    button = the(user, "takes.filter_voice")
    menu = button.parent_slot.parent
    assert button.text == "All Voices" and button.props["aria-expanded"] == "false"
    assert button.props["aria-haspopup"] == "menu"
    user.find(marker="takes.filter_voice").click()
    assert "is-open" in menu.classes and button.props["aria-expanded"] == "true"
    choices = [the(user, f"takes.filter_voice.{key}") for key in
               ("all", "voice.aiko", "voice.marco", "voice.friend", "voice.narrator", "voice.host-turbo")]
    assert [choice.props["aria-checked"] for choice in choices] == ["true"] + ["false"] * 5
    assert choices[0].props["role"] == "menuitemradio"
    user.find(marker="takes.filter_voice.voice.aiko").click()
    assert "is-open" not in menu.classes and button.text == "aiko"
    await user.should_see("3 takes of 7")
    assert row_ids(user) == [7, 6, 5]
    assert the(user, "takes.filter_voice.voice.aiko").props["aria-checked"] == "true"
    user.find(marker="takes.filter_voice").click()
    assert "is-open" in menu.classes
    user.find(marker="takes.filter_voice.backdrop").click()               # a click outside closes it
    assert "is-open" not in menu.classes
    user.find(marker="takes.filter_voice").click()
    user.find(marker="takes.filter_voice").trigger("keydown.esc")         # so does Escape
    assert "is-open" not in menu.classes
    user.find(marker="takes.filter_voice.all").click()
    await user.should_see("4 takes of 7")


@pytest.mark.scenario("takes")
async def test_the_model_menu_filters_by_model(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    assert the(user, "takes.filter_model").text == "All Models"
    user.find(marker="takes.filter_model").click()
    user.find(marker="takes.filter_model.chatterbox-standard").click()
    await user.should_see("1 take of 7")
    assert row_ids(user) == [2] and the(user, "takes.filter_model").text == "Standard"
    user.find(marker="takes.filter_model.all").click()
    await user.should_see("7 takes")


@pytest.mark.scenario("firstrun")
async def test_the_first_run_page(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    await user.should_see("No takes yet · generate one on Speak")
    await user.should_see(marker="takes.compare.placeholder")
    await user.should_see("Pin two takes as A and B")
    await user.should_see("Hover a row and use its A and B buttons. Pins stay set as you move between pages.")
    await user.should_not_see(marker="takes.compare.card")
    assert (the(user, "takes.count").text, the(user, "takes.foot").text) == ("0 takes", "0 starred · 0.0 s of audio")
    assert the(user, "takes.filter_voice").props.get("disabled") is True
    assert the(user, "takes.export").props.get("disabled") is True
    assert the(user, "takes.purge").props.get("disabled") is True
    assert "disabled" not in the(user, "takes.folder").props
    assert row_ids(user) == []


# ---- rows ---------------------------------------------------------------------------------------------

async def test_a_row_plays_pins_and_stars_in_place(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    rows_before = {element.id for element in the(user, "takes.list").descendants()}
    _, playback = controllers(user)
    user.find(marker="takes.row.6").click()
    await settle(core, fake)
    assert core.session.take_id == 6 and playback.vm.target.src == "/media/take/6" and playback.vm.target.playing
    assert the(user, "takes.row.6").props["aria-pressed"] == "true"
    user.find(marker="takes.pin_a.6").click()
    await user.should_see("A · Take 6")
    assert the(user, "takes.pin_a.6").props["aria-label"] == "Unpin Take 6 as A"
    user.find(marker="takes.star.6").click()
    await settle(core, fake)
    await user.should_see("4 starred · 27.8 s of audio")
    assert the(user, "takes.star.6").props["aria-pressed"] == "true"
    assert {element.id for element in the(user, "takes.list").descendants()} == rows_before   # updated in place
    assert fake.loads == []


async def test_a_new_take_adds_its_row_on_top_and_keeps_the_others(user: User, core: Core, fake: FakeEngine) -> None:
    """R3-3: a new take arriving adds one row on top; the rows already shown keep their elements (they used to be
    rebuilt, which froze the page for seconds with thousands of rows shown)."""
    await opened(user, core, fake)
    old_row = the(user, "takes.row.7")
    old_ids = row_ids(user)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(24000)
        sink.writeframes(b"\x00\x10" * 24000)
    record = NewTake(voice="aiko", backend="chatterbox-multilingual", language="ja", text="新しいテイク",
                     exaggeration=0.5, cfg_weight=0.5, gen_seconds=1.0, session=core.session_id)
    take = await asyncio.to_thread(core.takes.add, buffer.getvalue(), record, core.paths.takes_default)
    core.publish_takes()
    await until(lambda: row_ids(user)[:1] == [take.id], "the new take's row")
    assert not old_row.is_deleted and the(user, "takes.row.7") is old_row and row_ids(user) == [take.id, *old_ids]
    assert the(user, "takes.count").text == "5 takes of 8"
    assert the(user, "takes.row.8").props["aria-pressed"] == "false"


async def test_a_missing_wav_dims_its_row_and_says_so(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    path = core.takes.path_of(6)
    path.unlink()
    await asyncio.to_thread(core.takes.check_files)
    core.publish_takes()
    await user.should_see("Missing")
    assert "is-missing" in the(user, "takes.row.6").parent_slot.parent.classes
    user.find(marker="takes.row.6").click()
    await user.should_see(f"Take 6's WAV is missing: {path}")
    assert core.session.take_id == 7


async def test_show_more_reveals_the_next_rows(user: User, core: Core, fake: FakeEngine,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(takes_module, "PAGE_ROWS", 3)
    await opened(user, core, fake)
    user.find(marker="takes.tab.all").click()
    await user.should_see("Show 3 more")
    assert row_ids(user) == [7, 6, 5]
    user.find(marker="takes.more").click()
    await user.should_see("Show 1 more")
    user.find(marker="takes.more").click()
    await user.should_not_see(marker="takes.more")
    assert len(row_ids(user)) == 7


# ---- Remove Unstarred ---------------------------------------------------------------------------------

async def test_remove_unstarred_arms_then_removes(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    trash = core.trash
    assert isinstance(trash, launch.ScratchTrash)
    user.find(marker="takes.purge").click()
    await settle(core, fake)
    purge = the(user, "takes.purge")
    assert purge.label == "Click Again to Remove 4" and "is-armed" in purge.classes and trash.moved == []
    core.scheduler.advance(0.6)
    user.find(marker="takes.purge").click()
    await settle(core, fake)
    assert sorted(path.name for path in trash.moved) == ["aiko_take5.wav", "aiko_take6.wav", "host-turbo_take1.wav",
                                                         "narrator_take2.wav"]
    await user.should_see("Moved 4 unstarred takes to the Recycle Bin")
    assert row_ids(user) == [7, 4] and the(user, "takes.foot").text == "3 starred · 10.6 s of audio"
    await user.should_see(marker="takes.compare.placeholder")             # pin A was take 5
    assert the(user, "takes.purge").props.get("disabled") is True and fake.loads == []


async def test_the_arm_is_voided_by_a_tab_and_a_page_change(user: User, core: Core, fake: FakeEngine,
                                                           quiet_speak: None) -> None:
    await opened(user, core, fake)
    user.find(marker="takes.purge").click()
    await user.should_see("Click Again to Remove 4")
    user.find(marker="takes.tab.all").click()
    await user.should_see("Remove Unstarred")
    user.find(marker="takes.purge").click()
    await user.should_see("Click Again to Remove 4")
    user.find(marker="nav.speak").click()
    await user.should_see(marker="page.speak")
    user.find(marker="nav.takes").click()
    await user.should_see(marker="page.takes")
    assert the(user, "takes.purge").label == "Remove Unstarred"
    user.find(marker="takes.purge").click()
    await settle(core, fake)
    assert the(user, "takes.purge").label == "Click Again to Remove 4" and core.trash.moved == []


# ---- the footer through the kit -----------------------------------------------------------------------

async def test_export_and_show_folder_go_through_the_kit(user: User, core: Core, fake: FakeEngine, kit: FakeKit,
                                                        tmp_path: Path) -> None:
    await opened(user, core, fake)
    user.find(marker="takes.export").click()
    await settle(core, fake)
    folder = tmp_path / "export"
    assert len(kit.called("pick_folder")) == 1
    assert sorted(p.name for p in folder.iterdir()) == ["aiko_take7.wav", "friend_take3.wav", "marco_take4.wav"]
    await user.should_see(f"Exported 3 starred takes to {folder}")
    user.find(marker="takes.folder").click()
    assert kit.called("open_folder") == [{"path": core.paths.root / "outputs" / "takes"}]
    assert fake.loads == []


# ---- the compare card ---------------------------------------------------------------------------------

async def test_the_compare_controls(user: User, core: Core, fake: FakeEngine, quiet_speak: None) -> None:
    await opened(user, core, fake)
    takes, playback = controllers(user)
    user.find(marker="takes.compare_play").click()                         # take 7 (B) is current: it plays
    assert playback.vm.target.src == "/media/take/7" and playback.vm.target.playing
    playback.on_player_state(True, 2.3, 4.6, False, "")
    await until(lambda: the(user, "takes.compare_play").props["aria-label"] == "Pause", "the Pause label")
    assert the(user, "takes.compare.time").text == "0:02.3 / 0:04.6"
    assert the(user, "takes.compare_play").icon == "pause"
    user.find(marker="takes.ab.a").click()                                 # A at the same point of the line
    target = playback.vm.target
    assert core.session.take_id == 5 and (target.src, target.playing, target.at) == ("/media/take/5", True, 2.3)
    await until(lambda: the(user, "takes.ab.a").props["aria-pressed"] == "true", "A current")
    user.find(marker="takes.keep_a").click()
    await settle(core, fake)
    await user.should_see("Kept Take 5 · starred")
    assert core.takes.get(5).starred
    user.find(marker="takes.reuse").click()
    await user.should_see(marker="page.speak")
    await user.should_see("Delivery set from Take 5")
    session = core.session
    assert (session.voice_id, session.language, session.exaggeration, session.cfg_weight) == ("aiko", "ja", 0.5, 0.3)
    assert fake.loads == [] and takes.vm.compare is not None


# ---- wiring rows --------------------------------------------------------------------------------------

CONTROLLERS: dict[str, type] = {"takes": TakesController, "playback": PlaybackController}


def _spy(monkeypatch: pytest.MonkeyPatch, intent: str, calls: list[str]) -> None:
    owner, name = intent.split(".")
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


async def _act(user: User, row: Wire) -> None:
    found = user.find(marker=row.marker)
    if row.act == "click":
        found.click()
    elif row.act == "type":
        found.type(row.arg)
    else:
        raise AssertionError(f"the Takes rows have no {row.act!r} act")


@pytest.mark.parametrize("row", [
    pytest.param(row, marks=pytest.mark.scenario(row.scenario), id=f"{index}-{row.marker}-{row.scenario}")
    for index, row in enumerate(WIRING)])
async def test_wiring_rows(user: User, core: Core, fake: FakeEngine, kit: FakeKit, quiet_speak: None,
                           monkeypatch: pytest.MonkeyPatch, row: Wire) -> None:
    for section, key, value in row.settings:
        core.settings.set(section, key, value)
    calls: list[str] = []
    _spy(monkeypatch, row.intent, calls)
    await user.open(row.route)
    await user.should_see(marker="page.takes")
    await settle(core, fake)
    fake.reset_records()
    calls.clear()
    await _act(user, row)
    await settle(core, fake)
    assert row.intent in calls, f"{row.marker} did not reach {row.intent}"
    if row.confirm == "arm":
        assert fake.loads == [] and core.trash.moved == []
    for text in row.shows:
        await user.should_see(text)
    for text in row.hides:
        await user.should_not_see(text)
    if row.confirm == "arm":
        core.scheduler.advance(0.6)
        await _act(user, row)
        await settle(core, fake)
        assert len(core.trash.moved) == 4
    assert fake.loads == list(row.loads)
    folders = [Path(call["path"]) for call in kit.called("open_folder")]
    assert folders == ([core.paths.root / "outputs" / "takes"] if row.intent == "takes.show_folder" else [])
    assert kit.called("reveal") == []


def test_the_wiring_rows_open_takes_and_load_nothing() -> None:
    assert WIRING and all(row.route == "/takes" and row.loads == () for row in WIRING)


# ---- bindings and the stylesheet ----------------------------------------------------------------------

async def test_the_page_binds_passively(user: User, core: Core, fake: FakeEngine) -> None:
    await opened(user, core, fake)
    user.find(marker="takes.tab.all").click()
    user.find(marker="takes.pin_a.6").click()
    await settle(core, fake)
    assert binding.active_links == []


COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def test_the_page_stylesheet_uses_only_the_theme_tokens() -> None:
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "pages" / "takes.css").read_text(encoding="utf-8"), flags=re.DOTALL)
    tokens = re.sub(r"/\*.*?\*/", "", (STATIC / "emiya.css").read_text(encoding="utf-8"), flags=re.DOTALL)
    assert not COLOUR.search(css)
    declared = set(re.findall(r"(--[\w-]+)\s*:", tokens))
    assert set(re.findall(r"var\((--[\w-]+)", css)) - declared == set()
    assert css.count("{") == css.count("}") and "url(" not in css and "http" not in css
    keyframes = set(re.findall(r"@keyframes\s+([\w-]+)", tokens))
    assert set(re.findall(r"animation:\s*([\w-]+)", css)) <= keyframes
