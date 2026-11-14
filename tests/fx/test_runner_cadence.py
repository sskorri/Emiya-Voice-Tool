"""The capped frame loops of the forge field and the take wave on fake clocks (§8.6), run in node.

runner_cadence.mjs drives the real FieldRunner, FrameClock, WavePlayhead and TraceDriver in a fake browser
whose vsync runs at 60, 144 or 280 Hz; these tests read its report against the §8.6 cadence table.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.fx import NODE, THEME, run_node

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

RATES = (60, 144, 280)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return run_node(THEME / "tests" / "runner_cadence.mjs")


def test_every_check_of_the_script_passes(report: dict[str, Any]) -> None:
    assert report["exit"] == 0 and report["ok"] is True, report["failures"]


@pytest.mark.parametrize("hz", RATES)
@pytest.mark.parametrize("state", ["cap60", "working"])
def test_capped_field_wakes_about_60_times_a_second(report: dict[str, Any], state: str, hz: int) -> None:
    """Cap 60: at most 62 animation-frame callbacks and 59-61 draws a second, whatever the display rate."""
    row = report["field"][state][str(hz)]
    assert row["raf_per_s"] <= 62, row
    assert 59 <= row["draws_per_s"] <= 61, row


def test_capped_field_does_not_draw_on_every_vsync(report: dict[str, Any]) -> None:
    """The spike's capped loop still woke at 280 Hz; the timer-gated one does not."""
    assert report["field"]["cap60"]["280"]["raf_per_s"] < 70


@pytest.mark.parametrize("hz", RATES)
def test_uncapped_field_draws_on_every_vsync(report: dict[str, Any], hz: int) -> None:
    row = report["field"]["cap0"][str(hz)]
    assert abs(row["draws_per_s"] - hz) <= 1, row


def test_reduce_motion_leaves_no_callbacks_after_the_still_frame(report: dict[str, Any]) -> None:
    reduce = report["field"]["reduce"]
    for case in ("at_mount", "while_running"):
        row = reduce[case]
        assert row["still_draws"] == 1 and row["still"] is True, row
        assert row["raf_per_s"] == 0 and row["draws_per_s"] == 0, row
        assert row["pending"]["timeouts"] == 0 and row["pending"]["frames"] == 0, row
    assert reduce["system"]["reduced_raf_per_s"] == 0
    assert 59 <= reduce["system"]["resumed_draws_per_s"] <= 61


def test_paused_field_draws_nothing(report: dict[str, Any]) -> None:
    paused = report["field"]["paused"]
    assert paused["at_mount"] == {"raf_per_s": 0, "draws_per_s": 0, "still": False}
    assert paused["paused_again"]["raf_per_s"] == 0 and paused["paused_again"]["draws_per_s"] == 0
    assert paused["reduce"] == {"still_draws_while_paused": 0, "still_draws_after": 1}
    assert 59 <= paused["resumed_draws_per_s"] <= 61


def test_hidden_document_stops_the_field(report: dict[str, Any]) -> None:
    visibility = report["field"]["visibility"]
    assert visibility["hidden_raf_per_s"] == 0
    assert 59 <= visibility["visible_draws_per_s"] <= 61


def test_stop_leaves_no_pending_timer(report: dict[str, Any]) -> None:
    stop = report["field"]["stop"]
    assert stop["after_stop"]["timeouts"] == 0 and stop["after_stop"]["frames"] == 0
    assert stop["raf_per_s_after_stop"] == 0
    assert stop["after_dispose"] == {"timeouts": 0, "intervals": 0, "frames": 0}
    assert stop["listeners_after_dispose"] == {"document": 0, "window": 0, "queries": 0}


def test_telemetry_only_with_the_option(report: dict[str, Any]) -> None:
    telemetry = report["field"]["telemetry"]
    assert telemetry["off"] == {"emits": ["ready"], "window_listeners": 0, "gaps_kept": 0}
    assert 5 <= telemetry["on"]["telemetry"] <= 6 and telemetry["on"]["acks"] == 1


@pytest.mark.parametrize("hz", RATES)
def test_take_wave_loop_is_capped_like_the_field(report: dict[str, Any], hz: int) -> None:
    capped = report["clock"]["fps60"][str(hz)]
    assert capped["raf_per_s"] <= 62 and 59 <= capped["ticks_per_s"] <= 61, capped
    assert abs(report["clock"]["fps0"][str(hz)]["ticks_per_s"] - hz) <= 1


@pytest.mark.parametrize("hz", RATES)
def test_head_pulse_keeps_its_css_timeline_at_the_cap_and_stops_when_hidden(report: dict[str, Any], hz: int) -> None:
    pulse = report["pulse"][str(hz)]
    assert pulse["steps_per_s"] == 60 and pulse["visible_raf"] == 0
    assert pulse["keyframe_error"] < 1e-6 and pulse["initial_phase"] == 137 and pulse["same"]
    assert pulse["held"] and pulse["hidden_raf"] == 0
    assert pulse["paused"] and pulse["off_plays"] and pulse["reduced"]
    assert 999 <= pulse["resumed_ms"] <= 1001
    assert pulse["canceled"] and pulse["listeners"] == 0
    assert pulse["pending"]["timeouts"] == 0 and pulse["pending"]["frames"] == 0
    assert report["clock"]["phase"][str(hz)]["grid_error"] < 1e-8


def test_playhead_moves_and_writes_the_time_text_only_while_playing(report: dict[str, Any]) -> None:
    playhead = report["playhead"]
    assert playhead["idle"]["raf_per_s"] == 0 and playhead["idle"]["width"] == "0%"
    assert 55 <= playhead["playing"]["raf_per_s"] <= 62
    assert abs(playhead["playing"]["shown_s"] - playhead["playing"]["audio_s"]) <= 0.12
    assert playhead["paused"] == {"raf_per_s": 0, "text": "set by Python"}
    assert playhead["other_take"] == {"raf_per_s": 0, "width": "0%"}
    assert playhead["scrubbed"]["valuetext"] == "0:02.3 / 0:04.6"


def test_generating_trace_loops_reveals_and_holds_still(report: dict[str, Any]) -> None:
    trace = report["trace"]
    assert trace["loop"]["raf_per_s"] <= 62 and trace["loop"]["min_offset"] < 50 < 950 < trace["loop"]["max_offset"]
    assert trace["fill"]["grow"]["offset"] == "500" and trace["fill"]["raf_per_s"] == 0
    assert trace["reduced"] == {"offset": "0", "head": "none", "raf_per_s": 0}
