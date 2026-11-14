"""engine.progress: the tqdm hook, Job status and cooperative cancel (BUILD_SPEC §3.2)."""
from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest

from engine import progress
from engine.errors import Cancelled
from engine.progress import TOKENS_PER_SECOND, Job, JobStatus, bind, current_job


class FakeTqdm:
    """Stands in for tqdm: records its calls and returns a marker."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return "original tqdm"


@pytest.fixture
def original() -> FakeTqdm:
    return FakeTqdm()


@pytest.fixture
def sampling(original: FakeTqdm):
    return progress._wrap(original, "sampling")


@pytest.fixture
def decoding(original: FakeTqdm):
    return progress._wrap(original, "decoding")


def test_outside_a_job_the_original_runs(sampling, original: FakeTqdm) -> None:
    assert current_job() is None
    assert sampling(range(1000), desc="Sampling", dynamic_ncols=True) == "original tqdm"
    assert original.calls == [((range(1000),), {"desc": "Sampling", "dynamic_ncols": True})]
    assert sampling.voicetool_original is original


def test_inside_a_job_the_same_items_come_back(sampling, original: FakeTqdm) -> None:
    job = Job()
    with bind(job):
        items = list(sampling(range(5), desc="Sampling"))
    assert items == [0, 1, 2, 3, 4]
    assert original.calls == []


def test_cancel_after_three_steps_raises_at_step_four(sampling) -> None:
    job = Job()
    seen: list[int] = []
    with bind(job), pytest.raises(Cancelled):
        for i in sampling(range(1000), desc="Sampling", dynamic_ncols=True):
            seen.append(i)
            if len(seen) == 3:
                job.cancel()
    assert seen == [0, 1, 2]
    status = job.status()
    assert status.tokens == 3
    assert status.phase == "sampling"          # the engine marks "cancelled"; the hook never moves on
    assert status.hit_token_limit is False


def test_an_early_break_moves_the_phase_to_decoding(sampling) -> None:
    job = Job()
    with bind(job):
        for i in sampling(range(1000)):
            if i == 4:
                break                          # EOS
    status = job.status()
    assert (status.phase, status.tokens, status.max_tokens, status.hit_token_limit) == (
        "decoding", 5, 1000, False)
    assert status.audio_s == pytest.approx(5 / TOKENS_PER_SECOND)
    assert job.sample_s is not None and job.sample_s >= 0


def test_exhaustion_sets_hit_token_limit(sampling) -> None:
    job = Job()
    with bind(job):
        for _ in sampling(range(50)):
            pass
    status = job.status()
    assert (status.phase, status.tokens, status.max_tokens, status.hit_token_limit) == (
        "decoding", 50, 50, True)


def test_turbo_shaped_take(sampling, decoding) -> None:
    """Sampling over range(1000) breaks after 87 steps; then the 2-step flow loop runs out."""
    job = Job()
    with bind(job):
        for step, _ in enumerate(sampling(range(1000))):
            if step + 1 == 87:
                break
        span = [0.0, 0.5, 1.0]
        for _ in decoding(zip(span[:-1], span[1:]), total=len(span) - 1):
            pass
    status = job.status()
    assert status.tokens == 87
    assert status.max_tokens == 1000
    assert job.decode_steps == 2
    assert status.hit_token_limit is False
    assert status.phase == "decoding"
    assert status.audio_s == pytest.approx(87 / 25)


def test_a_cancel_stops_the_flow_loop_without_touching_tokens(sampling, decoding) -> None:
    job = Job()
    with bind(job):
        for step, _ in enumerate(sampling(range(1000))):
            if step == 9:
                break
        with pytest.raises(Cancelled):
            for _ in decoding(iter(range(2)), total=2):
                job.cancel()
    assert job.decode_steps == 1
    assert job.status().tokens == 10


def test_unknown_length_gives_no_bound(sampling) -> None:
    job = Job()
    with bind(job):
        for i in sampling(iter(range(3))):
            pass
    assert job.status().max_tokens is None


def test_a_late_close_never_moves_a_finished_job_back(sampling) -> None:
    """A loop held by a live frame closes after the engine already marked the job failed."""
    job = Job()
    with bind(job):
        loop = sampling(range(1000))
        next(loop)
        next(loop)
    job._set_phase("failed")
    loop.close()                               # runs the hook's finally now
    status = job.status()
    assert (status.phase, status.tokens) == ("failed", 2)


def test_check_cancel_and_cancelled() -> None:
    job = Job()
    job.check()
    assert job.cancelled is False
    job.cancel()
    job.cancel()                               # idempotent
    assert job.cancelled is True
    with pytest.raises(Cancelled):
        job.check()


def test_cancel_from_another_thread_is_seen(sampling) -> None:
    job = Job()
    canceller = threading.Thread(target=job.cancel)
    with bind(job), pytest.raises(Cancelled):
        for i in sampling(range(1000)):
            if i == 1:
                canceller.start()
                canceller.join()
    assert job.status().tokens == 2


def test_new_job_status() -> None:
    status = Job().status()
    assert isinstance(status, JobStatus)
    assert (status.phase, status.tokens, status.audio_s, status.max_tokens, status.hit_token_limit) == (
        "waiting", 0, 0.0, None, False)
    assert status.elapsed_s >= 0


def test_new_job_attributes() -> None:
    job = Job()
    assert (job.bake_s, job.sample_s, job.decode_s, job.made_s, job.peak_reserved_bytes,
            job.conds_load_s, job.decode_steps) == (None, None, None, None, None, None, 0)


def test_set_phase_validates_and_freezes_elapsed_when_finished() -> None:
    job = Job()
    with pytest.raises(ValueError):
        job._set_phase("sleeping")
    job._set_phase("preparing")
    assert job.status().phase == "preparing"
    job._set_phase("done")
    first = job.status().elapsed_s
    time.sleep(0.02)
    assert job.status().elapsed_s == first


def test_done_fills_decode_seconds_after_sampling(sampling) -> None:
    job = Job()
    job._set_phase("preparing")
    with bind(job):
        for i in sampling(range(1000)):
            if i == 3:
                break
    job._set_phase("done")
    assert job.decode_s is not None and job.decode_s >= 0
    assert job.sample_s is not None
    assert job.status().phase == "done"


def test_done_without_sampling_leaves_decode_seconds_unset() -> None:
    job = Job()
    job._set_phase("baking")
    job._set_phase("done")
    assert job.decode_s is None and job.sample_s is None


def test_bind_is_thread_local_and_nests() -> None:
    outer, inner = Job(), Job()
    seen_elsewhere: list[Job | None] = []
    with bind(outer) as bound:
        assert bound is outer and current_job() is outer
        worker = threading.Thread(target=lambda: seen_elsewhere.append(current_job()))
        worker.start()
        worker.join()
        with bind(inner):
            assert current_job() is inner
            with bind(None):
                assert current_job() is None
            assert current_job() is inner
        assert current_job() is outer
    assert current_job() is None
    assert seen_elsewhere == [None]


def test_bind_restores_after_an_exception() -> None:
    job = Job()
    with pytest.raises(KeyError):
        with bind(job):
            raise KeyError("x")
    assert current_job() is None


def test_uninstall_without_chatterbox_imports_nothing() -> None:
    had_chatterbox = "chatterbox" in sys.modules
    progress.uninstall()
    assert ("chatterbox" in sys.modules) == had_chatterbox


# ---------- the shared fakes (tests/engine/conftest.py) take through the real hook ----------

def _factory(name: str):
    from engine import engine as engine_module
    return engine_module._BACKEND_FACTORIES[name]


def test_scratch_config_shape(scratch_config, tmp_path) -> None:
    assert scratch_config.ROOT == tmp_path
    assert scratch_config.CACHE_DIR.is_dir() and scratch_config.VOICES_DIR.is_dir()
    assert scratch_config.OUTPUTS_DIR.is_dir()
    assert (scratch_config.DEVICE, scratch_config.MAX_RESIDENT_BACKENDS) == ("cpu", 1)
    assert scratch_config.ENABLED_BACKENDS == [
        "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"]
    assert (scratch_config.DEFAULT_BACKEND, scratch_config.DEFAULT_LANGUAGE) == ("chatterbox-multilingual", "en")
    assert (scratch_config.DEFAULT_EXAGGERATION, scratch_config.DEFAULT_CFG_WEIGHT) == (0.5, 0.5)


def test_a_fake_take_reports_tokens_and_cancels(fake_factories, scratch_config, make_clip) -> None:
    backend = _factory("chatterbox-turbo")(scratch_config.CACHE_DIR / "chatterbox-turbo", "cpu")
    assert fake_factories.calls["chatterbox-turbo"] == 1
    assert fake_factories.builds == [("chatterbox-turbo", "cpu")]
    backend.load_voice("aiko", make_clip("aiko.wav", 6.0, folder=scratch_config.VOICES_DIR))
    assert backend.list_voices() == ["aiko"] and backend._active_voice == "aiko"

    job = Job()
    with bind(job):
        wav = backend.speak("Hello there.", "aiko")
    assert wav[:4] == b"RIFF"
    status = job.status()
    assert (status.phase, status.tokens, status.max_tokens, status.hit_token_limit) == ("decoding", 12, 1000, False)
    assert backend.last_conds_load_s is None                 # aiko was already active

    model = fake_factories.model("chatterbox-turbo")
    model.generate_gate = fake_factories.gate()
    gate = model.generate_gate
    job = Job()
    outcome: list[str] = []

    def take() -> None:
        try:
            with bind(job):
                backend.speak("Stop me.", "aiko")
        except Cancelled:
            outcome.append("cancelled")

    worker = threading.Thread(target=take)
    worker.start()
    assert gate.wait_entered(5)
    job.cancel()
    gate.release()
    worker.join(5)
    assert outcome == ["cancelled"] and job.status().tokens == 1
    progress.install()                                       # stubbed by the fixture
    assert fake_factories.installs == 1


def test_a_failed_fake_build_frees_its_model(fake_factories, scratch_config) -> None:
    import gc

    fake_factories.fail("chatterbox-standard", RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"))
    message = ""
    try:
        _factory("chatterbox-standard")(scratch_config.CACHE_DIR / "chatterbox-standard", "cpu")
    except RuntimeError as exc:
        message = str(exc)
    assert message.startswith("CUDA out of memory")
    gc.collect()
    assert fake_factories.model_ref("chatterbox-standard")() is None
    assert fake_factories.backend("chatterbox-standard") is None


def test_a_blocked_fake_build_waits_for_its_gate(fake_factories, scratch_config) -> None:
    gate = fake_factories.block("chatterbox-multilingual")
    built: list[object] = []
    worker = threading.Thread(target=lambda: built.append(
        _factory("chatterbox-multilingual")(scratch_config.CACHE_DIR / "chatterbox-multilingual", "cuda")))
    worker.start()
    assert gate.wait_entered(5)
    assert built == [] and fake_factories.model("chatterbox-multilingual") is not None
    gate.release()
    worker.join(5)
    assert built and built[0] is fake_factories.backend("chatterbox-multilingual")
    assert fake_factories.builds == [("chatterbox-multilingual", "cuda")]


# ---------- the real Chatterbox modules (imports chatterbox, loads no model) ----------

@pytest.fixture
def installed_hook():
    t3 = pytest.importorskip("chatterbox.models.t3.t3")
    flow = pytest.importorskip("chatterbox.models.s3gen.flow_matching")
    originals = (t3.tqdm, flow.tqdm)
    progress.install()
    try:
        yield t3, flow, originals
    finally:
        progress.uninstall()


@pytest.mark.slow
def test_install_twice_patches_once(installed_hook) -> None:
    t3, flow, (t3_original, flow_original) = installed_hook
    first = (t3.tqdm, flow.tqdm)
    assert t3.tqdm.voicetool_original is t3_original
    assert flow.tqdm.voicetool_original is flow_original
    progress.install()
    assert (t3.tqdm, flow.tqdm) == first
    assert t3.tqdm.voicetool_original is t3_original


@pytest.mark.slow
def test_hooked_chatterbox_tqdm_cancels_at_step_four(installed_hook) -> None:
    t3, _, _ = installed_hook
    job = Job()
    seen: list[int] = []
    with bind(job), pytest.raises(Cancelled):
        for i in t3.tqdm(range(1000), desc="Sampling", dynamic_ncols=True):
            seen.append(i)
            if len(seen) == 3:
                job.cancel()
    assert seen == [0, 1, 2]
    assert job.status().tokens == 3


@pytest.mark.slow
def test_hooked_chatterbox_tqdm_outside_a_job_is_a_real_bar(installed_hook) -> None:
    import tqdm.std

    t3, flow, _ = installed_hook
    bar = t3.tqdm(range(3), desc="Sampling", dynamic_ncols=True)
    try:
        assert isinstance(bar, tqdm.std.tqdm)
        assert list(bar) == [0, 1, 2]
    finally:
        bar.close()
    flow_bar = flow.tqdm(zip([0.0], [1.0]), total=1)
    try:
        assert isinstance(flow_bar, tqdm.std.tqdm)
    finally:
        flow_bar.close()


@pytest.mark.slow
def test_uninstall_restores_the_originals(installed_hook) -> None:
    t3, flow, (t3_original, flow_original) = installed_hook
    progress.uninstall()
    assert t3.tqdm is t3_original and flow.tqdm is flow_original
