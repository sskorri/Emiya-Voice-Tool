"""The GPU test's external VRAM measurement must not stall the UI loop it measures."""
from __future__ import annotations

import asyncio
import threading

import pytest

from tests.e2e import conftest as gpu


async def test_vram_measurement_keeps_the_event_loop_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a slow driver query must let the loop acknowledge that the query started."""
    started = threading.Event()
    acknowledged = threading.Event()

    def slow_reading() -> int:
        started.set()
        return 42 if acknowledged.wait(timeout=2) else -1

    monkeypatch.setattr(gpu, "gpu_used_median", slow_reading)
    reading = asyncio.create_task(gpu.nvsmi_used())
    for _ in range(200):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    acknowledged.set()
    assert await reading == 42, "the VRAM query blocked the loop until its worker timed out"


@pytest.mark.parametrize("value", [None, 1234])
async def test_vram_measurement_preserves_missing_or_valid_readings(
    monkeypatch: pytest.MonkeyPatch, value: int | None,
) -> None:
    monkeypatch.setattr(gpu, "gpu_used_median", lambda: value)
    assert await gpu.nvsmi_used() == value
