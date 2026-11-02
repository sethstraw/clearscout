"""A pending row's publisher facts and content-access state after a fresh read."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
    pending_discovery_readiness,
)
from tailorcv.application.discovery.posting_text import publisher_posted_date
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.discovery.scan_staging import checks as staging_checks
from tailorcv.application.discovery.screening import ScreeningCode, ScreeningStanding
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing
from tailorcv.application.failures import (
    ConflictError,
    PublisherUnverifiedRefreshError,
    ValidationError,
)
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import SearchParametersService
from tailorcv.domain import (
    PendingDiscoveryReadiness,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    utc_now,
)
from tailorcv.domain.search_parameters import JobSearchParameters, Seniority
from tailorcv.domain.staged_facts import (
    ListingIdentityTrust,
    PostedDateGrade,
    PublishedFact,
    StagedListingFacts,
)
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    PendingDiscovery,
    PendingPostingContent,
    Source,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedContentResolver, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    dismiss_pending_discovery,
    mark_reset_archived,
    pending_posting_content,
    scan_workspace,
    seed_pending_discovery,
    titles,
)


@pytest.mark.parametrize("publisher_employer", ["", "Globex"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("source_origin", ["api", "search.guided_campaign"])
def test_oracle_refresh_uses_the_registered_employer_only_when_the_publisher_omits_it(
    durable_database: Database, publisher_employer: str, enabled: bool, source_origin: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    tenant_placeholder = "Recruiting tenant acme (company not stated)"
    source_id = board_source(
        durable_database,
        workspace_id,
        "Acme",
        "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
        enabled=enabled,
        origin=source_origin,
    )
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer=tenant_placeholder,
        created_at=utc_now(),
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.discovery_url = (
            "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
        )
        pending.canonical_url = pending.discovery_url
        pending.staged_facts = StagedListingFacts().as_json()
        source = session.get(Source, source_id)
        assert source is not None
        source.name = "Board display label"
    checked = PostingCheck(
        status="readable",
        note="Oracle published this posting metadata.",
        platform="oracle",
        employer=publisher_employer,
        title="Data Platform Lead",
        location="Remote",
        facts=replace(
            published_listing_facts("oracle", locations="Remote"),
            employer=PublishedFact(
                values=(publisher_employer,) if publisher_employer else (),
                provenance="oracle.employer" if publisher_employer else "",
                complete=bool(publisher_employer),
            ),
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)
    refreshed_again = service.refresh_pending_discovery(workspace_id, pending_id)

    expected = publisher_employer or "Acme"
    provenance = "oracle.employer" if publisher_employer else "source.registered_employer"
    assert refreshed.employer == refreshed_again.employer == expected
    assert refreshed.staged_facts.employer.values == [expected]
    assert refreshed.staged_facts.employer.provenance == provenance
    assert refreshed_again.staged_facts.employer.provenance == provenance
    with durable_database.session() as session:
        stored = session.get(PendingDiscovery, pending_id)
        assert stored is not None
        facts = StagedListingFacts.from_json(stored.staged_facts)
        assert stored.employer == expected
        assert facts.employer == PublishedFact(
            values=(expected,), provenance=provenance, complete=True
        )
        assert facts.publisher_vouched is bool(publisher_employer or source_origin == "api")


@pytest.mark.parametrize("prior_trust", list(ListingIdentityTrust))
def test_oracle_refresh_keeps_a_publisher_title_excluded_with_a_registered_employer(
    durable_database: Database, tmp_path: Path, prior_trust: ListingIdentityTrust
) -> None:
    workspace_id = scan_workspace(durable_database)
    title = "Director Product Management"
    url = "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
    source_id = board_source(
        durable_database,
        workspace_id,
        "Acme",
        "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
        origin="api",
    )
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Acme",
        title=title,
        created_at=utc_now(),
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending = session.get_one(PendingDiscovery, pending_id)
        pending.discovery_url = pending.canonical_url = url
    storage = runtime_storage(durable_database, tmp_path)
    content = PostingContentResolutionService(
        durable_database, UnexpectedContentResolver(), storage=storage
    )
    [target] = content.preflight(workspace_id, [pending_id]).targets
    content.store_assisted_result(
        workspace_id,
        target,
        ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=b"Director Product Management\nOwn the product roadmap.",
        ),
        None,
    )
    with durable_database.session() as session:
        pending = session.get_one(PendingDiscovery, pending_id)
        pending.staged_facts = StagedListingFacts(identity_trust=prior_trust).as_json()
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead"), excluded_titles=[title])
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=storage,
        posting_checker=lambda _url, **_: PostingCheck(
            status="readable",
            note="Oracle published this posting metadata.",
            platform="oracle",
            title=title,
            location="Remote",
            facts=published_listing_facts("oracle", locations="Remote"),
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)
    page = service.queries.pending_discovery_page(workspace_id)

    assert refreshed.employer == "Acme"
    assert refreshed.title == title
    assert refreshed.staged_facts.employer.provenance == "source.registered_employer"
    assert refreshed.screening_standing is ScreeningStanding.REFUSE
    assert ScreeningCode.TITLE_EXCLUDED in {
        finding.code for finding in refreshed.screening_findings
    }
    assert refreshed.readiness is PendingDiscoveryReadiness.SCREENING_REFUSED
    assert refreshed.staged_facts.identity_trust is ListingIdentityTrust.PUBLISHER
    assert page.ready.total == 0
    assert [row.id for row in page.attention.items] == [pending_id]
    assert page.attention.items[0].readiness is PendingDiscoveryReadiness.SCREENING_REFUSED


def test_oracle_refresh_keeps_an_earlier_published_employer_when_the_fresh_read_omits_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database,
        workspace_id,
        "Acme",
        "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
    )
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    employer_fact = PublishedFact(values=("Globex",), provenance="oracle.employer", complete=True)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.discovery_url = (
            "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
        )
        pending.canonical_url = pending.discovery_url
        pending.staged_facts = replace(
            published_listing_facts("oracle", locations="Remote"),
            employer=employer_fact,
        ).as_json()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: PostingCheck(
            status="readable",
            note="Oracle published this posting metadata.",
            platform="oracle",
            title="Data Platform Lead",
            location="Remote",
            facts=published_listing_facts("oracle", locations="Remote"),
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)

    assert refreshed.employer == "Globex"
    assert refreshed.staged_facts.employer.values == ["Globex"]
    assert refreshed.staged_facts.employer.provenance == "oracle.employer"
    with durable_database.session() as session:
        stored = session.get(PendingDiscovery, pending_id)
        assert stored is not None
        facts = StagedListingFacts.from_json(stored.staged_facts)
        assert stored.employer == "Globex"
        assert facts.employer == employer_fact
        assert facts.publisher_vouched


@pytest.mark.parametrize(
    "registration",
    ["missing", "archived_source", "archived_employer", "unbound", "aggregator"],
)
def test_oracle_refresh_keeps_a_missing_employer_when_registration_cannot_supply_it(
    durable_database: Database, registration: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = None
    if registration != "missing":
        source_id = board_source(
            durable_database,
            workspace_id,
            "Acme",
            "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
        )
        with durable_database.session() as session:
            source = session.get(Source, source_id)
            assert source is not None
            if registration == "archived_source":
                source.archived_at = utc_now()
            elif registration == "archived_employer":
                employer = session.get(Employer, source.employer_id)
                assert employer is not None
                employer.archived_at = utc_now()
            elif registration == "unbound":
                source.employer_id = None
            elif registration == "aggregator":
                source.source_type = "aggregator"
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="",
        created_at=utc_now(),
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.discovery_url = (
            "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
        )
        pending.canonical_url = pending.discovery_url
    checked = PostingCheck(
        status="readable",
        note="Oracle published this posting metadata.",
        platform="oracle",
        title="Data Platform Lead",
        facts=published_listing_facts("oracle"),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)

    assert refreshed.employer == ""
    assert refreshed.staged_facts.employer.values == []
    assert refreshed.staged_facts.employer.provenance == ""


def test_a_non_oracle_refresh_keeps_an_employer_the_publisher_does_not_restate(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(durable_database, workspace_id, "Acme", "https://jobs.lever.co/acme")
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: PostingCheck(
            status="readable",
            note="Lever published this posting metadata.",
            platform="lever",
            title="Data Platform Lead",
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)

    assert refreshed.employer == "Globex"
    assert refreshed.staged_facts.employer.values == []


def test_a_direct_publisher_refresh_cannot_change_a_dismissed_pending_row(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Acme", created_at=utc_now()
    )
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with pytest.raises(ConflictError, match="dismissed"):
        service.staging.checks.refresh_pending_discovery(
            workspace_id,
            pending_id,
            PostingCheck(
                status="readable",
                note="Oracle published this posting metadata.",
                platform="oracle",
                employer="Globex",
                title="Changed title",
            ),
            actor="local_user",
            origin="test",
            correlation_id=None,
        )

    with durable_database.session() as session:
        stored = session.get(PendingDiscovery, pending_id)
        assert stored is not None
        assert stored.employer == "Acme"
        assert stored.title == "Data Platform Lead"
    assert service.list_pending_discoveries(workspace_id) == []


def test_refreshing_an_active_pending_discovery_replaces_publisher_facts(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="readable",
        note="Workday published this posting metadata.",
        platform="workday",
        title="Principal Product Manager",
        location="USA - Waltham, MA",
        facts=published_listing_facts(
            "workday",
            locations="USA - Waltham, MA",
            arrangements="Hybrid (8 days/month)",
        ),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://example.wd1.myworkdayjobs.com/Careers/job/Principal-Product-Manager_R-1",
        date(2026, 8, 24),
        title="Unverified hint",
    )
    assert staged is not None

    refreshed = service.refresh_pending_discovery(
        workspace_id,
        staged.pending_discovery.id,
        correlation_id="refresh-1",
    )

    assert refreshed.title == "Principal Product Manager"
    assert refreshed.location == "USA - Waltham, MA"
    assert refreshed.staged_facts.arrangements.values == ["Hybrid (8 days/month)"]

    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.publisher_facts_refreshed"
            )
        )
    assert event is not None
    assert event.entity_id == staged.pending_discovery.id
    assert event.details["platform"] == "workday"
    assert event.correlation_id == "refresh-1"


def test_refresh_withdraws_a_pending_discovery_the_publisher_no_longer_lists(
    durable_database: Database,
) -> None:
    """Refresh validates liveness: a confirmed-closed posting is withdrawn, not errored."""
    workspace_id = scan_workspace(durable_database)
    readable = PostingCheck(
        status="readable",
        note="Ashby published this posting metadata.",
        platform="ashby",
        title="Principal Product Manager",
        location="Remote, Canada",
        facts=published_listing_facts("ashby", locations="Remote, Canada"),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: readable,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.ashbyhq.com/globex/3c9a7e14-8b2d-4f6a-a1c5-7d0e9b2f4c81",
        date(2026, 8, 24),
        title="Principal Product Manager",
    )
    assert staged is not None

    service.posting_checker = lambda _url, **_: PostingCheck(
        status="closed",
        note="Ashby holds 5 open jobs and none of them is 3c9a7e14, so it has closed.",
        platform="ashby",
    )
    service.refresh_pending_discovery(
        workspace_id, staged.pending_discovery.id, correlation_id="refresh-closed"
    )

    history = service.list_pending_discoveries(workspace_id, closed_only=True)
    assert len(history) == 1
    assert history[0].withdrawn_at is not None
    assert service.list_pending_discoveries(workspace_id) == []

    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.withdrawn_on_refresh")
        )
    assert event is not None
    assert event.entity_id == staged.pending_discovery.id
    assert event.correlation_id == "refresh-closed"


def test_refresh_restores_a_withdrawn_row_the_publisher_shows_open_again(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: PostingCheck(
            status="closed", note="Ashby no longer lists 3c9a7e14.", platform="ashby"
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.ashbyhq.com/globex/3c9a7e14-8b2d-4f6a-a1c5-7d0e9b2f4c81",
        date(2026, 8, 24),
        title="Principal Product Manager",
    )
    assert staged is not None
    service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)
    assert service.list_pending_discoveries(workspace_id) == []

    service.posting_checker = lambda _url, **_: PostingCheck(
        status="readable",
        note="Ashby published this posting metadata.",
        platform="ashby",
        title="Principal Product Manager, Clinical Data",
        location="Remote, Canada",
        facts=published_listing_facts("ashby", locations="Remote, Canada"),
    )
    refreshed = service.refresh_pending_discovery(
        workspace_id, staged.pending_discovery.id, correlation_id="refresh-reopened"
    )

    assert refreshed.withdrawn_at is None
    assert refreshed.title == "Principal Product Manager, Clinical Data"
    assert [row.id for row in service.list_pending_discoveries(workspace_id)] == [
        staged.pending_discovery.id
    ]
    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "pending_discovery.relisted_on_refresh")
        )
    assert event is not None
    assert event.correlation_id == "refresh-reopened"


def test_refresh_relisting_a_variant_under_a_withdrawn_head_lists_it_on_its_own(
    durable_database: Database,
) -> None:
    """The head withdrew after the variant did, so its release skipped the variant."""
    workspace_id = scan_workspace(durable_database)
    withdrawn = utc_now()
    head_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=withdrawn
    )
    variant_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=withdrawn
    )
    with durable_database.session() as session:
        head = session.get(PendingDiscovery, head_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert head is not None and variant is not None
        variant.regional_variant_of = head_id
        variant.withdrawn_at = withdrawn
        head.withdrawn_at = withdrawn
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: PostingCheck(
            status="readable",
            note="Greenhouse published this posting metadata.",
            platform="greenhouse",
            employer="Globex",
            title="Data Platform Lead",
            location="Remote",
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.refresh_pending_discovery(workspace_id, variant_id)

    page = SourceScanQueries(durable_database).pending_discovery_page(workspace_id)
    assert [item.id for item in (*page.ready.items, *page.attention.items)] == [variant_id]


def test_refresh_checks_a_recovered_row_at_the_address_its_evidence_reads(
    durable_database: Database, tmp_path: Path
) -> None:
    """A listing that closed cannot withdraw a row recovered at a live publisher address."""
    workspace_id = scan_workspace(durable_database)
    listing_url = "https://listings.example.test/view/operations-lead-4821"
    recovered_url = "https://careers.example.test/jobs/operations-lead"
    readable = PostingCheck(
        status="readable",
        note="The publisher page names this posting.",
        platform="greenhouse",
        title="Operations Lead",
    )
    closed = PostingCheck(status="closed", note="The listing page says it has closed.")
    checked: list[str] = []

    def publisher(url: str, **_: object) -> PostingCheck:
        checked.append(url)
        return closed if url == listing_url else readable

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_checker=publisher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id, listing_url, date(2026, 8, 24), title="Operations Lead"
    )
    assert staged is not None
    pending_id = staged.pending_discovery.id
    content = PostingContentResolutionService(
        durable_database,
        UnexpectedContentResolver(),
        storage=runtime_storage(durable_database, tmp_path),
    )
    [target] = content.preflight(workspace_id, [pending_id]).targets
    content.store_assisted_result(
        workspace_id,
        target,
        ResolvedPostingContent(
            requested_url=recovered_url,
            final_url=recovered_url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=b"Operations Lead\nRun the regional operations desk.",
        ),
        None,
    )
    checked.clear()

    refreshed = service.refresh_pending_discovery(workspace_id, pending_id)

    assert refreshed.withdrawn_at is None
    assert refreshed.canonical_url == recovered_url
    assert checked == [recovered_url]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None and row.current_content_id is not None
        evidence = session.get(PendingPostingContent, row.current_content_id)
        assert pending_discovery_readiness(row, evidence) is PendingDiscoveryReadiness.READY


def test_refresh_replaces_stored_arrangement_with_publisher_contradiction(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    old = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Data Architect",
        location="Remote (Canada)",
        facts=published_listing_facts("lever", locations="Remote (Canada)", arrangements="hybrid"),
    )
    corrected = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Data Architect",
        location="Remote (Canada)",
        facts=published_listing_facts(
            "lever",
            locations="Remote (Canada)",
            arrangements_unresolved="hybrid",
        ),
    )
    checks = iter((old, corrected))
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: next(checks),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/00000000-0000-0000-0000-000000000000",
        date(2026, 8, 24),
        check_publisher_facts=True,
    )
    assert staged is not None
    assert staged.pending_discovery.staged_facts.arrangements.values == ["hybrid"]

    refreshed = service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    assert refreshed.staged_facts.arrangements.values == []
    assert refreshed.staged_facts.arrangements.unresolved_values == ["hybrid"]
    assert refreshed.staged_facts.arrangements.complete is False


def test_a_refresh_keeps_a_published_fact_the_fresh_read_states_nothing_about(
    durable_database: Database,
) -> None:
    """A publisher silent on a field this time is not the publisher retracting it."""
    workspace_id = scan_workspace(durable_database)
    staged_check = PostingCheck(
        status="readable",
        note="Workday published this posting metadata.",
        platform="workday",
        title="Principal Product Manager",
        location="Northtown, Freedonia",
        facts=published_listing_facts(
            "workday",
            locations="Northtown, Freedonia",
            employment_types="Full time",
        ),
    )
    quieter_check = replace(
        staged_check,
        facts=published_listing_facts("workday", locations="Northtown, Freedonia"),
    )
    served = [staged_check, quieter_check]
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: served.pop(0),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://example.wd1.myworkdayjobs.com/Careers/job/Principal-Product-Manager_R-2",
        date(2026, 8, 24),
        check_publisher_facts=True,
    )
    assert staged is not None

    service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    (row,) = service.list_pending_discoveries(workspace_id)
    assert list(row.staged_facts.employment_types.values) == ["Full time"]
    assert list(row.staged_facts.locations.values) == ["Northtown, Freedonia"]


def test_publisher_refreshes_keep_the_earliest_posted_date_and_record_a_repost(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = scan_workspace(durable_database)

    def check(posted: str) -> PostingCheck:
        facts = published_listing_facts("workday", locations="Northtown, Freedonia")
        return PostingCheck(
            status="readable",
            note="Workday published this posting metadata.",
            platform="workday",
            title="Principal Product Manager",
            location="Northtown, Freedonia",
            facts=replace(
                facts,
                posted_date=publisher_posted_date(
                    posted, basis="jobPostingInfo.startDate", grade=PostedDateGrade.BOARD_DATE
                ),
            ),
        )

    served = [check("2026-08-01"), check("2026-08-20"), check("")]
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: served.pop(0),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://example.wd1.myworkdayjobs.com/Careers/job/Principal-Product-Manager_R-2",
        date(2026, 8, 2),
        check_publisher_facts=True,
    )
    assert staged is not None
    monkeypatch.setattr(staging_checks, "utc_now", lambda: datetime(2026, 8, 21, 9, tzinfo=UTC))

    service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)
    service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)
    assert served == []

    (row,) = service.list_pending_discoveries(workspace_id)
    posted = row.staged_facts.posted_date
    assert posted is not None
    assert posted.day == date(2026, 8, 1)
    assert posted.earliest.read_on == date(2026, 8, 2)
    assert posted.reposted
    assert (posted.latest.normalized_value, posted.latest.read_on) == (
        "2026-08-20",
        date(2026, 8, 21),
    )
    assert posted.grade is PostedDateGrade.BOARD_DATE


def test_refreshing_a_pending_with_an_active_evaluation_task_raises_conflict(
    durable_database: Database,
) -> None:
    from tailorcv.application.tasks import EvaluationWorkflowPayload

    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="readable",
        note="Workday published this posting metadata.",
        platform="workday",
        title="Principal Product Manager",
        location="USA - Waltham, MA",
        facts=published_listing_facts("workday", locations="USA - Waltham, MA"),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://example.wd1.myworkdayjobs.com/Careers/job/Principal-Product-Manager_R-1",
        date(2026, 8, 24),
        title="Unverified hint",
    )
    assert staged is not None
    pending_id = staged.pending_discovery.id
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload=EvaluationWorkflowPayload(pending_discovery_id=pending_id).model_dump(),
        idempotency_key=f"evaluate-{pending_id}",
    )

    with pytest.raises(ConflictError, match="in flight"):
        service.refresh_pending_discovery(workspace_id, pending_id)

    queue = service.list_pending_discoveries(workspace_id)
    assert [row.title for row in queue] == ["Unverified hint"]


def test_a_failed_refresh_names_the_row_it_belongs_to(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database)
    readable = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Director of Product Management",
        location="Remote",
        facts=published_listing_facts("lever", locations="Remote"),
    )
    unreadable = PostingCheck(
        status="unreadable", note="Lever returned HTTP 503.", platform="lever"
    )
    checks = iter((readable, unreadable))
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: next(checks),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/00000000-0000-0000-0000-000000000000",
        date(2026, 8, 24),
        employer="Globex",
        check_publisher_facts=True,
    )
    assert staged is not None

    with pytest.raises(ValidationError) as raised:
        service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    message = str(raised.value)
    assert message.startswith("Globex, Director of Product Management: ")
    assert "Lever returned HTTP 503." in message


@pytest.mark.parametrize("status", ["404", "410"])
def test_a_dead_link_refresh_says_the_posting_may_still_exist_at_the_employer(
    durable_database: Database, status: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    readable = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Director of Product Management",
        location="Remote",
        facts=published_listing_facts("lever", locations="Remote"),
    )
    dead = PostingCheck(
        status="unreadable", note=f"Lever returned HTTP {status}.", platform="lever"
    )
    checks = iter((readable, dead))
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: next(checks),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/00000000-0000-0000-0000-000000000001",
        date(2026, 8, 24),
        employer="Globex",
        check_publisher_facts=True,
    )
    assert staged is not None

    with pytest.raises(ValidationError) as raised:
        service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    message = str(raised.value)
    assert f"Lever returned HTTP {status}." in message
    assert "This link is dead" in message
    assert "Globex may still be advertising the posting on its own board" in message

    # The dead link kept the stored facts rather than clearing them.
    (row,) = service.list_pending_discoveries(workspace_id)
    assert row.title == "Director of Product Management"


@pytest.mark.parametrize(
    ("note", "unverified"),
    [
        ("Workday returned HTTP 403 (S22).", True),
        ("Lever returned HTTP 404.", False),
    ],
)
def test_a_dead_link_stays_a_failure_while_an_unconfirmed_read_is_only_unverified(
    durable_database: Database, note: str, unverified: bool
) -> None:
    workspace_id = scan_workspace(durable_database)
    readable = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Director of Product Management",
        location="Remote",
        facts=published_listing_facts("lever", locations="Remote"),
    )
    checks = iter((readable, PostingCheck(status="unreadable", note=note, platform="lever")))
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: next(checks),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/00000000-0000-0000-0000-000000000002",
        date(2026, 8, 24),
        employer="Globex",
        check_publisher_facts=True,
    )
    assert staged is not None

    with pytest.raises(ValidationError) as raised:
        service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    assert isinstance(raised.value, PublisherUnverifiedRefreshError) is unverified


def test_board_staging_persists_and_projects_the_posting_content_access_state(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    listing = SourceListing(
        "Globex",
        "Platform Lead",
        "https://jobs.example.test/opening-1",
        content_status=PostingContentAccessStatus.RENDER_REQUIRED,
        content_note="The publisher returned an application shell without posting text.",
    )

    service.stage_listings(
        workspace_id,
        [listing],
        utc_now().date(),
        reason="Publisher index discovery.",
    )

    [pending] = service.list_pending_discoveries(workspace_id)
    assert pending.content_status is PostingContentAccessStatus.RENDER_REQUIRED
    assert pending.content_note == (
        "The publisher returned an application shell without posting text."
    )


def test_publisher_metadata_does_not_replace_a_retained_content_refusal(
    durable_database: Database,
) -> None:

    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    listing = SourceListing(
        "Globex",
        "Platform Lead",
        "https://jobs.example.test/refused",
        content_status=PostingContentAccessStatus.READABLE,
        content_note="Publisher metadata available.",
    )
    service.stage_listings(workspace_id, [listing], utc_now().date(), reason="First scan.")
    [projected] = service.list_pending_discoveries(workspace_id)
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, str(projected.id))
        assert pending is not None
        content = pending_posting_content(
            workspace_id,
            pending.id,
            listing.url,
            content_status=PostingContentAccessStatus.POLICY_REFUSED,
        )
        session.add(content)
        session.flush()
        pending.current_content_id = content.id
        pending.content_status = PostingContentAccessStatus.POLICY_REFUSED
        pending.content_note = "The body read was refused."
    service.stage_listings(workspace_id, [listing], utc_now().date(), reason="Metadata refresh.")
    [projected] = service.list_pending_discoveries(workspace_id)
    assert projected.content_status is PostingContentAccessStatus.POLICY_REFUSED
    assert projected.content_note == "The body read was refused."


def test_manual_staging_defaults_to_not_yet_read_content(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.stage_found_posting(
        workspace_id,
        "https://jobs.example.test/opening-1",
        utc_now().date(),
    )

    assert result is not None
    assert result.pending_discovery.content_status is PostingContentAccessStatus.NOT_YET_READ
    assert result.pending_discovery.content_note == ""


def test_readable_publisher_check_does_not_claim_body_content_was_retained(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="readable",
        note="Publisher text was read.",
        title="Platform Lead",
        employer="Globex",
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.stage_found_posting(
        workspace_id,
        "https://jobs.example.test/opening-1",
        utc_now().date(),
        check_publisher_facts=True,
    )

    assert result is not None
    assert result.pending_discovery.content_status is PostingContentAccessStatus.NOT_YET_READ
    assert result.pending_discovery.content_note == ""


def test_requeue_without_a_new_read_preserves_the_content_access_state(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    first = service.stage_found_posting(
        workspace_id,
        "https://jobs.example.test/opening-1",
        utc_now().date(),
        content_status=PostingContentAccessStatus.ACCESS_BLOCKED,
        content_note="The publisher refused the anonymous request.",
    )
    assert first is not None
    mark_reset_archived(durable_database, workspace_id, first.pending_discovery.id)

    requeued = service.stage_found_posting(
        workspace_id,
        first.pending_discovery.discovery_url,
        utc_now().date(),
    )

    assert requeued is not None
    assert requeued.pending_discovery.content_status is PostingContentAccessStatus.ACCESS_BLOCKED
    assert requeued.pending_discovery.content_note == (
        "The publisher refused the anonymous request."
    )
