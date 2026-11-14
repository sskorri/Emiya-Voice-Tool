"""SettingsController (§5.11, §6.12, §7): every row of the General, Effects and Keys tabs, the key capture rules
(captureKey, M L1984-2008, plus the reserved combinations and IME composition), the About facts with the GPU
detecting, detected, absent and failed, and that every value is saved to ui_settings.json and read back by a
second core built on the same data root (W12).

Pure Python against a real Core on FakeEngine with the manual clock and a fake KitPort; no NiceGUI client.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ui.behaviours import BehaviourFactory
from ui.behaviours.settings import (
    BROWSE_LABEL,
    LISTENING,
    NEEDS_WINDOW,
    NO_CUDA_NOTE,
    WRITE_PROBE,
    SettingsController,
    speed_value,
)
from ui.contract import viewmodels as vms
from ui.contract.plans import ActionState, ForgeRequest
from ui.core.services.device import GPU_TIP
from ui.core.services.settings import ABOUT_TIPS, DEFAULT_KEYS, SettingsStore, engine_text

from tests.behaviours import (MTL, STD, TUR, FakeKit, factory_for, no_error_logs, offline, started,  # noqa: F401
                              texts, tick, watch_toasts)
from tests.core.test_facade import Rig, entered, make_rig, rigs  # noqa: F401

THEME = "theme.emiya"
LABELS = ("Generate", "Generate 3 Takes", "Play / Pause", "Previous Take", "Next Take", "Switch A / B", "Save As…",
          "Search", "Send to Call", "Stop Sending")
IDS = ("gen", "gen3", "play", "prev", "next", "ab", "save", "search", "call_send", "call_stop")


# ---------- helpers ----------

class ForgeStub:
    """The Forge controller's set_backend, for the Default Model re-pick (P3 owns the real one)."""

    def __init__(self) -> None:
        self.backends: list[str] = []

    def set_backend(self, backend: str) -> None:
        self.backends.append(backend)

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Navigation closes the language list (StatusController.on_route)."""


@dataclass
class SlowKit(FakeKit):
    """A folder dialog that stays open until the test lets it close."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        found = await super().pick_folder(title=title, start_dir=start_dir)
        await self.gate.wait()
        return found


@dataclass
class BrokenKit(FakeKit):
    """A folder dialog that fails to open."""

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        raise RuntimeError("the window is gone")


async def controller(rigs: Callable[..., Rig], scenario: str = "speak", *, kit: FakeKit | None = None,
                     browser: bool = False, **options: Any) -> tuple[SettingsController, BehaviourFactory, Rig]:
    """A started rig's Settings controller, with a stub Forge controller in its factory."""
    rig = await started(rigs, scenario, **options)
    factory, _ = factory_for(rig, kit, browser=browser)
    factory.provide("forge", ForgeStub())
    return factory.settings(), factory, rig


def rows(settings: SettingsController) -> dict[str, vms.KeyRowVM]:
    return {row.id: row for row in settings.vm.key_rows}


def press(settings: SettingsController, command_id: str, slot: str, key: str, *, ctrl: bool = False,
          shift: bool = False, alt: bool = False, composing: bool = False) -> None:
    settings.capture(command_id, slot, key=key, ctrl=ctrl, shift=shift, alt=alt, composing=composing)


def shape(func: Callable[..., Any]) -> str:
    """'async (a, *, b=)': the parameter shape after self, as tests\\rules\\test_contract.py writes it."""
    out, keyword_only = [], False
    for param in list(inspect.signature(func).parameters.values())[1:]:
        if param.kind is param.KEYWORD_ONLY and not keyword_only:
            out.append("*")
            keyword_only = True
        out.append(param.name if param.default is param.empty else f"{param.name}=")
    return ("async " if inspect.iscoroutinefunction(func) else "") + "(" + ", ".join(out) + ")"


