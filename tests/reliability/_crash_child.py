"""R2 · the child process of the crash-mid-write tests (tests\\reliability\\test_crash_recovery.py).

    venv\\Scripts\\python.exe tests\\reliability\\_crash_child.py <writer> <data root>

The child sets up one writer on a scratch data root, prints READY, then repeats that writer's write as fast as
it can, appending the number of every finished operation to <root>\\journal.txt (one line per operation, written
after the operation returned), until the parent kills it with TerminateProcess. It never runs longer than
MAX_RUN_S, so a lost parent cannot leave it behind. A fresh child continues the sequence from what the files hold.

Writers (the real code; only the model and the Recycle Bin are stand-ins):
- registry: VoiceRegistry register, update and remove (engine\\registry.py).
- takes: TakesStore add, set_starred and purge_unstarred, each an index.json rewrite (the WAV first for add).
- settings: SettingsStore.set, and preview() + flush(), each a write of ui_settings.json.
- history: LoadHistory.append (cache\\load_history.jsonl, keep=HISTORY_KEEP so the next start compacts).
- pt: ChatterboxBackend.load_voice(force=True) with a fake model, i.e. engine.backends.base.save_atomic of a
  256 KB "conds" object written in flushed chunks (the backends' atomic .pt save).
- owner: EngineLock.acquire and release (cache\\engine.lock and engine.owner.json).
- hold: takes cache\\gui.lock the way gui.py does (ui.entry.take_instance_lock) and cache\\engine.lock with its
  owner record (program "gui.py"), then waits to be killed.

The settings and history writers replace NiceGUI with a stub before importing ui.core.services.settings: the
stores' file code never touches nicegui outside an event loop, and the real import costs each child about 0.9 s.

The state helpers below (registry_ops, takes_ops, settings_value, pt_step, history_steps, ...) are imported by
the test, so the child and the parent map a file back to the operation it holds in exactly the same way.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import types
import wave
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[2]
MTL = "chatterbox-multilingual"
JOURNAL = "journal.txt"
READY = "READY"
MAX_RUN_S = 60.0
HISTORY_KEEP = 40
PT_CHUNKS = 8
PT_CHUNK_CHARS = 32 * 1024
SETTINGS_VALUES = tuple(range(25, 205, 5))           # theme.emiya.fx_speed: 25..200 step 5 (36 values)
CHILD_PROGRAM = "crash-child"
WRITERS = ("registry", "takes", "settings", "history", "pt", "owner", "hold")


# ---- paths and the journal ---------------------------------------------------------------------------

def registry_path(root: Path) -> Path:
    return root / "cache" / "registry.json"


def takes_dir(root: Path) -> Path:
    return root / "outputs" / "takes"


def settings_path(root: Path) -> Path:
    return root / "ui_settings.json"


def history_path(root: Path) -> Path:
    return root / "cache" / "load_history.jsonl"


def pt_path(root: Path) -> Path:
    return root / "cache" / MTL / "aiko.pt"


def reference(root: Path) -> Path:
    return root / "voices" / "reference" / "aiko_ref.wav"


def last_journaled(root: Path) -> int:
    """The last operation the journal records as finished (-1 when none); only complete lines count."""
    try:
        data = (root / JOURNAL).read_bytes()
    except FileNotFoundError:
        return -1
    lines = [line for line in data.split(b"\n")[:-1] if line.strip()]
    return int(lines[-1]) if lines else -1


# ---- state helpers (shared with the test) ------------------------------------------------------------

def registry_entry_step(voice_id: str, entry: Any, ref: str) -> tuple[int, bool]:
    """(step, updated) of one registry entry the registry writer wrote; ValueError for anything else."""
    match = re.fullmatch(r"v(\d+)", voice_id)
    if match is None or not isinstance(entry, dict):
        raise ValueError(f"unexpected entry {voice_id!r}: {entry!r}")
    step = int(match.group(1))
    updated = entry.get("exaggeration") == 0.75
    meta = {"step": step, "updated": True} if updated else {"step": step}
    expected = {"backend": MTL, "reference_wav": ref, "language": "en", "exaggeration": 0.75 if updated else 0.5,
                "meta": meta}
    for key, value in expected.items():
        if entry.get(key) != value:
            raise ValueError(f"{voice_id}.{key} = {entry.get(key)!r}, expected {value!r} (a mixed entry)")
    for key in ("created_at", "forged_at"):
        if not isinstance(entry.get(key), str):
            raise ValueError(f"{voice_id}.{key} is missing")
    if set(entry) != {"backend", "reference_wav", "language", "exaggeration", "meta", "created_at", "forged_at"}:
        raise ValueError(f"{voice_id} has keys {sorted(entry)}")
    return step, updated


def registry_ops(entries: dict[str, Any], ref: str) -> set[int]:
    """The operations whose result the registry holds; ValueError when it holds no operation's result.

    Operation n = 3i + k: k=0 register v<i> (fresh), k=1 update v<i>, k=2 remove v<i-1> (nothing for i=0).
    """
    if not entries:
        return {-1}
    steps = sorted((registry_entry_step(vid, entry, ref) for vid, entry in entries.items()), key=lambda s: s[0])
    if len(steps) == 1:
        step, updated = steps[0]
        if not updated:
            if step != 0:
                raise ValueError(f"a lone fresh v{step} (its predecessor vanished)")
            return {0}
        return {3 * step + 2} | ({1} if step == 0 else set())
    if len(steps) == 2:
        (old, old_updated), (new, new_updated) = steps
        if new != old + 1 or not old_updated:
            raise ValueError(f"registry holds {steps}")
        return {3 * new + (1 if new_updated else 0)}
    raise ValueError(f"registry holds {len(steps)} voices: {steps}")


def take_step(take: Any) -> int:
    match = re.fullmatch(r"step (\d+)", getattr(take, "text", ""))
    if match is None:
        raise ValueError(f"unexpected take {take!r}")
    return int(match.group(1))


def takes_ops(takes: Iterable[Any]) -> set[int]:
    """The operations whose result the index holds; ValueError when it holds no operation's result.

    Operation n = 4i + k: k=0 add step i, k=1 star it, k=2 unstar step i-1, k=3 purge step i-1 (k=2, 3 are
    nothing for i=0).
    """
    items = sorted((take_step(take), bool(take.starred)) for take in takes)
    if not items:
        return {-1}
    if len(items) == 1:
        step, starred = items[0]
        if step == 0:
            return {1, 2, 3} if starred else {0}
        if not starred:
            raise ValueError(f"a lone unstarred step {step}")
        return {4 * step + 3}
    if len(items) == 2:
        (old, old_star), (new, new_star) = items
        if new != old + 1:
            raise ValueError(f"index holds steps {items}")
        if old_star and not new_star:
            return {4 * new}
        if old_star and new_star:
            return {4 * new + 1}
        if not old_star and new_star:
            return {4 * new + 2}
    raise ValueError(f"index holds {items}")


def settings_value(n: int) -> int:
    return SETTINGS_VALUES[n % len(SETTINGS_VALUES)]


SETTINGS_SECTION = "theme.emiya"
SETTINGS_DEFAULTS = {"fx_on": True, "fx_speed": 100, "work_glow": True, "saber_art": "show"}   # its [prefs]


def settings_store(path: Path) -> Any:
    """The settings store with a theme.emiya section like Emiya's (its [prefs] types; the child never imports the
    theme, which would import NiceGUI for real)."""
    from ui.core.services.settings import SettingsStore, theme_schema

    return SettingsStore(path, theme_section=SETTINGS_SECTION, theme_prefs=theme_schema(SETTINGS_DEFAULTS))


def settings_read_early(path: Path) -> dict[str, Any]:
    from ui.core.services.settings import SettingsStore, theme_schema

    return SettingsStore.read_early(path, theme_section=SETTINGS_SECTION, theme_prefs=theme_schema(SETTINGS_DEFAULTS))


def pt_step(path: Path) -> int:
    """The step a complete .pt holds (-1 when absent); ValueError for a torn or foreign file."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return -1
    if not (isinstance(data, dict) and data.get("voice") == "aiko" and data.get("end") is True
            and isinstance(data.get("step"), int) and data.get("pad") == "x" * (PT_CHUNKS * PT_CHUNK_CHARS)):
        raise ValueError(f"a torn or foreign .pt: {str(data)[:120]}")
    return data["step"]


