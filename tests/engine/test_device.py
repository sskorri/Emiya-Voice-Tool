"""engine.device.device_info: lock-free GPU readings, torch only when asked (BUILD_SPEC §3.4).

torch.cuda is never called for real: the fast tests put a fake torch module in
sys.modules, the slow ones monkeypatch the real torch.cuda functions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.device import DeviceInfo, device_info

ROOT = Path(__file__).resolve().parents[2]
GIB = 2**30
TOTAL = 12_878_086_144


class FakeCuda:
    """The torch.cuda calls device_info makes, with recorded calls."""

    def __init__(self, *, initialized: bool = False, available: bool = True, free: int = 7 * GIB,
                 reserved: int = 3 * GIB, allocated: int = 2 * GIB, peak: int = 4 * GIB,
                 fail: str | None = None) -> None:
        self.initialized = initialized
        self.available = available
        self.free, self.reserved, self.allocated, self.peak = free, reserved, allocated, peak
        self.fail = fail
        self.calls: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("CUDA error: unknown error\nCUDA kernel errors might be reported later")

    def is_initialized(self) -> bool:
        self._call("is_initialized")
        return self.initialized

    def is_available(self) -> bool:
        self._call("is_available")
        return self.available

    def get_device_properties(self, index: int) -> SimpleNamespace:
        self._call("get_device_properties")
        assert index == 0
        self.initialized = True                  # the real call creates the CUDA context
        return SimpleNamespace(name="NVIDIA GeForce RTX 4070 Ti", total_memory=TOTAL)

    def mem_get_info(self, index: int) -> tuple[int, int]:
        self._call("mem_get_info")
        assert index == 0
        return self.free, TOTAL

    def memory_reserved(self, index: int) -> int:
        self._call("memory_reserved")
        return self.reserved

    def memory_allocated(self, index: int) -> int:
        self._call("memory_allocated")
        return self.allocated

    def max_memory_reserved(self, index: int) -> int:
        self._call("max_memory_reserved")
        return self.peak


@pytest.fixture
def fake_torch(monkeypatch: pytest.MonkeyPatch):
    """fake_torch(**FakeCuda options) puts a fake torch in sys.modules and returns its cuda."""
    def install(**options: object) -> FakeCuda:
        cuda = FakeCuda(**options)
        module = types.ModuleType("torch")
        module.cuda = cuda
        module.__version__ = "2.6.0+cu124"
        module.version = SimpleNamespace(cuda="12.4")
        monkeypatch.setitem(sys.modules, "torch", module)
        return cuda
    return install


def _assert_no_cuda_numbers(info: DeviceInfo) -> None:
    assert (info.name, info.total_bytes, info.free_bytes, info.other_bytes) == (None, None, None, None)
    assert (info.reserved_bytes, info.allocated_bytes, info.peak_reserved_bytes) == (0, 0, 0)


def test_without_torch_it_returns_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    info = device_info()
    assert "torch" not in sys.modules
    assert info.initialized is False and info.cuda_available is None
    _assert_no_cuda_numbers(info)
    assert (info.torch_version, info.cuda_version, info.error) == ("", None, None)
    assert info.measured_at > 0


def test_uninitialised_cuda_is_not_initialised_without_probe(fake_torch) -> None:
    cuda = fake_torch(initialized=False)
    info = device_info()
    assert cuda.calls == ["is_initialized"]
    assert info.initialized is False and info.cuda_available is None
    _assert_no_cuda_numbers(info)
    assert (info.torch_version, info.cuda_version, info.error) == ("2.6.0+cu124", "12.4", None)


def test_probe_reads_name_size_and_free(fake_torch) -> None:
    cuda = fake_torch(initialized=False)
    info = device_info(probe=True)
    assert cuda.calls.index("is_available") < cuda.calls.index("get_device_properties")
    assert info.initialized is True and info.cuda_available is True
    assert info.name == "NVIDIA GeForce RTX 4070 Ti"
    assert (info.total_bytes, info.free_bytes) == (TOTAL, 7 * GIB)
    assert (info.reserved_bytes, info.allocated_bytes, info.peak_reserved_bytes) == (3 * GIB, 2 * GIB, 4 * GIB)
    assert info.other_bytes == TOTAL - 7 * GIB - 3 * GIB
    assert info.error is None


def test_initialised_cuda_is_read_without_probe(fake_torch) -> None:
    cuda = fake_torch(initialized=True)
    info = device_info()
    assert "is_available" not in cuda.calls
    assert info.cuda_available is True and info.free_bytes == 7 * GIB


def test_other_bytes_is_clamped_at_zero(fake_torch) -> None:
    fake_torch(initialized=True, free=10 * GIB, reserved=5 * GIB)
    assert device_info().other_bytes == 0


def test_probe_without_cuda(fake_torch) -> None:
    cuda = fake_torch(available=False)
    info = device_info(probe=True)
    assert cuda.calls == ["is_initialized", "is_available"]
    assert info.cuda_available is False and info.initialized is False and info.error is None
    _assert_no_cuda_numbers(info)


def test_cuda_errors_become_text(fake_torch) -> None:
    fake_torch(fail="get_device_properties")
    info = device_info(probe=True)
    assert info.cuda_available is False
    assert info.error == "CUDA error: unknown error"
    _assert_no_cuda_numbers(info)
    assert info.torch_version == "2.6.0+cu124"


def test_probe_without_a_usable_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)      # `import torch` raises ImportError
    info = device_info(probe=True)
    assert info.cuda_available is False and info.error
    _assert_no_cuda_numbers(info)


def test_importing_the_leaf_modules_loads_no_torch() -> None:
    """Fresh interpreter: import engine.device and the other leaf modules, read the device."""
    code = (
        "import json, sys\n"
        "import engine.device\n"
        "info = engine.device.device_info()\n"
        "after_device = sorted(m for m in ('torch', 'chatterbox') if m in sys.modules)\n"
        "import engine.audio, engine.errors, engine.lock, engine.progress, engine.types\n"
        "import engine.backends.info\n"
        "heavy = sorted(m for m in ('torch', 'chatterbox', 'huggingface_hub', 'librosa') if m in sys.modules)\n"
        "print(json.dumps({'after_device': after_device, 'heavy': heavy,\n"
        "                  'initialized': info.initialized, 'cuda_available': info.cuda_available}))\n"
    )
    env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result == {"after_device": [], "heavy": [], "initialized": False, "cuda_available": None}


# ---------- real torch, monkeypatched torch.cuda ----------

@pytest.mark.slow
def test_real_torch_without_probe_leaves_cuda_alone() -> None:
    torch = pytest.importorskip("torch")
    if torch.cuda.is_initialized():
        pytest.skip("CUDA was initialised by something else in this process")
    info = device_info()
    assert info.initialized is False and info.cuda_available is None
    assert info.torch_version == str(torch.__version__)
    assert info.cuda_version == torch.version.cuda
    assert torch.cuda.is_initialized() is False


@pytest.mark.slow
def test_real_torch_with_monkeypatched_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = pytest.importorskip("torch")
    props = SimpleNamespace(name="NVIDIA GeForce RTX 4070 Ti", total_memory=TOTAL)
    monkeypatch.setattr(torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_properties", lambda index: props)
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda index: (8 * GIB, TOTAL))
    monkeypatch.setattr(torch.cuda, "memory_reserved", lambda index: 3 * GIB)
    monkeypatch.setattr(torch.cuda, "memory_allocated", lambda index: 2 * GIB)
    monkeypatch.setattr(torch.cuda, "max_memory_reserved", lambda index: 4 * GIB)
    info = device_info()
    assert info.initialized is True and info.cuda_available is True
    assert (info.name, info.total_bytes, info.free_bytes) == ("NVIDIA GeForce RTX 4070 Ti", TOTAL, 8 * GIB)
    assert (info.reserved_bytes, info.allocated_bytes, info.peak_reserved_bytes) == (3 * GIB, 2 * GIB, 4 * GIB)
    assert info.other_bytes == TOTAL - 8 * GIB - 3 * GIB
    assert info.torch_version == str(torch.__version__)
