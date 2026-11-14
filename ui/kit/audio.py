"""Player: the shell's one hidden <audio> per client (vt_player.js), reconciled to a PlayerTarget.

The shell creates it once, outside ui.sub_pages, so playback survives navigation, binds its target
to PlaybackVM.target and passes PlaybackController.on_player_state as on_state.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, NamedTuple

from nicegui import binding
from nicegui.element import Element
from nicegui.events import GenericEventArguments

from ui.contract.plans import PlayerTarget

StateHandler = Callable[[bool, float, float, bool, str], Any]   # (playing, position, duration, ended, error)
MAX_ERROR_CHARS = 300


class PlayerReport(NamedTuple):
    """One `state` event from the browser, sanitised (seq None when the event carried none)."""

    seq: int | None
    playing: bool
    position: float
    duration: float
    ended: bool
    error: str


class Player(Element, component="vt_player.js"):
    """Plays what `target` says and reports the browser's state at most 4 times a second.

    The target is state, not commands: a new src loads and seeks to `at`; the same src with `at`
    set seeks; then it plays or pauses. A target whose seq was already applied changes nothing.
    Reports carry the seq of the target in force; one for an older target is dropped.
    The theme's take wave reads currentTime from the <audio> whose DOM id is `audio_id`.
    """

    target = binding.BindableProperty(on_change=lambda sender, value: sender._push_target(value))

    def __init__(self, target: PlayerTarget = PlayerTarget(), *, on_state: StateHandler | None = None) -> None:
        """
        :param target: what to play first (default: nothing)
        :param on_state: called with (playing, position, duration, ended, error) for every report
        """
        super().__init__()
        self._on_state = on_state
        self.target = _checked(target)          # first assignment: on_change does not run ...
        self._props["target"] = _target_props(target)   # ... so the initial prop is set here
        self.on("state", self._handle_state)    # listeners must exist before the element is first sent

    @property
    def audio_id(self) -> str:
        """DOM id of the <audio> element (the component's root)."""
        return self.html_id

    def bind_target_from(self, target_object: Any, target_name: str = "target") -> Player:
        """Follow target_object.<target_name> (a bindable property, so the link is never polled)."""
        binding.bind_from(self, "target", target_object, target_name)
        return self

    def _push_target(self, value: Any) -> None:
        self._props["target"] = _target_props(_checked(value))

    def _handle_state(self, event: GenericEventArguments) -> Any:
        report = parse_report(event.args)
        if report is None or self._on_state is None:
            return None
        if report.seq is not None and report.seq != self.target.seq:
            return None
        return self._on_state(report.playing, report.position, report.duration, report.ended, report.error)


def parse_report(args: Any) -> PlayerReport | None:
    """A `state` event's arguments as a PlayerReport; None when they are not a report."""
    if not isinstance(args, dict):
        return None
    seq, error = args.get("seq"), args.get("error")
    return PlayerReport(
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        playing=args.get("playing") is True,
        position=_seconds(args.get("position")),
        duration=_seconds(args.get("duration")),
        ended=args.get("ended") is True,
        error=error[:MAX_ERROR_CHARS] if isinstance(error, str) else "",
    )


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, float(value))


def _checked(value: Any) -> PlayerTarget:
    if not isinstance(value, PlayerTarget):
        raise TypeError(f"Player.target must be a PlayerTarget, not {type(value).__name__}")
    return value


def _target_props(target: PlayerTarget) -> dict[str, Any]:
    return {"seq": target.seq, "src": target.src, "playing": target.playing, "at": target.at, "muted": target.muted}
