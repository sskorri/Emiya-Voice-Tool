"""StatusController (§5.2) and the shell copy every page shares.

The controller owns ShellVM: the header chip, the Model dock rows and status line, the VRAM block, the
dock buttons, the switch popover's fields (from the client's AskState), the Close arm, the Log toggle,
the toast, the art layer and the search box. It recomputes on core.events.changed, on setting changes
and when the client's Arm or AskState changes; intents recompute too.

The helpers below are shared with ModelsController and the page controllers, so all five places that
show a load (header chip, dock status, Generate, Forge and the Models card chip) change form at the
same tick: row_state(), load_progress() with load_text(), switch_ask() and then_text() for the popover.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ui.contract.plans import ActionState, AfterForge, AfterGenerate, ConfirmRequest, SwitchPlan, Toast
from ui.contract.routes import TITLES
from ui.contract.viewmodels import ChipTone, DockRowVM, DotTone, RowState, SearchItem, ShellVM, TextTone
from ui.contract.viewmodels import StatusController as StatusControllerProtocol
from ui.core import catalog
from ui.core.facade import BUSY
from ui.core.jobs import NOTHING_TO_FREE
from ui.core.planner import round_half_up
from ui.core.services.device import gb, vram_texts
from ui.core.services.settings import CORE

from .confirm import ARM_DELETE, ARM_PURGE, ARM_QUIT, ARM_REPLACE, Subscriptions

if TYPE_CHECKING:
    from ui.contract.state import JobState
    from ui.core import Core
    from ui.core.clock import Handle

    from . import BehaviourFactory

__all__ = [
    "CLOSE_ARMED",
    "CLOSE_LABEL",
    "LoadPlace",
    "LoadProgress",
    "MINIMIZE_ONLY_NATIVE",
    "NOTHING_TO_FREE",
    "NOT_LOADED_YET",
    "QUIT_BUSY",
    "TOAST_S",
    "StatusController",
    "app_gb",
    "default_backend",
    "is_unloading",
    "load_progress",
    "load_text",
    "loads_line",
    "named_plan",
    "normalise_route",
    "row_state",
    "switch_ask",
    "then_text",
]

TOAST_S = 2.6                                                          # M L1379
CLOSE_LABEL = "Close · frees VRAM"                                     # M L2924
CLOSE_ARMED = "Click Again to Quit"
QUIT_BUSY = "A job is running and can't be cancelled · click again to quit"   # M L1761
MINIMIZE_ONLY_NATIVE = "Minimize works in the app window"              # NEW
NOT_LOADED_YET = " · Not loaded yet on this PC"                         # NEW (§5.2)

LoadPlace = Literal["chip", "dock", "button", "card"]
_TITLES: dict[str, str] = dict(TITLES)
LANGUAGE_PAGES: tuple[str, ...] = ("speak", "voices", "forge")   # the page controllers with a language list


# ---------- state helpers (shared) ----------

def app_gb(core: Core) -> str | None:
    """This app's VRAM as "3.4" from the last CUDA reading; None before one, without CUDA or on the CPU."""
    device = core.state.device
    if device.cuda is not True or core.state.engine.device == "cpu":
        return None
    return gb(device.app_bytes)


def is_unloading(job: JobState) -> bool:
    """An unload job, or a switch in its unload phase (job.source is the backend being freed)."""
    return job.kind == "unload" or (job.kind == "switch" and job.phase == "unload")


def row_state(core: Core, backend: str) -> RowState:
    """A backend's state on the dock, the Models cards and the Speak group chips."""
    job, engine = core.state.job, core.state.engine
    if job.kind == "switch" and job.phase == "load" and job.target == backend:
        return "loading"
    if is_unloading(job) and job.source == backend:
        return "unloading"
    if engine.resident == backend:
        return "resident"
    if engine.failed == backend and not job.kind:
        return "failed"
    return "not_loaded"


