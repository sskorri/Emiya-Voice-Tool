"""Theme skeleton and asset checks (T0; BUILD_SPEC sections 6.1, 6.4, 6.6 and 8.7 "manifest and assets").

- ui\\themes\\emiya\\theme.toml is exactly section 6.1 and yields its window args and native run kwargs.
- Every file the theme references exists: the manifest's stylesheet and icon, the six page stylesheets
  that emiya.css imports first, url() targets in the theme's CSS, /theme/emiya paths and asset() names in
  the theme's Python, the section 6.6 images and the fonts folder.
- The copied images keep the byte counts, SHA-256 values, sizes and frames of section 6.6.
- icon.ico holds PNG frames 256, 48, 32, 24, 16 in that order, matches what tools\\make_icon.py builds,
  and Windows loads its first frame (the one NiceGUI's window icon uses) as 256x256.
- Importing ui.themes.emiya adds no route, builds no UI and imports no torch. PAGES maps every route to a
  lazy builder; a page module that does not exist yet renders "This page is not built yet." with a
  WARNING, never an ERROR, and every other failure propagates.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from ui.contract.manifest import WindowSpec, load_manifest, run_kwargs, window_args
from ui.contract.routes import PAGE_ID, ROUTES

ROOT = Path(__file__).resolve().parents[2]
THEME_DIR = ROOT / "ui" / "themes" / "emiya"
STATIC = THEME_DIR / "static"
ICON = STATIC / "icon.ico"
MAKE_ICON = ROOT / "tools" / "make_icon.py"
THEME_URL = "/theme/emiya/"
PAGES_PACKAGE = "ui.themes.emiya.pages"
MISSING_PAGE = f"{PAGES_PACKAGE}.zz_never_built"   # a page module that never exists
PLACEHOLDER_LOGGER = "voicetool.theme.emiya"
PROBE = "T0-PROBE "                                 # prefix of the JSON line a subprocess prints

SPEC_MANIFEST: dict[str, Any] = {                   # section 6.1, value for value
    "slug": "emiya", "name": "Emiya", "contract": [1, 0], "dark": True, "css": ["emiya.css"],
    "window": {"title": "Emiya Voice", "frameless": True, "size": [1280, 800], "min_size": [1280, 800],
               "resizable": False, "background_color": "#0D0E10", "text_select": False, "center": True,
               "icon": "static/icon.ico"},
    "palette": {"primary": "#FCDB7F", "dark_page": "#0D0E10", "negative": "#F0706E", "warning": "#F2C14E"},
    "fx": {"fps_cap": 60},
    "prefs": {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"},
}
PAGE_CSS = tuple(f"pages/{PAGE_ID[route]}.css" for route in ROUTES)
ASSETS: dict[str, tuple[int, str, int, int]] = {    # section 6.6: bytes, SHA-256, width, height
    "img/logo.gif": (59_497, "62E7922FDAEFF0F4C962E6E0C81E7BFB4F253D0939122A4620BD922168F8F985", 278, 278),
    "img/logo-still.png": (44_617, "0A902E4D6FB7639DB51759577179E377EC6545AFA3C7F6891214C5249CFC9636", 278, 278),
    "img/saber-art.png": (872_321, "82B218C868E4CA4A84E8CD1B580D96E1682175496671D8ACC1DA413841B10C44", 895, 893),
}
ICON_SIZES = (256, 48, 32, 24, 16)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?(['"]?)([^'")\s;]+)\1""")
CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]*)\1\s*\)""")
CSS_PRELUDE = re.compile(r"(?:\s*@(?:charset|import|layer)\b[^;{]*;)*")   # what may precede other rules
PY_THEME_PATH = re.compile(r"/theme/emiya/((?:[\w-]+/)*[\w.-]+\.\w+)")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


# ---- helpers -------------------------------------------------------------------------------------

def _run_python(code: str, tmp_path: Path) -> dict[str, Any]:
    """Run code in a fresh interpreter (offline, app folder, scratch NiceGUI storage); return its PROBE JSON.

    PYTEST_CURRENT_TEST stays set, as NiceGUI's user simulation needs: outside pytest its storage reset
    reaches for context.client and switches NiceGUI into script mode.
    """
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "NICEGUI_STORAGE_PATH": str(tmp_path / "nicegui-storage"),
           "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", __name__)}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith(PROBE)]
    assert lines, done.stdout + done.stderr
    return json.loads(lines[-1][len(PROBE):])


def _resolve(base: Path, ref: str) -> Path | None:
    """The file a theme reference names (relative to base, or /theme/emiya/...); None if it names no theme file."""
    ref = re.split(r"[?#]", ref.strip(), maxsplit=1)[0]
    if not ref or ref.lower().startswith("data:"):
        return None
    if ref.startswith(THEME_URL):
        return STATIC / ref[len(THEME_URL):]
    if ref.startswith("/"):
        return None                                   # another route: /media/..., /_nicegui/...
    return base / ref


def _missing(base: Path, refs: list[str]) -> list[str]:
    """The refs (sorted, once each) that do not name an existing file inside static."""
    bad = set()
    for ref in refs:
        target = _resolve(base, ref)
        if target is not None and not (target.resolve().is_relative_to(STATIC) and target.is_file()):
            bad.add(ref)
    return sorted(bad)


def _css_refs(text: str) -> list[str]:
    """Every url() and @import target in a stylesheet, comments removed."""
    text = CSS_COMMENT.sub("", text)
    return [match.group(2) for match in (*CSS_URL.finditer(text), *CSS_IMPORT.finditer(text))]


def _css_prelude(text: str) -> str:
    """The leading @charset, @import and @layer statements: all that may come before an @import."""
    match = CSS_PRELUDE.match(text)
    return match.group(0) if match else ""


def _asset_name(node: ast.AST) -> str | None:
    """The literal name in a call asset("img/x.png") or ctx.asset("img/x.png")."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func, first = node.func, node.args[0]
    named = (isinstance(func, ast.Name) and func.id == "asset") or (
        isinstance(func, ast.Attribute) and func.attr == "asset")
    return first.value if named and isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _python_refs(source: str) -> list[str]:
    """Static file names one theme module uses: /theme/emiya/<file> literals and asset("<file>") arguments."""
    tree = ast.parse(source)
    in_fstrings = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
    refs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in in_fstrings:
            refs += [match.group(1) for match in PY_THEME_PATH.finditer(node.value)]
        elif (name := _asset_name(node)) is not None:
            refs.append(name)
    return refs


