"""Deterministic evidence checks: provenance, must-haves, figures, ownership, and structure."""

from __future__ import annotations

from collections.abc import Sequence

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.stages.draft import (
    DraftedLine,
    ResumeDraft,
    SourceType,
    printed_evidence,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec
from clearscout.application.tailoring.stages.select import Plan
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.results import Finding, Outcome
from clearscout.shared.text import sentence_pieces

from .citations import cited, source_text, typed_source_text
from .lexicon import (
    FIRST_PERSON_VERB,
    LICENSED_CREDENTIAL,
    OWNERSHIP_VERBS,
    QUALIFIER,
    WORD,
    figures_in,
)


def provenance(draft: ResumeDraft, plan: Plan) -> Finding:
    """Require every drafted line to cite at least one selected piece of evidence."""
    return cited(draft.lines(), plan, "sources cited")


def _must_haves(spec: JobSpec, plan: Plan) -> set[str]:
    """Return the must-have requirements some CV entry answers."""
    must_haves = {item.id for item in spec.requirements if item.importance is Importance.MUST_HAVE}
    return must_haves & {
        requirement
        for requirements in plan.requirements_for.values()
        for requirement in requirements
    }


def missing_must_haves(
    draft: ResumeDraft, plan: Plan, spec: JobSpec, excluded: frozenset[str] = frozenset()
) -> list[str]:
    """Return the answerable must-haves nothing the page prints answers."""
    answered = {
        requirement
        for entry_id in printed_evidence(draft, plan, excluded)
        for requirement in plan.requirements_for.get(entry_id, ())
    }
    return sorted(_must_haves(spec, plan) - answered)


def must_have_coverage(
    draft: ResumeDraft, plan: Plan, spec: JobSpec, excluded: frozenset[str]
) -> Finding:
    """Distinguish source omissions from redraftable selected bullet omissions."""
    check = "must-have coverage"
    missing = missing_must_haves(draft, plan, spec, excluded)
    if not missing:
        return Finding(
            check,
            Outcome.PASS,
            f"all {len(_must_haves(spec, plan))} source-answerable must-have(s) cited on the page",
        )
    selected = set(plan.all_ids())
    bullet_ids = {entry_id for ids in plan.role_bullets.values() for entry_id in ids}
    omissions = [
        _coverage_omission(item, plan, selected - excluded, bullet_ids - excluded)
        for item in missing
    ]
    redraftable = any(repairable for _, repairable in omissions)
    return Finding(
        check,
        Outcome.FAIL if redraftable else Outcome.WARN,
        "; ".join(detail for detail, _ in omissions),
        redraftable=redraftable,
    )


def _coverage_omission(
    requirement: str, plan: Plan, selected: set[str], bullet_ids: set[str]
) -> tuple[str, bool]:
    """Name the repair available for one source-answerable omitted requirement."""
    candidates = {
        entry_id
        for entry_id, requirements in plan.requirements_for.items()
        if requirement in requirements
    }
    slots = sorted(candidates & bullet_ids)
    if slots:
        return f"{requirement}: selected bullet answer not drafted; use {', '.join(slots)}", True
    if candidates & selected:
        return f"{requirement}: selected context/skill answer not drafted", False
    if candidates & set(plan.all_ids()):
        return f"{requirement}: its answer was dropped to fit the page", False
    return f"{requirement}: source evidence not selected; review selection/space", False


def numbers(draft: ResumeDraft, inventory: CvInventory) -> Finding:
    """Require every number in the resume to appear in the evidence it cites."""
    return figures(draft.lines(), inventory, "numbers traced")


def figures(
    lines: Sequence[DraftedLine],
    inventory: CvInventory,
    check: str,
    *,
    research: tuple[ResearchSource, ...] = (),
) -> Finding:
    """Require every number in the output to appear in the evidence it cites.

    Checked against each cited entry individually, so a line citing two bullets
    cannot borrow a number from one to support a claim made by the other.
    """
    untraced = [
        found
        for line in lines
        for found in _untraced_figures(line, typed_source_text(inventory, line, research))
    ]
    count = sum(len(raws) for raws, _ in untraced)
    offenders = [f"{', '.join(map(repr, raws))} in {sentence!r}" for raws, sentence in untraced]
    if offenders:
        return Finding(
            check,
            Outcome.FAIL,
            f"{count} figure(s) absent from cited evidence: {'; '.join(offenders)}",
        )
    return Finding(check, Outcome.PASS, "every figure appears in its cited evidence")


def _untraced_figures(line: DraftedLine, sources: list[str]) -> list[tuple[list[str], str]]:
    """Return each sentence of ``line`` with the figures no single cited source states."""
    per_source = [
        {value for _, values in figures_in(source) for value in values} for source in sources
    ]
    found = []
    for sentence in sentence_pieces(line.text.strip()):
        raws = [
            raw
            for raw, values in figures_in(sentence)
            if not any(values & numbers for numbers in per_source)
        ]
        if raws:
            found.append((raws, sentence))
    return found


def structure(markdown: str) -> Finding:
    """Confirm the assembled Markdown still has the shape it is supposed to have.

    clearscout writes this document itself, so a fault here is a bug in the
    assembler rather than a content problem.
    """
    lines = markdown.splitlines()
    problems = []
    if not lines or not lines[0].startswith("# "):
        problems.append("no H1 name line")
    if sum(1 for line in lines if line.startswith("# ")) != 1:
        problems.append("more than one H1")
    if not any(line.startswith("## ") for line in lines):
        problems.append("no sections")
    if "@" not in "\n".join(lines[:6]):
        problems.append("no contact address near the top")
    if problems:
        return Finding("document structure", Outcome.FAIL, "; ".join(problems), redraftable=False)
    sections = sum(1 for line in lines if line.startswith("## "))
    roles = sum(1 for line in lines if line.startswith("### "))
    return Finding("document structure", Outcome.PASS, f"{sections} sections, {roles} roles")


def factual_review(
    lines: Sequence[DraftedLine],
    inventory: CvInventory,
    *,
    research: tuple[ResearchSource, ...] = (),
) -> Finding:
    """Flag bounded lexical risks without claiming citation membership proves meaning."""
    risks = []
    ignored = {
        "your",
        "their",
        "our",
        "team",
        "company",
        "organization",
        "with",
        "from",
        "this",
        "that",
        "have",
        "been",
        "were",
        "into",
    }
    for line in lines:
        source = " ".join(typed_source_text(inventory, line, research))
        candidate_source = " ".join(
            text
            for source_id, kind in zip(line.source_ids, line.source_types, strict=False)
            if kind is SourceType.CANDIDATE
            for text in source_text(inventory, (source_id,))
        )
        words = {word.lower() for word in WORD.findall(line.text) if len(word) > 3} - ignored
        source_words = {word.lower() for word in WORD.findall(source)} - ignored
        if source and words and not words.intersection(source_words):
            risks.append(f"possibly unrelated citation: {line.text[:80]}")
        if QUALIFIER.search(candidate_source) and not QUALIFIER.search(line.text):
            risks.append(f"review omitted ownership/qualification limit: {line.text[:80]}")
        credentials = {"phd", "doctorate", "certified", "bachelor", "master"}
        if LICENSED_CREDENTIAL.search(line.text):
            credentials.add("licensed")
        if (words & credentials) - source_words:
            risks.append(f"review unsupported credential: {line.text[:80]}")
    detail = (
        "; ".join(risks[:5])
        if risks
        else "Citation and number checks do not establish semantic support."
    )
    return Finding(
        "factual review",
        Outcome.WARN,
        detail + " Human factual review is required.",
        redraftable=False,
    )


def ownership_verbs(
    lines: Sequence[DraftedLine], inventory: CvInventory, *, first_person: bool = False
) -> Finding:
    """Warn on an ownership verb a line claims that none of its cited entries uses.

    A resume line claims its opening verb; with ``first_person``, a letter
    paragraph claims each verb that follows "I".
    """
    promoted = []
    for line in lines:
        verbs = _claimed_ownership_verbs(line.text, first_person=first_person)
        if not verbs:
            continue
        source = {
            word.casefold()
            for text in source_text(inventory, line.source_ids)
            for word in WORD.findall(text)
        }
        promoted += [f"{verb!r} in {line.text[:70]!r}" for verb in verbs if verb not in source]
    if promoted:
        return Finding(
            "ownership verbs",
            Outcome.WARN,
            f"{len(promoted)} claim(s) use an ownership verb their evidence never uses; "
            f"check the claim: {'; '.join(promoted[:5])}",
            redraftable=False,
        )
    return Finding("ownership verbs", Outcome.PASS, "every claimed ownership verb is in evidence")


def _claimed_ownership_verbs(text: str, *, first_person: bool) -> list[str]:
    """Return the ownership verbs a line claims, casefolded, in order."""
    if first_person:
        claimed = [match.group(1) for match in FIRST_PERSON_VERB.finditer(text)]
    else:
        opening = WORD.match(text.strip())
        claimed = [opening.group(0)] if opening else []
    return [verb.casefold() for verb in claimed if verb.casefold() in OWNERSHIP_VERBS]
