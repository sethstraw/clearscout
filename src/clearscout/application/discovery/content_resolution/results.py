"""A typed posting read, the checks it must pass, and whether it withdraws its row."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.failures import ValidationError
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.records import RobotsReadBasis
from clearscout.domain.staged_facts import PublisherClosingDate

MAX_IDENTITY_EVIDENCE_BYTES = 16_384


@dataclass(frozen=True)
class ResolvedPostingContent:
    """One deterministic content attempt with no inferred job facts."""

    requested_url: str
    final_url: str | None
    content_status: PostingContentAccessStatus
    identity_status: PostingIdentityStatus
    posting_status: PostingStatus | None
    extraction_method: PostingContentExtractionMethod | None
    fetched_at: datetime
    content: bytes | None = None
    http_status: int | None = None
    publisher_markup_present: bool | None = None
    identity_evidence: dict[str, Any] | None = None
    note: str = ""
    # None when no page body was read, so robots policy was never the question.
    robots_basis: RobotsReadBasis | None = None
    # What the body was converted from, stored beside it and never read here.
    source: PostingSource | None = None
    publisher_closing_date: PublisherClosingDate | None = None


LISTING_PAGE_NOTE = "The page this read retained lists many postings rather than this one."


def resolved_publisher_fetch(
    requested_url: str,
    fetched: FetchedPosting,
) -> ResolvedPostingContent | None:
    """Reuse a direct probe only when a supported publisher read supplied its body."""
    check = fetched.publisher_check
    if (
        fetched.posting_status != PostingStatus.ACTIVE
        or not fetched.text
        or check is None
        or check.status != "readable"
    ):
        return None
    return ResolvedPostingContent(
        requested_url=requested_url,
        final_url=fetched.final_url or requested_url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=PostingIdentityStatus.VERIFIED,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
        fetched_at=fetched.fetched_at,
        content=fetched.text.encode("utf-8"),
        http_status=fetched.http_status,
        publisher_markup_present=fetched.publisher_markup_present,
        identity_evidence={
            "method": fetched.method,
            "platform": check.platform,
            "publisher_declaration": True,
            "publisher_title": check.title,
            "publisher_employer": check.employer,
        },
        note="Publisher content was retained from the guided-campaign verification read.",
        robots_basis=fetched.robots_basis,
        source=fetched.source,
        publisher_closing_date=fetched.publisher_closing_date,
    )


def withdraws_row(result: ResolvedPostingContent) -> bool:
    """Return whether this read withdraws its row, for the write and the batch count alike."""
    return result.posting_status is PostingStatus.CLOSED


def validate_result(expected_requested_url: str, result: ResolvedPostingContent) -> None:
    """Refuse a read for another URL, in an inconsistent state, or with oversized evidence."""
    validate_resolved_posting_content(expected_requested_url, result)
    encoded_evidence = json.dumps(
        result.identity_evidence or {}, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    if len(encoded_evidence) > MAX_IDENTITY_EVIDENCE_BYTES:
        raise ValidationError("Posting identity evidence exceeds the retained evidence limit.")


def validate_resolved_posting_content(expected_url: str, result: ResolvedPostingContent) -> None:
    """Require one deterministic effect result to belong to its requested URL."""
    if result.requested_url != expected_url:
        raise ValidationError("Resolved posting content does not belong to the requested URL.")
    _validate_observation_facts(result)
    _validate_content_state(result)


def _validate_observation_facts(result: ResolvedPostingContent) -> None:
    """Require a real instant and a valid HTTP status when one was observed."""
    if result.fetched_at.tzinfo is None or result.fetched_at.utcoffset() is None:
        raise ValidationError("Resolved posting content requires an aware fetch timestamp.")
    if result.http_status is not None and not 100 <= result.http_status <= 599:
        raise ValidationError("Resolved posting content has an invalid HTTP status.")


def _validate_content_state(result: ResolvedPostingContent) -> None:
    """Require one coherent readable, closed, or unreadable effect result."""
    readable = result.content_status == PostingContentAccessStatus.READABLE
    if readable != (result.content is not None):
        raise ValidationError("Readable posting content requires exactly one retained body.")
    if readable:
        _validate_readable_content(result)
        return
    _validate_unreadable_content(result)


def _validate_readable_content(result: ResolvedPostingContent) -> None:
    if not result.content:
        raise ValidationError("Readable posting content cannot retain an empty body.")
    if not result.final_url:
        raise ValidationError("Readable posting content requires its final publisher URL.")
    if result.extraction_method is None:
        raise ValidationError("Readable posting content requires an extraction method.")
    if result.posting_status is not PostingStatus.ACTIVE:
        raise ValidationError("Readable posting content must describe an active posting.")


def _validate_unreadable_content(result: ResolvedPostingContent) -> None:
    if result.extraction_method is not None:
        raise ValidationError("Unreadable posting content cannot claim an extraction method.")
    if result.posting_status not in {None, PostingStatus.CLOSED}:
        raise ValidationError("Unreadable posting content cannot claim an active posting.")
    if (
        result.posting_status is PostingStatus.CLOSED
        and result.identity_status is PostingIdentityStatus.VERIFIED
    ):
        raise ValidationError("A closure observation cannot verify posting identity by itself.")
