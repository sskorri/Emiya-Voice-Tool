"""ui_settings.json: the settings store (§4.14, §7.1-7.2) and the About card's facts (§7.3).

gui.py reads Offline Mode before anything else (ui.entry.read_offline, stdlib only, never writes);
the app process then owns the file through SettingsStore. set() updates memory at once and schedules
one coalesced atomic write in a worker; flush() writes synchronously (on_shutdown). Invalid values
fall back to their defaults with one warning each; unknown sections and keys are kept. A corrupt file
falls back to defaults and is moved to ui_settings.json.corrupt-<unix> only at the first write.

The core section is fixed here. The active theme's own section, theme.<slug>, is whatever its
theme.toml [prefs] declares: the composition root passes theme_section and theme_schema(prefs,
checks), where each default's type gives its check and the theme may refine a key (Emiya's Effect
Speed steps). The core knows no theme by name; pref() reads the active theme's section.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nicegui import Event, run

from engine.backends.info import BACKENDS, LANGUAGES

if TYPE_CHECKING:
    from ui.contract.state import AboutFacts

__all__ = [
    "ABOUT_TIPS",
    "CORE",
    "DEFAULT_KEYS",
    "VERSION",
    "SettingsStore",
    "engine_text",
    "fill_about",
    "folder_path",
    "folder_value",
    "models_in_vram_text",
    "theme_schema",
    "write_atomic",
]

log = logging.getLogger("voicetool.settings")

VERSION = 1
CORE = "core"
ENGINE_PACKAGE = "chatterbox-tts"

DEFAULT_KEYS: Mapping[str, tuple[str, str]] = {
    "gen": ("Ctrl+Enter", ""),
    "gen3": ("Ctrl+Shift+Enter", ""),
    "play": ("Space", ""),
    "prev": ("Alt+Up", ""),
    "next": ("Alt+Down", ""),
    "ab": ("Ctrl+B", ""),
    "save": ("Ctrl+S", ""),
    "search": ("Ctrl+K", ""),
    "call_send": ("", ""),
    "call_stop": ("", ""),
}

ABOUT_TIPS: Mapping[str, str] = {
    "registry": "The list of voices: model, reference clip path, language, Expressiveness, created date",
    "cache": "Each voice's baked conditioning, one file per voice inside its model's folder",
    "clips": "The WAVs voices are forged from; deleting a voice leaves its clip here",
}

_REPLACE_TRIES = 5
_REPLACE_DELAY_S = 0.05
_QUARANTINE_TRIES = 100


# ---------- validators: return the clean value or raise ValueError ----------

def _boolean(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise ValueError("expected true or false")


def _one_of(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if isinstance(value, str) and value in options:
            return value
        raise ValueError(f"expected one of {', '.join(options)}")
    return check


def _whole(value: Any) -> int:
    """An int, or a float with an integral value (a slider may send 100.0); never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a whole number")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("expected a whole number")
        return int(value)
    return value


def _int_in(*options: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        number = _whole(value)
        if number in options:
            return number
        raise ValueError(f"expected one of {', '.join(map(str, options))}")
    return check


def _path_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("expected a folder path")
    return value


def _absolute_or_empty(value: Any) -> str:
    if value == "":
        return value
    if not isinstance(value, str) or "\x00" in value or not os.path.isabs(value):
        raise ValueError('expected an absolute folder or ""')
    return value


def _key_pair(value: Any) -> tuple[str, str]:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, str) for v in value):
        return value[0], value[1]
    raise ValueError("expected [key, alternate]")


def _call_device(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(value.get(k), str) for k in ("id", "name")):
        raise ValueError("expected an audio endpoint id and name")
    return {k: value[k] for k in ("id", "name")}


def _keys(value: Any) -> dict[str, tuple[str, str]]:
    """A full or partial {command id: [key, alternate]} mapping of known ids (set() merges it)."""
    if not isinstance(value, Mapping):
        raise ValueError("expected {command: [key, alternate]}")
    unknown = sorted(str(k) for k in value if k not in DEFAULT_KEYS)
    if unknown:
        raise ValueError(f"unknown commands {', '.join(unknown)}")
    return {command: _key_pair(pair) for command, pair in value.items()}


