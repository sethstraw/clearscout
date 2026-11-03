"""Whether a posting URL still names an open posting."""

from __future__ import annotations

import time
from dataclasses import replace

import httpx

from tailorcv.application.discovery.source_reading import PostingCheck
from tailorcv.domain.identity import (
    normalize_url,
)
from tailorcv.domain.records import PostingReadIntent
from tailorcv.infrastructure.web_fetch.board_mappings import (
    icims_job_page,
)
from tailorcv.infrastructure.web_fetch.board_platform import host_match
from tailorcv.infrastructure.web_fetch.ingest.url.addresses import (
    canonical_posting_address,
    left_the_posting,
    names_the_posting,
    redirected_off_the_posting,
)
from tailorcv.infrastructure.web_fetch.ingest.url.page_text import PageText, read_page_text
from tailorcv.infrastructure.web_fetch.ingest.url.platform_readers import (
    POST_ENDPOINTS,
    check_ashby,
    check_dayforce,
    check_greenhouse,
    check_lever,
    check_oracle,
    check_post_endpoint,
    check_smartrecruiters,
    check_workday,
)
from tailorcv.infrastructure.web_fetch.ingest.url.transport import (
    MAX_POSTING_READ_SECONDS,
    READ_DEADLINE_AT,
    client_for_call,
    fetch_posting,
)
from tailorcv.infrastructure.web_fetch.platforms.icims import ICIMS_JOB
from tailorcv.infrastructure.web_fetch.policy import (
    PoliteHttpClient,
    RobotsDeniedError,
    read_basis,
)
from tailorcv.shared.errors import IngestError


def check_posting(
    url: str,
    *,
    client: PoliteHttpClient | None = None,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck:
    """Read supported publisher metadata for a deliberate liveness check.

    The manual-add preflight and the pending-discovery refresh both call this and
    both treat a canonical-endpoint 404 as an authoritative closure, unlike
    board-scan enrichment, which tolerates a just-listed job's transient 404.
    Only the manual-add preflight passes ``SINGLE_POSTING``; a refresh reads as a crawl.
    """
    token = READ_DEADLINE_AT.set(time.monotonic() + MAX_POSTING_READ_SECONDS)
    try:
        with client_for_call(client) as http:
            return _check_posting(url, http=http, intent=intent)
    finally:
        READ_DEADLINE_AT.reset(token)


def _check_posting(url: str, *, http: PoliteHttpClient, intent: PostingReadIntent) -> PostingCheck:
    """Run the liveness readers within the check's shared deadline."""
    checked = (
        check_greenhouse(url, client=http, intent=intent)
        or check_lever(url, client=http, intent=intent)
        or check_workday(url, client=http, gone_is_closure=True, intent=intent)
        or check_ashby(url, client=http, intent=intent)
        or check_oracle(url, client=http, intent=intent)
        or check_smartrecruiters(url, client=http, intent=intent)
        or check_dayforce(url, client=http, intent=intent)
        or next(
            (
                found
                for endpoint in POST_ENDPOINTS
                if (found := check_post_endpoint(endpoint, url, client=http, intent=intent))
            ),
            None,
        )
    )
    if checked is not None and checked.status in ("readable", "closed"):
        return checked
    declared = _declared_page_check(url, client=http, intent=intent)
    if declared is not None:
        return declared
    if checked is not None:
        return checked
    return PostingCheck(
        status="unsupported",
        note="Free metadata checking does not support this publisher link.",
    )


def _declared_page_check(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> PostingCheck | None:
    """Return what the posting page declares about itself, or None when it declares nothing.

    The page is read under the same fetch policy as a full ingest, so a host that
    refuses a crawl leaves the publisher API's own answer standing.
    """
    read_at = page_address(url)
    try:
        address, response, page = posting_page(read_at, client=client, declined=[], intent=intent)
    except IngestError:
        return None
    landed = str(response.url)
    check = _declared_posting(page, address, landed)
    # A body read at its own derived address also answers for the address it came from.
    if (
        check is None
        and read_at != url
        and normalize_url(address) == normalize_url(read_at) == normalize_url(landed)
    ):
        check = _declared_posting(page, url, url)
    if check is None:
        return None
    return replace(check, robots_basis=read_basis(response))


def _declared_posting(page: PageText, url: str, landed: str) -> PostingCheck | None:
    """Return the readable check a page's lone declared block states for this posting."""
    # An index page states other employers' jobs with the same publisher authority,
    # so only a lone block naming this posting may answer for it.
    if page.declared is None or page.declared_count != 1:
        return None
    if not names_the_posting(page.declared, url, landed):
        return None
    check = page.publisher_check
    return check if check is not None and check.status == "readable" else None


def posting_page(
    url: str,
    *,
    client: PoliteHttpClient,
    declined: list[str],
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[str, httpx.Response, PageText]:
    """Read a posting page, or the canonical page it names when only that one declares it.

    Returns the address the page answers for beside the response and its text.
    """
    response = _posting_page_response(url, client=client, declined=declined, intent=intent)
    landed = str(response.url)
    page = read_page_text(response.text, landed)
    if _declared_posting(page, url, landed) is not None:
        return url, response, page
    canonical = canonical_posting_address(url, landed, response.text)
    if not canonical:
        return url, response, page
    try:
        followed = _posting_page_response(
            canonical, client=client, declined=declined, intent=intent
        )
    except IngestError:
        return url, response, page
    followed_page = read_page_text(followed.text, str(followed.url))
    if _declared_posting(followed_page, canonical, str(followed.url)) is None:
        return url, response, page
    return canonical, followed, followed_page


def page_address(url: str) -> str:
    """Return the address whose page holds this posting's body, which is usually the URL itself."""
    return (icims_job_page(url) if host_match(ICIMS_JOB, url) else "") or url


def _posting_page_response(
    url: str,
    *,
    client: PoliteHttpClient,
    declined: list[str],
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> httpx.Response:
    """GET a posting page, refusing an answer that landed somewhere that is not the posting."""
    try:
        response = fetch_posting(url, client=client, intent=intent)
    except RobotsDeniedError as denial:
        # Preserve the closed-posting reason when an index redirect is refused.
        if redirected_off_the_posting(url, denial.url):
            raise IngestError(left_the_posting(url, denial.url, declined)) from denial
        raise IngestError(str(denial)) from denial
    landed = str(response.url)
    if redirected_off_the_posting(url, landed):
        raise IngestError(left_the_posting(url, landed, declined))
    return response
