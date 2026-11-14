"""Reliability helpers (R1): the monkey's action catalogue, a quiet settle and the invariants it checks.

test_rel_monkey.py and test_rel_soak.py drive the whole app with these, under NiceGUI's user fixture on a FakeEngine
scenario's scratch data and the manual clock (tests\\pages\\conftest.py). Nothing opens a window or loads a model.

- quiesce(): waits until every click task, navigation, job step, timer task and settings write has finished (a job
  parked at a FakeEngine hold excepted). Work still running after FREEZE_S is a freeze.
- Monkey: finds what a user could do on the page right now (every shown, enabled control with a click listener,
  clicked once or twice in a row, the rail, text boxes, sliders, key slots, the rail search, the key commands, the
  player's reports, Back and reload, a native file drop, minimize and restore) plus time passing, releasing the
  FakeEngine holds, engine and disk faults and other programs' changes, and does one of them, chosen by a seeded
  random.Random. Scripted flows (the long session) use the same act() door, so every action is checked alike.
- Invariants: spies installed before the first action; check() after every action lists the violations: an
  exception, an ERROR log or a warning that swallowed one, two models resident, a load without a named control,
  a confirmed ask or the user's warm-up, an unload without Free VRAM, Unload or the idle setting, more than 3
  takes queued, a stuck job, a page.error marker, a polled binding, an engine call on the event-loop thread,
  Python error text in a toast or the Log, and the mirrors out of step with the engine once everything settled.
  Monkey.finish() then compares the takes index, ui_settings.json, the load history and the library with memory.
- Trace: every action as one line; a failure message carries the seed, the scenario, the start route and the
  trace, so `pytest -k <id>` replays it.

The theme-specific markers (the Emiya rail search, key dispatchers and player) sit in EMIYA; every other control
is found through its event listeners, so new controls join the monkey without a change here.
"""
from __future__ import annotations

import asyncio
import collections
import faulthandler
import inspect
import logging
import math
import os
import random
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from nicegui import background_tasks, binding, ui
from nicegui.element import Element
from nicegui.elements.mixins.disableable_element import DisableableElement
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from engine.errors import BackendLoadError, EngineBusy, GenerationError
from ui.behaviours.confirm import ARM_PAUSE_S, ARM_WINDOW_S
from ui.contract.routes import ROUTES, Route
from ui.core import Core, catalog
from ui.core.clock import ManualScheduler
from ui.core.fake import FakeEngine
from ui.core.jobs import MAX_QUEUE

__all__ = [
    "EMIYA", "FREEZE_S", "MONKEY_WEIGHTS", "SCRIPTS", "Act", "Frozen", "Invariants", "Monkey", "Trace", "clock_kind",
    "data_files", "disarm_faults", "element_counts", "fault_catalogue", "is_enabled", "key_of", "leftovers",
    "library_matches", "open_monkey", "page_wrapper", "quiesce", "scan", "stored_state", "summary",
    "takes_consistency",
]

FREEZE_S = 10.0                  # an action whose work is still running after this long froze the app
WATCHDOG_S = 6 * FREEZE_S        # faulthandler dumps every stack if an action blocks the loop itself this long
POLL_S = 0.025                   # real-time polls stay above Windows' 15.6 ms monotonic tick (see _load_begun)
TRACE_TAIL = 60                  # trace lines in a failure message (the whole trace goes to stdout)
SPY_KEEP = 512                   # minted tokens and tickets the load spy remembers

# The Emiya theme's own markers the monkey needs to drive the browser halves (no JS runs in the user fixture).
EMIYA: Mapping[str, str] = MappingProxyType({
    "search": "shell.search", "result": "shell.search.result", "keys": "shell.keys",
    "escape": "shell.keys.escape", "player": "shell.player", "ask": "confirm.ask",
    "primary": "confirm.ask.primary",
})
ERROR_MARKERS = ("page.error", "shell.fallback")

# NiceGUI's own app-lifetime tasks (one set per test): never waited for.
LONG_LIVED = ("outbox loop", "refresh bindings", "<bound method Client.prune_instances",
              "<bound method Slot.prune_stacks", "<function prune_tab_storage")

# Engine calls that block (the RLock, a model, the disk or a CUDA probe): never on the event-loop thread.
ENGINE_WORK = frozenset({"make_resident", "load_backend", "unload_backend", "close", "speak", "load_voice",
                         "update_voice", "delete_voice", "voice_details", "device_info", "weights_status"})

# Text that only an exception's message has: it must never reach a toast, the Log or a warning.
PY_ERROR = re.compile(r"Traceback|NoneType|object has no attribute|unexpected keyword|not subscriptable|"
                      r"is not iterable|positional argument|index out of range|referenced before assignment|"
                      r"unhashable type|division by zero|object is not callable|invalid literal for|"
                      r"maximum recursion|would leave \d+ models resident")
# Warnings the app writes when it swallowed an exception or lost a write: each one is a bug here.
SWALLOWED = re.compile(r"^(A timer callback failed|A background task failed|An engine event listener failed|"
                       r"A behaviour listener failed|A job ended unexpectedly|An idle hook failed|"
                       r"The test line callback failed|Closing the .* controller failed|Startup failed|"
                       r"Couldn't write the load history|Couldn't save |Couldn't record the close|"
                       r"Client has been deleted but is still being used)")

MONKEY_WEIGHTS: Mapping[str, float] = MappingProxyType({
    "click": 42.0, "burst": 3.0, "nav": 9.0, "type": 9.0, "search": 6.0, "key": 5.0, "trigger": 5.0,
    "slide": 4.0, "tick": 7.0, "player": 2.0, "release": 3.0, "back": 1.0, "reload": 1.0, "drop": 1.5,
    "fault": 1.5, "window": 1.0, "external": 1.0,
})
REGION_WEIGHTS: Mapping[str, float] = MappingProxyType({"ask": 6.0, "page": 3.0, "shell": 1.0})
TICKS: tuple[tuple[float, float], ...] = ((0.2, 3.0), (1.0, 3.0), (3.1, 3.0), (15.0, 2.0), (60.0, 1.0),
                                          (600.0, 1.0), (1800.0, 0.3))
HOLD_LIMIT = 40                  # actions after which the monkey releases the holds itself (HOLD_TIMEOUT_S is 30 s)

SCRIPTS = ("Hello there. How are you?", "こんにちは。テストです。", "Okay, okay [chuckle] it works.",
           "Buongiorno! Che bella giornata.", "One. Two. Three. Four. Five.", "Hi", "", "   ", "x" * 5000,
           "おはよう！今日はいい天気ですね。")
NAMES = ("rin", "aiko", "Aiko", "host-turbo", "narrator", "bad name!", "", "a" * 70, "con", "x-1_2", "rin2",
         "sample", "friend")
QUERIES = ("aiko", "marco", "host", "Take", "おはよう", "Forge", "Free", "zzz", "a", "", "(.*[", "7", "rin")
NASTY = ("(.*[", "a" * 5000, "😀 émoji ✓", "مرحبا بالعالم", "\x00\x07\t\n", "con", "LPT9", "../../etc/passwd",
         "日本語のテキストです。", "  ", "<script>alert(1)</script>", "Ctrl+Enter", "%s %d {0}", "\\\\server\\share")
