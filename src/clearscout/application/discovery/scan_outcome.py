"""The status and note a finished scan records, and when a drained frontier settles it."""

from __future__ import annotations

from clearscout.application.discovery.source_reading import (
    DETAIL_FAILURE_EXAMPLE_LIMIT,
    SourceDetailSummary,
)
from clearscout.domain import SourceReadOutcome
from clearscout.infrastructure.persistence.models import SourceScan


def detail_failure_notes_prefix(failed_count: int, examples: tuple[str, ...]) -> str:
    """Prefix scan notes with typed failure examples when detail_failed_count > 0."""
    if failed_count <= 0 or not examples:
        return ""
    return "detail_failure_examples: " + "|".join(examples[:DETAIL_FAILURE_EXAMPLE_LIMIT]) + "\n"


def _staging_note(matched: int, staged: int, refused: int, *, unfiltered: bool) -> str:
    """Return what a scan read and declined to stage."""
    refusal_label = f"hard-constraint exclusion{'s' if refused != 1 else ''}"
    if unfiltered:
        if refused:
            return (
                f"Screened {refused} {refusal_label}; none entered Pending. "
                "No title is stated, so other openings were not "
                "bulk-staged; each screened URL remains recoverable."
            )
        return (
            f"Read {matched} openings and staged none: no title is stated, so every "
            "opening would match. Accept a title to narrow this board."
        )
    notes: list[str] = []
    if matched > staged:
        notes.append(
            f"Matched {matched} reviewable openings and retained {staged}; "
            f"{matched - staged} exceeded the per-source staging limit."
        )
    if refused:
        notes.append(
            f"Screened {refused} {refusal_label}; none entered Pending. "
            "Each screened URL remains recoverable."
        )
    return " ".join(notes)


def _empty_read_note(*, complete: bool) -> str:
    """Return what a scan should say when the board answered with no openings.

    A complete empty read is distinct from a partial one: the platform said
    "nothing here" instead of "here is a bounded slice". Both belong under
    ``completed_empty`` so the operator can tell them apart from a scan that
    staged nothing after filtering.
    """
    if complete:
        return (
            "The board API answered with zero openings for this address. "
            "The scan reached the platform, so this is not a fetch failure; "
            "the source may be idle or its address may be stale."
        )
    return (
        "The board API returned no openings within the bounded read. "
        "The scan reached the platform, so this is not a fetch failure; "
        "a wider window may still surface openings."
    )


INCOMPLETE_READ_NOTE = "The source read is incomplete and remains eligible for recovery."


def _scan_outcome(
    *,
    read_outcome: SourceReadOutcome,
    raw_count: int,
    listings_count: int,
    read_complete: bool,
    skip_reasons: dict[str, int],
    matched: int,
    staged: int,
    refused: int,
    unfiltered: bool,
) -> tuple[str, str]:
    """Return the sub-status and operator-readable note for one completed scan.

    A zero-listing read is ``completed_empty`` regardless of the filter; a
    non-empty read stays ``completed`` and the note explains how the filter
    narrowed it.
    """
    if read_outcome in {
        SourceReadOutcome.CRAWL_CONTINUATION,
        SourceReadOutcome.ACCESS_BLOCKED,
        SourceReadOutcome.SESSION_REQUIRED,
        SourceReadOutcome.RENDER_REQUIRED,
        SourceReadOutcome.POLICY_REFUSED,
        SourceReadOutcome.NO_SUPPORTED_REPRESENTATION,
        SourceReadOutcome.FETCH_FAILED,
    }:
        return "incomplete", INCOMPLETE_READ_NOTE
    if listings_count == 0:
        if raw_count:
            skipped = ", ".join(
                f"{count} {reason.replace('_', ' ')}"
                for reason, count in sorted(skip_reasons.items())
            )
            return (
                "completed_empty",
                f"The board returned {raw_count} raw records, but none became usable "
                f"postings. Skipped: {skipped or 'reason not reported'}.",
            )
        return "completed_empty", _empty_read_note(complete=read_complete)
    return "completed", _staging_note(matched, staged, refused, unfiltered=unfiltered)


