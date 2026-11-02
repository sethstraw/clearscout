"""Read a posting out of a PDF.

pypdfium2 is BSD-3-Clause; PyMuPDF is AGPL-3.0 and would not suit an
Apache-2.0 tool. A PDF with no text layer is a scan, and the error says to
screenshot the posting instead.

A caller that asks for columns has each page segmented by recursive XY cut
before its text is read, so a two-column layout is returned column by column
rather than in the stream order, which reads across the gutter.

Emphasis is read from the font each character was set in, so a document that
fakes bold by stroking its outlines reads as ordinary text.
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path
from typing import Any

from tailorcv.infrastructure.web_fetch.ingest.text import MINIMUM_USEFUL_CHARACTERS, JDText
from tailorcv.shared.errors import IngestError
from tailorcv.shared.layout import plain_layout

# A gutter is a page-scale feature, so every threshold is a fraction of the page
# rather than of the region being cut, which shrinks as the recursion descends.
COLUMN_GAP = 0.03
ROW_GAP = 0.02
# A side holding less than this share of a region's characters is a margin note
# or a right-aligned date column, not a column of its own.
COLUMN_SHARE = 0.2
MAXIMUM_CUTS = 6
MINIMUM_BLOCK_CHARACTERS = 12
# A font descriptor states a weight on the CSS scale, where semibold starts at 600.
BOLD_WEIGHT = 600
# One of the base fourteen fonts carries no descriptor, so its name is all there is.
BOLD_NAMES = ("bold", "black", "heavy")
# An embedded subset is named after six arbitrary capitals, which can spell a weight.
SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
FONT_NAME_LIMIT = 256

_Box = tuple[float, float, float, float]
_Span = tuple[float, float]


def from_pdf(path: Path) -> JDText:
    """Return job description text for a PDF."""
    text = plain_layout(extract_text(path))
    if len(text) < MINIMUM_USEFUL_CHARACTERS:
        raise IngestError(
            f"{path} has almost no extractable text, so it is probably a scan or an "
            "image. Open it, screenshot the posting, and pass the image instead."
        )
    return JDText(text=text, origin=str(path), method="pypdfium2")


def extract_text(path: Path, *, columns: bool = False) -> str:
    """Extract the text layer of a PDF, page by page, in stream order unless asked for columns.

    A posting states its details in a table that reads across, and splitting its
    columns would separate every label from its value, so only a document known
    to be laid out in columns asks for them.
    """
    document = _open(path)
    try:
        pages = [_page_text(document, number, columns) for number in range(len(document))]
    finally:
        document.close()
    return "\n\n".join(page for page in pages if page.strip())


def extract_bold_lines(path: Path) -> list[str]:
    """Return each line of a PDF whose every visible character is set in a bold font.

    The lines come back in stream order and spaced as the reader spaced them, so a
    caller matching them against another reading compares the characters alone.
    """
    document = _open(path)
    try:
        found: list[str] = []
        for number in range(len(document)):
            found += _page_bold_lines(document, number)
    finally:
        document.close()
    return found


def _open(path: Path) -> Any:
    """Open a PDF, turning a missing dependency or an unreadable file into an IngestError."""
    try:
        import pypdfium2
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise IngestError("The pypdfium2 package is not installed.") from error

    try:
        return pypdfium2.PdfDocument(str(path))
    except Exception as error:
        raise IngestError(
            f"Could not read {path}: {error}\n"
            "If the file opens fine in a viewer, screenshot the posting and pass the image."
        ) from error


def _page_bold_lines(document: Any, number: int) -> list[str]:
    """Return one page's bold lines, treating an unreadable page as an empty one."""
    try:
        page = document[number]
        textpage = page.get_textpage()
        try:
            return _bold_lines(textpage)
        finally:
            textpage.close()
            page.close()
    except Exception:  # noqa: BLE001 - one bad page should not lose the rest
        return []


def _bold_lines(textpage: Any) -> list[str]:
    """Return the lines, split where pdfium reports a break, that carry no lighter character."""
    import pypdfium2.raw as pdfium_c

    count = int(textpage.count_chars())
    text = str(textpage.get_text_range(0, count)) if count else ""
    found: list[str] = []
    line: list[str] = []
    bold = True
    for index, character in enumerate(text[:count]):
        if character in "\r\n":
            if bold and "".join(line).strip():
                found.append("".join(line))
            line, bold = [], True
            continue
        line.append(character)
        if not character.isspace():
            bold = bold and _is_bold(pdfium_c, textpage, index)
    if bold and "".join(line).strip():
        found.append("".join(line))
    return found


