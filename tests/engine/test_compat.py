"""app.py and cli.py keep working, unchanged, on the new engine (BUILD_SPEC §3.11).

cli.py runs in a child process, as it does for the user, against a scratch copy of the
config and with the shared fake factories, so no model loads and the real cache\\ is
never touched. app.py runs in-process under FastAPI's TestClient on the same scratch
config.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Iterable

import pytest

from engine.registry import VoiceRegistry
from tests.engine.conftest import write_clip

ROOT = Path(__file__).resolve().parents[2]
MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
TIMEOUT_S = 120
SCRATCH_KEYS = ("ROOT", "CACHE_DIR", "VOICES_DIR", "OUTPUTS_DIR", "DEVICE")

# Runs `cli.py <args>` exactly as `python cli.py` would, after pointing config at a scratch
# root and installing the fake factories; reports what was built and imported on its last line.
CLI_RUNNER = r"""
import json, sys
from pathlib import Path

import pytest

import config

root = Path(sys.argv[1])
config.ROOT = root
config.CACHE_DIR = root / "cache"
config.VOICES_DIR = root / "voices" / "reference"
config.OUTPUTS_DIR = root / "outputs"
config.DEVICE = "cpu"

from tests.engine.conftest import install_fake_factories

fakes = install_fake_factories(pytest.MonkeyPatch())

import cli

sys.argv = ["cli.py", *sys.argv[2:]]
try:
    cli.main()
finally:
    report = {"builds": fakes.builds, "torch": "torch" in sys.modules, "chatterbox": "chatterbox" in sys.modules}
    print("@@" + json.dumps(report), flush=True)
