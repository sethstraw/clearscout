from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from clearscout.infrastructure.web_fetch import rendered as rendered_module
from clearscout.infrastructure.web_fetch.rendered import RenderedPageFetcher, RenderLimits


@dataclass
class _Request:
    url: str
    method: str = "GET"
    resource_type: str = "document"
    post_data: str | None = None
    headers: dict[str, str] | None = None
    frame: object | None = None

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"


class _PolicyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.options: list[dict[str, Any]] = []

    def ensure_allowed(self, url: str) -> None:
        self.calls.append(("ALLOW", url, {}, None))

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("GET", url, kwargs["headers"], None))
        self.options.append(kwargs)
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "set-cookie": "secret=1"},
            content=b"<main>Rendered</main>",
            request=httpx.Request("GET", url),
        )


class _Route:
    def __init__(self) -> None:
        self.fulfilled: list[dict[str, Any]] = []
        self.aborted = False

    def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled.append(kwargs)

    def abort(self) -> None:
        self.aborted = True


class _WebSocket:
    url = "wss://publisher.example/events"

    def __init__(self) -> None:
        self.closed: tuple[int | None, str | None] | None = None

    def close(self, *, code: int | None = None, reason: str | None = None) -> None:
        self.closed = (code, reason)


class _Page:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.main_frame = object()

    def on(self, _event: str, _handler: Any) -> None:
        return None

    def goto(self, url: str, **_kwargs: Any) -> None:
        socket = _WebSocket()
        self.context.socket = socket
        self.context.websocket_handler(socket)

        route = _Route()
        self.context.route_handler(
            route, _Request(url, headers={"Cookie": "private"}, frame=self.main_frame)
        )
        assert not route.aborted
        assert route.fulfilled[0]["headers"] == {"content-type": "text/html"}

        api_route = _Route()
        self.context.route_handler(
            api_route,
            _Request(
                "https://publisher.example/api/search",
                method="POST",
                resource_type="xhr",
                post_data='{"page": 0}',
                headers={"Content-Type": "application/json", "Authorization": "private"},
            ),
        )
        assert api_route.aborted

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def content(self) -> str:
        return "<html><main>Rendered</main></html>"


class _Context:
    def __init__(self) -> None:
        self.route_handler: Any = None
        self.websocket_handler: Any = None
        self.socket: _WebSocket | None = None

    def route(self, _pattern: str, handler: Any) -> None:
        self.route_handler = handler

    def route_web_socket(self, _pattern: str, handler: Any) -> None:
        self.websocket_handler = handler

    def new_page(self) -> _Page:
        return _Page(self)

    def close(self) -> None:
        return None


class _Browser:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.context: _Context | None = None

    def new_context(self, **kwargs: Any) -> _Context:
        self.options = kwargs
        self.context = _Context()
        return self.context


def test_rendered_fetch_routes_browser_network_through_the_policy_client() -> None:
    client = _PolicyClient()
    browser = _Browser()
    fetcher = RenderedPageFetcher(
        client=cast(Any, client),
        limits=RenderLimits(settle_ms=0),
    )

    result = fetcher._read(cast(Any, browser), "https://publisher.example/careers")

    assert browser.options["service_workers"] == "block"
    assert [(method, url) for method, url, _, _ in client.calls] == [
        ("GET", "https://publisher.example/careers"),
    ]
    assert client.calls[0][2] == {}
    assert result.html == "<html><main>Rendered</main></html>"
    assert result.final_url == "https://publisher.example/careers"
    assert result.responses == ()
    assert [blocked.reason for blocked in result.blocked_requests] == [
        "websocket_not_supported",
        "ValueError",
    ]
    assert browser.context is not None and browser.context.socket is not None
    assert browser.context.socket.closed == (
        1008,
        "Rendered reads do not permit WebSockets.",
    )
    assert result.blocked_request_overflow_count == 0


def test_rendered_fetch_bounds_blocked_request_evidence() -> None:
    browser = _Browser()
    fetcher = RenderedPageFetcher(
        client=cast(Any, _PolicyClient()),
        limits=RenderLimits(settle_ms=0, max_blocked_requests=1),
    )

    result = fetcher._read(cast(Any, browser), "https://publisher.example/careers")

    assert [blocked.reason for blocked in result.blocked_requests] == ["websocket_not_supported"]
    assert result.blocked_request_overflow_count == 1


