from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from socket import AF_INET, SOCK_STREAM
from typing import Any, cast

import httpx
import pytest

from tailorcv.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
    pending_discovery_readiness,
)
from tailorcv.application.discovery.posting_fetch import FetchedPosting
from tailorcv.application.discovery.posting_text import publisher_closing_date
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.domain import (
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
)
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.records import PageClassification, PostingReadIntent, RobotsReadBasis
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery, PendingPostingContent
from tailorcv.infrastructure.web_fetch import posting_resolution
from tailorcv.infrastructure.web_fetch.policy import (
    UNRESOLVED_DESTINATION_NOTE,
    DestinationUnresolvableError,
    HostHeldError,
    PoliteHttpClient,
)
from tailorcv.infrastructure.web_fetch.posting_fetch import UNRESOLVED_DESTINATION_METHOD
from tailorcv.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from tailorcv.infrastructure.web_fetch.rendered import (
    RESPONSE_BYTE_LIMIT_REASON,
    BlockedRenderedRequest,
    RenderedPage,
    RenderedPageFetcher,
)
from tailorcv.shared.errors import IngestError
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import pending_discovery


class _Static:
    def __init__(self, result: FetchedPosting | Exception) -> None:
        self.result = result
        self.intents: list[PostingReadIntent] = []

    def fetch(
        self,
        _url: str,
        *,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        max_elapsed_seconds: float | None = None,
        board_url: str = "",
    ) -> FetchedPosting:
        del max_elapsed_seconds, board_url
        self.intents.append(intent)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Rendered:
    def __init__(self, page: RenderedPage | Exception) -> None:
        self.page = page

    def fetch(self, _url: str, **_kwargs: object) -> RenderedPage:
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def _resolver(
    static: FetchedPosting | Exception, rendered: RenderedPage | Exception | None = None
) -> DeterministicPostingContentResolver:
    resolver = DeterministicPostingContentResolver.__new__(DeterministicPostingContentResolver)
    resolver.static = cast(Any, _Static(static))
    resolver.rendered = cast(Any, _Rendered(rendered)) if rendered else cast(Any, None)
    resolver.client = cast(Any, None)
    return resolver


def _body(title: str) -> str:
    details = "Build reliable systems and lead cross-functional delivery. " * 12
    return (
        f"<html><main><h1>{title}</h1><h2>Responsibilities</h2><p>{details}</p>"
        f"<h2>Qualifications</h2><p>{details}</p></main></html>"
    )


