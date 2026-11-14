"""The GPU soak (I3): 3 rounds x 3 backends on the real engine, through the UI. `pytest -m gpu tests\\e2e\\test_gpu_soak.py`
(about 6-10 minutes; it loads 9 models in turn, one at a time).

Each round switches to every backend with the named Models card, makes three takes per backend on Speak (on Turbo
one of them is a long script; its sampled duration is recorded), stars one take per backend and removes the unstarred ones,
then frees VRAM. Round 1 forges one scratch voice per backend first. Checked, with the numbers kept in the results
file (tests\\e2e\\conftest.py):
- after every unload (evictions and Free VRAM) allocated is under 0.2 GiB, and the reserved bytes after an unload do
  not grow from round to round;
- take times do not drift: each backend's seconds of generation per second of audio stays within 50 % of round 1;
- never two models resident (the factory spy and the sampler), every load caused by its named control, no ERROR log
  (the user fixture), and the event-loop lag during real loads and takes (max and p95; over 500 ms is flagged).
Removed takes go to a scratch folder in the data root, not the user's Recycle Bin.
"""
from __future__ import annotations

import pytest

from tests.e2e.conftest import (BACKENDS, FREED_LIMIT, GIB, MIB, TUR, Rig, cuda_bytes, cuda_initialized,
                                lag_summary, nvsmi_used, seed_everything)
from ui.contract.manifest import RECONNECT_TIMEOUT_S

pytestmark = [pytest.mark.gpu, pytest.mark.soak]

ROUNDS = 3
VOICE = {backend: f"soak_{backend.removeprefix('chatterbox-')[:3]}" for backend in BACKENDS}
SHORT = "The quick brown fox jumps over the lazy dog."
MEDIUM = "Reliability runs repeat the same work until something breaks, or until nothing does."
SECOND = "Every round loads the model again and speaks the same three lines."
LONG = ("Reliability is not a single test but a habit. We load a model, we speak a few lines, we unload it, and then "
        "we do the same thing again, and again, watching the memory, the clock and the queue. If anything grows that "
        "should not grow, or slows down that should not slow down, we want to see it here, in a long take like this "
        "one, before anyone else ever has to hear it. So this paragraph keeps going, calmly and clearly, until it is "
        "close to the forty second limit.")
LONG_ON = TUR
DRIFT_LIMIT = 0.5                    # take times may not drift by more than 50 %
RESERVED_SLACK = 64 * MIB            # reserved after an unload may not grow round to round beyond this


def scripts(backend: str) -> tuple[str, ...]:
    return (SHORT, MEDIUM, LONG if backend == LONG_ON else SECOND)