def _image_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG IHDR chunk or a GIF logical screen descriptor."""
    if data.startswith(PNG_SIGNATURE) and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
    else:
        raise AssertionError("neither a PNG nor a GIF")
    return width, height


def _icon_frames(data: bytes) -> list[tuple[int, int, int, bytes]]:
    """(side, planes, bits per pixel, payload) per ICONDIRENTRY, in file order, after checking the layout."""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "not an ICO file"
    frames, expected = [], 6 + 16 * count
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        width, height, colours, zero, planes, bits, size, offset = entry
        assert (height, colours, zero, offset) == (width, 0, 0, expected), f"directory entry {index}"
        frames.append((width or 256, planes, bits, data[offset:offset + size]))
        expected = offset + size
    assert expected == len(data), "bytes after the last frame"
    return frames


def _pixels(png: bytes) -> tuple[tuple[int, int], str]:
    """Size and SHA-256 of the RGBA pixels of a PNG."""
    from PIL import Image
    with Image.open(io.BytesIO(png)) as image:
        return image.size, hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()


def _load_make_icon() -> ModuleType:
    """tools\\make_icon.py as a module (tools is not a package)."""
    spec = importlib.util.spec_from_file_location("t0_make_icon", MAKE_ICON)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _loaded_icon_size(path: Path, side: int) -> tuple[int, int]:
    """Size of the icon LoadImageW(path, IMAGE_ICON, side, side, LR_LOADFROMFILE) loads (0 = the first frame)."""
    import ctypes
    from ctypes import wintypes

    class IconInfo(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class Bitmap(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                    ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    user32, gdi32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32")
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(IconInfo)]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    icon = user32.LoadImageW(None, str(path), 1, side, side, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
    assert icon, f"LoadImageW failed with error {ctypes.get_last_error()}"
    info, bitmap = IconInfo(), Bitmap()
    try:
        assert user32.GetIconInfo(icon, ctypes.byref(info))
        assert gdi32.GetObjectW(info.hbmColor or info.hbmMask, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
    finally:
        for handle in (info.hbmColor, info.hbmMask):
            if handle:
                gdi32.DeleteObject(handle)
        user32.DestroyIcon(icon)
    return bitmap.bmWidth, bitmap.bmHeight


# ---- theme.toml ----------------------------------------------------------------------------------

def test_theme_toml_is_the_section_6_1_manifest() -> None:
    assert tomllib.loads((THEME_DIR / "theme.toml").read_text(encoding="utf-8")) == SPEC_MANIFEST


def test_load_manifest_accepts_the_theme_folder() -> None:
    m = load_manifest(THEME_DIR)
    assert (m.slug, m.name, m.contract, m.dark, m.css, m.fps_cap) == ("emiya", "Emiya", (1, 0), True,
                                                                       ("emiya.css",), 60)
    assert m.window == WindowSpec(title="Emiya Voice", frameless=True, size=(1280, 800), min_size=(1280, 800),
                                  resizable=False, background_color="#0D0E10", text_select=False, center=True,
                                  icon=ICON)
    assert dict(m.palette) == SPEC_MANIFEST["palette"]
    assert dict(m.prefs) == SPEC_MANIFEST["prefs"]
    assert (m.folder, m.static_dir, m.static_url) == (THEME_DIR, STATIC, "/theme/emiya")


def test_window_args_are_exact() -> None:
    assert window_args(load_manifest(THEME_DIR)) == {
        "resizable": False, "min_size": (1280, 800), "background_color": "#0D0E10", "text_select": False,
        "easy_drag": False}


def test_native_run_kwargs_are_exact() -> None:
    assert run_kwargs(load_manifest(THEME_DIR)) == {
        "native": True, "reload": False, "frameless": True, "window_size": (1280, 800), "dark": True,
        "title": "Emiya Voice", "favicon": str(ICON), "show_welcome_message": False, "reconnect_timeout": 10.0}


# ---- referenced files ----------------------------------------------------------------------------

def test_the_skeleton_files_exist() -> None:
    expected = ["emiya.css", "icon.ico", *PAGE_CSS, *ASSETS]
    assert [name for name in expected if not (STATIC / name).is_file()] == []
    assert (STATIC / "fonts").is_dir()


def test_emiya_css_imports_the_six_page_stylesheets_first() -> None:
    text = CSS_COMMENT.sub("", (STATIC / "emiya.css").read_text(encoding="utf-8"))
    prelude = _css_prelude(text)
    imports = [match.group(2) for match in CSS_IMPORT.finditer(prelude)]
    assert tuple(imports[:6]) == PAGE_CSS
    assert "@import" not in text[len(prelude):], "the browser ignores an @import that follows another rule"


def test_css_references_name_existing_static_files() -> None:
    missing = {}
    for sheet in sorted(STATIC.rglob("*.css")):
        if bad := _missing(sheet.parent, _css_refs(sheet.read_text(encoding="utf-8"))):
            missing[sheet.relative_to(STATIC).as_posix()] = bad
    assert missing == {}


def test_theme_code_names_existing_static_files() -> None:
    missing = {}
    for module in sorted(THEME_DIR.rglob("*.py")):
        parts = module.relative_to(THEME_DIR).parts
        if parts[0] == "tests" or "__pycache__" in parts:
            continue                                  # node tests and reference copies, never served
        if bad := _missing(STATIC, _python_refs(module.read_text(encoding="utf-8"))):
            missing[module.relative_to(ROOT).as_posix()] = bad
    assert missing == {}


def test_reference_scanners_find_what_they_should() -> None:
    """The scanners above flag broken references, so their passing on the tree means something."""
    source = ('A = "/theme/emiya/img/logo.gif?v=1"\nB = ctx.asset("img/nope.png")\nC = f"/theme/emiya/{x}.png"\n'
              'D = "see /theme/emiya/<path>"\n')
    assert sorted(_python_refs(source)) == ["img/logo.gif", "img/nope.png"]
    assert _missing(STATIC, _python_refs(source)) == ["img/nope.png"]
    css = ('@import url("pages/speak.css"); /* url(gone.png) */ a { background: url(../img/logo.gif) }\n'
           'b { mask: url("#m") } c { background: url(img/none.png) } d { cursor: url(../../theme.toml) }')
    assert _missing(STATIC / "pages", _css_refs(css)) == ["../../theme.toml", "img/none.png", "pages/speak.css"]
    assert _css_prelude(":root { } @import url('pages/speak.css');") == ""
    head = '@charset "utf-8";\n@import url(a.css);'
    assert _css_prelude(head + "\n:root { }") == head


# ---- images and icon -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(ASSETS))
def test_images_keep_their_section_6_6_bytes(name: str) -> None:
    size, sha256, width, height = ASSETS[name]
    data = (STATIC / name).read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest().upper()) == (size, sha256)
    assert _image_size(data) == (width, height)


def test_logo_gif_has_three_frames_of_70_ms() -> None:
    from PIL import Image
    with Image.open(STATIC / "img" / "logo.gif") as gif:
        durations = []
        for index in range(gif.n_frames):
            gif.seek(index)
            durations.append(gif.info.get("duration"))
    assert durations == [70, 70, 70]


def test_icon_frames_are_png_and_largest_first() -> None:
    frames = _icon_frames(ICON.read_bytes())
    assert [side for side, *_ in frames] == list(ICON_SIZES)
    for side, planes, bits, payload in frames:
        assert (planes, bits) == (1, 32)
        assert payload.startswith(PNG_SIGNATURE)
        assert _image_size(payload) == (side, side)
        assert payload[24:26] == b"\x08\x06", "8-bit RGBA"


@pytest.mark.skipif(sys.platform != "win32", reason="LoadImageW is Windows only")
def test_windows_loads_the_256_px_frame_first() -> None:
    assert _loaded_icon_size(ICON, 0) == (256, 256)   # NiceGUI's call: the file's first frame for both icons
    assert _loaded_icon_size(ICON, 32) == (32, 32)    # gui.py re-sends ICON_BIG at SM_CXICON
    assert _loaded_icon_size(ICON, 16) == (16, 16)    # and ICON_SMALL at SM_CXSMICON


def test_icon_is_what_make_icon_builds() -> None:
    from PIL import Image
    make_icon = _load_make_icon()
    assert make_icon.SIZES == ICON_SIZES
    with Image.open(STATIC / "img" / "logo-still.png") as still:   # content box (59, 21)-(220, 232)
        assert make_icon.content_square(still.convert("RGBA")) == (34, 21, 245, 232)   # a 211 px square
    built, shipped = _icon_frames(make_icon.build_icon()), _icon_frames(ICON.read_bytes())
    assert [frame[:3] for frame in built] == [frame[:3] for frame in shipped]
    assert [_pixels(frame[3]) for frame in built] == [_pixels(frame[3]) for frame in shipped]


# ---- the theme package and PAGES -----------------------------------------------------------------

IMPORT_PROBE = """\
import json, sys
from nicegui import Client, app, core

