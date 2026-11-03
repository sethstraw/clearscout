"""Retiring a staged discovery, reporting what is holding one, and which titles a person fixed."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from sqlalchemy import ColumnElement, Select, exists, select
from sqlalchemy.orm import Session, aliased

from clearscout.application.failures import ConflictError, NotFoundError
from clearscout.application.tasks.records import task_pending_discovery_ids
from clearscout.domain import (
    PendingWithdrawalCause,
    PostingContentAccessStatus,
    PostingStatus,
    TaskStatus,
    new_id,
    utc_now,
)
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    Employer,
    GuidedCampaignAdmittedDiscovery,
    GuidedCampaignCapturedUrl,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    Source,
)

_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        TaskStatus.QUEUED.value,
        TaskStatus.RUNNING.value,
        TaskStatus.NEEDS_REVIEW.value,
        TaskStatus.COMMITTING.value,
    }
)
# A campaign payload names no pending targets, so its passes guard the whole queue.
_CAMPAIGN_TASK_TYPES: frozenset[str] = frozenset(
    {
        "search.guided_campaign",
        "search.guided_campaign_retry",
    }
)
_GUARDED_TASK_TYPES: frozenset[str] = frozenset(
    {
        "posting.recover_batch",
        "discovery.evaluate",
        "discovery.evaluate_batch",
        "discovery.relevance_review",
        *_CAMPAIGN_TASK_TYPES,
    }
)
TITLE_CORRECTED_ACTION = "pending_discovery.hint_corrected"
QUEUE_RESET_ACTION = "pending_discovery.queue_reset"
MARKERS_CHANGED_ACTION = "pending_discovery.markers_changed"
CLOSING_DATE_CHANGED_ACTION = "pending_discovery.closing_date_changed"
VARIANT_RELEASED_ACTION = "pending_discovery.regional_variant_released"


@dataclass(frozen=True)
class PendingQueueResetResult:
    """Counts from rebuilding the pending queue around human decisions."""

    archived_count: int
    kept_count: int
    rescreened_count: int


@dataclass(frozen=True)
class PendingMarkers:
    """A family's markers as its visible head shows them."""

    favorite: bool = False
    kept: bool = False
    keep_note: str | None = None

    @property
    def marked(self) -> bool:
        """Return whether either marker holds the family against unconfirmed dismissal."""
        return self.favorite or self.kept

    @property
    def review_note(self) -> str | None:
        """Return the note a relevance review is sent, which only a kept family carries."""
        return self.keep_note if self.kept and self.keep_note else None

    @property
    def rank(self) -> int:
        """Order favorites first, then kept rows, then everything else."""
        return 0 if self.favorite else 1 if self.kept else 2


def retire_pending(session: Session, pending: PendingDiscovery) -> None:
    """Archive a discovery and every regional variant of it."""
    now = utc_now()
    pending.archived_at = now
    for variant in session.scalars(
        select(PendingDiscovery).where(
            PendingDiscovery.regional_variant_of == pending.id,
            PendingDiscovery.archived_at.is_(None),
        )
    ):
        variant.archived_at = now


