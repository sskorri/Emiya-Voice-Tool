"""Call output contract and WinMM buffer ownership, all driven by a simulated driver."""
from __future__ import annotations

import ctypes as ct
import io
import struct
from threading import Event

import pytest

from ui.contract.ports import AudioDefault, AudioEndpoint, AudioOutPort, cable_default_roles, resolve_endpoint
from ui.kit import sound_out as sound


CABLE = AudioEndpoint("cable-id", "CABLE Input (VB-Audio Virtual Cable)")
SPEAKER = AudioEndpoint("speaker-id", "Speakers")


def chunk(kind, body):
    return struct.pack("<4sI", kind, len(body)) + body + (b"\0" if len(body) & 1 else b"")


def wav(samples, tag=sound.FLOAT32, rate=24000, *, extensible=False):
    code, width = ("f", 4) if tag == sound.FLOAT32 else ("h", 2)
    fmt = struct.pack("<HHIIHH", 0xfffe if extensible else tag, 1, rate, rate * width, width, width * 8)
    if extensible:
        fmt += struct.pack("<HHII", 22, width * 8, 4, tag) + bytes.fromhex("00001000800000aa00389b71")
    body = b"WAVE" + chunk(b"JUNK", b"odd") + chunk(b"fmt ", fmt)
    body += chunk(b"data", struct.pack(f"<{len(samples)}{code}", *samples))
    return b"RIFF" + struct.pack("<I", len(body)) + body


class SimulatedStream:
    def __init__(self, fail=False):
        self.writes = []
        self.queued = 0
        self.closed = False
        self.fail = fail

    def submit(self, data):
        if self.fail:
            raise OSError("endpoint disconnected")
        self.writes.append(data)
        self.queued += 1

    def reap(self):
        self.queued = max(0, self.queued - 1)
        return self.queued

    def close(self):
        self.closed = True


class Driver:
    def __init__(self):
        self.devices_now = [(0, SPEAKER), (7, CABLE)]
        self.formats = {sound.FLOAT32, sound.PCM16}
        self.checks = []
        self.opened = []
        self.stream = SimulatedStream()

    def devices(self):
        return self.devices_now

    def supports(self, index, enc, rate):
        self.checks.append((index, enc, rate))
        return enc in self.formats

    def open(self, *args):
        self.opened.append(args)
        return self.stream


def test_persistent_selection_only_falls_back_to_a_unique_exact_name():
    twin = AudioEndpoint("second", CABLE.name)
    assert resolve_endpoint([twin, CABLE], AudioEndpoint("CABLE-ID", "old display name")) == CABLE
    assert resolve_endpoint([SPEAKER, twin], CABLE) == twin
    assert resolve_endpoint([twin, AudioEndpoint("other", CABLE.name)], CABLE) is None
    assert resolve_endpoint([CABLE, CABLE], CABLE) is None
    assert resolve_endpoint([SPEAKER], AudioEndpoint("", "Speak")) is None
    assert resolve_endpoint([SPEAKER], AudioEndpoint("", "speakers")) is None
    assert resolve_endpoint([SPEAKER], AudioEndpoint("", "")) is None


def test_cable_default_warning_includes_playback_and_recording_roles():
    roles = (AudioDefault("playback", "console", SPEAKER),
             AudioDefault("playback", "communications", CABLE),
             AudioDefault("recording", "multimedia", AudioEndpoint("mic", "CABLE Output (VB-Audio Virtual Cable)")))
    assert cable_default_roles(roles) == roles[1:]


