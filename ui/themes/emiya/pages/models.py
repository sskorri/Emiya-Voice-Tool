"""The Models page (BUILD_SPEC §6.11; Main.dc.html L854-957).

The section "Models · one in VRAM at a time" holds one card per enabled backend; below it, the VRAM card (meter,
legend, GPU line, Free VRAM) over the Behavior card (Warm Up on Launch, Auto-unload After Idle, Confirm Model
Switches and the locked "Models in VRAM" fact), and the Load History beside them. Everything shown comes from
the ModelsController's view model (ModelsVM) through passive bindings, and every control calls one intent:

- a card's "Load This Model" / "Switch to This Model": request_switch(backend, origin="card"). Its aria-label
  ("Load Turbo" / "Switch to Turbo") names the model, so it starts at once and asks only on low VRAM (D1);
- a card's "Unload · Free 3.4 GB" and Free VRAM: free_vram();
- the Behavior rows: set_warmup (applies at the next launch), set_idle_unload and set_confirm_switches (live).

The cards are updated in place, so the flash after a load plays on the card itself. The builder is sync and
subscribes to nothing (R4): the controller recomputes the view model. Geometry and colours live in
static/pages/models.css.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from nicegui import binding, ui
from nicegui.element import Element

from ui.contract.viewmodels import FactVM, ModelCardVM

from ..widgets import (
    Card,
    Chip,
    DotIndicator,
    DotSwitch,
    EmiyaButton,
    ListHost,
    Meter,
    Row,
    SectionLabel,
    Segmented,
    TextSpan,
    flash_class,
    set_class,
    set_prop,
    svg_markup,
    swap_classes,
)

if TYPE_CHECKING:
    from ui.contract.theme import ThemeContext
    from ui.contract.viewmodels import ModelsController, ModelsVM

__all__ = [
    "CARDS_TITLE", "CONFIRM_LABEL", "CONFIRM_NOTE", "HISTORY_EMPTY", "IDLE_OPTIONS", "LIMIT_LABEL", "LIMIT_NOTE",
    "LINE_TONES", "WARMUP_OPTIONS", "ModelCard", "ModelCards", "ModelsPage", "build",
]

CARDS_TITLE = "Models · one in VRAM at a time"                                    # M L858
WARMUP_OPTIONS: tuple[tuple[str, str], ...] = (("off", "Off"), ("last", "Last Model"))     # M L3216
IDLE_OPTIONS: tuple[tuple[str, str], ...] = (                                     # M L3217; minutes, 0 = Never
    ("0", "Never"), ("10", "10 min"), ("30", "30 min"), ("60", "1 h"))
CONFIRM_LABEL = "Confirm Model Switches"                                          # M L929
CONFIRM_NOTE = "Buttons that name the switch never ask"                           # M L930
LIMIT_LABEL = "Models in VRAM"                                                    # M L936
LIMIT_NOTE = "Fixed in config.py"                                                 # M L938
HISTORY_EMPTY = "No models loaded yet."                                           # M L950 (D-T8)
# Load History tones (M L2820-2821): older days grey, today lighter, failures red, as TextTone names.
LINE_TONES: Mapping[str, str] = MappingProxyType({"old": "faint", "today": "text-soft", "failed": "bad"})

UNLOAD = "unload"                                       # ModelCardVM.action_kind of the resident card
_WARMUP_MODES: Mapping[str, Literal["off", "last"]] = MappingProxyType({"off": "off", "last": "last"})


def build(ctx: ThemeContext) -> None:
    """The Models page for one page load (sync, bound to the ModelsVM, no subscriptions)."""
    ModelsPage(ctx.behaviours.models()).build()


# ---- page-only elements -------------------------------------------------------------------------------

class ModelCard(Card):
    """One model card (M L861-881): the state dot or ring, the label and chip, the facts and the action button.

    show() redraws it in place, so a load's flash plays on this element; the button's markers follow its kind:
    models.switch and models.switch.<backend> on Load / Switch, models.unload on the resident card.
    """

    def __init__(self, card: ModelCardVM, on_act: Callable[[str, str], Any]) -> None:
        super().__init__()
        self.backend = card.backend
        self.classes("vt-models-card")
        self._flash_seq = card.flash_seq             # a load before this page opened does not flash it
        self._flash_class = ""
        self._kind = ""
        self._facts: tuple[FactVM, ...] | None = None
        with self:
            with Element("div").classes("vt-models-head"):
                self.indicator = DotIndicator()
                self.caption = TextSpan(tag="p").classes("vt-models-name")
                self.chip = Chip(size=20)
            self.fact_box = Element("div").classes("vt-models-facts")
            self.button = EmiyaButton(size=30, on_click=lambda: on_act(self.backend, self._kind)).classes(
                "vt-models-act")
        self.show(card)

    @property
    def kind(self) -> str:
        """The action the button stands for now: "load", "switch" or "unload"."""
        return self._kind

    def show(self, card: ModelCardVM) -> None:
        """Draw one ModelCardVM: tooltip, ring, dot or ring indicator, chip, facts, button, flash."""
        set_prop(self, "title", card.tip)
        set_class(self, "is-ringed", card.ringed)
        self.indicator.state = card.state
        self.indicator.ring = card.ring
        self.caption.text = card.label
        self.chip.text = card.chip_text
        self.chip.tone = card.chip_tone
        if card.facts != self._facts:
            self._show_facts(card.facts)
        self.button.action = card.action
        if card.action_kind != self._kind:
            self._kind = card.action_kind
            self.button.mark(_action_markers(card.backend, card.action_kind))
        if card.flash_seq != self._flash_seq:
            self._flash_seq = card.flash_seq
            self._flash_class = swap_classes(self, self._flash_class, flash_class(card.flash_seq))

    def _show_facts(self, facts: tuple[FactVM, ...]) -> None:
        """The fact rows (M L874-879): label on the left, value on the right with its tooltip."""
        self._facts = facts
        self.fact_box.clear()
        with self.fact_box:
            for fact in facts:
                with Element("div").classes("vt-models-fact"):
                    TextSpan(fact.label).classes("vt-models-fact-label")
                    TextSpan(fact.value, tip=fact.tip).classes("vt-models-fact-value")


class ModelCards(Element):
    """The three-column grid of model cards (M L859); a card is reused while the enabled backends stay the same."""

    cards = binding.BindableProperty(on_change=lambda sender, value: sender._show(value))

    def __init__(self, on_act: Callable[[str, str], Any]) -> None:
        super().__init__("div")
        self.classes("vt-models-cards")
        self._on_act = on_act
        self._cards: dict[str, ModelCard] = {}
        self.cards = ()

    def card(self, backend: str) -> ModelCard | None:
        """The card of one backend, or None when it is not enabled."""
        return self._cards.get(backend)

    def _show(self, cards: tuple[ModelCardVM, ...]) -> None:
        if [card.backend for card in cards] != list(self._cards):
            self.clear()
            with self:
                self._cards = {card.backend: ModelCard(card, self._on_act).mark(f"models.card.{card.backend}")
                               for card in cards}
            return
        for card in cards:
            self._cards[card.backend].show(card)


# ---- the page -----------------------------------------------------------------------------------------

class ModelsPage:
    """One page load's Models page: its elements, their bindings, and the handlers that call the intents."""

    def __init__(self, models: ModelsController) -> None:
        self._models = models
        self._vm: ModelsVM = models.vm
        self.cards: ModelCards | None = None

    def build(self) -> None:
        """The cards, then the VRAM and Behavior cards beside the Load History (M L855-956)."""
        with Element("div").classes("vt-models"):
            self._cards()
            with Element("div").classes("vt-models-lower"):
                with Element("div").classes("vt-models-left"):
                    self._vram()
                    self._behavior()
                self._history()

    # ---------- the cards ----------

    def _cards(self) -> None:
        with _section(CARDS_TITLE):
            self.cards = ModelCards(self._act).mark("models.cards")
            binding.bind_from(self.cards, "cards", self._vm, "cards")

    def _act(self, backend: str, kind: str) -> Any:
        """A card's button: Unload frees VRAM; Load or Switch asks for the switch (named; low VRAM still asks)."""
        if kind == UNLOAD:
            return self._models.free_vram()
        self._models.request_switch(backend, origin="card")
        return None

    # ---------- VRAM ----------

    def _vram(self) -> None:
        """The 8 px meter, its legend, the GPU line and Free VRAM (M L888-905)."""
        vm = self._vm
        with _section("VRAM"), Card().classes("vt-models-vram").mark("models.vram"):
            meter = Meter(tall=True).mark("models.vram.meter")
            binding.bind_from(meter, "app_pct", vm, "vram_app_pct")
            binding.bind_from(meter, "other_pct", vm, "vram_other_pct")
            binding.bind_from(meter, "aria", vm, "vram_tip")
            meter.bind_visibility_from(vm, "meter_visible")
            with Element("div").classes("vt-models-legend") as legend:
                legend.bind_visibility_from(vm, "meter_visible")         # the key of a meter that is shown
                self._legend("app", "legend_app")
                self._legend("other", "legend_other")
                self._legend("free", "legend_free")
            with Element("div").classes("vt-models-gpu"):
                gpu = TextSpan().classes("vt-models-gpu-line").mark("models.gpu")
                gpu.bind_text_from(vm, "gpu_line")
                binding.bind_from(gpu, "tip", vm, "gpu_tip")
                free = EmiyaButton("Free VRAM", size=26, on_click=self._models.free_vram).classes(
                    "vt-models-free").mark("models.free")
                free.bind_action_from(vm, "free")

    def _legend(self, key: str, field: str) -> None:
        """One legend entry: its swatch and "This app · 3.4 GB · Multilingual" (or other, free)."""
        with Element("span").classes("vt-models-key"):
            set_prop(Element("span").classes(f"vt-models-swatch is-{key}"), "aria-hidden", "true")
            TextSpan().mark(f"models.legend.{key}").bind_text_from(self._vm, field)

    # ---------- Behavior ----------

    def _behavior(self) -> None:
        """Warm Up on Launch, Auto-unload After Idle, Confirm Model Switches, Models in VRAM (M L906-941)."""
        vm = self._vm
        with _section("Behavior"), Card().classes("vt-models-behavior"):
            with Row("Warm Up on Launch", first=True):
                warmup = Segmented(WARMUP_OPTIONS, aria="Warm up on launch", item_marker="models.warmup",
                                   on_change=self._set_warmup).classes("vt-models-warmup").mark("models.warmup")
                binding.bind_from(warmup, "value", vm, "warmup")
            with Row("Auto-unload After Idle"):
                idle = Segmented(IDLE_OPTIONS, aria="Auto-unload after idle", item_marker="models.idle",
                                 on_change=self._set_idle).classes("vt-models-idle").mark("models.idle")
                binding.bind_from(idle, "value", vm, "idle_min", backward=str)
            with Row():
                confirm = DotSwitch(CONFIRM_LABEL, note=CONFIRM_NOTE,
                                    on_change=self._models.set_confirm_switches).mark("models.confirm")
                binding.bind_from(confirm, "value", vm, "confirm_switches")
            self._limit()

    def _limit(self) -> None:
        """The locked fact: lock icon, "One at a time", "Fixed in config.py"; its tooltip is limit_tip."""
        vm = self._vm
        with Row(LIMIT_LABEL, off=True).classes("vt-models-limit").mark("models.limit") as row:
            binding.bind_from(row, "tip", vm, "limit_tip")
            with Element("span").classes("vt-models-limit-value"):
                ui.html(svg_markup("lock", 12), sanitize=False, tag="span").classes("vt-ico")
                TextSpan().classes("vt-models-limit-text").bind_text_from(vm, "limit_text")
            TextSpan(LIMIT_NOTE).classes("vt-models-limit-note")

    def _set_warmup(self, key: str) -> None:
        mode = _WARMUP_MODES.get(key)
        if mode is not None:
            self._models.set_warmup(mode)

    def _set_idle(self, key: str) -> None:
        """A segment of Auto-unload After Idle: "0" (Never), "10", "30" or "60" minutes."""
        if key.isdigit():
            self._models.set_idle_unload(int(key))

    # ---------- Load History ----------

    def _history(self) -> None:
        """Typed residency events, oldest at the top, the newest kept in view (M L943-953, D-T8)."""
        vm = self._vm
        with _section("Load History"), Card().classes("vt-models-history em-scroll").mark("models.history"):
            lines = ListHost(_history_line).classes("vt-models-lines").mark("models.history.lines")
            lines.bind_items_from(vm, "history")
            lines.bind_visibility_from(vm, "history_empty", backward=_negate)
            empty = TextSpan(HISTORY_EMPTY, tag="p").classes("vt-models-empty").mark("models.history.empty")
            empty.bind_visibility_from(vm, "history_empty")


# ---- helpers ------------------------------------------------------------------------------------------

def _section(title: str) -> Element:
    """A page section with its 22 px caption (M L857-858); enter it to add the card below the caption."""
    section = Element("section").classes("vt-models-section")
    with section:
        SectionLabel(title)
    return section


def _history_line(line: tuple[str, str]) -> None:
    text, tone = line
    TextSpan(text, tag="p", tone=LINE_TONES.get(tone, LINE_TONES["old"])).classes("vt-models-line")


def _action_markers(backend: str, kind: str) -> str:
    """models.unload on the resident card's Unload; models.switch and models.switch.<backend> on Load / Switch."""
    return "models.unload" if kind == UNLOAD else f"models.switch models.switch.{backend}"


def _negate(value: Any) -> bool:
    return not value
