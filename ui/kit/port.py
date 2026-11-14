"""make_kit(browser): the KitPort the behaviours receive, built from ui.kit.window and ui.kit.files."""
from __future__ import annotations

from pathlib import Path

from ui.contract.ports import KitPort

from . import files, window


class NativeKit:
    """KitPort for the app window: native dialogs, Explorer, window-first quit.

    Safe without a native window (the user fixture): dialogs return None, minimize() returns False.
    """

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """Native OPEN dialog for one file; None when cancelled."""
        return await files.pick_file(title=title, filters=filters, start_dir=start_dir)

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        """Native FOLDER dialog; None when cancelled."""
        return await files.pick_folder(title=title, start_dir=start_dir)

    async def save_as(self, *, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """Native SAVE dialog; None when cancelled."""
        return await files.save_as(default_name=default_name, filters=filters, start_dir=start_dir)

    def reveal(self, path: Path) -> None:
        """Show the file in Explorer: explorer /select,"<path>"."""
        files.reveal(path)

    def open_folder(self, path: Path) -> None:
        """Open the folder in Explorer: os.startfile."""
        files.open_folder(path)

    def quit(self) -> None:
        """Window-first quit (§6.2.3)."""
        window.quit_app()

    def minimize(self) -> bool:
        """Minimize the window; False when there is no native window."""
        return window.minimize()


class BrowserKit(NativeKit):
    """KitPort for --browser: an upload stands in for OPEN, a download for SAVE; no window to minimize."""

    def __init__(self, browser_files: files.BrowserFiles | None = None) -> None:
        self._files = browser_files or files.BrowserFiles()

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """Upload one file in a stock dialog; None when cancelled."""
        return await self._files.pick_file(title=title, filters=filters)

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        """Always None: a browser cannot pick a folder (the pages disable these controls)."""
        return None

    async def save_as(self, *, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        """A scratch path to write; the file is downloaded when the calling task ends."""
        return self._files.save_as(default_name=default_name, filters=filters)

    def minimize(self) -> bool:
        """Always False: --browser has no native window."""
        return False


def make_kit(browser: bool = False) -> KitPort:
    """The process-wide KitPort: BrowserKit under --browser, else NativeKit."""
    return BrowserKit() if browser else NativeKit()