def routes():
    return [str(getattr(route, 'path', route)) for route in app.routes]

before, clients = routes(), len(Client.instances)
from ui.contract.manifest import load_manifest
from ui.contract.routes import ROUTES
from ui.contract.theme import Theme
from ui.themes.emiya import THEME
from ui.themes.emiya.pages import PAGES
banned = ('torch', 'chatterbox', 'engine', 'config', 'soundfile', 'send2trash',
          'ui.core', 'ui.behaviours', 'ui.launch', 'ui.entry')
print(PROBE + json.dumps({
    'routes_before': before, 'routes_after': routes(),
    'new_clients': len(Client.instances) - clients, 'script_mode': core.script_mode,
    'banned': sorted(m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in banned)),
    'is_theme': isinstance(THEME, Theme),
    'manifest': THEME.manifest == load_manifest(THEME.manifest.folder),
    'routes': list(THEME.pages) == list(ROUTES),
    'pages': dict(THEME.pages) == dict(PAGES),
}))
"""


def test_importing_the_theme_adds_no_route_ui_or_heavy_module(tmp_path: Path) -> None:
    result = _run_python(f"PROBE = {PROBE!r}\n{IMPORT_PROBE}", tmp_path)
    assert result.pop("routes_after") == result.pop("routes_before")
    assert result == {"new_clients": 0, "script_mode": False, "banned": [], "is_theme": True, "manifest": True,
                      "routes": True, "pages": True}


class _NoUI:
    """Stands in for nicegui.ui in this process: UI built outside a page would switch NiceGUI to script mode."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"ui.{name} was called outside a page")


