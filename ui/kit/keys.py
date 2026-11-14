"""KeyDispatcher: the document-wide key handler (vt_keys.js) that turns bound combos into commands.

The shell creates one, feeds it CommandRegistry.combos(), suspends it while a Keys slot listens and
passes on_command to CommandRegistry.handle(id, route).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nicegui import binding
from nicegui.element import Element
from nicegui.events import GenericEventArguments

CommandHandler = Callable[[str], Any]


class KeyDispatcher(Element, component="vt_keys.js"):
    """Sends a command id for every pressed combo bound in `combos` (combo -> command id).

    The browser applies the rules ui.keyboard cannot: no command during IME composition or while
    `suspended`; in an input or textarea only Ctrl or Alt combos fire; on a button Space and Enter
    keep their meaning; a match calls preventDefault; a held key's repeats fire nothing.
    The `command` event carries {"id": <command id>, "combo": <combo>}; an id that no combo binds
    any more, or one that arrives while suspended, is dropped.
    """

    combos = binding.BindableProperty(on_change=lambda sender, value: sender._push_combos(value))
    suspended = binding.BindableProperty(on_change=lambda sender, value: sender._push_suspended(value))

    def __init__(self, combos: Mapping[str, str] | None = None, *, suspended: bool = False,
                 on_command: CommandHandler | None = None) -> None:
        """
        :param combos: combo -> command id, as CommandRegistry.combos() returns it
        :param suspended: True while a Keys slot listens for a new binding
        :param on_command: called with the command id of every combo that fires
        """
        super().__init__()
        self._on_command = on_command
        first = combo_map(combos or {})
        self.combos = first                     # first assignments: on_change does not run ...
        self.suspended = bool(suspended)
        self._props["combos"] = first           # ... so the initial props are set here
        self._props["suspended"] = bool(suspended)
        self.on("command", self._handle_command)   # listeners must exist before the element is first sent

    def set_combos(self, combos: Mapping[str, str]) -> None:
        """Apply new bindings live (always a fresh copy, so a changed mapping is never missed)."""
        self.combos = combo_map(combos)

    def bind_combos_from(self, target_object: Any, target_name: str,
                         *, backward: Callable[[Any], Any] | None = None) -> KeyDispatcher:
        """Follow a bindable property; backward turns its value into a combo map."""
        binding.bind_from(self, "combos", target_object, target_name, backward=backward)
        return self

    def bind_suspended_from(self, target_object: Any, target_name: str = "suspended",
                            *, backward: Callable[[Any], Any] | None = None) -> KeyDispatcher:
        """Follow a bindable property; backward turns its value into a bool."""
        binding.bind_from(self, "suspended", target_object, target_name, backward=backward)
        return self

    def _push_combos(self, value: Any) -> None:
        self._props["combos"] = combo_map(value)

    def _push_suspended(self, value: Any) -> None:
        self._props["suspended"] = bool(value)

    def _handle_command(self, event: GenericEventArguments) -> Any:
        command_id = event.args.get("id") if isinstance(event.args, dict) else None
        if not isinstance(command_id, str) or self._props["suspended"]:
            return None
        if self._on_command is None or command_id not in self._props["combos"].values():
            return None
        return self._on_command(command_id)


def combo_map(combos: Mapping[str, str]) -> dict[str, str]:
    """A plain {combo: command id} copy without empty entries; TypeError for anything else."""
    if not isinstance(combos, Mapping):
        raise TypeError(f"combos must be a mapping of combo to command id, not {type(combos).__name__}")
    return {str(combo): str(command) for combo, command in combos.items() if combo and command}
