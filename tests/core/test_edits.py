"""TakeEdits (core.edits): the takes list's edits saved one at a time in the order asked (R1-BUG-1, R1-BUG-2).

The race tests under tests\\pages\\test_rel_monkey.py drive the same writer through the real pages; these pin its
own rules quickly: click order, the coalescing of a burst of stars, a purge that waits for an earlier star, and a
failure that is reported once and never blocks the next edit.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

from ui.contract.plans import Toast
from ui.core.services.takes_store import TakeNotFoundError, TakesStore

from tests.core.test_facade import Rig, rigs, speak  # noqa: F401  (fixtures)

HOLD_S = 5.0                                    # a held worker gives up after this, so a broken test never hangs


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def record_stars(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    """Every set_starred the store runs, in the order the workers run them."""
    calls: list[tuple[int, bool]] = []
    real = TakesStore.set_starred

    def recorded(self: TakesStore, take_id: int, starred: bool) -> Any:
        calls.append((take_id, starred))
        return real(self, take_id, starred)

    monkeypatch.setattr(TakesStore, "set_starred", recorded)
    return calls


def toasts(rig: Rig) -> list[Toast]:
    seen: list[Toast] = []
    rig.core.events.toast.subscribe(seen.append)
    return seen


async def test_a_burst_of_stars_lands_as_the_last_click_in_at_most_two_writes(speak: Rig,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Five toggles of Take 5 (unstarred) in one tick: the first is written as asked, the other four wait as one
    queued write of the latest value, so the take ends starred (an odd number of toggles) after two rewrites."""
    calls = record_stars(monkeypatch)
    edits = speak.core.edits
    assert not speak.core.takes.get(5).starred
    for _ in range(5):
        edits.star(5, not edits.wanted(5, speak.core.takes.get(5).starred))
    assert edits.wanted(5, False) is True and edits.busy
    await speak.settle()
    assert calls == [(5, True), (5, True)]
    assert speak.core.takes.get(5).starred and speak.state.takes.takes == speak.core.takes.takes
    assert edits.wanted(5, False) is False and not edits.busy          # nothing pending: the store holds it


async def test_a_double_click_is_two_writes_in_click_order(speak: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_stars(monkeypatch)
    edits = speak.core.edits
    starred = speak.core.takes.get(7).starred
    edits.star(7, not edits.wanted(7, starred))
    edits.star(7, not edits.wanted(7, starred))
    await speak.settle()
    assert calls == [(7, not starred), (7, starred)] and speak.core.takes.get(7).starred is starred


async def test_a_purge_waits_for_a_star_asked_before_it(speak: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The star's write is held: the purge must not even start until it is done, and then keeps the take."""
    began: list[str] = []
    entered, release = threading.Event(), threading.Event()
    star_write, purge_write = TakesStore.set_starred, TakesStore.purge_unstarred

    def held_star(self: TakesStore, take_id: int, starred: bool) -> Any:
        began.append("star")
        entered.set()
        release.wait(HOLD_S)
        return star_write(self, take_id, starred)

    def purge(self: TakesStore, ids: Any, **kwargs: Any) -> Any:
        began.append("purge")
        return purge_write(self, ids, **kwargs)

    monkeypatch.setattr(TakesStore, "set_starred", held_star)
    monkeypatch.setattr(TakesStore, "purge_unstarred", purge)
    core = speak.core
    unstarred = tuple(take.id for take in core.takes.takes if not take.starred)
    assert 5 in unstarred
    core.edits.star(5, True)
    purging = asyncio.create_task(core.edits.call(core.takes.purge_unstarred, unstarred, trash=core.trash))
    assert await asyncio.to_thread(entered.wait, HOLD_S)
    for _ in range(20):
        await asyncio.sleep(0)
    assert began == ["star"], "the purge started while an earlier star was still being saved"
    release.set()
    result = await asyncio.wait_for(purging, HOLD_S)
    assert began == ["star", "purge"] and 5 not in result.moved
    assert core.takes.get(5) is not None and core.takes.get(5).starred


async def test_a_failed_star_is_one_red_toast_and_the_next_toggle_starts_from_the_store(
        speak: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = toasts(speak)

    def gone(self: TakesStore, take_id: int, starred: bool) -> Any:
        raise TakeNotFoundError(take_id)

    monkeypatch.setattr(TakesStore, "set_starred", gone)
    edits = speak.core.edits
    task = edits.star(5, True)
    assert await task is False
    await speak.settle()
    assert [(t.text, t.bad) for t in seen] == [("Couldn't save the star: Take 5 is not in the takes list", True)]
    assert edits.wanted(5, False) is False and not edits.busy


async def test_a_failing_edit_raises_to_its_caller_and_never_blocks_the_next(speak: Rig,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: TakesStore, ids: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(TakesStore, "purge_unstarred", broken)
    core = speak.core
    with pytest.raises(OSError, match="No space left"):
        await core.edits.call(core.takes.purge_unstarred, (5,), trash=core.trash)
    calls = record_stars(monkeypatch)
    assert await core.edits.star(5, True) is True and calls == [(5, True)]


async def test_edits_from_two_page_loads_share_one_order(speak: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clients (two BehaviourFactory instances) toggle the same star: one writer, one order, one result."""
    from tests.behaviours import factory_for

    calls = record_stars(monkeypatch)
    first, second = factory_for(speak)[0].playback(), factory_for(speak)[0].playback()
    clicks: list[Callable[[int], None]] = [first.star, second.star, first.star]
    for click in clicks:
        click(4)
    await speak.settle()
    start = calls[0][1]
    assert calls == [(4, start), (4, start)] and speak.core.takes.get(4).starred is start
