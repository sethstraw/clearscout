"""Build posting.pdf, a posting whose details sit in a label-and-value table.

Usage, from the repository root, with this directory as <here>:
    python -m tests.fixtures.posting_formats.details_table_pdf.build <here>/posting.pdf

Each details row places its label and its value on the same baseline, which is
how a posting states location, employment type, and pay.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.support.pdf_documents import Placed, text_pdf

LABEL_X = 45
VALUE_X = 260

BODY = [
    "Staff Platform Engineer at Acme Logistics",
    "We are hiring a staff platform engineer to own the scheduling API and the",
    "services that feed it. You will lead the migration of fourteen regional",
    "dispatch services onto a single contract, and set the review bar for the",
    "team that keeps them running. Eight years of production experience with",
    "distributed systems is what we are looking for.",
]
DETAILS = [
    ("Location", "Bramblewick, Calderfield"),
    ("Employment type", "Full time, permanent"),
    ("Requisition", "REQ-4417"),
    ("Posted", "3 days ago"),
    ("Team", "Platform engineering"),
    ("Reports to", "Director of Engineering"),
    ("On site", "Two days a week"),
    ("Travel", "10 percent"),
]


def placements() -> list[Placed]:
    """Return the body paragraph, then each details row across both cells."""
    placed: list[Placed] = [(LABEL_X, 730 - 16 * index, line) for index, line in enumerate(BODY)]
    for index, (label, value) in enumerate(DETAILS):
        placed += [(LABEL_X, 610 - 16 * index, label), (VALUE_X, 610 - 16 * index, value)]
    return placed


if __name__ == "__main__":
    text_pdf(Path(sys.argv[1]), placements())
