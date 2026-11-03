"""Synthetic master CVs, one longer than two pages, and drafts written to a set share of it.

Fit only trims when selection buys more than the page holds, which the fixture
record never does.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from clearscout.application.master_cv.cv import CvInventory, EntryKind, parse_cv
from clearscout.application.tailoring.stages.draft import (
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import Classification, Match, Strength
from clearscout.application.tailoring.stages.select import Plan

PROSE = (
    "migrated the claims warehouse to a streaming design, cut the nightly load from 7 "
    "hours to 40 minutes, and trained 12 analysts on the new lineage and data quality "
    "tooling"
)
WORDS = PROSE.split()

DETAILED_BULLET = "Led a vocabulary-mapping program across three code systems"
DETAIL = "The crosswalks covered 40,000 codes and used ToolX"


def sentence(chars: int, offset: int) -> str:
    """Return synthetic evidence text of at most ``chars`` characters."""
    words: list[str] = []
    while len(" ".join(words)) < chars:
        words.append(WORDS[(offset + len(words)) % len(WORDS)])
    return " ".join(words)[:chars].rsplit(" ", 1)[0].capitalize() + "."


def long_record(tmp_path: Path, papers: tuple[str, ...] = ()) -> CvInventory:
    """Return a record of three roles with 30 bullets each, far more than two pages hold."""
    publications = "".join(f"- {paper}\n" for paper in papers)
    roles = [
        f"### Example Org {role + 1}\n\n**Senior Data Engineer**  \n"
        f"March {2022 - 4 * role} to {'Present' if role == 0 else f'May {2025 - 4 * role}'}"
        f" | Remote\n\n{sentence(160, role)}\n\n"
        + "\n".join(
            f"- {sentence(100 + (index * 53) % 130, 7 * role + index)}" for index in range(30)
        )
        for role in range(3)
    ]
    source = tmp_path / "CV.md"
    source.write_text(
        "# JORDAN EXAMPLE\n\nSpringfield, Illinois | jordan@example.com\n\n"
        f"## Summary\n\n{sentence(400, 3)}\n\n"
        f"## Core Skills\n\n**Platforms:** {sentence(180, 5)}\n\n"
        f"**Governance:** {sentence(150, 9)}\n\n"
        "## Professional Experience\n\n" + "\n\n".join(roles) + "\n\n"
        "## Education\n\n**Bachelor of Science, Statistics**, Example State University, 2010\n"
        + (f"\n## Publications\n\n{publications}" if papers else ""),
        encoding="utf-8",
    )
    return parse_cv(source)


def detailed_record() -> CvInventory:
    """Return a one-role record whose first bullet carries an indented detail."""
    return parse_cv(
        Path("CV.md"),
        "# JORDAN EXAMPLE\n\nSpringfield, Illinois | jordan@example.com\n\n"
        "## Summary\n\nData engineer for claims analytics.\n\n"
        "## Professional Experience\n\n### Example Org\n\n**Analytics Engineer**  \n"
        "March 2022 to Present | Remote\n\n"
        f"- {DETAILED_BULLET}\n  - {DETAIL}\n"
        "- Ran the weekly data quality review for the claims warehouse\n",
    )


DEGREE = "**Master of Science, Biostatistics**, Example State University, 2012"
THESIS = "Thesis: Mixed models for sparse registries"
PAPER = "Example J. A registry paper. *Journal of Examples*. 2020;1(1):1-2."
PAPER_DATE = "**Publication date:** 1 March 2020"


def nested_copies_record() -> CvInventory:
    """Return a record whose degree and publication each nest one detail beneath them."""
    return parse_cv(
        Path("CV.md"),
        "# JORDAN EXAMPLE\n\njordan@example.com\n\n"
        "## Summary\n\nData engineer for claims analytics.\n\n"
        "## Professional Experience\n\n### Example Org\n\n**Analytics Engineer**  \n"
        "March 2022 to Present | Remote\n\n- Ran the weekly data quality review\n\n"
        f"## Education\n\n- {DEGREE}\n  - {THESIS}\n\n"
        f"## Publications\n\n- {PAPER}\n  - {PAPER_DATE}\n",
    )


def bullet_ids(record: CvInventory) -> list[str]:
    """Return every bullet of ``record`` in entry order."""
    return [entry.id for entry in record.of_kind(EntryKind.BULLET)]


def must_haves(*numbers: int) -> JobSpec:
    """Return a posting asking for one must-have per number, as ``r01`` onwards."""
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=tuple(
            Requirement(id=f"r0{number}", text=f"Need {number}", importance=Importance.MUST_HAVE)
            for number in numbers
        ),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def cited(requirement: str, ids: Iterable[str], strength: Strength = Strength.STRONG) -> Match:
    """Return a match citing ``ids``: direct when strong, transferable otherwise."""
    return Match(
        requirement_id=requirement,
        entry_ids=tuple(ids),
        classification=(
            Classification.DIRECT if strength is Strength.STRONG else Classification.TRANSFERABLE
        ),
        strength=strength,
    )


def drafted_to_length(inventory: CvInventory, plan: Plan, share: float) -> ResumeDraft:
    """Return a draft writing every planned entry at ``share`` of its master text's length."""

    def shortened(entry_id: str) -> DraftedLine:
        text = inventory.by_id(entry_id).text
        return DraftedLine(
            text=text[: int(len(text) * share)].rsplit(" ", 1)[0] + ".", source_ids=(entry_id,)
        )

    return ResumeDraft(
        summary=shortened(plan.summary_ids[0]),
        skills=tuple(
            DraftedSkill(
                label="Skills", text=inventory.by_id(entry_id).text, source_ids=(entry_id,)
            )
            for entry_id in plan.skill_ids
        ),
        roles=tuple(
            DraftedRole(
                role_key=role_key,
                context=shortened(plan.role_context[role_key][0]),
                bullets=tuple(shortened(entry_id) for entry_id in plan.role_bullets[role_key]),
            )
            for role_key in plan.recent_roles
        ),
    )


__all__ = [
    "DEGREE",
    "DETAIL",
    "DETAILED_BULLET",
    "PAPER",
    "PAPER_DATE",
    "THESIS",
    "bullet_ids",
    "cited",
    "detailed_record",
    "drafted_to_length",
    "long_record",
    "must_haves",
    "nested_copies_record",
    "sentence",
]
