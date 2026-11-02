"""Materialized set of requirement texts a workspace has been noted as missing.

Written by a post-commit hook after every evaluation lands, read by the ranker
before it scores a pending discovery. Rebuilds from
``job_evaluations.fit_report_payload``; a workspace never edits it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.models import ReportRequirementResponse
from tailorcv.domain.requirements import is_boilerplate_requirement
from tailorcv.domain.rulings import HIDING_RULINGS, Ruling
from tailorcv.infrastructure.persistence.models import (
    MissedRequirementText,
    RequirementRuling,
)


def _requirement(stored: object) -> ReportRequirementResponse:
    """Validate one requirement from a persisted fit-report payload."""
    try:
        return ReportRequirementResponse.model_validate(stored)
    except PydanticValidationError as error:
        raise ValidationError("A stored fit-report requirement has an invalid shape.") from error


if TYPE_CHECKING:
    from tailorcv.infrastructure.persistence.database import Database

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Return the grouping key one requirement text falls under."""
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def _rulings_by_key(session: Session, workspace_id: str) -> dict[str, Ruling]:
    rows = session.scalars(
        select(RequirementRuling).where(RequirementRuling.workspace_id == workspace_id)
    ).all()
    return {row.requirement_key: Ruling(row.ruling) for row in rows}


def _missed_texts_from_payload(
    payload: dict[str, Any] | None, rulings: dict[str, Ruling]
) -> set[str]:
    """Return normalized texts the payload marks as unanswered and not hidden."""
    if not payload:
        return set()
    texts: set[str] = set()
    for stored in payload.get("requirements", []):
        requirement = _requirement(stored)
        if requirement.evidence_keys or not requirement.established:
            continue
        if is_boilerplate_requirement(requirement.text):
            continue
        ruling = rulings.get(requirement.requirement_key)
        if ruling in HIDING_RULINGS:
            continue
        texts.add(normalize(requirement.text))
    return texts


def refresh_for_evaluation(
    session: Session,
    *,
    workspace_id: str,
    master_cv_version_id: str,
    evaluation_id: str,
    payload: dict[str, Any] | None,
    origin: str = "evaluation",
    correlation_id: str | None = None,
    actor: str = "worker",
) -> None:
    """Replace the derived rows for one evaluation with its current missed set."""
    session.execute(
        delete(MissedRequirementText).where(
            MissedRequirementText.source_evaluation_id == evaluation_id
        )
    )
    rulings = _rulings_by_key(session, workspace_id)
    for text in _missed_texts_from_payload(payload, rulings):
        session.add(
            MissedRequirementText(
                workspace_id=workspace_id,
                master_cv_version_id=master_cv_version_id,
                source_evaluation_id=evaluation_id,
                requirement_text=text,
                created_by=actor,
                origin=origin,
                correlation_id=correlation_id,
            )
        )


def missed_requirement_texts_in(
    session: Session, workspace_id: str, master_cv_version_id: str
) -> frozenset[str]:
    """Return the distinct missed texts within a caller's transaction."""
    rows = session.scalars(
        select(MissedRequirementText.requirement_text)
        .where(
            MissedRequirementText.workspace_id == workspace_id,
            MissedRequirementText.master_cv_version_id == master_cv_version_id,
        )
        .distinct()
    ).all()
    return frozenset(rows)


def missed_requirement_texts(
    database: Database, workspace_id: str, master_cv_version_id: str
) -> frozenset[str]:
    """Return the distinct missed texts a ranker should compare a title against."""
    with database.session() as session:
        return missed_requirement_texts_in(session, workspace_id, master_cv_version_id)
