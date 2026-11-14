"""VoiceRegistry: register() keeps history, update(), find_casefold(), and the file rules."""
import itertools
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import filelock
import pytest
from filelock import FileLock

import engine.registry as registry_module
from engine.registry import VoiceRegistry

ROOT = Path(__file__).resolve().parents[2]
MTL = "chatterbox-multilingual"
TURBO = "chatterbox-turbo"
TODAYS_KEYS = {"backend", "reference_wav", "exaggeration", "language", "created_at"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Registry timestamps T0, T1, ... (the Windows clock ticks only every 15.6 ms)."""
    issued: list[str] = []
    seconds = itertools.count()

    def fake_now() -> str:
        issued.append(f"2026-11-13T05:00:{next(seconds):02d}+00:00")
        return issued[-1]

    monkeypatch.setattr(registry_module, "_now_iso", fake_now)
    return issued


@pytest.fixture
def reg(tmp_path: Path) -> VoiceRegistry:
    return VoiceRegistry(tmp_path / "cache" / "registry.json")


@pytest.fixture
def clip(tmp_path: Path) -> str:
    return str(tmp_path / "voices" / "reference" / "aiko_ref.wav")


@pytest.fixture
def other_clip(tmp_path: Path) -> str:
    return str(tmp_path / "voices" / "reference" / "aiko_new.wav")


def stored(reg: VoiceRegistry) -> dict:
    return json.loads(reg.path.read_text(encoding="utf-8"))


def leftovers(reg: VoiceRegistry) -> list[str]:
    return sorted(p.name for p in reg.path.parent.iterdir() if ".tmp-" in p.name)


# ---------- register ----------


def test_register_new_voice_has_todays_keys_plus_forged_at(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja")
    entry = reg.get("aiko")
    assert set(entry) == TODAYS_KEYS | {"forged_at"}
    assert entry == {
        "backend": MTL,
        "reference_wav": clip,
        "exaggeration": 0.5,
        "language": "ja",
        "created_at": clock[0],
        "forged_at": clock[0],
    }
    assert leftovers(reg) == []


def test_real_timestamps_are_utc_iso(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    entry = reg.get("aiko")
    created = datetime.fromisoformat(entry["created_at"])
    assert created.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - created) < timedelta(minutes=1)
    assert entry["forged_at"] == entry["created_at"]


def test_two_registers_keep_created_at_and_move_forged_at(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja")
    reg.register("aiko", MTL, clip, 0.7, "ja")
    entry = reg.get("aiko")
    assert entry["created_at"] == clock[0]
    assert entry["forged_at"] == clock[1]
    assert entry["exaggeration"] == 0.7


def test_register_without_bake_keeps_previous_forged_at(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja")
    reg.register("aiko", MTL, clip, 0.5, "ja", baked=False)
    entry = reg.get("aiko")
    assert entry["created_at"] == clock[0]
    assert entry["forged_at"] == clock[0]


def test_register_without_bake_and_without_history_has_no_forged_at(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", baked=False)
    entry = reg.get("aiko")
    assert entry["created_at"] == clock[0]
    assert "forged_at" not in entry


def test_register_keeps_created_at_of_a_legacy_entry(reg, clip, clock) -> None:
    reg._write({"aiko": {"backend": MTL, "reference_wav": clip, "exaggeration": 0.5,
                         "language": "en", "created_at": "2026-11-01T00:00:00+00:00"}})
    reg.register("aiko", MTL, clip, 0.5, "en")
    entry = reg.get("aiko")
    assert entry["created_at"] == "2026-11-01T00:00:00+00:00"
    assert entry["forged_at"] == clock[0]


def test_register_stores_meta(reg, clip) -> None:
    meta = {"consent": True, "consent_at": "2026-11-13T05:31:58+00:00", "clip_seconds": 11.2,
            "clip_source": "C:\\clips\\aiko.wav"}
    reg.register("aiko", MTL, clip, 0.5, "ja", meta=meta)
    assert reg.get("aiko")["meta"] == meta


def test_register_with_another_clip_drops_consent(reg, clip, other_clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True, "clip_seconds": 11.2})
    reg.register("aiko", MTL, other_clip, 0.5, "ja")
    entry = reg.get("aiko")
    assert entry["reference_wav"] == other_clip
    assert "meta" not in entry


def test_register_with_another_clip_keeps_only_the_new_meta(reg, clip, other_clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True, "clip_seconds": 11.2})
    reg.register("aiko", MTL, other_clip, 0.5, "ja", meta={"clip_seconds": 9.0})
    assert reg.get("aiko")["meta"] == {"clip_seconds": 9.0}


def test_register_on_another_backend_drops_previous_meta(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    reg.register("aiko", TURBO, clip, 0.5, "en")
    entry = reg.get("aiko")
    assert entry["backend"] == TURBO
    assert "meta" not in entry


def test_register_same_clip_merges_meta(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True, "clip_seconds": 11.2})
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"clip_seconds": 12.0, "consent_at": "x"})
    assert reg.get("aiko")["meta"] == {"consent": True, "clip_seconds": 12.0, "consent_at": "x"}


def test_register_same_clip_without_meta_keeps_consent(reg, clip) -> None:
    # cli.py load --force of the same clip must not wipe what the GUI recorded.
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True})
    reg.register("aiko", MTL, clip, 0.5, "ja")
    assert reg.get("aiko")["meta"] == {"consent": True}


def test_register_meta_none_value_drops_the_key(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True, "clip_seconds": 3.0})
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": None})
    assert reg.get("aiko")["meta"] == {"clip_seconds": 3.0}


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(str.upper, id="case",
                     marks=pytest.mark.skipif(os.name != "nt", reason="NTFS ignores case")),
        pytest.param(lambda p: Path(p).as_posix(), id="forward-slashes"),
        pytest.param(lambda p: str(Path(p).parent / "sub" / ".." / Path(p).name), id="dot-dot"),
    ],
)
def test_same_clip_is_compared_by_normalised_path(reg, clip, spelling) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True})
    reg.register("aiko", MTL, spelling(clip), 0.5, "ja")
    assert reg.get("aiko")["meta"] == {"consent": True}


def test_relative_clip_resolves_against_the_working_directory(
    reg, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    reg.register("aiko", MTL, str(tmp_path / "voices" / "aiko.wav"), 0.5, "en",
                 meta={"consent": True})
    reg.register("aiko", MTL, os.path.join("voices", "aiko.wav"), 0.5, "en")
    assert reg.get("aiko")["meta"] == {"consent": True}


def test_register_stores_a_path_object_as_text(reg, tmp_path) -> None:
    reg.register("aiko", MTL, tmp_path / "aiko.wav", 0.5, "en")  # type: ignore[arg-type]
    assert stored(reg)["aiko"]["reference_wav"] == str(tmp_path / "aiko.wav")


def test_register_leaves_other_voices_alone(reg, clip, other_clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja", meta={"consent": True})
    before = reg.get("aiko")
    reg.register("rin", TURBO, other_clip, 0.5, "en")
    assert reg.get("aiko") == before
    assert reg.list_ids() == ["aiko", "rin"]


def test_failed_register_leaves_the_file_unchanged(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja")
    before = reg.path.read_bytes()
    with pytest.raises(TypeError):
        reg.register("rin", MTL, clip, 0.5, "en", meta={"bad": object()})
    assert reg.path.read_bytes() == before
    assert leftovers(reg) == []


# ---------- update ----------


def test_update_language_keeps_everything_else(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.6, "en", meta={"consent": True})
    before = reg.get("aiko")
    new = reg.update("aiko", language="ja")
    assert new == {**before, "language": "ja"}
    assert reg.get("aiko") == new
    assert len(clock) == 1  # update never stamps a date


def test_update_exaggeration(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    assert reg.update("aiko", exaggeration=1.25)["exaggeration"] == 1.25
    assert reg.get("aiko")["exaggeration"] == 1.25


def test_update_merges_meta(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    reg.update("aiko", meta={"clip_seconds": 11.2})
    assert reg.get("aiko")["meta"] == {"consent": True, "clip_seconds": 11.2}


def test_update_meta_none_removes_the_key(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True, "clip_seconds": 11.2})
    reg.update("aiko", meta={"consent": None})
    assert reg.get("aiko")["meta"] == {"clip_seconds": 11.2}


def test_update_removing_the_last_meta_key_removes_meta(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    new = reg.update("aiko", meta={"consent": None})
    assert "meta" not in new
    assert "meta" not in reg.get("aiko")


def test_update_never_touches_identity_or_dates(reg, clip, clock) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    reg.update("aiko", language="ja", exaggeration=0.9, meta={"consent": False})
    entry = reg.get("aiko")
    assert (entry["backend"], entry["reference_wav"]) == (MTL, clip)
    assert entry["created_at"] == entry["forged_at"] == clock[0]
    assert entry["language"] == "ja"
    assert entry["exaggeration"] == 0.9
    assert entry["meta"] == {"consent": False}


def test_update_missing_voice_returns_none_and_writes_nothing(reg, clip) -> None:
    assert reg.update("ghost", language="ja") is None
    assert not reg.path.exists()
    reg.register("aiko", MTL, clip, 0.5, "en")
    before = reg.path.read_bytes()
    assert reg.update("ghost", meta={"consent": True}) is None
    assert reg.path.read_bytes() == before


def test_update_returns_a_copy(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    new = reg.update("aiko", language="ja")
    new["language"] = "fr"
    new["meta"]["consent"] = False
    assert reg.get("aiko")["language"] == "ja"
    assert reg.get("aiko")["meta"] == {"consent": True}


def test_update_without_changes_does_not_rewrite(reg, clip, monkeypatch) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    writes: list[dict] = []
    monkeypatch.setattr(reg, "_write", writes.append)
    assert reg.update("aiko") == reg.get("aiko")
    assert reg.update("aiko", language="en", meta={"consent": True}) == reg.get("aiko")
    assert writes == []


def test_update_waits_for_the_lock_and_times_out(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    before = reg.path.read_bytes()
    reg._lock.timeout = 0.2
    holder = FileLock(str(reg.path) + ".lock")
    with holder:
        started = time.perf_counter()
        with pytest.raises(filelock.Timeout):
            reg.update("aiko", language="ja")
        assert time.perf_counter() - started >= 0.15
    assert reg.path.read_bytes() == before


_WORKER = r"""
import sys
import time
from pathlib import Path

root, registry_path, field, rounds, go, ready = sys.argv[1:7]
sys.path.insert(0, root)
from engine.registry import VoiceRegistry

registry = VoiceRegistry(Path(registry_path))
Path(ready).touch()
deadline = time.monotonic() + 30
while not Path(go).exists():
    if time.monotonic() > deadline:
        sys.exit("no go signal")
    time.sleep(0.002)
for i in range(int(rounds)):
    if field == "language":
        registry.update("aiko", language=("ja", "fr")[i % 2], meta={"lang%d" % i: i})
    else:
        registry.update("aiko", exaggeration=i / 100, meta={"exag%d" % i: i})
"""


def _wait_for(paths: list[Path], procs: list[subprocess.Popen], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not all(p.exists() for p in paths):
        failed = [proc for proc in procs if proc.poll() not in (None, 0)]
        assert not failed, failed[0].communicate()[1]
        assert time.monotonic() < deadline, "workers did not start"
        time.sleep(0.01)


def test_two_processes_updating_different_fields_both_survive(reg, clip, tmp_path) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    before = reg.get("aiko")
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    go = tmp_path / "go"
    rounds = 15
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    procs, ready = [], []
    for field in ("language", "exaggeration"):
        flag = tmp_path / f"ready-{field}"
        ready.append(flag)
        procs.append(subprocess.Popen(
            [sys.executable, str(script), str(ROOT), str(reg.path), field, str(rounds),
             str(go), str(flag)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    try:
        _wait_for(ready, procs, timeout_s=60)
        go.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    entry = reg.get("aiko")
    assert entry["language"] == ("ja", "fr")[(rounds - 1) % 2]
    assert entry["exaggeration"] == (rounds - 1) / 100
    expected_meta = {"consent": True}
    expected_meta.update({f"lang{i}": i for i in range(rounds)})
    expected_meta.update({f"exag{i}": i for i in range(rounds)})
    assert entry["meta"] == expected_meta  # no update was lost
    for key in ("backend", "reference_wav", "created_at", "forged_at"):
        assert entry[key] == before[key]
    assert leftovers(reg) == []


# ---------- find_casefold and reads ----------


def test_find_casefold_finds_another_spelling(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    assert reg.find_casefold("Aiko") == "aiko"
    assert reg.find_casefold("AIKO") == "aiko"


def test_find_casefold_ignores_the_exact_spelling_and_strangers(reg, clip) -> None:
    assert reg.find_casefold("aiko") is None  # no registry file yet
    reg.register("aiko", MTL, clip, 0.5, "en")
    assert reg.find_casefold("aiko") is None
    assert reg.find_casefold("rin") is None
    assert reg.find_casefold("aiko2") is None


def test_find_casefold_with_legacy_twins_is_deterministic(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    reg.register("Aiko", TURBO, clip, 0.5, "en")
    assert reg.find_casefold("AIKO") == "Aiko"
    assert reg.find_casefold("aiko") == "Aiko"
    assert reg.find_casefold("Aiko") == "aiko"


def test_lockless_reads_never_wait_for_the_lock(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "en")
    with FileLock(str(reg.path) + ".lock"):
        started = time.monotonic()
        assert reg.find_casefold("AIKO") == "aiko"
        assert list(reg.entries()) == ["aiko"]
        assert reg.get("aiko")["backend"] == MTL
        assert time.monotonic() - started < 2.0


def test_malformed_entries_are_replaced_or_ignored(reg, clip) -> None:
    reg._write({"aiko": "garbage", "rin": ["also", "garbage"]})
    assert reg.update("rin", language="ja") is None
    reg.register("aiko", MTL, clip, 0.5, "en", meta={"consent": True})
    entry = reg.get("aiko")
    assert entry["backend"] == MTL
    assert entry["meta"] == {"consent": True}
    assert entry["created_at"] == entry["forged_at"]


def test_entries_is_one_fresh_read(reg, clip, other_clip) -> None:
    assert reg.entries() == {}
    reg.register("aiko", MTL, clip, 0.5, "ja")
    reg.register("rin", TURBO, other_clip, 0.5, "en")
    entries = reg.entries()
    assert sorted(entries) == ["aiko", "rin"]
    assert entries["rin"]["backend"] == TURBO
    entries["aiko"]["language"] = "fr"
    assert reg.entries()["aiko"]["language"] == "ja"


def test_existing_reads_and_remove_still_work(reg, clip) -> None:
    reg.register("aiko", MTL, clip, 0.5, "ja")
    assert reg.backend_for("aiko") == MTL
    assert reg.backend_for("ghost") is None
    assert reg.get("ghost") is None
    assert reg.remove("aiko") is True
    assert reg.remove("aiko") is False
    assert reg.list_ids() == []


def test_corrupt_file_is_quarantined(reg, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="engine")
    reg.path.parent.mkdir(parents=True)
    reg.path.write_text("{not json", encoding="utf-8")
    assert reg.entries() == {}
    moved = [p.name for p in reg.path.parent.glob("registry.json.corrupt-*")]
    assert len(moved) == 1
    assert not reg.path.exists()
    assert any("was malformed" in r.getMessage() for r in caplog.records if r.name == "engine")


@pytest.mark.parametrize("content", [b"[" * 100_000, b'{"a": ' * 50_000], ids=["deep-list", "deep-object"])
def test_a_registry_nested_too_deep_is_quarantined_too(reg, caplog, content: bytes) -> None:
    """R2-04: JSON nested deeper than the parser's recursion limit is corrupt content like any other."""
    caplog.set_level(logging.WARNING, logger="engine")
    reg.path.parent.mkdir(parents=True)
    reg.path.write_bytes(content)
    assert reg.entries() == {}
    (moved,) = reg.path.parent.glob("registry.json.corrupt-*")
    assert moved.read_bytes() == content and not reg.path.exists()
    assert any("RecursionError" in r.getMessage() or "recursion" in r.getMessage() for r in caplog.records)


def test_a_registry_saved_with_a_byte_order_mark_reads(reg) -> None:
    """R2-05: UTF-8 with a BOM (PowerShell 5.1's Set-Content -Encoding UTF8) is valid, never quarantined."""
    reg.path.parent.mkdir(parents=True)
    entry = {"backend": MTL, "reference_wav": "voices/reference/aiko.wav", "exaggeration": 0.5, "language": "ja"}
    reg.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"aiko": entry}).encode("utf-8"))
    assert reg.entries() == {"aiko": entry}
    assert list(reg.path.parent.glob("registry.json.corrupt-*")) == []
    reg.update("aiko", exaggeration=0.75)                       # the next write drops the BOM
    assert reg.path.read_bytes()[:1] == b"{" and reg.get("aiko")["exaggeration"] == 0.75