def history_steps(path: Path) -> tuple[list[int], int]:
    """(the seconds of every readable line in order, unreadable lines); only the last line may be unreadable."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], 0
    lines = data.split(b"\n")
    steps: list[int] = []
    bad = 0
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
            steps.append(int(event["seconds"]))
        except (ValueError, KeyError, TypeError):
            if index != len(lines) - 1:
                raise ValueError(f"an unreadable line in the middle: {raw[:80]!r}") from None
            bad += 1
    return steps, bad


# ---- the writers -------------------------------------------------------------------------------------

def _journal(root: Path, next_op: int) -> Callable[[int], None]:
    """The journal's writer. A child that resumes after an operation the last kill left unjournaled (the files hold
    op J+1 while the journal says J) records it first, so the files are never two operations past the journal."""
    handle = open(root / JOURNAL, "ab", buffering=0)       # noqa: SIM115 - open until the kill

    def done(n: int) -> None:
        handle.write(f"{n}\n".encode())

    if next_op - 1 > last_journaled(root):
        done(next_op - 1)
    return done


def _stub_nicegui() -> None:
    class Event:
        def __init__(self) -> None:
            self.callbacks: list[Callable[..., Any]] = []

        def subscribe(self, callback: Callable[..., Any], **_: Any) -> None:
            self.callbacks.append(callback)

        def emit(self, *args: Any) -> None:
            for callback in list(self.callbacks):
                callback(*args)

    module = types.ModuleType("nicegui")
    module.Event = Event                                    # type: ignore[attr-defined]
    module.run = types.SimpleNamespace(io_bound=None)       # type: ignore[attr-defined]
    sys.modules["nicegui"] = module


def tone_wav(seconds: float = 0.2, rate: int = 24_000) -> bytes:
    """A short PCM_16 mono WAV (standard library only)."""
    frames = int(seconds * rate)
    samples = bytearray()
    for i in range(frames):
        value = int(8000 * ((i // 40) % 2 * 2 - 1))
        samples += value.to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(bytes(samples))
    return buffer.getvalue()


def run_registry(root: Path, ready: Callable[[], None]) -> None:
    from engine.registry import VoiceRegistry

    registry = VoiceRegistry(registry_path(root))
    ref = str(reference(root))
    n = max(registry_ops(registry.entries(), ref)) + 1
    done = _journal(root, n)
    ready()
    while True:
        i, k = divmod(n, 3)
        if k == 0:
            registry.register(f"v{i}", MTL, ref, 0.5, "en", meta={"step": i})
        elif k == 1:
            registry.update(f"v{i}", exaggeration=0.75, meta={"updated": True})
        elif i > 0:
            registry.remove(f"v{i - 1}")
        done(n)
        n += 1


def trash_into(root: Path) -> Callable[[Path], None]:
    """The takes writer's Recycle Bin: a move into <root>\\.trash."""
    folder = root / ".trash"

    def trash(path: Path) -> None:
        folder.mkdir(exist_ok=True)
        os.replace(path, folder / f"{time.monotonic_ns()}-{Path(path).name}")

    return trash


