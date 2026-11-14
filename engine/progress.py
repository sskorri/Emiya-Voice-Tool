"""Token progress and cooperative cancel for takes, through Chatterbox's two tqdm loops.

install() replaces `chatterbox.models.t3.t3.tqdm` (the speech-token sampling loops,
t3.py L338 and L435) and `chatterbox.models.s3gen.flow_matching.tqdm` (Turbo's flow
steps, flow_matching.py L240) with a hook. Inside a job bound to the current thread
the hook yields the same items, records progress and raises Cancelled at the next
step after Job.cancel(); outside a job it calls the original tqdm unchanged, so
cli.py keeps its progress bar. The active job is thread-local because NiceGUI's
run.io_bound does not copy contextvars. Importing this module imports no torch and
no chatterbox.
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from .errors import Cancelled

__all__ = [
    "PHASES",
    "TOKENS_PER_SECOND",
    "Cancelled",
    "Job",
    "JobStatus",
    "bind",
    "current_job",
    "install",
    "uninstall",
]

TOKENS_PER_SECOND = 25                                   # CB/models/s3tokenizer/s3tokenizer.py L18

PHASES = ("waiting", "preparing", "sampling", "decoding", "baking", "done", "cancelled", "failed")
_TERMINAL = frozenset({"done", "cancelled", "failed"})
_SAMPLING = "sampling"
_DECODING = "decoding"

# (module, phase) for every tqdm call site in Chatterbox 0.1.7.
_HOOK_SITES = (
    ("chatterbox.models.t3.t3", _SAMPLING),
    ("chatterbox.models.s3gen.flow_matching", _DECODING),
)


@dataclass(frozen=True)
class JobStatus:
    """A consistent snapshot of a job's progress."""

    phase: str            # one of PHASES
    tokens: int           # speech tokens sampled so far
    audio_s: float        # tokens / 25
    max_tokens: int | None
    hit_token_limit: bool # sampling used the whole bound without EOS: the end of the script was cut off
    elapsed_s: float      # since the Job was created; frozen once the job is done, cancelled or failed


class Job:
    """One engine call (a take or a bake): its progress and a cancel flag.

    ui.core creates it and passes it to speak()/load_voice(); the engine binds it to
    the worker thread. The engine writes the phase with `_set_phase()` and the timing
    attributes; the hook writes the loop fields. Every method is safe from any thread.
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._mx = threading.Lock()
        self._t0 = time.monotonic()
        self._t_end: float | None = None
        self._phase = "waiting"
        self._tokens = 0
        self._bound: int | None = None
        self._hit = False
        self._sampling_started: float | None = None
        self._sampling_ended: float | None = None
        self.bake_s: float | None = None
        self.sample_s: float | None = None
        self.decode_s: float | None = None
        self.made_s: float | None = None
        self.peak_reserved_bytes: int | None = None   # max_memory_reserved() after the take; never reset per take
        self.conds_load_s: float | None = None        # the voice's .pt load in this take; None = already active
        self.decode_steps = 0                         # flow-loop steps (Turbo only); never counted as tokens

    def cancel(self) -> None:
        """Ask the job to stop at its next cancel point. Idempotent, any thread."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        """Raise Cancelled when the job has been cancelled."""
        if self._cancel.is_set():
            raise Cancelled()

    def status(self) -> JobStatus:
        """Thread-safe snapshot."""
        with self._mx:
            end = self._t_end if self._t_end is not None else time.monotonic()
            return JobStatus(
                phase=self._phase,
                tokens=self._tokens,
                audio_s=self._tokens / TOKENS_PER_SECOND,
                max_tokens=self._bound,
                hit_token_limit=self._hit,
                elapsed_s=end - self._t0,
            )

    # ---- engine side ----

    def _set_phase(self, phase: str) -> None:
        """Set the phase. "done" also fills decode_s (end of sampling until now)."""
        if phase not in PHASES:
            raise ValueError(f"Unknown job phase {phase!r}; expected one of {PHASES}")
        now = time.monotonic()
        with self._mx:
            self._phase = phase
            if phase not in _TERMINAL:
                return
            self._t_end = now
            if phase == "done" and self.decode_s is None and self._sampling_ended is not None:
                self.decode_s = now - self._sampling_ended

    # ---- hook side: one setter family for both loop sites ----

    def _loop_started(self, phase: str, bound: int | None) -> None:
        now = time.monotonic()
        with self._mx:
            if phase == _SAMPLING:
                self._tokens = 0
                self._bound = bound
                self._hit = False
                self._sampling_started = now
            if self._phase not in _TERMINAL:
                self._phase = phase

    def _loop_step(self, phase: str, step: int) -> None:
        with self._mx:
            if phase == _SAMPLING:
                self._tokens = step
            else:
                self.decode_steps += 1

    def _loop_ended(self, phase: str, exhausted: bool) -> None:
        """End of a loop. Only the sampling loop sets hit_token_limit and moves on to decoding."""
        if phase != _SAMPLING:
            return
        now = time.monotonic()
        with self._mx:
            self._hit = exhausted
            self._sampling_ended = now
            if self._sampling_started is not None:
                self.sample_s = now - self._sampling_started
            # The generator can be closed late (an exception kept its frame alive), so
            # never move a job that already finished back to "decoding".
            if self._phase == _SAMPLING and not self._cancel.is_set():
                self._phase = _DECODING


_tls = threading.local()


def current_job() -> Job | None:
    """The job bound to this thread, or None."""
    return getattr(_tls, "job", None)


class bind:
    """`with bind(job):` makes `job` this thread's current job for the block (None unbinds)."""

    def __init__(self, job: Job | None) -> None:
        self.job = job
        self._previous: Job | None = None

    def __enter__(self) -> Job | None:
        self._previous = current_job()
        _tls.job = self.job
        return self.job

    def __exit__(self, *exc_info: object) -> None:
        _tls.job = self._previous


