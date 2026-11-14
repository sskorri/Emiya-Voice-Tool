"""Media routes (§4.16): takes, reference clips and staged clips, served as files with Range support.

Starlette's FileResponse answers Range requests (206), so the player can seek. Routes look files up
from the core's own records only: a take id in the takes store, a registered voice's resolved clip,
a clip id the ClipService staged (or test-<voice>). A request never names a path, and anything else
is a 404 with a JSON body. Not app.add_media_files: the Output Folder can move.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.responses import FileResponse, JSONResponse, Response

from engine.backends.base import voice_id_error

if TYPE_CHECKING:
    from . import Core

__all__ = ["MEDIA_TYPES", "ROUTES", "mount_media"]

MEDIA_TYPES = {".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".mp3": "audio/mpeg"}
ROUTES = ("/media/take/{take_id}", "/media/clip/{voice_id}", "/media/staged/{clip_id}")
_TAKE_ID = re.compile(r"[0-9]{1,9}")
_HEADERS = {"Cache-Control": "no-store"}


def mount_media(app: Any, core: Core) -> None:
    """Add the three GET routes to app (NiceGUI's app, or any FastAPI app in tests)."""

    async def take(take_id: str) -> Response:
        return take_response(core, take_id)

    async def clip(voice_id: str) -> Response:
        return clip_response(core, voice_id)

    async def staged(clip_id: str) -> Response:
        return staged_response(core, clip_id)

    for path, endpoint in zip(ROUTES, (take, clip, staged)):
        app.add_api_route(path, endpoint, methods=["GET"], include_in_schema=False)


def take_response(core: Core, take_id: str) -> Response:
    """A take's WAV; 404 for an unknown id or a missing file."""
    if not _TAKE_ID.fullmatch(take_id):
        return _not_found()
    return _file(core.takes.path_of(int(take_id)))


def clip_response(core: Core, voice_id: str) -> Response:
    """A registered voice's resolved reference clip; 404 for anything else."""
    if voice_id_error(voice_id) is not None:
        return _not_found()
    row = next((r for r in core.state.library.rows if r.voice_id == voice_id), None)
    return _file(row.reference_path if row is not None else None)


def staged_response(core: Core, clip_id: str) -> Response:
    """A clip staged for Forge (uuid4 hex) or a test line (test-<voice>); 404 for anything else."""
    return _file(core.clips.staged_path(clip_id))


def _file(path: Path | None) -> Response:
    if path is None or not Path(path).is_file():
        return _not_found()
    path = Path(path)
    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                        headers=dict(_HEADERS))


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers=dict(_HEADERS))
