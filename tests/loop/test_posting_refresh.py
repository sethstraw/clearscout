from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from socket import gaierror
from typing import Any, Literal, cast

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tailorcv.application import job_search
from tailorcv.application.config import Settings
from tailorcv.application.contracts import ApplicationEventInput
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.posting_fetch import FetchedPosting
from tailorcv.application.discovery.posting_refresh import (
    PostingRefreshService,
    PostingRefreshWithReextractionHandler,
)
from tailorcv.application.discovery.posting_text import publisher_closing_date
from tailorcv.application.discovery.refresh_decision import (
    PreviousObservation,
    RefreshDecision,
    decide_refresh,
)
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.job_search.postings import RequirementRulingService
from tailorcv.application.tasks import (
    TaskContext,
    TaskQueueService,
    TaskResponse,
    task_types,
    validate_task_payload,
)
from tailorcv.application.workspaces import LearningLoopPolicyService, WorkspaceService
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.bootstrap.worker import TaskCancelledError, Worker, default_handlers
from tailorcv.domain import ApplicationStatus, utc_now
from tailorcv.domain.artifacts import PostingSource
from tailorcv.domain.identity import normalize_url
from tailorcv.domain.policy import LearningLoopPolicy
from tailorcv.domain.records import PostingReadIntent, RobotsReadBasis
from tailorcv.domain.rulings import Ruling
from tailorcv.domain.staged_facts import PublisherClosingDate
from tailorcv.infrastructure.artifacts.store import file_uri_path
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    ApplicationEvent,
    Artifact,
    AuditEvent,
    CostRecord,
    EligibilityRecord,
    Employer,
    JobPosting,
    ModelCall,
    PendingDiscovery,
    PostingObservation,
    PostingObservationDiff,
    PostingRequirement,
    PostingUrl,
    RequirementRuling,
    Workspace,
)
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.web_fetch.ingest import url as url_ingest
from tailorcv.infrastructure.web_fetch.ingest.url import (
    PublisherPostingUnverifiedError,
    UrlPostingText,
    check_posting,
)
from tailorcv.infrastructure.web_fetch.policy import (
    UNRESOLVED_DESTINATION_NOTE,
    PoliteHttpClient,
    RobotsDeniedError,
)
from tailorcv.infrastructure.web_fetch.posting_fetch import (
    UNRESOLVED_DESTINATION_METHOD,
    PolitePostingFetcher,
)
from tailorcv.shared.costs import Ledger
from tailorcv.shared.errors import IngestError
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.doubles import ScriptedProvider
from tests.support.http import (
    DAYFORCE_POSTING_URL,
    CountingFeedClient,
    dayforce_feed_row,
    policy_client,
)
from tests.support.scenarios import evaluation_commit
from tests.support.scenarios import fit_report as _fit_report


class ScriptedFetcher:
    """Deterministic posting source that reaches no network."""

    def __init__(self, results: list[FetchedPosting]) -> None:
        self.results = results
        self.urls: list[str] = []
        self.intents: list[PostingReadIntent] = []

    def fetch(
        self,
        url: str,
        *,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> FetchedPosting:
        self.urls.append(url)
        self.intents.append(intent)
        return self.results.pop(0)


def _job(
    database: Database,
    artifact_root: Path,
    *,
    observed_at: object | None = None,
    extraction_method: str | None = None,
) -> tuple[str, str]:
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="refresh", name="Refresh"
    )
    proposal = evaluation_commit(
        workspace.id,
        **({"observed_at": observed_at} if observed_at is not None else {}),
    )
    if extraction_method is not None:
        proposal = proposal.model_copy(
            update={
                "posting_body": proposal.posting_body.model_copy(
                    update={"extraction_method": extraction_method}
                )
            }
        )
    job = job_search.commit_evaluation(
        database, artifact_service(database, artifact_root), proposal
    )
    return workspace.id, job.job_posting_id


def _run_refresh(
    database: Database,
    artifact_root: Path,
    fetcher: ScriptedFetcher,
    workspace_id: str,
    job_id: str,
    key: str,
) -> TaskResponse:
    queue = TaskQueueService(database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh",
        payload={"job_posting_id": job_id},
        idempotency_key=key,
    )
    worker = Worker(
        database,
        worker_id="refresh-worker",
        handlers=default_handlers(database, artifact_root=artifact_root, fetcher=fetcher),
    )
    assert worker.run_once()
    return queue.get(task.id)


def test_shared_registry_validates_all_production_payloads() -> None:
    assert task_types() == (
        "artifact.verify",
        "posting.refresh",
        "posting.refresh_batch",
        "posting.refresh_with_reextraction",
        "posting.refresh_with_rematch",
        "posting.recover_batch",
        "discovery.evaluate",
        "discovery.evaluate_batch",
        "discovery.refresh_batch",
        "discovery.rescreen_queue",
        "discovery.relevance_review",
        "workflow.tailor",
        "workflow.approve",
        "workflow.batch",
        "workflow.discovery",
        "document.render",
        "source.scan",
        "source.continue",
        "search.guided_campaign",
        "search.guided_campaign_retry",
        "search.vocabulary",
    )
    assert validate_task_payload("discovery.rescreen_queue", {}) == {}
    with pytest.raises(ValidationError, match="Extra inputs"):
        validate_task_payload("discovery.rescreen_queue", {"pending_discovery_ids": ["p1"]})
    assert validate_task_payload("source.scan", {}) == {"source_ids": []}
    assert validate_task_payload("source.scan", {"source_ids": ["source-1"]}) == {
        "source_ids": ["source-1"]
    }
    with pytest.raises(ValidationError, match="must be unique"):
        validate_task_payload("source.scan", {"source_ids": ["source-1", "source-1"]})
    with pytest.raises(ValidationError, match="at least 1 character"):
        validate_task_payload("source.scan", {"source_ids": [""]})
    assert validate_task_payload("posting.refresh", {"job_posting_id": "job-1"}) == {
        "job_posting_id": "job-1"
    }
    assert validate_task_payload("posting.refresh_with_rematch", {"job_posting_id": "job-1"}) == {
        "job_posting_id": "job-1"
    }
    with pytest.raises(ValidationError, match="Unsupported task type"):
        validate_task_payload("unknown", {})
    with pytest.raises(ValidationError, match="exactly one"):
        validate_task_payload("posting.refresh_batch", {})


def test_relevance_review_reviews_the_whole_selection_without_a_reuse_flag() -> None:
    """Every readable selected row is reviewed, so no payload chooses between two passes."""
    assert validate_task_payload(
        "discovery.relevance_review", {"pending_discovery_ids": ["pending-1"]}
    ) == {"pending_discovery_ids": ["pending-1"]}
    with pytest.raises(ValidationError, match="force_review"):
        validate_task_payload(
            "discovery.relevance_review",
            {"pending_discovery_ids": ["pending-1"], "force_review": True},
        )


def test_production_fetcher_respects_robots_honest_identity_and_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities: list[str] = []

    def robots_get(url: str, **kwargs: object) -> httpx.Response:
        headers = kwargs["headers"]
        assert isinstance(headers, Mapping)
        identities.append(str(headers["User-Agent"]))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: *\nAllow: /jobs/\n",
        )

    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.url_ingest.from_url",
        lambda url, client, **kwargs: (
            client.get(url),
            UrlPostingText(text="A" * 300, origin=url, method="scripted page"),
        )[1],
    )
    pauses: list[float] = []
    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=pauses.append,
        jitter=lambda low, high: 0.5,
        getter=robots_get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
        clock=lambda: 100.0,
    )
    fetcher = PolitePostingFetcher(client=client)
    result = fetcher.fetch("https://example.test/jobs/1")
    assert result.posting_status == "active"
    assert pauses == [0.5]
    assert identities == ["tailorcv/test", "tailorcv/test"]

    denied_client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: *\nDisallow: /jobs/\n",
        ),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    denied = PolitePostingFetcher(client=denied_client)
    with pytest.raises(ValidationError, match="robots.txt"):
        denied.fetch("https://denied.test/jobs/1")


def test_a_host_that_answers_no_address_is_a_retryable_bodiless_read() -> None:
    """Every caller of the fetcher sees a read outcome, not an escaping policy error."""

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        del host, port
        raise gaierror("synthetic DNS failure")

    fetcher = PolitePostingFetcher(
        client=PoliteHttpClient(
            user_agent="tailorcv/test",
            getter=lambda url, **kwargs: pytest.fail(f"unresolved URL was contacted: {url}"),
            resolver=resolve,
        )
    )

    result = fetcher.fetch("https://missing.test/jobs/1")

    assert result.method == UNRESOLVED_DESTINATION_METHOD
    assert result.text is None
    assert result.verification_note == UNRESOLVED_DESTINATION_NOTE


_POSTING_PAGE = (
    '<html><head><script type="application/ld+json">{"@type": "JobPosting", '
    '"title": "Staff Data Engineer", "hiringOrganization": {"name": "Example Health"}, '
    '"description": "' + "Build governed data pipelines for clinical analytics. " * 12 + '"}'
    "</script></head><body><main><h1>Staff Data Engineer</h1></main></body></html>"
)


def _robots_disallowed_host(
    page: str,
    requested: list[str],
    *,
    status: int = 200,
    robots: str = "User-agent: *\nDisallow: /jobs/\n",
) -> PoliteHttpClient:
    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, request=httpx.Request("GET", url), text=robots)
        return httpx.Response(status, request=httpx.Request("GET", url), text=page)

    return PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _: None,
        getter=get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )


