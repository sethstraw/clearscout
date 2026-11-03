"""What a new read keeps of its row's retained body, and the address it adopts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_slugs import employer_name, publisher_spelling_of_host
from clearscout.application.discovery.content_resolution.results import (
    LISTING_PAGE_NOTE,
    ResolvedPostingContent,
)
from clearscout.application.discovery.content_resolution.targets import AddressHolder, StoredTarget
from clearscout.application.discovery.posting_identity import keeps_stored_layout
from clearscout.application.discovery.posting_text import lists_many_postings
from clearscout.application.job_search.evaluation_targets import find_job_by_durable_identifier
from clearscout.domain import PostingContentAccessStatus, PostingIdentityStatus, PostingStatus
from clearscout.domain.identity import normalize_url, url_host, url_sha256
from clearscout.domain.records import SourceKind
from clearscout.domain.staged_facts import PublisherClosingDate, StagedListingFacts
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    PendingDiscovery,
    PendingPostingContent,
    Source,
)
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import spells_the_page_host
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.platforms.lever import wrapped_lever_job
from clearscout.shared.text import sanitize


def retain_date_without_body(
    result: ResolvedPostingContent, previous: PendingPostingContent | None
) -> ResolvedPostingContent:
    """Carry the previous closing date into a bodiless read of the same or a closed posting."""
    if (
        result.content is None
        and previous is not None
        and previous.publisher_closing_date is not None
        and (
            result.posting_status is PostingStatus.CLOSED
            or (
                previous.requested_url == result.requested_url
                and result.identity_status is not PostingIdentityStatus.CONTRADICTED
            )
        )
    ):
        return replace(
            result,
            publisher_closing_date=PublisherClosingDate.from_json(previous.publisher_closing_date),
        )
    return result


def identity_of_retained_body(result: ResolvedPostingContent) -> ResolvedPostingContent:
    """Contradict an attempt whose retained body is a page of job cards, not one posting."""
    if result.content is None or result.identity_status is not PostingIdentityStatus.VERIFIED:
        return result
    if not lists_many_postings(result.content.decode("utf-8", errors="replace")):
        return result
    return replace(
        result,
        identity_status=PostingIdentityStatus.CONTRADICTED,
        identity_evidence={**(result.identity_evidence or {}), "lists_many_postings": True},
        note=LISTING_PAGE_NOTE,
    )


def relaid(stored: bytes, result: ResolvedPostingContent) -> ResolvedPostingContent:
    """Return the read to store, keeping stored bytes when it says their words no richer."""
    if result.content is None or not stored:
        return result
    fresh = result.content.decode("utf-8", errors="ignore")
    if keeps_stored_layout(stored.decode("utf-8", errors="ignore"), fresh):
        return replace(result, content=stored)
    return result


def with_retained_body_recovery(
    result: ResolvedPostingContent, previous: PendingPostingContent | None, stored: bytes
) -> ResolvedPostingContent:
    """Record an unavailable retained body and the result of its recovery read."""
    if previous is None or stored:
        return result
    recovery = "The retained posting body is unavailable. " + (
        "Recovered from fresh posting content."
        if result.content is not None
        else "Retry the posting read or provide its text."
    )
    return replace(
        result,
        note=f"{result.note} {recovery}".strip(),
        identity_evidence={
            **(result.identity_evidence or {}),
            "retained_body_recovery": recovery,
        },
    )


def latest_bodied(
    session: Session, current: PendingPostingContent | None
) -> PendingPostingContent | None:
    """Return the newest content row in this chain that retained a body, past bodiless reads."""
    row = current
    while row is not None and row.artifact_id is None:
        earlier = row.supersedes_content_id
        row = session.get(PendingPostingContent, earlier) if earlier is not None else None
    return row


def keeps_readable_body(
    previous: PendingPostingContent | None, result: ResolvedPostingContent, stored: bytes
) -> bool:
    """Return whether a bodiless, unclosed re-read of one URL leaves its readable body current.

    A read that landed on some other page contradicts the body, so it replaces it.
    """
    return (
        bool(stored)
        and previous is not None
        and previous.artifact_id is not None
        and PostingContentAccessStatus(previous.content_status)
        is PostingContentAccessStatus.READABLE
        and previous.requested_url == result.requested_url
        and result.content is None
        and result.posting_status is not PostingStatus.CLOSED
        and result.identity_status is not PostingIdentityStatus.CONTRADICTED
    )


def current_content(session: Session, target: StoredTarget) -> PendingPostingContent | None:
    """Return the content row a pending posting pointed at when its read began."""
    if not target.current_content_id:
        return None
    return session.get(PendingPostingContent, target.current_content_id)


def take_read_identity(pending: PendingDiscovery, result: ResolvedPostingContent) -> None:
    """Fill a blank title and employer from what a verified read of the posting states."""
    if result.identity_status is not PostingIdentityStatus.VERIFIED or result.content is None:
        return
    evidence = result.identity_evidence or {}
    title = evidence.get("publisher_title")
    if not pending.title.strip() and isinstance(title, str) and title.strip():
        pending.title = sanitize(title)[:500]
    stated = evidence.get("publisher_employer")
    stated = sanitize(stated) if isinstance(stated, str) else ""
    if spells_the_page_host(stated, result.final_url or result.requested_url):
        stated = ""
    text = result.content.decode("utf-8", errors="ignore")
    employer, tenant_only = employer_name(stated, pending.discovery_url, text)
    # A slug-only name would downgrade a vouched row's trust; the refresh owns that hint.
    if not pending.employer and employer and not tenant_only:
        pending.employer = employer[:300]
    # The site's own name stays a hint, so only a row that is already a hint takes it.
    if (
        not pending.employer
        and result.source is not None
        and not StagedListingFacts.from_json(pending.staged_facts).publisher_vouched
    ):
        page_url = result.final_url or result.requested_url
        page_title = inspect_publisher_page(page_url, result.source.page_html).page_title
        pending.employer = (publisher_spelling_of_host(page_url, page_title) or "")[:300]


def take_wrapped_lever_identity(pending: PendingDiscovery, result: ResolvedPostingContent) -> None:
    """Give a row stating no requisition the Lever id of the posting its read page wraps."""
    if pending.requisition_id.strip() or result.source is None:
        return
    pending.requisition_id = wrapped_lever_job(
        result.final_url or result.requested_url, result.source.page_html
    )


def body_backed(result: ResolvedPostingContent) -> bool:
    """Return whether this attempt retained the body of a verified, active posting."""
    return (
        result.content is not None
        and result.identity_status is PostingIdentityStatus.VERIFIED
        and result.posting_status is PostingStatus.ACTIVE
    )


def _address_holder(
    session: Session, workspace_id: str, pending_id: str, url: str
) -> AddressHolder | None:
    """Return another pending row or tracked posting in the workspace that holds this address."""
    wanted = normalize_url(url)
    fingerprint = url_sha256(url)
    host = url_host(wanted)
    # LIKE only narrows by host; the normalized comparison below decides.
    candidates = session.execute(
        select(
            PendingDiscovery.id,
            PendingDiscovery.fingerprint,
            PendingDiscovery.canonical_url,
            PendingDiscovery.discovery_url,
        )
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.id != pending_id,
            or_(
                PendingDiscovery.canonical_url.contains(host, autoescape=True),
                PendingDiscovery.discovery_url.contains(host, autoescape=True),
                PendingDiscovery.fingerprint == fingerprint,
            ),
        )
        .order_by(PendingDiscovery.created_at, PendingDiscovery.id)
    ).all()
    for other_id, other_fingerprint, canonical_url, discovery_url in candidates:
        if other_fingerprint == fingerprint or wanted in {
            normalize_url(canonical_url),
            normalize_url(discovery_url),
        }:
            return AddressHolder("pending_discovery", other_id)
    job = find_job_by_durable_identifier(
        session,
        workspace_id=workspace_id,
        employer="",
        requisition_id="",
        canonical_url=url,
        discovery_url="",
        active_only=True,
    )
    return None if job is None else AddressHolder("job_posting", job.id)


def repoint_to_read_address(
    session: Session,
    workspace_id: str,
    pending: PendingDiscovery,
    result: ResolvedPostingContent,
    *,
    evidence_id: str,
    audit: dict[str, Any],
) -> AddressHolder | None:
    """Point the row at the address this read verified its posting at, unless another holds it."""
    previous_url = pending.canonical_url or pending.discovery_url
    if not body_backed(result) or result.requested_url == previous_url:
        return None
    listed_by = session.get(Source, pending.source_id) if pending.source_id else None
    # An aggregator's terms bind its listings to the apply address it supplied.
    if listed_by is not None and listed_by.source_type == SourceKind.AGGREGATOR.value:
        return None
    holder = _address_holder(session, workspace_id, pending.id, result.requested_url)
    if holder is not None:
        return holder
    pending.canonical_url = result.requested_url
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action="pending_discovery.repointed_on_content_read",
            entity_type="pending_discovery",
            entity_id=pending.id,
            details={
                "previous_url": previous_url,
                "url": result.requested_url,
                "evidence_id": evidence_id,
            },
            **audit,
        )
    )
    return None
