"""The line model, checked against pressume renders of synthetic documents.

Every recorded number is a measurement, not an expectation: ``resume`` below
built the document, pressume rendered it with the traditional theme at
balanced density on US Letter, and the lines were read back from the PDF.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import EntryKind, parse_cv
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.layout import (
    BLOCK_GAP_LINES,
    CHARACTERS_PER_LINE,
    LINES_PER_PAGE,
    NAME_LINES,
    PAGE_BREAK_LINES,
    ROLE_LINES,
    SECTION_LINES,
    SHAPING_CONSTANTS,
    Budget,
    markdown_lines,
    shaping_fingerprint,
    wrapped_lines,
)
from clearscout.application.tailoring.stages.draft import (
    Document,
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
)
from clearscout.application.tailoring.stages.select import Plan

PROSE = (
    "Led a 12-person platform team through the migration of 40 PostgreSQL services to "
    "AWS, cutting p95 latency by 35% and saving $1.2M a year in hosting. Built the Kafka "
    "ingestion tier that now carries 3 billion events a day for Finance, Operations and "
    "the Analytics Guild. Designed the schema registry, data contracts and lineage "
    "service adopted by 9 product teams; wrote the Terraform modules, CI/CD pipelines and "
    "on-call runbooks. Partnered with Legal and Compliance on HIPAA and SOC 2 audits, "
    "closing 27 findings ahead of the Q3 deadline. Mentored 6 engineers, 2 of whom were "
    "promoted to Senior within a year. Standardized cross-functional observability, "
    "reconciliation and incident-management practices across infrastructure, "
    "implementation and customer-onboarding workstreams."
)
TEXT = PROSE.split()


def filler(chars: int, offset: int = 0) -> str:
    """Return at most ``chars`` characters of resume-like text starting at word ``offset``."""
    words: list[str] = []
    index = offset
    while len(" ".join(words)) < chars:
        words.append(TEXT[index % len(TEXT)])
        index += 1
    text = " ".join(words)
    while len(text) > chars and " " in text:
        text = text.rsplit(" ", 1)[0]
    return text.rstrip(",;") + "."


def spread(chars: int, index: int) -> int:
    """Return ``chars`` moved up to 45 characters either way, varying with ``index``."""
    return max(20, chars + (index * 37) % 91 - 45)


def resume(
    *,
    roles: int,
    bullets: int,
    bullet_chars: int,
    context_chars: int = 0,
    skill_lines: int = 4,
    skill_chars: int = 150,
    summary_chars: int = 300,
    contacts: tuple[int, ...] = (60, 45),
    education: int = 2,
    publications: int = 0,
    earlier: bool = False,
) -> str:
    """Return what assembly writes for a synthetic record drafted verbatim, entry for entry.

    ``bullets`` are spread across ``roles``, most recent role first.
    """
    role_blocks = []
    for role in range(roles):
        lines = [
            f"### Organization {role + 1}",
            "",
            f"**Senior Engineer {role + 1}**  ",
            f"January {2010 + role} to December {2011 + role} | Remote",
        ]
        if context_chars:
            lines += ["", filler(spread(context_chars, role), 13 * role)]
        share = [
            f"- {filler(spread(bullet_chars, 7 * role + index), 17 * role + 3 * index)}"
            for index in range(bullets // roles + (1 if role < bullets % roles else 0))
        ]
        if share:
            lines += ["", *share]
        role_blocks.append("\n".join(lines))
    if earlier:
        role_blocks.append(
            "### Early Organization\n\n**Engineer**  \nMarch 2001 to June 2009 | Remote"
        )
    skills = [
        f"**Area {index + 1}:** {filler(spread(skill_chars, index), 5 * index)}"
        for index in range(skill_lines)
    ]
    record = parse_cv(
        Path("master_cv.md"),
        "\n\n".join(
            [
                "# Jordan Example",
                *(filler(chars, 3 * index) for index, chars in enumerate(contacts)),
                f"## Summary\n\n{filler(summary_chars, 11)}",
                "## Core Skills\n\n" + "\n\n".join(skills),
                "## Professional Experience\n\n" + "\n\n".join(role_blocks),
                "## Education\n\n"
                + "\n\n".join(
                    f"Master of Science {index + 1}, Example State University, 20{10 + index}"
                    for index in range(education)
                ),
                "## Publications\n\n"
                + "\n".join(f"- {filler(160, 7 * index)}" for index in range(publications)),
            ]
        ),
    )
    keys = [role.key for role in record.roles]
    recent = tuple(keys[:roles])

    def ids(kind: EntryKind, role_key: str | None = None) -> tuple[str, ...]:
        return tuple(
            entry.id
            for entry in record.of_kind(kind)
            if role_key is None or entry.role_key == role_key
        )

    def verbatim(entry_id: str) -> DraftedLine:
        return DraftedLine(text=record.by_id(entry_id).text, source_ids=(entry_id,))

    plan = Plan(
        summary_ids=ids(EntryKind.SUMMARY),
        skill_ids=ids(EntryKind.SKILL),
        recent_roles=recent,
        earlier_roles=tuple(keys[roles:]),
        role_context={key: ids(EntryKind.ROLE_CONTEXT, key) for key in recent},
        role_bullets={key: ids(EntryKind.BULLET, key) for key in recent},
        education_ids=ids(EntryKind.EDUCATION),
        publication_ids=ids(EntryKind.PUBLICATION),
    )
    draft = ResumeDraft(
        summary=verbatim(plan.summary_ids[0]),
        skills=tuple(
            DraftedSkill(
                label=record.by_id(entry_id).label or "",
                text=record.by_id(entry_id).text,
                source_ids=(entry_id,),
            )
            for entry_id in plan.skill_ids
        ),
        roles=tuple(
            DraftedRole(
                role_key=key,
                context=verbatim(plan.role_context[key][0]) if plan.role_context[key] else None,
                bullets=tuple(verbatim(entry_id) for entry_id in plan.role_bullets[key]),
            )
            for key in recent
        ),
        earlier_career=DraftedLine(text=filler(220, 29)) if earlier else None,
    )
    return Document(draft, record, plan, Settings(), Guardrails()).markdown()


# Shape, then the rendered lines summed over pages from each page's top to its last line.
RENDERED: list[tuple[dict[str, Any], float]] = [
    ({"roles": 2, "bullets": 6, "bullet_chars": 150}, 46.19),
    ({"roles": 3, "bullets": 10, "bullet_chars": 180, "context_chars": 150}, 64.29),
    (
        {
            "roles": 4,
            "bullets": 12,
            "bullet_chars": 120,
            "context_chars": 240,
            "skill_lines": 5,
            "skill_chars": 200,
        },
        75.25,
    ),
    (
        {"roles": 2, "bullets": 4, "bullet_chars": 300, "publications": 4, "education": 3},
        59.46,
    ),
    (
        {"roles": 5, "bullets": 15, "bullet_chars": 200, "contacts": (60, 45, 180, 90)},
        82.99,
    ),
    (
        {"roles": 3, "bullets": 9, "bullet_chars": 100, "earlier": True, "skill_lines": 2},
        52.81,
    ),
    (
        {
            "roles": 6,
            "bullets": 24,
            "bullet_chars": 190,
            "context_chars": 200,
            "skill_lines": 5,
            "skill_chars": 180,
            "education": 4,
        },
        117.39,
    ),
    (
        {
            "roles": 8,
            "bullets": 30,
            "bullet_chars": 230,
            "context_chars": 180,
            "skill_lines": 5,
            "skill_chars": 220,
            "education": 4,
            "contacts": (70, 60, 200, 60),
        },
        162.05,
    ),
    (
        {
            "roles": 4,
            "bullets": 40,
            "bullet_chars": 160,
            "publications": 5,
            "education": 2,
            "earlier": True,
        },
        138.75,
    ),
    (
        {
            "roles": 3,
            "bullets": 45,
            "bullet_chars": 250,
            "context_chars": 260,
            "skill_lines": 6,
            "skill_chars": 260,
        },
        181.11,
    ),
    (
        {
            "roles": 7,
            "bullets": 35,
            "bullet_chars": 140,
            "context_chars": 120,
            "skill_lines": 4,
            "education": 3,
            "publications": 3,
        },
        133.77,
    ),
]

# The estimate may miss a render by this share of its lines.
RENDERED_TOLERANCE = 0.02

# Shape without a bullet count, then the most bullets that still rendered on two pages.
TWO_PAGES_HELD: list[tuple[dict[str, Any], int]] = [
    ({"roles": 2, "bullet_chars": 160}, 47),
    ({"roles": 4, "bullet_chars": 200, "context_chars": 180, "publications": 3}, 31),
    (
        {
            "roles": 6,
            "bullet_chars": 150,
            "context_chars": 150,
            "skill_lines": 5,
            "skill_chars": 200,
            "education": 3,
        },
        35,
    ),
    (
        {
            "roles": 8,
            "bullet_chars": 230,
            "context_chars": 200,
            "skill_lines": 5,
            "skill_chars": 220,
            "education": 4,
            "contacts": (70, 60, 200, 60),
        },
        17,
    ),
    (
        {
            "roles": 3,
            "bullet_chars": 110,
            "earlier": True,
            "publications": 5,
            "education": 2,
        },
        50,
    ),
    (
        {
            "roles": 5,
            "bullet_chars": 300,
            "skill_lines": 6,
            "skill_chars": 260,
            "summary_chars": 450,
        },
        22,
    ),
]

# A prediction may fall short of what two pages held by this many bullets, never exceed it.
BULLETS_SHORT_OF_A_FULL_PAGE = 2


@pytest.mark.parametrize(("shape", "rendered"), RENDERED)
def test_the_estimate_matches_what_the_renderer_printed(
    shape: dict[str, Any], rendered: float
) -> None:
    assert abs(markdown_lines(resume(**shape)) - rendered) <= RENDERED_TOLERANCE * rendered


@pytest.mark.parametrize(("shape", "held"), TWO_PAGES_HELD)
def test_the_page_budget_fills_two_pages_without_spilling_to_a_third(
    shape: dict[str, Any], held: int
) -> None:
    """Under-filling by a bullet or two is recoverable; over-filling silently costs a page."""
    budget = Budget(2).total_lines
    predicted = max(
        count for count in range(1, 121) if markdown_lines(resume(bullets=count, **shape)) <= budget
    )
    assert held - BULLETS_SHORT_OF_A_FULL_PAGE <= predicted <= held


def test_editing_the_length_budget_moves_the_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calibration change is invisible to every declared setting.

    Two calibrations would otherwise be identical inputs, and the reuse check
    would serve the wrong package.
    """
    before = shaping_fingerprint()
    assert before == shaping_fingerprint(), "the fingerprint has to be stable to be an identity"

    for constant in ("CHARACTERS_PER_LINE", "ROLE_LINES"):
        moved = dict(SHAPING_CONSTANTS, **{constant: SHAPING_CONSTANTS[constant] + 1})
        monkeypatch.setattr("clearscout.application.tailoring.layout.SHAPING_CONSTANTS", moved)
        assert shaping_fingerprint() != before, f"editing {constant} left the identity unchanged"