def test_a_single_posting_read_retains_a_robots_disallowed_posting_with_its_basis() -> None:
    requested: list[str] = []
    fetcher = PolitePostingFetcher(client=_robots_disallowed_host(_POSTING_PAGE, requested))

    result = fetcher.fetch(
        "https://careers.example.test/jobs/staff-data-engineer",
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.posting_status == "active"
    assert result.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ
    assert result.page_classification == "posting"
    assert requested.count("https://careers.example.test/jobs/staff-data-engineer") == 1


_COUNTRY_SCOPED_PAGE = (
    '<html><head><script type="application/ld+json">{"@type": "JobPosting", '
    '"title": "Staff Data Engineer", "hiringOrganization": {"name": "Example Health"}, '
    '"jobLocationType": "TELECOMMUTE", '
    '"applicantLocationRequirements": {"@type": "Country", "name": "Canada"}, '
    '"description": "' + "Build governed data pipelines for operational analytics. " * 12 + '"}'
    "</script></head><body><main><h1>Staff Data Engineer</h1></main></body></html>"
)


def test_a_refresh_retains_the_publisher_node_and_page_beside_the_body_it_extracted(
    durable_database: Database, tmp_path: Path
) -> None:
    """Extracted text drops the node typing, so the read keeps what it converted."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=PolitePostingFetcher(
            client=_robots_disallowed_host(_COUNTRY_SCOPED_PAGE, [], robots="")
        ),
    )

    result = service.refresh(workspace_id, job_id)

    with durable_database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        rows = session.scalars(
            select(Artifact).where(
                Artifact.related_entity_type == "posting_observation",
                Artifact.related_entity_id == result["observation_id"],
            )
        ).all()
        files = {row.logical_type: row for row in rows}
        paths = {kind: file_uri_path(row.storage_uri) for kind, row in files.items()}
        contents = {kind: service.artifacts.read(row, workspace) for kind, row in files.items()}

    node = json.loads(contents["posting.source.json"])
    assert node["applicantLocationRequirements"]["@type"] == "Country"
    assert contents["posting.body.html"].decode("utf-8") == node["description"].strip()
    assert contents["posting.source.html"].decode("utf-8") == _COUNTRY_SCOPED_PAGE
    assert {kind: path.name for kind, path in paths.items() if path is not None} == {
        "posting.body.text": "body.txt",
        "posting.source.json": "raw.json",
        "posting.body.html": "description.html",
        "posting.source.html": "page.html",
    }
    assert len({path.parent for path in paths.values() if path is not None}) == 1


def test_a_single_read_redirected_to_a_retryable_host_reads_the_target_once() -> None:
    """A 503 with no Retry-After is not retried by ingest once the exempt basis is stamped."""
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url == "https://careers.example.test/robots.txt":
            return httpx.Response(
                200, request=httpx.Request("GET", url), text="User-agent: *\nDisallow: /jobs/\n"
            )
        if url == "https://other.example.test/robots.txt":
            return httpx.Response(
                200, request=httpx.Request("GET", url), text="User-agent: *\nAllow: /\n"
            )
        if url == "https://careers.example.test/jobs/staff-data-engineer":
            return httpx.Response(
                301,
                request=httpx.Request("GET", url),
                headers={"Location": "https://other.example.test/roles/staff-data-engineer"},
            )
        return httpx.Response(503, request=httpx.Request("GET", url))

    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _: None,
        getter=get,
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    fetcher = PolitePostingFetcher(client=client)

    with pytest.raises(IngestError, match="503"):
        fetcher.fetch(
            "https://careers.example.test/jobs/staff-data-engineer",
            intent=PostingReadIntent.SINGLE_POSTING,
        )

    assert requested.count("https://other.example.test/roles/staff-data-engineer") == 1


def test_a_single_read_classifies_a_listing_page_as_a_collection() -> None:
    links = "".join(
        f'<li><a href="/jobs/role-{index}">Senior Data Engineer {index}</a></li>'
        for index in range(6)
    )
    listing = (
        "<html><body><main><h1>Open roles</h1><ul>"
        + links
        + "</ul><p>"
        + "Explore careers across our clinical data teams. " * 10
        + "</p></main></body></html>"
    )
    fetcher = PolitePostingFetcher(client=_robots_disallowed_host(listing, []))

    result = fetcher.fetch(
        "https://careers.example.test/jobs/search", intent=PostingReadIntent.SINGLE_POSTING
    )

    assert result.page_classification == "collection"


def test_a_forbidden_single_read_ends_without_another_request() -> None:
    requested: list[str] = []
    fetcher = PolitePostingFetcher(
        client=_robots_disallowed_host("forbidden", requested, status=403)
    )

    with pytest.raises(IngestError, match="403"):
        fetcher.fetch(
            "https://careers.example.test/jobs/staff-data-engineer",
            intent=PostingReadIntent.SINGLE_POSTING,
        )

    assert requested.count("https://careers.example.test/jobs/staff-data-engineer") == 1


def test_a_single_posting_read_named_for_this_client_stays_a_policy_refusal() -> None:
    """A robots group written for this client refuses even the single-posting exemption.

    The read itself hits ``RobotsDeniedError``, not just the precheck, so the
    fetcher must translate the read's own denial into ``ValidationError`` too.
    """
    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        sleeper=lambda _: None,
        getter=lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: tailorcv\nDisallow: /\n",
        ),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    fetcher = PolitePostingFetcher(client=client)

    with pytest.raises(ValidationError, match="robots.txt") as excinfo:
        fetcher.fetch("https://named.example.test/jobs/1", intent=PostingReadIntent.SINGLE_POSTING)

    assert isinstance(excinfo.value.__cause__, RobotsDeniedError)


def test_a_robots_blocked_human_url_still_reads_through_a_permitted_publisher_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board answers on a permitted API path even when its human posting URL is robots-blocked."""
    # from_url stands in for the platform readers that answer on the permitted API path.
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.url_ingest.from_url",
        lambda url, client, **kwargs: UrlPostingText(
            text="Director of Product Management. " + "A" * 300,
            origin=url,
            method="Workday API",
        ),
    )
    denied_client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: *\nDisallow: /job/\n",
        ),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    result = PolitePostingFetcher(client=denied_client).fetch(
        "https://example.wd1.myworkdayjobs.com/site/job/req-1"
    )

    assert result.posting_status == "active"
    assert result.text is not None
    assert result.text.startswith("Director of Product Management")


def test_a_robots_refusal_on_an_api_only_board_stays_a_policy_refusal() -> None:
    """The board's API and its page are both disallowed, so nothing was refused by the publisher."""
    denied_client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: *\nDisallow: /\n",
        ),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )

    with pytest.raises(ValidationError, match="robots.txt") as excinfo:
        PolitePostingFetcher(client=denied_client).fetch(
            "https://globex.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Data-Manager_R-1"
        )

    assert isinstance(excinfo.value.__cause__, RobotsDeniedError)


