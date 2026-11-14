"""SettingsStore (§4.14, §7): defaults, read_early, validation, unknown keys, coalesced atomic writes,
flush, corrupt-file handling, and the About card facts."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from ui.contract.state import AboutFacts
from ui.core.services import settings as settings_module
from ui.core.services.settings import (
    ABOUT_TIPS,
    DEFAULT_KEYS,
    SettingsStore,
    engine_text,
    fill_about,
    folder_path,
    folder_value,
    models_in_vram_text,
    theme_schema,
)

from tests.theme_settings import emiya_read_early, emiya_settings

CORE_DEFAULTS = {
    "takes_dir": "outputs/takes", "autoplay": True, "stop_drops_queue": True, "device": "cuda",
    "default_backend": "chatterbox-multilingual", "offline": True, "reduce_motion": "system",
    "warmup": "off", "idle_unload_min": 10, "confirm_switches": True, "forge_language": "en",
    "call_enabled": False, "call_device": {"id": "", "name": ""}, "call_hear": True,
    "call_overlap": "wait", "call_send": "preview", "call_delay_s": 0,
    "browse_dir": "", "keys": dict(DEFAULT_KEYS),
}
THEME_DEFAULTS = {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"}
SPEC_FILE = {                           # §7.1 verbatim
    "version": 1,
    "core": {
        "takes_dir": "outputs/takes", "autoplay": True, "device": "cuda",
        "default_backend": "chatterbox-multilingual", "offline": True, "reduce_motion": "system",
        "warmup": "off", "idle_unload_min": 0, "confirm_switches": True,
        "forge_language": "en", "browse_dir": "",
        "keys": {"gen": ["Ctrl+Enter", ""], "gen3": ["Ctrl+Shift+Enter", ""], "play": ["Space", ""],
                 "prev": ["Alt+Up", ""], "next": ["Alt+Down", ""], "ab": ["Ctrl+B", ""],
                 "save": ["Ctrl+S", ""], "search": ["Ctrl+K", ""]},
    },
    "theme.emiya": {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"},
}


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "ui_settings.json"


def store_at(path: Path) -> SettingsStore:
    """The store as the composition root builds it for an Emiya run: core plus theme.emiya (its [prefs])."""
    return emiya_settings(path)


def read_early(path: Path) -> dict:
    return emiya_read_early(path)


def write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def folder_state(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


def all_values(store: SettingsStore) -> tuple[dict, dict]:
    core = {key: store.get("core", key) for key in CORE_DEFAULTS}
    theme = {key: store.get("theme.emiya", key) for key in THEME_DEFAULTS}
    return core, theme


@pytest.fixture
def logged() -> Iterator[list[str]]:
    """The store's warnings, collected on its own logger (whatever another test did to logging)."""
    logger = logging.getLogger("voicetool.settings")
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(logging.WARNING)
    saved = logger.level, logger.disabled
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    logger.addHandler(handler)
    yield messages
    logger.removeHandler(handler)
    logger.setLevel(saved[0])
    logger.disabled = saved[1]


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Count the atomic writes the store makes."""
    calls: list[Path] = []
    real = settings_module.write_atomic

    def counting(target: Path, text: str) -> None:
        calls.append(target)
        real(target, text)

    monkeypatch.setattr(settings_module, "write_atomic", counting)
    return calls


async def settled(store: SettingsStore) -> None:
    """Wait for the store's background write, if one is scheduled."""
    task = store._task
    if task is not None:
        await asyncio.wait_for(task, timeout=10)


# ---------- reading ----------

def test_defaults_when_the_file_is_missing(path: Path) -> None:
    store = store_at(path)
    assert all_values(store) == (CORE_DEFAULTS, THEME_DEFAULTS)
    assert not path.exists()


def test_existing_explicit_never_is_preserved(path: Path, logged: list[str]) -> None:
    write(path, SPEC_FILE)
    store = store_at(path)
    assert all_values(store) == ({**CORE_DEFAULTS, "idle_unload_min": 0}, THEME_DEFAULTS)
    assert logged == []


@pytest.mark.parametrize("content", [None, b"{ not json", b"\xff\xfe", b"[1, 2]", b"",
                                     json.dumps({"core": {"offline": "yes", "device": 5}}).encode()])
