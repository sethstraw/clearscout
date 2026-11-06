"""Publisher search body receipts, and the listing coverage they prove."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.detail_checkpoints import (
    detail_count_checkpoints,
)
from clearscout.application.discovery.source_continuation.family import (
    current_scan_family_ids,
    select_frontier_rows,
)
from clearscout.application.discovery.source_continuation.frontier_recording import (
    record_publisher_listing_receipts,
)
from clearscout.application.discovery.source_continuation.publisher_targets import (
    listing_receipts_agree,
    own_listing_pairs,
    publisher_page_membership,
    publisher_search_receipts,
    publisher_search_row_target,
    row_publisher_scope,
    sole_publisher_scope,
)
from clearscout.application.discovery.source_continuation.rescreen import rescreen_cutoff
from clearscout.application.discovery.source_continuation.targets import frontier_target
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import (
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceRead,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.persistence.models import (
    Source,
    SourceScan,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from clearscout.infrastructure.web_fetch.board_reads import (
    NATIVE_LISTED_PLATFORMS,
    publisher_search_scope,
    scope_owned_by_root,
)


def _publisher_search_receipt_view(
    read: SourceRead,
    owned: list[SourceScanFrontierItem],
    scope: tuple[str, str],
) -> SourceRead:
    deferred = tuple(
        target for target in read.deferred_targets if publisher_search_scope(target) == scope
    )
    if not all(
        item.deferred_reason.endswith(("_search_detail", "_posting_pending")) for item in owned
    ):
        return replace(read, deferred_targets=deferred, exhausted_outcome=None)
    addresses = {item.normalized_url for item in owned}
    known = addresses | {
        normalize_fetch_url(url)
        for item in owned
        if item.detail_checkpoint is not None
        for _identity, url in SourceDetailCheckpoint.from_json(item.detail_checkpoint).completed
    }
    return replace(
        read,
        detail_read_urls=tuple(
            url for url in read.detail_read_urls or () if normalize_fetch_url(url) in known
        ),
        deferred_targets=tuple(
            target for target in deferred if normalize_fetch_url(target.url) in addresses
        ),
        exhausted_outcome=None,
        publisher_listing_receipts=(),
    )


def retain_publisher_search_read(
    session: Session,
    scan: SourceScan,
    items: list[SourceScanFrontierItem],
    read: SourceRead,
) -> bool:
    """Retain one publisher scope's body receipts from a chunk read; False when none apply."""
    if read.detail_read_urls is None or not items:
        return False
    source = session.get(Source, scan.source_id)
    continuation = session.get(SourceScanContinuation, items[0].continuation_id)
    if source is None or continuation is None:
        return False
    root, _ = source_read_address(source.url, source.resolved_url)
    family = current_scan_family_ids(session, continuation)
    selected = [
        (item, scope)
        for item in items
        if item.correlation_id in family
        and (scope := publisher_search_scope(frontier_target(item))) is not None
        and scope[0] != "alignerr"
        and scope_owned_by_root(scope[1], root)
    ]
    scopes = {scope for _item, scope in selected}
    if len(scopes) != 1:
        return False
    scope = next(iter(scopes))
    owned = [item for item, _scope in selected]
    receipt = _publisher_search_receipt_view(read, owned, scope)
    if any(
        publisher_search_scope(frontier_target(item), posting_urls=receipt.detail_read_urls or ())
        != scope
        for item in owned
    ):
        return False
    if any(item.deferred_reason.endswith("_search_page") for item in owned) and any(
        item.correlation_id not in family
        and publisher_search_scope(frontier_target(item)) is not None
        for item in items
    ):
        return False
    return retain_publisher_body_receipt(
        session,
        scan,
        owned,
        receipt,
        continuation,
        scope,
        family,
        page_raw=read.raw_count
        if len(owned) == len(items)
        and all(item.deferred_reason.endswith("_search_page") for item in owned)
        else 0,
    )


