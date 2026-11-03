"""Shared outbound HTTP politeness policy."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from socket import AF_INET, SOCK_STREAM, gaierror
from typing import Any

import httpx
import pytest

from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.infrastructure.web_fetch.policy import (
    MAX_REDIRECTS,
    MAX_ROBOTS_BYTES,
    ROBOTS_BASIS_EXTENSION,
    DestinationUnresolvableError,
    HostHeldError,
    InvalidFetchUrlError,
    PoliteHttpClient,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from tests.support.http import fake_httpx_client, public_resolver


def _response(url: str, status: int = 200, *, text: str = "") -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url), text=text)


def _resolving(*addresses: str) -> Callable[[str, int], list[tuple[object, ...]]]:
    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        family = AF_INET if ":" not in addresses[0] else 10
        return [(family, SOCK_STREAM, 6, "", (address, port)) for address in addresses]

    return resolve


def _pinned_transport(
    monkeypatch: pytest.MonkeyPatch, *, unreachable: frozenset[str] = frozenset()
) -> list[str]:
    """Replace the real transport, returning the addresses it was asked to reach."""
    attempts: list[str] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(attempts=attempts, unreachable=unreachable),
    )
    return attempts


def test_every_request_waits_after_robots_and_uses_an_honest_identity() -> None:
    events: list[tuple[str, object]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        events.append(("get", (url, headers["User-Agent"])))
        return _response(url, 404 if url.endswith("/robots.txt") else 200)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: events.append(("sleep", seconds)),
        jitter=lambda low, high: 0.5,
        getter=get,
        resolver=public_resolver,
        clock=lambda: 100.0,
    )

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert events == [
        ("get", ("https://example.test/robots.txt", "clearscout/test")),
        ("sleep", 0.5),
        ("get", ("https://example.test/jobs/1", "clearscout/test")),
        ("sleep", 0.5),
        ("get", ("https://example.test/jobs/2", "clearscout/test")),
    ]


def _timed_client(
    now: list[float], sleeps: list[float], *, response_seconds: float, robots: str = ""
) -> PoliteHttpClient:
    """A client whose clock advances by each sleep and by each page's response time."""

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, text=robots)
        now[0] += response_seconds
        return _response(url)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=sleep,
        jitter=lambda low, high: 0.5,
        getter=get,
        resolver=public_resolver,
        clock=lambda: now[0],
    )


def test_a_request_after_a_slow_response_starts_without_another_wait() -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = _timed_client(now, sleeps, response_seconds=2.0)

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert sleeps == [0.5]


def test_a_request_waits_only_for_the_rest_of_its_interval_on_that_host() -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = _timed_client(now, sleeps, response_seconds=0.2)

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert sleeps == [0.5, pytest.approx(0.3)]


def test_a_crawl_delay_still_holds_a_request_the_interval_would_allow() -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = _timed_client(
        now, sleeps, response_seconds=2.0, robots="User-agent: *\nCrawl-delay: 5\n"
    )

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert sleeps == [0.5, 5.0]


@pytest.mark.parametrize(
    ("floor", "later_floor", "expected"),
    [(2.2, 0.2, 2.2), (0.2, 0.0, 0.5)],
)
def test_a_registered_floor_can_only_strengthen_the_hosts_interval(
    floor: float, later_floor: float, expected: float
) -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = _timed_client(now, sleeps, response_seconds=0.0)
    client.register_host_minimum_interval("https://EXAMPLE.test/jobs", floor)
    client.register_host_minimum_interval("http://example.test:8080/elsewhere", later_floor)

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert sleeps == [pytest.approx(expected), pytest.approx(expected)]


def test_a_registered_floor_leaves_other_hosts_and_clients_at_the_default_interval() -> None:
    now = [100.0]
    sleeps: list[float] = []
    configured = _timed_client(now, sleeps, response_seconds=0.0)
    ordinary = _timed_client(now, sleeps, response_seconds=0.0)
    configured.register_host_minimum_interval("https://one.example.test/jobs", 2.2)

    configured.get("https://one.example.test/jobs/1")
    configured.get("https://two.example.test/jobs/1")
    ordinary.get("https://one.example.test/jobs/2")

    assert sleeps == [pytest.approx(2.2), 0.5, 0.5]


@pytest.mark.parametrize("seconds", [-0.1, float("inf"), float("-inf"), float("nan")])
def test_invalid_host_intervals_are_refused_before_any_request(seconds: float) -> None:
    client = _timed_client([100.0], [], response_seconds=0.0)

    with pytest.raises(ValueError, match="finite and nonnegative"):
        client.register_host_minimum_interval("https://example.test/jobs", seconds)


def test_a_registered_floor_preserves_a_stronger_crawl_delay() -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = _timed_client(
        now, sleeps, response_seconds=2.0, robots="User-agent: *\nCrawl-delay: 5\n"
    )
    client.register_host_minimum_interval("https://example.test/jobs", 2.2)

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert sleeps == [pytest.approx(2.2), 5.0]


def test_a_registered_floor_preserves_a_stronger_retry_after() -> None:
    now = [100.0]
    starts: list[float] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url)
        starts.append(now[0])
        if len(starts) == 1:
            return httpx.Response(
                429, request=httpx.Request("GET", url), headers={"Retry-After": "5"}
            )
        return _response(url, text="Full posting body")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=get,
        sleeper=sleep,
        jitter=lambda _low, _high: 0.5,
        resolver=public_resolver,
        clock=lambda: now[0],
    )
    client.register_host_minimum_interval("https://example.test/jobs", 2.2)

    response = client.get("https://example.test/jobs/1")

    assert response.text == "Full posting body"
    assert starts == [pytest.approx(102.2), pytest.approx(107.2)]


