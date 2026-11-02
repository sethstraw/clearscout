"""Build cv.pdf, a two-column CV whose person, employers, and institutions are invented.

Usage, from the repository root, with this directory as <here>:
    python -m tests.fixtures.cv_formats.two_column_pdf.build <here>/cv.pdf

Each line is placed at an explicit coordinate and written in row order, left cell
before right cell, which is how a page laid out row by row hands its text to a
reader that follows the stream.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.support.pdf_documents import Placed, text_pdf

LEFT_X = 62
RIGHT_X = 330

HEADER: list[tuple[int, str]] = [
    (742, "Rowan Example"),
    (726, "rowan.example@example.com | +00 555 0177"),
]
ROWS: list[tuple[int, str, str]] = [
    (690, "SKILLS", "PROFESSIONAL EXPERIENCE"),
    (674, "Python, Go, PostgreSQL, Kafka,", "Acme Logistics"),
    (658, "Terraform, Kubernetes", "Platform Engineer | 2019 to Present"),
    (642, "", "Springfield"),
    (626, "EDUCATION", "Consolidated 14 regional dispatch"),
    (610, "MSc, Distributed Systems,", "services into one scheduling API."),
    (594, "Example University, 2013 to 2015", "Cut overnight batch runtime from"),
    (578, "", "6 hours to 35 minutes."),
    (562, "", "Initrode Freight"),
    (546, "", "Integration Engineer | 2015 to 2019"),
    (530, "", "Riverside"),
    (514, "", "Delivered 9 carrier integrations"),
    (498, "", "against a shared message contract."),
]


def placements() -> list[Placed]:
    """Return the header, then each row across both columns."""
    placed: list[Placed] = [(LEFT_X, y, text) for y, text in HEADER]
    for y, left, right in ROWS:
        if left:
            placed.append((LEFT_X, y, left))
        if right:
            placed.append((RIGHT_X, y, right))
    return placed


if __name__ == "__main__":
    text_pdf(Path(sys.argv[1]), placements())
