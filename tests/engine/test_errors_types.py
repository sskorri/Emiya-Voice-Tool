"""engine.errors messages and data; engine.types records (BUILD_SPEC §3.1, §3.7)."""
from __future__ import annotations

import copy
import dataclasses
import pickle
from pathlib import Path

import pytest

from engine.errors import (
    BackendLoadError,
    BackendNotResident,
    Cancelled,
    EngineBusy,
    GenerationError,
    first_line,
)
from engine.types import DeleteResult, EngineEvent, LoadReport, Residency, VoiceDetails


# ---------- errors ----------

def test_backend_not_resident_message_and_fields() -> None:
    plain = BackendNotResident("chatterbox-turbo", ())
    assert str(plain) == "chatterbox-turbo is not loaded"
    assert plain.backend == "chatterbox-turbo" and plain.resident == ()
    other = BackendNotResident("chatterbox-turbo", ("chatterbox-multilingual",))
    assert str(other) == "chatterbox-turbo is not loaded (chatterbox-multilingual is)"
    assert other.resident == ("chatterbox-multilingual",)
    assert isinstance(other, RuntimeError)


def test_backend_not_resident_lists_several_resident_backends() -> None:
    error = BackendNotResident("chatterbox-turbo", ["chatterbox-multilingual", "chatterbox-standard"])
    assert error.resident == ("chatterbox-multilingual", "chatterbox-standard")
    assert str(error) == ("chatterbox-turbo is not loaded "
                          "(chatterbox-multilingual, chatterbox-standard are)")


def test_backend_load_error_message_and_fields() -> None:
    error = BackendLoadError("chatterbox-turbo", "out_of_memory", "CUDA out of memory", 2.5)
    assert str(error) == "chatterbox-turbo failed to load: CUDA out of memory"
    assert (error.backend, error.kind, error.detail, error.seconds) == (
        "chatterbox-turbo", "out_of_memory", "CUDA out of memory", 2.5)
    assert isinstance(error, RuntimeError) and not isinstance(error, ValueError)


def test_engine_busy_names_the_holder() -> None:
    error = EngineBusy({"pid": 4242, "program": "cli.py", "since": "14:02:11"})
    assert str(error) == ("The voice engine is in use by cli.py (pid 4242) since 14:02:11. "
                          "Close it, or wait for it to finish, then try again.")
    assert error.holder == {"pid": 4242, "program": "cli.py", "since": "14:02:11"}
    assert isinstance(error, RuntimeError)


@pytest.mark.parametrize("holder", [None, {}, {"pid": 7}, {"program": "cli.py", "since": "x"}])
def test_engine_busy_without_a_usable_holder(holder: dict | None) -> None:
    assert str(EngineBusy(holder)) == ("The voice engine is in use by another program. "
                                      "Close it, or wait for it to finish, then try again.")


def test_engine_busy_copies_the_holder() -> None:
    holder = {"pid": 1, "program": "app.py", "since": "09:00:00"}
    error = EngineBusy(holder)
    holder["pid"] = 2
    assert error.holder["pid"] == 1


def test_generation_error_keeps_the_first_line_at_most_200_chars() -> None:
    error = GenerationError("other", "Sizes of tensors must match\n  File ...\nmore", during="speak")
    assert str(error) == "Sizes of tensors must match" == error.detail
    assert (error.kind, error.during) == ("other", "speak")
    long = GenerationError("out_of_memory", "x" * 500, during="bake")
    assert str(long) == "x" * 200 and long.during == "bake" and long.kind == "out_of_memory"
    assert isinstance(error, RuntimeError) and not isinstance(error, ValueError)
    assert error.__context__ is None and error.__cause__ is None


def test_generation_error_during_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        GenerationError("other", "boom", "speak")  # type: ignore[misc]


def test_cancelled_is_a_plain_exception() -> None:
    assert issubclass(Cancelled, Exception) and not issubclass(Cancelled, RuntimeError)
    assert Cancelled.__doc__ == "A job was cancelled before it started or inside a sampling/flow loop."


@pytest.mark.parametrize("error", [
    BackendNotResident("chatterbox-turbo", ("chatterbox-multilingual",)),
    BackendLoadError("chatterbox-standard", "missing_files", "model files are missing", 0.4),
    EngineBusy({"pid": 4242, "program": "cli.py", "since": "14:02:11"}),
    EngineBusy(None),
    GenerationError("other", "boom", during="bake"),
    Cancelled(),
])
def test_errors_pickle_and_copy_with_their_data(error: Exception) -> None:
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error)):
        assert type(clone) is type(error)
        assert str(clone) == str(error)
        assert vars(clone) == vars(error)


@pytest.mark.parametrize(("text", "expected"), [
    ("one\ntwo", "one"),
    ("\n\n  spaced  \nnext", "spaced"),
    ("", ""),
    ("y" * 300, "y" * 200),
])
def test_first_line(text: str, expected: str) -> None:
    assert first_line(text) == expected


# ---------- types ----------

def _field_names(cls: type) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def test_residency_fields_and_frozen() -> None:
    assert _field_names(Residency) == ["resident", "loading", "unloading", "busy", "device",
                                       "preferred_device", "max_resident", "last_used", "lock_held"]
    residency = Residency(resident=("chatterbox-turbo",), loading=None, unloading=None, busy=None,
                          device="cuda", preferred_device="cuda", max_resident=1, last_used=None,
                          lock_held=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        residency.busy = "load"  # type: ignore[misc]
    assert residency == dataclasses.replace(residency)


def test_load_report_fields() -> None:
    assert _field_names(LoadReport) == ["backend", "device", "seconds", "already_resident", "evicted",
                                        "freed_bytes", "reserved_bytes", "allocated_bytes",
                                        "first_in_process"]
    report = LoadReport("chatterbox-turbo", "cpu", 0.0, True, (), None, None, None, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.seconds = 1.0  # type: ignore[misc]


def test_delete_result_fields() -> None:
    assert _field_names(DeleteResult) == ["removed_entry", "removed_files", "failed_files"]
    result = DeleteResult(True, (Path("a.pt"),), ((Path("b.pt"), "kept: belongs to Aiko"),))
    assert result.failed_files[0][1] == "kept: belongs to Aiko"


def test_voice_details_fields() -> None:
    assert _field_names(VoiceDetails) == [
        "voice_id", "backend", "backend_enabled", "language", "exaggeration", "reference_wav",
        "reference_path", "conds_path", "conds_exists", "created_at", "forged_at", "consent", "meta"]


def test_engine_event_defaults() -> None:
    assert _field_names(EngineEvent) == [
        "kind", "backend", "at", "seconds", "device", "reserved_bytes", "allocated_bytes",
        "freed_bytes", "peak_bytes", "reason", "error", "first_in_process"]
    event = EngineEvent("load_started", "chatterbox-turbo", 1.0)
    assert (event.seconds, event.device, event.reserved_bytes, event.allocated_bytes, event.freed_bytes,
            event.peak_bytes, event.reason, event.error, event.first_in_process) == (None,) * 9
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = "loaded"  # type: ignore[misc]
