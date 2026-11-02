"""The review queue's staging writes, taken one at a time under a shared write turn."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from threading import Lock

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.discovery.board_slugs import backfilled_listing
from tailorcv.application.discovery.pending import (
    corrected_title_ids,
    dismissed_pending_discovery_ids,
    marked_family_ids,
    person_restored_pending_discovery_ids,
    reset_archive_markers,
)
from tailorcv.application.discovery.posting_fetch import FetchedPosting
from tailorcv.application.discovery.posting_refresh import PostingRefreshService
from tailorcv.application.discovery.scan_staging.absence import (
    AbsenceCheck,
    WithdrawingRead,
    withdraw_absent_in_session,
)
from tailorcv.application.discovery.scan_staging.capture import ListingCapture
from tailorcv.application.discovery.scan_staging.checks import PublisherCheckWrites
from tailorcv.application.discovery.scan_staging.families import place_unlinked
from tailorcv.application.discovery.scan_staging.found import (
    FoundPostingStageResult,
    insert_found_pending,
    requeue_pending,
)
from tailorcv.application.discovery.scan_staging.matching import (
    known_row,
    listed_row_ids,
    requisition_holder,
    requisition_keys,
    rows_by_requisition,
    staged_rows,
    tracked_job,
)
from tailorcv.application.discovery.scan_staging.publisher_facts import name_employer_from_url
from tailorcv.application.discovery.scan_staging.rows import ListingRows
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.evaluation_targets import remember_posting_url
from tailorcv.application.tasks import ShieldedTaskContext, TaskContext
from tailorcv.domain import PostingContentAccessStatus, utc_now
from tailorcv.domain.identity import normalize_url, url_sha256
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    JobPosting,
    PendingDiscovery,
    PostingUrl,
    Source,
)
from tailorcv.infrastructure.web_fetch.policy import FetchPolicyError, fetch_refusal
from tailorcv.shared.errors import TailorcvError

logger = logging.getLogger(__name__)


_REFRESH_FAILURES = (TailorcvError, FetchPolicyError, httpx.HTTPError)


@dataclass(frozen=True)
class StagedListings:
    """Listings one staging pass inserted, and known rows it requeued."""

    inserted: list[SourceListing]
    requeued: list[SourceListing]
    # A listing URL maps to the unarchived row it named, however staging matched it.
    row_ids_by_url: dict[str, str] = field(default_factory=dict)
    refresh_failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def queued(self) -> list[SourceListing]:
        """Every listing this pass placed in the review queue awaiting a decision."""
        return [*self.inserted, *self.requeued]

    def row_ids(self, listings: Iterable[SourceListing]) -> list[str]:
        """Return the rows these listings named, once each, in listing order."""
        return list(
            dict.fromkeys(
                self.row_ids_by_url[item.url]
                for item in listings
                if item.url in self.row_ids_by_url
            )
        )


def shield_progress(context: TaskContext | None) -> TaskContext | None:
    """Keep a nested call's progress out of the outer task, when there is one."""
    return None if context is None else ShieldedTaskContext(context)


class StagingBusyError(Exception):
    """Another staging write held the turn for longer than the caller could wait."""


def _tracked_publisher_read(listing: SourceListing) -> FetchedPosting | None:
    if (
        not listing.published_description.strip()
        or listing.source is None
        or not listing.facts.publisher_vouched
    ):
        return None
    return FetchedPosting(
        text=listing.published_description,
        posting_status="active",
        method="static_structured",
        fetched_at=utc_now(),
        http_status=None,
        publisher_markup_present=True,
        title=listing.title,
        employer=listing.employer,
        final_url=listing.url,
        robots_basis=None,
        source=listing.source,
        publisher_closing_date=listing.facts.closing_date,
    )


