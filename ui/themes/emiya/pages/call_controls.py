"""Shared manual-send controls for Speak and Takes."""
from collections.abc import Callable

from nicegui import binding
from nicegui.element import Element

from ui.contract.viewmodels import PlaybackController

from ..widgets import EmiyaButton


def send_button(playback: PlaybackController, take_id: Callable[[], int], marker: str) -> EmiyaButton:
    button = EmiyaButton("Send", size=26, aria="Send take to call",
                         on_click=lambda: playback.send_take(take_id())).mark(marker).classes("vt-call-send")
    binding.bind_from(button, "enabled", playback.vm, "call_available")
    return button


class TakeActions(Element):
    """The existing row toolbar. Create its Send button only while the user has enabled calls."""

    calls_enabled = binding.BindableProperty(on_change=lambda toolbar, enabled: toolbar._show_send(enabled))

    def __init__(self, playback: PlaybackController, take_id: int, marker: str) -> None:
        super().__init__("div")
        self._playback, self._take_id, self._marker = playback, take_id, marker
        self._send: EmiyaButton | None = None
        self.calls_enabled = False
        binding.bind_from(self, "calls_enabled", playback.vm, "call_enabled")

    def _show_send(self, enabled: bool) -> None:
        if enabled:
            with self:
                self._send = send_button(self._playback, lambda: self._take_id, self._marker)
            self._send.move(self, 0)
        elif self._send is not None:
            self._send.delete()
            self._send = None


def stop_button(playback: PlaybackController, marker: str) -> None:
    button = EmiyaButton("Stop Sending", size=26, on_click=playback.stop_sending).mark(marker)
    button.bind_visibility_from(playback.vm, "call_active")
