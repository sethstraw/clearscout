"""Deterministically resolve pending posting content through static and rendered reads."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError

from clearscout.application.discovery.content_resolution import ResolvedPostingContent
from clearscout.application.discovery.posting_text import closure_signal, publisher_closing_date
from clearscout.application.failures import ValidationError
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import employer_name_words
from clearscout.domain.records import PageClassification, PostingReadIntent, RobotsReadBasis
from clearscout.domain.staged_facts import PublisherClosingDate
from clearscout.infrastructure.web_fetch.ingest import url as url_ingest
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.policy import (
    UNRESOLVED_DESTINATION_NOTE,
    DestinationUnresolvableError,
    FetchPolicyError,
    HostHeldError,
    InvalidFetchUrlError,
    PoliteHttpClient,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from clearscout.infrastructure.web_fetch.posting_fetch import (
    PUBLISHER_REFUSED_METHOD,
    UNRESOLVED_DESTINATION_METHOD,
    PolitePostingFetcher,
)
from clearscout.infrastructure.web_fetch.rendered import (
    RESPONSE_BYTE_LIMIT_REASON,
    RenderedPage,
    RenderedPageFetcher,
    RenderRuntimeUnavailableError,
)
from clearscout.shared.errors import IngestError

MAX_CONTENT_RESOLUTION_SECONDS = 45.0
_RETRYABLE_READ_NOTE = "The rendered publisher read failed and remains retryable."
# LinkedIn redirects a job view to a slug ending in the same job id.
_LINKEDIN_JOB_VIEW = re.compile(r"^/jobs/view/(?:[^/]*-)?(?P<id>\d+)/?$")


class DeterministicPostingContentResolver:
    """Use supported APIs, publisher HTML, then a policy-routed browser renderer."""

    def __init__(
        self,
        *,
        client: PoliteHttpClient | None = None,
        rendered: RenderedPageFetcher | None = None,
    ) -> None:
        """Share one outbound policy across the static and rendered tiers."""
        self.client = client or PoliteHttpClient(user_agent=url_ingest.USER_AGENT)
        self.static = PolitePostingFetcher(client=self.client)
        self.rendered = rendered or RenderedPageFetcher(client=self.client)

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        """Return the first publisher-verifiable body or an exact retryable outcome."""
        deadline_at = time.monotonic() + MAX_CONTENT_RESOLUTION_SECONDS
        static = self._static(
            url,
            employer=employer,
            title=title,
            deadline_at=deadline_at,
            intent=intent,
            board_url=board_url,
        )
        if static is not None:
            return static
        # The browser tier reads as a crawl whatever the caller's intent.
        return self._rendered(url, employer=employer, title=title, deadline_at=deadline_at)

    def _static(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        deadline_at: float,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent | None:
        try:
            fetched = self.static.fetch(
                url,
                max_elapsed_seconds=self._remaining_seconds(deadline_at),
                intent=intent,
                board_url=board_url,
            )
        except (IngestError, ValidationError, httpx.HTTPError, ValueError):
            return None
        if fetched.posting_status == PostingStatus.CLOSED:
            return ResolvedPostingContent(
                requested_url=url,
                final_url=fetched.final_url or url,
                content_status=PostingContentAccessStatus.NOT_YET_READ,
                identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                posting_status=PostingStatus.CLOSED,
                extraction_method=None,
                fetched_at=fetched.fetched_at,
                http_status=fetched.http_status,
                publisher_markup_present=fetched.publisher_markup_present,
                identity_evidence={"method": fetched.method, "closure": True},
                note="The publisher reports that this posting is closed.",
                robots_basis=fetched.robots_basis,
            )
        if fetched.method in {PUBLISHER_REFUSED_METHOD, UNRESOLVED_DESTINATION_METHOD}:
            return _unreadable(
                url,
                (
                    PostingContentAccessStatus.ACCESS_BLOCKED
                    if fetched.http_status in {401, 403}
                    else PostingContentAccessStatus.FETCH_FAILED
                ),
                PostingIdentityStatus.NOT_YET_VERIFIED,
                fetched.fetched_at,
                fetched.verification_note,
                {"method": fetched.method},
                http_status=fetched.http_status,
            )
        if not fetched.text:
            return None
        if _publisher_declared(fetched.publisher_markup_present, fetched.method):
            identity = _publisher_identity(
                title,
                employer,
                fetched.title,
                fetched.employer,
                url,
                fetched.final_url or url,
            )
            return _readable(
                url,
                fetched.final_url or url,
                fetched.text,
                fetched.fetched_at,
                PostingContentExtractionMethod.STATIC_STRUCTURED,
                fetched.http_status,
                fetched.publisher_markup_present,
                {
                    "method": fetched.method,
                    "publisher_declaration": True,
                    "expected_title": title,
                    "publisher_title": fetched.title,
                    "expected_employer": employer,
                    "publisher_employer": fetched.employer,
                },
                identity,
                robots_basis=fetched.robots_basis,
                source=fetched.source,
                closing_date=fetched.publisher_closing_date,
            )
        if fetched.page_classification != PageClassification.POSTING:
            return None
        return _readable(
            url,
            fetched.final_url or url,
            fetched.text,
            fetched.fetched_at,
            PostingContentExtractionMethod.STATIC_HTML,
            fetched.http_status,
            fetched.publisher_markup_present,
            {
                "classification": fetched.page_classification,
                "expected_title": title,
                "publisher_title": fetched.title,
            },
            _publisher_identity(
                title,
                employer,
                fetched.title,
                fetched.employer,
                url,
                fetched.final_url or url,
            ),
            robots_basis=fetched.robots_basis,
            source=fetched.source,
            closing_date=fetched.publisher_closing_date,
        )

    def _rendered(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        deadline_at: float,
    ) -> ResolvedPostingContent:
        fetched_at = datetime.now(UTC)
        try:
            page = self.rendered.fetch(
                url, max_elapsed_seconds=self._remaining_seconds(deadline_at)
            )
        except RenderRuntimeUnavailableError as exc:
            return _unreadable(
                url,
                PostingContentAccessStatus.RENDER_REQUIRED,
                PostingIdentityStatus.NOT_YET_VERIFIED,
                fetched_at,
                "A supported browser runtime is not installed.",
                {"error": type(exc).__name__},
            )
        except FetchPolicyError as exc:
            status, note = _policy_stop_outcome(exc)
            return _unreadable(
                url,
                status,
                PostingIdentityStatus.NOT_YET_VERIFIED,
                fetched_at,
                note,
                {"error": type(exc).__name__},
            )
        except (PlaywrightError, httpx.HTTPError, ValueError) as exc:
            return _unreadable(
                url,
                PostingContentAccessStatus.FETCH_FAILED,
                PostingIdentityStatus.NOT_YET_VERIFIED,
                fetched_at,
                _RETRYABLE_READ_NOTE,
                {"error": type(exc).__name__},
            )
        resolved = _naming_byte_refusals(
            self._from_page(url, page, employer=employer, title=title, fetched_at=fetched_at), page
        )
        if not page.navigation_timed_out:
            return resolved
        return replace(
            resolved,
            identity_evidence={**(resolved.identity_evidence or {}), "navigation_timed_out": True},
        )

    def _from_page(
        self,
        url: str,
        page: RenderedPage,
        *,
        employer: str,
        title: str,
        fetched_at: datetime,
    ) -> ResolvedPostingContent:
        if page.html_truncated:
            return _unreadable(
                url,
                PostingContentAccessStatus.RENDER_REQUIRED,
                PostingIdentityStatus.AMBIGUOUS,
                fetched_at,
                "The rendered publisher document exceeded the retained content limit.",
                {
                    "html_truncated": True,
                    "blocked_request_count": len(page.blocked_requests),
                    "blocked_request_overflow_count": page.blocked_request_overflow_count,
                    "deferred_response_count": len(page.deferred_response_urls),
                },
                final_url=page.final_url,
                http_status=page.document_status,
            )
        evidence = inspect_publisher_page(page.final_url, page.html)
        if closure_signal(evidence.visible_text):
            return _unreadable(
                url,
                PostingContentAccessStatus.NOT_YET_READ,
                PostingIdentityStatus.NOT_YET_VERIFIED,
                fetched_at,
                "The publisher reports that this posting is closed.",
                {"classification": evidence.classification.value, "closure": True},
                final_url=page.final_url,
                http_status=page.document_status,
                posting_status=PostingStatus.CLOSED,
            )
        if evidence.classification is not PageClassification.POSTING:
            return _rendered_unreadable(
                url,
                page.final_url,
                evidence.classification,
                fetched_at,
                page.document_status,
            )
        try:
            posting = url_ingest.from_html(page.final_url, page.html)
        except IngestError:
            return _unreadable(
                url,
                PostingContentAccessStatus.RENDER_REQUIRED,
                PostingIdentityStatus.AMBIGUOUS,
                fetched_at,
                "The rendered page identified a posting but did not expose a complete body.",
                {"classification": evidence.classification.value, "title": evidence.title},
                final_url=page.final_url,
            )
        publisher_title = posting.title or evidence.title
        return _readable(
            url,
            page.final_url,
            posting.text,
            fetched_at,
            _page_method(page, posting.publisher_markup_present, posting.method),
            page.document_status,
            posting.publisher_markup_present,
            {
                "classification": evidence.classification.value,
                "expected_title": title,
                "publisher_title": publisher_title,
                "expected_employer": employer,
                "publisher_employer": posting.employer,
                "blocked_request_count": len(page.blocked_requests),
                "deferred_response_count": len(page.deferred_response_urls),
            },
            _publisher_identity(
                title,
                employer,
                publisher_title,
                posting.employer,
                url,
                page.final_url,
            ),
            robots_basis=page.robots_basis,
            source=posting.source,
            closing_date=publisher_closing_date(
                posting.text,
                valid_through=posting.valid_through,
                source_url=page.final_url,
                observed_at=fetched_at,
            ),
        )

    @staticmethod
    def _remaining_seconds(deadline_at: float) -> float:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("Posting content resolution exceeded its total deadline.")
        return remaining


def _policy_stop_outcome(exc: FetchPolicyError) -> tuple[PostingContentAccessStatus, str]:
    """Return the stored status and note for a rendered read the fetch policy stopped.

    Only a robots rule or an address the policy never fetches refuses the read.
    """
    match exc:
        case RobotsDeniedError():
            return (
                PostingContentAccessStatus.POLICY_REFUSED,
                "Published fetch policy refused the rendered read.",
            )
        case InvalidFetchUrlError():
            return (
                PostingContentAccessStatus.POLICY_REFUSED,
                "The publisher's address, or a redirect it answered, is not a fetchable "
                "public HTTP(S) URL.",
            )
        case DestinationUnresolvableError():
            note = UNRESOLVED_DESTINATION_NOTE
        case RobotsUnavailableError():
            note = (
                "The publisher's robots policy could not be read, and the read remains retryable."
            )
        case HostHeldError():
            note = (
                "The publisher asked for a longer wait than this read allows, and the read "
                "remains retryable."
            )
        case _:
            note = _RETRYABLE_READ_NOTE
    return PostingContentAccessStatus.FETCH_FAILED, note


def _publisher_declared(publisher_markup_present: bool | None, method: str) -> bool:
    """Return whether a static read took its posting from what the publisher declared."""
    return bool(publisher_markup_present) or method != "page extraction"


def _page_method(
    page: RenderedPage, publisher_markup_present: bool | None, method: str
) -> PostingContentExtractionMethod:
    """Name the extraction behind a posting read from a rendered page result."""
    # The main document kept after a navigation timeout came from the HTTP client,
    # not the browser DOM.
    if page.navigation_timed_out:
        return (
            PostingContentExtractionMethod.STATIC_STRUCTURED
            if _publisher_declared(publisher_markup_present, method)
            else PostingContentExtractionMethod.STATIC_HTML
        )
    return (
        PostingContentExtractionMethod.RENDERED_JSON
        if publisher_markup_present
        else PostingContentExtractionMethod.RENDERED_HTML
    )


def _readable(
    requested_url: str,
    final_url: str,
    text: str,
    fetched_at: datetime,
    method: PostingContentExtractionMethod,
    http_status: int | None,
    publisher_markup_present: bool | None,
    identity_evidence: dict[str, object],
    identity_status: PostingIdentityStatus = PostingIdentityStatus.VERIFIED,
    *,
    robots_basis: RobotsReadBasis | None = None,
    source: PostingSource | None = None,
    closing_date: PublisherClosingDate | None = None,
) -> ResolvedPostingContent:
    if closing_date is None:
        closing_date = publisher_closing_date(text, source_url=final_url, observed_at=fetched_at)
    note = _identity_note(identity_status)
    if closing_date.has_passed(fetched_at) is True:
        note += " Closing date passed; verify availability."
    return ResolvedPostingContent(
        source=source,
        requested_url=requested_url,
        final_url=final_url,
        content_status=PostingContentAccessStatus.READABLE,
        identity_status=identity_status,
        posting_status=PostingStatus.ACTIVE,
        extraction_method=method,
        fetched_at=fetched_at,
        content=text.encode("utf-8"),
        http_status=http_status,
        publisher_markup_present=publisher_markup_present,
        identity_evidence=identity_evidence,
        note=note,
        robots_basis=robots_basis,
        publisher_closing_date=closing_date,
    )


def _publisher_identity(
    expected_title: str,
    expected_employer: str,
    observed_title: str,
    observed_employer: str,
    requested_url: str,
    final_url: str,
) -> PostingIdentityStatus:
    expected_title_key = _identity_key(expected_title)
    observed_title_key = _identity_key(observed_title)
    if not observed_title_key:
        return PostingIdentityStatus.AMBIGUOUS
    if expected_title_key and expected_title_key != observed_title_key:
        return PostingIdentityStatus.CONTRADICTED
    expected_employer_words = employer_name_words(expected_employer)
    observed_employer_words = employer_name_words(observed_employer)
    if expected_employer_words and observed_employer_words:
        return (
            PostingIdentityStatus.VERIFIED
            if expected_employer_words == observed_employer_words
            or _differ_by_trailing_descriptors(expected_employer_words, observed_employer_words)
            else PostingIdentityStatus.CONTRADICTED
        )
    if _url_identity(requested_url) == _url_identity(final_url):
        return PostingIdentityStatus.VERIFIED
    return PostingIdentityStatus.AMBIGUOUS


# Words that trail an employer's own name without naming a different employer.
_EMPLOYER_DESCRIPTORS = frozenset(
    {
        "ai",
        "analytics",
        "bio",
        "biosciences",
        "care",
        "clinical",
        "consulting",
        "data",
        "diagnostics",
        "digital",
        "health",
        "healthcare",
        "informatics",
        "labs",
        "medical",
        "pharma",
        "pharmaceuticals",
        "sciences",
        "software",
        "solutions",
        "systems",
        "tech",
        "technologies",
        "technology",
        "therapeutics",
    }
)
_NAME_ARTICLES = frozenset({"a", "an", "the"})


def _differ_by_trailing_descriptors(first: list[str], second: list[str]) -> bool:
    """Return whether one name is the other plus trailing descriptors, as "Globex Analytics"."""
    shorter, longer = sorted((first, second), key=len)
    return (
        longer[: len(shorter)] == shorter
        and all(word in _EMPLOYER_DESCRIPTORS for word in longer[len(shorter) :])
        and any(word not in _EMPLOYER_DESCRIPTORS | _NAME_ARTICLES for word in shorter)
    )


def _identity_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _url_identity(value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").removeprefix("www.")
    linkedin = host == "linkedin.com" or host.endswith(".linkedin.com")
    if linkedin and (job := _LINKEDIN_JOB_VIEW.search(parts.path)):
        return f"linkedin:{job.group('id')}"
    return value.partition("#")[0].rstrip("/").casefold()


def _identity_note(status: PostingIdentityStatus) -> str:
    if status is PostingIdentityStatus.VERIFIED:
        return "Publisher posting content is readable and stored for evaluation."
    if status is PostingIdentityStatus.CONTRADICTED:
        return "Publisher content is readable, but its identity contradicts the selected posting."
    return "Publisher content is readable, but its identity remains ambiguous."


def _naming_byte_refusals(
    result: ResolvedPostingContent, page: RenderedPage
) -> ResolvedPostingContent:
    """Say so on an unreadable render that refused a response over its size limits."""
    refused = sum(item.reason == RESPONSE_BYTE_LIMIT_REASON for item in page.blocked_requests)
    if not refused or result.content_status is PostingContentAccessStatus.READABLE:
        return result
    note = f"The render refused {refused} response(s) over its size limits."
    return replace(
        result,
        note=f"{result.note} {note}".strip(),
        identity_evidence={**(result.identity_evidence or {}), "byte_limit_refusals": refused},
    )


def _rendered_unreadable(
    requested_url: str,
    final_url: str,
    classification: PageClassification,
    fetched_at: datetime,
    http_status: int | None,
) -> ResolvedPostingContent:
    status = (
        PostingContentAccessStatus.ACCESS_BLOCKED
        if http_status in {401, 403}
        else {
            PageClassification.ERROR_OR_CHALLENGE: PostingContentAccessStatus.SESSION_REQUIRED,
            PageClassification.JS_SHELL: PostingContentAccessStatus.RENDER_REQUIRED,
            PageClassification.AMBIGUOUS: PostingContentAccessStatus.FETCH_FAILED,
        }.get(classification, PostingContentAccessStatus.RENDER_REQUIRED)
    )
    identity = (
        PostingIdentityStatus.CONTRADICTED
        if classification in {PageClassification.NAVIGATION, PageClassification.APPLICATION_CHANNEL}
        else PostingIdentityStatus.AMBIGUOUS
    )
    return _unreadable(
        requested_url,
        status,
        identity,
        fetched_at,
        f"Rendered publisher content was classified as {classification.value}.",
        {"classification": classification.value},
        final_url=final_url,
        http_status=http_status,
    )


def _unreadable(
    requested_url: str,
    status: PostingContentAccessStatus,
    identity: PostingIdentityStatus,
    fetched_at: datetime,
    note: str,
    evidence: dict[str, object],
    *,
    final_url: str | None = None,
    http_status: int | None = None,
    posting_status: PostingStatus | None = None,
) -> ResolvedPostingContent:
    return ResolvedPostingContent(
        requested_url=requested_url,
        final_url=final_url,
        content_status=status,
        identity_status=identity,
        posting_status=posting_status,
        extraction_method=None,
        fetched_at=fetched_at,
        http_status=http_status,
        identity_evidence=evidence,
        note=note,
    )