# A publisher page read in place of a refused board API cannot speak for the board.
_UNSETTLEABLE_SKIP_REASONS = frozenset(
    {"time_limit_reached", "board_api_access_refused", "board_api_policy_refused"}
)


def may_settle_drained_scan(
    status: str, read_outcome: SourceReadOutcome, skip_reasons: dict[str, int]
) -> bool:
    """Whether a drained frontier is allowed to complete this scan.

    Only a continued read that is still incomplete, did not run out of time, and
    was not a publisher fallback for a refused board API can claim the whole source.
    """
    return (
        status == "incomplete"
        and read_outcome == SourceReadOutcome.CRAWL_CONTINUATION
        and not _UNSETTLEABLE_SKIP_REASONS.intersection(skip_reasons)
    )


def resolve_exhausted_outcome(
    *,
    has_frontier: bool,
    discovery_truncated: bool,
    exhausted_outcome: SourceReadOutcome | None,
    unresolved_count: int,
) -> SourceReadOutcome | None:
    """Outcome once nothing is unresolved; None when discovery was truncated."""
    if not has_frontier or discovery_truncated or exhausted_outcome is None:
        return None
    if unresolved_count != 0:
        return None
    return exhausted_outcome


def exhausted_continuation_note(base_note: str) -> str:
    """Append why a scan with a deferred frontier still reads as done."""
    suffix = (
        "Every deferred publisher link has been read by continuation; "
        "this scan covers its discovered set."
    )
    return " ".join(part for part in (base_note, suffix) if part)


def settle_drained_scan(
    scan: SourceScan, *, unresolved_count: int, listing_complete: bool = False
) -> bool:
    """Settle complete discovery after every eligible retained body obligation resolves."""
    proven_coverage = (
        scan.detail_coverage is not None or listing_complete
    ) and scan.status == "incomplete"
    if not proven_coverage and not may_settle_drained_scan(
        scan.status, scan.read_outcome, scan.skip_reasons
    ):
        return False
    resolved = resolve_exhausted_outcome(
        has_frontier=True,
        discovery_truncated=scan.discovery_truncated,
        exhausted_outcome=scan.exhausted_outcome,
        unresolved_count=unresolved_count,
    )
    if resolved is None:
        return not scan.discovery_truncated and scan.exhausted_outcome is not None
    scan.complete = True
    scan.status, _note = _scan_outcome(
        read_outcome=resolved,
        raw_count=scan.raw_count,
        listings_count=scan.read_count,
        read_complete=bool(scan.complete),
        skip_reasons=scan.skip_reasons,
        matched=0,
        staged=scan.result_count,
        refused=0,
        unfiltered=False,
    )
    scan.read_outcome = resolved
    kept = scan.notes.replace(INCOMPLETE_READ_NOTE, "").strip()
    if listing_complete and scan.raw_count and not scan.read_count:
        scan.status = "completed"
        kept = (
            f"Publisher enumerated {scan.raw_count} identities. "
            "No eligible posting body work remains."
        )
    scan.notes = exhausted_continuation_note(kept)
    return True


def build_scan_status_note(
    *,
    read_outcome: SourceReadOutcome,
    raw_count: int,
    read_count: int,
    skip_reasons: dict[str, int],
    matched: int,
    staged: int,
    refused: int,
    unfiltered: bool,
    read_note: str,
    detail: SourceDetailSummary,
    complete: bool,
    listing_complete: bool = False,
) -> tuple[str, str]:
    """Return one scan's status and note, built once from its actual final outcome."""
    status, outcome_note = _scan_outcome(
        read_outcome=read_outcome,
        raw_count=raw_count,
        listings_count=read_count,
        read_complete=complete,
        skip_reasons=skip_reasons,
        matched=matched,
        staged=staged,
        refused=refused,
        unfiltered=unfiltered,
    )
    if complete and listing_complete and raw_count and not read_count:
        return "completed", (
            f"Publisher enumerated {raw_count} identities. No eligible posting body work remains."
        )
    source_note = f"{read_note} " if read_note else ""
    note = (
        detail_failure_notes_prefix(detail.failed, detail.failure_examples)
        + source_note
        + (outcome_note or "")
    )
    return status, note
