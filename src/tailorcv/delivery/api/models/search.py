"""Job-search parameter API contracts."""

from datetime import datetime

from pydantic import Field

from tailorcv.application.discovery.guided_campaign import BranchKind
from tailorcv.application.discovery.guided_campaign_report import EstimateBasis
from tailorcv.application.workspaces.parameter_models import (
    SearchParametersSavedResponse,
    SearchParametersStateResponse,
    SearchParameterVersionResponse,
)
from tailorcv.domain.campaigns import (
    AttemptErrorCategory,
    AttemptState,
    BranchOutcome,
    CampaignReason,
    CampaignStatus,
    CapturedUrlOutcome,
    StagedBasis,
)
from tailorcv.domain.search_parameters import CompensationBasis, JobSearchParameters
from tailorcv.shared.models import StrictModel


class SearchParametersBody(StrictModel):
    """A complete replacement set of job-search parameters.

    The fields cross-validate, so the document is replaced whole, never patched.
    """

    workspace_id: str = Field(min_length=1)
    parameters: JobSearchParameters
    expected_version: int | None = Field(
        default=None,
        ge=0,
        description="Version this edit started from. Omit only for a first write.",
    )


class SearchParameterCaps(StrictModel):
    """The bounds on the search vocabulary, straight from the constants that apply them.

    Capabilities and domains past their cap are saved but never ranked against
    until moved up the list. Titles and archetypes bound what a guided search
    may propose. Cluster terms are the one bound a save refuses to exceed.
    """

    titles: int
    archetypes: int
    capabilities: int
    domains: int
    cluster_terms: int


class SearchParameterOptions(StrictModel):
    """The vocabularies a parameters form offers, from the constants that apply them.

    Anything that renders a choice here reads this.
    """

    arrangements: list[str]
    levels: list[str]
    bases: list[CompensationBasis]
    eligibility: list[str]
    acceptance: list[str]
    lanes: list[str]
    employment_types: list[str]
    caps: SearchParameterCaps


class GuidedCampaignBoundsResponse(StrictModel):
    """The planned bounds attempts are sized to; a run may use more searches than planned."""

    max_model_calls: int
    max_hosted_searches: int
    max_output_tokens: int
    searches_per_attempt: int


class GuidedCampaignBranchPreviewResponse(StrictModel):
    """One coverage identity the campaign would search, with its prior outcome."""

    position: int
    key: str
    label: str
    kind: BranchKind
    priority_family: str
    protected: bool
    markets: list[str]
    floor_query: str
    attempt_position: int | None
    prior_outcome: BranchOutcome | None
    last_searched_at: datetime | None


class GuidedCampaignAttemptPreviewResponse(StrictModel):
    """One grouped provider call the campaign would dispatch."""

    position: int
    branch_positions: list[int]
    search_cap: int


class GuidedCampaignPreviewResponse(StrictModel):
    """What confirming the current inputs and bounds would run."""

    manifest_sha256: str
    cycle: int
    compiler_version: str
    bounds: GuidedCampaignBoundsResponse
    branches: list[GuidedCampaignBranchPreviewResponse]
    attempts: list[GuidedCampaignAttemptPreviewResponse]
    provider: str
    model: str
    supersedes_campaign_id: str | None
    planned_search_fee_cents: int | None
    estimated_cost_cents: int | None
    estimate_basis: EstimateBasis | None


class GuidedCampaignFindingResponse(StrictModel):
    """A newly staged posting, described by publisher facts only."""

    captured_url_id: str
    url: str
    staged_basis: StagedBasis
    pending_discovery_id: str
    employer: str
    title: str
    location: str | None
    branch_labels: list[str]
    model_rationale: str | None


class GuidedCampaignAdmittedPostingResponse(StrictModel):
    """One posting a board admission staged from its listings."""

    pending_discovery_id: str
    url: str
    employer: str
    title: str
    location: str | None


class GuidedCampaignBoardAdmissionResponse(StrictModel):
    """A board the campaign admitted; a registered one stays disabled until someone enables it."""

    url: str
    source_id: str | None
    employer: str
    branch_labels: list[str]
    admitted_postings: list[GuidedCampaignAdmittedPostingResponse]


class GuidedCampaignNotStagedUrlResponse(StrictModel):
    """A captured URL a refusal or a gap kept out of staging, kept visible and recoverable."""

    url: str
    outcome: CapturedUrlOutcome
    detail: str | None


class GuidedCampaignBranchRowResponse(StrictModel):
    """Coverage for one branch, as the report settled it."""

    position: int
    label: str
    priority_family: str
    protected: bool
    attempt_position: int | None
    outcome: BranchOutcome
    matched_queries: list[str]


class GuidedCampaignAttemptRowResponse(StrictModel):
    """Execution and spend for one grouped provider call."""

    position: int
    state: AttemptState
    hosted_search_count: int
    page_action_count: int
    finish: str | None
    captured_not_selected: int | None
    cost_cents: int | None
    error_category: AttemptErrorCategory | None


class GuidedCampaignReportResponse(StrictModel):
    """Everything the latest campaign found, spent, and left unsettled."""

    campaign_id: str
    manifest_sha256: str
    cycle: int
    status: CampaignStatus
    max_model_calls: int
    max_hosted_searches: int
    model_calls_used: int
    hosted_searches_used: int
    cost_cents: int | None
    known_posting_count: int
    registered_board_count: int
    findings: list[GuidedCampaignFindingResponse]
    boards_admitted: list[GuidedCampaignBoardAdmissionResponse]
    not_staged: list[GuidedCampaignNotStagedUrlResponse]
    branches: list[GuidedCampaignBranchRowResponse]
    attempts: list[GuidedCampaignAttemptRowResponse]
    warnings: list[CampaignReason]
    stranded_processing_attempts: int


class GuidedCampaignAbandonRequest(StrictModel):
    """The workspace an abandon decision must match before it is applied."""

    workspace_id: str = Field(min_length=1)


__all__ = [
    "GuidedCampaignAbandonRequest",
    "GuidedCampaignAdmittedPostingResponse",
    "GuidedCampaignAttemptPreviewResponse",
    "GuidedCampaignAttemptRowResponse",
    "GuidedCampaignBoardAdmissionResponse",
    "GuidedCampaignBoundsResponse",
    "GuidedCampaignBranchPreviewResponse",
    "GuidedCampaignBranchRowResponse",
    "GuidedCampaignFindingResponse",
    "GuidedCampaignNotStagedUrlResponse",
    "GuidedCampaignPreviewResponse",
    "GuidedCampaignReportResponse",
    "SearchParameterCaps",
    "SearchParameterOptions",
    "SearchParameterVersionResponse",
    "SearchParametersBody",
    "SearchParametersSavedResponse",
    "SearchParametersStateResponse",
]
