"""The synthetic CV format corpus under ``tests/fixtures/cv_formats`` and what it should yield.

Each shape directory holds one source CV and an ``expected.toml`` naming the fields a
faithful extraction keeps. ``correct_extraction`` builds that extraction against the
lines the shipped reader produced, citing each field's own lines.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from clearscout.application.master_cv.sources import SourceDocument
from clearscout.application.tailoring.stages.structure import (
    Dropped,
    ImportedCv,
    ImportedRole,
    ImportedSection,
    Sourced,
)

CV_FORMATS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "cv_formats"
SHAPES = (
    "europass_docx",
    "academic_pdf",
    "functional_md",
    "linkedin_txt",
    "two_column_pdf",
    "long_md",
)
MARKUP = re.compile(r"^(?:#{1,6}\s+|[-*+\u2022]\s+|\d{1,3}[.)]\s+)")


def cv_format(shape: str) -> tuple[Path, dict[str, Any]]:
    """Return one corpus CV and the fields a faithful extraction holds."""
    if shape not in SHAPES:
        raise ValueError(f"no CV format shape named {shape!r}")
    expected = tomllib.loads((CV_FORMATS / shape / "expected.toml").read_text(encoding="utf-8"))
    return CV_FORMATS / shape / expected["source"], expected


def flat(text: str) -> str:
    """Drop list and heading markup and bold markers, and collapse whitespace."""
    return " ".join(MARKUP.sub("", text.strip()).replace("**", "").split())


def within(wanted: str, text: str) -> bool:
    """Return whether ``wanted`` occurs in ``text`` as whole words."""
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", text) is not None


def cite(source: SourceDocument, text: str, start: int = 1) -> tuple[int, ...]:
    """Return the lines holding ``text`` from ``start`` on, preferring an exact span."""
    lines = [line for line in source.lines if line.number >= start]
    wanted = flat(text)
    for exact in (True, False):
        for width in range(1, 5):
            for index in range(len(lines) - width + 1):
                span = lines[index : index + width]
                joined = " ".join(flat(line.text) for line in span)
                if joined == wanted if exact else within(wanted, joined):
                    return tuple(line.number for line in span)
    raise AssertionError(f"{text!r} is in no line of {source.origin} from line {start}")


def correct_extraction(source: SourceDocument, expected: dict[str, Any]) -> ImportedCv:
    """Build the extraction a faithful model returns for one corpus CV."""

    def top(key: str) -> tuple[Sourced, ...]:
        return tuple(Sourced(text=text, lines=cite(source, text)) for text in expected.get(key, []))

    roles = []
    for role in expected["roles"]:
        organisation = Sourced(text=role["employer"], lines=cite(source, role["employer"]))
        cursor = anchor = organisation.lines[0]
        fields: dict[str, Any] = {}
        for key in ("title", "dates", "location"):
            if key in role:
                # A packed line can hold the dates above the title line.
                try:
                    lines = cite(source, role[key], cursor)
                except AssertionError:
                    lines = cite(source, role[key], anchor)
                fields[key] = Sourced(text=role[key], lines=lines)
                cursor = max(cursor, lines[0])
        bullets = []
        for text in role["bullets"]:
            bullets.append(Sourced(text=text, lines=cite(source, text, cursor)))
            cursor = bullets[-1].lines[-1]
        roles.append(ImportedRole(organisation=organisation, bullets=tuple(bullets), **fields))
    return ImportedCv(
        name=Sourced(text=expected["name"], lines=cite(source, expected["name"])),
        contact=top("contact"),
        summary=top("summary"),
        skills=top("skills"),
        roles=tuple(roles),
        education=top("education"),
        publications=top("publications"),
        sections=tuple(
            ImportedSection(
                heading=Sourced(text=section["heading"], lines=cite(source, section["heading"])),
                items=tuple(
                    Sourced(text=text, lines=cite(source, text)) for text in section["items"]
                ),
            )
            for section in expected.get("sections", [])
        ),
        dropped=tuple(
            Dropped(line=cite(source, text)[0], reason="heading or page furniture")
            for text in expected["dropped"]
        ),
    )
