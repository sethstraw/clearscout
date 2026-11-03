"""A posting staged from a link: a new row, or a requeue of an archived one."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_slugs import (
    backfill_employer_from_url,
    backfill_title_from_url,
)
from clearscout.application.discovery.pending import corrected_title_ids
from clearscout.application.discovery.scan_staging.families import (
    clear_regional_variant_family_if_identity_changed,
    release_from_hidden_head,
)
from clearscout.application.discovery.scan_staging.matching import locale_variant_of_english
from clearscout.application.discovery.scan_staging.publisher_facts import (
    apply_publisher_check,
    publisher_staged_facts,
)
from clearscout.application.discovery.scan_staging.reasons import found_posting_reason
from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import PostingContentAccessStatus, new_id
from clearscout.domain.staged_facts import (
    ListingIdentityTrust,
    StagedListingFacts,
    merged_posted_date,
)
from clearscout.infrastructure.persistence.models import AuditEvent, PendingDiscovery
from clearscout.infrastructure.web_fetch.listing_facts import stated_place
from clearscout.shared.text import sanitize

_PENDING_FINGERPRINT_CONSTRAINTS = (
    "uq_pending_fingerprint",
    "pending_discoveries.workspace_id, pending_discoveries.fingerprint",
)


@dataclass(frozen=True)
class FoundPostingStageResult:
    """Identity and outcome of one manual staging transaction."""

    pending_discovery_id: str
    requeued: bool


def requeue_pending(
    session: Session,
    pending: PendingDiscovery,
    *,
    publisher_check: PostingCheck | None,
    content_status: PostingContentAccessStatus | None,
    content_note: str | None,
    workspace_id: str,
    actor: str,
    origin: str,
    correlation_id: str | None,
    reset_event_id: str | None,
    dismissed: Collection[str],
    today: date,
    staged_url: str = "",
) -> FoundPostingStageResult | None:
    """Requeue an archived row a reset marker released; None when it stays put."""
    if pending.archived_at is None or reset_event_id is None:
        return None
    pending.archived_at = None
    pending.withdrawn_at = None
    details: dict[str, str] = {
        "discovery_url": pending.discovery_url,
        "reset_event_id": reset_event_id,
    }
    release_from_hidden_head(session, pending, dismissed, details)
    if content_status is not None and pending.current_content_id is None:
        pending.content_status = content_status
        pending.content_note = content_note or ""
    if publisher_check is not None and publisher_check.status == "readable":
        prior_identity = (pending.employer, pending.title)
        apply_publisher_check(
            pending,
            publisher_check,
            read_on=today,
            keep_title=pending.id in corrected_title_ids(session, workspace_id, [pending.id]),
            # Another locale's page names the opening in that locale's words.
            keep_names=locale_variant_of_english(pending, staged_url),
        )
        clear_regional_variant_family_if_identity_changed(session, pending, prior_identity)
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action="pending_discovery.requeued",
            entity_type="pending_discovery",
            entity_id=pending.id,
            details=details,
            **audit_columns(actor, origin, correlation_id),
        )
    )
    session.flush()
    return FoundPostingStageResult(pending.id, requeued=True)


def insert_found_pending(
    session: Session,
    *,
    workspace_id: str,
    fingerprint: str,
    url: str,
    today: date,
    publisher_check: PostingCheck | None,
    content_status: PostingContentAccessStatus | None,
    content_note: str | None,
    title: str,
    employer: str,
    location: str,
    derive_url_hints: bool,
    allow_nested: bool,
    origin: str,
    from_board_read: bool,
) -> FoundPostingStageResult | None:
    """Insert a found posting as a new row; None when a row already holds its address."""
    employer_hint = employer or (backfill_employer_from_url(url) if derive_url_hints else "") or ""
    title_hint = title or (backfill_title_from_url(url) if derive_url_hints else "") or ""
    publisher = (
        publisher_check
        if publisher_check is not None and publisher_check.status == "readable"
        else None
    )
    staged_facts = (
        publisher_staged_facts(publisher, employer_hint, title_hint, location)
        if publisher is not None
        else StagedListingFacts(identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
    )
    staged_facts = replace(
        staged_facts, posted_date=merged_posted_date(None, staged_facts.posted_date, today)
    )
    pending = PendingDiscovery(
        id=new_id(),
        workspace_id=workspace_id,
        fingerprint=fingerprint,
        employer=sanitize((publisher.employer if publisher is not None else "") or employer_hint)[
            :300
        ],
        title=sanitize((publisher.title if publisher is not None else "") or title_hint)[:500],
        location=stated_place(
            sanitize((publisher.location if publisher is not None else "") or location)
        )[:500],
        staged_facts=staged_facts.as_json(),
        requisition_id="",
        discovery_url=url,
        canonical_url=url,
        content_status=content_status or PostingContentAccessStatus.NOT_YET_READ,
        content_note=content_note or "",
        reason=found_posting_reason(origin, from_board_read=from_board_read),
        first_seen_on=today,
        source_id=None,
        regional_variant_of=None,
        origin=origin,
    )
    if not allow_nested:
        session.add(pending)
        session.flush()
        return FoundPostingStageResult(pending.id, requeued=False)
    try:
        with session.begin_nested():
            session.add(pending)
            session.flush()
    except IntegrityError as exc:
        message = str(exc.orig).casefold()
        if not any(marker in message for marker in _PENDING_FINGERPRINT_CONSTRAINTS):
            raise
        return None
    return FoundPostingStageResult(pending.id, requeued=False)