def test_read_early_never_writes(tmp_path: Path, content: bytes | None) -> None:
    path = tmp_path / "ui_settings.json"
    if content is not None:
        path.write_bytes(content)
    before = folder_state(tmp_path)
    values = read_early(path)
    assert folder_state(tmp_path) == before
    assert values["core"]["offline"] is True and values["version"] == 1


def test_read_early_returns_every_validated_setting(path: Path) -> None:
    write(path, {"core": {"offline": False, "device": "gpu"}, "theme.emiya": {"fx_speed": 50}})
    values = read_early(path)
    assert values["core"] == {**CORE_DEFAULTS, "offline": False}
    assert values["theme.emiya"] == {**THEME_DEFAULTS, "fx_speed": 50}


def test_read_early_accepts_a_byte_order_mark(path: Path) -> None:
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"core": {"offline": False}}).encode())
    assert read_early(path)["core"]["offline"] is False


def test_invalid_values_fall_back_with_one_warning_each(path: Path, logged: list[str]) -> None:
    write(path, {
        "core": {
            "takes_dir": "", "autoplay": 1, "device": "gpu", "default_backend": "chatterbox-other",
            "offline": False, "reduce_motion": "never", "warmup": "first", "idle_unload_min": 15,
            "confirm_switches": "no", "forge_language": "xx", "browse_dir": "relative\\folder",
        },
        "theme.emiya": {"fx_on": None, "fx_speed": 103, "work_glow": False, "saber_art": "forge"},
    })
    store = store_at(path)
    core, theme = all_values(store)
    assert core == {**CORE_DEFAULTS, "offline": False}
    assert theme == {**THEME_DEFAULTS, "work_glow": False}
    assert len(logged) == 13
    assert "ui_settings.json: core.device = 'gpu' is not valid; using 'cuda'" in logged
    assert "ui_settings.json: theme.emiya.fx_speed = 103 is not valid; using 100" in logged


@pytest.mark.parametrize(("key", "given", "expected"), [
    ("fx_speed", 25, 25), ("fx_speed", 200, 200), ("fx_speed", 150.0, 150), ("fx_speed", 20, 100),
    ("fx_speed", 205, 100), ("fx_speed", True, 100), ("fx_speed", "100", 100),
])
def test_effect_speed_range_and_steps(path: Path, key: str, given: object, expected: int) -> None:
    write(path, {"theme.emiya": {key: given}})
    assert store_at(path).get("theme.emiya", key) == expected


@pytest.mark.parametrize(("given", "expected"), [(0, 0), (10, 10), (30, 30), (60, 60), (60.0, 60), (5, 10),
                                                 (-10, 10), (False, 10)])
def test_idle_unload_choices(path: Path, given: object, expected: int) -> None:
    write(path, {"core": {"idle_unload_min": given}})
    assert store_at(path).get("core", "idle_unload_min") == expected


def test_browse_dir_and_takes_dir(path: Path) -> None:
    write(path, {"core": {"browse_dir": "D:\\Clips", "takes_dir": "D:\\Takes"}})
    store = store_at(path)
    assert (store.get("core", "browse_dir"), store.get("core", "takes_dir")) == ("D:\\Clips", "D:\\Takes")


def test_keys_fall_back_per_command(path: Path, logged: list[str]) -> None:
    write(path, {"core": {"keys": {"gen": ["Ctrl+G", "F5"], "play": "Space", "save": ["Ctrl+S"],
                                   "unknown": ["Ctrl+U", ""]}}})
    keys = store_at(path).get("core", "keys")
    assert keys == {**DEFAULT_KEYS, "gen": ("Ctrl+G", "F5")}
    assert len(logged) == 2                                             # play and save; unknown ids are ignored


def test_a_section_that_is_not_an_object_falls_back(path: Path, logged: list[str]) -> None:
    write(path, {"core": ["nope"], "theme.emiya": {"fx_on": False}})
    store = store_at(path)
    assert all_values(store) == (CORE_DEFAULTS, {**THEME_DEFAULTS, "fx_on": False})
    assert logged == ["ui_settings.json: core is not an object; using its defaults"]


