"""Define the posting-fetch result and application effect boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.records import PageClassification, PostingReadIntent, RobotsReadBasis
from clearscout.domain.staged_facts import PublisherClosingDate


@dataclass(frozen=True)
class FetchedPosting:
    """One posting read with source metadata and publisher closing statements."""

    text: str | None
    posting_status: str
    method: str
    fetched_at: datetime
    http_status: int | None = 200
    publisher_markup_present: bool | None = None
    title: str = ""
    employer: str = ""
    final_url: str = ""
    publisher_check: PostingCheck | None = None
    verification_note: str = ""
    robots_basis: RobotsReadBasis | None = None
    page_classification: PageClassification | None = None
    source: PostingSource | None = None
    publisher_closing_date: PublisherClosingDate | None = None


@runtime_checkable
class PostingFetcher(Protocol):
    """Network seam kept scripted in every test."""

    def fetch(
        self,
        url: str,
        *,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> FetchedPosting:
        """Fetch one posting politely or return an explicit closure observation."""
        ...
