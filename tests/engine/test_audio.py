"""engine.audio: clip facts, the selection rule, peaks, WAV bytes and trim_copy (BUILD_SPEC §3.5)."""
from __future__ import annotations

import dataclasses
import io
import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from engine import audio
from engine.audio import (
    MAX_CLIP_BYTES,
    SUPPORTED_EXTS,
    TURBO_MIN_EXCLUSIVE_S,
    ClipFacts,
    clip_duration,
    clip_problem,
    peaks,
    probe_clip,
    read_wav_bytes,
    selection_end,
    trim_copy,
)

SAMPLE = Path(__file__).resolve().parents[2] / "voices" / "reference" / "sample.wav"
needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason="voices/reference/sample.wav is not on this PC")
TURBO = "chatterbox-turbo"


def _facts(path: Path, **overrides: object) -> ClipFacts:
    return dataclasses.replace(probe_clip(path), **overrides)


def _write(path: Path, data: np.ndarray, sr: int, subtype: str | None = None, fmt: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr, subtype=subtype, format=fmt)
    return path


def _is_riff_wave(path: Path) -> bool:
    head = path.read_bytes()[:12]
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def test_constants() -> None:
    assert SUPPORTED_EXTS == (".wav", ".flac", ".ogg", ".mp3")
    assert MAX_CLIP_BYTES == 500 * 2**20
    assert TURBO_MIN_EXCLUSIVE_S == 5.0


# ---------- sample.wav (read-only) ----------

@needs_sample
def test_sample_wav_facts() -> None:
    facts = probe_clip(SAMPLE)
    assert facts.path == SAMPLE
    assert facts.duration_s == pytest.approx(30.03, abs=0.005)
    assert (facts.sample_rate, facts.channels, facts.fmt, facts.subtype) == (22050, 1, "WAV", "PCM_16")
    assert facts.peak_dbfs == pytest.approx(-8.42, abs=0.01)
    assert facts.peak == pytest.approx(10 ** (facts.peak_dbfs / 20))
    assert facts.clipped is False and facts.silent is False
    assert facts.selection_s == pytest.approx(13.42, abs=0.02)
    assert len(facts.peaks) == 230
    assert max(facts.peaks) == 1.0 and min(facts.peaks) >= 0.0
    assert clip_problem(facts, TURBO) is None and clip_problem(facts, None) is None


@needs_sample
def test_sample_wav_selection_end_on_the_whole_clip() -> None:
    mono, sr = sf.read(str(SAMPLE), dtype="float32")
    assert selection_end(mono, sr) == pytest.approx(13.42, abs=0.02)
    assert selection_end(mono, sr) == probe_clip(SAMPLE).selection_s


@needs_sample
def test_sample_wav_in_small_blocks_gives_the_same_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    whole = probe_clip(SAMPLE, n_bars=138)
    monkeypatch.setattr(audio, "_BLOCK_FRAMES", 1000)
    blocked = probe_clip(SAMPLE, n_bars=138)
    assert len(blocked.peaks) == 138
    assert blocked.peaks == pytest.approx(whole.peaks)
    assert dataclasses.replace(blocked, peaks=()) == dataclasses.replace(whole, peaks=())


@needs_sample
def test_clip_duration_of_sample_wav() -> None:
    assert clip_duration(SAMPLE) == pytest.approx(30.03, abs=0.005)


# ---------- synthetic clips ----------

def test_a_silent_clip(tmp_path: Path) -> None:
    path = _write(tmp_path / "silent.wav", np.zeros(3 * 24000, dtype=np.float32), 24000, "PCM_16")
    facts = probe_clip(path)
    assert facts.silent is True and facts.peak == 0.0 and facts.peak_dbfs == -math.inf
    assert set(facts.peaks) == {0.0}
    assert clip_problem(facts, None) == "the clip is silent"


def test_a_clip_under_one_second(make_clip) -> None:
    facts = probe_clip(make_clip("short.wav", 0.6))
    assert facts.duration_s == pytest.approx(0.6)
    assert facts.selection_s == facts.duration_s
    assert clip_problem(facts, None) == "0.6 s is too short to forge"
    assert clip_problem(facts, TURBO) == "0.6 s is too short to forge"


def test_silent_comes_before_too_short(tmp_path: Path) -> None:
    path = _write(tmp_path / "blip.wav", np.zeros(4800, dtype=np.float32), 24000)
    assert clip_problem(probe_clip(path), TURBO) == "the clip is silent"


