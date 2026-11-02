"""Render the review as the machine-readable record and the readable one."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tailorcv.application.tailoring.changes.record import (
    ACCEPT,
    INPUT_NAMES,
    REVIEW,
    SCHEMA,
    Excerpt,
    Kind,
    Proposal,
    Review,
)
from tailorcv.application.tailoring.stages.draft import CoverLetter
from tailorcv.shared.results import Finding, Outcome

QUOTED = 50


@dataclass(frozen=True)
class _RenderedReview:
    """Exact review texts and the structured facts they encode."""

    json_text: str
    markdown_text: str
    payload: dict[str, Any]


def render(
    review_result: Review,
    decisions: dict[str, str] | None = None,
    *,
    findings: Sequence[Finding] = (),
    letter: CoverLetter | None = None,
) -> _RenderedReview:
    """Render both review artifacts without choosing where they are stored.

    An undecided line a non-passing finding names defaults to ``review``.
    """
    flagged = _flagged(review_result, findings)
    chosen = {
        proposal.id: (decisions or {}).get(
            proposal.id, REVIEW if proposal.id in flagged else ACCEPT
        )
        for proposal in review_result.proposals
    }
    payload = review_result.payload()
    for proposal in payload["proposals"]:
        proposal["decision"] = chosen[str(proposal["id"])]
    if letter is not None:
        payload["letter"] = {"paragraphs": _letter_citations(letter)}
    return _RenderedReview(
        json_text=json.dumps(payload, indent=2) + "\n",
        markdown_text=markdown(review_result, chosen),
        payload=payload,
    )


def _flagged(review_result: Review, findings: Sequence[Finding]) -> frozenset[str]:
    """Return the decidable proposals whose text a non-passing finding quotes."""
    details = [item.detail for item in findings if item.outcome is not Outcome.PASS]
    return frozenset(
        proposal.id
        for proposal in review_result.proposals
        if proposal.text.strip()
        and not proposal.immutable
        and proposal.kind is not Kind.OMITTED
        # Checks quote a drafted line by its first 60 to 80 characters.
        and any(proposal.text[:QUOTED] in detail for detail in details)
    )


def _letter_citations(letter: CoverLetter) -> list[dict[str, Any]]:
    """Return each letter paragraph's cited identifiers, numbered from one."""
    return [
        {
            "paragraph": number,
            "source_ids": list(paragraph.source_ids),
            "source_types": [kind.value for kind in paragraph.source_types],
        }
        for number, paragraph in enumerate(letter.paragraphs, 1)
    ]


def markdown(review_result: Review, decisions: dict[str, str] | None = None) -> str:
    """Render the review for a person, proposal by proposal."""
    counts: dict[str, int] = {}
    for proposal in review_result.proposals:
        counts[proposal.kind.value] = counts.get(proposal.kind.value, 0) + 1
    tally = ", ".join(f"{value} {key}" for key, value in sorted(counts.items())) or "none"
    lines = [
        "# Proposed changes",
        "",
        f"{len(review_result.proposals)} proposal(s): {tally}.",
        "",
        "Every line the run would put on the page, the master CV text behind it, and",
        "the requirements it answers. Lines marked immutable are copied from the CV;",
        "editing them is a job for the CV, not for a review.",
        "",
        f"- Review digest: `{review_result.digest}`",
        f"- Schema: {SCHEMA}",
    ]
    lines += [f"- {INPUT_NAMES[key]}: `{value}`" for key, value in review_result.run.items()]
    lines += [
        "",
        "To accept or reject individual lines, copy `changes.json`, set `decision` to",
        '`"reject"` on the lines you refuse, and re-run the same posting with',
        "`--approve <that file>`. Replacement text goes in a `replacement` object on a",
        "rejected line and is held to the same evidence checks the model's text is.",
        'A line marked `"review"` was named by a check; set it to accept or reject.',
    ]
    for proposal in review_result.proposals:
        lines += _proposal_markdown(proposal, (decisions or {}).get(proposal.id, ACCEPT))
    return "\n".join(lines).rstrip() + "\n"


def _proposal_markdown(proposal: Proposal, decision: str) -> list[str]:
    """Render one proposal as a readable block."""
    marks = [proposal.kind.value]
    if proposal.immutable:
        marks.append("immutable")
    if decision != ACCEPT:
        marks.append(decision)
    lines = [
        "",
        f"## {proposal.id}  {proposal.slot}",
        "",
        f"- Kind: {', '.join(marks)}",
        f"- Evidence: {', '.join(proposal.evidence_ids) or 'none'}",
        f"- Requirements: {', '.join(proposal.requirement_ids) or 'none'}",
        f"- Digest: `{proposal.digest}`",
        "",
        f"> {proposal.text}" if proposal.text else "> (nothing reaches the page)",
        "",
        proposal.rationale,
    ]
    for excerpt in proposal.evidence:
        lines += ["", f"Source `{excerpt.source}`:", "", *[f"    {row}" for row in _rows(excerpt)]]
    return lines


def _rows(excerpt: Excerpt) -> list[str]:
    """Return an excerpt's lines, indented as a literal block."""
    return excerpt.text.splitlines() or [""]
