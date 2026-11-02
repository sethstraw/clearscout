"""Readable text trafilatura recovers from a page, and the closure notice a short page shows."""

from __future__ import annotations

import re
from typing import Any

from tailorcv.application.discovery.posting_text import closure_signal
from tailorcv.infrastructure.web_fetch.ingest.text import MINIMUM_USEFUL_CHARACTERS
from tailorcv.shared.layout import plain_layout

# A size-0 rerun also skips baseline's embedded body, so it must keep this share of the words.
_RERUN_WORD_SHARE = 0.9


_LIST_ITEM_TAG = re.compile(r"<li[\s>]", re.IGNORECASE)


def closed_page_notice(body: str, text: str) -> str:
    """Return a short page's visible closure notice, or "" when the page is not closed.

    ``text`` is the page's extracted text; a long page or a hidden notice is not a closure.
    """
    if len(text.strip()) >= MINIMUM_USEFUL_CHARACTERS or not closure_signal(text):
        return ""
    visible, _within_blocks = readable_text(
        body,
        prune_xpath=[
            "//*[@hidden or @aria-hidden='true' or "
            "contains(translate(@style, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ \t\r\n', "
            "'abcdefghijklmnopqrstuvwxyz'), 'display:none') or "
            "contains(translate(@style, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ \t\r\n', "
            "'abcdefghijklmnopqrstuvwxyz'), 'visibility:hidden')]"
        ],
    )
    return visible if visible and closure_signal(visible) else ""


def readable_text(body: str, *, prune_xpath: list[str] | None = None) -> tuple[str | None, bool]:
    """Return a page's readable blocks as trafilatura lays them out, or None when it finds none.

    The flag is False when the page states words outside those blocks.
    """
    import trafilatura
    from trafilatura.settings import use_config

    options: dict[str, Any] = {
        "include_comments": False,
        "include_tables": True,
        "prune_xpath": prune_xpath,
    }
    extracted = trafilatura.extract(body, **options)
    if extracted and "\n" not in extracted:
        # Under MIN_EXTRACTED_SIZE trafilatura swaps in baseline(), whose text_content()
        # joins every block of an article or embedded body onto one line.
        config = use_config()
        config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "0")
        options["config"] = config
        rerun = trafilatura.extract(body, **options)
        # Words the rerun loses sat outside the page's blocks, where only a render lays them out.
        if not rerun or len(rerun.split()) < _RERUN_WORD_SHARE * len(extracted.split()):
            return plain_layout(extracted), False
        extracted = rerun
    if extracted and _LIST_ITEM_TAG.search(body):
        recalled = trafilatura.extract(body, favor_recall=True, **options)
        extracted = _with_recalled_list_items(extracted, recalled)
    return (plain_layout(extracted) if extracted else None), True


def _with_recalled_list_items(extracted: str, recalled: str | None) -> str:
    """Return the extracted read with the recall read's list items spliced between its lines.

    On a page with no article or main frame, trafilatura's default pass drops every list item.
    """
    lines = extracted.splitlines()
    spliced = _recalled_lines_by_line(lines, recalled or "")
    if not spliced:
        return extracted
    return "\n".join(
        kept for index, line in enumerate(lines) for kept in spliced.get(index, [line])
    )


def _recalled_lines_by_line(lines: list[str], recalled: str) -> dict[int, list[str]]:
    """Map each extracted line to itself and the recall list items after it, or none on a reorder.

    Items before the first or after the last shared line sit outside the posting, so they drop.
    """
    shared = [(index, _unmarked(line)) for index, line in enumerate(lines) if line.strip()]
    spliced: dict[int, list[str]] = {}
    matched = 0
    for line in recalled.splitlines():
        if matched < len(shared) and _unmarked(line) == shared[matched][1]:
            index = shared[matched][0]
            # The default pass can keep a list item's paragraph and drop only its marker.
            spliced[index] = [line if _is_list_item(line) else lines[index]]
            matched += 1
        elif 0 < matched < len(shared) and _is_list_item(line):
            spliced[shared[matched - 1][0]].append(line)
    return spliced if shared and matched == len(shared) else {}


def _is_list_item(line: str) -> bool:
    """Return whether a laid-out line carries a list marker."""
    return line.lstrip().startswith("- ")


def _unmarked(line: str) -> str:
    """Return a line's text without its indent or list marker."""
    return line.strip().removeprefix("- ")
