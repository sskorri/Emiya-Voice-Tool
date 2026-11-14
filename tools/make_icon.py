"""Build ui\\themes\\emiya\\static\\icon.ico from the Saber Lion still frame (BUILD_SPEC section 6.6).

Run from the app folder:  venv\\Scripts\\python.exe tools\\make_icon.py

The still is cropped to a square around its content box (211 px), resized with LANCZOS and stored as PNG
frames 256, 48, 32, 24 and 16, in that order, in an ICO container written by hand. PIL's ICO writer always
sorts the frames smallest first, and NiceGUI loads the file's first frame for the window icon (LoadImageW
with size 0), so a PIL-written icon shows as 16x16; this one loads as 256x256. The app never runs this
tool: icon.ico is a build artefact, checked by tests\\rules\\test_manifest_assets.py.
"""
from __future__ import annotations

import io
import os
import struct
from collections.abc import Sequence
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ui" / "themes" / "emiya" / "static"
SOURCE = STATIC / "img" / "logo-still.png"
TARGET = STATIC / "icon.ico"
SIZES: tuple[int, ...] = (256, 48, 32, 24, 16)   # largest first: the frame NiceGUI loads
ICONDIR = struct.Struct("<HHH")                  # reserved, type (1 = icon), frame count
ICONDIRENTRY = struct.Struct("<BBBBHHII")        # width, height, colours, reserved, planes, bits, bytes, offset


def content_square(image: Image.Image) -> tuple[int, int, int, int]:
    """Crop box of the square that holds the visible content, centred on it (side = its longer edge)."""
    box = image.getbbox()
    if box is None:
        raise ValueError("the image has no visible content")
    left, top, right, bottom = box
    side = max(right - left, bottom - top)
    x0 = (left + right) // 2 - side // 2
    y0 = (top + bottom) // 2 - side // 2
    return x0, y0, x0 + side, y0 + side


def png_frame(square: Image.Image, size: int) -> bytes:
    """The square resized to size x size with LANCZOS, as PNG bytes without metadata chunks."""
    frame = square.resize((size, size), Image.Resampling.LANCZOS)
    frame.info.clear()
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    return buffer.getvalue()


def ico_container(frames: Sequence[tuple[int, bytes]]) -> bytes:
    """An ICO file holding the (size, PNG bytes) frames in the order given."""
    offset = ICONDIR.size + ICONDIRENTRY.size * len(frames)
    entries = []
    for size, data in frames:
        side = 0 if size >= 256 else size          # an ICONDIRENTRY stores 256 as 0
        entries.append(ICONDIRENTRY.pack(side, side, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    return ICONDIR.pack(0, 1, len(frames)) + b"".join(entries) + b"".join(data for _, data in frames)


def build_icon(source: Path = SOURCE, sizes: Sequence[int] = SIZES) -> bytes:
    """The icon file for source, with one frame per size in the order of sizes."""
    with Image.open(source) as image:
        still = image.convert("RGBA")
    square = still.crop(content_square(still))
    return ico_container([(size, png_frame(square, size)) for size in sizes])


def main() -> None:
    """Write TARGET (temporary file, then replace) and say what was written."""
    data = build_icon()
    partial = TARGET.with_name(TARGET.name + ".tmp")
    partial.write_bytes(data)
    os.replace(partial, TARGET)
    print(f"wrote {TARGET} ({len(data):,} bytes; frames {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
