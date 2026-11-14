"""The core's clock and timers (§5.0, §4.9): later(), every(), now() and spawn().

Every timer, ticker and elapsed label in ui.core and ui.behaviours runs on a Scheduler, so tests drive
time by hand: ManualScheduler (VOICETOOL_CLOCK=manual) only moves when advance() is called, and its
settle() waits for the work the timers and jobs started. The real Scheduler uses loop.call_later on
the running event loop and NiceGUI's app-level background tasks. A timer callback may be sync or
return an awaitable (which is spawned); a failing callback is logged as text and never stops a timer.
"""
from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

from nicegui import background_tasks

__all__ = ["CLOCK_ENV", "Handle", "ManualScheduler", "Scheduler", "default_scheduler"]

CLOCK_ENV = "VOICETOOL_CLOCK"           # "manual" selects ManualScheduler (the page tests)

log = logging.getLogger("voicetool.core")

TimerCallback = Callable[[], Any]


class Handle:
    """A scheduled timer; cancel() stops it (idempotent)."""

    def __init__(self) -> None:
        self._cancelled = False
        self._loop_handle: asyncio.TimerHandle | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
        if self._loop_handle is not None:
            self._loop_handle.cancel()
            self._loop_handle = None


class Scheduler:
    """Timers on the running event loop; now() is time.monotonic()."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def now(self) -> float:
        return time.monotonic()

    def later(self, delay_s: float, fn: TimerCallback) -> Handle:
        """Run fn once, delay_s seconds from now."""
        handle = Handle()
        self._arm(handle, max(0.0, delay_s), lambda: self._fire(handle, fn))
        return handle

    def every(self, interval_s: float, fn: TimerCallback) -> Handle:
        """Run fn every interval_s seconds, the first time interval_s from now, until cancelled."""
        if interval_s <= 0:
            raise ValueError(f"interval must be positive, got {interval_s}")
        handle = Handle()

        def tick() -> None:
            if handle.cancelled:
                return
            self._arm(handle, interval_s, tick)
            self._invoke(fn)

        self._arm(handle, interval_s, tick)
        return handle

    def spawn(self, awaitable: Awaitable[Any], *, name: str = "voicetool task") -> asyncio.Task[Any]:
        """Run an awaitable as an app-level task (never tied to a client)."""
        loop = asyncio.get_running_loop()
        if getattr(background_tasks.core, "loop", None) is loop:
            return background_tasks.create(awaitable, name=name)
        task = loop.create_task(_coroutine(awaitable), name=name)
        self._keep(task)
        return task

    # ---------- plumbing ----------

    def _arm(self, handle: Handle, delay_s: float, run: Callable[[], None]) -> None:
        handle._loop_handle = asyncio.get_running_loop().call_later(delay_s, run)

    def _fire(self, handle: Handle, fn: TimerCallback) -> None:
        if not handle.cancelled:
            handle._loop_handle = None
            self._invoke(fn)

    def _invoke(self, fn: TimerCallback) -> None:
        """Call a timer callback; spawn what it returns when awaitable; log failures as text."""
        try:
            result = fn()
        except Exception as exc:
            text = _text(exc)
            log.warning("A timer callback failed: %s", text)
            return
        if inspect.isawaitable(result):
            self.spawn(result, name=getattr(fn, "__name__", "timer"))

    def _keep(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.warning("A background task failed: %s", _text(error))


class _Timer:
    """One ManualScheduler timer: due time, period (None = once) and its handle."""

    __slots__ = ("due", "period", "fn", "handle")

    def __init__(self, due: float, period: float | None, fn: TimerCallback, handle: Handle) -> None:
        self.due = due
        self.period = period
        self.fn = fn
        self.handle = handle


class ManualScheduler(Scheduler):
    """A clock that moves only when advance() is called; for tests (VOICETOOL_CLOCK=manual).

    advance(seconds) fires every timer due within that span in time order, with now() set to each
    timer's due time while it runs; settle() then waits until every task spawned through it is done.
    """

    def __init__(self, start: float = 0.0) -> None:
        super().__init__()
        self._now = float(start)
        self._heap: list[tuple[float, int, _Timer]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._now

    def later(self, delay_s: float, fn: TimerCallback) -> Handle:
        handle = Handle()
        self._push(_Timer(_grid(self._now + max(0.0, delay_s)), None, fn, handle))
        return handle

    def every(self, interval_s: float, fn: TimerCallback) -> Handle:
        if interval_s <= 0:
            raise ValueError(f"interval must be positive, got {interval_s}")
        handle = Handle()
        self._push(_Timer(_grid(self._now + interval_s), interval_s, fn, handle))
        return handle

    def spawn(self, awaitable: Awaitable[Any], *, name: str = "voicetool task") -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(_coroutine(awaitable), name=name)
        self._keep(task)
        return task

    @property
    def pending(self) -> int:
        """Timers still armed."""
        return sum(1 for _, _, timer in self._heap if not timer.handle.cancelled)

    def advance(self, seconds: float) -> None:
        """Move the clock forward, firing due timers in order (sync)."""
        if seconds < 0:
            raise ValueError(f"time only moves forward, got {seconds}")
        end = _grid(self._now + seconds)
        while self._heap and self._heap[0][0] <= end:
            due, _, timer = heapq.heappop(self._heap)
            if timer.handle.cancelled:
                continue
            self._now = max(self._now, due)
            if timer.period is not None:
                timer.due = _grid(due + timer.period)
                self._push(timer)
            self._invoke(timer.fn)
        self._now = end

    async def settle(self, timeout: float = 10.0, *, ignore: Iterable[asyncio.Task[Any] | None] = ()) -> None:
        """Wait until every task spawned through this scheduler (and what they spawn) has finished.

        ignore: tasks not to wait for, e.g. a job that a test holds (core.jobs.task).
        """
        skip = {task for task in ignore if task is not None}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for _ in range(3):
                await asyncio.sleep(0)
            pending = [task for task in self._tasks if not task.done() and task not in skip]
            if not pending:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{len(pending)} task(s) still running after {timeout} s: "
                                   + ", ".join(task.get_name() for task in pending))
            await asyncio.wait(pending, timeout=remaining)

    async def run_for(self, seconds: float, *, step: float = 0.1) -> None:
        """Advance in steps of `step`, settling the spawned work after each one."""
        left = seconds
        while left > 1e-9:
            span = min(step, left)
            self.advance(span)
            await self.settle()
            left -= span

    def cancel_pending(self) -> int:
        """Cancel every unfinished spawned task (test teardown); returns how many."""
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        return len(pending)

    def _push(self, timer: _Timer) -> None:
        heapq.heappush(self._heap, (timer.due, next(self._seq), timer))


def _grid(seconds: float) -> float:
    """Manual-clock times on a nanosecond grid, so 22 steps of 0.2 s land exactly on 4.4 s."""
    return round(seconds, 9)


def default_scheduler() -> Scheduler:
    """ManualScheduler when VOICETOOL_CLOCK=manual, else the real Scheduler."""
    return ManualScheduler() if os.environ.get(CLOCK_ENV, "").strip().lower() == "manual" else Scheduler()


def _coroutine(awaitable: Awaitable[Any]) -> Coroutine[Any, Any, Any]:
    if asyncio.iscoroutine(awaitable):
        return awaitable

    async def wrapper() -> Any:
        return await awaitable

    return wrapper()


def _text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return f"{type(exc).__name__}: {lines[0][:200]}" if lines else type(exc).__name__
