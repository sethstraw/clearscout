"""Source spans for tests whose subject is not the span check itself."""

from __future__ import annotations

from tailorcv.application.tailoring.stages.spans import Span
from tailorcv.domain import SpanStatus


def found(start: int = 0, end: int = 1) -> Span:
    """Return a verified span; extraction fails closed, so one built without it is not."""
    return Span(status=SpanStatus.VERIFIED, start=start, end=end)