def default_backend(core: Core) -> str:
    """The default model ("Load Multilingual" after a failed load, D13): config.DEFAULT_BACKEND when it is
    enabled, else the first enabled backend."""
    enabled = core.state.engine.enabled
    if catalog.DEFAULT_BACKEND in enabled:
        return catalog.DEFAULT_BACKEND
    return enabled[0] if enabled else ""


def normalise_route(path: str) -> str:
    """"/takes?x=1" → "/takes"; "" → "/"."""
    route = str(path or "").split("?", 1)[0].split("#", 1)[0].strip() or "/"
    return route.rstrip("/") or "/"


# ---------- the load progress copy (§5.2): one rule for five places ----------

@dataclass(frozen=True)
class LoadProgress:
    """A switch in its load phase: N elapsed whole seconds, M the estimate (None without history)."""

    backend: str
    label: str
    n: int                              # elapsed seconds rounded half up, at least 1
    m: int | None                       # the history estimate rounded half up
    over: bool                          # elapsed reached the estimate
    fill: float | None                  # JobState.fill (None = indeterminate)

    @property
    def left(self) -> int:
        """K = max(1, M − N)."""
        return max(1, (self.m or 0) - self.n)


def load_progress(core: Core) -> LoadProgress | None:
    """The running load, or None when no switch is in its load phase."""
    job = core.state.job
    if job.kind != "switch" or job.phase != "load":
        return None
    about = core.planner.load_estimate(job.target).about_s or None
    return LoadProgress(
        backend=job.target,
        label=catalog.label(job.target),
        n=max(1, round_half_up(job.elapsed_s)),
        m=None if about is None else round_half_up(about),
        over=about is not None and job.elapsed_s >= about,
        fill=job.fill,
    )


def load_text(progress: LoadProgress, place: LoadPlace) -> str:
    """The progress copy of one place: chip, dock (status line), button (Generate, Forge) or card."""
    head = {"chip": f"Loading {progress.label} · ", "dock": "Loading · ",
            "button": f"Loading {progress.label}… ", "card": "Loading · "}[place]
    if progress.m is None:
        return f"{head}{progress.n} s"
    if progress.over:
        return f"{head}{progress.n} s, usually ~{progress.m} s"
    if place == "dock":
        return f"{head}~{progress.left} s left"
    return f"{head}{progress.n} s of ~{progress.m} s"


# ---------- the switch popover (§6.2.7) ----------

def loads_line(core: Core, backend: str) -> str:
    """"Loads Turbo · about 9 s (last 8.7 s) · 3.1 GB", or "· first load on this PC" without history."""
    name = catalog.label(backend)
    estimate = core.planner.load_estimate(backend)
    if estimate.about_s:
        text = f"Loads {name} · about {round_half_up(estimate.about_s)} s"
        if estimate.last_s is not None:
            text += f" (last {estimate.last_s:.1f} s)"
        return text + (f" · {estimate.gb:.1f} GB" if estimate.gb is not None else "")
    weights = core.facade.weights_bytes(backend)
    return f"Loads {name} · first load on this PC" + (f" · about {gb(weights)} GB of weights" if weights else "")


def switch_ask(core: Core, plan: SwitchPlan, token: str) -> ConfirmRequest:
    """The popover for plan from the current state: title, lines with their tones, primary button."""
    target = plan.to_backend
    lines: list[tuple[str, str]] = []
    resident = core.state.engine.resident
    if resident:
        freed = app_gb(core)
        lines.append((f"Unloads {catalog.label(resident)}" + (f" · frees {freed} GB" if freed else ""), "text-soft"))
    lines.append((loads_line(core, target), "text-soft"))
    lines.append((catalog.rules(target), "muted"))
    low = core.planner.low_vram(target)
    if low:
        free = gb(core.planner.free_after_bytes())
        lines.append((f"Low VRAM · {free} GB free · close other GPU apps", "warn"))
    return ConfirmRequest(token=token, kind="switch_model", style="ask", title=f"Switch to {catalog.label(target)}",
                          lines=tuple(lines), primary="Load Anyway" if low else "Switch Model", switch=plan)