def _publisher_represented_membership(
    receipts: tuple[SourceDetailCheckpoint, ...],
    membership: dict[str, str],
    current: dict[str, str],
) -> dict[str, str | None]:
    variants: dict[str, set[str]] = {}
    for checkpoint in receipts:
        for item, url in checkpoint.listing_urls:
            variants.setdefault(item, set()).add(url)
    for item, url in membership.items():
        variants.setdefault(item, set()).add(url)
    represented = {
        item: next(iter(urls)) if len(urls) == 1 else None for item, urls in variants.items()
    }
    represented.update(current)
    return represented


def _publisher_enumerated_membership(
    items: list[SourceScanFrontierItem],
    checkpoints: tuple[SourceDetailCheckpoint, ...],
    read: SourceRead,
    scope: tuple[str, str],
) -> dict[str, str]:
    page_platform = (
        "listed:" + scope[0]
        if scope[0] in NATIVE_LISTED_PLATFORMS
        else "search:" + scope[0] + "_search_page"
    )
    pages = tuple(checkpoint for checkpoint in checkpoints if checkpoint.platform == page_platform)
    enumeration = dict(pair for checkpoint in pages for pair in checkpoint.listing_urls)
    page_targets = tuple(
        frontier_target(item)
        for item in items
        if item.deferred_reason.endswith(
            ("_search_page", "_sitemap_pending", "_detail_rows_pending")
        )
    )
    enumeration.update(
        pair
        for checkpoint in read.publisher_listing_receipts
        if any(
            publisher_search_scope(replace(target, detail_checkpoint=checkpoint)) == scope
            for target in page_targets
        )
        for pair in checkpoint.listing_urls
    )
    return enumeration


def _retain_publisher_observation_counts(
    scan: SourceScan,
    receipts: tuple[SourceDetailCheckpoint, ...],
    represented: dict[str, str | None],
    bodies: dict[str, str],
    page_platform: str,
    page_raw: int,
) -> None:
    completed = {
        url
        for checkpoint in receipts
        for item, url in checkpoint.completed
        if represented.get(item) == url
    } | {url for item, url in bodies.items() if represented.get(item) == url}
    enumerated_ids = {
        identity
        for checkpoint in receipts
        if checkpoint.platform == page_platform
        for identity in checkpoint.listing_ids
    }
    observed_ids = {
        identity for checkpoint in receipts for identity, _url in checkpoint.completed
    } | set(bodies)
    scan.raw_count = max(scan.raw_count or 0, len(enumerated_ids | observed_ids), page_raw)
    scan.read_count = len(completed)
    if (
        any(checkpoint.platform == page_platform for checkpoint in receipts)
        and observed_ids - enumerated_ids
        and (scan.complete or scan.exhausted_outcome is not None)
    ):
        scan.complete = False
        scan.status = "incomplete"
        scan.read_outcome = SourceReadOutcome.PUBLISHER_LINKED_PAGES
        scan.discovery_truncated = True
        scan.exhausted_outcome = None
        scan.detail_coverage = None
        scan.skip_reasons = {
            **scan.skip_reasons,
            "publisher_listing_changed": max(
                scan.skip_reasons.get("publisher_listing_changed", 0), 1
            ),
        }


def _native_completed_bodies(
    receipts: tuple[SourceDetailCheckpoint, ...],
    represented: dict[str, str | None],
    bodies: dict[str, str],
    enumerated: set[str],
) -> dict[str, str]:
    """Keep completed native identities with one represented original URL."""
    completed = {
        identity: url
        for checkpoint in receipts
        for identity, url in checkpoint.completed
        if identity in enumerated and represented.get(identity) == url
    }
    completed.update(
        (identity, url)
        for identity, url in bodies.items()
        if identity in enumerated and represented.get(identity) == url
    )
    return completed


