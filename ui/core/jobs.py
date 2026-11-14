"""JobRunner (§4.8-4.9): one job slot, chains, the queue of 3, JobState and the 0.2 s ticker.

One job runs at a time (the second layer of the one-model rule, §4.5): a switch, an unload, takes, a
forge or a Forge test line. The slot is held for a whole chain (D11): a switch and the takes or forge it
was started for, and a generate job until its queue is empty. Every start_* call answers at once with a
StartResult and claims the slot synchronously (JobState.kind), then runs the chain as an app-level task.
Chain steps write JobState, log lines, toasts, load history and the other mirrors on the event loop;
elapsed times come from core.scheduler, so a manual clock makes every label deterministic. A chain never
raises: the slot is released whatever happens.

StartResult.reason is toast copy for the controller ("stale" means: no toast, recompute the label).
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from nicegui import run

from engine.backends.info import BACKENDS
from engine.progress import Job, JobStatus
from ui.contract.plans import AfterForge, AfterGenerate, ForgeRequest, GenerateRequest
from ui.contract.records import LoadEvent

from . import catalog
from .facade import BUSY, OOM, STALE, Outcome, _boxed, describe_error, not_loaded
from .services.device import apply_reading, gb
from .services.estimates import TakeEstimate, estimate_take
from .services.load_history import event_text, now_iso
from .services.settings import folder_path, folder_value
from .services.takes_store import NewTake, TakeNotListedError

if TYPE_CHECKING:
    from engine.types import EngineEvent, LoadReport
    from ui.contract.records import VoiceRow

    from . import Core
    from .clock import Handle
    from .tokens import LoadTicket

__all__ = [
    "BAKE_FILL_MAX",
    "FILL_MAX",
    "MAX_QUEUE",
    "QUEUE_FULL",
    "TEST_LINE_JA",
    "TICK_S",
    "JobRunner",
    "StartResult",
]

MAX_QUEUE = 3                            # takes waiting behind the running one (M L1621)
TICK_S = 0.2                             # the progress ticker
FILL_MAX = 0.95                          # loads and takes (M L1583)
BAKE_FILL_MAX = 0.97                     # a bake (M L1814)
QUEUE_FULL = "Queue full · 3 takes waiting"
NOTHING_TO_FREE = "Nothing to free · no model loaded"
NO_CONSENT = "Confirm Consent First"
TEST_LINE_JA = "こんにちは。テストです。"
GIB = 2 ** 30

_TAKE_PHASES = {"waiting": "preparing", "preparing": "preparing", "sampling": "sampling", "decoding": "decoding"}
_IDLE: dict[str, Any] = {"kind": "", "phase": "", "source": "", "target": "", "voice": "", "take_no": 0,
                         "queued": 0, "started_at": 0.0, "elapsed_s": 0.0, "audio_s": 0.0, "fill": None}

log = logging.getLogger("voicetool.core")


@dataclass(frozen=True)
class StartResult:
    """The answer to a start_* call: started, queued (behind a running take) or refused (with why)."""

    status: Literal["started", "queued", "refused"]
    reason: str = ""
    dropped: int = 0                     # takes that did not fit the queue


_STARTED = StartResult("started")
_STALE = StartResult("refused", STALE)
_BUSY = StartResult("refused", BUSY)


class JobRunner:
    """The job slot. Construct it after the Core (build_core does): it listens to the facade's events."""

    def __init__(self, core: Core) -> None:
        self._core = core
        self._queue: list[GenerateRequest] = []
        self._task: asyncio.Task[Any] | None = None
        self._job: Job | None = None
        self._ticker: Handle | None = None
        self._history_tail: asyncio.Task[Any] | None = None
        self._unloaded: dict[str, EngineEvent] = {}
        self._idle_hooks: list[Callable[[], None]] = []
        self._once_hooks: list[Callable[[], None]] = []
        self._load_about: float | None = None
        self._take_est: TakeEstimate | None = None
        self._bake_est: float | None = None
        self._idle_minutes = 0
        self._stop = False
        self._stop_before_take = False
        self.started_any = False         # a switch, generate or forge job started since launch (warm-up rule)
        self.quiet_take_id = 0           # latest ready take kept after a Stop during its save
        self.last_activity = core.scheduler.now()   # end of the last switch, generate or forge job
        core.facade.listen(self._on_engine_event)

    # ---------- state ----------

    @property
    def busy(self) -> bool:
        return bool(self._core.state.job.kind)

    @property
    def queued(self) -> tuple[GenerateRequest, ...]:
        """The takes waiting behind the running job, oldest first (at most 3)."""
        return tuple(self._queue)

    @property
    def task(self) -> asyncio.Task[Any] | None:
        """The running chain's task, or None (tests: ManualScheduler.settle(ignore=[jobs.task]))."""
        return self._task

    def on_idle(self, hook: Callable[[], None]) -> None:
        """Call hook() each time a job ends and the slot is free again."""
        self._idle_hooks.append(hook)

    def once_idle(self, hook: Callable[[], None]) -> None:
        """Call hook() once: now when no job runs, else as soon as the running job ends (a failure is logged)."""
        if self.busy:
            self._once_hooks.append(hook)
        else:
            self._call_hook(hook)

    async def wait_idle(self) -> None:
        """Wait until no job runs."""
        while self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    # ---------- starting jobs ----------

    def start_switch(self, token: str) -> StartResult:
        """A named control, the dock, the Models card, Retry, Load <default> or the popover primary."""
        if self.busy:
            return _BUSY
        ticket = self._core.tokens.redeem(token)
        if ticket is None:
            return _STALE
        after = ticket.plan.after
        first: GenerateRequest | None = None
        forge = after.req if isinstance(after, AfterForge) else None
        if isinstance(after, AfterGenerate) and after.reqs:
            first, rest = after.reqs[0], after.reqs[1:]
            self._queue = list(rest[:MAX_QUEUE])
        if ticket.plan.to_backend == self._core.state.engine.resident:   # nothing to load: just the after-action
            if first is not None:
                self._claim(self._take_fields(first))
            elif forge is not None:
                self._claim(self._forge_fields(forge))
            else:
                return _STALE
            self._launch(lambda: self._chain(forge=forge, first=first), name="after-action")
            return _STARTED
        self._claim(self._switch_fields(ticket.plan.to_backend))
        self._launch(lambda: self._chain(ticket=ticket, forge=forge, first=first), name="switch")
        return _STARTED

    def start_free(self, *, reason: Literal["requested", "idle"] = "requested") -> StartResult:
        """Free VRAM (dock, Models page, search action) or the idle auto-unload."""
        if self.busy:
            return _BUSY
        resident = self._core.state.engine.resident
        if not resident:
            return StartResult("refused", NOTHING_TO_FREE)
        idle = reason == "idle"
        self._idle_minutes = int(self._core.settings.get("core", "idle_unload_min")) if idle else 0
        self._queue.clear()
        self._claim(self._unload_fields(resident))
        self._launch(lambda: self._unload(resident, idle=idle), name="unload", activity=False)
        return _STARTED

    def start_generate(self, reqs: Sequence[GenerateRequest], *, token: str | None = None) -> StartResult:
        """Takes: start, queue behind a take or switch for the same backend, or refuse (§4.8 table)."""
        reqs = tuple(reqs)
        if not reqs:
            raise ValueError("start_generate needs at least one request")
        backend = reqs[0].backend
        job = self._core.state.job
        if job.kind:
            if job.phase == "stopping":
                return StartResult("refused", "Wait for the running take to stop")
            if job.kind == "switch" or (job.kind == "generate" and job.take_no > 0):
                if job.target != backend:
                    return StartResult("refused", f"Busy with {catalog.label(job.target)} · {reqs[0].voice_id} "
                                                  f"needs {catalog.label(backend)} · switch after the queue")
                return self._enqueue(reqs)
            return _BUSY
        resident = self._core.state.engine.resident == backend
        ticket = None if resident or not token else self._core.tokens.redeem(token, expect_to=backend)
        if not resident and ticket is None:
            return _STALE
        rest = reqs[1:]
        self._queue = list(rest[:MAX_QUEUE])
        dropped = len(rest) - len(self._queue)
        if resident:
            self._claim(self._take_fields(reqs[0]))
            self._launch(lambda: self._chain(first=reqs[0]), name="takes")
        else:
            self._claim(self._switch_fields(backend))
            self._launch(lambda: self._chain(ticket=ticket, first=reqs[0]), name="switch and takes")
        return StartResult("started", QUEUE_FULL if dropped else "", dropped) if dropped else _STARTED

    def cancel_queue(self) -> int:
        """Drop the waiting takes (the running one finishes); returns how many were dropped."""
        dropped = len(self._queue)
        self._queue.clear()
        if dropped:
            self._set_job(queued=0)
            self._core.log.add(f"Cancelled {dropped} queued take{'s' if dropped != 1 else ''}")
        return dropped

    def note_activity(self, backend: str) -> None:
        """Postpone idle unload for edits to the resident model's Speak or Forge draft."""
        if backend and backend == self._core.state.engine.resident:
            self.last_activity = self._now()

    def stop_take(self, *, drop_queue: bool = True) -> bool:
        """Stop a Speak take, including one whose WAV is being committed. Loads and Forge keep running."""
        state = self._core.state.job
        if state.kind != "generate" or state.take_no <= 0:
            return False
        if drop_queue:
            self.cancel_queue()
        if self._job is not None:
            self._job.cancel()
        else:
            self._stop_before_take = True
        self._set_job(phase="stopping")
        return True

    def start_forge(self, req: ForgeRequest, *, token: str | None = None) -> StartResult:
        """Forge on the resident backend, or switch first with a valid token for req.backend."""
        if self.busy:
            return _BUSY
        if req.consent is not True:
            return StartResult("refused", NO_CONSENT)
        if self._core.state.engine.resident == req.backend:
            self._claim(self._forge_fields(req))
            self._launch(lambda: self._chain(forge=req), name="forge")
            return _STARTED
        ticket = self._core.tokens.redeem(token, expect_to=req.backend) if token else None
        if ticket is None:
            return _STALE
        self._claim(self._switch_fields(req.backend))
        self._launch(lambda: self._chain(ticket=ticket, forge=req), name="switch and forge")
        return _STARTED

    def start_test_line(self, voice_id: str, *,
                        on_done: Callable[[str], Any] | None = None) -> StartResult:
        """Forge's "Play Test Line": one unsaved take to staging; on_done(url) when its WAV is written."""
        if self.busy:
            return _BUSY
        row = self._row(voice_id)
        if row is None:
            return StartResult("refused", f"{voice_id} no longer exists")
        if self._core.state.engine.resident != row.backend:
            return StartResult("refused", not_loaded(row.backend))
        req = _test_request(row)
        self._claim(self._test_fields(req))
        self._launch(lambda: self._test_line(req, on_done), name="test line")
        return _STARTED

    def cancel_running(self) -> None:
        """Cancel the running take or bake at its next step and drop the queue (core.stop() only)."""
        self._stop = True
        self._queue.clear()
        if self.busy:
            self._set_job(queued=0)
        if self._job is not None:
            self._job.cancel()

    # ---------- chains ----------

    def _launch(self, chain: Callable[[], Coroutine[Any, Any, None]], *, name: str, activity: bool = True) -> None:
        self._stop = False
        if activity:
            self.started_any = True
        scheduler = self._core.scheduler
        if self._ticker is not None:
            self._ticker.cancel()
        self._ticker = scheduler.every(TICK_S, self._tick)
        self._task = scheduler.spawn(self._run(chain, activity), name=f"voicetool {name}")

    async def _run(self, chain: Callable[[], Coroutine[Any, Any, None]], activity: bool) -> None:
        try:
            await chain()
        except Exception as exc:         # a bug in a step: report it as text, never leave the slot held
            text = describe_error(exc)
            log.warning("A job ended unexpectedly: %s", text)
        finally:
            self._finish(activity)

    async def _chain(self, *, ticket: LoadTicket | None = None, forge: ForgeRequest | None = None,
                     first: GenerateRequest | None = None) -> None:
        if ticket is not None and not await self._switch(ticket):
            return
        if forge is not None and not (await self._forge(forge)):
            return
        req = first if first is not None else self._next()
        while req is not None and not self._stop:
            if not await self._take(req):
                return
            req = self._next()

    def _finish(self, activity: bool) -> None:
        self._stop_before_take = False
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        self._job = None
        self._queue.clear()
        self._unloaded.clear()
        self._task = None
        if activity:
            self.last_activity = self._core.scheduler.now()
        self._set_job(**_IDLE)
        once, self._once_hooks = self._once_hooks, []
        for hook in [*self._idle_hooks, *once]:
            self._call_hook(hook)

    def _call_hook(self, hook: Callable[[], None]) -> None:
        try:
            hook()
        except Exception as exc:
            text = describe_error(exc)
            log.warning("An idle hook failed: %s", text)

    def _next(self) -> GenerateRequest | None:
        return self._queue.pop(0) if self._queue else None

    def _enqueue(self, reqs: tuple[GenerateRequest, ...]) -> StartResult:
        room = max(0, MAX_QUEUE - len(self._queue))
        taken = reqs[:room]
        dropped = len(reqs) - len(taken)
        self._queue.extend(taken)
        self._set_job(queued=len(self._queue))
        if not taken:
            return StartResult("refused", QUEUE_FULL, dropped)
        return StartResult("queued", QUEUE_FULL if dropped else "", dropped)

    # ---------- switch ----------

    def _switch_fields(self, target: str) -> dict[str, Any]:
        resident = self._core.state.engine.resident
        self._load_about = self._estimate_load(target)
        return {"kind": "switch", "phase": "unload" if resident else "load", "source": resident, "target": target,
                "voice": "", "take_no": 0, "queued": len(self._queue), "started_at": self._now(),
                "elapsed_s": 0.0, "audio_s": 0.0, "fill": None if resident else self._start_fill(self._load_about)}

    async def _switch(self, ticket: LoadTicket) -> bool:
        core = self._core
        plan = ticket.plan
        target, source = plan.to_backend, core.state.engine.resident
        device = core.planner.device()
        self._set_job(**self._switch_fields(target))
        core.facade.set_engine(failed="")
        if source:
            core.log.add(f"Switching {catalog.label(source)} → {catalog.label(target)}")
            self._record(LoadEvent(t=now_iso(), kind="switching", backend=target, from_backend=source, device=device))
        else:
            warm = plan.reason == "warmup"
            core.log.add(f"Loading {target}" + (" · warm-up" if warm else ""))
            self._record(LoadEvent(t=now_iso(), kind="loading", backend=target, reason="warm-up" if warm else "",
                                   device=device))
        out = await core.facade.load(ticket)
        if out.ok and out.value is not None:
            if not out.value.already_resident:
                self._loaded(out.value)
            return True
        if out.status == "refused":
            self._load_refused(target, out)
        elif out.status == "failed":
            await self._load_failed(target, out.reason, device)
        self._queue.clear()
        return False

    def _loaded(self, report: LoadReport) -> None:
        core = self._core
        backend, cpu = report.backend, report.device == "cpu"
        size = None if cpu else _gb(report.reserved_bytes)
        core.log.add(event_text(LoadEvent(t=now_iso(), kind="loaded", backend=backend, seconds=report.seconds,
                                          gb=size, device=report.device)))
        suffix = " · CPU" if cpu else (f" · {size:.1f} GB" if size is not None else "")
        core.events.say(f"{catalog.label(backend)} ready · {report.seconds:.1f} s{suffix}")
        core.events.flash.emit(f"dock-{backend}")
        core.events.spark.emit(f"dock-{backend}")
        self._record(LoadEvent(t=now_iso(), kind="loaded", backend=backend, seconds=round(report.seconds, 2),
                               gb=_round(size), device=report.device, cold=bool(report.first_in_process)))

    def _load_refused(self, target: str, out: Outcome[Any]) -> None:
        core = self._core
        if out.reason == STALE:
            return
        holder = out.value if isinstance(out.value, dict) else None
        if holder is not None or out.reason.startswith("The voice engine is in use"):
            program, pid = (holder or {}).get("program"), (holder or {}).get("pid")
            who = f"{program} (pid {pid})" if program and pid is not None else "another program"
            core.log.add(f"Load refused: {target} · {who} holds the engine")
        else:
            core.log.add(f"Load refused: {target} · {out.reason}")
        core.events.say(out.reason, bad=True)

    async def _load_failed(self, target: str, reason: str, device: str) -> None:
        core = self._core
        core.facade.set_engine(failed=target)
        core.log.add(f"Load failed: {target} · {reason}")
        suffix = ""
        if reason == OOM:
            reading = await core.facade.device_info()
            if reading.ok and reading.value is not None and reading.value.free_bytes is not None:
                if apply_reading(core.state.device, reading.value):
                    core.events.notify()
                suffix = f" ({gb(reading.value.free_bytes)} GB free)"
        core.events.say(f"{catalog.label(target)} failed to load: {reason}{suffix}", bad=True)
        self._record(LoadEvent(t=now_iso(), kind="load_failed", backend=target, reason=reason, device=device))

    def _on_engine_event(self, event: EngineEvent) -> None:
        """On the loop: a switch's eviction moves it to its load phase (the unloaded event, §4.8)."""
        if event.kind != "unloaded":
            return
        self._unloaded[event.backend] = event
        job = self._core.state.job
        if job.kind != "switch" or event.reason != "evicted":
            return
        freed = _gb(event.freed_bytes)
        self._set_job(phase="load", source="", started_at=self._now(), elapsed_s=0.0,
                      fill=self._start_fill(self._load_about))
        self._core.log.add(event_text(LoadEvent(t=now_iso(), kind="unloaded", backend=event.backend, gb=freed)))
        self._record(LoadEvent(t=now_iso(), kind="unloaded", backend=event.backend, gb=_round(freed),
                               peak_gb=_round(_gb(event.peak_bytes)), reason="switch",
                               device=event.device or "cuda"))

    # ---------- unload ----------

    def _unload_fields(self, backend: str) -> dict[str, Any]:
        return {**_IDLE, "kind": "unload", "source": backend, "started_at": self._now()}

    async def _unload(self, backend: str, *, idle: bool) -> None:
        core = self._core
        self._set_job(**self._unload_fields(backend))
        out = await core.facade.unload(backend, reason="idle" if idle else "requested")
        if out.ok:
            if out.value:
                self._unload_done(backend, idle)
            else:
                core.facade.resync()
        elif out.status in ("failed", "refused"):
            core.log.add(f"Free VRAM failed · {out.reason}")
            core.events.say(f"Couldn't free VRAM: {out.reason}", bad=True)

    def _unload_done(self, backend: str, idle: bool) -> None:
        core = self._core
        event = self._unloaded.pop(backend, None)
        freed = _gb(event.freed_bytes) if event is not None else None
        idle_s = self._idle_minutes * 60 if idle else None
        reason = "idle" if idle else "free"
        core.log.add(event_text(LoadEvent(t=now_iso(), kind="unloaded", backend=backend, gb=freed, reason=reason,
                                          seconds=idle_s)))
        text = f"Freed {freed:.1f} GB" if freed is not None else f"Unloaded {catalog.label(backend)}"
        core.events.say(text + (f" · idle {self._idle_minutes} min" if idle else ""))
        device = (event.device if event is not None else None) or "cuda"
        self._record(LoadEvent(t=now_iso(), kind="unloaded", backend=backend, gb=_round(freed),
                               peak_gb=_round(_gb(event.peak_bytes) if event is not None else None),
                               reason=reason, seconds=idle_s, device=device))

    # ---------- takes ----------

    def _take_fields(self, req: GenerateRequest) -> dict[str, Any]:
        core = self._core
        self._take_est = estimate_take(core.takes.takes, req.backend, req.text)
        return {"kind": "generate", "phase": "preparing", "source": "", "target": req.backend,
                "voice": req.voice_id, "take_no": core.takes.next_id, "queued": len(self._queue),
                "started_at": self._now(), "elapsed_s": 0.0, "audio_s": 0.0,
                "fill": self._start_fill(self._take_est)}

    async def _take(self, req: GenerateRequest) -> bool:
        core = self._core
        if self._stop_before_take:
            self._stop_before_take = False
            core.events.say(f"Stopped Take {core.state.job.take_no}")
            return True
        fields = self._take_fields(req)
        take_no = fields["take_no"]
        self._set_job(**fields)
        core.log.add(f"Generating take {take_no} · {req.voice_id} · {catalog.label(req.backend)}")
        job = Job()
        self._job = job
        try:
            out = await core.facade.speak(req, job)
            if out.status == "cancelled" or (out.ok and job.cancelled):
                core.events.say(f"Stopped Take {take_no}")
                return job.cancelled     # an explicit Stop may keep the waiting takes
            if not out.ok or out.value is None:
                self._queue.clear()
                core.log.add(f"Take {take_no} failed · {out.reason}")
                core.events.say(f"Take {take_no} failed: {out.reason}", bad=True)
                return False
            if job.conds_load_s is not None:
                core.log.add(f"Voice {req.voice_id} · conditioning cached · {job.conds_load_s:.2f} s")
            return await self._save_take(req, take_no, out.value, job)
        finally:
            self._job = None

    async def _save_take(self, req: GenerateRequest, take_no: int, wav: bytes, job: Job) -> bool:
        core = self._core
        status = job.status()
        made_s = job.made_s if job.made_s is not None else status.elapsed_s
        record = NewTake(voice=req.voice_id, backend=req.backend, language=req.language, text=req.text,
                         exaggeration=req.exaggeration, cfg_weight=req.cfg_weight, gen_seconds=round(made_s, 3),
                         session=core.session_id, tokens=status.tokens or None,
                         hit_token_limit=bool(status.hit_token_limit))
        folder = folder_path(core.settings.get("core", "takes_dir"), core.paths.root)
        problem: tuple[str, str] | None = None
        saved = None
        try:
            saved = await run.io_bound(_boxed, core.takes.add, wav, record, folder)
        except TakeNotListedError as exc:
            problem = (exc.reason, str(exc))
        except Exception as exc:
            reason = describe_error(exc)
            problem = (reason, f"Couldn't save Take {take_no}: {reason}")
        core.publish_takes()
        if problem is not None:
            self._queue.clear()
            core.log.add(f"Take {take_no} couldn't be saved · {problem[0]}")
            core.events.say(problem[1], bad=True)
            return False
        if saved is None:
            return False
        take = saved[0]
        core.log.add(f"Take {take.id} · {take.seconds:.1f} s audio in {made_s:.1f} s")
        if take.hit_token_limit:
            core.log.add(f"Take {take.id} hit the 40 s limit · the end of the script was cut off")
        self.quiet_take_id = take.id if job.cancelled else 0
        if job.cancelled:
            core.events.say(f"Take {take.id} was already done · kept")
        else:
            core.events.say(f"Take {take.id} ready · {take.seconds:.1f} s")
            core.call.route_saved(req, take.id, truncated=take.hit_token_limit)
        core.session.take_id = take.id
        core.events.notify()
        core.events.take_ready.emit(take.id)
        core.events.spark.emit("take")
        return True

    # ---------- forge ----------

    def _forge_fields(self, req: ForgeRequest) -> dict[str, Any]:
        core = self._core
        self._bake_est = core.history.bake_estimate(req.backend, core.state.engine.device or None)
        return {"kind": "forge", "phase": "bake", "source": "", "target": req.backend, "voice": req.voice_id,
                "take_no": 0, "queued": len(self._queue), "started_at": self._now(), "elapsed_s": 0.0,
                "audio_s": 0.0, "fill": self._start_fill(self._bake_est)}

    async def _forge(self, req: ForgeRequest) -> bool:
        core = self._core
        self._set_job(**self._forge_fields(req))
        core.log.add(f"Forging {req.voice_id} · {catalog.label(req.backend)}")
        core.events.forge_started.emit(req.voice_id)
        committed = await self._commit(req)
        if committed is None:
            return False
        path, created = committed
        job = Job()
        self._job = job
        out = await core.facade.bake(req, path, job)
        self._job = None
        if not out.ok:
            if created:
                await self._rollback(req, path)
            if out.status != "cancelled":
                self._forge_failed(req, out.reason)
            return False
        bake_s = job.bake_s if job.bake_s is not None else job.status().elapsed_s
        await self._forged(req, bake_s)
        return True

    async def _commit(self, req: ForgeRequest) -> tuple[Path, bool] | None:
        problem = ""
        done = None
        try:
            done = await run.io_bound(_boxed, self._core.clips.commit, req.clip, req.voice_id)
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            self._forge_failed(req, problem)
            return None
        return None if done is None else done[0]

    async def _rollback(self, req: ForgeRequest, path: Path) -> None:
        problem = ""
        try:
            await run.io_bound(self._core.clips.rollback, req.clip, path)
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            log.warning("Couldn't roll back the clip of %s: %s", req.voice_id, problem)

    def _forge_failed(self, req: ForgeRequest, reason: str) -> None:
        self._queue.clear()
        self._core.log.add(f"Forge failed: {req.voice_id} · {reason}")
        self._core.events.say(f"Forge failed: {reason}", bad=True)

    async def _forged(self, req: ForgeRequest, bake_s: float) -> None:
        core = self._core
        conds = folder_value(core.paths.cache / req.backend / f"{req.voice_id}.pt", core.paths.root)
        core.log.add(f"Cached {conds}")
        core.events.say(f"Forged {req.voice_id} · {catalog.label(req.backend)} · {bake_s:.1f} s")
        self._record(LoadEvent(t=now_iso(), kind="forged", backend=req.backend, seconds=round(bake_s, 2),
                               device=core.state.engine.device or "cuda"))
        await core.refresh_library()
        core.tokens.void_all()
        draft = core.forge_draft
        if draft.name == req.voice_id:
            draft.forged = req.voice_id
            draft.rev += 1
        core.events.forged.emit(req.voice_id)

    # ---------- test line ----------

    def _test_fields(self, req: GenerateRequest) -> dict[str, Any]:
        return {**_IDLE, "kind": "generate", "phase": "preparing", "target": req.backend, "voice": req.voice_id,
                "started_at": self._now()}

    async def _test_line(self, req: GenerateRequest, on_done: Callable[[str], Any] | None) -> None:
        core = self._core
        self._set_job(**self._test_fields(req))
        job = Job()
        self._job = job
        out = await core.facade.speak(req, job)
        self._job = None
        if out.status == "cancelled":
            return
        problem = "" if out.ok and out.value is not None else (out.reason or "no audio came back")
        if not problem:
            try:
                path = core.clips.test_line_path(req.voice_id)
                await run.io_bound(_write_file, path, out.value)
            except Exception as exc:
                problem = describe_error(exc)
        if problem:
            core.log.add(f"Test line failed · {problem}")
            core.events.say(f"Test line failed: {problem}", bad=True)
            return
        clip_id = f"test-{req.voice_id}"
        core.bump_staged(clip_id)
        if on_done is not None:
            try:
                on_done(core.staged_url(clip_id))
            except Exception as exc:
                text = describe_error(exc)
                log.warning("The test line callback failed: %s", text)

    # ---------- the ticker ----------

    def _tick(self) -> None:
        job = self._core.state.job
        if not job.kind:
            return
        elapsed = round(max(0.0, self._now() - job.started_at), 3)
        changes: dict[str, Any] = {"elapsed_s": elapsed}
        if job.kind == "switch" and job.phase == "load":
            changes["fill"] = _capped(elapsed, self._load_about, FILL_MAX)
        elif job.kind == "generate" and self._job is not None:
            status = self._job.status()
            if not self._job.cancelled:
                changes["phase"] = _TAKE_PHASES.get(status.phase, job.phase)
            changes["audio_s"] = round(status.audio_s, 3)
            if job.take_no > 0:
                changes["fill"] = self._take_fill(status, elapsed)
        elif job.kind == "forge":
            changes["fill"] = _capped(elapsed, self._bake_est, BAKE_FILL_MAX)
        self._set_job(**changes)

    def _take_fill(self, status: JobStatus, elapsed: float) -> float | None:
        est = self._take_est
        if est is None:
            return None
        if status.phase == "decoding":
            return FILL_MAX
        if status.tokens > 0 and est.audio_s > 0:
            return round(min(FILL_MAX, status.audio_s / est.audio_s), 4)
        return _capped(elapsed, est.made_s, FILL_MAX)

    # ---------- helpers ----------

    def _claim(self, fields: dict[str, Any]) -> None:
        """Hold the slot at once (the chain's first step writes the same fields again)."""
        self._set_job(**fields)
        if fields.get("kind") == "switch":
            self._core.facade.set_engine(failed="")

    def _set_job(self, **changes: Any) -> None:
        job = self._core.state.job
        kind = job.kind
        changed = False
        for name, value in changes.items():
            if getattr(job, name) != value:
                setattr(job, name, value)
                changed = True
        if job.kind != kind:
            job.rev += 1
        if changed:
            self._core.events.notify()

    def _record(self, event: LoadEvent) -> None:
        """Append one load-history event in a worker, in order, then mirror the history."""
        previous = self._history_tail
        self._history_tail = self._core.scheduler.spawn(self._append(event, previous), name="voicetool history")

    async def _append(self, event: LoadEvent, previous: asyncio.Task[Any] | None) -> None:
        if previous is not None and not previous.done():
            await asyncio.wait({previous})
        problem = ""
        try:
            await run.io_bound(self._core.history.append, event)
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            log.warning("Couldn't write the load history: %s", problem)
        self._core.publish_history()

    def _estimate_load(self, backend: str) -> float | None:
        return self._core.planner.load_estimate(backend).about_s

    def _start_fill(self, estimate: object | None) -> float | None:
        return 0.0 if estimate else None

    def _row(self, voice_id: str) -> VoiceRow | None:
        return next((row for row in self._core.state.library.rows if row.voice_id == voice_id), None)

    def _now(self) -> float:
        return self._core.scheduler.now()


def _test_request(row: VoiceRow) -> GenerateRequest:
    """The test line for a voice: Japanese for a ja voice, else "Hello, this is <name>." (§5.9)."""
    info = BACKENDS.get(row.backend)
    japanese = row.language == "ja" and not (info is not None and info.english_only)
    return GenerateRequest(
        voice_id=row.voice_id,
        backend=row.backend,
        text=TEST_LINE_JA if japanese else f"Hello, this is {row.voice_id}.",
        language="ja" if japanese else "en",
        exaggeration=row.exaggeration if info is None or info.uses_exaggeration else None,
        cfg_weight=0.5 if info is None or info.uses_cfg else None,
    )


def _write_file(path: Path, data: bytes) -> None:
    """Write bytes through <name>.part and os.replace (worker)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        part.write_bytes(data)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def _capped(elapsed: float, estimate: float | None, cap: float) -> float | None:
    """min(cap, elapsed / estimate), or None without an estimate (indeterminate)."""
    if not estimate or estimate <= 0:
        return None
    return round(min(cap, elapsed / estimate), 4)


def _gb(n_bytes: int | None) -> float | None:
    return None if n_bytes is None else n_bytes / GIB


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
