"""CommandRegistry (§5.5): defaults, normalisation, hint chips, bind with its clash and reserved refusals,
hand-edited duplicates, and dispatch per scope; the gen keys queue while a take runs and load nothing."""
from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from ui.behaviours import PAGE_CONTROLLERS, BehaviourFactory, PageController
from ui.behaviours.commands import (COMMANDS, CommandRegistry, canonical, combo_parts, label_of, normalize_combo,
                                    reserved_reason)
from ui.contract import viewmodels as vms
from ui.contract.plans import ActionState, ConfirmRequest
from ui.contract.viewmodels import BindResult
from ui.core.jobs import QUEUE_FULL
from ui.core.services.settings import DEFAULT_KEYS

from tests.behaviours import (MTL, TUR, FakeKit, StubSpeak, factory_for, no_error_logs, offline,  # noqa: F401
                              started)
from tests.core.test_facade import Rig, entered, request, rigs  # noqa: F401


async def registry(rigs: Callable[..., Rig], scenario: str = "speak",
                   **options: object) -> tuple[Rig, BehaviourFactory, CommandRegistry, FakeKit]:
    rig = await started(rigs, scenario, **options)
    factory, kit = factory_for(rig)
    return rig, factory, factory.commands(), kit


# ---------- the table ----------

def test_the_commands_and_their_scopes() -> None:
    assert [(c.id, c.label, c.scope) for c in COMMANDS] == [
        ("gen", "Generate", "speak"), ("gen3", "Generate 3 Takes", "speak"), ("play", "Play / Pause", "every"),
        ("prev", "Previous Take", "speak"), ("next", "Next Take", "speak"), ("ab", "Switch A / B", "every"),
        ("save", "Save As…", "speak"), ("search", "Search", "every"),
        ("call_send", "Send to Call", "global"), ("call_stop", "Stop Sending", "global")]
    assert [c.id for c in COMMANDS] == list(DEFAULT_KEYS)
    assert (label_of("ab"), label_of("nope")) == ("Switch A / B", "")


@pytest.mark.parametrize(("key", "mods", "combo"), [
    (" ", (False, False, False), "Space"),
    ("k", (True, False, False), "Ctrl+K"),
    ("Enter", (True, True, False), "Ctrl+Shift+Enter"),
    ("ArrowUp", (False, False, True), "Alt+Up"),
    ("ArrowDown", (False, False, True), "Alt+Down"),
    ("b", (True, False, False), "Ctrl+B"),
    ("+", (True, False, False), "Ctrl++"),
    ("F4", (False, False, True), "Alt+F4"),
    ("é", (True, True, True), "Ctrl+Shift+Alt+É"),
    ("Shift", (False, True, False), ""),
    ("Control", (True, False, False), ""),
    ("", (False, False, False), ""),
])
def test_normalize_builds_the_capture_combo(key: str, mods: tuple[bool, bool, bool], combo: str) -> None:
    ctrl, shift, alt = mods
    assert normalize_combo(key, ctrl=ctrl, shift=shift, alt=alt) == combo


@pytest.mark.parametrize(("stored", "clean"), [
    ("Ctrl+K", "Ctrl+K"), ("ctrl+k", "Ctrl+K"), ("shift+ctrl+enter", "Ctrl+Shift+Enter"), ("Ctrl++", "Ctrl++"),
    ("+", "+"), ("  Space ", "Space"), ("space", "Space"), ("Alt+ArrowUp", "Alt+Up"), ("alt+arrowdown", "Alt+Down"),
    ("Ctrl+f12", "Ctrl+F12"), ("Ctrl+MediaPlayPause", "Ctrl+MediaPlayPause"), ("Ctrl+", ""), ("Ctrl+Ctrl+K", ""),
    ("Hyper+K", ""), ("Ctrl+Shift", ""), ("", ""), ("Ctrl++K", ""),
])
def test_canonical_reads_hand_edited_combos(stored: str, clean: str) -> None:
    assert canonical(stored) == clean


def test_combo_parts_and_reserved_combos() -> None:
    assert combo_parts("Ctrl+Enter") == ("Ctrl", "Enter")
    assert combo_parts("Ctrl++") == ("Ctrl", "+")
    assert combo_parts("") == () and combo_parts("Ctrl+") == ()
    assert reserved_reason("Alt+F4") == "Alt+F4 is reserved by Windows"
    for combo in ("Ctrl+=", "Ctrl+-", "Ctrl++", "Ctrl+0", "Ctrl+Shift+="):
        assert reserved_reason(combo) == f"{combo} is the window's zoom"
    assert reserved_reason("Ctrl+K") == reserved_reason("Alt+0") == reserved_reason("0") == ""


# ---------- bindings ----------

