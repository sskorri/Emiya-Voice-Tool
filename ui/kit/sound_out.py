"""Windows call audio. Enumeration and format queries never open a playback stream."""
from __future__ import annotations

import ctypes as ct
import math
import os
import struct
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO

from ui.contract.ports import AudioDefault, AudioEndpoint

PCM16, FLOAT32 = 1, 3
_BITS = {PCM16: 16, FLOAT32: 32}
_FORMAT_QUERY = 1
_BADFORMAT = 32
_STILLPLAYING = 33
_INVALID_HANDLE = 5
_DONE = 1
_P = ct.c_void_p
_U = ct.c_uint32
_W = ct.c_uint16
_H = ct.c_int32


class _Format(ct.LittleEndianStructure):
    _pack_ = 1
    _fields_ = [("tag", _W), ("channels", _W), ("rate", _U), ("byte_rate", _U),
                ("align", _W), ("bits", _W), ("extra", _W)]


class _Header(ct.Structure):
    _fields_ = [("data", _P), ("size", _U), ("recorded", _U), ("user", ct.c_size_t),
                ("flags", _U), ("loops", _U), ("next", _P), ("reserved", ct.c_size_t)]


def _fmt(encoding: int, rate: int) -> _Format:
    bits = _BITS[encoding]
    return _Format(encoding, 1, rate, rate * (bits // 8), bits // 8, bits, 0)


def _checked(code: int, action: str) -> None:
    if code:
        raise OSError(f"{action} failed (WinMM {code})")


def _dll_function(dll, name, ret, *args):
    fn = getattr(dll, name)
    fn.restype = ret
    fn.argtypes = list(args)
    return fn


def _com_call(obj, slot, ret, args, *values):
    table = ct.cast(obj, ct.POINTER(ct.POINTER(_P))).contents
    return ct.WINFUNCTYPE(ret, _P, *args)(table[slot])(obj, *values)


def _hr(code: int) -> None:
    if code < 0:
        raise OSError(f"Windows audio endpoint query failed (HRESULT 0x{code & 0xffffffff:08X})")


@contextmanager
def _com_object():
    obj = _P()
    try:
        yield obj
    finally:
        if obj:
            _com_call(obj, 2, _U, ())  # IUnknown::Release


@contextmanager
def _enumerator():
    ole = ct.WinDLL("ole32", use_last_error=True)
    init = _dll_function(ole, "CoInitializeEx", _H, _P, _U)(None, 0)
    # RPC_E_CHANGED_MODE means the caller already owns a different valid apartment.
    if init != -2147417850:
        _hr(init)
    try:
        clsid = uuid.UUID("bcde0395-e52f-467c-8e3d-c4579291692e").bytes_le
        iid = uuid.UUID("a95664d2-9614-4f35-a746-de8db63617e6").bytes_le
        with _com_object() as enum:
            create = _dll_function(ole, "CoCreateInstance", _H, _P, _P, _U, _P, ct.POINTER(_P))
            _hr(create(clsid, None, 1, iid, ct.byref(enum)))
            yield ole, enum
    finally:
        if init >= 0:
            _dll_function(ole, "CoUninitialize", None)()


class _PropertyValue(ct.Structure):
    # PROPVARIANT's union is two pointers wide on 64-bit Windows (e.g. BLOB).
    _fields_ = [("vt", _W), ("reserved", _W * 3), ("value", _P), ("extra", _P)]


def _friendly_name(ole, device) -> str:
    key = uuid.UUID("a45c254e-df1c-4efd-8020-67d146a850e0").bytes_le + struct.pack("<I", 14)
    with _com_object() as store:
        _hr(_com_call(device, 4, _H, (_U, ct.POINTER(_P)), 0, ct.byref(store)))  # OpenPropertyStore
        value = _PropertyValue()
        try:
            _hr(_com_call(store, 5, _H, (_P, _P), key, ct.byref(value)))  # GetValue
            if value.vt != 31 or not value.value:  # VT_LPWSTR
                raise OSError("Windows audio endpoint has no reported name")
            return ct.wstring_at(value.value)
        finally:
            _dll_function(ole, "PropVariantClear", _H, _P)(ct.byref(value))


def _defaults() -> tuple[AudioDefault, ...]:
    routes = []
    with _enumerator() as (ole, enum):
        for flow, direction in enumerate(("playback", "recording")):
            for role, name in enumerate(("console", "multimedia", "communications")):
                with _com_object() as dev:
                    hr = _com_call(enum, 4, _H, (_U, _U, ct.POINTER(_P)), flow, role, ct.byref(dev))
                    if hr == -2147023728:  # E_NOTFOUND: no default exists for this flow.
                        continue
                    _hr(hr)
                    ident = _P()
                    _hr(_com_call(dev, 5, _H, (ct.POINTER(_P),), ct.byref(ident)))  # IMMDevice::GetId
                    try:
                        endpoint = AudioEndpoint(ct.wstring_at(ident), _friendly_name(ole, dev))
                        routes.append(AudioDefault(direction, name, endpoint))
                    finally:
                        _dll_function(ole, "CoTaskMemFree", None, _P)(ident)
    return tuple(routes)


class _WinMM:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Call audio output requires Windows")
        dll = ct.WinDLL("winmm", use_last_error=True)
        self.count = _dll_function(dll, "waveOutGetNumDevs", _U)
        self.message = _dll_function(dll, "waveOutMessage", _U, _P, _U, ct.c_size_t, ct.c_size_t)
        self.open_wave = _dll_function(dll, "waveOutOpen", _U, ct.POINTER(_P), _U, ct.POINTER(_Format),
                                       ct.c_size_t, ct.c_size_t, _U)
        self.get_index = _dll_function(dll, "waveOutGetID", _U, _P, ct.POINTER(_U))
        self.prepare = _dll_function(dll, "waveOutPrepareHeader", _U, _P, ct.POINTER(_Header), _U)
        self.write = _dll_function(dll, "waveOutWrite", _U, _P, ct.POINTER(_Header), _U)
        self.unprepare = _dll_function(dll, "waveOutUnprepareHeader", _U, _P, ct.POINTER(_Header), _U)
        self.reset = _dll_function(dll, "waveOutReset", _U, _P)
        self.close_wave = _dll_function(dll, "waveOutClose", _U, _P)

    def endpoint_id(self, index):
        length = _U()
        _checked(self.message(index, 0x812, ct.addressof(length), 0), "Read audio endpoint ID size")
        if length.value < 4 or length.value > 131072 or length.value % 2:
            raise OSError("Windows audio endpoint has an invalid ID")
        ident = ct.create_unicode_buffer(length.value // 2)
        _checked(self.message(index, 0x811, ct.addressof(ident), length.value), "Read audio endpoint ID")
        if not ident.value:
            raise OSError("Windows audio endpoint has an empty ID")
        return ident.value

    def devices(self):
        found = []
        with _enumerator() as (ole, enum):
            for index in range(self.count()):
                try:
                    ident = self.endpoint_id(index)
                    with _com_object() as dev:
                        _hr(_com_call(enum, 5, _H, (ct.c_wchar_p, ct.POINTER(_P)), ident, ct.byref(dev)))
                        found.append((index, AudioEndpoint(ident, _friendly_name(ole, dev))))
                except OSError:
                    # An endpoint removed while enumerating is ineligible for this snapshot.
                    continue
        return found

    def supports(self, index: int, encoding: int, rate: int) -> bool:
        result = self.open_wave(None, index, ct.byref(_fmt(encoding, rate)), 0, 0, _FORMAT_QUERY)
        if result == _BADFORMAT:
            return False
        _checked(result, "Query call audio format")
        return True

    def open(self, index, endpoint, encoding, rate):
        handle = _P()
        _checked(self.open_wave(ct.byref(handle), index, ct.byref(_fmt(encoding, rate)), 0, 0, 0),
                 "Open selected call device")
        try:
            current = _U()
            _checked(self.get_index(handle, ct.byref(current)), "Identify opened call device")
            if self.endpoint_id(current.value).casefold() != endpoint.id.casefold():
                raise OSError("Selected call device changed while opening")
        except BaseException:
            _checked(self.close_wave(handle), "Close unused call device")
            raise
        return _Stream(self, handle)


class _Stream:
    """Keep each native WAVEHDR and its storage alive until waveOutUnprepareHeader succeeds."""

    def __init__(self, driver, handle):
        self.driver = driver
        self.handle = handle
        self.buffers = []
        self.last_progress = time.monotonic()

    def submit(self, data):
        payload = ct.create_string_buffer(data, len(data))
        header = _Header(ct.addressof(payload), len(data))
        _checked(self.driver.prepare(self.handle, ct.byref(header), ct.sizeof(header)), "Prepare call audio")
        self.buffers.append((header, payload))
        _checked(self.driver.write(self.handle, ct.byref(header), ct.sizeof(header)), "Write call audio")
        if len(self.buffers) == 1:
            self.last_progress = time.monotonic()

    def reap(self):
        while self.buffers and self.buffers[0][0].flags & _DONE:
            self._release_first()
            self.last_progress = time.monotonic()
        if self.buffers and time.monotonic() - self.last_progress > 2:
            raise OSError("Selected call device stopped returning audio buffers")
        return len(self.buffers)

    def _release_first(self):
        header = self.buffers[0][0]
        while True:
            rc = self.driver.unprepare(self.handle, ct.byref(header), ct.sizeof(header))
            if rc != _STILLPLAYING:
                break
            time.sleep(.005)
        if rc != _INVALID_HANDLE:
            _checked(rc, "Release call audio buffer")
        self.buffers.pop(0)

    def close(self):
        try:
            if self.buffers:
                rc = self.driver.reset(self.handle)
                if rc != _INVALID_HANDLE:
                    _checked(rc, "Stop call audio")
        finally:
            try:
                while self.buffers:
                    self._release_first()
            finally:
                rc = self.driver.close_wave(self.handle)
                if rc != _INVALID_HANDLE:
                    _checked(rc, "Close call audio")


@dataclass(frozen=True)
class _Wav:
    encoding: int
    rate: int
    start: int
    size: int


def _read_wav(file: BinaryIO) -> _Wav:
    raw = file.read(12)
    if len(raw) != 12 or raw[:4] != b"RIFF" or raw[8:] != b"WAVE":
        raise ValueError("Call audio requires a RIFF WAV file")
    end = 8 + struct.unpack_from("<I", raw, 4)[0]
    if end > file.seek(0, 2):
        raise ValueError("Truncated call audio WAV")
    file.seek(12)
    fmt = None
    samples = None
    while file.tell() < end:
        header = file.read(8)
        if len(header) != 8:
            raise ValueError("Truncated WAV chunk header")
        kind, length = struct.unpack("<4sI", header)
        pos = file.tell()
        after = pos + length + (length & 1)
        if after > end:
            raise ValueError("Truncated WAV chunk")
        if kind == b"fmt " and fmt is None:
            body = file.read(min(length, 40))
            if len(body) < 16:
                raise ValueError("Invalid WAV format chunk")
            tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", body)
            if tag == 0xfffe and len(body) >= 40 and body[26:40] == bytes.fromhex("000000001000800000aa00389b71"):
                if struct.unpack_from("<HH", body, 16) == (22, bits):
                    tag = struct.unpack_from("<H", body, 24)[0]
            if (tag not in _BITS or bits != _BITS[tag] or channels != 1 or not 0 < rate <= 384000
                    or align != bits // 8 or byte_rate != rate * align):
                raise ValueError("Call audio requires a mono float32 or PCM16 WAV")
            fmt = tag, rate, align
        elif kind == b"data" and samples is None:
            samples = pos, length
        file.seek(after)
    if fmt is None or samples is None or not samples[1] or samples[1] % fmt[2]:
        raise ValueError("WAV has no complete mono audio frames")
    return _Wav(fmt[0], fmt[1], *samples)


def _blocks(file: BinaryIO, wav: _Wav, out: int) -> Iterator[bytes]:
    file.seek(wav.start)
    pending = wav.size
    block_size = max(1, wav.rate // 10) * (_BITS[wav.encoding] // 8)
    while pending:
        amount = min(pending, block_size)
        block = file.read(amount)
        if len(block) != amount:
            raise OSError("WAV was truncated during call playback")
        pending -= amount
        if out == wav.encoding:
            yield block
        elif out == FLOAT32:
            values = [v / 32768 for v, in struct.iter_unpack("<h", block)]
            yield struct.pack(f"<{len(values)}f", *values)
        else:
            values = [_to_pcm16(v) for v, in struct.iter_unpack("<f", block)]
            yield struct.pack(f"<{len(values)}h", *values)


def _to_pcm16(sample: float) -> int:
    if not math.isfinite(sample):
        return 0
    return max(-32768, min(32767, round(sample * 32768)))


class WindowsAudioOut:
    """Constructed without loading DLLs. All methods are synchronous worker entry points."""

    def __init__(self, driver=None):
        self._injected = driver

    def _driver(self):
        return self._injected if self._injected is not None else _WinMM()

    def list_devices(self) -> tuple[AudioEndpoint, ...]:
        return tuple(endpoint for _, endpoint in self._driver().devices())

    def default_roles(self) -> tuple[AudioDefault, ...]:
        return _defaults()

    def play(self, endpoint: AudioEndpoint, path: Path, stop_event: Event, on_start: Callable[[], None]) -> None:
        if stop_event.is_set():
            return
        with Path(path).open("rb") as file:
            wav = _read_wav(file)
            driver = self._driver()
            indices = [index for index, dev in driver.devices() if endpoint.id and dev.id.casefold() == endpoint.id.casefold()]
            if len(indices) != 1:
                raise OSError("Selected call device is missing or ambiguous")
            index = indices[0]
            encoding = next((fmt for fmt in (wav.encoding, FLOAT32 if wav.encoding == PCM16 else PCM16)
                             if driver.supports(index, fmt, wav.rate)), None)
            if encoding is None:
                raise OSError(f"Selected call device does not accept mono float32 or PCM16 at {wav.rate} Hz")
            if stop_event.is_set():
                return
            stream = driver.open(index, endpoint, encoding, wav.rate)
            try:
                first = True
                for block in _blocks(file, wav, encoding):
                    while stream.reap() >= 2 and not stop_event.is_set():
                        stop_event.wait(.01)
                    if stop_event.is_set():
                        return
                    stream.submit(block)
                    if first:
                        first = False
                        on_start()
                while stream.reap() and not stop_event.is_set():
                    stop_event.wait(.01)
            finally:
                stream.close()

    def probe(self) -> dict:
        """Read 24 kHz format support and defaults without opening a stream."""
        driver = self._driver()
        devices = []
        for index, endpoint in driver.devices():
            devices.append(dict(**asdict(endpoint), float32=driver.supports(index, FLOAT32, 24000),
                                pcm16=driver.supports(index, PCM16, 24000)))
        return dict(sample_rate=24000, channels=1, playback=devices,
                    defaults=[asdict(role) for role in self.default_roles()])


if __name__ == "__main__":
    import json

    print(json.dumps(WindowsAudioOut().probe(), ensure_ascii=False, indent=2))