@pytest.mark.parametrize(("seconds", "problem"), [
    (4.2, "chatterbox-turbo needs a reference clip longer than 5s (got 4.2s). "
          "Use a longer clip or another backend."),
    (5.0, "chatterbox-turbo needs a reference clip longer than 5s (got 5.0s). "
          "Use a longer clip or another backend."),
    (5.1, None),
])
def test_the_turbo_rule(make_clip, seconds: float, problem: str | None) -> None:
    facts = probe_clip(make_clip(f"clip-{seconds}.wav", seconds))
    assert clip_problem(facts, TURBO) == problem
    assert clip_problem(facts, "chatterbox-multilingual") is None
    assert clip_problem(facts, None) is None


def test_the_turbo_rule_reads_the_selection(make_clip) -> None:
    facts = _facts(make_clip("long.wav", 20.0), selection_s=4.0)
    assert "(got 4.0s)" in clip_problem(facts, TURBO)


def _with_full_scale_run(n: int, start: int, length: int) -> np.ndarray:
    data = 0.2 * np.sin(np.arange(n) / 7.0)
    data[start:start + length] = 1.0
    return data.astype(np.float32)


def test_three_full_scale_samples_are_clipping(tmp_path: Path) -> None:
    path = _write(tmp_path / "clipped.wav", _with_full_scale_run(48000, 1000, 3), 24000, "PCM_16")
    facts = probe_clip(path)
    assert facts.clipped is True and facts.peak_dbfs == pytest.approx(0.0, abs=0.01)


def test_two_full_scale_samples_are_not_clipping(tmp_path: Path) -> None:
    path = _write(tmp_path / "near.wav", _with_full_scale_run(48000, 1000, 2), 24000, "PCM_16")
    assert probe_clip(path).clipped is False


def test_clipping_across_a_block_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "_BLOCK_FRAMES", 64)
    path = _write(tmp_path / "edge.wav", _with_full_scale_run(4096, 63, 3), 24000, "FLOAT")
    assert probe_clip(path).clipped is True


def test_clipping_in_one_channel_of_a_stereo_clip(tmp_path: Path) -> None:
    left = _with_full_scale_run(24000, 500, 4)
    right = 0.1 * np.ones(24000, dtype=np.float32)
    facts = probe_clip(_write(tmp_path / "stereo.wav", np.stack([left, right], axis=1), 24000, "PCM_16"))
    assert facts.channels == 2 and facts.clipped is True