@pytest.mark.parametrize("boundary", ["address", "stale", "robots"])
def test_pinned_deadline_metadata_keeps_physical_attempts_separate_from_robots(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    now = [100.0]
    requests: list[httpx.Request] = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"User-agent: *\n"
            now[0] += 2.0
            yield b"Allow: /\n"

    def send(_connection: Any, request: httpx.Request) -> None:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return
        now[0] += 2.0
        if boundary == "address":
            raise httpx.ConnectError("Synthetic unroutable address", request=request)
        raise httpx.ReadError("Synthetic stale connection", request=request)

    client_type = fake_httpx_client(before_send=send)
    if boundary == "robots":

        def send_robots(
            _client: Any, request: httpx.Request, *, stream: bool = False
        ) -> httpx.Response:
            requests.append(request)
            assert request.url.path == "/robots.txt"
            return httpx.Response(200, request=request, stream=SlowStream())

        monkeypatch.setattr(client_type, "send", send_robots)
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", client_type)
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: now[0])
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        resolver=_resolving("93.184.216.34", "93.184.216.35"),
        clock=lambda: now[0],
    )
    if boundary != "robots":
        client.ensure_allowed("https://example.test/jobs/1")
        requests.clear()

    with pytest.raises(httpx.ReadTimeout) as raised:
        client.get("https://example.test/jobs/1", max_elapsed_seconds=1.0)

    assert sum(request.url.path == "/jobs/1" for request in requests) == int(boundary != "robots")
    assert getattr(raised.value, "request_attempted", None) is (boundary != "robots")


@pytest.mark.parametrize("boundary", ["pacing", "redirect", "retry", "robots"])
def test_deadline_metadata_distinguishes_a_dispatch_from_only_policy_work(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    now = [100.0]
    requests: list[str] = []
    url = "https://example.test/jobs/1"

    def get(address: str, **_kwargs: object) -> httpx.Response:
        requests.append(address)
        if address.endswith("/robots.txt"):
            if boundary == "robots":
                now[0] += 1.0
            return _response(address)
        if boundary == "redirect" and address == url:
            return httpx.Response(
                302, headers={"Location": "/jobs/2"}, request=httpx.Request("GET", address)
            )
        if boundary == "retry" and address == url:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, request=httpx.Request("GET", address)
            )
        return _response(address)

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: now[0])
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=get,
        resolver=public_resolver,
        jitter=lambda _low, _high: 0.0,
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        clock=lambda: now[0],
    )
    if boundary != "robots":
        client.get("https://example.test/priming")
        requests.clear()
        client.register_host_minimum_interval(url, 2.2)

    with pytest.raises(httpx.ReadTimeout) as raised:
        client.get(
            url,
            follow_redirects=True,
            max_elapsed_seconds=3.0 if boundary in {"redirect", "retry"} else 1.0,
        )

    dispatched = boundary in {"redirect", "retry"}
    assert requests.count(url) == int(dispatched)
    assert getattr(raised.value, "request_attempted", None) is dispatched
    assert "https://example.test/jobs/2" not in requests


def test_a_registered_floor_cannot_send_a_request_after_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    requested: list[str] = []

    def get(url: str, **_kwargs: object) -> httpx.Response:
        requested.append(url)
        return _response(url)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: now[0])
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=get,
        sleeper=sleep,
        jitter=lambda _low, _high: 0.5,
        resolver=public_resolver,
        clock=lambda: now[0],
    )
    client.register_host_minimum_interval("https://example.test/jobs", 2.2)

    with pytest.raises(httpx.ReadTimeout, match="total read deadline"):
        client.get("https://example.test/jobs/1", max_elapsed_seconds=1.0)

    assert requested == ["https://example.test/robots.txt"]
    assert now[0] == 101.0


def test_requests_to_one_host_reuse_one_connection_pinned_to_its_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    sent: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(
            expected_init_kwargs={"follow_redirects": False},
            forward_extensions=True,
            clients=clients,
            on_request=lambda request: sent.append(
                (str(request.url), request.headers["Host"], request.extensions["sni_hostname"])
            ),
        ),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )

    client.get("https://example.test/jobs/1")
    client.get("https://example.test/jobs/2")

    assert len(clients) == 1
    assert sent == [
        ("https://93.184.216.34/robots.txt", "example.test", "example.test"),
        ("https://93.184.216.34/jobs/1", "example.test", "example.test"),
        ("https://93.184.216.34/jobs/2", "example.test", "example.test"),
    ]


def test_two_hosts_behind_one_address_never_share_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(clients=clients),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )

    client.get("https://one.example.test/jobs/1")
    client.get("https://two.example.test/jobs/1")
    client.get("https://one.example.test/jobs/2")

    assert len(clients) == 2


def test_closing_the_client_closes_every_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(clients=clients),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )
    client.get("https://one.example.test/jobs/1")
    client.get("https://two.example.test/jobs/1")

    client.close()

    assert [pooled.closed for pooled in clients] == [True, True]


def _answering(*records: tuple[str, ...]) -> Callable[[str, int], list[tuple[object, ...]]]:
    """Answer each lookup with the next record set, as a CDN rotating its addresses does."""
    answers = itertools.cycle(records)

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        return [(AF_INET, SOCK_STREAM, 6, "", (address, port)) for address in next(answers)]

    return resolve


def test_a_host_whose_record_order_rotates_keeps_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(clients=clients),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=_answering(("93.184.216.34", "93.184.216.35"), ("93.184.216.35", "93.184.216.34")),
    )

    for index in range(3):
        client.get(f"https://cdn.example.test/jobs/{index}")

    assert len(clients) == 1


