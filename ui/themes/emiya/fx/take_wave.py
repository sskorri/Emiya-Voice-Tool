"""EmiyaTakeWave: the waveform box of Speak's Take card (230 bars, playhead, scrub) and its generating trace.

The browser half is emiya_take_wave.js. Python owns what the box shows, a WaveVM from the behaviours
(SpeakVM.card.wave); the browser owns the motion. While the shell's one <audio> (the kit Player) plays this
take, the component's own frame loop, capped like the forge field, moves the playhead and writes the time
text into the card's time element, because the Player reports to Python at most four times a second; Python
sets that text only while paused and on seek. Every colour comes from the theme stylesheet (the classes
emiya-wave-bar, is-played, emiya-wave-playhead, emiya-wave-glow and the emiya-trace-* parts; the mockup's
animation classes vt-rise, vt-head-glow and vt-spark; vt-scrub and vt-wavebox for the scrub input).

Bind it from the VMs with nicegui.binding, for example:

    wave = EmiyaTakeWave(take_url=ctx.core.take_url, time_target=time_label.html_id, on_seek=playback.seek)
    binding.bind_from(wave, "wave", speak.vm, "card", backward=lambda card: card.wave)
    binding.bind_from(wave, "reduce_always", status.vm, "reduce_always")
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import binding, ui
from nicegui.events import GenericEventArguments

from ui.contract.manifest import DEFAULT_FPS_CAP
from ui.contract.viewmodels import WaveVM

BAR_DECIMALS = 3                        # a bar is 3 + v * 50 px, so three decimals of v are exact enough


def wave_props(wave: WaveVM, take_url: Callable[[int], str]) -> dict[str, Any]:
    """The component props for one WaveVM: take (None without a take), generating, new-seq, spark-seq."""
    take = None
    if wave.take_id:
        take = {
            "id": wave.take_id,
            "bars": [round(float(v), BAR_DECIMALS) for v in wave.bars],
            "length_s": float(wave.length_s),
            "src": take_url(wave.take_id),
        }
    return {
        "take": take,
        "generating": {"active": wave.generating, "fill": wave.fill},
        "new-seq": wave.new_seq,
        "spark-seq": wave.spark_seq,
    }


def reduce_prop(reduce_always: bool) -> bool | str:
    """Reduce Motion for a component: True for "Always", "system" to follow Windows."""
    return True if reduce_always else "system"


class EmiyaTakeWave(ui.element, component="emiya_take_wave.js"):
    """The 692x56 waveform box: the generating trace, the take's bars with playhead and scrub, or nothing.

    ``seek`` (the scrub input released) calls ``on_seek(seconds)``.
    """

    # on_change only fires when a value *changes*; __init__ writes the first props itself.
    wave = binding.BindableProperty(on_change=lambda sender, _: sender._refresh())
    reduce_always = binding.BindableProperty(on_change=lambda sender, _: sender._refresh())

    def __init__(self, *, take_url: Callable[[int], str], audio_id: str = "", time_target: str = "",
                 wave: WaveVM | None = None, reduce_always: bool = False, fps_cap: int = DEFAULT_FPS_CAP,
                 on_seek: Callable[[float], Any] | None = None) -> None:
        """
        :param take_url: core.take_url; the playhead moves only while the audio plays this URL
        :param audio_id: html_id of the shell's Player (the <audio> or an element around it); "" = the page's <audio>
        :param time_target: html_id of the card's time element (a label); "" = the loop writes no text
        :param wave: the initial WaveVM
        :param reduce_always: Reduce Motion is "Always" (else the component follows Windows)
        :param fps_cap: frame cap of the playhead and trace loops ([fx] fps_cap)
        :param on_seek: called with the scrubbed position in seconds
        """
        super().__init__()
        self._take_url = take_url
        self._props["audio-id"] = audio_id
        self._props["time-target"] = time_target
        self._props["fps-cap"] = fps_cap
        self.wave = wave if wave is not None else WaveVM()
        self.reduce_always = reduce_always
        self._props.update(self._derived())
        if on_seek is not None:
            self.on("seek", lambda e: on_seek(_seconds(e)))

    def _derived(self) -> dict[str, Any]:
        return {**wave_props(self.wave, self._take_url), "reduce": reduce_prop(self.reduce_always)}

    def _refresh(self) -> None:
        derived = self._derived()
        if any(self._props.get(key) != value for key, value in derived.items()):
            self._props.update(derived)   # Props is observable: this queues one element update


def _seconds(event: GenericEventArguments) -> float:
    try:
        return max(0.0, float(event.args))
    except (TypeError, ValueError):
        return 0.0
