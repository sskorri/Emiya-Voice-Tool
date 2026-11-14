"""Per-client view models (§5.12) and the controller protocols a theme calls (§5.2-§5.11).

Top-level VMs are bindable dataclasses that a controller recomputes; everything nested in them is a
frozen value that is replaced as a whole. Themes only read VMs; every change goes through an intent.

Tone vocabularies (a theme maps each value to its own classes; colours never appear in a VM):
- TextTone: the colour tokens of §6.4, by name.
- ChipTone: "base", "gold", "red", "off" (§6.3 Chip). DotTone: "gold" (filled), "hollow", "red".
- RowState: a backend's state on the dock, the Models cards and the Speak group chips.
- Bars: (value 0..1, zone) pairs. Forge zones: "timbre" (selected, first 10 s), "selection", "outside".
  Voices zones: "timbre" (first 10 s), "rest".
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from nicegui import binding

from ui.contract.plans import ActionState, ConfirmRequest, PlayerTarget, Toast
from ui.contract.records import CheckRow, ClipMark
from ui.contract.routes import Route

TextTone = Literal["text", "text-strong", "text-soft", "muted", "faint", "disabled", "gold", "gold-dim", "warn", "bad"]
ChipTone = Literal["base", "gold", "red", "off"]
DotTone = Literal["gold", "hollow", "red"]
RowState = Literal["resident", "not_loaded", "unloading", "loading", "failed"]
Bars = tuple[tuple[float, str], ...]


# ---- shared rows ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchItem:
    """One rail search result."""

    item_id: str
    label: str                      # voice id, take text or action name
    route_text: str                 # "Voices · Multilingual · Japanese", "Takes · aiko · Take 7", "Model › Action"


@dataclass(frozen=True)
class LangItemVM:
    """One entry of a 23-language list."""

    code: str
    name: str
    selected: bool
    mark: str                       # "voice default" or ""


@dataclass(frozen=True)
class TakeRowVM:
    """One take row (Speak's session list and the Takes page)."""

    id: int
    text: str
    lang: str                       # lang attribute of the text cell
    voice: str
    model_label: str
    expr_text: str                  # "0.50" or "—"
    guid_text: str
    length_text: str                # "4.6 s" or "Missing"
    when_text: str                  # "14:31", "Nov 11", "2025"
    when_tip: str                   # the full date
    current: bool
    pin_a: bool
    pin_b: bool
    starred: bool
    missing: bool
    is_new: bool                    # replays the row's enter animation
    flash_seq: int = 0              # bumps to replay the flash of a search jump (0 = no flash)


@dataclass(frozen=True)
class BindResult:
    """Outcome of CommandRegistry.bind: the toast text, red when not ok."""

    ok: bool
    text: str                       # "Bound Ctrl+K", "Cleared the binding", "<combo> is already used by <label>"


# ---- shell ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DockRowVM:
    """One row of the rail's Model dock."""

    backend: str
    label: str
    tip: str                        # row title; adds " · Not loaded yet on this PC" without history
    state: RowState
    info: str                       # "3.4 GB", "~14 s", "Freeing", "7 s", "Failed" or ""
    info_tone: TextTone
    enabled: bool
    dim: bool                       # busy and not the loading or unloading row (opacity 0.55)
    ring: float | None              # loading: fill 0..0.95, None = indeterminate arc
    flash_seq: int                  # bumps to replay the flash
    spark_seq: int                  # bumps to replay the spark


@binding.bindable_dataclass
class ShellVM:
    """Rail, header, dock, VRAM block, popover, log, toast and art (StatusController)."""

    title: str = ""
    route: str = "/"
    chip_text: str = "No model loaded"
    chip_tone: ChipTone = "base"
    chip_dot: DotTone = "hollow"
    dock_rows: tuple[DockRowVM, ...] = ()
    dock_status: str = "No model loaded"
    dock_status_tone: TextTone = "muted"
    vram_value: str = "—"
    vram_gold: bool = False
    vram_app_pct: float = 0.0
    vram_other_pct: float = 0.0
    vram_detail: str = "Detecting GPU…"
    vram_tip: str = ""
    meter_visible: bool = True
    free: ActionState = ActionState(False, "Free VRAM")
    show_failed_actions: bool = False       # Retry and Load <default> replace Free VRAM
    retry: ActionState = ActionState(False, "Retry")
    load_default: ActionState = ActionState(False, "")
    ask: ConfirmRequest | None = None       # the switch popover
    ask_dont: bool = False                  # the "Don't ask again" toggle (UI state until the primary)
    ask_dont_visible: bool = False
    ask_then: str = ""                      # "Then generates take 8" or ""
    close: ActionState = ActionState(True, "Close · frees VRAM", tone="danger")
    log_open: bool = False
    toast: Toast | None = None
    art_visible: bool = False
    art_page: str = ""                      # where the art sits: "forge" | "speak" | ""
    script_dark: bool = False               # the library has 0 voices
    reduce_always: bool = False             # Reduce Motion is "Always"
    logo_still: bool = False
    search_hint: str = "Type a voice, a take or an action"
    search_items: tuple[SearchItem, ...] = ()
    search_badge: str = "Ctrl K"


# ---- speak ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceItemVM:
    """One voice row in a Speak group."""

    voice_id: str
    sub: str                        # "Japanese · 0.50", "English · tags", "no .pt · re-forge"
    sub_tone: TextTone
    selected: bool
    flash_seq: int
    audition_label: str             # "Audition the reference clip of aiko"
    enabled: bool


@dataclass(frozen=True)
class VoiceGroupVM:
    """One backend group in Speak's Voice panel (groups without voices are not listed)."""

    backend: str
    label: str
    chip: str                       # "Resident", "Freeing…", "Loading · 7 s", "Not loaded · ~14 s", ...
    chip_tone: ChipTone
    enabled: bool                   # False: "Not enabled", rows disabled
    rows: tuple[VoiceItemVM, ...]


@dataclass(frozen=True)
class WaveVM:
    """Props of the take waveform."""

    take_id: int = 0                # 0 = no take
    bars: tuple[float, ...] = ()    # 230 values 0..1, normalised to the take's own maximum
    length_s: float = 0.0
    generating: bool = False        # draw the circuit trace instead of bars
    fill: float | None = None       # trace reveal 0..1; None = loop every 3 s
    new_seq: int = 0                # bumps when a new take arrives (bars rise)
    spark_seq: int = 0


@dataclass(frozen=True)
class TakeCardVM:
    """Speak's Take card."""

    visible: bool = False
    generating: bool = False
    pinned: bool = False            # D19: stays on a playing take while the next queued take generates
    title: str = ""                 # "Take 7"
    caption: str = ""
    caption_warn: str = ""          # " · cut at 40 s" or ""
    star_visible: bool = False
    starred: bool = False
    wave: WaveVM = WaveVM()
    play: ActionState = ActionState(False, "Play")
    time_text: str = "0:00.0 / 0:00.0"     # set while paused and on seek; the wave writes it while playing
    ab_visible: bool = False
    ab_a_label: str = ""            # "A · Take 5"
    ab_b_label: str = ""
    ab_current: str = ""            # "A" | "B" | ""
    save: ActionState = ActionState(False, "Save As…")


@dataclass(frozen=True)
class GeneratingRowVM:
    """The "Generating…" row on top of Speak's session list (never for a test line)."""

    take_no: int
    voice: str
    expr_text: str
    guid_text: str


@binding.bindable_dataclass
class SpeakVM:
    """The Speak page."""

    library_state: str = "loading"          # "loading" | "empty" | "ready" | "failed" | "corrupt"
    library_message: str = ""
    groups: tuple[VoiceGroupVM, ...] = ()
    count_text: str = ""                    # "5 voices"
    show_sample: bool = True                # sample.wav exists ("Use sample.wav" enabled)
    sample_tip: str = ""
    delivery_enabled: bool = False
    presets: tuple[tuple[str, bool], ...] = ()   # (label, on)
    expr: float = 0.5
    expr_text: str = "—"
    expr_tone: TextTone = "text"
    expr_tip: str = ""
    expr_tick: float | None = None          # the registered value
    guid: float = 0.5
    guid_text: str = "—"
    language_mode: str = "disabled"         # "pick" | "locked" | "disabled"
    language_name: str = "English"
    lang_open: bool = False
    lang_items: tuple[LangItemVM, ...] = ()
    show_reset: bool = False
    note: str = ""
    note_tone: TextTone = "muted"
    show_guid0: bool = False
    stats_text: str = "Empty"
    stats_warn: str = ""                    # " · may be cut at 40 s" or ""
    toolbar_mode: str = "none"              # "chip" | "fix" | "tags" | "bad" | "none"
    lang_chip_text: str = ""
    tags: tuple[str, ...] = ()
    clear: ActionState = ActionState(False, "Clear")
    text_lang: str = "en"
    generate: ActionState = ActionState(False, "Pick a Voice", tone="grey")
    generate3: ActionState = ActionState(False, "Generate 3 Takes", tone="plain")
    show_cancel_queue: bool = False
    summary_text: str = ""
    summary_tone: TextTone = "muted"
    card: TakeCardVM = TakeCardVM()
    session_visible: bool = False
    session_rows: tuple[TakeRowVM, ...] = ()
    generating_row: GeneratingRowVM | None = None


# ---- voices --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceListRowVM:
    """One row of the Voices library list."""

    voice_id: str
    model_label: str
    model_resident: bool
    model_tip: str
    language_name: str
    expr_text: str                  # "0.50" or "tags"
    clip_text: str                  # "11.2 s" or "—"
    clip_missing: bool
    created_text: str               # "Today", "Nov 8", "2025"
    created_tip: str
    selected: bool


@dataclass(frozen=True)
class VoiceDetailsVM:
    """The Voices details card for one voice."""

    voice_id: str
    title: str
    caption: str
    chip_text: str
    chip_tone: ChipTone
    play: ActionState
    playing: bool
    bars: Bars                      # 138 bars over the whole clip
    bars_aria: str
    language_mode: str              # "pick" | "locked" | "disabled"
    language_name: str
    lang_open: bool
    lang_items: tuple[LangItemVM, ...]
    expr_enabled: bool
    expr: float
    expr_text: str
    ref_text: str
    ref_tip: str
    ref_missing: bool
    cache_text: str
    cache_missing: bool
    reveal_enabled: bool
    consent_ok: bool
    consent_text: str
    edits_enabled: bool
    speak_label: str                # "Speak with aiko"
    reforge: ActionState
    copy: ActionState               # "Forge on Turbo…"
    copy_visible: bool
    replace: ActionState
    delete: ActionState


@binding.bindable_dataclass
class VoicesVM:
    """The Voices page."""

    query: str = ""
    backend_filter: str | None = None       # None = All
    segments: tuple[tuple[str, str, bool], ...] = ()   # (key, label, on); key "" = All
    count_text: str = ""
    rows: tuple[VoiceListRowVM, ...] = ()
    empty_text: str = ""
    details: VoiceDetailsVM | None = None
    no_details_text: str = "Pick a voice to see its details."


# ---- forge ---------------------------------------------------------------------------------------

@binding.bindable_dataclass
class ForgeVM:
    """The Forge page (the draft itself lives in the process-wide ForgeDraft)."""

    clip_state: str = "empty"               # "empty" | "reading" | "ready" | "failed"
    clip_file: str = "No clip yet"
    clip_meta: str = "Browse for a WAV of one speaker, 6–15 s"
    clip_meta_tone: TextTone = "muted"
    bars: Bars = ()                         # 230 bars over the whole source
    marks: tuple[ClipMark, ...] = ()
    sel_pct: float = 100.0                  # selection end, percent of the clip
    bars_aria: str = ""                     # "Waveform of sample.wav, 13.4 s selected" (M L3039)
    clip_note: str = ""
    checks: tuple[CheckRow, ...] = ()
    play: ActionState = ActionState(False, "Play the reference clip", tone="plain")
    playing: bool = False
    browse: ActionState = ActionState(True, "Browse…", tone="plain")
    name: str = ""
    name_msg: str = "Becomes the file name, for example rin"
    name_tone: TextTone = "muted"
    name_bad: bool = False                  # the input text turns red
    fix_label: str = ""                     # "Use rin-2"; "" = no fix button
    segments: tuple[tuple[str, str, bool], ...] = ()   # (backend, label, on)
    model_note: str = ""
    model_note_tone: TextTone = "muted"
    lang_locked: bool = False               # Standard and Turbo: "English" + "English only"
    language_name: str = "English"
    lang_open: bool = False
    lang_items: tuple[LangItemVM, ...] = ()
    expr: float = 0.5
    expr_text: str = "0.50"
    expr_enabled: bool = True
    expr_note: str = "Starting value on Speak"
    consent: bool = False
    consent_req_tone: TextTone = "gold"     # "Required" is gold while consent is off
    inputs_enabled: bool = True
    forge: ActionState = ActionState(False, "Pick a Clip First", tone="grey")
    tracing: bool = False                   # the Trace On overlay (cosmetic)
    trace_progress: float = 0.0
    forged: str = ""                        # the forged voice id; "" = no result row
    speak_label: str = ""
    test_line: ActionState = ActionState(False, "Play Test Line", tone="plain")
    another: ActionState = ActionState(True, "Forge Another", tone="plain")


# ---- takes ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CompareSideVM:
    """One side (A or B) of the Takes compare card."""

    side: str                       # "A" | "B"
    take_id: int
    title: str                      # "A · Take 5"
    title_active: bool              # the current take (gold)
    length_text: str                # "4.6 s" or "4.6 s · missing"
    bars: tuple[float, ...]         # 110 values 0..1, scaled to the louder take
    progress: float                 # played part 0..1 (current side only)
    missing: bool


@dataclass(frozen=True)
class CompareRowVM:
    """One row of the compare table."""

    label: str                      # "Voice", "Model", "Language", "Expressiveness", "Guidance", "Length"
    a: str
    b: str
    differs: bool


@dataclass(frozen=True)
class CompareVM:
    """The Takes compare card (None in TakesVM until two takes are pinned)."""

    sides: tuple[CompareSideVM, ...]
    rows: tuple[CompareRowVM, ...]
    note: str
    note_differs: bool
    play_label: str                 # "Play A" | "Play" | "Pause"
    playing: bool
    time_text: str
    show_ab: bool
    keep_a: ActionState
    keep_b: ActionState
    reuse: ActionState


@binding.bindable_dataclass
class TakesVM:
    """The Takes page."""

    tab: str = "session"                    # "session" | "all" | "starred"
    list_title: str = "This Session"
    count_text: str = ""
    query: str = ""
    voice_label: str = "All Voices"
    model_label: str = "All Models"
    voice_options: tuple[str, ...] = ()     # voices that have takes, most recent first
    model_options: tuple[tuple[str, str], ...] = ()   # (backend, label)
    rows: tuple[TakeRowVM, ...] = ()
    more_count: int = 0                     # "Show 120 more"
    empty_text: str = ""
    export: ActionState = ActionState(False, "Export Starred…", tone="plain")
    folder: ActionState = ActionState(True, "Show Folder", tone="plain")
    purge: ActionState = ActionState(False, "Remove Unstarred", tone="plain")
    foot_text: str = ""
    compare: CompareVM | None = None


# ---- models --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FactVM:
    """One fact row on a Models card."""

    label: str                      # "Languages", "Controls", "Reference clip", "Measured VRAM", ...
    value: str
    tip: str = ""


@dataclass(frozen=True)
class ModelCardVM:
    """One Models page card."""

    backend: str
    label: str
    tip: str
    state: RowState
    chip_text: str
    chip_tone: ChipTone
    ring: float | None              # loading: fill 0..0.95, None = indeterminate
    facts: tuple[FactVM, ...]
    action: ActionState
    action_kind: str                # "load" | "switch" | "unload"
    ringed: bool                    # resident: inset gold ring
    flash_seq: int


@binding.bindable_dataclass
class ModelsVM:
    """The Models page (also feeds the dock and the popover)."""

    cards: tuple[ModelCardVM, ...] = ()
    vram_value: str = "—"
    vram_gold: bool = False
    vram_app_pct: float = 0.0
    vram_other_pct: float = 0.0
    vram_detail: str = "Detecting GPU…"
    vram_tip: str = ""
    meter_visible: bool = True
    free: ActionState = ActionState(False, "Free VRAM", tone="plain")
    legend_app: str = ""                    # "This app · 3.4 GB · Multilingual"
    legend_other: str = ""                  # "Other apps · 2.5 GB"
    legend_free: str = ""                   # "Free · 6.1 GB"
    gpu_line: str = "Detecting the GPU…"
    gpu_tip: str = ""
    warmup: str = "off"                     # "off" | "last"
    idle_min: int = 10                      # 0 = Never, 10, 30, 60
    confirm_switches: bool = True
    limit_text: str = "One at a time"
    limit_tip: str = ""
    history: tuple[tuple[str, str], ...] = ()   # (text, tone) with tone "old" | "today" | "failed"
    history_empty: bool = True


# ---- settings ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyRowVM:
    """One row of the Keys table."""

    id: str
    label: str
    key_text: str                   # "Ctrl+Enter" or "—"
    alt_text: str
    key_listening: bool
    alt_listening: bool
    key_aria: str                   # "Generate key: Ctrl+Enter"
    alt_aria: str                   # "Generate alternate key: none"


@dataclass(frozen=True)
class AboutVM:
    """The About card."""

    engine: str = ""
    gpu: str = ""
    gpu_tip: str = ""
    models_in_vram: str = ""
    registry: str = ""
    registry_tip: str = ""
    cache: str = ""
    cache_tip: str = ""
    clips: str = ""
    clips_tip: str = ""
    takes: str = ""


@binding.bindable_dataclass
class SettingsVM:
    """The Settings page."""

    tab: str = "general"                    # "general" | "effects" | "keys" | "voice_chat"
    out_dir_text: str = ""                  # absolute Output Folder
    out_tip: str = ""
    browse: ActionState = ActionState(True, "Browse…", tone="plain")
    autoplay: bool = True
    stop_drops_queue: bool = True
    device: str = "cuda"                    # "cuda" | "cpu"
    device_note: str = ""
    default_backend: str = ""
    backend_options: tuple[tuple[str, str], ...] = ()   # (backend, label)
    offline: bool = True
    fx_on: bool = True
    fx_speed: int = 100
    fx_speed_text: str = "100%"
    work_glow: bool = True
    reduce_motion: str = "system"           # "system" | "always"
    saber_art: str = "show"                 # "show" | "hide"
    key_rows: tuple[KeyRowVM, ...] = ()
    about: AboutVM = AboutVM()
    call_enabled: bool = False
    call_devices: tuple[tuple[str, str], ...] = ()
    call_device: str = ""
    call_note: str = ""
    call_send: str = "preview"
    call_hear: bool = True
    call_overlap: str = "wait"
    call_delay: str = "0"
    call_shortcuts: str = ""


# ---- playback ------------------------------------------------------------------------------------

@binding.bindable_dataclass
class PlaybackVM:
    """The shell's one audio player: the target it reconciles to and what it last reported (≤ 4 Hz)."""

    target: PlayerTarget = PlayerTarget()
    playing: bool = False
    position: float = 0.0
    duration: float = 0.0
    call_enabled: bool = False
    call_available: bool = False
    call_active: bool = False
    call_hear: bool = True
    call_switch_visible: bool = False
    call_status: str = ""


# ---- controller protocols ------------------------------------------------------------------------
#
# Every intent a theme calls. An intent never raises into the theme: it refuses quietly (or with a toast) what the
# current state does not allow, and nothing here loads a model unless the docstring says the click names one.

@runtime_checkable
class Confirmable(Protocol):
    """A controller that answers an ask."""

    async def confirm(self, token: str) -> None:
        """The ask's primary button, with the token of the ConfirmRequest it showed (a void token does nothing)."""

    def dismiss(self) -> None:
        """Cancel, Escape or a click outside: drop the ask or the arm this controller raised."""


@runtime_checkable
class StatusController(Protocol):
    """Chip, dock rows, VRAM texts, close arm, log, toasts and the title (§5.2)."""

    vm: ShellVM

    def toggle_log(self) -> None:
        """Open or close the Log panel (the status chip)."""

    def close_log(self) -> None:
        """Close the Log panel."""

    def request_quit(self) -> None:
        """The Close button: quit when idle; while a job runs the first click arms for 3 s, a second one quits."""

    def minimize(self) -> None:
        """Minimize the window; in --browser mode a toast says it needs the app window."""

    def on_route(self, path: str) -> None:
        """The shell's route changed: set the title, close the popover, the lists, the search and a key capture,
        and disarm what the old page armed."""

    def escape(self) -> None:
        """D12: Escape closes the Log, the switch popover, the search results and the language lists."""

    def dismiss_toast(self) -> None:
        """Hide the toast shown now (it also hides itself after 2.6 s)."""


@runtime_checkable
class ModelsController(Confirmable, Protocol):
    """Switch requests (dock, Models card, Retry, Load <default>), Free VRAM, behaviour settings (§5.3)."""

    vm: ModelsVM

    def request_switch(self, backend: str, *,
                       origin: Literal["dock", "card", "retry", "default"]) -> ConfirmRequest | None:
        """A dock row, a Models card, Retry or Load <default>: start the switch the control names, or return the
        switch popover's request when it must ask first (low VRAM, Confirm Model Switches), or do nothing."""

    async def confirm(self, token: str) -> None:
        """The switch popover's primary: start the asked switch with its after-action; commit "Don't ask again"."""

    def dismiss(self) -> None:
        """Cancel, Escape or the same dock row again: close the popover; nothing loads."""

    def set_dont_ask(self, on: bool) -> None:
        """The popover's "Don't ask again" toggle (saved only when the primary is pressed)."""

    async def free_vram(self) -> None:
        """Free VRAM: unload the resident model as a job; nothing resident or a busy slot is a red toast."""

    def set_warmup(self, mode: Literal["off", "last"]) -> None:
        """Warm Up on Launch: "off" or "last" (load the last model at the next launch)."""

    def set_idle_unload(self, minutes: int) -> None:
        """Auto-unload After Idle: 0 (never), 10, 30 or 60 minutes without a switch, take or forge."""

    def set_confirm_switches(self, on: bool) -> None:
        """Confirm Model Switches: whether an unnamed switch (a dock row) asks before it loads."""


@runtime_checkable
class PlaybackController(Protocol):
    """Current take, play and pause, A/B, pins, stars, audition, Save As, reveal (§5.4)."""

    vm: PlaybackVM

    def send_take(self, take_id: int) -> None:
        """Send the indexed WAV to the chosen output when calls are enabled. Refuse missing takes or outputs."""

    def stop_sending(self) -> None:
        """Stop the outgoing stream and clear waiting sends, including a pending start delay."""

    def play_take(self, take_id: int) -> None:
        """Make the take current and play it from 0 (a missing WAV is a red toast)."""

    def toggle(self) -> None:
        """Play or pause the current take; at its end, play restarts it from 0."""

    def seek(self, seconds: float) -> None:
        """Move the current take to `seconds` (the play state stays)."""

    def ab_switch(self, take_id: int) -> None:
        """Make that pinned take current at the same point of the line, keeping the play state."""

    def pin(self, take_id: int, side: Literal["A", "B"]) -> None:
        """Toggle the take's A or B pin; a take is never both A and B."""

    def star(self, take_id: int) -> None:
        """Toggle the take's star, saved in click order (a failure is the red toast "Couldn't save the star: …")."""

    def prev(self) -> None:
        """Play the newer row of "Takes · This Session"."""

    def next(self) -> None:
        """Play the older row of "Takes · This Session"."""

    def audition(self, voice_id: str) -> None:
        """Play the voice's reference clip (a take pauses); a second call stops it."""

    def play_url(self, url: str) -> None:
        """Play a staged clip or a test line from 0."""

    def stop(self) -> None:
        """Stop what plays: a clip or URL rewinds to 0, a take pauses where it is."""

    def on_player_state(self, playing: bool, position: float, duration: float, ended: bool, error: str) -> None:
        """The shell's Player reports its target (at most 4 times a second; stale reports are dropped)."""

    async def save_as(self) -> None:
        """Save As… for the current take: the native save dialog, then a copy and "Saved aiko_take7.wav"."""

    def reveal(self) -> None:
        """Show the current take's WAV in Explorer (a missing file is a red toast)."""


@runtime_checkable
class CommandRegistry(Protocol):
    """Key commands: ids, bindings, clash and reserved checks, dispatch (§5.5)."""

    def normalize(self, key: str, *, ctrl: bool, shift: bool, alt: bool) -> str:
        """A keydown as a combo: "Ctrl+Shift+Enter"; "" for a modifier on its own."""

    def bindings(self) -> Mapping[str, tuple[str, str]]:
        """{command id: (key, alternate)} in command order; "" = no binding."""

    def combos(self) -> Mapping[str, str]:
        """{combo: command id}, for the shell's KeyDispatcher."""

    def hint_parts(self, command_id: str) -> tuple[str, ...]:
        """The key hint chips of a command: ("Ctrl", "Enter"); the alternate when the key is cleared; () if none."""

    def bind(self, command_id: str, slot: Literal["key", "alt"], combo: str) -> BindResult:
        """Bind (or with "" clear) one slot; refused for a clash or a reserved combo. The result's text is the
        toast, red when not ok."""

    def handle(self, command_id: str, route: str) -> None:
        """Run a command for the current route; Speak-only commands do nothing elsewhere."""

    def set_focus_search(self, focus: Callable[[], Any] | None) -> None:
        """The shell's way to focus its rail search box (the "search" command); None while there is none."""


@runtime_checkable
class SearchController(Protocol):
    """The rail search over voices, takes and two actions (§5.6)."""

    def results(self, query: str) -> tuple[SearchItem, ...]:
        """The matches for query (also published as ShellVM.search_items and search_hint)."""

    def run(self, item_id: str) -> Route | None:
        """Act on a result: select or play it, or run the action; returns the route to open (None: stay)."""

    def clear(self) -> None:
        """Escape, a pick or navigation: empty the results."""


@runtime_checkable
class SpeakController(Confirmable, Protocol):
    """The Speak page (§5.7): voice, delivery, script and Generate."""

    vm: SpeakVM

    def stop_take(self) -> None:
        """Cancel the running Speak take and drop its waiting takes. A model load or Forge continues."""

    def set_call_switch(self, on: bool) -> None:
        """Set this session's automatic-send switch. Generate snapshots it when switch mode is active."""

    def pick_voice(self, voice_id: str) -> None:
        """Select a voice with its own language and Expressiveness, Guidance 0.5; loads nothing."""

    def set_text(self, text: str) -> None:
        """The script as typed; typing disarms Clear."""

    def insert_tag(self, tag: str, caret: int) -> int:
        """Insert a Turbo tag at the browser caret (UTF-16 units); returns the caret after it (Turbo voices only)."""

    def set_delivery(self, *, exaggeration: float | None = None, cfg_weight: float | None = None,
                     language: str | None = None) -> None:
        """Expressiveness 0.25-2 and Guidance 0-1 on the 0.05 grid, or a language (Multilingual); never Turbo."""

    def apply_preset(self, name: Literal["Default", "Slower", "Dramatic"]) -> None:
        """A delivery preset: Default (0.5, 0.5), Slower (0.5, 0.3) or Dramatic (0.7, 0.3)."""

    def reset_delivery(self) -> None:
        """The voice's own Expressiveness and language, Guidance 0.5."""

    def set_guidance_zero(self) -> None:
        """Guidance 0, offered while the language differs from the clip's and Guidance is above 0."""

    def use_japanese(self) -> None:
        """Japanese for a Multilingual voice whose script holds kana."""

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open, close (open=False) or toggle the language list; it opens only for a Multilingual voice."""

    def clear_text(self) -> ConfirmRequest | None:
        """Clear: the first press arms "Click Again to Clear" for 3 s (the arm's request), a second one empties."""

    async def generate(self, n: int = 1, token: str | None = None) -> ConfirmRequest | None:
        """n takes of the script: queued, started, or a switch with the token the button's named label carries;
        returns the switch popover's request when VRAM is low. A void token loads nothing."""

    def cancel_queue(self) -> None:
        """Drop the takes waiting behind the running one (the running take finishes)."""

    def opened(self) -> None:
        """The page was built: re-read a registry that changed outside the app (cli.py)."""


@runtime_checkable
class VoicesController(Confirmable, Protocol):
    """The Voices page (§5.8): the library, the details editor and the actions on a voice."""

    vm: VoicesVM

    def select(self, voice_id: str) -> None:
        """Show the voice in the details (disarms Delete)."""

    def set_filter(self, *, query: str | None = None, backend: str | None = None) -> None:
        """The filter text and the model segment ("" = All); with neither given, the segment goes back to All."""

    async def edit(self, voice_id: str, *, language: str | None = None, exaggeration: float | None = None) -> None:
        """Save a new language and/or default Expressiveness to the registry, in click order; no model loads."""

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open, close (open=False) or toggle the details' language list (Multilingual voices only)."""

    async def delete(self, voice_id: str) -> ConfirmRequest | None:
        """The first press arms "Click Again to Delete" (the arm's request); a second one within 3 s deletes."""

    def reforge(self, voice_id: str) -> Route | None:
        """Prefill the Forge draft from the voice and return "/forge" (None when refused)."""

    def forge_copy(self, voice_id: str) -> Route | None:
        """Prefill a copy of the voice on the other model ("Forge on Turbo…") and return "/forge"."""

    async def replace_clip(self, voice_id: str) -> Route | None:
        """Pick a new clip, then prefill a re-forge with it; None when cancelled or refused."""

    async def reveal_cache(self, voice_id: str) -> None:
        """Show the voice's .pt in Explorer, or open its model folder when the .pt is missing."""

    def speak_with(self, voice_id: str) -> Route:
        """Select the voice on Speak (its row flashes) and return "/"; nothing loads."""

    def toggle_clip_playback(self, voice_id: str) -> None:
        """Play the voice's reference clip; a second press stops it."""

    def opened(self) -> None:
        """The page was built: re-read a registry that changed outside the app (cli.py)."""


@runtime_checkable
class ForgeController(Confirmable, Protocol):
    """The Forge page (§5.9): the reference clip, the identity and Forge."""

    vm: ForgeVM

    async def browse(self) -> None:
        """Browse…: the native open dialog (an upload dialog in --browser), then stage the pick."""

    async def pick_clip(self, path: Path) -> None:
        """Stage a picked or dropped clip in a worker; the draft reads "reading" meanwhile."""

    async def drop_paths(self, paths: Sequence[str]) -> None:
        """A native drop on Forge (full paths): one clip at a time."""

    async def upload(self, name: str, data: bytes) -> None:
        """An uploaded clip (--browser), staged from its bytes like a pick."""

    async def use_sample(self) -> Route | None:
        """Speak's "Use sample.wav": stage voices\\reference\\sample.wav and return "/forge" (None when refused)."""

    def prefill(self, *, voice_id: str, mode: Literal["reforge", "copy"],
                clip_path: Path | None = None) -> Route | None:
        """Re-forge…, Forge on <model>… and Replace…: the draft from a voice, then "/forge" (None when refused)."""

    def set_name(self, name: str) -> None:
        """The Name box as typed (clears the result row and the replace arm)."""

    def fix_name(self) -> None:
        """"Use <suggested>": take the suggested name (or the existing spelling on a case clash)."""

    def set_backend(self, backend: str) -> None:
        """The Model segment; never loads (Multilingual restores its remembered language, the others are English)."""

    def set_language(self, code: str) -> None:
        """A pick in the 23-language list (Multilingual only)."""

    def toggle_language_list(self, open: bool | None = None) -> None:
        """Open, close (open=False) or toggle the language list (never while busy or on an English-only model)."""

    def set_exaggeration(self, value: float) -> None:
        """Default Expressiveness on release: 0.25-2.0, rounded to 0.01 (not for Turbo)."""

    def set_consent(self, given: bool) -> None:
        """"The speaker agreed to having this voice cloned" (Forge needs it)."""

    async def forge(self, token: str | None = None) -> ConfirmRequest | None:
        """Forge: arm a replace, ask on low VRAM (the popover's request), start the switch the button names with
        its token, or forge on the resident model."""

    def speak_with(self) -> Route | None:
        """"Speak with <name>": select the new voice on Speak and return "/"; nothing loads."""

    async def test_line(self) -> None:
        """"Play Test Line": one unsaved take of the new voice, played at once (refused unless its model is loaded)."""

    def forge_another(self) -> None:
        """"Forge Another": the name and the consent cleared, the Default Model; the clip stays."""

    def toggle_clip_playback(self) -> None:
        """Play the staged selection from 0, or stop it."""

    def opened(self) -> None:
        """The page was built: re-read a registry that changed outside the app (cli.py)."""


@runtime_checkable
class TakesController(Confirmable, Protocol):
    """The Takes page (§5.10): the list, its filters, the compare card and the footer."""

    vm: TakesVM

    def set_tab(self, tab: Literal["session", "all", "starred"]) -> None:
        """This Session, All or Starred (voids the purge arm; the list starts again at one page)."""

    def set_query(self, text: str) -> None:
        """The search box as typed; the list follows 150 ms after typing stops."""

    def set_voice_filter(self, voice_id: str | None) -> None:
        """Only that voice's takes; None = All Voices."""

    def set_model_filter(self, backend: str | None) -> None:
        """Only that model's takes; None = All Models."""

    def row_click(self, take_id: int) -> None:
        """Play the take from 0; on the current take, play or pause it. A missing WAV is a red toast."""

    def compare_play(self) -> None:
        """Play A from 0 when neither pinned take is current, else play or pause the current one."""

    def keep(self, side: Literal["A", "B"]) -> None:
        """Keep A / Keep B: star that pinned take (never unstar it) and say "Kept Take N · starred"."""

    def reuse_settings(self) -> Route:
        """Speak's delivery from take A (never the text); returns "/" (or "/takes" without an A)."""

    async def purge_unstarred(self) -> ConfirmRequest | None:
        """Remove Unstarred: the first click arms with the unstarred takes it counted (the arm's request); a second
        one within 3 s sends those still unstarred to the Recycle Bin."""

    async def export_starred(self) -> None:
        """Export Starred…: a folder dialog, then every starred take is copied there (needs the app window)."""

    def show_folder(self) -> None:
        """Open the Output Folder in Explorer, creating it first; a failure is a red toast."""

    def show_more(self) -> None:
        """"Show N more": the next 200 rows of the filtered list (TakesVM.more_count)."""

    def opened(self) -> None:
        """The page is being built (call it before the list): the list starts again at one page of rows."""


@runtime_checkable
class SettingsController(Confirmable, Protocol):
    """The Settings page (§5.11): General, Effects and Keys, and the About card."""

    vm: SettingsVM

    async def refresh_call_devices(self) -> None:
        """List playback endpoints and cable default roles without opening an audio stream."""

    def select_call_device(self, device_id: str) -> None:
        """Remember a listed endpoint or clear the selection with an empty ID."""

    def set_call_preference(self, field: str, value: bool | str | int) -> None:
        """Write a validated master, monitor, routing, overlap or start-delay preference."""

    def set_tab(self, tab: Literal["general", "effects", "keys", "voice_chat"]) -> None:
        """A header tab (stops a key capture)."""

    async def browse_output_folder(self) -> None:
        """Browse…: the native folder dialog and a write probe, then new takes go there (needs the app window)."""

    def set_autoplay(self, on: bool) -> None:
        """Autoplay New Takes (applies to the next take)."""

    def set_stop_drops_queue(self, on: bool) -> None:
        """Choose whether stopping the current take also cancels the waiting takes."""

    def set_device(self, device: Literal["cuda", "cpu"]) -> None:
        """Run Models On: used at the next model load; the resident model stays where it is."""

    def set_default_backend(self, backend: str) -> None:
        """Default Model for New Voices (an idle Forge draft follows)."""

    def set_offline(self, on: bool) -> None:
        """Offline Mode (applies after a restart)."""

    def set_reduce_motion(self, policy: Literal["system", "always"]) -> None:
        """Reduce Motion: follow Windows, or always (live, for every theme)."""

    def set_pref(self, key: Literal["fx_on", "fx_speed", "work_glow", "saber_art"],
                 value: bool | int | str) -> None:
        """One of the active theme's Effects prefs, saved (a pref the theme does not declare is refused)."""

    def preview_pref(self, key: Literal["fx_speed"], value: float) -> None:
        """Effect Speed while its range moves: shown live, saved only by set_pref on release."""

    def listen(self, command_id: str, slot: Literal["key", "alt"]) -> None:
        """A Keys slot was clicked: it listens for the next combination (one slot per client at a time)."""

    def stop_listening(self) -> None:
        """Blur, Tab, Escape, a tab change or navigation: the slot stops listening; nothing changes."""

    def capture(self, command_id: str, slot: Literal["key", "alt"], *, key: str, ctrl: bool, shift: bool,
                alt: bool, composing: bool = False) -> None:
        """A keydown on the listening slot: Escape cancels, Backspace or Delete clears, a combo binds (with a
        toast); nothing is captured while an IME composes."""