def test_a_connection_idle_past_keep_alive_is_closed_when_another_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    clients: list[Any] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(clients=clients),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=_answering(*((f"93.184.216.{suffix}",) for suffix in range(10, 14))),
        clock=lambda: now[0],
    )

    for index in range(4):
        client.get(f"https://cdn.example.test/jobs/{index}")
        now[0] += 10.0

    assert [pooled.closed for pooled in clients] == [True, True, True, False]


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError])
def test_a_reused_connection_the_server_closed_is_retried_on_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch, error: type[httpx.TransportError]
) -> None:
    clients: list[Any] = []

    def first_connection_went_stale(pooled: Any, request: httpx.Request) -> None:
        if pooled is clients[0] and request.url.path != "/robots.txt":
            raise error("Server disconnected without sending a response.", request=request)

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(clients=clients, before_send=first_connection_went_stale),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1")

    assert response.status_code == 200
    assert [pooled.closed for pooled in clients] == [True, False]


def test_a_reused_connection_that_fails_partway_through_a_reply_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
            return httpx.Request(method, url, headers=kwargs["headers"])

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)

            def cut_short() -> Iterator[bytes]:
                yield b"<html>partial"
                raise httpx.ReadError("connection reset", request=request)

            return httpx.Response(200, request=request, content=cut_short())

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )

    with pytest.raises(httpx.ReadError):
        client.get("https://example.test/jobs/1")

    assert paths == ["/robots.txt", "/jobs/1"]


def test_a_connection_that_fails_after_its_retry_is_not_retried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def every_page_read_fails(pooled: Any, request: httpx.Request) -> None:
        paths.append(request.url.path)
        if request.url.path != "/robots.txt":
            raise httpx.ReadError("connection reset", request=request)

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(before_send=every_page_read_fails),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda seconds: None, resolver=public_resolver
    )

    with pytest.raises(httpx.ReadError):
        client.get("https://example.test/jobs/1")

    assert paths == ["/robots.txt", "/jobs/1", "/jobs/1"]


def test_separate_clients_space_their_requests_to_one_host() -> None:
    now = [100.0]
    first_sleeps: list[float] = []
    second_sleeps: list[float] = []
    first = _timed_client(now, first_sleeps, response_seconds=0.0)
    second = _timed_client(now, second_sleeps, response_seconds=0.0)
    second.ensure_allowed("https://shared.example.test/jobs/1")
    now[0] += 10.0

    first.get("https://shared.example.test/jobs/1")
    second.get("https://shared.example.test/jobs/2")

    assert second_sleeps == [0.5]


def test_two_hosts_do_not_wait_for_each_other() -> None:
    """The board scan reads boards together, and one slow host must not hold the rest.

    The lock is per host on purpose. A single lock would keep politeness and
    make concurrent scanning pointless, and nothing else would notice.
    """
    guard = threading.Lock()
    start = threading.Barrier(2)
    overlap = threading.Barrier(2, timeout=5)
    active = 0
    maximum_active = 0

    def get(url: str, **kwargs: object) -> httpx.Response:
        nonlocal active, maximum_active
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        overlap.wait()
        with guard:
            active -= 1
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        jitter=lambda low, high: 0.0,
        getter=get,
        resolver=public_resolver,
    )

    def request(host: str) -> None:
        start.wait()
        client.get(f"https://{host}/jobs/1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(request, host) for host in ("one.test", "two.test")]:
            future.result()

    assert maximum_active == 2


def test_a_bounded_page_read_stops_after_the_bytes_the_caller_can_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed: list[tuple[str, bool]] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
            headers = kwargs["headers"]
            assert isinstance(headers, httpx.Headers)
            return httpx.Request(method, url, headers=headers)

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            streamed.append((request.url.path, stream))
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)
            return httpx.Response(200, request=request, content=b"useful-excess-body")

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    response = client.get("https://example.test/careers", max_bytes=6)

    assert response.content == b"useful"
    assert streamed == [("/robots.txt", True), ("/careers", True)]


def test_an_oversized_streamed_robots_policy_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
            headers = kwargs["headers"]
            assert isinstance(headers, httpx.Headers)
            return httpx.Request(method, url, headers=headers)

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(
                200,
                request=request,
                content=b"x" * (MAX_ROBOTS_BYTES + 2),
            )

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    with pytest.raises(RobotsUnavailableError, match="response exceeded limit"):
        client.get("https://example.test/careers")

    assert requested == ["/robots.txt"]


def test_an_oversized_injected_robots_policy_fails_closed() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        return _response(url, text="x" * (MAX_ROBOTS_BYTES + 2))

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(RobotsUnavailableError, match="response exceeded limit"):
        client.get("https://example.test/careers")

    assert requested == ["https://example.test/robots.txt"]


def test_a_bounded_page_read_has_a_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    closed = False

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"useful"
            now[0] = 31.0
            yield b"still-arriving"

        def close(self) -> None:
            nonlocal closed
            closed = True

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
            headers = kwargs["headers"]
            assert isinstance(headers, httpx.Headers)
            return httpx.Request(method, url, headers=headers)

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)
            return httpx.Response(200, request=request, stream=SlowStream())

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: now[0])
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    with pytest.raises(httpx.ReadTimeout, match="total read deadline") as raised:
        client.get(
            "https://example.test/careers",
            max_bytes=100,
            max_elapsed_seconds=30.0,
        )

    assert closed
    assert getattr(raised.value, "request_attempted", None) is True


def test_each_network_attempt_uses_only_the_logical_deadline_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    timeouts: list[httpx.Timeout] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
            timeout = kwargs["timeout"]
            assert isinstance(timeout, httpx.Timeout)
            timeouts.append(timeout)
            headers = kwargs["headers"]
            assert isinstance(headers, httpx.Headers)
            return httpx.Request(method, url, headers=headers)

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            if request.url.path == "/robots.txt":
                now[0] = 25.0
                return httpx.Response(404, request=request)
            return httpx.Response(200, request=request, content=b"posting")

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: now[0])
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    client.get(
        "https://example.test/careers",
        timeout=httpx.Timeout(20.0),
        max_elapsed_seconds=30.0,
    )

    assert len(timeouts) == 2
    assert timeouts[0].read == 20.0
    assert timeouts[1].connect == 5.0
    assert timeouts[1].read == 5.0


