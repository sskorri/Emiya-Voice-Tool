"""cli.py and app.py answer an engine refusal with one line, not a traceback (BUILD_SPEC §3.11, E4).

A validation error (ValueError), EngineBusy (another program holds the voice engine) and
BackendLoadError (the model failed to load) end `cli.py load` and `cli.py speak` with exit
code 1, never 3, and the error's one-line message on stderr. app.py answers EngineBusy with
HTTP 503 and the message; a failed load stays HTTP 500, as today.

cli.py runs in a child process, as the user runs it, on a scratch config with the shared
fake factories, while this test process holds the engine lock the way the GUI does.
app.py runs in-process under FastAPI's TestClient on the same scratch config. Nothing
loads a model or touches the real cache\\.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import subprocess
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.lock import EngineLock
from engine.registry import VoiceRegistry
from tests.engine.conftest import write_clip

ROOT = Path(__file__).resolve().parents[2]
MTL = "chatterbox-multilingual"
TIMEOUT_S = 120
SCRATCH_KEYS = ("ROOT", "CACHE_DIR", "VOICES_DIR", "OUTPUTS_DIR", "DEVICE")
BUSY_TAIL = "Close it, or wait for it to finish, then try again."
OOM = "CUDA out of memory. Tried to allocate 2.00 GiB."

# Runs `cli.py <args>` exactly as `python cli.py` would, on a scratch root with the fake
# factories. argv[2] is JSON {"fail_load" | "fail_bake": {backend: [error type, text]}}:
# that backend's next build, or the first bake on it, raises. The last stdout line
# reports the builds.
CLI_RUNNER = r"""
import json, sys
from pathlib import Path

import pytest

import config

root, setup = Path(sys.argv[1]), json.loads(sys.argv[2])
config.ROOT = root
config.CACHE_DIR = root / "cache"
config.VOICES_DIR = root / "voices" / "reference"
config.OUTPUTS_DIR = root / "outputs"
config.DEVICE = "cpu"

from engine import engine as engine_module
from tests.engine.conftest import install_fake_factories

ERRORS = {"RuntimeError": RuntimeError, "FileNotFoundError": FileNotFoundError}
fakes = install_fake_factories(pytest.MonkeyPatch())
for name, (kind, text) in setup.get("fail_load", {}).items():
    fakes.fail(name, ERRORS[kind](text))
for name, (kind, text) in setup.get("fail_bake", {}).items():
    def build(cache_dir, device, _build=engine_module._BACKEND_FACTORIES[name], _error=ERRORS[kind](text)):
        backend = _build(cache_dir, device)
        backend.model.bake_error = _error
        return backend
    engine_module._BACKEND_FACTORIES[name] = build

import cli

sys.argv = ["cli.py", *sys.argv[3:]]
try:
    cli.main()
finally:
    print("@@" + json.dumps({"builds": fakes.builds}), flush=True)