async def test_the_defaults_from_the_mockup(rigs: Callable[..., Rig]) -> None:
    _, factory, commands, _ = await registry(rigs)
    assert dict(commands.bindings()) == dict(DEFAULT_KEYS)
    assert commands.combos() == {"Ctrl+Enter": "gen", "Ctrl+Shift+Enter": "gen3", "Space": "play", "Alt+Up": "prev",
                                 "Alt+Down": "next", "Ctrl+B": "ab", "Ctrl+S": "save", "Ctrl+K": "search"}
    assert commands.hint_parts("gen") == ("Ctrl", "Enter")
    assert commands.hint_parts("search") == ("Ctrl", "K")
    assert commands.normalize("k", ctrl=True, shift=False, alt=False) == "Ctrl+K"
    assert commands.label("save") == "Save As…"
    assert factory.status().vm.search_badge == "Ctrl K"


async def test_bind_and_clear_a_slot(rigs: Callable[..., Rig]) -> None:
    rig, _, commands, _ = await registry(rigs)
    assert commands.bind("gen", "alt", "Ctrl+J") == BindResult(True, "Bound Ctrl+J")
    assert commands.bindings()["gen"] == ("Ctrl+Enter", "Ctrl+J") and commands.combos()["Ctrl+J"] == "gen"
    assert rig.core.settings.get("core", "keys")["gen"] == ("Ctrl+Enter", "Ctrl+J")
    assert commands.bind("gen", "key", "") == BindResult(True, "Cleared the binding")
    assert commands.hint_parts("gen") == ("Ctrl", "J")    # the alternate stands in for the key hint
    assert commands.bind("gen", "alt", "") == BindResult(True, "Cleared the binding")
    assert commands.hint_parts("gen") == () and "gen" not in commands.combos().values()
    rig.core.settings.flush()
    stored = json.loads(rig.paths.settings.read_text(encoding="utf-8"))
    assert stored["core"]["keys"]["gen"] == ["", ""]


async def test_a_clash_names_the_command_that_has_the_combo(rigs: Callable[..., Rig]) -> None:
    _, _, commands, _ = await registry(rigs)
    assert commands.bind("play", "key", "Ctrl+K") == BindResult(False, "Ctrl+K is already used by Search")
    assert commands.bind("gen", "alt", "Ctrl+Enter") == BindResult(False, "Ctrl+Enter is already used by Generate")
    assert commands.bind("gen", "key", "Ctrl+Enter") == BindResult(True, "Bound Ctrl+Enter")   # its own slot
    assert dict(commands.bindings()) == dict(DEFAULT_KEYS)


@pytest.mark.parametrize(("combo", "text"), [
    ("Alt+F4", "Alt+F4 is reserved by Windows"),
    ("Ctrl+=", "Ctrl+= is the window's zoom"),
    ("Ctrl+-", "Ctrl+- is the window's zoom"),
    ("Ctrl++", "Ctrl++ is the window's zoom"),
    ("Ctrl+0", "Ctrl+0 is the window's zoom"),
])
async def test_reserved_combinations_are_refused(rigs: Callable[..., Rig], combo: str, text: str) -> None:
    _, _, commands, _ = await registry(rigs)
    assert commands.bind("save", "alt", combo) == BindResult(False, text)
    assert commands.bindings()["save"] == ("Ctrl+S", "")


async def test_bind_refuses_unknown_commands_slots_and_non_combos(rigs: Callable[..., Rig]) -> None:
    _, _, commands, _ = await registry(rigs)
    with pytest.raises(KeyError):
        commands.bind("dance", "key", "Ctrl+D")
    with pytest.raises(ValueError):
        commands.bind("gen", "third", "Ctrl+D")  # type: ignore[arg-type]
    assert commands.bind("gen", "alt", "Ctrl+") == BindResult(False, "Ctrl+ is not a key combination")


async def test_a_hand_edited_file_keeps_the_first_of_each_combo(rigs: Callable[..., Rig]) -> None:
    keys = {"gen": ["ctrl+k", ""], "search": ["Ctrl+K", "Ctrl+J"], "play": ["Alt+F4", "Space"],
            "ab": ["Ctrl+B", "Ctrl+Y"], "save": ["Ctrl+=", "Ctrl+Y"]}
    _, factory, commands, _ = await registry(rigs, settings={"keys": keys})
    found = commands.bindings()
    assert found["gen"] == ("Ctrl+K", "")
    assert found["search"] == ("", "Ctrl+J")             # the later Ctrl+K is cleared
    assert found["play"] == ("", "Space")                # a reserved combo is never bound
    assert found["ab"] == ("Ctrl+B", "Ctrl+Y") and found["save"] == ("", "")   # ab came first: it keeps Ctrl+Y
    assert commands.combos()["Ctrl+K"] == "gen" and factory.status().vm.search_badge == "Ctrl J"


# ---------- dispatch ----------

async def test_play_works_on_every_page(rigs: Callable[..., Rig]) -> None:
    _, factory, commands, _ = await registry(rigs)
    playback = factory.playback()
    commands.handle("play", "/takes")
    assert playback.is_playing_take(7)
    commands.handle("play", "/settings?tab=keys")
    assert playback.vm.playing is False


