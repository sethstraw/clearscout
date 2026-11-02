"""Stand-in for ``httpx.Client`` that records what the code under test dispatched.

Monkeypatch the returned class over ``httpx.Client`` in the target module. The
class matches the surface the ``PoliteHttpClient`` transport uses (constructor,
``build_request``, ``send``, ``close``); every parameter is typed so the
whole strict-mypy suite covers the seam.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from socket import AF_INET, SOCK_STREAM
from typing import Any

import httpx
import pytest

from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient

DAYFORCE_POSTING_URL = "https://jobs.dayforcehcm.com/en-US/globex/CANDIDATEPORTAL/jobs/{reference}"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


# An employer's own careers page: prose about the company, a short list of roles, an about link.
COMPANY_CAREERS_PAGE = (
    "<html><head><title>Careers at Example Works</title></head><body>"
    '<header><nav><a href="/">Home</a><a href="/products">Products</a></nav></header>'
    "<main><h1>Careers at Example Works</h1>"
    "<p>Example Works builds scheduling and routing software for regional transit "
    "agencies. Our teams work in small groups that own a service from design through "
    "operation, and we publish our engineering handbook so candidates can read how we "
    "work before they talk to us. Most roles are remote within the country, and we meet "
    "in person twice a year.</p>"
    '<ul><li><a href="/careers/roles/route-planner">Route planning engineer</a></li>'
    '<li><a href="/careers/roles/fleet-data">Fleet data analyst</a></li></ul>'
    '<p><a href="/about-us">About us</a></p>'
    "</main></body></html>"
)


def sitemap_urlset(*locs: str) -> str:
    """Build the sitemap document a publisher serves for these posting addresses."""
    entries = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset xmlns="{SITEMAP_NS}">{entries}</urlset>'


def sitemap_rss(channel: str, *links: str) -> str:
    """Build the RSS feed of postings some SuccessFactors sites serve at their sitemap address."""
    items = "".join(
        f"<item><title>Role {index}</title><description><![CDATA[&lt;p&gt;Role {index}."
        f"&lt;/p&gt;]]></description><link>{link}</link>"
        f'<guid isPermaLink="false">{index}</guid><g:id>{index}</g:id>'
        "<g:location>Springfield, CA</g:location></item>"
        for index, link in enumerate(links)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" '
        'xmlns:g="http://base.google.com/ns/1.0"><channel><title></title><description>'
        f"</description><link>{channel}</link><language>en_US</language><ttl>720</ttl>"
        f"{items}</channel></rss>"
    )


def public_resolver(host: str, port: int) -> list[tuple[object, ...]]:
    """Resolve any host to one public address, so policy never depends on real DNS."""
    return [(AF_INET, SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def recorded_web_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[PoliteHttpClient], list[PoliteHttpClient]]:
    """Record every polite client built, and every one closed, for the rest of the test."""
    built: list[PoliteHttpClient] = []
    closed: list[PoliteHttpClient] = []
    build = PoliteHttpClient.__init__
    close = PoliteHttpClient.close

    def recording_build(self: PoliteHttpClient, *args: Any, **kwargs: Any) -> None:
        build(self, *args, **kwargs)
        built.append(self)

    def recording_close(self: PoliteHttpClient) -> None:
        closed.append(self)
        close(self)

    monkeypatch.setattr(PoliteHttpClient, "__init__", recording_build)
    monkeypatch.setattr(PoliteHttpClient, "close", recording_close)
    return built, closed


def policy_client(
    handler: Callable[[str], httpx.Response],
    *,
    robots: str = "",
    poster: Callable[[str, Any], httpx.Response] | None = None,
) -> PoliteHttpClient:
    """Route real client calls to fixed responses under one published robots policy.

    Every host serves ``robots``, so a test states the policy the read is held to
    rather than patching the readers. A POST goes to ``poster`` with its JSON body,
    or to ``handler`` when none is given, and never to the network.
    """

    def get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return httpx.Response(200, request=httpx.Request("GET", url), text=robots)
        return handler(url)

    def post(url: str, *, json: Any = None, **_kwargs: object) -> httpx.Response:
        return handler(url) if poster is None else poster(url, json)

    return PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        getter=get,
        poster=post,
        resolver=public_resolver,
    )


class CountingFeedClient:
    """Answer one publisher site feed as JSON and count how often it was read."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Serve these feed rows to every request."""
        self.rows = rows
        self.requested: list[str] = []

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        """Answer the feed, recording the exact address the reader asked for."""
        self.requested.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=self.rows)