class _HeldLane:
    """A host lane another request holds, recording how long each caller offered to wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.waits.append(timeout)
        return False

    def release(self) -> None:
        pytest.fail("a lane that was never acquired was released")


def test_a_host_lane_wait_cannot_outlive_the_callers_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = _HeldLane()
    monkeypatch.setitem(PoliteHttpClient._host_locks, "lane-timeout.example.test", lane)
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: 100.0)
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    with pytest.raises(httpx.ReadTimeout, match="total read deadline"):
        client.get("https://lane-timeout.example.test/jobs/one", max_elapsed_seconds=2.0)

    assert lane.waits == [2.0]


def test_robots_denial_prevents_the_target_request() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        return _response(url, text="User-agent: *\nDisallow: /jobs/\n")

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: pytest.fail(f"unexpected wait: {seconds}"),
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(RobotsDeniedError, match="robots.txt"):
        client.get("https://example.test/jobs/1")

    assert requested == ["https://example.test/robots.txt"]


@pytest.mark.parametrize(
    ("first_policy", "second_policy", "first_allowed", "second_allowed"),
    [
        ("Allow: /", "Disallow: /", True, False),
        ("Disallow: /", "Allow: /", False, True),
    ],
)
def test_expired_robots_cache_observes_policy_changes_in_the_same_client(
    first_policy: str,
    second_policy: str,
    first_allowed: bool,
    second_allowed: bool,
) -> None:
    now = [100.0]
    policy = [first_policy]
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        text = f"User-agent: *\n{policy[0]}\n" if url.endswith("/robots.txt") else "ok"
        return _response(url, text=text)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda _seconds: None,
        getter=get,
        resolver=public_resolver,
        clock=lambda: now[0],
        robots_cache_ttl_seconds=10,
    )
    target = "https://example.test/jobs/1"

    if first_allowed:
        assert client.get(target).status_code == 200
    else:
        with pytest.raises(RobotsDeniedError):
            client.get(target)

    policy[0] = second_policy
    now[0] += 11
    if second_allowed:
        assert client.get(target).status_code == 200
    else:
        with pytest.raises(RobotsDeniedError):
            client.get(target)

    assert requested.count("https://example.test/robots.txt") == 2


@pytest.mark.parametrize("status", [401, 403, 410, 422])
def test_a_policy_the_host_declines_to_serve_is_read_as_no_policy(status: int) -> None:
    """RFC 9309 classes a 4xx robots response as unavailable, which permits the read.

    An API host that answers `/robots.txt` with 401 has stated nothing about
    crawling, and refusing there makes every board on that platform unreadable.
    """

    def get(url: str, **kwargs: object) -> httpx.Response:
        return _response(url, status if url.endswith("/robots.txt") else 200)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    assert client.get("https://api.example.test/jobs").status_code == 200


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_policy_that_may_still_exist_refuses_the_read(status: int) -> None:
    """A rate limit or a server fault hides a policy rather than denying one."""

    def get(url: str, **kwargs: object) -> httpx.Response:
        return _response(url, status if url.endswith("/robots.txt") else 200)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(RobotsUnavailableError, match="Could not verify"):
        client.get("https://api.example.test/jobs")


def test_non_http_and_credentialed_urls_are_refused_without_a_request() -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        pytest.fail(f"refused URL reached the network: {url}")

    client = PoliteHttpClient(user_agent="clearscout/test", getter=get, resolver=public_resolver)

    for url in ("file:///etc/passwd", "https://user:secret@example.test/jobs/1"):
        with pytest.raises(InvalidFetchUrlError):
            client.get(url)


def test_json_posts_use_the_same_robots_and_rate_policy() -> None:
    requested: list[tuple[str, str]] = []
    pauses: list[float] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(("GET", url))
        return _response(url, 404)

    def post(url: str, **kwargs: object) -> httpx.Response:
        requested.append(("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=pauses.append,
        jitter=lambda low, high: 0.5,
        getter=get,
        poster=post,
        resolver=public_resolver,
        clock=lambda: 100.0,
    )

    client.post("https://example.test/api/jobs", json={"limit": 20})

    assert requested == [
        ("GET", "https://example.test/robots.txt"),
        ("POST", "https://example.test/api/jobs"),
    ]
    assert pauses == [0.5]


def test_requests_to_one_host_are_serialized_across_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    refused: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        if url.endswith("/jobs/0"):
            # The second client asks while the first still holds the host's lane.
            try:
                clients[1].get("https://serialized.test/jobs/1", max_elapsed_seconds=1.0)
            except httpx.ReadTimeout as exc:
                refused.append(str(exc))
        return _response(url)

    # Each deadline reading advances a second, so a held lane is tried without waiting.
    ticks = itertools.count(100.0)
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: next(ticks)
    )
    clients = [
        PoliteHttpClient(
            user_agent="clearscout/test",
            sleeper=lambda seconds: None,
            jitter=lambda low, high: 0.0,
            getter=get,
            resolver=public_resolver,
        )
        for _ in range(2)
    ]

    clients[0].get("https://serialized.test/jobs/0")

    assert refused == ["The response exceeded its total read deadline."]
    assert requested == ["https://serialized.test/robots.txt", "https://serialized.test/jobs/0"]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.8", "169.254.169.254", "::1"],
)
def test_private_destinations_are_refused_before_a_request(address: str) -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        pytest.fail(f"private URL reached the network: {url}")

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        family = AF_INET if ":" not in address else 10
        return [(family, SOCK_STREAM, 6, "", (address, port))]

    client = PoliteHttpClient(user_agent="clearscout/test", getter=get, resolver=resolve)

    with pytest.raises(DestinationUnresolvableError, match="public network address"):
        client.get("https://example.test/jobs/1")


def test_unresolvable_destinations_are_refused_before_a_request() -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        pytest.fail(f"unresolved URL reached the network: {url}")

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        raise gaierror("synthetic DNS failure")

    client = PoliteHttpClient(user_agent="clearscout/test", getter=get, resolver=resolve)

    with pytest.raises(DestinationUnresolvableError, match="could not be resolved"):
        client.get("https://missing.test/jobs/1")


def test_redirects_are_rechecked_before_the_next_host_is_contacted() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        return httpx.Response(
            302,
            request=httpx.Request("GET", url),
            headers={"Location": "http://127.0.0.1/private"},
        )

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        address = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        return [(AF_INET, SOCK_STREAM, 6, "", (address, port))]

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=resolve,
    )

    with pytest.raises(DestinationUnresolvableError, match="public network address"):
        client.get("https://example.test/jobs/1")

    assert requested == [
        "https://example.test/robots.txt",
        "https://example.test/jobs/1",
    ]


def test_each_public_redirect_hop_gets_robots_and_rate_checks() -> None:
    requested: list[str] = []
    pauses: list[float] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        if url == "https://first.test/jobs/1":
            return httpx.Response(
                302,
                request=httpx.Request("GET", url),
                headers={"Location": "https://second.test/openings/1"},
            )
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=pauses.append,
        jitter=lambda low, high: 0.5,
        getter=get,
        resolver=public_resolver,
        clock=lambda: 100.0,
    )

    response = client.get("https://first.test/jobs/1")

    assert str(response.url) == "https://second.test/openings/1"
    assert requested == [
        "https://first.test/robots.txt",
        "https://first.test/jobs/1",
        "https://second.test/robots.txt",
        "https://second.test/openings/1",
    ]
    assert pauses == [0.5, 0.5]


def test_default_transport_connects_to_the_validated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, object]] = []

    def record(request: httpx.Request) -> None:
        requests.append(
            (
                str(request.url),
                request.headers["Host"],
                request.extensions["sni_hostname"],
            )
        )

    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client",
        fake_httpx_client(
            expected_init_kwargs={"follow_redirects": False},
            forward_extensions=True,
            on_request=record,
        ),
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=public_resolver,
    )

    response = client.get("https://example.test/jobs/1")

    assert requests == [
        ("https://93.184.216.34/robots.txt", "example.test", "example.test"),
        ("https://93.184.216.34/jobs/1", "example.test", "example.test"),
    ]
    assert str(response.url) == "https://example.test/jobs/1"


def test_a_redirected_robots_policy_is_followed_to_the_page_that_holds_it() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url == "https://example.test/robots.txt":
            return httpx.Response(
                301,
                request=httpx.Request("GET", url),
                headers={"Location": "https://www.example.test/robots.txt"},
            )
        if url == "https://www.example.test/robots.txt":
            return _response(url, text="User-agent: *\nAllow: /\n")
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    assert client.get("https://example.test/jobs/1").status_code == 200
    assert requested == [
        "https://example.test/robots.txt",
        "https://www.example.test/robots.txt",
        "https://example.test/jobs/1",
    ]


def test_a_caller_user_agent_is_replaced_rather_than_sent_alongside_ours() -> None:
    sent: list[list[tuple[bytes, bytes]]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        sent.append(httpx.Request("GET", url, headers=headers).headers.raw)
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    client.get(
        "https://example.test/jobs/1",
        headers={"User-Agent": "page/1", "Accept": "text/html"},
    )

    identities = [value for key, value in sent[0] if key.lower() == b"user-agent"]
    assert identities == [b"clearscout/test"]


def test_a_cold_fetch_resolves_the_host_once_for_robots_and_the_target() -> None:
    resolutions: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        resolutions.append((host, port))
        return [(AF_INET, SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=lambda url, **kwargs: _response(url, 404 if url.endswith("robots.txt") else 200),
        resolver=resolve,
    )

    client.get("https://example.test/jobs/1")

    assert resolutions == [("example.test", 443)]


def test_an_unroutable_address_falls_back_to_the_rest_of_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = _pinned_transport(monkeypatch, unreachable=frozenset({"2606:2800:220::1"}))
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=_resolving("2606:2800:220::1", "93.184.216.34"),
    )

    assert client.get("https://example.test/jobs/1").status_code == 200
    assert attempts == [
        "2606:2800:220::1",
        "93.184.216.34",
        "2606:2800:220::1",
        "93.184.216.34",
    ]


def test_a_private_record_beside_a_public_one_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = _pinned_transport(monkeypatch)
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        resolver=_resolving("10.0.0.8", "93.184.216.34"),
    )

    assert client.get("https://example.test/jobs/1").status_code == 200
    assert attempts == ["93.184.216.34", "93.184.216.34"]


def test_ensure_allowed_rechecks_the_address_behind_a_cached_robots_policy() -> None:
    address = ["93.184.216.34"]

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        return [(AF_INET, SOCK_STREAM, 6, "", (address[0], port))]

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=lambda url, **kwargs: _response(url, 404),
        resolver=resolve,
    )

    client.ensure_allowed("https://example.test/jobs/1")
    address[0] = "127.0.0.1"

    with pytest.raises(DestinationUnresolvableError, match="public network address"):
        client.ensure_allowed("https://example.test/jobs/2")


def test_a_redirect_chain_is_capped_rather_than_followed_forever() -> None:
    hops: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        hops.append(url)
        step = int(url.rsplit("/", 1)[1])
        return httpx.Response(
            302,
            request=httpx.Request("GET", url),
            headers={"Location": f"https://example.test/hop/{step + 1}"},
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(httpx.TooManyRedirects):
        client.get("https://example.test/hop/0")

    assert len(hops) == MAX_REDIRECTS + 1


def test_credentials_are_not_carried_onto_a_redirect_target() -> None:
    seen: list[tuple[str, object, object]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        seen.append((url, headers.get("Authorization"), headers.get("Cookie")))
        if url == "https://first.test/jobs/1":
            return httpx.Response(
                302,
                request=httpx.Request("GET", url),
                headers={"Location": "https://second.test/openings/1"},
            )
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    client.get(
        "https://first.test/jobs/1",
        headers={"Authorization": "Bearer secret", "Cookie": "session=1"},
    )

    assert seen == [
        ("https://first.test/jobs/1", "Bearer secret", "session=1"),
        ("https://second.test/openings/1", None, None),
    ]


def test_a_cookie_jar_carries_a_cookie_set_on_a_redirect_only_to_its_own_host() -> None:
    seen: list[tuple[str, object]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        seen.append((url, headers.get("Cookie")))
        hops = {
            "https://jobs.acme.test/search": (
                "https://jobs.acme.test/search?checked=1",
                "session=abc; domain=.acme.test; path=/",
            ),
            "https://jobs.acme.test/search?checked=1": ("https://other.test/page", ""),
        }
        if url in hops:
            location, cookie = hops[url]
            headers_out = {"Location": location, **({"Set-Cookie": cookie} if cookie else {})}
            return httpx.Response(302, request=httpx.Request("GET", url), headers=headers_out)
        return _response(url)

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )
    jar = httpx.Cookies()

    client.get("https://jobs.acme.test/search", cookies=jar)

    assert seen == [
        ("https://jobs.acme.test/search", None),
        ("https://jobs.acme.test/search?checked=1", "session=abc"),
        ("https://other.test/page", None),
    ]
    assert jar.get("session") == "abc"


def test_a_see_other_downgrades_the_post_and_drops_the_body_it_described() -> None:
    calls: list[tuple[str, str, object, object]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 404)
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        calls.append(("GET", url, headers.get("Content-Length"), kwargs.get("json")))
        return _response(url)

    def post(url: str, **kwargs: object) -> httpx.Response:
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        calls.append(("POST", url, headers.get("Content-Length"), kwargs["json"]))
        return httpx.Response(
            303,
            request=httpx.Request("POST", url),
            headers={"Location": "https://example.test/done"},
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        poster=post,
        resolver=public_resolver,
    )

    client.post(
        "https://example.test/api/jobs",
        json={"limit": 20},
        headers={"Content-Length": "13"},
    )

    assert calls == [
        ("POST", "https://example.test/api/jobs", "13", {"limit": 20}),
        ("GET", "https://example.test/done", None, None),
    ]


def test_a_temporary_redirect_keeps_the_method_and_the_body() -> None:
    posted: list[tuple[str, object]] = []

    def post(url: str, **kwargs: object) -> httpx.Response:
        posted.append((url, kwargs["json"]))
        if url == "https://example.test/api/jobs":
            return httpx.Response(
                307,
                request=httpx.Request("POST", url),
                headers={"Location": "https://example.test/api/v2/jobs"},
            )
        return httpx.Response(200, request=httpx.Request("POST", url), json={})

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=lambda url, **kwargs: _response(url, 404),
        poster=post,
        resolver=public_resolver,
    )

    client.post("https://example.test/api/jobs", json={"limit": 20})

    assert posted == [
        ("https://example.test/api/jobs", {"limit": 20}),
        ("https://example.test/api/v2/jobs", {"limit": 20}),
    ]


def test_an_out_of_range_port_is_refused_without_a_request() -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        pytest.fail(f"invalid URL reached the network: {url}")

    client = PoliteHttpClient(user_agent="clearscout/test", getter=get, resolver=public_resolver)

    with pytest.raises(InvalidFetchUrlError, match="valid network port"):
        client.get("https://example.test:99999/jobs/1")


def _policy_client(
    policies: Mapping[str, str], requested: list[str], *, page_status: int = 200
) -> PoliteHttpClient:
    """A client whose hosts publish the given robots text and answer every page."""

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        host = httpx.URL(url).host
        if url.endswith("/robots.txt"):
            return _response(url, text=policies.get(host, ""))
        return _response(url, page_status, text="<html>posting</html>")

    return PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )


def test_a_single_posting_read_opens_one_page_robots_disallows_and_says_so() -> None:
    requested: list[str] = []
    client = _policy_client({"example.test": "User-agent: *\nDisallow: /jobs/\n"}, requested)

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.status_code == 200
    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ
    assert requested == ["https://example.test/robots.txt", "https://example.test/jobs/1"]


def test_a_permitted_read_reports_that_robots_allowed_it() -> None:
    client = _policy_client({"example.test": "User-agent: *\nAllow: /\n"}, [])

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.ALLOWED


_KNOWN_POSTING_ADDRESSES = ["https://example.test/jobs/1", "https://example.test/api/v1/jobs/1"]


@pytest.mark.parametrize("target", _KNOWN_POSTING_ADDRESSES)
def test_a_known_posting_read_is_exempt_wherever_the_posting_is_stated(target: str) -> None:
    """The page and the vendor endpoint behind it are one person's read of one posting."""
    client = _policy_client({"example.test": "User-agent: *\nDisallow: /\n"}, [])

    response = client.get(target, intent=PostingReadIntent.SINGLE_POSTING)

    assert response.status_code == 200
    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ


@pytest.mark.parametrize("target", _KNOWN_POSTING_ADDRESSES)
def test_a_crawl_of_either_address_is_refused_by_that_policy(target: str) -> None:
    client = _policy_client({"example.test": "User-agent: *\nDisallow: /\n"}, [])

    with pytest.raises(RobotsDeniedError):
        client.get(target)


@pytest.mark.parametrize("named", ["clearscout", "tailorcv"])
def test_a_robots_group_that_names_this_client_refuses_even_a_single_read(named: str) -> None:
    requested: list[str] = []
    client = _policy_client(
        {"example.test": f"User-agent: {named}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"},
        requested,
    )

    with pytest.raises(RobotsDeniedError):
        client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert requested == ["https://example.test/robots.txt"]


def test_a_group_for_the_former_name_refuses_a_crawl_the_wildcard_allows() -> None:
    client = _policy_client(
        {"example.test": "User-agent: tailorcv\nDisallow: /jobs/\n\nUser-agent: *\nAllow: /\n"},
        [],
    )

    with pytest.raises(RobotsDeniedError):
        client.get("https://example.test/jobs/1")


def test_a_group_for_the_current_name_wins_over_one_for_the_former_name() -> None:
    client = _policy_client(
        {"example.test": "User-agent: tailorcv\nDisallow: /\n\nUser-agent: clearscout\nAllow: /\n"},
        [],
    )

    client.get("https://example.test/jobs/1")