def reset_pending_queue(
    session: Session,
    workspace_id: str,
    *,
    actor: str = "local_user",
    correlation_id: str | None = None,
) -> PendingQueueResetResult:
    """Forget machine-controlled rows while preserving human decisions and active work."""
    rows = session.scalars(
        select(PendingDiscovery)
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.archived_at.is_(None),
        )
        .with_for_update()
    ).all()
    latest = {
        event.pending_discovery_id: event
        for event in session.scalars(
            latest_disposition_statement(workspace_id, [row.id for row in rows])
        )
    }
    row_ids = [row.id for row in rows]
    guarded = active_task_target_ids(session, workspace_id, row_ids)
    retained = reset_protected_ids(session, workspace_id, row_ids)
    reset_at = utc_now()
    archived = 0
    kept = 0
    rescreened = 0
    for row in rows:
        disposition = latest.get(row.id)
        if _is_human_disposition(disposition) or row.id in guarded or row.id in retained:
            kept += 1
            continue
        if disposition is not None and disposition.action == "dismiss":
            _restore_for_reset(
                session,
                disposition,
                actor=actor,
                correlation_id=correlation_id,
                created_at=reset_at,
            )
        if row.source_id is None:
            if row.withdrawn_at is not None:
                row.withdrawn_at = None
                session.add(
                    AuditEvent(
                        id=new_id(),
                        workspace_id=workspace_id,
                        action="pending_discovery.withdrawal_reset",
                        entity_type="pending_discovery",
                        entity_id=row.id,
                        details={"discovery_url": row.discovery_url},
                        created_at=reset_at,
                        created_by=actor,
                        origin="pending_queue.reset",
                        correlation_id=correlation_id,
                    )
                )
            rescreened += 1
            continue
        row.archived_at = reset_at
        session.add(
            AuditEvent(
                id=new_id(),
                workspace_id=workspace_id,
                action=QUEUE_RESET_ACTION,
                entity_type="pending_discovery",
                entity_id=row.id,
                details={"discovery_url": row.discovery_url},
                created_at=reset_at,
                created_by=actor,
                origin="pending_queue.reset",
                correlation_id=correlation_id,
            )
        )
        archived += 1
    archived_ids = {row.id for row in rows if row.archived_at is not None}
    release_families(
        session,
        workspace_id,
        [
            row
            for row in rows
            if row.archived_at is None and row.regional_variant_of in archived_ids
        ],
        reason="the queue reset archived its head",
        created_at=reset_at,
        actor=actor,
        origin="pending_queue.reset",
        correlation_id=correlation_id,
    )
    return PendingQueueResetResult(
        archived_count=archived,
        kept_count=kept,
        rescreened_count=rescreened,
    )


def release_variants_of_withdrawn(
    session: Session,
    workspace_id: str,
    head_ids: Iterable[str],
    *,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    """Release the live rows under heads just withdrawn, unless the person dismissed the head."""
    heads = list(head_ids)
    if not heads:
        return
    variants = session.scalars(
        select(PendingDiscovery).where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.regional_variant_of.in_(heads),
            PendingDiscovery.regional_variant_of.not_in(
                dismissed_pending_discovery_ids(workspace_id)
            ),
            PendingDiscovery.archived_at.is_(None),
            PendingDiscovery.withdrawn_at.is_(None),
        )
    ).all()
    release_families(
        session,
        workspace_id,
        variants,
        reason="its head was withdrawn",
        created_at=utc_now(),
        actor=actor,
        origin=origin,
        correlation_id=correlation_id,
    )


def release_families(
    session: Session,
    workspace_id: str,
    variants: Iterable[PendingDiscovery],
    *,
    reason: str,
    created_at: datetime,
    actor: str,
    origin: str,
    correlation_id: str | None,
) -> None:
    """Unlink rows from a head the queue stops showing, refolding each family under one member.

    The new head is the first undismissed, then unwithdrawn, then readable, then
    earliest-staged member, and it takes over the released head's markers.
    """
    families: dict[str, list[PendingDiscovery]] = {}
    for row in variants:
        if row.regional_variant_of is not None:
            families.setdefault(row.regional_variant_of, []).append(row)
    if not families:
        return
    dismissed = {
        event.pending_discovery_id
        for event in session.scalars(
            latest_disposition_statement(
                workspace_id, [row.id for family in families.values() for row in family]
            )
        )
        if event.action == "dismiss"
    }
    for head_id, family in families.items():
        released_head = session.get(PendingDiscovery, head_id)
        # The queue shows only an undismissed, unwithdrawn head, and a readable copy
        # serves the person better than an earlier one nobody can read.
        family.sort(
            key=lambda row: (
                row.id in dismissed,
                row.withdrawn_at is not None,
                row.content_status != PostingContentAccessStatus.READABLE,
                row.first_seen_on,
                row.discovery_url,
            )
        )
        # A dismissed survivor never takes markers; the released head keeps them and stays listed.
        if released_head is not None and family[0].id not in dismissed:
            move_pending_markers(released_head, family[0])
        for row in family:
            details = {
                "discovery_url": row.discovery_url,
                "released_from_head": head_id,
                "reason": reason,
            }
            row.regional_variant_of = None if row is family[0] else family[0].id
            if row.regional_variant_of is not None:
                details["refolded_under"] = row.regional_variant_of
            session.add(
                AuditEvent(
                    id=new_id(),
                    workspace_id=row.workspace_id,
                    action=VARIANT_RELEASED_ACTION,
                    entity_type="pending_discovery",
                    entity_id=row.id,
                    details=details,
                    created_at=created_at,
                    created_by=actor,
                    origin=origin,
                    correlation_id=correlation_id,
                )
            )


