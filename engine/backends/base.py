"""Backend interface, voice-id validation and the mechanics the Chatterbox backends share.

Nothing in this module imports torch or chatterbox: the model hooks of each backend import
them when they first run, so importing a backend module stays cheap and side-effect free.
"""
import contextlib
import contextvars
import io
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal
from uuid import uuid4

from ..errors import ScriptTooLong
from .info import BACKENDS

VoiceIdError = Literal["chars", "length", "reserved"]

MAX_VOICE_ID_LENGTH = 64

# Windows device names: "nul.pt" opens the NUL device instead of a file, in any letter case.
RESERVED_VOICE_IDS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(10)}
    | {f"LPT{digit}" for digit in range(10)}
)

# Always used with fullmatch(): re.match() with "$" also accepts a trailing newline.
_VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

_VOICE_ID_PROBLEMS: dict[str, str] = {
    "chars": "only letters, digits, '_' and '-' are allowed (this becomes a filename on disk).",
    "length": (
        f"use at most {MAX_VOICE_ID_LENGTH} characters (this becomes a filename on disk)."
    ),
    "reserved": (
        "it is a reserved device name on Windows (CON, PRN, AUX, NUL, COM0-9, LPT0-9)."
    ),
}

_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY_S = 0.05
_staging: contextvars.ContextVar[tuple[Path, Path] | None] = contextvars.ContextVar("conditioning_stage", default=None)


@contextlib.contextmanager
def stage_conditioning(path: Path, *, cached: bool, invalidate: Callable[[], None]) -> Iterator[Path | None]:
    """Keep a bake private until the registry publishes its file and metadata together."""
    if cached:
        yield None
        return
    staged = path.with_name(f"{path.name}.stage-{os.getpid()}-{uuid4().hex}")
    token = _staging.set((path, staged))
    try:
        yield staged
    except BaseException:
        invalidate()
        raise
    finally:
        _staging.reset(token)
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()


def voice_id_error(voice_id: str) -> VoiceIdError | None:
    """Why voice_id cannot name a file on disk ("chars", "length", "reserved"), or None."""
    if not isinstance(voice_id, str) or not _VOICE_ID_RE.fullmatch(voice_id):
        return "chars"
    if len(voice_id) > MAX_VOICE_ID_LENGTH:
        return "length"
    if voice_id.upper() in RESERVED_VOICE_IDS:
        return "reserved"
    return None


def validate_voice_id(voice_id: str) -> None:
    """Raise ValueError if voice_id isn't safe to use as a filename.

    Every backend turns voice_id into a path component (cache_dir / f"{voice_id}.pt").
    Without this check, a voice_id like "../../etc/passwd" could write outside cache_dir,
    and "nul" would "save" into the NUL device.
    """
    error = voice_id_error(voice_id)
    if error is not None:
        raise ValueError(f"Invalid voice_id {voice_id!r}: {_VOICE_ID_PROBLEMS[error]}")


def save_atomic(save: Callable[[Path], object], path: Path) -> None:
    """Write path through save(<name>.tmp-<pid>) and os.replace.

    A quit or crash in the middle of save() (NiceGUI exits with os._exit) never leaves a
    torn file at path: it holds either the old content or the new. The temp file is removed
    whatever happens.
    """
    staging = _staging.get()
    if staging is not None and staging[0] == path:
        save(staging[1])
        return
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        save(tmp)
        _replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried: a reader's open handle makes it raise PermissionError on Windows."""
    for _ in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(_REPLACE_RETRY_DELAY_S)
    os.replace(src, dst)  # final attempt; let it raise if it still fails


class VoiceBackend(ABC):
    """A voice-cloning backend. One instance per model, many voices per instance."""

    name: str  # registry key, e.g. "chatterbox-multilingual"

    # Seconds the last speak() spent loading the voice's cached conditionals; None when the
    # voice was already active. The engine copies it to Job.conds_load_s after speak().
    last_conds_load_s: float | None = None

    @abstractmethod
    def load_voice(self, voice_id: str, reference_wav: Path, **params: Any) -> str:
        """Prepare and cache conditionals for a voice. Returns voice_id."""

    @abstractmethod
    def speak(self, text: str, voice_id: str, **params: Any) -> bytes:
        """Generate WAV bytes for `text` in `voice_id`'s voice."""

    @abstractmethod
    def list_voices(self) -> list[str]:
        """Voice IDs with cached conditionals available to this backend."""


