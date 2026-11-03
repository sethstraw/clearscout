"""Read answers, posting text and the errors a posting read raises."""

from __future__ import annotations

from dataclasses import dataclass

from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.records import PageClassification, RobotsReadBasis
from clearscout.infrastructure.web_fetch.ingest.text import (
    JDText,
)
from clearscout.shared.errors import IngestError


@dataclass(frozen=True)
class Answer:
    """What a board API gave back, and why it gave nothing when it did not.

    ``note`` is empty in exactly two cases: the API answered, or this reader was
    never the right one for the URL. Both mean there is nothing to report.
    """

    text: str | None
    note: str = ""
    title: str = ""
    employer: str = ""
    publisher_check: PostingCheck | None = None

    @classmethod
    def absent(cls) -> Answer:
        """Return the answer for a URL this reader does not recognize at all."""
        return cls(None, "")


class UrlPostingText(JDText):
    """Posting text plus publisher validity facts available only from a URL."""

    valid_through: str = ""
    publisher_markup_present: bool | None = None
    title: str = ""
    employer: str = ""
    publisher_check: PostingCheck | None = None
    robots_basis: RobotsReadBasis | None = None
    page_classification: PageClassification | None = None
    source: PostingSource | None = None


PUBLISHER_MARKUP_METHOD = "publisher markup"


PAGE_WITH_PUBLISHER_MARKUP_METHOD = "page extraction with publisher markup"


class PublisherPostingUnverifiedError(IngestError):
    """A recognized publisher URL could not verify its specific posting."""


class PublisherReadRefusedError(IngestError):
    """A publisher API refused a posting whose page has no other readable form."""

    def __init__(self, note: str, http_status: int | None) -> None:
        """Keep the status the API answered with beside its stated reason."""
        super().__init__(note)
        self.http_status = http_status


def publisher_markup_was_present(method: str) -> bool:
    """Return whether an extraction method records a JobPosting declaration."""
    return method in {PUBLISHER_MARKUP_METHOD, PAGE_WITH_PUBLISHER_MARKUP_METHOD}
