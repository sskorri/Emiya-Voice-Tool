"""EmiyaBars: static waveform bars for the Forge clip, the Voices details card and the Takes compare card.

The browser half is emiya_bars.js. The behaviours decide each bar's zone (viewmodels.Bars: Forge "timbre",
"selection", "outside"; Voices "timbre", "rest"; compare bars are plain values); the component maps the
zones to the classes in-timbre, in-sel and out and computes each bar's height for its kind:

    forge    230 bars, 3 + v * 56 px, marks, selection edges and the Trace On overlay while baking
    voices   138 bars at 0.72 of that height (at most 42 px)
    compare  110 bars of max(3, round((3 + v * 50) * 0.7)) px (at most 37 px) in a 329x40 box,
             the played part clipped to ``progress``

Every colour comes from the theme stylesheet (emiya-bars--<kind> scopes the classes emiya-bar, in-timbre,
in-sel, out and is-played, the mark and selection classes and the emiya-trace-* parts). Bind it from the VMs
with nicegui.binding, for example
``binding.bind_from(bars, "bars", forge.vm, "bars")`` or
``binding.bind_from(bars, "bars", voices.vm, "details", backward=lambda d: d.bars if d else ())``.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from nicegui import binding, ui

from ui.contract.records import ClipMark

from .take_wave import BAR_DECIMALS, reduce_prop

BarKind = Literal["forge", "voices", "compare"]
KINDS: tuple[str, ...] = ("forge", "voices", "compare")


def bars_prop(bars: Sequence[float | tuple[float, str]]) -> list[Any]:
    """[[value, zone], ...] for zone pairs and [value, ...] for plain values, JSON-ready."""
    out: list[Any] = []
    for bar in bars:
        if isinstance(bar, (tuple, list)):
            value, zone = bar
            out.append([round(float(value), BAR_DECIMALS), str(zone)])
        else:
            out.append(round(float(bar), BAR_DECIMALS))
    return out


def marks_prop(marks: Sequence[ClipMark]) -> list[dict[str, Any]]:
    """The Forge marks ("Prompt 6 s", "Timbre 10 s") as {label, pct}."""
    return [{"label": mark.label, "pct": round(float(mark.pct), BAR_DECIMALS)} for mark in marks]


def optional_float(value: float | None) -> float | None:
    """None stays None (the part is not drawn); anything else becomes a float."""
    return None if value is None else float(value)


# Python property -> (prop key, conversion); the keys are the component's props in kebab case.
_PROPS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "bars": ("bars", bars_prop),
    "marks": ("marks", marks_prop),
    "sel_pct": ("sel-pct", optional_float),
    "progress": ("progress", optional_float),
    "tracing": ("tracing", bool),
    "trace_progress": ("trace-progress", float),
    "aria": ("aria", str),
    "reduce_always": ("reduce", reduce_prop),
}


def _pusher(name: str) -> Callable[[EmiyaBars, Any], None]:
    key, convert = _PROPS[name]

    def push(sender: EmiyaBars, value: Any) -> None:
        sender._props[key] = convert(value)   # Props is observable: the assignment queues the update
    return push


class EmiyaBars(ui.element, component="emiya_bars.js"):
    """Waveform bars of one kind; every property below is bindable and pushed on change."""

    # on_change only fires when a value *changes*; __init__ writes the first props itself.
    bars = binding.BindableProperty(on_change=_pusher("bars"))
    marks = binding.BindableProperty(on_change=_pusher("marks"))
    sel_pct = binding.BindableProperty(on_change=_pusher("sel_pct"))
    progress = binding.BindableProperty(on_change=_pusher("progress"))
    tracing = binding.BindableProperty(on_change=_pusher("tracing"))
    trace_progress = binding.BindableProperty(on_change=_pusher("trace_progress"))
    aria = binding.BindableProperty(on_change=_pusher("aria"))
    reduce_always = binding.BindableProperty(on_change=_pusher("reduce_always"))

    def __init__(self, kind: BarKind, *, bars: Sequence[float | tuple[float, str]] = (),
                 marks: Sequence[ClipMark] = (), sel_pct: float | None = None, progress: float | None = None,
                 tracing: bool = False, trace_progress: float = 0.0, aria: str = "",
                 reduce_always: bool = False) -> None:
        """
        :param kind: "forge", "voices" or "compare"
        :param bars: (value, zone) pairs or plain values 0..1; empty draws a flat 3 px row
        :param marks: Forge marks
        :param sel_pct: Forge selection end in percent of the clip (None = no selection edges)
        :param progress: played part 0..1 drawn as a gold second row (None = no such row)
        :param tracing: Forge: show the Trace On overlay
        :param trace_progress: its reveal 0..1
        :param aria: accessible label (role img); "" hides the bars from assistive technology
        :param reduce_always: Reduce Motion is "Always" (else the component follows Windows)
        """
        if kind not in KINDS:
            raise ValueError(f"EmiyaBars kind must be one of {', '.join(KINDS)}, got {kind!r}")
        super().__init__()
        self._props["kind"] = kind
        values = {"bars": bars, "marks": marks, "sel_pct": sel_pct, "progress": progress, "tracing": tracing,
                  "trace_progress": trace_progress, "aria": aria, "reduce_always": reduce_always}
        for name, value in values.items():
            setattr(self, name, value)          # first assignment: on_change does not run ...
            key, convert = _PROPS[name]
            self._props[key] = convert(value)   # ... so the prop is written here