@dataclass(frozen=True)
class _Setting:
    default: Any
    check: Callable[[Any], Any]


SCHEMA: Mapping[str, Mapping[str, _Setting]] = {
    CORE: {
        "takes_dir": _Setting("outputs/takes", _path_text),
        "autoplay": _Setting(True, _boolean),
        "stop_drops_queue": _Setting(True, _boolean),
        "device": _Setting("cuda", _one_of("cuda", "cpu")),
        "default_backend": _Setting("chatterbox-multilingual", _one_of(*BACKENDS)),
        "offline": _Setting(True, _boolean),
        "reduce_motion": _Setting("system", _one_of("system", "always")),
        "warmup": _Setting("off", _one_of("off", "last")),
        "idle_unload_min": _Setting(10, _int_in(0, 10, 30, 60)),
        "call_enabled": _Setting(False, _boolean),
        "call_device": _Setting({"id": "", "name": ""}, _call_device),
        "call_hear": _Setting(True, _boolean),
        "call_overlap": _Setting("wait", _one_of("wait", "cut")),
        "call_send": _Setting("preview", _one_of("preview", "switch", "auto")),
        "call_delay_s": _Setting(0, _int_in(0, 1, 2, 3)),
        "confirm_switches": _Setting(True, _boolean),
        "forge_language": _Setting("en", _one_of(*LANGUAGES)),
        "browse_dir": _Setting("", _absolute_or_empty),
        "keys": _Setting(dict(DEFAULT_KEYS), _keys),
    },
}

Schema = Mapping[str, Mapping[str, _Setting]]