"""


def run_cli(root: Path, *args: str, **setup: Any) -> tuple[int, str, str, list[list[str]]]:
    """(exit code, stdout before the report, stderr, builds) of one `cli.py <args>` run."""
    done = subprocess.run(
        [sys.executable, "-c", CLI_RUNNER, str(root), json.dumps(setup), *args], cwd=ROOT,
        capture_output=True, encoding="utf-8", timeout=TIMEOUT_S,
        env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"),
    )
    out, marker, report = done.stdout.rpartition("@@")
    assert marker, f"no report from the child:\n{done.stdout}\n{done.stderr}"
    return done.returncode, out, done.stderr, json.loads(report)["builds"]


def cli_args(command: str, clip: Path, target: Path) -> list[str]:
    """`load` registers the new voice "aiko" from `clip`; `speak` asks the seeded "rin" for a take."""
    if command == "load":
        return ["load", "aiko", str(clip)]
    return ["speak", "rin", "Hello there.", "--out", str(target)]


def registry_ids(config: SimpleNamespace) -> list[str]:
    """The voice ids in the scratch registry."""
    return VoiceRegistry(config.CACHE_DIR / "registry.json").list_ids()


@contextlib.contextmanager
def held_engine(cache_dir: Path, program: str | None = "gui.py") -> Iterator[str]:
    """Hold cache_dir\\engine.lock as another program does; yields the message callers must show.

    program None removes the owner record, as when the holder's record cannot be read.
    """
    lock = EngineLock(cache_dir / "engine.lock", program=program or "gui.py")
    lock.acquire()
    try:
        if program is None:
            lock.owner_path.unlink(missing_ok=True)
            yield f"The voice engine is in use by another program. {BUSY_TAIL}"
        else:
            since = lock.holder()["since"]
            yield f"The voice engine is in use by {program} (pid {os.getpid()}) since {since}. {BUSY_TAIL}"
    finally:
        lock.release()


@pytest.fixture
def clip(scratch_config: SimpleNamespace) -> Path:
    """A 6 s reference clip; the voice "rin" is registered from it on Multilingual (no .pt, no model)."""
    path = write_clip(scratch_config.VOICES_DIR / "aiko.wav", 6.0)
    VoiceRegistry(scratch_config.CACHE_DIR / "registry.json").register("rin", MTL, str(path), 0.5, "en")
    return path


# ---------- cli.py ----------

@pytest.mark.parametrize("program", ["gui.py", None], ids=["named-holder", "unknown-holder"])
@pytest.mark.parametrize("command", ["load", "speak"])
def test_cli_exits_1_with_the_busy_message(scratch_config, clip, command, program) -> None:
    """`load` and `speak` while another program holds the engine: its one-line message, exit 1."""
    target = scratch_config.OUTPUTS_DIR / "hello.wav"
    with held_engine(scratch_config.CACHE_DIR, program) as message:
        code, out, err, builds = run_cli(scratch_config.ROOT, *cli_args(command, clip, target))
    assert (code, out, err) == (1, "", message + "\n")
    assert builds == [] and registry_ids(scratch_config) == ["rin"] and not target.exists()


def test_cli_list_info_and_backends_ignore_a_held_engine(scratch_config, clip) -> None:
    """The read-only commands never take the engine, so a holder never stops them."""
    commands = {"list": ["list"], "info": ["info", "rin"], "backends": ["backends"]}
    with held_engine(scratch_config.CACHE_DIR):
        runs = {name: run_cli(scratch_config.ROOT, *args) for name, args in commands.items()}
    assert all(code == 0 and err == "" and builds == [] for code, _, err, builds in runs.values()), runs
    assert runs["list"][1].splitlines() == [f"  rin  [{MTL}]"]
    assert json.loads(runs["info"][1])["rin"]["backend"] == MTL
    assert runs["backends"][1].splitlines()[:2] == ["Selectable backends (loaded on demand):",
                                                    f"  {MTL}  (default)"]


@pytest.mark.parametrize(("command", "error", "message"), [
    ("load", ["RuntimeError", f"{OOM}\nGPU 0 has a total capacity of 11.99 GiB of which 0 bytes is free."],
     f"{MTL} failed to load: {OOM}"),
    ("speak", ["FileNotFoundError", "t3_mtl23ls_v2.safetensors is not in the local cache"],
     f"{MTL} failed to load: t3_mtl23ls_v2.safetensors is not in the local cache"),
], ids=["load-out-of-memory", "speak-missing-files"])
def test_cli_exits_1_with_the_load_failure(scratch_config, clip, command, error, message) -> None:
    """A model that fails to load ends `load` and `speak` with the first line of the failure, exit 1."""
    target = scratch_config.OUTPUTS_DIR / "hello.wav"
    code, out, err, builds = run_cli(scratch_config.ROOT, *cli_args(command, clip, target),
                                     fail_load={MTL: error})
    assert (code, out, err) == (1, "", message + "\n")
    assert builds == [[MTL, "cpu"]] and registry_ids(scratch_config) == ["rin"] and not target.exists()
    assert not (scratch_config.CACHE_DIR / "engine.owner.json").exists()     # the engine was freed


def test_cli_load_exits_1_with_a_validation_message(scratch_config, clip) -> None:
    """A voice `load` refuses before any model loads (an invalid name) ends with that one line, exit 1."""
    from engine.backends.base import validate_voice_id

    with pytest.raises(ValueError) as refused:
        validate_voice_id("bad name")
    code, out, err, builds = run_cli(scratch_config.ROOT, "load", "bad name", str(clip))
    assert (code, out, err) == (1, "", f"{refused.value}\n")
    assert builds == [] and registry_ids(scratch_config) == ["rin"]


def test_cli_keeps_the_traceback_for_a_failed_bake(scratch_config, clip) -> None:
    """Only the two refusals become one line: a failed bake still ends in GenerationError's traceback."""
    failure = "CUDA error: device-side assert triggered\nCompile with `TORCH_USE_CUDA_DSA` to enable it."
    code, out, err, builds = run_cli(scratch_config.ROOT, "load", "aiko", str(clip),
                                     fail_bake={MTL: ["RuntimeError", failure]})
    lines = err.splitlines()
    assert (code, out, builds) == (1, "", [[MTL, "cpu"]])
    assert "Traceback (most recent call last):" in lines
    assert lines[-1] == "engine.errors.GenerationError: CUDA error: device-side assert triggered"
    assert registry_ids(scratch_config) == ["rin"]


