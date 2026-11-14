"""PlaybackController (§5.4): the client's one player, the current take, A/B pins, stars, audition, Save As.

The shell's kit Player follows vm.target, a PlayerTarget (state, not commands): every change bumps seq; a
new src loads and seeks to `at`, the same src with `at` seeks, then it plays or pauses. The Player reports
back at most 4 times a second (on_player_state). Takes play from core.take_url(id), so the take wave can
tell that the shared <audio> plays its take. Session.take_id and the pins are process-wide (Session);
what is loaded, the play state and the position belong to this client.

D19: while a take generates, the Speak card stays on the current take when that take was playing at the
moment the generating take started (pinned()); take_ready then selects the new take, playing it from 0
when Autoplay New Takes is on.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ui.contract.plans import PlayerTarget
from ui.contract.viewmodels import PlaybackController as PlaybackControllerProtocol
from ui.contract.viewmodels import PlaybackVM
from ui.core.facade import describe_error
from ui.core.services.settings import CORE
from ui.core.services.takes_store import take_file_name

from .confirm import Listeners, Subscriptions

if TYPE_CHECKING:
    from ui.contract.records import Take

    from . import BehaviourFactory

__all__ = ["END_SLACK_S", "NO_TIME", "WAV_FILTERS", "Loaded", "PlaybackController", "clock", "time_text"]

WAV_FILTERS: tuple[str, ...] = ("WAV audio (*.wav)",)
END_SLACK_S = 0.05                        # Play at the end restarts from 0 (M L1675)
NO_TIME = "0:00.0 / 0:00.0"               # M L3024

SourceKind = Literal["", "take", "clip", "url"]


def clock(seconds: float) -> str:
    """m:ss.s, rounded to tenths first (59.96 s reads 1:00.0), like the take wave's clock() (M L1424-1428)."""
    value = seconds if isinstance(seconds, (int, float)) and math.isfinite(seconds) else 0.0
    tenths = max(0, math.floor(value * 10 + 0.5))
    minutes, rest = divmod(tenths, 600)
    return f"{minutes}:{rest // 10:02d}.{rest % 10}"


def time_text(position: float, length: float) -> str:
    """"0:01.9 / 0:04.6"."""
    return f"{clock(min(position, length) if length > 0 else position)} / {clock(length)}"


@dataclass(frozen=True)
class Loaded:
    """What the client's player holds: a take (key = take id), a reference clip (voice id) or a URL."""

    kind: SourceKind = ""
    key: int | str = ""


