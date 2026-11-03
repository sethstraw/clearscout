"""Screened-out listings kept reviewable across scans, one row per normalized address.

A scan records each refusal in its own outcome ledger; this view folds those
refusals together so a person reviews a title once, and again only when the
title finding it was reviewed against changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.funnel_audit import FunnelOutcome, FunnelReason
from clearscout.application.discovery.pending import (
    dismissed_pending_discovery_ids,
    visible_pending_discovery_filters,
)
from clearscout.application.discovery.scan_queries import SCREENED_OUT_REASONS
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import (
    ScreenedOutReviewState,
    ScreenedOutTitlePageResponse,
    ScreenedOutTitleResponse,
    ScreenedOutTitleReviewResponse,
)
from clearscout.domain import utc_now
from clearscout.domain.identity import url_sha256
from clearscout.infrastructure.persistence.models import (
    PendingDiscovery,
    ScreenedOutTitle,
    SourceScanOutcome,
)

MAX_PAGE_LIMIT = 250


def _title_finding(outcome: FunnelOutcome) -> tuple[str, str]:
    finding = next((item for item in outcome.screening_findings if item.dimension == "title"), None)
    return ("", "") if finding is None else (finding.code, finding.subject[:500])


def record_screened_out_titles(
    session: Session, workspace_id: str, recorded: Sequence[tuple[FunnelOutcome, str, str]]
) -> None:
    """Fold newly recorded outcomes, as (outcome, source name, normalized URL), into the view."""
    latest = {
        normalized_url: (outcome, source_name)
        for outcome, source_name, normalized_url in recorded
        if outcome.reason.value in SCREENED_OUT_REASONS
    }
    if not latest:
        return
    now = utc_now()
    existing = {
        row.normalized_url: row
        for row in session.scalars(
            select(ScreenedOutTitle).where(
                ScreenedOutTitle.workspace_id == workspace_id,
                ScreenedOutTitle.normalized_url.in_(latest),
            )
        )
    }
    # A database carried from before this view still holds the older refusals.
    earliest: dict[str, datetime] = dict(
        session.execute(
            select(SourceScanOutcome.normalized_url, func.min(SourceScanOutcome.created_at))
            .where(
                SourceScanOutcome.workspace_id == workspace_id,
                SourceScanOutcome.normalized_url.in_(set(latest) - set(existing)),
                SourceScanOutcome.reason.in_(SCREENED_OUT_REASONS),
            )
            .group_by(SourceScanOutcome.normalized_url)
        )
        .tuples()
        .all()
    )
    for normalized_url, (outcome, source_name) in latest.items():
        code, subject = _title_finding(outcome)
        row = existing.get(normalized_url)
        if row is None:
            seen: datetime | None = earliest.get(normalized_url)
            row = ScreenedOutTitle(
                workspace_id=workspace_id,
                normalized_url=normalized_url,
                first_seen_at=now if seen is None else min(seen, now),
                created_by="worker",
                origin="source.scan",
            )
            session.add(row)
        row.url = outcome.url
        row.fingerprint = url_sha256(outcome.url)
        row.source_name = source_name
        row.employer = outcome.employer[:300]
        row.title = outcome.title[:500]
        row.reason = outcome.reason.value
        row.title_code = code
        row.title_subject = subject
        row.last_seen_at = now


def _needs_review() -> ColumnElement[bool]:
    return or_(
        ScreenedOutTitle.reviewed_at.is_(None),
        ScreenedOutTitle.reviewed_title_code != ScreenedOutTitle.title_code,
        ScreenedOutTitle.reviewed_title_subject != ScreenedOutTitle.title_subject,
    )


def _review_state(row: ScreenedOutTitle) -> ScreenedOutReviewState:
    if row.reviewed_at is None:
        return "new"
    current = (row.reviewed_title_code, row.reviewed_title_subject) == (
        row.title_code,
        row.title_subject,
    )
    return "reviewed" if current else "changed"


def list_screened_out_titles(
    session: Session,
    workspace_id: str,
    *,
    needs_review: bool | None = None,
    search: str = "",
    source: str = "",
    offset: int = 0,
    limit: int = 100,
) -> ScreenedOutTitlePageResponse:
    """Return one page of screened-out titles not since staged, with workspace-wide counts."""
    if offset < 0:
        raise ValidationError("Screened-out title offset cannot be negative.")
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValidationError(f"Screened-out title limit must be between 1 and {MAX_PAGE_LIMIT}.")
    in_pending = select(PendingDiscovery.fingerprint).where(
        *visible_pending_discovery_filters(
            workspace_id, dismissed_pending_discovery_ids(workspace_id)
        )
    )
    visible = (
        ScreenedOutTitle.workspace_id == workspace_id,
        ScreenedOutTitle.fingerprint.not_in(in_pending),
    )
    filters: list[ColumnElement[bool]] = [*visible]
    if needs_review is not None:
        filters.append(_needs_review() if needs_review else ~_needs_review())
    if source.strip():
        filters.append(ScreenedOutTitle.source_name == source.strip())
    if term := search.strip():
        filters.append(
            or_(
                *(
                    column.icontains(term, autoescape=True)
                    for column in (
                        ScreenedOutTitle.employer,
                        ScreenedOutTitle.title,
                        ScreenedOutTitle.source_name,
                    )
                )
            )
        )

    def count(*conditions: ColumnElement[bool]) -> int:
        query = select(func.count()).select_from(ScreenedOutTitle).where(*conditions)
        return session.scalar(query) or 0

    rows = session.scalars(
        select(ScreenedOutTitle)
        .where(*filters)
        .order_by(
            ScreenedOutTitle.last_seen_at.desc(),
            ScreenedOutTitle.employer,
            ScreenedOutTitle.title,
            ScreenedOutTitle.id,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return ScreenedOutTitlePageResponse(
        items=[
            ScreenedOutTitleResponse(
                id=row.id,
                url=row.url,
                source_name=row.source_name,
                employer=row.employer,
                title=row.title,
                reason=FunnelReason(row.reason),
                title_code=row.title_code,
                title_subject=row.title_subject,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                reviewed_at=row.reviewed_at,
                review_state=_review_state(row),
            )
            for row in rows
        ],
        total=count(*filters),
        offset=offset,
        limit=limit,
        sources=sorted(
            session.scalars(select(ScreenedOutTitle.source_name).where(*visible).distinct()).all()
        ),
        all_count=count(*visible),
        needs_review_count=count(*visible, _needs_review()),
    )


def mark_screened_out_titles_reviewed(
    session: Session, workspace_id: str, ids: Sequence[str]
) -> ScreenedOutTitleReviewResponse:
    """Mark titles reviewed against the title finding each one carries now."""
    wanted = set(ids)
    rows = session.scalars(
        select(ScreenedOutTitle).where(
            ScreenedOutTitle.workspace_id == workspace_id, ScreenedOutTitle.id.in_(wanted)
        )
    ).all()
    if len(rows) != len(wanted):
        raise ValidationError("Some screened-out titles are not in this workspace.")
    now = utc_now()
    for row in rows:
        row.reviewed_at = now
        row.reviewed_title_code = row.title_code
        row.reviewed_title_subject = row.title_subject
    return ScreenedOutTitleReviewResponse(reviewed=len(rows))