def test_a_robots_file_that_starts_with_a_byte_order_mark_still_applies_its_first_group() -> None:
    requested: list[str] = []
    client = _policy_client({"example.test": "\ufeffUser-agent: *\nDisallow: /jobs/\n"}, requested)

    with pytest.raises(RobotsDeniedError):
        client.get("https://example.test/jobs/1")

    assert requested == ["https://example.test/robots.txt"]


@pytest.mark.parametrize(
    ("policy", "intent", "refused"),
    [
        ("User-agent: *\nDisallow: /\n", PostingReadIntent.CRAWL, True),
        ("User-agent: *\nDisallow: /\n", PostingReadIntent.SINGLE_POSTING, False),
        (
            "User-agent: clearscout\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
            PostingReadIntent.SINGLE_POSTING,
            True,
        ),
    ],
)
def test_the_precheck_applies_the_same_robots_rule_as_the_read(
    policy: str, intent: PostingReadIntent, refused: bool
) -> None:
    client = _policy_client({"example.test": policy}, [])

    try:
        client.ensure_allowed("https://example.test/jobs/1", intent=intent)
    except RobotsDeniedError:
        precheck_refused = True
    else:
        precheck_refused = False
    try:
        client.get("https://example.test/jobs/1", intent=intent)
    except RobotsDeniedError:
        read_refused = True
    else:
        read_refused = False

    assert (precheck_refused, read_refused) == (refused, refused)


