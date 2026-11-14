"""Send indexed WAVs to one selected audio endpoint. This service never calls a model."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ui.contract.plans import GenerateRequest
from ui.contract.ports import AudioEndpoint, cable_default_roles, resolve_endpoint

if TYPE_CHECKING:
    from ui.contract.ports import AudioDefault, AudioOutPort
    from . import Core
    from .clock import Handle


@dataclass(frozen=True)
class _Send:
    take_id: int
    endpoint: AudioEndpoint


def routes_to_call(request: GenerateRequest, *, enabled: bool, epoch: int, truncated: bool) -> bool:
    """Decide once, after a successful commit, using the intent frozen at Generate."""
    return enabled and not truncated and request.call_epoch == epoch


class CallService:
    """Own the outgoing stream across windows. All state changes run on the app event loop."""

    def __init__(self, core: Core, output: AudioOutPort | None):
        self._core = core
        self._output = output
        self.devices: tuple[AudioEndpoint, ...] = ()
        self.default_roles: tuple[AudioDefault, ...] = ()
        self.device_error = ""
        self.hotkey_error = ""
        self.error = ""
        self.phase = "idle"
        self.take_id = 0
        self._epoch = 0
        self._refresh_seq = 0
        self._queue: deque[_Send] = deque()
        self._recent: dict[int, float] = {}
        self._monitor: int | None = None
        self._monitor_owner: object | None = None
        self._capture_owners: set[object] = set()
        self._cancel = threading.Event()
        self._delay: Handle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        core.settings.changed.subscribe(self._setting_changed)

    def _setting(self, key: str):
        return self._core.settings.get("core", key)

    def _choice(self) -> AudioEndpoint:
        choice = self._setting("call_device")
        return AudioEndpoint(choice["id"], choice["name"])

    @property
    def selected_device(self) -> AudioEndpoint | None:
        return resolve_endpoint(self.devices, self._choice())

    @property
    def available(self) -> bool:
        return not self._closed and self._setting("call_enabled") and self.selected_device is not None

    async def refresh_devices(self) -> None:
        """Read the endpoint list on demand. No endpoint is opened for playback."""
        if self._closed or self._output is None:
            return
        self._refresh_seq += 1
        seq = self._refresh_seq
        devices, roles, error = (), (), ""
        try:
            devices = await asyncio.to_thread(self._output.list_devices)
            roles = cable_default_roles(await asyncio.to_thread(self._output.default_roles))
        except Exception as exc:
            error = f"Couldn't check audio devices: {exc}"
        if seq == self._refresh_seq and not self._closed:
            self.devices, self.default_roles, self.device_error = tuple(devices), tuple(roles), error
            self._core.events.notify()

    def _setting_changed(self, section: str, key: str) -> None:
        if section != "core":
            return
        if key == "call_enabled" and not self._setting(key):
            self._epoch += 1
            self.stop()
        elif key == "call_device":
            self._epoch += 1
            self.stop()

    def capture(self, requests: Sequence[GenerateRequest], *, switch: bool) -> tuple[GenerateRequest, ...]:
        """Freeze routing at Generate time, including for a named model switch or a queued batch."""
        reqs = tuple(requests)
        mode = self._setting("call_send")
        if (reqs and self._output is not None and self._setting("call_enabled")
                and (mode == "auto" or (mode == "switch" and switch))):
            return (replace(reqs[0], call_epoch=self._epoch), *reqs[1:])
        return reqs

    def route_saved(self, request: GenerateRequest, take_id: int, *, truncated: bool) -> None:
        self._monitor = None
        if routes_to_call(request, enabled=self._setting("call_enabled"), epoch=self._epoch, truncated=truncated):
            # A chosen call still follows Hear It if the device is missing or the queue is full.
            self._monitor = take_id
            self.send(take_id)

    def handles_monitor(self, take_id: int) -> bool:
        """Should take_ready defer local playback to the output device's actual start?"""
        return self._monitor == take_id

    def claim_monitor(self, take_id: int, owner: object) -> bool:
        """Allow one connected player to audibly follow the outgoing stream."""
        if self.phase != "sending" or take_id != self.take_id:
            return False
        if self._monitor_owner is None:
            self._monitor_owner = owner
        return self._monitor_owner is owner

    def release_monitor(self, owner: object) -> None:
        if self._monitor_owner is owner:
            self._monitor_owner = None

    @property
    def hotkeys_suspended(self) -> bool:
        return bool(self._capture_owners)

    def set_hotkey_capture(self, owner: object, active: bool) -> None:
        suspended = self.hotkeys_suspended
        if active:
            self._capture_owners.add(owner)
        else:
            self._capture_owners.discard(owner)
        if suspended != self.hotkeys_suspended:
            self._core.events.notify()

    def send(self, take_id: int) -> bool:
        """Schedule a saved take and return immediately, even if another take is still sending."""
        if self._closed or not self._setting("call_enabled"):
            return self._refuse("Enable sending in Voice Chat first")
        endpoint = self._choice()
        if self._output is None or not (endpoint.id or endpoint.name):
            return self._refuse("Choose a call device in Voice Chat")
        if type(take_id) is not int or take_id <= 0 or self._core.takes.path_of(take_id) is None:
            return self._refuse(f"Take {take_id} is unavailable")
        now = self._core.scheduler.now()
        self._recent = {key: when for key, when in self._recent.items() if now - when <= 0.5}
        if take_id in self._recent:
            self._recent[take_id] = now
            return False
        if self.phase != "idle" and self._setting("call_overlap") == "cut":
            self.stop()
        if len(self._queue) >= 3:
            return self._refuse("Call queue full · 3 takes waiting")
        self._recent[take_id] = now
        self._queue.append(_Send(take_id, endpoint))
        self._advance()
        return True

    def _refuse(self, message: str) -> bool:
        self.error = message
        self._core.events.say(message, bad=True)
        self._core.events.notify()
        return False

    def _advance(self) -> None:
        if self.phase != "idle" or not self._queue or self._closed:
            return
        send = self._queue.popleft()
        self.take_id, self.error = send.take_id, ""
        self._monitor_owner = None
        self._cancel = threading.Event()
        delay = self._setting("call_delay_s")
        if delay:
            self.phase = "delayed"
            self._delay = self._core.scheduler.later(delay, lambda: self._open(send))
        else:
            self._open(send)
        self._core.events.notify()

    def _open(self, send: _Send) -> None:
        self._delay = None
        self.phase = "opening"
        self._task = self._core.scheduler.spawn(self._play(send, self._cancel), name="send saved take")
        self._core.events.notify()

    async def _play(self, send: _Send, cancel: threading.Event) -> None:
        loop = asyncio.get_running_loop()
        started = False

        def on_start() -> None:
            nonlocal started
            if self._cancel is cancel and not cancel.is_set() and not self._closed:
                started, self.phase = True, "sending"
                self._core.events.call_started.emit(send.take_id)
                self._core.events.notify()

        try:
            assert self._output is not None
            devices = tuple(await asyncio.to_thread(self._output.list_devices))
            if cancel.is_set():
                return
            self.devices = devices
            endpoint = resolve_endpoint(devices, send.endpoint)
            if endpoint is None:
                raise ValueError(f"Selected call device not found: {send.endpoint.name}")
            path = self._core.takes.path_of(send.take_id)
            if path is None or not await asyncio.to_thread(path.is_file):
                raise FileNotFoundError(f"Take {send.take_id} WAV is missing")
            if not cancel.is_set():
                await asyncio.to_thread(self._output.play, endpoint, path, cancel,
                                        lambda: loop.call_soon_threadsafe(on_start))
        except Exception as exc:
            if not cancel.is_set():
                self._refuse(f"Couldn't send Take {send.take_id}: {exc}")
        finally:
            cancel.set()
            self._task, self.take_id, self.phase = None, 0, "idle"
            self._monitor_owner = None
            if started:
                self._core.events.call_ended.emit(send.take_id)
            self._core.events.notify()
            self._advance()

    def stop(self) -> None:
        """Drop waiting sends and signal the open device immediately, including during a start delay."""
        self._queue.clear()
        self._cancel.set()
        if self._delay is not None:
            self._delay.cancel()
            self._delay, self.take_id, self.phase = None, 0, "idle"
        elif self._task is not None:
            self.phase = "stopping"
        self._core.events.notify()

    def close(self) -> None:
        self._closed = True
        self._capture_owners.clear()
        self.stop()
        self._core.settings.changed.unsubscribe(self._setting_changed)

    async def shutdown(self) -> None:
        """Release the open audio device before the server exits."""
        self.close()
        if self._task is not None:
            await asyncio.shield(self._task)