CAPTURES: tuple[dict[str, Any], ...] = (
    {"key": "k", "ctrl": True}, {"key": "Enter", "ctrl": True}, {"key": "Enter", "ctrl": True, "shift": True},
    {"key": "F5"}, {"key": "Escape"}, {"key": "Backspace"}, {"key": "Delete"}, {"key": "Shift", "shift": True},
    {"key": "=", "ctrl": True}, {"key": "F4", "alt": True}, {"key": "Tab"}, {"key": "ArrowUp", "alt": True},
    {"key": " "}, {"key": "p", "ctrl": True, "composing": True}, {"key": "g", "ctrl": True}, {"key": "F9"},
)
TRIGGERS = ("keydown.esc", "keydown.enter", "blur", "seek", "mousedown")


# ---- small helpers -----------------------------------------------------------------------------------

class Frozen(AssertionError):
    """Work that had not finished FREEZE_S after an action."""


def _long_lived(task: asyncio.Task[Any]) -> bool:
    return task.get_name().startswith(LONG_LIVED)


def handler_types(element: Element) -> set[str]:
    """The event types this element handles in Python (a JS-only listener does nothing on the server)."""
    return {listener.type for listener in element._event_listeners.values() if listener.handler is not None}


def is_enabled(element: Element) -> bool:
    if isinstance(element, DisableableElement) and not element.enabled:
        return False
    return not element.props.get("disabled")


def texts_of(element: Element) -> list[str]:
    return [str(item) for item in element._displayed_contents(only_visible=False) if item]


def names_of(element: Element) -> str:
    """Everything that names a control: its label, aria-label, title and the text of its descendants."""
    parts = [str(getattr(element, "label", "") or ""), str(element.props.get("aria-label", "") or ""),
             str(element.props.get("title", "") or ""), *texts_of(element)]
    for child in element.descendants():
        parts += texts_of(child)
    return " ".join(part for part in parts if part)


def key_of(element: Element) -> str:
    """A stable name for an element: its markers, else tag, classes and text."""
    if element._markers:
        return " ".join(element._markers)
    text = " ".join(texts_of(element))[:30]
    return f"{element.tag}.{'.'.join(sorted(element.classes))}:{text}"


def short(backend: str) -> str:
    return catalog.label(backend) if backend else "-"


def clip_text(text: Any, limit: int = 60) -> str:
    value = repr(text) if isinstance(text, str) else str(text)
    return value if len(value) <= limit else value[:limit - 1] + "…"


@dataclass(frozen=True)
class Seen:
    """A shown element of the page and where it sits: the page, the shell or the switch popover."""

    element: Element
    region: str


def scan(root: Element) -> list[Seen]:
    """Every shown element under root (itself and every ancestor visible), in document order."""
    found: list[Seen] = []
    stack: list[tuple[Element, str]] = [(root, "shell")]
    while stack:
        element, region = stack.pop()
        if not element.visible:
            continue
        markers = element._markers
        if EMIYA["ask"] in markers:
            region = "ask"
        elif region == "shell" and any(marker.startswith("page.") for marker in markers):
            region = "page"
        found.append(Seen(element, region))
        children = [child for slot in element.slots.values() for child in slot.children]
        stack.extend((child, region) for child in reversed(children))
    return found


def page_wrapper(user: User) -> Element | None:
    """The page.<id> wrapper of the page shown now (the router keeps one)."""
    for element in user.client.elements.values():
        if any(marker.startswith("page.") for marker in element._markers):
            return element
    return None


def element_counts(user: User) -> tuple[int, int]:
    """(elements in the client, elements in the shown page's wrapper)."""
    page = page_wrapper(user)
    return len(user.client.elements), (sum(1 for _ in page.descendants()) if page is not None else 0)


async def quiesce(core: Core, fake: FakeEngine, *, timeout: float = FREEZE_S, skip_job: bool = False) -> None:
    """Wait until the app is quiet: click tasks, navigation, job steps, timer tasks and the settings write done.

    A job parked at a FakeEngine hold is left alone (it waits for release_all()); a job on its way to an armed
    hold is waited for until it gets there; skip_job leaves the running job alone too (a FakeEngine with
    speed > 0 sleeps for real). Frozen when anything is still running after `timeout` seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    me = asyncio.current_task()
    while True:
        for _ in range(3):
            await asyncio.sleep(0)
        waiting = [hold for hold in fake.holds if not hold.is_set()]
        job = core.jobs.task
        parked = job is not None and any(hold.entered.is_set() for hold in waiting)
        busy = {task for task in background_tasks.running_tasks if not task.done() and not _long_lived(task)}
        busy |= {task for task in core.scheduler._tasks if not task.done()}
        settings_task = core.settings._task
        if settings_task is not None and not settings_task.done():
            busy.add(settings_task)
        if parked or skip_job:
            busy.discard(job)
        busy.discard(me)
        if not busy:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            names = ", ".join(sorted(task.get_name() for task in busy))
            raise Frozen(f"still running {timeout:.0f} s after the action: {names}")
        step = min(remaining, 0.02) if waiting else remaining
        await asyncio.wait(busy, timeout=step, return_when=asyncio.FIRST_COMPLETED)


def data_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def leftovers(root: Path) -> list[str]:
    """Temp files a write left behind: *.part, *.part-*, *.tmp and *.tmp-* anywhere in the data root."""
    found = []
    for path in data_files(root):
        name = path.name.lower()
        if name.endswith((".part", ".tmp")) or ".part-" in name or ".tmp-" in name:
            found.append(str(path.relative_to(root)))
    return sorted(found)


def takes_consistency(core: Core, *, deleted: Iterable[Path] = ()) -> list[str]:
    """The takes index on disk, the store in memory and the WAVs agree (an empty list means they do).

    deleted: WAVs removed behind the app's back (Explorer); the app finds those only when it touches them.
    """
    import json

    gone = {os.path.normcase(str(path)) for path in deleted}
    problems: list[str] = []
    store = core.takes
    index = core.paths.takes_index
    listed = {take.id: take for take in store.takes}
    try:
        data = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {"takes": []}
    except (OSError, ValueError) as exc:
        return [f"index.json unreadable: {exc}"]
    on_disk = {int(entry["id"]) for entry in data.get("takes", [])}
    if on_disk != set(listed):
        problems.append(f"index ids {sorted(on_disk ^ set(listed))} differ between disk and memory")
    if isinstance(data.get("next_id"), int) and data["next_id"] != store.next_id:
        problems.append(f"next_id {data['next_id']} on disk, {store.next_id} in memory")
    if core.state.takes.takes != store.takes:
        problems.append("TakesState is not the store's tuple")
    folders = set()
    for take in store.takes:
        path = store.path_of(take.id)
        if path is None or not path.is_file():
            if path is None or os.path.normcase(str(path)) not in gone:
                problems.append(f"take {take.id} is listed but {path} is missing")
        elif path.parent:
            folders.add(path.parent)
        if take.id >= store.next_id:
            problems.append(f"take {take.id} is not below next_id {store.next_id}")
    folders.add(core.paths.takes_default)
    wav = re.compile(r".*_take(\d+)(?:-\d+)?\.wav$", re.IGNORECASE)
    listed_paths = {os.path.normcase(str(store.path_of(take.id))) for take in store.takes}
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if wav.fullmatch(path.name) and os.path.normcase(str(path)) not in listed_paths:
                problems.append(f"{path.name} is on disk but not listed")
    return problems


def stored_state(core: Core) -> list[str]:
    """The settings file and the load history file hold what memory holds (call once the app is quiet)."""
    import json

    from ui.core.services.load_history import _parse_lines

    problems: list[str] = []
    core.settings.flush()
    stored = core.settings.read_back()                     # the core section and the active theme's own
    for section, values in stored.items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            memory = core.settings.get(section, key)
            if json.dumps(value, sort_keys=True, default=list) != json.dumps(memory, sort_keys=True, default=list):
                problems.append(f"ui_settings.json has {section}.{key}={value!r}, memory {memory!r}")
    path = core.paths.history
    events, skipped = _parse_lines(path.read_bytes()) if path.exists() else ([], 0)
    if skipped:
        problems.append(f"load_history.jsonl has {skipped} unreadable lines")
    memory_events = core.history.events
    if memory_events and tuple(events[-len(memory_events):]) != memory_events:
        problems.append("the load history in memory is not the tail of load_history.jsonl")
    return problems


async def library_matches(core: Core, fake: FakeEngine) -> list[str]:
    """After a refresh the library mirror lists exactly the registry's voices."""
    await core.refresh_library()
    mirror = {row.voice_id for row in core.state.library.rows}
    registry = set(fake.registry.list_ids())
    return [] if mirror == registry else [f"library {sorted(mirror ^ registry)} differs from registry.json"]