def _is_bold(pdfium_c: Any, textpage: Any, index: int) -> bool:
    """Return whether one character's font is bold by its declared weight or by its name."""
    if int(pdfium_c.FPDFText_GetFontWeight(textpage, index)) >= BOLD_WEIGHT:
        return True
    buffer = ctypes.create_string_buffer(FONT_NAME_LIMIT)
    flags = ctypes.c_int()
    length = int(
        pdfium_c.FPDFText_GetFontInfo(textpage, index, buffer, FONT_NAME_LIMIT, ctypes.byref(flags))
    )
    name = SUBSET_PREFIX.sub("", buffer.raw[: max(length - 1, 0)].decode("utf-8", "replace"))
    return any(token in name.lower() for token in BOLD_NAMES)


def _page_text(document: object, number: int, columns: bool) -> str:
    """Return one page's text, treating an unreadable page as an empty one.

    One malformed page in a ten-page brochure should not lose the nine that
    parsed, and the caller already refuses a document that yields too little.
    """
    try:
        page = document[number]  # type: ignore[index]
        textpage = page.get_textpage()
        try:
            return _ordered_text(page, textpage) if columns else str(textpage.get_text_bounded())
        finally:
            textpage.close()
            page.close()
    except Exception:  # noqa: BLE001 - see the docstring
        return ""


def _ordered_text(page: Any, textpage: Any) -> str:
    """Return one page's blocks in reading order, each read from its own bounds."""
    boxes = _character_boxes(textpage)
    blocks = _blocks(boxes, page.get_size(), MAXIMUM_CUTS) if boxes else []
    if len(blocks) < 2:
        return str(textpage.get_text_bounded())
    found = []
    for block in blocks:
        text = str(
            textpage.get_text_bounded(
                left=min(box[0] for box in block) - 1,
                bottom=min(box[1] for box in block) - 1,
                right=max(box[2] for box in block) + 1,
                top=max(box[3] for box in block) + 1,
            )
        )
        if text.strip():
            found.append(text)
    return "\n".join(found)


def _character_boxes(textpage: Any) -> list[_Box]:
    """Return a box per visible character, which is what the cut is measured on."""
    count = int(textpage.count_chars())
    if not count:
        return []
    text = str(textpage.get_text_range(0, count))
    boxes = []
    for index in range(count):
        if index < len(text) and text[index].isspace():
            continue
        box: _Box = textpage.get_charbox(index)
        if box[2] > box[0] and box[3] > box[1]:
            boxes.append(box)
    return boxes


def _blocks(boxes: list[_Box], size: tuple[float, float], depth: int) -> list[list[_Box]]:
    """Split one region by its widest qualifying whitespace gap, recursively."""
    width, height = size
    if depth <= 0 or len(boxes) < MINIMUM_BLOCK_CHARACTERS or not width or not height:
        return [boxes]
    sides = _cut(boxes, width, height)
    if sides is None:
        return [boxes]
    near, far = sides
    return _blocks(near, size, depth - 1) + _blocks(far, size, depth - 1)


def _cut(boxes: list[_Box], width: float, height: float) -> tuple[list[_Box], list[_Box]] | None:
    """Return the two sides of this region's widest qualifying gap, or None to keep it whole.

    A vertical cut wins ties, because a run of line gaps would otherwise split a
    column into single lines before its gutter was ever found.
    """
    column = _widest(_empty_spans([(box[0], box[2]) for box in boxes]))
    row = _widest(_empty_spans([(box[1], box[3]) for box in boxes]))
    column_share = (column[1] - column[0]) / width if column else 0.0
    row_share = (row[1] - row[0]) / height if row else 0.0
    if column is not None and column_share >= COLUMN_GAP and column_share >= row_share:
        sides = _column_sides(boxes, column)
        if sides is not None:
            return sides
    if row is not None and row_share >= ROW_GAP:
        return _row_sides(boxes, row)
    return None


def _column_sides(boxes: list[_Box], gap: _Span) -> tuple[list[_Box], list[_Box]] | None:
    """Return the sides of a vertical gap, or None when one side is too thin to be a column."""
    near = [box for box in boxes if box[2] <= gap[0]]
    far = [box for box in boxes if box[0] >= gap[1]]
    if min(len(near), len(far)) < COLUMN_SHARE * len(boxes):
        return None
    return near, far


def _row_sides(boxes: list[_Box], gap: _Span) -> tuple[list[_Box], list[_Box]] | None:
    """Return the sides of a horizontal gap, or None when the gap has text on one side only."""
    upper = [box for box in boxes if box[1] >= gap[1]]
    lower = [box for box in boxes if box[3] <= gap[0]]
    if not upper or not lower:
        return None
    return upper, lower


def _empty_spans(spans: list[_Span]) -> list[_Span]:
    """Return the gaps between projected extents, leaving the outer margins out."""
    found = []
    reach: float | None = None
    for start, end in sorted(spans):
        if reach is not None and start > reach:
            found.append((reach, start))
        reach = end if reach is None else max(reach, end)
    return found


def _widest(spans: list[_Span]) -> _Span | None:
    """Return the widest span, or None when there is none."""
    return max(spans, key=lambda span: span[1] - span[0], default=None)
