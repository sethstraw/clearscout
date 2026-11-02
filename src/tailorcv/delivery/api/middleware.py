"""Local HTTP boundary middleware and safe exception translation."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import IntegrityError, InternalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from tailorcv.application.failures import (
    CareerPageUnreadableError,
    ConflictError,
    FreeModeError,
    NotFoundError,
    UnavailableError,
    ValidationError,
)
from tailorcv.delivery.api.http_security import (
    BROWSER_MUTATION_HEADER,
    BROWSER_MUTATION_VALUE,
    UNSAFE_METHODS,
    authority,
    boundary_problem,
    origin_authority,
    secure_headers,
)
from tailorcv.delivery.api.models.base import FREE_MODE_PROBLEM, Problem


async def secure_local_response(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Enforce the loopback browser boundary and attach safe response headers."""
    correlation = request.headers.get("X-Correlation-ID", "").strip()
    if not correlation or len(correlation) > 120 or not correlation.isprintable():
        correlation = str(uuid4())
    request.state.correlation_id = correlation
    host = authority(request.headers.get("host", ""), scheme=request.url.scheme)
    if host is None:
        return secure_headers(
            boundary_problem(400, "Invalid Host", "Requests must name a loopback Host."),
            correlation,
        )
    if request.method in UNSAFE_METHODS:
        refusal = _mutation_refusal(request, host)
        if refusal is not None:
            return secure_headers(refusal, correlation)
    response = await call_next(request)
    return secure_headers(response, correlation)


def _mutation_refusal(request: Request, host: tuple[str, int]) -> Response | None:
    """Return the problem response refusing an unsafe browser request, or None to allow it."""
    origin = request.headers.get("origin")
    if origin is None:
        if request.headers.get("sec-fetch-site") is not None or request.headers.get("referer"):
            return boundary_problem(
                403,
                "Browser mutation refused",
                "Browser mutations must carry an Origin header.",
            )
        return None
    if origin_authority(origin) != (request.url.scheme, *host):
        return boundary_problem(
            403,
            "Cross-origin request refused",
            "Browser mutations must come from the exact local origin.",
        )
    if request.headers.get(BROWSER_MUTATION_HEADER) != BROWSER_MUTATION_VALUE:
        return boundary_problem(
            403,
            "Browser mutation refused",
            "Browser mutations must identify the TailorCV web client.",
        )
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        return boundary_problem(
            403,
            "Browser mutation refused",
            "Browser mutations must have same-origin fetch metadata.",
        )
    return None


async def not_found(request: Request, error: NotFoundError) -> JSONResponse:
    """Translate a missing application record into a safe problem response."""
    return problem(request, 404, "Not found", str(error), "not-found")


async def conflict(request: Request, error: ConflictError) -> JSONResponse:
    """Translate an application conflict into a safe problem response."""
    return problem(request, 409, "Conflict", str(error), "conflict")


async def free_mode(request: Request, error: FreeModeError) -> JSONResponse:
    """Refuse paid work while no provider key is set, pointing at where to add one."""
    return problem(
        request,
        409,
        "Free mode",
        str(error),
        FREE_MODE_PROBLEM,
        hint="Add an API key in Settings > Provider.",
    )


async def invalid(request: Request, error: ValidationError) -> JSONResponse:
    """Translate application validation failure into a safe problem response."""
    return problem(request, 422, "Validation failed", str(error), "validation")


async def unavailable(request: Request, error: UnavailableError) -> JSONResponse:
    """Translate an unavailable dependency into a safe problem response."""
    return problem(request, 503, "Service unavailable", str(error), "unavailable")


async def page_unreadable(request: Request, error: CareerPageUnreadableError) -> JSONResponse:
    """Translate a refused or failed upstream page read into a problem naming its cause."""
    return problem(request, 502, "Career page unreadable", str(error), "career-page-unreadable")


async def request_invalid(request: Request, error: RequestValidationError) -> JSONResponse:
    """Reject a request that does not match the published contract."""
    for entry in error.errors():
        location = tuple(entry.get("loc") or ())
        if (
            len(location) >= 2
            and location[0] == "header"
            and str(location[1]).lower() == "idempotency-key"
        ):
            return problem(
                request,
                400,
                "Missing Idempotency-Key header",
                "Requests that create or replay a task require the Idempotency-Key request header.",
                "missing-idempotency-key",
                hint="Send the request again with an Idempotency-Key header set to a unique value.",
            )
    return problem(
        request,
        422,
        "Validation failed",
        "The request did not match the documented API contract.",
        "request-validation",
    )


async def integrity_conflict(request: Request, error: IntegrityError) -> JSONResponse:
    """Translate a persistence constraint failure without leaking internals."""
    del error
    return problem(
        request,
        409,
        "Conflict",
        "The write conflicts with existing durable state.",
        "integrity-conflict",
    )


async def database_fault(request: Request, error: InternalError) -> JSONResponse:
    """Keep an unexpected database-side raise inside the problem contract.

    A trigger that raises without an error code reaches SQLAlchemy as
    InternalError rather than IntegrityError, and would otherwise bypass every
    handler here.
    """
    del error
    return problem(
        request,
        500,
        "Internal error",
        "The database refused the operation for an unexpected reason.",
        "database-fault",
    )


async def framework_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    """Translate framework routing errors into the public problem contract."""
    status, title, detail, problem_type = {
        404: (404, "No such endpoint", "The requested path is not an endpoint.", "not-found"),
        405: (
            405,
            "Method not allowed",
            "That HTTP method is not allowed on this path.",
            "method-not-allowed",
        ),
    }.get(
        error.status_code,
        (error.status_code, "HTTP error", "The request could not be completed.", "http-error"),
    )
    return problem(request, status, title, detail, problem_type)


def install_middleware(app: FastAPI) -> None:
    """Install the local boundary and application exception handlers."""
    app.middleware("http")(secure_local_response)
    app.exception_handler(NotFoundError)(not_found)
    app.exception_handler(ConflictError)(conflict)
    app.exception_handler(FreeModeError)(free_mode)
    app.exception_handler(ValidationError)(invalid)
    app.exception_handler(UnavailableError)(unavailable)
    app.exception_handler(CareerPageUnreadableError)(page_unreadable)
    app.exception_handler(RequestValidationError)(request_invalid)
    app.exception_handler(IntegrityError)(integrity_conflict)
    app.exception_handler(InternalError)(database_fault)
    app.exception_handler(StarletteHTTPException)(framework_error)


def problem(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
    problem_type: str,
    *,
    hint: str | None = None,
) -> JSONResponse:
    """Build an RFC 9457-style response without internal trace data."""
    correlation_id = str(getattr(request.state, "correlation_id", uuid4()))
    body = Problem(
        type=f"urn:tailorcv:problem:{problem_type}",
        title=title,
        status=status_code,
        detail=detail,
        correlation_id=correlation_id,
        hint=hint,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
    )
