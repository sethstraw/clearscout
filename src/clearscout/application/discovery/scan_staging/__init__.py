"""Put board listings into the review queue and mark the ones a board has pulled.

A listing already tracked as a job is refreshed rather than staged again, one
listing the board states per market is tied to the row staged first, and only a
complete read or the board's own stated end of listing may say an opening is gone.
"""

from clearscout.application.discovery.scan_staging.absence import (
    ABSENCE_CHECK_BATCH,
    AbsenceCheck,
    WithdrawingRead,
    queue_absence_checks,
    withdraw_absent_in_session,
)
from clearscout.application.discovery.scan_staging.found import FoundPostingStageResult
from clearscout.application.discovery.scan_staging.publisher_facts import publisher_staged_facts
from clearscout.application.discovery.scan_staging.reasons import (
    CAMPAIGN_BOARD_LISTING_REASON,
    CAMPAIGN_FOUND_POSTING_REASON,
    FOUND_POSTING_REASON,
    staged_by_campaign,
    staged_by_person,
    staged_from_single_posting,
)
from clearscout.application.discovery.scan_staging.service import (
    DiscoveryStaging,
    StagedListings,
    StagingBusyError,
    shield_progress,
)

__all__ = [
    "ABSENCE_CHECK_BATCH",
    "CAMPAIGN_BOARD_LISTING_REASON",
    "CAMPAIGN_FOUND_POSTING_REASON",
    "FOUND_POSTING_REASON",
    "AbsenceCheck",
    "DiscoveryStaging",
    "FoundPostingStageResult",
    "StagedListings",
    "StagingBusyError",
    "WithdrawingRead",
    "publisher_staged_facts",
    "queue_absence_checks",
    "shield_progress",
    "staged_by_campaign",
    "staged_by_person",
    "staged_from_single_posting",
    "withdraw_absent_in_session",
]