# ---- the trace -----------------------------------------------------------------------------------------

class Trace:
    """Every action as one line, for the failure message and an optional REL_TRACE_DIR file.

    keep bounds the lines held (the long session keeps the last ones, so the trace never looks like a leak).
    """

    def __init__(self, header: str, *, keep: int | None = None) -> None:
        self.header = header
        self.lines: collections.deque[str] = collections.deque(maxlen=keep)

    def add(self, line: str) -> None:
        self.lines.append(line)

    def tail(self, count: int = TRACE_TAIL) -> str:
        return "\n".join(list(self.lines)[-count:])

    def dump(self) -> str:
        return "\n".join([self.header, *self.lines])

    def save(self, name: str) -> None:
        folder = os.environ.get("REL_TRACE_DIR")
        if folder:
            path = Path(folder) / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.dump(), encoding="utf-8")


@dataclass
class Act:
    """One action: what it was, what named it beforehand, when (manual clock) and what it did."""

    index: int
    kind: str
    key: str
    detail: str = ""
    names: str = ""              # the control's accessible name and text before the act (the named rule)
    at: float = 0.0
    route: str = ""
    replaces_voice: bool = False
    effects: list[str] = field(default_factory=list)


# ---- the invariants --------------------------------------------------------------------------------------

class LogWatch(logging.Handler):
    """WARNING and above from every logger, tagged with the action that was running."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.action = -1
        self.records: list[tuple[int, str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:                          # a bad format string is still a record to report
            message = str(record.msg)
        self.records.append((self.action, record.levelname, record.name, message))


@dataclass(frozen=True)
class Door:
    """The ticket a load came through: its plan's backend and reason, whether it rode on an ask, and when."""

    serial: int
    backend: str
    reason: str
    asked: bool | None
    redeemed_at: int


@dataclass(frozen=True)
class Made:
    """One make_resident call (a load or an already-resident no-op) and the ticket behind it."""

    action: int
    backend: str
    door: Door | None


