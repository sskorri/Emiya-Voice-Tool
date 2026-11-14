"""CommandRegistry (§5.5): the eight key commands, their bindings (core.keys), checks and dispatch.

Combos are written as the Keys capture and vt_keys.js build them: Ctrl, Shift, Alt in that order, then
the key, with " " as "Space", one character upper case and "Arrow" dropped ("Ctrl+Shift+Enter",
"Alt+Up", "Ctrl++"). A hand-edited ui_settings.json may hold anything: bindings() reads every slot
leniently, clears what is not a combo, a reserved combination, and every later duplicate (the first
slot keeps it).
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from ui.contract.viewmodels import BindResult
from ui.contract.viewmodels import CommandRegistry as CommandRegistryProtocol
from ui.core.services.settings import CORE

from .status import normalise_route

if TYPE_CHECKING:
    from . import BehaviourFactory

__all__ = [
    "COMMANDS",
    "Command",
    "CommandRegistry",
    "canonical",
    "combo_parts",
    "effective_bindings",
    "label_of",
    "normalize_combo",
    "reserved_reason",
]

Slot = Literal["key", "alt"]
Scope = Literal["speak", "every", "global"]


@dataclass(frozen=True)
class Command:
    """One key command: its id, its label on the Keys tab and where it works."""

    id: str
    label: str
    scope: Scope


COMMANDS: tuple[Command, ...] = (                                   # M L1231-1240
    Command("gen", "Generate", "speak"),
    Command("gen3", "Generate 3 Takes", "speak"),
    Command("play", "Play / Pause", "every"),
    Command("prev", "Previous Take", "speak"),
    Command("next", "Next Take", "speak"),
    Command("ab", "Switch A / B", "every"),
    Command("save", "Save As…", "speak"),
    Command("search", "Search", "every"),
    Command("call_send", "Send to Call", "global"),
    Command("call_stop", "Stop Sending", "global"),
)
_BY_ID: Mapping[str, Command] = MappingProxyType({command.id: command for command in COMMANDS})
_MODIFIERS = ("Ctrl", "Shift", "Alt")
_MODIFIER_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt"}
_MODIFIER_KEYS = frozenset({"Shift", "Control", "Alt", "Meta", "AltGraph", "OS"})
_ZOOM_KEYS = frozenset({"=", "-", "+", "0"})                        # WebView2 zoom (WV/platforms/edgechromium.py)
_NAMED_KEYS = {name.lower(): name for name in (                    # KeyboardEvent.key names, for hand edits
    "Enter", "Escape", "Tab", "Backspace", "Delete", "Insert", "Home", "End", "PageUp", "PageDown",
    "Up", "Down", "Left", "Right", "Space", *(f"F{number}" for number in range(1, 25)))}
_SPEAK = "/"


def label_of(command_id: str) -> str:
    """"Generate", "Play / Pause", ...; "" for an unknown id."""
    command = _BY_ID.get(command_id)
    return command.label if command is not None else ""


def _key_name(key: str) -> str:
    """A keydown's key as vt_keys.js writes it: " " → "Space", one character upper case, "Arrow" dropped."""
    return "Space" if key == " " else key.upper() if len(key) == 1 else key.replace("Arrow", "", 1)


def _stored_key(key: str) -> str:
    """A key read from the file, leniently: "enter" → "Enter", "arrowup" → "Up"."""
    if len(key) > 5 and key[:5].lower() == "arrow":
        key = key[5:]
    key = _key_name(key)
    return _NAMED_KEYS.get(key.lower(), key)


def normalize_combo(key: str, *, ctrl: bool, shift: bool, alt: bool) -> str:
    """The combo for one keydown ("Ctrl+Shift+Enter"); "" for a modifier key on its own."""
    if not isinstance(key, str) or not key or key in _MODIFIER_KEYS:
        return ""
    parts = [name for name, on in zip(_MODIFIERS, (ctrl, shift, alt)) if on]
    return "+".join([*parts, _key_name(key)])


def combo_parts(combo: str) -> tuple[str, ...]:
    """("Ctrl", "Enter") for "Ctrl+Enter", ("Ctrl", "+") for "Ctrl++"; () for "" or a malformed combo."""
    if not isinstance(combo, str) or not combo:
        return ()
    if combo == "+":
        return ("+",)
    if combo.endswith("++"):
        head, key = combo[:-2], "+"
    elif combo.endswith("+"):
        return ()
    else:
        head, _, key = combo.rpartition("+")
    modifiers = head.split("+") if head else []
    if any(not part for part in modifiers):
        return ()
    return (*modifiers, key)


def canonical(combo: str) -> str:
    """A stored combo in the capture's form ("ctrl+k" → "Ctrl+K"); "" when it is not a combo."""
    parts = combo_parts(combo.strip() if isinstance(combo, str) else "")
    if not parts:
        return ""
    *modifiers, key = parts
    names: list[str] = []
    for part in modifiers:
        name = _MODIFIER_NAMES.get(part.lower())
        if name is None or name in names:
            return ""
        names.append(name)
    key = _stored_key(key)
    if not key or key in _MODIFIER_KEYS:
        return ""
    return "+".join([*(name for name in _MODIFIERS if name in names), key])


def reserved_reason(combo: str) -> str:
    """Why a combo cannot be bound (NEW copy, §5.5), or ""."""
    if combo == "Alt+F4":
        return "Alt+F4 is reserved by Windows"
    parts = combo_parts(combo)
    if parts and "Ctrl" in parts[:-1] and parts[-1] in _ZOOM_KEYS:
        return f"{combo} is the window's zoom"
    return ""


