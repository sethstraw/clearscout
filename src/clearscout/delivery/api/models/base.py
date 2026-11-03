"""Shared strict contracts for the local HTTP API."""

from typing import Any, Literal

from clearscout.shared.models import StrictModel

# Every problem type the API emits; the SPA reads this union from the generated schema.
ProblemType = Literal[
    "urn:clearscout:problem:career-page-unreadable",
    "urn:clearscout:problem:conflict",
    "urn:clearscout:problem:database-fault",
    "urn:clearscout:problem:free-mode",
    "urn:clearscout:problem:http-error",
    "urn:clearscout:problem:integrity-conflict",
    "urn:clearscout:problem:local-boundary",
    "urn:clearscout:problem:method-not-allowed",
    "urn:clearscout:problem:missing-idempotency-key",
    "urn:clearscout:problem:not-found",
    "urn:clearscout:problem:request-validation",
    "urn:clearscout:problem:unavailable",
    "urn:clearscout:problem:validation",
]


class Problem(StrictModel):
    """RFC 9457-style safe error body without internal trace data."""

    type: ProblemType
    title: str
    status: int
    detail: str
    correlation_id: str
    hint: str | None = None


FREE_MODE_PROBLEM: ProblemType = "urn:clearscout:problem:free-mode"
# OpenAPI ``responses`` entry for every route that can start paid work.
FREE_MODE_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": Problem,
        "description": (
            f"Problem type {FREE_MODE_PROBLEM} when paid work is "
            "requested and the configured provider has no API key. Other conflicts use "
            "urn:clearscout:problem:conflict."
        ),
    }
}
