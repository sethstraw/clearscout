"""Shared robots, rate-limit, and host-serialization policy for web reads."""

from __future__ import annotations

import ipaddress
import math
import random
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

from clearscout.domain.records import PostingReadIntent, RobotsReadBasis

MIN_REQUEST_DELAY_SECONDS = 0.25
MAX_REQUEST_DELAY_SECONDS = 0.75
MAX_REDIRECTS = 20
MAX_ROBOTS_BYTES = 512_000
ROBOTS_CACHE_TTL_SECONDS = 15 * 60
# A 429 or 503 is retried once only when its Retry-After fits this bound; a longer
# stated wait still holds the host back, up to the backoff ceiling.
MAX_RETRY_AFTER_SECONDS = 30.0
MAX_HOST_BACKOFF_SECONDS = 600.0
MAX_UNBOUNDED_HOST_WAIT_SECONDS = 60.0
# Each board page read, and each continuation of one, gets this much wall time.
MAX_PAGE_READ_SECONDS = 30.0
# httpx's default keep-alive expiry: a client idle this long holds no live socket.
IDLE_CONNECTION_SECONDS = 5.0
_RETRY_AFTER_STATUSES = frozenset({429, 503})
# A keep-alive socket the server already closed fails this way before any reply.
_STALE_CONNECTION_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)
# httpx response extension holding the RobotsReadBasis that let the request proceed.
ROBOTS_BASIS_EXTENSION = "clearscout.robots_basis"
# Sites wrote robots.txt groups for the project's former name; they still apply.
FORMER_ROBOTS_AGENT = "tailorcv"


def read_basis(response: httpx.Response) -> RobotsReadBasis | None:
    """Return why robots policy let this response be read, as the client stamped it."""
    basis = response.extensions.get(ROBOTS_BASIS_EXTENSION)
    return basis if isinstance(basis, RobotsReadBasis) else None


type _Get = Callable[..., httpx.Response]
type _Post = Callable[..., httpx.Response]
type _Resolver = Callable[[str, int], Sequence[tuple[Any, ...]]]
type _Hop = Callable[..., httpx.Response]


