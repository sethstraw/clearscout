"""Estimate how many body lines a rendered resume occupies.

A line is the 11.1pt body pitch of a letter page rendered by pressume in the
``THEME`` and ``DENSITY`` below. Every figure below was measured from renders
of synthetic documents in that setting. Selection budgets with them before
drafting, and the fit pass measures the drafted Markdown with the same figures.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Every render asks pressume for this setting; its own default theme runs a page longer.
THEME = "traditional"
DENSITY = "balanced"

# Body lines a letter page holds; the first page's name counts against them.
LINES_PER_PAGE = 65.0

# Sticky headings and unsplit lines left this much white space at a page foot.
PAGE_BREAK_LINES = 1.5

# Characters one rendered body line holds.
CHARACTERS_PER_LINE = 110

# The name heading and the space below it, in lines.
NAME_LINES = 3.2

# A section heading with its rule and the space around it, in lines.
SECTION_LINES = 2.2

# An organization heading, in lines; the role's title and dates wrap as text.
ROLE_LINES = 1.8

# Each paragraph or list sits 0.8pt further from the block above it than one line does.
BLOCK_GAP_LINES = 0.07

# The lines a summary is charged before it is drafted.
SUMMARY_LINES = 4.0

# The lines a skills section is charged before it is drafted.
SKILLS_LINES = 8.0

# The lines a role's context is charged before it is drafted.
CONTEXT_LINES = 1.0

# Under the shortest drafted-to-master wrap ratio, so selection over-buys and fit trims
# rather than leaving room.
DRAFTED_SHARE = 0.65

_HEADING = re.compile(r"^(#{1,3})\s")
_THEMATIC_BREAK = re.compile(r"^-{3,}\s*$")
_LIST_ITEM = re.compile(r"^\s*[-*+]\s+")
_HEADING_LINES = {1: NAME_LINES, 2: SECTION_LINES, 3: ROLE_LINES}


@dataclass(frozen=True)
class Budget:
    """How many body lines a document of ``pages`` may carry."""

    pages: int

    @property
    def total_lines(self) -> float:
        """Return the whole line budget for the document."""
        return self.pages * LINES_PER_PAGE - (self.pages - 1) * PAGE_BREAK_LINES

    def selection_lines(self, fixed: float) -> int:
        """Return the master-text bullet lines to select once ``fixed`` lines are placed."""
        # Drafting shortens every bullet, so selection buys more than the page holds.
        return max(0, int((self.total_lines - fixed) / DRAFTED_SHARE))

    def last_page_share(self, lines: float) -> float:
        """Return how full ``lines`` leaves the last page, as a share of what it holds."""
        before = Budget(self.pages - 1).total_lines if self.pages > 1 else 0.0
        return max(0.0, lines - before) / (self.total_lines - before)


def wrapped_lines(text: str) -> int:
    """Return how many lines ``text`` occupies once wrapped."""
    return max(1, math.ceil(len(text) / CHARACTERS_PER_LINE))


def paragraph_lines(lines: Iterable[str]) -> float:
    """Return the lines one paragraph occupies, each hard-broken line wrapped on its own."""
    return BLOCK_GAP_LINES + sum(wrapped_lines(line) for line in lines)


def markdown_lines(markdown: str) -> float:
    """Return the lines a Markdown document occupies once rendered."""
    total = 0.0
    for block in re.split(r"\n[ \t]*\n", markdown.strip()):
        rows = [row for row in block.splitlines() if row.strip()]
        while rows and (heading := _HEADING.match(rows[0])):
            total += _HEADING_LINES[len(heading.group(1))]
            rows = rows[1:]
        if not rows or all(_THEMATIC_BREAK.match(row) for row in rows):
            continue
        total += paragraph_lines(_visual_lines(rows))
    return total


def _visual_lines(rows: Sequence[str]) -> list[str]:
    """Return a block's rendered lines: each list item and hard-broken line starts one."""
    lines: list[str] = []
    current = ""
    for row in rows:
        item = _LIST_ITEM.match(row)
        if item and current:
            lines.append(current)
            current = ""
        text = row[item.end() :] if item else row
        current = f"{current} {text.strip()}".strip().replace("**", "")
        # Markdown breaks a line on two trailing spaces or a trailing backslash.
        if row.endswith(("  ", "\\")):
            lines.append(current.rstrip("\\"))
            current = ""
    if current:
        lines.append(current)
    return lines


# Include every shaping constant in the reuse fingerprint.
SHAPING_CONSTANTS: dict[str, int | float] = {
    "LINES_PER_PAGE": LINES_PER_PAGE,
    "PAGE_BREAK_LINES": PAGE_BREAK_LINES,
    "CHARACTERS_PER_LINE": CHARACTERS_PER_LINE,
    "NAME_LINES": NAME_LINES,
    "SECTION_LINES": SECTION_LINES,
    "ROLE_LINES": ROLE_LINES,
    "BLOCK_GAP_LINES": BLOCK_GAP_LINES,
    "SUMMARY_LINES": SUMMARY_LINES,
    "SKILLS_LINES": SKILLS_LINES,
    "CONTEXT_LINES": CONTEXT_LINES,
    "DRAFTED_SHARE": DRAFTED_SHARE,
}


def shaping_fingerprint() -> str:
    """Return a fingerprint of the shaping constants only, blind to the code that reads them."""
    payload = json.dumps(SHAPING_CONSTANTS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