class PlaybackController(PlaybackControllerProtocol):
    """Current take, play and pause, A/B, pins, stars, audition, Save As, reveal (§5.4); vm: PlaybackVM."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self.vm = PlaybackVM()
        self.listeners = Listeners()          # pages redraw their play state after every change
        self._seq = 0
        self._loaded = Loaded()
        self._resume: tuple[int, float] = (0, 0.0)   # the last take loaded and its position
        self._anchor = 0.0                    # the position at the last command (the paused time text)
        self._error_seq = 0
        self._monitor_id = 0
        self._monitor_started_at = 0.0
        self._window_visible = self._core.state.window.visible
        self._pin_for = 0
        self._pinned = False
        self._save_dir: Path | None = None
        self._saving = False
        self._subs = Subscriptions()
        self._subs.add(self._core.events.take_ready, self._on_take_ready)
        self._subs.add(self._core.events.call_started, self._on_call_started)
        self._subs.add(self._core.events.call_ended, self._on_call_ended)
        self._subs.add(self._core.settings.changed, self._on_setting)
        self._subs.add(self._core.events.changed, self._on_changed)
        self._refresh_call()

    # ---------- what is playing ----------

    @property
    def loaded(self) -> Loaded:
        return self._loaded

    def current(self) -> Take | None:
        """The current take (Session.take_id), or None."""
        return self._take(self._core.session.take_id)

    def session_takes(self) -> tuple[Take, ...]:
        """"Takes · This Session": the takes of this run, newest first (D14)."""
        session_id = self._core.session_id
        return tuple(take for take in self._core.state.takes.takes if take.session == session_id)

    def is_playing_take(self, take_id: int) -> bool:
        return self.vm.playing and self._loaded == Loaded("take", take_id)

    def is_playing_clip(self, voice_id: str) -> bool:
        return self.vm.playing and self._loaded == Loaded("clip", voice_id)

    def is_playing_url(self, url: str) -> bool:
        return self.vm.playing and self._loaded == Loaded("url", url)

    def position_of(self, take_id: int) -> float:
        """Where that take is: live while it is loaded, else where it was left, else 0."""
        if self._loaded == Loaded("take", take_id):
            return self.vm.position
        return self._resume[1] if self._resume[0] == take_id else 0.0

    def time_text(self, take_id: int | None = None) -> str:
        """The card's "0:01.9 / 0:04.6" for a take (default: the current one).

        While that take plays, the text keeps its value from the last command: the take wave writes the
        live text into the card (§6.5), so Python never fights it.
        """
        take = self._take(take_id) if take_id else self.current()
        if take is None:
            return NO_TIME
        position = self._anchor if self.is_playing_take(take.id) else self.position_of(take.id)
        return time_text(position, take.seconds)

    def pinned(self) -> bool:
        """D19: a take generates and the card stays on the take that was playing when it started."""
        job = self._core.state.job
        if job.kind != "generate" or job.take_no <= 0:
            self._pin_for, self._pinned = 0, False
            return False
        if self._pin_for != job.take_no:
            current = self._core.session.take_id
            self._pin_for = job.take_no
            self._pinned = bool(current) and self.is_playing_take(current)
        return self._pinned

    def card_generating(self) -> bool:
        """The Speak card shows the generating state (a take generates and the card is not pinned)."""
        job = self._core.state.job
        return job.kind == "generate" and job.take_no > 0 and not self.pinned()

    # ---------- takes ----------

    def send_take(self, take_id: int) -> None:
        self._core.call.send(take_id)

    def stop_sending(self) -> None:
        self._core.call.stop()

    def play_take(self, take_id: int) -> None:
        """Make the take current and play it from 0 (a missing WAV is a red toast)."""
        take = self._take(take_id)
        if take is None:
            return
        if self._wav_missing(take):
            return
        self._select(take.id)
        self._load_take(take, playing=True, at=0.0)

    def toggle(self) -> None:
        """Pause; or play the current take, restarting from 0 at its end."""
        take = self.current()
        if take is None:
            return
        if self._loaded == Loaded("take", take.id):
            if self.vm.playing:
                self._command(playing=False, at=None)
            else:
                self._command(playing=True, at=0.0 if self._at_end(self.vm.position, take) else None)
            return
        if self._wav_missing(take):
            return
        position = self.position_of(take.id)
        self._load_take(take, playing=True, at=0.0 if self._at_end(position, take) else position)

    def seek(self, seconds: float) -> None:
        """Move the current take to seconds (the play state stays)."""
        take = self.current()
        if take is None or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            return
        at = min(max(0.0, float(seconds)), take.seconds)
        self._load_take(take, playing=self.is_playing_take(take.id), at=at)

    def ab_switch(self, take_id: int) -> None:
        """Select that take, keeping the position (clamped to its length) and the play state (M L1665)."""
        take = self._take(take_id)
        if take is None or self._wav_missing(take):
            return
        current = self.current()
        position = self.position_of(current.id) if current is not None else 0.0
        playing = current is not None and self.is_playing_take(current.id)
        self._select(take.id)
        self._load_take(take, playing=playing, at=min(position, take.seconds))

    def pin(self, take_id: int, side: Literal["A", "B"]) -> None:
        """Toggle a pin; a take is never both A and B (M L1679-1688)."""
        session = self._core.session
        if side not in ("A", "B"):
            return
        mine, other = ("pin_a", "pin_b") if side == "A" else ("pin_b", "pin_a")
        on = getattr(session, mine) != take_id
        if on and self._take(take_id) is None:
            return
        setattr(session, mine, take_id if on else 0)
        if on and getattr(session, other) == take_id:
            setattr(session, other, 0)
        self._core.events.notify()

    def star(self, take_id: int) -> None:
        """Toggle the star, saved in click order through the core's take edits (a toggle starts from the value
        asked last, so a double click toggles twice); a failure is the red toast "Couldn't save the star: …"."""
        take = self._take(take_id)
        if take is None:
            return
        edits = self._core.edits
        edits.star(take.id, not edits.wanted(take.id, take.starred))

    def prev(self) -> None:
        """Play the newer row of "Takes · This Session"."""
        self._step(-1)

    def next(self) -> None:
        """Play the older row of "Takes · This Session"."""
        self._step(1)

    # ---------- clips ----------

    def audition(self, voice_id: str) -> None:
        """Play the voice's reference clip (a take pauses); a second call stops it."""
        if self.is_playing_clip(voice_id):
            self.stop()
            return
        row = next((r for r in self._core.state.library.rows if r.voice_id == voice_id), None)
        path = row.reference_path if row is not None else None
        if row is None or path is None or not Path(path).is_file():
            self._core.events.say(f"Couldn't play the reference clip of {voice_id}: file not found", bad=True)
            return
        self._target(self._core.clip_url(voice_id), playing=True, at=0.0, loaded=Loaded("clip", voice_id),
                     duration=row.clip_s or 0.0)

    def play_url(self, url: str) -> None:
        """Play a staged clip or a test line from 0."""
        if url:
            self._target(url, playing=True, at=0.0, loaded=Loaded("url", url))

    def stop(self) -> None:
        """Stop what plays: a clip or URL rewinds to 0, a take pauses where it is."""
        if not self.vm.target.src:
            return
        self._command(playing=False, at=None if self._loaded.kind == "take" else 0.0)

    # ---------- the player's reports ----------

    def on_player_state(self, playing: bool, position: float, duration: float, ended: bool, error: str) -> None:
        """A report of the current target (stale ones are dropped by the Player)."""
        vm = self.vm
        if ended:
            playing = False
            position = duration if duration > 0 else position
        if duration > 0:
            vm.duration = float(duration)
        vm.position = max(0.0, float(position))
        vm.playing = bool(playing) and not error
        if self._loaded.kind == "take":
            self._resume = (int(self._loaded.key), vm.position)
        if not vm.playing:
            self._anchor = vm.position
        if error and self._error_seq != self._seq:
            self._error_seq = self._seq
            self._core.events.say(f"Couldn't play {self._what()}: {error}", bad=True)
        self.listeners.notify()

    # ---------- files ----------

    async def save_as(self) -> None:
        """Save As… for the current take: the native SAVE dialog, then a copy and "Saved aiko_take7.wav"."""
        take = self.current()
        if take is None or self.card_generating() or self._saving or self._wav_missing(take):
            return
        self._saving = True
        try:
            dest = await self._factory.kit.save_as(default_name=take_file_name(take), filters=WAV_FILTERS,
                                                   start_dir=self._save_dir)
            if dest is not None:
                await self._copy(take, Path(dest))
        finally:
            self._saving = False

    def reveal(self) -> None:
        """Show the current take's WAV in Explorer (a missing file is a red toast)."""
        take = self.current()
        if take is None or self.card_generating() or self._wav_missing(take):
            return
        path = self._core.takes.path_of(take.id)
        if path is None:
            return
        try:
            self._factory.kit.reveal(path)
        except OSError as exc:
            self._core.events.say(f"Couldn't show {path.name} in Explorer: {describe_error(exc)}", bad=True)

    def close(self) -> None:
        self._core.call.release_monitor(self)
        self._subs.close()

    # ---------- plumbing ----------

    def _take(self, take_id: int | None) -> Take | None:
        if not isinstance(take_id, int) or isinstance(take_id, bool) or take_id <= 0:
            return None
        return self._core.takes.get(take_id)

    def _select(self, take_id: int) -> None:
        session = self._core.session
        if session.take_id != take_id:
            session.take_id = take_id
            self._core.events.notify()

    def _load_take(self, take: Take, *, playing: bool, at: float | None) -> None:
        self._target(self._core.take_url(take.id), playing=playing, at=at, loaded=Loaded("take", take.id),
                     duration=take.seconds)

    def _command(self, *, playing: bool, at: float | None) -> None:
        """Play or pause what is loaded, optionally seeking."""
        self._target(self.vm.target.src, playing=playing, at=at, loaded=self._loaded, duration=self.vm.duration)

    def _target(self, src: str, *, playing: bool, at: float | None, loaded: Loaded, duration: float = 0.0,
                monitor_id: int = 0, muted: bool = False) -> None:
        vm = self.vm
        self._monitor_id = monitor_id
        previous = vm.target.src
        self._seq += 1
        vm.target = PlayerTarget(seq=self._seq, src=src, playing=bool(playing and src), at=at, muted=muted)
        if src != previous:
            vm.duration = float(duration)
            vm.position = at or 0.0
        elif at is not None:
            vm.position = at
        vm.playing = bool(playing and src)
        self._loaded = loaded if src else Loaded()
        self._anchor = vm.position
        if self._loaded.kind == "take":
            self._resume = (int(self._loaded.key), vm.position)
        self.listeners.notify()

    def _at_end(self, position: float, take: Take) -> bool:
        return position >= take.seconds - END_SLACK_S

    def _step(self, direction: int) -> None:
        ids = [take.id for take in self.session_takes()]
        if not ids:
            return
        current = self._core.session.take_id
        index = ids.index(current) + direction if current in ids else 0
        if 0 <= index < len(ids) and ids[index] != current:
            self.play_take(ids[index])

    def _wav_missing(self, take: Take) -> bool:
        """True (with the red toast "Take 7's WAV is missing: <path>") when the take's WAV is gone."""
        path = self._core.takes.path_of(take.id)
        if not take.missing and path is not None and path.is_file():
            return False
        self._core.events.say(f"Take {take.id}'s WAV is missing: {path or take.wav}", bad=True)
        return True

    def _what(self) -> str:
        kind, key = self._loaded.kind, self._loaded.key
        if kind == "take":
            return f"Take {key}"
        if kind == "clip":
            return f"the reference clip of {key}"
        return "the clip"

    async def _copy(self, take: Take, dest: Path) -> None:
        problem = ""
        try:
            await asyncio.to_thread(self._core.takes.copy_to, take.id, dest)
        except Exception as exc:
            problem = describe_error(exc)
        if problem:
            self._core.events.say(f"Couldn't save {dest.name}: {problem}", bad=True)
            return
        self._save_dir = dest.parent
        self._core.events.say(f"Saved {dest.name}")

    # ---------- events ----------

    def _on_take_ready(self, take_id: int) -> None:
        """A new take is current (the core set Session.take_id): load it, playing it when Autoplay is on."""
        take = self._take(take_id)
        if take is not None:
            if self._core.call.handles_monitor(take_id):
                return
            play = self._core.jobs.quiet_take_id != take_id and self._core.settings.get(CORE, "autoplay")
            self._load_take(take, playing=bool(play), at=0.0)

    def _on_call_started(self, take_id: int) -> None:
        take = self._take(take_id)
        if take is None:
            return
        self._select(take_id)
        owner = self._core.call.claim_monitor(take_id, self)
        self._monitor_started_at = self._core.scheduler.now()
        self._target(self._core.take_url(take.id), playing=True, at=0.0, loaded=Loaded("take", take.id),
                     duration=take.seconds, monitor_id=take.id,
                     muted=not (owner and self._core.settings.get(CORE, "call_hear")))

    def _on_call_ended(self, take_id: int) -> None:
        if self._monitor_id == take_id:
            self._command(playing=False, at=None)

    def _on_setting(self, section: str, key: str) -> None:
        if section == CORE and key.startswith("call_"):
            self._refresh_call()
        if section == CORE and key == "call_hear" and self._monitor_id:
            owner = self._core.call.claim_monitor(self._monitor_id, self)
            self._target(self.vm.target.src, playing=self.vm.playing, at=None, loaded=self._loaded,
                         duration=self.vm.duration, monitor_id=self._monitor_id,
                         muted=not (owner and self._core.settings.get(CORE, "call_hear")))

    def _on_changed(self) -> None:
        self.pinned()                       # latch the D19 decision the moment a take starts
        self._refresh_call()
        visible = self._core.state.window.visible
        restored = visible and not self._window_visible
        self._window_visible = visible
        if restored and self._monitor_id and self._core.call.phase == "sending":
            # WebView2 may suspend muted media while minimized. Rejoin the running call on restore.
            elapsed = min(self.vm.duration, max(0.0, self._core.scheduler.now() - self._monitor_started_at))
            self._target(self.vm.target.src, playing=True, at=elapsed, loaded=self._loaded,
                         duration=self.vm.duration, monitor_id=self._monitor_id, muted=self.vm.target.muted)

    def _refresh_call(self) -> None:
        call, vm, settings = self._core.call, self.vm, self._core.settings
        vm.call_enabled = settings.get(CORE, "call_enabled")
        vm.call_available = call.available
        vm.call_active = vm.call_enabled and call.phase != "idle"
        vm.call_hear = settings.get(CORE, "call_hear")
        vm.call_switch_visible = vm.call_enabled and settings.get(CORE, "call_send") == "switch"
        labels = {"delayed": "Waiting to send", "opening": "Opening call device for",
                  "sending": "Sending", "stopping": "Stopping"}
        vm.call_status = f"{labels[call.phase]} Take {call.take_id}" if call.phase in labels else call.error
