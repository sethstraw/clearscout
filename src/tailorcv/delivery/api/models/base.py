"""Shared strict contracts for the local HTTP API."""

from tailorcv.shared.models import StrictModel


class Problem(StrictModel):
    """RFC 9457-style safe error body without internal trace data."""

    type: str
    title: str
    status: int
    detail: str
    correlation_id: str
    hint: str | None = None