class Invariants:
    """Spies and the checks run after every action (see the module docstring)."""

    def __init__(self, user: User, core: Core, fake: FakeEngine, monkeypatch: pytest.MonkeyPatch, *,
                 warmup: bool = False) -> None:
        self.user, self.core, self.fake = user, core, fake
        self.warmup = warmup                         # Warm Up on Launch was on at startup: its load is the user's
        self.action = -1
        self.loop_thread = threading.get_ident()
        self.watch = LogWatch()
        self.toasts: list[tuple[int, str]] = []
        self.log_lines: list[tuple[int, str]] = []
        self.faults: list[str] = []
        self.deleted: list[Path] = []                # take WAVs removed behind the app's back (Monkey._external)
        self.thread_errors: list[str] = []
        self.made: list[Made] = []
        self.loads: list[tuple[int, str, str]] = []  # (action, backend, why it was allowed)
        self.counts: collections.Counter[str] = collections.Counter()
        self._minted: collections.OrderedDict[str, tuple[bool, int]] = collections.OrderedDict()
        self._tickets: collections.OrderedDict[int, Door] = collections.OrderedDict()
        self._doors: collections.deque[Door | None] = collections.deque()
        self._mx = threading.Lock()
        self._arms: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=16)
        self._seen = {"log": 0, "toast": 0, "line": 0, "made": 0, "calls": 0, "thread": 0}
        self._loads_seen = 0
        self._spy(monkeypatch)

    # ---------- setup ----------

    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        core, fake = self.core, self.fake
        logging.getLogger().addHandler(self.watch)
        core.events.toast.subscribe(self._on_toast)
        add_line = core.log.add

        def log_add(text: str) -> str:
            line = add_line(text)
            self.log_lines.append((self.action, line))
            return line

        monkeypatch.setattr(core.log, "add", log_add)
        monkeypatch.setattr(threading, "excepthook", self._thread_hook)
        monkeypatch.setattr(sys, "unraisablehook", self._unraisable_hook)
        tokens = core.tokens
        mint, redeem = tokens.mint, tokens.redeem

        def spy_mint(plan: Any, *, asked: bool = False) -> str:
            token = mint(plan, asked=asked)
            self._minted[token] = (bool(asked), self.action)
            while len(self._minted) > SPY_KEEP:
                self._minted.popitem(last=False)
            return token

        def spy_redeem(token: str, *, expect_to: str | None = None) -> Any:
            ticket = redeem(token, expect_to=expect_to)
            if ticket is not None:
                asked = self._minted.get(token, (None, -1))[0]
                self._tickets[ticket.serial] = Door(ticket.serial, ticket.plan.to_backend, ticket.plan.reason,
                                                    asked, self.action)
                while len(self._tickets) > SPY_KEEP:
                    self._tickets.popitem(last=False)
            return ticket

        monkeypatch.setattr(tokens, "mint", spy_mint)
        monkeypatch.setattr(tokens, "redeem", spy_redeem)
        load = core.facade.load

        async def spy_load(ticket: Any) -> Any:
            door = self._tickets.get(getattr(ticket, "serial", -1))
            with self._mx:
                self._doors.append(door)
            try:
                return await load(ticket)
            finally:
                with self._mx:
                    if door in self._doors:          # refused before the engine call: the door closes unused
                        self._doors.remove(door)

        monkeypatch.setattr(core.facade, "load", spy_load)
        make = fake.make_resident

        def spy_make(name: str) -> Any:
            with self._mx:
                door = self._doors.popleft() if self._doors else None
            self.made.append(Made(self.action, name, door))
            return make(name)

        monkeypatch.setattr(fake, "make_resident", spy_make)

    def close(self) -> None:
        logging.getLogger().removeHandler(self.watch)
        self.core.events.toast.unsubscribe(self._on_toast)

    def _on_toast(self, toast: Any) -> None:
        self.toasts.append((self.action, f"{'!' if toast.bad else ''}{toast.text}"))

    def _thread_hook(self, args: Any) -> None:
        self.thread_errors.append(f"{getattr(args.thread, 'name', '?')}: {args.exc_type.__name__}: {args.exc_value}")

    def _unraisable_hook(self, args: Any) -> None:
        self.thread_errors.append(f"unraisable {type(args.exc_value).__name__}: {args.exc_value} "
                                  f"in {args.object!r}"[:300])

    def baseline(self) -> None:
        """Everything that happened so far (the scenario's own replay) is not the monkey's doing."""
        self._loads_seen = len(self.fake.loads)
        self._seen.update(log=len(self.watch.records), toast=len(self.toasts), line=len(self.log_lines),
                          made=len(self.made), calls=len(self.fake.calls), thread=len(self.thread_errors))

    def trim(self) -> None:
        """Forget what was already checked: the FakeEngine's call records and the spies' own lists.

        Call it only between actions, once they were checked (a long session would keep tens of thousands).
        """
        self.fake.reset_records()
        self._loads_seen = 0
        self._seen["calls"] = 0
        for name, items in (("log", self.watch.records), ("toast", self.toasts), ("line", self.log_lines),
                            ("made", self.made), ("thread", self.thread_errors)):
            del items[:self._seen[name]]
            self._seen[name] = 0
        self._minted.clear()                         # a ticket is redeemed and spent within one action
        self._tickets.clear()

    # ---------- per action ----------

    def begin(self, act: Act) -> None:
        self.action = act.index
        self.watch.action = act.index
        if "forge.go" in act.key.split():
            draft = self.core.forge_draft
            act.replaces_voice = any(row.voice_id == draft.name and row.backend == draft.backend
                                     for row in self.core.state.library.rows)

    def note_click(self, act: Act) -> None:
        """Remember a click's names: an arm's second press may load what the first press named (Forge row 13)."""
        self._arms.append((act.key, act.names, act.at))

    def check(self, act: Act, *, quiet: bool) -> list[str]:
        """The violations after act; quiet means quiesce() finished (the mirrors must match the engine)."""
        core, fake = self.core, self.fake
        bad: list[str] = []
        records = self.watch.records
        for _, level, name, message in records[self._seen["log"]:]:
            if level in ("ERROR", "CRITICAL"):
                bad.append(f"{level} log from {name}: {message[:400]}")
            elif SWALLOWED.search(message) or PY_ERROR.search(message):
                bad.append(f"warning from {name}: {message[:400]}")
            else:
                self.counts[f"warning: {message[:90]}"] += 1
        self._seen["log"] = len(records)
        for error in self.thread_errors[self._seen["thread"]:]:
            bad.append(f"exception outside the loop: {error}")
        self._seen["thread"] = len(self.thread_errors)
        for _, text in self.toasts[self._seen["toast"]:]:
            act.effects.append(f"toast {clip_text(text, 70)}")
            if PY_ERROR.search(text):
                bad.append(f"toast with Python error text: {text!r}")
        self._seen["toast"] = len(self.toasts)
        for _, line in self.log_lines[self._seen["line"]:]:
            if PY_ERROR.search(line):
                bad.append(f"Log line with Python error text: {line!r}")
        self._seen["line"] = len(self.log_lines)
        residency = fake.residency()
        if len(residency.resident) > 1:
            bad.append(f"{len(residency.resident)} models resident: {residency.resident}")
        bad += self._check_loads(act)
        calls = fake.calls
        for call in calls[self._seen["calls"]:]:
            if call.name in ENGINE_WORK and call.thread == self.loop_thread:
                bad.append(f"engine call {call.name}{call.args} ran on the event-loop thread")
            if call.name == "unload_backend":
                bad += self._check_unload(act, call)
        self._seen["calls"] = len(calls)
        job = core.state.job
        if job.queued > MAX_QUEUE or len(core.jobs.queued) > MAX_QUEUE:
            bad.append(f"{max(job.queued, len(core.jobs.queued))} takes queued (the cap is {MAX_QUEUE})")
        if job.kind and job.queued != len(core.jobs.queued):
            bad.append(f"JobState.queued {job.queued} but {len(core.jobs.queued)} takes wait")
        if job.fill is not None and not (0.0 <= job.fill <= 1.0 and math.isfinite(job.fill)):
            bad.append(f"JobState.fill {job.fill}")
        for element in list(self.user.client.elements.values()):
            hit = [marker for marker in ERROR_MARKERS if marker in element._markers]
            if hit:
                bad.append(f"{hit[0]} is on the page ({' '.join(texts_of(element))[:80]})")
                break
        if binding.active_links:
            bad.append(f"{len(binding.active_links)} polled bindings (binding.active_links)")
        if quiet:
            bad += self._check_quiet()
        return bad

    def _check_quiet(self) -> list[str]:
        """Once quiesce() finished: no job without a hold, and the mirrors match the engine."""
        core, fake = self.core, self.fake
        bad: list[str] = []
        holding = any(not hold.is_set() and hold.entered.is_set() for hold in fake.holds)
        job, task = core.state.job, core.jobs.task
        if not holding:
            if job.kind or (task is not None and not task.done()):
                bad.append(f"stuck job: JobState {job.kind or '-'}/{job.phase or '-'} with no hold "
                           f"(task {'running' if task is not None and not task.done() else 'none'})")
            if core.jobs.queued or job.queued:
                bad.append(f"{len(core.jobs.queued)} takes still queued with no job running")
            residency = fake.residency()
            resident = residency.resident[-1] if residency.resident else ""
            if core.state.engine.resident != resident:
                bad.append(f"EngineState says {short(core.state.engine.resident)} but the engine holds "
                           f"{short(resident)}")
            if residency.loading or residency.unloading or residency.busy:
                bad.append(f"the engine is still busy: loading={residency.loading} unloading={residency.unloading} "
                           f"busy={residency.busy}")
        elif not job.kind:
            bad.append("a call is parked at a hold but JobState is idle")
        return bad

    def _check_loads(self, act: Act) -> list[str]:
        bad: list[str] = []
        for made in self.made[self._seen["made"]:]:
            door = made.door
            if door is None:
                bad.append(f"make_resident({made.backend}) without a ticket")
            elif door.asked and act.key != EMIYA["primary"]:
                bad.append(f"an asked ticket for {made.backend} was redeemed by {act.kind} {act.key}")
            elif door.reason == "warmup" and not self.warmup:
                bad.append(f"a warm-up load of {made.backend} with Warm Up on Launch off")
        self._seen["made"] = len(self.made)
        loads = self.fake.loads
        for backend in loads[self._loads_seen:]:
            why = self._why(act, backend)
            act.effects.append(f"LOAD {short(backend)} ({why or 'unnamed'})")
            if why:
                self.loads.append((act.index, backend, why))
                self.counts[f"load via {why}"] += 1
            else:
                bad.append(f"{short(backend)} loaded by {act.kind} {act.key!r} ({clip_text(act.names, 80)}), "
                           f"which names no model and is no confirmed ask")
        self._loads_seen = len(loads)
        return bad

    def _check_unload(self, act: Act, call: Any) -> list[str]:
        """An unload comes from a control that says Free or Unload, or from the idle setting on a time step."""
        backend = call.args[0] if call.args else "?"
        if call.kwargs.get("reason") == "idle":
            minutes = int(self.core.settings.get("core", "idle_unload_min"))
            if act.kind == "tick" and minutes > 0:
                self.counts["unload via idle"] += 1
                return []
            return [f"an idle unload of {short(backend)} during {act.kind} {act.key!r} (Auto-unload {minutes} min)"]
        if act.kind in ("click", "burst", "search", "trigger") and ("Free" in act.names or "Unload" in act.names):
            self.counts["unload via named"] += 1
            return []
        return [f"{short(backend)} unloaded by {act.kind} {act.key!r} ({clip_text(act.names, 80)}), which says "
                f"neither Free nor Unload"]

    def _why(self, act: Act, backend: str) -> str:
        """Why act may load backend: "named", "asked", "armed" or "key"; "" when it may not."""
        label = catalog.label(backend)
        if act.kind in ("click", "nav", "burst"):
            if act.key == EMIYA["primary"]:
                return "asked" if label in act.names else ""
            if act.replaces_voice:
                previous = [entry for entry in list(self._arms)[:-1] if entry[0] == act.key]
                if "Click Again" not in act.names or not previous or act.at <= previous[-1][2] + ARM_PAUSE_S:
                    return ""
                if any(label in names and act.at - at < ARM_WINDOW_S for _, names, at in previous):
                    return "armed"
                return ""
            if label in act.names:
                return "named"
        if act.kind == "key" and label in act.names:
            return "key"
        return ""


