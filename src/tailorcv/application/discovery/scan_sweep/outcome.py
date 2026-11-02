"""How a failed or finished board read is named in the funnel."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from tailorcv.application.discovery.funnel_audit import FunnelReason
from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.domain import SourceReadOutcome
from tailorcv.infrastructure.web_fetch.policy import RobotsDeniedError


def source_failure_outcome(exc: Exception) -> SourceReadOutcome:
    """Classify a failed source read without deriving a successful representation."""
    if isinstance(exc, RobotsDeniedError):
        return SourceReadOutcome.POLICY_REFUSED
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return SourceReadOutcome.ACCESS_BLOCKED
    return SourceReadOutcome.FETCH_FAILED


# The two outcomes source_failure_outcome can classify a failed read as; every
# other member of SourceReadOutcome describes a read that succeeded.
_REFUSED_READ_OUTCOMES = frozenset(
    {SourceReadOutcome.POLICY_REFUSED, SourceReadOutcome.ACCESS_BLOCKED}
)


def failed_read_reason(read_outcome: SourceReadOutcome | None) -> FunnelReason:
    """Split a failed read into a standing refusal retrying cannot change, or a fetch failure."""
    if read_outcome in _REFUSED_READ_OUTCOMES:
        return FunnelReason.FETCH_REFUSED
    return FunnelReason.FETCH_FAILED


def openings_reason(
    read: SourceRead, wanted: Sequence[Any], new_listings: Sequence[Any]
) -> FunnelReason:
    """Only a complete read can establish that a board has no openings."""
    if read.raw_count == 0:
        return FunnelReason.NO_OPEN_POSITIONS if read.complete else FunnelReason.NO_READABLE_BOARD
    if not read.complete and not read.listings:
        return FunnelReason.NO_READABLE_BOARD
    if not wanted:
        return FunnelReason.NO_MATCHING_LISTINGS
    if not new_listings:
        return FunnelReason.NO_NEW_OPENINGS
    return FunnelReason.STAGED


def unreachable_network(exc: BaseException) -> bool:
    """Whether a read failed before any publisher answered: no route, DNS, or connect."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, (httpx.TransportError, OSError)):
            return True
        seen = seen.__cause__
    return False
