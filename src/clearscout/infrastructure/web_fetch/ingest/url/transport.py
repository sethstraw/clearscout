"""HTTP headers, deadlines, retries and the posting request guard."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar

import httpx

from clearscout import __version__
from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    PoliteHttpClient,
    RobotsDeniedError,
    read_basis,
    retry_after_seconds,
)
from clearscout.shared.errors import IngestError

logger = logging.getLogger(__name__)


# Identify the client consistently to public boards.
USER_AGENT = f"clearscout/{__version__} (+https://github.com/sethstraw/clearscout)"


PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


JSON_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


TIMEOUT = httpx.Timeout(20.0)


MAX_POSTING_READ_SECONDS = 30.0


MAX_POSTING_RESPONSE_BYTES = 8_000_000


READ_DEADLINE_AT: ContextVar[float | None] = ContextVar(
    "clearscout_posting_read_deadline", default=None
)


POSTING_REQUEST_GUARD: ContextVar[Callable[[str], bool] | None] = ContextVar(
    "clearscout_posting_request_guard", default=None
)


# Retry once for transient refusal responses.
RETRY_PAUSE_SECONDS = 1.0


RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


LINKEDIN_JOB_VIEW = re.compile(r"(?<![\w-])(?:[\w-]+\.)*linkedin\.com/jobs/view/")


# A LinkedIn guest job page lists other postings below and beside its own, reshuffled per
# request, and serves its job alert prompt in two layouts, only one of which reads as text.
LINKEDIN_RELATED_LISTINGS = [
    f"//section[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"
    for name in (
        "similar-jobs",
        "right-rail",
        "job-alert-redirect-section",
        "job-alert-redirect-section__wrapper",
    )
]


@contextmanager
def client_for_call(client: PoliteHttpClient | None) -> Iterator[PoliteHttpClient]:
    """Yield the caller's client, or one built for this call and closed when it ends."""
    if client is not None:
        yield client
        return
    with closing(PoliteHttpClient(user_agent=USER_AGENT)) as owned:
        yield owned


@contextmanager
def guarded_posting_reads(request_guard: Callable[[str], bool] | None) -> Iterator[None]:
    """Apply a posting protection check to every request and redirect in this read."""
    if request_guard is None:
        yield
        return
    previous = POSTING_REQUEST_GUARD.get()

    def guard(url: str) -> bool:
        return request_guard(url) and (previous is None or previous(url))

    token = POSTING_REQUEST_GUARD.set(guard)
    try:
        yield
    finally:
        POSTING_REQUEST_GUARD.reset(token)


def posting_read_allowed(url: str) -> bool:
    """Return whether the active posting guard lets this read request ``url``."""
    request_guard = POSTING_REQUEST_GUARD.get()
    return request_guard is None or request_guard(url)


def fetch_posting(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> httpx.Response:
    """GET a URL, turning transport and status failures into an actionable error.

    The whole response is returned rather than its body because where a request
    finished is as informative as what it returned.
    """
    try:
        response = http_get(url, {**PAGE_HEADERS, **(headers or {})}, client=client, intent=intent)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise IngestError(
            f"{url} returned HTTP {error.response.status_code}. If the posting "
            "requires a login, save it as a PDF or screenshot it instead."
        ) from error
    except httpx.HTTPError as error:
        raise IngestError(f"Could not fetch {url}: {error}") from error
    except RobotsDeniedError:
        # Let callers identify whether the refused URL was the posting or a redirect.
        raise
    except FetchPolicyError as error:
        raise IngestError(str(error)) from error
    return response


def http_get(
    url: str,
    headers: dict[str, str],
    *,
    wait: Callable[[float], None] | None = None,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> httpx.Response:
    """GET once, then once more if the answer was the kind that changes on retry.

    A response let through on the single-posting exemption is not retried.

    ``wait`` defaults to the client's own sleeper, so one injected pause
    governs every wait a read makes.
    """
    deadline_at = read_deadline_at()
    with client_for_call(client) as http:
        response = http.get(
            url,
            timeout=TIMEOUT,
            follow_redirects=True,
            headers=headers,
            max_bytes=MAX_POSTING_RESPONSE_BYTES,
            max_elapsed_seconds=time_left(deadline_at, "GET", url),
            intent=intent,
            request_guard=POSTING_REQUEST_GUARD.get(),
        )
        # A stated Retry-After was already honored, or refused as too long, by the client.
        if (
            read_basis(response) == RobotsReadBasis.SINGLE_POSTING_READ
            or response.status_code not in RETRYABLE_STATUS
            or retry_after_seconds(response.headers.get("Retry-After")) is not None
        ):
            return response
        (wait or http.sleeper)(min(RETRY_PAUSE_SECONDS, max(0.0, deadline_at - time.monotonic())))
        return http.get(
            url,
            timeout=TIMEOUT,
            follow_redirects=True,
            headers=headers,
            max_bytes=MAX_POSTING_RESPONSE_BYTES,
            max_elapsed_seconds=time_left(deadline_at, "GET", url),
            intent=intent,
            request_guard=POSTING_REQUEST_GUARD.get(),
        )


def read_deadline_at() -> float:
    """Return when the current posting read must finish, by the caller's deadline or the cap."""
    return READ_DEADLINE_AT.get() or time.monotonic() + MAX_POSTING_READ_SECONDS


def time_left(deadline_at: float, method: str, url: str) -> float:
    """Return the seconds a read has left, raising once its deadline has passed."""
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise httpx.ReadTimeout(
            "The posting exceeded its total read deadline.",
            request=httpx.Request(method, url),
        )
    return remaining
