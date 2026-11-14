"""Requests, plans and confirmations as data: the behaviours decide, a theme only draws them (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ui.contract.records import ClipInfo

SwitchReason = Literal["explicit", "generate", "forge", "retry", "warmup"]
ConfirmKind = Literal["switch_model", "delete_voice", "replace_voice", "purge_takes", "clear_script", "quit_busy"]
ConfirmStyle = Literal["named", "arm", "ask"]
ActionTone = Literal["primary", "plain", "grey", "danger", "link"]


@dataclass(frozen=True)
class GenerateRequest:
    """One take, snapshotted when Generate is clicked (D2); a queued take never reads live state."""

    voice_id: str
    backend: str
    text: str
    language: str                   # "en" for English-only voices
    exaggeration: float | None      # None for Turbo
    cfg_weight: float | None        # None for Turbo
    call_epoch: int | None = None   # explicit auto-send intent, valid until calls are disabled


@dataclass(frozen=True)
class ForgeRequest:
    """One forge, snapshotted when Forge is pressed; the running request is immutable."""

    voice_id: str
    clip: ClipInfo
    backend: str
    language: str                   # code; "en" for Standard and Turbo
    exaggeration: float             # 0.25..2.0 rounded to 0.01; forced to 0.5 for Turbo
    consent: bool                   # must be True
    replace: bool = False           # the name exists on this backend and the replace arm was pressed twice


@dataclass(frozen=True)
class AfterGenerate:
    """Takes that run right after a switch (the chain [switch, takes])."""

    reqs: tuple[GenerateRequest, ...]


@dataclass(frozen=True)
class AfterForge:
    """The forge that runs right after a switch (the chain [switch, forge])."""

    req: ForgeRequest


@dataclass(frozen=True)
class SwitchPlan:
    """What loading to_backend would do, from the mirrors and the load history (Planner.switch_plan)."""

    to_backend: str
    from_backend: str | None        # EngineState.resident, or None
    reason: SwitchReason
    label: str                      # "Switch to Turbo & Generate", "Load This Model", "Retry", ...
    needs_bytes: int                # Planner.needs_bytes()
    needs_estimated: bool           # False when measured by an earlier load on this device
    free_after_bytes: int | None    # free VRAM once the resident model is gone; None = not detected yet
    low_vram: bool                  # CUDA and free_after_bytes < needs_bytes + 0.5 GiB: always an ask (D1)
    about_s: float | None = None    # load estimate from the history; None = no history
    last_s: float | None = None     # the last measured load of this backend on this device
    after: AfterGenerate | AfterForge | None = None


@dataclass(frozen=True)
class ConfirmRequest:
    """A confirmation the theme must render: a named control, an arm (click twice) or an ask (popover)."""

    token: str                      # the switch token this request redeems; "" when it carries none (arms)
    kind: ConfirmKind
    style: ConfirmStyle
    title: str = ""                 # "Switch to Turbo"
    lines: tuple[tuple[str, str], ...] = ()   # (text, tone) with tone "text-soft" | "muted" | "warn"
    primary: str = ""               # "Switch Model", "Load Anyway", "Click Again to Delete"
    danger: bool = False
    switch: SwitchPlan | None = None


@dataclass(frozen=True)
class ActionState:
    """Everything a theme needs to draw one button."""

    enabled: bool
    label: str
    reason: str = ""                        # why disabled
    pending: ConfirmRequest | None = None   # style "named": the token rides on the control
    aria: str = ""                          # accessible name when the visible label does not name the model
    tone: ActionTone = "primary"
    fill: float | None = None               # gold fill 0..1 (None = no fill)
    indeterminate: bool = False             # sweep instead of a fill
    armed: bool = False
    tooltip: str = ""
    hint: tuple[str, ...] = ()              # key hint chips, for example ("Ctrl", "Enter")


@dataclass(frozen=True)
class PlayerTarget:
    """What the shell's one audio player should do; the player reconciles to it (state, not commands)."""

    seq: int = 0                    # bumps on every change, so the same target can be replayed
    src: str = ""                   # media URL; "" = nothing loaded
    playing: bool = False
    at: float | None = None         # seek here (seconds); None = keep the position
    muted: bool = False             # follow the sent WAV silently when Hear It is off


@dataclass(frozen=True)
class Toast:
    """One toast; a newer seq replaces the shown one."""

    text: str
    bad: bool = False
    seq: int = 0
