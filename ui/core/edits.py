"""TakeEdits (§5.4, §5.10): the takes list's edits from the pages, saved one at a time in the order they were asked.

A star (the Speak card, a session row, a Takes row), Keep A / Keep B and Remove Unstarred each rewrite
outputs/takes/index.json in a worker. They all go through this one ordered writer, which the Core owns, so every
client and page shares it: an edit starts only once the edit before it has finished. Two quick clicks on a star are
therefore saved in click order, and a purge confirmed right after a star sees that star (R1-BUG-1, R1-BUG-2). A
take added by a job is not an edit here: TakesStore's own lock keeps it apart, and it never touches another take.

Stars coalesce. A star asked while nothing is being written is saved as asked; while another edit runs, the clicks
on one take's star wait as a single queued write that saves the latest wanted value when its turn comes, so ten
clicks cost at most two index rewrites. wanted() is the value a toggle starts from, so a double click toggles twice
even before the first write lands. A failed star write is the red toast "Couldn't save the star: <reason>"; every
finished edit copies the store's tuple into the takes mirror.
"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from nicegui import run

from .facade import _boxed, describe_error

if TYPE_CHECKING:
    from . import Core

__all__ = ["STAR_FAILED", "TakeEdits"]

STAR_FAILED = "Couldn't save the star: {reason}"

T = TypeVar("T")


class TakeEdits:
    """The ordered writer for the takes list's edits (one per Core)."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._tail: asyncio.Task[Any] | None = None       # the last edit asked for
        self._wanted: dict[int, bool] = {}                # take id -> the star asked for last, until it is saved
        self._queued: dict[int, asyncio.Task[bool]] = {}  # take id -> its star write still waiting for its turn

    @property
    def busy(self) -> bool:
        """An edit is running or waiting for its turn."""
        return self._tail is not None and not self._tail.done()

    def wanted(self, take_id: int, current: bool) -> bool:
        """The star take_id has once every edit asked so far is saved; `current` (the store's) when none is pending."""
        return self._wanted.get(take_id, current)

    def star(self, take_id: int, starred: bool) -> asyncio.Task[bool]:
        """Save take_id's star as `starred` after every earlier edit. Returns the task that saves it: True when the
        star was saved, False when the write failed (its toast is raised here)."""
        self._wanted[take_id] = bool(starred)
        waiting = self._queued.get(take_id)
        if waiting is not None:
            return waiting                                # that write saves the latest wanted value
        if not self.busy:
            value = bool(starred)
            return self._enqueue(lambda: self._save_star(take_id, value), name="voicetool star")
        task = self._enqueue(lambda: self._save_star(take_id, None), name="voicetool star")
        self._queued[take_id] = task
        return task

    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
        """fn(*args, **kwargs) (a TakesStore mutation) in a worker once every earlier edit has finished.

        Returns its result (None when the app is stopping) and raises what it raises, here in the caller: the
        edit itself runs as an app-level task that never fails, so it finishes even when the client that asked
        for it goes away, and nothing is reported twice.
        """
        work = functools.partial(fn, *args, **kwargs)
        task = self._enqueue(lambda: self._captured(work), name=f"voicetool {getattr(fn, '__name__', 'edit')}")
        try:
            value, error = await asyncio.shield(task)
        finally:
            self._core.publish_takes()
        if error is not None:
            raise error
        return value

    async def wait_idle(self) -> None:
        """Wait until every edit asked so far has finished (tests, shutdown)."""
        while self.busy and self._tail is not None:
            await asyncio.wait({self._tail})

    # ---------- plumbing ----------

    def _enqueue(self, make: Callable[[], Awaitable[T]], *, name: str) -> asyncio.Task[T]:
        previous = self._tail
        task: asyncio.Task[T] = self._core.scheduler.spawn(self._after(previous, make), name=name)
        self._tail = task
        return task

    @staticmethod
    async def _after(previous: asyncio.Task[Any] | None, make: Callable[[], Awaitable[T]]) -> T:
        if previous is not None and not previous.done():
            await asyncio.wait({previous})               # its outcome is its own: an edit never fails the next
        return await make()

    @staticmethod
    async def _in_worker(work: Callable[[], T]) -> T | None:
        done = await run.io_bound(_boxed, work)
        return None if done is None else done[0]

    @classmethod
    async def _captured(cls, work: Callable[[], T]) -> tuple[T | None, Exception | None]:
        try:
            return await cls._in_worker(work), None
        except Exception as exc:
            return None, exc

    async def _save_star(self, take_id: int, value: bool | None) -> bool:
        if value is None:                                 # a queued write: from now on a click queues a new one
            self._queued.pop(take_id, None)
            value = self._wanted.get(take_id)
            if value is None:
                return True
        problem = ""
        try:
            await self._in_worker(functools.partial(self._core.takes.set_starred, take_id, value))
        except Exception as exc:
            problem = describe_error(exc)
        if take_id not in self._queued:                   # nothing newer is waiting: the store holds the truth
            self._wanted.pop(take_id, None)
        self._core.publish_takes()
        if problem:
            self._core.events.say(STAR_FAILED.format(reason=problem), bad=True)
            return False
        return True