def test_rendered_routes_bound_each_response_before_fulfilling_it() -> None:
    client = _PolicyClient()
    fetcher = RenderedPageFetcher(
        client=cast(Any, client),
        limits=RenderLimits(max_response_bytes=4, max_total_response_bytes=10),
    )
    route = _Route()
    capture = rendered_module._RenderCapture("https://publisher.example/careers", [], [], [])

    fetcher._route(
        cast(Any, route),
        cast(Any, _Request("https://publisher.example/careers", headers={})),
        capture,
    )

    assert client.options[0]["max_bytes"] == 5
    assert client.options[0]["max_elapsed_seconds"] == 30
    assert route.aborted
    assert route.fulfilled == []
    assert [blocked.reason for blocked in capture.blocked] == ["response_byte_limit_reached"]


def test_rendered_fetch_keeps_main_document_provenance_when_a_child_frame_loads() -> None:
    client = _PolicyClient()
    fetcher = RenderedPageFetcher(client=cast(Any, client))
    capture = rendered_module._RenderCapture("https://publisher.example/job", [], [], [])

    fetcher._route(
        cast(Any, _Route()),
        cast(Any, _Request("https://publisher.example/job", headers={})),
        capture,
        is_main_document=True,
    )
    fetcher._route(
        cast(Any, _Route()),
        cast(Any, _Request("https://publisher.example/embedded", headers={})),
        capture,
        is_main_document=False,
    )

    assert capture.final_document_url == "https://publisher.example/job"
    assert capture.document_status == 200


class _StalledPage(_Page):
    def goto(self, url: str, **_kwargs: Any) -> None:
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded.")


class _DocumentThenStalledPage(_Page):
    def goto(self, url: str, **_kwargs: Any) -> None:
        self.context.route_handler(_Route(), _Request(url, headers={}, frame=self.main_frame))
        raise PlaywrightTimeoutError("Timeout 30000ms exceeded.")


class _StalledContext(_Context):
    def __init__(self, page_type: type[_Page]) -> None:
        super().__init__()
        self.page_type = page_type

    def new_page(self) -> _Page:
        return self.page_type(self)


class _StalledBrowser(_Browser):
    closed = False

    def __init__(self, page_type: type[_Page] = _StalledPage) -> None:
        super().__init__()
        self.page_type = page_type

    def new_context(self, **kwargs: Any) -> _Context:
        self.options = kwargs
        self.context = _StalledContext(self.page_type)
        return self.context

    def close(self) -> None:
        self.closed = True


class _Playwright:
    chromium = object()

    def __enter__(self) -> _Playwright:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _stalled_fetcher(
    monkeypatch: pytest.MonkeyPatch, client: _PolicyClient, browser: _StalledBrowser
) -> RenderedPageFetcher:
    monkeypatch.setattr(rendered_module, "sync_playwright", _Playwright)
    return RenderedPageFetcher(
        client=cast(Any, client),
        limits=RenderLimits(settle_ms=0),
        launcher=lambda _chromium: cast(Any, browser),
    )


def test_a_navigation_timeout_after_the_landing_read_returns_the_landing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _PolicyClient()
    browser = _StalledBrowser()
    fetcher = _stalled_fetcher(monkeypatch, client, browser)

    page = fetcher.fetch("https://board.example/viewjob?jk=a1b2c3")

    assert page.navigation_timed_out
    assert page.requested_url == "https://board.example/viewjob?jk=a1b2c3"
    assert page.final_url == "https://board.example/viewjob?jk=a1b2c3"
    assert page.document_status == 200
    assert page.html == "<main>Rendered</main>"
    assert [(method, url) for method, url, _, _ in client.calls] == [
        ("GET", "https://board.example/viewjob?jk=a1b2c3")
    ]
    assert browser.closed


