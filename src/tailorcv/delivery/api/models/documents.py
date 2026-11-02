"""Artifact and tailoring-review API contracts."""

from typing import Literal

from pydantic import Field

from tailorcv.application.documents.models import (
    ApprovalDecisionValue,
    ArtifactDiffResponse,
    ArtifactResponse,
    CitationCheckResponse,
    ReviewDecisionResponse,
    ReviewProposalPayload,
    ReviewProposalResponse,
    TailoringReviewResponse,
)
from tailorcv.domain import ArtifactLogicalType
from tailorcv.shared.models import StrictModel


class DocumentSave(StrictModel):
    """Explicit editor save request that creates a new artifact version."""

    workspace_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    author_type: Literal["human", "model"] = "human"
    override_reason: str = ""


class CitationCheck(StrictModel):
    """Unsaved editor content checked against stored line citations."""

    workspace_id: str = Field(min_length=1)
    content: str


class ArtifactRelabel(StrictModel):
    """Mutable logical label for immutable file bytes."""

    workspace_id: str = Field(min_length=1)
    logical_type: ArtifactLogicalType


class ArtifactRevert(StrictModel):
    """Append the selected older bytes as a new current version."""

    workspace_id: str = Field(min_length=1)
    target_artifact_id: str = Field(min_length=1)


class ApprovalDecisionBody(StrictModel):
    """Human decision bound to the exact reviewed proposal digest."""

    workspace_id: str = Field(min_length=1)
    proposal_digest: str = Field(min_length=1)
    decision: ApprovalDecisionValue
    replacement_text: str | None = None
    replacement_source_ids: list[str] = Field(default_factory=list)


__all__ = [
    "ApprovalDecisionBody",
    "ArtifactDiffResponse",
    "ArtifactRelabel",
    "ArtifactResponse",
    "ArtifactRevert",
    "CitationCheck",
    "CitationCheckResponse",
    "DocumentSave",
    "ReviewDecisionResponse",
    "ReviewProposalPayload",
    "ReviewProposalResponse",
    "TailoringReviewResponse",
]
