"""Shared strict contracts for the local HTTP API."""

from typing import Any

from tailorcv.shared.models import StrictModel


class Problem(StrictModel):
    """RFC 9457-style safe error body without internal trace data."""

    type: str
    title: str
    status: int
    detail: str
    correlation_id: str
    hint: str | None = None


FREE_MODE_PROBLEM = "free-mode"
# OpenAPI ``responses`` entry for every route that can start paid work.
FREE_MODE_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": Problem,
        "description": (
            f"Problem type urn:tailorcv:problem:{FREE_MODE_PROBLEM} when paid work is "
            "requested and the configured provider has no API key. Other conflicts use "
            "urn:tailorcv:problem:conflict."
        ),
    }
}