def test_a_workable_short_link_missing_from_a_populated_board_is_closed() -> None:
    url = "https://apply.workable.com/j/AB12CD34EF"
    feed_url = "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"

    def response(target: str) -> httpx.Response:
        assert target == feed_url
        return httpx.Response(
            200,
            request=httpx.Request("GET", target),
            json={"jobs": [{"shortcode": "OTHER", "title": "Other Role"}]},
        )

    result = PolitePostingFetcher(client=policy_client(response)).fetch(
        url,
        board_url="https://apply.workable.com/acme",
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.posting_status == "closed"


def test_recognized_greenhouse_posting_that_cannot_be_verified_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic or 404 hosted page is neither usable text nor closure evidence."""

    def unreadable(url: str, client: PoliteHttpClient, **kwargs: object) -> UrlPostingText:
        raise PublisherPostingUnverifiedError(
            "Greenhouse could not verify this hosted posting because its board identifier was "
            "no longer available."
        )

    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.url_ingest.from_url", unreadable
    )
    client = PoliteHttpClient(
        user_agent="tailorcv/test",
        getter=lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="User-agent: *\nAllow: /\n",
        ),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )

    result = PolitePostingFetcher(client=client).fetch(
        "https://careers.example.test/roles/open?gh_jid=1234567890"
    )

    assert result.posting_status == "unverified"
    assert result.method == "publisher_unverified"
    assert "could not verify" in result.verification_note


def test_the_fetcher_builds_its_own_client_with_the_honest_identity_and_seams() -> None:
    """The injected-client tests cannot see this wiring, and production uses only this."""
    pauses: list[float] = []
    fetcher = PolitePostingFetcher(sleeper=pauses.append, jitter=lambda low, high: 0.25)

    assert fetcher.client.user_agent == url_ingest.USER_AGENT
    fetcher.client.sleeper(fetcher.client.jitter(0.1, 0.9))
    assert pauses == [0.25]


@pytest.mark.parametrize(
    ("valid_through", "normalized", "precision", "timezone", "needs_clarification", "passed"),
    [
        ("2026-09-01", "2026-09-01", "date", None, False, True),
        ("2026-09-03", "2026-09-03", "date", None, False, False),
        (
            "2026-09-01T23:59:00-06:00",
            "2026-09-01T23:59:00-06:00",
            "timestamp",
            "-06:00",
            False,
            True,
        ),
        ("2026-09-01T23:59:00Z", "2026-09-01T23:59:00+00:00", "timestamp", "+00:00", False, True),
        ("2026-09-01T23:59:00", "2026-09-01T23:59:00", "timestamp", None, False, False),
        ("not a date", None, None, None, True, False),
    ],
)
def test_a_readable_posting_retains_the_publishers_date_without_inferring_closure(
    monkeypatch: pytest.MonkeyPatch,
    valid_through: str,
    normalized: str | None,
    precision: str | None,
    timezone: str | None,
    needs_clarification: bool,
    passed: bool,
) -> None:
    fetched_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.utc_now",
        lambda: fetched_at,
    )
    source = PostingSource(
        publisher="page",
        record=json.dumps({"validThrough": valid_through}),
        page_html="<html><body>Senior Data Analyst</body></html>",
    )
    posting = UrlPostingText(
        text="A" * 400,
        origin="https://example.test/jobs/expired",
        method="publisher markup",
        publisher_markup_present=True,
        valid_through=valid_through,
        source=source,
    )
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.url_ingest.from_url",
        lambda url, client, **kwargs: posting,
    )

    result = PolitePostingFetcher(client=policy_client(lambda url: httpx.Response(200))).fetch(
        posting.origin
    )

    assert result.posting_status == "active"
    assert result.method == posting.method
    assert result.http_status == 200
    assert result.text == posting.text
    assert result.source == source
    closing_date = result.publisher_closing_date
    assert closing_date is not None
    assert closing_date.source_url == posting.origin
    assert closing_date.observed_at == fetched_at
    assert closing_date.normalized_value == normalized
    assert closing_date.precision == precision
    assert closing_date.timezone == timezone
    assert closing_date.needs_clarification is needs_clarification
    assert closing_date.statements[0].wording == valid_through
    assert closing_date.statements[0].basis == "valid_through"
    assert result.verification_note == (
        "Closing date passed; verify availability." if passed else ""
    )


@pytest.mark.parametrize(
    "fetched_at",
    [
        datetime(2021, 10, 29, 12, tzinfo=UTC),
        datetime(2021, 10, 30, 12, tzinfo=UTC),
    ],
)
def test_a_readable_static_posting_retains_its_stated_end_date(
    monkeypatch: pytest.MonkeyPatch,
    fetched_at: datetime,
) -> None:
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.utc_now",
        lambda: fetched_at,
    )
    page = (
        "<html><body><main><h1>Senior Data Analyst</h1><p>Example Co</p>"
        "<p>Posting End Date: 29-OCT-2021</p>"
        "<p>Build dashboards for our field teams and own the reporting model. "
        "Support operations with careful analysis and documented data quality checks.</p>"
        "<p>Work with regional teams to reconcile incoming datasets, publish repeatable "
        "reports, and explain changes in operational metrics to colleagues.</p>"
        "<p>Maintain shared reporting definitions and review monthly data with partner "
        "teams. Document assumptions, investigate missing records, and support "
        "consistent reporting across projects with clear written procedures.</p>"
        "</main></body></html>"
    )
    client = policy_client(
        lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=page)
    )

    result = PolitePostingFetcher(client=client).fetch("https://example.test/jobs/ended")

    assert (result.posting_status, result.method) == ("active", "page extraction")
    assert result.text is not None
    assert "Posting End Date: 29-OCT-2021" in result.text
    assert result.http_status == 200
    assert result.source is not None
    assert result.source.page_html == page
    closing_date = result.publisher_closing_date
    assert closing_date is not None
    assert closing_date.normalized_value == "2021-10-29"
    assert closing_date.precision == "date"
    assert closing_date.timezone is None
    assert closing_date.statements[0].wording == "Posting End Date: 29-OCT-2021"
    assert closing_date.statements[0].basis == "body_label"
    assert closing_date.source_url == result.final_url
    assert closing_date.observed_at == fetched_at
    assert result.verification_note == (
        "Closing date passed; verify availability."
        if fetched_at.date() > datetime(2021, 10, 29).date()
        else ""
    )


@pytest.mark.parametrize(
    "notice",
    [
        "<p>This job is no longer available.</p>",
        "<p>This position has been filled.</p>",
        "<p>Applications have closed.</p>",
        "<p>This listing has expired.</p>",
    ],
)
def test_a_short_publisher_notice_returns_a_closed_bodiless_posting(
    monkeypatch: pytest.MonkeyPatch, notice: str
) -> None:
    fetched_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.utc_now", lambda: fetched_at
    )
    page = f"<html><body><main>{notice}</main></body></html>"
    client = policy_client(
        lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=page)
    )

    result = PolitePostingFetcher(client=client).fetch("https://example.test/jobs/closed")

    assert result.posting_status == "closed"
    assert result.method == "closure_signal"
    assert result.text is None
    assert result.final_url == "https://example.test/jobs/closed"
    assert result.fetched_at == fetched_at


@pytest.mark.parametrize(
    "page",
    [
        "<html><body><p>Please sign in to continue.</p></body></html>",
        "<html><body><!-- This job is no longer available. --><p>Please sign in.</p></body></html>",
        '<html><body><script>const notice = "This job is no longer available.";</script>'
        "<p>Please sign in.</p></body></html>",
        "<html><body><p hidden>This job is no longer available.</p>"
        "<p>Please sign in.</p></body></html>",
        '<html><body><p style="display: none">This job is no longer available.</p>'
        "<p>Please sign in.</p></body></html>",
    ],
)
def test_a_short_page_without_a_visible_closure_stays_unreadable(page: str) -> None:
    client = policy_client(
        lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=page)
    )

    with pytest.raises(IngestError, match="too little to tailor"):
        PolitePostingFetcher(client=client).fetch("https://example.test/jobs/unreadable")


@pytest.mark.parametrize(
    "concealed",
    [
        "<!-- This job is no longer available. -->",
        '<script>const notice = "This job is no longer available.";</script>',
        "<p hidden>This job is no longer available.</p>",
        '<p style="display: none">This job is no longer available.</p>',
    ],
)
def test_a_concealed_closure_cannot_close_a_readable_posting(concealed: str) -> None:
    page = (
        "<html><body><main><h1>Senior Data Analyst</h1>"
        "<p>Build dashboards for our field teams and own the reporting model. "
        "Support operations with careful analysis and documented data quality checks.</p>"
        "<p>Work with regional teams to reconcile incoming datasets, publish repeatable "
        "reports, and explain changes in operational metrics to colleagues.</p>"
        "<p>Maintain shared reporting definitions and review monthly data with partner "
        "teams. Document assumptions, investigate missing records, and support "
        "consistent reporting across projects with clear written procedures.</p>"
        f"{concealed}</main></body></html>"
    )
    client = policy_client(
        lambda url: httpx.Response(200, request=httpx.Request("GET", url), text=page)
    )

    result = PolitePostingFetcher(client=client).fetch("https://example.test/jobs/active")

    assert result.posting_status == "active"
    assert result.text is not None
    assert "Build dashboards" in result.text


@pytest.mark.parametrize(
    "message",
    ["Source returned HTTP 404", "Source returned HTTP 410", "This job is no longer available."],
)
def test_an_explicit_closure_still_returns_a_closed_bodiless_posting(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def closed_read(url: str, **kwargs: object) -> UrlPostingText:
        raise IngestError(message)

    monkeypatch.setattr(
        "tailorcv.infrastructure.web_fetch.posting_fetch.url_ingest.from_url",
        closed_read,
    )
    client = policy_client(lambda url: httpx.Response(200))

    result = PolitePostingFetcher(client=client).fetch("https://example.test/jobs/closed")

    assert result.posting_status == "closed"
    assert result.method == "closure_signal"
    assert result.text is None


def test_disappearing_publisher_markup_closes_a_previously_declared_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(
        durable_database,
        root,
        extraction_method="publisher markup",
    )
    body = "Lead product and data platform governance. Canada remote role."
    _run_refresh(
        durable_database,
        root,
        ScriptedFetcher(
            [
                FetchedPosting(
                    body,
                    "active",
                    "publisher markup",
                    utc_now(),
                    200,
                    publisher_markup_present=True,
                )
            ]
        ),
        workspace_id,
        job_id,
        "publisher-markup-unchanged",
    )
    refreshed = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher(
            [
                FetchedPosting(
                    body,
                    "active",
                    "page extraction",
                    utc_now(),
                    200,
                    publisher_markup_present=False,
                )
            ]
        ),
        workspace_id,
        job_id,
        "publisher-markup-disappeared",
    )

    assert "status" in refreshed.progress["changed_fields"]
    with durable_database.session() as session:
        latest = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            .limit(1)
        )
        assert latest is not None
        assert latest.posting_status == "closed"
        assert latest.extraction_method == "publisher_markup_disappeared"


def test_refresh_short_circuits_unchanged_body_without_model_calls(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    body = "Lead product and data platform governance. Canada remote role."
    fetched = FetchedPosting(body, "active", "scripted", utc_now(), 200)
    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([fetched]),
        workspace_id,
        job_id,
        "unchanged",
    )
    assert state.status == "completed"
    progress = state.progress
    assert isinstance(progress, dict)
    assert progress["content_unchanged"] is True
    assert progress["observation_unchanged"] is True
    assert progress["short_circuit"] is True
    assert progress["model_calls_made"] == 0

    repeated = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "unchanged-again",
    )
    assert repeated.progress["content_unchanged"] is True
    assert repeated.progress["observation_unchanged"] is True
    assert repeated.progress["short_circuit"] is True
    with durable_database.session() as session:
        observations = session.scalars(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at)
        ).all()
        assert len(observations) == 3
        assert len({item.body_artifact_id for item in observations}) == 1
        latest = observations[-1]
        assert latest.location == "Canada remote"
        assert latest.residence_rule == "Canada residents"
        assert latest.employment_type == "full-time"
        assert latest.payroll_arrangement == "Canadian payroll"
        eligibility = session.scalars(
            select(EligibilityRecord).where(EligibilityRecord.observation_id == latest.id)
        ).all()
        assert len(eligibility) == 1
        assert eligibility[0].workspace_location_key == "explicit_canada"
        assert session.scalar(select(func.count()).select_from(Artifact)) == 3
        assert session.scalar(select(func.count()).select_from(PostingObservationDiff)) == 2


@pytest.mark.parametrize(
    ("replacement", "changed"),
    [("2030-12-01", True), ("2026-09-01", False), ("", True)],
)
def test_refresh_replaces_the_publisher_date_without_replacing_an_unchanged_body(
    durable_database: Database, tmp_path: Path, replacement: str, changed: bool
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(
        durable_database, root, observed_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    body = "Lead product and data platform governance. Canada remote role."
    source_url = "https://example.test/jobs/date"
    results = []
    for index, value in enumerate(("2026-09-01", replacement)):
        fetched_at = datetime(2026, 9, 2 + index, 12, tzinfo=UTC)
        source = PostingSource(publisher="page", record=json.dumps({"validThrough": value}))
        fetched = FetchedPosting(
            body,
            "active",
            "scripted",
            fetched_at,
            200,
            source=source,
            publisher_closing_date=publisher_closing_date(
                body, valid_through=value, source_url=source_url, observed_at=fetched_at
            ),
        )
        result = _run_refresh(
            durable_database,
            root,
            ScriptedFetcher([fetched]),
            workspace_id,
            job_id,
            f"date-{index}",
        )
        assert result.status == "completed"
        assert result.progress["model_calls_made"] == 0
        assert result.progress["content_unchanged"] is True
        results.append(result)
    result = results[-1]
    assert ("publisher_closing_date" in result.progress["changed_fields"]) is changed
    assert result.progress["short_circuit"] is (not changed)
    observations = _observations(durable_database, job_id)
    assert len({item.body_artifact_id for item in observations}) == 1
    assert observations[-1].posting_status == "active"
    previous = PublisherClosingDate.from_json(observations[-2].publisher_closing_date)
    latest = PublisherClosingDate.from_json(observations[-1].publisher_closing_date)
    assert previous.normalized_value == "2026-09-01"
    assert previous.has_passed(datetime(2026, 9, 3, tzinfo=UTC)) is True
    assert latest.normalized_value == (replacement or None)
    assert latest.observed_at == datetime(2026, 9, 3, 12, tzinfo=UTC)
    assert observations[-1].publisher_closing_date is not None
    if replacement:
        assert latest.statements[0].artifact_id == latest.artifact_id
        assert latest.artifact_id != previous.artifact_id
        assert latest.artifact_id != observations[-1].body_artifact_id
        with durable_database.session() as session:
            artifact = session.get(Artifact, latest.artifact_id)
            assert artifact is not None
            assert artifact.logical_type == "posting.source.json"
            artifact_path = file_uri_path(artifact.storage_uri)
            assert artifact_path is not None
            assert json.loads(artifact_path.read_text()) == {"validThrough": replacement}
    else:
        assert latest.statements == ()
        with durable_database.session() as session:
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.related_entity_id == observations[-1].id,
                    Artifact.logical_type == "posting.source.json",
                )
            )
            assert artifact is not None
            artifact_path = file_uri_path(artifact.storage_uri)
            assert artifact_path is not None
            assert json.loads(artifact_path.read_text()) == {"validThrough": ""}


def test_refresh_binds_body_and_publisher_date_statements_to_their_own_artifacts(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(
        durable_database, root, observed_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    body = "Lead product and data platform governance.\nPosting End Date: 2030-12-01"
    fetched_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    fetched = FetchedPosting(
        body,
        "active",
        "scripted",
        fetched_at,
        200,
        source=PostingSource(publisher="page", record='{"validThrough":"2030-12-01"}'),
        publisher_closing_date=publisher_closing_date(
            body, valid_through="2030-12-01", observed_at=fetched_at
        ),
    )
    result = _run_refresh(
        durable_database, root, ScriptedFetcher([fetched]), workspace_id, job_id, "mixed-dates"
    )
    assert result.status == "completed"
    assert result.progress["model_calls_made"] == 0
    observation = _observations(durable_database, job_id)[-1]
    date = PublisherClosingDate.from_json(observation.publisher_closing_date)
    assert observation.posting_status == "active"
    assert date.normalized_value == "2030-12-01"
    assert {item.basis: item.artifact_id for item in date.statements} == {
        "valid_through": date.artifact_id,
        "body_label": observation.body_artifact_id,
    }
    assert date.artifact_id != observation.body_artifact_id


def test_refresh_recovers_missing_facts_from_an_exact_same_body_observation(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    body = "Lead product and data platform governance. Canada remote role."
    with durable_database.session() as session:
        previous = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            .limit(1)
        )
        assert previous is not None
        session.add(
            PostingObservation(
                id="same-body-with-missing-facts",
                job_posting_id=job_id,
                observed_at=utc_now(),
                posting_status="active",
                location="",
                residence_rule="",
                employment_type="",
                payroll_arrangement="",
                body_artifact_id=previous.body_artifact_id,
                extraction_method="refresh_hash_unchanged",
                fetched_at=utc_now(),
                http_status=200,
                content_sha256=previous.content_sha256,
                created_by="test",
                origin="test.posting_refresh",
            )
        )

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "recover-exact-body-facts",
    )

    assert state.status == "completed"
    with durable_database.session() as session:
        latest = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            .limit(1)
        )
        assert latest is not None
        assert latest.location == "Canada remote"
        assert latest.residence_rule == "Canada residents"
        assert latest.employment_type == "full-time"
        assert latest.payroll_arrangement == "Canadian payroll"


def _observations(database: Database, job_id: str) -> list[PostingObservation]:
    with database.session() as session:
        return list(
            session.scalars(
                select(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
                .order_by(PostingObservation.observed_at, PostingObservation.id)
            ).all()
        )


def test_a_refresh_that_only_relays_the_body_keeps_the_known_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    relaid = "Lead product  and data platform governance.\u00a0Canada  remote role. "

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(relaid, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "relaid",
    )

    assert state.progress["content_unchanged"] is True
    assert "body" not in state.progress["changed_fields"]
    first, latest = _observations(durable_database, job_id)
    assert latest.content_sha256 == first.content_sha256
    assert latest.body_artifact_id == first.body_artifact_id
    assert latest.location == "Canada remote"
    assert latest.residence_rule == "Canada residents"


def _body(database: Database, root: Path, workspace_id: str, artifact_id: str | None) -> bytes:
    with database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        return artifact_service(database, root).read(artifact, workspace)


_LINED = "Lead product and data platform governance.\n\nCanada remote role."


def test_a_refresh_that_lays_a_flat_body_out_in_lines_replaces_it(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    (first,) = _observations(durable_database, job_id)
    flat = _body(durable_database, root, workspace_id, first.body_artifact_id)

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(_LINED, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "lined",
    )

    assert state.progress["content_unchanged"] is False
    _, latest = _observations(durable_database, job_id)
    assert latest.content_sha256 == hashlib.sha256(_LINED.encode()).hexdigest()
    assert latest.body_artifact_id != first.body_artifact_id
    assert _body(durable_database, root, workspace_id, latest.body_artifact_id) == _LINED.encode()
    assert _body(durable_database, root, workspace_id, first.body_artifact_id) == flat


def test_a_refresh_that_flattens_a_lined_body_keeps_the_lined_bytes(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    flat = "Lead product and data platform governance. Canada remote role."
    fetcher = ScriptedFetcher(
        [
            FetchedPosting(_LINED, "active", "scripted", utc_now(), 200),
            FetchedPosting(flat, "active", "scripted", utc_now(), 200),
        ]
    )
    _run_refresh(durable_database, root, fetcher, workspace_id, job_id, "lined")

    state = _run_refresh(durable_database, root, fetcher, workspace_id, job_id, "flattened")

    assert state.progress["content_unchanged"] is True
    _, lined, latest = _observations(durable_database, job_id)
    assert latest.content_sha256 == lined.content_sha256
    assert latest.body_artifact_id == lined.body_artifact_id
    assert _body(durable_database, root, workspace_id, latest.body_artifact_id) == _LINED.encode()


def test_a_refresh_over_a_lost_stored_body_retains_the_fresh_read(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    artifacts = artifact_service(durable_database, root)
    (first,) = _observations(durable_database, job_id)
    with durable_database.session() as session:
        artifact = session.get(Artifact, first.body_artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        body = artifacts.read(artifact, workspace)
        lost = file_uri_path(artifact.storage_uri)
        assert lost is not None
    lost.unlink()

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(body.decode(), "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "lost-body",
    )

    assert state.status == "completed"
    latest = _observations(durable_database, job_id)[-1]
    assert latest.body_artifact_id != first.body_artifact_id
    with durable_database.session() as session:
        artifact = session.get(Artifact, latest.body_artifact_id)
        workspace = session.get(Workspace, workspace_id)
        assert artifact is not None and workspace is not None
        assert artifacts.read(artifact, workspace) == body


def test_a_refresh_of_a_job_never_observed_writes_its_first_observation(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, observed_id = _job(durable_database, root)
    url = "https://jobs.example.test/postings/never-observed"
    with durable_database.session() as session:
        observed = session.get(JobPosting, observed_id)
        assert observed is not None
        audit = audit_columns("local_user", "test", None)
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=observed.employer_id,
            title="Mirrored Product Manager",
            normalized_title="mirrored product manager",
            first_seen_at=utc_now(),
            **audit,
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="canonical",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
                **audit,
            )
        )
        job_id = job.id
    body = "Product Manager\n\nOwn the pharmacy data roadmap. Remote in Canada."
    fetcher = ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)])

    state = _run_refresh(durable_database, root, fetcher, workspace_id, job_id, "first-read")

    assert state.status == "completed", state.model_dump().get("error_message")
    assert state.progress["previous_observation_id"] is None
    assert state.progress["diff_id"] is None
    assert state.progress["model_calls_made"] == 0
    assert fetcher.urls == [url]
    (first,) = _observations(durable_database, job_id)
    assert first.posting_status == "active"
    assert _body(durable_database, root, workspace_id, first.body_artifact_id) == body.encode()


def test_a_refresh_whose_words_change_reports_the_changed_body(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    reworded = "Lead product and data\n\nplatform governance.\u00a0Canada  remote team."

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(reworded, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "reworded",
    )

    assert state.progress["content_unchanged"] is False
    assert "body" in state.progress["changed_fields"]
    first, latest = _observations(durable_database, job_id)
    assert latest.content_sha256 != first.content_sha256
    assert latest.body_artifact_id != first.body_artifact_id


def test_a_refresh_whose_body_states_no_place_takes_the_publishers_place(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    fetched = FetchedPosting(
        "Lead product for the Harbor Ledger analytics platform.",
        "active",
        "greenhouse_api",
        utc_now(),
        200,
        publisher_check=PostingCheck(
            status="readable", note="", platform="greenhouse", location="Remote, Northvale"
        ),
    )

    _run_refresh(durable_database, root, ScriptedFetcher([fetched]), workspace_id, job_id, "placed")

    assert _observations(durable_database, job_id)[-1].location == "Remote, Northvale"


def _placeless_read(hint: str, check_status: Literal["readable", "unreadable"]) -> RefreshDecision:
    previous = PreviousObservation(
        job_id="job-7",
        workspace_id="workspace-7",
        observation_id="observation-7",
        artifact_id="artifact-7",
        body="Lead analytics for Harbor Ledger.",
        content_sha256="0" * 64,
        posting_status="active",
        location="",
        residence_rule="",
        employment_type="",
        payroll_arrangement="",
        fetched_at=None,
        extraction_method="greenhouse_api",
        source_url="https://example.test/jobs/7",
        title="Analytics Lead",
    )
    fetched = FetchedPosting(
        "Lead product for the Harbor Ledger analytics platform.",
        "active",
        "greenhouse_api",
        utc_now(),
        200,
        publisher_check=PostingCheck(
            status=check_status,
            note="",
            platform="greenhouse",
            location="Remote, Northvale",
        ),
    )
    return decide_refresh(previous, fetched, "", hint)


def test_a_refresh_hint_that_counts_places_yields_the_publishers_place() -> None:
    assert _placeless_read("3 Locations", "readable").facts.location == "Remote, Northvale"


def test_a_refresh_ignores_the_place_of_a_publisher_check_it_could_not_read() -> None:
    assert _placeless_read("", "unreadable").facts.location == ""


def test_repeated_discovery_refreshes_the_tracked_posting_and_current_facts(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    body = (
        "Title: Principal Platform Product Manager\n"
        "Location: Toronto, Canada\n"
        "Employment type: Full-time\n"
        "Work authorization: Must be authorized to work in Canada.\n"
        "Engagement: W-2 employee"
    )
    fetcher = ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)])
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        posting_fetcher=fetcher,
    )

    created = service.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme Health",
                title="Principal Product Manager",
                url="https://example.test/jobs/42",
                location="Remote, Canada",
            )
        ],
        utc_now().date(),
        reason="Board scan matched the title filter.",
    )

    assert created.queued == []
    assert fetcher.urls == ["https://example.test/jobs/42?utm_source=feed"]
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.title == "Principal Product Manager"
        latest = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            .limit(1)
        )
        assert latest is not None
        assert latest.posting_status == "active"
        assert latest.location == "Remote, Canada"
        assert latest.residence_rule == "Must be authorized to work in Canada."
        assert latest.employment_type == "Full-time"
        assert latest.payroll_arrangement == "W-2 employee"
        assert session.scalar(select(func.count()).select_from(PendingDiscovery)) == 0


def _title_corrections(database: Database, job_id: str) -> list[AuditEvent]:
    with database.session() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "posting.title_corrected",
                    AuditEvent.entity_id == job_id,
                )
            ).all()
        )


@pytest.mark.parametrize(
    "published_employer",
    ["Northwind", "Recruiting tenant acme.oraclecloud.com/CX_1 (company not stated)"],
    ids=["publisher-company", "honest-tenant-label"],
)
def test_a_publisher_company_change_preserves_a_submitted_application_identity(
    durable_database: Database, tmp_path: Path, published_employer: str
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    event = job_search.append_application_event(
        durable_database,
        job_id,
        ApplicationEventInput(
            status=ApplicationStatus.APPLIED,
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
            notes="Submitted with the recorded employer identity.",
        ),
        workspace_id=workspace_id,
        idempotency_key="submitted-identity",
    )
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        employer = session.get(Employer, job.employer_id)
        assert employer is not None
        original = (job.employer_id, employer.name, employer.normalized_name)
    fetched = FetchedPosting(
        "Lead product and data platform governance. Canada remote role. Now hiring.",
        "active",
        "scripted",
        datetime(2026, 10, 9, tzinfo=UTC),
        employer=published_employer,
    )

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([fetched]),
        workspace_id,
        job_id,
        "preserve-submitted-company",
    )

    assert state.status == "completed"
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        employer = session.get(Employer, job.employer_id)
        assert employer is not None
        assert (job.employer_id, employer.name, employer.normalized_name) == original
        recorded = session.get(ApplicationEvent, event.id)
        assert recorded is not None
        assert recorded.application_id == event.application_id
        assert recorded.status == ApplicationStatus.APPLIED.value
        assert recorded.notes == event.notes
        assert recorded.occurred_at == event.occurred_at


def test_a_heading_in_the_body_cannot_rename_a_named_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    """A page heading is the page's own wording, not the publisher naming the role."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    body = "# The Role\n\nLead product and data platform governance. Canada remote role."

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "heading-does-not-rename",
    )

    assert state.status == "completed"
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == "Product Manager, Data Platform"
    assert _title_corrections(durable_database, job_id) == []


