"""VoiceLibrary (§4.15.4): snapshot rows and order, names, dates, missing files, consent, corrupt registry."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.registry import VoiceRegistry
from engine.types import VoiceDetails
from tests.engine.conftest import write_clip
from ui.core.services import voices as voices_module
from ui.core.services.voices import (
    LibrarySnapshot,
    VoiceLibrary,
    corrupt_text,
    created_text,
    created_tip,
    language_name,
)

MTL, STANDARD, TURBO = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"
CREATED = "2026-11-08T05:41:09.112233+00:00"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "cache").mkdir()
    (tmp_path / "voices" / "reference").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def library(root: Path) -> VoiceLibrary:
    return VoiceLibrary(root / "cache" / "registry.json")


def details(root: Path, voice_id: str, backend: str = MTL, *, language: str = "en", exaggeration: float = 0.5,
            clip_s: float | None = 2.0, pt: bool = True, created_at: str | None = CREATED,
            forged_at: str | None = None, consent: bool | None = True, enabled: bool = True) -> VoiceDetails:
    """A VoiceDetails as engine.voice_details() reports it; clip_s None = the reference clip is missing."""
    reference = root / "voices" / "reference" / f"{voice_id}_ref.wav"
    if clip_s is not None and not reference.exists():
        write_clip(reference, clip_s)
    conds = root / "cache" / backend / f"{voice_id}.pt"
    if pt:
        conds.parent.mkdir(parents=True, exist_ok=True)
        conds.write_bytes(b"conds")
    return VoiceDetails(
        voice_id=voice_id, backend=backend, backend_enabled=enabled, language=language,
        exaggeration=exaggeration, reference_wav=str(reference),
        reference_path=reference if clip_s is not None else None,
        conds_path=conds, conds_exists=pt, created_at=created_at, forged_at=forged_at,
        consent=consent, meta={} if consent is None else {"consent": consent},
    )


# ---------- rows ----------

def test_rows_sort_by_backend_then_name_ignoring_case(root: Path, library: VoiceLibrary) -> None:
    given = [
        details(root, "host-turbo", TURBO), details(root, "narrator", STANDARD), details(root, "marco"),
        details(root, "Aiko"), details(root, "friend"), details(root, "zed", "chatterbox-future"),
        details(root, "beta"),
    ]
    snapshot = library.snapshot(given)
    assert isinstance(snapshot, LibrarySnapshot)
    assert [(r.backend, r.voice_id) for r in snapshot.rows] == [
        (MTL, "Aiko"), (MTL, "beta"), (MTL, "friend"), (MTL, "marco"),
        (STANDARD, "narrator"), (TURBO, "host-turbo"), ("chatterbox-future", "zed"),
    ]


def test_row_fields_come_from_the_details(root: Path, library: VoiceLibrary) -> None:
    d = details(root, "aiko", language="ja", exaggeration=0.7, clip_s=2.0, enabled=False)
    (row,) = library.snapshot([d]).rows
    assert row.voice_id == "aiko" and row.backend == MTL and row.backend_enabled is False
    assert (row.language, row.language_name, row.exaggeration) == ("ja", "Japanese", 0.7)
    assert row.reference_name == "aiko_ref.wav"
    assert row.reference_path == d.reference_path
    assert row.clip_s == pytest.approx(2.0)
    assert (row.pt, row.pt_exists) == (d.conds_path, True)
    assert row.created == CREATED
    assert row.consent is True


@pytest.mark.parametrize(("backend", "code", "name"), [
    (MTL, "ja", "Japanese"),
    (MTL, "it", "Italian"),
    (MTL, "JA", "Japanese"),
    (MTL, "xx", "xx"),
    (STANDARD, "en", "English"),
    (STANDARD, "ja", "English"),
    (TURBO, "en", "English"),
    ("chatterbox-future", "fr", "French"),
])
def test_language_names(backend: str, code: str, name: str) -> None:
    assert language_name(backend, code) == name


def test_created_is_the_last_forge_else_the_first_registration(root: Path, library: VoiceLibrary) -> None:
    forged = "2026-11-13T07:00:00+00:00"
    rows = library.snapshot([
        details(root, "a", forged_at=forged), details(root, "b"), details(root, "c", created_at=None),
    ]).rows
    assert [r.created for r in rows] == [forged, CREATED, None]


def test_missing_clip_and_missing_pt(root: Path, library: VoiceLibrary) -> None:
    d = details(root, "ghost", clip_s=None, pt=False)
    (row,) = library.snapshot([d]).rows
    assert row.reference_path is None and row.clip_s is None
    assert row.reference_name == "ghost_ref.wav"
    assert row.pt == root / "cache" / MTL / "ghost.pt" and row.pt_exists is False


def test_an_unreadable_clip_has_no_duration(root: Path, library: VoiceLibrary) -> None:
    broken = root / "voices" / "reference" / "noise_ref.wav"
    broken.write_bytes(b"not audio")
    d = replace(details(root, "noise", clip_s=None), reference_path=broken)
    (row,) = library.snapshot([d]).rows
    assert row.reference_path == broken and row.clip_s is None


def test_consent_is_true_only_when_recorded_true(root: Path, library: VoiceLibrary) -> None:
    rows = library.snapshot([details(root, "a", consent=True), details(root, "b", consent=False),
                             details(root, "c", consent=None)]).rows
    assert [r.consent for r in rows] == [True, False, False]


def test_clip_durations_are_cached_by_path_mtime_and_size(root: Path, library: VoiceLibrary,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    real = voices_module.clip_duration

    def counting(path: Path) -> float | None:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(voices_module, "clip_duration", counting)
    d = details(root, "aiko", clip_s=2.0)
    library.snapshot([d])
    library.snapshot([d])
    assert len(calls) == 1
    write_clip(d.reference_path, 3.0)                    # a new size and mtime: read again
    stat = d.reference_path.stat()
    os.utime(d.reference_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    (row,) = library.snapshot([d]).rows
    assert len(calls) == 2 and row.clip_s == pytest.approx(3.0)


def test_an_empty_library(library: VoiceLibrary) -> None:
    assert library.snapshot([]) == LibrarySnapshot(rows=(), corrupt_files=(), new_corrupt=())


# ---------- registry file facts ----------

def test_registry_mtime(root: Path, library: VoiceLibrary) -> None:
    assert library.registry_mtime() is None
    registry = root / "cache" / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    assert library.registry_mtime() == registry.stat().st_mtime_ns


def test_a_newly_quarantined_registry_is_reported_once(root: Path, library: VoiceLibrary) -> None:
    registry = root / "cache" / "registry.json"
    registry.write_text("{ this is not json", encoding="utf-8")
    assert VoiceRegistry(registry).entries() == {}       # the engine moves it aside as registry.json.corrupt-<t>
    (name,) = library.corrupt_files()
    assert name.startswith("registry.json.corrupt-")
    first = library.snapshot([])
    assert first.corrupt_files == (name,) and first.new_corrupt == (name,)
    second = library.snapshot([])
    assert second.corrupt_files == (name,) and second.new_corrupt == ()


def test_corrupt_copies_from_before_the_library_existed_are_not_new(root: Path) -> None:
    cache = root / "cache"
    (cache / "registry.json.corrupt-1700000000").write_text("old", encoding="utf-8")
    (cache / "registry.json.corrupt-dir").mkdir()             # folders are not copies
    (cache / "other.json.corrupt-1700000001").write_text("x", encoding="utf-8")
    library = VoiceLibrary(cache / "registry.json")
    snapshot = library.snapshot([])
    assert snapshot.corrupt_files == ("registry.json.corrupt-1700000000",)
    assert snapshot.new_corrupt == ()
    (cache / "registry.json.corrupt-1800000000").write_text("new", encoding="utf-8")
    assert library.snapshot([]).new_corrupt == ("registry.json.corrupt-1800000000",)


def test_corrupt_files_without_a_cache_folder(tmp_path: Path) -> None:
    assert VoiceLibrary(tmp_path / "nowhere" / "registry.json").corrupt_files() == ()


def test_corrupt_text() -> None:
    assert corrupt_text("registry.json.corrupt-1700000000") == (
        "The voice registry was unreadable · moved it to registry.json.corrupt-1700000000 · "
        "voices are missing until it is restored")


# ---------- dates ----------

def utc(*parts: int) -> str:
    return datetime(*parts, tzinfo=timezone.utc).isoformat()


def test_created_text() -> None:
    now = datetime(2026, 11, 13, 15, 0).astimezone()
    today = now.astimezone(timezone.utc).isoformat()
    assert created_text(today, now=now) == "Today"
    assert created_text(datetime(2026, 11, 8, 12, 0).astimezone().isoformat(), now=now) == "Nov 8"
    assert created_text(datetime(2026, 1, 2, 12, 0).astimezone().isoformat(), now=now) == "Jan 2"
    assert created_text(utc(2025, 6, 1, 12), now=now) == "2025"
    assert created_text(None, now=now) == "—"
    assert created_text("", now=now) == "—"
    assert created_text("last tuesday", now=now) == "—"


def test_created_tip_is_the_full_local_date_and_time() -> None:
    stamp = datetime(2026, 11, 8, 14, 31).astimezone()
    assert created_tip(stamp.isoformat()) == "Nov 8, 2026 · 14:31"
    assert created_tip(stamp.astimezone(timezone.utc).isoformat()) == "Nov 8, 2026 · 14:31"
    assert created_tip(None) == ""
    assert created_tip("garbage") == ""


# ---------- reveal ----------

@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(voices_module, "_popen", lambda command: calls.append(("popen", command)))
    monkeypatch.setattr(voices_module, "_startfile", lambda folder: calls.append(("startfile", folder)))
    return calls


def test_reveal_selects_an_existing_file(root: Path, library: VoiceLibrary,
                                         launched: list[tuple[str, object]]) -> None:
    pt = root / "cache" / MTL / "aiko.pt"
    pt.parent.mkdir(parents=True)
    pt.write_bytes(b"x")
    library.reveal(pt)
    explorer = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    assert launched == [("popen", f'"{explorer}" /select,"{pt}"')]     # by full path, never a planted one


def test_reveal_opens_the_folder_of_a_missing_file(root: Path, library: VoiceLibrary,
                                                   launched: list[tuple[str, object]]) -> None:
    folder = root / "cache" / MTL
    folder.mkdir(parents=True)
    library.reveal(folder / "gone.pt")
    assert launched == [("startfile", folder)]


def test_reveal_raises_when_the_folder_is_missing_too(root: Path, library: VoiceLibrary,
                                                      launched: list[tuple[str, object]]) -> None:
    with pytest.raises(FileNotFoundError):
        library.reveal(root / "cache" / TURBO / "gone.pt")
    assert launched == []
