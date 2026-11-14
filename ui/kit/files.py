"""File dialogs and Explorer for the behaviours (through KitPort), and the --browser stand-ins.

Native dialogs call app.native.main_window.create_file_dialog behind window.native_lock. pywebview
checks the filters in the window process, where a filter it rejects raises and the request never
gets an answer (nicegui/native/native_mode.py L129-135), which would hold the lock for good; so
check_filters() applies the same rule (webview/util.py L155-165) here first. pywebview 6.2.1 dialogs
take no title: `title` is shown only by the --browser upload dialog.

--browser has no native window. BrowserFiles asks for an upload in a stock dialog in place of OPEN.
In place of SAVE it hands out a scratch path and, when the calling task ends, sends the file the
caller wrote there as a download. Browsers cannot pick folders, so there is no FOLDER stand-in.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from nicegui import app, ui
from nicegui.slot import Slot, get_task_id

from .window import native_lock

if TYPE_CHECKING:
    from nicegui.client import Client
    from nicegui.events import UploadEventArguments

OPEN_DIALOG, FOLDER_DIALOG, SAVE_DIALOG = 10, 20, 30      # webview.FileDialog in pywebview 6.2.1
FILTER_RE = re.compile(r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$")   # webview/util.py L160
MAX_NAME_CHARS = 120
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                             *(f"LPT{i}" for i in range(1, 10))})

log = logging.getLogger("voicetool.kit")
_scratch_root: Path | None = None


def check_filters(filters: Sequence[str]) -> None:
    """Raise ValueError for a filter pywebview would reject.

    The form is "Description (*.ext;*.ext)"; the description holds only letters, digits, _ and
    spaces (no commas, dashes or parentheses).
    """
    for item in filters:
        if not isinstance(item, str) or not FILTER_RE.search(item):
            raise ValueError(f"{item!r} is not a file filter of the form 'Description (*.ext;*.ext)'")


async def pick_file(*, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
    """Native OPEN dialog for one file; None when cancelled or without a native window."""
    found = await _native_dialog(OPEN_DIALOG, start_dir=start_dir, filters=filters)
    return Path(found) if found else None


async def pick_folder(*, title: str, start_dir: Path | None) -> Path | None:
    """Native FOLDER dialog; None when cancelled or without a native window."""
    found = await _native_dialog(FOLDER_DIALOG, start_dir=start_dir)
    return Path(found) if found else None


async def save_as(*, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
    """Native SAVE dialog; None when cancelled or without a native window."""
    found = await _native_dialog(SAVE_DIALOG, start_dir=start_dir, filters=filters, save_name=default_name)
    return Path(found) if found else None


def reveal(path: Path) -> None:
    """Show the file selected in Explorer (explorer /select,"<path>")."""
    target = Path(os.path.abspath(path))
    if not target.exists():
        raise FileNotFoundError(f"Not found: {target}")
    os.startfile(_explorer(), arguments=f'/select,"{target}"')


def open_folder(path: Path) -> None:
    """Open the folder in Explorer (os.startfile)."""
    target = Path(os.path.abspath(path))
    if not target.is_dir():
        raise NotADirectoryError(f"Not a folder: {target}")
    os.startfile(str(target))


def accept_of(filters: Sequence[str]) -> str:
    """The upload's accept list (".wav,.mp3") for the filters; "" when one of them allows any file."""
    suffixes: list[str] = []
    for item in filters:
        match = FILTER_RE.search(item)
        for pattern in match.group(2).split(";") if match else ():
            suffix = pattern[1:].lower()                   # "*.wav" -> ".wav"
            if not suffix or "*" in suffix:
                return ""
            if suffix not in suffixes:
                suffixes.append(suffix)
    return ",".join(suffixes)