def test_a_stereo_clip_uses_the_mono_mix_for_bars(tmp_path: Path) -> None:
    n = 24000 * 2
    left = np.zeros(n, dtype=np.float32)
    left[: n // 2] = 0.8                       # loud first half, left only
    right = np.full(n, 0.2, dtype=np.float32)
    facts = probe_clip(_write(tmp_path / "stereo.wav", np.stack([left, right], axis=1), 24000, "FLOAT"), n_bars=4)
    assert facts.channels == 2
    assert facts.peak == pytest.approx(0.8)
    assert facts.peaks == pytest.approx((1.0, 1.0, 0.2, 0.2))   # mono 0.5 then 0.1, normalised


@pytest.mark.parametrize(("subtype", "fmt", "suffix"), [
    ("PCM_24", "WAV", ".wav"), ("FLOAT", "WAV", ".wav"), ("PCM_16", "FLAC", ".flac"),
    ("PCM_24", "FLAC", ".flac"), ("VORBIS", "OGG", ".ogg"), ("MPEG_LAYER_III", "MP3", ".mp3"),
])
def test_formats_and_subtypes(make_clip, subtype: str, fmt: str, suffix: str) -> None:
    facts = probe_clip(make_clip(f"clip{suffix}", 8.0, sr=22050, subtype=subtype))
    assert (facts.fmt, facts.subtype) == (fmt, subtype)
    assert facts.duration_s == pytest.approx(8.0, abs=0.01)
    assert facts.sample_rate == 22050 and facts.selection_s == facts.duration_s


def test_a_wavex_file_is_reported_as_wav(make_clip) -> None:
    facts = probe_clip(make_clip("extensible.wav", 2.0, fmt="WAVEX", subtype="PCM_16"))
    assert facts.fmt == "WAV"


def test_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "clip.aiff"
    path.write_bytes(b"FORM")
    with pytest.raises(ValueError, match=r"^use WAV, FLAC, OGG or MP3$"):
        probe_clip(path)


def test_a_clip_over_the_size_limit(make_clip, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_clip("big.wav", 1.5)
    monkeypatch.setattr(audio, "MAX_CLIP_BYTES", 100)
    with pytest.raises(ValueError, match=r"^Clip file is over 500 MB$"):
        probe_clip(path)


def test_an_unreadable_clip_gives_soundfiles_reason(tmp_path: Path) -> None:
    path = tmp_path / "garbage.wav"
    path.write_bytes(b"this is not audio" * 20)
    with pytest.raises(ValueError) as failure:
        probe_clip(path)
    assert str(failure.value) == "Format not recognised."
    assert failure.value.__cause__ is None


def test_a_missing_clip(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"^file not found$"):
        probe_clip(tmp_path / "missing.wav")


def test_clip_duration_of_missing_or_unreadable_files(tmp_path: Path, make_clip) -> None:
    assert clip_duration(tmp_path / "missing.wav") is None
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"nope" * 50)
    assert clip_duration(garbage) is None
    assert clip_duration(make_clip("one.wav", 2.5)) == pytest.approx(2.5)


def test_bar_count_and_normalisation(make_clip) -> None:
    facts = probe_clip(make_clip("bars.wav", 3.0, level=0.25), n_bars=50)
    assert len(facts.peaks) == 50
    assert max(facts.peaks) == 1.0 and all(0.0 <= v <= 1.0 for v in facts.peaks)


# ---------- the selection rule ----------

@pytest.mark.parametrize(("pauses", "expected"), [
    ([(7.0, 7.5), (11.0, 11.3), (16.0, 17.0)], 11.0),   # the last pause that begins in [6, 15]
    ([], 15.0),                                         # no pause
    ([(3.0, 4.0)], 15.0),                               # begins before 6 s
    ([(9.0, 9.14)], 15.0),                              # shorter than 200 ms
    ([(6.0, 6.2)], 6.0),                                # exactly 6.0 s and 200 ms
    ([(8.0, 9.0), (14.9, 15.3)], 14.9),                 # begins before 15 s, ends after it
    ([(8.0, 9.0), (15.1, 16.0)], 8.0),                  # begins after 15 s: not counted
])
def test_selection_end_rules(make_clip, pauses: list[tuple[float, float]], expected: float) -> None:
    path = make_clip("long.wav", 20.0, pauses=pauses)
    facts = probe_clip(path)
    assert facts.selection_s == pytest.approx(expected, abs=0.021)
    mono, sr = sf.read(str(path), dtype="float32")
    assert selection_end(mono, sr) == pytest.approx(facts.selection_s)


def test_a_clip_of_fifteen_seconds_or_less_is_used_whole(make_clip) -> None:
    facts = probe_clip(make_clip("short.wav", 12.0, pauses=[(7.0, 8.0)]))
    assert facts.selection_s == facts.duration_s == pytest.approx(12.0)


def test_selection_end_mixes_a_stereo_array(make_clip) -> None:
    mono, sr = sf.read(str(make_clip("long.wav", 20.0, pauses=[(10.0, 11.0)])), dtype="float32")
    assert selection_end(np.stack([mono, mono], axis=1), sr) == pytest.approx(10.0, abs=0.021)


# ---------- peaks ----------

def test_peaks_are_absolute_and_binned() -> None:
    assert peaks(np.array([0.0, 0.5, -0.25, 0.1], dtype=np.float32), 2) == pytest.approx([0.5, 0.25])
    assert peaks(np.arange(10, dtype=np.float32) / 10, 5) == pytest.approx([0.1, 0.3, 0.5, 0.7, 0.9])


def test_peaks_are_clipped_to_one() -> None:
    assert peaks(np.array([2.0, -3.0, 0.5, 0.25]), 2) == [1.0, 0.5]


def test_peaks_of_nothing_and_of_short_input() -> None:
    assert peaks(np.zeros(0, dtype=np.float32), 3) == [0.0, 0.0, 0.0]
    assert len(peaks(np.ones(5, dtype=np.float32), 230)) == 230


def test_peaks_of_a_two_channel_array_take_the_louder_channel() -> None:
    frames = np.array([[0.1, -0.7], [0.2, 0.0], [0.0, 0.3], [-0.4, 0.1]], dtype=np.float32)
    assert peaks(frames, 2) == pytest.approx([0.7, 0.4])


def test_peaks_need_a_bin() -> None:
    with pytest.raises(ValueError):
        peaks(np.ones(4), 0)


# ---------- WAV bytes ----------

def test_read_wav_bytes_reads_the_engines_float32_wav() -> None:
    samples = (0.25 * np.sin(np.arange(2400) / 5.0)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, samples, 24000, format="WAV", subtype="FLOAT")
    decoded, sr = read_wav_bytes(buffer.getvalue())
    assert sr == 24000 and decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, samples)


# ---------- trim_copy ----------

