"""Store the content evidence a board read itself supplies for staged rows."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.discovery.scan_staging import DiscoveryStaging, StagedListings
from clearscout.application.discovery.source_reading import (
    SourceListing,
    SourceRead,
    SourceReadOptions,
)
from clearscout.application.failures import ValidationError
from clearscout.application.tasks import TaskContext
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    utc_now,
)
from clearscout.domain.staged_facts import PublisherClosingDate
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PendingDiscovery, Source

logger = logging.getLogger(__name__)


def _same_closing_statement(
    previous: PublisherClosingDate | None, current: PublisherClosingDate
) -> bool:
    return (
        previous is not None
        and previous.source_url == current.source_url
        and previous.with_artifacts(body_artifact_id=None, publisher_artifact_id=None).statements
        == current.with_artifacts(body_artifact_id=None, publisher_artifact_id=None).statements
    )


class StagedContentEvidence:
    """Write published bodies and closures a read found onto the rows it staged."""

    def __init__(
        self,
        database: Database,
        staging: DiscoveryStaging,
        content_resolution: PostingContentResolutionService | None,
    ) -> None:
        """Bind the database, staging, and content service the evidence writes go through."""
        self.database = database
        self.staging = staging
        self.content_resolution = content_resolution

    def visible_addresses(self, workspace_id: str, ids: list[str]) -> dict[str, str]:
        """Map active, unprotected rows to their current read addresses."""
        options = SourceReadOptions(
            protected_posting_urls=self.staging.protected_posting_urls(workspace_id)
        )
        with self.database.session() as session:
            return {
                row.id: row.canonical_url or row.discovery_url
                for row in session.execute(
                    select(
                        PendingDiscovery.id,
                        PendingDiscovery.canonical_url,
                        PendingDiscovery.discovery_url,
                    ).where(
                        PendingDiscovery.workspace_id == workspace_id,
                        PendingDiscovery.id.in_(ids),
                        PendingDiscovery.archived_at.is_(None),
                        PendingDiscovery.withdrawn_at.is_(None),
                    )
                )
                if options.allows_posting(row.discovery_url)
                and options.allows_posting(row.canonical_url or row.discovery_url)
            }

    def retain_published_descriptions(
        self,
        workspace_id: str,
        listings: list[SourceListing],
        placed: StagedListings,
        context: TaskContext | None,
        *,
        origin: str,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> None:
        """Promote a board-published full description into current content evidence."""
        if self.content_resolution is None:
            return
        bodied = [item for item in listings if item.published_description.strip()]
        ids = placed.row_ids(bodied)
        if not ids:
            return
        # A row matched by requisition or read address keeps its own address, and
        # evidence is current only when requested at that address.
        addresses = self.visible_addresses(workspace_id, ids)
        retained: set[str] = set()
        for listing in bodied:
            body = listing.published_description.strip()
            pending_id = placed.row_ids_by_url.get(listing.url)
            if pending_id is None or pending_id in retained or pending_id not in addresses:
                continue
            retained.add(pending_id)
            try:
                current = self.content_resolution.current.read(workspace_id, pending_id)
            except ValidationError as exc:
                # An unreadable stored body is replaced by the published one, not fatal.
                logger.warning(
                    "retained_body_unreadable",
                    extra={"pending_discovery_id": pending_id, "error_type": type(exc).__name__},
                )
                current = None
            closing_date = listing.facts.closing_date or publisher_closing_date(
                body,
                source_url=listing.url,
                observed_at=utc_now(),
            )
            if current is not None and current.body == body.encode("utf-8"):
                previous_date = current.publisher_closing_date
                if _same_closing_statement(previous_date, closing_date):
                    continue
            observed_at = closing_date.observed_at or utc_now()
            note = "Publisher board supplied and retained the full posting description."
            if closing_date.has_passed(observed_at):
                note += " Closing date passed; verify availability."
            self.content_resolution.store_prefetched_result(
                workspace_id,
                pending_id,
                ResolvedPostingContent(
                    requested_url=addresses[pending_id],
                    final_url=listing.url,
                    content_status=PostingContentAccessStatus.READABLE,
                    identity_status=PostingIdentityStatus.VERIFIED,
                    posting_status=PostingStatus.ACTIVE,
                    extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
                    fetched_at=observed_at,
                    content=body.encode("utf-8"),
                    publisher_markup_present=True,
                    identity_evidence={"publisher_description": True},
                    note=note,
                    source=listing.source,
                    publisher_closing_date=closing_date,
                ),
                context,
                origin=origin,
                require_source_read=require_source_read,
            )

    def record_closed_postings(
        self,
        workspace_id: str,
        source: Source,
        read: SourceRead,
        context: TaskContext | None,
        *,
        origin: str,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> list[str]:
        """Store closure evidence for open rows whose posting page this read found closed.

        Returns the ids of the rows it recorded closed.
        """
        if self.content_resolution is None or not read.closed_posting_urls:
            return []
        ids = self.staging.open_rows_at(workspace_id, source.id, read.closed_posting_urls)
        if not ids:
            return []
        observed_at = utc_now()
        addresses = self.visible_addresses(workspace_id, sorted(ids))
        for pending_id, address in addresses.items():
            self.content_resolution.store_prefetched_result(
                workspace_id,
                pending_id,
                ResolvedPostingContent(
                    requested_url=address,
                    final_url=address,
                    content_status=PostingContentAccessStatus.NOT_YET_READ,
                    identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                    posting_status=PostingStatus.CLOSED,
                    extraction_method=None,
                    fetched_at=observed_at,
                    identity_evidence={"method": "board_detail", "closure": True},
                    note="The publisher reports that this posting is closed.",
                ),
                context,
                origin=origin,
                require_source_read=require_source_read,
            )
        return list(addresses)