def test_a_navigation_timeout_after_the_browser_read_the_document_returns_that_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BareRequestRefusingClient(_PolicyClient):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            if not self.calls:
                super().get(url, **kwargs)
                return httpx.Response(
                    403,
                    headers={"content-type": "text/html"},
                    content=b"<p>Request blocked.</p>",
                    request=httpx.Request("GET", url),
                )
            return super().get(url, **kwargs)

    client = _BareRequestRefusingClient()
    browser = _StalledBrowser(_DocumentThenStalledPage)
    fetcher = _stalled_fetcher(monkeypatch, client, browser)

    page = fetcher.fetch("https://board.example/viewjob?jk=a1b2c3")

    assert page.navigation_timed_out
    assert page.requested_url == "https://board.example/viewjob?jk=a1b2c3"
    assert page.final_url == "https://board.example/viewjob?jk=a1b2c3"
    assert page.document_status == 200
    assert page.html == "<main>Rendered</main>"
    assert not page.html_truncated
    assert [(method, url) for method, url, _, _ in client.calls] == [
        ("GET", "https://board.example/viewjob?jk=a1b2c3"),
        ("GET", "https://board.example/viewjob?jk=a1b2c3"),
    ]
    assert browser.closed


def test_a_navigation_timeout_without_a_landing_read_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RefusingClient(_PolicyClient):
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ReadTimeout("Landing read timed out.")

    fetcher = _stalled_fetcher(monkeypatch, _RefusingClient(), _StalledBrowser())

    with pytest.raises(httpx.ReadTimeout):
        fetcher.fetch("https://board.example/viewjob?jk=a1b2c3")


def test_the_browser_document_request_is_served_from_the_landing_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _PolicyClient()
    browser = _StalledBrowser(_Page)
    fetcher = _stalled_fetcher(monkeypatch, client, browser)

    page = fetcher.fetch("https://publisher.example/careers")

    assert [(method, url) for method, url, _, _ in client.calls] == [
        ("GET", "https://publisher.example/careers"),
    ]
    assert not page.navigation_timed_out
    assert page.final_url == "https://publisher.example/careers"
    assert page.document_status == 200
    assert page.html == "<html><main>Rendered</main></html>"


def _route_subresources(
    posting_url: str, urls: list[str]
) -> tuple[_PolicyClient, rendered_module._RenderCapture]:
    client = _PolicyClient()
    fetcher = RenderedPageFetcher(client=cast(Any, client))
    capture = rendered_module._RenderCapture(posting_url, [], [], [])
    for url in urls:
        fetcher._route(
            cast(Any, _Route()),
            cast(Any, _Request(url, resource_type="script", headers={})),
            capture,
        )
    return client, capture


def test_listed_non_content_hosts_are_aborted_before_policy() -> None:
    client, capture = _route_subresources(
        "https://jobs.publisher.example/posting/1",
        [
            "https://cdn.cookielaw.org/scripttemplates/otSDKStub.js",
            "https://www.googletagmanager.com/gtm.js?id=GTM-X",
            "https://jobs.publisher.example/static/app.js",
            "https://cdn.assets-vendor.example/widget.js",
        ],
    )

    assert [url for _, url, _, _ in client.calls] == [
        "https://jobs.publisher.example/static/app.js",
        "https://cdn.assets-vendor.example/widget.js",
    ]
    assert [(blocked.url, blocked.reason) for blocked in capture.blocked] == [
        ("https://cdn.cookielaw.org/scripttemplates/otSDKStub.js", "non_content_third_party_host"),
        ("https://www.googletagmanager.com/gtm.js?id=GTM-X", "non_content_third_party_host"),
    ]


def test_a_posting_on_a_listed_domain_still_reads_its_own_site() -> None:
    client, capture = _route_subresources(
        "https://careers.segment.com/jobs/42",
        ["https://cdn.segment.com/app.js", "https://www.google-analytics.com/analytics.js"],
    )

    assert [url for _, url, _, _ in client.calls] == ["https://cdn.segment.com/app.js"]
    assert [blocked.reason for blocked in capture.blocked] == ["non_content_third_party_host"]


def test_the_non_content_host_list_loads_from_the_package() -> None:
    hosts = rendered_module.non_content_hosts()

    assert {"cookielaw.org", "googletagmanager.com", "nr-data.net", "mixpanel.com"} <= hosts
    assert "publisher.example" not in hosts


def test_rendered_fetch_records_unsupported_browser_requests() -> None:
    client = _PolicyClient()
    fetcher = RenderedPageFetcher(client=cast(Any, client))
    request = _Request(
        "https://publisher.example/socket",
        method="CONNECT",
        resource_type="xhr",
        headers={},
    )

    with pytest.raises(ValueError, match="refuse non-GET method"):
        fetcher._request(cast(Any, request))
