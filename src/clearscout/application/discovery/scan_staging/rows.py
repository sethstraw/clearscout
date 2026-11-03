"""Listings inserted as new rows, and the known rows a listing refreshes."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from sqlalchemy.orm import Session

from clearscout.application.discovery.scan_staging.capture import ListingCapture
from clearscout.application.discovery.scan_staging.families import (
    clear_regional_variant_family_if_identity_changed,
    fold_family,
    owned_by_person,
    record_relisted,
    regional_head_of,
    staged_before,
)
from clearscout.application.discovery.scan_staging.found import requeue_pending
from clearscout.application.discovery.scan_staging.matching import locale_variant_of_english
from clearscout.application.discovery.scan_staging.publisher_facts import (
    hint_still_used,
    with_silent_fields_kept,
)
from clearscout.application.discovery.scan_staging.reasons import staged_by_person
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.domain import new_id
from clearscout.domain.identity import url_sha256
from clearscout.domain.records import SourceKind
from clearscout.domain.staged_facts import (
    ListingIdentityTrust,
    PublisherClosingDate,
    StagedListingFacts,
    merged_posted_date,
)
from clearscout.infrastructure.persistence.models import PendingDiscovery, Source
from clearscout.infrastructure.web_fetch.listing_facts import stated_place
from clearscout.shared.text import sanitize


def _restate_names(pending: PendingDiscovery, listing: SourceListing, *, keep_title: bool) -> None:
    """Rewrite a row's names and place from a fresh read, keeping a field the read leaves blank."""
    pending.employer = sanitize(listing.employer)[:300] or pending.employer
    if not keep_title:
        pending.title = sanitize(listing.title)[:500] or pending.title
    pending.location = _bounded_places(sanitize(listing.location)) or pending.location


def _bounded_places(location: str) -> str:
    """Fit a "; "-joined place list in the column without cutting a place in half."""
    place = stated_place(location)
    if len(place) <= 500:
        return place
    cut = place.rfind("; ", 0, 501)
    return place[:cut] if cut > 0 else place[:500]


def _refresh_content_projection(pending: PendingDiscovery, listing: SourceListing) -> None:
    """Publisher metadata cannot supersede a retained body-read attempt."""
    if pending.current_content_id is None:
        pending.content_status = listing.content_status
        pending.content_note = listing.content_note


def _carried_closing_date(
    listing: SourceListing, prior: StagedListingFacts
) -> PublisherClosingDate | None:
    """Keep a prior closing date a listing is silent on, unless it was a board's listing end."""
    if listing.facts.closing_date is not None:
        return listing.facts.closing_date
    # A listing end speaks only for the board that stated it, so another read never inherits it.
    if prior.closing_date is not None and prior.closing_date.states_listing_end:
        return None
    return prior.closing_date


def _kind_of(session: Session, source_id: str | None) -> str | None:
    source = session.get(Source, source_id) if source_id is not None else None
    return source.source_type if source is not None else None


def _employer_board_keeps(
    session: Session, pending: PendingDiscovery, source_id: str | None
) -> bool:
    return (
        pending.source_id != source_id
        and _kind_of(session, source_id) == SourceKind.AGGREGATOR.value
        and _kind_of(session, pending.source_id) == SourceKind.EMPLOYER_BOARD.value
    )


