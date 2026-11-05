"""Group what the master CV could not answer across the whole ledger.

A requirement is unanswered when the stored fit report marks it unmet: the run
settled it and the record answers it with nothing. A requirement the run did not
settle is not a gap.

Two postings share a grouped gap when their stored requirement texts match under
one stated normalization rule. The rule is a text grouping, not a shared
identity.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import select

from clearscout.application.job_search._support import (
    _current_evaluation_id_expression,
    _requirement,
)
from clearscout.application.job_search.models import (
    ReportRequirementResponse,
    RequirementGapResponse,
    RequirementGapSourceResponse,
    RequirementGapSummaryResponse,
)
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.domain.requirements import is_boilerplate_requirement
from clearscout.domain.rulings import HIDING_RULINGS, Ruling
from clearscout.infrastructure.persistence.models import Employer, JobEvaluation, JobPosting

if TYPE_CHECKING:
    from clearscout.infrastructure.persistence.database import Database

MAX_GAPS = 100

# The exact rule two postings must share for their unanswered requirements to
# appear as one row. Stated on the response so the panel does not need to name
# it separately.
GROUPING_RULE = (
    "Requirements are grouped by their text after lowercasing and collapsing"
    " runs of whitespace to a single space. Grouping does not assert that two"
    " postings mean the same thing."
)

_WHITESPACE = re.compile(r"\s+")


def _normalized(text: str) -> str:
    """Return the grouping key one stored requirement's text falls under."""
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def _visible_gap(
    stored: object,
    importance_filter: frozenset[str] | None,
    rulings: dict[str, Ruling],
) -> tuple[ReportRequirementResponse, Ruling | None] | None:
    """Return an unanswered visible requirement and its workspace ruling."""
    requirement = _requirement(stored)
    if not requirement.unmet:
        return None
    if is_boilerplate_requirement(requirement.text):
        return None
    if importance_filter is not None and requirement.importance not in importance_filter:
        return None
    ruling = rulings.get(requirement.requirement_key)
    return None if ruling in HIDING_RULINGS else (requirement, ruling)


def _group_gaps(
    rows: Sequence[tuple[str, str, str, JobEvaluation]],
    importance_filter: frozenset[str] | None,
    rulings: dict[str, Ruling],
) -> dict[str, tuple[str, list[RequirementGapSourceResponse]]]:
    """Collect visible requirements under the stated text-normalization rule."""
    grouped: dict[str, tuple[str, list[RequirementGapSourceResponse]]] = {}
    for job_posting_id, title, employer, evaluation in rows:
        payload = evaluation.fit_report_payload or {}
        for stored in payload.get("requirements", []):
            visible = _visible_gap(stored, importance_filter, rulings)
            if visible is None:
                continue
            requirement, ruling = visible
            sources = grouped.setdefault(_normalized(requirement.text), (requirement.text, []))[1]
            sources.append(
                RequirementGapSourceResponse(
                    job_posting_id=job_posting_id,
                    employer=employer,
                    title=title,
                    importance=requirement.importance,
                    evaluated_at=evaluation.evaluated_at,
                    requirement_key=requirement.requirement_key,
                    ruling=ruling.value if ruling is not None else None,
                )
            )
    return grouped


def requirement_gaps(
    database: Database,
    workspace_id: str,
    *,
    min_raised: int = 1,
    importance: Sequence[str] | None = None,
) -> RequirementGapSummaryResponse:
    """Group the unanswered requirements of every posting's current evaluation.

    ``min_raised`` drops groups raised by fewer than that many postings.
    ``importance`` narrows the raised-by rows to those importance levels; a
    group left with no matching rows is dropped.
    """
    importance_filter = frozenset(importance) if importance else None
    rulings = RequirementRulingService(database).by_key(workspace_id)
    with database.session() as session:
        rows = session.execute(
            select(JobPosting.id, JobPosting.title, Employer.name, JobEvaluation)
            .join(Employer, Employer.id == JobPosting.employer_id)
            .join(JobEvaluation, JobEvaluation.job_posting_id == JobPosting.id)
            .where(
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
                JobEvaluation.id == _current_evaluation_id_expression(),
            )
        ).all()

        grouped = _group_gaps(rows, importance_filter, rulings)

    kept = [(text, sources) for text, sources in grouped.values() if len(sources) >= min_raised]
    ordered = sorted(kept, key=lambda item: (-len(item[1]), item[0]))
    return RequirementGapSummaryResponse(
        evaluations_read=len(rows),
        gap_count=len(ordered),
        omitted_count=max(len(ordered) - MAX_GAPS, 0),
        grouping_rule=GROUPING_RULE,
        gaps=[
            RequirementGapResponse(
                text=text,
                raised_by=sorted(sources, key=lambda source: (source.employer, source.title)),
            )
            for text, sources in ordered[:MAX_GAPS]
        ],
    )
