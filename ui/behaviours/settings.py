"""SettingsController (§5.11, §6.12, §7): the Settings page's General, Effects and Keys tabs and its About card.

Every value lives in the settings store (ui_settings.json, §7.1): core.* for the app, the active theme's own
section (theme.<slug>.*, the store's theme_section) for the Effects; a theme that declares no such pref reads
its neutral default here. An intent validates, writes the store (memory at once, one coalesced atomic write
soon) and toasts where the mockup does. Everything else follows the store's `changed` event: the shell
re-applies the Effects to the forge field live, the core pushes Run Models On to the engine for its next load
and refreshes the About card's Takes row, the StatusController re-reads Reduce Motion and the search badge.

Key capture follows captureKey (M L1984-2008): one slot of this client listens at a time; Tab passes (the
blur stops listening), Escape cancels, a modifier on its own keeps listening, Backspace or Delete clears the
slot, anything else becomes a combo that the CommandRegistry checks for clashes and reserved combinations
before it saves it. Nothing is captured while an IME composes.
"""
from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.plans import ActionState
from ui.contract.viewmodels import AboutVM, KeyRowVM, SettingsVM
from ui.contract.viewmodels import SettingsController as SettingsControllerProtocol
from ui.core import catalog
from ui.core.facade import describe_error
from ui.core.services.device import GPU_TIP, gpu_about
from ui.core.services.settings import ABOUT_TIPS, CORE, folder_path, folder_value

from .commands import COMMANDS, label_of
from .confirm import Subscriptions

if TYPE_CHECKING:
    from . import BehaviourFactory

__all__ = [
    "BROWSE_LABEL",
    "DEVICE_NOTES",
    "LISTENING",
    "NEEDS_WINDOW",
    "NO_CUDA_NOTE",
    "NO_KEY",
    "SPEED_MAX",
    "SPEED_MIN",
    "SPEED_STEP",
    "TABS",
    "WRITE_PROBE",
    "SettingsController",
    "speed_value",
]

Tab = Literal["general", "effects", "keys", "voice_chat"]
Slot = Literal["key", "alt"]

TABS: tuple[str, ...] = ("general", "effects", "keys", "voice_chat")
SLOTS: tuple[str, ...] = ("key", "alt")
DEVICES: tuple[str, ...] = ("cuda", "cpu")
MOTION_POLICIES: tuple[str, ...] = ("system", "always")
PREFS: tuple[str, ...] = ("fx_on", "fx_speed", "work_glow", "saber_art")
SPEED_MIN, SPEED_MAX, SPEED_STEP = 25, 200, 5                                # M L1027
SPEED_DEFAULT = 100                                                          # a theme without Effect Speed

BROWSE_LABEL = "Browse…"                                                     # M L970
NEEDS_WINDOW = "Needs the app window"                                        # --browser (§6.12)
OUTPUT_TITLE = "Output Folder"
WRITE_PROBE = ".voicetool-write-test"                                        # the write probe (§5.11)
NO_PICKER = "Couldn't open the folder picker"                                # NEW: the dialog itself failed
DEVICE_NOTES: dict[str, str] = {"cuda": "Falls back to CPU without CUDA",   # M L3237
                                "cpu": "Much slower than the GPU"}
NO_CUDA_NOTE = "No CUDA GPU detected · models run on the CPU"                # NEW (§6.12)
LISTENING = "Press a Key…"                                                   # M L2829
EMPTY_KEY = "—"
NO_KEY = "none"                                                              # in the slot's aria-label (M L2839)

_CLEAR_KEYS = frozenset({"Backspace", "Delete"})                             # M L1992
_PASS_KEYS = frozenset({"", "Tab", "Process", "Dead", "Unidentified"})       # never a combo; listening goes on

log = logging.getLogger("voicetool.behaviours")