def _text(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError("expected a non-empty string")


def _typed(default: Any) -> Callable[[Any], Any]:
    """The check a theme.toml [prefs] default implies: true/false, a whole number, or a non-empty string."""
    if isinstance(default, bool):
        return _boolean
    if isinstance(default, int):
        return _whole
    return _text


def theme_schema(defaults: Mapping[str, bool | int | str],
                 checks: Mapping[str, Callable[[Any], Any]] | None = None) -> dict[str, _Setting]:
    """A theme's settings section from its manifest [prefs] (the defaults); `checks` refine a key beyond its type.

    A check takes the value and returns the clean value or raises ValueError, like the core's own checks.
    """
    checks = checks or {}
    unknown = sorted(set(checks) - set(defaults))
    if unknown:
        raise KeyError(f"checks for prefs the theme does not declare: {', '.join(unknown)}")
    return {key: _Setting(value, checks.get(key) or _typed(value)) for key, value in defaults.items()}


def _sections(theme_section: str, theme_prefs: Mapping[str, _Setting] | None) -> dict[str, Mapping[str, _Setting]]:
    sections: dict[str, Mapping[str, _Setting]] = dict(SCHEMA)
    if theme_section:
        if theme_section == CORE or not theme_section.startswith("theme."):
            raise ValueError(f"a theme's settings section is theme.<slug>, got {theme_section!r}")
        sections[theme_section] = dict(theme_prefs or {})
    return sections


def _setting(section: str, key: str, schema: Schema = SCHEMA) -> _Setting:
    try:
        return schema[section][key]
    except KeyError:
        raise KeyError(f"unknown setting {section}.{key}") from None


def _checked(setting: _Setting, section: str, key: str, current: Any, value: Any) -> Any:
    """The validated new value (core.keys merges into the current bindings); ValueError names the setting."""
    try:
        return {**current, **setting.check(value)} if key == "keys" else setting.check(value)
    except ValueError as exc:
        raise ValueError(f"{section}.{key}: {exc}") from None


# ---------- reading ----------

def _read_raw(path: Path) -> tuple[dict[str, Any], str]:
    """(the file's object, problem): problem is "" (fine or missing), "corrupt: ..." or "unreadable: ..."."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return {}, ""
    except OSError as exc:
        return {}, f"unreadable: {type(exc).__name__}: {exc}"
    except (ValueError, RecursionError) as exc:      # bad UTF-8 or JSON; nesting deeper than the parser goes
        return {}, f"corrupt: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return {}, f"corrupt: the top level is a JSON {type(data).__name__}, not an object"
    return data, ""


def _file_keys(value: Any) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """core.keys from the file: per command, a valid pair or the default; unknown ids are ignored."""
    if not isinstance(value, Mapping):
        return dict(DEFAULT_KEYS), [f"core.keys = {value!r} is not valid; using the default bindings"]
    keys, problems = dict(DEFAULT_KEYS), []
    for command, default in DEFAULT_KEYS.items():
        if command not in value:
            continue
        try:
            keys[command] = _key_pair(value[command])
        except ValueError:
            problems.append(f"core.keys.{command} = {value[command]!r} is not valid; using {list(default)!r}")
    return keys, problems


def _validated(raw: Mapping[str, Any], sections: Schema = SCHEMA) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every known setting from the file, or its default; plus one message per invalid value."""
    values: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for section, schema in sections.items():
        given = raw.get(section, {})
        if not isinstance(given, Mapping):
            problems.append(f"{section} is not an object; using its defaults")
            given = {}
        out: dict[str, Any] = {}
        for key, setting in schema.items():
            if key not in given:
                out[key] = _copy(setting.default)
            elif key == "keys":
                out[key], extra = _file_keys(given[key])
                problems.extend(extra)
            else:
                try:
                    out[key] = setting.check(given[key])
                except ValueError:
                    problems.append(f"{section}.{key} = {given[key]!r} is not valid; using {setting.default!r}")
                    out[key] = setting.default
        values[section] = out
    return values, problems


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


# ---------- writing ----------

def write_atomic(path: Path, text: str) -> None:
    """Write text to path via a unique temp file, fsync and os.replace (retried on PermissionError).

    A reader sees the old file or the new one, never a torn write. The temp file is removed on failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:          # an indexer or antivirus has the target open for a moment
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_DELAY_S * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _quarantine(path: Path) -> Path | None:
    """Move an unreadable settings file aside as <name>.corrupt-<unix>, never over an older copy.

    Returns the new name, or None when the file is already gone.
    """
    stamp = int(time.time())
    for n in range(_QUARANTINE_TRIES):
        target = path.with_name(f"{path.name}.corrupt-{stamp}" + (f"-{n}" if n else ""))
        if target.exists():
            continue
        try:
            os.rename(path, target)
        except FileNotFoundError:
            return None
        log.warning("%s was unreadable; moved it to %s", path.name, target.name)
        return target
    raise FileExistsError(f"no free name for a copy of {path.name}")


def _json_value(key: str, value: Any, previous: Any) -> Any:
    """The JSON form of a setting; core.keys keeps command ids this version does not know."""
    if key != "keys":
        return value
    out = dict(previous) if isinstance(previous, Mapping) else {}
    out.update({command: list(pair) for command, pair in value.items()})
    return out


def _merged(base: Mapping[str, Any], updates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """base with updates written over it: unknown sections and keys survive, known ones are replaced."""
    out: dict[str, Any] = {"version": base.get("version", VERSION)}
    out.update((k, v) for k, v in base.items() if k != "version")
    for section, values in updates.items():
        current = out.get(section)
        merged = dict(current) if isinstance(current, Mapping) else {}
        for key, value in values.items():
            merged[key] = _json_value(key, value, merged.get(key))
        out[section] = merged
    return out


class SettingsStore:
    """The app's settings: get/set by (section, key), validated, saved atomically in ui_settings.json.

    theme_section ("theme.<slug>") and theme_prefs (theme_schema(manifest.prefs, theme.pref_checks)) add the
    active theme's section; without them the store holds the core settings only.
    """

    def __init__(self, path: Path, *, theme_section: str = "",
                 theme_prefs: Mapping[str, _Setting] | None = None) -> None:
        self.path = Path(path)
        self.changed: Event[[str, str]] = Event()
        self.save_failed: Event[[str]] = Event()               # a background save failed (on the loop): the reason
        self.theme_section = theme_section                     # "" = no theme section
        self.theme_prefs: Mapping[str, _Setting] = dict(theme_prefs or {})
        self._schema = _sections(theme_section, self.theme_prefs)
        raw, problem = _read_raw(self.path)
        if problem:
            log.warning("%s is %s; using the defaults", self.path.name, problem)
        self._values, problems = _validated(raw, self._schema)
        for message in problems:
            log.warning("%s: %s", self.path.name, message)
        self._touched: set[tuple[str, str]] = set()
        self._touched_commands: set[str] = set()
        self._previewed_commands: set[str] = set()
        self._previewed: set[tuple[str, str]] = set()   # shown in memory by preview(), not saved yet
        self._dirty = False
        self._failure = ""                     # why the last write failed
        self._lock = threading.Lock()          # guards the values and the dirty flag
        self._write_lock = threading.Lock()    # one write at a time (background task or flush)
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def read_early(path: Path, *, theme_section: str = "",
                   theme_prefs: Mapping[str, _Setting] | None = None) -> dict[str, Any]:
        """Every known setting, validated, from one tolerant read; stdlib only, never writes or logs."""
        raw, _ = _read_raw(Path(path))
        values, _ = _validated(raw, _sections(theme_section, theme_prefs))
        return {"version": VERSION, **values}

    def read_back(self) -> dict[str, Any]:
        """What the file holds now for this store's sections, validated (read_early with this store's theme)."""
        return self.read_early(self.path, theme_section=self.theme_section, theme_prefs=self.theme_prefs)

    def pref(self, key: str, default: Any = None) -> Any:
        """The active theme's pref (theme.<slug>.<key>), or `default` when the theme declares no such key."""
        if key not in self._schema.get(self.theme_section, {}):
            return default
        return self.get(self.theme_section, key)

    def get(self, section: str, key: str) -> Any:
        """The current value; core.keys is a fresh {command: (key, alternate)} dict."""
        _setting(section, key, self._schema)
        with self._lock:
            return _copy(self._values[section][key])

    def set(self, section: str, key: str, value: Any) -> None:
        """Change a setting on the event loop: memory now, one coalesced write soon, then `changed`.

        Raises KeyError for an unknown setting and ValueError for a value it does not allow.
        """
        setting = _setting(section, key, self._schema)
        with self._lock:
            current = self._values[section][key]
            clean = _checked(setting, section, key, current, value)
            previewed = (section, key) in self._previewed
            if clean == current and not previewed:
                return
            self._previewed.discard((section, key))
            self._values[section][key] = clean
            self._touched.add((section, key))
            if key == "keys":
                self._touched_commands.update(command for command in value if current[command] != clean[command])
                self._touched_commands.update(self._previewed_commands)
                self._previewed_commands.clear()
            self._dirty = True
        self._schedule_write()
        if clean != current:
            self.changed.emit(section, key)

    def preview(self, section: str, key: str, value: Any) -> None:
        """Show a value at once without saving it (a slider while it moves): memory now, then `changed`.

        Validated like set(). The next set() of that setting saves it, even with the same value; flush()
        saves it too. Raises KeyError for an unknown setting and ValueError for a value it does not allow.
        """
        setting = _setting(section, key, self._schema)
        with self._lock:
            current = self._values[section][key]
            clean = _checked(setting, section, key, current, value)
            if clean == current:
                return
            self._values[section][key] = clean
            self._previewed.add((section, key))
            if key == "keys":
                self._previewed_commands.update(command for command in value if current[command] != clean[command])
        self.changed.emit(section, key)

    def flush(self) -> None:
        """Write pending and previewed changes now, synchronously (on_shutdown); failures are logged, not raised."""
        with self._lock:
            if self._previewed:
                self._touched |= self._previewed
                self._touched_commands.update(self._previewed_commands)
                self._previewed_commands.clear()
                self._previewed.clear()
                self._dirty = True
        self._write_pending()

    # ---------- writing ----------

    def _schedule_write(self) -> None:
        if self._task is not None and not self._task.done():
            return                             # the running task writes again when it sees the change
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                   # no event loop (a script or a sync test): write now
            self._write_pending()
            return
        self._task = loop.create_task(self._write_soon())

    async def _write_soon(self) -> None:
        while self._dirty:
            written = await run.io_bound(self._write_pending)
            if not written:                    # stopping (None) or failed: flush() or the next set() retries
                if written is False:
                    self.save_failed.emit(self._failure)
                return

    def _write_pending(self) -> bool:
        """Write if anything changed; False when the write failed (the change stays pending)."""
        with self._write_lock:
            with self._lock:
                if not self._dirty:
                    return True
                self._dirty = False
                touched, self._touched = self._touched, set()
                commands, self._touched_commands = self._touched_commands, set()
                values = {section: {key: _copy(v) for key, v in keys.items()}
                          for section, keys in self._values.items()}
                values[CORE]["keys"] = {cmd: values[CORE]["keys"][cmd] for cmd in commands}
            try:
                self._write(values, touched)
            except Exception as exc:  # any failure leaves the change pending; never raised into the loop
                with self._lock:
                    self._dirty = True
                    self._touched.update(touched)
                    self._touched_commands.update(commands)
                self._failure = getattr(exc, "strerror", None) or _first_line(exc)
                log.warning("Couldn't save %s: %s", self.path.name, _first_line(exc))
                return False
            return True

    def _write(self, values: dict[str, dict[str, Any]], touched: set[tuple[str, str]]) -> None:
        base, problem = _read_raw(self.path)
        if problem.startswith("unreadable"):
            raise OSError(f"{self.path.name} is {problem}")
        if problem.startswith("corrupt"):
            _quarantine(self.path)
        updates = {section: {key: value for key, value in keys.items() if (section, key) in touched}
                   for section, keys in values.items()}
        data = _merged(base, {section: keys for section, keys in updates.items() if keys})
        write_atomic(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


# ---------- folders as stored and shown ----------

def folder_value(folder: Path, root: Path) -> str:
    """How a folder is stored and shown: relative to the data root with "/" when inside it, else absolute."""
    folder = Path(folder)
    absolute = Path(os.path.normpath(folder if folder.is_absolute() else Path(root) / folder))
    try:
        return absolute.relative_to(Path(os.path.normpath(root))).as_posix()
    except ValueError:
        return str(absolute)


def folder_path(value: str, root: Path) -> Path:
    """The absolute folder a stored value names (a relative value is relative to the data root)."""
    path = Path(value)
    return Path(os.path.normpath(path if path.is_absolute() else Path(root) / path))


# ---------- About card (§7.3) ----------

def engine_text() -> str:
    """"chatterbox-tts 0.1.7" from the installed package metadata (no chatterbox import)."""
    try:
        return f"{ENGINE_PACKAGE} {importlib.metadata.version(ENGINE_PACKAGE)}"
    except importlib.metadata.PackageNotFoundError:
        return f"{ENGINE_PACKAGE} (not installed)"


def models_in_vram_text(max_resident: int) -> str:
    """The locked "Models in VRAM" fact: "One at a time", or "Up to 2 at a time"."""
    return "One at a time" if max_resident <= 1 else f"Up to {max_resident} at a time"


def fill_about(about: AboutFacts, *, root: Path, cache: Path, voices: Path, takes_dir: str,
               max_resident: int) -> None:
    """Write every About fact except the GPU row (device.gpu_about after detection) into the mirror."""
    cache_text = folder_value(cache, root)
    joiner = "/" if not Path(cache_text).is_absolute() else os.sep
    about.engine = engine_text()
    about.models_in_vram = models_in_vram_text(max_resident)
    about.registry = folder_value(Path(cache) / "registry.json", root)
    about.cache = joiner.join((cache_text, "‹model›", "‹voice›.pt"))
    about.clips = folder_value(voices, root)
    about.takes = folder_value(folder_path(takes_dir, root), root)
