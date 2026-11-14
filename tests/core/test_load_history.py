"""LoadHistory (§4.15.3): JSONL append and read, skipped lines, compaction, estimates, line copy."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ui.contract.records import LoadEvent
from ui.core.services.load_history import (
    LoadEstimate,
    LoadHistory,
    backend_label,
    event_text,
    history_rows,
    now_iso,
)

TURBO, STANDARD, MTL = "chatterbox-turbo", "chatterbox-standard", "chatterbox-multilingual"
T0 = "2026-11-13T10:00:00+00:00"


def ev(kind: str, backend: str = TURBO, **fields) -> LoadEvent:
    return LoadEvent(t=fields.pop("t", T0), kind=kind, backend=backend, **fields)


def loaded(seconds: float | None, backend: str = TURBO, **fields) -> LoadEvent:
    return ev("loaded", backend, seconds=seconds, **fields)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "load_history.jsonl"


def filled(path: Path, events: list[LoadEvent], **options) -> LoadHistory:
    history = LoadHistory(path, **options)
    for event in events:
        history.append(event)
    return history


# ---------- append and read ----------

def test_append_writes_one_json_line_per_event_and_load_reads_them_back(path: Path) -> None:
    events = [
        ev("switching", backend=STANDARD, from_backend=TURBO),
        ev("loading", reason="warm-up"),
        loaded(8.7, gb=3.1, cold=True),
        ev("unloaded", gb=3.1, peak_gb=3.6, reason="switch"),
        ev("load_failed", reason="CUDA out of memory"),
        ev("closed", gb=3.4, peak_gb=3.9, device="cuda"),
        ev("forged", backend=MTL, seconds=2.1),
    ]
    history = filled(path, events)
    assert history.events == tuple(events)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    assert json.loads(lines[2]) == {"t": T0, "kind": "loaded", "backend": TURBO, "from_backend": "",
                                    "seconds": 8.7, "gb": 3.1, "peak_gb": None, "reason": "",
                                    "device": "cuda", "cold": True}
    fresh = LoadHistory(path)
    fresh.load()
    assert fresh.events == tuple(events)


def test_load_of_a_missing_file_is_empty_and_writes_nothing(path: Path) -> None:
    history = LoadHistory(path)
    history.load()
    assert history.events == ()
    assert not path.exists()


def test_append_creates_the_cache_folder(path: Path) -> None:
    assert not path.parent.exists()
    LoadHistory(path).append(loaded(9.0))
    assert path.is_file()


def test_malformed_lines_and_a_partial_last_line_are_skipped(path: Path) -> None:
    good = [loaded(9.0), loaded(10.0, backend=STANDARD)]
    partial = '{"t": "2026-11-13T10:00:00+00:00", "kind": "load'
    path.parent.mkdir(parents=True)
    text = "\n".join([
        json.dumps({"t": T0, "kind": "loaded", "backend": TURBO, "seconds": 9.0}),  # optional fields missing
        "not json at all",
        json.dumps({"t": 5, "kind": "loaded", "backend": TURBO}),                   # t is not a string
        json.dumps({"t": "yesterday", "kind": "loaded", "backend": TURBO}),         # t is not ISO-8601
        json.dumps({"t": T0, "kind": "teleported", "backend": TURBO}),              # unknown kind
        json.dumps({"t": T0, "kind": "loaded", "backend": TURBO, "seconds": "9"}),  # wrong type
        json.dumps({"t": T0, "kind": "loaded", "backend": TURBO, "cold": 1}),       # 1 is not a bool
        '{"t": "2026-11-13T10:00:00+00:00", "kind": "loaded", "backend": "x", "seconds": NaN}',
        "[1, 2, 3]",
        "",
        json.dumps({"t": T0, "kind": "loaded", "backend": STANDARD, "seconds": 10, "extra": "ignored"}),
        partial,
    ])
    path.write_text(text, encoding="utf-8")
    history = LoadHistory(path)
    history.load()
    assert history.events == tuple(good)
    # the unreadable lines are cleaned out, and the next append starts on a line of its own
    history.append(loaded(11.0))
    again = LoadHistory(path)
    again.load()
    assert again.events == (*good, loaded(11.0))


def test_append_after_a_hard_exit_starts_a_new_line(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps({"t": T0, "kind": "loaded", "backend": TURBO, "seconds": 9.0}).encode()
                     + b'\n{"t": "2026-11-1')
    history = LoadHistory(path)          # no load(): append must still never glue onto the partial line
    history.append(loaded(10.0))
    fresh = LoadHistory(path)
    fresh.load()
    assert fresh.events == (loaded(9.0), loaded(10.0))


def test_compaction_keeps_the_last_events_and_rewrites_atomically(path: Path) -> None:
    path.parent.mkdir(parents=True)
    events = [loaded(float(i)) for i in range(520)]
    path.write_text("".join(json.dumps(e.__dict__) + "\n" for e in events), encoding="utf-8")
    history = LoadHistory(path)
    history.load()
    assert history.keep == 500
    assert history.events == tuple(events[-500:])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 500
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]       # no temp file left behind
    untouched = path.stat().st_mtime_ns
    LoadHistory(path).load()                                                   # 500 lines: nothing to rewrite
    assert path.stat().st_mtime_ns == untouched


def test_memory_keeps_only_the_last_keep_events(path: Path) -> None:
    history = filled(path, [loaded(float(i)) for i in range(8)], keep=5)
    assert [e.seconds for e in history.events] == [3.0, 4.0, 5.0, 6.0, 7.0]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 8            # the file compacts at load()
    fresh = LoadHistory(path, keep=5)
    fresh.load()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5


def test_append_refuses_an_event_that_would_not_read_back(path: Path) -> None:
    history = LoadHistory(path)
    with pytest.raises(ValueError):
        history.append(ev("loaded", t="yesterday"))
    with pytest.raises(ValueError):
        history.append(LoadEvent(t=T0, kind="exploded", backend=TURBO))  # type: ignore[arg-type]
    assert history.events == ()
    assert not path.exists()


def test_non_finite_measurements_are_stored_as_unknown(path: Path) -> None:
    history = LoadHistory(path)
    history.append(loaded(float("nan"), gb=float("inf")))
    assert history.events == (loaded(None, gb=None),)
    fresh = LoadHistory(path)
    fresh.load()
    assert fresh.events == (loaded(None, gb=None),)


def test_keep_must_be_positive(path: Path) -> None:
    with pytest.raises(ValueError):
        LoadHistory(path, keep=0)


# ---------- estimates ----------

def test_median_of_the_last_five_loads_per_backend_and_device(path: Path) -> None:
    history = filled(path, [
        loaded(30.0), loaded(9.0), loaded(10.0, device="cpu"), loaded(11.0), loaded(8.0),
        loaded(20.0, backend=STANDARD), loaded(12.0), loaded(7.0),
    ])
    turbo = history.estimate(TURBO, "cuda", cold=False)
    assert turbo.about_s == 9.0                    # median of 9, 11, 8, 12, 7: the 30 s load fell out
    assert turbo.last_s == 7.0
    assert turbo.count == 6
    assert history.estimate(TURBO, "cpu", cold=False).about_s == 10.0
    assert history.estimate(STANDARD, "cuda", cold=False).about_s == 20.0
    assert history.estimate(MTL, "cuda") == LoadEstimate()


def test_estimate_prefers_loads_of_the_same_cold_class(path: Path) -> None:
    history = filled(path, [loaded(25.0, cold=True), loaded(9.0), loaded(27.0, cold=True), loaded(10.0),
                            loaded(11.0)])
    assert history.estimate(TURBO, "cuda", cold=True).about_s == 26.0
    assert history.estimate(TURBO, "cuda", cold=False).about_s == 10.0
    only_warm = filled(path.with_name("warm.jsonl"), [loaded(9.0), loaded(10.0)])
    assert only_warm.estimate(TURBO, "cuda", cold=True).about_s == 9.5  # no cold load: all of them


def test_the_next_load_is_cold_until_this_process_has_loaded(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(e.__dict__) + "\n"
                            for e in (loaded(25.0, cold=True), loaded(9.0), loaded(10.0))), encoding="utf-8")
    history = LoadHistory(path)
    history.load()                                 # earlier runs never make this process warm
    assert history.estimate(TURBO, "cuda").about_s == 25.0
    history.append(loaded(26.0, cold=True))
    assert history.estimate(TURBO, "cuda").about_s == 9.5
    failed_first = LoadHistory(path.with_name("failed.jsonl"))
    failed_first.append(ev("load_failed", reason="CUDA out of memory"))
    failed_first.append(loaded(9.0))
    failed_first.append(loaded(30.0, cold=True))
    assert failed_first.estimate(TURBO, "cuda").about_s == 9.0   # a failed load already paid the imports


def test_failed_and_untimed_loads_never_count(path: Path) -> None:
    history = filled(path, [ev("load_failed", seconds=3.0, reason="x"), loaded(None, gb=3.0), loaded(9.0)])
    estimate = history.estimate(TURBO, "cuda", cold=False)
    assert (estimate.about_s, estimate.last_s, estimate.count) == (9.0, 9.0, 1)


def test_measured_gb_and_peak_come_from_the_latest_readings(path: Path) -> None:
    history = filled(path, [
        loaded(9.0, gb=3.1), ev("unloaded", gb=3.1, peak_gb=3.6, reason="free"),
        loaded(9.0, gb=3.2), loaded(9.0, gb=None), ev("closed", gb=3.2, peak_gb=3.7),
        ev("unloaded", gb=1.0, peak_gb=9.9, device="cpu"),
        ev("unloaded", backend=STANDARD, gb=3.5, peak_gb=4.0),
    ])
    estimate = history.estimate(TURBO, "cuda")
    assert (estimate.gb, estimate.peak_gb) == (3.2, 3.7)
    assert history.estimate(STANDARD, "cuda").peak_gb == 4.0
    assert history.estimate(STANDARD, "cuda").gb is None


def test_bake_estimate_is_the_median_of_the_last_five_forges(path: Path) -> None:
    history = filled(path, [ev("forged", MTL, seconds=s) for s in (1.9, 2.1, 2.0, 5.0, 1.8, 2.2)]
                     + [ev("forged", MTL, seconds=40.0, device="cpu"), ev("forged", TURBO, seconds=1.0)])
    assert history.bake_estimate(MTL, "cuda") == 2.1
    assert history.bake_estimate(MTL, "cpu") == 40.0
    assert history.bake_estimate(MTL) == 2.2                       # all devices: 2.0, 5.0, 1.8, 2.2, 40.0
    assert history.bake_estimate(STANDARD) is None


def test_last_loaded_is_the_backend_of_the_latest_successful_load(path: Path) -> None:
    history = LoadHistory(path)
    assert history.last_loaded() is None
    for event in (loaded(9.0), ev("switching", STANDARD, from_backend=TURBO), loaded(12.0, backend=STANDARD),
                  ev("unloaded", STANDARD, gb=3.5, reason="free"), ev("load_failed", MTL, reason="x"),
                  ev("closed", STANDARD)):
        history.append(event)
    assert history.last_loaded() == STANDARD


# ---------- line copy ----------

@pytest.mark.parametrize(("event", "text"), [
    (ev("switching", STANDARD, from_backend=TURBO), "Switching Turbo → Standard"),
    (ev("loading", MTL), "Loading chatterbox-multilingual"),
    (ev("loading", MTL, reason="warm-up"), "Loading chatterbox-multilingual · warm-up"),
    (loaded(12.1, backend=STANDARD, gb=3.5), "Loaded chatterbox-standard · 12.1 s · 3.5 GB"),
    (loaded(40.04, backend=STANDARD, device="cpu"), "Loaded chatterbox-standard · 40.0 s · CPU"),
    (loaded(8.7), "Loaded chatterbox-turbo · 8.7 s"),
    (ev("unloaded", gb=3.1, reason="switch"), "Unloaded chatterbox-turbo · freed 3.1 GB"),
    (ev("unloaded", gb=3.1, reason="idle", seconds=1800.0), "Unloaded chatterbox-turbo · idle 30 min · freed 3.1 GB"),
    (ev("unloaded", gb=3.1, reason="idle"), "Unloaded chatterbox-turbo · idle · freed 3.1 GB"),
    (ev("unloaded", device="cpu"), "Unloaded chatterbox-turbo"),
    (ev("load_failed", reason="CUDA out of memory"), "Load failed: chatterbox-turbo · CUDA out of memory"),
    (ev("closed", MTL, gb=3.4), "Closed · freed 3.4 GB"),
    (ev("forged", MTL, seconds=2.0), ""),
])
def test_event_text_follows_the_mockup_copy(event: LoadEvent, text: str) -> None:
    assert event_text(event) == text


def test_event_text_takes_the_catalog_labels() -> None:
    labels = {TURBO: "Fast", STANDARD: "Classic"}
    assert event_text(ev("switching", STANDARD, from_backend=TURBO), labels.__getitem__) == "Switching Fast → Classic"


def test_backend_label() -> None:
    assert [backend_label(b) for b in (MTL, STANDARD, TURBO)] == ["Multilingual", "Standard", "Turbo"]
    assert backend_label("chatterbox-other") == "chatterbox-other"


def local(*parts: int) -> str:
    return datetime(*parts).astimezone().isoformat(timespec="seconds")


def test_history_rows_prefix_order_and_tones() -> None:
    now = datetime(2026, 11, 13, 21, 0, 0).astimezone()
    events = [
        loaded(8.7, gb=3.1, t=local(2025, 11, 11, 19, 20, 41)),
        ev("switching", STANDARD, from_backend=TURBO, t=local(2026, 11, 11, 19, 34, 10)),
        ev("load_failed", reason="CUDA out of memory", t=local(2026, 11, 12, 8, 0, 1)),
        ev("forged", MTL, seconds=2.0, t=local(2026, 11, 13, 9, 0, 0)),
        loaded(12.1, backend=STANDARD, gb=3.5, t=local(2026, 11, 13, 19, 20, 41)),
        ev("load_failed", reason="CUDA out of memory", t=local(2026, 11, 13, 19, 21, 0)),
    ]
    assert history_rows(events, now=now) == (
        ("Nov 11 2025 19:20:41  Loaded chatterbox-turbo · 8.7 s · 3.1 GB", "old"),
        ("Nov 11 19:34:10  Switching Turbo → Standard", "old"),
        ("Nov 12 08:00:01  Load failed: chatterbox-turbo · CUDA out of memory", "failed"),
        ("Today 19:20:41  Loaded chatterbox-standard · 12.1 s · 3.5 GB", "today"),
        ("Today 19:21:00  Load failed: chatterbox-turbo · CUDA out of memory", "failed"),
    )
    assert history_rows([], now=now) == ()


def test_now_iso_is_local_time_with_an_offset() -> None:
    stamp = datetime.fromisoformat(now_iso())
    assert stamp.utcoffset() is not None
    assert abs((datetime.now().astimezone() - stamp).total_seconds()) < 5