async def until(predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def shut(rig: Rig) -> None:
    """What the rigs fixture does at teardown, for rigs made with make_rig."""
    rig.fake.release_all()
    with contextlib.suppress(Exception):
        await rig.clock.settle(timeout=10)
    rig.clock.cancel_pending()


# ---------- the controller ----------

async def test_the_controller_matches_its_protocol(rigs: Callable[..., Rig]) -> None:
    settings, factory, _ = await controller(rigs)
    assert isinstance(settings, vms.SettingsController) and factory.settings() is settings
    shapes: dict[str, str] = {}
    for cls in reversed(vms.SettingsController.__mro__):
        if cls is not object and getattr(cls, "_is_protocol", False):
            shapes.update({name: shape(member) for name, member in vars(cls).items()
                           if inspect.isfunction(member) and not name.startswith("_")})
    assert {name: shape(getattr(SettingsController, name)) for name in shapes} == shapes
    assert await settings.confirm("any token") is None and settings.dismiss() is None    # Settings never asks


async def test_the_view_model_starts_from_the_mockup_defaults(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    vm = settings.vm
    takes = str(rig.paths.root / "outputs" / "takes")
    assert (vm.tab, vm.out_dir_text, vm.out_tip) == ("general", takes, takes)
    assert vm.browse == ActionState(True, BROWSE_LABEL, tone="plain") and BROWSE_LABEL == "Browse…"
    assert (vm.autoplay, vm.device, vm.device_note, vm.offline) == (True, "cuda", "Falls back to CPU without CUDA",
                                                                    True)
    assert (vm.default_backend, vm.backend_options) == (
        MTL, ((MTL, "Multilingual"), (STD, "Standard"), (TUR, "Turbo")))
    assert (vm.fx_on, vm.fx_speed, vm.fx_speed_text, vm.work_glow, vm.reduce_motion, vm.saber_art) == (
        True, 100, "100%", True, "system", "show")


async def test_key_rows_include_unbound_global_call_commands(rigs: Callable[..., Rig]) -> None:
    settings, _, _ = await controller(rigs)
    found = settings.vm.key_rows
    assert tuple(row.id for row in found) == IDS and tuple(row.label for row in found) == LABELS
    assert tuple(row.key_text for row in found) == tuple(DEFAULT_KEYS[command][0] or "—" for command in IDS)
    assert all(row.alt_text == "—" and not row.key_listening and not row.alt_listening for row in found)
    gen = rows(settings)["gen"]
    assert (gen.key_aria, gen.alt_aria) == ("Generate key: Ctrl+Enter", "Generate alternate key: none")


# ---------- tabs ----------

async def test_a_tab_change_shows_the_tab_and_stops_a_capture(rigs: Callable[..., Rig],
                                                              caplog: pytest.LogCaptureFixture) -> None:
    settings, _, _ = await controller(rigs)
    settings.listen("gen", "key")
    settings.set_tab("keys")
    assert settings.vm.tab == "keys" and not rows(settings)["gen"].key_listening
    settings.set_tab("effects")
    assert settings.vm.tab == "effects"
    settings.set_tab("theme")                                 # there is no Theme tab (or row) in this phase
    assert settings.vm.tab == "effects"
    assert any("Refused the Settings tab 'theme'" in r.getMessage() for r in caplog.records)


# ---------- General: Output Folder ----------

async def test_browse_saves_a_folder_in_the_data_root_relative_and_shows_it_absolute(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    picked = rig.paths.root / "outputs" / "mine"
    factory, kit = factory_for(rig, FakeKit(folder_result=picked))
    settings = factory.settings()
    toasts = watch_toasts(rig)
    await settings.browse_output_folder()
    assert kit.called("pick_folder") == [{"title": "Output Folder", "start_dir": rig.paths.root / "outputs" / "takes"}]
    assert rig.core.settings.get("core", "takes_dir") == "outputs/mine"
    assert (settings.vm.out_dir_text, settings.vm.out_tip) == (str(picked), str(picked))
    assert texts(toasts) == [(f"New takes will be saved in {picked}", False)]
    assert picked.is_dir() and not (picked / WRITE_PROBE).exists()
    await tick()
    assert settings.vm.about.takes == "outputs/mine"           # the About card shows the relative form (§7.3)
    assert settings.vm.browse.enabled is True


async def test_a_folder_outside_the_data_root_stays_absolute(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    outside = tmp_path / "Recordings" / "Voice takes"
    rig = await started(rigs)
    factory, _ = factory_for(rig, FakeKit(folder_result=outside))
    settings = factory.settings()
    await settings.browse_output_folder()
    assert rig.core.settings.get("core", "takes_dir") == str(outside)
    assert settings.vm.out_dir_text == str(outside) and outside.is_dir()
    await tick()
    assert settings.vm.about.takes == str(outside)


async def test_cancelling_the_dialog_changes_nothing(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, kit = factory_for(rig, FakeKit(folder_result=None))
    toasts = watch_toasts(rig)
    await factory.settings().browse_output_folder()
    assert len(kit.called("pick_folder")) == 1 and toasts == []
    assert rig.core.settings.get("core", "takes_dir") == "outputs/takes"


async def test_a_folder_that_cannot_be_written_is_refused(rigs: Callable[..., Rig], tmp_path: Path) -> None:
    blocker = tmp_path / "a file"
    blocker.write_text("not a folder", encoding="utf-8")
    picked = blocker / "takes"
    rig = await started(rigs)
    factory, _ = factory_for(rig, FakeKit(folder_result=picked))
    settings = factory.settings()
    toasts = watch_toasts(rig)
    await settings.browse_output_folder()
    [(text, bad)] = texts(toasts)
    assert bad and text.startswith(f"Can't write to {picked}: ") and len(text) > len(f"Can't write to {picked}: ")
    assert rig.core.settings.get("core", "takes_dir") == "outputs/takes"
    assert settings.vm.out_dir_text == str(rig.paths.root / "outputs" / "takes")


async def test_the_dialog_opens_without_a_start_folder_when_the_folder_is_gone(rigs: Callable[..., Rig],
                                                                               tmp_path: Path) -> None:
    rig = await started(rigs, settings={"takes_dir": str(tmp_path / "unplugged drive")})
    factory, kit = factory_for(rig, FakeKit(folder_result=None))
    await factory.settings().browse_output_folder()
    assert kit.called("pick_folder") == [{"title": "Output Folder", "start_dir": None}]


async def test_browse_is_busy_while_the_dialog_is_open(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    kit = SlowKit(folder_result=rig.paths.root / "outputs" / "slow")
    factory, _ = factory_for(rig, kit)
    settings = factory.settings()
    first = asyncio.create_task(settings.browse_output_folder())
    await until(lambda: bool(kit.called("pick_folder")), "the folder dialog")
    assert settings.vm.browse.enabled is False and settings.vm.browse.label == "Browse…"
    await settings.browse_output_folder()                      # a second click while the dialog is open
    assert len(kit.called("pick_folder")) == 1
    kit.gate.set()
    await first
    assert settings.vm.browse.enabled is True and rig.core.settings.get("core", "takes_dir") == "outputs/slow"


async def test_browse_needs_the_app_window(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, kit = factory_for(rig, FakeKit(folder_result=rig.paths.root / "x"), browser=True)
    settings = factory.settings()
    assert (settings.vm.browse.enabled, settings.vm.browse.reason) == (False, NEEDS_WINDOW)
    await settings.browse_output_folder()
    assert kit.calls == [] and rig.core.settings.get("core", "takes_dir") == "outputs/takes"


async def test_a_dialog_that_fails_to_open_is_a_red_toast(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    factory, _ = factory_for(rig, BrokenKit())
    settings = factory.settings()
    toasts = watch_toasts(rig)
    await settings.browse_output_folder()
    assert texts(toasts) == [("Couldn't open the folder picker: the window is gone", True)]
    assert settings.vm.browse.enabled is True


# ---------- General: the other rows ----------

async def test_autoplay_is_saved(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.set_autoplay(False)
    assert rig.core.settings.get("core", "autoplay") is False and settings.vm.autoplay is False and toasts == []
    settings.set_autoplay(True)
    assert settings.vm.autoplay is True


async def test_run_models_on_applies_at_the_next_load(rigs: Callable[..., Rig],
                                                      caplog: pytest.LogCaptureFixture) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    rig.fake.reset_records()
    settings.set_device("cpu")
    assert (settings.vm.device, settings.vm.device_note) == ("cpu", "Much slower than the GPU")
    assert [call.args for call in rig.calls("set_preferred_device")] == [("cpu",)]
    assert rig.state.engine.resident == MTL and rig.fake.loads == []         # the resident model stays
    settings.set_device("cpu")                                                # no change: no toast
    settings.set_device("cuda")
    settings.set_device("tpu")
    assert texts(toasts) == [("CPU applies the next time a model loads", False),
                             ("GPU applies the next time a model loads", False)]
    assert (settings.vm.device, settings.vm.device_note) == ("cuda", "Falls back to CPU without CUDA")
    assert any("Refused the device 'tpu'" in r.getMessage() for r in caplog.records)


async def test_without_cuda_the_device_note_says_so(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs, "cpu")
    assert rig.state.device.detected and rig.state.device.cuda is False
    assert settings.vm.device_note == NO_CUDA_NOTE == "No CUDA GPU detected · models run on the CPU"
    settings.set_device("cpu")
    assert settings.vm.device_note == NO_CUDA_NOTE


async def test_default_model_saves_and_moves_an_idle_forge_draft(rigs: Callable[..., Rig]) -> None:
    settings, factory, rig = await controller(rigs)
    forge = factory.forge()
    settings.set_default_backend(TUR)
    assert rig.core.settings.get("core", "default_backend") == TUR and settings.vm.default_backend == TUR
    assert forge.backends == [TUR]
    settings.set_default_backend(TUR)                           # the same model again still re-picks (M L3240)
    assert forge.backends == [TUR, TUR]


async def test_default_model_leaves_a_forged_result_alone(rigs: Callable[..., Rig]) -> None:
    settings, factory, rig = await controller(rigs)
    rig.core.forge_draft.forged = "rin"                         # the Forge page shows its result row
    settings.set_default_backend(STD)
    assert rig.core.settings.get("core", "default_backend") == STD and factory.forge().backends == []


async def test_default_model_never_touches_a_running_forge(rigs: Callable[..., Rig]) -> None:
    settings, factory, rig = await controller(rigs, "forge_ready")
    hold = rig.fake.hold("load_voice")
    clip = await asyncio.to_thread(rig.core.clips.stage_file, rig.paths.voices / "sample.wav")
    rig.core.jobs.start_forge(ForgeRequest(voice_id="rin", clip=clip, backend=MTL, language="ja", exaggeration=0.5,
                                           consent=True))
    await entered(hold)
    assert rig.state.job.kind == "forge"
    settings.set_default_backend(TUR)
    assert rig.core.settings.get("core", "default_backend") == TUR and factory.forge().backends == []
    hold.release()
    await rig.settle()


async def test_default_model_offers_the_enabled_backends_only(rigs: Callable[..., Rig],
                                                              caplog: pytest.LogCaptureFixture) -> None:
    settings, factory, rig = await controller(rigs, enabled=(MTL, STD))
    assert settings.vm.backend_options == ((MTL, "Multilingual"), (STD, "Standard"))
    settings.set_default_backend(TUR)
    assert rig.core.settings.get("core", "default_backend") == MTL and factory.forge().backends == []
    assert any("Refused the default model 'chatterbox-turbo'" in r.getMessage() for r in caplog.records)


async def test_a_stored_default_that_is_not_enabled_shows_the_first_enabled(rigs: Callable[..., Rig]) -> None:
    settings, _, _ = await controller(rigs, enabled=(MTL, STD), settings={"default_backend": TUR})
    assert settings.vm.default_backend == MTL


async def test_offline_mode_applies_after_a_restart(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.set_offline(False)
    settings.set_offline(False)                                  # unchanged: no second toast
    settings.set_offline(True)
    assert texts(toasts) == [("Offline Mode off · applies after a restart", False),
                             ("Offline Mode on · applies after a restart", False)]
    assert rig.core.settings.get("core", "offline") is True and settings.vm.offline is True


# ---------- Effects ----------

async def test_every_effects_row_reaches_the_store_live(rigs: Callable[..., Rig]) -> None:
    settings, factory, rig = await controller(rigs)
    status = factory.status().vm
    store = rig.core.settings
    changed: list[tuple[str, str]] = []
    store.changed.subscribe(lambda section, key: changed.append((section, key)))
    settings.set_pref("fx_on", False)
    settings.set_pref("fx_speed", 150)
    settings.set_pref("work_glow", False)
    settings.set_pref("saber_art", "hide")
    settings.set_reduce_motion("always")
    assert changed == [(THEME, "fx_on"), (THEME, "fx_speed"), (THEME, "work_glow"), (THEME, "saber_art"),
                       ("core", "reduce_motion")]
    assert (store.get(THEME, "fx_on"), store.get(THEME, "fx_speed"), store.get(THEME, "work_glow"),
            store.get(THEME, "saber_art"), store.get("core", "reduce_motion")) == (False, 150, False, "hide", "always")
    vm = settings.vm
    assert (vm.fx_on, vm.fx_speed, vm.fx_speed_text, vm.work_glow, vm.saber_art, vm.reduce_motion) == (
        False, 150, "150%", False, "hide", "always")
    assert (status.reduce_always, status.logo_still) == (True, True)       # the shell and the logos follow


@pytest.mark.parametrize(("sent", "kept"), [(150.0, 150), (25, 25), (200, 200), (10, 25), (1000, 200), (27, 25),
                                            (33, 35), (99.9, 100)])
async def test_effect_speed_is_clamped_to_its_grid(rigs: Callable[..., Rig], sent: float, kept: int) -> None:
    settings, _, rig = await controller(rigs)
    settings.set_pref("fx_speed", sent)
    assert rig.core.settings.get(THEME, "fx_speed") == kept and settings.vm.fx_speed_text == f"{kept}%"


def test_speed_value_refuses_what_is_not_a_number() -> None:
    assert [speed_value(value) for value in (True, "150", None, float("nan"), float("inf"))] == [None] * 5
    assert speed_value(152.4) == 150 and speed_value(197.6) == 200


async def test_effect_speed_is_pushed_live_and_saved_on_release(rigs: Callable[..., Rig],
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    """§6.12: while the range moves the store's memory and `changed` follow, the file only on release."""
    settings, _, rig = await controller(rigs)
    store, path = rig.core.settings, rig.core.paths.settings
    changed: list[tuple[str, str]] = []
    store.changed.subscribe(lambda section, key: changed.append((section, key)))
    settings.preview_pref("fx_speed", 152.4)
    await tick()
    assert (store.get(THEME, "fx_speed"), settings.vm.fx_speed_text, changed) == (150, "150%", [(THEME, "fx_speed")])
    assert not path.exists()
    settings.preview_pref("fx_on", False)                               # only Effect Speed previews
    settings.preview_pref("fx_speed", "fast")
    assert len([r for r in caplog.records if r.getMessage().startswith("Refused")]) == 2
    settings.set_pref("fx_speed", 150)                                  # the release
    store.flush()
    assert json.loads(path.read_text(encoding="utf-8"))[THEME]["fx_speed"] == 150


async def test_invalid_effects_values_change_nothing(rigs: Callable[..., Rig],
                                                     caplog: pytest.LogCaptureFixture) -> None:
    settings, _, rig = await controller(rigs)
    settings.set_pref("fx_on", "yes")
    settings.set_pref("saber_art", "forge")
    settings.set_pref("fx_speed", "fast")
    settings.set_pref("theme", "dark")
    settings.set_reduce_motion("on")
    store = rig.core.settings
    assert (store.get(THEME, "fx_on"), store.get(THEME, "saber_art"), store.get(THEME, "fx_speed"),
            store.get("core", "reduce_motion")) == (True, "show", 100, "system")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([text for text in warnings if text.startswith("Refused")]) == 5


# ---------- Keys: listening ----------

async def test_a_clicked_slot_listens_and_only_one_listens(rigs: Callable[..., Rig]) -> None:
    settings, _, _ = await controller(rigs)
    settings.listen("gen", "key")
    gen = rows(settings)["gen"]
    assert (gen.key_text, gen.key_listening, gen.alt_listening) == (LISTENING, True, False)
    assert gen.key_text == "Press a Key…" and gen.key_aria == "Generate key: Ctrl+Enter"
    settings.listen("search", "alt")
    found = rows(settings)
    assert not found["gen"].key_listening and found["gen"].key_text == "Ctrl+Enter"
    assert (found["search"].alt_text, found["search"].alt_listening) == ("Press a Key…", True)
    assert sum(row.key_listening + row.alt_listening for row in settings.vm.key_rows) == 1


async def test_listening_needs_a_known_command_and_slot(rigs: Callable[..., Rig],
                                                        caplog: pytest.LogCaptureFixture) -> None:
    settings, _, _ = await controller(rigs)
    settings.listen("theme", "key")
    settings.listen("gen", "third")
    assert not any(row.key_listening or row.alt_listening for row in settings.vm.key_rows)
    assert len([r for r in caplog.records if "Refused to listen" in r.getMessage()]) == 2


async def test_blur_navigation_and_stop_end_the_capture(rigs: Callable[..., Rig]) -> None:
    settings, factory, rig = await controller(rigs)
    settings.listen("ab", "alt")
    assert rig.core.call.hotkeys_suspended
    settings.stop_listening()                                     # blur or Tab
    assert not rig.core.call.hotkeys_suspended
    assert not rows(settings)["ab"].alt_listening and rows(settings)["ab"].alt_text == "—"
    settings.stop_listening()                                     # idempotent
    settings.listen("ab", "alt")
    factory.status().on_route("/")                                # navigation (M L1751-1753)
    assert not rows(settings)["ab"].alt_listening
    assert not rig.core.call.hotkeys_suspended
    settings.listen("ab", "alt")
    settings.close()
    assert not rig.core.call.hotkeys_suspended


# ---------- Keys: capture (captureKey, M L1984-2008) ----------

@pytest.mark.parametrize(("key", "mods", "combo"), [
    ("k", {"ctrl": True, "shift": True}, "Ctrl+Shift+K"),
    ("ArrowLeft", {"alt": True}, "Alt+Left"),
    ("F5", {}, "F5"),
    ("a", {"shift": True, "alt": True}, "Shift+Alt+A"),
    ("Enter", {"ctrl": True, "alt": True}, "Ctrl+Alt+Enter"),
    (" ", {"ctrl": True}, "Ctrl+Space"),
    ("g", {}, "G"),
])
async def test_a_combination_is_normalised_bound_and_saved(rigs: Callable[..., Rig], key: str, mods: dict[str, bool],
                                                           combo: str) -> None:
    settings, factory, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("gen", "alt")
    press(settings, "gen", "alt", key, **mods)
    gen = rows(settings)["gen"]
    assert (gen.alt_text, gen.alt_listening, gen.alt_aria) == (combo, False, f"Generate alternate key: {combo}")
    assert texts(toasts) == [(f"Bound {combo}", False)]
    assert rig.core.settings.get("core", "keys")["gen"] == ("Ctrl+Enter", combo)
    assert factory.commands().combos()[combo] == "gen"


@pytest.mark.parametrize("key", ["Shift", "Control", "Alt", "Meta"])
async def test_a_modifier_on_its_own_keeps_listening(rigs: Callable[..., Rig], key: str) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("next", "key")
    press(settings, "next", "key", key, ctrl=key == "Control", shift=key == "Shift", alt=key == "Alt")
    assert rows(settings)["next"].key_listening and toasts == []
    assert rig.core.settings.get("core", "keys")["next"] == ("Alt+Down", "")


async def test_escape_cancels_and_tab_or_composition_change_nothing(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("save", "key")
    press(settings, "save", "key", "Tab")                          # Tab passes; the blur stops listening
    press(settings, "save", "key", "k", ctrl=True, composing=True)  # an IME is composing
    press(settings, "save", "key", "Process")
    assert rows(settings)["save"].key_listening
    press(settings, "save", "key", "Escape")
    save = rows(settings)["save"]
    assert (save.key_text, save.key_listening) == ("Ctrl+S", False) and toasts == []
    assert rig.core.settings.get("core", "keys")["save"] == ("Ctrl+S", "")


@pytest.mark.parametrize(("key", "mods"), [("Backspace", {}), ("Delete", {}), ("Backspace", {"ctrl": True})])
async def test_backspace_or_delete_clears_the_slot(rigs: Callable[..., Rig], key: str, mods: dict[str, bool]) -> None:
    settings, factory, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("search", "key")
    press(settings, "search", "key", key, **mods)
    search = rows(settings)["search"]
    assert (search.key_text, search.key_listening, search.key_aria) == ("—", False, "Search key: none")
    assert texts(toasts) == [("Cleared the binding", False)]
    assert rig.core.settings.get("core", "keys")["search"] == ("", "")
    assert "Ctrl+K" not in factory.commands().combos() and factory.status().vm.search_badge == ""


async def test_a_combination_used_elsewhere_is_refused(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("gen3", "alt")
    press(settings, "gen3", "alt", "k", ctrl=True)
    assert (rows(settings)["gen3"].alt_text, rows(settings)["gen3"].alt_listening) == ("—", False)
    settings.listen("gen", "alt")                                  # the command's own other slot counts too
    press(settings, "gen", "alt", "Enter", ctrl=True)
    assert texts(toasts) == [("Ctrl+K is already used by Search", True),
                             ("Ctrl+Enter is already used by Generate", True)]
    assert rig.core.settings.get("core", "keys") == dict(DEFAULT_KEYS)


async def test_the_slot_s_own_combination_is_no_clash(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("gen", "key")
    press(settings, "gen", "key", "Enter", ctrl=True)
    assert texts(toasts) == [("Bound Ctrl+Enter", False)] and rows(settings)["gen"].key_text == "Ctrl+Enter"


@pytest.mark.parametrize(("key", "mods", "text"), [
    ("F4", {"alt": True}, "Alt+F4 is reserved by Windows"),
    ("=", {"ctrl": True}, "Ctrl+= is the window's zoom"),
    ("-", {"ctrl": True}, "Ctrl+- is the window's zoom"),
    ("+", {"ctrl": True, "shift": True}, "Ctrl+Shift++ is the window's zoom"),
    ("0", {"ctrl": True}, "Ctrl+0 is the window's zoom"),
])
async def test_reserved_combinations_are_refused(rigs: Callable[..., Rig], key: str, mods: dict[str, bool],
                                                 text: str) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    settings.listen("ab", "alt")
    press(settings, "ab", "alt", key, **mods)
    assert texts(toasts) == [(text, True)]
    assert (rows(settings)["ab"].alt_text, rows(settings)["ab"].alt_listening) == ("—", False)
    assert rig.core.settings.get("core", "keys")["ab"] == ("Ctrl+B", "")


async def test_a_keydown_on_a_slot_that_is_not_listening_is_ignored(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    toasts = watch_toasts(rig)
    press(settings, "gen", "key", "g", ctrl=True)                  # nothing listens
    settings.listen("gen", "alt")
    press(settings, "gen", "key", "g", ctrl=True)                  # another slot listens
    assert toasts == [] and rows(settings)["gen"].alt_listening
    assert rig.core.settings.get("core", "keys")["gen"] == ("Ctrl+Enter", "")


async def test_a_binding_from_another_page_load_shows_live(rigs: Callable[..., Rig]) -> None:
    settings, _, rig = await controller(rigs)
    other, _ = factory_for(rig)
    other.commands().bind("play", "alt", "P")
    assert rows(settings)["play"].alt_text == "P"


# ---------- About (§7.3) ----------

async def test_about_shows_the_detected_gpu_and_the_locations(rigs: Callable[..., Rig]) -> None:
    settings, _, _ = await controller(rigs)
    assert settings.vm.about == vms.AboutVM(
        engine=engine_text(), gpu="RTX 4070 Ti · 12.0 GB · detected", gpu_tip=GPU_TIP, models_in_vram="One at a time",
        registry="cache/registry.json", registry_tip=ABOUT_TIPS["registry"], cache="cache/‹model›/‹voice›.pt",
        cache_tip=ABOUT_TIPS["cache"], clips="voices/reference", clips_tip=ABOUT_TIPS["clips"],
        takes="outputs/takes")
    assert settings.vm.about.engine.startswith("chatterbox-tts ")


async def test_about_says_detecting_until_the_first_reading(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    await rig.start(connect=False)
    settings = factory_for(rig)[0].settings()
    assert (settings.vm.about.gpu, settings.vm.about.gpu_tip) == ("Detecting…", "")
    rig.core.client_connected()                                    # the first client: the first reading
    await rig.settle()
    await tick()
    assert (settings.vm.about.gpu, settings.vm.about.gpu_tip) == ("RTX 4070 Ti · 12.0 GB · detected", GPU_TIP)


async def test_about_without_cuda(rigs: Callable[..., Rig]) -> None:
    settings, _, _ = await controller(rigs, "cpu")
    assert (settings.vm.about.gpu, settings.vm.about.gpu_tip) == ("No CUDA GPU · models run on the CPU", "")


async def test_about_when_detection_failed(rigs: Callable[..., Rig]) -> None:
    rig = rigs("speak")
    rig.fake.fail_next("device_info", RuntimeError("CUDA driver version is insufficient"))
    await rig.start()
    await tick()
    settings = factory_for(rig)[0].settings()
    error = rig.state.device.error
    assert "CUDA driver version is insufficient" in error
    assert (settings.vm.about.gpu, settings.vm.about.gpu_tip) == (f"Not detected · {error}", "")
    assert settings.vm.device_note == "Falls back to CPU without CUDA"


# ---------- persistence (§7, W12) ----------

async def test_every_setting_is_saved_and_a_second_core_reads_it_back(tmp_path: Path) -> None:
    outside = tmp_path / "takes elsewhere"
    root = tmp_path / "pc"
    first = make_rig(root, "speak")
    try:
        await first.start()
        factory, _ = factory_for(first, FakeKit(folder_result=outside))
        factory.provide("forge", ForgeStub())
        settings = factory.settings()
        await settings.browse_output_folder()
        settings.set_autoplay(False)
        settings.set_device("cpu")
        settings.set_default_backend(TUR)
        settings.set_offline(False)
        settings.set_reduce_motion("always")
        for key, value in (("fx_on", False), ("fx_speed", 150), ("work_glow", False), ("saber_art", "hide")):
            settings.set_pref(key, value)
        settings.listen("gen", "alt")
        press(settings, "gen", "alt", "g", ctrl=True)
        settings.listen("search", "key")
        press(settings, "search", "key", "Backspace")
        first.core.stop()                                           # on_shutdown: the store flushes
    finally:
        await shut(first)
    data = json.loads(first.paths.settings.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert {key: data["core"][key] for key in ("takes_dir", "autoplay", "device", "default_backend", "offline",
                                               "reduce_motion")} == {
        "takes_dir": str(outside), "autoplay": False, "device": "cpu", "default_backend": TUR, "offline": False,
        "reduce_motion": "always"}
    assert data["core"]["keys"]["gen"] == ["Ctrl+Enter", "Ctrl+G"] and data["core"]["keys"]["search"] == ["", ""]
    assert data[THEME] == {"fx_on": False, "fx_speed": 150, "work_glow": False, "saber_art": "hide"}
    assert SettingsStore.read_early(first.paths.settings)["core"]["offline"] is False   # what gui.py reads first

    second = make_rig(root, "speak")
    try:
        await second.start()
        vm = factory_for(second)[0].settings().vm
        assert (vm.out_dir_text, vm.autoplay, vm.device, vm.default_backend, vm.offline) == (
            str(outside), False, "cpu", TUR, False)
        assert (vm.fx_on, vm.fx_speed, vm.work_glow, vm.reduce_motion, vm.saber_art) == (
            False, 150, False, "always", "hide")
        found = {row.id: row for row in vm.key_rows}
        assert (found["gen"].alt_text, found["search"].key_text) == ("Ctrl+G", "—")
        assert vm.about.takes == str(outside)
        assert [call.args for call in second.calls("set_preferred_device")][:1] == [("cpu",)]
    finally:
        await shut(second)


async def test_close_drops_the_subscriptions(rigs: Callable[..., Rig]) -> None:
    rig = await started(rigs)
    events, store = rig.core.events, rig.core.settings
    before = len(events.changed.callbacks), len(store.changed.callbacks)
    factory, _ = factory_for(rig)
    factory.settings()
    assert (len(events.changed.callbacks), len(store.changed.callbacks)) == (before[0] + 1, before[1] + 1)
    factory.close()
    assert (len(events.changed.callbacks), len(store.changed.callbacks)) == before
