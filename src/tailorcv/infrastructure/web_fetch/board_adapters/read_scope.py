"""Per-read state every board request consults: its deadline and its read options."""

from __future__ import annotations

import time
from contextvars import ContextVar

from tailorcv.application.discovery.source_reading import SourceReadOptions
from tailorcv.infrastructure.web_fetch.policy import MAX_PAGE_READ_SECONDS, RequestDeadlineExceeded

BOARD_DEADLINE_AT: ContextVar[float | None] = ContextVar("tailorcv_board_deadline_at", default=None)


DEFAULT_READ_OPTIONS = SourceReadOptions()


SOURCE_READ_OPTIONS: ContextVar[SourceReadOptions] = ContextVar(
    "tailorcv_source_read_options", default=DEFAULT_READ_OPTIONS
)


def board_read_deadline() -> float:
    """Return the monotonic time at which a board read starting now must stop."""
    return time.monotonic() + MAX_PAGE_READ_SECONDS


def remaining_seconds(url: str) -> float:
    """Return the current read's remaining budget, a full budget outside any read."""
    deadline_at = BOARD_DEADLINE_AT.get()
    remaining = MAX_PAGE_READ_SECONDS if deadline_at is None else deadline_at - time.monotonic()
    if remaining <= 0:
        raise RequestDeadlineExceeded("GET", url)
    return remaining
