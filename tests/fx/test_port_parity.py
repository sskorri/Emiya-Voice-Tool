"""The forge field port and the wave logic against the Emiya Voice mockup, run in node (§8.6).

port_parity.mjs replays the mockup's canvas engine (mockup_engine_ref.js) next to ForgeEngine with the same
random numbers and compares every canvas call. The port draws with the --emiya-fx-* colours it parses from
static\\emiya.css, so a match in place also proves that the theme stylesheet carries the mockup's colours.
wave_logic.mjs checks the take wave's and the bars' geometry, heights and classes against the mockup.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.fx import NODE, THEME, run_node

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

# The nine forge-field tokens with the colours the mockup engine hard-codes (mockup_engine_ref.js: fxEmber's
# palette, fxDraw's strokes, glows and dots), declared as the theme stylesheet must declare them:
# port_parity.mjs resolves var() only between --emiya-* tokens.
FX_TOKENS_CSS = """body.theme-emiya {
  --emiya-gold: #FCDB7F;
  --emiya-fx-ember-1: #9E2028;
  --emiya-fx-ember-2: #C43E2A;
  --emiya-fx-ember-3: #DE7030;
  --emiya-fx-ember-hot: var(--emiya-gold);
  --emiya-fx-trace-shadow: #961E22;
  --emiya-fx-trace: #AC282C;
  --emiya-fx-trace-blade: #C84A32;
  --emiya-fx-glow: #F4963C;
  --emiya-fx-spark: var(--emiya-gold);
}
"""
STAGED = ("fx/emiya_forge_field.js", "tests/port_parity.mjs", "tests/mockup_engine_ref.js")


def _assert_parity(report: dict[str, Any]) -> None:
    assert report["exit"] == 0 and report["ok"] is True, report
    assert report["mismatches"] == 0 and report["first"] is None
    assert report["seeds"] == 4 and report["calls"] > 1_000_000
    coverage = report["coverage"]
    assert min(coverage["blade_strokes"], coverage["trace_strokes"], coverage["gradients"],
               coverage["still_frame_calls"]) > 0, coverage


def test_forge_engine_makes_the_mockup_engines_canvas_calls(tmp_path: Path) -> None:
    """ForgeEngine, as shipped, against the mockup engine with the mockup's colours: 0 mismatches."""
    for rel in STAGED:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(THEME / rel, target)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "emiya.css").write_text(FX_TOKENS_CSS, encoding="utf-8")
    _assert_parity(run_node(tmp_path / "tests" / "port_parity.mjs"))


def test_theme_stylesheet_carries_the_mockup_colours() -> None:
    """port_parity.mjs in place: static\\emiya.css itself declares the nine --emiya-fx-* tokens."""
    css = (THEME / "static" / "emiya.css").read_text(encoding="utf-8")
    if "--emiya-fx-" not in css and not (THEME / "shell.py").exists():
        pytest.skip("static\\emiya.css is still T0's stub; T1 adds the --emiya-fx-* tokens with shell.py")
    _assert_parity(run_node(THEME / "tests" / "port_parity.mjs"))


def test_wave_logic_matches_the_mockup() -> None:
    """circuit, pathD and along in both components, bar heights, zone classes, time text, head glow geometry."""
    report = run_node(THEME / "tests" / "wave_logic.mjs")
    assert report["exit"] == 0 and report["ok"] is True, report["failures"]
    assert report["checks"] >= 30 and report["failures"] == []
