"""Verify a drafted resume, and the text a reviewer supplies in place of a line."""

from __future__ import annotations

from collections.abc import Sequence

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.draft import DraftedLine, ResumeDraft, skill_placements
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.application.tailoring.stages.select import Plan
from clearscout.shared.results import Finding, Outcome

from .checks import (
    duration_scope,
    factual_review,
    figures,
    missing_must_haves,
    must_have_coverage,
    numbers,
    ownership_verbs,
    provenance,
    structure,
)
from .citations import cited, uncitable_ids
from .guardrail_checks import (
    characters,
    characters_in_source,
    first_person,
    forbidden,
    protected,
    required,
)
from .lexicon import WORD
from .report import Report, coverage_finding, keyword_coverage


def check(
    *,
    markdown: str,
    draft: ResumeDraft,
    plan: Plan,
    inventory: CvInventory,
    spec: JobSpec,
    guardrails: Guardrails,
    excluded: frozenset[str] = frozenset(),
    drafted: ResumeDraft | None = None,
) -> Report:
    """Run every programmatic check and return the findings and keyword coverage.

    ``excluded`` names what fitting dropped: a line resting only on those entries,
    and a dropped publication, answers nothing. ``drafted`` is the draft before
    fitting, so a role key fitting dropped still fails provenance.
    """
    coverage = keyword_coverage(markdown, spec)
    # Copied master-CV text is an input defect, not a rewrite failure.
    written = "\n".join(line.text for line in draft.lines())
    # Copied and trimmed entries were never the drafter's to restore.
    shown = set(plan.drafted_ids()) - excluded
    shown_text = {entry.id: entry.full_text for entry in inventory.entries if entry.id in shown}
    recorded = [entry.full_text for entry in inventory.entries if entry.id not in shown]
    findings = (
        provenance(draft, plan, drafted),
        must_have_coverage(draft, plan, spec, excluded),
        numbers(draft, inventory),
        duration_scope(draft.lines(), inventory),
        ownership_verbs(draft.lines(), inventory),
        _skills_grouping(draft, inventory),
        factual_review(draft.lines(), inventory),
        _repeated_phrasing(draft),
        structure(markdown),
        first_person(written, guardrails),
        characters(written, guardrails, source="the drafted text"),
        characters_in_source(markdown, written, guardrails),
        forbidden(markdown, written, guardrails),
        required(markdown, guardrails, drafted=shown_text, recorded=recorded),
        protected(markdown, guardrails),
        coverage_finding(coverage),
    )
    offenders = {"must-have coverage": len(missing_must_haves(draft, plan, spec, excluded))}
    return Report(
        findings=findings,
        coverage=coverage,
        offenders=offenders,
        uncitable=uncitable_ids(inventory, plan.all_ids()),
    )


def check_supplied(
    *,
    lines: Sequence[DraftedLine],
    plan: Plan,
    inventory: CvInventory,
    guardrails: Guardrails,
) -> tuple[Finding, ...]:
    """Hold text a reviewer supplied to exactly the checks drafted text faces.

    Structure, coverage, and required strings are left out: they are properties
    of the whole document, which is checked again in full once the approval has
    been applied.
    """
    text = "\n".join(line.text for line in lines)
    return (
        cited(lines, plan, "sources cited"),
        figures(lines, inventory, "numbers traced"),
        duration_scope(lines, inventory),
        first_person(text, guardrails),
        characters(text, guardrails, source="the replacement text"),
        forbidden(text, text, guardrails),
        protected(text, guardrails),
    )


def _repeated_phrasing(draft: ResumeDraft) -> Finding:
    """Report substantial exact phrase reuse separately from factual support."""
    summary = [word.lower() for word in WORD.findall(draft.summary.text)]
    phrases = {tuple(summary[index : index + 7]) for index in range(len(summary) - 6)}
    repeated = []
    for role in draft.roles:
        for bullet in role.bullets:
            words = [word.lower() for word in WORD.findall(bullet.text)]
            if any(tuple(words[index : index + 7]) in phrases for index in range(len(words) - 6)):
                repeated.append(bullet.text[:80])
    if repeated:
        return Finding(
            "repeated phrasing",
            Outcome.FAIL,
            "Summary and bullet reuse at least seven consecutive words; vary the construction: "
            + repeated[0],
        )
    return Finding(
        "repeated phrasing",
        Outcome.PASS,
        "No seven-word exact phrase reused between summary and bullets; broader voice needs review",
    )


def _skills_grouping(draft: ResumeDraft, inventory: CvInventory) -> Finding:
    """Warn on a skills item no cited entry lists, whether or not the CV's skills carry headings.

    Under headings such an item sits under its line's first citation's heading.
    """
    unplaced = []
    for skill in draft.skills:
        entries = [entry for entry_id in skill.source_ids if (entry := inventory.entry(entry_id))]
        if any(listing is None for _, listing in skill_placements(skill.text, entries)):
            unplaced.append(f"{skill.label}: {skill.text}"[:80])
    if unplaced:
        return Finding(
            "skills grouping",
            Outcome.WARN,
            f"{len(unplaced)} skills line(s) carry an item no cited entry lists; "
            f"review: {'; '.join(unplaced)}",
            redraftable=False,
        )
    return Finding("skills grouping", Outcome.PASS, "every skills item is listed by its entry")
