"""Which staged row or tracked job a listing names, by address or requisition."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.discovery.posting_identity import (
    posting_board_uuid,
    posting_host,
    posting_locale,
    posting_requisition_key,
)
from clearscout.application.job_search.evaluation_targets import find_job_by_durable_identifier
from clearscout.domain.identity import normalize_url, url_sha256
from clearscout.infrastructure.persistence.models import JobPosting, PendingDiscovery, PostingUrl


def requisition_keys(urls: Iterable[str]) -> set[str]:
    """Return the requisition keys these URLs name, locale-folded paths included."""
    return {key for url in urls if url and (key := posting_requisition_key(url))}


def _english_locale(url: str) -> bool:
    """Whether a posting URL leads with an English locale segment such as `en` or `en-us`."""
    return (posting_locale(url) or "").partition("-")[0] == "en"


def locale_variant_of_english(row: PendingDiscovery, listing_url: str) -> bool:
    """Whether a listing in another locale matched a row staged from an English-locale URL."""
    locale = posting_locale(listing_url)
    return (
        locale is not None
        and _english_locale(row.discovery_url)
        and locale != posting_locale(row.discovery_url)
    )


def _preference(
    *,
    ended: bool,
    variant: bool,
    url: str,
    first_seen_on: date,
    row_id: str,
) -> tuple[bool, bool, bool, date, str]:
    """Return the sort key of one row among rows sharing a requisition."""
    return (ended, variant, not _english_locale(url), first_seen_on, row_id)


def _requisition_preference(row: PendingDiscovery) -> tuple[bool, bool, bool, date, str]:
    """Order rows sharing a requisition: live, a family head, English-locale, staged earliest."""
    return _preference(
        ended=row.archived_at is not None or row.withdrawn_at is not None,
        variant=row.regional_variant_of is not None,
        url=row.discovery_url,
        first_seen_on=row.first_seen_on,
        row_id=row.id,
    )


def rows_by_requisition(rows: Iterable[PendingDiscovery]) -> dict[str, PendingDiscovery]:
    """Map each requisition to the preferred row naming it."""
    by_requisition: dict[str, PendingDiscovery] = {}
    for row in sorted(rows, key=_requisition_preference):
        for requisition in requisition_keys((row.discovery_url, row.canonical_url)):
            by_requisition.setdefault(requisition, row)
    return by_requisition


def requisition_holder(session: Session, workspace_id: str, url: str) -> PendingDiscovery | None:
    """Return the preferred staged row on this URL's host that names the same requisition.

    A withdrawn row that is not archived is skipped: only archived rows are requeued, so
    holding the add on it would leave the person's posting nowhere.
    """
    requisition = posting_requisition_key(url)
    if requisition is None:
        return None
    host = posting_host(url)
    candidates = session.execute(
        select(
            PendingDiscovery.id,
            PendingDiscovery.discovery_url,
            PendingDiscovery.canonical_url,
            PendingDiscovery.archived_at,
            PendingDiscovery.withdrawn_at,
            PendingDiscovery.regional_variant_of,
            PendingDiscovery.first_seen_on,
        ).where(
            PendingDiscovery.workspace_id == workspace_id,
            func.lower(PendingDiscovery.discovery_url).contains(host)
            | func.lower(PendingDiscovery.canonical_url).contains(host),
            PendingDiscovery.withdrawn_at.is_(None) | PendingDiscovery.archived_at.is_not(None),
        )
    ).all()
    preferred = [
        _preference(
            ended=archived_at is not None or withdrawn_at is not None,
            variant=variant_of is not None,
            url=discovery_url,
            first_seen_on=first_seen_on,
            row_id=row_id,
        )
        for (
            row_id,
            discovery_url,
            canonical_url,
            archived_at,
            withdrawn_at,
            variant_of,
            first_seen_on,
        ) in candidates
        if requisition in requisition_keys((discovery_url, canonical_url))
    ]
    return session.get(PendingDiscovery, min(preferred)[-1]) if preferred else None


def tracked_job(
    session: Session,
    workspace_id: str,
    url: str,
    *,
    employer: str = "",
    requisition_id: str = "",
    include_closed: bool = False,
) -> JobPosting | None:
    """Return the active tracked job a URL names, a path variant of its requisition included.

    A board uuid in the path names one opening at any host, so its twin at another host matches.
    ``include_closed`` lets an archived job claim that twin when no active job does.
    """
    found = find_job_by_durable_identifier(
        session,
        workspace_id=workspace_id,
        employer=employer,
        requisition_id=requisition_id,
        canonical_url=url,
        discovery_url=url,
        active_only=True,
    )
    requisition = posting_requisition_key(url)
    board_uuid = posting_board_uuid(url)
    if found is not None or (requisition is None and board_uuid is None):
        return found
    open_only = () if include_closed else (JobPosting.archived_at.is_(None),)
    addresses = session.execute(
        select(PostingUrl.job_posting_id, PostingUrl.normalized_url)
        .join(JobPosting, JobPosting.id == PostingUrl.job_posting_id)
        .where(
            JobPosting.workspace_id == workspace_id,
            *open_only,
            PostingUrl.archived_at.is_(None),
            func.lower(PostingUrl.normalized_url).contains(board_uuid or posting_host(url)),
        )
        .order_by(JobPosting.archived_at.is_not(None))
    )
    for job_id, address in addresses:
        if (board_uuid is not None and posting_board_uuid(address) == board_uuid) or (
            requisition is not None and posting_requisition_key(address) == requisition
        ):
            return session.get(JobPosting, job_id)
    return None


def listed_row_ids(staged: Sequence[PendingDiscovery], listed: Sequence[str]) -> set[str]:
    """Return the ids of staged rows a complete read listed.

    A row a content read re-pointed is listed at its read address too. A listed URL with
    a row of its own keeps only that row, so an unlisted regional head still goes.
    """
    present = {url_sha256(url) for url in listed}
    owned = {
        url_sha256(url) for row in staged for url in (row.discovery_url, row.canonical_url) if url
    }
    requisitions = requisition_keys(url for url in listed if url_sha256(url) not in owned)
    return {
        row.id
        for row in staged
        if row.fingerprint in present
        or (row.canonical_url and url_sha256(row.canonical_url) in present)
        or not requisition_keys((row.discovery_url, row.canonical_url)).isdisjoint(requisitions)
    }


def staged_rows(
    session: Session, workspace_id: str
) -> tuple[dict[str, PendingDiscovery], dict[str, PendingDiscovery]]:
    """Index every staged row, archived ones too, by fingerprint and by read address."""
    known = {
        item.fingerprint: item
        for item in session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
    }
    at_address = {
        normalize_url(item.canonical_url): item for item in known.values() if item.canonical_url
    }
    return known, at_address


def known_row(
    known: Mapping[str, PendingDiscovery],
    at_address: Mapping[str, PendingDiscovery],
    at_requisition: Mapping[str, PendingDiscovery],
    claimed: Collection[str],
    fingerprint: str,
    url: str,
) -> tuple[PendingDiscovery | None, bool]:
    """Return the staged row a listing names, and whether only its address matched.

    A row a content read re-pointed keeps its first fingerprint but holds this address.
    A path variant of a staged requisition matches that row and keeps its address,
    unless another listing in this pass already took the row, as a regional copy does.
    """
    pending = known.get(fingerprint)
    if pending is not None:
        return pending, False
    requisition = posting_requisition_key(url)
    by_requisition = at_requisition.get(requisition) if requisition else None
    if by_requisition is not None and by_requisition.id in claimed:
        by_requisition = None
    return at_address.get(normalize_url(url)) or by_requisition, True
