"""Build posting.pdf, an ordinary one-column posting with an indented bullet list.

Usage, from the repository root, with this directory as <here>:
    python -m tests.fixtures.posting_formats.single_column_pdf.build <here>/posting.pdf

The bullets are indented under their heading, so the page carries an inner
left-hand gap that is not a gutter.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.support.pdf_documents import Placed, text_pdf

BODY_X = 45
BULLET_X = 75

LINES: list[tuple[str, bool]] = [
    ("Data Platform Engineer at Northwind Rail", False),
    ("Northwind Rail runs the timetable that four regional operators publish from,", False),
    ("and the platform team keeps the feeds behind it honest. You will own the", False),
    ("ingestion path from operator uploads through to the published timetable.", False),
    ("WHAT WE ARE LOOKING FOR", False),
    ("Five years building batch or streaming data pipelines in production.", True),
    ("Fluency in Python and SQL, and comfort reading a schema you did not write.", True),
    ("Experience carrying a pager for a pipeline you wrote.", True),
    ("HOW WE WORK", False),
    ("Reviews are written, releases are small, and the roadmap is public to the", False),
    ("whole company. The team meets in person two days a week.", False),
]


def placements() -> list[Placed]:
    """Return every line down the page, bullets set at their own indent."""
    return [
        (BULLET_X if indented else BODY_X, 730 - 16 * index, text)
        for index, (text, indented) in enumerate(LINES)
    ]


if __name__ == "__main__":
    text_pdf(Path(sys.argv[1]), placements())