def test_get_returns_copies_and_refuses_unknown_settings(path: Path) -> None:
    store = store_at(path)
    keys = store.get("core", "keys")
    keys["gen"] = ("X", "")
    assert store.get("core", "keys")["gen"] == ("Ctrl+Enter", "")
    with pytest.raises(KeyError):
        store.get("core", "theme")
    with pytest.raises(KeyError):
        store.get("theme.paper", "fx_on")


# ---------- set and write ----------

async def test_set_changes_memory_at_once_and_coalesces_one_write(path: Path, writes: list[Path]) -> None:
    store = store_at(path)
    store.set("core", "autoplay", False)
    store.set("core", "device", "cpu")
    store.set("theme.emiya", "fx_speed", 150)
    assert (store.get("core", "autoplay"), store.get("core", "device")) == (False, "cpu")
    assert not path.exists()                                            # the write runs in a worker
    await settled(store)
    assert writes == [path]
    data = read(path)
    assert data["version"] == 1
    assert data["core"] == {"autoplay": False, "device": "cpu"}
    assert data["theme.emiya"] == {"fx_speed": 150}
    assert list(data) == ["version", "core", "theme.emiya"]


async def test_a_change_during_a_write_is_written_after_it(path: Path, writes: list[Path],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(path)
    real = settings_module.SettingsStore._write

    def slow_write(self: SettingsStore, values, touched) -> None:
        real(self, values, touched)
        if len(writes) == 1:
            self._values["core"]["warmup"] = "last"                     # as if set() ran mid-write
            self._touched.add(("core", "warmup"))
            self._dirty = True

    monkeypatch.setattr(settings_module.SettingsStore, "_write", slow_write)
    store.set("core", "autoplay", False)
    await settled(store)
    assert len(writes) == 2
    assert read(path)["core"]["warmup"] == "last"


async def test_changed_fires_after_memory_and_only_for_real_changes(path: Path, writes: list[Path]) -> None:
    store = store_at(path)
    seen: list[tuple[str, str, object]] = []
    store.changed.subscribe(lambda section, key: seen.append((section, key, store.get(section, key))))
    store.set("core", "idle_unload_min", 30)
    store.set("core", "idle_unload_min", 30)                            # unchanged: no event, no write
    store.set("core", "confirm_switches", True)                         # already the default
    await settled(store)
    assert seen == [("core", "idle_unload_min", 30)]
    assert writes == [path]


def test_set_refuses_invalid_values_and_unknown_settings(path: Path) -> None:
    store = store_at(path)
    for section, key, value in [("core", "device", "gpu"), ("core", "autoplay", 1),
                                ("theme.emiya", "fx_speed", 103), ("core", "idle_unload_min", 15),
                                ("core", "default_backend", "chatterbox-other"),
                                ("core", "browse_dir", "relative"), ("core", "keys", {"nope": ["A", ""]}),
                                ("core", "keys", {"gen": ["A"]}), ("core", "keys", ["Ctrl+G"])]:
        with pytest.raises(ValueError):
            store.set(section, key, value)
    with pytest.raises(KeyError):
        store.set("core", "theme", "emiya")
    assert all_values(store) == (CORE_DEFAULTS, THEME_DEFAULTS)
    assert not path.exists()


def test_set_merges_a_partial_key_map(path: Path) -> None:
    store = store_at(path)                                         # no event loop: writes at once
    store.set("core", "keys", {"gen": ["Ctrl+G", ""], "search": ("", "Ctrl+F")})
    assert store.get("core", "keys") == {**DEFAULT_KEYS, "gen": ("Ctrl+G", ""), "search": ("", "Ctrl+F")}
    assert read(path)["core"]["keys"] == {"gen": ["Ctrl+G", ""], "search": ["", "Ctrl+F"]}


def test_a_later_sparse_write_preserves_other_edits_to_previously_saved_keys(path: Path) -> None:
    store = store_at(path)
    store.set("core", "keys", {"gen": ("F5", "")})
    write(path, {"core": {"keys": {"gen": ["F6", ""], "custom": ["F7", ""]}}})
    store.set("core", "keys", {"call_send": ("F8", "")})
    assert read(path)["core"]["keys"] == {"gen": ["F6", ""], "custom": ["F7", ""], "call_send": ["F8", ""]}


@pytest.mark.parametrize(("key", "invalid"), [("call_enabled", 1), ("call_device", "Speakers"),
    ("call_device", {"id": 2, "name": ""}), ("call_overlap", "both"), ("call_send", "always"),
    ("call_hear", None), ("call_delay_s", 4), ("call_delay_s", True)])
def test_voice_chat_settings_validate_at_read_and_set(path: Path, key: str, invalid: object) -> None:
    write(path, {"core": {key: invalid}})
    store = store_at(path)
    assert store.get("core", key) == CORE_DEFAULTS[key]
    with pytest.raises(ValueError):
        store.set("core", key, invalid)


async def test_flush_writes_pending_changes_synchronously(path: Path, writes: list[Path]) -> None:
    store = store_at(path)
    store.set("core", "offline", False)
    store.flush()                                                       # on_shutdown: before the task ran
    assert read(path)["core"]["offline"] is False
    await settled(store)
    assert writes == [path]                                             # the task found nothing left
    store.flush()
    assert writes == [path]


async def test_preview_shows_a_value_without_saving_it(path: Path, writes: list[Path]) -> None:
    """Effect Speed while its range moves (§6.12): memory and `changed` at once; the release saves it."""
    store = store_at(path)
    seen: list[tuple[str, str, object]] = []
    store.changed.subscribe(lambda section, key: seen.append((section, key, store.get(section, key))))
    store.preview("theme.emiya", "fx_speed", 150)
    store.preview("theme.emiya", "fx_speed", 150)                       # unchanged: no second event
    await settled(store)
    assert store.get("theme.emiya", "fx_speed") == 150 and writes == [] and not path.exists()
    assert seen == [("theme.emiya", "fx_speed", 150)]
    store.set("theme.emiya", "fx_speed", 150)                           # the release saves what is shown
    await settled(store)
    assert writes == [path] and read(path)["theme.emiya"]["fx_speed"] == 150
    assert seen == [("theme.emiya", "fx_speed", 150)]                   # nothing new to announce
    store.set("theme.emiya", "fx_speed", 150)                           # saved: nothing to do
    await settled(store)
    assert writes == [path]


async def test_flush_saves_a_previewed_value(path: Path, writes: list[Path]) -> None:
    store = store_at(path)
    store.preview("theme.emiya", "fx_speed", 50)
    store.flush()                                                       # on_shutdown in the middle of a drag
    assert read(path)["theme.emiya"]["fx_speed"] == 50 and writes == [path]
    store.flush()
    assert writes == [path]


def test_preview_refuses_invalid_values_and_unknown_settings(path: Path) -> None:
    store = store_at(path)
    with pytest.raises(ValueError):
        store.preview("theme.emiya", "fx_speed", 103)
    with pytest.raises(KeyError):
        store.preview("core", "theme", "emiya")
    assert all_values(store) == (CORE_DEFAULTS, THEME_DEFAULTS) and not path.exists()


def test_a_fresh_file_holds_only_the_touched_setting(path: Path) -> None:
    store = store_at(path)
    store.set("core", "warmup", "last")
    assert read(path) == {"version": 1, "core": {"warmup": "last"}}
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]   # no temp file left


