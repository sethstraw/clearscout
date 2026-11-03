"""Job pages, the application funnel and the workspace summary."""

from clearscout.application.job_search.models.jobs import JobSummaryResponse
from clearscout.shared.models import StrictModel


class JobEmployerFacetResponse(StrictModel):
    """One employer in a filtered list and how many rows it has there."""

    name: str
    count: int


class JobPageResponse(StrictModel):
    """Stable offset page for the jobs ledger."""

    items: list[JobSummaryResponse]
    total: int
    offset: int
    limit: int
    # Counted with every filter except the employer selection itself.
    employers: list[JobEmployerFacetResponse]


class ApplicationFunnelResponse(StrictModel):
    """Current application counts for command-center pipeline stages."""

    package_ready: int
    applied: int
    interviewing: int
    offer: int


# decided_without_outcome holds jobs whose current decision is apply_now or
# premium_target while their application status is still not_applied; its count
# covers the whole set, the list is capped.
class WorkspaceSummaryResponse(StrictModel):
    """Untruncated command-center summary for one complete workspace ledger."""

    total_jobs: int
    actionable_applications: int
    incomplete_jobs: int
    application_funnel: ApplicationFunnelResponse
    highest_intrinsic: list[JobSummaryResponse]
    highest_upside: list[JobSummaryResponse]
    actionable: list[JobSummaryResponse]
    decided_without_outcome: list[JobSummaryResponse]
    decided_without_outcome_count: int