def _update_native_listing_receipts(
    session: Session,
    continuation: SourceScanContinuation,
    scope: tuple[str, str],
    family: frozenset[str],
    receipts: tuple[SourceDetailCheckpoint, ...],
    represented: dict[str, str | None],
    completed: dict[str, str],
    page_platform: str,
) -> None:
    """Persist completed and protected observations on current native page receipts."""
    protected = {
        identity: url
        for checkpoint in receipts
        for identity, url in checkpoint.protected
        if represented.get(identity) == url and identity not in completed
    }
    for row in session.scalars(
        select_frontier_rows().where(
            SourceScanFrontierItem.continuation_id == continuation.id,
            SourceScanFrontierItem.correlation_id.in_(family),
        )
    ):
        if (target := publisher_search_row_target(row)) is None or publisher_search_scope(
            target
        ) != scope:
            continue
        checkpoint = target.detail_checkpoint
        if checkpoint is None or checkpoint.platform != page_platform:
            continue
        owned = dict(checkpoint.listing_urls)
        row.detail_checkpoint = replace(
            checkpoint,
            completed=tuple(
                (identity, url) for identity, url in completed.items() if owned.get(identity) == url
            ),
            protected=tuple(
                (identity, url) for identity, url in protected.items() if owned.get(identity) == url
            ),
        ).as_json()


def _retain_native_listed_counts(
    session: Session,
    scan: SourceScan,
    continuation: SourceScanContinuation,
    scope: tuple[str, str],
    family: frozenset[str],
    receipts: tuple[SourceDetailCheckpoint, ...],
    represented: dict[str, str | None],
    bodies: dict[str, str],
    page_platform: str,
) -> bool:
    """Count current native identities whose exact published bodies were committed."""
    if not any(checkpoint.platform == page_platform for checkpoint in receipts):
        return False
    enumerated = {
        identity
        for checkpoint in receipts
        if checkpoint.platform == page_platform
        for identity in checkpoint.listing_ids
    }
    completed = _native_completed_bodies(receipts, represented, bodies, enumerated)
    _update_native_listing_receipts(
        session, continuation, scope, family, receipts, represented, completed, page_platform
    )
    scan.raw_count = len(enumerated)
    scan.read_count = len(completed)
    return True


def _native_enumeration_receipts(
    items: list[SourceScanFrontierItem],
    read: SourceRead,
    scope: tuple[str, str],
) -> tuple[SourceDetailCheckpoint, ...]:
    """Validate incoming native membership against the owning page cursors."""
    pages = tuple(
        frontier_target(item)
        for item in items
        if item.deferred_reason.endswith(("_sitemap_pending", "_detail_rows_pending"))
    )
    return tuple(
        checkpoint
        for checkpoint in read.publisher_listing_receipts
        if checkpoint.platform == "listed:" + scope[0]
        and any(
            publisher_search_scope(replace(target, detail_checkpoint=checkpoint)) == scope
            for target in pages
        )
    )


def _publisher_body_membership(
    checkpoints: tuple[SourceDetailCheckpoint, ...],
    *,
    native: bool,
) -> dict[str, str]:
    """Keep exact publisher identity URLs and abstain on native variants."""
    if native:
        return {
            identity: url
            for identity, url in _publisher_represented_membership(checkpoints, {}, {}).items()
            if url is not None
        }
    return dict(pair for checkpoint in checkpoints for pair in checkpoint.listing_urls)


def _publisher_current_bodies(
    read: SourceRead,
    membership: dict[str, str],
    *,
    native: bool,
) -> dict[str, str]:
    """Select mapped full bodies named by the current read's exact proof."""
    return {
        listing.requisition_id: listing.url
        for listing in read.listings
        if listing.published_description
        and listing.url in (read.detail_read_urls or ())
        and (not native or membership.get(listing.requisition_id) == listing.url)
    }


def _publisher_body_proof(
    checkpoints: tuple[SourceDetailCheckpoint, ...],
    read: SourceRead,
    current: dict[str, str],
    *,
    native: bool,
) -> set[str] | None:
    """Reject proof outside declared full bodies or intersecting protected work."""
    known = set(
        dict(pair for checkpoint in checkpoints for pair in checkpoint.completed).values()
    ) | set(current.values())
    if native:
        declared = {pair for checkpoint in checkpoints for pair in checkpoint.listing_urls}
        known.update(
            listing.url
            for listing in read.listings
            if listing.published_description and (listing.requisition_id, listing.url) in declared
        )
    proof = set(read.detail_read_urls or ())
    protected = {url for checkpoint in checkpoints for _identity, url in checkpoint.protected}
    if not proof <= known or proof & (protected | set(read.protected_posting_urls)):
        return None
    return proof