def test_a_group_naming_another_client_leaves_a_single_read_exempt() -> None:
    client = _policy_client(
        {"example.test": "User-agent: otherbot\nDisallow: /\n\nUser-agent: *\nDisallow: /\n"},
        [],
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ


@pytest.mark.parametrize("named", ["github", "https", "sethstraw"])
def test_a_group_matching_only_the_user_agent_comment_does_not_name_this_client(
    named: str,
) -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(
                url, text=f"User-agent: {named}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
            )
        return _response(url, text="<html>posting</html>")

    client = PoliteHttpClient(
        user_agent="clearscout/0.1 (+https://github.com/sethstraw/clearscout)",
        sleeper=lambda _: None,
        getter=get,
        resolver=public_resolver,
    )

    response = client.get("https://example.test/jobs/1")

    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.ALLOWED


_JOB_BOARD_POLICY = "User-agent: *\nDisallow: /\nAllow: */JobBoard/\nDisallow: */JobBoardView\n"
_JOB_BOARD = "https://example.test/ACME1001/JobBoard/0000-uuid"


@pytest.mark.parametrize(
    ("policy", "target", "allowed"),
    [
        (_JOB_BOARD_POLICY, f"{_JOB_BOARD}/", True),
        (_JOB_BOARD_POLICY, f"{_JOB_BOARD}/OpportunityDetail?opportunityId=1", True),
        (_JOB_BOARD_POLICY, f"{_JOB_BOARD}/JobBoardView/LoadSearchResults", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "https://example.test/a.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "https://example.test/a.pdf?x=1", True),
        ("User-agent: *\nDisallow: /jobs\nAllow: /jobs\n", "https://example.test/jobs/1", True),
        (
            "User-agent: *\nDisallow: /\n\nUser-agent: clearscout\nDisallow: /\nAllow: /jobs/\n",
            "https://example.test/jobs/1",
            True,
        ),
        (
            "User-agent: clearscout\nDisallow: /jobs/\n\nUser-agent: *\nAllow: /\n",
            "https://example.test/jobs/1",
            False,
        ),
    ],
)
def test_the_longest_matching_robots_rule_decides_a_crawl(
    policy: str, target: str, allowed: bool
) -> None:
    """RFC 9309: the most specific rule wins, `*` and `$` are patterns, and a tie allows."""
    client = _policy_client({"example.test": policy}, [])

    if allowed:
        assert client.get(target).extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.ALLOWED
    else:
        with pytest.raises(RobotsDeniedError):
            client.get(target)


def test_a_single_read_is_not_retried_when_the_host_asks_for_a_wait() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, text="User-agent: *\nDisallow: /jobs/\n")
        return httpx.Response(429, request=httpx.Request("GET", url), headers={"Retry-After": "1"})

    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.status_code == 429
    assert requested.count("https://example.test/jobs/1") == 1


def _redirecting_getter(
    requested: list[str], *, robots: Mapping[str, str], target: str
) -> Callable[..., httpx.Response]:
    """Answer one posting URL with a redirect to ``target``, under per-host robots."""

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, text=robots[httpx.URL(url).host])
        if url == "https://example.test/jobs/1":
            return httpx.Response(
                302, request=httpx.Request("GET", url), headers={"Location": target}
            )
        return _response(url, text="posting")

    return get