@pytest.mark.parametrize("extensible", [False, True])
def test_enumeration_and_send_after_backend_reorder_without_default_playback(tmp_path, extensible):
    driver = Driver()
    adapter = sound.WindowsAudioOut(driver)
    assert isinstance(adapter, AudioOutPort)
    assert adapter.list_devices() == (SPEAKER, CABLE)
    driver.devices_now = [(1, CABLE), (7, SPEAKER)]
    file = tmp_path / "hello.wav"
    samples = [.25] * 5001
    file.write_bytes(wav(samples, extensible=extensible))
    started = []
    adapter.play(CABLE, file, Event(), lambda: started.append(len(driver.stream.writes)))
    assert driver.checks == [(1, sound.FLOAT32, 24000)]
    assert driver.opened == [(1, CABLE, sound.FLOAT32, 24000)]
    assert started == [1] and driver.stream.closed
    assert [len(block) // 4 for block in driver.stream.writes] == [2400, 2400, 201]
    assert b"".join(driver.stream.writes) == struct.pack("<5001f", *samples)


@pytest.mark.parametrize("source,destination,values,expected", [
    (sound.FLOAT32, sound.PCM16, [-2., -1., -.5, 0., .5, 1., 2., float("nan")], [-32768, -32768, -16384, 0, 16384, 32767, 32767, 0]),
    (sound.PCM16, sound.FLOAT32, [-32768, -123, 0, 32767], [-1., -123 / 32768, 0., 32767 / 32768]),
    (sound.PCM16, sound.PCM16, [-32768, -123, 0, 32767], [-32768, -123, 0, 32767]),
])
def test_converts_only_for_the_selected_device(tmp_path, source, destination, values, expected):
    driver = Driver()
    driver.formats = {destination}
    path = tmp_path / "test.wav"
    path.write_bytes(wav(values, source, rate=16000))
    sound.WindowsAudioOut(driver).play(CABLE, path, Event(), lambda: None)
    assert driver.opened == [(7, CABLE, destination, 16000)]
    code = "f" if destination == sound.FLOAT32 else "h"
    assert driver.stream.writes == [struct.pack(f"<{len(expected)}{code}", *expected)]


def test_device_disappeared_or_unsupported_never_uses_another_device(tmp_path):
    driver = Driver()
    path = tmp_path / "voice.wav"
    path.write_bytes(wav([0.]))
    driver.formats.clear()
    adapter = sound.WindowsAudioOut(driver)
    with pytest.raises(OSError, match="does not accept"):
        adapter.play(CABLE, path, Event(), lambda: pytest.fail("reported start"))
    assert not driver.opened and all(c[0] == 7 for c in driver.checks)
    driver.devices_now = [(7, SPEAKER)]
    with pytest.raises(OSError, match="missing"):
        adapter.play(CABLE, path, Event(), lambda: pytest.fail("reported start"))
    assert not driver.opened


def test_device_loss_releases_stream_and_preserves_the_take(tmp_path):
    driver = Driver()
    driver.stream.fail = True
    path = tmp_path / "take.wav"
    raw = wav([.125] * 5000)
    path.write_bytes(raw)
    with pytest.raises(OSError, match="disconnected"):
        sound.WindowsAudioOut(driver).play(CABLE, path, Event(), lambda: pytest.fail("reported start"))
    assert driver.stream.closed and path.read_bytes() == raw


def test_stop_before_open_and_stop_after_the_first_block(tmp_path):
    driver = Driver()
    adapter = sound.WindowsAudioOut(driver)
    path = tmp_path / "take.wav"
    path.write_bytes(wav([0.] * 7200))
    stop = Event()
    stop.set()
    adapter.play(CABLE, path, stop, lambda: pytest.fail("reported start"))
    assert not driver.opened
    stop.clear()
    adapter.play(CABLE, path, stop, stop.set)
    assert driver.stream.closed and len(driver.stream.writes) == 1


@pytest.mark.parametrize("broken", [b"", wav([]), wav([1.])[:-1],
    wav([1.]).replace(struct.pack("<HHIIHH", 3, 1, 24000, 96000, 4, 32),
                      struct.pack("<HHIIHH", 3, 2, 24000, 96000, 4, 32)),
    wav([1.]).replace(b"data", b"JUNK")])
def test_rejects_malformed_or_unsupported_wavs_before_opening(broken):
    with pytest.raises(ValueError):
        sound._read_wav(io.BytesIO(broken))


class LowLevelDriver:
    def __init__(self):
        self.log = []
        self.writes = []
        self.busy_once = True

    def prepare(self, handle, head, size):
        self.log.append("prepare")
        return 0

    def write(self, handle, head, size):
        self.log.append("write")
        self.writes.append(ct.cast(head, ct.POINTER(sound._Header)).contents)
        return 0

    def reset(self, handle):
        self.log.append("reset")
        for header in self.writes:
            assert ct.string_at(header.data, header.size) == b"sample"
            header.flags |= sound._DONE
        return 0

    def unprepare(self, handle, head, size):
        self.log.append("unprepare")
        assert ct.cast(head, ct.POINTER(sound._Header)).contents.flags & sound._DONE
        if self.busy_once:
            self.busy_once = False
            return sound._STILLPLAYING
        return 0

    def close_wave(self, handle):
        self.log.append("close")
        return 0


def test_reset_and_unprepare_every_buffer_before_free_and_close():
    driver = LowLevelDriver()
    stream = sound._Stream(driver, 17)
    stream.submit(b"sample")
    stream.submit(b"sample")
    assert stream.reap() == 2
    stream.close()
    assert driver.log == ["prepare", "write", "prepare", "write", "reset", "unprepare", "unprepare", "unprepare", "close"]
    assert stream.buffers == []


def test_opened_endpoint_is_reverified_before_the_first_write():
    native = sound._WinMM.__new__(sound._WinMM)
    closed = []
    native.open_wave = lambda *args: 0
    native.get_index = lambda *args: 0
    native.endpoint_id = lambda index: "different-device"
    native.close_wave = lambda handle: closed.append(handle) or 0
    with pytest.raises(OSError, match="changed while opening"):
        native.open(0, CABLE, sound.FLOAT32, 24000)
    assert len(closed) == 1


def test_probe_queries_specific_devices_and_never_opens_a_stream(monkeypatch):
    driver = Driver()
    adapter = sound.WindowsAudioOut(driver)
    monkeypatch.setattr(adapter, "default_roles", lambda: (AudioDefault("playback", "console", SPEAKER),))
    report = adapter.probe()
    assert [d["id"] for d in report["playback"]] == [SPEAKER.id, CABLE.id]
    assert driver.checks == [(i, fmt, 24000) for i in (0, 7) for fmt in (sound.FLOAT32, sound.PCM16)]
    assert not driver.opened
    assert report["defaults"][0]["endpoint"]["name"] == SPEAKER.name