class DiscoveryStaging:
    """Own the review queue's writes and the withdrawal a complete read implies."""

    def protected_posting_urls(self, workspace_id: str) -> frozenset[str]:
        """Collect dismissed discovery and retired tracked URL identities before reads."""
        from tailorcv.application.discovery.dispositions import DiscoveryDispositionService

        with self.database.session() as session:
            dismissed = DiscoveryDispositionService(self.database).person_dismissed_ids_in_session(
                session, workspace_id
            )
            pending_urls = session.execute(
                select(PendingDiscovery.canonical_url, PendingDiscovery.discovery_url).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.id.in_(dismissed),
                )
            )
            urls = {url for row in pending_urls for url in row if url}
            urls.update(
                session.scalars(
                    select(PostingUrl.url)
                    .join(JobPosting, JobPosting.id == PostingUrl.job_posting_id)
                    .where(
                        JobPosting.workspace_id == workspace_id,
                        JobPosting.archived_at.is_not(None),
                    )
                )
            )
            return frozenset(urls)

    def __init__(self, database: Database, postings: PostingRefreshService) -> None:
        """Bind staging to one durable database and the posting refresh it defers to."""
        self.database = database
        self.postings = postings
        self.rows = ListingRows(ListingCapture(postings))
        self.checks = PublisherCheckWrites(database)
        # A board scan and the worker's continuation slots share one instance across threads.
        self._write_turn = Lock()

    @contextmanager
    def write_turn(self, wait_seconds: float = math.inf) -> Iterator[None]:
        """Hold the one staging write turn, raising StagingBusyError after `wait_seconds`."""
        timeout = -1 if math.isinf(wait_seconds) else max(0.0, wait_seconds)
        if not self._write_turn.acquire(timeout=timeout):
            raise StagingBusyError("Another scan was still staging when this task ran out of time.")
        try:
            yield
        finally:
            self._write_turn.release()

    def partition_scan_listings(
        self, workspace_id: str, listings: list[SourceListing], *, new_limit: int
    ) -> tuple[list[SourceListing], list[SourceListing], list[SourceListing]]:
        """Keep new discoveries bounded while letting active rows refresh their facts."""
        with self.database.session() as session:
            rows = session.execute(
                select(
                    PendingDiscovery.fingerprint,
                    PendingDiscovery.discovery_url,
                    PendingDiscovery.canonical_url,
                ).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.archived_at.is_(None),
                )
            ).all()
        active = {row.fingerprint for row in rows} | {
            url_sha256(row.canonical_url) for row in rows if row.canonical_url
        }
        active_reqs = requisition_keys(
            url for row in rows for url in (row.discovery_url, row.canonical_url)
        )
        new: list[SourceListing] = []
        refreshable: list[SourceListing] = []
        omitted: list[SourceListing] = []
        for listing in listings:
            if url_sha256(listing.url) in active or requisition_keys([listing.url]) & active_reqs:
                refreshable.append(listing)
            elif len(new) < new_limit:
                new.append(listing)
            else:
                omitted.append(listing)
        return new, refreshable, omitted

    def unstaged_listings(
        self, workspace_id: str, listings: list[SourceListing]
    ) -> list[SourceListing]:
        """Return the listings staging would insert as new rows, matched as staging matches.

        A listing naming a tracked job or any staged row, archived ones included, is left out.
        """
        with self.database.session() as session:
            known, at_address = staged_rows(session, workspace_id)
            at_requisition = rows_by_requisition(known.values())
            claimed: set[str] = set()
            unstaged: list[SourceListing] = []
            for raw_listing in listings:
                listing = backfilled_listing(raw_listing)
                tracked = tracked_job(
                    session,
                    workspace_id,
                    listing.url,
                    employer=listing.employer,
                    requisition_id=listing.requisition_id,
                    include_closed=True,
                )
                if tracked is not None:
                    continue
                pending, _by_address = known_row(
                    known, at_address, at_requisition, claimed, url_sha256(listing.url), listing.url
                )
                if pending is not None:
                    claimed.add(pending.id)
                    continue
                unstaged.append(raw_listing)
            return unstaged

    def row_ids_at_listed_urls(
        self, workspace_id: str, listings: list[SourceListing]
    ) -> dict[str, str]:
        """Map each listing URL to the unarchived row staged at exactly that URL."""
        by_url = {url_sha256(item.url): item.url for item in listings}
        if not by_url:
            return {}
        with self.database.session() as session:
            rows = session.execute(
                select(PendingDiscovery.fingerprint, PendingDiscovery.id).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.fingerprint.in_(by_url),
                    PendingDiscovery.archived_at.is_(None),
                )
            ).tuples()
            return {by_url[fingerprint]: row_id for fingerprint, row_id in rows}

    def stage_listings(
        self,
        workspace_id: str,
        listings: list[SourceListing],
        today: date,
        *,
        reason: str,
        source_id: str | None = None,
        context: TaskContext | None = None,
        refresh_only: bool = False,
        wait_seconds: float = math.inf,
        require_source_read: Callable[[Session], None] | None = None,
    ) -> StagedListings:
        """Stage listings in one owned transaction under the write turn, then refresh jobs."""
        refreshes: list[tuple[str, SourceListing]] = []
        with self.write_turn(wait_seconds), self.database.exclusive_session() as session:
            if require_source_read is not None:
                require_source_read(session)
            source = session.get(Source, source_id) if source_id is not None else None
            board_url = source_read_address(source.url, source.resolved_url)[0] if source else ""
            staged = self.stage_listings_in_session(
                workspace_id,
                listings,
                today,
                reason=reason,
                source_id=source_id,
                session=session,
                refreshes=refreshes,
                refresh_only=refresh_only,
                correlation_id=context.task_id if context is not None else None,
            )
        failed = self.refresh_tracked(
            workspace_id,
            refreshes,
            context,
            board_url=board_url,
            require_source_read=require_source_read,
        )
        return replace(staged, refresh_failed=failed) if failed else staged

    def stage_listings_in_session(
        self,
        workspace_id: str,
        listings: list[SourceListing],
        today: date,
        *,
        reason: str,
        source_id: str | None = None,
        correlation_id: str | None = None,
        session: Session,
        refreshes: list[tuple[str, SourceListing]],
        refresh_only: bool = False,
    ) -> StagedListings:
        """Stage unseen listings and collect tracked jobs for post-commit refresh.

        Report inserted and requeued listings separately. With ``refresh_only``
        nothing inserts and nothing requeues, so the listings that refreshed a
        tracked job or a known row come back as ``inserted``. Such a pass matches a row
        only by its own or its read address, since a held-out listing sharing a
        requisition may be a regional sibling of a wanted row.
        """
        if not listings:
            return StagedListings([], [])
        corrected = corrected_title_ids(session, workspace_id)
        dismissed = set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))
        # A marked family, like a person's restore, never folds under a dismissed head.
        restored = set(
            session.scalars(person_restored_pending_discovery_ids(workspace_id))
        ) | marked_family_ids(session, workspace_id)
        known, at_address = staged_rows(session, workspace_id)
        at_requisition = {} if refresh_only else rows_by_requisition(known.values())
        claimed: set[str] = set()
        reset_markers = reset_archive_markers(
            session, workspace_id, (pending.id for pending in known.values())
        )
        new_listings: list[SourceListing] = []
        requeued_listings: list[SourceListing] = []
        unplaced: list[tuple[PendingDiscovery, SourceListing]] = []
        known_listings: list[SourceListing] = []
        tracked: dict[str, SourceListing] = {}
        row_ids: dict[str, str] = {}
        for raw_listing in listings:
            listing = backfilled_listing(raw_listing)
            fingerprint = url_sha256(listing.url)
            existing_job = tracked_job(
                session,
                workspace_id,
                listing.url,
                employer=listing.employer,
                requisition_id=listing.requisition_id,
                include_closed=True,
            )
            if existing_job is not None:
                closed = existing_job.archived_at is not None
                # A closed job keeps the twin on file, so later reads protect it, and stays unread.
                remember_posting_url(
                    session,
                    existing_job,
                    url=listing.url,
                    source_id=source_id,
                    url_type="discovery",
                    make_primary=not closed,
                    actor="worker",
                    origin="discovery.board_scan",
                    correlation_id=correlation_id,
                )
                if not closed:
                    tracked[str(existing_job.id)] = listing
                known_listings.append(listing)
                continue
            pending, by_address = known_row(
                known, at_address, at_requisition, claimed, fingerprint, listing.url
            )
            if pending is not None:
                claimed.add(pending.id)
                requeued = self.rows.refresh_known_pending(
                    session,
                    workspace_id,
                    pending,
                    listing,
                    known,
                    corrected,
                    dismissed,
                    reason=reason,
                    source_id=source_id,
                    correlation_id=correlation_id,
                    reset_event_id=(reset_markers.get(pending.id) if not refresh_only else None),
                    restored=restored,
                    today=today,
                    by_address=by_address,
                )
                (requeued_listings if requeued else known_listings).append(listing)
            elif refresh_only:
                continue
            else:
                pending = self.rows.insert_listing(
                    session,
                    workspace_id,
                    listing,
                    today,
                    known,
                    dismissed,
                    reason=reason,
                    source_id=source_id,
                    correlation_id=correlation_id,
                )
                new_listings.append(listing)
            if pending.regional_variant_of is None:
                unplaced.append((pending, listing))
            if pending.archived_at is None:
                row_ids.setdefault(listing.url, pending.id)
        place_unlinked(session, unplaced, known, dismissed, restored, correlation_id=correlation_id)
        refreshes.extend(tracked.items())
        if refresh_only:
            return StagedListings(known_listings, [], row_ids)
        return StagedListings(new_listings, requeued_listings, row_ids)

    def refresh_tracked(
        self,
        workspace_id: str,
        refreshes: list[tuple[str, SourceListing]],
        context: TaskContext | None = None,
        *,
        board_url: str = "",
        require_source_read: Callable[[Session], None] | None = None,
    ) -> list[dict[str, str]]:
        """Refresh tracked jobs once staging commits, returning the ones that failed."""
        nested = shield_progress(context)
        failed: list[dict[str, str]] = []
        for job_id, listing in refreshes:
            try:
                self.postings.refresh(
                    workspace_id,
                    job_id,
                    nested,
                    title_hint=listing.title,
                    location_hint=listing.location,
                    board_url=board_url,
                    require_source_read=require_source_read,
                    prefetched=_tracked_publisher_read(listing),
                )
            except _REFRESH_FAILURES as error:
                logger.warning(
                    "tracked_refresh",
                    extra={"job_posting_id": job_id, "error_type": type(error).__name__},
                )
                failed.append(
                    {
                        "job_posting_id": job_id,
                        "error_category": type(error).__name__,
                        "error_message": str(error),
                    }
                )
        return failed

    def stage_found_posting(
        self,
        workspace_id: str,
        url: str,
        today: date,
        *,
        title: str = "",
        employer: str = "",
        location: str = "",
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
        publisher_check: PostingCheck | None = None,
        content_status: PostingContentAccessStatus | None = None,
        content_note: str | None = None,
        derive_url_hints: bool = True,
        from_board_read: bool = False,
        existing_session: Session | None = None,
    ) -> FoundPostingStageResult | None:
        """Stage or requeue a posting in one transaction; None when it is already known.

        No source is named, so no later board read withdraws it. What the posting
        states is read when it is evaluated, and whatever the person typed is a
        hint until then. ``from_board_read`` says the caller took this listing off a
        board page it read, which the queue row states.
        """
        refusal = fetch_refusal(url)
        if refusal:
            raise ValidationError(f"That address cannot be read: {refusal}.")
        fingerprint = url_sha256(url)
        normalized_url = normalize_url(url)
        with (
            self.database.session() if existing_session is None else nullcontext(existing_session)
        ) as session:
            if tracked_job(session, workspace_id, normalized_url) is not None:
                return None
            pending = session.scalar(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.fingerprint == fingerprint,
                )
            ) or requisition_holder(session, workspace_id, url)
            if pending is not None:
                if derive_url_hints:
                    name_employer_from_url(pending, url)
                reset_event_id = reset_archive_markers(session, workspace_id, [pending.id]).get(
                    pending.id
                )
                return requeue_pending(
                    session,
                    pending,
                    publisher_check=publisher_check,
                    content_status=content_status,
                    content_note=content_note,
                    workspace_id=workspace_id,
                    actor=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                    reset_event_id=reset_event_id,
                    dismissed=set(session.scalars(dismissed_pending_discovery_ids(workspace_id))),
                    today=today,
                    staged_url=url,
                )
            return insert_found_pending(
                session,
                workspace_id=workspace_id,
                fingerprint=fingerprint,
                url=url,
                today=today,
                publisher_check=publisher_check,
                content_status=content_status,
                content_note=content_note,
                title=title,
                employer=employer,
                location=location,
                derive_url_hints=derive_url_hints,
                allow_nested=existing_session is None,
                origin=origin,
                from_board_read=from_board_read,
            )

    def open_rows_at(self, workspace_id: str, source_id: str, urls: Sequence[str]) -> set[str]:
        """Return this source's open, unarchived rows that ``urls`` name."""
        if not urls:
            return set()
        with self.database.session() as session:
            staged = session.scalars(
                select(PendingDiscovery).where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.source_id == source_id,
                    PendingDiscovery.archived_at.is_(None),
                    PendingDiscovery.withdrawn_at.is_(None),
                )
            ).all()
            return listed_row_ids(staged, urls)

    def withdraw_absent(
        self,
        workspace_id: str,
        source_id: str,
        present_urls: Iterable[str],
        today: date,
        *,
        correlation_id: str | None = None,
        require_source_read: Callable[[Session], None] | None = None,
        read: WithdrawingRead | None = None,
        check_absent: AbsenceCheck | None = None,
    ) -> list[str]:
        """Mark this source's staged openings that its complete coverage no longer lists."""
        with self.database.exclusive_session() as session:
            if require_source_read is not None:
                require_source_read(session)
            return withdraw_absent_in_session(
                session,
                workspace_id,
                source_id,
                present_urls,
                today,
                correlation_id=correlation_id,
                read=read,
                check_absent=check_absent,
            )
