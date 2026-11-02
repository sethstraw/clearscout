"""Screened-out title pages and reviews."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from tailorcv.application.discovery.funnel_audit import (
    FunnelReason,
)
from tailorcv.shared.models import StrictModel

ScreenedOutReviewState = Literal["new", "changed", "reviewed"]


class ScreenedOutTitleResponse(StrictModel):
    """One screened-out listing across every scan that refused it."""

    id: str
    url: str
    source_name: str
    employer: str
    title: str
    reason: FunnelReason
    title_code: str
    title_subject: str
    first_seen_at: datetime
    last_seen_at: datetime
    reviewed_at: datetime | None
    review_state: ScreenedOutReviewState


class ScreenedOutTitlePageResponse(StrictModel):
    """One filtered page of screened-out titles, with the workspace-wide counts."""

    items: list[ScreenedOutTitleResponse]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    sources: list[str]
    all_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)


class ScreenedOutTitleReviewRequest(StrictModel):
    """Mark these screened-out titles reviewed against their current title finding."""

    ids: list[str] = Field(min_length=1, max_length=250)


class ScreenedOutTitleReviewResponse(StrictModel):
    """How many screened-out titles the request marked reviewed."""

    reviewed: int = Field(ge=0)
