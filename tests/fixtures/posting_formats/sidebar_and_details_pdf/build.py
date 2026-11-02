"""Build posting.pdf, a posting carrying both a sidebar and a details table.

Usage, from the repository root, with this directory as <here>:
    python -m tests.fixtures.posting_formats.sidebar_and_details_pdf.build <here>/posting.pdf

One page holds the shape a column cut repairs beside the shape it breaks, which
is what a single reading treatment has to answer for.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.support.pdf_documents import Placed, text_pdf

BODY_X = 45
SIDEBAR_X = 390
VALUE_X = 260

ROWS: list[tuple[int, str, str]] = [
    (730, "Scheduling Engineer at Globex Freight", "AT A GLANCE"),
    (714, "Globex Freight is hiring a scheduling", "Team"),
    (698, "engineer for the dispatch platform that", "Dispatch platform"),
    (682, "eleven depots plan their day from. You", "Reports to"),
    (666, "will own the planner service and the", "Head of Dispatch"),
    (650, "contract it publishes to the depots.", "Interview"),
    (634, "WHAT YOU WILL DO", "Three conversations"),
    (618, "Keep the planner within its overnight", ""),
    (602, "window as depots are added.", ""),
]
DETAILS = [
    ("Location", "Thistledown Quay"),
    ("Employment type", "Full time, permanent"),
    ("Requisition", "REQ-8120"),
    ("Posted", "Yesterday"),
    ("On site", "Three days a week"),
    ("Travel", "Occasional depot visits"),
]


def placements() -> list[Placed]:
    """Return the body rows beside the sidebar, then the details table below both."""
    placed: list[Placed] = []
    for y, body, sidebar in ROWS:
        if body:
            placed.append((BODY_X, y, body))
        if sidebar:
            placed.append((SIDEBAR_X, y, sidebar))
    for index, (label, value) in enumerate(DETAILS):
        placed += [(BODY_X, 550 - 16 * index, label), (VALUE_X, 550 - 16 * index, value)]
    return placed


if __name__ == "__main__":
    text_pdf(Path(sys.argv[1]), placements())
