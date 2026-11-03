"""Validated payloads accepted by production background tasks."""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from clearscout.application.tasks.models import PostingRecoveryApprovalManifest


class TaskPayload(BaseModel):
    """Strict base for payloads accepted through the production registry."""

    model_config = ConfigDict(extra="forbid")


class ArtifactVerifyPayload(TaskPayload):
    """Verify one indexed artifact."""

    artifact_id: str = Field(min_length=1)


class PostingRefreshPayload(TaskPayload):
    """Refresh one durable posting by stable database identity."""

    job_posting_id: str = Field(min_length=1)


class PostingRecoverBatchPayload(TaskPayload):
    """Run one paid assisted recovery batch against its approval manifest."""

    approval: PostingRecoveryApprovalManifest


class PostingRefreshFilter(TaskPayload):
    """Bounded saved-filter substitute for a batch refresh."""

    decisions: list[str] = Field(default_factory=list, max_length=20)
    application_statuses: list[str] = Field(default_factory=list, max_length=20)
    completeness: str = ""


class PostingRefreshBatchPayload(TaskPayload):
    """Select an explicit, saved-view, or typed-filter batch."""

    job_posting_ids: list[str] = Field(default_factory=list, max_length=250)
    saved_view_id: str | None = None
    filters: PostingRefreshFilter | None = None
    force: bool = False

    @model_validator(mode="after")
    def one_selector(self) -> PostingRefreshBatchPayload:
        """Refuse ambiguous or unbounded batch selection."""
        selectors = sum(
            [
                bool(self.job_posting_ids),
                bool(self.saved_view_id),
                self.filters is not None,
            ]
        )
        if selectors != 1:
            raise ValueError(
                "Batch refresh requires exactly one of job_posting_ids, saved_view_id, or filters."
            )
        if len(set(self.job_posting_ids)) != len(self.job_posting_ids):
            raise ValueError("Batch refresh job_posting_ids must be unique.")
        return self


class WorkflowJobPayload(TaskPayload):
    """One stored posting selected for an existing single-job workflow."""

    job_posting_id: str = Field(min_length=1)


class EvaluationWorkflowPayload(TaskPayload):
    """One pending discovery or stored posting selected for evaluation."""

    pending_discovery_id: str | None = Field(default=None, min_length=1)
    job_posting_id: str | None = Field(default=None, min_length=1)
    # A person's re-run asks the provider again; its answers still refresh the cache.
    fresh: bool = False

    @model_validator(mode="after")
    def one_target(self) -> EvaluationWorkflowPayload:
        """Require exactly one discovery or rerun target."""
        if (self.pending_discovery_id is None) == (self.job_posting_id is None):
            raise ValueError("Evaluation requires exactly one target.")
        return self


class DiscoveryEvaluateBatchPayload(TaskPayload):
    """Evaluate a selection of pending discoveries or tracked postings in one approved task."""

    pending_discovery_ids: list[str] = Field(default_factory=list, max_length=250)
    job_posting_ids: list[str] = Field(default_factory=list, max_length=250)

    @model_validator(mode="after")
    def one_target_kind(self) -> DiscoveryEvaluateBatchPayload:
        """Require exactly one non-empty target kind and refuse duplicate work in one sweep."""
        if bool(self.pending_discovery_ids) == bool(self.job_posting_ids):
            raise ValueError("Evaluate-batch requires exactly one target kind.")
        ids = self.pending_discovery_ids or self.job_posting_ids
        if len(set(ids)) != len(ids):
            raise ValueError("Evaluate-batch target ids must be unique.")
        return self


class DiscoveryRefreshBatchPayload(TaskPayload):
    """Refresh publisher facts for a whole selection of pending discoveries."""

    pending_discovery_ids: list[str] = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def unique_discoveries(self) -> DiscoveryRefreshBatchPayload:
        """Refuse duplicate work inside one refresh sweep."""
        if len(set(self.pending_discovery_ids)) != len(self.pending_discovery_ids):
            raise ValueError("Refresh-batch pending_discovery_ids must be unique.")
        return self


class DiscoveryRescreenQueuePayload(TaskPayload):
    """Re-screen the whole active pending queue; takes no additional fields."""


class TailoringPayload(WorkflowJobPayload):
    """One full package request through the existing tailoring pipeline."""

    cover_letter: bool = True
    regenerate: bool = False
    approval_tailoring_run_id: str | None = None


class ApprovalWorkflowPayload(WorkflowJobPayload):
    """Generate a new package from one completely decided digest-bound review."""

    approval_tailoring_run_id: str = Field(min_length=1)
    cover_letter: bool = True
    regenerate: bool = False


