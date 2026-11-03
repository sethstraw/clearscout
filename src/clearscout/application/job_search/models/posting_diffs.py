"""Posting observation bodies and fact diffs."""

from typing import Literal

from clearscout.application.contracts import AggregatorCreditResponse
from clearscout.application.job_search.models.jobs import ObservationResponse
from clearscout.domain import (
    ArtifactLogicalType,
)
from clearscout.shared.models import StrictModel


class PostingObservationBodyResponse(ObservationResponse):
    """Verified full body and source facts for one posting observation."""

    job_posting_id: str
    workspace_id: str
    source_url: str
    discovery_url: str | None = None
    aggregator: AggregatorCreditResponse | None = None
    logical_type: ArtifactLogicalType
    mime_type: str
    sha256: str
    body: str
    rendered_text: str


class PostingFactDiffResponse(StrictModel):
    """One changed structured posting fact."""

    field: Literal[
        "posting_status",
        "location",
        "residence_rule",
        "employment_type",
        "payroll_arrangement",
    ]
    before: str
    after: str


class PostingDiffResponse(StrictModel):
    """Verified comparison of two observations of one posting."""

    job_posting_id: str
    from_observation_id: str
    to_observation_id: str
    changed_facts: list[PostingFactDiffResponse]
    body_diff: str