def effective_bindings(stored: Mapping[str, tuple[str, str]]) -> Mapping[str, tuple[str, str]]:
    """Resolve conflicts once across both window commands and Windows global shortcuts."""
    seen: set[str] = set()
    found: dict[str, tuple[str, str]] = {}
    for command in COMMANDS:
        slots = []
        for value in stored.get(command.id, ("", "")):
            combo = canonical(value)
            if not combo or combo in seen or reserved_reason(combo):
                combo = ""
            else:
                seen.add(combo)
            slots.append(combo)
        found[command.id] = (slots[0], slots[1])
    return MappingProxyType(found)


class CommandRegistry(CommandRegistryProtocol):
    """Key commands: ids, bindings, clash and reserved checks, dispatch (§5.5)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self._focus_search: Callable[[], Any] | None = None

    # ---------- bindings ----------

    def normalize(self, key: str, *, ctrl: bool, shift: bool, alt: bool) -> str:
        """"Ctrl+Shift+Enter"; " " → "Space"; one character upper case; "Arrow" dropped."""
        return normalize_combo(key, ctrl=ctrl, shift=shift, alt=alt)

    def bindings(self) -> Mapping[str, tuple[str, str]]:
        """{command id: (key, alternate)} in command order; "" = no binding."""
        return effective_bindings(self._core.settings.get(CORE, "keys"))

    def combos(self) -> Mapping[str, str]:
        """{combo: command id} for the KeyDispatcher."""
        return {combo: command_id for command_id, pair in self.bindings().items()
                if _BY_ID[command_id].scope != "global" for combo in pair if combo}

    def hint_parts(self, command_id: str) -> tuple[str, ...]:
        """The key hint chips: ("Ctrl", "Enter"); the alternate when the key is cleared; () with neither."""
        key, alt = self.bindings().get(command_id, ("", ""))
        return combo_parts(key or alt)

    def label(self, command_id: str) -> str:
        return label_of(command_id)

    def bind(self, command_id: str, slot: Literal["key", "alt"], combo: str) -> BindResult:
        """Bind (or with "" clear) one slot; the result's text is the toast, red when not ok."""
        if command_id not in _BY_ID:
            raise KeyError(f"unknown command {command_id!r}")
        if slot not in ("key", "alt"):
            raise ValueError(f"unknown slot {slot!r}")
        clean = canonical(combo) if combo else ""
        if combo and not clean:
            return BindResult(False, f"{combo} is not a key combination")
        if clean:
            refusal = reserved_reason(clean) or self._clash(clean, command_id, slot)
            if refusal:
                return BindResult(False, refusal)
        key, alt = self.bindings()[command_id]
        pair = (clean, alt) if slot == "key" else (key, clean)
        if pair != (key, alt):
            self._core.settings.set(CORE, "keys", {command_id: list(pair)})
        return BindResult(True, f"Bound {clean}" if clean else "Cleared the binding")

    def _clash(self, combo: str, command_id: str, slot: Slot) -> str:
        """"<combo> is already used by <label>": another command, or this command's other slot."""
        for other_id, pair in self.bindings().items():
            for other_slot, value in zip(("key", "alt"), pair):
                if value == combo and (other_id, other_slot) != (command_id, slot):
                    return f"{combo} is already used by {label_of(other_id)}"
        return ""

    # ---------- dispatch ----------

    def set_focus_search(self, focus: Callable[[], Any] | None) -> None:
        """The shell's way to focus the rail search box (the "search" command)."""
        self._focus_search = focus

    def handle(self, command_id: str, route: str) -> None:
        """Run a command for the current route; Speak-only commands do nothing elsewhere."""
        command = _BY_ID.get(command_id)
        if command is None or (command.scope == "speak" and normalise_route(route) != _SPEAK):
            return
        factory = self._factory
        if command_id in ("gen", "gen3"):
            self._generate(3 if command_id == "gen3" else 1)
        elif command_id == "play":
            factory.playback().toggle()
        elif command_id == "prev":
            factory.playback().prev()
        elif command_id == "next":
            factory.playback().next()
        elif command_id == "ab":
            self._ab_switch()
        elif command_id == "save":
            playback = factory.playback()
            if playback.current() is not None and not playback.card_generating():
                self._spawn(playback.save_as(), command_id)
        elif command_id == "search" and self._focus_search is not None:
            self._focus_search()

    def _generate(self, count: int) -> None:
        """speak.generate even while Generate is disabled by a take or a switch: it queues (§5.0)."""
        speak = self._factory.speak()
        action = speak.vm.generate if count == 1 else speak.vm.generate3
        pending = action.pending
        token = pending.token if pending is not None and pending.token else None
        self._spawn(speak.generate(count, token), "gen3" if count == 3 else "gen")

    def _ab_switch(self) -> None:
        """Both pins set: switch to the other pin; from neither, to A."""
        session = self._core.session
        if not (session.pin_a and session.pin_b):
            return
        target = session.pin_b if session.take_id == session.pin_a else session.pin_a
        self._factory.playback().ab_switch(target)

    def _spawn(self, result: Any, command_id: str) -> None:
        if inspect.isawaitable(result):
            self._core.scheduler.spawn(result, name=f"voicetool key {command_id}")
