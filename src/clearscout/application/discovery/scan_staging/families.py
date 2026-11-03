"""Regional families: one opening listed per market, tied to the row staged first."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.pending import move_pending_markers, release_families
from clearscout.application.discovery.posting_identity import posting_board_uuid
from clearscout.application.discovery.scan_staging.reasons import staged_by_person
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import utc_now
from clearscout.domain.identity import is_board_uuid, normalize_text
from clearscout.infrastructure.persistence.models import AuditEvent, PendingDiscovery


def staged_before(
    row: PendingDiscovery, pending: Iterable[PendingDiscovery], dismissed: Collection[str] = ()
) -> list[PendingDiscovery]:
    """Return the discoveries that entered the queue ahead of ``row``, and any dismissed one.

    Identifiers are random, so a pair staged on the same day is ordered by URL to
    keep which market is kept stable from one read to the next. A dismissal rules
    on a board-uuid opening whenever its row was staged.
    """
    uuid_named = is_board_uuid(row.requisition_id or posting_board_uuid(row.discovery_url) or "")
    return [
        other
        for other in pending
        if (other.first_seen_on, other.discovery_url) < (row.first_seen_on, row.discovery_url)
        or (uuid_named and other.id in dismissed and row.id not in dismissed)
    ]


def _one_opening(
    requisition_id: str, employer: str, source_id: str | None, other: PendingDiscovery
) -> bool:
    """Return whether a shared requisition makes ``other`` the same opening.

    A requisition is scoped to one board, so it identifies an opening across
    rows read from that source even when their postings name different entities.
    A board-issued uuid identifies it across every source and employer spelling.
    """
    requisition = normalize_text(requisition_id)
    # A careers-site row may state no requisition while its listing path carries the board uuid;
    # a content read may since have re-pointed its canonical address elsewhere.
    stated = (
        other.requisition_id
        or posting_board_uuid(other.discovery_url)
        or posting_board_uuid(other.canonical_url)
        or ""
    )
    if not requisition or normalize_text(stated) != requisition:
        return False
    if is_board_uuid(requisition):
        return True
    same_source = source_id is not None and other.source_id == source_id
    return same_source or normalize_text(other.employer) == normalize_text(employer)


def regional_head_of(
    listing: SourceListing,
    pending: Iterable[PendingDiscovery],
    source_id: str | None,
    dismissed: Collection[str],
    *,
    person_owned: bool = False,
) -> str | None:
    """Return the pending discovery this listing is the same job as.

    Before body content is read, a board's requisition identity is the only
    evidence that two URLs are one opening. Employer and title resemblance
    cannot distinguish a regional advertisement from two real headcounts.
    ``person_owned`` marks a row the person restored or added, which a
    dismissed head never absorbs.
    """
    requisition = listing.requisition_id or posting_board_uuid(listing.url) or ""
    for other in pending:
        # A dismissed head keeps absorbing its opening; any other archived or withdrawn
        # head would hide a live copy, since the queue shows neither.
        allowed = (
            not person_owned
            if other.id in dismissed
            else other.archived_at is None and other.withdrawn_at is None
        )
        if (
            other.regional_variant_of is None
            and allowed
            and _one_opening(requisition, listing.employer, source_id, other)
        ):
            return str(other.id)
    return None


def owned_by_person(row: PendingDiscovery, restored: Collection[str]) -> bool:
    """Return whether the person restored or added this row."""
    return row.id in restored or staged_by_person(row.reason)


def fold_family(
    session: Session,
    pending: PendingDiscovery,
    head_id: str,
    rows: Iterable[PendingDiscovery],
    dismissed: Collection[str],
    restored: Collection[str],
    *,
    correlation_id: str | None,
) -> None:
    """Fold a row and its markers under ``head_id``, and move the rows it headed there too.

    A child the person restored or added never moves under a dismissed head; it is
    released instead.
    """
    # Family reads and retirement look one level down, so a family never nests.
    pending.regional_variant_of = head_id
    head = session.get(PendingDiscovery, head_id)
    if head is not None:
        move_pending_markers(pending, head)
    children = [child for child in rows if child.regional_variant_of == pending.id]
    kept_out = [
        child for child in children if head_id in dismissed and owned_by_person(child, restored)
    ]
    release_families(
        session,
        pending.workspace_id,
        kept_out,
        reason="its head folded under a dismissed head",
        created_at=utc_now(),
        actor="worker",
        origin="source.scan",
        correlation_id=correlation_id,
    )
    for child in children:
        if child in kept_out:
            continue
        child.regional_variant_of = head_id
        session.add(
            AuditEvent(
                workspace_id=pending.workspace_id,
                action="pending_discovery.regional_variant_moved",
                entity_type="pending_discovery",
                entity_id=child.id,
                details={
                    "discovery_url": child.discovery_url,
                    "from_head": pending.id,
                    "to_head": head_id,
                },
                **audit_columns("worker", "source.scan", correlation_id),
            )
        )


def place_unlinked(
    session: Session,
    unplaced: list[tuple[PendingDiscovery, SourceListing]],
    known: dict[str, PendingDiscovery],
    dismissed: Collection[str],
    restored: Collection[str],
    *,
    correlation_id: str | None,
) -> None:
    """Fold each row a pass left unlinked under an earlier-staged head of its opening."""
    # A head requeued or relisted later in the pass was hidden when these rows were placed.
    for pending, listing in unplaced:
        if pending.regional_variant_of is not None:
            continue
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


def release_from_hidden_head(
    session: Session,
    pending: PendingDiscovery,
    dismissed: Collection[str],
    details: dict[str, str],
) -> None:
    """Unlink a row from an archived or withdrawn head the queue no longer shows."""
    head = (
        session.get(PendingDiscovery, pending.regional_variant_of)
        if pending.regional_variant_of is not None
        else None
    )
    # The queue shows only unarchived, unwithdrawn heads, so a row under any other undismissed
    # head is lost.
    if (
        head is not None
        and (head.archived_at is not None or head.withdrawn_at is not None)
        and head.id not in dismissed
    ):
        pending.regional_variant_of = None
        details["released_from_head"] = head.id


def record_relisted(
    session: Session,
    pending: PendingDiscovery,
    *,
    action: str,
    workspace_id: str,
    actor: str,
    origin: str,
    correlation_id: str | None,
    dismissed: Collection[str],
) -> None:
    """Reopen a withdrawn row a fresh read lists again, and record why."""
    if pending.withdrawn_at is None:
        return
    pending.withdrawn_at = None
    details = {"discovery_url": pending.discovery_url}
    release_from_hidden_head(session, pending, dismissed, details)
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action=action,
            entity_type="pending_discovery",
            entity_id=pending.id,
            details=details,
            **audit_columns(actor, origin, correlation_id),
        )
    )


def clear_regional_variant_family_if_identity_changed(
    session: Session, pending: PendingDiscovery, prior_identity: tuple[str, str]
) -> None:
    """Unlink a family when publisher metadata no longer names the same opening.

    A title change unlinks every member; an employer-only change keeps each link
    whose requisition still makes the other row the same opening.
    """
    if (pending.employer, pending.title) == prior_identity:
        return
    title_changed = pending.title != prior_identity[1]

    def unlinks(other: PendingDiscovery | None) -> bool:
        return (
            title_changed
            or other is None
            or not _one_opening(pending.requisition_id, pending.employer, pending.source_id, other)
        )

    if pending.regional_variant_of is not None and unlinks(
        session.get(PendingDiscovery, pending.regional_variant_of)
    ):
        pending.regional_variant_of = None
    variants = session.scalars(
        select(PendingDiscovery).where(
            PendingDiscovery.workspace_id == pending.workspace_id,
            PendingDiscovery.regional_variant_of == pending.id,
        )
    )
    for variant in variants:
        if unlinks(variant):
            variant.regional_variant_of = None
