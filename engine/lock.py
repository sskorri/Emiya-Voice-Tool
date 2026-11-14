"""Cross-process engine lock: cache\\engine.lock, held by a process while it has a model resident.

Any process that loads a model through a VoiceEngine (the GUI, cli.py, app.py) takes it,
so two processes can never hold models on the one GPU at the same time. The OS drops
the lock when its process dies, including NiceGUI's os._exit(0). Next to it,
engine.owner.json names the holder for the EngineBusy message; it is written right
after acquiring and read only when acquiring fails, so a stale copy is harmless and
the next holder overwrites it.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from .errors import EngineBusy

__all__ = ["OWNER_FILE_NAME", "EngineLock"]

OWNER_FILE_NAME = "engine.owner.json"
_REPLACE_TRIES = 5
_REPLACE_DELAY_S = 0.05


class EngineLock:
    """Non-blocking, process-wide (not thread-local) lock on one lock file."""

    def __init__(self, path: Path, program: str | None = None) -> None:
        self.path = Path(path)
        self.owner_path = self.path.with_name(OWNER_FILE_NAME)
        self.program = program or _default_program()
        # thread_local=False: a lock taken on one io_bound worker must release from another.
        self._lock = FileLock(str(self.path), timeout=0, thread_local=False)
        self._mx = threading.Lock()

    def acquire(self) -> None:
        """Take the lock (idempotent). Raises EngineBusy at once when another process holds it."""
        with self._mx:
            if self._lock.is_locked:
                return
            if not self._try_lock():
                raise EngineBusy(self.holder())
            self._write_owner()

    def release(self) -> None:
        """Delete the owner file, then release (idempotent)."""
        with self._mx:
            if not self._lock.is_locked:
                return
            with contextlib.suppress(OSError):
                self.owner_path.unlink()
            self._lock.release(force=True)

    def held(self) -> bool:
        """True while this process holds the lock."""
        return self._lock.is_locked

    def holder(self) -> dict | None:
        """{"pid", "program", "since"} from the owner file; None if missing or unreadable."""
        try:
            data = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not _valid_owner(data):
            return None
        return {"pid": data["pid"], "program": data["program"], "since": data["since"]}

    def _try_lock(self) -> bool:
        try:
            self._lock.acquire()
        except Timeout:
            return False
        return True

    def _write_owner(self) -> None:
        record = {
            "pid": os.getpid(),
            "program": self.program,
            "since": datetime.now().strftime("%H:%M:%S"),
        }
        tmp = self.owner_path.with_name(f"{self.owner_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            tmp.write_text(json.dumps(record), encoding="utf-8")
            _replace(tmp, self.owner_path)
        except OSError:
            # The record only names the holder in EngineBusy messages; the lock itself is
            # held. Never leave an older holder's record behind under our lock.
            with contextlib.suppress(OSError):
                self.owner_path.unlink()
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _default_program() -> str:
    return os.path.basename(sys.argv[0] if sys.argv else "") or "python"


def _valid_owner(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    pid = data.get("pid")
    return (isinstance(pid, int) and not isinstance(pid, bool)
            and isinstance(data.get("program"), str) and isinstance(data.get("since"), str))


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried: a reader's open handle can make it fail on Windows for a moment."""
    for _ in range(_REPLACE_TRIES - 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(_REPLACE_DELAY_S)
    os.replace(src, dst)