def test_an_existing_file_gets_only_the_changed_settings(path: Path) -> None:
    write(path, {"version": 1, "core": {"device": "cpu", "idle_unload_min": 15}})
    store = store_at(path)
    write(path, {"version": 1, "core": {"device": "cpu", "idle_unload_min": 15, "offline": False}})   # a hand edit
    store.set("core", "autoplay", False)
    assert read(path) == {"version": 1, "core": {"device": "cpu", "idle_unload_min": 15, "offline": False,
                                                 "autoplay": False}}


def test_unknown_sections_keys_and_commands_are_kept(path: Path) -> None:
    write(path, {"version": 1, "ui": {"theme": "emiya"}, "extra": [1, 2],
                 "core": {"future_key": {"a": 1}, "keys": {"gen": ["Ctrl+Enter", ""], "dance": ["Ctrl+D", ""]}},
                 "theme.paper": {"ink": "blue"}})
    store = store_at(path)
    store.set("core", "keys", {"gen": ["Ctrl+G", ""]})
    store.set("theme.emiya", "saber_art", "hide")
    data = read(path)
    assert (data["ui"], data["extra"], data["theme.paper"]) == ({"theme": "emiya"}, [1, 2], {"ink": "blue"})
    assert data["core"]["future_key"] == {"a": 1}
    assert data["core"]["keys"]["dance"] == ["Ctrl+D", ""] and data["core"]["keys"]["gen"] == ["Ctrl+G", ""]
    assert data["theme.emiya"] == {"saber_art": "hide"}


