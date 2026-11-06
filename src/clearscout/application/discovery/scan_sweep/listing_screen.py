"""Screen one board's listings by title and facts, and read back its body-screen findings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial

from sqlalchemy import select

from clearscout.application.discovery.dispositions import BODY_REFUSAL_CATEGORIES
from clearscout.application.discovery.funnel_audit import FunnelScreeningFinding
from clearscout.application.discovery.pending import latest_disposition_statement
from clearscout.application.discovery.scan_staging import StagedListings
from clearscout.application.discovery.screening import (
    DiscoveryScreening,
    StructuredHoldout,
    listed_title_may_qualify,
    screen_discovery,
    select_title_survivors,
    structured_holdout,
    summary_focus_terms,
    supports_bulk_staging,
)
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.domain import PostingIdentityStatus, PostingStatus
from clearscout.domain.identity import normalize_url
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import PublishedFact
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PendingDiscovery, PendingPostingContent

# Most one source may stage from a single scan. The board is still read whole,
# so absence still decides withdrawal.
SOURCE_STAGE_LIMIT = 200


def screen_listings(
    listings: list[SourceListing],
    parameters: JobSearchParameters,
    targets: list[str],
    *,
    source_employer: str,
) -> tuple[
    list[SourceListing],
    list[tuple[SourceListing, StructuredHoldout]],
    list[tuple[SourceListing, DiscoveryScreening]],
]:
    """Separate reviewable, hard-refused, and recoverable uncertain listings."""
    candidates: list[tuple[SourceListing, DiscoveryScreening]] = []
    refused: list[tuple[SourceListing, StructuredHoldout]] = []
    unsupported: list[tuple[SourceListing, DiscoveryScreening]] = []
    for listing in listings:
        item = _with_summary_focus_terms(listing, parameters)
        screening = screen_discovery(
            item.employer,
            item.title,
            item.location,
            parameters,
            targets,
            item.facts,
            summary=item.summary,
            source_employer=source_employer,
        )
        holdout = structured_holdout(
            item.facts, item.location, screening, parameters, item.published_description
        )
        if holdout is not None:
            refused.append((item, holdout))
        elif item.title and supports_bulk_staging(screening, include_bounded_review=True):
            candidates.append((item, screening))
        else:
            unsupported.append((item, screening))
    selected, _family_omitted, stage_omitted = select_title_survivors(
        candidates, SOURCE_STAGE_LIMIT
    )
    return selected + stage_omitted, refused, unsupported


def listed_title_screen(
    parameters: JobSearchParameters, targets: list[str], *, source_employer: str
) -> Callable[[str], bool] | None:
    """Return the pre-read title screen, or None when the search states no title."""
    if not any(term.strip() for term in targets):
        return None
    return partial(
        listed_title_may_qualify,
        parameters=parameters,
        searched=targets,
        source_employer=source_employer,
    )


def body_screened_listings(
    database: Database,
    workspace_id: str,
    listings: list[SourceListing],
    placed: StagedListings,
) -> dict[str, tuple[FunnelScreeningFinding, ...]]:
    """Return current verified-body dismissal findings keyed by stable listing URL."""
    ids = placed.row_ids(listings)
    if not ids:
        return {}
    with database.session() as session:
        latest = {
            event.pending_discovery_id: event
            for event in session.scalars(latest_disposition_statement(workspace_id, ids))
        }
        # A dismissal is this scan's finding only while a body screen judged the current
        # body; a re-read that could not verify identity left an older verdict unrevisited.
        screened_now = set(
            session.scalars(
                select(PendingDiscovery.id)
                .join(
                    PendingPostingContent,
                    PendingPostingContent.id == PendingDiscovery.current_content_id,
                )
                .where(
                    PendingDiscovery.id.in_(list(latest)),
                    PendingPostingContent.identity_status == PostingIdentityStatus.VERIFIED.value,
                    PendingPostingContent.posting_status == PostingStatus.ACTIVE.value,
                )
            )
        )

    findings_by_row: dict[str, tuple[FunnelScreeningFinding, ...]] = {}
    for pending_id, event in latest.items():
        if (
            pending_id not in screened_now
            or event.action != "dismiss"
            or not event.origin.startswith("source.")
            or not event.origin.endswith(".body_screen")
            or event.category not in BODY_REFUSAL_CATEGORIES
        ):
            continue
        findings = tuple(
            FunnelScreeningFinding(
                dimension=str(finding["dimension"]),
                code=str(finding["code"]),
                subject=str(finding.get("subject", "")),
            )
            for finding in event.screening.get("findings", [])
            if finding.get("code") == event.category
        )
        if findings:
            findings_by_row[pending_id] = findings
    return {
        normalize_url(url): findings_by_row[pending_id]
        for url, pending_id in placed.row_ids_by_url.items()
        if pending_id in findings_by_row
    }


def _with_summary_focus_terms(
    listing: SourceListing, parameters: JobSearchParameters
) -> SourceListing:
    """Keep the saved focus terms a listing's summary states, so a rescreen can read them."""
    terms = summary_focus_terms(parameters, listing.summary)
    if not terms:
        return listing
    stated = PublishedFact(values=terms, provenance="posting.summary", complete=True)
    return replace(listing, facts=replace(listing.facts, focus_terms=stated))
