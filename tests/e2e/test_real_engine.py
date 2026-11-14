"""§8.9: the real-engine end-to-end run (I3). `pytest -m gpu tests\\e2e\\test_real_engine.py` (about 3-5 minutes).

gui.py runs under the NiceGUI user fixture with VOICETOOL_ENGINE=real on a scratch data root (tests\\e2e\\conftest.py):
the controllers, core and the real engine together, driven through markers with no window. Steps 1-11 of §8.9, and
after every step the "all" row: never two models resident (a factory spy and a sampler thread), every load_started
caused by the named control the step clicked, every unloaded event (evictions included) with allocated < 0.2 GiB,
the six pages visited without a load, and the event-loop lag recorded. Steps 9 and 10 use send2trash (the user's
Recycle Bin, as the real app does); the files are this run's scratch takes and .pt files.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from tests.e2e.conftest import (ENGINE_LOCK, FREED_LIMIT, GIB, MTL, STD, TUR, Rig, cuda_bytes, cuda_initialized,
                                nvsmi_used, seed_everything)

pytestmark = [pytest.mark.gpu, pytest.mark.trash("recycle")]

HELLO = "Hello there."
JAPANESE = "こんにちは。テストです。"
STANDARD_LINE = "Standard model speaking with guidance at zero."
TURBO_LINE = "Okay, okay [chuckle] it works."
VOICES = ("e2e_mtl", "e2e_std", "e2e_tur")


def wav_facts(path) -> dict:
    import soundfile as sf                      # here, so the default run's collection never imports it

    data = path.read_bytes()
    info = sf.info(io.BytesIO(data))
    return {"riff": data[:4] == b"RIFF" and data[8:12] == b"WAVE", "sample_rate": info.samplerate,
            "channels": info.channels, "seconds": round(info.duration, 3), "subtype": info.subtype, "bytes": len(data)}


def take_wav(rig: Rig, take_id: int):
    path = rig.core.takes.path_of(take_id)
    assert path is not None and path.is_file(), f"take {take_id} has no WAV"
    return path


def registry(rig: Rig) -> dict:
    return json.loads((rig.root / "cache" / "registry.json").read_text(encoding="utf-8"))


def events_between(rig: Rig, start: float, kinds=("load_started", "unloaded", "loaded", "load_failed")) -> list:
    return [(e["kind"], e["backend"], e["reason"]) for e in rig.events if e["t"] >= start and e["kind"] in kinds]


async def test_real_engine_end_to_end(rig: Rig) -> None:
    core, user, numbers = rig.core, rig.user, rig.numbers
    numbers["engine_lock"] = str(rig.engine._engine_lock.path)
    assert rig.engine._engine_lock.path == ENGINE_LOCK, "the run must use the real engine lock"
    fresh_cuda = not cuda_initialized()
    numbers["cuda_fresh_process"] = fresh_cuda
    numbers["nvsmi_before_detection_mib"] = await nvsmi_used()

    # 1. launch; wait 10 s: no load, "No model loaded", the device detected
    async with rig.step("1 launch") as step:
        await rig.open()
        step["detect_s"] = await rig.until(lambda: core.state.device.detected, "the first device reading", 120)
        await asyncio.sleep(max(0.0, step["start"] + 10.0 - rig.now()))            # §8.9: wait 10 s
        numbers["nvsmi_after_detection_mib"] = await nvsmi_used()
        if fresh_cuda and numbers["nvsmi_before_detection_mib"] is not None \
                and numbers["nvsmi_after_detection_mib"] is not None:
            numbers["cuda_context_mib"] = numbers["nvsmi_after_detection_mib"] - numbers["nvsmi_before_detection_mib"]
        device = core.state.device
        numbers["device"] = {"cuda": device.cuda, "name": device.name, "total_bytes": device.total_bytes,
                             "free_bytes": device.free_bytes, "other_bytes": device.other_bytes}
        assert device.cuda, "CUDA was not detected"
        assert rig.loads_so_far() == 0, "a model loaded at launch"
        assert rig.the("status").text == "No model loaded"
        assert rig.resident() == ()
        seed_everything()
    await rig.tour("after step 1")

    # 2. Models › Multilingual "Load This Model": one load, resident, history `loaded`, the dock row resident
    async with rig.step("2 load Multilingual") as step:
        loaded = await rig.load_from_card(MTL)
        step["load"] = loaded
        numbers.setdefault("loads", []).append(loaded)
        assert rig.loads_so_far() == 1
        assert rig.engine.residency().resident == (MTL,)
        await rig.until(lambda: any(e.kind == "loaded" and e.backend == MTL for e in core.state.history.events),
                        "the history's loaded line", 10)
        assert rig.the(f"dock.row.{MTL}").props.get("aria-checked") == "true"
    await rig.tour("after step 2")

    # 3. Forge e2e_mtl from a dropped sample.wav, with consent
    async with rig.step("3 forge e2e_mtl") as step:
        forged = await rig.forge("e2e_mtl", MTL)
        step["forge"] = forged
        assert not forged["switched"]
        assert (rig.root / "cache" / MTL / "e2e_mtl.pt").is_file()
        entry = registry(rig)["e2e_mtl"]
        assert entry["backend"] == MTL and entry["meta"]["consent"] is True
        assert (rig.root / "voices" / "reference" / "e2e_mtl_ref.wav").is_file()
        assert entry["reference_wav"].endswith("e2e_mtl_ref.wav"), entry["reference_wav"]
    await rig.tour("after step 3")

    # 4. Speak "Hello there.": a RIFF 24 kHz mono take over 0.5 s, its index entry, peaks and tokens
    async with rig.step("4 take Multilingual") as step:
        take = await rig.take("e2e_mtl", HELLO)
        step["take"] = take
        facts = wav_facts(take_wav(rig, take["take_id"]))
        step["wav"] = facts
        assert facts["riff"] and facts["sample_rate"] == 24000 and facts["channels"] == 1 and facts["seconds"] > 0.5
        record = next(t for t in core.takes.takes if t.id == take["take_id"])
        assert record.peaks and max(record.peaks) > 0, "the take has no peaks"
        assert record.tokens and record.tokens > 2, f"tokens {record.tokens}"
    await rig.tour("after step 4")

    # 5. Japanese with Use Japanese (the pykakasi path)
    async with rig.step("5 take Japanese") as step:
        take = await rig.take("e2e_mtl", JAPANESE, japanese=True)
        step["take"] = take
        assert take["language"] == "ja" and take["seconds"] > 0.5
    await rig.tour("after step 5")

    # 6. Forge e2e_std on Standard: "Switch to Standard & Forge" evicts Multilingual before Standard loads
    async with rig.step("6 forge e2e_std") as step:
        forged = await rig.forge("e2e_std", STD)
        step["forge"] = forged
        assert forged["switched"]
        order = events_between(rig, step["start"])
        assert order[:3] == [("load_started", STD, None), ("unloaded", MTL, "evicted"), ("loaded", STD, None)], order
        build = [b for b in rig.builds if b["backend"] == STD][-1]
        assert build["resident_at_build"] == [], build
        numbers.setdefault("loads", []).append([e for e in rig.events if e["kind"] == "loaded"][-1])
    await rig.tour("after step 6")

    # 7. Speak e2e_std with Guidance 0 (E16a: no RuntimeError)
    async with rig.step("7 take Standard guidance 0") as step:
        take = await rig.take("e2e_std", STANDARD_LINE, guidance=0.0)
        step["take"] = take
        assert take["cfg_weight"] == 0.0 and take["seconds"] > 0.5
    await rig.tour("after step 7")

    # 8. Forge e2e_tur on Turbo (sample.wav is over 5 s); Speak with a tag: tokens are the sampled count
    async with rig.step("8 forge and take Turbo") as step:
        forged = await rig.forge("e2e_tur", TUR)
        step["forge"] = forged
        assert forged["switched"]
        numbers.setdefault("loads", []).append([e for e in rig.events if e["kind"] == "loaded"][-1])
        take = await rig.take("e2e_tur", TURBO_LINE)
        step["take"] = take
        assert take["tokens"] and take["tokens"] > 2, f"Turbo tokens {take['tokens']}"
        assert take["seconds"] > 0.5
    await rig.tour("after step 8")

    # 9. Takes: star one, Remove Unstarred (arm, second click): only the starred one stays, the rest recycled
    async with rig.step("9 star and purge") as step:
        session_takes = [t.id for t in core.takes.takes]
        keep = session_takes[0]
        await rig.star(keep)
        wavs = {t.id: take_wav(rig, t.id) for t in core.takes.takes if t.id != keep}
        gone = await rig.purge_unstarred()
        step["purged"] = gone
        assert sorted(gone) == sorted(wavs) and [t.id for t in core.takes.takes] == [keep]
        assert all(not path.exists() for path in wavs.values()), "a purged WAV is still on disk"
        recycled = {path.name for path in core.trash.moved}
        assert {path.name for path in wavs.values()} <= recycled, recycled
        step["recycled"] = sorted(recycled)
    await rig.tour("after step 9")

    # 10. Voices: delete all three (arm): the entries go, the .pt files to the Recycle Bin, the WAVs stay
    async with rig.step("10 delete voices") as step:
        pts = {voice: rig.root / "cache" / backend / f"{voice}.pt" for voice, backend in zip(VOICES, (MTL, STD, TUR))}
        assert all(path.is_file() for path in pts.values())
        for voice in VOICES:
            await rig.delete_voice(voice)
        assert not set(VOICES) & set(registry(rig))
        assert all(not path.exists() for path in pts.values())
        assert {path.name for path in pts.values()} <= {path.name for path in core.trash.moved}
        assert all((rig.root / "voices" / "reference" / f"{voice}_ref.wav").is_file() for voice in VOICES)
        step["recycled"] = sorted(path.name for path in core.trash.moved)
    await rig.tour("after step 10")

    # 11. Free VRAM: nothing resident, reserved under 0.2 GiB, the engine lock released
    async with rig.step("11 free VRAM") as step:
        freed = await rig.free()
        step["free"] = freed
        numbers["free_vram"] = freed
        assert rig.engine.residency().resident == ()
        reserved = cuda_bytes("memory_reserved")
        assert reserved is not None and reserved < FREED_LIMIT, f"reserved {reserved / GIB:.2f} GiB after Free VRAM"
        assert not rig.engine.residency().lock_held and not rig.engine._engine_lock.held()
        await rig.until(lambda: core.state.device.app_bytes < FREED_LIMIT, "the VRAM reading after Free VRAM", 10)
        numbers["nvsmi_after_free_mib"] = await nvsmi_used()
    await rig.tour("after step 11")

    # all: the numbers the Forge Notes and the Models page show
    numbers["takes"] = [{k: t[k] for k in ("voice", "backend", "tokens", "made_s", "conds_load_s", "wall_s",
                                           "peak_reserved_bytes")} for t in rig.takes]
    numbers["bakes"] = rig.bakes
    numbers["unloaded"] = [e for e in rig.events if e["kind"] == "unloaded"]
    assert all(e["allocated_bytes"] is not None and e["allocated_bytes"] < FREED_LIMIT for e in numbers["unloaded"])
    assert rig.max_resident_seen == 1 and rig.loads_so_far() == 3
    assert "torch" in sys.modules
