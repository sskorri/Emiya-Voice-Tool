"""GPU readings as UI state (§4.15.6): DeviceInfo → DeviceState, the short GPU name, and the texts of
the VRAM block (§6.2.6), the Models GPU line (§6.11) and the About GPU row (§7.3).

"GB" is bytes / 2^30 with one decimal (§0.3): 12,878,086,144 bytes read "12.0 GB".
"""
from __future__ import annotations

from dataclasses import dataclass, fields

from engine.device import DeviceInfo
from ui.contract.state import DeviceState

__all__ = [
    "DETECTING",
    "GIB",
    "GPU_TIP",
    "NO_CUDA",
    "VramTexts",
    "apply_reading",
    "gb",
    "gpu_about",
    "gpu_line",
    "short_name",
    "to_state",
    "vram_texts",
]

GIB = 2 ** 30
GOLD_MIN_GB = 0.05                        # the value turns gold above this (M L2893)
NAME_PREFIX = "NVIDIA GeForce "
DETECTING = "Detecting GPU…"
NO_CUDA = "No CUDA GPU · models run on the CPU"
GPU_TIP = ("Detected: name and size from torch.cuda.get_device_properties(0); "
           "usage from torch.cuda.memory_reserved() and torch.cuda.mem_get_info()")


@dataclass(frozen=True)
class VramTexts:
    """The VRAM block of the rail and the Models page."""

    value: str                            # this app: "3.4 GB"; "—" before the first reading; "CPU" without CUDA
    gold: bool                            # this app holds more than 0.05 GB
    app_pct: float                        # gold segment, 0..100 of the total
    other_pct: float                      # grey segment, 0..100 of the total
    meter_visible: bool                   # hidden without CUDA
    detail: str                           # "2.5 other · 6.1 free", "Detecting GPU…", "No CUDA GPU · …"
    tip: str                              # "RTX 4070 Ti · 12.0 GB · this app 3.4 (Multilingual) · other 2.5 · free 6.1"


def short_name(name: str | None) -> str:
    """The GPU name without "NVIDIA GeForce " (M L1407): "RTX 4070 Ti"."""
    return (name or "").strip().removeprefix(NAME_PREFIX).strip()


def gb(n_bytes: int | None) -> str:
    """bytes as GB with one decimal ("3.4"); "—" when unknown."""
    return "—" if n_bytes is None else f"{n_bytes / GIB:.1f}"


def to_state(info: DeviceInfo) -> DeviceState:
    """A DeviceState for one reading; a reading that did not check CUDA leaves it undetected."""
    return DeviceState(
        detected=info.cuda_available is not None,
        cuda=info.cuda_available,
        name=info.name or "",
        short_name=short_name(info.name),
        total_bytes=info.total_bytes,
        free_bytes=info.free_bytes,
        app_bytes=info.reserved_bytes,
        other_bytes=info.other_bytes,
        peak_bytes=info.peak_reserved_bytes,
        error=info.error or "",
        measured_at=info.measured_at,
    )


def apply_reading(state: DeviceState, info: DeviceInfo) -> bool:
    """Write a reading into the DeviceState mirror in place (never replace a mirror, §4.10).

    Returns True when anything other than measured_at changed. Call it on the event loop.
    """
    fresh = to_state(info)
    changed = False
    for field in fields(DeviceState):
        value = getattr(fresh, field.name)
        if getattr(state, field.name) != value:
            changed = changed or field.name != "measured_at"
            setattr(state, field.name, value)
    return changed


def _phase(state: DeviceState) -> str:
    """"detecting", "cuda", "error" or "cpu"."""
    if not state.detected or state.cuda is None:
        return "detecting"
    if state.cuda:
        return "cuda"
    return "error" if state.error else "cpu"


def _pct(part: int | None, total: int | None) -> float:
    if not part or not total:
        return 0.0
    return round(min(100.0, max(0.0, part / total * 100.0)), 2)


def vram_texts(state: DeviceState, resident_label: str = "") -> VramTexts:
    """The VRAM value, meter, detail line and tooltip (§6.2.6); resident_label names the model in VRAM."""
    phase = _phase(state)
    if phase == "detecting":
        return VramTexts("—", False, 0.0, 0.0, True, DETECTING, "")
    if phase != "cuda":
        tip = f"GPU not detected · {state.error}" if phase == "error" else ""
        return VramTexts("CPU", False, 0.0, 0.0, False, NO_CUDA, tip)
    app = state.app_bytes
    resident = f" ({resident_label})" if resident_label else ""
    tip = (f"{state.short_name or 'GPU'} · {gb(state.total_bytes)} GB · this app {gb(app)}{resident}"
           f" · other {gb(state.other_bytes)} · free {gb(state.free_bytes)}")
    app_pct = _pct(app, state.total_bytes)
    other_pct = min(_pct(state.other_bytes, state.total_bytes), round(100.0 - app_pct, 2))
    return VramTexts(
        value=f"{gb(app)} GB",
        gold=app / GIB > GOLD_MIN_GB,
        app_pct=app_pct,
        other_pct=other_pct,
        meter_visible=True,
        detail=f"{gb(state.other_bytes)} other · {gb(state.free_bytes)} free",
        tip=tip,
    )


def gpu_line(state: DeviceState) -> str:
    """The Models page GPU line (§6.11): "NVIDIA GeForce RTX 4070 Ti · 12.0 GB · detected at startup"."""
    phase = _phase(state)
    if phase == "detecting":
        return "Detecting the GPU…"
    if phase == "error":
        return f"GPU not detected · {state.error}"
    if phase == "cpu":
        return "No CUDA GPU detected · models run on the CPU"
    return f"{state.name or 'GPU'} · {gb(state.total_bytes)} GB · detected at startup"


def gpu_about(state: DeviceState) -> str:
    """The About card's GPU row (§7.3): "RTX 4070 Ti · 12.0 GB · detected"."""
    phase = _phase(state)
    if phase == "detecting":
        return "Detecting…"
    if phase == "error":
        return f"Not detected · {state.error}"
    if phase == "cpu":
        return NO_CUDA
    return f"{state.short_name or 'GPU'} · {gb(state.total_bytes)} GB · detected"