def test_a_failed_replace_keeps_the_old_file_and_retries_later(path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              logged: list[str]) -> None:
    write(path, {"version": 1, "core": {"autoplay": True}})
    before = path.read_bytes()
    store = store_at(path)
    real_replace = settings_module.os.replace

    def locked(src: object, dst: object) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(settings_module.os, "replace", locked)
    monkeypatch.setattr(settings_module, "_REPLACE_DELAY_S", 0.0)
    store.set("core", "autoplay", False)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]   # the temp file was removed
    assert any(m.startswith("Couldn't save ui_settings.json") for m in logged)
    assert store.get("core", "autoplay") is False
    monkeypatch.setattr(settings_module.os, "replace", real_replace)
    store.flush()
    assert read(path)["core"]["autoplay"] is False


def test_a_corrupt_file_is_moved_aside_only_at_the_first_write(path: Path, logged: list[str]) -> None:
    path.write_bytes(b'{"core": {"autoplay": fals')
    store = store_at(path)
    assert all_values(store) == (CORE_DEFAULTS, THEME_DEFAULTS)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]   # reading never moves it
    assert len(logged) == 1 and logged[0].startswith("ui_settings.json is corrupt: JSONDecodeError")
    store.set("core", "autoplay", False)
    (copy,) = path.parent.glob("ui_settings.json.corrupt-*")
    assert copy.read_bytes() == b'{"core": {"autoplay": fals'
    assert read(path) == {"version": 1, "core": {"autoplay": False}}