def test_a_publishers_own_title_renames_the_posting_and_records_the_old_name(
    durable_database: Database, tmp_path: Path
) -> None:
    """The publisher's own page or API title outranks the name the ledger carries."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    body = "Lead product and data platform governance. Canada remote role. Now hiring."
    fetched = FetchedPosting(body, "active", "scripted", utc_now(), 200, title="VP, Data Platform")

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([fetched]),
        workspace_id,
        job_id,
        "publisher-renames",
    )

    assert state.status == "completed"
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == "VP, Data Platform"
    (event,) = _title_corrections(durable_database, job_id)
    assert event.details["old"] == "Product Manager, Data Platform"
    assert event.details["new"] == "VP, Data Platform"


def test_a_posting_with_no_stored_title_takes_the_one_the_body_states(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        job.title = ""
        job.normalized_title = ""
        session.commit()
    body = "# Data Platform Lead\n\nLead product and data platform governance. Canada remote role."

    state = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "blank-title-filled",
    )

    assert state.status == "completed"
    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == "Data Platform Lead"
    assert len(_title_corrections(durable_database, job_id)) == 1


def test_changed_and_closed_refreshes_append_artifacts_observations_and_diffs(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    changed_body = (
        "Lead product and data platform governance. Canada remote role.\n"
        "Required: production SQL. Compensation CAD 150,000."
    )
    changed = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(changed_body, "active", "scripted", utc_now(), 200)]),
        workspace_id,
        job_id,
        "changed",
    )
    assert changed.progress["content_unchanged"] is False
    assert "body" in changed.progress["changed_fields"]
    closed = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher([FetchedPosting(None, "closed", "closure_signal", utc_now(), 404)]),
        workspace_id,
        job_id,
        "closed",
    )
    assert "status" in closed.progress["changed_fields"]
    assert closed.progress["observation_unchanged"] is False
    with durable_database.session() as session:
        observations = session.scalars(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at, PostingObservation.id)
        ).all()
        assert len(observations) == 3
        assert observations[-1].posting_status == "closed"
        assert observations[-1].http_status == 404
        assert session.scalar(select(func.count()).select_from(Artifact)) == 4
        assert session.scalar(select(func.count()).select_from(PostingObservationDiff)) == 2
        diff_id = session.scalar(select(PostingObservationDiff.id).limit(1))
    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE posting_observation_diffs SET body_diff = '' WHERE id = :id"),
            {"id": diff_id},
        )


def test_unverified_refresh_keeps_the_last_verified_observation(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(
        durable_database, root, observed_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    dated = FetchedPosting(
        "Lead product and data platform governance. Canada remote role.",
        "active",
        "scripted",
        fetched_at,
        200,
        publisher_closing_date=publisher_closing_date(
            "", valid_through="2030-12-01", observed_at=fetched_at
        ),
    )
    _run_refresh(
        durable_database, root, ScriptedFetcher([dated]), workspace_id, job_id, "known-date"
    )
    known_date = _observations(durable_database, job_id)[-1].publisher_closing_date
    result = _run_refresh(
        durable_database,
        root,
        ScriptedFetcher(
            [
                FetchedPosting(
                    None,
                    "unverified",
                    "publisher_unverified",
                    datetime(2026, 9, 2, tzinfo=UTC),
                    verification_note="Greenhouse could not verify this hosted posting.",
                )
            ]
        ),
        workspace_id,
        job_id,
        "unverified",
    )

    assert result.progress["unverified"] is True
    assert result.progress["model_calls_made"] == 0
    assert "could not verify" in result.progress["reason"]
    with durable_database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
            )
            == 2
        )
    assert _observations(durable_database, job_id)[-1].publisher_closing_date == known_date


def test_a_stale_latest_observation_leaves_no_refresh_records(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)

    class InterleavingFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            with durable_database.session() as session:
                latest = session.scalar(
                    select(PostingObservation)
                    .where(PostingObservation.job_posting_id == job_id)
                    .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
                    .limit(1)
                )
                assert latest is not None
                session.add(
                    PostingObservation(
                        id="newer-observation",
                        job_posting_id=job_id,
                        observed_at=datetime(2099, 1, 1, tzinfo=UTC),
                        posting_status=latest.posting_status,
                        location=latest.location,
                        residence_rule=latest.residence_rule,
                        employment_type=latest.employment_type,
                        payroll_arrangement=latest.payroll_arrangement,
                        body_artifact_id=latest.body_artifact_id,
                        extraction_method=latest.extraction_method,
                        fetched_at=datetime(2099, 1, 1, tzinfo=UTC),
                        http_status=latest.http_status,
                        content_sha256=latest.content_sha256,
                        created_by="test",
                        origin="test.interleaving",
                    )
                )
            return FetchedPosting("Changed body", "active", "scripted", utc_now(), 200)

    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=InterleavingFetcher(),
    )

    with pytest.raises(ConflictError, match="newer posting observation"):
        service.refresh(workspace_id, job_id)

    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(Artifact)) == 3
        assert session.scalar(select(func.count()).select_from(PostingObservation)) == 2
        assert session.scalar(select(func.count()).select_from(PostingObservationDiff)) == 0


@pytest.mark.parametrize("known_alias", [False, True])
def test_refresh_accepts_full_publisher_body_for_a_known_posting_url(
    durable_database: Database, tmp_path: Path, known_alias: bool
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    with durable_database.session() as session:
        primary = session.scalar(select(PostingUrl).where(PostingUrl.job_posting_id == job_id))
        assert primary is not None
        url = primary_url = primary.url
        if known_alias:
            url = "https://publisher.example/alternate/jobs/data-lead"
            session.add(
                PostingUrl(
                    job_posting_id=job_id,
                    url=url,
                    normalized_url=normalize_url(url),
                    is_primary=False,
                    url_type="discovery",
                )
            )
    body = "Lead data platform delivery and maintain clinical integrations. " * 12
    markup = "<p>" + body + "</p>"
    prefetched = FetchedPosting(
        body,
        "active",
        "static_structured",
        utc_now(),
        http_status=None,
        publisher_markup_present=True,
        final_url=url,
        source=PostingSource(
            publisher="darwinbox",
            record=json.dumps({"description": markup}),
            description_markup=markup,
            description_paths=("description",),
        ),
    )
    fetcher = ScriptedFetcher([])
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=fetcher,
    )

    result = service.refresh(workspace_id, job_id, prefetched=prefetched)

    assert result["model_calls_made"] == 0
    assert result["unverified"] is False
    assert fetcher.urls == []
    with durable_database.session() as session:
        observation = session.get(PostingObservation, result["observation_id"])
        assert observation is not None
        assert observation.content_sha256 == hashlib.sha256(body.encode()).hexdigest()
        assert observation.http_status is None and observation.robots_basis is None
        assert observation.extraction_method == "static_structured"
        artifacts = list(
            session.scalars(select(Artifact).where(Artifact.related_entity_id == observation.id))
        )
        assert {row.logical_type for row in artifacts} == {
            "posting.body.text",
            "posting.body.html",
            "posting.source.json",
        }
        assert all(row.provenance["source_url"] == url for row in artifacts)
        retained_primary = session.scalar(
            select(PostingUrl.url).where(
                PostingUrl.job_posting_id == job_id, PostingUrl.is_primary.is_(True)
            )
        )
        assert retained_primary == primary_url
        assert list(session.scalars(select(ModelCall))) == []


@pytest.mark.parametrize(
    "invalid", ["foreign_url", "empty_url", "empty_body", "unverified", "no_source", "no_markup"]
)
def test_refresh_refuses_unbound_or_unverified_supplied_bodies(
    durable_database: Database, tmp_path: Path, invalid: str
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    with durable_database.session() as session:
        url = session.scalar(select(PostingUrl.url).where(PostingUrl.job_posting_id == job_id))
        assert url is not None
        observations = list(session.scalars(select(PostingObservation.id)))
        artifacts = list(session.scalars(select(Artifact.id)))
    prefetched = FetchedPosting(
        "A full publisher body for this opening.",
        "active",
        "static_structured",
        utc_now(),
        http_status=None,
        publisher_markup_present=True,
        final_url=url,
        source=PostingSource(publisher="darwinbox", record='{"id": "data-lead"}'),
    )
    changes: dict[str, Any] = {
        "foreign_url": {"final_url": "https://publisher.example/jobs/unrelated"},
        "empty_url": {"final_url": ""},
        "empty_body": {"text": " "},
        "unverified": {"posting_status": "unverified"},
        "no_source": {"source": None},
        "no_markup": {"publisher_markup_present": False},
    }
    fetcher = ScriptedFetcher([])
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=fetcher,
    )
    files = set(root.rglob("*"))

    with pytest.raises(ValidationError, match="supplied publisher"):
        service.refresh(workspace_id, job_id, prefetched=replace(prefetched, **changes[invalid]))

    assert fetcher.urls == []
    assert set(root.rglob("*")) == files
    with durable_database.session() as session:
        assert list(session.scalars(select(PostingObservation.id))) == observations
        assert list(session.scalars(select(Artifact.id))) == artifacts
        assert list(session.scalars(select(PostingObservationDiff))) == []
        assert list(session.scalars(select(ModelCall))) == []


@pytest.mark.parametrize(
    "interruption", ["cancel", "source_loss", "retire", "alias_retire", "new_observation"]
)
def test_supplied_body_keeps_refresh_write_guards(
    durable_database: Database, tmp_path: Path, interruption: str
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    with durable_database.session() as session:
        url = session.scalar(select(PostingUrl.url).where(PostingUrl.job_posting_id == job_id))
        assert url is not None
        observations = list(session.scalars(select(PostingObservation.id)))
        artifacts = list(session.scalars(select(Artifact.id)))
    prefetched = FetchedPosting(
        "A changed full publisher body.",
        "active",
        "static_structured",
        utc_now(),
        http_status=None,
        publisher_markup_present=True,
        final_url=url,
        source=PostingSource(publisher="darwinbox", record='{"id": "data-lead"}'),
    )
    fetcher = ScriptedFetcher([])
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=fetcher,
    )
    queue = TaskQueueService(durable_database)
    callback_stages: list[str] = []
    guard_calls: list[bool] = []

    class InterruptingContext:
        task_id = "controlled-refresh"
        worker_id = "test-worker"

        def __init__(self) -> None:
            self.queue = queue
            self.cancelled = False

        def progress(self, stage: str, payload: dict[str, Any]) -> None:
            callback_stages.append(stage)
            if stage != "persisting":
                return
            if interruption == "cancel":
                self.cancelled = True
            elif interruption in ("retire", "alias_retire", "new_observation"):
                with durable_database.session() as session:
                    job = session.get(JobPosting, job_id)
                    assert job is not None
                    if interruption == "retire":
                        job.archived_at = utc_now()
                    elif interruption == "alias_retire":
                        alias = session.scalar(
                            select(PostingUrl).where(
                                PostingUrl.job_posting_id == job_id,
                                PostingUrl.normalized_url == normalize_url(prefetched.final_url),
                            )
                        )
                        assert alias is not None
                        alias.archived_at = utc_now()
                    else:
                        session.add(
                            PostingObservation(
                                job_posting_id=job_id,
                                posting_status="active",
                                observed_at=datetime(2099, 1, 1, tzinfo=UTC),
                                origin="test.interleaving",
                                created_by="test",
                            )
                        )

        def cancellation_requested(self) -> bool:
            return self.cancelled

        def raise_if_cancelled(self) -> None:
            if self.cancelled:
                raise TaskCancelledError

    def require_source_read(session: Session) -> None:
        guard_calls.append(True)
        if interruption == "source_loss":
            raise ConflictError("The source read is no longer owned.")

    context: TaskContext = InterruptingContext()
    expected_error = (
        TaskCancelledError
        if interruption == "cancel"
        else NotFoundError
        if interruption == "retire"
        else ValidationError
        if interruption == "alias_retire"
        else ConflictError
    )
    files = set(root.rglob("*"))

    with pytest.raises(expected_error):
        service.refresh(
            workspace_id,
            job_id,
            context,
            prefetched=prefetched,
            require_source_read=require_source_read,
        )

    assert callback_stages == ["fetching", "persisting"]
    assert guard_calls == ([] if interruption == "cancel" else [True])
    assert fetcher.urls == []
    assert set(root.rglob("*")) == files
    with durable_database.session() as session:
        current = list(session.scalars(select(PostingObservation.id)))
        assert set(observations).issubset(current)
        assert len(current) == len(observations) + (interruption == "new_observation")
        assert list(session.scalars(select(Artifact.id))) == artifacts
        assert list(session.scalars(select(PostingObservationDiff))) == []
        assert list(session.scalars(select(ModelCall))) == []


def test_source_ownership_loss_keeps_tracked_observation_and_artifacts(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    with durable_database.session() as session:
        previous_observations = list(session.scalars(select(PostingObservation.id)))
        previous_artifacts = list(session.scalars(select(Artifact.id)))
        previous_job = session.get(JobPosting, job_id)
        assert previous_job is not None
        previous_title = previous_job.title

    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting("Changed publisher body", "active", "scripted", utc_now(), 200)]
        ),
    )

    def cancelled_source(session: Session) -> None:
        job = session.get(JobPosting, job_id)
        assert job is not None
        job.title = "This transaction must roll back."
        raise ConflictError("The source read is no longer owned.")

    with pytest.raises(ConflictError, match="source read is no longer owned"):
        service.refresh(workspace_id, job_id, require_source_read=cancelled_source)

    with durable_database.session() as session:
        assert list(session.scalars(select(PostingObservation.id))) == previous_observations
        assert list(session.scalars(select(Artifact.id))) == previous_artifacts
        assert list(session.scalars(select(PostingObservationDiff))) == []
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == previous_title


def test_batch_refresh_skips_a_posting_read_today_and_is_idempotent(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root, observed_at=utc_now())
    queue = TaskQueueService(durable_database)
    payload = {"job_posting_ids": [job_id], "saved_view_id": None, "filters": None}
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_batch",
        payload=payload,
        idempotency_key="batch-1",
    )
    assert (
        queue.enqueue(
            workspace_id=workspace_id,
            task_type="posting.refresh_batch",
            payload=payload,
            idempotency_key="batch-1",
        ).id
        == task.id
    )
    worker = Worker(
        durable_database,
        worker_id="batch-worker",
        handlers=default_handlers(
            durable_database,
            artifact_root=root,
            fetcher=ScriptedFetcher([]),
        ),
    )
    assert worker.run_once()
    state = queue.get(str(task.id))
    assert state.progress["refreshed"] == 0
    assert state.progress["skipped"] == [{"job_posting_id": job_id, "reason": "read_today"}]


def test_batch_refresh_reports_unverified_without_replacing_the_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_batch",
        payload={
            "job_posting_ids": [job_id],
            "saved_view_id": None,
            "filters": None,
            "force": True,
        },
        idempotency_key="batch-unverified",
    )
    worker = Worker(
        durable_database,
        worker_id="batch-unverified-worker",
        handlers=default_handlers(
            durable_database,
            artifact_root=root,
            fetcher=ScriptedFetcher(
                [
                    FetchedPosting(
                        None,
                        "unverified",
                        "publisher_unverified",
                        utc_now(),
                        verification_note="Greenhouse could not verify this hosted posting.",
                    )
                ]
            ),
        ),
    )

    assert worker.run_once()
    state = queue.get(str(task.id))
    assert state.status == "completed"
    assert state.progress["refreshed"] == 0
    assert state.progress["unverified"] == 1
    assert state.progress["unverified_postings"] == [
        {"job_posting_id": job_id, "reason": "Greenhouse could not verify this hosted posting."}
    ]


def test_batch_refresh_preserves_successes_when_one_posting_cannot_be_read(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, first_job_id = _job(
        durable_database, root, observed_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    second = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, root),
        evaluation_commit(
            workspace_id,
            title="Second Product Role",
            requisition_id="REQ-SECOND",
            canonical_url="https://example.test/jobs/second",
            discovery_url="https://example.test/jobs/second",
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    dated = FetchedPosting(
        "Second Product Role with enough text to retain as a new observation.",
        "active",
        "scripted",
        fetched_at,
        200,
        publisher_closing_date=publisher_closing_date(
            "", valid_through="2030-12-01", observed_at=fetched_at
        ),
    )
    PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedFetcher([dated]),
    ).refresh(workspace_id, second.job_posting_id)
    previous = _observations(durable_database, second.job_posting_id)[-1]
    known_date = previous.publisher_closing_date
    previous_id = previous.id

    class PartialFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            if url.endswith("/second"):
                raise IngestError("The second posting returned no readable text.")
            return FetchedPosting(
                "Changed posting body with enough text to retain as a new observation.",
                "active",
                "scripted",
                datetime(2026, 9, 2, tzinfo=UTC),
                200,
            )

    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_batch",
        payload={
            "job_posting_ids": [first_job_id, second.job_posting_id],
            "saved_view_id": None,
            "filters": None,
            "force": True,
        },
        idempotency_key="partial-batch",
    )
    worker = Worker(
        durable_database,
        worker_id="partial-batch-worker",
        handlers=default_handlers(
            durable_database,
            artifact_root=root,
            fetcher=PartialFetcher(),
        ),
    )

    assert worker.run_once()
    state = queue.get(str(task.id))
    assert state.status == "completed"
    assert state.progress["refreshed"] == 1
    assert state.progress["changed"] == 1
    assert state.progress["failed"] == 1
    assert state.progress["failures"] == [
        {
            "job_posting_id": second.job_posting_id,
            "error_category": "IngestError",
            "error_message": "The second posting returned no readable text.",
        }
    ]
    observations = _observations(durable_database, second.job_posting_id)
    assert len(observations) == 2
    assert observations[-1].id == previous_id
    assert observations[-1].publisher_closing_date == known_date


def test_a_batch_row_being_refreshed_keeps_its_step_in_the_stored_progress(
    durable_database: Database, tmp_path: Path
) -> None:
    """The Tasks page reads the step from stored progress while a row's nested read runs."""
    root = tmp_path / "artifacts"
    workspace_id, first_job_id = _job(durable_database, root)
    second = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, root),
        evaluation_commit(
            workspace_id,
            title="Second Product Role",
            requisition_id="REQ-SECOND",
            canonical_url="https://example.test/jobs/second",
            discovery_url="https://example.test/jobs/second",
        ),
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_batch",
        payload={
            "job_posting_ids": [first_job_id, second.job_posting_id],
            "saved_view_id": None,
            "filters": None,
            "force": True,
        },
        idempotency_key="step-batch",
    )
    seen: list[dict[str, Any]] = []

    class ProbeFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            del url, intent, board_url
            seen.append(dict(queue.get(str(task.id)).progress))
            return FetchedPosting(
                "Changed posting body with enough text to retain as a new observation.",
                "active",
                "scripted",
                utc_now(),
                200,
            )

    worker = Worker(
        durable_database,
        worker_id="step-batch-worker",
        handlers=default_handlers(durable_database, artifact_root=root, fetcher=ProbeFetcher()),
    )

    assert worker.run_once()

    assert [
        (item.get("current"), item.get("total"), item.get("job_posting_id")) for item in seen
    ] == [
        (1, 2, first_job_id),
        (2, 2, second.job_posting_id),
    ]
    assert seen[1].get("so_far") == {
        "refreshed": 1,
        "unchanged": 0,
        "changed": 1,
        "withdrawn_as_closed": 0,
        "unverified": 0,
        "skipped": 0,
        "failed": 0,
    }
    events = queue.events_after(str(task.id), limit=250)
    steps = [event.payload for event in events if event.stage == "refreshing"]
    assert [step.get("so_far", {}).get("refreshed") for step in steps] == [0, 1]
    fetching = [event.payload for event in events if event.stage == "fetching"]
    persisting = [event.payload for event in events if event.stage == "persisting"]
    assert [payload["job_posting_id"] for payload in fetching] == [
        first_job_id,
        second.job_posting_id,
    ]
    assert all(payload["url"].startswith("https://example.test/jobs/") for payload in fetching)
    assert [payload["model_calls_made"] for payload in persisting] == [0, 0]


