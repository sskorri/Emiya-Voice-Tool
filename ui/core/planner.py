"""Switch plans and the low-VRAM rule (§4.6).

A plan says what loading a backend would do, from the mirrors and the load history only: it is sync,
never touches the engine and never calls huggingface_hub (the weights sizes come from the facade's
startup cache). Low VRAM: on CUDA, with free VRAM known, when the VRAM free once the resident model is
gone is below what the backend needs plus a 0.5 GiB margin (the mockup's rule, M L1555, L2448).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal, Protocol

from ui.contract.plans import AfterForge, AfterGenerate, SwitchPlan

from . import catalog

if TYPE_CHECKING:
    from ui.contract.state import AppState
    from ui.core.services.load_history import LoadEstimate, LoadHistory

__all__ = ["FALLBACK_NEEDS_BYTES", "GIB", "MARGIN_BYTES", "Planner", "about_text", "round_half_up"]

GIB = 2 ** 30
MARGIN_BYTES = GIB // 2                  # 0.5 GiB
FALLBACK_NEEDS_BYTES = 4 * GIB           # nothing measured and no weights size yet

Reason = Literal["explicit", "generate", "forge", "retry", "warmup"]


class _Sizes(Protocol):
    def weights_bytes(self, backend: str) -> int | None: ...
    def preferred_device(self) -> str: ...


class Planner:
    """Builds SwitchPlans; the TokenStore uses low_vram() to re-check a plan when it is redeemed."""

    def __init__(self, state: AppState, history: LoadHistory, sizes: _Sizes) -> None:
        self._state = state
        self._history = history
        self._sizes = sizes

    def device(self) -> str:
        """Where the next load runs: the preferred device, or the CPU once no CUDA GPU was found."""
        preferred = self._sizes.preferred_device()
        return "cpu" if preferred == "cuda" and self._state.device.cuda is False else preferred

    def load_estimate(self, backend: str) -> LoadEstimate:
        """The load history's estimate for backend on the next load's device (dock "~14 s", progress copy)."""
        return self._history.estimate(backend, self.device())

    def needs_bytes(self, backend: str) -> tuple[int, bool]:
        """(bytes, estimated?): the measured reserve or peak, else the weights size, else 4 GiB."""
        estimate = self.load_estimate(backend)
        measured = [gb for gb in (estimate.gb, estimate.peak_gb) if gb is not None]
        if measured:
            return round(max(measured) * GIB), False
        weights = self._sizes.weights_bytes(backend)
        if weights:
            return int(weights), True
        return FALLBACK_NEEDS_BYTES, True

    def free_after_bytes(self) -> int | None:
        """Free VRAM once the resident model is unloaded; None before the first reading."""
        device = self._state.device
        if device.free_bytes is None:
            return None
        return device.free_bytes + (device.app_bytes if self._state.engine.resident else 0)

    def low_vram(self, backend: str) -> bool:
        """The low-VRAM rule for loading backend now (CPU and unknown free VRAM are never low)."""
        return self._low(self.needs_bytes(backend)[0], self.free_after_bytes())

    def switch_plan(self, to_backend: str, *, reason: Reason,
                    after: AfterGenerate | AfterForge | None = None) -> SwitchPlan:
        """What loading to_backend would do now, with the label its named control shows."""
        resident = self._state.engine.resident or None
        needs, estimated = self.needs_bytes(to_backend)
        free_after = self.free_after_bytes()
        estimate = self.load_estimate(to_backend)
        return SwitchPlan(
            to_backend=to_backend,
            from_backend=resident,
            reason=reason,
            label=_label(to_backend, resident, reason, estimate.about_s),
            needs_bytes=needs,
            needs_estimated=estimated,
            free_after_bytes=free_after,
            low_vram=self._low(needs, free_after),
            about_s=estimate.about_s,
            last_s=estimate.last_s,
            after=after,
        )

    def _low(self, needs: int, free_after: int | None) -> bool:
        if free_after is None or self.device() != "cuda" or not self._state.device.cuda:
            return False
        return free_after < needs + MARGIN_BYTES


def round_half_up(seconds: float) -> int:
    """Whole seconds, halves up: 8.5 → 9, 8.4 → 8 (the estimates' rounding, §5.2)."""
    return int(math.floor(seconds + 0.5))


def about_text(seconds: float | None) -> str:
    """" · about 9 s" for an estimate, "" without one."""
    return f" · about {round_half_up(seconds)} s" if seconds else ""


def _label(to_backend: str, resident: str | None, reason: Reason, about_s: float | None) -> str:
    name = catalog.label(to_backend)
    if reason == "generate":
        return f"Switch to {name} & Generate" if resident else f"Load {name} & Generate{about_text(about_s)}"
    if reason == "forge":
        return f"Switch to {name} & Forge" if resident else f"Load {name} & Forge"
    if reason == "explicit":
        return "Switch to This Model" if resident else "Load This Model"
    if reason == "retry":
        return "Retry"
    return f"Load {name}"
