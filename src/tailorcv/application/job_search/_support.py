"""Shared projections and validation for job-search services."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.job_search.models import (
    PostingRequirementResponse,
    ReportRequirementResponse,
)
from tailorcv.infrastructure.persistence.models import (
    ApplicationEvent,
    Artifact,
    CompensationComponent,
    EvaluationScore,
    EvaluationScoreEvidence,
    JobEvaluation,
    JobPosting,
    PostingObservation,
    Workspace,
)


def _current_evaluation_id_expression() -> Any:
    """Select the newest immutable evaluation for the correlated posting."""
    candidate = aliased(JobEvaluation)
    return (
        select(candidate.id)
        .where(candidate.job_posting_id == JobPosting.id)
        .order_by(candidate.version_number.desc(), candidate.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )


def _readable_artifact(
    artifacts: EvidenceArtifactService,
    session: Session,
    artifact_id: str,
    workspace_id: str,
    *,
    missing: str,
    unreadable: str,
) -> tuple[Artifact, str]:
    """Return an artifact this workspace owns, and its text.

    The ownership check is repeated against the artifact rather than trusted
    from the row that pointed at it, so a stored identifier crossing workspaces
    reads as missing instead of as content. Callers supply both messages,
    because what was unreadable is the useful half of the answer.
    """
    artifact = session.get(Artifact, artifact_id)
    workspace = session.get(Workspace, workspace_id)
    if (
        artifact is None
        or workspace is None
        or workspace.archived_at is not None
        or artifact.workspace_id != workspace_id
    ):
        raise NotFoundError(missing)
    try:
        return artifact, artifacts.read(artifact, workspace).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(unreadable) from exc


def _requirement(stored: object) -> ReportRequirementResponse:
    """Validate one requirement from a persisted fit-report payload."""
    try:
        return ReportRequirementResponse.model_validate(stored)
    except PydanticValidationError as error:
        raise ValidationError("A stored fit-report requirement has an invalid shape.") from error


def _posting_requirement(stored: object) -> PostingRequirementResponse:
    """Validate one persisted posting requirement."""
    try:
        return PostingRequirementResponse.model_validate(stored)
    except PydanticValidationError as error:
        raise ValidationError("A persisted posting requirement has an invalid shape.") from error


def _requirements(stored: object) -> list[ReportRequirementResponse]:
    """Validate the complete requirement list from a persisted fit report."""
    if not isinstance(stored, list):
        raise ValidationError("Stored fit-report requirements must be a list.")
    return [_requirement(item) for item in stored]


def _decimal_float(value: Decimal | None) -> float | None:
    """Return an API-safe decimal without obscuring a missing product as zero."""
    return float(value) if value is not None else None


def _compensation_component(component: CompensationComponent) -> dict[str, Any]:
    """Project one persisted compensation component."""
    return {
        "component_type": component.component_type,
        "amount_min": _decimal_float(component.amount_min),
        "amount_max": _decimal_float(component.amount_max),
        "annualized_min": _decimal_float(component.annualized_min),
        "annualized_max": _decimal_float(component.annualized_max),
        "currency": component.currency,
        "basis": component.basis,
        "geography": component.geography,
        "description": component.description,
    }


def _assert_score_traceability(
    scores: Sequence[EvaluationScore], links: Sequence[EvaluationScoreEvidence]
) -> None:
    """Reject every stated or conflicting component that lacks an evidence relation."""
    linked = {item.evaluation_score_id for item in links}
    unsupported = [
        item.component
        for item in scores
        if (item.score is not None or item.finding_state in ("conflict", "supported"))
        and item.id not in linked
    ]
    if unsupported:
        raise ValidationError(
            "Every stated or conflicting score requires linked evidence; missing: "
            + ", ".join(sorted(unsupported))
        )


def _application_event(event: ApplicationEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "application_id": event.application_id,
        "status": event.status,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "created_at": event.created_at,
        "notes": event.notes,
        "artifact_uri": event.artifact_uri,
        "corrects_event_id": event.corrects_event_id,
        "reverses_event_id": event.reverses_event_id,
    }


def _observation(observation: PostingObservation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "observed_at": observation.observed_at,
        "posting_status": observation.posting_status,
        "location": observation.location,
        "residence_rule": observation.residence_rule,
        "employment_type": observation.employment_type,
        "payroll_arrangement": observation.payroll_arrangement,
        "body_artifact_id": observation.body_artifact_id,
        "extraction_method": observation.extraction_method,
        "fetched_at": observation.fetched_at,
        "http_status": observation.http_status,
        "robots_basis": observation.robots_basis,
        "publisher_closing_date": observation.publisher_closing_date,
    }