def test_batch_refresh_reads_one_site_feed_once_for_every_posting_on_it(
    durable_database: Database, tmp_path: Path
) -> None:
    """The feed answers for the whole site, so a batch refresh must not download it per row."""
    root = tmp_path / "artifacts"
    first_url = DAYFORCE_POSTING_URL.format(reference="12345")
    other_url = DAYFORCE_POSTING_URL.format(reference="67890")
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="feed-batch", name="Feed batch"
    )
    artifacts = artifact_service(durable_database, root)
    first = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(workspace.id, canonical_url=first_url, discovery_url=first_url),
    )
    second = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            title="Staff Data Engineer",
            requisition_id="REQ-67890",
            canonical_url=other_url,
            discovery_url=other_url,
        ),
    )
    client = CountingFeedClient(
        [
            dayforce_feed_row("12345", "Clinical Data Manager"),
            dayforce_feed_row("67890", "Staff Data Engineer"),
        ]
    )

    class CheckingFetcher:
        def fetch(
            self,
            url: str,
            *,
            intent: PostingReadIntent = PostingReadIntent.CRAWL,
            board_url: str = "",
        ) -> FetchedPosting:
            del intent, board_url
            checked = check_posting(url, client=cast(Any, client))
            return FetchedPosting(
                "Own the clinical data model and terminology governance for this site. " * 4,
                "active",
                "scripted",
                utc_now(),
                200,
                title=checked.title,
            )

    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace.id,
        task_type="posting.refresh_batch",
        payload={
            "job_posting_ids": [first.job_posting_id, second.job_posting_id],
            "saved_view_id": None,
            "filters": None,
            "force": True,
        },
        idempotency_key="feed-batch",
    )
    worker = Worker(
        durable_database,
        worker_id="feed-batch-worker",
        handlers=default_handlers(durable_database, artifact_root=root, fetcher=CheckingFetcher()),
    )

    assert worker.run_once()
    assert queue.get(str(task.id)).progress["refreshed"] == 2
    assert len(client.requested) == 1


