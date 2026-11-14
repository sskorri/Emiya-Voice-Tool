"""Switch tokens and load tickets (§4.7): the third layer of the one-model rule (§4.5).

A named control or an ask carries a token minted for one SwitchPlan. Redeeming it once, while the world
revision (EngineState.rev, JobState.rev, LibraryState.rev) is the one it was minted in, yields a
LoadTicket, the only object EngineFacade.load() accepts. A low-VRAM plan redeems only from an ask
(asked=True), and redeem re-checks VRAM, because DeviceState is not part of the world revision.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ui.contract.plans import SwitchPlan

if TYPE_CHECKING:
    from ui.contract.state import AppState

    from .planner import Planner

__all__ = ["LoadTicket", "TokenStore"]

_ISSUER = object()                       # module-private: only TokenStore constructs tickets


@dataclass(frozen=True)
class LoadTicket:
    """Permission to load plan.to_backend once; issued by TokenStore.redeem() only."""

    plan: SwitchPlan
    serial: int
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError("a LoadTicket is issued only by TokenStore.redeem()")


@dataclass(frozen=True)
class _Minted:
    plan: SwitchPlan
    asked: bool
    world: tuple[int, int, int]


class TokenStore:
    """Mints single-use switch tokens and the tickets they redeem to."""

    def __init__(self, state: AppState, planner: Planner) -> None:
        self._state = state
        self._planner = planner
        self._tokens: dict[str, _Minted] = {}
        self._tickets: dict[int, LoadTicket] = {}
        self._serials = itertools.count(1)

    def world(self) -> tuple[int, int, int]:
        """The revision a token is valid in: any residency, job-kind or library change voids it."""
        state = self._state
        return state.engine.rev, state.job.rev, state.library.rev

    def mint(self, plan: SwitchPlan, *, asked: bool = False) -> str:
        """A new token (uuid4 hex) for plan; asked=True when it rides on a confirmed ask."""
        world = self.world()
        self._tokens = {token: minted for token, minted in self._tokens.items() if minted.world == world}
        token = uuid.uuid4().hex
        self._tokens[token] = _Minted(plan, bool(asked), world)
        return token

    def peek(self, token: str) -> SwitchPlan | None:
        """The plan of a token that is still valid, else None; the token stays."""
        minted = self._tokens.get(token)
        if minted is None or minted.world != self.world():
            return None
        return minted.plan

    def redeem(self, token: str, *, expect_to: str | None = None) -> LoadTicket | None:
        """Use a token once; None when void, unknown, for another backend, or a low-VRAM plan not asked."""
        minted = self._tokens.pop(token, None) if isinstance(token, str) else None
        if minted is None or minted.world != self.world():
            return None
        plan = minted.plan
        if expect_to is not None and expect_to != plan.to_backend:
            return None
        if not minted.asked and (plan.low_vram or self._planner.low_vram(plan.to_backend)):
            return None
        ticket = LoadTicket(plan, next(self._serials), _ISSUER)
        self._tickets[ticket.serial] = ticket
        return ticket

    def spend(self, ticket: object) -> bool:
        """True exactly once for a ticket this store issued (EngineFacade.load)."""
        if not isinstance(ticket, LoadTicket):
            return False
        issued = self._tickets.get(ticket.serial)
        if issued is not ticket:
            return False
        del self._tickets[ticket.serial]
        return True

    def void_all(self) -> None:
        """Void every outstanding token (a click with one then loads nothing)."""
        self._tokens.clear()

    @property
    def outstanding(self) -> int:
        """Tokens minted in the current world revision and not redeemed yet."""
        world = self.world()
        return sum(1 for minted in self._tokens.values() if minted.world == world)
