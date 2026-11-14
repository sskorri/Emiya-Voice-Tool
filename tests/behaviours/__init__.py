"""Behaviour tests (§8.4): pure Python against a real Core on FakeEngine, the manual clock and a fake KitPort.

No NiceGUI client is involved. The helpers here are shared by U6's tests and the pages' behaviour tests:

- FakeKit: a KitPort that records every call and returns scripted paths.
- started(rigs, scenario): a started rig (core.start(), the first client and its device reading) with the
  scenario's Speak draft copied into core.session, as the composition root does.
- factory_for(rig): a BehaviourFactory on that core (client None).
- StubSpeak: the Speak controller's generate and pick_voice as P1 builds them, for cross-page tests.
- tick(): lets the coalesced core.events.changed emit (and what it triggers) run.
- Import the fixtures into a test module: `from tests.behaviours import no_error_logs, offline`, plus
  `rigs` from tests.core.test_facade.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pytest

from ui.behaviours import BehaviourFactory
from ui.contract.plans import GenerateRequest, Toast
from ui.contract.viewmodels import SpeakVM
from ui.core.jobs import StartResult

from tests.core.test_facade import Rig

MTL, STD, TUR = "chatterbox-multilingual", "chatterbox-standard", "chatterbox-turbo"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture(autouse=True)
def no_error_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Like the NiceGUI user plugin: a test fails when anything logged an ERROR."""
    caplog.set_level(logging.WARNING)
    yield
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == []


@dataclass
class FakeKit:
    """A KitPort: records calls, returns the scripted paths, raises the scripted errors."""

    file_result: Path | None = None
    folder_result: Path | None = None
    save_result: Path | None = None
    minimize_result: bool = True
    reveal_error: OSError | None = None
    open_error: OSError | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def pick_file(self, *, title: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        self.calls.append(("pick_file", {"title": title, "filters": filters, "start_dir": start_dir}))
        return self.file_result

    async def pick_folder(self, *, title: str, start_dir: Path | None) -> Path | None:
        self.calls.append(("pick_folder", {"title": title, "start_dir": start_dir}))
        return self.folder_result

    async def save_as(self, *, default_name: str, filters: tuple[str, ...], start_dir: Path | None) -> Path | None:
        self.calls.append(("save_as", {"default_name": default_name, "filters": filters, "start_dir": start_dir}))
        return self.save_result

    def reveal(self, path: Path) -> None:
        self.calls.append(("reveal", {"path": path}))
        if self.reveal_error is not None:
            raise self.reveal_error

    def open_folder(self, path: Path) -> None:
        self.calls.append(("open_folder", {"path": path}))
        if self.open_error is not None:
            raise self.open_error

    def quit(self) -> None:
        self.calls.append(("quit", {}))

    def minimize(self) -> bool:
        self.calls.append(("minimize", {}))
        return self.minimize_result

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for call, kwargs in self.calls if call == name]


def apply_session(rig: Rig) -> None:
    """Copy the scenario's Speak draft into core.session (the composition root's job, U7)."""
    seed = rig.fake.scenario.session
    if seed is None:
        return
    session = rig.core.session
    for item in fields(seed):
        setattr(session, item.name, getattr(seed, item.name))


async def started(rigs: Callable[..., Rig], scenario: Any = "speak", *, session: bool = True,
                  **options: Any) -> Rig:
    """A started rig (first client connected, first device reading done), optionally with its Speak draft."""
    rig = rigs(scenario, **options)
    await rig.start()
    if session:
        apply_session(rig)
    await tick()
    return rig


def factory_for(rig: Rig, kit: FakeKit | None = None, *, browser: bool = False) -> tuple[BehaviourFactory, FakeKit]:
    kit = kit if kit is not None else FakeKit()
    return BehaviourFactory(rig.core, None, kit, browser=browser), kit


def watch_toasts(rig: Rig) -> list[Toast]:
    """Every toast the core raises from now on."""
    seen: list[Toast] = []
    rig.core.events.toast.subscribe(seen.append)
    return seen


def texts(toasts: list[Toast]) -> list[tuple[str, bool]]:
    return [(toast.text, toast.bad) for toast in toasts]


async def tick(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def advance(rig: Rig, seconds: float) -> None:
    """Move the manual clock and let the ticks' changes reach the controllers."""
    rig.clock.advance(seconds)
    await tick()


class StubSpeak:
    """The Speak controller's generate and pick_voice as P1 builds them (snapshot the Session, then the jobs)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._core = factory.core
        self.vm = SpeakVM()
        self.picked: list[str] = []
        self.lists: list[bool | None] = []
        self.generated: list[tuple[int, str | None, StartResult]] = []

    def pick_voice(self, voice_id: str) -> None:
        self.picked.append(voice_id)
        self._core.session.voice_id = voice_id

    def toggle_language_list(self, open: bool | None = None) -> None:
        self.lists.append(open)

    async def generate(self, n: int = 1, token: str | None = None) -> None:
        session = self._core.session
        row = next(r for r in self._core.state.library.rows if r.voice_id == session.voice_id)
        turbo = row.backend == TUR
        req = GenerateRequest(voice_id=row.voice_id, backend=row.backend, text=session.text,
                              language="en" if turbo else session.language,
                              exaggeration=None if turbo else session.exaggeration,
                              cfg_weight=None if turbo else session.cfg_weight)
        self.generated.append((n, token, self._core.jobs.start_generate([req] * n, token=token)))