_DISPUTED_REQUIREMENT_TEXT = "Lead a data platform"
_BODY_WITH_DISPUTED_TEXT = (
    "Own the schema, quality, and governance of our data platform.\n"
    "Lead a data platform team across product analytics and warehouse pipelines.\n"
    "Canada remote."
)
_BODY_WITHOUT_DISPUTED_TEXT = (
    "Own the schema, quality, and governance of our data platform.\nCanada remote."
)


def _workspace_with_dispute_reextraction_setting(
    database: Database, *, enable_dispute_reextraction: bool, slug: str = "reextract"
) -> str:
    """Create a workspace whose learning policy carries the requested opt-in state."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug=f"{slug}-{'on' if enable_dispute_reextraction else 'off'}",
        name="Reextract",
    )
    LearningLoopPolicyService(database).save(
        workspace.id,
        LearningLoopPolicy(enable_dispute_reextraction=enable_dispute_reextraction),
        expected_version=0,
    )
    return workspace.id


def _seed_disputed_posting(
    database: Database,
    artifact_root: Path,
    *,
    workspace_id: str,
    disputed_text: str,
    posting_key: str = "42",
) -> tuple[str, str]:
    """Commit an evaluation whose fit report names ``disputed_text`` and rule it disputed."""
    committed = job_search.commit_evaluation(
        database,
        artifact_service(database, artifact_root),
        evaluation_commit(
            workspace_id,
            title=f"Data Platform Lead {posting_key}",
            requisition_id=f"REQ-{posting_key}",
            canonical_url=f"https://example.test/jobs/{posting_key}?utm_source=feed",
            discovery_url=f"https://example.test/jobs/{posting_key}",
            fit_report=_fit_report(
                {
                    "requirement_id": "REQ-1",
                    "importance": "must_have",
                    "text": disputed_text,
                    "evidence_keys": ["candidate-platform"],
                }
            ),
        ),
    )
    with database.session() as session:
        row = session.scalar(
            select(PostingRequirement).where(
                PostingRequirement.job_evaluation_id == committed.evaluation_id,
                PostingRequirement.requirement_id == "REQ-1",
            )
        )
        assert row is not None
        requirement_key = row.requirement_key
    RequirementRulingService(database).set(
        workspace_id=workspace_id,
        requirement_key=requirement_key,
        ruling=Ruling.DISPUTED_EXTRACTION,
        note="I disagree with this reading.",
        audit=audit_columns("local_user", "test", None),
    )
    return committed.job_posting_id, requirement_key


def _scripted_jobspec(
    payload: dict[str, object],
) -> ScriptedProvider:
    return ScriptedProvider({"JobSpecExtraction": lambda schema: payload})


def _make_client(
    settings: Settings, provider: ScriptedProvider, tmp_path: Path
) -> tuple[Client, ResponseCache]:
    cache = ResponseCache(enabled=False, directory=tmp_path / "cache")
    client = Client(
        provider=provider,
        settings=settings,
        cache=cache,
        ledger=Ledger(),
    )
    return client, cache


def test_free_refresh_never_reads_disputes(durable_database: Database, tmp_path: Path) -> None:
    """Refresh without a client/guard leaves rulings alone and writes no audit row."""
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True
    )
    job_id, requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )

    result = service.refresh(workspace_id, job_id)

    assert result["model_calls_made"] == 0
    with durable_database.session() as session:
        row = session.scalar(
            select(RequirementRuling).where(
                RequirementRuling.workspace_id == workspace_id,
                RequirementRuling.requirement_key == requirement_key,
            )
        )
        assert row is not None
        assert row.ruling == Ruling.DISPUTED_EXTRACTION.value
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "ruling.reversed_by_reextraction")
            )
            == 0
        )


def test_paid_refresh_skips_when_workspace_did_not_opt_in(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    """Even with a client/guard, an off flag never calls the extraction model."""
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=False
    )
    job_id, requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )
    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": "Lead a data platform team",
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    client, cache = _make_client(settings, provider, tmp_path)
    try:
        result = service.refresh(
            workspace_id,
            job_id,
            client=client,
        )
    finally:
        cache.close()

    assert result["model_calls_made"] == 0
    assert provider.calls == []
    with durable_database.session() as session:
        row = session.scalar(
            select(RequirementRuling).where(
                RequirementRuling.workspace_id == workspace_id,
                RequirementRuling.requirement_key == requirement_key,
            )
        )
        assert row is not None
        assert row.ruling == Ruling.DISPUTED_EXTRACTION.value


def test_paid_refresh_skips_when_no_disputes_exist(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    """No active disputes means the model is never asked, even with the flag on."""
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True
    )
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database))
    _ = workspace
    proposal = evaluation_commit(workspace_id)
    committed = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, root),
        proposal,
    )
    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": "Lead a data platform team",
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    client, cache = _make_client(settings, provider, tmp_path)
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )
    try:
        result = service.refresh(
            workspace_id,
            committed.job_posting_id,
            client=client,
        )
    finally:
        cache.close()

    assert result["model_calls_made"] == 0
    assert provider.calls == []


def test_paid_refresh_calls_extract_once_with_dispute_block(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    """One extraction call per invocation, with the rendered dispute block."""
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True
    )
    job_id, requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )
    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": "Lead a data platform team",
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    client, cache = _make_client(settings, provider, tmp_path)
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )
    try:
        result = service.refresh(
            workspace_id,
            job_id,
            client=client,
        )
    finally:
        cache.close()

    assert result["model_calls_made"] == 1
    assert len(provider.calls) == 1
    schema_name, plan = provider.calls[0]
    assert schema_name == "JobSpecExtraction"
    assert requirement_key in plan.task.text
    assert _DISPUTED_REQUIREMENT_TEXT in plan.task.text


def test_paid_refresh_reverses_only_when_cited_text_is_in_body(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    """The model overrules only when its cited span still appears in the body."""
    root = tmp_path / "artifacts"

    def _run(quote: str, body: str, slug: str) -> tuple[RequirementRuling | None, int]:
        workspace_id = _workspace_with_dispute_reextraction_setting(
            durable_database, enable_dispute_reextraction=True, slug=slug
        )
        job_id, requirement_key = _seed_disputed_posting(
            durable_database,
            root,
            workspace_id=workspace_id,
            disputed_text=_DISPUTED_REQUIREMENT_TEXT,
        )
        provider = _scripted_jobspec(
            {
                "title": "Data Platform Lead",
                "requirements": [
                    {
                        "text": _DISPUTED_REQUIREMENT_TEXT,
                        "importance": "must_have",
                        "quote_or_span": quote,
                    }
                ],
                "keywords": [],
                "hard_gates": [],
            }
        )
        client, cache = _make_client(settings, provider, tmp_path / f"cache-{quote[:5]}")
        service = PostingRefreshService(
            durable_database,
            storage=runtime_storage(durable_database, root),
            fetcher=ScriptedFetcher([FetchedPosting(body, "active", "scripted", utc_now(), 200)]),
        )
        try:
            service.refresh(
                workspace_id,
                job_id,
                client=client,
            )
        finally:
            cache.close()
        with durable_database.session() as session:
            row = session.scalar(
                select(RequirementRuling).where(
                    RequirementRuling.workspace_id == workspace_id,
                    RequirementRuling.requirement_key == requirement_key,
                )
            )
            reversal_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.workspace_id == workspace_id,
                        AuditEvent.action == "ruling.reversed_by_reextraction",
                    )
                )
                or 0
            )
        return row, reversal_count

    reversed_row, reversed_events = _run(
        "Lead a data platform team", _BODY_WITH_DISPUTED_TEXT, "reverses"
    )
    assert reversed_row is not None
    assert reversed_row.ruling == Ruling.REVERSED.value
    assert reversed_events == 1

    unchanged_row, unchanged_events = _run(
        "words the posting never states", _BODY_WITHOUT_DISPUTED_TEXT, "keeps"
    )
    assert unchanged_row is not None
    assert unchanged_row.ruling == Ruling.DISPUTED_EXTRACTION.value
    assert unchanged_events == 0


def test_paid_refresh_does_not_reverse_when_the_model_only_echoes_the_requirement(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    """A dispute reversal requires new evidence, not a self-referential citation.

    A cited span that normalizes to the requirement text trivially appears in the
    body it was extracted from, so it offers no new evidence.
    """
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True, slug="echo"
    )
    job_id, requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )
    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": _DISPUTED_REQUIREMENT_TEXT,
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    client, cache = _make_client(settings, provider, tmp_path / "cache-echo")
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )
    try:
        service.refresh(
            workspace_id,
            job_id,
            client=client,
        )
    finally:
        cache.close()

    with durable_database.session() as session:
        row = session.scalar(
            select(RequirementRuling).where(
                RequirementRuling.workspace_id == workspace_id,
                RequirementRuling.requirement_key == requirement_key,
            )
        )
        reversal_count = int(
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "ruling.reversed_by_reextraction",
                )
            )
            or 0
        )
    assert row is not None
    assert row.ruling == Ruling.DISPUTED_EXTRACTION.value
    assert reversal_count == 0


def test_paid_refresh_only_reads_and_reverses_disputes_for_the_refreshed_posting(
    durable_database: Database, tmp_path: Path, settings: Settings
) -> None:
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True
    )
    first_job_id, first_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
        posting_key="first",
    )
    second_job_id, second_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
        posting_key="second",
    )

    first_detail = job_search.job_detail(durable_database, first_job_id, workspace_id)
    second_detail = job_search.job_detail(durable_database, second_job_id, workspace_id)
    assert first_detail.disputed_extraction_ruling_count == 1
    assert second_detail.disputed_extraction_ruling_count == 1
    assert first_detail.disputed_fit_ruling_count == 0
    assert first_detail.enable_dispute_reextraction is True
    assert first_detail.enable_dispute_rematch is False

    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": "Lead a data platform team",
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    client, cache = _make_client(settings, provider, tmp_path)
    service = PostingRefreshService(
        durable_database,
        storage=runtime_storage(durable_database, root),
        fetcher=ScriptedFetcher(
            [FetchedPosting(_BODY_WITH_DISPUTED_TEXT, "active", "scripted", utc_now(), 200)]
        ),
    )
    try:
        service.refresh(
            workspace_id,
            first_job_id,
            client=client,
        )
    finally:
        cache.close()

    assert second_key not in provider.calls[0][1].task.text
    with durable_database.session() as session:
        rulings = {
            row.requirement_key: row.ruling
            for row in session.scalars(
                select(RequirementRuling).where(
                    RequirementRuling.requirement_key.in_([first_key, second_key])
                )
            )
        }
    assert rulings == {
        first_key: Ruling.REVERSED.value,
        second_key: Ruling.DISPUTED_EXTRACTION.value,
    }


def test_paid_refresh_worker_files_the_model_call_and_cost(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=True, slug="billed"
    )
    job_id, _requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )
    provider = _scripted_jobspec(
        {
            "title": "Data Platform Lead",
            "requirements": [
                {
                    "text": _DISPUTED_REQUIREMENT_TEXT,
                    "importance": "must_have",
                    "quote_or_span": "Lead a data platform team",
                }
            ],
            "keywords": [],
            "hard_gates": [],
        }
    )
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_with_reextraction",
        payload={"job_posting_id": job_id},
        idempotency_key="paid-refresh-billed",
    )
    worker = Worker(
        durable_database,
        worker_id="paid-refresh-worker",
        handlers={
            "posting.refresh_with_reextraction": PostingRefreshWithReextractionHandler(
                durable_database,
                storage=runtime_storage(durable_database, root),
                fetcher=ScriptedFetcher(
                    [
                        FetchedPosting(
                            _BODY_WITH_DISPUTED_TEXT,
                            "active",
                            "scripted",
                            utc_now(),
                            200,
                        )
                    ]
                ),
                provider=provider,
            )
        },
    )

    assert worker.run_once()
    assert queue.get(task.id).status == "completed"
    with durable_database.session() as session:
        calls = session.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()
        costs = session.scalars(
            select(CostRecord).where(CostRecord.model_call_id.in_([call.id for call in calls]))
        ).all()
    assert [(call.stage, call.origin) for call in calls] == [
        ("jobspec", "posting.refresh_with_reextraction")
    ]
    assert len(costs) == 1


def test_paid_refresh_worker_short_circuits_when_flag_off(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag-off workspace never resolves provider credentials or bills a call."""
    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=False, slug="short-circuit"
    )
    job_id, _requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )

    def _fail_resolve(*args: object, **kwargs: object) -> object:
        raise AssertionError("resolve() must not be called when the flag is off.")

    monkeypatch.setattr("tailorcv.application.discovery.paid_task.resolve", _fail_resolve)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_with_reextraction",
        payload={"job_posting_id": job_id},
        idempotency_key="paid-refresh-flag-off",
    )
    worker = Worker(
        durable_database,
        worker_id="paid-refresh-off-worker",
        handlers={
            "posting.refresh_with_reextraction": PostingRefreshWithReextractionHandler(
                durable_database,
                storage=runtime_storage(durable_database, root),
                fetcher=ScriptedFetcher(
                    [
                        FetchedPosting(
                            _BODY_WITH_DISPUTED_TEXT,
                            "active",
                            "scripted",
                            utc_now(),
                            200,
                        )
                    ]
                ),
            )
        },
    )

    assert worker.run_once()
    assert queue.get(task.id).status == "completed"
    with durable_database.session() as session:
        calls = session.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()
        costs = session.scalars(select(CostRecord)).all()
        observation_count = int(
            session.scalar(
                select(func.count())
                .select_from(PostingObservation)
                .where(PostingObservation.job_posting_id == job_id)
            )
            or 0
        )
    assert calls == []
    assert costs == []
    assert observation_count == 2


