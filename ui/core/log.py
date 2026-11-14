"""The app log (§4.13): a ring of the last 60 lines "HH:MM:SS  text", mirrored into LogState.

Every line also goes to the `voicetool` logger at INFO, which lands in logs\\gui.log under pythonw.
Lines are text only: the log never holds an exception object (§3.0).
"""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.contract.state import LogState

    from .events import CoreEvents

__all__ = ["LOGGER_NAME", "MAX_LINES", "AppLog"]

LOGGER_NAME = "voicetool"
MAX_LINES = 60
SEPARATOR = "  "                         # two spaces between the time and the text (M L1364-1373)

_logger = logging.getLogger(LOGGER_NAME)


class AppLog:
    """The Log panel's lines, newest last."""

    def __init__(self, maxlen: int = MAX_LINES, *, state: LogState | None = None,
                 events: CoreEvents | None = None, clock: Callable[[], datetime] | None = None) -> None:
        if maxlen < 1:
            raise ValueError(f"maxlen must be at least 1, got {maxlen}")
        self._ring: deque[str] = deque(maxlen=maxlen)
        self._state = state
        self._events = events
        self._clock = clock or datetime.now

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self._ring)

    def add(self, text: str) -> str:
        """Append one line stamped with the local time; returns the stamped line."""
        text = " ".join(str(text).split("\n")).strip()
        line = f"{self._clock():%H:%M:%S}{SEPARATOR}{text}"
        self._ring.append(line)
        _logger.info("%s", text)
        self._publish()
        return line

    def restore(self, lines: Iterable[str]) -> None:
        """Replace the ring with lines that are already stamped (a scenario's log); not re-logged."""
        self._ring.clear()
        self._ring.extend(str(line) for line in lines)
        self._publish()

    def _publish(self) -> None:
        if self._state is not None:
            self._state.lines = tuple(self._ring)
        if self._events is not None:
            self._events.notify()
