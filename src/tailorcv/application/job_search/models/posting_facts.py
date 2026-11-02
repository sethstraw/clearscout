"""Published posting facts, posted dates and screening findings."""

from datetime import date
from typing import Literal

from pydantic import Field

from tailorcv.application.contracts import PublisherClosingDateResponse
from tailorcv.application.discovery.screening import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningStanding,
)
from tailorcv.domain.staged_facts import FreshnessState, ListingIdentityTrust, PostedDateGrade
from tailorcv.shared.models import StrictModel


class PublishedFactResponse(StrictModel):
    """One publisher-stated fact and whether its values are complete."""

    values: list[str] = Field(default_factory=list)
    unresolved_values: list[str] = Field(default_factory=list)
    provenance: str = ""
    complete: bool = False


class PostedDateStatementResponse(StrictModel):
    """One publisher posted-date statement and the day it was read."""

    wording: str
    basis: str
    grade: PostedDateGrade
    normalized_value: str | None = None
    precision: Literal["date", "timestamp"] | None = None
    timezone: str | None = None
    clarification: str = ""
    read_on: date | None = None


class PublisherPostedDateResponse(StrictModel):
    """The earliest posted date a publisher stated, and its latest statement."""

    earliest: PostedDateStatementResponse
    latest: PostedDateStatementResponse
    reads: int = 1
    changes: int = 0
    day: date | None = None
    grade: PostedDateGrade
    reposted: bool = False
    needs_clarification: bool = False


class StagedListingFactsResponse(StrictModel):
    """The versioned facts retained before full posting ingestion."""

    schema_version: Literal[5] = 5
    identity_trust: ListingIdentityTrust = ListingIdentityTrust.UNVERIFIED_HINT
    employer: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    role_aliases: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    locations: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    arrangements: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    employment_types: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    compensation: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    freshness: FreshnessState = FreshnessState.UNKNOWN
    focus_terms: PublishedFactResponse = Field(default_factory=PublishedFactResponse)
    closing_date: PublisherClosingDateResponse | None = None
    posted_date: PublisherPostedDateResponse | None = None


class ScreeningFindingResponse(StrictModel):
    """One deterministic search-policy finding exposed to a person."""

    dimension: ScreeningDimension
    standing: ScreeningStanding
    code: ScreeningCode
    subject: str = ""


# The funnel records only non-match findings and drops each finding's standing,
# so this ledger contract has no standing field, unlike ScreeningFindingResponse.
class FunnelScreeningFindingResponse(StrictModel):
    """One decisive screening finding retained with a board-scan outcome."""

    dimension: ScreeningDimension
    code: ScreeningCode
    subject: str = ""
