"""Refreshing a known frontier row from the target a later read declares for it."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.family import (
    current_scan_family_ids,
    fresh_root_task,
)
from tailorcv.application.discovery.source_continuation.limits import (
    CARRIED_ORIGIN,
    DISCOVERED_DURING_FOLLOW_UP,
    MAX_AUTOMATIC_ATTEMPTS,
)
from tailorcv.application.discovery.source_continuation.targets import retry_status
from tailorcv.application.discovery.source_lifecycle import source_read_address
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
)
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.persistence.models import (
    Source,
    SourceScanContinuation,
    SourceScanFrontierItem,
)
from tailorcv.infrastructure.web_fetch.board_adapters import STRUCTURED_PAGE_CURSOR_REASONS
from tailorcv.infrastructure.web_fetch.board_reads import (
    publisher_search_scope,
    scope_owned_by_root,
)


def follow_up_target(target: SourceContinuationTarget) -> SourceContinuationTarget:
    """Cap eligibility for a target discovered mid-chain; a page cursor is exempt and finite."""
    if target.deferred_reason in STRUCTURED_PAGE_CURSOR_REASONS:
        return target
    return replace(target, automatic_eligible=False, deferred_reason=DISCOVERED_DURING_FOLLOW_UP)


def refresh_declared_page_scope(
    row: SourceScanFrontierItem,
    target: SourceContinuationTarget,
    correlation_id: str | None,
) -> None:
    """Refresh observed page membership without changing its failure history.

    A fresh search cursor at a held page carries its own eligibility, since the page-only
    address merges both cursors into this one row.
    """
    checkpoint = target.detail_checkpoint
    if (
        row.detail_checkpoint is None
        or checkpoint is None
        or not (
            checkpoint.platform == "search:alignerr_search_page"
            or checkpoint.platform.startswith("search:phenom_page:")
            or (
                target.deferred_reason.endswith("_search_page")
                and publisher_search_scope(target) is not None
            )
        )
    ):
        return
    prior = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
    if (
        prior.platform == checkpoint.platform
        and normalize_fetch_url(prior.board_url) == normalize_fetch_url(checkpoint.board_url)
        and checkpoint.listing_urls
    ):
        row.detail_checkpoint = checkpoint.as_json()
        row.correlation_id = correlation_id
        if target.deferred_reason.endswith("_search_page") and (
            target.deferred_reason != "alignerr_search_page"
        ):
            row.automatic_eligible = target.automatic_eligible


def refresh_declared_search_detail(
    session: Session,
    source_id: str,
    row: SourceScanFrontierItem,
    target: SourceContinuationTarget,
    correlation_id: str | None,
) -> None:
    """Refresh a known search-detail row from the checkpoint a later read declares for it."""
    if (
        row.status not in {"pending", "failed"}
        or row.deferred_reason not in {"", target.deferred_reason}
        or not target.deferred_reason.endswith("_search_detail")
        or target.deferred_reason == "alignerr_search_detail"
        or publisher_search_scope(target) is None
        or target.detail_checkpoint is None
    ):
        return
    source = session.get(Source, source_id)
    if source is None:
        return
    root, _ = source_read_address(source.url, source.resolved_url)
    if not scope_owned_by_root(target.source_url, root) or normalize_fetch_url(
        row.source_url
    ) != normalize_fetch_url(target.source_url):
        return
    checkpoint = target.detail_checkpoint
    prior = (
        SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if row.detail_checkpoint is not None
        else None
    )
    if prior is not None and (
        prior.platform != checkpoint.platform
        or normalize_fetch_url(prior.board_url) != row.normalized_url
        or prior.pending_ids != checkpoint.pending_ids
    ):
        return
    attempts = max(
        row.attempt_count,
        dict(checkpoint.failed_attempts).get(checkpoint.pending_ids[0], 0),
        dict(prior.failed_attempts).get(checkpoint.pending_ids[0], 0) if prior is not None else 0,
    )
    continuation = session.get(SourceScanContinuation, row.continuation_id)
    reopened = (
        prior is not None
        and target.automatic_eligible
        and continuation is not None
        and fresh_root_task(session, continuation.correlation_id) is not None
        and row.correlation_id not in current_scan_family_ids(session, continuation)
    )
    if row.status == "failed":
        if not reopened:
            return
        row.status = "pending"
    row.url = target.url
    row.deferred_reason = target.deferred_reason
    row.source_url = target.source_url
    row.correlation_id = correlation_id
    row.detail_checkpoint = replace(
        checkpoint,
        failed_attempts=((checkpoint.pending_ids[0], attempts),) if attempts else (),
    ).as_json()
    row.automatic_eligible = target.automatic_eligible and (row.automatic_eligible or reopened)


def refresh_reemitted_cursor(
    row: SourceScanFrontierItem, target: SourceContinuationTarget, correlation_id: str | None
) -> None:
    """Let a rescan that declares an untried manual page cursor again as automatic queue it."""
    if (
        target.deferred_reason not in STRUCTURED_PAGE_CURSOR_REASONS
        or row.status != "pending"
        or row.automatic_eligible
        or not target.automatic_eligible
        or row.attempt_count
        or row.last_error
        or row.detail_checkpoint is not None
        or target.detail_checkpoint is not None
        or row.deferred_reason != target.deferred_reason
    ):
        return
    row.automatic_eligible = True
    row.correlation_id = correlation_id


def refresh_classification_hold(
    row: SourceScanFrontierItem, target: SourceContinuationTarget
) -> None:
    """Refresh classification holds from declared detail work without resetting attempts."""
    if (
        row.detail_checkpoint is not None
        and target.detail_checkpoint is not None
        and row.deferred_reason == target.deferred_reason == "alignerr_search_detail"
    ):
        checkpoint = SourceDetailCheckpoint.from_json(row.detail_checkpoint)
        if (
            checkpoint.platform == target.detail_checkpoint.platform
            and checkpoint.pending_ids == target.detail_checkpoint.pending_ids
        ):
            row.detail_checkpoint = replace(
                checkpoint, listing_facts=target.detail_checkpoint.listing_facts
            ).as_json()
        return
    unattempted = row.attempt_count == 0 and not row.last_error
    classification_deferred = row.last_error == f"Publisher read deferred: {row.deferred_reason}."
    # Failed rows can be pending with zero attempts after carry-forward.
    if (
        row.status != "pending"
        or not (unattempted or classification_deferred)
        or row.origin == CARRIED_ORIGIN
        or row.automatic_eligible
        or row.detail_checkpoint is not None
        or row.deferred_reason
        not in {"classification_ambiguous", "classification_error_or_challenge"}
        or target.detail_checkpoint is None
    ):
        return
    row.url = target.url
    row.label = target.label
    row.label_source = target.label_source
    row.source_url = target.source_url
    row.structural_context = target.structural_context
    row.relations = list(target.relations)
    row.depth = target.depth
    row.automatic_eligible = (
        target.automatic_eligible and row.attempt_count < MAX_AUTOMATIC_ATTEMPTS
    )
    row.deferred_reason = target.deferred_reason
    row.detail_checkpoint = target.detail_checkpoint.as_json()


def update_deferred_frontier_row(
    row: SourceScanFrontierItem,
    target: SourceContinuationTarget,
    correlation_id: str,
    *,
    fresh_detail_retry: bool,
) -> None:
    """Retain one attempted row's publisher deferral and recovery eligibility."""
    refresh_declared_page_scope(row, target, correlation_id)
    row.automatic_eligible = row.automatic_eligible and target.automatic_eligible
    if fresh_detail_retry and not target.automatic_eligible:
        row.correlation_id = correlation_id
    row.deferred_reason = target.deferred_reason
    row.detail_checkpoint = (
        target.detail_checkpoint.as_json() if target.detail_checkpoint is not None else None
    )
    row.status = retry_status(row)
    row.last_error = (
        f"Publisher read deferred: {target.deferred_reason}."
        if target.deferred_reason
        else "The publisher URL remained unreadable after this attempt."
    )
