"""Job description text: its size bounds, the markup stripper, and the injected-line filter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict

from tailorcv.shared.errors import IngestError
from tailorcv.shared.layout import plain_layout

MINIMUM_USEFUL_CHARACTERS = 400


# Bound posting size to limit memory and provider cost.
MAXIMUM_USEFUL_CHARACTERS = 120_000


class JDText(BaseModel):
    """A job description reduced to text, with a note on where it came from."""

    model_config = ConfigDict(frozen=True)

    text: str
    origin: str
    method: str

    def summary(self) -> str:
        """Return a one-line description for the run report."""
        return f"{self.origin} via {self.method} ({len(self.text)} characters)"


_APPLICATION_INSTRUCTION = re.compile(
    r"(?i)\b(?:mention|include|quote|tag|add)\b[^.\n]{0,200}?"
    r"\b(?:when\s+(?:you\s+)?apply(?:ing)?|(?:in|with)\s+your\s+application)\b"
)


_OPAQUE_TOKEN = re.compile(
    r"\b(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[0-9])"
    r"[A-Za-z0-9+/]{12,}={0,2}\b"
)


# The gap each element leaves at its edges. A paragraph, a heading or a container
# ends a block; an item, a row or a line break ends a line; anything else is inline.
_GAPS = {
    **dict.fromkeys(("br", "li", "tr", "dt", "dd"), "\n"),
    **dict.fromkeys(("p", "div", "section", "article", "blockquote", "pre", "hr"), "\n\n"),
    **dict.fromkeys(("ul", "ol", "dl", "table", "header", "footer", "form"), "\n\n"),
    **{f"h{level}": "\n\n" for level in range(1, 7)},
}


_SKIPPED = frozenset({"script", "style"})


# Weakest gap first. Two elements meeting keep the stronger of the gaps they state,
# so an inline span inside a list item does not cost the item its break.
_GAP_ORDER = ("", " ", "\n", "\n\n")


_HEADING_TAGS = frozenset({*(f"h{level}" for level in range(1, 7)), "dt"})


# Elements that hold one line of their own: a heading, an item, or a candidate item.
_LEAVES = frozenset({*_HEADING_TAGS, "p", "div", "dd", "tr"})


_EMPHASIS_TAGS = frozenset({"b", "strong", "u"})


_BOLD_STYLE = re.compile(r"font-weight\s*:\s*(?:bold|[6-9]00)", re.IGNORECASE)


_HEADING_WORDS = 10


_SHOUTED_LETTERS = 5


# An item stated in a line of its own is at most one sentence, and a short one.
_ITEM_WORDS = 40


_INNER_SENTENCE = re.compile(r"[.!?]\s+\S")


_TERMINAL = (".", "!", "?")


_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


# A body escaped twice still opens with a tag once the parser has read it.
_ESCAPED_MARKUP = re.compile(
    r"\s*<(?:p|div|ul|ol|li|h[1-6]|br|span|strong|b|section|article|table)\b[^<>]*>",
    re.IGNORECASE,
)


_UNESCAPE_ROUNDS = 2


@dataclass
class _List:
    ordered: bool
    # How many items were open when the list opened, so an unclosed <li> ends with it.
    items_outside: int
    count: int = 0


@dataclass
class _Leaf:
    """One element that may hold a line of its own, and the collected parts it spans."""

    tag: str
    first: int = -1
    last: int = -1
    plain: bool = False
    nested: bool = False
    cells: int = 0
    header: bool = False


class _PlainText(HTMLParser):
    """Collect the text a document carries, keeping the breaks its elements state.

    A list item starts its line with "- ", or "N. " inside an ordered list, and
    nested lists stay flat.
    """

    def __init__(self) -> None:
        """Start with nothing collected, no gap owed, and no skipped element open."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.leaves: list[_Leaf] = []
        self._open: list[_Leaf] = []
        self._gap = ""
        self._skipping = 0
        self._lists: list[_List] = []
        self._items = 0
        self._marker = ""
        self._emphasis = 0
        self._spans: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element and owe the gap its kind states."""
        self._owe(tag)
        self._open_leaf(tag)
        if tag in _SKIPPED:
            self._skipping += 1
        elif tag in ("ul", "ol"):
            self._lists.append(_List(ordered=tag == "ol", items_outside=self._items))
        elif tag == "li":
            self._open_item()
        elif tag in _EMPHASIS_TAGS:
            self._emphasis += 1
        elif tag == "span":
            bold = any(key == "style" and _BOLD_STYLE.search(value or "") for key, value in attrs)
            self._spans.append(bold)
            self._emphasis += bold

    def handle_endtag(self, tag: str) -> None:
        """Close the element and owe the gap its kind states."""
        if tag in _SKIPPED and self._skipping:
            self._skipping -= 1
        elif tag in ("ul", "ol") and self._lists:
            self._items = self._lists.pop().items_outside
        elif tag == "li" and self._items:
            self._items -= 1
            self._marker = ""
        elif tag in _EMPHASIS_TAGS and self._emphasis:
            self._emphasis -= 1
        elif tag == "span" and self._spans:
            self._emphasis -= self._spans.pop()
        if any(leaf.tag == tag for leaf in self._open):
            while (leaf := self._open.pop()).tag != tag:
                self.leaves.append(leaf)
            self.leaves.append(leaf)
        self._owe(tag)

    def handle_data(self, data: str) -> None:
        """Keep text from everywhere except a script or a style block."""
        if self._skipping:
            return
        # Whitespace between two elements is layout, and widens no gap they state.
        if not data.strip():
            self._owe("")
            return
        text = self._marker + data.lstrip() if self._marker else data
        for leaf in self._open:
            leaf.first = len(self.parts) if leaf.first < 0 else leaf.first
            leaf.last = len(self.parts)
            leaf.plain = leaf.plain or not self._emphasis
        self.parts.append(self._gap + text)
        self._gap = ""
        self._marker = ""

    def _open_leaf(self, tag: str) -> None:
        """Track an element that may be a heading or an item, and what it holds."""
        container = self._open[-1] if self._open else None
        if container is not None and tag in ("td", "th"):
            container.cells += 1
            container.header = container.header or tag == "th"
        elif container is not None and _GAPS.get(tag, " ") != " ":
            container.nested = True
        if tag in _LEAVES and not self._items:
            self._open.append(_Leaf(tag))

    def _open_item(self) -> None:
        current = self._lists[-1] if self._lists else None
        if current is None:
            self._marker = "- "
        else:
            self._items = current.items_outside
            current.count += 1
            self._marker = f"{current.count}. " if current.ordered else "- "
        self._items += 1

    def _owe(self, tag: str) -> None:
        gap = _GAPS.get(tag, " ")
        # Inside an item a paragraph or nested list keeps the item on its own line.
        if self._items and tag != "li" and gap == "\n\n":
            gap = " "
        self._gap = max(self._gap, gap, key=_GAP_ORDER.index)


def strip_tags(body: str) -> str:
    """Return the text a document's markup carries, in the layout ``plain_layout`` writes."""
    text = body
    for _ in range(_UNESCAPE_ROUNDS + 1):
        text = _parsed(text)
        if not _ESCAPED_MARKUP.match(text):
            break
    return plain_layout(text)


