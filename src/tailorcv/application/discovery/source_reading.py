"""Define the source-reading values and effect boundary used by discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast, runtime_checkable

from pydantic import StrictInt, TypeAdapter

from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.identity import is_absolute_http_url
from tailorcv.domain.records import (
    PostingContentAccessStatus,
    RobotsReadBasis,
    SourceReadOutcome,
)
from tailorcv.domain.staged_facts import StagedListingFacts

MAX_DEFERRED_SOURCE_URL_LENGTH = 4_096
MAX_SOURCE_READ_ATTEMPTS = 3
ContinuationLabelSource = Literal["visible", "aria-label", "title", "none"]
ContinuationStructuralContext = Literal[
    "article", "card", "footer", "header", "list", "main", "navigation", "other", "section", "table"
]
ContinuationRelation = Literal[
    "same-origin",
    "external-handoff",
    "publisher-content",
    "navigation-chrome",
    "substantive-label",
    "repeated-target-shape",
]
_LABEL_SOURCES = frozenset({"visible", "aria-label", "title", "none"})
_STRUCTURAL_CONTEXTS = frozenset(
    {
        "article",
        "card",
        "footer",
        "header",
        "list",
        "main",
        "navigation",
        "other",
        "section",
        "table",
    }
)
_RELATIONS = frozenset(
    {
        "same-origin",
        "external-handoff",
        "publisher-content",
        "navigation-chrome",
        "substantive-label",
        "repeated-target-shape",
    }
)
# A traversal that reads its whole discovered set settles as a publisher index,
# so completeness is not the ATS reader's claim alone.
_COMPLETE_READ_OUTCOMES = frozenset(
    {SourceReadOutcome.ATS_COMPLETE, SourceReadOutcome.PUBLISHER_INDEX}
)


@dataclass(frozen=True)
class SourceListing:
    """One open position as its published source states it."""

    employer: str
    title: str
    url: str
    location: str = ""
    requisition_id: str = ""
    facts: StagedListingFacts = field(default_factory=StagedListingFacts)
    content_status: PostingContentAccessStatus = PostingContentAccessStatus.NOT_YET_READ
    content_note: str = ""
    # The posting's own description when the board API publishes one, as plain
    # text; screening reads it for responsibility evidence and never stores it.
    summary: str = ""
    # The same board-published description in full, untruncated. Retained at
    # discovery as evidence; the bounded summary above stays the screening input.
    published_description: str = ""
    source: PostingSource | None = None


@dataclass(frozen=True)
class PostingCheck:
    """Free publisher metadata read for one supported ATS posting URL."""

    status: Literal["readable", "unreadable", "unsupported", "closed"]
    note: str
    platform: str = ""
    title: str = ""
    employer: str = ""
    location: str = ""
    facts: StagedListingFacts = field(default_factory=StagedListingFacts)
    text: str = ""
    # The status the publisher API answered with when it refused the read.
    http_status: int | None = None
    # Why robots policy let the read this check came from proceed; None when no
    # response was read.
    robots_basis: RobotsReadBasis | None = None
    # Present only beside a body, since raw is kept only where a body is stored.
    source: PostingSource | None = None
    # Place entries the publisher stated, counting a repeated label each time.
    location_entries: int = 0


def _require_absolute_url(value: str, noun: str) -> None:
    if not value or len(value) > MAX_DEFERRED_SOURCE_URL_LENGTH:
        raise ValueError(f"{noun} must contain 1 to 4096 characters")
    if not is_absolute_http_url(value):
        raise ValueError(f"{noun} must be an absolute HTTP URL")


def _require_known_relations(relations: tuple[ContinuationRelation, ...]) -> None:
    if not relations or len(set(relations)) != len(relations):
        raise ValueError("source continuation relations must be nonempty and unique")
    if any(relation not in _RELATIONS for relation in relations):
        raise ValueError("source continuation relation is not recognized")


@dataclass(frozen=True)
class SourceDetailCheckpoint:
    """Stable publisher identities resolved or awaiting detail reads for one board."""

    platform: str
    board_url: str
    completed: tuple[tuple[str, str], ...] = ()
    closed_ids: tuple[str, ...] = ()
    failed_attempts: tuple[tuple[str, StrictInt], ...] = ()
    pending_ids: tuple[str, ...] = ()
    protected: tuple[tuple[str, str], ...] = ()
    listing_ids: tuple[str, ...] = ()
    listing_urls: tuple[tuple[str, str], ...] = ()
    listing_facts: tuple[tuple[str, StagedListingFacts], ...] = ()
    page_fingerprints: tuple[str, ...] = ()
    listing_total: StrictInt | None = None
    listing_unstable: bool = False
    listing_pass: StrictInt = 1
    pass_listing_ids: tuple[str, ...] = ()
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        """Reject contradictory identities and unsafe durable addresses."""
        self._validate_listing()
        _require_absolute_url(self.board_url, "detail checkpoint board URL")
        if not self.platform.strip():
            raise ValueError("detail checkpoint platform must be stated")
        completed = tuple(item for item, _url in self.completed)
        failed = tuple(item for item, _attempts in self.failed_attempts)
        protected = tuple(item for item, _url in self.protected)
        url_ids = tuple(item for item, _url in self.listing_urls)
        fact_ids = tuple(item for item, _facts in self.listing_facts)
        for ids in (
            completed,
            self.closed_ids,
            failed,
            self.pending_ids,
            protected,
            self.listing_ids,
            self.pass_listing_ids,
            fact_ids,
            url_ids,
        ):
            if any(not item for item in ids) or len(set(ids)) != len(ids):
                raise ValueError("detail checkpoint identities must be nonempty and unique")
        resolved = set(completed) | set(self.closed_ids) | set(protected)
        if set(protected) & (set(completed) | set(self.closed_ids)):
            raise ValueError("protected detail identities cannot be completed or closed")
        if set(completed) & set(self.closed_ids) or resolved & set(self.pending_ids):
            raise ValueError("resolved detail identities cannot remain pending or closed twice")
        if self.listing_urls and set(url_ids) != set(self.listing_ids):
            raise ValueError("enumeration URLs must match listing identities")
        self._validate_pending_details(failed, fact_ids)
        if any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in self.page_fingerprints):
            raise ValueError("listing page fingerprints must be SHA256 digests")
        for _item, url in (*self.completed, *self.protected, *self.listing_urls):
            _require_absolute_url(url, "completed detail posting URL")

    def _validate_listing(self) -> None:
        if self.listing_total is not None and self.listing_total < 0:
            raise ValueError("listing total must be nonnegative")
        if self.listing_pass < 1 or not set(self.pass_listing_ids) <= set(self.listing_ids):
            raise ValueError("a listing pass must be positive and list only known identities")

    def _validate_pending_details(self, failed: tuple[str, ...], fact_ids: tuple[str, ...]) -> None:
        pending = set(self.pending_ids)
        if not set(failed) <= pending:
            raise ValueError("failed detail identities must remain pending")
        if not set(fact_ids) <= pending:
            raise ValueError("retained listing facts must belong to pending detail identities")
        if any(attempts <= 0 for _item, attempts in self.failed_attempts):
            raise ValueError("failed detail attempt counts must be positive")

    def as_json(self) -> dict[str, object]:
        """Serialize this checkpoint for its existing durable frontier owner."""
        return cast(dict[str, object], _DETAIL_CHECKPOINT_DOCUMENT.dump_python(self, mode="json"))

    @classmethod
    def from_json(cls, value: object) -> SourceDetailCheckpoint:
        """Validate a durable checkpoint before a reader uses its identities."""
        return _DETAIL_CHECKPOINT_DOCUMENT.validate_python(value)


_DETAIL_CHECKPOINT_DOCUMENT = TypeAdapter(SourceDetailCheckpoint)


@dataclass(frozen=True)
class SourceDetailCoverage:
    """Resolved posting membership from a complete enumeration of one board."""

    platform: str
    board_url: str
    posting_urls: tuple[str, ...]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        """Keep authoritative empty membership distinct from unavailable coverage."""
        _require_absolute_url(self.board_url, "detail coverage board URL")
        if not self.platform.strip() or len(set(self.posting_urls)) != len(self.posting_urls):
            raise ValueError("detail coverage requires a platform and unique posting URLs")
        for url in self.posting_urls:
            _require_absolute_url(url, "detail coverage posting URL")

    def as_json(self) -> dict[str, object]:
        """Serialize the board scope and its resolved current posting membership."""
        return cast(dict[str, object], _DETAIL_COVERAGE_DOCUMENT.dump_python(self, mode="json"))

    @classmethod
    def from_json(cls, value: object) -> SourceDetailCoverage:
        """Validate retained coverage before treating absence as withdrawal evidence."""
        return _DETAIL_COVERAGE_DOCUMENT.validate_python(value)


_DETAIL_COVERAGE_DOCUMENT = TypeAdapter(SourceDetailCoverage)


@dataclass(frozen=True)
class SourceContinuationTarget:
    """Publisher-declared frontier identity retained across bounded reads."""

    url: str
    label: str
    label_source: ContinuationLabelSource
    source_url: str
    structural_context: ContinuationStructuralContext
    relations: tuple[ContinuationRelation, ...]
    depth: int = 1
    automatic_eligible: bool = True
    deferred_reason: str = ""
    detail_checkpoint: SourceDetailCheckpoint | None = None
    manual_detail_retry: bool = False

    def __post_init__(self) -> None:
        """Refuse targets that cannot be safely persisted or fetched."""
        _require_absolute_url(self.url, "source continuation URL")
        if len(self.label) > 300:
            raise ValueError("source continuation label exceeds 300 characters")
        _require_absolute_url(self.source_url, "source continuation source URL")
        if self.label_source not in _LABEL_SOURCES:
            raise ValueError("source continuation label source is not recognized")
        if self.structural_context not in _STRUCTURAL_CONTEXTS:
            raise ValueError("source continuation structural context is not recognized")
        _require_known_relations(self.relations)
        if self.depth < 1:
            raise ValueError("source continuation depth must be positive")
        if len(self.deferred_reason) > 120:
            raise ValueError("source continuation deferred reason exceeds 120 characters")


DETAIL_FAILURE_EXAMPLE_LIMIT = 3
DetailFailureReason = Literal["timeout", "http_status", "parse", "other"]


@dataclass(frozen=True)
class SourceDetailSummary:
    """Bounded detail-read outcomes for one source read."""

    attempted: int = 0
    read: int = 0
    failed: int = 0
    interrupted: int = 0
    capped: int = 0
    failure_reasons: tuple[tuple[DetailFailureReason, int], ...] = ()
    failure_examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse negative counts and unsettled detail attempts."""
        if any(
            count < 0
            for count in (self.attempted, self.read, self.failed, self.interrupted, self.capped)
        ):
            raise ValueError("source detail counts cannot be negative")
        if self.attempted != self.read + self.failed + self.interrupted:
            raise ValueError(
                "source detail attempts must equal read, failed, and interrupted counts"
            )
        if any(count <= 0 for _reason, count in self.failure_reasons):
            raise ValueError("source detail failure reason counts must be positive")

    def combined(self, other: SourceDetailSummary) -> SourceDetailSummary:
        """Sum detail outcomes and retain bounded distinct failure examples."""
        reasons = dict(self.failure_reasons)
        for reason, count in other.failure_reasons:
            reasons[reason] = reasons.get(reason, 0) + count
        return SourceDetailSummary(
            attempted=self.attempted + other.attempted,
            read=self.read + other.read,
            failed=self.failed + other.failed,
            interrupted=self.interrupted + other.interrupted,
            capped=self.capped + other.capped,
            failure_reasons=tuple(reasons.items()),
            failure_examples=tuple(
                dict.fromkeys((*self.failure_examples, *other.failure_examples))
            )[:DETAIL_FAILURE_EXAMPLE_LIMIT],
        )


