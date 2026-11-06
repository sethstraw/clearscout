"""Publisher search rows: the target each row carries and the scope it belongs to."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.orm import Session

from clearscout.application.discovery.source_continuation.family import (
    current_scan_family_ids,
    fresh_root_task,
    select_frontier_rows,
)
from clearscout.application.discovery.source_continuation.limits import POSTING_DETAIL_COMPLETED
from clearscout.application.discovery.source_continuation.targets import (
    frontier_target,
    owner_source_id,
)
from clearscout.application.discovery.source_lifecycle import source_read_address
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
)
from clearscout.domain.identity import normalize_fetch_url
from clearscout.infrastructure.persistence.models import (
    Source,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from clearscout.infrastructure.web_fetch.board_adapters import STRUCTURED_PAGE_CURSOR_REASONS
from clearscout.infrastructure.web_fetch.board_reads import (
    NATIVE_LISTED_PLATFORMS,
    publisher_search_scope,
    scope_owned_by_root,
)


def own_listing_pairs(
    pairs: tuple[tuple[str, str], ...], normalized_url: str
) -> tuple[tuple[str, str], ...]:
    """Keep the identity pairs whose URL is the row's own address."""
    return tuple(pair for pair in pairs if normalize_fetch_url(pair[1]) == normalized_url)


def own_search_detail_target(
    normalized_url: str, target: SourceContinuationTarget
) -> SourceContinuationTarget:
    """Keep only a search-detail target's own settled pair; page rows carry the union."""
    checkpoint = target.detail_checkpoint
    if checkpoint is None or not target.deferred_reason.endswith("_search_detail"):
        return target
    return replace(
        target,
        detail_checkpoint=replace(
            checkpoint, completed=own_listing_pairs(checkpoint.completed, normalized_url)
        ),
    )


def _publisher_search_owned_detail_target(
    row: SourceScanFrontierItem,
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    *,
    reopen: bool,
) -> SourceContinuationTarget | None:
    own = tuple(
        item
        for item, url in (checkpoint.listing_urls if reopen else checkpoint.completed)
        if normalize_fetch_url(url) == row.normalized_url
    )
    excluded = set(checkpoint.closed_ids).union(dict(checkpoint.protected))
    if len(own) != 1 or own[0] in excluded:
        return None
    return replace(
        target,
        deferred_reason=checkpoint.platform + "_search_detail",
        detail_checkpoint=replace(
            checkpoint,
            pending_ids=own,
            completed=()
            if reopen
            else tuple(pair for pair in checkpoint.completed if pair[0] not in own),
            listing_facts=(),
            failed_attempts=(),
        ),
    )


def _native_listing_row_target(
    row: SourceScanFrontierItem,
    platform: str,
) -> SourceContinuationTarget | None:
    """Reconstruct native receipt scope without reactivating completed work."""
    native = platform.removeprefix("listed:")
    try:
        target = frontier_target(row)
    except ValueError:
        return None
    if row.status == "completed" and row.deferred_reason in {
        "",
        POSTING_DETAIL_COMPLETED,
        "protected_posting",
    }:
        target = replace(
            target,
            deferred_reason=native
            + (
                "_sitemap_pending"
                if platform.startswith("listed:")
                and normalize_fetch_url(target.url) != normalize_fetch_url(target.source_url)
                else "_posting_pending"
            ),
        )
    return target if publisher_search_scope(target) is not None else None


def publisher_search_row_target(
    row: SourceScanFrontierItem, *, reopen: bool = False
) -> SourceContinuationTarget | None:
    """Return the publisher search target a frontier row carries, or None for other rows."""
    if row.detail_checkpoint is None:
        return None
    platform = str(row.detail_checkpoint.get("platform", ""))
    native = platform.removeprefix("listed:")
    if native in NATIVE_LISTED_PLATFORMS:
        return _native_listing_row_target(row, platform)
    registered_detail = platform + "_search_detail" in STRUCTURED_PAGE_CURSOR_REASONS
    completed_search = row.status == "completed" and (
        platform.removeprefix("search:").endswith("_search_page") or registered_detail
    )
    if (
        not row.deferred_reason.endswith(("_search_page", "_search_detail"))
        and not completed_search
    ):
        return None
    try:
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        target = frontier_target(row)
    except ValueError:
        return None
    if row.status == "completed" and not row.deferred_reason and platform.startswith("search:"):
        target = replace(target, deferred_reason=platform.removeprefix("search:"))
    reopen_blank_detail = reopen and not row.deferred_reason and registered_detail
    if row.status == "completed" and (
        row.deferred_reason == POSTING_DETAIL_COMPLETED or reopen_blank_detail
    ):
        owned_target = _publisher_search_owned_detail_target(row, target, checkpoint, reopen=reopen)
        if owned_target is None:
            return None
        target = owned_target
    return target if publisher_search_scope(target) is not None else None


def failed_publisher_search_detail(row: SourceScanFrontierItem) -> bool:
    """Identify failed publisher details whose retained retry history stays durable."""
    return (
        row.status == "failed"
        and row.deferred_reason.endswith(("_search_detail", "_posting_pending"))
        and row.deferred_reason != "alignerr_search_detail"
        and publisher_search_row_target(row) is not None
    )


