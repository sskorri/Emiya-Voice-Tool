"""chatterbox's three tqdm call shapes (lifted from spikes\\theme_boot\\tqdm_probe.py; no NiceGUI import here).

N11 runs tqdm_job() inside the app through run.io_bound (smoke_plan), the way chatterbox's sampling loop
runs in a worker thread, and checks all three shapes finish and tqdm's write lock is free afterwards.
"""
from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from typing import Any

SHAPES = 3


def tqdm_job() -> dict[str, Any]:
    """Run tqdm the way chatterbox does (t3.py's sampling loop, flow_matching.py's zip loop)."""
    try:
        from tqdm import tqdm
    except Exception as error:  # pragma: no cover - tqdm cannot even be imported
        return {"import_error": f"{type(error).__name__}: {error}", "shapes": {}, "stderr_is_none": sys.stderr is None}

    a, b = list(range(200)), list(range(200))
    n = len(a)
    shapes: dict[str, Callable[[], int]] = {
        "tqdm(range(200), desc='Sampling', dynamic_ncols=True)":
            lambda: sum(1 for _ in tqdm(range(200), desc="Sampling", dynamic_ncols=True)),
        "tqdm(range(200))": lambda: sum(1 for _ in tqdm(range(200))),
        "tqdm(zip(a, b), total=n)": lambda: sum(1 for _ in tqdm(zip(a, b), total=n)),
    }
    results: dict[str, Any] = {}
    for name, call in shapes.items():
        try:
            results[name] = {"ok": True, "iterations": call()}
        except Exception as error:   # recorded, not raised: the check wants to see exactly what fails
            results[name] = {"ok": False, "error": f"{type(error).__name__}: {error}",
                             "traceback": traceback.format_exc(limit=8)}
    if sys.stderr is not None:
        sys.stderr.flush()
    return {"shapes": results, "ok": sum(1 for r in results.values() if r["ok"]),
            "stderr_is_none": sys.stderr is None, "stderr": repr(sys.stderr), "locks": lock_state()}


def lock_state() -> dict[str, Any]:
    """Is tqdm's class-wide write lock free right now? (non-blocking)

    tqdm.refresh() takes TqdmDefaultWriteLock and display() writes to the stream; when the stream is None
    that write raises before release(), so the lock stays held by the failing thread.
    """
    from tqdm.std import TqdmDefaultWriteLock

    state: dict[str, Any] = {}
    thread_lock = TqdmDefaultWriteLock.th_lock
    free = thread_lock.acquire(blocking=False)
    if free:
        thread_lock.release()
    state["thread_lock_free"] = free
    mp_lock = getattr(TqdmDefaultWriteLock, "mp_lock", None)
    if mp_lock is not None:
        free = mp_lock.acquire(False)
        if free:
            mp_lock.release()
        state["mp_lock_free"] = free
    return state