def _resolve_addresses(host: str, port: int) -> Sequence[tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class FetchPolicyError(RuntimeError):
    """Base error for a request refused before the target is contacted."""


class InvalidFetchUrlError(FetchPolicyError):
    """The target is not an HTTP(S) URL suitable for an outbound request."""


class DestinationUnresolvableError(FetchPolicyError):
    """The target's host answered no public address, so the read is retryable."""


UNRESOLVED_DESTINATION_NOTE = (
    "The publisher's destination could not be resolved, and the read remains retryable."
)


class RobotsDeniedError(FetchPolicyError):
    """The target's robots policy refuses this client identity."""

    def __init__(self, url: str) -> None:
        """Record which URL was refused, which is not always the one requested."""
        super().__init__(f"robots.txt disallows fetching {url}")
        self.url = url


class RobotsUnavailableError(FetchPolicyError):
    """The target's robots policy could not be read safely."""


class RequestDeadlineExceeded(httpx.ReadTimeout):
    """A logical read deadline, with whether its target request was dispatched."""

    def __init__(self, method: str, url: str, *, request_attempted: bool = False) -> None:
        """Retain dispatch evidence independently of policy and pacing work."""
        super().__init__(
            "The response exceeded its total read deadline.", request=httpx.Request(method, url)
        )
        self.request_attempted = request_attempted


class HostHeldError(FetchPolicyError):
    """The host asked for a wait (Crawl-delay, Retry-After) longer than this read allows.

    ``paced`` is true when only Crawl-delay pacing outlasts the read and a fresh page
    read budget covers the wait; a Retry-After backoff or a longer wait is not paced.
    """

    def __init__(self, url: str, *, ready_at: float, paced: bool = False) -> None:
        """Record when, on the client's clock, the host accepts another request."""
        super().__init__(f"{url} is held until the host's requested wait has passed")
        self.url = url
        self.ready_at = ready_at
        self.paced = paced


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Return the wait a Retry-After header states, in seconds, or None when unreadable."""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        stated = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if stated.tzinfo is None:
        stated = stated.replace(tzinfo=UTC)
    return max(0.0, (stated - (now or datetime.now(UTC))).total_seconds())


def _publishes_no_policy(status: int) -> bool:
    """Answer whether a robots response means the host states no crawling policy."""
    # RFC 9309 section 2.3.1.3 treats 400 through 499 as unavailable, permitting the
    # read. A 429 or a 5xx withholds the policy instead of saying none exists.
    return 400 <= status < 500 and status != httpx.codes.TOO_MANY_REQUESTS


def _send_jar(cookies: httpx.Cookies, method: str, url: str, headers: httpx.Headers) -> None:
    """Set the Cookie header to what the jar holds for this hop's address, and nothing else."""
    probe = httpx.Request(method, url)
    cookies.set_cookie_header(probe)
    headers.pop("Cookie", None)
    if "Cookie" in probe.headers:
        headers["Cookie"] = probe.headers["Cookie"]


@dataclass
class _PooledConnection:
    """One keep-alive client pinned to a host's address, and how it is in use."""

    client: httpx.Client
    busy: int = 0
    idle_since: float = 0.0
    answered: bool = False
    discarded: bool = False


class PoliteHttpClient:
    """Apply one outbound-request policy across all web fetch consumers."""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _host_locks: ClassVar[dict[str, threading.Lock]] = {}
    # Written under each host's lane lock, so separate clients space requests to it.
    _host_started_at: ClassVar[dict[str, float]] = {}

    def __init__(
        self,
        *,
        user_agent: str,
        sleeper: Callable[[float], None] | None = None,
        jitter: Callable[[float, float], float] | None = None,
        getter: _Get | None = None,
        poster: _Post | None = None,
        resolver: _Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
        robots_cache_ttl_seconds: float = ROBOTS_CACHE_TTL_SECONDS,
    ) -> None:
        """Configure the declared client identity and injectable network/timing effects."""
        if robots_cache_ttl_seconds <= 0:
            raise ValueError("robots cache TTL must be positive")
        self.user_agent = user_agent
        # RFC 9309 matches robots groups on the product token, never the UA comment.
        self._robots_agents = (user_agent.split("/", 1)[0].strip(), FORMER_ROBOTS_AGENT)
        self.sleeper = sleeper or time.sleep
        self.jitter = jitter or random.uniform
        self.getter = getter
        self.poster = poster
        self.resolver = resolver or _resolve_addresses
        self.clock = clock
        self.robots_cache_ttl_seconds = robots_cache_ttl_seconds
        self._robots: dict[str, tuple[Protego, float]] = {}
        # Pacing a host asked for (Crawl-delay, Retry-After) is held per client instance.
        self._host_ready_at: dict[str, float] = {}
        self._host_backoff_until: dict[str, float] = {}
        self._host_minimum_intervals: dict[str, float] = {}
        # Keyed by logical host as well as address: a connection carries its host's SNI.
        self._connections: dict[tuple[str, str], _PooledConnection] = {}
        self._connections_guard = threading.Lock()

    def close(self) -> None:
        """Close every pooled connection; a later request opens new ones."""
        with self._connections_guard:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.client.close()

    def register_host_minimum_interval(self, url: str, seconds: float) -> None:
        """Raise this client's interval floor for the URL's logical host."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("host minimum interval must be finite and nonnegative")
        host, _authority = self._destination(url)
        with self._locks_guard:
            self._host_minimum_intervals[host] = max(
                self._host_minimum_intervals.get(host, 0.0), seconds
            )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        follow_redirects: bool = True,
        max_bytes: int | None = None,
        max_elapsed_seconds: float | None = None,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        request_guard: Callable[[str], bool] | None = None,
        cookies: httpx.Cookies | None = None,
    ) -> httpx.Response:
        """GET one allowed URL after waiting inside its host lock.

        ``cookies`` is a caller-owned jar each redirect hop sends from and stores into.
        """
        return self._request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            intent=intent,
            request_guard=request_guard,
            max_bytes=max_bytes,
            max_elapsed_seconds=max_elapsed_seconds,
            cookies=cookies,
        )

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        follow_redirects: bool = True,
        max_bytes: int | None = None,
        max_elapsed_seconds: float | None = None,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        request_guard: Callable[[str], bool] | None = None,
        cookies: httpx.Cookies | None = None,
    ) -> httpx.Response:
        """POST JSON, or ``data`` as a form, to one allowed URL inside its host lock."""
        return self._request(
            "POST",
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            json=json,
            data=data,
            intent=intent,
            request_guard=request_guard,
            max_bytes=max_bytes,
            max_elapsed_seconds=max_elapsed_seconds,
            cookies=cookies,
        )

    def ensure_allowed(
        self,
        url: str,
        *,
        max_elapsed_seconds: float | None = None,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
    ) -> None:
        """Check a URL, its address, and its robots policy under ``intent`` without a request."""
        if max_elapsed_seconds is not None and max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive when provided.")
        deadline_at = (
            time.monotonic() + max_elapsed_seconds if max_elapsed_seconds is not None else None
        )
        host, authority = self._destination(url)
        with self._locked_host(host, "GET", url, deadline_at):
            addresses = self._public_addresses(url)
            self._robots_basis_locked(url, authority, addresses, intent, deadline_at=deadline_at)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: httpx.Timeout | float | None,
        follow_redirects: bool,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        max_elapsed_seconds: float | None = None,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        request_guard: Callable[[str], bool] | None = None,
        cookies: httpx.Cookies | None = None,
    ) -> httpx.Response:
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("max_bytes must be positive when provided.")
        if max_elapsed_seconds is not None and max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive when provided.")
        deadline_at = (
            time.monotonic() + max_elapsed_seconds if max_elapsed_seconds is not None else None
        )
        exempted = False

        def hop(hop_method: str, hop_url: str, **kwargs: Any) -> httpx.Response:
            # Every hop of a redirect chain carries the caller's intent, so a
            # single-posting read reaches the address its host redirects to. Each hop
            # still loads that host's own robots and refuses a group naming this client.
            nonlocal exempted
            response = self._request_once(hop_method, hop_url, intent=intent, **kwargs)
            basis = response.extensions.get(ROBOTS_BASIS_EXTENSION)
            if basis == RobotsReadBasis.SINGLE_POSTING_READ:
                exempted = True
            return response

        def attempt() -> httpx.Response:
            return self._follow(
                method,
                url,
                headers=httpx.Headers(headers),
                timeout=timeout,
                json=json,
                data=data,
                follow_redirects=follow_redirects,
                hop=hop,
                max_bytes=max_bytes,
                deadline_at=deadline_at,
                request_guard=request_guard,
                cookies=cookies,
            )

        response = attempt()
        if exempted:
            # The exempt hop's basis governs the whole request, whatever a later
            # hop or status answered, and it is never retried.
            response.extensions[ROBOTS_BASIS_EXTENSION] = RobotsReadBasis.SINGLE_POSTING_READ
            return response
        if response.status_code not in _RETRY_AFTER_STATUSES:
            return response
        wait = retry_after_seconds(response.headers.get("Retry-After"))
        if wait is None or wait > MAX_RETRY_AFTER_SECONDS:
            return response
        if deadline_at is not None and time.monotonic() + wait >= deadline_at:
            return response
        # The host lane already holds back for ``wait``; the retry sleeps inside it.
        try:
            return attempt()
        except RequestDeadlineExceeded as exc:
            exc.request_attempted = True
            raise

    def _follow(
        self,
        method: str,
        url: str,
        *,
        headers: httpx.Headers,
        timeout: httpx.Timeout | float | None,
        json: Any,
        follow_redirects: bool,
        hop: _Hop,
        max_bytes: int | None,
        deadline_at: float | None,
        request_guard: Callable[[str], bool] | None = None,
        data: Mapping[str, str] | None = None,
        cookies: httpx.Cookies | None = None,
    ) -> httpx.Response:
        current_method = method
        current_url = url
        current_headers = httpx.Headers(headers)
        current_json = json
        current_data = data

        attempted = False
        try:
            for redirect_count in range(MAX_REDIRECTS + 1):
                if request_guard is not None and not request_guard(current_url):
                    raise FetchPolicyError("Posting target is protected from this read.")
                self._raise_if_deadline_elapsed(current_method, current_url, deadline_at)
                if cookies is not None:
                    _send_jar(cookies, current_method, current_url, current_headers)
                response = hop(
                    current_method,
                    current_url,
                    headers=current_headers,
                    timeout=timeout,
                    json=current_json,
                    data=current_data,
                    max_bytes=max_bytes,
                    deadline_at=deadline_at,
                )
                attempted = True
                if cookies is not None:
                    # A host may set its session cookie on the redirect that tests for it.
                    cookies.extract_cookies(
                        httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            request=httpx.Request(current_method, current_url),
                        )
                    )
                if not follow_redirects or not response.has_redirect_location:
                    return response
                if redirect_count == MAX_REDIRECTS:
                    raise httpx.TooManyRedirects(
                        "Exceeded maximum allowed redirects.", request=response.request
                    )

                current_url = urljoin(current_url, response.headers["Location"])
                next_method = self._redirect_method(current_method, response.status_code)
                if next_method != current_method:
                    current_json = None
                    current_data = None
                    current_headers.pop("Content-Length", None)
                    current_headers.pop("Transfer-Encoding", None)
                current_method = next_method
                current_headers.pop("Authorization", None)
                current_headers.pop("Cookie", None)

            raise AssertionError("redirect loop did not return or raise")
        except RequestDeadlineExceeded as exc:
            exc.request_attempted |= attempted
            raise

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: httpx.Timeout | float | None,
        json: Any,
        data: Mapping[str, str] | None = None,
        max_bytes: int | None,
        deadline_at: float | None,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
    ) -> httpx.Response:
        host, authority = self._destination(url)
        # Assign the user agent so caller headers cannot override it.
        request_headers = httpx.Headers(headers)
        request_headers["User-Agent"] = self.user_agent
        with self._locked_host(host, method, url, deadline_at):
            addresses = self._public_addresses(url)
            try:
                parser, basis = self._robots_basis_locked(
                    url, authority, addresses, intent, deadline_at=deadline_at
                )
            except RobotsUnavailableError as exc:
                if isinstance(exc.__cause__, RequestDeadlineExceeded):
                    raise RequestDeadlineExceeded(method, url) from exc
                raise
            now = self.clock()
            held = max(0.0, self._host_ready_at.get(host, 0.0) - now)
            # The interval runs from the previous request's start on this host, so a
            # slow response has already spent some or all of it.
            interval = max(
                self.jitter(MIN_REQUEST_DELAY_SECONDS, MAX_REQUEST_DELAY_SECONDS),
                self._host_minimum_intervals.get(host, 0.0),
            )
            interval_wait = self._host_started_at.get(host, -math.inf) + interval - now
            delay = max(interval_wait, held)
            remaining = (
                MAX_UNBOUNDED_HOST_WAIT_SECONDS
                if deadline_at is None
                else max(0.0, deadline_at - time.monotonic())
            )
            if held > remaining:
                backoff = self._host_backoff_until.get(host, 0.0) - now
                paced = backoff <= remaining and held <= MAX_PAGE_READ_SECONDS
                raise HostHeldError(url, ready_at=now + held, paced=paced)
            delay = min(delay, remaining)
            if delay > 0:
                self.sleeper(delay)
            response = self._send(
                method,
                url,
                headers=request_headers,
                timeout=timeout,
                json=json,
                data=data,
                addresses=addresses,
                max_bytes=max_bytes,
                deadline_at=deadline_at,
            )
            self._hold_host(host, parser, response)
            response.extensions[ROBOTS_BASIS_EXTENSION] = basis
            return response

    def _hold_host(self, host: str, parser: Protego, response: httpx.Response) -> None:
        """Record the earliest next request a host's Crawl-delay or Retry-After allows."""
        crawl_delay = float(parser.crawl_delay(self._robots_agent(parser)) or 0.0)
        waits = [min(crawl_delay, MAX_HOST_BACKOFF_SECONDS)]
        if response.status_code in _RETRY_AFTER_STATUSES:
            stated = retry_after_seconds(response.headers.get("Retry-After"))
            if stated is not None:
                waits.append(min(stated, MAX_HOST_BACKOFF_SECONDS))
                backoff_until = self.clock() + waits[-1]
                self._host_backoff_until[host] = max(
                    self._host_backoff_until.get(host, 0.0), backoff_until
                )
        wait = max(waits)
        if wait > 0:
            ready_at = self.clock() + wait
            self._host_ready_at[host] = max(self._host_ready_at.get(host, 0.0), ready_at)

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: httpx.Timeout | float | None,
        json: Any,
        data: Mapping[str, str] | None = None,
        addresses: list[str] | None = None,
        max_bytes: int | None = None,
        deadline_at: float | None = None,
    ) -> httpx.Response:
        resolved = self._public_addresses(url) if addresses is None else addresses
        effective_timeout = self._timeout_for_deadline(method, url, timeout, deadline_at)
        dispatched = False
        try:
            if method == "GET" and self.getter is not None:
                PoliteHttpClient._host_started_at[self._destination(url)[0]] = self.clock()
                dispatched = True
                response = self.getter(
                    url,
                    timeout=effective_timeout,
                    headers=headers,
                    follow_redirects=False,
                )
                return self._limit_loaded_response(response, max_bytes, method=method, url=url)
            if method == "POST" and self.poster is not None:
                PoliteHttpClient._host_started_at[self._destination(url)[0]] = self.clock()
                dispatched = True
                # A form body goes to the injected poster only when there is one.
                form = {} if data is None else {"data": data}
                response = self.poster(
                    url,
                    json=json,
                    **form,
                    timeout=effective_timeout,
                    headers=headers,
                    follow_redirects=False,
                )
                return self._limit_loaded_response(response, max_bytes, method=method, url=url)
            return self._send_pinned(
                method,
                url,
                addresses=resolved,
                headers=headers,
                timeout=timeout,
                json=json,
                data=data,
                max_bytes=max_bytes,
                deadline_at=deadline_at,
            )
        except RequestDeadlineExceeded as exc:
            exc.request_attempted |= dispatched
            raise

    @staticmethod
    def _limit_loaded_response(
        response: httpx.Response,
        max_bytes: int | None,
        *,
        method: str,
        url: str,
    ) -> httpx.Response:
        """Apply the production response ceiling to injected transports too."""
        if max_bytes is None or len(response.content) <= max_bytes:
            return response
        headers = httpx.Headers(response.headers)
        headers.pop("Content-Encoding", None)
        headers.pop("Content-Length", None)
        headers.pop("Transfer-Encoding", None)
        try:
            request = response.request
        except RuntimeError:
            request = httpx.Request(method, url)
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=response.content[:max_bytes],
            request=request,
            extensions=response.extensions,
        )

    def _send_pinned(
        self,
        method: str,
        url: str,
        *,
        addresses: list[str],
        headers: Mapping[str, str],
        timeout: httpx.Timeout | float | None,
        json: Any,
        data: Mapping[str, str] | None,
        max_bytes: int | None,
        deadline_at: float | None,
    ) -> httpx.Response:
        """Try each resolved address, so one unroutable record is not a dead host."""
        host = httpx.URL(url).host
        # An address already pooled for this host goes first, so a rotating record keeps it.
        with self._connections_guard:
            pooled = [address for address in addresses if (host, address) in self._connections]
        *alternatives, final = pooled + [address for address in addresses if address not in pooled]
        attempted = False
        try:
            for address in alternatives:
                try:
                    return self._attempt(
                        method,
                        url,
                        address=address,
                        headers=headers,
                        timeout=timeout,
                        json=json,
                        data=data,
                        max_bytes=max_bytes,
                        deadline_at=deadline_at,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    attempted = True
                    continue
            return self._attempt(
                method,
                url,
                address=final,
                headers=headers,
                timeout=timeout,
                json=json,
                data=data,
                max_bytes=max_bytes,
                deadline_at=deadline_at,
            )
        except RequestDeadlineExceeded as exc:
            exc.request_attempted |= attempted
            raise

    def _lease(self, host: str, address: str) -> _PooledConnection:
        """Return the keep-alive client pinned to one host's address, closing idle ones."""
        now = self.clock()
        with self._connections_guard:
            idle = [
                key
                for key, pooled in self._connections.items()
                if pooled.busy == 0 and now - pooled.idle_since >= IDLE_CONNECTION_SECONDS
            ]
            expired = [self._connections.pop(key) for key in idle]
            connection = self._connections.get((host, address))
            if connection is None:
                # trust_env stays on, so a proxy or CA bundle set in the environment applies.
                connection = _PooledConnection(httpx.Client(follow_redirects=False))
                self._connections[(host, address)] = connection
            connection.busy += 1
        for pooled in expired:
            pooled.client.close()
        return connection

    def _release(self, connection: _PooledConnection) -> None:
        """Return a leased client to the pool, closing it once a discarded one is free."""
        with self._connections_guard:
            connection.busy -= 1
            connection.idle_since = self.clock()
            closing = connection.discarded and connection.busy == 0
        if closing:
            connection.client.close()

    def _discard(self, host: str, address: str, connection: _PooledConnection) -> None:
        """Drop a client whose socket failed, so the next lease opens a fresh one."""
        with self._connections_guard:
            connection.discarded = True
            if self._connections.get((host, address)) is connection:
                del self._connections[(host, address)]
            closing = connection.busy == 0
        if closing:
            connection.client.close()

    def _attempt(
        self,
        method: str,
        url: str,
        *,
        address: str,
        headers: Mapping[str, str],
        timeout: httpx.Timeout | float | None,
        json: Any,
        data: Mapping[str, str] | None,
        max_bytes: int | None,
        deadline_at: float | None,
    ) -> httpx.Response:
        logical_url = httpx.URL(url)
        host = logical_url.host
        request_headers = httpx.Headers(headers)
        request_headers["Host"] = logical_url.netloc.decode("ascii")
        effective_timeout = self._timeout_for_deadline(method, url, timeout, deadline_at)

        opened = False
        dispatched = False

        def read_on(connection: _PooledConnection) -> httpx.Response:
            nonlocal opened, dispatched
            try:
                request = connection.client.build_request(
                    method,
                    str(logical_url.copy_with(host=address)),
                    json=json,
                    data=data,
                    timeout=effective_timeout,
                    headers=request_headers,
                    extensions={"sni_hostname": host},
                )
                self._raise_if_deadline_elapsed(method, url, deadline_at)
                PoliteHttpClient._host_started_at[host] = self.clock()
                dispatched = True
                streamed = connection.client.send(request, stream=True)
                opened = connection.answered = True
                return self._read_streamed(
                    streamed, method=method, url=url, max_bytes=max_bytes, deadline_at=deadline_at
                )
            finally:
                self._release(connection)

        try:
            connection = self._lease(host, address)
            reused = connection.answered
            try:
                response = read_on(connection)
            except _STALE_CONNECTION_ERRORS:
                # Only a socket that answered before can have gone stale, and only before
                # this reply began; a fresh connection's failure is the host's answer.
                if not reused or opened:
                    raise
                self._discard(host, address, connection)
                response = read_on(self._lease(host, address))
            response.request.url = logical_url
            return response
        except RequestDeadlineExceeded as exc:
            exc.request_attempted |= dispatched
            raise

    def _read_streamed(
        self,
        streamed: httpx.Response,
        *,
        method: str,
        url: str,
        max_bytes: int | None,
        deadline_at: float | None,
    ) -> httpx.Response:
        """Read an opened response's body, stopping at the caller's byte ceiling."""
        if max_bytes is None:
            try:
                streamed.read()
            finally:
                streamed.close()
            return streamed
        content = bytearray()
        try:
            for chunk in streamed.iter_bytes():
                self._raise_if_deadline_elapsed(method, url, deadline_at)
                remaining = max_bytes - len(content)
                content.extend(chunk[:remaining])
                if len(content) == max_bytes:
                    break
        finally:
            streamed.close()
        response_headers = httpx.Headers(streamed.headers)
        response_headers.pop("Content-Encoding", None)
        response_headers.pop("Content-Length", None)
        response_headers.pop("Transfer-Encoding", None)
        return httpx.Response(
            streamed.status_code,
            headers=response_headers,
            content=bytes(content),
            request=streamed.request,
            extensions=streamed.extensions,
        )

    @staticmethod
    def _raise_if_deadline_elapsed(method: str, url: str, deadline_at: float | None) -> None:
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise RequestDeadlineExceeded(method, url)

    @staticmethod
    def _timeout_for_deadline(
        method: str,
        url: str,
        timeout: httpx.Timeout | float | None,
        deadline_at: float | None,
    ) -> httpx.Timeout | float | None:
        """Clamp every new network attempt to the logical operation's remaining time."""
        if deadline_at is None:
            return timeout
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            PoliteHttpClient._raise_if_deadline_elapsed(method, url, deadline_at)
        remaining = max(0.001, remaining)
        if isinstance(timeout, httpx.Timeout):

            def bounded(value: float | None) -> float:
                return remaining if value is None else min(value, remaining)

            return httpx.Timeout(
                connect=bounded(timeout.connect),
                read=bounded(timeout.read),
                write=bounded(timeout.write),
                pool=bounded(timeout.pool),
            )
        if timeout is None:
            return httpx.Timeout(remaining)
        return min(timeout, remaining)

    def _ensure_allowed_locked(
        self,
        url: str,
        authority: str,
        addresses: list[str],
        *,
        deadline_at: float | None = None,
    ) -> Protego:
        cached = self._robots.get(authority)
        if cached is None or self.clock() >= cached[1]:
            parser = self._load_robots(authority, addresses, deadline_at=deadline_at)
            self._robots[authority] = (
                parser,
                self.clock() + self.robots_cache_ttl_seconds,
            )
        else:
            parser = cached[0]
        if not parser.can_fetch(url, self._robots_agent(parser)):
            raise RobotsDeniedError(url)
        return parser

    def _robots_basis_locked(
        self,
        url: str,
        authority: str,
        addresses: list[str],
        intent: PostingReadIntent,
        *,
        deadline_at: float | None,
    ) -> tuple[Protego, RobotsReadBasis]:
        """Return the host's policy and why it lets this read proceed, or raise the refusal."""
        try:
            parser = self._ensure_allowed_locked(url, authority, addresses, deadline_at=deadline_at)
        except RobotsDeniedError:
            if intent != PostingReadIntent.SINGLE_POSTING:
                raise
            # _ensure_allowed_locked caches the parser before raising, so it is here.
            parser = self._robots[authority][0]
            # A group written for this client is a statement about it, not about crawlers.
            # Protego exposes the group it applies only through this private method.
            group = parser._get_matching_rule_set(self._robots_agent(parser))
            if group is not None and group.user_agent != "*":
                raise
            return parser, RobotsReadBasis.SINGLE_POSTING_READ
        return parser, RobotsReadBasis.ALLOWED

    def _robots_agent(self, parser: Protego) -> str:
        """Return the token whose own robots.txt group applies, else the current name."""
        for agent in self._robots_agents:
            group = parser._get_matching_rule_set(agent)
            if group is not None and group.user_agent != "*":
                return agent
        return self._robots_agents[0]

    def _load_robots(
        self, authority: str, addresses: list[str], *, deadline_at: float | None = None
    ) -> Protego:
        robots_url = f"{authority}/robots.txt"

        def hop(method: str, hop_url: str, **kwargs: Any) -> httpx.Response:
            # Reuse the resolved address only for the first host.
            return self._send(
                method, hop_url, **kwargs, addresses=addresses if hop_url == robots_url else None
            )

        try:
            # Follow ordinary robots redirects before classifying the policy.
            response = self._follow(
                "GET",
                robots_url,
                timeout=httpx.Timeout(20.0),
                headers=httpx.Headers({"User-Agent": self.user_agent, "Accept": "text/plain"}),
                json=None,
                follow_redirects=True,
                hop=hop,
                max_bytes=MAX_ROBOTS_BYTES + 1,
                deadline_at=deadline_at,
            )
        except httpx.HTTPError as exc:
            raise RobotsUnavailableError(
                f"Could not verify robots policy for {authority}: {exc}"
            ) from exc
        if response.is_success and len(response.content) > MAX_ROBOTS_BYTES:
            raise RobotsUnavailableError(
                f"Could not verify robots policy for {authority}: response exceeded limit"
            )
        if response.is_success:
            # A leading byte-order mark would otherwise hide the first group from the parser.
            return Protego.parse(response.text.removeprefix("\ufeff"))
        if _publishes_no_policy(response.status_code):
            return Protego.parse("")
        raise RobotsUnavailableError(
            f"Could not verify robots policy for {authority}: HTTP {response.status_code}"
        )

    def _public_addresses(self, url: str) -> list[str]:
        host, _ = self._destination(url)
        parsed = urlsplit(url)
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = self.resolver(host, port)
        except (OSError, ValueError) as exc:
            raise DestinationUnresolvableError(
                "The destination could not be resolved to a public network address."
            ) from exc

        resolved: list[str] = []
        for result in addresses:
            sockaddr = result[4]
            address = str(sockaddr[0]).split("%", 1)[0]
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError:
                continue
            # Ignore unsafe alternate addresses when another public address remains.
            if parsed_address.is_global:
                resolved.append(address)
        if not resolved:
            raise DestinationUnresolvableError(
                "Fetching requires a destination on a public network address."
            )
        return resolved

    @staticmethod
    def _redirect_method(method: str, status_code: int) -> str:
        if status_code == httpx.codes.SEE_OTHER and method != "HEAD":
            return "GET"
        if status_code == httpx.codes.FOUND and method != "HEAD":
            return "GET"
        if status_code == httpx.codes.MOVED_PERMANENTLY and method == "POST":
            return "GET"
        return method

    @classmethod
    def _host_lock(cls, host: str) -> threading.Lock:
        with cls._locks_guard:
            return cls._host_locks.setdefault(host, threading.Lock())

    @classmethod
    @contextmanager
    def _locked_host(
        cls,
        host: str,
        method: str,
        url: str,
        deadline_at: float | None,
    ) -> Iterator[None]:
        """Acquire a host lane without waiting beyond the caller's deadline."""
        lock = cls._host_lock(host)
        if deadline_at is None:
            lock.acquire()
            acquired = True
        else:
            remaining = max(0.0, deadline_at - time.monotonic())
            acquired = lock.acquire(timeout=remaining)
        if not acquired:
            cls._raise_if_deadline_elapsed(method, url, deadline_at)
            raise RequestDeadlineExceeded(method, url)
        try:
            yield
        finally:
            lock.release()

    @staticmethod
    def _destination(url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidFetchUrlError("Fetching requires an HTTP(S) URL with a hostname.")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidFetchUrlError("Fetching does not accept credentials in a URL.")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise InvalidFetchUrlError("Fetching requires a valid network port.") from exc
        return parsed.hostname.casefold(), f"{parsed.scheme}://{parsed.netloc}"


def fetch_refusal(url: str) -> str:
    """Return why an address may not be fetched, or an empty string.

    A registered source is a list a person curated; an address a model wrote or
    somebody pasted can name anything, including the machine the worker runs on.
    The scheme and the host are decided before any request is made, and a bare
    address rather than a hostname is refused outright, because a board is a name.
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return "unreadable_url"
    if parsed.scheme not in {"http", "https"} or not host:
        return "not_an_http_url"
    if parsed.username or parsed.password:
        return "credentials_in_url"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "not_a_hostname"
    if host == "localhost" or host.rpartition(".")[2] in {"localhost", "local", "internal"}:
        return "local_hostname"
    return ""