class BatchWorkflowPayload(TaskPayload):
    """Two to five postings screened separately before deterministic comparison."""

    job_posting_ids: list[str] = Field(min_length=2, max_length=5)
    priorities: list[int] = Field(default_factory=list, max_length=5)
    notes: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def aligned(self) -> BatchWorkflowPayload:
        """Require unique jobs and optional per-job metadata with matching length."""
        if len(set(self.job_posting_ids)) != len(self.job_posting_ids):
            raise ValueError("Batch workflow job_posting_ids must be unique.")
        if self.priorities and len(self.priorities) != len(self.job_posting_ids):
            raise ValueError("Batch priorities must match job_posting_ids in length.")
        if self.notes and len(self.notes) != len(self.job_posting_ids):
            raise ValueError("Batch notes must match job_posting_ids in length.")
        if not self.priorities:
            self.priorities = [0] * len(self.job_posting_ids)
        if not self.notes:
            self.notes = [""] * len(self.job_posting_ids)
        return self


class DiscoveryProposalPayload(TaskPayload):
    """Person-authored wording proposed for confirmed discovery."""

    text: str = Field(min_length=1)
    scope: str = ""
    role_key: str = ""
    section: str = ""
    ambiguities: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def one_placement(self) -> DiscoveryProposalPayload:
        """Place confirmed material under exactly one known role or section."""
        if bool(self.role_key) == bool(self.section):
            raise ValueError("Discovery proposal requires exactly one of role_key or section.")
        return self


class DiscoveryWorkflowPayload(WorkflowJobPayload):
    """One replayable step of person-only evidence-gap questioning."""

    subject: str = ""
    replies: list[str] = Field(default_factory=list, max_length=20)
    proposal: DiscoveryProposalPayload | None = None
    confirmed_points: list[str] = Field(default_factory=list, max_length=8)
    write_confirmed: bool = False

    @model_validator(mode="after")
    def confirmation_shape(self) -> DiscoveryWorkflowPayload:
        """Never write discovery without a proposal and explicit confirmations."""
        if self.confirmed_points and self.proposal is None:
            raise ValueError("Discovery confirmations require a proposal.")
        if self.write_confirmed and (self.proposal is None or not self.confirmed_points):
            raise ValueError("Writing discovery requires a proposal and confirmed_points.")
        return self


class DocumentRenderPayload(TaskPayload):
    """Render one indexed Markdown artifact through pressume."""

    artifact_id: str = Field(min_length=1)
    job_posting_id: str | None = None


# An empty source_ids sweeps every enabled source; a named selection reads only those.
class SourceScanPayload(TaskPayload):
    """Sweep selected sources, or resume unfinished work from a source batch."""

    source_ids: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    resume_from_task_id: Annotated[str, Field(min_length=1)] | None = None
    fresh_details: bool = False

    @model_serializer(mode="wrap")
    def serialize_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Omit optional selectors from ordinary scan requests."""
        payload: dict[str, object] = handler(self)
        if self.resume_from_task_id is None:
            payload.pop("resume_from_task_id", None)
        if not self.fresh_details:
            payload.pop("fresh_details", None)
        return payload

    @model_validator(mode="after")
    def unique_sources(self) -> SourceScanPayload:
        """Refuse duplicate work inside one board selection."""
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Source scan source_ids must be unique.")
        return self


class GuidedCampaignRetryPayload(TaskPayload):
    """Reprocess captured campaign attempts whose local processing failed."""


class SourceContinuationPayload(TaskPayload):
    """Consume the next bounded chunk of a durable publisher frontier."""

    continuation_id: str = Field(min_length=1)
    generation: int = Field(ge=1)


class GuidedCampaignPayload(TaskPayload):
    """Run one approved gap-campaign cycle against its compiled manifest."""

    campaign_manifest_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    campaign_cycle: int = Field(ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_hosted_searches: int | None = Field(default=None, ge=1)


class SearchVocabularyPayload(TaskPayload):
    """Suggest CV-grounded search vocabulary; takes no additional fields."""


class RelevanceReviewPayload(TaskPayload):
    """Review a selected set of pending discoveries for relevance."""

    pending_discovery_ids: list[str] = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def unique_selection(self) -> RelevanceReviewPayload:
        """Refuse duplicate work inside one relevance-review selection."""
        if len(set(self.pending_discovery_ids)) != len(self.pending_discovery_ids):
            raise ValueError("Relevance review pending_discovery_ids must be unique.")
        return self
