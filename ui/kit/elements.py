"""Theme-neutral element plumbing every theme can build on (R5): attribute and class helpers, a bindable
re-rendering list, a keyed row list and a disableable plain-HTML element. No CSS and no colours: a theme styles
what these produce.

- set_prop / set_flag / set_class / swap_classes / set_style change an element only when the value changes, so
  a view model that recomputes often sends nothing new.
- ListHost re-renders its children from a BindableProperty (a small list: chips, check rows, history lines).
- KeyedRows keeps one view per key and updates it in place (a long list: takes, voices); "Show N more", a new
  row or a pick costs only the rows it changes.
- Disableable sets the HTML `disabled` attribute (NiceGUI's mixin sets Quasar's `disable` prop instead), so the
  user fixture's click() honours it, and reports its aria-label as displayed content.

Every BindableProperty here is meant for binding.bind_from(element, name, vm, field): a passive link that dies
with the element (R9).
"""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, Self

from nicegui import binding
from nicegui.element import Element
from nicegui.elements.mixins.disableable_element import DisableableElement
from nicegui.events import GenericEventArguments

__all__ = ["Disableable", "KeyedRows", "ListHost", "RangeInput", "clicker", "set_class", "set_flag", "set_prop", "set_style",
           "swap_classes"]


def set_prop(element: Element, name: str, value: Any) -> None:
    """Set an HTML attribute; None or "" removes it. Nothing is sent when nothing changes."""
    props = element.props
    if value is None or value == "":
        if name in props:
            del props[name]
    elif props.get(name) != value:
        props[name] = value


def set_flag(element: Element, name: str, on: bool) -> None:
    """An ARIA state attribute as the strings "true" / "false" (aria-pressed, aria-checked, ...)."""
    set_prop(element, name, "true" if on else "false")


def set_class(element: Element, name: str, on: bool) -> None:
    """Add or remove one class (no update when it is already so)."""
    if on and name not in element.classes:
        element.classes(add=name)
    elif not on and name in element.classes:
        element.classes(remove=name)


def swap_classes(element: Element, old: str, new: str) -> str:
    """Replace the classes `old` by `new` (space-separated) and return `new`, for the next swap."""
    if old != new:
        element.classes(remove=old or None, add=new or None)
    return new


def set_style(element: Element, name: str, value: str | None) -> None:
    """Set one inline style property (a CSS custom property, a width); None removes it."""
    style = element.style
    if value is None:
        if name in style:
            del style[name]
    elif style.get(name) != value:
        style[name] = value


def clicker(handler: Callable[..., Any]) -> Callable[[GenericEventArguments], Any]:
    """An event listener that calls handler() and hands back its result (an awaitable becomes a task)."""
    def listener(_event: GenericEventArguments) -> Any:
        return handler()
    return listener


class RangeInput(Element, component="vt_range.js"):
    """A native range whose current gesture survives server renders."""

    def __init__(self) -> None:
        super().__init__()
        self.props["sync"] = 0

    def acknowledge(self, sequence: int) -> None:
        if type(sequence) is int and sequence > self.props["sync"]:
            self.props["sync"] = sequence


class ListHost(Element):
    """A container that re-renders its children from `items` (a BindableProperty) with render(item)."""

    items = binding.BindableProperty(on_change=lambda sender, _: sender.refresh())

    def __init__(self, render: Callable[[Any], None], *, items: Iterable[Any] = (), tag: str = "div") -> None:
        """
        :param render: builds the elements of one item (called inside this container)
        :param items: the first items
        :param tag: the container's HTML tag
        """
        super().__init__(tag)
        self._render = render
        self.items = tuple(items)
        self.refresh()

    def bind_items_from(self, target: Any, name: str, *, backward: Callable[[Any], Any] | None = None) -> Self:
        """Follow target.<name> (a bindable field, so the link is never polled)."""
        binding.bind_from(self, "items", target, name, backward=lambda value: tuple(
            backward(value) if backward is not None else value))
        return self

    def refresh(self) -> None:
        """Rebuild every child from the current items."""
        self.clear()
        with self:
            for item in self.items:
                self._render(item)


class KeyedRows(Element):
    """A list of row views kept by key (S03 risk 14): a row that stays listed keeps its element and is updated in
    place with view.show(item); a new key gets a new view from make(item), a gone key's view is deleted, and the
    views follow the order of `items` (a BindableProperty). Elements made with head() stay above the rows.

    Unlike ListHost, a change never rebuilds the rows already shown, so "Show N more", a new take or a pick costs
    only the rows it adds, and a flash or an enter animation plays once on its own row.
    """

    items = binding.BindableProperty(on_change=lambda sender, value: sender.show_items(value))

    def __init__(self, make: Callable[[Any], Element], *, key: Callable[[Any], Hashable], tag: str = "div") -> None:
        """
        :param make: builds the view of one item (called inside this container); the view has show(item)
        :param key: an item's key (a take id, a voice id)
        """
        super().__init__(tag)
        self._make = make
        self._key = key
        self._views: dict[Hashable, Element] = {}
        self._keys: tuple[Hashable, ...] = ()
        self._head: list[Element] = []
        self.items = ()

    @property
    def views(self) -> Mapping[Hashable, Element]:
        """The row views by key, in no particular order."""
        return MappingProxyType(self._views)

    def head(self, element: Element) -> Element:
        """Keep element (a child made inside this container) above the rows."""
        self._head.append(element)
        return element

    def bind_items_from(self, target: Any, name: str) -> Self:
        """Follow target.<name> (a bindable field, so the link is never polled)."""
        binding.bind_from(self, "items", target, name, backward=lambda value: tuple(value or ()))
        return self

    def show_items(self, items: Iterable[Any]) -> None:
        """Keep, add, delete and order the views for items, then show each item on its view."""
        items = tuple(items or ())
        keys = tuple(self._key(item) for item in items)
        if keys != self._keys:
            self._sync(items, keys)
        for key, item in zip(keys, items):
            self._views[key].show(item)              # type: ignore[attr-defined]

    def _sync(self, items: tuple[Any, ...], keys: tuple[Hashable, ...]) -> None:
        wanted = set(keys)
        for key in [key for key in self._views if key not in wanted]:
            self._views.pop(key).delete()
        with self:
            for key, item in zip(keys, items):
                if key not in self._views:
                    self._views[key] = self._make(item)
        ordered = [*self._head, *(self._views[key] for key in keys)]
        children = self.default_slot.children
        if children != ordered:
            children[:] = ordered                    # the same elements in the new order: nothing is rebuilt
            self.update()
        self._keys = keys


class Disableable(DisableableElement):
    """A DisableableElement for plain HTML: `enabled` sets the `disabled` attribute; its aria-label is content."""

    def _handle_enabled_change(self, enabled: bool) -> None:
        set_prop(self, "disabled", None if enabled else True)

    def _displayed_contents(self, *, only_visible: bool) -> list:
        return [*super()._displayed_contents(only_visible=only_visible), self.props.get("aria-label")]