@dataclass(frozen=True)
class SourceRead:
    """What a source published, and whether that was all of it.

    Absence only means an opening was withdrawn when the read was complete. A
    platform API that answers with every posting is complete; a page whose
    markup happens to describe some jobs, or a paged source that hit its page
    ceiling, is not.
    """

    listings: list[SourceListing]
    complete: bool
    raw_count: int
    outcome: SourceReadOutcome
    skip_reasons: dict[str, int] = field(default_factory=dict)
    note: str = ""
    deferred_targets: tuple[SourceContinuationTarget, ...] = ()
    # (host, kind, detail) handoff scopes from a publisher read; empty for board adapters.
    handoff_scopes: tuple[tuple[str, str, str], ...] = ()
    # None when the adapter does not track attempts; a tuple records exactly which
    # supplied continuation targets reached a GET attempt before the traversal stopped.
    attempted_target_urls: tuple[str, ...] | None = None
    protected_posting_urls: tuple[str, ...] = ()
    # Posting pages this read fetched and found closed by the publisher.
    closed_posting_urls: tuple[str, ...] = ()
    # True when discovering this read's candidates (not just reading them) hit
    # its own bound, so a scan must never treat a drained frontier as full coverage.
    discovery_truncated: bool = False
    # What this read's outcome would be with an empty deferred frontier; lets a
    # scan that dedups every deferred target away relabel itself without
    # guessing an outcome the adapter never actually reported.
    exhausted_outcome: SourceReadOutcome | None = None
    detail: SourceDetailSummary = field(default_factory=SourceDetailSummary)
    detail_coverage: SourceDetailCoverage | None = None
    listing_coverage: SourceDetailCoverage | None = None
    # Cumulative mapped detail bodies for one checkpoint scope, excluding protected membership.
    detail_read_urls: tuple[str, ...] | None = None
    publisher_listing_receipts: tuple[SourceDetailCheckpoint, ...] = ()
    # Board listing pages this read fetched; past one, a count-keeping swap between
    # pages can hide an open posting, so absence alone does not withdraw it.
    listing_pages: int = 0

    def __post_init__(self) -> None:
        """Refuse contradictory counts, completion claims, and frontiers."""
        if self.raw_count < 0 or self.raw_count < len(self.listings):
            raise ValueError("source read raw count cannot be negative or below listing count")
        self._validate_detail_reads()
        if any(count <= 0 for count in self.skip_reasons.values()):
            raise ValueError("source read skip counts must be positive")
        if self.complete and self.outcome not in _COMPLETE_READ_OUTCOMES:
            raise ValueError(
                "source read completion requires an ATS-complete or publisher-index outcome"
            )
        if not self.complete and self.outcome is SourceReadOutcome.ATS_COMPLETE:
            raise ValueError("an ATS-complete outcome requires a complete source read")
        if self.complete and self.deferred_targets:
            raise ValueError("a complete source read cannot retain continuation targets")
        if self.deferred_targets and self.outcome is not SourceReadOutcome.CRAWL_CONTINUATION:
            raise ValueError("a deferred source frontier requires a crawl-continuation outcome")
        if self.exhausted_outcome is not None and not self.deferred_targets:
            raise ValueError("an exhausted outcome requires a deferred frontier to resolve")

    def _validate_detail_reads(self) -> None:
        if self.detail_read_urls is None:
            return
        if len(set(self.detail_read_urls)) != len(self.detail_read_urls):
            raise ValueError("source detail read URLs must be unique")
        if len(self.detail_read_urls) > self.raw_count:
            raise ValueError("source detail reads cannot exceed represented listing count")
        for url in self.detail_read_urls:
            _require_absolute_url(url, "source detail read URL")