def retain_publisher_body_receipt(
    session: Session,
    scan: SourceScan,
    items: list[SourceScanFrontierItem],
    read: SourceRead,
    continuation: SourceScanContinuation,
    scope: tuple[str, str],
    family: frozenset[str],
    *,
    page_raw: int,
) -> bool:
    """Persist the bodies a read proves for one publisher scope and update the scan counts."""
    checkpoints = detail_count_checkpoints(items, read)
    if any(publisher_search_scope(target) != scope for target in read.deferred_targets):
        return False
    native = scope[0] in NATIVE_LISTED_PLATFORMS
    if native:
        checkpoints = (*checkpoints, *_native_enumeration_receipts(items, read, scope))
    membership = _publisher_body_membership(checkpoints, native=native)
    bodies = dict(pair for checkpoint in checkpoints for pair in checkpoint.completed)
    current = _publisher_current_bodies(read, membership, native=native)
    membership.update(current)
    proof = _publisher_body_proof(checkpoints, read, current, native=native)
    if proof is None:
        return False
    bodies.update(current)
    bodies = {
        item: url for item, url in bodies.items() if membership.get(item) == url and url in proof
    }
    page_platform = "listed:" + scope[0] if native else "search:" + scope[0] + "_search_page"
    enumeration = _publisher_enumerated_membership(items, checkpoints, read, scope)
    _persist_publisher_body_receipt(
        session, items, checkpoints, bodies, read, enumeration=enumeration
    )
    receipts = publisher_search_receipts(session, continuation, scope, family)
    represented = _publisher_represented_membership(receipts, membership, {} if native else current)
    if native:
        return _retain_native_listed_counts(
            session, scan, continuation, scope, family, receipts, represented, bodies, page_platform
        )
    _retain_publisher_observation_counts(
        scan, receipts, represented, bodies, page_platform, page_raw
    )
    return True


def _publisher_body_checkpoint(
    checkpoint: SourceDetailCheckpoint,
    retained: dict[str, str],
    bodies: dict[str, str],
    read: SourceRead,
    *,
    unstable: bool,
) -> SourceDetailCheckpoint:
    """Apply exact body and protected observations without erasing pending failure history."""
    retained_bodies = {
        identity: url for identity, url in bodies.items() if retained.get(identity) == url
    }
    protected = {
        identity: url for identity, url in checkpoint.protected if retained.get(identity) == url
    }
    if checkpoint.platform.removeprefix("listed:") in NATIVE_LISTED_PLATFORMS:
        protected.update(
            (identity, url)
            for identity, url in retained.items()
            if url in read.protected_posting_urls and identity not in retained_bodies
        )
    pending = tuple(
        identity
        for identity in checkpoint.pending_ids
        if identity not in retained_bodies and identity not in protected
    )
    return replace(
        checkpoint,
        completed=tuple(retained_bodies.items()),
        protected=tuple(protected.items()),
        pending_ids=pending,
        failed_attempts=tuple(pair for pair in checkpoint.failed_attempts if pair[0] in pending),
        listing_facts=tuple(pair for pair in checkpoint.listing_facts if pair[0] in pending),
        listing_ids=tuple(retained),
        listing_urls=tuple(retained.items()),
        listing_unstable=unstable,
        pass_listing_ids=tuple(
            identity for identity in checkpoint.pass_listing_ids if identity in retained
        ),
    )


def _record_native_page_receipts(
    session: Session,
    items: list[SourceScanFrontierItem],
    read: SourceRead,
) -> None:
    """Record native enumeration on its existing continuation and source owner."""
    native_pages = tuple(
        checkpoint
        for checkpoint in read.publisher_listing_receipts
        if checkpoint.platform.startswith("listed:")
    )
    if items and native_pages:
        source_id = session.scalar(
            select(SourceScan.source_id)
            .join(SourceScanContinuation, SourceScanContinuation.source_scan_id == SourceScan.id)
            .where(SourceScanContinuation.id == items[0].continuation_id)
        )
        if source_id is not None:
            rows = {
                row.normalized_url: row
                for row in session.scalars(
                    select_frontier_rows().where(
                        SourceScanFrontierItem.continuation_id == items[0].continuation_id
                    )
                )
            }
            record_publisher_listing_receipts(
                session,
                items[0].continuation_id,
                source_id,
                native_pages,
                items[0].correlation_id,
                rows,
            )


