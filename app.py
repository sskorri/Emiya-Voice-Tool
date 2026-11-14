"""The HTTP API of the voice tool (FastAPI): /backends, /voices, /voices/{id}, /load_voice and /speak.

    uvicorn app:app --reload

It builds its own VoiceEngine on startup and shares cache/registry.json and the cross-process engine lock with
gui.py and cli.py: while another program holds a model in VRAM, /load_voice and /speak answer 503.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import config
from engine.engine import VoiceEngine
from engine.errors import EngineBusy, ScriptTooLong

# Built on startup (see lifespan), NOT at import time: importing this module
# must not load models or require CUDA (matters for tests, schema generation,
# and uvicorn --workers N spawning N import copies).
engine: VoiceEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = VoiceEngine(config)
    yield
    engine = None


app = FastAPI(title="Voice Tool", lifespan=lifespan)


class SpeakRequest(BaseModel):
    text: str
    voice_id: str
    language_id: str | None = None      # None -> use the voice's registered language
    cfg_weight: float = config.DEFAULT_CFG_WEIGHT
    exaggeration: float | None = None   # None -> use the voice's baked exaggeration


class LoadVoiceRequest(BaseModel):
    voice_id: str
    reference_wav: str
    backend: str = config.DEFAULT_BACKEND
    exaggeration: float = config.DEFAULT_EXAGGERATION
    language: str = config.DEFAULT_LANGUAGE
    force: bool = False


@app.get("/backends")
def list_backends():
    return {
        "backends": engine.list_backends(),
        "loaded": engine.list_loaded_backends(),
        "default": config.DEFAULT_BACKEND,
    }


@app.get("/voices")
def list_voices():
    return {"voices": engine.list_voices()}


@app.get("/voices/{voice_id}")
def voice_info(voice_id: str):
    info = engine.voice_info(voice_id)
    if info is None:
        raise HTTPException(404, f"Voice '{voice_id}' is not registered")
    return {"voice_id": voice_id, **info}


@app.post("/load_voice")
def load_voice(req: LoadVoiceRequest):
    path = Path(req.reference_wav)
    if not path.exists():
        raise HTTPException(404, f"Reference clip not found: {path}")
    try:
        engine.load_voice(
            req.voice_id,
            path,
            backend=req.backend,
            exaggeration=req.exaggeration,
            language=req.language,
            force=req.force,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except EngineBusy as e:      # another program holds the voice engine: try again once it is free
        raise HTTPException(503, str(e))
    return {"voice_id": req.voice_id, "backend": req.backend, "status": "cached"}


@app.post("/speak")
def speak(req: SpeakRequest):
    try:
        wav = engine.speak(
            req.text,
            req.voice_id,
            language_id=req.language_id,
            cfg_weight=req.cfg_weight,
            exaggeration=req.exaggeration,
        )
    except ScriptTooLong as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except EngineBusy as e:
        raise HTTPException(503, str(e))
    return Response(content=wav, media_type="audio/wav")
