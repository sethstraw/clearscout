"""Per-platform posting readers and checks."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

import httpx

from tailorcv.application.discovery.source_reading import PostingCheck
from tailorcv.domain.records import PostingReadIntent, RobotsReadBasis
from tailorcv.infrastructure.web_fetch.board_mappings import (
    ADP_MAPPING,
    ASHBY_MAPPING,
    BAMBOOHR_MAPPING,
    DAYFORCE_MAPPING,
    DOVER_MAPPING,
    GEM_MAPPING,
    GREENHOUSE_MAPPING,
    LEVER_MAPPING,
    ORACLE_DETAIL_MAPPING,
    RIPPLING_MAPPING,
    SMARTRECRUITERS_MAPPING,
    TELUS_MAPPING,
    TELUS_POST,
    UKG_MAPPING,
    UKG_OPPORTUNITY,
    WORKABLE_MAPPING,
    WORKDAY_DETAIL_MAPPING,
    BoardDeclaration,
    Posting,
    adp_requisition,
    adp_requisition_url,
    bamboohr_detail_url,
    bamboohr_opening,
    bamboohr_withdrawn,
    dayforce_feed_url,
    dover_detail_url,
    dover_job,
    gem_post_url,
    posting_check,
    rippling_detail_url,
    rippling_job,
    telus_post_url,
    telus_withdrawn,
    ukg_opportunity,
    ukg_opportunity_url,
    ukg_withdrawn,
)
from tailorcv.infrastructure.web_fetch.board_platform import host_match
from tailorcv.infrastructure.web_fetch.ingest.url.addresses import LISTING_WORDS
from tailorcv.infrastructure.web_fetch.ingest.url.feeds import (
    GONE_ENDPOINT_NOTES,
    WORKDAY_STATED_SITES,
    checked_answer,
    page_payload,
    read_json,
    site_feed,
    site_record_feed,
)
from tailorcv.infrastructure.web_fetch.ingest.url.results import Answer
from tailorcv.infrastructure.web_fetch.ingest.url.transport import (
    JSON_HEADERS,
    MAX_POSTING_RESPONSE_BYTES,
    PAGE_HEADERS,
    POSTING_REQUEST_GUARD,
    TIMEOUT,
    http_get,
    logger,
    read_deadline_at,
    time_left,
)
from tailorcv.infrastructure.web_fetch.platforms.adp import ADP_JOB
from tailorcv.infrastructure.web_fetch.platforms.ashby import (
    ASHBY,
    ASHBY_FORM_API,
    ashby_form,
    ashby_form_request,
)
from tailorcv.infrastructure.web_fetch.platforms.bamboohr import BAMBOOHR_JOB
from tailorcv.infrastructure.web_fetch.platforms.dayforce import DAYFORCE_JOB, dayforce_posting_id
from tailorcv.infrastructure.web_fetch.platforms.dover import DOVER_JOB
from tailorcv.infrastructure.web_fetch.platforms.gem import GEM_JOB
from tailorcv.infrastructure.web_fetch.platforms.greenhouse import (
    GREENHOUSE,
    GREENHOUSE_BOARD,
    GREENHOUSE_BOARD_EMBED,
    GREENHOUSE_BOARD_TOKEN,
    GREENHOUSE_EMBED,
    GREENHOUSE_HOSTED_JID,
)
from tailorcv.infrastructure.web_fetch.platforms.lever import (
    LEVER,
    lever_apply_url,
    lever_form_cards,
)
from tailorcv.infrastructure.web_fetch.platforms.oracle import ORACLE
from tailorcv.infrastructure.web_fetch.platforms.rippling import RIPPLING_JOB
from tailorcv.infrastructure.web_fetch.platforms.smartrecruiters import (
    SMARTRECRUITERS_JOB,
    SMARTRECRUITERS_POSTING_REF,
)
from tailorcv.infrastructure.web_fetch.platforms.workable import WORKABLE_BOARD
from tailorcv.infrastructure.web_fetch.platforms.workday import (
    WORKDAY,
    workday_posting_path,
    workday_site_root,
    workday_stated_tenant,
)
from tailorcv.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    PoliteHttpClient,
    RobotsDeniedError,
    read_basis,
)
from tailorcv.shared.errors import IngestError

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1"


# These career sites render client-side from the same API, so once the static page
# also yields nothing, a browser render can only reproduce the refused API read.
API_ONLY_PLATFORMS = frozenset(
    {"workday", "oracle", "dayforce", "bamboohr", "gem", "telus", "workable"}
)


_WORKABLE_SHORT_JOB = re.compile(r"apply\.workable\.com/j/(?P<job>[A-Za-z0-9]+)(?:/|$)")


def posting_readers(board_url: str = "") -> dict[str, tuple[Callable[..., Answer], str]]:
    """Return each publisher's single-posting reader and the method name it reports."""
    return {
        "greenhouse": (partial(_greenhouse, board_url=board_url), "Greenhouse API"),
        "lever": (_lever, "Lever API"),
        "workday": (_workday, "Workday API"),
        "ashby": (_ashby, "Ashby API"),
        "oracle": (_oracle, "Oracle API"),
        "smartrecruiters": (_smartrecruiters, "SmartRecruiters API"),
        "dayforce": (_dayforce, "Dayforce API"),
        "workable": (partial(_workable, board_url=board_url), "Workable API"),
        **{
            endpoint.platform: (
                partial(_read_post_endpoint, endpoint),
                f"{endpoint.publisher} {endpoint.read_as}",
            )
            for endpoint in POST_ENDPOINTS
        },
    }


