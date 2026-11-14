"""engine.lock.EngineLock: cross-process, thread-agnostic, with a holder record (BUILD_SPEC §3.3)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO

import pytest

from engine import lock as lock_module
from engine.errors import EngineBusy
from engine.lock import OWNER_FILE_NAME, EngineLock

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60
SINCE = re.compile(r"^\d\d:\d\d:\d\d$")

HOLDER_SCRIPT = """
import os, sys
from pathlib import Path
from engine.lock import EngineLock
lock = EngineLock(Path(sys.argv[1]), program="holder-a")
lock.acquire()
print("held", os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""

TRY_SCRIPT = """
import json, os, sys
from pathlib import Path
from engine.errors import EngineBusy
from engine.lock import EngineLock
lock = EngineLock(Path(sys.argv[1]), program="tester-b")
try:
    lock.acquire()
except EngineBusy as exc:
    print(json.dumps({"busy": True, "message": str(exc), "holder": exc.holder}))
else:
    print(json.dumps({"busy": False, "pid": os.getpid(), "held": lock.held(), "holder": lock.holder()}))
    lock.release()
"""


def _env() -> dict[str, str]:
    return dict(os.environ, HF_HUB_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")


def _read_line(stream: IO[str], timeout: float = TIMEOUT_S) -> str:
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return lines[0].strip() if lines else ""


def _try_from_another_process(path: Path) -> dict:
    done = subprocess.run([sys.executable, "-c", TRY_SCRIPT, str(path)], cwd=ROOT, env=_env(),
                          capture_output=True, text=True, timeout=TIMEOUT_S)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "engine.lock"


def test_two_processes(lock_path: Path) -> None:
    """B is refused while A holds the lock, and names A; once A releases, B gets it."""
    holder = subprocess.Popen([sys.executable, "-c", HOLDER_SCRIPT, str(lock_path)], cwd=ROOT, env=_env(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # The interpreter's own pid: a venv python.exe on Windows is a launcher with a child.
        word, _, pid_text = _read_line(holder.stdout).partition(" ")
        assert word == "held"
        holder_pid = int(pid_text)
        refused = _try_from_another_process(lock_path)
        assert refused["busy"] is True
        assert refused["holder"]["pid"] == holder_pid
        assert refused["holder"]["program"] == "holder-a"
        assert SINCE.match(refused["holder"]["since"])
        assert refused["message"] == (
            f"The voice engine is in use by holder-a (pid {holder_pid}) since {refused['holder']['since']}. "
            "Close it, or wait for it to finish, then try again.")

        out, err = holder.communicate("release\n", timeout=TIMEOUT_S)
        assert holder.returncode == 0, err
        assert out.strip() == "released"

        taken = _try_from_another_process(lock_path)
        assert taken["busy"] is False and taken["held"] is True
        assert taken["holder"]["pid"] == taken["pid"]
        assert taken["holder"]["program"] == "tester-b"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(TIMEOUT_S)
    assert not lock_path.with_name(OWNER_FILE_NAME).exists()


def test_acquire_on_one_thread_release_on_another(lock_path: Path) -> None:
    engine_lock = EngineLock(lock_path, program="gui")
    first = threading.Thread(target=engine_lock.acquire)
    first.start()
    first.join()
    assert engine_lock.held()
    second = threading.Thread(target=engine_lock.release)
    second.start()
    second.join()
    assert not engine_lock.held()
    other = EngineLock(lock_path, program="other")
    other.acquire()                    # the OS lock really is free
    assert other.held()
    other.release()


def test_a_second_holder_in_this_process_is_refused(lock_path: Path) -> None:
    first = EngineLock(lock_path, program="cli.py")
    first.acquire()
    try:
        second = EngineLock(lock_path, program="gui.py")
        with pytest.raises(EngineBusy) as refused:
            second.acquire()
        holder = refused.value.holder
        assert holder["pid"] == os.getpid() and holder["program"] == "cli.py"
        assert str(refused.value).startswith(f"The voice engine is in use by cli.py (pid {os.getpid()}) since ")
        assert refused.value.__context__ is None
        assert not second.held()
    finally:
        first.release()


def test_without_an_owner_record_the_message_is_generic(lock_path: Path) -> None:
    first = EngineLock(lock_path, program="cli.py")
    first.acquire()
    try:
        first.owner_path.unlink()
        with pytest.raises(EngineBusy) as refused:
            EngineLock(lock_path).acquire()
        assert refused.value.holder is None
        assert str(refused.value) == ("The voice engine is in use by another program. "
                                      "Close it, or wait for it to finish, then try again.")
    finally:
        first.release()


def test_acquire_and_release_are_idempotent(lock_path: Path) -> None:
    engine_lock = EngineLock(lock_path, program="gui")
    engine_lock.release()              # not held: nothing happens
    engine_lock.acquire()
    engine_lock.acquire()
    assert engine_lock.held()
    engine_lock.release()              # one release frees it, whatever the acquire count
    assert not engine_lock.held()
    engine_lock.release()
    other = EngineLock(lock_path)
    other.acquire()
    other.release()


def test_owner_file_written_atomically_and_removed_on_release(lock_path: Path) -> None:
    engine_lock = EngineLock(lock_path, program="gui.py")
    assert engine_lock.owner_path == lock_path.with_name("engine.owner.json")
    engine_lock.acquire()
    record = json.loads(engine_lock.owner_path.read_text(encoding="utf-8"))
    assert record["pid"] == os.getpid() and record["program"] == "gui.py"
    assert SINCE.match(record["since"])
    assert engine_lock.holder() == record
    assert list(lock_path.parent.glob(f"{OWNER_FILE_NAME}.tmp-*")) == []
    engine_lock.release()
    assert not engine_lock.owner_path.exists()
    assert engine_lock.holder() is None


def test_a_stale_owner_file_is_overwritten(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True)
    owner = lock_path.with_name(OWNER_FILE_NAME)
    owner.write_text(json.dumps({"pid": 999999, "program": "stale.py", "since": "00:00:00"}), encoding="utf-8")
    engine_lock = EngineLock(lock_path, program="gui.py")
    engine_lock.acquire()
    try:
        assert engine_lock.holder()["pid"] == os.getpid()
        assert engine_lock.holder()["program"] == "gui.py"
    finally:
        engine_lock.release()


def test_a_failed_owner_write_keeps_the_lock_and_drops_the_stale_record(
        lock_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path.parent.mkdir(parents=True)
    owner = lock_path.with_name(OWNER_FILE_NAME)
    owner.write_text(json.dumps({"pid": 999999, "program": "stale.py", "since": "00:00:00"}), encoding="utf-8")

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(lock_module, "_replace", refuse)
    engine_lock = EngineLock(lock_path, program="gui.py")
    engine_lock.acquire()
    try:
        assert engine_lock.held()
        assert not owner.exists()
        assert list(lock_path.parent.glob(f"{OWNER_FILE_NAME}.tmp-*")) == []
    finally:
        engine_lock.release()


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps(["pid", 1]),
    json.dumps({"pid": "1", "program": "cli.py", "since": "10:00:00"}),
    json.dumps({"pid": True, "program": "cli.py", "since": "10:00:00"}),
    json.dumps({"pid": 1, "program": "cli.py"}),
])
def test_holder_is_none_for_unusable_records(lock_path: Path, content: str) -> None:
    lock_path.parent.mkdir(parents=True)
    lock_path.with_name(OWNER_FILE_NAME).write_text(content, encoding="utf-8")
    assert EngineLock(lock_path).holder() is None


def test_holder_is_none_without_a_record(lock_path: Path) -> None:
    assert EngineLock(lock_path).holder() is None


@pytest.mark.parametrize(("argv", "expected"), [
    ([r"E:\Voice Tool\cli.py", "speak"], "cli.py"),
    (["gui.py"], "gui.py"),
    ([""], "python"),
    ([], "python"),
])
def test_program_defaults_to_the_script_name(lock_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             argv: list[str], expected: str) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    assert EngineLock(lock_path).program == expected
    assert EngineLock(lock_path, program="app.py").program == "app.py"
