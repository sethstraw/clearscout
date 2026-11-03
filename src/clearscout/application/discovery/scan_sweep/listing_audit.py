"""The funnel audit rows a sweep writes for each source and listing it read."""

from __future__ import annotations

from dataclasses import replace

from clearscout.application.discovery.funnel_audit import (
    FunnelAuditBuilder,
    FunnelReason,
    FunnelScreeningFinding,
    FunnelStage,
    bulk_staging_refusal_outcome,
)
from clearscout.application.discovery.scan_sweep.models import SweepBoard
from clearscout.application.discovery.screening import (
    DiscoveryScreening,
    StructuredHoldout,
    bulk_staging_refusal,
)
from clearscout.application.discovery.source_reading import SourceListing, SourceRead
from clearscout.domain import SourceReadOutcome
from clearscout.domain.identity import normalize_url


def audit_alignerr_metadata(
    audit: FunnelAuditBuilder, source: str, listings: list[SourceListing]
) -> None:
    """Account for listed identities without claiming a verified readable opening."""
    for listing in listings:
        if alignerr_metadata(listing):
            _audit_listing(
                audit,
                source,
                listing,
                FunnelReason.INCOMPLETE_POSTING,
                detail=("The publisher listed this identity. Its full posting body is unverified."),
            )


def audit_listings(
    audit: FunnelAuditBuilder,
    source: str,
    wanted: list[SourceListing],
    selected: list[SourceListing],
    stage_omitted: list[SourceListing],
    refused: list[tuple[SourceListing, StructuredHoldout]],
    selected_refused: list[SourceListing],
    held_out_pending_urls: set[str],
    unsupported: list[tuple[SourceListing, DiscoveryScreening]],
    new_listings: list[SourceListing],
    *,
    body_screened: dict[str, tuple[FunnelScreeningFinding, ...]],
    unfiltered: bool,
) -> None:
    """Record why every listing staged, remained known, or was not selected."""
    new_urls = {item.url.casefold() for item in new_listings}
    _audit_reviewable_listings(
        audit,
        source,
        wanted,
        selected,
        stage_omitted,
        new_urls,
        body_screened,
        unfiltered=unfiltered,
    )
    refused_screening = {item.url.casefold(): holdout.screening for item, holdout in refused}
    selected_refused_urls = {item.url.casefold() for item in selected_refused}
    for item, holdout in refused:
        if item.url.casefold() in selected_refused_urls:
            continue
        _audit_listing(
            audit,
            source,
            item,
            FunnelReason.HARD_CONSTRAINT_EXCLUDED,
            screening=holdout.screening,
            detail=(
                "Publisher facts conflict with the current search boundaries. "
                "The stable URL remains recoverable and was not added to Pending."
            ),
        )
    for item in selected_refused:
        if item.url.casefold() not in held_out_pending_urls:
            _audit_listing(
                audit,
                source,
                item,
                FunnelReason.ALREADY_KNOWN,
                screening=refused_screening[item.url.casefold()],
            )
            continue
        _audit_listing(
            audit,
            source,
            item,
            FunnelReason.HARD_CONSTRAINT_EXCLUDED,
            screening=refused_screening[item.url.casefold()],
            detail="A known row the program holds out meets a structured search hold-out.",
        )
    for item, screening in unsupported:
        if unfiltered:
            reason = FunnelReason.NO_TITLE_CONFIGURED
            detail = "No title is configured for this source."
        else:
            refusal = bulk_staging_refusal(screening, include_bounded_review=True)
            reason, detail = bulk_staging_refusal_outcome(refusal)
        _audit_listing(
            audit,
            source,
            item,
            reason,
            rejected=unfiltered,
            screening=screening,
            detail=detail,
        )


def _audit_reviewable_listings(
    audit: FunnelAuditBuilder,
    source: str,
    wanted: list[SourceListing],
    selected: list[SourceListing],
    stage_omitted: list[SourceListing],
    new_urls: set[str],
    body_screened: dict[str, tuple[FunnelScreeningFinding, ...]],
    *,
    unfiltered: bool,
) -> None:
    """Audit the bounded reviewable lane independently of hard refusals."""
    if unfiltered:
        for item in wanted:
            _audit_listing(audit, source, item, FunnelReason.NO_TITLE_CONFIGURED, rejected=True)
        return
    for item in selected:
        findings = body_screened.get(normalize_url(item.url))
        if findings:
            _audit_listing(
                audit,
                source,
                item,
                FunnelReason.HARD_CONSTRAINT_EXCLUDED,
                screening_findings=findings,
                detail=(
                    "Verified publisher body content explicitly conflicts with the current "
                    "search boundaries. The dismissal remains individually restorable."
                ),
            )
            continue
        is_new = item.url.casefold() in new_urls
        _audit_listing(
            audit,
            source,
            item,
            FunnelReason.STAGED if is_new else FunnelReason.ALREADY_KNOWN,
            staged=is_new,
        )
    for item in stage_omitted:
        _audit_listing(audit, source, item, FunnelReason.STAGE_LIMIT_REACHED, rejected=True)


def record_source(
    audit: FunnelAuditBuilder,
    board: SweepBoard,
    reason: FunnelReason,
    *,
    fetched: bool = False,
    staged: bool = False,
    detail: str = "",
) -> None:
    """Record the source-level funnel row for one board."""
    audit.record_lead(
        employer=board.employer,
        url=board.url,
        source=board.source.name,
        fetched=fetched,
        staged=staged,
        reason=reason,
        detail=detail,
    )


def _audit_listing(
    audit: FunnelAuditBuilder,
    source: str,
    listing: SourceListing,
    reason: FunnelReason,
    *,
    staged: bool = False,
    rejected: bool = False,
    screening: DiscoveryScreening | None = None,
    screening_findings: tuple[FunnelScreeningFinding, ...] | None = None,
    detail: str = "",
) -> None:
    audit.record_listing(
        listing,
        source=source,
        terminal=(FunnelStage.STAGED if staged else FunnelStage.REJECTED if rejected else None),
        reason=reason,
        detail=detail,
        screening=screening,
        screening_findings=screening_findings,
    )


def alignerr_metadata(listing: SourceListing) -> bool:
    """Identify declared list records without verified full publisher bodies."""
    return bool(
        listing.source
        and listing.source.publisher == "alignerr"
        and not listing.published_description.strip()
    )


def prepare_alignerr_enumeration(
    read: SourceRead, wanted: list[SourceListing], *, unfiltered: bool
) -> SourceRead:
    """Schedule only eligible declared bodies while retaining every list identity."""
    metadata = {item.url for item in read.listings if alignerr_metadata(item)}
    eligible = {item.url for item in wanted} if not unfiltered else set()
    targets = tuple(
        target
        for target in read.deferred_targets
        if target.deferred_reason != "alignerr_search_detail"
        or target.url not in metadata
        or target.url in eligible
    )
    return replace(
        read,
        deferred_targets=targets,
        complete=read.complete and not metadata,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES
        if not targets and metadata
        else read.outcome,
        exhausted_outcome=read.exhausted_outcome if targets else None,
    )
