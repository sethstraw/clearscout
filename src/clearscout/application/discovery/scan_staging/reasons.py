"""The queue reasons staging writes, and what each one says about who staged a row."""

from __future__ import annotations

FOUND_POSTING_REASON = (
    "You added this posting from a link you found. Evaluation reads its "
    "contents before creating a ledger job."
)


CAMPAIGN_FOUND_POSTING_REASON = (
    "A guided search campaign proposed this posting directly. Evaluation reads its "
    "contents before creating a ledger job."
)


CAMPAIGN_BOARD_LISTING_REASON = (
    "A guided search campaign read a job board and this posting was listed on it. "
    "Evaluation reads its contents before creating a ledger job."
)


def found_posting_reason(origin: str, *, from_board_read: bool) -> str:
    """Return the truthful reason for one pending row, keyed by what staged it."""
    if origin != "search.guided_campaign":
        return FOUND_POSTING_REASON
    if from_board_read:
        return CAMPAIGN_BOARD_LISTING_REASON
    return CAMPAIGN_FOUND_POSTING_REASON


def staged_from_single_posting(reason: str) -> bool:
    """Whether a pending row was staged one posting URL at a time, not by a board read."""
    # These reasons are the sentence the queue shows the person, so rewording one
    # changes which rows this predicate selects.
    return reason in (
        FOUND_POSTING_REASON,
        CAMPAIGN_FOUND_POSTING_REASON,
        CAMPAIGN_BOARD_LISTING_REASON,
    )


def staged_by_campaign(reason: str) -> bool:
    """Whether a guided campaign staged this row, for a row no campaign record names."""
    # Staging writes these two sentences on no path but a campaign, so they stand as a
    # record of origin. The third found-posting sentence belongs to a person's own link.
    return reason in (CAMPAIGN_FOUND_POSTING_REASON, CAMPAIGN_BOARD_LISTING_REASON)


def staged_by_person(reason: str) -> bool:
    """Whether the person staged this row from a link they found themselves."""
    # Only the person's add-a-posting route writes this sentence.
    return reason == FOUND_POSTING_REASON
