"""Theme manifests: parse and check ui\\themes\\<slug>\\theme.toml, then derive the window and run arguments.

Stdlib only and free of side effects: gui.py and ui.entry import this module before NiceGUI, in the
server process and again in pywebview's window process. load_manifest() reads files and never writes.
"""
from __future__ import annotations

import os
import re
import socket
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ui.contract import CONTRACT_VERSION

MANIFEST_NAME = "theme.toml"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_WINDOW_PX = 10_000
MAX_FPS_CAP = 240
DEFAULT_FPS_CAP = 60
BROWSER_HOST = "127.0.0.1"         # never 0.0.0.0: NiceGUI binds all interfaces when no host is given
BROWSER_PORTS = (8080, 8999)
RECONNECT_TIMEOUT_S = 10.0         # provisional; NiceGUI's default is 3.0 (I3 measures the loop lag)
QUASAR_COLORS = frozenset({
    "primary", "secondary", "accent", "dark", "dark_page", "positive", "negative", "info", "warning",
})
TOP_KEYS = ("slug", "name", "contract", "dark", "css")
WINDOW_KEYS = ("title", "frameless", "size", "min_size", "resizable", "background_color", "text_select",
               "center", "icon")

_SLUG_RE = re.compile(r"[a-z][a-z0-9_]{1,30}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class ManifestError(ValueError):
    """theme.toml is missing, unreadable or breaks the theme contract."""


@dataclass(frozen=True)
class WindowSpec:
    """Window properties pywebview fixes when it creates the window (they change only with a restart)."""

    title: str
    frameless: bool
    size: tuple[int, int]           # logical px
    min_size: tuple[int, int]
    resizable: bool
    background_color: str           # upper-case #RRGGBB, painted before the page loads
    text_select: bool
    center: bool                    # gui.py centres the window on the work area; pywebview does not
    icon: Path                      # absolute .ico inside the theme folder: window, taskbar and favicon


@dataclass(frozen=True)
class ThemeManifest:
    """A checked theme.toml."""

    slug: str                       # equals the folder name: CSS scope, /theme/<slug>, component prefix
    name: str
    contract: tuple[int, int]
    dark: bool | None               # None = follow Windows ("auto")
    css: tuple[str, ...]            # stylesheets relative to static\, linked on every page
    window: WindowSpec
    palette: Mapping[str, str]      # Quasar brand colours for app.colors(**palette)
    fps_cap: int                    # [fx] fps_cap; 0 = every vsync (dev comparison only)
    prefs: Mapping[str, bool | int | str]   # [prefs]: defaults of the theme's own settings
    folder: Path                    # absolute theme folder

    @property
    def static_dir(self) -> Path:
        """Folder served at static_url."""
        return self.folder / "static"

    @property
    def static_url(self) -> str:
        """URL prefix of the theme's static files."""
        return f"/theme/{self.slug}"


def load_manifest(folder: str | os.PathLike[str]) -> ThemeManifest:
    """Parse and check <folder>\\theme.toml; raise ManifestError for any problem."""
    root = Path(folder).resolve()
    path = root / MANIFEST_NAME
    data = _parse(path)
    _require(path, data, TOP_KEYS, "")
    slug = _slug(path, data, root)
    return ThemeManifest(
        slug=slug,
        name=_text(path, data, "name", ""),
        contract=_contract(path, data),
        dark=_dark(path, data),
        css=_css(path, data, (root / "static").resolve()),
        window=_window(path, data, root),
        palette=_palette(path, data, slug),
        fps_cap=_fps_cap(path, data),
        prefs=_prefs(path, data),
        folder=root,
    )


def window_args(m: ThemeManifest) -> dict[str, Any]:
    """Keyword arguments for app.native.window_args (picklable; both processes compute the same dict)."""
    return {
        "resizable": m.window.resizable,
        "min_size": m.window.min_size,
        "background_color": m.window.background_color,
        "text_select": m.window.text_select,
        "easy_drag": False,
    }


def run_kwargs(m: ThemeManifest, *, browser: bool = False) -> dict[str, Any]:
    """Keyword arguments for ui.run: the native window, or (--browser) a tab served on loopback only."""
    if browser:
        return {
            "native": False,
            "reload": False,
            "show": True,
            "host": BROWSER_HOST,
            "port": _free_port(BROWSER_HOST, *BROWSER_PORTS),
            "dark": m.dark,
            "title": m.window.title,
            "favicon": str(m.window.icon),
        }
    return {
        "native": True,
        "reload": False,
        "frameless": m.window.frameless,
        "window_size": m.window.size,
        "dark": m.dark,
        "title": m.window.title,
        "favicon": str(m.window.icon),
        "show_welcome_message": False,
        "reconnect_timeout": RECONNECT_TIMEOUT_S,
    }


def _free_port(host: str, first: int, last: int) -> int:
    """First port in [first, last] that can be bound exclusively on host."""
    for port in range(first, last + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port on {host} between {first} and {last}")


# ---- reading -------------------------------------------------------------------------------------

def _fail(path: Path, message: str) -> ManifestError:
    return ManifestError(f"{path}: {message}")


def _show(value: object) -> str:
    """A short repr for error messages."""
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _read_text(path: Path) -> str:
    """The file as text: at most MAX_MANIFEST_BYTES of UTF-8 (a BOM is tolerated), with no URL in it."""
    try:
        if not path.is_file():
            raise ManifestError(f"{path} is not a file" if path.exists() else f"{path} not found")
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise ManifestError(f"{path} cannot be read: {error}") from error
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _fail(path, f"larger than {MAX_MANIFEST_BYTES // 1024} KiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _fail(path, f"not UTF-8 ({error.reason} at byte {error.start})") from error
    if _URL_RE.search(text):
        raise _fail(path, "holds an http(s) URL; a theme ships every file it uses")
    return text


def _parse(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError as error:
        raise _fail(path, f"not valid TOML: {error}") from error


# ---- typed fields --------------------------------------------------------------------------------

def _require(path: Path, table: Mapping[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [key for key in keys if key not in table]
    if missing:
        raise _fail(path, f"{where}missing {', '.join(missing)}")


def _table(path: Path, data: Mapping[str, Any], key: str, *, required: bool) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise _fail(path, f"missing [{key}] table" if value is None else f"[{key}] must be a table")
    return value


def _text(path: Path, table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, f"{where}{key} must be a non-empty string, got {_show(value)}")
    return value


def _flag(path: Path, table: Mapping[str, Any], key: str, where: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise _fail(path, f"{where}{key} must be true or false, got {_show(value)}")
    return value


def _whole(value: object, low: int, high: int) -> bool:
    """A TOML integer (never a bool) inside [low, high]."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _pair(path: Path, table: Mapping[str, Any], key: str, where: str) -> tuple[int, int]:
    value = table.get(key)
    if not (isinstance(value, list) and len(value) == 2 and all(_whole(v, 1, MAX_WINDOW_PX) for v in value)):
        raise _fail(path, f"{where}{key} must be two whole numbers from 1 to {MAX_WINDOW_PX}, got {_show(value)}")
    return value[0], value[1]


def _colour(path: Path, value: object, where: str) -> str:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise _fail(path, f"{where} must be #RRGGBB, got {_show(value)}")
    return value.upper()


def _file_inside(path: Path, base: Path, value: object, where: str, suffix: str) -> Path:
    """Resolve a relative file name that must stay inside base and name an existing <suffix> file."""
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, f"{where} must be a relative file name, got {_show(value)}")
    if Path(value).anchor:
        raise _fail(path, f"{where} {value!r} must be relative to {base}")
    target = (base / value).resolve()
    if not target.is_relative_to(base):
        raise _fail(path, f"{where} {value!r} leaves {base}")
    if target.suffix.lower() != suffix:
        raise _fail(path, f"{where} {value!r} must be a {suffix} file")
    if not target.is_file():
        raise _fail(path, f"{where} {value!r} is missing ({target})")
    return target


# ---- sections ------------------------------------------------------------------------------------

def _slug(path: Path, data: Mapping[str, Any], root: Path) -> str:
    slug = data.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise _fail(path, f"slug must be 2-31 lower-case letters, digits or _ starting with a letter, "
                          f"got {_show(slug)}")
    if slug != root.name:
        raise _fail(path, f"slug {slug!r} must equal the folder name {root.name!r}")
    return slug


def _contract(path: Path, data: Mapping[str, Any]) -> tuple[int, int]:
    value = data.get("contract")
    if not (isinstance(value, list) and len(value) == 2 and all(_whole(v, 0, 999) for v in value)):
        raise _fail(path, f"contract must be [major, minor], got {_show(value)}")
    if value[0] != CONTRACT_VERSION[0]:
        raise _fail(path, f"contract {value[0]}.{value[1]} does not match this app's {CONTRACT_VERSION[0]}.x")
    return value[0], value[1]


def _dark(path: Path, data: Mapping[str, Any]) -> bool | None:
    value = data.get("dark")
    if value == "auto":
        return None
    if not isinstance(value, bool):
        raise _fail(path, f'dark must be true, false or "auto", got {_show(value)}')
    return value


def _css(path: Path, data: Mapping[str, Any], static: Path) -> tuple[str, ...]:
    value = data.get("css")
    if not isinstance(value, list) or not value:
        raise _fail(path, f"css must be a non-empty list of stylesheets in static, got {_show(value)}")
    names: list[str] = []
    for item in value:
        _file_inside(path, static, item, "css entry", ".css")
        if item in names:
            raise _fail(path, f"css lists {item!r} twice")
        names.append(item)
    return tuple(names)


def _window(path: Path, data: Mapping[str, Any], root: Path) -> WindowSpec:
    table = _table(path, data, "window", required=True)
    where = "[window] "
    _require(path, table, WINDOW_KEYS, where)
    size = _pair(path, table, "size", where)
    min_size = _pair(path, table, "min_size", where)
    if min_size[0] > size[0] or min_size[1] > size[1]:
        raise _fail(path, f"{where}min_size {min_size} exceeds size {size}")
    return WindowSpec(
        title=_text(path, table, "title", where),
        frameless=_flag(path, table, "frameless", where),
        size=size,
        min_size=min_size,
        resizable=_flag(path, table, "resizable", where),
        background_color=_colour(path, table.get("background_color"), f"{where}background_color"),
        text_select=_flag(path, table, "text_select", where),
        center=_flag(path, table, "center", where),
        icon=_file_inside(path, root, table.get("icon"), f"{where}icon", ".ico"),
    )


def _palette(path: Path, data: Mapping[str, Any], slug: str) -> dict[str, str]:
    palette: dict[str, str] = {}
    for key, value in _table(path, data, "palette", required=False).items():
        if not _KEY_RE.fullmatch(key) or (key not in QUASAR_COLORS and not key.startswith(f"{slug}_")):
            raise _fail(path, f"[palette] {key!r} is neither a Quasar colour nor named {slug}_*")
        palette[key] = _colour(path, value, f"[palette] {key}")
    return palette


def _fps_cap(path: Path, data: Mapping[str, Any]) -> int:
    value = _table(path, data, "fx", required=False).get("fps_cap", DEFAULT_FPS_CAP)
    if not _whole(value, 0, MAX_FPS_CAP):
        raise _fail(path, f"[fx] fps_cap must be a whole number from 0 to {MAX_FPS_CAP}, got {_show(value)}")
    return value


def _prefs(path: Path, data: Mapping[str, Any]) -> dict[str, bool | int | str]:
    prefs: dict[str, bool | int | str] = {}
    for key, value in _table(path, data, "prefs", required=False).items():
        if not _KEY_RE.fullmatch(key):
            raise _fail(path, f"[prefs] key {key!r} must be lower-case letters, digits and _")
        if not isinstance(value, (bool, int, str)):
            raise _fail(path, f"[prefs] {key} must be true/false, a whole number or a string, got {_show(value)}")
        prefs[key] = value
    return prefs