def test_learning_policy_default_disables_dispute_reextraction() -> None:
    """A seed without the field validates and the flag defaults to False."""
    default = Settings()
    assert default.learning.enable_dispute_reextraction is False
    assert default.learning.enable_dispute_rematch is False

    stored = Settings.model_validate({"postings": {"enable_hint": True}})
    assert stored.learning.enable_dispute_reextraction is False
    assert stored.learning.enable_dispute_rematch is False

    enabled = Settings(
        learning=LearningLoopPolicy(
            enable_dispute_reextraction=True,
            enable_dispute_rematch=True,
        )
    )
    assert enabled.learning.enable_dispute_reextraction is True
    assert enabled.learning.enable_dispute_rematch is True


def test_job_detail_read_refuses_an_archived_workspace(
    durable_database: Database, tmp_path: Path
) -> None:
    """Job detail on an archived workspace raises NotFoundError."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)

    with durable_database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.archived_at = utc_now()
        session.commit()

    with pytest.raises(NotFoundError, match=workspace_id):
        job_search.job_detail(durable_database, job_id, workspace_id)


def test_job_detail_reads_an_archived_job_under_a_live_workspace(
    durable_database: Database, tmp_path: Path
) -> None:
    """Job detail returns an archived job when the workspace is live."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        job.archived_at = utc_now()
        session.commit()

    detail = job_search.job_detail(durable_database, job_id, workspace_id)
    assert detail.job_posting_id == job_id
    assert detail.archived_at is not None