def safe_name(name: str) -> str:
    """A plain Windows file name from a browser-supplied one ("upload" when nothing usable is left)."""
    base = _UNSAFE_CHARS.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    if len(base) > MAX_NAME_CHARS:
        stem, suffix = os.path.splitext(base)
        keep = MAX_NAME_CHARS - len(suffix)
        base = stem[:keep] + suffix if keep > 0 else base[:MAX_NAME_CHARS]
    if not base:
        return "upload"
    return f"_{base}" if base.split(".")[0].upper() in _RESERVED_NAMES else base


class BrowserFiles:
    """The --browser stand-ins for OPEN and SAVE; their files live in a scratch folder."""

    def __init__(self, scratch: Path | None = None) -> None:
        """:param scratch: parent of the per-call folders (default: one TEMP folder, removed at exit)"""
        self._scratch = scratch

    async def pick_file(self, *, title: str, filters: tuple[str, ...]) -> Path | None:
        """Ask for one upload in a stock dialog; the uploaded file's path, or None on Cancel."""
        check_filters(filters)
        client = _current_client()
        if client is None:
            return None
        folder = self._new_folder()
        with client:
            dialog = ui.dialog()
            with dialog, ui.card():
                ui.label(title)
                upload = ui.upload(auto_upload=True, max_files=1,
                                   on_upload=lambda event: _receive(event, folder, dialog))
                accept = accept_of(filters)
                if accept:
                    upload.props(f'accept="{accept}"')
                ui.button("Cancel", on_click=lambda: dialog.submit(None))
        try:
            return await dialog
        finally:
            if not dialog.is_deleted:
                dialog.delete()

    def save_as(self, *, default_name: str, filters: tuple[str, ...] = ()) -> Path | None:
        """A scratch path for the caller to write; its file is downloaded when the calling task ends."""
        check_filters(filters)
        client = _current_client()
        task = _current_task()
        if client is None or task is None:
            return None
        target = self._new_folder() / safe_name(default_name)
        task.add_done_callback(lambda _done: _deliver(client, target))
        return target

    def _new_folder(self) -> Path:
        root = self._scratch or _default_scratch()
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=root))


async def _native_dialog(kind: int, *, start_dir: Path | None, filters: Sequence[str] = (),
                         save_name: str = "") -> str | None:
    """The path the native dialog returns; None when cancelled or without a native window."""
    check_filters(filters)
    window = app.native.main_window
    if window is None:
        return None
    async with native_lock:
        result = await window.create_file_dialog(
            dialog_type=kind, directory=str(start_dir) if start_dir else "", allow_multiple=False,
            save_filename=save_name, file_types=tuple(filters))
    if isinstance(result, str):
        return result or None
    return str(result[0]) if result else None


async def _receive(event: UploadEventArguments, folder: Path, dialog: ui.dialog) -> None:
    target = folder / safe_name(event.file.name)
    await event.file.save(target)
    dialog.submit(target)


def _deliver(client: Client, target: Path) -> None:
    """Send the file the caller wrote as a download, then drop its scratch folder."""
    try:
        if target.is_file() and not client.is_deleted:
            client.download(target.read_bytes(), target.name, mimetypes.guess_type(target.name)[0] or "")
        else:
            log.info("Nothing to download for %s", target.name)
    except OSError as error:
        log.warning("Couldn't send %s as a download: %s", target.name, error)
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


def _current_client() -> Client | None:
    """The client of the current UI context; None outside one (never enters NiceGUI's script mode)."""
    stack = Slot.stacks.get(get_task_id())
    if not stack:
        return None
    try:
        return stack[-1].parent.client
    except RuntimeError:
        return None


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _default_scratch() -> Path:
    """One TEMP folder per process for --browser files, removed at exit."""
    global _scratch_root  # noqa: PLW0603
    if _scratch_root is None:
        _scratch_root = Path(tempfile.mkdtemp(prefix="voicetool-browser-"))
        atexit.register(shutil.rmtree, _scratch_root, ignore_errors=True)
    return _scratch_root


def _explorer() -> str:
    """explorer.exe by full path, so no explorer.exe in the app or working folder can run instead."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
