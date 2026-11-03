"""Attempt limits and the frontier row markers continuation reads and writes."""

from __future__ import annotations

from clearscout.application.discovery.source_reading import MAX_SOURCE_READ_ATTEMPTS

MAX_AUTOMATIC_ATTEMPTS = MAX_SOURCE_READ_ATTEMPTS


MAX_NO_PROGRESS_TASKS = 3


# Stored in deferred_reason on a completed row; that column never crosses the wire.
POSTING_DETAIL_COMPLETED = "posting_detail_completed"


DISCOVERED_DURING_FOLLOW_UP = "discovered_during_follow_up"


# A failed row with this origin already had its one cross-scan retry; only a row
# carried as failed takes it, so a row carried while pending keeps its own origin.
CARRIED_ORIGIN = "source.scan.carry_forward"


# A publisher link that answered gone is held for a person; it neither blocks certification
# nor is withdrawn by one. A refused or unreadable link still blocks, since it was never read.
PUBLISHER_GONE_OUTCOMES = frozenset({"stable_http_404", "stable_http_410"})


HELD_UNTIL_REEMITTED = frozenset(
    {"posting_identity_not_established", "shape_probe_pending", "low_priority_navigation"}
)


# Why a Workday continuation stopped short of the whole listing; certification clears them.
WORKDAY_TRUNCATION_REASONS = frozenset(
    {"incomplete_workday_listing", "repeated_workday_page", "duplicate_workday_identity"}
)


_ORACLE_TRUNCATION_REASONS = frozenset({"incomplete_oracle_listing"})


# A later truncated read replaces these counts rather than adding to them.
CARRIED_TRUNCATION_REASONS = WORKDAY_TRUNCATION_REASONS | _ORACLE_TRUNCATION_REASONS
