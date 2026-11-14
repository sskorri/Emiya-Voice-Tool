"""GPU readings for the VRAM meter: lock-free, and torch-free unless torch is already loaded.

device_info() never takes the engine RLock, so it answers from any thread while a model
loads. Without `probe` it never imports torch and never initialises CUDA; `probe=True`
imports torch and reads the name, size and free memory, which creates this process's
CUDA context (its VRAM then shows under "other").
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any

from .errors import first_line

__all__ = ["DeviceInfo", "device_info"]


@dataclass(frozen=True)
class DeviceInfo:
    """One reading of GPU 0 and this process's CUDA caching allocator."""

    initialized: bool              # torch.cuda.is_initialized()
    cuda_available: bool | None    # None = not checked yet (probe False and CUDA not initialised)
    name: str | None               # get_device_properties(0).name, e.g. "NVIDIA GeForce RTX 4070 Ti"
    total_bytes: int | None        # .total_memory
    free_bytes: int | None         # torch.cuda.mem_get_info(0)[0], device-wide
    reserved_bytes: int            # torch.cuda.memory_reserved(0): this process's caching allocator
    allocated_bytes: int           # torch.cuda.memory_allocated(0)
    other_bytes: int | None        # max(0, total - free - reserved): other processes + this process's CUDA context
    peak_reserved_bytes: int       # torch.cuda.max_memory_reserved(0)
    torch_version: str             # "" while torch is not imported
    cuda_version: str | None
    error: str | None
    measured_at: float             # time.monotonic()


def device_info(probe: bool = False) -> DeviceInfo:
    """Read the GPU without loading anything; see the module docstring for the rules."""
    if not probe and "torch" not in sys.modules:
        return _reading()
    try:
        import torch  # already loaded unless probing; waits if another thread is importing it
    except Exception as exc:  # a missing or broken torch install
        return _reading(cuda_available=False, error=_error_text(exc))
    versions = _versions(torch)
    try:
        return _read_cuda(torch.cuda, probe, versions)
    except Exception as exc:  # CUDA driver or runtime errors
        return _reading(initialized=_is_initialized(torch), cuda_available=False,
                        error=_error_text(exc), **versions)


def _read_cuda(cuda: Any, probe: bool, versions: dict[str, Any]) -> DeviceInfo:
    if not cuda.is_initialized():
        if not probe:
            return _reading(**versions)
        if not cuda.is_available():
            return _reading(cuda_available=False, **versions)
    props = cuda.get_device_properties(0)            # creates the CUDA context when probing
    free = int(cuda.mem_get_info(0)[0])
    total = int(props.total_memory)
    reserved = int(cuda.memory_reserved(0))
    return DeviceInfo(
        initialized=bool(cuda.is_initialized()),
        cuda_available=True,
        name=str(props.name),
        total_bytes=total,
        free_bytes=free,
        reserved_bytes=reserved,
        allocated_bytes=int(cuda.memory_allocated(0)),
        other_bytes=max(0, total - free - reserved),
        peak_reserved_bytes=int(cuda.max_memory_reserved(0)),
        error=None,
        measured_at=time.monotonic(),
        **versions,
    )


def _reading(*, initialized: bool = False, cuda_available: bool | None = None,
             torch_version: str = "", cuda_version: str | None = None,
             error: str | None = None) -> DeviceInfo:
    """A reading without any CUDA numbers."""
    return DeviceInfo(
        initialized=initialized,
        cuda_available=cuda_available,
        name=None,
        total_bytes=None,
        free_bytes=None,
        reserved_bytes=0,
        allocated_bytes=0,
        other_bytes=None,
        peak_reserved_bytes=0,
        torch_version=torch_version,
        cuda_version=cuda_version,
        error=error,
        measured_at=time.monotonic(),
    )


def _versions(torch: Any) -> dict[str, Any]:
    cuda_version = getattr(getattr(torch, "version", None), "cuda", None)
    return {
        "torch_version": str(getattr(torch, "__version__", "")),
        "cuda_version": None if cuda_version is None else str(cuda_version),
    }


def _is_initialized(torch: Any) -> bool:
    try:
        return bool(torch.cuda.is_initialized())
    except Exception:
        return False


def _error_text(exc: BaseException) -> str:
    return first_line(str(exc)) or type(exc).__name__
