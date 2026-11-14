"""EmiyaForgeField: the Emiya theme's animated forge background, and field_state(), the state that drives it.

The browser half lives next to this file in emiya_forge_field.js (a NiceGUI custom component): ForgeEngine is
the mockup's canvas engine, FieldRunner its frame loop, capped by a timer. Theme effects follow the
"<slug>_*.js" naming rule because NiceGUI registers components by file stem, globally.

Python owns *what* the field shows; the browser owns *how* it animates. The state is a plain dict that is
always replaced, never mutated in place:

    {'seq': 7, 'heat': 'idle' | 'working' | 'cold', 'focus': 'switch' | 'gen' | 'forge' | None,
     'glow': True, 'speed': 1.0, 'reduce': 'system' | True, 'fps_cap': 60, 'paused': False}

``focus`` names one of the fixed ANCHORS (elements with the class ``emiya-anchor-<name>``); the browser
measures it at runtime. ``seq`` lets the browser ignore re-renders that carry the same state.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from nicegui import binding, ui
from nicegui.events import GenericEventArguments

from ui.contract.manifest import DEFAULT_FPS_CAP

if TYPE_CHECKING:
    from nicegui.awaitable_response import AwaitableResponse

    from ui.contract.state import EngineState, JobState
    from ui.contract.theme import ThemePrefs

Handler = Callable[[GenericEventArguments], Any]
FieldState = dict[str, Any]

HEAT_IDLE, HEAT_WORKING, HEAT_COLD = "idle", "working", "cold"
FOCUS_SWITCH, FOCUS_GEN, FOCUS_FORGE = "switch", "gen", "forge"
BLADE_ANCHOR = "forge-blade"
ANCHOR_PREFIX = "emiya-anchor-"
# The mockup's fixed focus points in window coordinates (M L2018-2020, L1807). They are 0x0 anchors in the
# shell, present on every route, so a take's traces gather at the same point whatever page is shown.
ANCHORS: Mapping[str, tuple[int, int]] = MappingProxyType({
    FOCUS_SWITCH: (380, 620),
    FOCUS_GEN: (900, 470),
    FOCUS_FORGE: (990, 560),
    BLADE_ANCHOR: (990, 660),
})
DEFAULT_SPEED = 100                     # Effect Speed in percent ([prefs] fx_speed)


def field_state(engine: EngineState, job: JobState, prefs: ThemePrefs | Mapping[str, Any],
                reduce_motion: str, window_visible: bool, *, fps_cap: int = DEFAULT_FPS_CAP) -> FieldState:
    """The field's state for the core mirrors and the Effects settings (§6.5; the mockup's syncHeat, M L2015-2021).

    prefs: the theme's ThemePrefs, or any mapping with its keys (work_glow, fx_speed).
    reduce_motion: the core's Reduce Motion policy, "system" (follow Windows) or "always".
    window_visible: WindowState.visible; a minimized window pauses the field.
    """
    glow = bool(_pref(prefs, "work_glow", True))
    heat, focus = _heat_and_focus(engine, job, glow)
    return {
        "heat": heat,
        "focus": focus,
        "glow": glow,
        "speed": _speed(prefs),
        "reduce": True if reduce_motion == "always" else "system",
        "fps_cap": fps_cap,
        "paused": not window_visible,
    }


def same_state(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two field states carry the same content (``seq`` aside)."""
    return {k: v for k, v in a.items() if k != "seq"} == {k: v for k, v in b.items() if k != "seq"}


def _pref(prefs: ThemePrefs | Mapping[str, Any], key: str, default: Any) -> Any:
    value = prefs.get(key)
    return default if value is None else value


def _speed(prefs: ThemePrefs | Mapping[str, Any]) -> float:
    """Effect Speed / 100 (M L2060); a missing or unusable value counts as 100 %."""
    value = _pref(prefs, "fx_speed", DEFAULT_SPEED)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        value = DEFAULT_SPEED
    return value / 100


def _heat_and_focus(engine: EngineState, job: JobState, glow: bool) -> tuple[str, str | None]:
    """The table of §6.5, evaluated in its order."""
    focus = _work_focus(job) if glow else None
    if focus is not None:
        return HEAT_WORKING, focus
    if not engine.resident and job.kind != "switch":
        return HEAT_COLD, None
    return HEAT_IDLE, None


