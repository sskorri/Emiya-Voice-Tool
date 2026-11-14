"""Media routes (§4.16): takes, reference clips and staged clips, with Range support and JSON 404s."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from ui.core.media import ROUTES, mount_media

from tests.core.test_facade import Rig, rigs, speak  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def mounted(rig: Rig) -> httpx.AsyncClient:
    app = FastAPI()
    mount_media(app, rig.core)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://media.test")


@pytest.fixture
async def client(speak: Rig) -> AsyncIterator[httpx.AsyncClient]:
    async with mounted(speak) as http:
        yield http


def not_found(response: httpx.Response) -> bool:
    return response.status_code == 404 and response.json() == {"detail": "Not Found"}


def test_the_three_routes() -> None:
    assert ROUTES == ("/media/take/{take_id}", "/media/clip/{voice_id}", "/media/staged/{clip_id}")


# ---------- takes ----------

async def test_a_take_is_served_whole(speak: Rig, client: httpx.AsyncClient) -> None:
    path = speak.core.takes.path_of(7)
    response = await client.get(speak.core.take_url(7))
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store" and response.headers["accept-ranges"] == "bytes"
    assert response.content == path.read_bytes()


async def test_a_take_answers_range_requests(speak: Rig, client: httpx.AsyncClient) -> None:
    data = speak.core.takes.path_of(7).read_bytes()
    head = await client.get("/media/take/7", headers={"Range": "bytes=0-99"})
    assert head.status_code == 206
    assert head.headers["content-range"] == f"bytes 0-99/{len(data)}" and head.content == data[:100]
    tail = await client.get("/media/take/7", headers={"Range": "bytes=1000-"})
    assert tail.status_code == 206 and tail.content == data[1000:]
    beyond = await client.get("/media/take/7", headers={"Range": f"bytes={len(data) + 10}-"})
    assert beyond.status_code == 416


@pytest.mark.parametrize("take_id", ["999", "0", "abc", "-1", "7.0", "٧", "1" * 12])
async def test_an_unknown_take_is_a_json_404(client: httpx.AsyncClient, take_id: str) -> None:
    assert not_found(await client.get(f"/media/take/{take_id}"))


async def test_a_take_whose_wav_is_gone_is_a_json_404(speak: Rig, client: httpx.AsyncClient) -> None:
    speak.core.takes.path_of(6).unlink()
    assert not_found(await client.get("/media/take/6"))
    assert (await client.get("/media/take/5")).status_code == 200


# ---------- reference clips ----------

async def test_a_registered_voice_serves_its_clip(speak: Rig, client: httpx.AsyncClient) -> None:
    clip = speak.paths.voices / "aiko_ref.wav"
    url = speak.core.clip_url("aiko")
    assert url == f"/media/clip/aiko?v={os.stat(clip).st_mtime_ns}"
    response = await client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.content == clip.read_bytes()
    ranged = await client.get(url, headers={"Range": "bytes=10-19"})
    assert ranged.status_code == 206 and ranged.content == clip.read_bytes()[10:20]


@pytest.mark.parametrize("voice_id", ["zed", "CON", "aiko%5C..%5Cconfig", "..", "aiko.pt", "%20aiko", "a" * 65])
async def test_unregistered_ids_and_path_tricks_are_refused(client: httpx.AsyncClient, voice_id: str) -> None:
    assert not_found(await client.get(f"/media/clip/{voice_id}"))


async def test_a_path_with_slashes_never_reaches_a_file(client: httpx.AsyncClient) -> None:
    for path in ("/media/clip/../../config.py", "/media/clip/..%2F..%2Fconfig.py", "/media/clip/"):
        response = await client.get(path)
        assert response.status_code == 404
        assert b"DEFAULT_BACKEND" not in response.content


async def test_a_clip_that_is_missing_on_disk_is_a_404(rigs: Callable[..., Rig]) -> None:
    rig = rigs("voices_missing_files")
    await rig.start()
    assert rig.core.clip_url("aiko") == "/media/clip/aiko"
    async with mounted(rig) as http:
        assert not_found(await http.get("/media/clip/aiko"))
        assert (await http.get("/media/clip/marco")).status_code == 200


# ---------- staged clips and test lines ----------

async def test_a_staged_clip_and_a_test_line_are_served(rigs: Callable[..., Rig]) -> None:
    rig = rigs("forge_ready")
    await rig.start()
    core = rig.core
    clip = await asyncio.to_thread(core.clips.stage_file, rig.paths.voices / "sample.wav")
    test_line = core.clips.test_line_path("rin")
    test_line.parent.mkdir(parents=True, exist_ok=True)
    test_line.write_bytes((rig.paths.voices / "short_hello.wav").read_bytes())
    async with mounted(rig) as http:
        response = await http.get(core.staged_url(clip.clip_id))
        assert response.status_code == 200 and response.content == clip.staged.read_bytes()
        assert response.headers["content-type"] == "audio/wav"
        line = await http.get(core.staged_url("test-rin"))
        assert line.status_code == 200 and line.content == test_line.read_bytes()
        for clip_id in ("0" * 32, "test-zed", "test-..", "sample", clip.clip_id.upper()):
            assert not_found(await http.get(f"/media/staged/{clip_id}"))


async def test_media_urls(speak: Rig) -> None:
    core = speak.core
    assert core.take_url(7) == "/media/take/7"
    assert core.clip_url("zed") == "/media/clip/zed"
    assert core.staged_url("test-rin") == "/media/staged/test-rin"
    assert core.bump_staged("test-rin") == 1
    assert core.staged_url("test-rin") == "/media/staged/test-rin?v=1"
    core.bump_staged("test-rin")
    assert core.staged_url("test-rin") == "/media/staged/test-rin?v=2"
    assert core.staged_url("0" * 32) == "/media/staged/" + "0" * 32