def test_job_detail_surfaces_a_ruling_orphaned_by_the_current_evaluation(
    durable_database: Database, tmp_path: Path
) -> None:
    """A dispute surfaces when the current evaluation dropped its requirement key."""
    from tailorcv.domain import new_id
    from tailorcv.infrastructure.persistence.models import JobEvaluation

    root = tmp_path / "artifacts"
    workspace_id = _workspace_with_dispute_reextraction_setting(
        durable_database, enable_dispute_reextraction=False, slug="orphan"
    )
    job_id, requirement_key = _seed_disputed_posting(
        durable_database,
        root,
        workspace_id=workspace_id,
        disputed_text=_DISPUTED_REQUIREMENT_TEXT,
    )

    with durable_database.session() as session:
        ruling_row = session.scalar(
            select(RequirementRuling).where(
                RequirementRuling.workspace_id == workspace_id,
                RequirementRuling.requirement_key == requirement_key,
            )
        )
        assert ruling_row is not None
        ruling_row.ruling = Ruling.DISPUTED_FIT.value
        v1 = session.scalar(select(JobEvaluation).where(JobEvaluation.job_posting_id == job_id))
        assert v1 is not None
        audit = audit_columns("test", "test", None)
        v2_id = new_id()
        session.add(
            JobEvaluation(
                id=v2_id,
                job_posting_id=job_id,
                version_number=2,
                supersedes_evaluation_id=v1.id,
                evaluated_at=utc_now(),
                decision="apply_now",
                rationale="test v2 with different requirements",
                payload_sha256="00" * 32,
                **audit,
            )
        )
        session.add(
            PostingRequirement(
                id=new_id(),
                job_posting_id=job_id,
                job_evaluation_id=v2_id,
                requirement_id="r01",
                requirement_key="orphan-test-key-0001",
                text="A completely different requirement.",
                importance="must_have",
                position=1,
                **audit,
            )
        )
        session.commit()

    detail = job_search.job_detail(durable_database, job_id, workspace_id)
    assert detail.orphaned_ruling_count == 1
    (orphan,) = detail.orphaned_rulings
    assert orphan.requirement_key == requirement_key
    assert orphan.ruling == "disputed_fit"
    assert orphan.requirement_text == _DISPUTED_REQUIREMENT_TEXT
    assert detail.disputed_fit_ruling_count == 0


def test_a_refresh_reads_as_a_crawl_and_stores_the_reads_robots_basis(
    durable_database: Database, tmp_path: Path
) -> None:
    """A refresh reads under robots, and its basis reaches the observation."""
    root = tmp_path / "artifacts"
    workspace_id, job_id = _job(durable_database, root)
    changed_body = (
        "Lead product and data platform governance. Canada remote role.\n"
        "Required: production SQL experience across a modern data stack."
    )
    fetcher = ScriptedFetcher(
        [
            FetchedPosting(
                text=changed_body,
                posting_status="active",
                method="scripted",
                fetched_at=utc_now(),
                http_status=200,
                robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
            )
        ]
    )
    _run_refresh(durable_database, root, fetcher, workspace_id, job_id, "known-posting-intent")

    assert fetcher.intents == [PostingReadIntent.CRAWL]
    with durable_database.session() as session:
        observation = session.scalar(
            select(PostingObservation)
            .where(PostingObservation.job_posting_id == job_id)
            .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
            .limit(1)
        )
        assert observation is not None
        assert observation.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ.value