def speed_value(value: Any) -> int | None:
    """Effect Speed on its 5 % grid, clamped to 25-200 %; None for anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(SPEED_MIN, min(SPEED_MAX, int(round(value / SPEED_STEP)) * SPEED_STEP))


class SettingsController(SettingsControllerProtocol):
    """General, Effects and Keys (the header tabs) and the About card of the Settings page (§5.11)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self.vm = SettingsVM()
        self._listening: tuple[str, str] | None = None     # (command id, slot) of this client's listening slot
        self._browsing = False                             # the folder dialog or its write probe is running
        self._subs = Subscriptions()
        self._subs.add(self._core.events.changed, self.refresh)
        self._subs.add(self._core.settings.changed, self._on_setting)
        self.refresh()

    # ---------- tabs ----------

    def set_tab(self, tab: Tab) -> None:
        """A header tab; a new tab stops a key capture (M L2711)."""
        if tab not in TABS:
            log.warning("Refused the Settings tab %r", tab)
            return
        self.stop_listening()
        self.vm.tab = tab
        if tab == "voice_chat":
            self._core.scheduler.spawn(self.refresh_call_devices(), name="refresh call devices")

    async def refresh_call_devices(self) -> None:
        await self._core.call.refresh_devices()

    def select_call_device(self, device_id: str) -> None:
        device = next((item for item in self._core.call.devices if item.id == device_id), None)
        if device is not None:
            self._write(CORE, "call_device", {"id": device.id, "name": device.name})
        elif not device_id:
            self._write(CORE, "call_device", {"id": "", "name": ""})

    def set_call_preference(self, field: str, value: bool | str | int) -> None:
        if field in ("call_enabled", "call_hear", "call_overlap", "call_send", "call_delay_s"):
            self._write(CORE, field, value)

    # ---------- General ----------

    async def browse_output_folder(self) -> None:
        """Browse…: the native folder dialog, a write probe, then core.takes_dir (relative inside the data root).

        Refused in --browser mode and while the dialog is open; Cancel changes nothing.
        """
        if self._factory.browser or self._browsing:
            return
        self._set_browsing(True)
        try:
            picked = await self._pick_output_folder()
            if picked is not None:
                await self._use_output_folder(picked)
        finally:
            self._set_browsing(False)

    def set_autoplay(self, on: bool) -> None:
        """Autoplay New Takes: applies to the next take."""
        self._write(CORE, "autoplay", bool(on))

    def set_stop_drops_queue(self, on: bool) -> None:
        self._write(CORE, "stop_drops_queue", bool(on))

    def set_device(self, device: Literal["cuda", "cpu"]) -> None:
        """Run Models On: saved, and the engine uses it at its next load; the resident model stays."""
        if device not in DEVICES:
            log.warning("Refused the device %r", device)
            return
        if self._core.settings.get(CORE, "device") == device:
            return
        if self._write(CORE, "device", device):
            self._say(f"{'CPU' if device == 'cpu' else 'GPU'} applies the next time a model loads")

    def set_default_backend(self, backend: str) -> None:
        """Default Model for New Voices; while Forge is idle and shows no result its draft follows (M L3238-3241)."""
        if backend not in self._core.state.engine.enabled:
            log.warning("Refused the default model %r: it is not an enabled backend", backend)
            return
        if not self._write(CORE, "default_backend", backend):
            return
        if self._core.state.job.kind != "forge" and not self._core.forge_draft.forged:
            self._repick_forge(backend)

    def set_offline(self, on: bool) -> None:
        """Offline Mode: gui.py reads it before anything imports huggingface_hub, so it applies after a restart."""
        on = bool(on)
        if self._core.settings.get(CORE, "offline") == on:
            return
        if self._write(CORE, "offline", on):
            self._say(f"Offline Mode {'on' if on else 'off'} · applies after a restart")

    # ---------- Effects ----------

    def set_reduce_motion(self, policy: Literal["system", "always"]) -> None:
        """Reduce Motion "Follow Windows" or "Always" (a core preference every theme honours); live."""
        if policy not in MOTION_POLICIES:
            log.warning("Refused the Reduce Motion policy %r", policy)
            return
        self._write(CORE, "reduce_motion", policy)

    def set_pref(self, key: Literal["fx_on", "fx_speed", "work_glow", "saber_art"],
                 value: bool | int | str) -> None:
        """An Effects preference of the active theme (theme.<slug>.*), saved; the shell applies it live.

        Effect Speed is clamped to 25-200 on its 5 % grid; the slider sends it on release (§6.12). A pref the
        active theme does not declare is refused by the store (logged, nothing changes).
        """
        if key not in PREFS:
            log.warning("Refused the Effects preference %r", key)
            return
        if key == "fx_speed":
            speed = speed_value(value)
            if speed is None:
                log.warning("Refused the Effect Speed %r", value)
                return
            value = speed
        self._write(self._core.settings.theme_section, key, value)

    def preview_pref(self, key: Literal["fx_speed"], value: float) -> None:
        """Effect Speed while the range moves: pushed live to the field, saved only by set_pref on release."""
        speed = speed_value(value) if key == "fx_speed" else None
        if speed is None:
            log.warning("Refused the Effects preview %r = %r", key, value)
            return
        section = self._core.settings.theme_section
        try:
            self._core.settings.preview(section, key, speed)
        except (KeyError, ValueError) as exc:
            log.warning("Refused the setting %s.%s: %s", section, key, exc)

    # ---------- Keys ----------

    def listen(self, command_id: str, slot: Slot) -> None:
        """A Keys slot was clicked: it listens for the next combination (one slot at a time per client)."""
        if not label_of(command_id) or slot not in SLOTS:
            log.warning("Refused to listen on the key slot %r %r", command_id, slot)
            return
        if self._listening != (command_id, slot):
            self._listening = (command_id, slot)
            self._core.call.set_hotkey_capture(self, True)
            self._refresh_keys()

    def stop_listening(self) -> None:
        """Blur, Tab, Escape, a tab change or navigation: the slot stops listening, nothing changes."""
        if self._listening is not None:
            self._listening = None
            self._core.call.set_hotkey_capture(self, False)
            self._refresh_keys()

    def capture(self, command_id: str, slot: Slot, *, key: str, ctrl: bool, shift: bool, alt: bool,
                composing: bool = False) -> None:
        """A keydown on the listening slot, as captureKey handles it (M L1984-2008)."""
        if self._listening != (command_id, slot) or composing or key in _PASS_KEYS:
            return
        if key == "Escape":
            self.stop_listening()
            return
        commands = self._factory.commands()
        combo = "" if key in _CLEAR_KEYS else commands.normalize(key, ctrl=ctrl, shift=shift, alt=alt)
        if not combo and key not in _CLEAR_KEYS:
            return                                         # Shift, Control, Alt or Meta on its own
        try:
            result = commands.bind(command_id, slot, combo)
        finally:
            self.stop_listening()
        self._say(result.text, bad=not result.ok)

    # ---------- Confirmable ----------

    async def confirm(self, token: str) -> None:
        """Settings raises no asks: nothing to confirm."""

    def dismiss(self) -> None:
        """Settings raises no asks: nothing to dismiss."""

    def close(self) -> None:
        """Drop the subscriptions (BehaviourFactory.close)."""
        self.stop_listening()
        self._subs.close()

    # ---------- the view model ----------

    def refresh(self) -> None:
        """Recompute the whole view model from the store and the core's mirrors."""
        core, vm = self._core, self.vm
        settings = core.settings
        folder = str(self._output_folder())
        vm.out_dir_text = folder
        vm.out_tip = folder
        vm.browse = self._browse_action()
        vm.autoplay = bool(settings.get(CORE, "autoplay"))
        vm.stop_drops_queue = bool(settings.get(CORE, "stop_drops_queue"))
        device = str(settings.get(CORE, "device"))
        vm.device = device
        vm.device_note = self._device_note(device)
        enabled = core.state.engine.enabled
        vm.backend_options = tuple((backend, catalog.label(backend)) for backend in enabled)
        vm.default_backend = self._default_backend(enabled)
        vm.offline = bool(settings.get(CORE, "offline"))
        self._refresh_effects()
        self._refresh_keys()
        self._refresh_call()
        vm.about = self._about()

    def _refresh_call(self) -> None:
        vm, core = self.vm, self._core
        call = core.call
        vm.call_enabled = core.settings.get(CORE, "call_enabled")
        vm.call_hear = core.settings.get(CORE, "call_hear")
        vm.call_overlap = core.settings.get(CORE, "call_overlap")
        vm.call_send = core.settings.get(CORE, "call_send")
        vm.call_delay = str(core.settings.get(CORE, "call_delay_s"))
        vm.call_devices = tuple((device.id, device.name) for device in call.devices)
        selected = call.selected_device
        vm.call_device = selected.id if selected else ""
        saved = core.settings.get(CORE, "call_device")["name"]
        notes = []
        if saved and not selected:
            notes.append(f"Not found: {saved}. Choose an available output or reconnect it and refresh.")
        if call.default_roles:
            roles = ", ".join(f"{item.flow} {item.role}" for item in call.default_roles)
            notes.append(f"Windows defaults use the cable ({roles}). Check your normal speakers and microphone.")
        vm.call_note = " ".join(filter(None, [*notes, call.device_error, call.hotkey_error]))

    def _refresh_effects(self) -> None:
        """The Effects tab from the active theme's prefs; one the theme does not declare reads as off."""
        settings, vm = self._core.settings, self.vm
        speed = int(settings.pref("fx_speed", SPEED_DEFAULT))
        vm.fx_on = bool(settings.pref("fx_on", False))
        vm.fx_speed = speed
        vm.fx_speed_text = f"{speed}%"
        vm.work_glow = bool(settings.pref("work_glow", False))
        vm.reduce_motion = str(settings.get(CORE, "reduce_motion"))
        vm.saber_art = str(settings.pref("saber_art", "hide"))

    def _refresh_keys(self) -> None:
        bindings = self._factory.commands().bindings()
        self.vm.key_rows = tuple(self._key_row(command.id, command.label, bindings.get(command.id, ("", "")))
                                 for command in COMMANDS)
        self.vm.call_shortcuts = " · ".join(f"{label}: {', '.join(filter(None, bindings[key])) or 'Unbound'}"
                                            for key, label in (("call_send", "Send"), ("call_stop", "Stop Sending")))

    def _key_row(self, command_id: str, label: str, pair: tuple[str, str]) -> KeyRowVM:
        key, alt = pair
        key_on = self._listening == (command_id, "key")
        alt_on = self._listening == (command_id, "alt")
        return KeyRowVM(id=command_id, label=label,
                        key_text=LISTENING if key_on else key or EMPTY_KEY,
                        alt_text=LISTENING if alt_on else alt or EMPTY_KEY,
                        key_listening=key_on, alt_listening=alt_on,
                        key_aria=f"{label} key: {key or NO_KEY}",
                        alt_aria=f"{label} alternate key: {alt or NO_KEY}")

    def _browse_action(self) -> ActionState:
        if self._factory.browser:
            return ActionState(False, BROWSE_LABEL, reason=NEEDS_WINDOW, tone="plain")
        return ActionState(not self._browsing, BROWSE_LABEL, tone="plain")

    def _device_note(self, device: str) -> str:
        """The note next to Run Models On; without a CUDA GPU it says so (NEW)."""
        state = self._core.state.device
        if state.detected and state.cuda is False and not state.error:
            return NO_CUDA_NOTE
        return DEVICE_NOTES.get(device, "")

    def _default_backend(self, enabled: tuple[str, ...]) -> str:
        """The stored Default Model; one that is not enabled shows as the first enabled backend (Forge's own pick)."""
        stored = str(self._core.settings.get(CORE, "default_backend"))
        return stored if stored in enabled or not enabled else enabled[0]

    def _about(self) -> AboutVM:
        """The About card (§7.3): the core's facts, the GPU state and the mockup's tooltips."""
        about, device = self._core.state.about, self._core.state.device
        return AboutVM(engine=about.engine, gpu=about.gpu or gpu_about(device),
                       gpu_tip=GPU_TIP if device.detected and device.cuda else "",
                       models_in_vram=about.models_in_vram,
                       registry=about.registry, registry_tip=ABOUT_TIPS["registry"],
                       cache=about.cache, cache_tip=ABOUT_TIPS["cache"],
                       clips=about.clips, clips_tip=ABOUT_TIPS["clips"],
                       takes=about.takes)

    # ---------- helpers ----------

    def _output_folder(self) -> Path:
        """The Output Folder as an absolute path (the store keeps it relative inside the data root)."""
        return folder_path(str(self._core.settings.get(CORE, "takes_dir")), self._core.paths.root)

    async def _pick_output_folder(self) -> Path | None:
        start = await asyncio.to_thread(_existing_folder, self._output_folder())
        try:
            return await self._factory.kit.pick_folder(title=OUTPUT_TITLE, start_dir=start)
        except Exception as exc:                           # the dialog failed: say so, change nothing
            self._say(f"{NO_PICKER}: {describe_error(exc)}", bad=True)
            return None

    async def _use_output_folder(self, picked: Path) -> None:
        root = self._core.paths.root
        folder = folder_path(str(picked), root)
        problem = await asyncio.to_thread(_write_problem, folder)
        if problem:
            self._say(f"Can't write to {folder}: {problem}", bad=True)
            return
        if self._write(CORE, "takes_dir", folder_value(folder, root)):
            self._say(f"New takes will be saved in {folder}")

    def _set_browsing(self, on: bool) -> None:
        self._browsing = on
        self.vm.browse = self._browse_action()

    def _repick_forge(self, backend: str) -> None:
        """Forge's draft takes the new default (ForgeController owns the draft)."""
        try:
            self._factory.forge().set_backend(backend)
        except Exception as exc:                           # the setting is saved; the draft keeps its model
            log.warning("Couldn't move the Forge draft to %s: %s", backend, describe_error(exc))

    def _write(self, section: str, key: str, value: Any) -> bool:
        """Save one setting; a value the store refuses is logged and changes nothing."""
        try:
            self._core.settings.set(section, key, value)
        except (KeyError, ValueError) as exc:
            log.warning("Refused the setting %s.%s: %s", section, key, exc)
            return False
        return True

    def _say(self, text: str, *, bad: bool = False) -> None:
        self._core.events.say(text, bad=bad)

    def _on_setting(self, section: str, key: str) -> None:
        self.refresh()


def _existing_folder(folder: Path) -> Path | None:
    """folder when it is a folder now (the dialog opens there), else None (worker)."""
    try:
        return folder if folder.is_dir() else None
    except OSError:
        return None


def _write_problem(folder: Path) -> str:
    """Why new takes could not be saved in folder, "" when they can (worker).

    The folder must exist or be creatable, and take a probe file that is created and deleted again.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / WRITE_PROBE
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return exc.strerror or describe_error(exc)
    return ""