# ---- the monkey --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """Something the monkey can do now: a kind, its element (None for time, faults, ...) and its region."""

    kind: str
    key: str
    element: Element | None = None
    region: str = "page"
    event: str = ""


Fault = tuple[str, Callable[[FakeEngine, Core], Any]]


def disarm_faults(fake: FakeEngine, core: Core) -> None:
    """Take back every fault the monkey armed and release the holds (a measurement must not trip over one)."""
    fake.disarm()
    vars(core.takes).pop("add", None)
    core.trash = getattr(core.trash, "_rel_original", core.trash)


def _fail_next_save(fake: FakeEngine, core: Core) -> None:
    """The next take's WAV cannot be written (a full disk); the store's own add comes back after it."""
    store = core.takes
    if "add" in vars(store):
        return

    def add(*args: Any, **kwargs: Any) -> Any:
        vars(store).pop("add", None)
        raise OSError(28, "No space left on device")

    store.add = add                                  # type: ignore[method-assign]


def _fail_next_trash(fake: FakeEngine, core: Core) -> None:
    """The next file sent to the Recycle Bin is locked for every retry (a player or a scan holds it)."""
    from ui.core.services.takes_store import TRASH_TRIES

    original = core.trash
    if getattr(original, "_rel_fault", False):
        return
    left = [TRASH_TRIES]

    def trash(path: Path) -> None:
        left[0] -= 1
        if left[0] <= 0:
            core.trash = original
        raise PermissionError(13, "The process cannot access the file because it is being used by another "
                                  "process", str(path))

    trash._rel_fault = True                          # type: ignore[attr-defined]
    trash._rel_original = original                   # type: ignore[attr-defined]
    core.trash = trash


def fault_catalogue() -> tuple[Fault, ...]:
    """Faults the monkey arms: each fires on the next matching call, the way the engine or the disk fails."""
    return (
        ("next load fails: out of memory", lambda fake, core: fake.fail_next(
            "make_resident", BackendLoadError("x", "out_of_memory", "CUDA out of memory. Tried to allocate 2.00 GiB",
                                              3.0))),
        ("next load fails: files missing", lambda fake, core: fake.fail_next(
            "make_resident", BackendLoadError("x", "missing_files", "model.safetensors is not in the cache", 0.4))),
        ("next load: engine busy", lambda fake, core: fake.fail_next(
            "make_resident", EngineBusy({"program": "cli.py", "pid": 4242, "since": "13:00:00"}))),
        ("next take fails: out of memory", lambda fake, core: fake.fail_next(
            "speak", GenerationError("out_of_memory", "CUDA out of memory. Tried to allocate 64.00 MiB",
                                     during="speak"))),
        ("next take fails: nan", lambda fake, core: fake.fail_next(
            "speak", GenerationError("other", "probability tensor contains either inf, nan or element < 0",
                                     during="speak"))),
        ("next bake fails", lambda fake, core: fake.fail_next(
            "load_voice", GenerationError("out_of_memory", "CUDA out of memory", during="bake"))),
        ("next unload fails", lambda fake, core: fake.fail_next(
            "unload_backend", RuntimeError("CUDA error: an illegal memory access"))),
        ("next reading fails", lambda fake, core: fake.fail_next("device_info", RuntimeError("CUDA driver error"))),
        ("next delete fails", lambda fake, core: fake.fail_next(
            "delete_voice", PermissionError("The file is in use by another process"))),
        ("next take save fails: disk full", _fail_next_save),
        ("next trashed file is locked", _fail_next_trash),
        ("hold the next load", lambda fake, core: fake.hold("make_resident")),
        ("hold the next take", lambda fake, core: fake.hold("speak")),
        ("hold the next bake", lambda fake, core: fake.hold("load_voice")),
        ("hold the next unload", lambda fake, core: fake.hold("unload_backend")),
    )