def _work_focus(job: JobState) -> str | None:
    """Where a running job's traces gather; None for no job and for an unload."""
    if job.kind == "switch":
        return FOCUS_SWITCH
    if job.kind == "generate":
        return FOCUS_GEN if job.take_no > 0 else FOCUS_FORGE        # take_no 0 is a Forge test line
    if job.kind == "forge":
        return FOCUS_FORGE
    return None


def anchor_class(name: str) -> str:
    """The class the field looks for: emiya-anchor-<name>."""
    return ANCHOR_PREFIX + name


def anchor_marker(name: str) -> str:
    """fx.anchor.switch, fx.anchor.gen, fx.anchor.forge, fx.anchor.forge_blade (§6.2.5)."""
    return "fx.anchor." + name.replace("-", "_")


def add_anchors() -> list[ui.element]:
    """The four fixed 0x0 anchors (§6.2.5), positioned in the current container in window coordinates.

    Build them in the shell's root, outside ui.sub_pages: navigation must never remove an anchor.
    """
    return [
        ui.element("div")
        .classes(f"emiya-anchor {anchor_class(name)}")
        .style(f"position: absolute; left: {x}px; top: {y}px; width: 0; height: 0; pointer-events: none")
        .props('aria-hidden="true"')
        .mark(anchor_marker(name))
        for name, (x, y) in ANCHORS.items()
    ]


class EmiyaForgeField(ui.element, component="emiya_forge_field.js"):
    """The Emiya "Trace On" canvas, drawn behind the shell (outside ui.sub_pages, so it never remounts).

    Events: ``ready`` once after mount (the colour tokens, the missing ones and the measured layout); with
    ``telemetry=True`` (gui.py --fx-telemetry) also ``telemetry`` every second, ``ack`` for every state
    received and ``jserror`` for window errors. Pass every handler here: a listener added later makes NiceGUI
    re-create (remount) the element in the browser.
    """

    # on_change only fires when a value *changes*; the first assignment in __init__ does not fire it.
    state = binding.BindableProperty(on_change=lambda sender, value: sender._push_state(value))

    def __init__(self, state: Mapping[str, Any], *, field_selector: str = ".emiya-field",
                 uid: str = "forge-field", telemetry: bool = False,
                 on_ready: Handler | None = None, on_telemetry: Handler | None = None,
                 on_ack: Handler | None = None, on_error: Handler | None = None) -> None:
        """
        :param state: initial state (field_state()); copied, never mutated
        :param field_selector: CSS selector of the element that bounds where traces may spawn
        :param uid: name of this instance in the browser's registry and in telemetry
        :param telemetry: emit telemetry, ack and jserror events (development only)
        """
        super().__init__()
        self._seq = itertools.count(1)
        self._props["options"] = {"uid": uid, "field": field_selector, "telemetry": telemetry}
        first = self._stamped(state)
        self.state = first              # BindableProperty: first assignment, on_change does not run ...
        self._props["state"] = first    # ... so the initial prop is written here
        for name, handler in (("ready", on_ready), ("telemetry", on_telemetry), ("ack", on_ack),
                              ("jserror", on_error)):
            if handler is not None:
                self.on(name, handler)

    def push(self, state: Mapping[str, Any]) -> bool:
        """Send `state` as the whole new state when its content changed; return whether it was sent."""
        if same_state(state, self.state):
            return False
        self.state = self._stamped(state)
        return True

    def set_state(self, **changes: Any) -> FieldState:
        """Push a copy of the state with `changes` applied (only when that changes it); return the state."""
        self.push({**self.state, **changes})
        return self.state

    def forge_blade(self, anchor: str = BLADE_ANCHOR) -> AwaitableResponse:
        """Grow the mockup's forced "blade" trace from the named anchor (fxForgeBlade)."""
        return self.run_method("forgeBlade", anchor)

    def snapshot(self) -> AwaitableResponse:
        """Ask the browser for the runner's counters (awaitable)."""
        return self.run_method("snapshot")

    def _stamped(self, state: Mapping[str, Any]) -> FieldState:
        return {**state, "seq": next(self._seq)}

    def _push_state(self, value: FieldState) -> None:
        self._props["state"] = value    # Props is observable: the assignment queues the element update
