"""Read the posting's family, specialty and orientation, and what the CV evidences."""

from __future__ import annotations

from datetime import date

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.fit.lexicon import (
    FAMILY_MARGIN,
    INDIVIDUAL_CONTRIBUTOR,
    MANAGEMENT_TITLES,
    MINIMUM_FAMILY_EVIDENCE,
    MINIMUM_SPECIALTY_EVIDENCE,
    PEOPLE_MANAGEMENT,
    SPECIALTY_MARGIN,
    TITLE_WEIGHT,
)
from tailorcv.application.tailoring.fit.structure import (
    Answer,
    Block,
    SectionKind,
    first_saying,
    quote_line,
)
from tailorcv.domain.taxonomy import Family, Taxonomy, says


def _score(lexicon: Taxonomy, family: Family, title: str, body: str) -> tuple[int, tuple[str, ...]]:
    """Return one family's evidence score and the phrases it rests on."""
    titles = lexicon.present(family.titles, title)
    terms = lexicon.present(family.terms, body)
    evidence = (*(f"title says {phrase!r}" for phrase in titles), *terms)
    return TITLE_WEIGHT * len(titles) + len(terms), evidence


def read_family(lexicon: Taxonomy, title: str, body: str) -> tuple[Family | None, Answer[str]]:
    """Return the family the text evidences, or an abstention naming the contenders.

    Two bars, and both are needed: a minimum, so one shared phrase is not a
    classification, and a margin, so a posting the vocabulary reads two ways is
    reported that way.
    """
    scored = sorted(
        ((_score(lexicon, family, title, body), family) for family in lexicon.families),
        key=lambda item: (-item[0][0], item[1].key),
    )
    (best_score, best_evidence), best = scored[0]
    runner_score = scored[1][0][0] if len(scored) > 1 else 0
    if best_score < MINIMUM_FAMILY_EVIDENCE:
        return None, Answer(
            "Role family",
            note=(
                "the posting does not use enough of any family's vocabulary to place it; "
                f"the closest was {best.label.casefold()} with {best_score} point(s)"
            ),
        )
    if best_score - runner_score < FAMILY_MARGIN:
        # A family named in the title outranks one the body only mentions, but only
        # when exactly one of the two contenders is named in the title; two titled
        # contenders, or none, still reads two ways.
        titled = [
            (evidence, family)
            for (score, evidence), family in scored[:2]
            if score and any(phrase.startswith("title says") for phrase in evidence)
        ]
        if len(titled) == 1:
            evidence, family = titled[0]
            return family, Answer("Role family", family.label, evidence=evidence)
        contenders = ", ".join(
            family.label.casefold() for (score, _), family in scored[:2] if score
        )
        return None, Answer(
            "Role family",
            note=(
                "the vocabulary reads this two ways and the text does not separate them: "
                f"{contenders}"
            ),
        )
    return best, Answer("Role family", best.label, evidence=best_evidence)


def read_specialty(lexicon: Taxonomy, family: Family | None, body: str) -> Answer[str]:
    """Return the narrower reading inside a family, when the text earns one."""
    if family is None:
        return Answer("Specialty", note="no family was placed, so there is nothing to narrow")
    if not family.specialties:
        return Answer(
            "Specialty", note=f"the taxonomy carries no specialties under {family.label.casefold()}"
        )
    scored = sorted(
        ((lexicon.present(item.terms, body), item) for item in family.specialties),
        key=lambda item: (-len(item[0]), item[1].key),
    )
    found, best = scored[0]
    runner = len(scored[1][0]) if len(scored) > 1 else 0
    if len(found) < MINIMUM_SPECIALTY_EVIDENCE or len(found) - runner < SPECIALTY_MARGIN:
        return Answer(
            "Specialty",
            note=(
                "the posting does not use enough of one specialty's vocabulary to narrow "
                f"{family.label.casefold()}"
            ),
        )
    return Answer("Specialty", best.label, evidence=found)


def read_orientation(blocks: tuple[Block, ...], title: str) -> Answer[str]:
    """Return whether the posting states people management, and abstain when it does not.

    A statement outranks a title, in both directions. "No direct reports" settles
    it whatever the title says, and a title containing "Manager" settles nothing:
    a roadmap, a backlog, and a vendor are all managed with the same verb.
    """
    question = "Individual contributor or management"
    lines = tuple(line for block in blocks for line in block.lines)
    if found := first_saying(lines, INDIVIDUAL_CONTRIBUTOR):
        return Answer(question, "individual contributor", evidence=(quote_line(found[1]),))
    responsibilities = tuple(
        line
        for block in blocks
        if block.kind in (SectionKind.RESPONSIBILITY, SectionKind.REQUIREMENT, SectionKind.OTHER)
        for line in block.lines
    )
    if found := first_saying(responsibilities, PEOPLE_MANAGEMENT):
        return Answer(question, "management", evidence=(quote_line(found[1]),))
    if titles := says(title, MANAGEMENT_TITLES):
        return Answer(
            question,
            note=(
                f"the title says {titles[0]!r} and no responsibility states a report, a hire, "
                "or a review, so the posting does not settle it"
            ),
        )
    return Answer(
        question, note="nothing states people management or an individual-contributor role"
    )


def read_record_families(lexicon: Taxonomy, inventory: CvInventory) -> Answer[str]:
    """Return which families the master CV's own language evidences.

    Reported beside the posting's family and never subtracted from it. Whether a
    record answers a posting is a judgment the matching stage owns.
    """
    titles = " ".join(
        part for role in inventory.roles for part in (role.title or "", role.organization)
    )
    body = "\n".join(entry.full_text for entry in inventory.entries)
    scored = [
        (score, family)
        for family in lexicon.families
        if (score := _score(lexicon, family, titles, body)[0]) >= MINIMUM_FAMILY_EVIDENCE
    ]
    if not scored:
        return Answer(
            "What the record reads as",
            note="the master CV does not use enough of any family's vocabulary to place it",
        )
    labels = tuple(
        family.label for _, family in sorted(scored, key=lambda item: (-item[0], item[1].key))[:3]
    )
    return Answer("What the record reads as", ", ".join(labels), evidence=labels)


def read_record_years(inventory: CvInventory, today: date) -> Answer[int]:
    """Return the span the master CV's dated roles cover, or that they carry no dates.

    A span, not a career length. Gaps, overlaps, and part-time work are invisible
    to a date range, so the evidence names the two years it was measured between.
    """
    dated = [role for role in inventory.roles if role.start_year is not None]
    if not dated:
        return Answer("Years the record covers", note="no role in the master CV carries a date")
    starts = [role.start_year for role in dated if role.start_year is not None]
    ends = [
        today.year if role.current or role.end_year is None else role.end_year for role in dated
    ]
    first, last = min(starts), max(ends)
    return Answer(
        "Years the record covers",
        max(0, last - first),
        evidence=(f"earliest dated role starts {first}, latest ends {last}",),
    )