@pytest.fixture
def pages() -> ModuleType:
    """The PAGES module, imported here (its parent package loads the real theme.toml)."""
    return importlib.import_module(PAGES_PACKAGE)


@pytest.fixture
def page_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: ModuleType) -> Iterator[Path]:
    """A folder on sys.path for throwaway t0_page_* modules; pages may build no UI meanwhile."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(pages, "ui", _NoUI())
    yield tmp_path
    for name in [name for name in sys.modules if name.startswith("t0_page_")]:
        del sys.modules[name]


def test_pages_maps_every_route_to_its_lazy_module(pages: ModuleType) -> None:
    assert tuple(pages.PAGES) == ROUTES
    assert all(isinstance(build, pages.LazyPage) for build in pages.PAGES.values())
    assert {route: build.module for route, build in pages.PAGES.items()} == {
        route: f"{PAGES_PACKAGE}.{PAGE_ID[route]}" for route in ROUTES}
    with pytest.raises(TypeError):
        pages.PAGES["/"] = pages.LazyPage(MISSING_PAGE)   # one mapping for every client: read-only


def test_lazy_page_runs_the_module_build(page_modules: Path, pages: ModuleType) -> None:
    (page_modules / "t0_page_ok.py").write_text("SEEN = []\n\n\ndef build(ctx):\n    SEEN.append(ctx)\n",
                                                encoding="utf-8")
    ctx = object()
    pages.LazyPage("t0_page_ok")(ctx)
    pages.LazyPage("t0_page_ok")(ctx)
    assert sys.modules["t0_page_ok"].SEEN == [ctx, ctx]


def test_lazy_page_lets_a_missing_dependency_fail(page_modules: Path, pages: ModuleType,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    (page_modules / "t0_page_broken.py").write_text("import t0_dependency_never_built\n", encoding="utf-8")
    with pytest.raises(ModuleNotFoundError) as caught:
        pages.LazyPage("t0_page_broken")(object())
    assert caught.value.name == "t0_dependency_never_built"   # the root shows "This page failed to build."
    assert [record for record in caplog.records if record.name == PLACEHOLDER_LOGGER] == []


def test_lazy_page_lets_build_errors_through(page_modules: Path, pages: ModuleType) -> None:
    (page_modules / "t0_page_raises.py").write_text("def build(ctx):\n    raise RuntimeError('boom')\n",
                                                    encoding="utf-8")
    with pytest.raises(RuntimeError, match="boom"):
        pages.LazyPage("t0_page_raises")(object())


class _Recorder(logging.Handler):
    """Keeps [level, logger, message] of every record that reaches the root logger."""

    def __init__(self, sink: list[list[str]]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append([record.levelname, record.name, record.getMessage()])


def _simulate_placeholder() -> None:
    """Subprocess body: render a missing page through the theme under NiceGUI's user simulation."""
    import asyncio

    from nicegui import ui
    from nicegui.testing import user_simulation

    from ui.themes.emiya import THEME
    from ui.themes.emiya.pages import NOT_BUILT_MARKER, LazyPage

    records: list[list[str]] = []
    logging.getLogger().addHandler(_Recorder(records))
    use_shell = not (THEME_DIR / "shell.py").is_file()   # T1's real shell needs a real core
    mounts: list[str] = []

    def mount_pages() -> None:
        mounts.append("mounted")
        LazyPage(MISSING_PAGE)(cast(Any, None))

    def root() -> None:
        if use_shell:
            THEME.build_shell(cast(Any, None), mount_pages)
        else:
            mount_pages()

    async def visit() -> list[str]:
        async with user_simulation(root=root) as user:
            await user.open("/")
            await user.should_see(marker=NOT_BUILT_MARKER)
            return [cast(ui.label, element).text for element in user.find(marker=NOT_BUILT_MARKER).elements]

    texts = asyncio.run(visit())
    print(PROBE + json.dumps({"texts": texts, "mounts": len(mounts), "use_shell": use_shell, "records": records}))


def test_a_missing_page_module_renders_the_placeholder_with_a_warning(tmp_path: Path) -> None:
    code = "from tests.rules.test_manifest_assets import _simulate_placeholder\n_simulate_placeholder()\n"
    result = _run_python(code, tmp_path)
    assert result["texts"] == ["This page is not built yet."]
    assert result["mounts"] == 1                      # the placeholder shell mounts the pages exactly once
    ours = [(level, message) for level, name, message in result["records"] if name == PLACEHOLDER_LOGGER]
    assert [level for level, _ in ours] == ["WARNING"]
    assert MISSING_PAGE in ours[0][1]
    assert [record for record in result["records"] if record[0] in ("ERROR", "CRITICAL")] == []