def run_takes(root: Path, ready: Callable[[], None]) -> None:
    from ui.core.services.takes_store import NewTake, TakesStore

    store = TakesStore(takes_dir(root) / "index.json", root)
    store.load(folders=[takes_dir(root)])
    n = max(takes_ops(store.takes)) + 1
    trash = trash_into(root)
    wav = tone_wav()
    done = _journal(root, n)

    def take_of(step: int) -> Any:
        return next(take for take in store.takes if take_step(take) == step)

    ready()
    while True:
        i, k = divmod(n, 4)
        if k == 0:
            store.add(wav, NewTake(voice="aiko", backend=MTL, language="en", text=f"step {i}", exaggeration=0.5,
                                   cfg_weight=0.5, gen_seconds=0.1, session="crash", tokens=5),
                      takes_dir(root))
        elif k == 1:
            store.set_starred(take_of(i).id, True)
        elif k == 2 and i > 0:
            store.set_starred(take_of(i - 1).id, False)
        elif k == 3 and i > 0:
            store.purge_unstarred([take_of(i - 1).id], trash=trash)
        done(n)
        n += 1


def run_settings(root: Path, ready: Callable[[], None]) -> None:
    _stub_nicegui()
    store = settings_store(settings_path(root))
    journaled = last_journaled(root)
    current = store.get("theme.emiya", "fx_speed")
    n = journaled + (2 if current == settings_value(journaled + 1) else 1)
    done = _journal(root, n)
    ready()
    while True:
        if n % 2 == 0:
            store.set("theme.emiya", "fx_speed", settings_value(n))
        else:
            store.preview("theme.emiya", "fx_speed", settings_value(n))
            store.flush()
        done(n)
        n += 1


