"""ModelsController (§5.3): switch requests, Free VRAM, the Behavior settings and the switch popover.

Switch requests come from a dock row, a Models card, Retry and Load <default> (request_switch) and from
the named Generate and Forge controls of the pages when VRAM is low (ask(plan) with the plan's
after-action). A request never loads by itself: it mints a token for the plan of this moment and either
starts the switch (start_switch redeems the token) or opens the popover, whose primary redeems an asked
token (confirm). While the popover is open it is redrawn from the live state; it closes when any job
starts or the residency changes. vm is the Models page (§6.11).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.plans import ActionState, ConfirmRequest, SwitchPlan
from ui.contract.records import LoadEvent
from ui.contract.viewmodels import ChipTone, FactVM, ModelCardVM, ModelsVM, RowState
from ui.contract.viewmodels import ModelsController as ModelsControllerProtocol
from ui.core import catalog
from ui.core.facade import BUSY, ONE_MODEL, STALE
from ui.core.services.device import GPU_TIP, gb, gpu_line, vram_texts
from ui.core.services.load_history import LoadEstimate, history_rows
from ui.core.services.settings import CORE

from .confirm import Subscriptions
from .status import NOT_LOADED_YET, NOTHING_TO_FREE, app_gb, load_progress, load_text, row_state, switch_ask

if TYPE_CHECKING:
    from ui.core.jobs import StartResult

    from . import BehaviourFactory

__all__ = ["CARD_FACTS", "ORIGINS", "ModelsController"]

Origin = Literal["dock", "card", "retry", "default"]
ORIGINS: tuple[str, ...] = ("dock", "card", "retry", "default")
ONE_AT_A_TIME = "One at a time"
LIMIT_TIP = ("MAX_RESIDENT_BACKENDS = {n} in config.py · switching always unloads the current model "
             "before the next one loads")
MEASURED_AFTER = "Measured after its first load"

# The Models card facts per backend (M L2794-2797): languages, controls, reference clip.
CARD_FACTS: dict[str, tuple[str, str, str]] = {
    catalog.MULTILINGUAL: ("23 languages", "Expressiveness · Guidance", "6–15 s ideal"),
    catalog.STANDARD: ("English only", "Expressiveness · Guidance", "6–15 s ideal"),
    catalog.TURBO: ("English only", "Tags · no Expr or Guid", "Over 5 s"),
}

_CARD_CHIPS: dict[RowState, tuple[str, ChipTone]] = {
    "resident": ("Resident", "gold"),
    "unloading": ("Freeing…", "gold"),        # D-T5
    "failed": ("Failed", "red"),
    "not_loaded": ("Not loaded", "off"),
    "loading": ("Loading", "gold"),
}

log = logging.getLogger("voicetool.behaviours")


class ModelsController(ModelsControllerProtocol):
    """Switch requests (dock, Models page, Retry, Load <default>), Free VRAM, behaviour settings, popover."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self.vm = ModelsVM()
        self._flash: Counter[str] = Counter()
        self._ask_rev = -1                       # EngineState.rev when the open ask was drawn
        self._ask_confirm = False                # the open ask came from Confirm Model Switches (D20)
        self._history_events: tuple[LoadEvent, ...] | None = None
        self._history_day = ""
        self._history: tuple[tuple[tuple[str, str], ...], bool] = ((), True)
        self._subs = Subscriptions()
        self._subs.add(self._core.events.changed, self._on_changed)
        self._subs.add(self._core.events.flash, self._on_flash)
        self._subs.add(self._core.settings.changed, self._on_setting)
        self.refresh()

    # ---------- switch requests ----------

    def request_switch(self, backend: str, *, origin: Origin) -> ConfirmRequest | None:
        """A dock row, a Models card, Retry or Load <default>: start, ask, or nothing (§5.3 table).

        Returns the ask when the popover opened, else None. Clicking the dock row of the open ask closes it.
        """
        core = self._core
        if origin not in ORIGINS or backend not in core.state.engine.enabled:
            return None
        ask = self._factory.ask
        if origin == "dock" and _ask_target(ask.request) == backend:
            ask.close()
            return None
        if core.state.job.kind or core.state.engine.resident == backend:
            return None
        plan = core.planner.switch_plan(backend, reason="retry" if origin == "retry" else "explicit")
        if plan.low_vram:
            return self.ask(plan)                                          # D1: low VRAM always asks
        if origin == "dock" and core.settings.get(CORE, "confirm_switches"):
            return self.ask(plan, dont_visible=True)
        self.start(plan)
        return None

    def ask(self, plan: SwitchPlan, *, dont_visible: bool = False) -> ConfirmRequest:
        """Open the popover for plan with an asked token; the pages call it for a low-VRAM named control.

        dont_visible shows "Don't ask again", only for an ask that Confirm Model Switches caused (D20);
        it stays hidden while the ask is a low-VRAM ask, where the tick could not stop the next ask.
        """
        core = self._core
        req = switch_ask(core, plan, core.tokens.mint(plan, asked=True))
        self._ask_rev = core.state.engine.rev
        self._ask_confirm = bool(dont_visible)
        self._factory.ask.open(req, dont_visible=self._dont_visible(plan))
        return req

    def start(self, plan: SwitchPlan) -> StartResult:
        """Start the switch of plan now (a named click): mint a token, start_switch, toast a refusal."""
        core = self._core
        self._factory.ask.close()
        result = core.jobs.start_switch(core.tokens.mint(plan))
        self._report(result)
        return result

    async def confirm(self, token: str) -> None:
        """The popover's primary: start the switch (its after-action kept); commit "Don't ask again"."""
        ask = self._factory.ask
        req = ask.request
        if req is None or req.switch is None or req.token != token:
            return
        commit = ask.dont_visible and ask.dont
        result = self._core.jobs.start_switch(token)
        if result.status == "refused" and result.reason == STALE:
            self._renew(req.switch)                                        # recompute; the popover stays
            return
        ask.close()
        if commit and result.status != "refused":
            self.set_confirm_switches(False)                               # D4: only on the primary
        self._report(result)

    def dismiss(self) -> None:
        """Cancel, Escape or the same dock row again."""
        self._factory.ask.close()

    def set_dont_ask(self, on: bool) -> None:
        """The popover's toggle: UI state until the primary (ignored where the toggle is not shown)."""
        self._factory.ask.set_dont(on)

    # ---------- Free VRAM ----------

    async def free_vram(self) -> None:
        """The dock, the Models VRAM card, a card's "Unload · Free N GB" and the search action."""
        self.free()

    def free(self) -> StartResult:
        """Free VRAM now; a refusal ("Nothing to free · no model loaded", "Busy · …") is a red toast."""
        result = self._core.jobs.start_free()
        self._report(result)
        return result

    # ---------- behaviour settings ----------

    def set_warmup(self, mode: Literal["off", "last"]) -> None:
        self._set("warmup", mode)

    def set_idle_unload(self, minutes: int) -> None:
        self._set("idle_unload_min", minutes)

    def set_confirm_switches(self, on: bool) -> None:
        self._set("confirm_switches", bool(on))

    def close(self) -> None:
        self._subs.close()

    # ---------- the popover ----------

    def _refresh_ask(self) -> None:
        """Redraw the open ask; close it once a job started or the residency changed (M L2437)."""
        ask = self._factory.ask
        req = ask.request
        if req is None or req.switch is None:
            return
        core = self._core
        if core.state.job.kind or core.state.engine.rev != self._ask_rev:
            ask.close()
        elif core.tokens.peek(req.token) is None:
            self._renew(req.switch)                                        # a library change voided it
        else:
            ask.replace(switch_ask(core, req.switch, req.token), dont_visible=self._dont_visible(req.switch))

    def _renew(self, plan: SwitchPlan) -> None:
        core = self._core
        fresh = core.planner.switch_plan(plan.to_backend, reason=plan.reason, after=plan.after)
        self._ask_rev = core.state.engine.rev
        self._factory.ask.replace(switch_ask(core, fresh, core.tokens.mint(fresh, asked=True)),
                                  dont_visible=self._dont_visible(fresh))

    def _dont_visible(self, plan: SwitchPlan) -> bool:
        """D20: "Don't ask again" on a Confirm-caused ask, never while loading plan's model is low on VRAM."""
        return self._ask_confirm and not self._core.planner.low_vram(plan.to_backend)

    def _report(self, result: StartResult) -> None:
        """Toast a start result's reason, red for a refusal; "stale" only means: recompute."""
        if result.reason and result.reason != STALE:
            self._core.events.say(result.reason, bad=result.status == "refused")

    def _set(self, key: str, value: Any) -> None:
        try:
            self._core.settings.set(CORE, key, value)
        except (KeyError, ValueError) as exc:
            log.warning("Refused the setting %s.%s: %s", CORE, key, exc)

    # ---------- the Models page ----------

    def refresh(self) -> None:
        core, vm = self._core, self.vm
        engine, job, device = core.state.engine, core.state.job, core.state.device
        busy = bool(job.kind)
        vm.cards = tuple(self._card(backend, busy) for backend in engine.enabled)
        resident = catalog.label(engine.resident)
        texts = vram_texts(device, resident)
        vm.vram_value, vm.vram_gold = texts.value, texts.gold
        vm.vram_app_pct, vm.vram_other_pct = texts.app_pct, texts.other_pct
        vm.vram_detail, vm.vram_tip, vm.meter_visible = texts.detail, texts.tip, texts.meter_visible
        why = BUSY if busy else ("" if engine.resident else NOTHING_TO_FREE)
        vm.free = ActionState(enabled=not why, label="Free VRAM", reason=why, tone="plain")
        note = f" · {resident}" if engine.resident and job.kind != "switch" else ""
        vm.legend_app = f"This app · {texts.value}{note}"
        vm.legend_other = f"Other apps · {_gb_text(device.other_bytes)}"
        vm.legend_free = f"Free · {_gb_text(device.free_bytes)}"
        vm.gpu_line = gpu_line(device)
        vm.gpu_tip = GPU_TIP if device.detected and device.cuda else ""
        settings = core.settings
        vm.warmup = str(settings.get(CORE, "warmup"))
        vm.idle_min = int(settings.get(CORE, "idle_unload_min"))
        vm.confirm_switches = bool(settings.get(CORE, "confirm_switches"))
        vm.limit_text, vm.limit_tip = self._limit()
        vm.history, vm.history_empty = self._history_rows()

    def _card(self, backend: str, busy: bool) -> ModelCardVM:
        core = self._core
        state = row_state(core, backend)
        estimate = core.planner.load_estimate(backend)
        chip, tone = _CARD_CHIPS[state]
        ring = None
        if state == "loading":
            progress = load_progress(core)
            chip = load_text(progress, "card") if progress is not None else chip
            ring = core.state.job.fill
        tip = catalog.tip(backend) + (NOT_LOADED_YET if state == "not_loaded" and not estimate.about_s else "")
        action, kind = self._card_action(backend, busy)
        return ModelCardVM(backend=backend, label=catalog.label(backend), tip=tip, state=state, chip_text=chip,
                           chip_tone=tone, ring=ring, facts=self._facts(backend, estimate), action=action,
                           action_kind=kind, ringed=state == "resident", flash_seq=self._flash[backend])

    def _card_action(self, backend: str, busy: bool) -> tuple[ActionState, str]:
        """"Unload · Free 3.4 GB" (resident), "Switch to This Model" or "Load This Model" (named by aria)."""
        core = self._core
        resident = core.state.engine.resident
        why = BUSY if busy else ""
        if resident == backend:
            size = app_gb(core)
            label = f"Unload · Free {size} GB" if size else "Unload"
            return ActionState(enabled=not busy, label=label, reason=why, tone="plain"), "unload"
        name = catalog.label(backend)
        tone = "grey" if busy else "primary"
        if resident:
            return ActionState(enabled=not busy, label="Switch to This Model", reason=why, aria=f"Switch to {name}",
                               tone=tone), "switch"
        return ActionState(enabled=not busy, label="Load This Model", reason=why, aria=f"Load {name}",
                           tone=tone), "load"

    def _facts(self, backend: str, estimate: LoadEstimate) -> tuple[FactVM, ...]:
        core = self._core
        languages, controls, clip = CARD_FACTS.get(backend, ("—", "—", "—"))
        if core.planner.device() == "cpu":
            vram = FactVM("Measured VRAM", "On CPU")
        elif estimate.gb is not None:
            vram = FactVM("Measured VRAM", f"{estimate.gb:.1f} GB")
        else:
            vram = FactVM("Measured VRAM", "—", MEASURED_AFTER)
        last = f"{estimate.last_s:.1f} s" if estimate.last_s is not None else "—"
        voices = sum(1 for row in core.state.library.rows if row.backend == backend)
        return (FactVM("Languages", languages), FactVM("Controls", controls), FactVM("Reference clip", clip), vram,
                FactVM("Last load", last), FactVM("Voices", str(voices)))

    def _limit(self) -> tuple[str, str]:
        """"One at a time" and its tooltip, with the detected GPU (or " · CPU") once a reading came in."""
        core = self._core
        limit = core.state.engine.max_resident
        tip = LIMIT_TIP.format(n=limit)
        device = core.state.device
        if device.detected and device.cuda:
            tip += f" · {device.short_name or 'GPU'} · {gb(device.total_bytes)} GB"
        elif device.detected and device.cuda is False:
            tip += " · CPU"
        return (ONE_AT_A_TIME if limit == 1 else ONE_MODEL), tip

    def _history_rows(self) -> tuple[tuple[tuple[str, str], ...], bool]:
        """Load History lines, rebuilt only when the events or the day changed."""
        events = self._core.state.history.events
        day = _today()
        if events is not self._history_events or day != self._history_day:
            rows = history_rows(events, label=catalog.label)
            self._history_events, self._history_day, self._history = events, day, (rows, not rows)
        return self._history

    # ---------- events ----------

    def _on_changed(self) -> None:
        self._refresh_ask()
        self.refresh()

    def _on_flash(self, key: str) -> None:
        if key.startswith("dock-"):
            self._flash[key.removeprefix("dock-")] += 1
            self.refresh()

    def _on_setting(self, section: str, key: str) -> None:
        self.refresh()


def _ask_target(req: ConfirmRequest | None) -> str | None:
    return req.switch.to_backend if req is not None and req.switch is not None else None


def _gb_text(n_bytes: int | None) -> str:
    return "—" if n_bytes is None else f"{gb(n_bytes)} GB"


def _today() -> str:
    return date.today().isoformat()
