"""field_state() against the §6.5 table, the fixed anchors, and the props the FX elements push (§8.6).

Everything here is pure Python: no NiceGUI client and no element is created (an element outside a page
would switch NiceGUI into script mode for the rest of the test run).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from ui.contract.manifest import DEFAULT_FPS_CAP
from ui.contract.records import ClipMark
from ui.contract.routes import ROUTES
from ui.contract.state import EngineState, JobState
from ui.contract.viewmodels import WaveVM
from ui.themes.emiya.fx import bars, forge_field, take_wave
from ui.themes.emiya.fx.forge_field import ANCHORS, BLADE_ANCHOR, field_state, same_state

MTL, TUR = "chatterbox-multilingual", "chatterbox-turbo"
PREFS = {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"}
NO_GLOW = {**PREFS, "work_glow": False}
FX = Path(forge_field.__file__).resolve().parent


def state(resident: str = "", kind: str = "", take_no: int = 0, prefs: dict[str, Any] = PREFS,
          reduce_motion: str = "system", visible: bool = True) -> dict[str, Any]:
    return field_state(EngineState(resident=resident), JobState(kind=kind, take_no=take_no), prefs,
                       reduce_motion, visible)


# (case, resident, job kind, take_no, prefs) -> (heat, focus), in the order of the §6.5 table.
TABLE = [
    ("switch", MTL, "switch", 0, PREFS, "working", "switch"),
    ("switch from nothing", "", "switch", 0, PREFS, "working", "switch"),
    ("generate a take", MTL, "generate", 8, PREFS, "working", "gen"),
    ("a Forge test line", TUR, "generate", 0, PREFS, "working", "forge"),
    ("forge", TUR, "forge", 0, PREFS, "working", "forge"),
    ("cold: nothing resident", "", "", 0, PREFS, "cold", None),
    ("idle", MTL, "", 0, PREFS, "idle", None),
    ("an unload job", MTL, "unload", 0, PREFS, "idle", None),
    ("an unload job, model gone", "", "unload", 0, PREFS, "cold", None),
    ("Work Glow off: switch", MTL, "switch", 0, NO_GLOW, "idle", None),
    ("Work Glow off: switch from nothing", "", "switch", 0, NO_GLOW, "idle", None),
    ("Work Glow off: generate", MTL, "generate", 8, NO_GLOW, "idle", None),
    ("Work Glow off: test line", TUR, "generate", 0, NO_GLOW, "idle", None),
    ("Work Glow off: forge", TUR, "forge", 0, NO_GLOW, "idle", None),
    ("Work Glow off: cold", "", "", 0, NO_GLOW, "cold", None),
]


@pytest.mark.parametrize(("case", "resident", "kind", "take_no", "prefs", "heat", "focus"), TABLE,
                         ids=[row[0] for row in TABLE])
def test_heat_and_focus_follow_the_table(case: str, resident: str, kind: str, take_no: int,
                                         prefs: dict[str, Any], heat: str, focus: str | None) -> None:
    result = state(resident, kind, take_no, prefs)
    assert (result["heat"], result["focus"]) == (heat, focus), case
    assert result["glow"] is prefs["work_glow"]


def test_the_state_holds_exactly_the_documented_keys() -> None:
    assert state(MTL) == {"heat": "idle", "focus": None, "glow": True, "speed": 1.0, "reduce": "system",
                          "fps_cap": DEFAULT_FPS_CAP, "paused": False}


def test_a_minimized_window_pauses_the_field() -> None:
    assert state(MTL, visible=False)["paused"] is True
    assert state(MTL, "generate", 8, visible=False)["heat"] == "working"


def test_reduce_motion_always_is_true_and_follow_windows_is_system() -> None:
    assert state(MTL, reduce_motion="always")["reduce"] is True
    assert state(MTL, reduce_motion="system")["reduce"] == "system"


@pytest.mark.parametrize(("fx_speed", "speed"), [(100, 1.0), (25, 0.25), (150, 1.5), (200, 2.0), (None, 1.0),
                                                  (True, 1.0), ("fast", 1.0), (0, 1.0)])
def test_effect_speed_is_a_factor(fx_speed: Any, speed: float) -> None:
    prefs = {k: v for k, v in PREFS.items() if k != "fx_speed"} if fx_speed is None else {**PREFS, "fx_speed": fx_speed}
    assert state(MTL, prefs=prefs)["speed"] == speed


def test_fps_cap_comes_from_the_manifest() -> None:
    engine, job = EngineState(resident=MTL), JobState()
    assert field_state(engine, job, PREFS, "system", True)["fps_cap"] == 60
    assert field_state(engine, job, PREFS, "system", True, fps_cap=0)["fps_cap"] == 0


def test_theme_prefs_objects_work_like_mappings() -> None:
    class Prefs:
        def get(self, key: str) -> bool | int | str:
            return {"work_glow": False, "fx_speed": 50}.get(key, "")

    result = field_state(EngineState(resident=MTL), JobState(kind="switch"), Prefs(), "system", True)
    assert (result["heat"], result["focus"], result["glow"], result["speed"]) == ("idle", None, False, 0.5)


def test_a_takes_focus_is_the_fixed_gen_anchor_on_every_route() -> None:
    """The state never depends on the route, and "gen" is a fixed shell anchor (the mockup's point)."""
    assert list(inspect.signature(field_state).parameters) == [
        "engine", "job", "prefs", "reduce_motion", "window_visible", "fps_cap"]
    for _route in ROUTES:
        assert state(MTL, "generate", 7)["focus"] == "gen"
        assert state(TUR, "generate", 0)["focus"] == "forge"
    assert ANCHORS["gen"] == (900, 470)


def test_anchors_are_the_mockups_fixed_points() -> None:
    assert dict(ANCHORS) == {"switch": (380, 620), "gen": (900, 470), "forge": (990, 560), "forge-blade": (990, 660)}
    assert BLADE_ANCHOR == "forge-blade"
    assert [forge_field.anchor_class(name) for name in ANCHORS] == [
        "emiya-anchor-switch", "emiya-anchor-gen", "emiya-anchor-forge", "emiya-anchor-forge-blade"]
    assert [forge_field.anchor_marker(name) for name in ANCHORS] == [
        "fx.anchor.switch", "fx.anchor.gen", "fx.anchor.forge", "fx.anchor.forge_blade"]
    focus = {state(MTL, "switch")["focus"], state(MTL, "generate", 3)["focus"], state(MTL, "forge")["focus"]}
    assert focus <= set(ANCHORS)


def test_same_state_ignores_the_sequence_number() -> None:
    a = state(MTL)
    assert same_state({**a, "seq": 1}, {**a, "seq": 9})
    assert not same_state({**a, "seq": 1}, {**a, "heat": "cold", "seq": 1})


# ---- the take wave's and the bars' props -----------------------------------------------------------

def url(take_id: int) -> str:
    return f"/media/take/{take_id}"


def test_wave_props_without_a_take() -> None:
    assert take_wave.wave_props(WaveVM(), url) == {
        "take": None, "generating": {"active": False, "fill": None}, "new-seq": 0, "spark-seq": 0}


def test_wave_props_for_a_take() -> None:
    wave = WaveVM(take_id=7, bars=(0.123456, 1.0, 0.0), length_s=4.6, new_seq=2, spark_seq=1)
    assert take_wave.wave_props(wave, url) == {
        "take": {"id": 7, "bars": [0.123, 1.0, 0.0], "length_s": 4.6, "src": "/media/take/7"},
        "generating": {"active": False, "fill": None}, "new-seq": 2, "spark-seq": 1}


def test_wave_props_while_generating() -> None:
    props = take_wave.wave_props(WaveVM(take_id=7, generating=True, fill=0.42), url)
    assert props["generating"] == {"active": True, "fill": 0.42}
    assert take_wave.wave_props(WaveVM(generating=True), url)["generating"] == {"active": True, "fill": None}


def test_reduce_prop() -> None:
    assert take_wave.reduce_prop(True) is True
    assert take_wave.reduce_prop(False) == "system"


def test_bars_prop_keeps_zones_and_rounds_values() -> None:
    assert bars.bars_prop(((0.5, "timbre"), (0.25, "selection"), (0.0, "outside"))) == [
        [0.5, "timbre"], [0.25, "selection"], [0.0, "outside"]]
    assert bars.bars_prop((0.1234567, 1)) == [0.123, 1.0]
    assert bars.bars_prop(()) == []


def test_marks_prop() -> None:
    marks = (ClipMark("Prompt 6 s", 44.71234), ClipMark("Timbre 10 s", 74.5))
    assert bars.marks_prop(marks) == [{"label": "Prompt 6 s", "pct": 44.712}, {"label": "Timbre 10 s", "pct": 74.5}]


def test_optional_progress_stays_none() -> None:
    assert bars.optional_float(None) is None
    assert bars.optional_float(1) == 1.0


def test_an_unknown_bar_kind_is_refused_before_any_element_exists() -> None:
    with pytest.raises(ValueError, match="kind"):
        bars.EmiyaBars("waves")  # type: ignore[arg-type]


@pytest.mark.parametrize(("element", "name"), [(forge_field.EmiyaForgeField, "emiya_forge_field"),
                                               (take_wave.EmiyaTakeWave, "emiya_take_wave"),
                                               (bars.EmiyaBars, "emiya_bars")])
def test_components_register_from_their_own_files(element: type, name: str) -> None:
    component = element.component
    assert component is not None and component.name == name
    assert Path(component.path).resolve() == FX / f"{name}.js"