def _persist_publisher_body_receipt(
    session: Session,
    items: list[SourceScanFrontierItem],
    checkpoints: tuple[SourceDetailCheckpoint, ...],
    bodies: dict[str, str],
    read: SourceRead,
    *,
    enumeration: dict[str, str],
) -> None:
    unstable = (
        any(checkpoint.listing_unstable for checkpoint in checkpoints) or read.discovery_truncated
    )
    for item in items:
        stored = session.get(SourceScanFrontierItem, item.id)
        if stored is None or item.detail_checkpoint is None:
            continue
        checkpoint = SourceDetailCheckpoint.from_json(
            stored.detail_checkpoint or item.detail_checkpoint
        )
        listing = checkpoint.listing_urls
        if item.deferred_reason.endswith("_search_detail"):
            # The scope's page rows hold the listing, so a detail row keeps only its own pair.
            listing = own_listing_pairs(listing, stored.normalized_url)
        retained = enumeration if item.deferred_reason.endswith("_search_page") else dict(listing)
        updated = _publisher_body_checkpoint(checkpoint, retained, bodies, read, unstable=unstable)
        stored.detail_checkpoint = updated.as_json()
    _record_native_page_receipts(session, items, read)


def _publisher_settled_pairs(
    session: Session,
    continuation: SourceScanContinuation,
    scan: SourceScan,
    scope: tuple[str, str],
    family: frozenset[str],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return completed and protected pairs, counting same-day completions carried in."""
    cutoff = rescreen_cutoff(session, continuation.workspace_id, continuation.id)
    rows = session.scalars(
        select_frontier_rows().where(SourceScanFrontierItem.continuation_id == continuation.id)
    )
    checkpoints = [
        SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        for row in rows
        if row_publisher_scope(row) == scope
        and (
            row.correlation_id in family
            or (
                row.status == "completed"
                and row.completed_at is not None
                and row.completed_at > cutoff
                and row.completed_at.astimezone(UTC).date() == scan.scanned_on
            )
        )
    ]
    completed = {pair for checkpoint in checkpoints for pair in checkpoint.completed}
    return completed, {pair for checkpoint in checkpoints for pair in checkpoint.protected}


def publisher_listing_coverage(
    session: Session,
    continuation: SourceScanContinuation,
    scan: SourceScan,
) -> SourceDetailCoverage | None:
    """Certify a publisher listing only when every identity it enumerated has a settled body."""
    if scan.exhausted_outcome is None or scan.discovery_truncated:
        return None
    source = session.get(Source, scan.source_id)
    if source is None:
        return None
    root_url, _ = source_read_address(source.url, source.resolved_url)
    family = current_scan_family_ids(session, continuation)
    sole = sole_publisher_scope(session, continuation, family, root_url)
    if sole is None:
        return None
    scope, unscoped = sole
    # Membership, totals and stability come only from this family's own page reads.
    receipts = publisher_search_receipts(session, continuation, scope, family)
    membership = publisher_page_membership(receipts, scope)
    if membership is None or not listing_receipts_agree(receipts, membership):
        return None
    # A row with no scope is a member body the scan's recent read already supplied.
    if not unscoped <= {normalize_fetch_url(url) for url in membership.values()}:
        return None
    listed = set(membership.items())
    completed, protected = _publisher_settled_pairs(session, continuation, scan, scope, family)
    if not listed <= completed | protected:
        return None
    scan.raw_count = len(membership)
    scan.read_count = len(listed & completed)
    return SourceDetailCoverage(
        platform=scope[0], board_url=root_url, posting_urls=tuple(membership.values())
    )


def drained_publisher_listing_coverage(
    session: Session, continuation_id: str, scan: SourceScan
) -> SourceDetailCoverage | None:
    """Certify a publisher listing whose frontier drained before any continue task ran."""
    continuation = session.get(SourceScanContinuation, continuation_id)
    if continuation is None or continuation.source_scan_id != scan.id:
        return None
    return publisher_listing_coverage(session, continuation, scan)
