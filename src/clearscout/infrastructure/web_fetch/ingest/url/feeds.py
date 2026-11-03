"""JSON feeds and site records a posting read shares within one call."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.infrastructure.web_fetch.ingest.url.results import Answer
from clearscout.infrastructure.web_fetch.ingest.url.transport import (
    JSON_HEADERS,
    MAX_POSTING_RESPONSE_BYTES,
    PAGE_HEADERS,
    http_get,
)
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    PoliteHttpClient,
    read_basis,
)

# Reads of a whole-site posting feed, shared only inside an open pass. A feed
# held any longer would keep answering for a posting the publisher has withdrawn.
_FeedRead = tuple[list[Any] | dict[str, Any] | None, str, RobotsReadBasis | None]


_FEED_READS: ContextVar[dict[tuple[str, PostingReadIntent], _FeedRead] | None] = ContextVar(
    "clearscout_posting_feed_reads", default=None
)


# The CXS site address each Workday career page stated in an open pass, empty when it
# stated none, keyed by host and site.
WORKDAY_STATED_SITES: ContextVar[dict[str, str] | None] = ContextVar(
    "clearscout_workday_stated_sites", default=None
)


@contextmanager
def shared_posting_feeds() -> Iterator[None]:
    """Read each whole-site feed and Workday career page once for every posting in this block."""
    token = _FEED_READS.set({})
    sites = WORKDAY_STATED_SITES.set({})
    try:
        yield
    finally:
        WORKDAY_STATED_SITES.reset(sites)
        _FEED_READS.reset(token)


def _answered(
    url: str,
    headers: dict[str, str],
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[httpx.Response | None, str, RobotsReadBasis | None]:
    """GET once, returning an OK response, why there is none, and the read's basis.

    The reason is returned rather than swallowed because a silent ``None`` makes
    a refused API look identical to a board that has no API, and the difference
    decides whether retrying or screenshotting is the right advice.
    """
    try:
        response = http_get(url, headers, client=client, intent=intent)
    except (httpx.HTTPError, FetchPolicyError) as error:
        return None, f"could not be reached ({type(error).__name__})", None
    basis = read_basis(response)
    if response.status_code != httpx.codes.OK:
        return None, f"returned HTTP {response.status_code}", basis
    return response, "", basis


def page_payload(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[str | None, str, RobotsReadBasis | None]:
    """GET a posting page whose markup embeds the publisher's own record."""
    response, note, basis = _answered(url, PAGE_HEADERS, client=client, intent=intent)
    return (None if response is None else response.text), note, basis


def _json_payload(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[Any, str, RobotsReadBasis | None]:
    """GET a JSON endpoint, returning the parsed body, why there is none, and the read's basis."""
    response, note, basis = _answered(url, JSON_HEADERS, client=client, intent=intent)
    if response is None:
        return None, note, basis
    try:
        return response.json(), "", basis
    except ValueError:
        # The client truncates an over-cap body at exactly the ceiling rather than
        # raising, so a body that long parses as broken JSON.
        if len(response.content) >= MAX_POSTING_RESPONSE_BYTES:
            return None, "returned a response that exceeded the size limit", basis
        return None, "returned something that was not JSON", basis


def read_json(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[dict[str, Any] | None, str, RobotsReadBasis | None]:
    """GET a JSON endpoint whose posting answer is one record."""
    payload, note, basis = _json_payload(url, client=client, intent=intent)
    if note:
        return None, note, basis
    if not isinstance(payload, dict):
        return None, "returned JSON that was not a record", basis
    return payload, "", basis


def _json_feed(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[list[Any] | None, str, RobotsReadBasis | None]:
    """GET a JSON endpoint whose posting answer is a bare list."""
    payload, note, basis = _json_payload(url, client=client, intent=intent)
    if note:
        return None, note, basis
    if not isinstance(payload, list):
        return None, "returned JSON that was not a list of postings", basis
    return payload, "", basis


def site_feed(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> tuple[list[Any] | None, str, RobotsReadBasis | None]:
    """Read a whole-site posting feed, reusing this pass's read of it when one is open.

    A read is reused only for the intent it ran at, so a crawl never answers from
    a body the single-posting exemption obtained.
    """
    reads = _FEED_READS.get()
    if reads is None:
        return _json_feed(url, client=client, intent=intent)
    key = (url, intent)
    if key not in reads:
        reads[key] = _json_feed(url, client=client, intent=intent)
    payload, note, basis = reads[key]
    if payload is None or isinstance(payload, list):
        return payload, note, basis
    return None, "returned JSON that was not a list of postings", basis


def site_record_feed(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> tuple[dict[str, Any] | None, str, RobotsReadBasis | None]:
    """Reuse a whole-site feed whose JSON root is a record."""
    reads = _FEED_READS.get()
    if reads is None:
        return read_json(url, client=client, intent=intent)
    key = (url, intent)
    if key not in reads:
        reads[key] = read_json(url, client=client, intent=intent)
    payload, note, basis = reads[key]
    if payload is None or isinstance(payload, dict):
        return payload, note, basis
    return None, "returned JSON that was not a record", basis


# A single-posting endpoint answers a gone requisition with 404 or 410, both
# authoritative (these are `read_json`'s notes for those statuses); 401/403 (auth) and
# 5xx/timeout (transient) are not, so they stay unreadable, not withdrawn.
GONE_ENDPOINT_NOTES = frozenset({"returned HTTP 404", "returned HTTP 410"})


def checked_answer(checked: PostingCheck | None, publisher: str) -> Answer:
    """Wrap one publisher check, keeping its reason only when it gave no posting."""
    if checked is None:
        return Answer.absent()
    note = "" if checked.status == "readable" else checked.note.removeprefix(f"{publisher} ")
    return Answer(checked.text or None, note, checked.title, checked.employer, checked)