class ChatterboxBackend(VoiceBackend):
    """What the three Chatterbox backends share: one model, one Lock, one cached .pt per voice.

    self.model.conds is shared, mutable state. FastAPI runs sync endpoints in a threadpool,
    so two concurrent requests for two different voices could otherwise interleave: request A
    points the model at its voice, request B overwrites that before A calls generate(), and A
    ends up speaking in B's voice. self._lock serialises "point at a voice, then generate".

    Subclasses supply the model hooks (_from_pretrained, _load_conds and, where the model
    differs, _check_clip and _prepare) and their own speak().
    """

    def __init__(self, cache_dir: Path, device: str = "cuda") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.model = self._from_pretrained(device)
        self._active_voice: str | None = None
        self.last_conds_load_s: float | None = None
        self._lock = Lock()

    # ---------- model hooks ----------

    @abstractmethod
    def _from_pretrained(self, device: str) -> Any:
        """Build the model from its weights (the slow part of a backend load)."""

    @abstractmethod
    def _load_conds(self, path: Path) -> Any:
        """Read a voice's cached conditionals from its .pt file."""

    def _text_tokens(self, text: str, language_id: str | None) -> int:
        """Count the same input IDs as generate(), on CPU before it touches the voice."""
        raise NotImplementedError

    def _text_limit(self) -> int:
        # Standard/Multilingual reserve two *additional* positions for the start/stop tokens.
        return min(BACKENDS[self.name].max_text_tokens, self.model.t3.hp.max_text_tokens)

    def _check_text(self, text: str, language_id: str | None = None) -> None:
        count = self._text_tokens(text, language_id)
        limit = self._text_limit()
        if count > limit:
            raise ScriptTooLong(count, limit)

    def _check_clip(self, reference_wav: Path) -> None:
        """Raise ValueError for a clip the model cannot bake, before anything changes."""

    def _prepare(self, reference_wav: Path, exaggeration: float) -> None:
        """Bake conditionals from the reference clip into self.model.conds."""
        self.model.prepare_conditionals(str(reference_wav), exaggeration=exaggeration)

    def _wav_bytes(self, wav: Any) -> bytes:
        """Encode generate()'s tensor as WAV bytes at the model's sample rate."""
        import torchaudio as ta

        buf = io.BytesIO()
        ta.save(buf, wav, self.model.sr, format="wav")
        return buf.getvalue()

    # ---------- voices ----------

    def _conds_path(self, voice_id: str) -> Path:
        return self.cache_dir / f"{voice_id}.pt"

    def _missing_conds_text(self, voice_id: str) -> str:
        return (
            f"Voice '{voice_id}' has no cached conditionals for {self.name}. "
            f"Call load_voice(backend='{self.name}') first."
        )

    def load_voice(
        self,
        voice_id: str,
        reference_wav: Path,
        exaggeration: float = 0.5,
        force: bool = False,
        **_: Any,
    ) -> str:
        """Point the model at voice_id: reuse its cached .pt, or bake one (none yet, or force)."""
        validate_voice_id(voice_id)
        path = self._conds_path(voice_id)
        with self._lock:
            if path.exists() and not force:
                self.model.conds = self._load_conds(path)
            else:
                self._bake(path, Path(reference_wav), exaggeration)
            self._active_voice = voice_id
        return voice_id

    def _bake(self, path: Path, reference_wav: Path, exaggeration: float) -> None:
        """Bake conditionals from the clip and save them atomically. Call with the lock held.

        No voice is active from the moment model.conds starts to change until the new .pt is
        in place: if the bake or the save fails, the next take reloads its voice from disk
        instead of speaking with the new clip's conditionals under the old voice's name.
        """
        self._check_clip(reference_wav)
        self._active_voice = None
        self._prepare(reference_wav, exaggeration)
        save_atomic(self.model.conds.save, path)

    def _activate(self, voice_id: str) -> None:
        """Load voice_id's .pt into the model unless it is already active. Call with the lock held.

        Sets last_conds_load_s to the seconds the load took, or None when nothing was loaded.
        """
        self.last_conds_load_s = None
        if voice_id == self._active_voice:
            return
        path = self._conds_path(voice_id)
        if not path.exists():
            raise ValueError(self._missing_conds_text(voice_id))
        started = time.perf_counter()
        self.model.conds = self._load_conds(path)
        self.last_conds_load_s = time.perf_counter() - started
        self._active_voice = voice_id

    def list_voices(self) -> list[str]:
        return sorted(p.stem for p in self.cache_dir.glob("*.pt"))
