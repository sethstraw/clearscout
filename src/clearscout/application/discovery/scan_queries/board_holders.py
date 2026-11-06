"""Finding an unready row's role among what its employer's boards already supplied."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.content_resolution import usable_current_evidence
from clearscout.application.discovery.pending import dismissed_pending_discovery_ids
from clearscout.application.discovery.posting_identity import BoardHolder, BoardRecord, board_holder
from clearscout.domain import PostingStatus, SourceKind
from clearscout.domain.identity import normalize_text, requisition_key
from clearscout.domain.staged_facts import StagedListingFacts
from clearscout.infrastructure.persistence.models import (
    Employer,
    JobPosting,
    PendingDiscovery,
    PendingPostingContent,
    PostingIdentifier,
    PostingObservation,
    PostingUrl,
    Source,
)
from clearscout.infrastructure.web_fetch.boards import board_identity


def _requisition_keys(employer: str, requisition: str) -> frozenset[str]:
    return frozenset({requisition_key(employer, requisition)} if requisition.strip() else ())


def _row_places(row: PendingDiscovery) -> tuple[str, ...]:
    return (row.location, *StagedListingFacts.from_json(row.staged_facts).locations.values)


def _tracked_records(
    session: Session, workspace_id: str, boards: dict[str, str]
) -> list[BoardRecord]:
    """Return the tracked postings at these boards' addresses; ``boards`` maps source id to URL."""
    # A posting tracked before its board was registered stores no source, so an
    # address on the board's own tenant is attributed to that board.
    tenants = {
        tenant: source_id for source_id, url in boards.items() if (tenant := board_identity(url))
    }
    rows = session.execute(
        select(JobPosting, PostingUrl.url, PostingUrl.source_id)
        .join(PostingUrl, PostingUrl.job_posting_id == JobPosting.id)
        .where(
            JobPosting.workspace_id == workspace_id,
            or_(PostingUrl.source_id.in_(boards), PostingUrl.source_id.is_(None)),
            PostingUrl.archived_at.is_(None),
        )
        # One record per job, at its board-sourced address before its primary one.
        .order_by(
            PostingUrl.source_id.is_(None), PostingUrl.is_primary.desc(), PostingUrl.created_at
        )
    ).all()
    records = []
    recorded: set[str] = set()
    for job, url, stored_source_id in rows:
        if job.id in recorded:
            continue
        source_id = stored_source_id
        if source_id is None and (tenant := board_identity(url)) is not None:
            source_id = tenants.get(tenant)
        if source_id is None:
            continue
        recorded.add(job.id)
        latest = session.scalars(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job.id)
            .order_by(PostingObservation.observed_at.desc())
            .limit(1)
        ).first()
        requisitions = frozenset(
            session.scalars(
                select(PostingIdentifier.identifier_value).where(
                    PostingIdentifier.job_posting_id == job.id,
                    PostingIdentifier.identifier_type == "requisition",
                )
            )
        )
        records.append(
            BoardRecord(
                holder=BoardHolder("job_posting", job.id, url),
                source_id=str(source_id),
                title=job.title,
                places=(latest.location,) if latest is not None else (),
                requisitions=requisitions,
                closed=job.archived_at is not None
                or (latest is not None and latest.posting_status == PostingStatus.CLOSED),
            )
        )
    return records


def _staged_records(
    session: Session,
    workspace_id: str,
    source_ids: list[str],
    pending_id: str,
    *,
    count_dismissed: bool,
) -> list[BoardRecord]:
    """Return the board's withdrawn rows, and its rows that hold usable evidence.

    A dismissed row counts only when ``count_dismissed`` is set.
    """
    statement = (
        select(PendingDiscovery, PendingPostingContent)
        .outerjoin(
            PendingPostingContent,
            PendingPostingContent.id == PendingDiscovery.current_content_id,
        )
        .where(
            PendingDiscovery.workspace_id == workspace_id,
            PendingDiscovery.source_id.in_(source_ids),
            PendingDiscovery.id != pending_id,
            PendingDiscovery.archived_at.is_(None),
        )
    )
    if not count_dismissed:
        statement = statement.where(
            PendingDiscovery.id.not_in(dismissed_pending_discovery_ids(workspace_id))
        )
    rows: Sequence[tuple[PendingDiscovery, PendingPostingContent | None]] = session.execute(
        statement
    ).all()
    return [
        BoardRecord(
            holder=BoardHolder("pending_discovery", row.id, row.canonical_url or row.discovery_url),
            source_id=str(row.source_id),
            title=row.title,
            places=_row_places(row),
            requisitions=_requisition_keys(row.employer, row.requisition_id),
            closed=row.withdrawn_at is not None,
        )
        for row, evidence in rows
        if row.withdrawn_at is not None
        or (evidence is not None and usable_current_evidence(row, evidence))
    ]


def registered_employer_boards(
    session: Session, workspace_id: str, employer: str
) -> dict[str, str]:
    """Return the employer's enabled job boards in this workspace, mapping source id to URL."""
    normalized = normalize_text(employer)
    if not normalized:
        return {}
    return dict(
        session.execute(
            select(Source.id, Source.url)
            .join(Employer, Employer.id == Source.employer_id)
            .where(
                Source.workspace_id == workspace_id,
                Source.enabled.is_(True),
                Source.archived_at.is_(None),
                Source.source_type == SourceKind.EMPLOYER_BOARD.value,
                Employer.normalized_name == normalized,
            )
        ).all()
    )


def registered_board_records(
    session: Session, workspace_id: str, boards: dict[str, str], pending_id: str
) -> list[BoardRecord]:
    """Return what these boards already supplied, other than the pending row itself."""
    if not boards:
        return []
    return [
        *_tracked_records(session, workspace_id, boards),
        *_staged_records(session, workspace_id, list(boards), pending_id, count_dismissed=True),
    ]


def registered_board_holder(
    session: Session, workspace_id: str, pending_id: str, *, count_dismissed: bool = False
) -> BoardHolder | None:
    """Find an unready row's role among what its employer's enabled boards already supplied.

    Reads only stored records, never the network. ``count_dismissed`` lets a dismissed
    board row hold the role.
    """
    pending = session.get(PendingDiscovery, pending_id)
    if pending is None or pending.workspace_id != workspace_id:
        return None
    current = (
        session.get(PendingPostingContent, pending.current_content_id)
        if pending.current_content_id is not None
        else None
    )
    if current is not None and usable_current_evidence(pending, current):
        return None
    boards = registered_employer_boards(session, workspace_id, pending.employer)
    if not boards:
        return None
    return board_holder(
        pending.title,
        _row_places(pending),
        _requisition_keys(pending.employer, pending.requisition_id),
        [
            *_tracked_records(session, workspace_id, boards),
            *_staged_records(
                session, workspace_id, list(boards), pending.id, count_dismissed=count_dismissed
            ),
        ],
    )