class Monkey:
    """Seeded random actions on the page the user sees, each one quiesced and checked (see the module docstring)."""

    def __init__(self, user: User, core: Core, fake: FakeEngine, inv: Invariants, trace: Trace, *,
                 rng: random.Random, drops: Sequence[Path] = (), weights: Mapping[str, float] = MONKEY_WEIGHTS,
                 faults: bool = True, reloads: bool = True) -> None:
        self.user, self.core, self.fake, self.inv, self.trace = user, core, fake, inv, trace
        self.rng = rng
        self.drops = tuple(drops)
        self.weights = dict(weights)
        if not faults:
            self.weights.pop("fault", None)
        if not reloads:
            self.weights.pop("reload", None)
            self.weights.pop("back", None)
        self.index = 0
        self.kinds: collections.Counter[str] = collections.Counter()
        self.slowest: dict[str, float] = {}
        self.slow_acts: list[tuple[float, str, str, str]] = []     # the 8 slowest: (seconds, kind, key, route)
        self._held_since: int | None = None
        self._names = 0
        self._faults = fault_catalogue()

    @property
    def clock(self) -> ManualScheduler:
        """The manual clock (time passing is an action only on it; VOICETOOL_CLOCK=real runs on its own)."""
        scheduler = self.core.scheduler
        assert isinstance(scheduler, ManualScheduler), "this run uses the real clock"
        return scheduler

    @property
    def manual(self) -> bool:
        return isinstance(self.core.scheduler, ManualScheduler)

    # ---------- what the user sees ----------

    def route(self) -> str:
        from tests.pages.test_wiring import behaviours_of

        try:
            return str(behaviours_of(self.user).status().vm.route)
        except AssertionError:
            return "?"

    def shown(self, marker: str) -> list[Element]:
        """The shown elements carrying marker (in the current page and shell)."""
        return [seen.element for seen in scan(self.user.current_layout) if marker in seen.element._markers]

    def first(self, marker: str, *, enabled: bool = True) -> Element | None:
        found = [element for element in self.shown(marker) if not enabled or is_enabled(element)]
        return min(found, key=lambda element: element.id) if found else None

    def prefixed(self, prefix: str) -> list[Element]:
        """Shown, enabled elements with a marker starting with prefix ("takes.star.")."""
        return [seen.element for seen in scan(self.user.current_layout)
                if is_enabled(seen.element) and any(m.startswith(prefix) for m in seen.element._markers)]

    def candidates(self) -> dict[str, list[Candidate]]:
        found: dict[str, list[Candidate]] = collections.defaultdict(list)
        for seen in scan(self.user.current_layout):
            element = seen.element
            is_box = isinstance(element, (ui.input, ui.textarea))
            types = handler_types(element)
            if not (types or is_box) or not is_enabled(element):
                continue
            key = key_of(element)
            if "click" in types:
                kind = "nav" if any(m.startswith("nav.") for m in element._markers) else "click"
                found[kind].append(Candidate(kind, key, element, seen.region))
                if kind == "click":
                    found["burst"].append(Candidate("burst", key, element, seen.region))
            if is_box:
                found["type"].append(Candidate("type", key, element, seen.region))
                if EMIYA["search"] in element._markers:
                    found["search"].append(Candidate("search", key, element, seen.region))
            if "change" in types and not is_box:
                found["slide"].append(Candidate("slide", key, element, seen.region))
            if "keydown" in types and "listen" in element.classes:
                found["trigger"].append(Candidate("trigger", key, element, seen.region, "keydown"))
            if "command" in types:
                found["key"].append(Candidate("key", key, element, seen.region))
            if "state" in types:
                found["player"].append(Candidate("player", key, element, seen.region))
            for event in TRIGGERS:
                if event in types:
                    found["trigger"].append(Candidate("trigger", key, element, seen.region, event))
        if self.manual:
            found["tick"].append(Candidate("tick", "clock"))
        if any(not hold.is_set() for hold in self.fake.holds):
            found["release"].append(Candidate("release", "holds"))
        if len(self.user.back_history) >= 2:
            found["back"].append(Candidate("back", "history"))
        found["reload"].append(Candidate("reload", "page"))
        if self.drops:
            found["drop"].append(Candidate("drop", "window"))
        found["fault"].append(Candidate("fault", "engine"))
        found["window"].append(Candidate("window", "minimize or restore"))
        found["external"].append(Candidate("external", "another program"))
        for items in found.values():
            items.sort(key=lambda c: (c.key, c.event, c.element.id if c.element is not None else 0))
        return found

    def choose(self) -> Candidate:
        found = self.candidates()
        kinds = [kind for kind in self.weights if found.get(kind)]
        kind = self.rng.choices(kinds, weights=[self.weights[kind] for kind in kinds])[0]
        items = found[kind]
        if kind == "click":
            return self.rng.choices(items, weights=[REGION_WEIGHTS[item.region] for item in items])[0]
        return self.rng.choice(items)

    # ---------- doing things ----------

    async def step(self) -> Act:
        """One random action (the holds are released once they have been held HOLD_LIMIT actions)."""
        waiting = any(not hold.is_set() for hold in self.fake.holds)
        if not waiting:
            self._held_since = None
        elif self._held_since is None:
            self._held_since = self.index
        if waiting and self._held_since is not None and self.index - self._held_since >= HOLD_LIMIT:
            return await self.act("release", "holds (limit)", self.fake.release_all)
        return await self.perform(self.choose())

    async def run(self, count: int) -> None:
        for _ in range(count):
            await self.step()

    async def perform(self, item: Candidate) -> Act:
        element = item.element
        kind = item.kind
        if kind in ("click", "nav"):
            assert element is not None
            return await self.click(element)
        if kind == "burst":
            assert element is not None
            return await self.burst(element, times=self.rng.choice((2, 2, 3)),
                                    yields=self.rng.choice((0, 0, 1, 3)))
        if kind == "type":
            assert element is not None
            return await self._type(element)
        if kind == "search":
            assert element is not None
            return await self._search(element)
        if kind == "slide":
            assert element is not None
            return await self._slide(element)
        if kind == "trigger":
            assert element is not None
            return await self._trigger(element, item.event)
        if kind == "key":
            assert element is not None
            return await self._key(element)
        if kind == "player":
            assert element is not None
            return await self._player(element)
        if kind == "tick":
            seconds = self.rng.choices([dt for dt, _ in TICKS], weights=[w for _, w in TICKS])[0]
            return await self.act("tick", f"+{seconds:g} s", lambda: self.clock.advance(seconds))
        if kind == "release":
            return await self.act("release", "holds", self.fake.release_all)
        if kind == "back":
            return await self.act("back", self.user.back_history[-2], self.user.navigate.back)
        if kind == "reload":
            return await self.reload()
        if kind == "drop":
            path = self.rng.choice(self.drops)
            return await self.act("drop", path.name, lambda: self.core.events.dropped.emit((str(path),)))
        if kind == "fault":
            name, arm = self.rng.choice(self._faults)

            def inject() -> None:
                arm(self.fake, self.core)
                self.inv.faults.append(name)

            return await self.act("fault", name, inject)
        if kind == "window":
            return await self._window()
        if kind == "external":
            return await self._external()
        raise AssertionError(f"unknown action kind {kind!r}")

    async def _window(self) -> Act:
        """The native window's minimize and restore events (ui.core.native, as pywebview sends them)."""
        from ui.core import native

        visible = self.core.state.window.visible
        event = native._BRIDGE.minimized if visible else native._BRIDGE.restored
        return await self.act("window", "minimized" if visible else "restored", event)

    async def _external(self) -> Act:
        """What another program or the user in Explorer does behind the app's back: cli.py removes or adds a
        registry entry, a take's WAV or a reference clip is deleted."""
        registry = self.fake.registry
        ids = registry.list_ids()
        takes = [take for take in self.core.takes.takes if (self.core.takes.path_of(take.id) or Path()).is_file()]
        clips = sorted({row.reference_path for row in self.core.state.library.rows
                        if row.reference_path is not None and Path(row.reference_path).is_file()}, key=str)
        choices = ["add a voice"] + (["remove a voice"] if ids else []) + (["delete a take"] if takes else []) \
            + (["delete a clip"] if clips else [])
        what = self.rng.choice(choices)
        if what == "remove a voice":
            voice = self.rng.choice(ids)
            return await self.act("external", what, lambda: registry.remove(voice), detail=voice)
        if what == "delete a take":
            path = self.core.takes.path_of(self.rng.choice(takes).id)
            assert path is not None

            def delete_take() -> None:
                path.unlink()
                self.inv.deleted.append(path)

            return await self.act("external", what, delete_take, detail=path.name)
        if what == "delete a clip":
            clip = Path(self.rng.choice(clips))
            return await self.act("external", what, clip.unlink, detail=clip.name)
        self._names += 1
        voice = f"cli{self._names}"
        source = self.drops[0] if self.drops else self.core.paths.voices / "sample.wav"
        return await self.act("external", what, lambda: registry.register(
            voice, "chatterbox-multilingual", str(source), 0.5, "en", baked=False), detail=voice)

    async def click(self, element: Element, *, job: bool = True) -> Act:
        names = names_of(element)
        if EMIYA["primary"] in element._markers:
            names += " " + self._ask_text()
        kind = "nav" if any(m.startswith("nav.") for m in element._markers) else "click"
        return await self.act(kind, key_of(element), lambda: UserInteraction(self.user, {element}, None).click(),
                              names=names, job=job)

    async def burst(self, element: Element, *, times: int = 2, yields: int = 0) -> Act:
        """A fast double (or triple) click: the clicks arrive before the app settles, `yields` loop turns apart."""
        names = names_of(element)
        if EMIYA["primary"] in element._markers:
            names += " " + self._ask_text()

        async def run() -> None:
            for index in range(times):
                if index:
                    for _ in range(yields):
                        await asyncio.sleep(0)
                if element.is_deleted:
                    break
                UserInteraction(self.user, {element}, None).click()

        return await self.act("burst", key_of(element), run, detail=f"x{times} {yields} turns apart", names=names)

    async def click_marker(self, marker: str, *, job: bool = True) -> Act | None:
        """Click the shown, enabled element with this marker (None: there is none)."""
        element = self.first(marker)
        return None if element is None else await self.click(element, job=job)

    async def set_text(self, element: Element, text: str, *, mode: str = "set") -> Act:
        def run() -> None:
            with self.user.client:
                if mode == "append":
                    UserInteraction(self.user, {element}, None).type(text)
                else:
                    element.set_value(text if mode == "set" else "")

        return await self.act("type", key_of(element), run, detail=f"{mode} {clip_text(text, 40)}")

    async def _type(self, element: Element) -> Act:
        key = key_of(element)
        pool: Sequence[str] = SCRIPTS + NASTY
        if "name" in key:
            self._names += 1
            pool = (*NAMES, f"m{self._names}")
        elif "text" in key or "script" in key:
            pool = SCRIPTS
        elif "search" in key or "filter" in key:
            pool = QUERIES
        mode = self.rng.choices(("set", "append", "clear"), weights=(6, 2.5, 1.5))[0]
        return await self.set_text(element, self.rng.choice(pool), mode=mode)

    async def _search(self, box: Element) -> Act:
        """Type a query into the rail search, then pick a result, press Enter (the first) or Escape: two acts,
        the second named by the result it runs."""
        query = self.rng.choice(QUERIES)
        way = self.rng.choices(("pick", "enter", "escape"), weights=(6, 2.5, 1.5))[0]
        await self.set_text(box, query)
        results = sorted(self.shown(EMIYA["result"]), key=lambda element: element.id)
        if box.is_deleted:
            return await self.act("search", f"{query!r} (the box went away)", lambda: None)
        if way == "pick" and results:
            pick = self.rng.choice(results)
            return await self.act("search", f"{query!r} pick", lambda: UserInteraction(
                self.user, {pick}, None).trigger("mousedown"), names=names_of(pick))
        if way == "enter":
            first = names_of(results[0]) if results else ""
            return await self.act("search", f"{query!r} Enter", lambda: UserInteraction(
                self.user, {box}, None).trigger("keydown.enter", query), names=first)
        return await self.act("search", f"{query!r} Escape", lambda: UserInteraction(
            self.user, {box}, None).trigger("keydown.esc"))

    async def _slide(self, element: Element) -> Act:
        low = float(getattr(element, "minimum", 0.0))
        high = float(getattr(element, "maximum", 1.0))
        value = self.rng.choice((low, high, round(self.rng.uniform(low, high), 2), round((low + high) / 2, 2)))

        def run() -> None:
            interaction = UserInteraction(self.user, {element}, None)
            if "input" in handler_types(element):
                interaction.trigger("input", value)
            interaction.trigger("change", value)

        return await self.act("slide", key_of(element), run, detail=f"{value:g}")

    async def _trigger(self, element: Element, event: str) -> Act:
        args: Any = None
        names = names_of(element)
        if event == "keydown":
            args = dict(self.rng.choice(CAPTURES))
        elif event == "keydown.enter":
            args = getattr(element, "value", None)
            if EMIYA["search"] in element._markers:     # Enter runs the first result: it names the act
                results = sorted(self.shown(EMIYA["result"]), key=lambda found: found.id)
                names = names_of(results[0]) if results else ""
        elif event == "seek":
            args = round(self.rng.uniform(-1.0, 12.0), 2)
        return await self.act("trigger", key_of(element),
                              lambda: UserInteraction(self.user, {element}, None).trigger(event, args),
                              detail=f"{event} {clip_text(args, 40) if args is not None else ''}".strip(),
                              names=names)

    async def _key(self, element: Element) -> Act:
        combos = element._props.get("combos") or {}
        pairs = sorted((str(combo), str(command)) for combo, command in combos.items())
        if not pairs:
            pairs = [("Escape", "escape")]
        combo, command = self.rng.choice(pairs)
        names = ""
        if command in ("gen", "gen3") and self.route() == "/":
            names = self._generate_names(command)
        return await self.act("key", f"{key_of(element)} {command}",
                              lambda: UserInteraction(self.user, {element}, None).trigger(
                                  "command", {"id": command, "combo": combo}),
                              detail=combo, names=names)

    async def _player(self, element: Element) -> Act:
        target = getattr(element, "target", None)
        seq = int(getattr(target, "seq", 0) or 0)
        duration = round(self.rng.uniform(0.5, 9.0), 2)
        report = self.rng.choice((
            {"playing": True, "position": round(duration / 3, 2), "duration": duration, "ended": False},
            {"playing": False, "position": round(duration / 2, 2), "duration": duration, "ended": False},
            {"playing": False, "position": duration, "duration": duration, "ended": True},
            {"playing": False, "position": 0.0, "duration": 0.0, "ended": False, "error": "MEDIA_ERR_DECODE"},
        ))
        stale = self.rng.random() < 0.15
        args = {"seq": seq - 1 if stale else seq, "error": "", **report}
        return await self.act("player", key_of(element),
                              lambda: UserInteraction(self.user, {element}, None).trigger("state", args),
                              detail=f"{'stale ' if stale else ''}{clip_text(report, 60)}")

    async def reload(self) -> Act:
        """Reload the page like the browser does: a new client on the same route, the old one goes away."""
        route = self.route()
        target = route if route in ROUTES else "/"

        async def run() -> None:
            old = self.user.client
            await self.user.open(target)
            if old is not None and old is not self.user.client and not old.is_deleted:
                old.delete()

        return await self.act("reload", target, run)

    async def _engine_call_begun(self, loads: int, calls: int, timeout: float = FREEZE_S) -> None:
        """The job this action started has reached the engine: a switch its load, an unload its unload_backend
        call (or the job ended). Then that load or unload counts as this action's.

        Polls every POLL_S: on Python 3.11 under Windows time.monotonic() ticks every 15.6 ms, and an
        asyncio.sleep() shorter than that returns at once, so a shorter poll spins the loop.
        """
        fake, job = self.fake, self.core.state.job
        deadline = time.perf_counter() + timeout

        def waiting() -> bool:
            if job.kind == "switch":
                return len(fake.loads) <= loads
            if job.kind == "unload":
                return not any(call.name == "unload_backend" for call in fake.calls[calls:])
            return False

        while waiting():
            if time.perf_counter() > deadline:
                raise Frozen(f"the {job.kind} job did not reach the engine in {timeout:.0f} s")
            await asyncio.sleep(POLL_S)

    def _ask_text(self) -> str:
        from tests.pages.test_wiring import behaviours_of

        ask = behaviours_of(self.user).status().vm.ask
        if ask is None:
            return ""
        return " ".join([ask.title, ask.primary, *(text for text, _ in ask.lines)])

    def _generate_names(self, command: str) -> str:
        from tests.pages.test_wiring import behaviours_of

        speak = behaviours_of(self.user).speak()
        state = speak.vm.generate if command == "gen" else speak.vm.generate3
        return f"{state.label} {state.aria} {state.tooltip}"

    async def act(self, kind: str, key: str, run: Callable[[], Any], *, detail: str = "", names: str = "",
                  job: bool = True) -> Act:
        """The one door for every action: perform it, quiesce, check the invariants, trace it, fail on a violation.

        job=False (a FakeEngine with speed > 0) does not wait for the job the action started: only until its
        load, if it makes one, has begun, so the load is still this action's; the quiet checks are skipped.
        """
        act = Act(self.index, kind, key, detail=detail, names=names, at=self.core.scheduler.now(), route=self.route())
        self.index += 1
        self.kinds[kind] += 1
        self.inv.begin(act)
        bad: list[str] = []
        quiet = job
        loads, calls = len(self.fake.loads), len(self.fake.calls)
        job_rev = self.core.state.job.rev
        started = time.perf_counter()
        if sys.__stderr__ is not None:                     # the loop itself stuck: quiesce could never time out
            faulthandler.dump_traceback_later(WATCHDOG_S, file=sys.__stderr__)
        try:
            result = run()
            if inspect.isawaitable(result):
                await result
        except Frozen as exc:
            bad.append(f"freeze: {exc}")
        except Exception as exc:                      # the action itself raised in the test's task
            bad.append(f"the action raised {type(exc).__name__}: {clip_text(str(exc), 300)}")
        try:
            await quiesce(self.core, self.fake, skip_job=not job)
            if not job and self.core.state.job.rev != job_rev:
                await self._engine_call_begun(loads, calls)
        except Frozen as exc:
            bad.append(f"freeze: {exc}")
            quiet = False
        finally:
            faulthandler.cancel_dump_traceback_later()
        elapsed = time.perf_counter() - started
        self.slowest[kind] = max(self.slowest.get(kind, 0.0), elapsed)
        if len(self.slow_acts) < 8 or elapsed > self.slow_acts[-1][0]:
            self.slow_acts = sorted([*self.slow_acts, (elapsed, kind, key, act.route)], reverse=True)[:8]
        if kind in ("click", "nav", "burst"):
            self.inv.note_click(act)
        bad += self.inv.check(act, quiet=quiet)
        job = self.core.state.job
        state = (f"job={job.kind or '-'}{'/' + job.phase if job.phase else ''} q={job.queued} "
                 f"res={short(self.core.state.engine.resident)}")
        line = (f"#{act.index:05d} {act.route:9} {kind:7} {clip_text(key, 60)}"
                f"{' [' + detail + ']' if detail else ''} -> {state}"
                f"{' | ' + '; '.join(act.effects) if act.effects else ''}")
        self.trace.add(line)
        if bad:
            self.trace.add("   VIOLATION: " + " || ".join(bad))
            print(self.trace.dump())
            self.trace.save(self.trace.header.split("\n")[0])
            raise AssertionError(f"{self.trace.header}\naction #{act.index} ({kind} {key}) broke an invariant:\n- "
                                 + "\n- ".join(bad) + f"\nlast actions:\n{self.trace.tail()}")
        return act

    async def finish(self) -> None:
        """Release every hold, let the app settle, and check that it ends idle and its files agree with memory."""
        await self.act("release", "holds (end)", self.fake.release_all)
        if self.manual:
            await self.act("tick", "+5 s (end)", lambda: self.clock.advance(5.0))
        problems = takes_consistency(self.core, deleted=self.inv.deleted)
        problems += stored_state(self.core)
        problems += await library_matches(self.core, self.fake)
        await quiesce(self.core, self.fake)
        problems += [f"left behind: {name}" for name in leftovers(self.core.paths.root)]
        if problems:
            print(self.trace.dump())
            raise AssertionError(f"{self.trace.header}\nat the end:\n- " + "\n- ".join(problems))