def test_structured_read_retains_explicit_title_contradiction() -> None:
    fetched = FetchedPosting(
        text="Different Role\n" + "Publisher description. " * 30,
        posting_status="active",
        method="Greenhouse API",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        title="Different Role",
        employer="Acme",
    )

    result = _resolver(fetched).resolve(
        "https://example.test/jobs/operations-lead",
        employer="Acme",
        title="Operations Lead",
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.identity_status is PostingIdentityStatus.CONTRADICTED
    assert result.identity_evidence == {
        "method": "Greenhouse API",
        "publisher_declaration": True,
        "expected_title": "Operations Lead",
        "publisher_title": "Different Role",
        "expected_employer": "Acme",
        "publisher_employer": "Acme",
    }
    # The fetch built no response (a scripted double), so no basis was ever known.
    assert result.robots_basis is None


def test_a_structured_read_carries_what_it_converted_to_the_resolved_content() -> None:
    """The deterministic resolver is the batch path, so retention has to reach it too."""
    source = PostingSource(
        publisher="greenhouse",
        record='{"title": "Operations Lead"}',
        description_markup="<p>Publisher description.</p>",
        description_paths=("content",),
    )
    fetched = FetchedPosting(
        text="Operations Lead\n" + "Publisher description. " * 30,
        posting_status="active",
        method="Greenhouse API",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        title="Operations Lead",
        employer="Acme",
        source=source,
    )

    result = _resolver(fetched).resolve(
        "https://example.test/jobs/operations-lead",
        employer="Acme",
        title="Operations Lead",
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.source == source


@pytest.mark.parametrize("method", ["Greenhouse API", "html"])
@pytest.mark.parametrize(
    ("closing_value", "passed"),
    [
        ("2026-08-27", True),
        ("2026-08-28", False),
        ("2026-08-28T15:00:00-07:00", False),
        ("2026-08-27T15:00:00", None),
        ("", None),
    ],
)
def test_a_static_read_retains_closing_facts_without_closing_the_posting(
    method: str, closing_value: str, passed: bool | None
) -> None:
    url = "https://example.test/jobs/operations-lead"
    final_url = "https://example.test/careers/operations-lead"
    observed_at = datetime(2026, 8, 28, 16, tzinfo=UTC)
    closing_date = publisher_closing_date(
        "",
        valid_through=closing_value,
        source_url=final_url,
        observed_at=observed_at,
    )
    fetched = FetchedPosting(
        text="Operations Lead\n" + "Publisher description. " * 30,
        posting_status="active",
        method=method,
        fetched_at=observed_at,
        final_url=final_url,
        title="Operations Lead",
        employer="Acme",
        page_classification=PageClassification.POSTING,
        publisher_closing_date=closing_date if closing_value else None,
    )

    result = _resolver(fetched).resolve(url, employer="Acme", title="Operations Lead")

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.posting_status is PostingStatus.ACTIVE
    assert fetched.text is not None
    assert result.content == fetched.text.encode("utf-8")
    assert result.publisher_closing_date is not None
    assert result.publisher_closing_date == closing_date
    assert result.publisher_closing_date.has_passed(observed_at) is passed
    assert ("Closing date passed; verify availability." in result.note) is (passed is True)


@pytest.mark.parametrize(
    ("expected_title", "expected_employer", "status"),
    [
        pytest.param(
            "Operations Lead", "Northwind", PostingIdentityStatus.VERIFIED, id="bare-name"
        ),
        pytest.param(
            "Operations Lead", "Globex", PostingIdentityStatus.CONTRADICTED, id="other-name"
        ),
        pytest.param(
            "Route Planner", "Northwind", PostingIdentityStatus.CONTRADICTED, id="other-title"
        ),
    ],
)
def test_a_legal_form_suffix_alone_does_not_contradict_the_employer(
    expected_title: str, expected_employer: str, status: PostingIdentityStatus
) -> None:
    """A board stores the bare name where the page states it with "Inc."."""
    url = "https://example.test/jobs/operations-lead"
    fetched = FetchedPosting(
        text="Operations Lead\n" + "Publisher description. " * 30,
        posting_status="active",
        method="Greenhouse API",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        title="Operations Lead",
        employer="Northwind Inc.",
        final_url=url,
    )

    result = _resolver(fetched).resolve(url, employer=expected_employer, title=expected_title)

    assert result.identity_status is status


_VERIFIED = PostingIdentityStatus.VERIFIED
_CONTRADICTED = PostingIdentityStatus.CONTRADICTED
_TITLE = "Product Manager - Population Health"


@pytest.mark.parametrize(
    ("row_employer", "publisher_employer", "row_title", "status"),
    [
        pytest.param("Globex", "Globex Analytics", _TITLE, _VERIFIED, id="row-short"),
        pytest.param("Globex Analytics", "Globex", _TITLE, _VERIFIED, id="publisher-short"),
        pytest.param(
            "Globex Health", "Globex Health Technologies", _TITLE, _VERIFIED, id="two-word-row"
        ),
        pytest.param(
            "Globex Health Technologies", "Globex Health", _TITLE, _VERIFIED, id="two-word-pub"
        ),
        pytest.param("Globex", "Globex Health Labs", _TITLE, _VERIFIED, id="two-descriptors"),
        pytest.param("Globex", "Globex Analytics Inc.", _TITLE, _VERIFIED, id="with-legal-form"),
        *[
            pytest.param(
                f"Globex Consulting{suffix}" if reverse else "Globex",
                "Globex" if reverse else f"Globex Consulting{suffix}",
                _TITLE,
                _VERIFIED,
                id=f"consulting-{legal_form}-{'publisher-short' if reverse else 'row-short'}",
            )
            for legal_form, suffix in [
                ("bare", ""),
                ("llc", " LLC"),
                ("dotted-llc", " L.L.C."),
                ("inc", " Inc."),
                ("corp", " Corp."),
                ("ltd", " Ltd."),
                ("trailing-comma", " LLC,"),
            ]
            for reverse in (False, True)
        ],
        pytest.param("globex", "GLOBEX ANALYTICS", _TITLE, _VERIFIED, id="case-folded"),
        pytest.param("Globex", "Globex", _TITLE, _VERIFIED, id="same-name"),
        pytest.param("Globex", "Initech", _TITLE, _CONTRADICTED, id="unrelated"),
        pytest.param("Globex", "Initech Analytics", _TITLE, _CONTRADICTED, id="other-stem"),
        pytest.param(
            "Globex", "Initech Consulting LLC", _TITLE, _CONTRADICTED, id="consulting-other-stem"
        ),
        pytest.param(
            "Globex", "Globexia Consulting LLC", _TITLE, _CONTRADICTED, id="consulting-stem-prefix"
        ),
        pytest.param(
            "Globex",
            "Globex Consulting Northwind LLC",
            _TITLE,
            _CONTRADICTED,
            id="consulting-unknown-suffix",
        ),
        pytest.param("Globex", "Globex Northwind", _TITLE, _CONTRADICTED, id="name-not-descriptor"),
        pytest.param("Health", "Health Northwind", _TITLE, _CONTRADICTED, id="generic-health"),
        pytest.param("Clinical", "Clinical Initech", _TITLE, _CONTRADICTED, id="generic-clinical"),
        pytest.param("Health", "Health Analytics", _TITLE, _CONTRADICTED, id="only-descriptors"),
        pytest.param(
            "Consulting", "Consulting Analytics LLC", _TITLE, _CONTRADICTED, id="generic-consulting"
        ),
        pytest.param(
            "Health",
            "Health Consulting LLC",
            _TITLE,
            _CONTRADICTED,
            id="consulting-only-descriptors",
        ),
        pytest.param("The", "The Globex", _TITLE, _CONTRADICTED, id="article-only"),
        pytest.param("The Globex", "Globex", _TITLE, _CONTRADICTED, id="leading-article"),
        pytest.param("Analytics Globex", "Globex", _TITLE, _CONTRADICTED, id="not-leading"),
        pytest.param(
            "Globex", "Globex Analytics", "Product Manager", _CONTRADICTED, id="other-title"
        ),
        pytest.param(
            "Globex",
            "Globex Consulting LLC",
            "Product Manager",
            _CONTRADICTED,
            id="consulting-other-title",
        ),
    ],
)
def test_a_trailing_descriptor_alone_does_not_contradict_the_employer(
    row_employer: str, publisher_employer: str, row_title: str, status: PostingIdentityStatus
) -> None:
    """A board stores "Globex" where the publisher states "Globex Analytics", or the reverse."""
    url = "https://example.test/jobs/product-manager"
    fetched = FetchedPosting(
        text=f"{_TITLE}\n" + "Publisher description. " * 30,
        posting_status="active",
        method="Greenhouse API",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        title=_TITLE,
        employer=publisher_employer,
        final_url=url,
    )

    result = _resolver(fetched).resolve(url, employer=row_employer, title=row_title)

    assert result.identity_status is status


def test_closure_signal_does_not_claim_verified_posting_identity() -> None:
    url = "https://example.test/jobs/operations-lead"
    fetched = FetchedPosting(
        text=None,
        posting_status="closed",
        method="closure_signal",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        http_status=404,
        final_url=url,
    )

    result = _resolver(fetched).resolve(
        url,
        employer="Acme",
        title="Operations Lead",
    )

    assert result.posting_status is PostingStatus.CLOSED
    assert result.identity_status is PostingIdentityStatus.NOT_YET_VERIFIED


@pytest.mark.parametrize(
    "html",
    [
        pytest.param(
            "<html><body><h1>Job not found</h1>"
            "<p>The job you requested was not found.</p></body></html>",
            id="job-not-found",
        ),
        pytest.param(
            '<html><body><main><div class="job-manager-info">'
            "This listing has expired.</div></main></body></html>",
            id="expired-listing",
        ),
        pytest.param(
            "<html><body><h1>Operations Lead</h1><p>We are not currently accepting "
            "applications for this position.</p></body></html>",
            id="closed-job-page",
        ),
    ],
)
def test_rendered_not_found_page_is_a_closure_not_another_render_requirement(html: str) -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=html,
        responses=(),
        document_status=200,
    )

    static = FetchedPosting(None, "active", "page extraction", datetime(2026, 8, 28, tzinfo=UTC))
    result = _resolver(static, page).resolve(
        url,
        employer="Acme",
        title="Operations Lead",
    )

    assert result.posting_status is PostingStatus.CLOSED
    assert result.content_status is PostingContentAccessStatus.NOT_YET_READ
    assert result.note == "The publisher reports that this posting is closed."


@pytest.mark.parametrize(
    ("html", "status"),
    [
        pytest.param(_body("Operations Lead"), PostingContentAccessStatus.READABLE, id="posting"),
        pytest.param(
            "<html><body><p>Loading.</p></body></html>",
            PostingContentAccessStatus.FETCH_FAILED,
            id="ambiguous",
        ),
    ],
)
def test_a_landing_page_kept_after_a_navigation_timeout_says_so_in_its_evidence(
    html: str, status: PostingContentAccessStatus
) -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=html,
        responses=(),
        document_status=200,
        navigation_timed_out=True,
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is status
    assert result.http_status == 200
    assert result.final_url == url
    assert (result.identity_evidence or {}).get("navigation_timed_out") is True


@pytest.mark.parametrize(
    ("reason", "named"),
    [(RESPONSE_BYTE_LIMIT_REASON, True), ("request_limit_reached", False)],
)
def test_an_empty_render_names_a_response_its_size_limits_refused(reason: str, named: bool) -> None:
    url = "https://example.test/jobs/operations-lead"
    bundle = BlockedRenderedRequest("https://example.test/static/app.js", "GET", "script", reason)
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html="<html><body><div id='root'></div></body></html>",
        responses=(),
        document_status=200,
        blocked_requests=(bundle,),
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is not PostingContentAccessStatus.READABLE
    assert ("over its size limits" in result.note) is named
    assert (result.identity_evidence or {}).get("byte_limit_refusals") == (1 if named else None)


def _resolved_rendered_posting_with(extra: str) -> ResolvedPostingContent:
    """Resolve a readable rendered posting whose main content ends with extra markup."""
    url = "https://example.test/jobs/operations-lead"
    html = _body("Operations Lead").replace("</main>", f"{extra}</main>")
    page = RenderedPage(
        requested_url=url, final_url=url, html=html, responses=(), document_status=200
    )
    return _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "We are not currently accepting applications from agencies.",
        "We are not currently accepting applications for this position from recruitment agencies.",
    ],
)
def test_a_rendered_posting_refusing_only_agency_applications_stays_open(sentence: str) -> None:
    result = _resolved_rendered_posting_with(f"<p>{sentence}</p>")

    assert result.posting_status is not PostingStatus.CLOSED
    assert result.content_status is PostingContentAccessStatus.READABLE


