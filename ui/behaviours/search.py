"""SearchController (§5.6): the rail search over the voices, the takes and two actions.

The pool is built from the in-memory snapshots (LibraryState, TakesState), never from files; a query
matches case-insensitively anywhere in "<label> <route text>", and the first 12 matches show. Running a
voice or a take jumps to Speak and selects it without loading anything (§4.5).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ui.contract.routes import Route
from ui.contract.viewmodels import SearchController as SearchControllerProtocol
from ui.contract.viewmodels import SearchItem
from ui.core import catalog

from .confirm import Subscriptions

if TYPE_CHECKING:
    from . import BehaviourFactory

__all__ = ["FORGE_ID", "FREE_ID", "HINT_EMPTY", "HINT_NONE", "MAX_RESULTS", "SearchController"]

MAX_RESULTS = 12                                                   # M L2462
HINT_EMPTY = "Type a voice, a take or an action"                   # M L2885
HINT_NONE = "No matching voices, takes or actions"
FREE_ID = "action:free"
FORGE_ID = "action:forge"
_VOICE = "voice:"
_TAKE = "take:"


class SearchController(SearchControllerProtocol):
    """The rail search: results(query) for the box, run(item_id) for a pick (Enter runs the first)."""

    def __init__(self, factory: BehaviourFactory) -> None:
        self._factory = factory
        self._core = factory.core
        self._query = ""
        self._items: tuple[SearchItem, ...] = ()
        self._subs = Subscriptions()
        self._subs.add(self._core.events.changed, self._on_changed)

    @property
    def query(self) -> str:
        return self._query

    def results(self, query: str) -> tuple[SearchItem, ...]:
        """The matches for query (also shown through ShellVM.search_items and search_hint)."""
        self._query = query if isinstance(query, str) else ""
        self._items = self._find(self._query)
        self._show()
        return self._items

    def run(self, item_id: str) -> Route | None:
        """Act on a result; returns the route to open (None: stay). The box clears either way."""
        route = self._act(item_id)
        self.clear()
        return route

    def clear(self) -> None:
        """Escape, a pick or navigation: empty the box and the results."""
        self._query = ""
        self._items = ()
        self._show()

    def pool(self) -> tuple[SearchItem, ...]:
        """Every searchable item: each voice, each take (newest first), then the two actions."""
        voices = tuple(SearchItem(f"{_VOICE}{row.voice_id}", row.voice_id,
                                  f"Voices · {catalog.label(row.backend)} · {row.language_name}")
                       for row in self._core.state.library.rows)
        takes = tuple(SearchItem(f"{_TAKE}{take.id}", take.text, f"Takes · {take.voice} · Take {take.id}")
                      for take in self._core.state.takes.takes)
        actions = (SearchItem(FREE_ID, "Free VRAM", "Model › Action"), SearchItem(FORGE_ID, "Forge a voice", "Forge"))
        return voices + takes + actions

    def close(self) -> None:
        self._subs.close()

    # ---------- plumbing ----------

    def _find(self, query: str) -> tuple[SearchItem, ...]:
        needle = query.strip().lower()
        if not needle:
            return ()
        found = (item for item in self.pool() if needle in f"{item.label} {item.route_text}".lower())
        return tuple(item for _, item in zip(range(MAX_RESULTS), found))

    def _hint(self) -> str:
        if self._items:
            return ""
        return HINT_NONE if self._query.strip() else HINT_EMPTY

    def _show(self) -> None:
        self._factory.status().show_search(self._items, self._hint())

    def _act(self, item_id: str) -> Route | None:
        factory = self._factory
        if item_id == FREE_ID:
            factory.models().free()
            return None
        if item_id == FORGE_ID:
            return "/forge"
        if item_id.startswith(_VOICE):
            return self._jump_voice(item_id.removeprefix(_VOICE))
        if item_id.startswith(_TAKE):
            key = item_id.removeprefix(_TAKE)
            return self._jump_take(int(key)) if key.isdigit() else None
        return None

    def _jump_voice(self, voice_id: str) -> Route | None:
        """Speak with the voice picked (pick_voice semantics) and its row flashed (M L1702-1706)."""
        if not any(row.voice_id == voice_id for row in self._core.state.library.rows):
            return None
        self._factory.speak().pick_voice(voice_id)
        self._core.events.flash.emit(f"voice-{voice_id}")
        return "/"

    def _jump_take(self, take_id: int) -> Route | None:
        """Speak with the take current and playing from 0, its row flashed (M L1708-1712)."""
        if self._core.takes.get(take_id) is None:
            return None
        self._factory.playback().play_take(take_id)
        self._core.events.flash.emit(f"take-{take_id}")
        return "/"

    def _on_changed(self) -> None:
        if self._query.strip():
            items = self._find(self._query)
            if items != self._items:
                self._items = items
                self._show()
