"""Planner (§4.6) and the backend catalog: needs, free-after, the low-VRAM margin, labels per reason."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.backends.info import BACKENDS
from ui.contract.plans import AfterGenerate, GenerateRequest
from ui.contract.records import LoadEvent
from ui.contract.state import AppState
from ui.core import catalog
from ui.core.planner import FALLBACK_NEEDS_BYTES, GIB, MARGIN_BYTES, Planner, about_text, round_half_up
from ui.core.services.load_history import LoadHistory

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


class Sizes:
    """The two facade reads the planner makes."""

    def __init__(self, weights: dict[str, int] | None = None, device: str = "cuda") -> None:
        self.weights = weights or {}
        self.device = device

    def weights_bytes(self, backend: str) -> int | None:
        return self.weights.get(backend)

    def preferred_device(self) -> str:
        return self.device


def build(tmp_path: Path, *, events: list[LoadEvent] = (), weights: dict[str, int] | None = None,
          device: str = "cuda", resident: str = MTL, free_gb: float | None = 6.1, app_gb: float = 3.4,
          cuda: bool | None = True) -> tuple[Planner, AppState]:
    history = LoadHistory(tmp_path / "load_history.jsonl")
    for event in events:
        history.append(event)
    state = AppState()
    state.engine.resident = resident
    state.device.cuda = cuda
    state.device.detected = cuda is not None
    state.device.free_bytes = None if free_gb is None else round(free_gb * GIB)
    state.device.app_bytes = round(app_gb * GIB) if resident else 0
    return Planner(state, history, Sizes(weights, device)), state


def loaded(backend: str, seconds: float, gb: float | None, device: str = "cuda") -> LoadEvent:
    return LoadEvent(t="2026-11-12T19:20:41+09:00", kind="loaded", backend=backend, seconds=seconds, gb=gb,
                     device=device)


def unloaded(backend: str, gb: float, peak_gb: float, device: str = "cuda") -> LoadEvent:
    return LoadEvent(t="2026-11-12T19:34:11+09:00", kind="unloaded", backend=backend, gb=gb, peak_gb=peak_gb,
                     reason="switch", device=device)


# ---------- needs ----------

def test_needs_is_the_larger_of_the_measured_reserve_and_peak(tmp_path: Path) -> None:
    planner, _ = build(tmp_path, events=[loaded(TUR, 8.7, 3.1), unloaded(TUR, 3.1, 3.3)])
    assert planner.needs_bytes(TUR) == (round(3.3 * GIB), False)
    planner, _ = build(tmp_path / "b", events=[loaded(TUR, 8.7, 3.1)])
    assert planner.needs_bytes(TUR) == (round(3.1 * GIB), False)


def test_needs_falls_back_to_the_weights_then_4_gib(tmp_path: Path) -> None:
    planner, _ = build(tmp_path, weights={TUR: 2_986_050_872})
    assert planner.needs_bytes(TUR) == (2_986_050_872, True)
    assert planner.needs_bytes(STD) == (FALLBACK_NEEDS_BYTES, True)
    assert FALLBACK_NEEDS_BYTES == 4 * GIB


def test_needs_reads_only_this_device(tmp_path: Path) -> None:
    planner, _ = build(tmp_path, events=[loaded(TUR, 40.0, None, device="cpu")], weights={TUR: 2_986_050_872})
    assert planner.needs_bytes(TUR) == (2_986_050_872, True)


# ---------- free after the unload ----------

def test_free_after_adds_back_the_resident_model(tmp_path: Path) -> None:
    planner, _ = build(tmp_path, free_gb=6.1, app_gb=3.4)
    assert planner.free_after_bytes() == round(6.1 * GIB) + round(3.4 * GIB)
    planner, _ = build(tmp_path / "b", resident="", free_gb=9.5)
    assert planner.free_after_bytes() == round(9.5 * GIB)
    planner, _ = build(tmp_path / "c", free_gb=None)
    assert planner.free_after_bytes() is None


# ---------- the low-VRAM rule ----------

def test_low_vram_at_the_half_gib_margin(tmp_path: Path) -> None:
    needs = round(3.3 * GIB)
    planner, state = build(tmp_path, events=[unloaded(TUR, 3.1, 3.3)], resident="", free_gb=0)
    state.device.free_bytes = needs + MARGIN_BYTES - 1
    assert planner.low_vram(TUR) and planner.switch_plan(TUR, reason="explicit").low_vram
    state.device.free_bytes = needs + MARGIN_BYTES
    assert not planner.low_vram(TUR) and not planner.switch_plan(TUR, reason="explicit").low_vram
    assert MARGIN_BYTES == GIB // 2


def test_low_vram_counts_the_model_that_is_unloaded_first(tmp_path: Path) -> None:
    planner, state = build(tmp_path, events=[unloaded(TUR, 3.1, 3.3)], free_gb=0.6, app_gb=3.4)
    assert not planner.low_vram(TUR)                         # 0.6 + 3.4 = 4.0 GB after the unload ≥ 3.3 + 0.5
    state.device.app_bytes = round(3.0 * GIB)                # 3.6 GB < 3.8 GB
    assert planner.low_vram(TUR)
    state.device.app_bytes = round(3.4 * GIB)
    state.engine.resident = ""                               # nothing to unload: only the 0.6 GB free counts
    assert planner.low_vram(TUR)


@pytest.mark.parametrize(("device", "cuda", "free_gb"), [("cpu", True, 0.0), ("cuda", False, 0.0),
                                                         ("cuda", None, None), ("cuda", True, None)])
def test_cpu_and_unknown_free_vram_are_never_low(tmp_path: Path, device: str, cuda: bool | None,
                                                 free_gb: float | None) -> None:
    planner, _ = build(tmp_path, device=device, cuda=cuda, free_gb=free_gb, resident="")
    assert not planner.low_vram(TUR)
    assert not planner.switch_plan(TUR, reason="generate").low_vram


def test_the_next_load_runs_on_the_cpu_without_cuda(tmp_path: Path) -> None:
    assert build(tmp_path, cuda=False)[0].device() == "cpu"
    assert build(tmp_path / "b", cuda=None)[0].device() == "cuda"
    assert build(tmp_path / "c", device="cpu")[0].device() == "cpu"


# ---------- plans and labels ----------

def test_a_plan_from_the_history(tmp_path: Path) -> None:
    events = [loaded(TUR, 8.4, 3.1), loaded(TUR, 8.7, 3.1), loaded(TUR, 9.1, 3.1), unloaded(TUR, 3.1, 3.3)]
    planner, _ = build(tmp_path, events=events)
    req = GenerateRequest("host-turbo", TUR, "Okay.", "en", None, None)
    plan = planner.switch_plan(TUR, reason="generate", after=AfterGenerate((req,)))
    assert (plan.to_backend, plan.from_backend, plan.reason) == (TUR, MTL, "generate")
    assert (plan.about_s, plan.last_s) == (8.7, 9.1)
    assert (plan.needs_bytes, plan.needs_estimated) == (round(3.3 * GIB), False)
    assert plan.free_after_bytes == round(6.1 * GIB) + round(3.4 * GIB) and not plan.low_vram
    assert plan.after == AfterGenerate((req,))


def test_the_load_estimate_is_for_the_next_device(tmp_path: Path) -> None:
    events = [loaded(TUR, 8.7, 3.1), loaded(TUR, 41.0, None, device="cpu")]
    planner, state = build(tmp_path, events=events)
    assert (planner.load_estimate(TUR).about_s, planner.load_estimate(TUR).count) == (8.7, 1)
    state.device.cuda = False
    assert planner.load_estimate(TUR).about_s == 41.0
    assert planner.load_estimate(STD).about_s is None and planner.load_estimate(STD).count == 0


def test_nothing_resident_has_no_from_backend(tmp_path: Path) -> None:
    plan = build(tmp_path, resident="")[0].switch_plan(MTL, reason="explicit")
    assert plan.from_backend is None and plan.about_s is None and plan.last_s is None


LABELS = [
    ("generate", MTL, "Switch to Turbo & Generate"),
    ("generate", "", "Load Turbo & Generate · about 9 s"),
    ("forge", MTL, "Switch to Turbo & Forge"),
    ("forge", "", "Load Turbo & Forge"),
    ("explicit", MTL, "Switch to This Model"),
    ("explicit", "", "Load This Model"),
    ("retry", "", "Retry"),
    ("warmup", "", "Load Turbo"),
]


@pytest.mark.parametrize(("reason", "resident", "label"), LABELS)
def test_labels_per_reason(tmp_path: Path, reason: str, resident: str, label: str) -> None:
    planner, _ = build(tmp_path, events=[loaded(TUR, 8.7, 3.1)], resident=resident)
    assert planner.switch_plan(TUR, reason=reason).label == label  # type: ignore[arg-type]


def test_without_history_the_generate_label_has_no_estimate(tmp_path: Path) -> None:
    planner, _ = build(tmp_path, resident="")
    assert planner.switch_plan(MTL, reason="generate").label == "Load Multilingual & Generate"
    with_history, _ = build(tmp_path / "b", events=[loaded(MTL, 14.2, 3.4)], resident="")
    assert with_history.switch_plan(MTL, reason="generate").label == "Load Multilingual & Generate · about 14 s"


def test_estimates_round_half_up() -> None:
    assert [round_half_up(x) for x in (8.4, 8.5, 8.7, 14.2, 0.4)] == [8, 9, 9, 14, 0]
    assert about_text(8.7) == " · about 9 s" and about_text(None) == "" and about_text(0.0) == ""


# ---------- the catalog (M L1192-1212) ----------

def test_the_catalog_copy_is_the_mockup_copy() -> None:
    assert catalog.ORDER == (MTL, STD, TUR) == tuple(BACKENDS)
    assert [catalog.label(name) for name in catalog.ORDER] == ["Multilingual", "Standard", "Turbo"]
    assert catalog.tip(MTL) == "Multilingual · 23 languages · Expressiveness and Guidance"
    assert catalog.tip(STD) == "Standard · English only · often the most natural English"
    assert catalog.tip(TUR) == ("Turbo · English only · fastest · tags · ignores Expressiveness and Guidance · "
                                "needs a clip over 5 s")
    assert catalog.rules(MTL) == "23 languages · Expressiveness and Guidance"
    assert catalog.rules(STD) == "English only · Expressiveness and Guidance"
    assert catalog.rules(TUR) == ("English only · ignores Expressiveness and Guidance · understands [laugh] [chuckle] "
                                  "[cough] [sigh] [gasp]")
    assert [(p.name, p.exaggeration, p.cfg_weight) for p in catalog.PRESETS] == [
        ("Default", 0.5, 0.5), ("Slower", 0.5, 0.3), ("Dramatic", 0.7, 0.3)]
    assert catalog.TAGS == ("[laugh]", "[chuckle]", "[cough]", "[sigh]", "[gasp]")


def test_an_unknown_backend_is_shown_as_itself() -> None:
    assert catalog.label("chatterbox-future") == "chatterbox-future"
    assert catalog.tip("chatterbox-future") == "" and catalog.rules("chatterbox-future") == ""
    assert catalog.label("") == ""