@pytest.fixture
def clock_kind(request: pytest.FixtureRequest, fake_env: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """"manual" (the page tests' clock; the default) or "real" (parametrize it indirectly): the job ticker, the
    device poller and the arms then run on their own, as in the app. Request it before `user`, so gui.py
    composes the app with it."""
    from ui.core.clock import CLOCK_ENV

    kind = str(getattr(request, "param", "manual"))
    if kind == "real":
        monkeypatch.setenv(CLOCK_ENV, "real")
    return kind


async def open_monkey(user: User, core: Core, fake: FakeEngine, open_route: Callable[..., Awaitable[None]],
                      route: Route, inv: Invariants) -> None:
    """Open route like a user (the scenario's replay runs), let it settle, and start counting from there."""
    await open_route(user, route)
    await quiesce(core, fake)
    inv.baseline()


def summary(monkey: Monkey) -> str:
    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(monkey.kinds.items()))
    loads = collections.Counter(why for _, _, why in monkey.inv.loads)
    slow = "; ".join(f"{kind} {clip_text(key, 40)} on {route} {seconds * 1000:.0f} ms"
                     for seconds, kind, key, route in monkey.slow_acts[:3])
    return (f"{monkey.index} actions ({kinds}); loads {dict(loads) or 0}; faults {len(monkey.inv.faults)}; "
            f"slowest: {slow}")
