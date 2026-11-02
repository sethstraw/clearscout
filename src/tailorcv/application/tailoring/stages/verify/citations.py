"""Resolve the ids a drafted line cites, and hold each line to citing known evidence."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.stages.context import entry_line
from tailorcv.application.tailoring.stages.draft import DraftedLine, SourceType
from tailorcv.application.tailoring.stages.jobspec import JobSpec
from tailorcv.application.tailoring.stages.select import Plan
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource
from tailorcv.shared.results import Finding, Outcome

# A quoted entry points at a line the person can see, so a few words identify it.
POINTER_WORDS = 8


def id_texts(
    spec: JobSpec, inventory: CvInventory, research: Sequence[ResearchSource] = ()
) -> dict[str, str]:
    """Return every id a run could cite, with the text it stands for."""
    return {
        **{entry.id: entry.full_text for entry in inventory.entries},
        **{source.id: source.text for source in research},
        **{requirement.id: requirement.text for requirement in spec.requirements},
    }


def worded(text: str, known: Mapping[str, str]) -> str:
    """Return critic prose with each known id, bracketed or bare, replaced by its quoted text."""
    if not known:
        return text
    return _id_pattern(known).sub(lambda found: f'"{_pointer(known[found.group(1)])}"', text)


def marked_uncitable(text: str, uncitable: Collection[str]) -> str:
    """Return critic prose with each id this document's writer cannot cite marked as such."""
    if not uncitable:
        return text
    return _id_pattern(uncitable).sub(
        lambda found: f"{found.group(0)} (not citable in this document)", text
    )


def _id_pattern(ids: Collection[str]) -> re.Pattern[str]:
    """Match any of these ids, bracketed or bare, never inside a longer identifier."""
    alternatives = "|".join(re.escape(key) for key in sorted(ids, key=len, reverse=True))
    return re.compile(rf"\[?(?<![\w-])({alternatives})(?![\w-])\]?")


def _pointer(text: str) -> str:
    """Return the opening words of an entry, enough to recognise it inside a sentence."""
    words = text.split()
    return text if len(words) <= POINTER_WORDS else " ".join(words[:POINTER_WORDS]) + " ..."


def uncitable_ids(inventory: CvInventory, offered: Collection[str]) -> frozenset[str]:
    """Return the record's ids a document's writer was never offered to cite."""
    return frozenset(entry.id for entry in inventory.entries if entry.id not in offered)


def line_citations(lines: Sequence[DraftedLine], inventory: CvInventory, unit: str = "Line") -> str:
    """Return each written line under its identifiers and the record text of each entry it cites.

    A company research identifier resolves to no entry, so nothing is shown beneath it.
    """
    return "\n\n".join(
        "\n".join(
            [
                f"{unit} {number}, source_ids: {', '.join(line.source_ids) or 'none'}",
                line.text.strip(),
                *(
                    entry_line(entry)
                    for source_id in dict.fromkeys(line.source_ids)
                    if (entry := inventory.entry(source_id))
                ),
            ]
        )
        for number, line in enumerate(lines, 1)
    )


def _uncited_lines(lines: Sequence[DraftedLine]) -> list[str]:
    """Return drafted lines that cite no source IDs."""
    return [line.text for line in lines if not line.source_ids]


def _unknown_source_ids(lines: Sequence[DraftedLine], allowed: set[str]) -> list[str]:
    """Return cited IDs absent from the run's allowed vocabulary."""
    return sorted(
        {entry_id for line in lines for entry_id in line.source_ids if entry_id not in allowed}
    )


def quoted(texts: Sequence[str]) -> str:
    """Quote every offending text in full, so a redraft sees each one it must fix."""
    return "; ".join(repr(text.strip()) for text in texts)


def cited(
    lines: Sequence[DraftedLine],
    plan: Plan,
    check: str,
    *,
    additional_ids: tuple[str, ...] = (),
    allow_company: bool = False,
) -> Finding:
    """Require every line to cite evidence from a known and typed source."""
    allowed = set(plan.all_ids()) | set(additional_ids)
    uncited = _uncited_lines(lines)
    if uncited:
        return Finding(
            check, Outcome.FAIL, f"{len(uncited)} line(s) cite no evidence: {quoted(uncited)}"
        )
    unknown = _unknown_source_ids(lines, allowed)
    if unknown:
        return Finding(check, Outcome.FAIL, f"cited unknown evidence: {', '.join(unknown)}")
    if not allow_company and any(
        source_type is SourceType.COMPANY for line in lines for source_type in line.source_types
    ):
        return Finding(check, Outcome.FAIL, "candidate-only provenance is required")
    mixed = [
        line.text
        for line in lines
        if SourceType.CANDIDATE in line.source_types and SourceType.COMPANY in line.source_types
    ]
    if mixed:
        return Finding(
            check,
            Outcome.FAIL,
            f"{len(mixed)} line(s) mix candidate and company claims: {quoted(mixed)}",
        )
    return Finding(check, Outcome.PASS, f"{len(lines)} lines all cite evidence")


def typed_source_text(
    inventory: CvInventory, line: DraftedLine, research: tuple[ResearchSource, ...]
) -> list[str]:
    """Resolve each citation through its declared source owner."""
    company = {source.id: source.text for source in research}
    return [
        text
        for source_id, kind in zip(line.source_ids, line.source_types, strict=False)
        for text in (
            source_text(inventory, (source_id,))
            if kind is SourceType.CANDIDATE
            else ([company[source_id]] if source_id in company else [])
        )
    ]


def source_text(inventory: CvInventory, ids: tuple[str, ...]) -> list[str]:
    """Return the full text of the cited entries."""
    return [entry.full_text for entry_id in ids if (entry := inventory.entry(entry_id))]