def then_text(core: Core, plan: SwitchPlan | None) -> str:
    """"Then generates take 8" / "Then generates takes 8-10" / "Then forges rin" (D20); "" without one."""
    after = plan.after if plan is not None else None
    if isinstance(after, AfterGenerate) and after.reqs:
        first, count = core.state.takes.next_id, len(after.reqs)
        return f"Then generates take {first}" if count == 1 else f"Then generates takes {first}-{first + count - 1}"
    if isinstance(after, AfterForge):
        return f"Then forges {after.req.voice_id}"
    return ""


def named_plan(core: Core, backend: str, reason: Literal["explicit", "retry"]) -> ConfirmRequest:
    """A named control's pending request: no token (it is minted at the click), the plan names the backend."""
    return ConfirmRequest(token="", kind="switch_model", style="named",
                          switch=core.planner.switch_plan(backend, reason=reason))


# ---------- the controller ----------

class StatusController(StatusControllerProtocol):
    """Chip, dock rows, VRAM texts, close arm, log toggle, toasts and the title (§5.2)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self.vm = ShellVM()
        self._flash: Counter[str] = Counter()
        self._spark: Counter[str] = Counter()
        self._toast_timer: Handle | None = None
        self._subs = Subscriptions()
        events = self._core.events
        self._subs.add(events.changed, self.refresh)
        self._subs.add(events.toast, self._on_toast)
        self._subs.add(events.flash, self._on_flash)
        self._subs.add(events.spark, self._on_spark)
        self._subs.add(self._core.settings.changed, self._on_setting)
        factory.arm.listeners.add(self.refresh)
        factory.ask.listeners.add(self.refresh)
        self.refresh()

    # ---------- intents ----------

    def toggle_log(self) -> None:
        self.vm.log_open = not self.vm.log_open

    def close_log(self) -> None:
        self.vm.log_open = False

    def request_quit(self) -> None:
        """Idle: quit. Busy: the first click arms for 3 s with a toast, a second click quits."""
        arm = self._factory.arm
        confirmed = arm.press(ARM_QUIT) if self._core.state.job.kind else True
        if confirmed:
            arm.disarm(ARM_QUIT)
            self._factory.kit.quit()
            return
        if confirmed is False:
            self._core.events.say(QUIT_BUSY)

    def minimize(self) -> None:
        if not self._factory.kit.minimize():
            self._core.events.say(MINIMIZE_ONLY_NATIVE)

    def on_route(self, path: str) -> None:
        """The shell's route: title, then close the popover, lists, search and capture; disarm (M L1752)."""
        route = normalise_route(path)
        self.vm.route = route
        self.vm.title = _TITLES.get(route, "")
        factory = self._factory
        factory.ask.close()
        factory.arm.disarm(ARM_DELETE)
        factory.arm.disarm(ARM_PURGE)
        if route != "/forge":
            factory.arm.disarm(ARM_REPLACE)
        self._close_lists()
        settings = factory.built("settings")
        if settings is not None:
            settings.stop_listening()
        self.refresh()

    def escape(self) -> None:
        """D12: Escape closes the Log, the switch popover, the search results and the language lists."""
        self.close_log()
        self._factory.models().dismiss()
        self._close_lists()
        self.refresh()

    def _close_lists(self) -> None:
        """The language lists of the page controllers built so far, and the search results."""
        factory = self._factory
        for name in LANGUAGE_PAGES:
            page = factory.built(name)
            if page is not None:
                page.toggle_language_list(False)
        search = factory.built("search")
        if search is not None:
            search.clear()

    def dismiss_toast(self) -> None:
        self._cancel_toast_timer()
        self.vm.toast = None

    def show_search(self, items: tuple[SearchItem, ...], hint: str) -> None:
        """SearchController's results for the rail search box."""
        self.vm.search_items = items
        self.vm.search_hint = hint

    def close(self) -> None:
        """Drop every subscription and timer (BehaviourFactory.close)."""
        self._subs.close()
        self._factory.arm.listeners.remove(self.refresh)
        self._factory.ask.listeners.remove(self.refresh)
        self._cancel_toast_timer()

    # ---------- recompute ----------

    def refresh(self) -> None:
        core, vm = self._core, self.vm
        vm.chip_text, vm.chip_tone, vm.chip_dot = self._chip()
        vm.dock_rows = tuple(self._dock_row(backend) for backend in core.state.engine.enabled)
        vm.dock_status, vm.dock_status_tone = self._dock_status()
        self._vram()
        self._dock_buttons()
        self._ask()
        armed = self._factory.arm.is_armed(ARM_QUIT)
        label = CLOSE_ARMED if armed else CLOSE_LABEL
        vm.close = ActionState(True, label, aria=label, tooltip=label, tone="danger", armed=armed)
        self._art()
        reduce_always = core.settings.get(CORE, "reduce_motion") == "always"
        vm.reduce_always = vm.logo_still = reduce_always
        vm.search_badge = " ".join(self._factory.commands().hint_parts("search"))

    def _chip(self) -> tuple[str, ChipTone, DotTone]:
        core = self._core
        engine, job = core.state.engine, core.state.job
        if engine.resident:
            text, tone, dot = self._ready_text(), "base", "gold"
        else:
            text, tone, dot = "No model loaded", "base", "hollow"
        if is_unloading(job):
            freeing = app_gb(core)
            text = f"Unloading {catalog.label(job.source)}" + (f" · freeing {freeing} GB" if freeing else "")
            tone, dot = "gold", "gold"
        progress = load_progress(core)
        if progress is not None:
            text, tone, dot = load_text(progress, "chip"), "gold", "gold"
        if job.kind == "generate":
            text, tone, dot = _generating_text(job), "gold", "gold"
        if job.kind == "forge":
            text, tone, dot = f"Forging {job.voice}…", "gold", "gold"
        if engine.failed and not job.kind:
            text, tone, dot = f"{catalog.label(engine.failed)} failed to load", "red", "red"
        return text, tone, dot

    def _ready_text(self) -> str:
        engine = self._core.state.engine
        name = f"{catalog.label(engine.resident)} ready"
        if engine.device == "cpu":
            return f"{name} · CPU"
        size = app_gb(self._core)
        return f"{name} · {size} GB" if size else name

    def _dock_row(self, backend: str) -> DockRowVM:
        core = self._core
        job = core.state.job
        state = row_state(core, backend)
        busy = bool(job.kind)
        tip = catalog.tip(backend)
        info, ring = "", None
        tone: TextTone = "faint"
        if state == "resident":
            info, tone = self._resident_info(), "gold-dim"
        elif state == "unloading":
            info, tone = "Freeing", "gold-dim"
        elif state == "loading":
            progress = load_progress(core)
            info, tone, ring = f"{progress.n if progress else 1} s", "gold", job.fill
        elif state == "failed":
            info, tone = "Failed", "bad"
        else:
            about = core.planner.load_estimate(backend).about_s
            if about:
                info = f"~{round_half_up(about)} s"
            else:
                tip += NOT_LOADED_YET
        return DockRowVM(backend=backend, label=catalog.label(backend), tip=tip, state=state, info=info,
                         info_tone=tone, enabled=not busy, dim=busy and state not in ("loading", "unloading"),
                         ring=ring, flash_seq=self._flash[backend], spark_seq=self._spark[backend])

    def _resident_info(self) -> str:
        if self._core.state.engine.device == "cpu":
            return "CPU"
        size = app_gb(self._core)
        return f"{size} GB" if size else "—"

    def _dock_status(self) -> tuple[str, TextTone]:
        core = self._core
        engine, job = core.state.engine, core.state.job
        text: str = "Ready" if engine.resident else "No model loaded"
        tone: TextTone = "muted"
        if job.kind == "generate":
            text, tone = "Busy · generating", "gold-dim"
        elif job.kind == "forge":
            text, tone = "Busy · forging", "gold-dim"
        if is_unloading(job):
            freeing = app_gb(core)
            text, tone = (f"Freeing {freeing} GB…" if freeing else "Freeing…"), "gold-dim"
        progress = load_progress(core)
        if progress is not None:
            text, tone = load_text(progress, "dock"), "gold"
        if engine.failed and job.kind != "switch":
            text, tone = "Load failed · see Log", "bad"
        return text, tone

    def _vram(self) -> None:
        core, vm = self._core, self.vm
        texts = vram_texts(core.state.device, catalog.label(core.state.engine.resident))
        vm.vram_value, vm.vram_gold = texts.value, texts.gold
        vm.vram_app_pct, vm.vram_other_pct = texts.app_pct, texts.other_pct
        vm.vram_detail, vm.vram_tip, vm.meter_visible = texts.detail, texts.tip, texts.meter_visible

    def _dock_buttons(self) -> None:
        core, vm = self._core, self.vm
        engine, job = core.state.engine, core.state.job
        busy = bool(job.kind)
        why = BUSY if busy else ("" if engine.resident else NOTHING_TO_FREE)
        vm.free = ActionState(enabled=not why, label="Free VRAM", reason=why, tone="plain")
        failed = engine.failed
        vm.show_failed_actions = bool(failed) and job.kind != "switch"
        if not vm.show_failed_actions:
            vm.retry = ActionState(False, "Retry", tone="plain")
            vm.load_default = ActionState(False, "", tone="plain")
            return
        aria = f"Retry loading {catalog.label(failed)}"
        vm.retry = ActionState(enabled=not busy, label="Retry", aria=aria, tooltip=aria, tone="plain",
                               pending=named_plan(core, failed, "retry"))
        default = default_backend(core)
        if default and default != failed:
            vm.load_default = ActionState(enabled=not busy, label=f"Load {catalog.label(default)}", tone="plain",
                                          pending=named_plan(core, default, "explicit"))
        else:
            vm.load_default = ActionState(False, "", tone="plain")   # D13: Retry already loads the default

    def _ask(self) -> None:
        vm, ask = self.vm, self._factory.ask
        req = ask.request
        vm.ask = req
        vm.ask_dont = ask.dont if req is not None else False
        vm.ask_dont_visible = req is not None and ask.dont_visible
        vm.ask_then = then_text(self._core, req.switch) if req is not None else ""

    def _art(self) -> None:
        core, vm = self._core, self.vm
        library = core.state.library
        empty = library.status in ("ready", "corrupt") and not library.rows
        page = "forge" if vm.route == "/forge" else "speak" if vm.route == "/" and empty else ""
        vm.art_page = page
        vm.art_visible = bool(page) and core.settings.pref("saber_art", "hide") == "show"   # the theme's own pref
        vm.script_dark = empty

    # ---------- events ----------

    def _on_toast(self, toast: Toast) -> None:
        """One toast at a time: a newer one replaces the shown one; each stays 2.6 s."""
        self._cancel_toast_timer()
        self.vm.toast = toast

        def expire() -> None:
            if self.vm.toast is not None and self.vm.toast.seq == toast.seq:
                self.vm.toast = None

        self._toast_timer = self._core.scheduler.later(TOAST_S, expire)

    def _on_flash(self, key: str) -> None:
        if key.startswith("dock-"):
            self._flash[key.removeprefix("dock-")] += 1
            self.refresh()

    def _on_spark(self, key: str) -> None:
        if key.startswith("dock-"):
            self._spark[key.removeprefix("dock-")] += 1
            self.refresh()

    def _on_setting(self, section: str, key: str) -> None:
        self.refresh()

    def _cancel_toast_timer(self) -> None:
        if self._toast_timer is not None:
            self._toast_timer.cancel()
            self._toast_timer = None


def _generating_text(job: JobState) -> str:
    """"Generating Take 8 · 2.1 s · 2 queued"; a Forge test line: "Generating test line · rin · 1.2 s"."""
    if job.take_no <= 0:
        return f"Generating test line · {job.voice} · {job.elapsed_s:.1f} s"
    queued = f" · {job.queued} queued" if job.queued else ""
    return f"Generating Take {job.take_no} · {job.elapsed_s:.1f} s{queued}"
