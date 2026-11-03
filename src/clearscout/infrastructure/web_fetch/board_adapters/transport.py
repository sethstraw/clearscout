"""Board requests under the shared outbound policy and the current read deadline."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

import httpx

from clearscout.application.discovery.source_reading import (
    PostingCheck,
    SourceContinuationTarget,
    SourceListing,
)
from clearscout.domain import SourceReadOutcome
from clearscout.infrastructure.web_fetch.board_adapters.read_scope import (
    BOARD_DEADLINE_AT,
    SOURCE_READ_OPTIONS,
    remaining_seconds,
)
from clearscout.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    BoardDeadlineReachedError,
    read_result,
)
from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT, workday_posting_detail
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError, PoliteHttpClient
from clearscout.infrastructure.web_fetch.publisher_sources import PublisherSourceFetcher

TIMEOUT = httpx.Timeout(20.0)


HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


PAGE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html"}


FEED_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/xml, text/xml",
}


# Career pages are read only far enough to identify a board.
DETECT_BYTES = 400_000


class BoardHttpTransport:
    """Board requests through the shared client, bounded by the current read deadline."""

    def __init__(self, *, client: PoliteHttpClient | None = None) -> None:
        """Use the shared outbound-request policy for every board read."""
        self.client = client or PoliteHttpClient(user_agent=USER_AGENT)
        self.publishers = PublisherSourceFetcher(client=self.client)

    def workday_detail(self, url: str) -> PostingCheck:
        """Read a just-listed Workday posting with the shared request policy."""
        return workday_posting_detail(
            url, client=self.client, request_guard=SOURCE_READ_OPTIONS.get().allows_posting
        )

    def register_host_minimum_interval(self, url: str, seconds: float) -> None:
        """Raise the shared client's interval floor for the URL's logical host."""
        self.client.register_host_minimum_interval(url, seconds)

    def get_text(self, url: str, *, max_bytes: int = DETECT_BYTES) -> str:
        """Return the final URL, a newline, then up to ``max_bytes`` of the page."""
        response = self.client.get(
            url,
            timeout=TIMEOUT,
            headers=PAGE_HEADERS,
            follow_redirects=True,
            max_bytes=max_bytes,
            max_elapsed_seconds=self._remaining_seconds(url),
            request_guard=SOURCE_READ_OPTIONS.get().allows_posting,
        )
        response.raise_for_status()
        # Use the final URL because vanity domains often redirect to the board.
        return f"{response.url}\n{response.text[:max_bytes]}"

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        """Return the decoded JSON body of a GET, adding any ``headers`` its API requires."""
        response = self.client.get(
            url,
            timeout=TIMEOUT,
            headers={**HEADERS, **(headers or {})},
            follow_redirects=True,
            max_bytes=MAX_API_RESPONSE_BYTES,
            max_elapsed_seconds=self._remaining_seconds(url),
        )
        response.raise_for_status()
        return response.json()

    def get_feed(self, url: str) -> str:
        """Read a complete XML/RSS response through the same outbound policy."""
        response = self.client.get(
            url,
            timeout=TIMEOUT,
            headers=FEED_HEADERS,
            follow_redirects=True,
            max_bytes=MAX_API_RESPONSE_BYTES,
            max_elapsed_seconds=self._remaining_seconds(url),
        )
        response.raise_for_status()
        return response.text

    def session_text(
        self, url: str, cookies: httpx.Cookies, *, form: Mapping[str, str] | None = None
    ) -> str:
        """Return the final URL, a newline, then the page a GET (or ``form`` POST) answers."""
        if form is None:
            response = self.client.get(
                url,
                timeout=TIMEOUT,
                headers=PAGE_HEADERS,
                follow_redirects=True,
                max_bytes=MAX_API_RESPONSE_BYTES,
                max_elapsed_seconds=self._remaining_seconds(url),
                request_guard=SOURCE_READ_OPTIONS.get().allows_posting,
                cookies=cookies,
            )
        else:
            response = self.client.post(
                url,
                data=form,
                timeout=TIMEOUT,
                headers=PAGE_HEADERS,
                follow_redirects=True,
                max_bytes=MAX_API_RESPONSE_BYTES,
                max_elapsed_seconds=self._remaining_seconds(url),
                request_guard=SOURCE_READ_OPTIONS.get().allows_posting,
                cookies=cookies,
            )
        response.raise_for_status()
        return f"{response.url}\n{response.text}"

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        """Return the decoded JSON body of a POST carrying body."""
        return self.post_json_with_headers(url, body, headers={})

    def post_json_with_headers(
        self, url: str, body: dict[str, Any], *, headers: Mapping[str, str]
    ) -> Any:
        """Return a JSON POST response with explicit publisher protocol headers."""
        response = self.client.post(
            url,
            json=body,
            timeout=TIMEOUT,
            headers={**HEADERS, "Content-Type": "application/json", **headers},
            follow_redirects=True,
            max_bytes=MAX_API_RESPONSE_BYTES,
            max_elapsed_seconds=self._remaining_seconds(url),
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def deadline_elapsed() -> bool:
        """Return whether the current board read has used up its time."""
        deadline_at = BOARD_DEADLINE_AT.get()
        return deadline_at is not None and time.monotonic() >= deadline_at

    @staticmethod
    def _remaining_seconds(url: str) -> float:
        return remaining_seconds(url)

    def paginated_request(
        self,
        request: Callable[[], Any],
        listings: list[SourceListing],
        raw_count: int,
    ) -> Any:
        """Run one page request, turning a spent deadline into a partial read."""
        if self.deadline_elapsed():
            raise BoardDeadlineReachedError(listings, raw_count)
        try:
            return request()
        except httpx.TimeoutException:
            if self.deadline_elapsed():
                raise BoardDeadlineReachedError(listings, raw_count) from None
            raise

    def page_or_stop(
        self,
        url: str,
        request: Callable[[str], Any],
        listings: list[SourceListing],
        raw_count: int,
        *,
        first_page: bool,
        cursor: SourceContinuationTarget,
    ) -> Any:
        """Return one page's body, or the incomplete read that resumes at ``cursor``.

        A failure on the first page still fails the board; a later page's deadline or
        failure keeps the pages already read and leaves the rest as continuation work.
        """
        # A deferred cursor is continuation work, not dropped work, so no truncation reason.
        reason: str | None = None
        try:
            return self.paginated_request(partial(request, url), listings, raw_count)
        except BoardDeadlineReachedError:
            pass
        except (FetchPolicyError, httpx.HTTPError):
            if first_page:
                raise
            reason = "fetch_failed"
        return read_result(
            listings,
            raw_count=raw_count,
            complete=False,
            incomplete_reason=reason,
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX,
            deferred_targets=(cursor,),
        )