# ---------- app.py ----------

@pytest.fixture
def app_client(scratch_config, fake_factories, monkeypatch) -> Iterator[Any]:
    """TestClient over app.app on the scratch config; an unhandled error comes back as HTTP 500."""
    with warnings.catch_warnings():   # starlette's notice about its httpx backend; nothing to act on here
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    import config

    for key in SCRATCH_KEYS:
        monkeypatch.setattr(config, key, getattr(scratch_config, key))
    app = importlib.import_module("app")
    with TestClient(app.app, raise_server_exceptions=False) as client:
        yield client
        app.engine.close()                 # frees the scratch engine lock before the lifespan ends


@pytest.mark.parametrize("program", ["gui.py", None], ids=["named-holder", "unknown-holder"])
def test_app_answers_503_with_the_busy_message(app_client, scratch_config, fake_factories, clip,
                                               program) -> None:
    """/load_voice and /speak while another program holds the engine: 503 and its message."""
    load = {"voice_id": "aiko", "reference_wav": str(clip)}
    with held_engine(scratch_config.CACHE_DIR, program) as message:
        refused = [app_client.post("/load_voice", json=load),
                   app_client.post("/speak", json={"text": "Hello there.", "voice_id": "rin"})]
        assert [(r.status_code, r.json()) for r in refused] == [(503, {"detail": message})] * 2
        assert app_client.get("/backends").json()["loaded"] == []
        assert app_client.get("/voices").json() == {"voices": ["rin"]}
    assert fake_factories.calls == {} and registry_ids(scratch_config) == ["rin"]
    assert app_client.post("/load_voice", json=load).status_code == 200      # free again: the same call works


@pytest.mark.parametrize("route", ["/load_voice", "/speak"])
def test_app_keeps_http_500_for_a_failed_load(app_client, scratch_config, fake_factories, clip,
                                              route) -> None:
    """A BackendLoadError is not a refusal to retry: it stays today's HTTP 500."""
    bodies = {"/load_voice": {"voice_id": "aiko", "reference_wav": str(clip)},
              "/speak": {"text": "Hello there.", "voice_id": "rin"}}
    fake_factories.fail(MTL, RuntimeError(OOM))
    response = app_client.post(route, json=bodies[route])
    assert (response.status_code, response.text) == (500, "Internal Server Error")
    assert fake_factories.calls == {MTL: 1} and app_client.get("/backends").json()["loaded"] == []