async def test_gpu_soak(rig: Rig) -> None:
    core, numbers = rig.core, rig.numbers
    fresh_cuda = not cuda_initialized()
    numbers["cuda_fresh_process"] = fresh_cuda
    numbers["nvsmi_before_detection_mib"] = await nvsmi_used()
    async with rig.step("launch") as step:
        await rig.open()
        step["detect_s"] = await rig.until(lambda: core.state.device.detected, "the first device reading", 120)
        numbers["nvsmi_after_detection_mib"] = await nvsmi_used()
        if fresh_cuda and None not in (numbers["nvsmi_before_detection_mib"], numbers["nvsmi_after_detection_mib"]):
            numbers["cuda_context_mib"] = numbers["nvsmi_after_detection_mib"] - numbers["nvsmi_before_detection_mib"]
        assert core.state.device.cuda, "CUDA was not detected"
        assert rig.loads_so_far() == 0
        seed_everything()

    rounds: list[dict] = []
    numbers["rounds"] = rounds
    for index in range(1, ROUNDS + 1):
        record: dict = {"round": index, "loads": [], "forges": [], "takes": [], "purged": [], "unloads": []}
        rounds.append(record)
        first_event = len(rig.events)
        for backend in BACKENDS:
            async with rig.step(f"round {index} load {backend}"):
                record["loads"].append(await rig.load_from_card(backend))
            if index == 1:
                async with rig.step(f"round {index} forge {VOICE[backend]}"):
                    record["forges"].append(await rig.forge(VOICE[backend], backend))
            kept = None
            for number, text in enumerate(scripts(backend), 1):
                async with rig.step(f"round {index} {backend} take {number}"):
                    take = await rig.take(VOICE[backend], text)
                    take["script"] = number
                    take["round"] = index
                    record["takes"].append(take)
                    kept = kept or take["take_id"]
            async with rig.step(f"round {index} star take {kept}"):
                await rig.star(kept)
        async with rig.step(f"round {index} purge"):
            record["purged"] = await rig.purge_unstarred()
            assert record["purged"], "nothing was unstarred"
        async with rig.step(f"round {index} free VRAM"):
            record["free"] = await rig.free("models.free")
            record["nvsmi_after_free_mib"] = await nvsmi_used()
        await rig.tour(f"after round {index}")
        record["unloads"] = [e for e in rig.events[first_event:] if e["kind"] == "unloaded"]
        record["reserved_after_unload_max"] = max(e["reserved_bytes"] or 0 for e in record["unloads"])
        record["reserved_after_free"] = record["free"]["reserved_after"]
        record["rtf"] = {backend: _rtf([t for t in record["takes"] if t["backend"] == backend])
                         for backend in BACKENDS}
        record["conds_load_s"] = {t["backend"]: t["conds_load_s"] for t in record["takes"]
                                  if t["conds_load_s"] is not None}

    # the memory after every unload, and its growth from round to round
    unloads = [e for r in rounds for e in r["unloads"]]
    assert len(unloads) == ROUNDS * len(BACKENDS), [(e["backend"], e["reason"]) for e in unloads]
    assert all(e["allocated_bytes"] is not None and e["allocated_bytes"] < FREED_LIMIT for e in unloads)
    first_reserved = rounds[0]["reserved_after_unload_max"]
    for later in rounds[1:]:
        assert later["reserved_after_unload_max"] <= first_reserved + RESERVED_SLACK, (
            f"round {later['round']}: reserved after an unload grew to "
            f"{later['reserved_after_unload_max'] / MIB:.0f} MiB (round 1: {first_reserved / MIB:.0f} MiB)")
    assert all(r["reserved_after_free"] is not None and r["reserved_after_free"] < FREED_LIMIT for r in rounds)

    # take times: generation seconds per audio second, per backend, against round 1
    drift = {backend: [round(r["rtf"][backend] / rounds[0]["rtf"][backend] - 1, 4) for r in rounds[1:]]
             for backend in BACKENDS}
    numbers["take_time_drift"] = drift
    long_takes = [t for r in rounds for t in r["takes"] if t["backend"] == LONG_ON and t["script"] == 3]
    numbers["long_takes"] = [{k: t[k] for k in ("round", "seconds", "tokens", "hit_token_limit", "gen_seconds")}
                             for t in long_takes]
    numbers["load_seconds"] = {backend: [r["loads"][i]["seconds"] for r in rounds]
                               for i, backend in enumerate(BACKENDS)}
    numbers["loop_lag"] = lag_summary(rig.lag)
    numbers["reserved_now"] = cuda_bytes("memory_reserved")
    for backend, values in drift.items():
        assert all(value <= DRIFT_LIMIT for value in values), f"{backend} take times drifted: {values}"
    assert all(t["seconds"] > 25 for t in long_takes), numbers["long_takes"]
    assert rig.loads_so_far() == ROUNDS * len(BACKENDS) and rig.max_resident_seen == 1
    assert numbers["loop_lag"]["all"]["max_ms"] < RECONNECT_TIMEOUT_S * 1000, numbers["loop_lag"]["all"]
    assert core.state.engine.resident == "" and not rig.engine.residency().lock_held
    assert numbers["reserved_now"] is not None and numbers["reserved_now"] < FREED_LIMIT, \
        f"{numbers['reserved_now'] / GIB:.2f} GiB reserved at the end"


def _rtf(takes: list[dict]) -> float:
    """Seconds of generation per second of audio over a backend's takes in one round."""
    audio = sum(t["seconds"] for t in takes)
    made = sum(t["gen_seconds"] for t in takes)
    return round(made / audio, 4) if audio else float("nan")
