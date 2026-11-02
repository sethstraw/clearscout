"""Build posting.pdf, a posting whose body runs beside an at-a-glance sidebar.

Usage, from the repository root, with this directory as <here>:
    python -m tests.fixtures.posting_formats.sidebar_panel_pdf.build <here>/posting.pdf

Each line is placed at an explicit coordinate and written in row order, body cell
before sidebar cell, which is how a page laid out row by row hands its text to a
reader that follows the stream.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.support.pdf_documents import Placed, text_pdf

BODY_X = 45
SIDEBAR_X = 390

ROWS: list[tuple[int, str, str]] = [
    (730, "Integration Engineer at Initrode Freight", "AT A GLANCE"),
    (714, "Initrode Freight moves freight across four", "Location"),
    (698, "regions and needs an integration engineer", "Marrow Hollow"),
    (682, "to keep its carrier contracts in step with", "Employment type"),
    (666, "each other. You will own the message", "Full time, permanent"),
    (650, "contract shared by nine carrier", "Pay"),
    (634, "integrations and the nightly suite that", "Band 4, reviewed yearly"),
    (618, "holds it steady.", "Apply by"),
    (602, "WHAT YOU WILL DO", "14 March"),
    (586, "Extend the shared contract as carriers join.", ""),
    (570, "Keep the integration suite green each night.", ""),
    (554, "Review the schedule the dispatch team runs.", ""),
]


def placements() -> list[Placed]:
    """Return each row across the body cell and the sidebar cell."""
    placed: list[Placed] = []
    for y, body, sidebar in ROWS:
        if body:
            placed.append((BODY_X, y, body))
        if sidebar:
            placed.append((SIDEBAR_X, y, sidebar))
    return placed


if __name__ == "__main__":
    text_pdf(Path(sys.argv[1]), placements())
