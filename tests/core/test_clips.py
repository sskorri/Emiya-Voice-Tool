"""ClipService (§4.15.5): staging rules 1-3, commit naming, roll back, discard, sweep, bars, lookups."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
import soundfile as sf

from tests.engine.conftest import write_clip
from ui.contract.records import ClipInfo
from ui.core.services import clips as clips_module
from ui.core.services.clips import ClipService

SAMPLE = Path(__file__).resolve().parents[2] / "voices" / "reference" / "sample.wav"


@pytest.fixture
def service(tmp_path: Path) -> ClipService:
    return ClipService(tmp_path / "voices" / "reference", tmp_path / "cache" / "staging")


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A folder outside the voices folder, like Downloads."""
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staging_files(service: ClipService) -> list[str]:
    return sorted(p.name for p in service.staging.iterdir()) if service.staging.exists() else []


def assert_pcm16_wav(path: Path, seconds: float) -> None:
    head = path.read_bytes()[:12]
    assert head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    info = sf.info(str(path))
    assert (info.format, info.subtype) == ("WAV", "PCM_16")
    assert info.duration == pytest.approx(seconds, abs=0.1)


# ---------- staging ----------

@pytest.mark.skipif(not SAMPLE.is_file(), reason="voices\\reference\\sample.wav is not on this PC")
def test_sample_wav_stages_its_13_42_s_selection(service: ClipService) -> None:
    before = sha(SAMPLE)
    clip = service.stage_file(SAMPLE)
    assert isinstance(clip, ClipInfo)
    assert clip.selection_s == pytest.approx(13.42, abs=0.02)
    assert clip.duration_s == pytest.approx(30.03, abs=0.01)
    assert (clip.samplerate, clip.channels, clip.fmt, clip.subtype) == (22050, 1, "WAV", "PCM_16")
    assert clip.peak_dbfs == pytest.approx(-8.42, abs=0.02) and not clip.clipped and not clip.silent
    assert len(clip.peaks) == 230 and max(clip.peaks) == pytest.approx(1.0)
    assert (clip.name, clip.source, clip.in_library) == ("sample.wav", SAMPLE, False)
    assert clip.staged == service.staging / f"{clip.clip_id}.wav"
    info = sf.info(str(clip.staged))
    assert (info.samplerate, info.channels, info.subtype) == (22050, 1, "PCM_16")
    assert info.duration == pytest.approx(clip.selection_s, abs=1e-3)
    assert sha(SAMPLE) == before                                       # the source is never modified


def test_a_short_wav_from_elsewhere_is_byte_copied(service: ClipService, outside: Path) -> None:
    src = write_clip(outside / "hello.wav", 8.0)
    clip = service.stage_file(src)
    assert not clip.in_library and clip.source == src
    assert clip.selection_s == clip.duration_s == pytest.approx(8.0)
    assert clip.staged.read_bytes() == src.read_bytes()
    assert staging_files(service) == [f"{clip.clip_id}.wav"]           # no .part left behind


def test_a_wav_of_exactly_15_s_is_used_whole(service: ClipService, outside: Path) -> None:
    src = write_clip(outside / "fifteen.wav", 15.0, subtype="PCM_24")
    clip = service.stage_file(src)
    assert clip.selection_s == clip.duration_s
    assert clip.staged.read_bytes() == src.read_bytes()


def test_a_long_wav_is_trimmed_to_its_selection(service: ClipService, outside: Path) -> None:
    src = write_clip(outside / "long.wav", 20.0, subtype="PCM_24", pauses=[(12.0, 12.5)])
    clip = service.stage_file(src)
    assert clip.duration_s == pytest.approx(20.0) and clip.selection_s == pytest.approx(12.0, abs=0.03)
    info = sf.info(str(clip.staged))
    assert info.subtype == "PCM_24" and info.duration == pytest.approx(clip.selection_s, abs=1e-3)


@pytest.mark.parametrize(("name", "fmt"), [("hello.mp3", "MP3"), ("hello.ogg", "OGG"), ("hello.flac", "FLAC")])
def test_a_short_mp3_ogg_or_flac_from_elsewhere_becomes_a_real_pcm16_wav(service: ClipService, outside: Path,
                                                                         name: str, fmt: str) -> None:
    src = write_clip(outside / name, 6.0)
    before = sha(src)
    clip = service.stage_file(src)
    assert (clip.fmt, clip.in_library, clip.name) == (fmt, False, name)
    assert clip.staged.suffix == ".wav" and clip.staged.parent == service.staging
    assert_pcm16_wav(clip.staged, clip.duration_s)
    assert clip.staged.read_bytes()[:4] != src.read_bytes()[:4]
    assert sha(src) == before


@pytest.mark.parametrize("name", ["inside.mp3", "inside.wav", "inside.flac"])
def test_a_short_clip_inside_the_voices_folder_stays_in_place(service: ClipService, name: str) -> None:
    src = write_clip(service.voices / name, 7.0)
    clip = service.stage_file(src)
    assert clip.in_library and clip.staged == src and clip.source == src
    assert staging_files(service) == []