def _parsed(body: str) -> str:
    # A quoted attribute value may carry an angle bracket of its own, so where a tag
    # ends is a parsing question rather than a match to the next ">".
    parser = _PlainText()
    parser.feed(_CDATA.sub(r"\1", body))
    parser.close()
    _mark_listed_leaves(parser.parts, parser.leaves)
    return "".join(parser.parts)


def _mark_listed_leaves(parts: list[str], leaves: list[_Leaf]) -> None:
    """Mark as items the run of one-line elements standing under a heading element.

    A run of two or more short single-sentence lines, at least one without a closing
    period, lists; paragraphs that each end a sentence are prose and stay unmarked.
    """
    kept = sorted((leaf for leaf in leaves if leaf.first >= 0), key=lambda leaf: leaf.first)
    kinds = [_leaf_kind(parts, leaf) for leaf in kept]
    for start, leaf in enumerate(kept):
        if kinds[start] != "heading":
            continue
        run = _item_run(leaf, kept[start + 1 :], kinds[start + 1 :])
        texts = [_leaf_text(parts, item) for item in run]
        if len(run) >= 2 and not all(text.endswith(_TERMINAL) for text in texts):
            _mark_items(parts, leaf, run)


def _item_run(heading: _Leaf, following: list[_Leaf], kinds: list[str]) -> list[_Leaf]:
    """Return the item elements that directly follow a heading element, in order."""
    run: list[_Leaf] = []
    for leaf, kind in zip(following, kinds, strict=False):
        previous = run[-1] if run else heading
        if kind != "item" or leaf.first != previous.last + 1:
            break
        run.append(leaf)
    return run


