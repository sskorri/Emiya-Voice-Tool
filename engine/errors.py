"""Exception types the engine raises to its callers (GUI, cli.py, app.py).

Each carries plain data only (strings, numbers, a dict), never a model object or
another exception, so holding one can never keep VRAM alive. They pickle and copy
with their own constructors.
"""
from __future__ import annotations

from typing import Any, Literal

__all__ = [
    "BackendLoadError",
    "BackendNotResident",
    "Cancelled",
    "EngineBusy",
    "GenerationError",
    "ScriptTooLong",
    "first_line",
]

MAX_DETAIL_CHARS = 200

LoadFailure = Literal["out_of_memory", "missing_files", "other"]
GenerationFailure = Literal["out_of_memory", "other"]


def first_line(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """The first non-empty line of `text`, stripped and cut to `limit` characters."""
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


class BackendNotResident(RuntimeError):
    """A call that must not load a model found its backend not resident."""

    def __init__(self, backend: str, resident: tuple[str, ...]) -> None:
        self.backend = backend
        self.resident = tuple(resident)
        message = f"{backend} is not loaded"
        if len(self.resident) == 1:
            message += f" ({self.resident[0]} is)"
        elif self.resident:
            message += f" ({', '.join(self.resident)} are)"
        super().__init__(message)

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.backend, self.resident))


class BackendLoadError(RuntimeError):
    """Building a backend (the model load) failed; nothing of it stays resident."""

    def __init__(self, backend: str, kind: LoadFailure, detail: str, seconds: float) -> None:
        self.backend = backend
        self.kind = kind
        self.detail = detail
        self.seconds = seconds
        super().__init__(f"{backend} failed to load: {detail}")

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.backend, self.kind, self.detail, self.seconds))


class EngineBusy(RuntimeError):
    """Another process holds the cross-process engine lock (it has a model resident)."""

    def __init__(self, holder: dict | None) -> None:
        self.holder = dict(holder) if holder else None
        super().__init__(_busy_message(self.holder))

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.holder,))


class GenerationError(RuntimeError):
    """A take (`during="speak"`) or a bake (`during="bake"`) failed inside the model.

    `str(e)` is `detail`: the first line of the original message, at most 200
    characters. The engine raises it fresh, outside its except block, so it has no
    `__context__` holding model frames.
    """

    def __init__(self, kind: GenerationFailure, detail: str, *,
                 during: Literal["speak", "bake"]) -> None:
        self.kind = kind
        self.detail = first_line(detail)
        self.during = during
        super().__init__(self.detail)

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_generation_error, (self.kind, self.detail, self.during))


class Cancelled(Exception):
    """A job was cancelled before it started or inside a sampling/flow loop."""


class ScriptTooLong(ValueError):
    """The normalized script exceeds this backend's input token limit."""

    def __init__(self, count: int, limit: int) -> None:
        self.count, self.limit = count, limit
        super().__init__(f"Script too long · {count:,} tokens, limit {limit:,}")

    def __reduce__(self) -> tuple[Any, ...]:
        return type(self), (self.count, self.limit)


def _busy_message(holder: dict | None) -> str:
    tail = "Close it, or wait for it to finish, then try again."
    if holder:
        program, pid, since = holder.get("program"), holder.get("pid"), holder.get("since")
        if program and pid is not None and since:
            return f"The voice engine is in use by {program} (pid {pid}) since {since}. {tail}"
    return f"The voice engine is in use by another program. {tail}"


def _rebuild_generation_error(kind: GenerationFailure, detail: str,
                              during: Literal["speak", "bake"]) -> GenerationError:
    return GenerationError(kind, detail, during=during)