@pytest.mark.skipif(os.name != "nt", reason="NTFS paths ignore case")
def test_the_voices_folder_matches_whatever_case_the_path_uses(service: ClipService) -> None:
    src = write_clip(service.voices / "Mixed.wav", 7.0)
    clip = service.stage_file(Path(str(src).upper()))
    assert clip.in_library and staging_files(service) == []


def test_a_long_clip_inside_the_voices_folder_is_trimmed_into_staging(service: ClipService) -> None:
    src = write_clip(service.voices / "long.wav", 18.0)
    clip = service.stage_file(src)
    assert not clip.in_library and clip.staged.parent == service.staging
    assert sf.info(str(clip.staged)).duration == pytest.approx(15.0, abs=1e-3)
    assert src.is_file()


def test_unreadable_clips_raise_value_error_and_stage_nothing(service: ClipService, outside: Path) -> None:
    text = outside / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="use WAV, FLAC, OGG or MP3"):
        service.stage_file(text)
    fake = outside / "fake.wav"
    fake.write_bytes(b"RIFF....not a wave file")
    with pytest.raises(ValueError):
        service.stage_file(fake)
    with pytest.raises(ValueError, match="file not found"):
        service.stage_file(outside / "missing.wav")
    assert staging_files(service) == []


def test_stage_bytes_turns_an_upload_into_a_staged_wav(service: ClipService, outside: Path) -> None:
    mp3 = write_clip(outside / "drop.mp3", 6.0)
    clip = service.stage_bytes("C:\\Users\\me\\drop.mp3", mp3.read_bytes())
    assert (clip.name, clip.source, clip.in_library, clip.fmt) == ("drop.mp3", None, False, "MP3")
    assert_pcm16_wav(clip.staged, clip.duration_s)
    wav = write_clip(outside / "drop.wav", 5.0)
    same = service.stage_bytes("drop.wav", wav.read_bytes())
    assert same.staged.read_bytes() == wav.read_bytes()
    assert staging_files(service) == sorted([f"{clip.clip_id}.wav", f"{same.clip_id}.wav"])
    with pytest.raises(ValueError, match="use WAV, FLAC, OGG or MP3"):
        service.stage_bytes("drop.m4a", b"x")
    with pytest.raises(ValueError):
        service.stage_bytes("broken.wav", b"garbage")
    assert len(staging_files(service)) == 2                             # nothing left from the failures


def test_stage_bytes_refuses_files_over_500_mb(service: ClipService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clips_module, "MAX_CLIP_BYTES", 10)
    with pytest.raises(ValueError, match="Clip file is over 500 MB"):
        service.stage_bytes("big.wav", b"x" * 11)


# ---------- commit ----------