def test_a_single_read_follows_a_redirect_into_another_hosts_blanket_disallow() -> None:
    requested: list[str] = []
    get = _redirecting_getter(
        requested,
        robots={
            "example.test": "User-agent: *\nDisallow: /\n",
            "other.test": "User-agent: *\nDisallow: /\n",
        },
        target="https://other.test/jobs/1",
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.text == "posting"
    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ
    assert "https://other.test/jobs/1" in requested


def test_a_redirect_target_naming_this_client_refuses_the_single_read() -> None:
    requested: list[str] = []
    get = _redirecting_getter(
        requested,
        robots={
            "example.test": "User-agent: *\nDisallow: /\n",
            "other.test": "User-agent: clearscout\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
        },
        target="https://other.test/jobs/1",
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    with pytest.raises(RobotsDeniedError):
        client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert "https://other.test/jobs/1" not in requested


def test_a_crawl_redirected_into_another_hosts_disallow_is_still_refused() -> None:
    requested: list[str] = []
    get = _redirecting_getter(
        requested,
        robots={
            "example.test": "User-agent: *\nAllow: /\n",
            "other.test": "User-agent: *\nDisallow: /\n",
        },
        target="https://other.test/jobs/1",
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    with pytest.raises(RobotsDeniedError):
        client.get("https://example.test/jobs/1")

    assert "https://other.test/jobs/1" not in requested


def test_an_allowed_single_posting_read_still_retries_a_retry_after_wait() -> None:
    """The retry skip is decided by why the read was let through, not the intent alone."""
    requested: list[str] = []
    statuses = iter([503, 200])

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return _response(url, text="User-agent: *\nAllow: /\n")
        status = next(statuses)
        if status == 503:
            return httpx.Response(
                503, request=httpx.Request("GET", url), headers={"Retry-After": "1"}
            )
        return _response(url, status)

    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.status_code == 200
    assert requested.count("https://example.test/jobs/1") == 2


def test_a_same_host_redirect_from_a_single_read_stays_exempt() -> None:
    """The exemption follows the chain the client itself follows."""
    requested: list[str] = []
    get = _redirecting_getter(
        requested,
        robots={"example.test": "User-agent: *\nDisallow: /\n"},
        target="https://example.test/careers",
    )
    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.text == "posting"
    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ
    assert "https://example.test/careers" in requested


def test_an_exempt_redirect_to_a_retryable_host_is_not_retried() -> None:
    """The exemption earned on the first hop must survive a redirect and a retry-eligible status."""
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url == "https://example.test/robots.txt":
            return _response(url, text="User-agent: *\nDisallow: /\n")
        if url == "https://example.test/jobs/1":
            return httpx.Response(
                301,
                request=httpx.Request("GET", url),
                headers={"Location": "https://other.test/roles/1"},
            )
        if url == "https://other.test/robots.txt":
            return _response(url, text="User-agent: *\nAllow: /\n")
        return httpx.Response(503, request=httpx.Request("GET", url), headers={"Retry-After": "1"})

    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    response = client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert response.status_code == 503
    assert response.extensions[ROBOTS_BASIS_EXTENSION] == RobotsReadBasis.SINGLE_POSTING_READ
    assert requested.count("https://other.test/roles/1") == 1


def test_an_unreadable_robots_policy_still_refuses_a_single_read() -> None:
    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, 503)
        pytest.fail(f"page read without a readable policy: {url}")

    client = PoliteHttpClient(
        user_agent="clearscout/test", sleeper=lambda _: None, getter=get, resolver=public_resolver
    )

    with pytest.raises(RobotsUnavailableError):
        client.get("https://example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)


@pytest.mark.parametrize(
    ("robots", "status", "retry_after", "advance", "paced", "ready_at"),
    [
        ("User-agent: *\nCrawl-delay: 5\n", 200, "5", 0.0, True, 105.0),
        ("", 429, "5", 0.0, False, 105.0),
        ("", 503, "5", 0.0, False, 105.0),
        ("User-agent: *\nCrawl-delay: 5\n", 429, "5", 0.0, False, 105.0),
        ("User-agent: *\nCrawl-delay: 5\n", 429, "2", 3.0, True, 105.0),
        ("User-agent: *\nCrawl-delay: 45\n", 200, "5", 0.0, False, 145.0),
    ],
    ids=[
        "crawl-delay",
        "retry-after-429",
        "retry-after-503",
        "both",
        "retry-after-expired-crawl-delay-holds",
        "crawl-delay-past-the-page-budget",
    ],
)
def test_a_hold_past_the_read_deadline_names_pacing_apart_from_a_backoff(
    monkeypatch: pytest.MonkeyPatch,
    robots: str,
    status: int,
    retry_after: str,
    advance: float,
    paced: bool,
    ready_at: float,
) -> None:
    now = [100.0]

    def get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return _response(url, text=robots)
        return httpx.Response(
            status, request=httpx.Request("GET", url), headers={"Retry-After": retry_after}
        )

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.monotonic", lambda: 50.0)
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        getter=get,
        sleeper=lambda seconds: None,
        jitter=lambda _low, _high: 0.0,
        resolver=public_resolver,
        clock=lambda: now[0],
    )
    client.get("https://example.test/jobs/1")
    now[0] += advance

    with pytest.raises(HostHeldError) as held:
        client.get("https://example.test/jobs/2", max_elapsed_seconds=1.0)

    assert held.value.paced is paced
    assert held.value.ready_at == pytest.approx(ready_at)
