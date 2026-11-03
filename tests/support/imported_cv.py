"""The messy source CV two suites import from, and one faithful structuring of it."""

from __future__ import annotations

from clearscout.application.master_cv.sources import SourceDocument, SourceLine
from clearscout.application.tailoring.stages.structure import (
    Dropped,
    ImportedCv,
    ImportedRole,
    Sourced,
)

# A CV as a PDF actually hands it over: a bullet broken across lines, a header
# with three fields on one line, and a page footer.
MESSY_CV = """\
Jane Q. Example
Springfield, Illinois | jane@example.com | 555-0100
SUMMARY
A senior data platform engineer with eleven years of experience.
EXPERIENCE
Northwind Analytics | Staff Systems Engineer | 2021-Present
Rebuilt the ingestion tier on Kafka and cut median end to end
latency from 40 minutes to 90 seconds across 212 source feeds.
Designed the schema registry and its compatibility rules.
EDUCATION
Bachelor of Science, Computer Science, University of Illinois, 2010-2014
Page 1 of 2
"""


def source_from(text: str) -> SourceDocument:
    """Number the non-empty lines the way a reader of the source document would."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return SourceDocument(
        origin="test",
        method="text",
        lines=tuple(SourceLine(number=i, text=t) for i, t in enumerate(lines, 1)),
    )


def tidy_extraction() -> ImportedCv:
    """What a faithful model returns: lines merged and split, dates copied as written."""
    return ImportedCv(
        name=Sourced(text="Jane Q. Example", lines=(1,)),
        contact=(Sourced(text="Springfield, Illinois | jane@example.com | 555-0100", lines=(2,)),),
        summary=(
            Sourced(
                text="A senior data platform engineer with eleven years of experience.",
                lines=(4,),
            ),
        ),
        roles=(
            ImportedRole(
                organisation=Sourced(text="Northwind Analytics", lines=(6,)),
                title=Sourced(text="Staff Systems Engineer", lines=(6,)),
                dates=Sourced(text="2021-Present", lines=(6,)),
                bullets=(
                    # One achievement the page broke over two lines.
                    Sourced(
                        text=(
                            "Rebuilt the ingestion tier on Kafka and cut median end to end "
                            "latency from 40 minutes to 90 seconds across 212 source feeds."
                        ),
                        lines=(7, 8),
                    ),
                    Sourced(
                        text="Designed the schema registry and its compatibility rules.",
                        lines=(9,),
                    ),
                ),
            ),
        ),
        education=(
            Sourced(
                text=("Bachelor of Science, Computer Science, University of Illinois, 2010-2014"),
                lines=(11,),
            ),
        ),
        dropped=(
            Dropped(line=3, reason="section heading, replaced by the structure"),
            Dropped(line=5, reason="section heading, replaced by the structure"),
            Dropped(line=10, reason="section heading, replaced by the structure"),
            Dropped(line=12, reason="page footer"),
        ),
    )
