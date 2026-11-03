"""Render public pages in an isolated browser and retain bounded read evidence."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import httpx
from playwright.sync_api import (
    Browser,
    BrowserType,
    Request,
    Route,
    WebSocketRoute,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from clearscout.domain.records import RobotsReadBasis
from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    PoliteHttpClient,
    read_basis,
)

_ALLOWED_RESOURCE_TYPES = frozenset({"document", "fetch", "script", "xhr"})
_JSON_TYPES = frozenset({"application/json", "application/ld+json"})


class RenderRuntimeUnavailableError(RuntimeError):
    """No supported browser executable is installed for rendered recovery."""


RESPONSE_BYTE_LIMIT_REASON = "response_byte_limit_reached"


@dataclass(frozen=True)
class RenderLimits:
    """Resource limits whose exhaustion is returned rather than hidden."""

    navigation_timeout_ms: int = 30_000
    settle_ms: int = 2_000
    max_html_characters: int = 2_000_000
    max_json_response_bytes: int = 2_000_000
    max_total_json_bytes: int = 10_000_000
    max_response_bytes: int = 5_000_000
    max_total_response_bytes: int = 30_000_000
    max_requests: int = 80
    max_blocked_requests: int = 200


@dataclass(frozen=True)
class RenderedResponse:
    """One public JSON response observed while the publisher page rendered."""

    url: str
    method: str
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class BlockedRenderedRequest:
    """One browser request refused with its exact policy or resource reason."""

    url: str
    method: str
    resource_type: str
    reason: str


@dataclass(frozen=True)
class RenderedPage:
    """Rendered publisher evidence plus every visible resource-limit outcome."""

    requested_url: str
    final_url: str
    html: str
    responses: tuple[RenderedResponse, ...]
    document_status: int | None = None
    html_truncated: bool = False
    deferred_response_urls: tuple[str, ...] = ()
    blocked_requests: tuple[BlockedRenderedRequest, ...] = ()
    blocked_request_overflow_count: int = 0
    # Why robots policy let the main document be read; None when none was read.
    robots_basis: RobotsReadBasis | None = None
    # The browser never reached DOMContentLoaded, so html is the main document as served.
    navigation_timed_out: bool = False


@dataclass
class _RenderCapture:
    """Mutable evidence accumulated by one isolated browser context."""

    final_document_url: str
    responses: list[RenderedResponse]
    deferred: list[str]
    blocked: list[BlockedRenderedRequest]
    total_json_bytes: int = 0
    total_response_bytes: int = 0
    request_count: int = 0
    blocked_overflow_count: int = 0
    document_status: int | None = None
    document_basis: RobotsReadBasis | None = None
    document_html: str | None = None


class RenderedPageFetcher:
    """Use an installed Chromium channel for a bounded, non-persistent read."""

    def __init__(
        self,
        *,
        client: PoliteHttpClient | None = None,
        limits: RenderLimits | None = None,
        launcher: Callable[[BrowserType], Browser] | None = None,
    ) -> None:
        """Configure policy, resource bounds, and an optional browser seam."""
        self.client = client or PoliteHttpClient(user_agent=USER_AGENT)
        self.limits = limits or RenderLimits()
        self.launcher = launcher

    def fetch(
        self,
        url: str,
        *,
        max_elapsed_seconds: float | None = None,
    ) -> RenderedPage:
        """Render one URL after applying fetch policy to each meaningful request.

        Every request, the document included, is read as a crawl under robots.txt.
        """
        navigation_seconds = self.limits.navigation_timeout_ms / 1_000
        budget = min(
            navigation_seconds,
            max_elapsed_seconds if max_elapsed_seconds is not None else navigation_seconds,
        )
        if budget <= 0:
            raise ValueError("max_elapsed_seconds must be positive when provided.")
        deadline_at = time.monotonic() + budget
        landing = self.client.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            timeout=httpx.Timeout(self.limits.navigation_timeout_ms / 1_000),
            follow_redirects=True,
            max_bytes=self.limits.max_html_characters * 4 + 1,
            max_elapsed_seconds=budget,
        )
        target_url = str(landing.url)
        with sync_playwright() as playwright:
            browser = (
                self.launcher(playwright.chromium)
                if self.launcher is not None
                else _installed_browser(playwright.chromium)
            )
            try:
                page = self._read(browser, target_url, deadline_at=deadline_at)
            except PlaywrightTimeoutError:
                return self._landing_page(url, landing)
            else:
                return replace(
                    page,
                    requested_url=url,
                    robots_basis=page.robots_basis or read_basis(landing),
                )
            finally:
                browser.close()

    def _landing_page(self, url: str, landing: httpx.Response) -> RenderedPage:
        """Return the landing document when the browser timed out before its own document read."""
        html = landing.text
        return RenderedPage(
            requested_url=url,
            final_url=str(landing.url),
            html=html[: self.limits.max_html_characters],
            responses=(),
            document_status=landing.status_code,
            html_truncated=len(html) > self.limits.max_html_characters,
            robots_basis=read_basis(landing),
            navigation_timed_out=True,
        )

    def _read(
        self,
        browser: Browser,
        url: str,
        *,
        deadline_at: float | None = None,
    ) -> RenderedPage:
        capture = _RenderCapture(url, [], [], [])
        context = browser.new_context(
            accept_downloads=False,
            service_workers="block",
            user_agent=USER_AGENT,
        )

        page = context.new_page()

        def route_request(route: Route, request: Request) -> None:
            self._route(
                route,
                request,
                capture,
                deadline_at=deadline_at,
                is_main_document=(
                    request.resource_type == "document"
                    and request.is_navigation_request()
                    and request.frame == page.main_frame
                ),
            )

        def block_websocket(websocket: WebSocketRoute) -> None:
            _record_blocked(
                capture,
                BlockedRenderedRequest(
                    websocket.url,
                    "CONNECT",
                    "websocket",
                    "websocket_not_supported",
                ),
                self.limits.max_blocked_requests,
            )
            websocket.close(code=1008, reason="Rendered reads do not permit WebSockets.")

        context.route("**/*", route_request)
        context.route_web_socket("**/*", block_websocket)
        page.on("popup", lambda opened: opened.close())
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=max(1, int(self._remaining_seconds(deadline_at) * 1_000)),
                )
            except PlaywrightTimeoutError:
                if capture.document_html is None:
                    raise
                return self._captured_page(
                    url, capture, capture.document_html, navigation_timed_out=True
                )
            remaining_ms = int(self._remaining_seconds(deadline_at) * 1_000)
            page.wait_for_timeout(min(self.limits.settle_ms, remaining_ms))
            rendered = page.content()
        finally:
            context.close()
        return self._captured_page(url, capture, rendered)

    def _captured_page(
        self,
        url: str,
        capture: _RenderCapture,
        html: str,
        *,
        navigation_timed_out: bool = False,
    ) -> RenderedPage:
        return RenderedPage(
            requested_url=url,
            final_url=capture.final_document_url,
            html=html[: self.limits.max_html_characters],
            responses=tuple(capture.responses),
            document_status=capture.document_status,
            html_truncated=len(html) > self.limits.max_html_characters,
            deferred_response_urls=tuple(dict.fromkeys(capture.deferred)),
            blocked_requests=tuple(capture.blocked),
            blocked_request_overflow_count=capture.blocked_overflow_count,
            robots_basis=capture.document_basis,
            navigation_timed_out=navigation_timed_out,
        )

    def _route(
        self,
        route: Route,
        request: Request,
        capture: _RenderCapture,
        *,
        deadline_at: float | None = None,
        is_main_document: bool = False,
    ) -> None:
        if request.resource_type not in _ALLOWED_RESOURCE_TYPES:
            _abort(
                route,
                request,
                capture,
                "resource_type_not_required",
                self.limits.max_blocked_requests,
            )
            return
        capture.request_count += 1
        if capture.request_count > self.limits.max_requests:
            _abort(
                route,
                request,
                capture,
                "request_limit_reached",
                self.limits.max_blocked_requests,
            )
            return
        remaining_total = self.limits.max_total_response_bytes - capture.total_response_bytes
        if remaining_total <= 0:
            _abort(
                route,
                request,
                capture,
                RESPONSE_BYTE_LIMIT_REASON,
                self.limits.max_blocked_requests,
            )
            return
        try:
            response = self._request(
                request,
                max_bytes=min(self.limits.max_response_bytes, remaining_total) + 1,
                max_elapsed_seconds=self._remaining_seconds(deadline_at),
            )
        except (FetchPolicyError, httpx.HTTPError, ValueError) as exc:
            _abort(
                route,
                request,
                capture,
                type(exc).__name__,
                self.limits.max_blocked_requests,
            )
            return
        body = response.content
        if _response_limit_reached(self.limits, capture, len(body)):
            _abort(
                route,
                request,
                capture,
                RESPONSE_BYTE_LIMIT_REASON,
                self.limits.max_blocked_requests,
            )
            return
        capture.total_response_bytes += len(body)
        self._capture_json(request, response, body, capture)
        if is_main_document:
            capture.final_document_url = str(response.url)
            capture.document_status = response.status_code
            capture.document_basis = read_basis(response)
            capture.document_html = response.text
        route.fulfill(
            status=response.status_code,
            headers=_browser_response_headers(response.headers),
            body=body,
        )

    def _capture_json(
        self,
        request: Request,
        response: httpx.Response,
        body: bytes,
        capture: _RenderCapture,
    ) -> None:
        media_type = response.headers.get("content-type", "").partition(";")[0].casefold()
        if media_type not in _JSON_TYPES or not 200 <= response.status_code < 300:
            return
        if (
            len(body) > self.limits.max_json_response_bytes
            or capture.total_json_bytes + len(body) > self.limits.max_total_json_bytes
        ):
            capture.deferred.append(request.url)
            return
        capture.total_json_bytes += len(body)
        capture.responses.append(
            RenderedResponse(
                url=str(response.url),
                method=request.method,
                status=response.status_code,
                content_type=media_type,
                body=body,
            )
        )

    def _request(
        self,
        request: Request,
        *,
        max_bytes: int | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> httpx.Response:
        """Replay one supported browser request through the pinned policy transport."""
        headers = _browser_request_headers(request.headers)
        timeout = httpx.Timeout(self.limits.navigation_timeout_ms / 1_000)
        if request.method == "GET":
            return self.client.get(
                request.url,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
                max_bytes=max_bytes,
                max_elapsed_seconds=max_elapsed_seconds,
            )
        raise ValueError(f"Rendered reads refuse non-GET method {request.method!r}.")

    def _remaining_seconds(self, deadline_at: float | None) -> float:
        maximum = self.limits.navigation_timeout_ms / 1_000
        if deadline_at is None:
            return maximum
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("Rendered page deadline elapsed.")
        return min(maximum, remaining)


def _installed_browser(chromium: BrowserType) -> Browser:
    """Prefer installed desktop channels, then an explicitly installed Chromium."""
    failures: list[str] = []
    for channel in ("chrome", "msedge", None):
        try:
            return chromium.launch(channel=channel, headless=True)
        except PlaywrightError as exc:
            failures.append(str(exc).splitlines()[0])
    raise RenderRuntimeUnavailableError("; ".join(failures))


def _blocked(request: Request, reason: str) -> BlockedRenderedRequest:
    return BlockedRenderedRequest(request.url, request.method, request.resource_type, reason)


def _abort(
    route: Route,
    request: Request,
    capture: _RenderCapture,
    reason: str,
    blocked_limit: int,
) -> None:
    _record_blocked(capture, _blocked(request, reason), blocked_limit)
    route.abort()


def _record_blocked(
    capture: _RenderCapture,
    blocked: BlockedRenderedRequest,
    limit: int,
) -> None:
    if len(capture.blocked) < limit:
        capture.blocked.append(blocked)
        return
    capture.blocked_overflow_count += 1


def _response_limit_reached(
    limits: RenderLimits,
    capture: _RenderCapture,
    response_bytes: int,
) -> bool:
    return (
        response_bytes > limits.max_response_bytes
        or capture.total_response_bytes + response_bytes > limits.max_total_response_bytes
    )


def _browser_request_headers(headers: dict[str, str]) -> dict[str, str]:
    refused = {"authorization", "content-length", "cookie", "host", "proxy-authorization"}
    return {key: value for key, value in headers.items() if key.casefold() not in refused}


def _browser_response_headers(headers: httpx.Headers) -> dict[str, str]:
    refused = {
        "connection",
        "content-encoding",
        "content-length",
        "proxy-authenticate",
        "set-cookie",
        "transfer-encoding",
    }
    return {key: value for key, value in headers.items() if key.casefold() not in refused}
