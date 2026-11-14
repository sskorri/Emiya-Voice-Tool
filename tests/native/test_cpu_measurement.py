"""CPU accounting uses elapsed kernel/user time, not a conversion of variable-frequency CPU cycles."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.native import smoke, winprobe


def test_cpu_seconds_adds_kernel_and_user_times(monkeypatch: pytest.MonkeyPatch) -> None:
    def times(handle, created, exited, kernel, user):
        kernel._obj.dwLowDateTime = 1_500_000
        user._obj.dwLowDateTime = 2_500_000
        return True

    monkeypatch.setattr(winprobe.kernel32, "GetProcessTimes", times)
    process = winprobe.TrackedProcess(1, 0, "owned.exe", 123, 0, 0)
    assert process.cpu_seconds() == pytest.approx(0.4)


def test_tree_cpu_includes_exited_descendants() -> None:
    tree = winprobe.ProcessTree.__new__(winprobe.ProcessTree)
    tree.procs = {1: SimpleNamespace(cpu_seconds=lambda: 1.25, alive=lambda: True),
                  2: SimpleNamespace(cpu_seconds=lambda: 2.75, alive=lambda: False)}
    assert tree.cpu_seconds() == 4.0


def test_soak_cpu_uses_cpu_time_without_a_clock_frequency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden():
        raise AssertionError("CPU seconds must not be divided by a calibrated cycle frequency")

    monkeypatch.setattr(winprobe, "cycle_counter_hz", forbidden)
    run = smoke.Run(smoke.Session(tmp_path), smoke.Spec(name="cpu-accounting"), {})
    run.cpu_time_samples = [(0, 0.0), (30, 3.0), (60, 6.0)]
    assert smoke.soak_cpu(run, 0, 30, 60) == [10.0, 10.0]


def test_idle_budget_requires_drawn_frames_on_an_inactive_desktop(tmp_path: Path) -> None:
    run = smoke.Run(smoke.Session(tmp_path), smoke.Spec(name="throttled"), {})
    run.cpu = {"idle": {"cpu_pct_one_core": 1, "tick_hz": 60, "fps": 1},
               "working": {"tick_hz": 60, "fps": 60}, "reduce_motion": {"cpu_pct_one_core": 1}}
    assert not smoke.cpu_check(run).ok
    run.cpu["idle"]["fps"] = 60
    assert smoke.cpu_check(run).ok