def set_pending_markers(
    session: Session,
    workspace_id: str,
    pending_id: str,
    *,
    favorite: bool,
    kept: bool,
    keep_note: str | None,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> str:
    """Set a family's markers on its head, clear them on its members, and return the head id.

    A changed family note leaves its stored relevance review out of date; no review runs.
    """
    row = session.scalar(
        select(PendingDiscovery)
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.id == pending_id,
            PendingDiscovery.archived_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("That pending discovery no longer exists in the active queue.")
    head_id = row.regional_variant_of or row.id
    latest = session.scalars(latest_disposition_statement(workspace_id, [head_id])).first()
    if (favorite or kept) and latest is not None and latest.action == "dismiss":
        raise ConflictError("A dismissed pending discovery must be restored before it is marked.")
    family = _family_rows(session, workspace_id, [head_id])
    head = next((member for member in family if member.id == head_id), None)
    if head is None:
        raise NotFoundError("That pending discovery's family head left the active queue.")
    note = keep_note.strip() or None if keep_note is not None and kept else None
    now = utc_now()
    for member in family:
        if member is not head:
            move_pending_markers(member, head, transfer_closing_date=False)
    head.favorite = favorite
    head.kept = kept
    head.keep_note = note
    session.add(
        AuditEvent(
            id=new_id(),
            workspace_id=workspace_id,
            action=MARKERS_CHANGED_ACTION,
            entity_type="pending_discovery",
            entity_id=head_id,
            details={
                "favorite": favorite,
                "kept": kept,
                "keep_note": note,
                "discovery_url": head.discovery_url,
            },
            created_at=now,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
    )
    return head_id


def _pending_closing_date_head(
    session: Session, workspace_id: str, pending_id: str
) -> PendingDiscovery:
    row = session.scalar(
        select(PendingDiscovery)
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.id == pending_id,
            PendingDiscovery.archived_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("That pending discovery no longer exists in the active queue.")
    head_id = row.regional_variant_of or row.id
    head = session.scalar(
        select(PendingDiscovery)
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.id == head_id,
            PendingDiscovery.archived_at.is_(None),
        )
        .with_for_update()
    )
    if head is None:
        raise NotFoundError("That pending discovery's family head left the active queue.")
    if _latest_dismissal(session, workspace_id, [row.id, head_id]) is not None:
        raise NotFoundError("That pending discovery no longer exists in the active queue.")
    return head


def _latest_dismissal(
    session: Session, workspace_id: str, pending_ids: list[str]
) -> PendingDiscoveryDispositionEvent | None:
    latest = session.scalars(latest_disposition_statement(workspace_id, pending_ids))
    return next((event for event in latest if event.action == "dismiss"), None)


def family_head_dismissal(
    session: Session, workspace_id: str, pending_id: str
) -> PendingDiscoveryDispositionEvent | None:
    """Return the dismissal of a regional variant's family head, of any origin."""
    row = session.get(PendingDiscovery, pending_id)
    if row is None or row.workspace_id != workspace_id or row.regional_variant_of is None:
        return None
    return _latest_dismissal(session, workspace_id, [row.regional_variant_of])


def pending_closing_date(
    session: Session, workspace_id: str, pending_id: str
) -> tuple[date | None, datetime | None]:
    """Return the active family's personal date and last explicit edit time."""
    head = _pending_closing_date_head(session, workspace_id, pending_id)
    return head.closing_date_override, head.closing_date_override_updated_at


def set_pending_closing_date(
    session: Session,
    workspace_id: str,
    pending_id: str,
    closing_date_override: date | None,
    *,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> str:
    """Record a personal date edit on the active family head and return its id."""
    head = _pending_closing_date_head(session, workspace_id, pending_id)
    old_date = head.closing_date_override
    now = utc_now()
    head.closing_date_override = closing_date_override
    head.closing_date_override_updated_at = now
    session.add(
        AuditEvent(
            id=new_id(),
            workspace_id=workspace_id,
            action=CLOSING_DATE_CHANGED_ACTION,
            entity_type="pending_discovery",
            entity_id=head.id,
            details={
                "old_closing_date_override": old_date.isoformat() if old_date else None,
                "closing_date_override": (
                    closing_date_override.isoformat() if closing_date_override else None
                ),
            },
            created_at=now,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
    )
    return head.id


def transfer_pending_closing_date(source: PendingDiscovery, target: PendingDiscovery) -> None:
    """Move the latest explicit personal date edit, keeping the destination on ties."""
    if source.id == target.id:
        return
    source_updated = source.closing_date_override_updated_at
    target_updated = target.closing_date_override_updated_at
    if source_updated is not None and (target_updated is None or source_updated > target_updated):
        target.closing_date_override = source.closing_date_override
        target.closing_date_override_updated_at = source_updated
    source.closing_date_override = None
    source.closing_date_override_updated_at = None


def clear_pending_markers(rows: Iterable[PendingDiscovery]) -> None:
    """Drop every marker on rows a person confirmed dismissing."""
    for row in rows:
        row.favorite = False
        row.kept = False
        row.keep_note = None


def move_pending_markers(
    source: PendingDiscovery, target: PendingDiscovery, *, transfer_closing_date: bool = True
) -> None:
    """Move family markers and date edits, preserving the target's existing keep note."""
    target.favorite = target.favorite or source.favorite
    target.kept = target.kept or source.kept
    if target.keep_note is None and source.kept and source.keep_note:
        target.keep_note = source.keep_note
    source.favorite = False
    source.kept = False
    source.keep_note = None
    if transfer_closing_date:
        transfer_pending_closing_date(source, target)


def family_markers(
    session: Session, workspace_id: str, pending_ids: Iterable[str]
) -> dict[str, PendingMarkers]:
    """Return, for each requested row, the markers of the family it belongs to."""
    candidates = list(dict.fromkeys(pending_ids))
    if not candidates:
        return {}
    heads = {
        pending_id: head_id or pending_id
        for pending_id, head_id in session.execute(
            select(PendingDiscovery.id, PendingDiscovery.regional_variant_of).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.id.in_(candidates),
            )
        )
    }
    family = _family_rows(session, workspace_id, set(heads.values()), marked_only=True)
    return {
        pending_id: _family_markers(
            [member for member in family if (member.regional_variant_of or member.id) == head],
            head,
        )
        for pending_id, head in heads.items()
    }


def marked_ids(session: Session, workspace_id: str, pending_ids: Iterable[str]) -> set[str]:
    """Return the requested rows whose family carries a favorite or keep marker."""
    return {
        pending_id
        for pending_id, markers in family_markers(session, workspace_id, pending_ids).items()
        if markers.marked
    }


def marked_family_ids(session: Session, workspace_id: str) -> set[str]:
    """Return every unarchived row carrying a marker, and the head of each one's family."""
    rows = session.execute(
        select(PendingDiscovery.id, PendingDiscovery.regional_variant_of).where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.archived_at.is_(None),
            PendingDiscovery.favorite | PendingDiscovery.kept,
        )
    )
    return {pending_id for row in rows for pending_id in row if pending_id is not None}


def _family_rows(
    session: Session, workspace_id: str, head_ids: Iterable[str], *, marked_only: bool = False
) -> list[PendingDiscovery]:
    heads = list(head_ids)
    statement = select(PendingDiscovery).where(
        PendingDiscovery.workspace_id == workspace_id,
        PendingDiscovery.archived_at.is_(None),
        PendingDiscovery.id.in_(heads) | PendingDiscovery.regional_variant_of.in_(heads),
    )
    if marked_only:
        statement = statement.where(PendingDiscovery.favorite | PendingDiscovery.kept)
    return list(session.scalars(statement.order_by(PendingDiscovery.id)))


def _family_markers(family: Iterable[PendingDiscovery], head_id: str) -> PendingMarkers:
    members = sorted(family, key=lambda member: (member.id != head_id, member.id))
    notes = [member.keep_note for member in members if member.kept and member.keep_note]
    return PendingMarkers(
        favorite=any(member.favorite for member in members),
        kept=any(member.kept for member in members),
        keep_note=notes[0] if notes else None,
    )


def campaign_staged_ids(session: Session, pending_ids: Iterable[str]) -> set[str]:
    """Return the rows a guided campaign's own records say it put in the queue.

    A capture keeps only the first row its URL staged while an admission keeps
    every one, so neither table alone names the whole set.
    """
    candidates = list(pending_ids)
    if not candidates:
        return set()
    admitted = session.scalars(
        select(GuidedCampaignAdmittedDiscovery.pending_discovery_id).where(
            GuidedCampaignAdmittedDiscovery.pending_discovery_id.in_(candidates)
        )
    )
    captured = session.scalars(
        select(GuidedCampaignCapturedUrl.pending_discovery_id).where(
            GuidedCampaignCapturedUrl.pending_discovery_id.in_(candidates)
        )
    )
    return {pending_id for pending_id in (*admitted, *captured) if pending_id is not None}


def reset_protected_ids(
    session: Session, workspace_id: str, pending_ids: Iterable[str]
) -> set[str]:
    """Return rows a reset leaves alone: marked families, campaign admissions, verified closures."""
    candidates = list(pending_ids)
    if not candidates:
        return set()
    # A withdrawal backed by stored closure evidence is a publisher fact, not machine state.
    closed_ids = set(
        session.scalars(
            select(PendingDiscovery.id)
            .join(
                PendingPostingContent,
                PendingPostingContent.id == PendingDiscovery.current_content_id,
            )
            .where(
                PendingDiscovery.id.in_(candidates),
                PendingDiscovery.withdrawn_at.is_not(None),
                PendingPostingContent.posting_status == PostingStatus.CLOSED.value,
            )
        )
    )
    admitted_ids = set(
        session.scalars(
            select(GuidedCampaignAdmittedDiscovery.pending_discovery_id).where(
                GuidedCampaignAdmittedDiscovery.pending_discovery_id.in_(candidates)
            )
        )
    )
    return marked_ids(session, workspace_id, candidates) | admitted_ids | closed_ids


def source_employer_names(session: Session, source_ids: Iterable[str | None]) -> dict[str, str]:
    """Return each source's registered employer name, keyed by source id."""
    candidates = {source_id for source_id in source_ids if source_id}
    if not candidates:
        return {}
    return dict(
        session.execute(
            select(Source.id, Employer.name)
            .join(Employer, Employer.id == Source.employer_id)
            .where(Source.id.in_(candidates))
        ).all()
    )


def latest_disposition_statement(
    workspace_id: str, ids: list[str] | None = None
) -> Select[PendingDiscoveryDispositionEvent]:
    """Select unsuperseded pending-discovery disposition events."""
    event = PendingDiscoveryDispositionEvent
    successor = aliased(PendingDiscoveryDispositionEvent)
    statement = select(event).where(
        event.workspace_id == workspace_id,
        ~exists().where(successor.supersedes_event_id == event.id),
    )
    return statement.where(event.pending_discovery_id.in_(ids)) if ids is not None else statement


def dismissed_pending_discovery_ids(workspace_id: str) -> Select[str]:
    """Select discoveries whose latest disposition is a dismissal."""
    return cast(
        "Select[str]",
        latest_disposition_statement(workspace_id)
        .where(PendingDiscoveryDispositionEvent.action == "dismiss")
        .with_only_columns(PendingDiscoveryDispositionEvent.pending_discovery_id),
    )


def person_restored_pending_discovery_ids(workspace_id: str) -> Select[str]:
    """Select discoveries whose latest disposition is the person's own restore."""
    return cast(
        "Select[str]",
        latest_disposition_statement(workspace_id)
        .where(
            PendingDiscoveryDispositionEvent.action == "restore",
            PendingDiscoveryDispositionEvent.restore_provenance == "person",
        )
        .with_only_columns(PendingDiscoveryDispositionEvent.pending_discovery_id),
    )


def root_discovery_filters(workspace_id: str) -> tuple[ColumnElement[bool], ...]:
    """Return filters for unarchived regional-family heads, dismissed or not."""
    return (
        PendingDiscovery.workspace_id == workspace_id,
        PendingDiscovery.archived_at.is_(None),
        PendingDiscovery.regional_variant_of.is_(None),
    )


def undismissed_root_filters(
    workspace_id: str, dismissed: Select[str]
) -> tuple[ColumnElement[bool], ...]:
    """Return filters for undismissed, unarchived regional-family heads."""
    return (
        *root_discovery_filters(workspace_id),
        PendingDiscovery.id.not_in(dismissed),
    )


def visible_pending_discovery_filters(
    workspace_id: str, dismissed: Select[str]
) -> tuple[ColumnElement[bool], ...]:
    """Return the filters for open, undismissed family heads the queue acts on."""
    return (
        *undismissed_root_filters(workspace_id, dismissed),
        PendingDiscovery.withdrawn_at.is_(None),
    )


def listed_pending_discovery_filters(
    workspace_id: str, dismissed: Select[str]
) -> tuple[ColumnElement[bool], ...]:
    """Return the Pending queue's listing filters: open heads, and closed heads a marker holds."""
    member = aliased(PendingDiscovery)
    # One uncorrelated list of marked variants' heads; a correlated family match
    # rescans the workspace for every closed head.
    marked_variant_heads = select(member.regional_variant_of).where(
        member.workspace_id == workspace_id,
        member.archived_at.is_(None),
        member.regional_variant_of.is_not(None),
        member.favorite | member.kept,
    )
    return (
        *undismissed_root_filters(workspace_id, dismissed),
        PendingDiscovery.withdrawn_at.is_(None)
        | PendingDiscovery.favorite
        | PendingDiscovery.kept
        | PendingDiscovery.id.in_(marked_variant_heads),
    )


def reset_archive_markers(
    session: Session, workspace_id: str, ids: Iterable[str]
) -> dict[str, str]:
    """Return each archived row's unconsumed queue-reset event."""
    candidates = list(ids)
    if not candidates:
        return {}
    reset_events = session.scalars(
        select(AuditEvent)
        .join(PendingDiscovery, PendingDiscovery.id == AuditEvent.entity_id)
        .where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.action == QUEUE_RESET_ACTION,
            AuditEvent.entity_type == "pending_discovery",
            AuditEvent.entity_id.in_(candidates),
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.archived_at.is_not(None),
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
    ).all()
    requeue_events = session.scalars(
        select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.action == "pending_discovery.requeued",
            AuditEvent.entity_id.in_(candidates),
        )
    )
    consumed = {
        marker
        for event in requeue_events
        for marker in [event.details.get("reset_event_id")]
        if isinstance(marker, str)
    }
    markers: dict[str, str] = {}
    for event in reset_events:
        if event.entity_id is not None and event.id not in consumed:
            markers[event.entity_id] = event.id
    return markers


_WITHDRAWAL_CAUSES = {
    "pending_discovery.withdrawn_absent": PendingWithdrawalCause.ABSENT,
    "pending_discovery.withdrawn_expired": PendingWithdrawalCause.LISTING_ENDED,
    "pending_discovery.withdrawn_on_refresh": PendingWithdrawalCause.PUBLISHER_CLOSED,
    "pending_discovery.saved_closure_applied": PendingWithdrawalCause.PUBLISHER_CLOSED,
    "pending_discovery.withdrawn_on_content_read": PendingWithdrawalCause.CONTENT_READ_CLOSED,
}


def withdrawal_causes(
    session: Session, workspace_id: str, rows: Iterable[PendingDiscovery]
) -> dict[str, PendingWithdrawalCause]:
    """Map each withdrawn row to the cause its latest withdrawal event names."""
    candidates = [row.id for row in rows if row.withdrawn_at is not None]
    if not candidates:
        return {}
    events = session.execute(
        select(AuditEvent.entity_id, AuditEvent.action)
        .where(
            AuditEvent.workspace_id == workspace_id,
            AuditEvent.entity_type == "pending_discovery",
            AuditEvent.action.in_(_WITHDRAWAL_CAUSES),
            AuditEvent.entity_id.in_(candidates),
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    causes = dict.fromkeys(candidates, PendingWithdrawalCause.UNRECORDED)
    for entity_id, action in events:
        if entity_id is not None:
            causes[entity_id] = _WITHDRAWAL_CAUSES[action]
    return causes


def _is_human_disposition(event: PendingDiscoveryDispositionEvent | None) -> bool:
    return event is not None and event.selection_mode == "explicit" and event.origin == "api"


def _restore_for_reset(
    session: Session,
    event: PendingDiscoveryDispositionEvent,
    *,
    actor: str,
    correlation_id: str | None,
    created_at: datetime,
) -> None:
    digest = hashlib.sha256(f"pending_queue.reset:{event.id}".encode("ascii")).hexdigest()
    session.add(
        PendingDiscoveryDispositionEvent(
            id=new_id(),
            workspace_id=event.workspace_id,
            pending_discovery_id=event.pending_discovery_id,
            action="restore",
            restore_provenance="audit",
            category=event.category,
            reason="The pending queue reset removed the previous machine decision.",
            selection_mode="automatic",
            snapshot_sha256=digest,
            search_parameter_set_id=event.search_parameter_set_id,
            screening=event.screening,
            supersedes_event_id=event.id,
            created_at=created_at,
            created_by=actor,
            origin="pending_queue.reset",
            correlation_id=correlation_id,
        )
    )


def corrected_title_ids(
    session: Session, workspace_id: str, ids: Iterable[str] | None = None
) -> set[str]:
    """Return pending ids, among ``ids`` when given, whose title a person corrected."""
    statement = select(AuditEvent.entity_id).where(
        AuditEvent.workspace_id == workspace_id,
        AuditEvent.action == TITLE_CORRECTED_ACTION,
    )
    if ids is not None:
        statement = statement.where(AuditEvent.entity_id.in_(list(ids)))
    return {entity_id for entity_id in session.scalars(statement) if entity_id is not None}


def active_task_target_ids(
    session: Session, workspace_id: str, pending_ids: Iterable[str]
) -> set[str]:
    """Return candidate pending ids guarded by active paid discovery work."""
    candidates = set(pending_ids)
    if not candidates:
        return set()
    rows = session.scalars(
        select(BackgroundTask).where(
            BackgroundTask.workspace_id == workspace_id,
            BackgroundTask.status.in_(_ACTIVE_STATUSES),
            BackgroundTask.task_type.in_(_GUARDED_TASK_TYPES),
        )
    )
    guarded: set[str] = set()
    for task in rows:
        if task.task_type in _CAMPAIGN_TASK_TYPES:
            return candidates
        guarded.update(
            candidates.intersection(task_pending_discovery_ids(task.task_type, task.payload))
        )
    return guarded


def active_task_targets(session: Session, workspace_id: str, pending_id: str) -> bool:
    """Report whether a paid discovery task is guarding this pending row."""
    return pending_id in active_task_target_ids(session, workspace_id, [pending_id])