def publisher_read_root(items: list[SourceScanFrontierItem], root_url: str) -> str:
    """Read a chunk under its site-wide publisher scope when the source sits below it."""
    scopes = {
        publisher_search_scope(target) if (target := publisher_search_row_target(item)) else None
        for item in items
    }
    scope = next(iter(scopes)) if len(scopes) == 1 else None
    if scope is not None and scope_owned_by_root(scope[1], root_url):
        return scope[1]
    return root_url


def publisher_search_receipts(
    session: Session,
    continuation: SourceScanContinuation,
    scope: tuple[str, str],
    family: frozenset[str],
) -> tuple[SourceDetailCheckpoint, ...]:
    """Return the scan family's detail checkpoints for one publisher search scope."""
    rows = session.scalars(
        select_frontier_rows().where(
            SourceScanFrontierItem.continuation_id == continuation.id,
            SourceScanFrontierItem.correlation_id.in_(family),
        )
    )
    return tuple(
        SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        for row in rows
        if (target := publisher_search_row_target(row)) is not None
        and publisher_search_scope(target) == scope
    )


def fresh_search_detail_retry_ids(
    session: Session,
    continuation: SourceScanContinuation,
    items: list[SourceScanFrontierItem],
) -> frozenset[str]:
    """Select current fresh declarations whose retained failures need a single retry."""
    if fresh_root_task(session, continuation.correlation_id) is None:
        return frozenset()
    source = session.get(Source, owner_source_id(session, continuation.source_scan_id))
    if source is None:
        return frozenset()
    root, _ = source_read_address(source.url, source.resolved_url)
    family = current_scan_family_ids(session, continuation)
    return frozenset(
        item.id
        for item in items
        if item.automatic_eligible
        and item.correlation_id in family
        and (target := publisher_search_row_target(item)) is not None
        and target.deferred_reason.endswith("_search_detail")
        and target.deferred_reason != "alignerr_search_detail"
        and target.detail_checkpoint is not None
        and target.detail_checkpoint.failed_attempts
        and scope_owned_by_root(target.source_url, root)
    )


def verified_search_members(
    receipts: tuple[SourceDetailCheckpoint, ...],
) -> dict[tuple[str, str], frozenset[str]]:
    """Return member ids and URLs per publisher search scope whose listing verified its total."""
    grouped: dict[tuple[str, str], list[SourceDetailCheckpoint]] = {}
    for receipt in receipts:
        page = receipt.platform.removeprefix("search:")
        if receipt.platform.startswith("search:") and page.endswith("_search_page"):
            scope = (page.removesuffix("_search_page"), normalize_fetch_url(receipt.board_url))
            grouped.setdefault(scope, []).append(receipt)
    verified: dict[tuple[str, str], frozenset[str]] = {}
    for scope, group in grouped.items():
        membership = publisher_page_membership(group, scope)
        # A read stopped short publishes the members it saw, so a declared total must match.
        if (
            scope[0] != "alignerr"
            and membership is not None
            and all(receipt.listing_total is not None for receipt in group)
            and listing_receipts_agree(group, membership)
        ):
            verified[scope] = frozenset(
                (*membership, *(normalize_fetch_url(url) for url in membership.values()))
            )
    return verified


def row_publisher_scope(row: SourceScanFrontierItem) -> tuple[str, str] | None:
    """Return the publisher search scope of a frontier row, if it has one."""
    target = publisher_search_row_target(row)
    return publisher_search_scope(target) if target is not None else None


def sole_publisher_scope(
    session: Session,
    continuation: SourceScanContinuation,
    family: frozenset[str],
    root_url: str,
) -> tuple[tuple[str, str], set[str]] | None:
    """Return the one publisher scope and the URLs of rows that carry no scope."""
    rows = session.scalars(
        select_frontier_rows().where(SourceScanFrontierItem.continuation_id == continuation.id)
    ).all()
    row_scopes = [(row.normalized_url, row_publisher_scope(row)) for row in rows]
    scopes = {scope for _, scope in row_scopes if scope is not None}
    # A second publisher holds membership this listing does not name.
    if len(scopes) != 1 or not any(row.correlation_id in family for row in rows):
        return None
    [scope] = scopes
    if scope[0] == "alignerr" or not scope_owned_by_root(scope[1], root_url):
        return None
    return scope, {url for url, row_scope in row_scopes if row_scope is None}


def publisher_page_membership(
    receipts: Sequence[SourceDetailCheckpoint], scope: tuple[str, str]
) -> dict[str, str] | None:
    """Map listing identities to URLs across a scope's page receipts; None on a conflict."""
    page_platform = (
        "listed:" + scope[0]
        if scope[0] in NATIVE_LISTED_PLATFORMS
        else "search:" + scope[0] + "_search_page"
    )
    membership: dict[str, str] = {}
    for checkpoint in receipts:
        if checkpoint.platform != page_platform:
            continue
        for identity, url in checkpoint.listing_urls:
            if membership.setdefault(identity, url) != url:
                return None
    return membership


def listing_receipts_agree(
    receipts: Sequence[SourceDetailCheckpoint], membership: dict[str, str]
) -> bool:
    """Return whether stable single-pass receipts agree on one total for the membership."""
    totals = {checkpoint.listing_total for checkpoint in receipts} - {None}
    return bool(membership) and not (
        any(checkpoint.listing_unstable or checkpoint.listing_pass > 1 for checkpoint in receipts)
        or totals - {len(membership)}
    )