async def test_speak_only_commands_do_nothing_elsewhere(rigs: Callable[..., Rig]) -> None:
    rig, factory, commands, kit = await registry(rigs)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    for command_id in ("gen", "gen3", "prev", "next", "save"):
        commands.handle(command_id, "/voices")
    await rig.settle()
    assert speak.generated == [] and kit.calls == [] and rig.core.session.take_id == 7
    commands.handle("unknown", "/")
    commands.handle("next", "/")
    assert rig.core.session.take_id == 6
    commands.handle("prev", "/")
    assert rig.core.session.take_id == 7


async def test_ab_switches_between_the_pins(rigs: Callable[..., Rig]) -> None:
    rig, factory, commands, _ = await registry(rigs)
    session = rig.core.session
    commands.handle("ab", "/takes")                      # current 7 is B: to A
    assert session.take_id == 5
    commands.handle("ab", "/takes")
    assert session.take_id == 7
    session.take_id = 6                                  # neither pin: to A
    commands.handle("ab", "/")
    assert session.take_id == 5
    session.pin_b = 0
    commands.handle("ab", "/")
    assert session.take_id == 5                          # both pins are needed


async def test_save_runs_save_as_on_speak_with_a_take(rigs: Callable[..., Rig]) -> None:
    rig, _, commands, kit = await registry(rigs)
    commands.handle("save", "/")
    await rig.settle()
    assert kit.called("save_as")[0]["default_name"] == "aiko_take7.wav"
    _, _, none, none_kit = await registry(rigs, "firstrun")
    none.handle("save", "/")
    assert none_kit.calls == []


async def test_search_focuses_the_rail_box(rigs: Callable[..., Rig]) -> None:
    _, _, commands, _ = await registry(rigs)
    focused: list[int] = []
    commands.handle("search", "/forge")                  # no box registered yet: nothing
    commands.set_focus_search(lambda: focused.append(1))
    commands.handle("search", "/forge")
    assert focused == [1]


async def test_the_gen_key_before_the_speak_page_exists_does_nothing(rigs: Callable[..., Rig],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    rig, factory, commands, _ = await registry(rigs)
    monkeypatch.setitem(PAGE_CONTROLLERS, "speak", PageController(
        "ui.behaviours.not_there_yet", "SpeakController", vms.SpeakController, vms.SpeakVM))
    commands.handle("gen", "/")
    commands.handle("gen3", "/")
    await rig.settle()
    assert rig.state.job.kind == "" and rig.state.takes.next_id == 8 and rig.fake.loads == []


async def test_the_gen_keys_pass_the_generate_buttons_token(rigs: Callable[..., Rig]) -> None:
    rig, factory, commands, _ = await registry(rigs)
    speak = StubSpeak(factory)
    named = ConfirmRequest(token="tok-1", kind="switch_model", style="named")
    speak.vm.generate = ActionState(True, "Switch to Turbo & Generate", pending=named)
    factory.provide("speak", speak)
    commands.handle("gen", "/")
    await rig.settle()
    assert [(n, token) for n, token, _ in speak.generated] == [(1, "tok-1")]


async def test_the_gen_key_queues_while_a_take_runs_and_loads_nothing(rigs: Callable[..., Rig]) -> None:
    rig, factory, commands, _ = await registry(rigs)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    assert speak.vm.generate.enabled is False            # the Generate button is disabled while generating
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])            # the `generating` board
    await entered(hold)
    commands.handle("gen", "/")
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    assert rig.state.job.queued == 1 and len(rig.core.jobs.queued) == 1
    assert speak.generated[0][2].status == "queued"
    commands.handle("gen3", "/")
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    assert rig.state.job.queued == 3
    assert speak.generated[1][2].status == "queued" and speak.generated[1][2].reason == QUEUE_FULL
    assert rig.fake.loads == []
    hold.release()
    await rig.settle()
    assert [take.id for take in rig.state.takes.takes[:4]] == [11, 10, 9, 8] and rig.fake.loads == []


async def test_the_gen_key_for_another_backend_is_refused_while_a_take_runs(rigs: Callable[..., Rig]) -> None:
    rig, factory, commands, _ = await registry(rigs)
    speak = StubSpeak(factory)
    factory.provide("speak", speak)
    hold = rig.fake.hold("speak")
    rig.core.jobs.start_generate([request()])
    await entered(hold)
    rig.core.session.voice_id = "host-turbo"
    commands.handle("gen", "/")
    await rig.clock.settle(ignore=[rig.core.jobs.task])
    result = speak.generated[0][2]
    assert result.status == "refused"
    assert result.reason == "Busy with Multilingual · host-turbo needs Turbo · switch after the queue"
    assert rig.state.job.queued == 0 and rig.fake.loads == []
    hold.release()
    await rig.settle()
    assert rig.fake.loads == [] and rig.state.engine.resident == MTL and TUR not in rig.fake.loads
