"""Shared ownership, parent-state, and audit persistence mechanics."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.infrastructure.persistence.models import (
    Application,
    AuditEvent,
    JobEvaluation,
    JobPosting,
    Workspace,
)


def require_workspace(session: Session, workspace_id: str) -> Workspace:
    """Return one active workspace or raise the shared not-found response."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
    return workspace


def require_owned_entity(
    session: Session, workspace_id: str, entity_type: str, entity_id: str
) -> Any:
    """Return one active supported entity owned by the workspace."""
    entity: Any
    if entity_type == "job_posting":
        entity = session.scalar(
            select(JobPosting).where(
                JobPosting.id == entity_id,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        )
    elif entity_type == "application":
        entity = session.scalar(
            select(Application)
            .join(JobPosting, JobPosting.id == Application.job_posting_id)
            .where(
                Application.id == entity_id,
                Application.workspace_id == workspace_id,
                Application.archived_at.is_(None),
                JobPosting.archived_at.is_(None),
            )
        )
    elif entity_type == "evaluation":
        entity = session.scalar(
            select(JobEvaluation)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                JobEvaluation.id == entity_id,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        )
    else:
        raise ValidationError(f"Unsupported entity type {entity_type!r}.")
    if entity is None:
        raise NotFoundError(f"{entity_type} {entity_id!r} does not exist in this workspace.")
    return entity


def active_parent_clauses(
    entity_type_column: Any,
    entity_id_column: Any,
    workspace_id: str,
    entity_types: tuple[str, ...],
) -> tuple[Any, ...]:
    """Return ownership clauses for the requested supported parent types."""
    active_parents = {
        "job_posting": exists(
            select(1).where(
                JobPosting.id == entity_id_column,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        ),
        "application": exists(
            select(1)
            .select_from(Application)
            .join(JobPosting, JobPosting.id == Application.job_posting_id)
            .where(
                Application.id == entity_id_column,
                Application.workspace_id == workspace_id,
                Application.archived_at.is_(None),
                JobPosting.archived_at.is_(None),
            )
        ),
        "evaluation": exists(
            select(1)
            .select_from(JobEvaluation)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                JobEvaluation.id == entity_id_column,
                JobPosting.workspace_id == workspace_id,
                JobPosting.archived_at.is_(None),
            )
        ),
    }
    try:
        return tuple(
            and_(entity_type_column == entity_type, active_parents[entity_type])
            for entity_type in entity_types
        )
    except KeyError as error:
        raise ValueError(f"Unsupported parent entity type {error.args[0]!r}.") from error


def audit_columns(actor: str, origin: str, correlation_id: str | None) -> dict[str, Any]:
    """Return consistent write provenance for one use case."""
    return {"created_by": actor, "origin": origin, "correlation_id": correlation_id}


def add_audit_event(
    session: Session,
    *,
    workspace_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any],
    origin: str,
    correlation_id: str | None,
) -> None:
    """Stage one audit event with caller-owned action details.

    ``origin`` says what performed the action and is required, so a trail cannot
    silently record the column default for a write whose caller knew better.
    """
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            origin=origin,
            correlation_id=correlation_id,
        )
    )
