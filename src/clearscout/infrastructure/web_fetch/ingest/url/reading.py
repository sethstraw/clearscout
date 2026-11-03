"""Read a posting URL into posting text."""

from __future__ import annotations

import time
from collections.abc import Callable

from clearscout.application.discovery.posting_text import closure_signal
from clearscout.application.discovery.source_reading import PostingCheck
from clearscout.domain.records import PageClassification, PostingReadIntent
from clearscout.infrastructure.web_fetch.boards import (
    POSTING_API_ORDER,
)
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.ingest.text import (
    MINIMUM_USEFUL_CHARACTERS,
    require_enough_text,
)
from clearscout.infrastructure.web_fetch.ingest.url.addresses import (
    landed_on_its_own_listing,
    left_the_posting,
)
from clearscout.infrastructure.web_fetch.ingest.url.checks import page_address, posting_page
from clearscout.infrastructure.web_fetch.ingest.url.page_text import (
    page_source,
    with_url_slug_title,
)
from clearscout.infrastructure.web_fetch.ingest.url.platform_readers import (
    API_ONLY_PLATFORMS,
    check_workday,
    posting_readers,
)
from clearscout.infrastructure.web_fetch.ingest.url.results import (
    Answer,
    PublisherPostingUnverifiedError,
    PublisherReadRefusedError,
    UrlPostingText,
)
from clearscout.infrastructure.web_fetch.ingest.url.transport import (
    READ_DEADLINE_AT,
    client_for_call,
    guarded_posting_reads,
)
from clearscout.infrastructure.web_fetch.platforms.greenhouse import (
    GREENHOUSE_HOSTED_JID,
)
from clearscout.infrastructure.web_fetch.policy import (
    PoliteHttpClient,
    RobotsDeniedError,
    read_basis,
)
from clearscout.shared.errors import IngestError


def workday_posting_detail(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    request_guard: Callable[[str], bool] | None = None,
) -> PostingCheck:
    """Read structured Workday detail for bounded board-scan enrichment."""
    with guarded_posting_reads(request_guard), client_for_call(client) as http:
        checked = check_workday(url, client=http)
    if checked is not None:
        return checked
    return PostingCheck(
        status="unsupported",
        note="This address is not a Workday posting link.",
    )


def from_url(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    max_elapsed_seconds: float | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> UrlPostingText:
    """Return job description text for a posting URL.

    ``board_url`` is the board that listed the posting, read for a token its page omits.
    """
    if max_elapsed_seconds is not None and max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be positive when provided.")
    deadline_at = (
        time.monotonic() + max_elapsed_seconds if max_elapsed_seconds is not None else None
    )
    token = READ_DEADLINE_AT.set(deadline_at)
    try:
        with client_for_call(client) as http:
            return _from_url(url, http=http, intent=intent, board_url=board_url)
    finally:
        READ_DEADLINE_AT.reset(token)


def _from_url(
    url: str,
    *,
    http: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> UrlPostingText:
    """Run the publisher readers within the caller's shared deadline."""
    publisher, declined, refused = _publisher_posting(
        url, client=http, intent=intent, board_url=board_url
    )
    if publisher is not None:
        return publisher
    try:
        page = _rendered_posting(url, client=http, declined=declined, intent=intent)
    except IngestError as error:
        # A closure the page itself signals outranks the API's refusal, and a page
        # robots.txt disallows is a policy refusal rather than the publisher's.
        if (
            refused is None
            or closure_signal(str(error))
            or isinstance(error.__cause__, RobotsDeniedError)
        ):
            raise
        raise PublisherReadRefusedError(refused.note, refused.http_status) from error
    # A refusing API-only site serves the shell its API fills, whose chrome passes for text.
    if (
        refused is not None
        and page.page_classification is not PageClassification.POSTING
        and not page.publisher_markup_present
    ):
        raise PublisherReadRefusedError(refused.note, refused.http_status)
    return page


def _publisher_posting(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> tuple[UrlPostingText | None, list[str], PostingCheck | None]:
    """Read publisher metadata before falling back to a rendered posting page."""
    readers = posting_readers(board_url)
    declined: list[str] = []
    refused: PostingCheck | None = None
    # A publisher with no board list reads after every board, in the order its endpoint is listed.
    boardless = [platform for platform in readers if platform not in POSTING_API_ORDER]
    for platform in (*POSTING_API_ORDER, *boardless):
        reader, method = readers[platform]
        answer = reader(url, client=client, intent=intent)
        if posting := _publisher_posting_text(answer, method=method, url=url):
            return posting, declined, None
        if answer.note:
            declined.append(f"  {method} declined: {answer.note}")
        check = answer.publisher_check
        if check is not None and check.status == "unreadable" and platform in API_ONLY_PLATFORMS:
            refused = check
    return None, declined, refused


def _publisher_posting_text(answer: Answer, *, method: str, url: str) -> UrlPostingText | None:
    """Return usable publisher text, preserving closure and unverified signals."""
    check = answer.publisher_check
    if check is not None and check.status == "closed":
        raise IngestError(answer.note or check.note)
    if check is not None and check.status == "unreadable" and GREENHOUSE_HOSTED_JID.search(url):
        raise PublisherPostingUnverifiedError(
            "Greenhouse could not verify this hosted posting because its board identifier "
            "was no longer available."
        )
    if answer.text is None or len(answer.text.strip()) < MINIMUM_USEFUL_CHARACTERS:
        return None
    return UrlPostingText(
        text=answer.text,
        origin=url,
        method=method,
        title=answer.title,
        employer=answer.employer,
        publisher_check=check,
        robots_basis=check.robots_basis if check is not None else None,
        source=check.source if check is not None else None,
    )


def _rendered_posting(
    url: str,
    *,
    client: PoliteHttpClient,
    declined: list[str],
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> UrlPostingText:
    """Read the canonical page only after no publisher adapter supplied a posting."""
    read_at = page_address(url)
    try:
        _address, response, page = posting_page(
            read_at, client=client, declined=declined, intent=intent
        )
    except IngestError as error:
        if read_at == url:
            raise
        if isinstance(error.__cause__, RobotsDeniedError):
            # A policy refusal states which page it refused, so it keeps that address.
            raise IngestError(f"{error} (the body of {url})") from error.__cause__
        # The derived address is the reader's, so the message names the one the person gave.
        raise IngestError(str(error).replace(read_at, url)) from error.__cause__
    landed = str(response.url)
    evidence = inspect_publisher_page(landed, response.text)
    if landed_on_its_own_listing(read_at, landed, page, evidence.classification):
        raise IngestError(left_the_posting(url, landed, declined))
    require_enough_text(page.text, url)
    text = with_url_slug_title(page.text, url)
    robots_basis = read_basis(response)
    return UrlPostingText(
        text=text,
        origin=url if read_at != url and landed == read_at else landed,
        method=page.method,
        valid_through=page.valid_through,
        publisher_markup_present=page.publisher_markup_present,
        title=page.title or evidence.title,
        employer=page.employer,
        publisher_check=page.publisher_check,
        robots_basis=robots_basis,
        page_classification=evidence.classification,
        source=page_source(page, response.text),
    )