def test_each_heading_level_costs_its_measured_height() -> None:
    assert markdown_lines("# Jordan Example") == NAME_LINES
    assert markdown_lines("## Summary") == SECTION_LINES
    assert markdown_lines("### Globex") == ROLE_LINES


def test_a_hard_line_break_starts_a_line_and_a_soft_one_does_not() -> None:
    assert markdown_lines("**Staff Engineer**  \nMarch 2021 to Present") == 2 + BLOCK_GAP_LINES
    assert markdown_lines("Staff Engineer\nMarch 2021 to Present") == 1 + BLOCK_GAP_LINES


def test_each_list_item_wraps_on_its_own() -> None:
    items = "- " + "x" * (CHARACTERS_PER_LINE + 1) + "\n- short"
    assert markdown_lines(items) == 3 + BLOCK_GAP_LINES


def test_a_nested_list_item_starts_its_own_line() -> None:
    """No indent was measured, so a nested item wraps at the width a flat one does."""
    nested = "- Example J. A registry paper. 2020.\n  - **Publication date:** 1 March 2020"
    flat = "- Example J. A registry paper. 2020.\n- **Publication date:** 1 March 2020"

    assert markdown_lines(nested) == markdown_lines(flat) == 2 + BLOCK_GAP_LINES


def test_emphasis_markers_and_section_rules_print_nothing() -> None:
    bold = "**" + "x" * (CHARACTERS_PER_LINE - 2) + ":** " + "y"
    assert markdown_lines(bold) == 2 + BLOCK_GAP_LINES
    assert markdown_lines("---") == 0


def test_a_bullet_wraps_at_the_measured_width() -> None:
    assert wrapped_lines("x" * CHARACTERS_PER_LINE) == 1
    assert wrapped_lines("x" * (CHARACTERS_PER_LINE + 1)) == 2
    assert wrapped_lines("x" * (CHARACTERS_PER_LINE * 3)) == 3


def test_an_empty_string_still_occupies_a_line() -> None:
    assert wrapped_lines("") == 1


def test_every_page_after_the_first_pays_for_a_page_break() -> None:
    assert Budget(1).total_lines == LINES_PER_PAGE
    assert Budget(2).total_lines == 2 * LINES_PER_PAGE - PAGE_BREAK_LINES


def test_selection_buys_more_bullet_lines_than_the_page_has_left() -> None:
    """Drafting shortens bullets, so selecting only what fits would leave the page short."""
    page = Budget(2)
    assert page.selection_lines(60.0) > page.total_lines - 60.0
    assert page.selection_lines(90.0) < page.selection_lines(60.0)


def test_the_selection_budget_never_goes_negative() -> None:
    assert Budget(1).selection_lines(500.0) == 0