def _mark_items(parts: list[str], heading: _Leaf, run: list[_Leaf]) -> None:
    """Set a heading element and its item run apart, each item on a marked line."""
    _set_gap(parts, heading.first, "\n\n")
    for position, item in enumerate(run):
        _set_gap(parts, item.first, "\n\n- " if position == 0 else "\n- ")
    if run[-1].last + 1 < len(parts):
        _set_gap(parts, run[-1].last + 1, "\n\n")


def _leaf_kind(parts: list[str], leaf: _Leaf) -> str:
    """Return "heading", "item", or "other" for one tracked element."""
    if leaf.nested or (leaf.tag == "tr" and leaf.cells != 1):
        return "other"
    text = _leaf_text(parts, leaf)
    words = len(text.split())
    short = words <= _HEADING_WORDS and not text.endswith(_TERMINAL)
    # A capitalised word this long is a heading shouted, not an acronym such as "SQL".
    shouted = any(len(word) >= _SHOUTED_LETTERS and word.isupper() for word in text.split())
    if (
        leaf.tag in _HEADING_TAGS
        or leaf.header
        or (short and not leaf.plain)
        or (short and shouted and text == text.upper())
        or (text.endswith(":") and words <= _HEADING_WORDS)
    ):
        return "heading"
    single = words <= _ITEM_WORDS and not _INNER_SENTENCE.search(text)
    starts_plainly = text[:1].isalnum()
    return "item" if single and starts_plainly else "other"


def _leaf_text(parts: list[str], leaf: _Leaf) -> str:
    return " ".join("".join(parts[leaf.first : leaf.last + 1]).split())


def _set_gap(parts: list[str], index: int, gap: str) -> None:
    """Put ``gap`` before one collected part in place of the gap it had."""
    parts[index] = (gap if index else gap.lstrip()) + parts[index].lstrip()


def strip_injected_instructions(text: str) -> str:
    """Return the text without any line telling an applicant to repeat an opaque token."""
    lines = text.splitlines()
    kept = [
        line
        for line in lines
        if not (_APPLICATION_INSTRUCTION.search(line) and _OPAQUE_TOKEN.search(line))
    ]
    return text if len(kept) == len(lines) else "\n".join(kept)


def require_enough_text(text: str, origin: str) -> None:
    """Reject an extraction too short or too long to be one job posting."""
    length = len(text.strip())
    if length < MINIMUM_USEFUL_CHARACTERS:
        raise IngestError(
            f"Only {length} characters of text came out of {origin}, "
            "which is too little to tailor against. Save the posting as a PDF or "
            "screenshot it and pass that instead."
        )
    if length > MAXIMUM_USEFUL_CHARACTERS:
        raise IngestError(
            f"{length:,} characters came out of {origin}, which is far more than "
            "a job posting. That is usually a whole careers site or a search page "
            "rather than one role.\n"
            "  Open the posting itself, save it as a PDF or screenshot it, and pass "
            "that path."
        )
