"""AppLog (§4.13): the 60-line ring, the "HH:MM:SS  text" format, the LogState mirror and the logging bridge."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import pytest

from ui.contract.state import LogState
from ui.core.events import CoreEvents
from ui.core.log import LOGGER_NAME, MAX_LINES, AppLog


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


class Clock:
    """A wall clock the test sets by hand."""

    def __init__(self) -> None:
        self.at = datetime(2026, 11, 13, 13, 40, 12)

    def __call__(self) -> datetime:
        return self.at


def test_lines_are_stamped_with_two_spaces_before_the_text() -> None:
    clock = Clock()
    app_log = AppLog(clock=clock)
    assert app_log.add("Emiya Voice started · no model loaded (models load on demand)") == \
        "13:40:12  Emiya Voice started · no model loaded (models load on demand)"
    clock.at = datetime(2026, 11, 13, 9, 5, 7)
    app_log.add("Loading chatterbox-multilingual")
    assert app_log.lines == ("13:40:12  Emiya Voice started · no model loaded (models load on demand)",
                             "09:05:07  Loading chatterbox-multilingual")


def test_the_default_clock_is_local_wall_time() -> None:
    line = AppLog().add("hello")
    assert re.fullmatch(r"\d\d:\d\d:\d\d  hello", line)


def test_the_ring_keeps_the_newest_60_lines() -> None:
    app_log = AppLog(clock=Clock())
    for n in range(1, 66):
        app_log.add(f"line {n}")
    assert MAX_LINES == 60
    assert len(app_log.lines) == 60
    assert app_log.lines[0].endswith("line 6") and app_log.lines[-1].endswith("line 65")


def test_a_smaller_ring_and_a_bad_size() -> None:
    app_log = AppLog(3, clock=Clock())
    for n in range(5):
        app_log.add(str(n))
    assert [line[10:] for line in app_log.lines] == ["2", "3", "4"]
    with pytest.raises(ValueError):
        AppLog(0)


def test_the_mirror_follows_every_line() -> None:
    state = LogState()
    app_log = AppLog(state=state, clock=Clock())
    app_log.add("one")
    app_log.add("two")
    assert state.lines == app_log.lines == ("13:40:12  one", "13:40:12  two")


def test_a_line_is_one_line() -> None:
    app_log = AppLog(clock=Clock())
    assert app_log.add("  first\nsecond  ") == "13:40:12  first second"


def test_every_line_goes_to_the_voicetool_logger_at_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    AppLog(clock=Clock()).add("Loaded chatterbox-turbo · 8.7 s · 3.1 GB")
    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert [(r.levelno, r.getMessage()) for r in records] == [(logging.INFO, "Loaded chatterbox-turbo · 8.7 s · 3.1 GB")]
    assert records[0].exc_info is None


def test_restore_shows_stamped_lines_without_logging_them(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    state = LogState()
    app_log = AppLog(state=state, clock=Clock())
    app_log.add("Emiya Voice started · no model loaded (models load on demand)")
    caplog.clear()
    app_log.restore(["13:57:02  Loading chatterbox-multilingual", "13:57:16  Loaded chatterbox-multilingual"])
    assert state.lines == ("13:57:02  Loading chatterbox-multilingual", "13:57:16  Loaded chatterbox-multilingual")
    assert not [r for r in caplog.records if r.name == LOGGER_NAME]
    app_log.add("Generating take 8 · aiko · Multilingual")
    assert state.lines[-1] == "13:40:12  Generating take 8 · aiko · Multilingual"


async def test_toasts_are_numbered_and_held_until_the_first_client() -> None:
    events = CoreEvents()
    shown: list[tuple[str, bool, int]] = []
    events.toast.subscribe(lambda toast: shown.append((toast.text, toast.bad, toast.seq)))
    events.say("The takes list couldn't be read", bad=True)
    assert shown == [] and [toast.seq for toast in events.held] == [1]
    events.release()
    events.say("Turbo ready · 8.7 s · 3.1 GB")              # after release: at once
    assert shown == [("Turbo ready · 8.7 s · 3.1 GB", False, 2)]
    await asyncio.sleep(0)                                   # the held ones go out on the next tick
    assert shown[-1] == ("The takes list couldn't be read", True, 1)
    assert events.last_toast is not None and events.last_toast.seq == 2 and events.held == ()


async def test_each_line_notifies_changed_once_per_tick() -> None:
    events = CoreEvents()
    seen: list[int] = []
    events.changed.subscribe(lambda: seen.append(1))
    app_log = AppLog(events=events, clock=Clock())
    app_log.add("one")
    app_log.add("two")
    assert seen == []
    for _ in range(3):
        await asyncio.sleep(0)
    assert seen == [1]