def _hooked_loop(job: Job, phase: str, iterable: Iterable[Any], bound: int | None) -> Iterator[Any]:
    """Yield the same items as `iterable`, recording each step; raise Cancelled before a step."""
    exhausted = False
    job._loop_started(phase, bound)
    try:
        for step, item in enumerate(iterable, 1):
            job.check()                       # cancel point, before every model step
            job._loop_step(phase, step)
            yield item                        # the same items: t3.py L372 uses them for positions
        exhausted = True
    finally:                                  # EOS break (generator closed), exhaustion and Cancelled
        job._loop_ended(phase, exhausted)


def _loop_bound(iterable: Iterable[Any], total: Any) -> int | None:
    if total is None:
        try:
            total = len(iterable)             # type: ignore[arg-type]  # range(1000)
        except TypeError:
            return None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _wrap(original: Callable[..., Any], phase: str) -> Callable[..., Any]:
    """A drop-in for `original` (tqdm) that hooks the loop while a job is bound."""

    def tqdm_or_hook(iterable: Iterable[Any] | None = None, *args: Any, **kwargs: Any) -> Any:
        job = current_job()
        if job is None or iterable is None:
            return original(iterable, *args, **kwargs)        # cli.py / app.py: the usual bar
        return _hooked_loop(job, phase, iterable, _loop_bound(iterable, kwargs.get("total")))

    tqdm_or_hook.voicetool_original = original  # type: ignore[attr-defined]
    return tqdm_or_hook


_install_lock = threading.Lock()


def install() -> None:
    """Patch both Chatterbox loop modules once. Idempotent; imports chatterbox (no model).

    VoiceEngine calls it right after a backend is built, when chatterbox is imported anyway.
    """
    modules = [(importlib.import_module(name), phase) for name, phase in _HOOK_SITES]
    with _install_lock:
        for module, phase in modules:
            if not hasattr(module.tqdm, "voicetool_original"):
                module.tqdm = _wrap(module.tqdm, phase)


def uninstall() -> None:
    """Restore the original tqdm on both modules (tests only). Imports nothing."""
    with _install_lock:
        for name, _ in _HOOK_SITES:
            module = sys.modules.get(name)
            original = getattr(getattr(module, "tqdm", None), "voicetool_original", None)
            if original is not None:
                module.tqdm = original