def _greenhouse(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> Answer:
    """Read a Greenhouse posting from its public board API.

    A 404 from the single-job endpoint is an uncaught-up edge, not a closure.
    """
    checked = check_greenhouse(url, client=client, intent=intent, board_url=board_url)
    return checked_answer(checked, "Greenhouse")


def check_greenhouse(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> PostingCheck | None:
    """Check whether a Greenhouse posting URL still names an open posting."""
    identity = _greenhouse_identity(url, client=client, intent=intent, board_url=board_url)
    if identity is None or isinstance(identity, PostingCheck):
        return identity
    org, job = identity
    payload, unresolved, basis = _greenhouse_payload(org, job, client=client, intent=intent)
    if unresolved is not None:
        return unresolved
    if payload is None:
        return PostingCheck(
            status="unreadable",
            note="Greenhouse did not return posting metadata.",
            platform="greenhouse",
            robots_basis=basis,
        )
    return _greenhouse_check(payload, basis)


def _greenhouse_check(payload: dict[str, Any], basis: RobotsReadBasis | None) -> PostingCheck:
    """Map one Greenhouse job record into publisher-stated metadata."""
    return replace(
        posting_check(
            GREENHOUSE_MAPPING, Posting(payload), note="Greenhouse published this posting metadata."
        ),
        robots_basis=basis,
    )


def _greenhouse_identity(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> tuple[str, str] | PostingCheck | None:
    match = host_match(GREENHOUSE, url) or host_match(GREENHOUSE_EMBED, url)
    if match is None:
        hosted = _greenhouse_hosted_from_page(
            url, client=client, intent=intent, board_url=board_url
        )
        if isinstance(hosted, PostingCheck):
            return hosted
        if hosted is None:
            return None
        org, job = hosted
    else:
        org, job = match.group("org"), match.group("job")
    return org, job


def _greenhouse_payload(
    org: str,
    job: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[dict[str, Any] | None, PostingCheck | None, RobotsReadBasis | None]:
    # `questions=true` adds the application form, which the residence screen reads.
    payload, note, basis = read_json(
        f"{GREENHOUSE_API}/boards/{org}/jobs/{job}?questions=true", client=client, intent=intent
    )
    if payload is not None:
        return payload, None, basis
    payload, listing_note, delisted, listing_basis = _from_greenhouse_listing(
        org, job, client=client, intent=intent
    )
    basis = basis or listing_basis
    if payload is not None:
        return payload, None, basis
    combined = f"Greenhouse single posting {note}, and the board listing {listing_note}."
    # The single-job endpoint can 404 transiently across edges, so only a complete
    # board listing omitting the job proves a delisting.
    status: Literal["closed", "unreadable"] = "closed" if delisted else "unreadable"
    return (
        None,
        PostingCheck(status=status, note=combined, platform="greenhouse", robots_basis=basis),
        basis,
    )


def _greenhouse_hosted_from_page(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> tuple[str, str] | PostingCheck | None:
    jid = GREENHOUSE_HOSTED_JID.search(url)
    if jid is None:
        return None
    job = jid.group("job")
    board = _greenhouse_hosted_board(url, client=client, intent=intent)
    if isinstance(board, str):
        return board, job
    # The board that listed this posting names its token when the page markup does not.
    if listed := host_match(GREENHOUSE_BOARD, board_url):
        return listed.group("org"), job
    return _greenhouse_guessed_board(url, job, client=client, intent=intent) or board


def _greenhouse_hosted_board(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> str | PostingCheck:
    """Return the board token a hosted careers page's markup names, or why it names none."""
    # Embedded Greenhouse pages expose the board token only in page markup;
    # their server-rendered body otherwise contains site chrome, not the posting.
    try:
        response = http_get(url, PAGE_HEADERS, client=client, intent=intent)
    except (httpx.HTTPError, FetchPolicyError, RobotsDeniedError) as error:
        logger.debug("Greenhouse hosted careers page could not be read: %s", url, exc_info=error)
        return PostingCheck(
            status="unreadable",
            note=f"Greenhouse hosted careers page could not be read: {error}",
            platform="greenhouse",
        )
    if response.status_code != httpx.codes.OK:
        logger.debug(
            "Greenhouse hosted careers page returned HTTP %s: %s", response.status_code, url
        )
        return PostingCheck(
            status="unreadable",
            note=f"Greenhouse hosted careers page returned HTTP {response.status_code}",
            platform="greenhouse",
            http_status=response.status_code,
            robots_basis=read_basis(response),
        )
    token = GREENHOUSE_BOARD_TOKEN.search(response.text) or GREENHOUSE_BOARD_EMBED.search(
        response.text
    )
    if token is None:
        logger.debug("Greenhouse hosted careers page has no board token: %s", url)
        return PostingCheck(
            status="unreadable",
            note="Greenhouse hosted careers page did not identify a board",
            platform="greenhouse",
            robots_basis=read_basis(response),
        )
    return token.group("org")


# Address parts that name a careers site's pages or its host rather than a board.
_NOT_A_BOARD_TOKEN = LISTING_WORDS | {"job-board", "apply", "www", "en", "us", "en-us"}


_PLAUSIBLE_BOARD_TOKEN = re.compile(r"[A-Za-z0-9._-]+")


_MOST_GUESSED_BOARDS = 4


def _greenhouse_guessed_board(
    url: str,
    job: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck | None:
    """Read a hosted posting through a board token its own address spells, if one holds the job.

    A Greenhouse job id lives on one board, so only the right token answers with this
    job. A guessed board's listing is never read: its omitting the job proves nothing.
    """
    for token in _board_token_candidates(url, job):
        payload, _note, basis = read_json(
            f"{GREENHOUSE_API}/boards/{token}/jobs/{job}?questions=true",
            client=client,
            intent=intent,
        )
        if payload is not None and str(payload.get("id")) == job:
            return _greenhouse_check(payload, basis)
    return None


def _board_token_candidates(url: str, job: str) -> list[str]:
    """Return the board tokens a hosted posting's address could spell, path words first."""
    parsed = urlparse(url)
    labels = (parsed.hostname or "").split(".")[:-1]
    host = next((label for label in labels if label not in {"www", "jobs", "careers", "apply"}), "")
    candidates: list[str] = []
    for word in [unquote(segment) for segment in parsed.path.split("/")] + [host]:
        if (
            _PLAUSIBLE_BOARD_TOKEN.fullmatch(word)
            and word != job
            and word.casefold() not in _NOT_A_BOARD_TOKEN
            and word.casefold() not in {candidate.casefold() for candidate in candidates}
        ):
            candidates.append(word)
    return candidates[:_MOST_GUESSED_BOARDS]


def _from_greenhouse_listing(
    org: str,
    job: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> tuple[dict[str, Any] | None, str, bool, RobotsReadBasis | None]:
    """Find one posting inside the whole board, which is answered when one job is not.

    Returns the posting (or None), a human note, whether the board is a populated
    listing that omits the job -- an authoritative delisting -- and the read's
    basis. The board listing is returned complete in one response, so an
    omission is proof, not a paging artifact.
    """
    payload, note, basis = read_json(
        f"{GREENHOUSE_API}/boards/{org}/jobs?content=true", client=client, intent=intent
    )
    if payload is None:
        return None, note, False, basis
    jobs = payload.get("jobs")
    # An empty or malformed board is a transient read, not proof this posting
    # closed; only a populated board that omits it is an authoritative delisting.
    if not isinstance(jobs, list) or not jobs:
        return None, "held no jobs", False, basis
    for item in jobs:
        if isinstance(item, dict) and str(item.get("id")) == job:
            return item, "", False, basis
    return (
        None,
        f"holds {len(jobs)} open jobs and none of them is {job}, so it has closed",
        True,
        basis,
    )


def _lever(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read a Lever posting from its public postings API, and its apply page's form."""
    checked = check_lever(url, client=client, intent=intent)
    match = host_match(LEVER, url)
    if match is not None and checked is not None:
        checked = _with_form(checked, partial(_lever_form, match, client=client, intent=intent))
    return checked_answer(checked, "Lever")


def _with_form(checked: PostingCheck, read: Callable[[], Any]) -> PostingCheck:
    """Keep the application form beside a body; an unread form leaves the posting as read."""
    if checked.source is None:
        return checked
    try:
        form = read()
    except (httpx.HTTPError, FetchPolicyError, IngestError, ValueError):
        return checked
    if not form:
        return checked
    return replace(
        checked,
        source=replace(checked.source, application_form=json.dumps(form, ensure_ascii=False)),
    )


def _lever_form(
    match: re.Match[str], *, client: PoliteHttpClient, intent: PostingReadIntent
) -> list[Any]:
    response = http_get(lever_apply_url(match), PAGE_HEADERS, client=client, intent=intent)
    return lever_form_cards(response.text) if response.status_code == httpx.codes.OK else []


def _ashby_form(
    match: re.Match[str], *, client: PoliteHttpClient, intent: PostingReadIntent
) -> Any:
    response = client.post(
        ASHBY_FORM_API,
        json=ashby_form_request(match),
        timeout=TIMEOUT,
        headers=JSON_HEADERS,
        max_bytes=MAX_POSTING_RESPONSE_BYTES,
        max_elapsed_seconds=time_left(read_deadline_at(), "POST", ASHBY_FORM_API),
        intent=intent,
    )
    return ashby_form(response.json()) if response.status_code == httpx.codes.OK else None


def check_lever(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck | None:
    """Check whether a Lever posting URL still names an open posting."""
    match = host_match(LEVER, url)
    if match is None:
        return None
    # Lever serves the EU from a parallel host; the API host mirrors the board.
    api_host = "api.eu.lever.co" if match.group("region") else "api.lever.co"
    payload, note, basis = read_json(
        f"https://{api_host}/v0/postings/{match.group('org')}/{match.group('job')}",
        client=client,
        intent=intent,
    )
    if payload is None:
        return _unavailable_lever(note, basis=basis)
    return replace(
        posting_check(
            LEVER_MAPPING, Posting(payload), note="Lever published this posting metadata."
        ),
        robots_basis=basis,
    )


def _unavailable_lever(note: str, *, basis: RobotsReadBasis | None) -> PostingCheck:
    """Treat the postings API's own 404 or 410 as a closure; anything else is unreadable."""
    if note in GONE_ENDPOINT_NOTES:
        return PostingCheck(
            status="closed",
            note="Lever no longer lists this posting, so it has closed.",
            platform="lever",
            robots_basis=basis,
        )
    return PostingCheck(
        status="unreadable", note=f"Lever {note}.", platform="lever", robots_basis=basis
    )


def _workday(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read a Workday posting from the site's own JSON endpoint.

    ``/wday/cxs/...`` is what the page requests client-side; the HTML is a shell.
    """
    checked = check_workday(url, client=client, gone_is_closure=True, intent=intent)
    return checked_answer(checked, "Workday")


_WORKDAY_NOT_FOUND_ERROR_CODES = frozenset({"S21"})


def _workday_error_code(response: httpx.Response) -> str:
    """Return the application ``errorCode`` a non-200 CXS response carries, or empty."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("errorCode") or "").strip() if isinstance(body, dict) else ""


def check_workday(
    url: str,
    *,
    client: PoliteHttpClient,
    gone_is_closure: bool = False,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck | None:
    """Read one Workday CXS detail response into publisher-stated metadata."""
    match = host_match(WORKDAY, url)
    remainder = "" if match is None else workday_posting_path(match)
    if match is None or not remainder:
        return None
    request_guard = POSTING_REQUEST_GUARD.get()
    if request_guard is not None and not request_guard(url):
        return PostingCheck(
            status="unreadable",
            note="Posting target is protected from this read.",
            platform="workday",
        )
    host = urlparse(match.string).netloc
    site = match.group("site")
    tenant = match.groupdict().get("tenant") or ""
    remembered = WORKDAY_STATED_SITES.get() or {}
    site_api = remembered.get(_workday_site_key(host, site)) or (
        f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}"
    )
    if tenant:
        site_api = _workday_stated_site_api(
            host, site, client=client, root_url=workday_site_root(match), tenant=tenant
        )
        if not site_api:
            return PostingCheck(
                status="unreadable",
                note="Workday career page did not confirm this tenant and site.",
                platform="workday",
            )
    try:
        response = http_get(
            f"{site_api}/job/{remainder}", JSON_HEADERS, client=client, intent=intent
        )
        if not tenant and _workday_wrong_tenant(response):
            stated = _workday_stated_site_api(host, site, client=client)
            if stated not in ("", site_api):
                site_api = stated
                response = http_get(
                    f"{site_api}/job/{remainder}", JSON_HEADERS, client=client, intent=intent
                )
    except (httpx.HTTPError, FetchPolicyError) as error:
        return PostingCheck(
            status="unreadable",
            note=f"Workday could not be reached ({type(error).__name__}).",
            platform="workday",
        )
    basis = read_basis(response)
    if response.status_code != httpx.codes.OK:
        return _unavailable_workday(
            response, site_api, remainder, gone_is_closure=gone_is_closure, client=client
        )
    try:
        payload = response.json()
    except ValueError:
        return PostingCheck(
            status="unreadable",
            note="Workday returned something not JSON.",
            platform="workday",
            robots_basis=basis,
        )
    if not isinstance(payload, dict):
        return PostingCheck(
            status="unreadable",
            note="Workday returned JSON that was not a record.",
            platform="workday",
            robots_basis=basis,
        )
    info = payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        return PostingCheck(
            status="unreadable",
            note="Workday returned a record with no posting in it.",
            platform="workday",
            robots_basis=basis,
        )
    return replace(
        posting_check(
            WORKDAY_DETAIL_MAPPING,
            Posting(payload, groups={"remainder": remainder}),
            note="Workday published this posting metadata.",
        ),
        robots_basis=basis,
    )


def _workday_site_key(host: str, site: str) -> str:
    """Return the key one Workday career site's stated tenant is remembered under."""
    return f"{host}/{site}".casefold()


def _workday_wrong_tenant(response: httpx.Response) -> bool:
    """Return whether a CXS answer may come from the host label naming the wrong tenant."""
    if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        return True
    refused = response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)
    return refused and bool(_workday_error_code(response))


def _workday_stated_site_api(
    host: str, site: str, *, client: PoliteHttpClient, root_url: str = "", tenant: str = ""
) -> str:
    """Return the API for a confirmed public root, caching answered roots per pass."""
    remembered = WORKDAY_STATED_SITES.get()
    key = root_url or _workday_site_key(host, site)
    if remembered is not None and key in remembered:
        return remembered[key]
    # The career root is a separate crawl even during a known-posting read.
    try:
        page = http_get(root_url or f"https://{host}/{site}", PAGE_HEADERS, client=client)
    except (httpx.HTTPError, FetchPolicyError):
        return ""
    if page.status_code in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
        if remembered is not None:
            remembered[key] = ""
        return ""
    if page.status_code != httpx.codes.OK:
        return ""
    stated_tenant = workday_stated_tenant(page.text, tenant=tenant, site=site if tenant else "")
    stated = f"https://{host}/wday/cxs/{stated_tenant}/{site}" if stated_tenant else ""
    if remembered is not None:
        remembered[key] = stated
    return stated


def _unavailable_workday(
    response: httpx.Response,
    site_api: str,
    remainder: str,
    *,
    gone_is_closure: bool,
    client: PoliteHttpClient,
) -> PostingCheck:
    """Read a detail answer that is not the posting, closing it only on the liveness path.

    A refusal carrying a Workday error closes only when the site's own search confirms
    it. Throttling, a wrong site (422), and 5xx say nothing about the requisition.
    """
    basis = read_basis(response)
    error_code = _workday_error_code(response)
    answered = f"HTTP {response.status_code}" + (f", errorCode {error_code}" if error_code else "")
    gone = response.status_code in (404, 410) or error_code in _WORKDAY_NOT_FOUND_ERROR_CODES
    if gone_is_closure and gone:
        return PostingCheck(
            status="closed",
            note=f"Workday no longer lists this requisition ({answered}), so it has closed.",
            platform="workday",
            robots_basis=basis,
        )
    requisition = _workday_requisition(remainder)
    shown = ""
    if gone_is_closure and error_code and response.status_code in (401, 403) and requisition:
        delisted, shown = _workday_search_verdict(
            f"{site_api}/jobs", requisition, f"/job/{remainder}", client=client
        )
        if delisted:
            return PostingCheck(
                status="closed",
                note=f"Workday no longer lists this requisition ({answered}, and {shown}), "
                "so it has closed.",
                platform="workday",
                robots_basis=basis,
            )
    return PostingCheck(
        status="unreadable",
        note=f"Workday returned {answered}" + (f", and {shown}." if shown else "."),
        platform="workday",
        http_status=response.status_code,
        robots_basis=basis,
    )


# Some sites answer a larger page with an empty 200.
_WORKDAY_SEARCH_LIMIT = 20


def _workday_search_verdict(
    endpoint: str, requisition: str, path: str, *, client: PoliteHttpClient
) -> tuple[bool, str]:
    """Return whether a Workday site's own search shows a requisition delisted, and what it showed.

    Some sites search by meaning or bullet a place rather than the id, so a miss
    counts only once a requisition the site lists is found the same way.
    """
    unconfirmed = "the site's own search could not confirm it"
    # Bullets may state a suffixed requisition's stem, and the control proves only
    # that plain ids are searchable.
    stem = _workday_requisition_stem(requisition)
    for text in filter(None, (requisition, stem)):
        found, failure = _workday_search(endpoint, text, client=client)
        if found is None:
            return False, f"{unconfirmed} (the search for {text} {failure})"
        if _workday_lists(found, requisition, path):
            return False, f"the site's own search still lists {requisition}"
    listing, failure = _workday_search(endpoint, "", client=client)
    if listing is None:
        return False, f"{unconfirmed} (the unfiltered search {failure})"
    control = _workday_control(listing)
    if control is None:
        return False, f"{unconfirmed} (its listing states no requisition id to search for)"
    listed, listed_path = control
    found, failure = _workday_search(endpoint, listed, client=client)
    if found is None or not _workday_lists(found, listed, listed_path):
        return False, f"{unconfirmed} (the search for listed {listed} {failure or 'misses it'})"
    missed = f"{requisition} or {stem}" if stem else requisition
    return True, f"the site's own search finds listed {listed} but not {missed}"


def _workday_search(
    endpoint: str, text: str, *, client: PoliteHttpClient
) -> tuple[list[dict[str, Any]] | None, str]:
    """POST one search of a Workday site, returning its postings or why there are none."""
    body = {"appliedFacets": {}, "limit": _WORKDAY_SEARCH_LIMIT, "offset": 0, "searchText": text}
    try:
        response = client.post(
            endpoint,
            json=body,
            timeout=TIMEOUT,
            headers=JSON_HEADERS,
            max_bytes=MAX_POSTING_RESPONSE_BYTES,
            max_elapsed_seconds=time_left(read_deadline_at(), "POST", endpoint),
        )
    except (httpx.HTTPError, FetchPolicyError) as error:
        return None, f"could not be reached ({type(error).__name__})"
    # "Answered", because a note saying "returned HTTP 404" reads as a closure.
    if response.status_code != httpx.codes.OK:
        return None, f"answered HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, "answered with something that was not JSON"
    postings = payload.get("jobPostings") if isinstance(payload, dict) else None
    if not isinstance(postings, list):
        return None, "answered with no list of postings"
    return [posting for posting in postings if isinstance(posting, dict)], ""


def _workday_requisition(path: str) -> str:
    """Return the requisition id after the last "_" of a Workday job path, or empty."""
    _slug, separator, requisition = path.rstrip("/").rsplit("/", 1)[-1].rpartition("_")
    return requisition if separator and re.search(r"\d", requisition) else ""


def _workday_requisition_stem(requisition: str) -> str:
    """Return a requisition id without a trailing "-<digits>", or empty unless an id remains.

    "R-123" has no stem: "R" states no number to search for.
    """
    stem, separator, suffix = requisition.rpartition("-")
    return stem if separator and suffix.isdigit() and re.search(r"\d", stem) else ""


def _workday_bullets_state(posting: dict[str, Any], requisition: str) -> bool:
    """Return whether a search result's bullet fields state this requisition id."""
    bullets = posting.get("bulletFields")
    wanted = requisition.casefold()
    return isinstance(bullets, list) and any(str(b).strip().casefold() == wanted for b in bullets)


def _workday_lists(postings: list[dict[str, Any]], requisition: str, path: str) -> bool:
    """Return whether search results hold a requisition, by its bullet id or stem, or its path."""
    ids = [text for text in (requisition, _workday_requisition_stem(requisition)) if text]
    return any(
        any(_workday_bullets_state(posting, text) for text in ids)
        or str(posting.get("externalPath") or "").casefold() == path.casefold()
        for posting in postings
    )


def _workday_control(listing: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return the id and path of the first listed posting whose bullets state its own id."""
    for posting in listing:
        path = str(posting.get("externalPath") or "")
        requisition = _workday_requisition(path)
        if requisition and _workday_bullets_state(posting, requisition):
            return requisition, path
    return None


def _oracle(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read an Oracle Recruiting Cloud posting from the site's own candidate API."""
    checked = check_oracle(url, client=client, intent=intent)
    return checked_answer(checked, "Oracle")


def check_oracle(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> PostingCheck | None:
    """Read one Oracle Recruiting Cloud requisition into publisher-stated metadata.

    The career site renders client-side, so the candidate REST API the page calls
    is the only structured source. Its ById finder is a direct existence query, so
    an HTTP 200 with no items is an authoritative delisting; a transient failure is
    a non-200 handled above it.
    """
    match = host_match(ORACLE, url)
    if match is None:
        return None
    host, site, job = match.group("host"), match.group("site"), match.group("job")
    payload, note, basis = read_json(
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f'?expand=all&finder=ById;Id="{job}",siteNumber="{site}"',
        client=client,
        intent=intent,
    )
    if payload is None:
        return PostingCheck(
            status="unreadable", note=f"Oracle {note}.", platform="oracle", robots_basis=basis
        )
    items = payload.get("items")
    stated_count = payload.get("count")
    # Only an explicit empty item list, not contradicted by a stated count, is a delisting.
    if not isinstance(items, list) or (not items and stated_count not in (None, 0)):
        return PostingCheck(
            status="unreadable",
            note="Oracle returned HTTP 200 without a readable requisition list.",
            platform="oracle",
            robots_basis=basis,
        )
    if not items:
        return PostingCheck(
            status="closed",
            note=(
                "Oracle no longer lists this requisition (HTTP 200, no items from the "
                "ById finder), so it has closed."
            ),
            platform="oracle",
            robots_basis=basis,
        )
    requisition = items[0]
    if not isinstance(requisition, dict):
        return PostingCheck(
            status="unreadable",
            note="Oracle returned a requisition that was not a record.",
            platform="oracle",
            robots_basis=basis,
        )
    return replace(
        posting_check(
            ORACLE_DETAIL_MAPPING,
            Posting(requisition, groups={"host": host, "site": site}),
            note="Oracle published this posting metadata.",
        ),
        robots_basis=basis,
    )


def _workable(
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
    board_url: str = "",
) -> Answer:
    """Read a Workable short link from the board that listed it."""
    match = host_match(_WORKABLE_SHORT_JOB, url)
    if match is None:
        return Answer.absent()
    board = host_match(WORKABLE_BOARD, board_url)
    if board is None:
        return checked_answer(
            PostingCheck(
                status="unreadable",
                note="Workable board address is unavailable for this short link.",
                platform="workable",
            ),
            "Workable",
        )
    org = quote(board.group("org"), safe="")
    payload, note, basis = site_record_feed(
        f"https://apply.workable.com/api/v1/widget/accounts/{org}?details=true",
        client=client,
        intent=intent,
    )
    if payload is None:
        checked = PostingCheck(
            status="unreadable",
            note=f"Workable {note}.",
            platform="workable",
            robots_basis=basis,
        )
        return checked_answer(checked, "Workable")
    jobs = payload.get("jobs")
    # An empty or malformed feed cannot establish that the publisher closed a job.
    if (
        not isinstance(jobs, list)
        or not jobs
        or any(
            not isinstance(job, dict)
            or not isinstance(job.get("shortcode"), str)
            or not job["shortcode"].strip()
            for job in jobs
        )
    ):
        checked = PostingCheck(
            status="unreadable",
            note="Workable returned no usable job list.",
            platform="workable",
            robots_basis=basis,
        )
        return checked_answer(checked, "Workable")
    shortcode = match.group("job")
    posting = next(
        (job for job in jobs if job["shortcode"].casefold() == shortcode.casefold()),
        None,
    )
    if posting is None:
        checked = PostingCheck(
            status="closed",
            note=(
                f"Workable holds {len(jobs)} open jobs and none has shortcode "
                f"{shortcode}, so it has closed."
            ),
            platform="workable",
            robots_basis=basis,
        )
        return checked_answer(checked, "Workable")
    checked = replace(
        posting_check(
            WORKABLE_MAPPING,
            Posting(posting),
            note="Workable published this posting metadata.",
        ),
        robots_basis=basis,
    )
    return checked_answer(checked, "Workable")


def _ashby(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read an Ashby posting from its public job-board API.

    Ashby exposes only a board listing, filtered here by the UUID in the job URL.
    """
    checked = check_ashby(url, client=client, intent=intent)
    match = host_match(ASHBY, url)
    if match is not None and checked is not None:
        checked = _with_form(checked, partial(_ashby_form, match, client=client, intent=intent))
    return checked_answer(checked, "Ashby")


def check_ashby(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> PostingCheck | None:
    """Read one Ashby board listing into publisher-stated metadata."""
    match = host_match(ASHBY, url)
    if match is None:
        return None
    organisation = quote(unquote(match.group("org")), safe="")
    job = match.group("job")
    payload, note, basis = read_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{organisation}",
        client=client,
        intent=intent,
    )
    if payload is None:
        return PostingCheck(
            status="unreadable", note=f"Ashby {note}.", platform="ashby", robots_basis=basis
        )
    jobs = payload.get("jobs")
    # An empty or malformed board is a transient read, not proof this posting
    # closed; only a populated board that omits it is an authoritative delisting.
    if not isinstance(jobs, list) or not jobs:
        return PostingCheck(
            status="unreadable",
            note="Ashby held no jobs.",
            platform="ashby",
            robots_basis=basis,
        )
    posting = next(
        (item for item in jobs if isinstance(item, dict) and str(item.get("id")) == job),
        None,
    )
    if posting is None:
        return PostingCheck(
            status="closed",
            note=f"Ashby holds {len(jobs)} open jobs and none of them is {job}, so it has closed.",
            platform="ashby",
            robots_basis=basis,
        )
    return replace(
        posting_check(
            ASHBY_MAPPING, Posting(posting), note="Ashby published this posting metadata."
        ),
        robots_basis=basis,
    )


def _smartrecruiters(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read a SmartRecruiters posting, whose body only its single-posting endpoint states."""
    return checked_answer(
        check_smartrecruiters(url, client=client, intent=intent), "SmartRecruiters"
    )


def check_smartrecruiters(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> PostingCheck | None:
    """Read one SmartRecruiters posting detail into publisher-stated metadata."""
    match = host_match(SMARTRECRUITERS_JOB, url) or host_match(SMARTRECRUITERS_POSTING_REF, url)
    if match is None:
        return None
    org = match.group("org")
    # A public posting address ends its numeric id with a title slug the API does not take.
    job = match.group("job").partition("-")[0]
    payload, note, basis = read_json(
        f"https://api.smartrecruiters.com/v1/companies/{org}/postings/{job}",
        client=client,
        intent=intent,
    )
    if payload is None:
        if note in GONE_ENDPOINT_NOTES:
            return PostingCheck(
                status="closed",
                note="SmartRecruiters no longer lists this posting, so it has closed.",
                platform="smartrecruiters",
                robots_basis=basis,
            )
        return PostingCheck(
            status="unreadable",
            note=f"SmartRecruiters {note}.",
            platform="smartrecruiters",
            robots_basis=basis,
        )
    return replace(
        posting_check(
            SMARTRECRUITERS_MAPPING,
            Posting(payload, groups={"org": org}),
            note="SmartRecruiters published this posting metadata.",
        ),
        robots_basis=basis,
    )


@dataclass(frozen=True)
class _PostEndpoint:
    """A publisher whose one-post endpoint states the whole posting a URL names."""

    platform: str
    publisher: str
    pattern: re.Pattern[str]
    address: Callable[[re.Match[str]], str]
    mapping: BoardDeclaration
    # The post record inside the endpoint's answer, or None when it carries none.
    record: Callable[[Any], Mapping[str, Any] | None]
    withdrawn: Callable[[Mapping[str, Any]], bool] = lambda _record: False
    posting: Callable[[re.Match[str], Mapping[str, Any]], Posting] = lambda _match, record: Posting(
        record
    )
    fetch: Callable[..., tuple[Any, str, RobotsReadBasis | None]] = read_json
    read_as: Literal["API", "page"] = "API"


def _titled(payload: Any) -> Mapping[str, Any] | None:
    """Return an answer that is itself a titled post record, or None."""
    return payload if isinstance(payload, Mapping) and payload.get("title") else None


POST_ENDPOINTS = (
    _PostEndpoint(
        platform="bamboohr",
        publisher="BambooHR",
        pattern=BAMBOOHR_JOB,
        address=lambda match: bamboohr_detail_url(match.group("tenant"), match.group("job")),
        mapping=BAMBOOHR_MAPPING,
        record=bamboohr_opening,
        withdrawn=bamboohr_withdrawn,
        # The mapping joins a list row to its opening; the row supplies only the id.
        posting=lambda match, opening: Posting(
            {"row": {"id": match.group("job")}, "opening": opening},
            groups={"tenant": match.group("tenant")},
        ),
    ),
    _PostEndpoint(
        platform="gem",
        publisher="Gem",
        pattern=GEM_JOB,
        address=lambda match: gem_post_url(match.group("org"), match.group("job")),
        mapping=GEM_MAPPING,
        record=_titled,
    ),
    _PostEndpoint(
        platform="rippling",
        publisher="Rippling",
        pattern=RIPPLING_JOB,
        address=lambda match: rippling_detail_url(match.group("org"), match.group("job")),
        mapping=RIPPLING_MAPPING,
        record=rippling_job,
        # The mapping joins a list row to its detail; the row supplies only the id.
        posting=lambda match, detail: Posting(
            {"row": {"id": match.group("job")}, "detail": detail},
            groups={"org": match.group("org")},
        ),
    ),
    _PostEndpoint(
        platform="dover",
        publisher="Dover",
        pattern=DOVER_JOB,
        address=lambda match: dover_detail_url(match.group("job")),
        mapping=DOVER_MAPPING,
        record=dover_job,
        posting=lambda match, detail: Posting(
            {"row": {"id": match.group("job")}, "detail": detail}
        ),
    ),
    _PostEndpoint(
        platform="adp",
        publisher="ADP Workforce Now",
        pattern=ADP_JOB,
        address=lambda match: adp_requisition_url(
            match.group("tenant"),
            match.group("job"),
            match.group("lang") or "",
            career_center=match.group("career_center") or "",
        ),
        mapping=ADP_MAPPING,
        record=adp_requisition,
        posting=lambda match, detail: Posting(
            {"row": {"itemID": match.group("job")}, "detail": detail},
            groups={"tenant": match.group("tenant"), "lang": match.group("lang") or ""},
        ),
    ),
    _PostEndpoint(
        platform="telus",
        publisher="TELUS AI Community",
        pattern=TELUS_POST,
        address=lambda match: telus_post_url(match.group("job")),
        mapping=TELUS_MAPPING,
        record=_titled,
        withdrawn=telus_withdrawn,
    ),
    _PostEndpoint(
        platform="ukg",
        publisher="UKG Pro Recruiting",
        pattern=UKG_OPPORTUNITY,
        address=ukg_opportunity_url,
        mapping=UKG_MAPPING,
        record=ukg_opportunity,
        withdrawn=ukg_withdrawn,
        fetch=page_payload,
        read_as="page",
    ),
)


def _read_post_endpoint(
    endpoint: _PostEndpoint,
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> Answer:
    """Read a posting whose page is a shell its publisher's post endpoint fills."""
    checked = check_post_endpoint(endpoint, url, client=client, intent=intent)
    return checked_answer(checked, endpoint.publisher)


def check_post_endpoint(
    endpoint: _PostEndpoint,
    url: str,
    *,
    client: PoliteHttpClient,
    intent: PostingReadIntent = PostingReadIntent.CRAWL,
) -> PostingCheck | None:
    """Read one post endpoint; only a gone endpoint or a stated withdrawal closes it."""
    match = host_match(endpoint.pattern, url)
    if match is None:
        return None
    payload, note, basis = endpoint.fetch(endpoint.address(match), client=client, intent=intent)
    record = endpoint.record(payload)
    gone = payload is None and note in GONE_ENDPOINT_NOTES
    if gone or (record is not None and endpoint.withdrawn(record)):
        return PostingCheck(
            status="closed",
            note=f"{endpoint.publisher} no longer lists this posting as open, so it has closed.",
            platform=endpoint.platform,
            robots_basis=basis,
        )
    if record is None:
        return PostingCheck(
            status="unreadable",
            note=f"{endpoint.publisher} {note or 'did not return a posting'}.",
            platform=endpoint.platform,
            robots_basis=basis,
        )
    return replace(
        posting_check(
            endpoint.mapping,
            endpoint.posting(match, record),
            note=f"{endpoint.publisher} published this posting metadata.",
        ),
        robots_basis=basis,
    )


def _dayforce(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> Answer:
    """Read a Dayforce posting from the anonymous feed, which states its body too."""
    return checked_answer(check_dayforce(url, client=client, intent=intent), "Dayforce")


def check_dayforce(
    url: str, *, client: PoliteHttpClient, intent: PostingReadIntent = PostingReadIntent.CRAWL
) -> PostingCheck | None:
    """Read one posting out of the Dayforce site feed the board scan reads.

    Dayforce serves no single-posting endpoint, so the anonymous feed of the
    whole site is the structured answer, and the URL carries an opaque namespace
    and site rather than any name.
    """
    match = host_match(DAYFORCE_JOB, url)
    if match is None:
        return None
    feed, note, basis = site_feed(
        dayforce_feed_url(match.group("namespace"), match.group("site")),
        client=client,
        intent=intent,
    )
    if feed is None:
        return PostingCheck(
            status="unreadable", note=f"Dayforce {note}.", platform="dayforce", robots_basis=basis
        )
    job = match.group("job")
    rows = [row for row in feed if isinstance(row, dict)]
    # An empty or malformed feed is a transient read, not proof this posting
    # closed; only a populated feed that omits it is an authoritative delisting.
    if not rows:
        return PostingCheck(
            status="unreadable",
            note="Dayforce held no jobs.",
            platform="dayforce",
            robots_basis=basis,
        )
    variants = [row for row in rows if dayforce_posting_id(row) == job]
    if not variants:
        return PostingCheck(
            status="closed",
            note=(
                f"Dayforce holds {len(rows)} open jobs and none of them is {job}, so it has closed."
            ),
            platform="dayforce",
            robots_basis=basis,
        )
    return replace(
        posting_check(
            DAYFORCE_MAPPING,
            # The feed states one row per region a posting serves, as the scan reads it.
            Posting({**variants[0], "variants": variants}),
            note="Dayforce published this posting metadata.",
        ),
        robots_basis=basis,
    )