def dayforce_feed_row(reference: str, title: str) -> dict[str, Any]:
    """Build one row of the whole-site feed a Dayforce posting check reads."""
    return {
        "Title": title,
        "Description": "Own the clinical data model and terminology governance.",
        "CompanyName": "Acme Health",
        "JobDetailsUrl": DAYFORCE_POSTING_URL.format(reference=reference),
        "City": "Springfield",
        "State": "IL",
        "Country": "US",
        "ReferenceNumber": reference,
    }


def paylocity_page_job(job_id: int, title: str, **stated: Any) -> dict[str, Any]:
    """Build one entry of a Paylocity board page's ``pageData.Jobs``."""
    return {
        "JobId": job_id,
        "JobTitle": title,
        "LocationName": "Springfield, OH",
        "ShouldDisplayLocation": True,
        "PublishedDate": "2026-09-17T07:05:54-05:00",
        "Description": "About UsAt Examplehealth, we're changing how people experience pharmacy "
        "care by simplifying the proce",
        "IsInternal": False,
        "HiringDepartment": None,
        "JobLocation": {
            "LocationId": 55,
            "Name": None,
            "Address": "1 Main St",
            "City": "Springfield",
            "State": "OH",
            "Zip": "45500",
            "Country": "USA",
        },
        "IsRemote": False,
        "IndeedRemoteType": 0,
        **stated,
    }


def paylocity_board_page(jobs: list[dict[str, Any]]) -> str:
    """Build the board page Paylocity serves, every opening embedded in ``window.pageData``."""
    page_data = {"Departments": [], "IsLeadJoinEnabled": True, "Jobs": jobs}
    return (
        "<html><head><title>Current Openings</title></head><body><div id='app'></div>"
        f"<script>window.pageData = {json.dumps(page_data)};\nwindow.other = 1;</script>"
        "</body></html>"
    )


def fake_httpx_client(
    *,
    attempts: list[str] | None = None,
    unreachable: frozenset[str] = frozenset(),
    expected_init_kwargs: Mapping[str, object] | None = None,
    forward_extensions: bool = False,
    on_request: Callable[[httpx.Request], None] | None = None,
    clients: list[Any] | None = None,
    before_send: Callable[[Any, httpx.Request], None] | None = None,
) -> type:
    """Return a class configured to record and script one test's HTTP traffic.

    ``attempts`` collects the host of every ``send`` call. ``unreachable`` names
    hosts that must raise ``ConnectError`` before any response. Set
    ``expected_init_kwargs`` to assert the transport was built with exactly
    those options; ``forward_extensions=True`` when the test needs the outbound
    request to carry the caller's ``extensions`` mapping (SNI, timeouts, ...).
    ``on_request`` fires once per built request for tests that inspect request
    shape. ``clients`` collects every constructed instance, each with a ``closed`` flag.
    ``before_send`` sees the sending instance and request, and may raise a transport error.
    """
    captured_attempts = attempts
    captured_unreachable = unreachable
    captured_expected = expected_init_kwargs
    captured_forward_extensions = forward_extensions
    captured_on_request = on_request
    captured_clients = clients
    captured_before_send = before_send

    class FakeHttpxClient:
        def __init__(self, **kwargs: object) -> None:
            if captured_expected is not None:
                assert kwargs == dict(captured_expected), (
                    f"unexpected httpx.Client init kwargs: {kwargs!r}"
                )
            self.closed = False
            if captured_clients is not None:
                captured_clients.append(self)

        def close(self) -> None:
            self.closed = True

        def build_request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | httpx.Headers | None = None,
            extensions: Mapping[str, object] | None = None,
            **_ignored: object,
        ) -> httpx.Request:
            if captured_forward_extensions and extensions is not None:
                request = httpx.Request(method, url, headers=headers, extensions=dict(extensions))
            else:
                request = httpx.Request(method, url, headers=headers)
            if captured_on_request is not None:
                captured_on_request(request)
            return request

        # Production sends streaming and non-streaming reads through the same
        # client, so a seam omitting stream would fail only on the streaming path.
        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            address = str(request.url.host)
            if captured_attempts is not None:
                captured_attempts.append(address)
            if address in captured_unreachable:
                raise httpx.ConnectError("no route to host", request=request)
            if captured_before_send is not None:
                captured_before_send(self, request)
            status = 404 if request.url.path == "/robots.txt" else 200
            return httpx.Response(status, request=request)

    return FakeHttpxClient