@pytest.mark.parametrize(
    "markup",
    [
        pytest.param(
            '<script>window.i18n = {"closed": "This job is no longer accepting '
            'applications."};</script>',
            id="script-catalog",
        ),
        pytest.param("<code><!-- This job is no longer available --></code>", id="html-comment"),
        pytest.param("<div hidden><p>This job is no longer available.</p></div>", id="hidden"),
        pytest.param(
            '<div style="display: none">This listing has expired.</div>', id="display-none"
        ),
        pytest.param("<template><p>Job not found</p></template>", id="template"),
    ],
)
def test_closure_words_no_reader_sees_leave_a_rendered_posting_open(markup: str) -> None:
    result = _resolved_rendered_posting_with(markup)

    assert result.posting_status is not PostingStatus.CLOSED
    assert result.content_status is PostingContentAccessStatus.READABLE


def test_a_visible_closure_sentence_closes_a_rendered_posting_with_a_body() -> None:
    result = _resolved_rendered_posting_with("<p>This job is no longer available.</p>")

    assert result.posting_status is PostingStatus.CLOSED


def test_structured_read_preserves_requested_and_final_urls_in_order() -> None:
    requested = "https://publisher.example/jobs/operations-lead"
    final = "https://jobs.publisher.example/openings/123"
    fetched = FetchedPosting(
        text="Operations Lead\n" + "Publisher description. " * 30,
        posting_status="active",
        method="publisher markup",
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        publisher_markup_present=True,
        title="Operations Lead",
        employer="Acme",
        final_url=final,
    )

    result = _resolver(fetched).resolve(
        requested,
        employer="Acme",
        title="Operations Lead",
    )

    assert result.requested_url == requested
    assert result.final_url == final
    assert result.identity_status is PostingIdentityStatus.VERIFIED