"""


def run_cli(root: Path, *args: str) -> tuple[int, str, str, dict[str, Any]]:
    """(exit code, stdout before the report, stderr, report) of one cli.py invocation."""
    done = subprocess.run(
        [sys.executable, "-c", CLI_RUNNER, str(root), *args], cwd=ROOT, capture_output=True,
        encoding="utf-8", timeout=TIMEOUT_S,
        env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"),
    )
    out, marker, report = done.stdout.rpartition("@@")
    assert marker, f"no report from the child:\n{done.stdout}\n{done.stderr}"
    return done.returncode, out, done.stderr, json.loads(report)


def seed_voice(root: Path, voice_id: str = "aiko") -> None:
    """A registered voice (as an older cli.py would write it), without any model."""
    VoiceRegistry(root / "cache" / "registry.json").register(
        voice_id, MTL, str(root / "voices" / "reference" / f"{voice_id}.wav"), 0.5, "ja")


# ---------- imports ----------

def test_app_and_cli_import_with_fake_factories(fake_factories) -> None:
    app = importlib.import_module("app")
    cli = importlib.import_module("cli")
    assert app.engine is None                                 # built in the lifespan, never at import
    assert cli.build_parser().parse_args(["backends"]).command == "backends"
    assert fake_factories.calls == {}


# ---------- cli.py ----------

def test_cli_list_info_and_backends_run_without_a_model(scratch_config) -> None:
    root = scratch_config.ROOT
    code, out, err, report = run_cli(root, "list")
    assert code == 0, err
    assert out.splitlines() == ["No voices registered yet. Run: python cli.py load <name> <wav>"]

    seed_voice(root)
    reports = [report]
    code, out, err, report = run_cli(root, "list")
    assert code == 0, err
    assert out.splitlines() == ["  aiko  [chatterbox-multilingual]"]
    reports.append(report)

    code, out, err, report = run_cli(root, "info", "aiko")
    assert code == 0, err
    info = json.loads(out)["aiko"]
    assert (info["backend"], info["language"]) == (MTL, "ja")
    assert {"created_at", "forged_at"} <= set(info)
    reports.append(report)

    code, out, err, report = run_cli(root, "info", "ghost")
    assert code == 1 and "Voice 'ghost' is not registered." in err
    reports.append(report)

    code, out, err, report = run_cli(root, "backends")
    assert code == 0, err
    assert out.splitlines() == ["Selectable backends (loaded on demand):", f"  {MTL}  (default)",
                                f"  {STD}", f"  {TUR}"]
    reports.append(report)

    assert all(r == {"builds": [], "torch": False, "chatterbox": False} for r in reports)
    assert not (root / "cache" / "engine.lock").exists()     # read-only commands never take the engine
    assert not (root / "cache" / "engine.owner.json").exists()


def test_cli_load_and_speak_with_fake_factories(scratch_config) -> None:
    root = scratch_config.ROOT
    clip = write_clip(root / "voices" / "reference" / "aiko.wav", 6.0)
    code, out, err, report = run_cli(root, "load", "aiko", str(clip), "--language", "ja")
    assert code == 0, err
    assert out.strip() == f"Registered 'aiko' on backend '{MTL}'"
    assert report["builds"] == [[MTL, "cpu"]] and not report["torch"]
    entry = VoiceRegistry(root / "cache" / "registry.json").get("aiko")
    assert (entry["reference_wav"], entry["language"]) == (str(clip.resolve()), "ja")
    assert {"created_at", "forged_at"} <= set(entry)
    assert (root / "cache" / MTL / "aiko.pt").exists()

    target = root / "outputs" / "hello.wav"
    code, out, err, report = run_cli(root, "speak", "aiko", "Hello there.", "--out", str(target))
    assert code == 0, err
    assert out.strip().startswith(f"Wrote {target} (")
    assert target.read_bytes()[:4] == b"RIFF"

    code, out, err, report = run_cli(root, "speak", "aiko", "   ")
    assert code == 1 and "Nothing to say: the text is empty." in err
    assert report["builds"] == []                            # refused before any load


# ---------- app.py ----------

@pytest.fixture
def app_client(scratch_config, fake_factories, monkeypatch) -> Iterable[Any]:
    """TestClient over app.app, its lifespan engine built on the scratch config."""
    with warnings.catch_warnings():   # starlette's notice about its httpx backend; nothing to act on here
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient

    import config

    for key in SCRATCH_KEYS:
        monkeypatch.setattr(config, key, getattr(scratch_config, key))
    app = importlib.import_module("app")
    with TestClient(app.app) as client:
        yield client
    assert app.engine is None


def test_app_routes_work_with_fake_factories(app_client, scratch_config, fake_factories) -> None:
    assert app_client.get("/backends").json() == {"backends": [MTL, STD, TUR], "loaded": [], "default": MTL}
    assert app_client.get("/voices").json() == {"voices": []}

    clip = write_clip(scratch_config.VOICES_DIR / "aiko.wav", 6.0)
    loaded = app_client.post("/load_voice", json={"voice_id": "aiko", "reference_wav": str(clip), "language": "ja"})
    assert loaded.status_code == 200
    assert loaded.json() == {"voice_id": "aiko", "backend": MTL, "status": "cached"}
    assert app_client.get("/backends").json()["loaded"] == [MTL]
    assert app_client.get("/voices").json() == {"voices": ["aiko"]}

    info = app_client.get("/voices/aiko").json()
    assert (info["voice_id"], info["reference_wav"], info["language"]) == ("aiko", str(clip.resolve()), "ja")
    assert {"created_at", "forged_at"} <= set(info)

    spoken = app_client.post("/speak", json={"text": "Hello there.", "voice_id": "aiko"})
    assert spoken.status_code == 200 and spoken.headers["content-type"] == "audio/wav"
    assert spoken.content[:4] == b"RIFF"
    assert fake_factories.model(MTL).generate_calls[-1][1] == {"language_id": "ja", "cfg_weight": 0.5,
                                                               "exaggeration": 0.5}


def test_app_keeps_its_error_mapping(app_client, scratch_config, fake_factories) -> None:
    short = write_clip(scratch_config.VOICES_DIR / "short.wav", 4.2)
    cases = [
        (("post", "/speak", {"text": "   ", "voice_id": "aiko"}), 404, "Nothing to say: the text is empty."),
        (("post", "/speak", {"text": "Hello.", "voice_id": "ghost"}), 404,
         "Voice 'ghost' is not registered. Call load_voice() first."),
        (("get", "/voices/ghost", None), 404, "Voice 'ghost' is not registered"),
        (("post", "/load_voice", {"voice_id": "bad name", "reference_wav": str(short)}), 400,
         "Invalid voice_id 'bad name': "),
        (("post", "/load_voice", {"voice_id": "rin", "reference_wav": str(scratch_config.ROOT / "gone.wav")}), 404,
         "Reference clip not found: "),
        (("post", "/load_voice", {"voice_id": "rin", "reference_wav": str(short), "backend": TUR}), 400,
         "Reference clip short.wav: chatterbox-turbo needs a reference clip longer than 5s (got 4.2s)."),
    ]
    for (method, path, body), status, detail in cases:
        response = getattr(app_client, method)(path, json=body) if body is not None else app_client.get(path)
        assert response.status_code == status, (path, response.text)
        assert response.json()["detail"].startswith(detail), (path, response.text)
    assert fake_factories.calls == {}                       # every refusal came before a model load


def test_speak_refuses_an_over_limit_script_without_breaking_the_next_request(app_client, scratch_config,
                                                                              fake_factories) -> None:
    clip = write_clip(scratch_config.VOICES_DIR / "aiko.wav", 6)
    assert app_client.post("/load_voice", json={"voice_id": "aiko", "reference_wav": str(clip)}).status_code == 200
    refused = app_client.post("/speak", json={"voice_id": "aiko", "text": "x" * 5000})
    assert refused.status_code == 422 and refused.json()["detail"] == "Script too long · 3,000 tokens, limit 2,048"
    assert fake_factories.model(MTL).generate_calls == []
    assert app_client.post("/speak", json={"voice_id": "aiko", "text": "Hello."}).status_code == 200