@dataclass(frozen=True)
class SourceReadOptions:
    """Detail reuse and protected posting addresses for one source read."""

    fresh_details: bool = False
    protected_posting_urls: frozenset[str] = frozenset()
    # Normalized posting URLs read since the rescreen cutoff, which an opted-in reader skips.
    recent_posting_urls: frozenset[str] = frozenset()
    protected_posting_keys: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Resolve protected URL variants once for this read."""
        if not self.protected_posting_urls:
            object.__setattr__(self, "protected_posting_keys", frozenset())
            return
        from tailorcv.application.discovery.posting_identity import posting_identity_keys

        object.__setattr__(
            self,
            "protected_posting_keys",
            frozenset(
                key for url in self.protected_posting_urls for key in posting_identity_keys(url)
            ),
        )

    def allows_posting(self, url: str) -> bool:
        """Keep protected URL and tenant-scoped requisition variants unread."""
        from tailorcv.application.discovery.posting_identity import posting_identity_keys

        return not self.protected_posting_keys.intersection(posting_identity_keys(url))


_DEFAULT_READ_OPTIONS = SourceReadOptions()


@runtime_checkable
class SourceReader(Protocol):
    """Read the postings published by one source."""

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = _DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """Return the source's explicit read outcome and respect protected postings."""
        ...


@runtime_checkable
class SourceContinuationReader(Protocol):
    """Read an exact bounded set of publisher-declared frontier URLs."""

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = _DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """GET supplied targets within stored scope and respect protected postings."""
        ...
