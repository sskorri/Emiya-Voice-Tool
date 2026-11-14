"""JSON-lines event files, one writer per file (lifted from spikes\\theme_boot\\eventlog.py).

The app's smoke plan writes plan.jsonl; the harness tails it while the app runs and answers checkpoints
with ack files (ack_path). A line is opened, written and closed per event, so every event is on disk the
moment emit() returns. Creating an EventLog writes nothing. Standard library only.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from tests.native import clock

PLAN_EVENTS = "plan.jsonl"          # the smoke plan's events, in the run's smoke folder
ACK_DIR = "acks"                    # the harness's answers: acks\<seq>.json


class EventLog:
    def __init__(self, path: Path, role: str) -> None:
        self.path = Path(path)
        self.role = role
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event as a JSON line and return the record (with its seq and precise time)."""
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "t": clock.now(), "pid": os.getpid(), "role": self.role, "event": event,
                      **fields}
            line = json.dumps(record, default=str, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        return record


class Tail:
    """Reads the complete lines appended to an event file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._offset = 0

    def read(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self._offset += end + 1
        events = []
        for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every complete JSON line of an event file (a torn last line is skipped)."""
    return Tail(path).read()


def ack_path(folder: Path, seq: int) -> Path:
    return Path(folder) / ACK_DIR / f"{seq}.json"


def write_ack(folder: Path, seq: int, payload: dict[str, Any]) -> None:
    """Answer checkpoint `seq`: written to a temporary name, then renamed, so the plan never reads half a file."""
    path = ack_path(folder, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    os.replace(tmp, path)