def test_commit_moves_the_staged_clip_to_voice_ref_wav(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    staged_bytes = clip.staged.read_bytes()
    path, created = service.commit(clip, "rin")
    assert (path, created) == (service.voices / "rin_ref.wav", True)
    assert path.is_absolute() and path.read_bytes() == staged_bytes
    assert not clip.staged.exists()
    assert service.staged_path(clip.clip_id) == path                   # playback keeps working
    assert service.commit(clip, "rin") == (path, False)                # again: the same file, nothing new


def test_commit_reuses_a_file_with_identical_bytes(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    existing = service.voices / "rin_ref.wav"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(clip.staged.read_bytes())
    stamp = existing.stat().st_mtime_ns
    assert service.commit(clip, "rin") == (existing, False)
    assert existing.stat().st_mtime_ns == stamp
    assert not clip.staged.exists()
    assert service.commit(clip, "rin") == (existing, False)            # a retry after a failed bake


def test_commit_never_overwrites_a_different_file(service: ClipService, outside: Path) -> None:
    service.voices.mkdir(parents=True)
    first = service.voices / "rin_ref.wav"
    second = service.voices / "rin_ref-2.wav"
    first.write_bytes(b"someone else's clip")
    second.write_bytes(b"another one")
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    path, created = service.commit(clip, "rin")
    assert (path, created) == (service.voices / "rin_ref-3.wav", True)
    assert first.read_bytes() == b"someone else's clip" and second.read_bytes() == b"another one"


def test_commit_of_an_in_place_clip_writes_nothing(service: ClipService) -> None:
    src = write_clip(service.voices / "aiko_ref.wav", 9.0)
    clip = service.stage_file(src)
    assert service.commit(clip, "aiko") == (src, False)
    assert sorted(p.name for p in service.voices.iterdir()) == ["aiko_ref.wav"]


def test_commit_refuses_a_bad_voice_id_and_a_vanished_clip(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    for bad in ("../rin", "rin\\x", "", "a" * 65, "rin.wav"):
        with pytest.raises(ValueError):
            service.commit(clip, bad)
    clip.staged.unlink()
    with pytest.raises(FileNotFoundError, match="pick the clip again"):
        service.commit(clip, "rin")


def test_commit_copies_when_hard_links_are_not_available(service: ClipService, outside: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    def no_links(src: object, dst: object) -> None:
        raise OSError(18, "Invalid cross-device link")                 # EXDEV: not a FileExistsError

    monkeypatch.setattr(clips_module.os, "link", no_links)
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    data = clip.staged.read_bytes()
    path, created = service.commit(clip, "rin")
    assert created and path.read_bytes() == data and not clip.staged.exists()


def test_rollback_moves_a_created_file_back_to_staging(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    data = clip.staged.read_bytes()
    path, created = service.commit(clip, "rin")
    assert created
    service.rollback(clip, path)
    assert not path.exists() and clip.staged.read_bytes() == data
    assert service.staged_path(clip.clip_id) == clip.staged
    assert service.commit(clip, "rin") == (path, True)                 # the draft can forge again
    with pytest.raises(ValueError):
        service.rollback(clip, outside / "rin.wav")                     # only the file its commit created


def test_rollback_never_touches_a_file_the_commit_only_reused(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "rin.wav", 8.0))
    existing = service.voices / "rin_ref.wav"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(clip.staged.read_bytes())
    path, created = service.commit(clip, "rin")
    assert (path, created) == (existing, False)
    with pytest.raises(ValueError):
        service.rollback(clip, path)
    assert existing.is_file()
    in_place = service.stage_file(existing)
    service.rollback(in_place, existing)                                # in place: nothing to undo
    assert existing.is_file()


# ---------- discard and sweep ----------

def test_discard_deletes_the_staging_copy_but_never_an_in_place_clip(service: ClipService, outside: Path) -> None:
    staged = service.stage_file(write_clip(outside / "a.wav", 8.0))
    service.discard(staged)
    assert not staged.staged.exists() and service.staged_path(staged.clip_id) is None
    service.discard(staged)                                             # twice is fine
    in_place = service.stage_file(write_clip(service.voices / "b.wav", 8.0))
    service.discard(in_place)
    assert in_place.staged.is_file() and service.staged_path(in_place.clip_id) is None


def test_sweep_deletes_old_staging_files_only(service: ClipService, outside: Path) -> None:
    live = service.stage_file(write_clip(outside / "live.wav", 8.0))
    service.staging.mkdir(parents=True, exist_ok=True)
    old = [service.staging / name for name in ("old.wav", "old.wav.part", "test-rin.wav")]
    fresh = service.staging / "fresh.wav"
    for path in (*old, fresh):
        path.write_bytes(b"x")
    long_ago, a_minute_ago = time.time() - 2 * 86_400, time.time() - 60
    for path in (*old, live.staged):
        os.utime(path, (long_ago, long_ago))
    os.utime(fresh, (a_minute_ago, a_minute_ago))
    assert service.sweep() == 3
    assert staging_files(service) == sorted([live.staged.name, "fresh.wav"])
    assert service.sweep(max_age_s=30) == 1                             # fresh.wav; the live clip stays
    assert live.staged.is_file()


def test_sweep_without_a_staging_folder(service: ClipService) -> None:
    assert service.sweep() == 0


# ---------- bars and lookups ----------

def test_bars_are_cached_by_path_mtime_size_and_count(service: ClipService, outside: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, int]] = []
    real = clips_module.probe_clip

    def counting(path: Path, *, n_bars: int = 230):
        calls.append((path, n_bars))
        return real(path, n_bars=n_bars)

    monkeypatch.setattr(clips_module, "probe_clip", counting)
    src = write_clip(outside / "voice.wav", 12.0)
    bars = service.bars(src, 138)
    assert len(bars) == 138 and max(bars) == pytest.approx(1.0) and min(bars) >= 0.0
    assert service.bars(src, 138) is bars
    assert len(calls) == 1
    assert len(service.bars(src, 230)) == 230 and len(calls) == 2
    write_clip(src, 9.0)
    assert len(service.bars(src, 138)) == 138 and len(calls) == 3
    assert service.bars(outside / "missing.wav", 138) == ()
    broken = outside / "broken.wav"
    broken.write_bytes(b"nope")
    assert service.bars(broken, 138) == ()
    with pytest.raises(ValueError):
        service.bars(src, 0)


def test_staged_path_serves_only_known_clips_and_test_lines(service: ClipService, outside: Path) -> None:
    clip = service.stage_file(write_clip(outside / "a.wav", 8.0))
    assert service.staged_path(clip.clip_id) == clip.staged
    assert service.staged_path("0" * 32) is None                        # well formed, never staged here
    test_line = service.test_line_path("rin")
    assert test_line == service.staging / "test-rin.wav"
    assert service.staged_path("test-rin") is None                      # not written yet
    test_line.write_bytes(b"RIFF")
    assert service.staged_path("test-rin") == test_line
    for bad in ("../a.wav", "test-../x", clip.clip_id.upper(), "test-", "", "staging"):
        assert service.staged_path(bad) is None
    with pytest.raises(ValueError):
        service.test_line_path("../rin")
