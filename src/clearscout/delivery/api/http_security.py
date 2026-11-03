"""Loopback Host, browser Origin, and response-header policy for the local API."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from fastapi.responses import JSONResponse, Response

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BROWSER_MUTATION_HEADER = "X-ClearScout-Request"
BROWSER_MUTATION_VALUE = "browser"


def authority(value: str, *, scheme: str) -> tuple[str, int] | None:
    """Parse one HTTP authority and accept only canonical loopback hosts."""
    if not value or any(character.isspace() for character in value) or "," in value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in LOOPBACK_HOSTS
    ):
        return None
    return parsed.hostname, port or (443 if scheme == "https" else 80)


def origin_authority(value: str) -> tuple[str, str, int] | None:
    """Parse a browser Origin, which must contain only scheme and authority."""
    if not value or any(character.isspace() for character in value) or "," in value:
        return None
    try:
        parsed: SplitResult = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in LOOPBACK_HOSTS
    ):
        return None
    return parsed.scheme, parsed.hostname, port or (443 if parsed.scheme == "https" else 80)


def secure_headers(response: Response, correlation: str) -> Response:
    """Apply the local browser boundary to success and early rejection alike."""
    response.headers["X-Correlation-ID"] = correlation
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def boundary_problem(status: int, title: str, detail: str) -> JSONResponse:
    """Return a path-free RFC-style rejection at the local HTTP boundary."""
    return JSONResponse(
        {
            "type": "urn:clearscout:problem:local-boundary",
            "title": title,
            "status": status,
            "detail": detail,
        },
        status_code=status,
        media_type="application/problem+json",
    )
