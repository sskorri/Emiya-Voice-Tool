"""Device mapping (§4.15.6): DeviceInfo → DeviceState, the short name, and the VRAM, GPU line and About texts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from engine.device import DeviceInfo
from ui.contract.state import DeviceState
from ui.core.services.device import (
    GPU_TIP,
    VramTexts,
    apply_reading,
    gb,
    gpu_about,
    gpu_line,
    short_name,
    to_state,
    vram_texts,
)

GIB = 2 ** 30
TOTAL = 12_878_086_144                  # RTX 4070 Ti on this PC: 11.99 GiB, shown as 12.0 GB
APP = int(3.4 * GIB)
OTHER = int(2.5 * GIB)
FREE = TOTAL - APP - OTHER              # 6.09 GiB


def reading(**changes) -> DeviceInfo:
    """A CUDA reading with a model resident."""
    base = DeviceInfo(
        initialized=True, cuda_available=True, name="NVIDIA GeForce RTX 4070 Ti", total_bytes=TOTAL,
        free_bytes=FREE, reserved_bytes=APP, allocated_bytes=APP - 100, other_bytes=OTHER,
        peak_reserved_bytes=APP + 1000, torch_version="2.6.0+cu124", cuda_version="12.4", error=None,
        measured_at=123.5,
    )
    return replace(base, **changes)


def unchecked() -> DeviceInfo:
    """device_info(probe=False) before CUDA exists: nothing is known yet."""
    return reading(initialized=False, cuda_available=None, name=None, total_bytes=None, free_bytes=None,
                   reserved_bytes=0, allocated_bytes=0, other_bytes=None, peak_reserved_bytes=0,
                   torch_version="", cuda_version=None)


def no_cuda(error: str | None = None) -> DeviceInfo:
    return replace(unchecked(), cuda_available=False, torch_version="2.6.0+cpu", error=error)


# ---------- mapping ----------

@pytest.mark.parametrize(("name", "short"), [
    ("NVIDIA GeForce RTX 4070 Ti", "RTX 4070 Ti"),
    ("NVIDIA GeForce GTX 1080", "GTX 1080"),
    ("NVIDIA RTX A6000", "NVIDIA RTX A6000"),
    ("Tesla T4", "Tesla T4"),
    ("  NVIDIA GeForce RTX 4090  ", "RTX 4090"),
    ("", ""),
    (None, ""),
])
def test_short_name_drops_the_geforce_prefix(name: str | None, short: str) -> None:
    assert short_name(name) == short


def test_a_cuda_reading_maps_every_field() -> None:
    assert to_state(reading()) == DeviceState(
        detected=True, cuda=True, name="NVIDIA GeForce RTX 4070 Ti", short_name="RTX 4070 Ti",
        total_bytes=TOTAL, free_bytes=FREE, app_bytes=APP, other_bytes=OTHER, peak_bytes=APP + 1000,
        error="", measured_at=123.5,
    )


def test_a_reading_without_cuda_or_with_an_error() -> None:
    plain = to_state(no_cuda())
    assert (plain.detected, plain.cuda, plain.name, plain.error, plain.total_bytes) == (True, False, "", "", None)
    broken = to_state(no_cuda("CUDA driver version is insufficient"))
    assert (broken.detected, broken.cuda, broken.error) == (True, False, "CUDA driver version is insufficient")


def test_a_reading_that_did_not_check_cuda_stays_undetected() -> None:
    state = to_state(unchecked())
    assert (state.detected, state.cuda, state.app_bytes, state.other_bytes) == (False, None, 0, None)


def test_apply_reading_updates_the_mirror_in_place() -> None:
    mirror = DeviceState()
    assert apply_reading(mirror, reading()) is True
    assert mirror == to_state(reading())
    assert apply_reading(mirror, reading(measured_at=124.0)) is False     # only the time moved
    assert mirror.measured_at == 124.0
    assert apply_reading(mirror, reading(reserved_bytes=APP + GIB, measured_at=125.0)) is True
    assert mirror.app_bytes == APP + GIB


def test_gb_has_one_decimal() -> None:
    assert (gb(TOTAL), gb(APP), gb(0), gb(None)) == ("12.0", "3.4", "0.0", "—")


# ---------- VRAM block ----------

def test_vram_texts_before_the_first_reading() -> None:
    assert vram_texts(DeviceState()) == VramTexts(
        value="—", gold=False, app_pct=0.0, other_pct=0.0, meter_visible=True, detail="Detecting GPU…", tip="")
    assert vram_texts(to_state(unchecked())).detail == "Detecting GPU…"


def test_vram_texts_with_cuda_and_a_resident_model() -> None:
    texts = vram_texts(to_state(reading()), "Multilingual")
    assert texts.value == "3.4 GB" and texts.gold is True and texts.meter_visible is True
    assert texts.app_pct == pytest.approx(APP / TOTAL * 100, abs=0.01)
    assert texts.other_pct == pytest.approx(OTHER / TOTAL * 100, abs=0.01)
    assert texts.detail == "2.5 other · 6.1 free"
    assert texts.tip == "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"


def test_vram_texts_with_cuda_and_nothing_resident() -> None:
    texts = vram_texts(to_state(reading(reserved_bytes=0, free_bytes=FREE + APP)))
    assert texts.value == "0.0 GB" and texts.gold is False and texts.app_pct == 0.0
    assert texts.detail == "2.5 other · 9.5 free"
    assert texts.tip == "RTX 4070 Ti · 12.0 GB · this app 0.0 · other 2.5 · free 9.5"


def test_vram_value_turns_gold_above_0_05_gb() -> None:
    assert vram_texts(to_state(reading(reserved_bytes=int(0.05 * GIB)))).gold is False
    assert vram_texts(to_state(reading(reserved_bytes=int(0.06 * GIB)))).gold is True


def test_vram_meter_never_exceeds_the_total() -> None:
    texts = vram_texts(to_state(reading(reserved_bytes=TOTAL, other_bytes=TOTAL)))
    assert texts.app_pct == 100.0 and texts.other_pct == 0.0


def test_vram_texts_without_cuda() -> None:
    assert vram_texts(to_state(no_cuda()), "Multilingual") == VramTexts(
        value="CPU", gold=False, app_pct=0.0, other_pct=0.0, meter_visible=False,
        detail="No CUDA GPU · models run on the CPU", tip="")


def test_vram_texts_after_a_detection_error() -> None:
    texts = vram_texts(to_state(no_cuda("CUDA driver version is insufficient")))
    assert (texts.value, texts.meter_visible) == ("CPU", False)
    assert texts.detail == "No CUDA GPU · models run on the CPU"
    assert texts.tip == "GPU not detected · CUDA driver version is insufficient"


# ---------- GPU line (Models) and About row ----------

@pytest.mark.parametrize(("state", "line", "about"), [
    (DeviceState(), "Detecting the GPU…", "Detecting…"),
    (to_state(reading()), "NVIDIA GeForce RTX 4070 Ti · 12.0 GB · detected at startup", "RTX 4070 Ti · 12.0 GB · detected"),
    (to_state(no_cuda()), "No CUDA GPU detected · models run on the CPU", "No CUDA GPU · models run on the CPU"),
    (to_state(no_cuda("no driver")), "GPU not detected · no driver", "Not detected · no driver"),
])
def test_gpu_line_and_about_row(state: DeviceState, line: str, about: str) -> None:
    assert gpu_line(state) == line
    assert gpu_about(state) == about


def test_gpu_tooltip_is_the_mockup_copy() -> None:
    assert GPU_TIP == ("Detected: name and size from torch.cuda.get_device_properties(0); usage from "
                       "torch.cuda.memory_reserved() and torch.cuda.mem_get_info()")