class ListingRows:
    """Insert unseen listings as rows and refresh the known rows a listing names."""

    def __init__(self, capture: ListingCapture) -> None:
        """Bind row writes to the capture that retains each listing's bytes."""
        self.capture = capture

    @staticmethod
    def _follow_listing(
        pending: PendingDiscovery,
        listing: SourceListing,
        source_id: str | None,
        *,
        by_address: bool,
    ) -> None:
        pending.requisition_id = listing.requisition_id[:300] or pending.requisition_id
        pending.absent_since = None
        if not by_address:
            # A row a content read pointed elsewhere keeps that address; one on its listing
            # follows it.
            if pending.canonical_url in ("", pending.discovery_url):
                pending.canonical_url = listing.url
            pending.discovery_url = listing.url
        pending.source_id = source_id

    def refresh_known_pending(
        self,
        session: Session,
        workspace_id: str,
        pending: PendingDiscovery,
        listing: SourceListing,
        known: dict[str, PendingDiscovery],
        corrected: set[str],
        dismissed: set[str],
        *,
        reason: str,
        source_id: str | None,
        correlation_id: str | None,
        reset_event_id: str | None,
        restored: set[str],
        today: date,
        by_address: bool = False,
    ) -> bool:
        """Refresh a known row and report whether a reset marker requeued it.

        A row matched only by the address a content read gave it takes the listing's
        facts and source, and keeps the listing address it was staged from. An aggregator
        listing refreshes an employer board's facts but leaves its source, address, close,
        requisition, and listed state.
        """
        board_keeps = _employer_board_keeps(session, pending, source_id)
        if board_keeps:
            # A listing end speaks only for the board that stated it, so its capture drops it too.
            listing = replace(listing, facts=replace(listing.facts, closing_date=None))
        else:
            # The owning employer board's own reads decide whether its row is listed.
            record_relisted(
                session,
                pending,
                action="pending_discovery.relisted",
                workspace_id=workspace_id,
                actor="worker",
                origin="source.scan",
                correlation_id=correlation_id,
                dismissed=dismissed,
            )
        prior_identity = (pending.employer, pending.title)
        retained_unverified_identity = not StagedListingFacts.from_json(
            pending.staged_facts
        ).publisher_vouched and hint_still_used(
            listing, pending.employer, pending.title, pending.location
        )
        # Another locale's listing names the opening in that locale's words.
        if not locale_variant_of_english(pending, listing.url):
            _restate_names(pending, listing, keep_title=pending.id in corrected)
        prior_facts = StagedListingFacts.from_json(pending.staged_facts)
        pending.staged_facts = replace(
            with_silent_fields_kept(listing.facts, prior_facts),
            closing_date=(
                prior_facts.closing_date
                if board_keeps
                else _carried_closing_date(listing, prior_facts)
            ),
            identity_trust=(
                ListingIdentityTrust.UNVERIFIED_HINT
                if retained_unverified_identity
                else listing.facts.identity_trust
            ),
            posted_date=merged_posted_date(
                prior_facts.posted_date, listing.facts.posted_date, today
            ),
        ).as_json()
        if not board_keeps:
            self._follow_listing(pending, listing, source_id, by_address=by_address)
        _refresh_content_projection(pending, listing)
        # The found sentence is what staging reads to tell that a person added the row.
        if not staged_by_person(pending.reason):
            pending.reason = reason
        # A requeue releases the row from a head it may no longer fold under, so it runs first.
        requeued = (
            requeue_pending(
                session,
                pending,
                publisher_check=None,
                content_status=None,
                content_note=None,
                workspace_id=workspace_id,
                actor="worker",
                origin="source.scan",
                correlation_id=correlation_id,
                reset_event_id=reset_event_id,
                dismissed=dismissed,
                today=today,
            )
            is not None
        )
        clear_regional_variant_family_if_identity_changed(session, pending, prior_identity)
        if pending.regional_variant_of is None:
            head = regional_head_of(
                listing,
                staged_before(pending, known.values(), dismissed),
                pending.source_id,
                dismissed,
                person_owned=owned_by_person(pending, restored),
            )
            if head is not None:
                fold_family(
                    session,
                    pending,
                    head,
                    known.values(),
                    dismissed,
                    restored,
                    correlation_id=correlation_id,
                )
        self.capture.capture_listing_description(
            session, workspace_id, pending, listing, correlation_id=correlation_id
        )
        return requeued

    def insert_listing(
        self,
        session: Session,
        workspace_id: str,
        listing: SourceListing,
        today: date,
        known: dict[str, PendingDiscovery],
        dismissed: set[str],
        *,
        reason: str,
        source_id: str | None,
        correlation_id: str | None,
    ) -> PendingDiscovery:
        """Stage an unseen listing as a new row and index it under its fingerprint."""
        fingerprint = url_sha256(listing.url)
        pending = PendingDiscovery(
            id=new_id(),
            workspace_id=workspace_id,
            fingerprint=fingerprint,
            employer=sanitize(listing.employer)[:300],
            title=sanitize(listing.title)[:500],
            location=_bounded_places(sanitize(listing.location)),
            staged_facts=replace(
                listing.facts,
                posted_date=merged_posted_date(None, listing.facts.posted_date, today),
            ).as_json(),
            requisition_id=listing.requisition_id[:300],
            discovery_url=listing.url,
            canonical_url=listing.url,
            content_status=listing.content_status,
            content_note=listing.content_note,
            reason=reason,
            first_seen_on=today,
            source_id=source_id,
            origin="source.scan",
            correlation_id=correlation_id,
            regional_variant_of=regional_head_of(listing, known.values(), source_id, dismissed),
        )
        session.add(pending)
        self.capture.capture_listing_description(
            session, workspace_id, pending, listing, correlation_id=correlation_id
        )
        known[fingerprint] = pending
        return pending
