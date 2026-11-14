"""TakesStore (§4.15.1): WAV then index, atomic rewrites, numbers never reused, stars, peaks,
purge through an injected trash, export, Save As and the missing-file check.

Everything runs in tmp_path; the trash is a fake that moves files into a scratch folder,
so nothing ever reaches the real Recycle Bin.
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import threading
import wave
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ui.core.services import takes_store
from ui.core.services.takes_store import (
    PEAK_BUCKETS,
    TRASH_RETRY_S,
    NewTake,
    PurgeResult,
    TakeNotFoundError,
    TakeNotListedError,
    TakesStore,
    pool_peaks,
    resolve_stored,
    stored_path,
    take_file_name,
)

SR = 24000
LOCKED = "The process cannot access the file because it is being used by another process"


# ---- helpers -------------------------------------------------------------------------------------

class Sleeps:
    """Stands in for the store's retry pause: records it, runs on_sleep, never waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.on_sleep: Callable[[], None] | None = None

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()


class Bin:
    """A fake trash: moves each file into a scratch folder; can refuse a file name n times."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.calls: list[Path] = []
        self.refuse: dict[str, int] = {}

    def __call__(self, path: Path) -> None:
        path = Path(path)
        self.calls.append(path)
        if self.refuse.get(path.name, 0) > 0:
            self.refuse[path.name] -= 1
            raise PermissionError(13, LOCKED, str(path))
        self.folder.mkdir(parents=True, exist_ok=True)
        os.replace(path, self.folder / path.name)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Sleeps:
    recorder = Sleeps()
    monkeypatch.setattr(takes_store, "_sleep", recorder)
    return recorder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(root: Path) -> TakesStore:
    return open_store(root)


def takes_dir(root: Path) -> Path:
    return root / "outputs" / "takes"


def open_store(root: Path, **options) -> TakesStore:
    store = TakesStore(takes_dir(root) / "index.json", root)
    store.load(**options)
    return store


def read_index(root: Path) -> dict:
    return json.loads((takes_dir(root) / "index.json").read_text(encoding="utf-8"))


def wav_bytes(samples: np.ndarray, sr: int = SR) -> bytes:
    """The engine's take format: a float32 mono WAV, as torchaudio's soundfile backend writes it."""
    buffer = io.BytesIO()
    sf.write(buffer, np.asarray(samples, dtype=np.float32), sr, format="WAV", subtype="FLOAT")
    return buffer.getvalue()


def tone(seconds: float = 0.5, level: float = 0.3) -> bytes:
    t = np.arange(int(round(seconds * SR))) / SR
    return wav_bytes(level * np.sin(2 * np.pi * 220.0 * t))


def new_take(**changes) -> NewTake:
    base = NewTake(voice="aiko", backend="chatterbox-multilingual", language="ja", text="おはよう！今日はいい天気ですね。",
                   exaggeration=0.5, cfg_weight=0.5, gen_seconds=6.9, session="2026-11-13T13:40:12+01:00",
                   tokens=115, hit_token_limit=False)
    return replace(base, **changes)


def refuse(path: Path, text: str) -> None:
    raise PermissionError(13, "Access is denied")


# ---- load ----------------------------------------------------------------------------------------

def test_a_missing_index_loads_empty_and_writes_nothing(root: Path) -> None:
    store = TakesStore(takes_dir(root) / "index.json", root)
    report = store.load()
    assert (report.count, report.next_id, report.notice, report.read_only) == (0, 1, "", False)
    assert store.takes == () and store.next_id == 1
    assert not root.exists()


def test_changes_before_load_are_refused(root: Path) -> None:
    store = TakesStore(takes_dir(root) / "index.json", root)
    with pytest.raises(RuntimeError, match="load"):
        store.add(tone(), new_take(), takes_dir(root))
    assert not root.exists()


def test_a_hand_written_index_loads_with_defaults_newest_first(root: Path) -> None:
    folder = takes_dir(root)
    folder.mkdir(parents=True)
    entry = {"voice": "aiko", "backend": "chatterbox-multilingual", "text": "x", "seconds": 4.6, "gen_seconds": 6.9}
    data = {"version": 1, "next_id": 2, "takes": [{"id": 3, "wav": "outputs/takes/aiko_take3.wav", **entry},
                                                  {"id": 7, "wav": "outputs/takes/aiko_take7.wav", **entry}]}
    (folder / "index.json").write_text(json.dumps(data), encoding="utf-8-sig")
    store = TakesStore(folder / "index.json", root)
    report = store.load()
    assert [take.id for take in store.takes] == [7, 3]
    assert report.next_id == store.next_id == 8          # never below the highest listed number + 1
    take = store.get(7)
    assert (take.language, take.exaggeration, take.tokens, take.starred, take.sample_rate, take.peaks) == (
        "en", None, None, False, 24000, ())


CORRUPT = [
    pytest.param(b"{not json", id="not-json"),
    pytest.param(b"\xff\xfe\x00garbage", id="not-utf8"),
    pytest.param(b"[1, 2, 3]", id="not-an-object"),
    pytest.param(b'{"version": 2, "next_id": 1, "takes": []}', id="other-version"),
    pytest.param(b'{"version": 1, "next_id": 1, "takes": {}}', id="takes-not-a-list"),
    pytest.param(b'{"version": 1, "next_id": 3, "takes": [{"id": 1, "voice": "a", "backend": "b", "text": "t"}]}',
                 id="take-without-wav"),
    pytest.param(b'{"version": 1, "next_id": 3, "takes": [{"id": 1, "voice": "a", "backend": "b", "text": "t", '
                 b'"wav": "w.wav", "seconds": NaN}]}', id="nan-seconds"),
    pytest.param(b'{"version": 1, "next_id": 3, "takes": [{"id": 1, "voice": "a", "backend": "b", '
                 b'"text": "\\ud800", "wav": "w.wav"}]}', id="unpaired-surrogate"),
    pytest.param(b'{"version": 1, "next_id": 3, "takes": [{"id": 1, "voice": "a", "backend": "b", "text": "t", '
                 b'"wav": "a\\u0000.wav"}]}', id="null-in-wav-path"),
    pytest.param(b'{"version": 1, "next_id": 3, "takes": [{"id": 1, "voice": "a", "backend": "b", "text": "t", '
                 b'"wav": "a.wav"}, {"id": 1, "voice": "a", "backend": "b", "text": "t", "wav": "b.wav"}]}',
                 id="duplicate-ids"),
]


@pytest.mark.parametrize("content", CORRUPT)
def test_a_corrupt_index_is_quarantined_and_numbering_recovers(root: Path, content: bytes) -> None:
    folder = takes_dir(root)
    folder.mkdir(parents=True)
    index = folder / "index.json"
    index.write_bytes(content)
    (folder / "aiko_take5.wav").write_bytes(b"five")
    (folder / "rin_take12-2.wav").write_bytes(b"twelve")
    store = TakesStore(index, root)
    report = store.load()
    assert re.fullmatch(r"index\.json\.corrupt-\d+", report.moved_to)
    assert (folder / report.moved_to).read_bytes() == content and not index.exists()
    assert report.notice == (f"The takes list couldn't be read · moved it to {report.moved_to} · "
                             f"the WAVs are untouched")
    assert not report.read_only and store.takes == ()
    assert report.next_id == store.next_id == 13         # 1 + the highest <voice>_take<N> on disk
    assert (folder / "aiko_take5.wav").read_bytes() == b"five"
    assert store.add(tone(), new_take(), folder).id == 13
    assert [take["id"] for take in read_index(root)["takes"]] == [13]


def test_an_unreadable_index_is_never_overwritten(root: Path, sleeps: Sleeps) -> None:
    folder = takes_dir(root)
    (folder / "index.json").mkdir(parents=True)          # reading a folder fails with an OSError
    store = TakesStore(folder / "index.json", root)
    report = store.load()
    assert report.read_only and report.error and not report.moved_to
    assert report.notice == f"The takes list couldn't be read · {report.error} · the WAVs are untouched"
    assert sleeps.calls == [0.05] * 4                    # five reads, 50 ms apart
    with pytest.raises(TakeNotListedError, match="couldn't be listed: the takes list couldn't be read"):
        store.add(tone(), new_take(), folder)
    assert (folder / "aiko_take1.wav").is_file()         # the audio is kept
    assert (folder / "index.json").is_dir() and store.takes == ()


def test_load_deletes_leftover_part_and_temp_files(root: Path) -> None:
    folder = takes_dir(root)
    custom = root / "custom"
    folder.mkdir(parents=True)
    custom.mkdir()
    (folder / "aiko_take3.wav.part").write_bytes(b"x")
    (custom / "rin_take9-2.wav.part").write_bytes(b"x")
    (folder / f"index.json.tmp-123-{'0' * 32}").write_text("{", encoding="utf-8")
    (folder / "notes.part").write_bytes(b"keep")
    (folder / "aiko_take2.wav").write_bytes(b"keep")
    report = TakesStore(folder / "index.json", root).load(folders=[Path("custom")])   # relative: in the data root
    assert report.cleaned == 3
    assert sorted(p.name for p in folder.iterdir()) == ["aiko_take2.wav", "notes.part"]
    assert list(custom.iterdir()) == []
    assert report.next_id == 3                            # part files never count as takes


# ---- add -----------------------------------------------------------------------------------------

def test_add_writes_the_exact_wav_before_the_index(store: TakesStore, root: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    folder = takes_dir(root)
    wav = tone(0.5)
    seen: list[list[str]] = []
    real = takes_store._write_atomic

    def spy(path: Path, text: str) -> None:
        seen.append(sorted(p.name for p in folder.iterdir()))
        real(path, text)

    monkeypatch.setattr(takes_store, "_write_atomic", spy)
    take = store.add(wav, new_take(), folder)
    assert seen == [["aiko_take1.wav"]]                   # in place, no .part left, before the index
    assert (folder / "aiko_take1.wav").read_bytes() == wav
    assert (take.id, take.wav, take.seconds, take.sample_rate) == (1, "outputs/takes/aiko_take1.wav", 0.5, SR)
    assert (take.voice, take.language, take.tokens, take.session) == ("aiko", "ja", 115, new_take().session)
    assert datetime.fromisoformat(take.created).utcoffset() is not None
    assert store.takes == (take,) and store.get(1) == take and store.next_id == 2
    raw = (folder / "index.json").read_text(encoding="utf-8")
    assert "おはよう" in raw                               # readable UTF-8, not \u escapes
    index = json.loads(raw)
    assert (index["version"], index["next_id"]) == (1, 2)
    assert index["takes"][0]["id"] == 1 and "missing" not in index["takes"][0]


def test_peaks_are_absolute_per_bucket(store: TakesStore, root: Path) -> None:
    half = SR // 2
    loud = np.zeros(SR)
    loud[half:] = 0.5 * np.where(np.arange(half) % 2 == 0, 1.0, -1.0)
    a = store.add(wav_bytes(loud), new_take(), takes_dir(root))
    b = store.add(wav_bytes(loud * 0.5), new_take(), takes_dir(root))
    assert len(a.peaks) == PEAK_BUCKETS and a.seconds == 1.0
    assert a.peaks == (0,) * 115 + (50,) * 115
    assert b.peaks == (0,) * 115 + (25,) * 115            # one scale: a quieter take has lower bars


def test_a_pcm_wav_from_the_standard_library_is_measured_too(store: TakesStore, root: Path) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(SR)
        sink.writeframes(struct.pack("<h", 16384) * SR)
    take = store.add(buffer.getvalue(), new_take(), takes_dir(root))
    assert take.seconds == 1.0 and set(take.peaks) == {50}


def test_a_taken_file_name_gets_a_number(store: TakesStore, root: Path) -> None:
    folder = takes_dir(root)
    folder.mkdir(parents=True)
    (folder / "aiko_take1.wav").write_bytes(b"stray")
    (folder / "aiko_take1-2.wav").write_bytes(b"stray")
    take = store.add(tone(), new_take(), folder)
    assert take.wav == "outputs/takes/aiko_take1-3.wav"
    assert (folder / "aiko_take1.wav").read_bytes() == b"stray"


def test_paths_inside_the_data_root_are_stored_relative(store: TakesStore, root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    inside = store.add(tone(), new_take(), Path("outputs/takes"))     # relative = in the data root
    outside = store.add(tone(), new_take(voice="marco"), elsewhere)
    assert inside.wav == "outputs/takes/aiko_take1.wav"
    assert outside.wav == str(elsewhere / "marco_take2.wav")
    assert store.path_of(1) == takes_dir(root) / "aiko_take1.wav"
    assert store.path_of(2) == elsewhere / "marco_take2.wav" and store.path_of(2).is_file()
    assert store.path_of(3) is None


@pytest.mark.parametrize("payload", [b"", b"ID3\x04 an mp3", b"RIFF\x24\x00\x00\x00WAVEjunk"],
                         ids=["empty", "mp3", "broken-riff"])
def test_add_refuses_audio_that_is_not_a_wav(store: TakesStore, root: Path, payload: bytes) -> None:
    with pytest.raises(ValueError, match="not a"):
        store.add(payload, new_take(), takes_dir(root))
    assert store.next_id == 1 and not root.exists()


@pytest.mark.parametrize("change", [{"gen_seconds": float("nan")}, {"exaggeration": float("inf")},
                                    {"tokens": -1}, {"language": None}, {"text": "a\ud800b"}],
                         ids=["nan-gen-seconds", "infinite-expressiveness", "negative-tokens", "language-not-text",
                              "unpaired-surrogate"])
def test_add_refuses_values_the_index_cannot_hold(store: TakesStore, root: Path, change: dict) -> None:
    with pytest.raises(ValueError):
        store.add(tone(), replace(new_take(), **change), takes_dir(root))
    assert not root.exists()


def test_numpy_numbers_are_saved_as_plain_numbers(store: TakesStore, root: Path) -> None:
    rec = new_take(gen_seconds=np.float32(6.5), tokens=np.int64(115), exaggeration=np.float32(0.75))
    take = store.add(tone(), rec, takes_dir(root))
    assert (type(take.gen_seconds), type(take.tokens), type(take.exaggeration)) == (float, int, float)
    saved = read_index(root)["takes"][0]
    assert (saved["gen_seconds"], saved["tokens"], saved["exaggeration"]) == (6.5, 115, 0.75)


def test_a_failed_index_write_keeps_the_wav_and_never_reuses_its_number(store: TakesStore, root: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    folder = takes_dir(root)
    real = takes_store._write_atomic
    failing = [True]

    def flaky(path: Path, text: str) -> None:
        if failing[0]:
            raise PermissionError(13, "Access is denied")
        real(path, text)

    monkeypatch.setattr(takes_store, "_write_atomic", flaky)
    with pytest.raises(TakeNotListedError) as caught:
        store.add(tone(), new_take(), folder)
    path = folder / "aiko_take1.wav"
    assert caught.value.path == path and path.is_file()
    assert str(caught.value) == f"Take 1 is saved at {path} but couldn't be listed: Access is denied"
    assert store.takes == () and store.next_id == 2
    failing[0] = False
    assert store.add(tone(), new_take(), folder).wav == "outputs/takes/aiko_take2.wav"
    (folder / "index.json").unlink()
    assert open_store(root).next_id == 3                  # even without the index: the numbers on disk


def test_concurrent_adds_get_distinct_numbers(store: TakesStore, root: Path) -> None:
    wav = tone(0.1)
    workers = [threading.Thread(target=store.add, args=(wav, new_take(), takes_dir(root))) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert [take.id for take in store.takes] == list(range(8, 0, -1))
    assert len({take.wav for take in store.takes}) == 8
    assert [take["id"] for take in read_index(root)["takes"]] == list(range(8, 0, -1))


# ---- numbering, stars, atomic writes -------------------------------------------------------------

def test_take_numbers_are_never_reused_after_purging_the_newest(store: TakesStore, root: Path) -> None:
    folder = takes_dir(root)
    for _ in range(3):
        store.add(tone(), new_take(), folder)
    assert store.purge_unstarred({3}, trash=Bin(root / "bin")).moved == (3,)
    assert store.add(tone(), new_take(), folder).id == 4
    again = open_store(root)
    assert [take.id for take in again.takes] == [4, 2, 1] and again.next_id == 5


def test_takes_and_stars_survive_a_restart(store: TakesStore, root: Path) -> None:
    folder = takes_dir(root)
    first = store.add(tone(), new_take(), folder)
    store.add(tone(0.3), new_take(voice="host-turbo", backend="chatterbox-turbo", language="en",
                                  text="Okay, okay [chuckle] it works.", exaggeration=None, cfg_weight=None,
                                  tokens=None, hit_token_limit=True), folder)
    starred = store.set_starred(2, True)
    again = open_store(root)
    assert again.takes == (starred, first)
    assert again.get(2).starred and again.get(2).cfg_weight is None and again.get(2).hit_token_limit
    assert again.next_id == 3


def test_set_starred_without_a_change_writes_nothing(store: TakesStore, root: Path) -> None:
    take = store.add(tone(), new_take(), takes_dir(root))
    rev = store.rev
    assert store.set_starred(1, False) is take and store.rev == rev
    with pytest.raises(TakeNotFoundError, match="^Take 9 is not in the takes list$"):
        store.set_starred(9, True)


def test_a_failed_star_leaves_memory_and_disk_unchanged(store: TakesStore, root: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    index = takes_dir(root) / "index.json"
    before_disk = index.read_bytes()
    before = (store.takes, store.rev)
    monkeypatch.setattr(takes_store, "_write_atomic", refuse)
    with pytest.raises(OSError, match="Access is denied"):
        store.set_starred(1, True)
    assert (store.takes, store.rev) == before and not store.get(1).starred
    assert index.read_bytes() == before_disk


def test_the_index_is_replaced_whole_after_an_fsync(store: TakesStore, root: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    index = takes_dir(root) / "index.json"
    before = index.read_bytes()
    steps: list[object] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        steps.append("fsync")
        real_fsync(fd)

    def blocked(src: Path, dst: Path) -> None:
        steps.append((Path(src).name, Path(dst).name))
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(takes_store.os, "fsync", fsync)
    monkeypatch.setattr(takes_store, "_replace", blocked)
    with pytest.raises(OSError):
        store.set_starred(1, True)
    assert steps[0] == "fsync"
    temp, target = steps[1]
    assert re.fullmatch(rf"index\.json\.tmp-{os.getpid()}-[0-9a-f]{{32}}", temp) and target == "index.json"
    assert index.read_bytes() == before                   # readers never see a torn index
    assert sorted(p.name for p in takes_dir(root).iterdir()) == ["aiko_take1.wav", "index.json"]


# ---- purge ---------------------------------------------------------------------------------------

def test_purge_works_one_file_at_a_time(store: TakesStore, root: Path, sleeps: Sleeps) -> None:
    folder = takes_dir(root)
    for _ in range(5):
        store.add(tone(), new_take(), folder)
    store.set_starred(1, True)
    (folder / "aiko_take4.wav").unlink()                  # already gone
    trash = Bin(root / "bin")
    trash.refuse["aiko_take3.wav"] = 99                   # locked the whole time
    trash.refuse["aiko_take5.wav"] = 1                    # locked for the first try only
    result = store.purge_unstarred({1, 2, 3, 4, 5, 99}, trash=trash)
    assert result == PurgeResult(moved=(2, 5), missing=(4,), failed=((3, LOCKED),))
    assert [p.name for p in trash.calls] == ["aiko_take2.wav"] + ["aiko_take3.wav"] * 3 + ["aiko_take5.wav"] * 2
    assert sleeps.calls == [TRASH_RETRY_S] * 3            # three tries, 150 ms apart
    assert [take.id for take in store.takes] == [3, 1]    # the failed and the starred take stay
    assert [take["id"] for take in read_index(root)["takes"]] == [3, 1]
    assert sorted(p.name for p in (root / "bin").iterdir()) == ["aiko_take2.wav", "aiko_take5.wav"]
    assert (folder / "aiko_take1.wav").is_file() and (folder / "aiko_take3.wav").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to move a file another handle has open")
def test_purge_retries_a_wav_that_is_still_open(store: TakesStore, root: Path, sleeps: Sleeps) -> None:
    folder = takes_dir(root)
    store.add(tone(), new_take(), folder)
    store.add(tone(), new_take(), folder)
    streaming = open(folder / "aiko_take1.wav", "rb")     # a player still streams take 1 ...
    sleeps.on_sleep = streaming.close                     # ... and lets go during the first pause
    playing = open(folder / "aiko_take2.wav", "rb")       # take 2 stays open throughout
    try:
        result = store.purge_unstarred({1, 2}, trash=Bin(root / "bin"))
    finally:
        playing.close()
        streaming.close()
    assert result.moved == (1,) and [take_id for take_id, _ in result.failed] == [2]
    assert [take.id for take in store.takes] == [2]


def test_purge_ignores_starred_and_unknown_ids(store: TakesStore, root: Path) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    store.set_starred(1, True)
    rev = store.rev
    trash = Bin(root / "bin")
    assert store.purge_unstarred({1, 7}, trash=trash) == PurgeResult()
    assert trash.calls == [] and store.rev == rev


def test_purge_rewrites_the_index_once(store: TakesStore, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(4):
        store.add(tone(), new_take(), takes_dir(root))
    writes: list[Path] = []
    real = takes_store._write_atomic

    def counted(path: Path, text: str) -> None:
        writes.append(path)
        real(path, text)

    monkeypatch.setattr(takes_store, "_write_atomic", counted)
    store.purge_unstarred({1, 2, 3, 4}, trash=Bin(root / "bin"))
    assert len(writes) == 1 and store.takes == () and store.next_id == 5


def test_a_purge_whose_index_write_fails_flags_the_takes_missing(store: TakesStore, root: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    store.add(tone(), new_take(), takes_dir(root))
    monkeypatch.setattr(takes_store, "_write_atomic", refuse)
    with pytest.raises(OSError, match="the takes list couldn't be updated: Access is denied"):
        store.purge_unstarred({1, 2}, trash=Bin(root / "bin"))
    assert [(take.id, take.missing) for take in store.takes] == [(2, True), (1, True)]


# ---- export, Save As, missing files --------------------------------------------------------------

def test_export_copies_and_never_overwrites(store: TakesStore, root: Path) -> None:
    folder = takes_dir(root)
    store.add(tone(0.4), new_take(), folder)
    store.add(tone(0.6), new_take(voice="marco"), folder)
    store.add(tone(), new_take(voice="rin"), folder)
    store.path_of(3).unlink()
    out = root / "export"
    out.mkdir()
    (out / "aiko_take1.wav").write_bytes(b"mine")
    result = store.export({1, 2, 3, 9}, out)
    assert [p.name for p in result.copied] == ["aiko_take1 (2).wav", "marco_take2.wav"]
    assert result.missing == (3, 9) and result.failed == ()
    assert (out / "aiko_take1.wav").read_bytes() == b"mine"
    assert (out / "aiko_take1 (2).wav").read_bytes() == store.path_of(1).read_bytes()
    assert os.stat(out / "marco_take2.wav").st_mtime == pytest.approx(os.stat(store.path_of(2)).st_mtime, abs=0.01)
    again = store.export({1, 2}, out)
    assert [p.name for p in again.copied] == ["aiko_take1 (3).wav", "marco_take2 (2).wav"]


def test_export_creates_the_folder(store: TakesStore, root: Path) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    result = store.export([1], root / "new" / "folder")
    assert [p.name for p in result.copied] == ["aiko_take1.wav"] and result.copied[0].is_file()


def test_copy_to_saves_as_and_replaces_the_target(store: TakesStore, root: Path) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    dest = root / "picked" / "mine.wav"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    assert store.copy_to(1, dest) == dest
    assert dest.read_bytes() == store.path_of(1).read_bytes()
    assert [p.name for p in dest.parent.iterdir()] == ["mine.wav"]       # no temp file left
    assert store.copy_to(1, store.path_of(1)) == store.path_of(1)       # onto itself: nothing to do
    with pytest.raises(TakeNotFoundError):
        store.copy_to(9, dest)
    store.path_of(1).unlink()
    with pytest.raises(FileNotFoundError, match="^Take 1's WAV is missing: "):
        store.copy_to(1, dest)


def test_check_files_flags_missing_wavs(store: TakesStore, root: Path) -> None:
    store.add(tone(), new_take(), takes_dir(root))
    store.add(tone(), new_take(), takes_dir(root))
    aside = root / "aside.wav"
    store.path_of(1).rename(aside)
    rev = store.rev
    assert store.check_files() == (1,)
    assert store.get(1).missing and not store.get(2).missing and store.rev == rev + 1
    assert store.check_files() == (1,) and store.rev == rev + 1           # nothing changed: not published
    assert all("missing" not in take for take in read_index(root)["takes"])
    aside.rename(store.path_of(1))
    assert store.check_files() == () and not store.get(1).missing


# ---- helpers -------------------------------------------------------------------------------------

def test_take_file_name_is_the_save_as_default(store: TakesStore, root: Path) -> None:
    take = store.add(tone(), new_take(voice="host-turbo"), takes_dir(root))
    assert take_file_name(take) == "host-turbo_take1.wav"
    assert take_file_name(replace(take, voice="../x y", id=7)) == "___x_y_take7.wav"


def test_stored_paths_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "data"
    inside = root / "outputs" / "takes" / "a.wav"
    outside = tmp_path / "elsewhere" / "b.wav"
    assert stored_path(inside, root) == "outputs/takes/a.wav"
    assert resolve_stored("outputs/takes/a.wav", root) == inside
    assert stored_path(outside, root) == str(outside)
    assert resolve_stored(str(outside), root) == outside


def test_pool_peaks_max_pools_every_value() -> None:
    pooled = pool_peaks(list(range(230)), 110)
    assert len(pooled) == 110 and pooled[0] == 1 and pooled[-1] == 229
    assert pool_peaks([5, 1, 9], 2) == (5, 9)
    assert pool_peaks([7], 3) == (0, 0, 7)
    with pytest.raises(ValueError):
        pool_peaks([1], 0)
