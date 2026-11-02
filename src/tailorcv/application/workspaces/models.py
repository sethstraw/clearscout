"""Typed workspace and master-CV read models."""

from datetime import datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from tailorcv.application.config import Effort, ProviderName
from tailorcv.domain import EvidenceClassification
from tailorcv.shared.costs import ServiceTier
from tailorcv.shared.models import StrictModel


class WorkspaceResponse(StrictModel):
    """Public workspace fields."""

    id: str
    slug: str
    name: str
    root_path: str | None
    provider: ProviderName
    created_at: datetime


class ScanScheduleConfiguration(StrictModel):
    """One opt-in local calendar and source scope for free scans."""

    frequency: Literal["daily", "weekly"] = "daily"
    local_time: str = Field(default="09:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1)
    weekday: int = Field(default=0, ge=0, le=6)
    scope: Literal["all_enabled", "selected"] = "all_enabled"
    source_ids: list[str] = Field(default_factory=list)
    paused: bool = False

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        """Require a named timezone with calendar transition rules."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid named timezone.") from exc
        return value

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        """Keep a saved selection distinct from all enabled sources."""
        if any(not source_id.strip() for source_id in self.source_ids):
            raise ValueError("Source IDs cannot be blank.")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Select each source only once.")
        if self.scope == "selected" and not self.source_ids:
            raise ValueError("Choose at least one source for a saved selection.")
        if self.scope == "all_enabled" and self.source_ids:
            raise ValueError("All enabled sources cannot also name a saved selection.")
        return self


class ScanScheduleInput(ScanScheduleConfiguration):
    """Save the calendar only if its displayed revision is still current."""

    expected_revision: int = Field(ge=0)


class ScanScheduleState(StrictModel):
    """Saved calendar and the latest locally claimed occurrence."""

    configuration: ScanScheduleConfiguration
    revision: int = Field(ge=1)
    next_run_at: datetime | None
    last_due_at: datetime | None = None
    last_task_id: str | None = None
    last_error: str | None = None


class ScanScheduleResponse(StrictModel):
    """Workspace calendar with unavailable saved sources kept visible."""

    workspace_id: str
    state: ScanScheduleState | None
    unavailable_source_ids: list[str] = Field(default_factory=list)


class EffectiveModelChoiceResponse(StrictModel):
    """The effective model, effort, and service tier one provider stage will send."""

    model: str = Field(min_length=1)
    effort: Effort | None
    service_tier: ServiceTier


class GuidedCampaignConfigurationResponse(StrictModel):
    """The effective paid provider choice one guided-campaign task will send."""

    provider: ProviderName
    model_choice: EffectiveModelChoiceResponse


class SearchVocabularyConfigurationResponse(StrictModel):
    """The effective paid provider choice one search-vocabulary task will send."""

    provider: ProviderName
    model_choice: EffectiveModelChoiceResponse


class RelevanceReviewConfigurationResponse(StrictModel):
    """The effective paid provider choice one relevance-review task will send."""

    provider: ProviderName
    model_choice: EffectiveModelChoiceResponse
    batch_rows: int = Field(description="Most rows one relevance-review model call carries.")


class MasterCvImportResponse(StrictModel):
    """Registered canonical CV and its retained source and audit report."""

    workspace_id: str
    master_cv_version_id: str
    artifact_id: str
    canonical_path: str
    source_path: str
    report_path: str | None
    method: Literal["adopted", "standardized"]
    content_sha256: str
    entry_count: int


class MasterCvVersionResponse(StrictModel):
    """One immutable master CV version."""

    id: str
    content_sha256: str
    effective_at: datetime
    byte_size: int | None
    source_uri: str
    is_current: bool


class MasterCvContentResponse(MasterCvVersionResponse):
    """A stored version with its verified text."""

    content: str


class MasterCvDiffResponse(StrictModel):
    """A unified diff between two stored versions."""

    from_version: str
    to_version: str
    diff: str


class CitationDriftResponse(StrictModel):
    """What the proposed text does to one cited quote."""

    found: bool
    text: str
    changed: bool
    material: bool
    reason: str


class CitationImpactResponse(StrictModel):
    """One committed citation and how the proposed CV would read against it."""

    evaluation_id: str
    job_posting_id: str
    employer: str
    title: str
    quote: str
    char_start: int | None
    char_end: int | None
    classification: EvidenceClassification
    # None where the cited version stores no entries, which is not the same as an
    # entry the edit removes.
    entry_carried: bool | None
    drift: CitationDriftResponse


class MasterCvImpactResponse(StrictModel):
    """Which committed evaluations cite text an edit would change."""

    workspace_id: str
    master_cv_version_id: str
    content_sha256: str
    identical: bool
    citation_count: int
    material_count: int
    changed_count: int
    unchanged_count: int
    not_found_count: int
    orphaned_count: int
    returned_count: int
    omitted_count: int
    citations: list[CitationImpactResponse]
    # A save with a reason here standardizes the draft with the model.
    reread_reason: str | None = None


class MasterCvRereadEstimateResponse(StrictModel):
    """What re-reading a CV with the model has cost in this workspace."""

    past_rereads: int
    average_usd: float | None


class MasterCvEditResponse(MasterCvImportResponse):
    """A saved edit, and whether it actually appended a version."""

    created: bool
    previous_master_cv_version_id: str | None
