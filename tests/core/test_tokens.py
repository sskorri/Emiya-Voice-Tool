"""TokenStore and LoadTicket (§4.7): single use, voided by any world change, expect_to, the low-VRAM rules."""
from __future__ import annotations

from collections.abc import Callable

import pytest

from ui.contract.plans import SwitchPlan
from ui.core.tokens import LoadTicket, TokenStore

from tests.core.test_facade import GIB, MTL, STD, TUR, Rig, rigs, speak  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def plan(rig: Rig, backend: str = TUR, reason: str = "explicit") -> SwitchPlan:
    return rig.core.planner.switch_plan(backend, reason=reason)  # type: ignore[arg-type]


def make_low(rig: Rig) -> None:
    """Another program takes almost all VRAM: free after the unload < needs + 0.5 GiB."""
    device = rig.state.device
    device.free_bytes = 0
    device.app_bytes = 1 * GIB


async def test_a_token_redeems_once(speak: Rig) -> None:
    tokens = speak.core.tokens
    token = tokens.mint(plan(speak))
    assert len(token) == 32 and int(token, 16) >= 0
    assert tokens.peek(token) == plan(speak)
    ticket = tokens.redeem(token)
    assert isinstance(ticket, LoadTicket) and ticket.plan.to_backend == TUR
    assert tokens.redeem(token) is None
    assert tokens.peek(token) is None


async def test_unknown_and_foreign_tokens_redeem_to_nothing(speak: Rig) -> None:
    tokens = speak.core.tokens
    assert tokens.redeem("0" * 32) is None
    assert tokens.redeem(None) is None  # type: ignore[arg-type]
    assert tokens.peek("nope") is None


@pytest.mark.parametrize("mirror", ["engine", "job", "library"])
async def test_any_world_change_voids_every_token(speak: Rig, mirror: str) -> None:
    tokens = speak.core.tokens
    first, second = tokens.mint(plan(speak, TUR)), tokens.mint(plan(speak, STD))
    assert tokens.outstanding == 2
    getattr(speak.state, mirror).rev += 1
    assert tokens.outstanding == 0
    assert tokens.peek(first) is None
    assert tokens.redeem(first) is None and tokens.redeem(second) is None


async def test_a_real_library_change_voids_tokens(speak: Rig) -> None:
    core = speak.core
    token = core.tokens.mint(plan(speak))
    assert (await core.facade.update_voice("marco", language="es")).ok
    await core.refresh_library()
    assert core.tokens.redeem(token) is None


async def test_a_job_start_voids_tokens(speak: Rig) -> None:
    core = speak.core
    token = core.tokens.mint(plan(speak))
    assert core.jobs.start_free().status == "started"
    assert core.tokens.redeem(token) is None
    await speak.settle()


async def test_expect_to_must_match_the_plan(speak: Rig) -> None:
    tokens = speak.core.tokens
    token = tokens.mint(plan(speak, TUR))
    assert tokens.redeem(token, expect_to=STD) is None
    assert tokens.redeem(token, expect_to=TUR) is None          # single use: the mismatch spent it
    assert tokens.redeem(tokens.mint(plan(speak, TUR)), expect_to=TUR) is not None


async def test_a_low_vram_plan_redeems_only_from_an_ask(speak: Rig) -> None:
    make_low(speak)
    low = plan(speak)
    assert low.low_vram
    tokens = speak.core.tokens
    assert tokens.redeem(tokens.mint(low)) is None
    ticket = tokens.redeem(tokens.mint(low, asked=True))
    assert ticket is not None and ticket.plan.low_vram


async def test_vram_is_checked_again_at_redeem(speak: Rig) -> None:
    tokens = speak.core.tokens
    fine = plan(speak)
    assert not fine.low_vram
    named, asked = tokens.mint(fine), tokens.mint(fine, asked=True)
    make_low(speak)                                              # DeviceState is not in the world revision
    assert tokens.peek(named) == fine
    assert tokens.redeem(named) is None
    assert tokens.redeem(asked) is not None


async def test_void_all(speak: Rig) -> None:
    tokens = speak.core.tokens
    token = tokens.mint(plan(speak))
    tokens.void_all()
    assert tokens.outstanding == 0 and tokens.redeem(token) is None


async def test_minting_forgets_tokens_of_an_older_world(speak: Rig) -> None:
    tokens = speak.core.tokens
    tokens.mint(plan(speak))
    speak.state.engine.rev += 1
    tokens.mint(plan(speak))
    assert len(tokens._tokens) == 1


async def test_tickets_come_only_from_their_store_and_are_spent_once(speak: Rig,
                                                                     rigs: Callable[..., Rig]) -> None:
    tokens = speak.core.tokens
    with pytest.raises(TypeError):
        LoadTicket(plan(speak), 1)
    ticket = tokens.redeem(tokens.mint(plan(speak)))
    assert ticket is not None
    other = rigs("speak")
    other_store = TokenStore(other.state, other.core.planner)
    assert other_store.spend(ticket) is False
    assert tokens.spend("ticket") is False
    assert tokens.spend(ticket) is True
    assert tokens.spend(ticket) is False
