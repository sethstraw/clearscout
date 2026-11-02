"""Workspace overrides on the analysis's judgment for one requirement or discovery."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from tailorcv.application.discovery.relevance_disputes import RelevanceDisputeService
from tailorcv.application.job_search.postings import RequirementRulingService
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import CorrelationId, DatabaseDep
from tailorcv.domain.rulings import Ruling
from tailorcv.shared.models import StrictModel

router = APIRouter()


class RequirementRulingBody(StrictModel):
    """Payload recording a ruling on one requirement; ``reversed`` is machine-set only."""

    ruling: Literal["met", "not_applicable", "disputed_fit", "disputed_extraction"]
    note: str | None = Field(default=None, max_length=1000)


class RequirementRulingResponse(StrictModel):
    """One recorded ruling and the requirement text last stored under its key."""

    requirement_key: str
    ruling: Literal["met", "not_applicable", "disputed_fit", "disputed_extraction", "reversed"]
    note: str | None
    updated_at: datetime
    requirement_text: str


class RequirementRulingsResponse(StrictModel):
    """Every ruling the workspace has recorded."""

    rulings: list[RequirementRulingResponse]


class RelevanceDisputeBody(StrictModel):
    """Payload setting aside one relevance verdict by the review still in force for it."""

    relevance_review_id: str
    note: str | None = Field(default=None, max_length=1000)


class RelevanceDisputeResponse(StrictModel):
    """The dispute now standing against one discovery's relevance verdict."""

    pending_discovery_id: str
    relevance_review_id: str
    disposition: Literal["set_aside"]
    note: str | None
    updated_at: datetime


class RelevanceDisputeClearedResponse(StrictModel):
    """Whether withdrawing a dispute removed one that was standing."""

    pending_discovery_id: str
    cleared: bool


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/relevance-disputes/{{pending_discovery_id}}",
    response_model=RelevanceDisputeResponse,
    status_code=200,
    tags=["workspaces"],
    summary="Dispute the relevance verdict recorded for one discovery",
)
async def set_relevance_dispute(
    correlation_id: CorrelationId,
    workspace_id: str,
    pending_discovery_id: str,
    body: RelevanceDisputeBody,
    database: DatabaseDep,
) -> RelevanceDisputeResponse:
    """Set aside the model's verdict on one discovery and return it to triage."""

    def _apply() -> RelevanceDisputeResponse:
        row = RelevanceDisputeService(database).dispute(
            workspace_id=workspace_id,
            pending_discovery_id=pending_discovery_id,
            relevance_review_id=body.relevance_review_id,
            note=body.note,
            audit=audit_columns("local_user", "api", correlation_id),
        )
        return RelevanceDisputeResponse(
            pending_discovery_id=row.pending_discovery_id,
            relevance_review_id=row.relevance_review_id,
            disposition=row.disposition,  # type: ignore[arg-type]
            note=row.note,
            updated_at=row.updated_at,
        )

    return await run_in_threadpool(_apply)


@router.delete(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/relevance-disputes/{{pending_discovery_id}}",
    response_model=RelevanceDisputeClearedResponse,
    status_code=200,
    tags=["workspaces"],
    summary="Withdraw a dispute of one discovery's relevance verdict",
)
async def clear_relevance_dispute(
    correlation_id: CorrelationId,
    workspace_id: str,
    pending_discovery_id: str,
    database: DatabaseDep,
) -> RelevanceDisputeClearedResponse:
    """Restore the model's verdict to the queue for one discovery."""

    def _apply() -> RelevanceDisputeClearedResponse:
        cleared = RelevanceDisputeService(database).clear(
            workspace_id=workspace_id,
            pending_discovery_id=pending_discovery_id,
            audit=audit_columns("local_user", "api", correlation_id),
        )
        return RelevanceDisputeClearedResponse(
            pending_discovery_id=pending_discovery_id, cleared=cleared
        )

    return await run_in_threadpool(_apply)


@router.put(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/requirement-rulings/{{requirement_key}}",
    response_model=RequirementRulingResponse,
    status_code=200,
    tags=["workspaces"],
    summary="Record or replace a workspace ruling on one posting requirement",
)
async def set_requirement_ruling(
    correlation_id: CorrelationId,
    workspace_id: str,
    requirement_key: str,
    body: RequirementRulingBody,
    database: DatabaseDep,
) -> RequirementRulingResponse:
    """Upsert the workspace's judgment on one requirement identified by key."""

    def _apply() -> RequirementRulingResponse:
        service = RequirementRulingService(database)
        row = service.set(
            workspace_id=workspace_id,
            requirement_key=requirement_key,
            ruling=Ruling(body.ruling),
            note=body.note,
            audit=audit_columns("local_user", "api", correlation_id),
        )
        listed = next(
            (
                item
                for item in service.list(workspace_id)
                if item.requirement_key == requirement_key
            ),
            None,
        )
        return RequirementRulingResponse(
            requirement_key=row.requirement_key,
            ruling=row.ruling,  # type: ignore[arg-type]
            note=row.note,
            updated_at=row.updated_at,
            requirement_text=listed.requirement_text if listed is not None else "",
        )

    return await run_in_threadpool(_apply)


@router.delete(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/requirement-rulings/{{requirement_key}}",
    status_code=204,
    tags=["workspaces"],
    summary="Clear a workspace ruling on one posting requirement",
)
async def clear_requirement_ruling(
    correlation_id: CorrelationId,
    workspace_id: str,
    requirement_key: str,
    database: DatabaseDep,
) -> None:
    """Remove the workspace's ruling for one requirement identified by key."""

    def _apply() -> None:
        RequirementRulingService(database).clear(
            workspace_id=workspace_id,
            requirement_key=requirement_key,
            audit=audit_columns("local_user", "api", correlation_id),
        )

    await run_in_threadpool(_apply)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/requirement-rulings",
    response_model=RequirementRulingsResponse,
    tags=["workspaces"],
    summary="List the workspace's rulings on posting requirements",
)
async def list_requirement_rulings(
    workspace_id: str,
    database: DatabaseDep,
) -> RequirementRulingsResponse:
    """Return every ruling in the workspace, denormalized with the requirement text."""

    def _apply() -> RequirementRulingsResponse:
        rows = RequirementRulingService(database).list(workspace_id)
        return RequirementRulingsResponse(
            rulings=[
                RequirementRulingResponse(
                    requirement_key=row.requirement_key,
                    ruling=row.ruling.value,
                    note=row.note,
                    updated_at=row.updated_at,
                    requirement_text=row.requirement_text,
                )
                for row in rows
            ]
        )

    return await run_in_threadpool(_apply)