def test_an_unreadable_file_is_not_overwritten(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(path, {"version": 1, "core": {"device": "cpu"}})
    store = store_at(path)
    real = settings_module._read_raw
    monkeypatch.setattr(settings_module, "_read_raw", lambda p: ({}, "unreadable: PermissionError: locked"))
    store.set("core", "autoplay", False)
    assert read(path) == {"version": 1, "core": {"device": "cpu"}}
    monkeypatch.setattr(settings_module, "_read_raw", real)
    store.flush()
    assert read(path) == {"version": 1, "core": {"device": "cpu", "autoplay": False}}


@pytest.mark.parametrize("content", [b'{"core": ' + b"[" * 100_000, b"[" * 100_000, b'{"a": ' * 60_000],
                         ids=["deep-core", "deep-top", "deep-objects"])
def test_a_file_nested_too_deep_is_corrupt_not_a_crash(path: Path, logged: list[str], content: bytes) -> None:
    """R2-01: json raises RecursionError for nesting deeper than the parser goes; that is a corrupt file (the
    defaults, one warning, moved aside at the first write), never an exception out of the store or read_early."""
    path.write_bytes(content)
    assert read_early(path)["core"] == CORE_DEFAULTS
    store = store_at(path)
    assert all_values(store) == (CORE_DEFAULTS, THEME_DEFAULTS)
    assert len(logged) == 1 and logged[0].startswith("ui_settings.json is corrupt: RecursionError")
    store.set("core", "autoplay", False)
    (copy,) = path.parent.glob("ui_settings.json.corrupt-*")
    assert copy.read_bytes() == content and read(path)["core"]["autoplay"] is False


# ---------- the theme's own section (theme.<slug> from its manifest [prefs]) ----------

def test_without_a_theme_section_the_store_holds_the_core_settings_only(path: Path) -> None:
    write(path, {"core": {"offline": False}, "theme.emiya": {"fx_speed": 50}})
    store = SettingsStore(path)
    assert store.get("core", "offline") is False and store.theme_section == ""
    with pytest.raises(KeyError):
        store.get("theme.emiya", "fx_speed")
    assert store.pref("fx_speed", 100) == 100                           # an undeclared pref reads its default
    store.set("core", "autoplay", False)
    assert read(path)["theme.emiya"] == {"fx_speed": 50}                # another theme's section is kept


def test_a_theme_section_comes_from_its_manifest_prefs(path: Path) -> None:
    """A second theme needs no core edit: its [prefs] defaults give the section and their types give the checks."""
    prefs = theme_schema({"density": "comfortable", "columns": 2, "rounded": True})
    write(path, {"theme.probe": {"density": "", "columns": 3.0, "rounded": "yes"}, "theme.emiya": {"fx_on": False}})
    store = SettingsStore(path, theme_section="theme.probe", theme_prefs=prefs)
    assert (store.pref("density"), store.pref("columns"), store.pref("rounded")) == ("comfortable", 3, True)
    assert store.pref("fx_on", "missing") == "missing"
    with pytest.raises(ValueError):
        store.set("theme.probe", "columns", "many")
    store.set("theme.probe", "density", "compact")
    data = read(path)
    assert data["theme.probe"]["density"] == "compact" and data["theme.emiya"] == {"fx_on": False}


def test_theme_checks_refine_a_pref_and_must_name_declared_prefs(path: Path) -> None:
    def even(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value % 2 == 0:
            return value
        raise ValueError("expected an even number")

    store = SettingsStore(path, theme_section="theme.probe", theme_prefs=theme_schema({"size": 2}, {"size": even}))
    with pytest.raises(ValueError, match="even"):
        store.set("theme.probe", "size", 3)
    with pytest.raises(KeyError, match="does not declare"):
        theme_schema({"size": 2}, {"colour": even})
    with pytest.raises(ValueError, match="theme"):
        SettingsStore(path, theme_section="core", theme_prefs={})


async def test_a_failed_background_save_is_announced_once_per_write(path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    logged: list[str]) -> None:
    """A read-only file or a full disk: the change holds in memory, the warning is logged and save_failed tells
    the core (which raises one red toast per session)."""
    store = store_at(path)
    reasons: list[str] = []
    store.save_failed.subscribe(reasons.append)

    def full(self: SettingsStore, values: object, touched: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settings_module.SettingsStore, "_write", full)
    store.set("core", "autoplay", False)
    await settled(store)
    assert reasons == ["No space left on device"] and store.get("core", "autoplay") is False
    assert any(message.startswith("Couldn't save ui_settings.json") for message in logged)


# ---------- folders and the About card ----------

def test_folder_value_is_relative_inside_the_data_root(tmp_path: Path) -> None:
    root = tmp_path / "Voice Tool"
    assert folder_value(root / "outputs" / "takes", root) == "outputs/takes"
    assert folder_value(Path("outputs/takes"), root) == "outputs/takes"
    outside = tmp_path / "Takes"
    assert folder_value(outside, root) == str(outside)
    assert folder_path("outputs/takes", root) == root / "outputs" / "takes"
    assert folder_path(str(outside), root) == outside
    assert folder_path(folder_value(root / "a" / ".." / "b", root), root) == root / "b"


def test_about_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module.importlib.metadata, "version", lambda name: "0.1.7")
    about = AboutFacts()
    fill_about(about, root=tmp_path, cache=tmp_path / "cache", voices=tmp_path / "voices" / "reference",
               takes_dir="outputs/takes", max_resident=1)
    assert (about.engine, about.models_in_vram, about.registry, about.cache, about.clips, about.takes) == (
        "chatterbox-tts 0.1.7", "One at a time", "cache/registry.json", "cache/‹model›/‹voice›.pt",
        "voices/reference", "outputs/takes")
    assert about.gpu == ""                                              # device.gpu_about() fills it later
    elsewhere = tmp_path.parent / "Takes"
    fill_about(about, root=tmp_path, cache=tmp_path / "cache", voices=tmp_path / "voices" / "reference",
               takes_dir=str(elsewhere), max_resident=2)
    assert (about.takes, about.models_in_vram) == (str(elsewhere), "Up to 2 at a time")


def test_engine_text_reads_the_installed_version() -> None:
    assert engine_text().startswith("chatterbox-tts ")
    assert models_in_vram_text(1) == "One at a time"


def test_about_tooltips_are_the_mockup_copy() -> None:
    assert ABOUT_TIPS == {
        "registry": "The list of voices: model, reference clip path, language, Expressiveness, created date",
        "cache": "Each voice's baked conditioning, one file per voice inside its model's folder",
        "clips": "The WAVs voices are forged from; deleting a voice leaves its clip here",
    }
