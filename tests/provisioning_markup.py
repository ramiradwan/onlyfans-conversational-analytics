"""Structural reader for the served setup page."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


@dataclass
class Element:
    tag: str
    attributes: dict[str, str | None]
    ancestors: tuple[str, ...]
    hidden: bool
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class PageMarkup(HTMLParser):
    """Elements by id, with their text, enclosing ids, and inherited `hidden` state."""

    def __init__(self, document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: dict[str, Element] = {}
        self._open: list[Element] = []
        self.feed(document)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element = Element(
            tag=tag,
            attributes=attributes,
            ancestors=tuple(parent.attributes["id"] for parent in self._open if parent.attributes.get("id")),
            hidden="hidden" in attributes or any(parent.hidden for parent in self._open),
        )
        if identifier := attributes.get("id"):
            self.elements[identifier] = element
        if tag not in VOID_ELEMENTS:
            self._open.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        for element in self._open:
            element.parts.append(data)

    def descriptions(self, identifier: str) -> list[Element]:
        """Elements named by the control's `aria-describedby`; unknown ids are omitted."""
        references = (self.elements[identifier].attributes.get("aria-describedby") or "").split()
        return [self.elements[reference] for reference in references if reference in self.elements]

    def has_visible_guidance(self, identifier: str) -> bool:
        """The control is described only by existing, visible, non-empty elements."""
        references = (self.elements[identifier].attributes.get("aria-describedby") or "").split()
        descriptions = self.descriptions(identifier)
        return (
            bool(references)
            and len(descriptions) == len(references)
            and all(description.text and not description.hidden for description in descriptions)
        )

    def is_visible_step_text(self, identifier: str, step: str) -> bool:
        element = self.elements.get(identifier)
        return element is not None and step in element.ancestors and bool(element.text) and not element.hidden