@pytest.mark.parametrize(("subtype", "dtype"), [
    ("PCM_16", "int16"), ("PCM_24", "int32"), ("PCM_32", "int32"), ("FLOAT", "float32"),
])
def test_trim_copy_keeps_the_pcm_subtype_and_samples(make_clip, tmp_path: Path, subtype: str, dtype: str) -> None:
    src = make_clip(f"src-{subtype}.wav", 20.0, sr=22050, subtype=subtype, channels=2)
    before = src.read_bytes()
    dst = trim_copy(src, tmp_path / "out" / "trimmed.wav", 13.42)
    assert dst == tmp_path / "out" / "trimmed.wav"
    info = sf.info(str(dst))
    assert (info.format, info.subtype, info.samplerate, info.channels) == ("WAV", subtype, 22050, 2)
    assert info.frames == round(13.42 * 22050)
    copied, _ = sf.read(str(dst), dtype=dtype)
    original, _ = sf.read(str(src), dtype=dtype, frames=info.frames)
    np.testing.assert_array_equal(copied, original)
    assert src.read_bytes() == before
    assert not dst.with_name(dst.name + ".part").exists()


@pytest.mark.parametrize(("source_subtype", "expected"), [
    ("PCM_U8", "PCM_16"), ("PCM_S8", "PCM_16"), ("DOUBLE", "FLOAT"),
])
def test_trim_copy_subtype_for_other_sources(make_clip, tmp_path: Path, source_subtype: str, expected: str) -> None:
    suffix = ".flac" if source_subtype == "PCM_S8" else ".wav"
    src = make_clip(f"src{suffix}", 3.0, subtype=source_subtype)
    dst = trim_copy(src, tmp_path / "out.wav", 2.0)
    assert sf.info(str(dst)).subtype == expected


@pytest.mark.parametrize(("suffix", "end_s"), [(".mp3", 8.0), (".mp3", 5.0), (".ogg", 8.0), (".ogg", 5.0)])
def test_trim_copy_turns_mp3_and_ogg_into_riff_pcm16(make_clip, tmp_path: Path, suffix: str, end_s: float) -> None:
    src = make_clip(f"clip{suffix}", 8.0, sr=22050)
    dst = trim_copy(src, tmp_path / "staged.wav", end_s)
    assert _is_riff_wave(dst)
    info = sf.info(str(dst))
    assert (info.format, info.subtype, info.samplerate, info.channels) == ("WAV", "PCM_16", 22050, 1)
    assert info.duration == pytest.approx(end_s, abs=0.01)
    samples, _ = sf.read(str(dst), dtype="float32")
    assert 0.1 < float(np.max(np.abs(samples))) <= 1.0


def test_trim_copy_past_the_end_copies_everything(make_clip, tmp_path: Path) -> None:
    src = make_clip("short.wav", 2.0)
    dst = trim_copy(src, tmp_path / "all.wav", 30.0)
    assert sf.info(str(dst)).frames == sf.info(str(src)).frames


def test_trim_copy_replaces_an_existing_file(make_clip, tmp_path: Path) -> None:
    dst = tmp_path / "staged.wav"
    dst.write_bytes(b"old")
    trim_copy(make_clip("clip.wav", 2.0), dst, 1.0)
    assert sf.info(str(dst)).duration == pytest.approx(1.0)


def test_trim_copy_failure_leaves_nothing_behind(tmp_path: Path) -> None:
    src = tmp_path / "garbage.wav"
    src.write_bytes(b"not audio" * 30)
    dst = tmp_path / "staged.wav"
    with pytest.raises(RuntimeError):
        trim_copy(src, dst, 1.0)
    assert not dst.exists() and not dst.with_name(dst.name + ".part").exists()


@pytest.mark.parametrize("end_s", [0.0, -1.0, math.nan, math.inf])
def test_trim_copy_needs_a_positive_end(make_clip, tmp_path: Path, end_s: float) -> None:
    with pytest.raises(ValueError):
        trim_copy(make_clip("clip.wav", 1.0), tmp_path / "out.wav", end_s)


def test_make_clip_writes_what_it_says(make_clip, tmp_path: Path) -> None:
    path = make_clip("helper.wav", 1.5, folder=tmp_path / "voices", sr=16000, channels=2,
                     pauses=[(0.5, 1.0)])
    assert path == tmp_path / "voices" / "helper.wav"
    data, sr = sf.read(str(path), dtype="float32")
    info = sf.info(str(path))
    assert (sr, info.channels, info.subtype) == (16000, 2, "PCM_16")
    assert info.duration == pytest.approx(1.5)
    assert not data[8000:16000].any() and data[:8000].any()