def test_rendered_posting_is_verified_against_its_publisher_heading() -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=_body("Operations Lead"),
        responses=(),
        document_status=200,
    )
    resolver = _resolver(IngestError("static shell"), page)

    result = resolver.resolve(url, employer="Acme", title="Operations Lead")

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.identity_status is PostingIdentityStatus.VERIFIED
    assert result.extraction_method is PostingContentExtractionMethod.RENDERED_HTML
    assert result.content is not None and b"Qualifications" in result.content


def test_rendered_paylocity_detail_retains_description_requirements_and_compensation(
    pytestconfig: pytest.Config, durable_database: Database, tmp_path: Path
) -> None:
    url = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456"
    html = (
        pytestconfig.rootpath / "tests/fixtures/board_responses/paylocity_detail.html"
    ).read_text(encoding="utf-8")
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=html,
        responses=(),
        document_status=200,
    )
    storage = runtime_storage(durable_database, tmp_path)
    workspace = WorkspaceService(durable_database, storage).create(
        slug="rendered-detail", name="Rendered detail"
    )
    pending = pending_discovery(
        workspace.id,
        employer="Globex",
        title="Operations Lead",
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    pending.discovery_url = pending.canonical_url = url
    with durable_database.session() as session:
        session.add(pending)
    service = PostingContentResolutionService(
        durable_database, _resolver(IngestError("static shell"), page), storage=storage
    )
    preflight = service.preflight(workspace.id, [pending.id])

    service.resolve_batch(
        workspace.id,
        [pending.id],
        preflight.snapshot_sha256,
        origin="discovery.refresh_batch",
    )

    with durable_database.session() as session:
        stored = session.get(PendingDiscovery, pending.id)
        assert stored is not None
        evidence = session.get(PendingPostingContent, stored.current_content_id)
        assert evidence is not None
        assert pending_discovery_readiness(stored, evidence) is PendingDiscoveryReadiness.READY
        assert evidence.identity_status == PostingIdentityStatus.VERIFIED.value
        assert evidence.extraction_method == PostingContentExtractionMethod.RENDERED_HTML.value
    content = service.current.read(workspace.id, pending.id)
    assert content is not None
    body = content.body.decode("utf-8")
    for text in (
        "Description",
        "Requirements",
        "Salary Description",
        "Coordinate the weekly delivery review",
        "Experience coordinating projects across several teams",
        "Compensation is discussed during the interview process.",
    ):
        assert text in body
    assert "View All Jobs" not in body
    assert "Apply" not in body


def _markup_body(title: str, employer: str) -> str:
    details = "Build reliable systems and lead cross-functional delivery. " * 12
    return (
        '<html><head><script type="application/ld+json">{"@type": "JobPosting", '
        f'"title": "{title}", "hiringOrganization": {{"name": "{employer}"}}, '
        f'"description": "{details}"}}</script></head>'
        f"<body><main><h1>{title}</h1></main></body></html>"
    )


def test_a_rendered_posting_declared_in_publisher_markup_is_recorded_as_a_json_read() -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=_markup_body("Operations Lead", "Acme"),
        responses=(),
        document_status=200,
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.publisher_markup_present is True
    assert result.extraction_method is PostingContentExtractionMethod.RENDERED_JSON


@pytest.mark.parametrize(
    ("html", "method"),
    [
        pytest.param(
            _body("Operations Lead"), PostingContentExtractionMethod.STATIC_HTML, id="page"
        ),
        pytest.param(
            _markup_body("Operations Lead", "Acme"),
            PostingContentExtractionMethod.STATIC_STRUCTURED,
            id="markup",
        ),
    ],
)
def test_a_landing_page_kept_after_a_navigation_timeout_is_recorded_as_a_static_read(
    html: str, method: PostingContentExtractionMethod
) -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=html,
        responses=(),
        document_status=200,
        navigation_timed_out=True,
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.extraction_method is method
    assert (result.identity_evidence or {}).get("navigation_timed_out") is True


def test_a_rendered_posting_with_no_heading_is_named_by_its_markup_title() -> None:
    url = "https://jobs.example.test/viewjob?jk=4f2a"
    details = "Build reliable systems and lead cross-functional delivery. " * 12
    html = (
        '<html><head><script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Operations Lead", "hiringOrganization": {"name": "Acme"}, '
        f'"description": "{details}"}}</script></head>'
        f"<body><main><div>{details}</div></main></body></html>"
    )
    page = RenderedPage(
        requested_url=url, final_url=url, html=html, responses=(), document_status=200
    )

    result = _resolver(IngestError("static shell"), page).resolve(url, employer="", title="")

    assert result.identity_status is PostingIdentityStatus.VERIFIED
    assert result.identity_evidence is not None
    assert result.identity_evidence["publisher_title"] == "Operations Lead"


@pytest.mark.parametrize(
    ("requested", "final", "status"),
    [
        (
            "https://www.linkedin.com/jobs/view/4398765210/",
            "https://www.linkedin.com/jobs/view/operations-lead-at-acme-4398765210",
            PostingIdentityStatus.VERIFIED,
        ),
        (
            "https://ca.linkedin.com/jobs/view/4398765210",
            "https://ca.linkedin.com/jobs/view/operations-lead-at-acme-4398765210/?trk=public",
            PostingIdentityStatus.VERIFIED,
        ),
        (
            "https://www.linkedin.com/jobs/view/operations-lead-at-acme-4398765210?refId=x",
            "https://www.linkedin.com/jobs/view/4398765210/",
            PostingIdentityStatus.VERIFIED,
        ),
        (
            "https://www.linkedin.com/jobs/view/4398765210/",
            "https://www.linkedin.com/jobs/view/operations-lead-at-acme-4466005677",
            PostingIdentityStatus.AMBIGUOUS,
        ),
    ],
)
def test_a_linkedin_job_view_is_one_page_across_its_slugged_redirect(
    requested: str, final: str, status: PostingIdentityStatus
) -> None:
    page = RenderedPage(
        requested_url=requested,
        final_url=final,
        html=_markup_body("Operations Lead", "Acme"),
        responses=(),
        document_status=200,
    )

    result = _resolver(IngestError("static shell"), page).resolve(requested, employer="", title="")

    assert result.identity_status is status


def test_a_rendered_read_names_every_declared_field_its_body_came_from() -> None:
    url = "https://example.test/jobs/operations-lead"
    node = {
        "@type": "JobPosting",
        "title": "Operations Lead",
        "hiringOrganization": {"name": "Acme"},
        "description": "Build reliable systems and lead cross-functional delivery. " * 12,
        "responsibilities": ["Run the weekly release review."],
    }
    html = f'<script type="application/ld+json">{json.dumps(node)}</script>'
    page = RenderedPage(
        requested_url=url, final_url=url, html=html, responses=(), document_status=200
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.source is not None
    assert result.source.description_paths == ("description", "responsibilities")
    assert result.source.description_markup.endswith("Run the weekly release review.")


@pytest.mark.parametrize("declares", [True, False])
def test_a_rendered_read_carries_the_rendered_page_and_any_node_it_declares(
    declares: bool,
) -> None:
    url = "https://example.test/jobs/operations-lead"
    html = _markup_body("Operations Lead", "Acme") if declares else _body("Operations Lead")
    page = RenderedPage(
        requested_url=url, final_url=url, html=html, responses=(), document_status=200
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content is not None and result.source is not None
    assert result.source.page_html == html
    if declares:
        node = json.loads(result.source.record)
        assert node["hiringOrganization"] == {"name": "Acme"}
        assert result.source.description_markup == node["description"].strip()
        assert result.source.description_paths == ("description",)
    else:
        assert (result.source.record, result.source.description_markup) == ("", "")


@pytest.mark.parametrize("declares", [True, False])
@pytest.mark.parametrize(
    ("closing_value", "body_label", "expected_value", "precision", "timezone", "clarify", "passed"),
    [
        ("2026-08-27", "", "2026-08-27", "date", None, False, True),
        ("2026-08-28", "", "2026-08-28", "date", None, False, False),
        (
            "2026-08-28T15:00:00-07:00",
            "",
            "2026-08-28T15:00:00-07:00",
            "timestamp",
            "-07:00",
            False,
            False,
        ),
        (
            "2026-08-27T15:00:00",
            "",
            "2026-08-27T15:00:00",
            "timestamp",
            None,
            False,
            None,
        ),
        ("2026-08-27", "Closing date: 2026-09-15", None, None, None, True, None),
        ("", "", None, None, None, False, None),
    ],
)
def test_a_rendered_read_retains_closing_statements_and_their_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    declares: bool,
    closing_value: str,
    body_label: str,
    expected_value: str | None,
    precision: str | None,
    timezone: str | None,
    clarify: bool,
    passed: bool | None,
) -> None:
    observed_at = datetime(2026, 8, 28, 16, tzinfo=UTC)

    class ObservedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ObservedDatetime:
            return cls.fromtimestamp(observed_at.timestamp(), tz=tz)

    monkeypatch.setattr(posting_resolution, "datetime", ObservedDatetime)
    url = "https://example.test/jobs/operations-lead"
    final_url = "https://example.test/careers/operations-lead"
    if declares:
        html = _markup_body("Operations Lead", "Acme")
        html = html.replace(
            '"description": "', f'"validThrough": "{closing_value}", "description": "'
        )
        html = html.replace("Build reliable systems", f"{body_label}<br>Build reliable systems", 1)
    else:
        label = f"Closing date: {closing_value}" if closing_value else ""
        html = _body("Operations Lead").replace(
            "</main>", f"<p>{label}</p><p>{body_label}</p></main>"
        )
    page = RenderedPage(
        requested_url=url, final_url=final_url, html=html, responses=(), document_status=200
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.posting_status is PostingStatus.ACTIVE
    assert result.content is not None
    fact = result.publisher_closing_date
    assert fact is not None
    assert fact.source_url == final_url
    assert fact.observed_at == observed_at
    assert fact.artifact_id is None
    assert fact.normalized_value == expected_value
    assert fact.precision == precision
    assert fact.timezone == timezone
    assert fact.needs_clarification is clarify
    assert fact.has_passed(observed_at) is passed
    assert ("Closing date passed; verify availability." in result.note) is (passed is True)
    assert len(fact.statements) == bool(closing_value) + bool(body_label)
    if closing_value:
        statement = fact.statements[0]
        assert statement.basis == ("valid_through" if declares else "body_label")
        assert statement.wording == (
            closing_value if declares else f"Closing date: {closing_value}"
        )
    if body_label:
        assert fact.statements[-1].wording == body_label


def test_a_rendered_read_records_the_basis_the_browser_document_was_read_under() -> None:
    """The rendered tier reads a known posting too, so it reports what robots granted."""
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=_body("Operations Lead"),
        responses=(),
        document_status=200,
        robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url,
        employer="Acme",
        title="Operations Lead",
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.robots_basis is RobotsReadBasis.SINGLE_POSTING_READ


def test_content_resolution_shares_one_deadline_across_static_and_rendered_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    budgets: list[tuple[str, float]] = []
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=_body("Operations Lead"),
        responses=(),
        document_status=200,
    )

    class SlowStatic:
        def fetch(
            self,
            _url: str,
            *,
            max_elapsed_seconds: float,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            budgets.append(("static", max_elapsed_seconds))
            now[0] = 40.0
            raise IngestError("static shell")

    class RemainingRendered:
        def fetch(
            self,
            _url: str,
            *,
            max_elapsed_seconds: float,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
        ) -> RenderedPage:
            del intent
            budgets.append(("rendered", max_elapsed_seconds))
            return page

    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_resolution.time.monotonic", lambda: now[0]
    )
    resolver = DeterministicPostingContentResolver.__new__(DeterministicPostingContentResolver)
    resolver.static = cast(Any, SlowStatic())
    resolver.rendered = cast(Any, RemainingRendered())
    resolver.client = cast(Any, None)

    result = resolver.resolve(url, employer="Acme", title="Operations Lead")

    assert budgets == [("static", 45.0), ("rendered", 5.0)]
    assert result.content_status is PostingContentAccessStatus.READABLE


def test_rendered_access_denial_is_not_reported_as_an_empty_source() -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html="<html><main><h1>Access denied</h1></main></html>",
        responses=(),
        document_status=403,
    )

    result = _resolver(IngestError("HTTP 403"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.ACCESS_BLOCKED
    assert result.content is None
    assert result.http_status == 403


def _robots_disallowed_single_read_client(page: str, requested: list[str]) -> PoliteHttpClient:
    def get(url: str, **_kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200, request=httpx.Request("GET", url), text="User-agent: *\nDisallow: /jobs/\n"
            )
        return httpx.Response(200, request=httpx.Request("GET", url), text=page)

    return PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _: None,
        getter=get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )


def test_single_intent_resolve_reads_a_robots_disallowed_posting_once() -> None:
    requested: list[str] = []
    page = (
        '<html><head><script type="application/ld+json">{"@type": "JobPosting", '
        '"title": "Staff Data Engineer", "hiringOrganization": {"name": "Example Health"}, '
        '"description": "' + "Build governed data pipelines for clinical analytics. " * 12 + '"}'
        "</script></head><body><main><h1>Staff Data Engineer</h1></main></body></html>"
    )
    resolver = DeterministicPostingContentResolver(
        client=_robots_disallowed_single_read_client(page, requested)
    )

    result = resolver.resolve(
        "https://careers.example.test/jobs/staff-data-engineer",
        employer="Example Health",
        title="Staff Data Engineer",
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.robots_basis is RobotsReadBasis.SINGLE_POSTING_READ
    assert requested.count("https://careers.example.test/jobs/staff-data-engineer") == 1


def test_the_rendered_tier_reads_as_a_crawl_even_for_a_single_posting_read() -> None:
    requested: list[str] = []
    url = "https://careers.example.test/jobs/staff-data-engineer"
    resolver = _resolver(IngestError("static shell"))
    resolver.rendered = RenderedPageFetcher(
        client=_robots_disallowed_single_read_client("<html></html>", requested)
    )

    result = resolver.resolve(
        url,
        employer="Example Health",
        title="Staff Data Engineer",
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.content_status is PostingContentAccessStatus.POLICY_REFUSED
    assert url not in requested


def _allowed_page_client(page: str) -> PoliteHttpClient:
    def get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200, request=httpx.Request("GET", url), text="User-agent: *\nAllow: /\n"
            )
        return httpx.Response(200, request=httpx.Request("GET", url), text=page)

    return PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _: None,
        getter=get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )


def test_an_h1_only_publisher_page_carries_its_heading_as_the_static_title() -> None:
    """No JSON-LD names the posting, so the page's own heading has to."""
    url = "https://careers.example.test/jobs/staff-data-engineer"
    resolver = DeterministicPostingContentResolver(
        client=_allowed_page_client(_body("Staff Data Engineer"))
    )

    result = resolver.resolve(url, employer="Example Health", title="Staff Data Engineer")

    assert result.content_status is PostingContentAccessStatus.READABLE
    assert result.identity_status is PostingIdentityStatus.VERIFIED
    assert result.extraction_method is PostingContentExtractionMethod.STATIC_HTML


def test_truncated_rendered_posting_never_becomes_usable_content() -> None:
    url = "https://example.test/jobs/operations-lead"
    page = RenderedPage(
        requested_url=url,
        final_url=url,
        html=_body("Operations Lead"),
        responses=(),
        document_status=200,
        html_truncated=True,
    )

    result = _resolver(IngestError("static shell"), page).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.RENDER_REQUIRED
    assert result.identity_status is PostingIdentityStatus.AMBIGUOUS
    assert result.content is None
    assert result.extraction_method is None
    assert result.identity_evidence == {
        "html_truncated": True,
        "blocked_request_count": 0,
        "blocked_request_overflow_count": 0,
        "deferred_response_count": 0,
    }


class _NeverRendered:
    def fetch(self, url: str, **_kwargs: object) -> RenderedPage:
        raise AssertionError(f"a refused API-only posting must not be rendered: {url}")


def _public_resolver(_host: str, port: int) -> list[tuple[object, ...]]:
    return [(AF_INET, SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_a_workday_api_refusal_is_access_blocked_without_a_rendered_read() -> None:
    url = "https://globex.wd1.myworkdayjobs.com/en-US/Careers/job/Product-Owner_R10023"
    requested: list[str] = []

    def get(target: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", target)
        if target.endswith("/robots.txt"):
            return httpx.Response(200, text="", request=request)
        requested.append(target)
        return httpx.Response(403, json={"errorCode": "S22"}, request=request)

    def post(target: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", target)
        return httpx.Response(403, json={"errorCode": "S22"}, request=request)

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        getter=get,
        poster=post,
        resolver=_public_resolver,
        clock=lambda: 100.0,
    )
    resolver = DeterministicPostingContentResolver(
        client=client, rendered=cast(Any, _NeverRendered())
    )

    result = resolver.resolve(url, employer="Globex", title="Product Owner")

    # A coded refusal checks the career page for another tenant, then tries the
    # posting page once; the browser tier never runs.
    assert requested == [
        "https://globex.wd1.myworkdayjobs.com/wday/cxs/globex/Careers/job/Product-Owner_R10023",
        "https://globex.wd1.myworkdayjobs.com/Careers",
        url,
    ]
    assert result.content_status is PostingContentAccessStatus.ACCESS_BLOCKED
    assert result.http_status == 403
    assert result.note == (
        "Workday returned HTTP 403, errorCode S22, and the site's own search could not "
        "confirm it (the search for R10023 answered HTTP 403)."
    )


def test_a_static_tier_resolver_miss_is_a_retryable_read_failure() -> None:
    """The fetcher reports the miss as a bodiless read, and the tier keeps it retryable."""
    url = "https://example.test/jobs/operations-lead"
    fetched = FetchedPosting(
        text=None,
        posting_status="unverified",
        method=UNRESOLVED_DESTINATION_METHOD,
        fetched_at=datetime(2026, 8, 28, tzinfo=UTC),
        final_url=url,
        verification_note=UNRESOLVED_DESTINATION_NOTE,
    )
    resolver = _resolver(fetched)
    resolver.rendered = cast(Any, _NeverRendered())

    result = resolver.resolve(url, employer="Acme", title="Operations Lead")

    assert result.content_status is PostingContentAccessStatus.FETCH_FAILED
    assert result.note == UNRESOLVED_DESTINATION_NOTE
    assert result.identity_evidence == {"method": UNRESOLVED_DESTINATION_METHOD}


def test_a_rendered_tier_resolver_miss_is_not_recorded_as_a_policy_refusal() -> None:
    url = "https://example.test/jobs/operations-lead"

    result = _resolver(
        IngestError("no static body"), DestinationUnresolvableError("no address")
    ).resolve(url, employer="Acme", title="Operations Lead")

    assert result.content_status is PostingContentAccessStatus.FETCH_FAILED
    assert result.note == UNRESOLVED_DESTINATION_NOTE


def _redirecting_client(location: str, target_robots: httpx.Response) -> PoliteHttpClient:
    """Answer the posting with a redirect and the target host's robots.txt as given."""

    def get(target: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", target)
        if target == "https://careers.example.test/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
        if target.endswith("/robots.txt"):
            return httpx.Response(
                target_robots.status_code, text=target_robots.text, request=request
            )
        if target.startswith("https://careers.example.test/"):
            return httpx.Response(302, headers={"Location": location}, request=request)
        return httpx.Response(200, text=_body("Operations Lead"), request=request)

    return PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        getter=get,
        resolver=_public_resolver,
        clock=lambda: 100.0,
    )


def test_a_rendered_redirect_whose_target_robots_is_unreadable_is_a_retryable_read_failure() -> (
    None
):
    resolver = DeterministicPostingContentResolver(
        client=_redirecting_client(
            "https://jobs.example.test/operations-lead", httpx.Response(503, text="")
        )
    )

    result = resolver.resolve(
        "https://careers.example.test/jobs/operations-lead",
        employer="Acme",
        title="Operations Lead",
    )

    assert result.content_status is PostingContentAccessStatus.FETCH_FAILED
    assert result.note == (
        "The publisher's robots policy could not be read, and the read remains retryable."
    )


@pytest.mark.parametrize(
    ("location", "target_robots", "note"),
    [
        (
            "https://jobs.example.test/operations-lead",
            httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
            "Published fetch policy refused the rendered read.",
        ),
        (
            "https://jobs.example.test:99999/operations-lead",
            httpx.Response(200, text=""),
            "The publisher's address, or a redirect it answered, is not a fetchable "
            "public HTTP(S) URL.",
        ),
    ],
    ids=["robots-disallowed-redirect-target", "malformed-redirect-target"],
)
def test_a_rendered_redirect_the_fetch_policy_refuses_is_a_policy_refusal(
    location: str, target_robots: httpx.Response, note: str
) -> None:
    resolver = DeterministicPostingContentResolver(
        client=_redirecting_client(location, target_robots)
    )

    result = resolver.resolve(
        "https://careers.example.test/jobs/operations-lead",
        employer="Acme",
        title="Operations Lead",
    )

    assert result.content_status is PostingContentAccessStatus.POLICY_REFUSED
    assert result.note == note


def test_a_host_wait_longer_than_the_rendered_read_allows_is_a_retryable_read_failure() -> None:
    url = "https://example.test/jobs/operations-lead"

    result = _resolver(IngestError("no static body"), HostHeldError(url, ready_at=160.0)).resolve(
        url, employer="Acme", title="Operations Lead"
    )

    assert result.content_status is PostingContentAccessStatus.FETCH_FAILED
    assert result.note == (
        "The publisher asked for a longer wait than this read allows, and the read "
        "remains retryable."
    )
