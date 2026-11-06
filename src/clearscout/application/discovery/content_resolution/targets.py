"""Pending rows bound for a read, and what storing a read reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clearscout.application.discovery.content_resolution.results import ResolvedPostingContent
from clearscout.domain import PostingContentAccessStatus
from clearscout.domain.records import PostingReadIntent


class PostingContentResolver(Protocol):
    """Effect boundary for deterministic static and rendered posting reads."""

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        """Return a typed attempt for one pending posting URL, given the board that listed it."""
        ...


@dataclass(frozen=True)
class PostingResolutionTarget:
    """Snapshot-bound pending identity presented by preflight."""

    id: str
    employer: str
    title: str
    url: str
    content_status: PostingContentAccessStatus
    fingerprint: str
    current_content_id: str | None
    # Blank where the display hint is not publisher identity, so it cannot contradict a read.
    expected_employer: str
    expected_title: str
    # Blank unless the workspace registers exactly one job board for the employer.
    registered_board_url: str = ""


@dataclass(frozen=True)
class PostingResolutionPreflight:
    """Exact free-batch target set and state digest."""

    targets: tuple[PostingResolutionTarget, ...]
    snapshot_sha256: str


@dataclass(frozen=True)
class StoredTarget:
    """A pending row as stored when its read was planned, checked again before the write."""

    id: str
    fingerprint: str
    employer: str
    title: str
    url: str
    current_content_id: str | None
    content_status: PostingContentAccessStatus
    expected_employer: str
    expected_title: str
    # The address of the source that listed this row, blank when a search staged it.
    board_url: str = ""
    # A person's add staged this row and nothing has read its content yet.
    unread_person_add: bool = False


@dataclass(frozen=True)
class AddressHolder:
    """The other record that already holds a posting address, named by its audit entity type."""

    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class StoredEvidence:
    """One stored read: its evidence id, whether it relisted its row, and who kept its address."""

    id: str
    relisted: bool = False
    # Set when the read's verified address stays with another record, so the row kept its own.
    address_holder: AddressHolder | None = None
    # True when the read was retained beside a readable body that stays the row's current one.
    kept_body: bool = False


@dataclass(frozen=True)
class PrefetchedContent:
    """One posting read made before its row was staged, and the identity it was read against."""

    url: str
    employer: str
    title: str
    result: ResolvedPostingContent | None = None
    failure: Exception | None = None

    def serves(self, target: StoredTarget) -> bool:
        """Whether a live read of this target would have asked exactly what this one asked."""
        return (self.url, self.employer, self.title) == (
            target.url,
            target.expected_employer,
            target.expected_title,
        )
