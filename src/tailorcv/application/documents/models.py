"""Typed document and tailoring-review read models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from tailorcv.domain import (
    ArtifactAuthorType,
    ArtifactIntegrityStatus,
    ArtifactLogicalType,
)
from tailorcv.shared.models import StrictModel

ApprovalDecisionValue = Literal["accept", "reject", "replace"]


class ArtifactResponse(StrictModel):
    """Safe file metadata without a filesystem path."""

    id: str
    workspace_id: str
    logical_type: ArtifactLogicalType
    mime_type: str
    byte_size: int
    sha256: str
    integrity_status: ArtifactIntegrityStatus
    last_verified_at: datetime | None
    created_at: datetime
    created_by: str
    provenance: dict[str, Any]
    related_entity_type: str | None
    related_entity_id: str | None
    tailoring_run_id: str | None
    artifact_family_id: str | None
    version_number: int | None
    supersedes_artifact_id: str | None
    author_type: ArtifactAuthorType
    archived_at: datetime | None
    inline_viewable: bool
    editable: bool


class CitationCheckResponse(StrictModel):
    """Evidence-backed lines changed by an unsaved edit."""

    artifact_id: str
    broken: list[dict[str, Any]]
    requires_override: bool


class ArtifactDiffResponse(StrictModel):
    """Unified diff between two verified text artifacts."""

    from_artifact_id: str
    to_artifact_id: str
    diff: str


class ProposalSourceExcerpt(StrictModel):
    """One cited source id and the master-CV text it resolves to."""

    source: str
    text: str


class ReviewProposalPayload(StrictModel):
    """Known digest-bound proposal metadata while preserving versioned extensions."""

    model_config = ConfigDict(extra="allow")

    kind: str | None = None
    immutable: bool | None = None
    rationale: str | None = None
    evidence: list[ProposalSourceExcerpt] = Field(default_factory=list)


class ReviewDecisionResponse(StrictModel):
    """Append-only human decision bound to one proposal digest."""

    id: str
    review_proposal_id: str
    decision: ApprovalDecisionValue
    replacement_text: str | None
    replacement_source_ids: list[str]
    manifest_sha256: str
    decided_at: datetime


class ReviewProposalResponse(StrictModel):
    """Evidence-linked text proposal and its ordered human decisions."""

    id: str
    proposal_key: str
    slot: str
    text: str
    source_ids: list[str]
    digest: str
    payload: ReviewProposalPayload
    decisions: list[ReviewDecisionResponse]


class TailoringCheckResponse(StrictModel):
    """One check as recorded when the application package was generated."""

    check: str
    outcome: Literal["pass", "warn", "fail"]
    detail: str
    advisories: list[str] = Field(
        default_factory=list, description="Notes for the reviewer that did not fail the check."
    )


class TailoringReviewResponse(StrictModel):
    """What one immutable tailoring run left for review, and what it finished without."""

    tailoring_run_id: str
    proposals: list[ReviewProposalResponse]
    checks: list[TailoringCheckResponse] = Field(default_factory=list)
