"""Native interfaces shared by the UI layers (stdlib only).

Implemented by ui.kit.port.make_kit(browser) and injected into the BehaviourFactory; behaviour tests
pass a fake that records calls and returns scripted paths. Behaviours never import ui.kit (R1).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AudioEndpoint:
    id: str
    name: str


@dataclass(frozen=True)
class AudioDefault:
    flow: str
    role: str
    endpoint: AudioEndpoint


def resolve_endpoint(devices: Iterable[AudioEndpoint], saved: AudioEndpoint) -> AudioEndpoint | None:
    """Resolve the saved Windows ID, or a unique exact reported name after a driver reinstall."""
    devices = tuple(devices)
    by_id = [d for d in devices if saved.id and saved.id.casefold() == d.id.casefold()]
    if by_id:
        return by_id[0] if len(by_id) == 1 else None
    by_name = [d for d in devices if saved.name and saved.name == d.name]
    return by_name[0] if len(by_name) == 1 else None


def cable_default_roles(defaults: Iterable[AudioDefault]) -> tuple[AudioDefault, ...]:
    """Windows default routes whose endpoint carries a VB-CABLE playback/recording name."""
    prefixes = ("cable input", "cable output", "cable in 16ch", "cable out 16ch",
                "cable-a ", "cable-b ", "cable-c ", "cable-d ")
    return tuple(d for d in defaults if d.endpoint.name.casefold().startswith(prefixes) or
                 ("vb-audio" in d.endpoint.name.casefold() and "cable" in d.endpoint.name.casefold()))


@runtime_checkable
class AudioOutPort(Protocol):
    """Each blocking operation runs on a worker. The caller owns cancellation."""

    def list_devices(self) -> tuple[AudioEndpoint, ...]: ...

    def default_roles(self) -> tuple[AudioDefault, ...]: ...

    def play(self, endpoint: AudioEndpoint, path: Path, stop_event: Event, on_start: Callable[[], None]) -> None:
        """Send one WAV to the exact endpoint, notifying once when its first buffer is accepted."""
        ...


class HotkeyPort(Protocol):
    """Process-wide global bindings. Replacing the bindings invalidates already queued presses."""

    def set_bindings(self, bindings: Mapping[str, str]) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class KitPort(Protocol):
    """File dialogs, Explorer, quit and minimize."""

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """Native OPEN dialog for one file; None when cancelled."""
        ...

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        """Native FOLDER dialog; None when cancelled."""
        ...

    async def save_as(self, *, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """Native SAVE dialog; None when cancelled."""
        ...

    def reveal(self, path: Path) -> None:
        """Show the file in Explorer: explorer /select,"<path>"."""
        ...

    def open_folder(self, path: Path) -> None:
        """Open the folder in Explorer: os.startfile."""
        ...

    def quit(self) -> None:
        """Window-first quit (§6.2.3)."""
        ...

    def minimize(self) -> bool:
        """Minimize the window; False when there is no native window (--browser)."""
        ...