def run_history(root: Path, ready: Callable[[], None]) -> None:
    _stub_nicegui()
    from ui.contract.records import LoadEvent
    from ui.core.services.load_history import LoadHistory, now_iso

    history = LoadHistory(history_path(root), keep=HISTORY_KEEP)
    history.load()
    n = int(history.events[-1].seconds) + 1 if history.events else 0
    done = _journal(root, n)
    ready()
    while True:
        history.append(LoadEvent(t=now_iso(), kind="loaded", backend=MTL, seconds=float(n), gb=3.4, device="cuda"))
        done(n)
        n += 1


class CrashConds:
    """Stands in for Chatterbox's Conditionals: save() writes 256 KB of JSON in chunks, flushing each."""

    def __init__(self, step: int) -> None:
        self.step = step

    def save(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as sink:
            sink.write(f'{{"voice": "aiko", "step": {self.step}, "pad": "')
            for _ in range(PT_CHUNKS):
                sink.write("x" * PT_CHUNK_CHARS)
                sink.flush()
            sink.write('", "end": true}')


class CrashModel:
    """A model whose prepare_conditionals makes CrashConds for the step the writer sets."""

    def __init__(self) -> None:
        self.next_step = 0
        self.conds: CrashConds | None = None

    def prepare_conditionals(self, wav: str, exaggeration: float = 0.5) -> None:
        self.conds = CrashConds(self.next_step)


def crash_backend(cache_dir: Path) -> Any:
    """A ChatterboxBackend (the real load_voice, _bake, save_atomic and _activate) over CrashModel."""
    from engine.backends.base import ChatterboxBackend

    class CrashBackend(ChatterboxBackend):
        name = MTL

        def _from_pretrained(self, device: str) -> Any:
            return CrashModel()

        def _load_conds(self, path: Path) -> Any:
            return CrashConds(pt_step(Path(path)))

        def speak(self, text: str, voice_id: str, **_: Any) -> bytes:
            with self._lock:
                self._activate(voice_id)
            return tone_wav()

    return CrashBackend(cache_dir, device="cpu")


def run_pt(root: Path, ready: Callable[[], None]) -> None:
    backend = crash_backend(pt_path(root).parent)
    n = pt_step(pt_path(root)) + 1
    done = _journal(root, n)
    ready()
    while True:
        backend.model.next_step = n
        backend.load_voice("aiko", reference(root), exaggeration=0.5, force=True)
        done(n)
        n += 1


def run_owner(root: Path, ready: Callable[[], None]) -> None:
    from engine.lock import EngineLock

    lock = EngineLock(root / "cache" / "engine.lock", program=CHILD_PROGRAM)
    n = 0
    done = _journal(root, n)
    ready()
    while True:
        if n % 2 == 0:
            lock.acquire()
        else:
            lock.release()
        done(n)
        n += 1


def run_hold(root: Path, ready: Callable[[], None]) -> None:
    from engine.lock import EngineLock
    from ui.entry import take_instance_lock

    gui = take_instance_lock(root / "cache" / "gui.lock")
    if isinstance(gui, int):
        raise SystemExit(f"gui.lock is held: exit code {int(gui)}")
    engine = EngineLock(root / "cache" / "engine.lock", program="gui.py")
    engine.acquire()
    ready()
    threading.Event().wait(MAX_RUN_S)
    del gui, engine                                         # both held (referenced) until the kill


RUNNERS: dict[str, Callable[[Path, Callable[[], None]], None]] = {
    "registry": run_registry, "takes": run_takes, "settings": run_settings, "history": run_history,
    "pt": run_pt, "owner": run_owner, "hold": run_hold,
}


def main(argv: list[str]) -> int:
    writer, root = argv[1], Path(argv[2])
    if str(APP_ROOT) not in sys.path:
        sys.path.insert(0, str(APP_ROOT))
    os.environ["HF_HUB_OFFLINE"] = "1"
    (root / "cache").mkdir(parents=True, exist_ok=True)
    deadline = threading.Timer(MAX_RUN_S, lambda: os._exit(3))
    deadline.daemon = True
    deadline.start()

    def ready() -> None:
        sys.stdout.write(READY + "\n")
        sys.stdout.flush()

    RUNNERS[writer](root, ready)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
