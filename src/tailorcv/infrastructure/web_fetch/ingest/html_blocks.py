"""Cut a server-rendered page into the elements a board renders each opening or field as."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class HtmlBlock:
    """One element's attributes and the markup between its tags, as the page wrote it."""

    attrs: dict[str, str]
    inner: str

    def has_class(self, name: str) -> bool:
        """Return whether the element's class list names ``name``."""
        return name in self.attrs.get("class", "").split()


class _Blocks(HTMLParser):
    """Record where each wanted element opens and closes, counting nested ones of its tag."""

    def __init__(self, page: str, tag: str, class_name: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self._page = page
        self._tag = tag
        self._class = class_name
        # getpos() counts only "\n" as a line break, unlike str.splitlines().
        self._lines = [0, *(match.end() for match in re.finditer("\n", page))]
        # Each open element of the tag: its attributes, where its body starts, and whether wanted.
        self._open: list[tuple[dict[str, str], int, bool]] = []
        self.blocks: list[HtmlBlock] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != self._tag:
            return
        stated = {key: value or "" for key, value in attrs}
        wanted = self._class is None or self._class in stated.get("class", "").split()
        start = self._offset() + len(self.get_starttag_text() or "")
        self._open.append((stated, start, wanted))

    def handle_endtag(self, tag: str) -> None:
        if tag != self._tag or not self._open:
            return
        stated, start, wanted = self._open.pop()
        if wanted:
            self.blocks.append(HtmlBlock(stated, self._page[start : self._offset()]))


def html_blocks(page: str, tag: str, *, class_name: str | None = None) -> list[HtmlBlock]:
    """Return every ``tag`` element (with ``class_name``, when given) in closing order.

    An element left unclosed is not returned, so a truncated page yields fewer
    blocks rather than one that runs to the end of the document.
    """
    parser = _Blocks(page, tag, class_name)
    parser.feed(page)
    parser.close()
    return parser.blocks
