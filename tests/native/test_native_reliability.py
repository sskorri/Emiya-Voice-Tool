"""Native reliability bench (test windows, FakeEngine only; only with the user's go-ahead).

    pytest -m "native and not soak" tests\\native\\test_native_reliability.py   (all but the soak, about 5 min)
    pytest -m "native and soak" tests\\native\\test_native_reliability.py   (the 5-minute run)

  * 10 launch/quit cycles through the Close path: exit 0 within 3 s, no process and no WebView2 profile left;
  * 10 minimize/restore cycles from outside (SW_SHOWMINNOACTIVE / posted SC_RESTORE): no activation and
    0 field frames while minimized;
  * 300 seeded route changes from inside: 0 JS errors, no steady growth of DOM nodes or JS heap (per 50);
  * the boards for visual review (PrintWindow captures + manifest), where the busy ones (loading,
    generating) also check that Close arms first and the second Close quits;
  * 5 minutes with the FX field and a held fake take: CPU and heap stay level.
Every launch follows the rules in smoke.py (free screen, no focus, our own tree only, 120 s / 420 s limits).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.native import smoke
from tests.native.test_native_smoke import _attempt

pytestmark = pytest.mark.native


def _bench(step: Any) -> smoke.Check | list[smoke.Check]:
    session = smoke.Session()
    found: dict[str, Any] = {}
    outcome = _attempt(session, lambda: found.setdefault("value", step(session)))
    if "skip" in outcome:
        pytest.skip(outcome["skip"])
    assert "fail" not in outcome, outcome["fail"]
    return found["value"]


def _assert_ok(check: smoke.Check) -> None:
    assert check.ok, f"{check.id}: {check.numbers}\n{json.dumps(check.detail, default=str)[:4000]}"


def test_launch_and_quit_ten_times() -> None:
    _assert_ok(_bench(lambda session: smoke.run_quit_cycles(session, 10)))


def test_minimize_and_restore_ten_times_from_outside() -> None:
    _assert_ok(_bench(lambda session: smoke.run_minrestore(session, 10)))


def test_three_hundred_route_changes_leak_nothing() -> None:
    _assert_ok(_bench(lambda session: smoke.run_routes(session, 300, seed=4)))


def test_board_captures_and_close_while_busy() -> None:
    checks = _bench(smoke.run_captures)
    assert isinstance(checks, list) and checks
    failed = [f"{c.id}: {c.numbers}" for c in checks if not c.ok]
    assert not failed, "\n".join(failed)
    assert {c.id for c in checks} >= {"close while busy (loading)", "close while busy (generating)"}


@pytest.mark.soak
def test_five_minutes_with_the_field_and_a_held_take() -> None:
    _assert_ok(_bench(lambda session: smoke.run_soak(session, 300.0, 30.0)))
