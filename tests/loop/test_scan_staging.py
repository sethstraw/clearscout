"""Publisher metadata updates keep regional-variant families internally consistent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select

from clearscout.application.discovery.board_scan import (
    DiscoveryRefreshBatchHandler,
    SourceScanService,
)
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
    pending_discovery_readiness,
)
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.hints import PendingDiscoveryHintsService
from clearscout.application.discovery.pending import (
    dismissed_pending_discovery_ids,
    reset_pending_queue,
    set_pending_markers,
)
from clearscout.application.discovery.posting_refresh import PostingRefreshService
from clearscout.application.discovery.posting_text import (
    publisher_closing_date,
    publisher_posted_date,
)
from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.scan_staging import (
    CAMPAIGN_BOARD_LISTING_REASON,
    CAMPAIGN_FOUND_POSTING_REASON,
    FOUND_POSTING_REASON,
    DiscoveryStaging,
    WithdrawingRead,
    queue_absence_checks,
    staged_from_single_posting,
)
from clearscout.application.discovery.scan_staging import service as staging_service
from clearscout.application.discovery.source_reading import PostingCheck, SourceListing
from clearscout.application.failures import (
    ConflictError,
    NotFoundError,
    PublisherUnverifiedRefreshError,
    ValidationError,
)
from clearscout.application.job_search.models import DispositionApply, DispositionSelection
from clearscout.application.tasks import TaskQueueService
from clearscout.application.tasks.commands import TaskCommands
from clearscout.application.tasks.events import TaskEventLog
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain import (
    PendingDiscoveryReadiness,
    PendingWithdrawalCause,
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    new_id,
    utc_now,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.identity import normalize_url, url_sha256
from clearscout.domain.records import PostingReadIntent, SourceReadOutcome
from clearscout.domain.staged_facts import (
    FreshnessState,
    ListingIdentityTrust,
    PostedDateGrade,
    PublishedFact,
    PublisherClosingDate,
    StagedListingFacts,
)
from clearscout.infrastructure.artifacts.store import file_uri_path
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    BackgroundTask,
    Employer,
    JobPosting,
    PendingDiscovery,
    PendingPostingContent,
    PostingUrl,
    RelevanceReview,
    RelevanceReviewDispute,
    SourceScan,
)
from clearscout.infrastructure.web_fetch.board_mappings.himalayas import HIMALAYAS_MAPPING
from clearscout.infrastructure.web_fetch.board_mappings.mapper import Posting, source_listing
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from tests.support.artifacts import runtime_storage
from tests.support.doubles import (
    ScriptedPage,
    UnexpectedContentResolver,
    UnexpectedPostingFetcher,
)
from tests.support.scenarios import (
    board_source,
    dismiss_pending_discovery,
    employer_board,
    mark_reset_archived,
    pending_posting_content,
    record_relevance_review,
    scan_workspace,
)


def _workspace(database: Database, artifact_root: Path) -> str:
    return (
        WorkspaceService(database, runtime_storage(database, artifact_root))
        .create(slug="scan-staging", name="Scan staging")
        .id
    )


def _pending(
    database: Database,
    workspace_id: str,
    *,
    url: str,
    title: str = "Operations Lead",
    employer: str = "Acme",
    first_seen_on: date,
    source_id: str | None = None,
    requisition_id: str = "",
    variant_of: str | None = None,
) -> str:
    with database.session() as session:
        pending = PendingDiscovery(
            id=new_id(),
            workspace_id=workspace_id,
            fingerprint=url_sha256(url),
            employer=employer,
            title=title,
            location="Remote",
            requisition_id=requisition_id,
            discovery_url=url,
            canonical_url=url,
            reason="Board scan matched the title filter.",
            first_seen_on=first_seen_on,
            source_id=source_id,
            regional_variant_of=variant_of,
        )
        session.add(pending)
        session.flush()
        return pending.id


def _staging(database: Database, artifact_root: Path) -> DiscoveryStaging:
    return DiscoveryStaging(
        database,
        PostingRefreshService(
            database,
            storage=runtime_storage(database, artifact_root),
            fetcher=UnexpectedPostingFetcher(),
        ),
    )


def _family(database: Database, workspace_id: str) -> tuple[str, str, str, str]:
    anchor_url = "https://example.test/jobs/anchor"
    primary_url = "https://example.test/jobs/operations-lead"
    variant_url = "https://example.test/jobs/operations-lead-variant"
    anchor_id = _pending(database, workspace_id, url=anchor_url, first_seen_on=date(2026, 8, 23))
    primary_id = _pending(database, workspace_id, url=primary_url, first_seen_on=date(2026, 8, 24))
    variant_id = _pending(database, workspace_id, url=variant_url, first_seen_on=date(2026, 8, 25))
    with database.session() as session:
        primary = session.get(PendingDiscovery, primary_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert primary is not None and variant is not None
        primary.regional_variant_of = anchor_id
        variant.regional_variant_of = primary_id
    return anchor_id, primary_id, variant_id, primary_url


def test_publisher_refresh_releases_a_renamed_regional_family(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    anchor_id, primary_id, variant_id, _ = _family(durable_database, workspace_id)
    staging = _staging(durable_database, tmp_path)

    staging.checks.refresh_pending_discovery(
        workspace_id,
        primary_id,
        PostingCheck(
            status="readable",
            note="The publisher corrected the job title.",
            platform="greenhouse",
            title="Platform Operations Lead",
            location="Remote",
            facts=published_listing_facts("greenhouse", locations="Remote"),
        ),
        actor="test",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        anchor = session.get(PendingDiscovery, anchor_id)
        primary = session.get(PendingDiscovery, primary_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert anchor is not None and primary is not None and variant is not None
        assert primary.title == "Platform Operations Lead"
        assert primary.regional_variant_of is None
        assert variant.regional_variant_of is None


# Each board carries the employer in the tenant segment a blank row is named from.
_TENANT_NAMED_BOARDS = [
    ("https://acme-health.recruitee.com/o/operations-lead", "Acme Health"),
    ("https://northwind.pinpointhq.com/en/postings/5a1b2c3d", "Northwind"),
    ("https://gamma-labs.teamtailor.com/jobs/4821-operations-lead", "Gamma Labs"),
    ("https://initrode.jobs.personio.com/job/1234567", "Initrode"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Operations-Lead_R-1234", "Acme"),
    ("https://jobs.lever.co/acme-health/9f0c2b7a-operations-lead", "Acme Health"),
    ("https://acme-health.bamboohr.com/careers/37", "Acme Health"),
    ("https://jobs.gem.com/initrode/am9icG9zdDrChY8Z", "Initrode"),
    ("https://apply.workable.com/initech/j/5A1B2C3D4E/", "Initech"),
    (
        "https://ats.rippling.com/globex-health/jobs/3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f",
        "Globex Health",
    ),
    ("https://gamma-labs.breezy.hr/p/7a86407-operations-lead", "Gamma Labs"),
]


def _readable_check(employer: str = "") -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="The publisher restated the posting.",
        platform="workday",
        title="Operations Lead",
        employer=employer,
        location="Remote",
        facts=published_listing_facts("workday", locations="Remote"),
    )


def _refreshed_row(
    database: Database, artifact_root: Path, *, url: str, employer: str, check: PostingCheck
) -> PendingDiscovery:
    workspace_id = _workspace(database, artifact_root)
    pending_id = _pending(
        database, workspace_id, url=url, employer=employer, first_seen_on=date(2026, 8, 24)
    )
    _staging(database, artifact_root).checks.refresh_pending_discovery(
        workspace_id, pending_id, check, actor="test", origin="test", correlation_id=None
    )
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        session.expunge(pending)
        return pending


@pytest.mark.parametrize(("url", "tenant"), _TENANT_NAMED_BOARDS)
def test_publisher_refresh_names_a_blank_employer_from_the_board_tenant(
    durable_database: Database, tmp_path: Path, url: str, tenant: str
) -> None:
    pending = _refreshed_row(
        durable_database,
        tmp_path,
        url=url,
        employer="",
        check=_readable_check(),
    )

    assert pending.employer == tenant
    assert (
        StagedListingFacts.from_json(pending.staged_facts).identity_trust
        is ListingIdentityTrust.UNVERIFIED_HINT
    )


def test_a_tenant_named_employer_stays_a_hint_on_a_row_an_earlier_read_vouched_for(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    url = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Operations-Lead_R-1234"
    pending_id = _pending(
        durable_database, workspace_id, url=url, employer="", first_seen_on=date(2026, 8, 24)
    )
    with durable_database.session() as session:
        stored = session.get(PendingDiscovery, pending_id)
        assert stored is not None
        stored.staged_facts = published_listing_facts("workday", locations="Remote").as_json()

    staging = _staging(durable_database, tmp_path)
    for _ in range(2):
        staging.checks.refresh_pending_discovery(
            workspace_id,
            pending_id,
            _readable_check(),
            actor="test",
            origin="test",
            correlation_id=None,
        )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.employer == "Acme"
        assert not StagedListingFacts.from_json(pending.staged_facts).publisher_vouched


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.smartrecruiters.com/ACME1/744000012345-operations-lead",
        "https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL/jobs/1234",
        "https://careers.example.com/postings/42",
    ],
)
def test_publisher_refresh_leaves_a_blank_employer_blank_when_no_tenant_names_it(
    durable_database: Database, tmp_path: Path, url: str
) -> None:
    pending = _refreshed_row(
        durable_database, tmp_path, url=url, employer="", check=_readable_check()
    )

    assert pending.employer == ""


@pytest.mark.parametrize("vouched_before", [True, False])
def test_publisher_refresh_names_the_tenant_as_the_posting_spells_it_and_vouches_for_it(
    durable_database: Database, tmp_path: Path, vouched_before: bool
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    pending_id = _pending(
        durable_database,
        workspace_id,
        url="https://northwindhealth.bamboohr.com/careers/37",
        employer="",
        first_seen_on=date(2026, 8, 24),
    )
    if vouched_before:
        with durable_database.session() as session:
            stored = session.get(PendingDiscovery, pending_id)
            assert stored is not None
            stored.staged_facts = published_listing_facts("bamboohr", locations="Remote").as_json()
    check = replace(_readable_check(), text="Northwind Health is hiring an Operations Lead.")

    _staging(durable_database, tmp_path).checks.refresh_pending_discovery(
        workspace_id, pending_id, check, actor="test", origin="test", correlation_id=None
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.employer == "Northwind Health"
        assert (
            StagedListingFacts.from_json(pending.staged_facts).identity_trust
            is ListingIdentityTrust.PUBLISHER
        )


def test_publisher_refresh_keeps_an_employer_the_check_does_not_restate(
    durable_database: Database, tmp_path: Path
) -> None:
    pending = _refreshed_row(
        durable_database,
        tmp_path,
        url="https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Operations-Lead_R-1234",
        employer="Aster Systems",
        check=_readable_check(),
    )

    assert pending.employer == "Aster Systems"


@pytest.mark.parametrize(("url", "tenant"), _TENANT_NAMED_BOARDS)
def test_publisher_refresh_vouches_for_an_employer_the_check_states(
    durable_database: Database, tmp_path: Path, url: str, tenant: str
) -> None:
    """A stated employer outranks the tenant slug and carries the publisher's trust."""
    pending = _refreshed_row(
        durable_database,
        tmp_path,
        url=url,
        employer="",
        check=_readable_check("Globex"),
    )

    assert pending.employer == "Globex" != tenant
    assert (
        StagedListingFacts.from_json(pending.staged_facts).identity_trust
        is ListingIdentityTrust.PUBLISHER
    )


def test_publisher_refresh_keeps_the_focus_terms_the_board_summary_stated(
    durable_database: Database, tmp_path: Path
) -> None:
    """A posting check reads no board summary, so it never clears what one stated."""
    workspace_id = _workspace(durable_database, tmp_path)
    url = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Operations-Lead_R-1234"
    pending_id = _pending(durable_database, workspace_id, url=url, first_seen_on=date(2026, 8, 24))
    stated = PublishedFact(
        values=("clinical data integration",), provenance="posting.summary", complete=True
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.staged_facts = StagedListingFacts(focus_terms=stated).as_json()

    _staging(durable_database, tmp_path).checks.refresh_pending_discovery(
        workspace_id,
        pending_id,
        _readable_check(),
        actor="test",
        origin="test",
        correlation_id=None,
    )

    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert StagedListingFacts.from_json(pending.staged_facts).focus_terms == stated


def test_a_requeue_keeps_the_title_a_person_corrected(
    durable_database: Database, tmp_path: Path
) -> None:
    """A requeue restates publisher facts, and a person's title is not one of them."""
    workspace_id = _workspace(durable_database, tmp_path)
    url = "https://boards.greenhouse.io/globex/jobs/1"
    pending_id = _pending(durable_database, workspace_id, url=url, first_seen_on=date(2026, 8, 24))
    PendingDiscoveryHintsService(durable_database).correct_title(
        workspace_id, pending_id, "Head of Operations"
    )
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.archived_at = utc_now()
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                action="pending_discovery.queue_reset",
                entity_type="pending_discovery",
                entity_id=pending_id,
                details={"discovery_url": url},
                created_by="local_user",
                origin="pending_queue.reset",
            )
        )

    requeued = _staging(durable_database, tmp_path).stage_found_posting(
        workspace_id, url, date(2026, 8, 25), publisher_check=_readable_check()
    )

    assert requeued is not None and requeued.requeued
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.title == "Head of Operations"


def test_board_refresh_releases_a_renamed_regional_family(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _, primary_id, variant_id, primary_url = _family(durable_database, workspace_id)
    staging = _staging(durable_database, tmp_path)

    staging.stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Acme",
                title="Platform Operations Lead",
                url=primary_url,
                location="Remote",
                facts=published_listing_facts("greenhouse", locations="Remote"),
            )
        ],
        date(2026, 8, 26),
        reason="Board scan matched the title filter.",
    )

    with durable_database.session() as session:
        primary = session.get(PendingDiscovery, primary_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert primary is not None and variant is not None
        assert primary.title == "Platform Operations Lead"
        assert primary.regional_variant_of is None
        assert variant.regional_variant_of is None


def _board_listing(
    employer: str, url: str, requisition_id: str = "R-100", *, location: str = "Remote"
) -> SourceListing:
    return SourceListing(
        employer=employer,
        title="Operations Lead",
        url=url,
        location=location,
        requisition_id=requisition_id,
        facts=published_listing_facts("greenhouse", locations=location),
    )


def _stage(
    database: Database,
    artifact_root: Path,
    workspace_id: str,
    listings: list[SourceListing],
    source_id: str | None,
) -> None:
    _staging(database, artifact_root).stage_listings(
        workspace_id,
        listings,
        date(2026, 8, 26),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )


@pytest.mark.parametrize(
    ("source", "logical_type", "retained_text"),
    [
        (
            PostingSource("page", record='{"validThrough":"2026-10-19"}'),
            "posting.source.json",
            '{"validThrough":"2026-10-19"}',
        ),
        (
            PostingSource("page", page_html='<meta content="2026-10-19">'),
            "posting.source.html",
            '<meta content="2026-10-19">',
        ),
        (
            PostingSource("feed", item_xml="<closingDate>2026-10-19</closingDate>"),
            "posting.source.xml",
            "<closingDate>2026-10-19</closingDate>",
        ),
    ],
)
def test_a_source_only_listing_binds_its_date_to_retained_publisher_bytes(
    durable_database: Database,
    tmp_path: Path,
    source: PostingSource,
    logical_type: str,
    retained_text: str,
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    base = _board_listing("Acme", "https://example.test/jobs/date")
    observed_at = datetime(2026, 10, 8, 12, tzinfo=UTC)
    closing_date = publisher_closing_date(
        "", valid_through="2026-10-19", source_url=base.url, observed_at=observed_at
    )
    listing = replace(base, source=source, facts=replace(base.facts, closing_date=closing_date))

    _stage(durable_database, tmp_path, workspace_id, [listing], None)
    _stage(durable_database, tmp_path, workspace_id, [listing], None)

    with durable_database.session() as session:
        pending = session.scalars(select(PendingDiscovery)).one()
        kept = StagedListingFacts.from_json(pending.staged_facts).closing_date
        assert kept is not None
        assert kept.normalized_value == "2026-10-19"
        assert kept.observed_at == observed_at
        assert kept.source_url == base.url
        assert kept.artifact_id is not None
        assert kept.statements[0].artifact_id == kept.artifact_id
        artifact = session.get_one(Artifact, kept.artifact_id)
        assert artifact.logical_type == logical_type
        assert artifact.related_entity_id == pending.id
        assert pending.listing_artifact_id is None
        path = file_uri_path(artifact.storage_uri)
        assert path is not None and path.read_text() == retained_text
        assert len(session.scalars(select(Artifact)).all()) == 1


def test_a_rescan_retains_changed_and_removed_dates_without_replacing_the_first_body(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    base = _board_listing("Acme", "https://example.test/jobs/date")
    description = "Own the operations programme."
    retained = []
    first_body_id = None
    for day, value in ((8, "2026-10-19"), (9, "2026-10-21"), (10, "")):
        at = datetime(2026, 10, day, 12, tzinfo=UTC)
        record = json.dumps({"validThrough": value, "description": description})
        listing = replace(
            base,
            published_description=description,
            source=PostingSource("page", record=record),
            facts=replace(
                base.facts,
                closing_date=publisher_closing_date(
                    description, valid_through=value, source_url=base.url, observed_at=at
                ),
            ),
        )
        _stage(durable_database, tmp_path, workspace_id, [listing], None)
        with durable_database.session() as session:
            pending = session.scalars(select(PendingDiscovery)).one()
            kept = StagedListingFacts.from_json(pending.staged_facts).closing_date
            assert kept is not None and kept.artifact_id is not None
            assert kept.normalized_value == (value or None)
            assert kept.observed_at == at
            if value:
                assert kept.statements[0].artifact_id == kept.artifact_id
            else:
                assert kept.statements == ()
            artifact = session.get_one(Artifact, kept.artifact_id)
            assert artifact.logical_type == "posting.source.json"
            path = file_uri_path(artifact.storage_uri)
            assert path is not None and path.read_text() == record
            retained.append((artifact.id, path, record))
            if first_body_id is None:
                first_body_id = pending.listing_artifact_id
            assert pending.listing_artifact_id == first_body_id
    assert len({artifact_id for artifact_id, _, _ in retained}) == 3
    assert all(path.read_text() == record for _, path, record in retained)
    with durable_database.session() as session:
        assert (
            len(
                session.scalars(
                    select(Artifact).where(Artifact.logical_type == "posting.listing.text")
                ).all()
            )
            == 1
        )


def test_a_changed_body_date_cites_its_actual_body_and_never_an_absent_body(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    base = _board_listing("Acme", "https://example.test/jobs/date")
    body_ids = []
    original_body_id = None
    for day, description, supplied_body in (
        (8, "Applications close: 2026-10-19", "Applications close: 2026-10-19"),
        (9, "Applications close: 2026-10-21", "Applications close: 2026-10-21"),
        (10, "Applications close: 2026-10-22", ""),
    ):
        closing_date = publisher_closing_date(
            description,
            source_url=base.url,
            observed_at=datetime(2026, 10, day, 12, tzinfo=UTC),
        )
        listing = replace(
            base,
            published_description=supplied_body,
            facts=replace(base.facts, closing_date=closing_date),
        )
        _stage(durable_database, tmp_path, workspace_id, [listing], None)
        with durable_database.session() as session:
            pending = session.scalars(select(PendingDiscovery)).one()
            kept = StagedListingFacts.from_json(pending.staged_facts).closing_date
            assert kept is not None
            assert len(kept.statements) == 1
            if original_body_id is None:
                original_body_id = pending.listing_artifact_id
            assert pending.listing_artifact_id == original_body_id
            if supplied_body:
                assert kept.artifact_id is not None
                assert kept.statements[0].artifact_id == kept.artifact_id
                artifact = session.get_one(Artifact, kept.artifact_id)
                path = file_uri_path(artifact.storage_uri)
                assert path is not None and path.read_text() == supplied_body
                body_ids.append(artifact.id)
            else:
                assert kept.artifact_id is None
                assert kept.statements[0].artifact_id is None
    assert len(set(body_ids)) == 2


def test_an_unknown_date_rescan_preserves_the_last_observed_date_and_its_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    base = _board_listing("Acme", "https://example.test/jobs/date")
    closing_date = publisher_closing_date(
        "",
        valid_through="2026-10-19",
        source_url=base.url,
        observed_at=datetime(2026, 10, 8, 12, tzinfo=UTC),
    )
    listing = replace(
        base,
        source=PostingSource("page", record='{"validThrough":"2026-10-19"}'),
        facts=replace(base.facts, closing_date=closing_date),
    )
    _stage(durable_database, tmp_path, workspace_id, [listing], None)
    with durable_database.session() as session:
        before = StagedListingFacts.from_json(
            session.scalars(select(PendingDiscovery)).one().staged_facts
        ).closing_date
        assert before is not None

    _stage(durable_database, tmp_path, workspace_id, [base], None)

    with durable_database.session() as session:
        after = StagedListingFacts.from_json(
            session.scalars(select(PendingDiscovery)).one().staged_facts
        ).closing_date
        assert after == before


def _variant_of_by_url(database: Database, workspace_id: str) -> dict[str, str | None]:
    with database.session() as session:
        rows = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        by_id = {row.id: row for row in rows}
        return {
            row.discovery_url: (
                by_id[row.regional_variant_of].discovery_url if row.regional_variant_of else None
            )
            for row in by_id.values()
        }


def test_a_board_scan_staged_row_names_the_scan_as_its_origin(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    url = "https://globex.example.test/jobs/operations-lead"

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], None)

    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).one()
        assert staged.origin == "source.scan"


def test_two_postings_on_one_peoplesoft_gateway_stage_two_rows(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    gateway = "https://careers.acme.example/psc/hrprd/EMPLOYEE/HRMS/c/HRS_HRAM.HRS_APP_SCHJOB.GBL"
    query = "?Page=HRS_APP_JBPST&Action=U&FOCUS=Applicant&SiteId=1001&PostingSeq=1"
    urls = {job: f"{gateway}{query}&JobOpeningId={job}" for job in ("79142", "79312")}
    listings = [_board_listing("Acme", url, job) for job, url in urls.items()]

    _stage(durable_database, tmp_path, workspace_id, listings, None)

    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery.discovery_url).where(
                PendingDiscovery.workspace_id == workspace_id
            )
        ).all()
    assert sorted(staged) == sorted(urls.values())


def test_one_board_folds_a_shared_requisition_that_names_different_entities(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    first = "https://globex.example.test/jobs/operations-lead-east"
    second = "https://globex.example.test/jobs/operations-lead-west"

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", first), _board_listing("Globex Retail Services", second)],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {first: None, second: first}


def test_two_boards_with_different_employers_never_fold_a_shared_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    globex = employer_board(durable_database, workspace_id, "Globex")
    initech = employer_board(durable_database, workspace_id, "Initech")
    first = "https://globex.example.test/jobs/operations-lead"
    second = "https://initech.example.test/jobs/operations-lead"

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", first)], globex)
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Initech", second)], initech)

    assert _variant_of_by_url(durable_database, workspace_id) == {first: None, second: None}


def test_two_boards_fold_a_shared_board_uuid_under_different_employer_spellings(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    careers = employer_board(durable_database, workspace_id, "Globex Systems")
    board = employer_board(durable_database, workspace_id, "Globex")
    job = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"
    first = f"https://careers.globex.example.test/job/{job}/operations-lead/"
    second = f"https://jobs.lever.co/globex/{job}"

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex Systems", first, job)],
        careers,
    )
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", second, job)], board)

    assert _variant_of_by_url(durable_database, workspace_id) == {first: None, second: first}


@pytest.mark.parametrize(
    ("archived", "dismissed", "folds"),
    [(True, False, False), (False, True, True), (True, True, True)],
    ids=["queue-reset", "dismissed", "dismissed-and-archived"],
)
def test_an_archived_head_absorbs_a_new_copy_only_when_the_person_dismissed_it(
    durable_database: Database, tmp_path: Path, archived: bool, dismissed: bool, folds: bool
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    job = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"
    head_url = f"https://jobs.lever.co/globex/{job}"
    copy_url = f"https://careers.globex.example.test/job/{job}/operations-lead/"
    head_id = _pending(
        durable_database,
        workspace_id,
        url=head_url,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        requisition_id=job,
    )
    if archived:
        mark_reset_archived(durable_database, workspace_id, head_id)
    if dismissed:
        dismiss_pending_discovery(durable_database, workspace_id, head_id)

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex Systems", copy_url, job)],
        None,
    )

    assert _variant_of_by_url(durable_database, workspace_id)[copy_url] == (
        head_url if folds else None
    )
    queue = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).list_pending_discoveries(workspace_id)
    assert [item.discovery_url for item in queue] == ([] if folds else [copy_url])


@pytest.mark.parametrize("requeue", ["board_scan", "found_posting", "board_scan_copy_then_head"])
def test_a_requeued_copy_of_a_head_a_reset_restored_returns_to_the_queue(
    durable_database: Database, tmp_path: Path, requeue: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    job = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"
    head_url = f"https://jobs.lever.co/globex/{job}"
    copy_url = f"https://careers.globex.example.test/job/{job}/operations-lead/"
    head_id = _pending(
        durable_database,
        workspace_id,
        url=head_url,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id=job,
    )
    dismiss_pending_discovery(durable_database, workspace_id, head_id)
    copy = [_board_listing("Globex", copy_url, job)]
    _stage(durable_database, tmp_path, workspace_id, copy, board)
    assert _variant_of_by_url(durable_database, workspace_id)[copy_url] == head_url
    with durable_database.session() as session:
        reset_pending_queue(session, workspace_id)

    if requeue == "board_scan":
        _stage(durable_database, tmp_path, workspace_id, copy, board)
    elif requeue == "found_posting":
        _staging(durable_database, tmp_path).stage_found_posting(
            workspace_id, copy_url, date(2026, 8, 27)
        )
    else:
        head = _board_listing("Globex", head_url, job)
        _stage(durable_database, tmp_path, workspace_id, [*copy, head], board)

    # A head requeued in the same pass is live again, so its copy folds back under it.
    head_requeued = requeue == "board_scan_copy_then_head"
    assert _variant_of_by_url(durable_database, workspace_id)[copy_url] == (
        head_url if head_requeued else None
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert [item.discovery_url for item in service.list_pending_discoveries(workspace_id)] == [
        head_url if head_requeued else copy_url
    ]
    with durable_database.session() as session:
        requeued = [
            event.details
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.action == "pending_discovery.requeued")
            )
            if event.details["discovery_url"] == copy_url
        ]
        assert [details["released_from_head"] for details in requeued] == [head_id]


def test_sourceless_rows_with_different_employers_never_fold_a_shared_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    first = "https://jobs.example.test/globex/operations-lead"
    second = "https://jobs.example.test/initech/operations-lead"

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", first), _board_listing("Initech", second)],
        None,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {first: None, second: None}


def test_a_split_family_on_one_board_folds_again_on_its_next_read(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    first = "https://globex.example.test/jobs/operations-lead-east"
    second = "https://globex.example.test/jobs/operations-lead-west"
    for url, employer, seen in (
        (first, "Globex", date(2026, 8, 24)),
        (second, "Globex Retail Services", date(2026, 8, 25)),
    ):
        _pending(
            durable_database,
            workspace_id,
            url=url,
            employer=employer,
            first_seen_on=seen,
            source_id=board,
            requisition_id="R-100",
        )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex Retail Services", second)],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {first: None, second: first}


def _board_family(database: Database, workspace_id: str, board: str) -> tuple[str, str, str]:
    head_url = "https://globex.example.test/jobs/operations-lead-east"
    child_url = "https://globex.example.test/jobs/operations-lead-west"
    head_id = _pending(
        database,
        workspace_id,
        url=head_url,
        employer="Globex",
        first_seen_on=date(2026, 8, 24),
        source_id=board,
        requisition_id="R-100",
    )
    _pending(
        database,
        workspace_id,
        url=child_url,
        employer="Globex",
        first_seen_on=date(2026, 8, 25),
        source_id=board,
        requisition_id="R-100",
        variant_of=head_id,
    )
    return head_id, head_url, child_url


def test_a_board_read_renaming_only_the_employer_keeps_the_family(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    _head_id, head_url, child_url = _board_family(durable_database, workspace_id, board)

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex Retail Services", child_url)],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {
        head_url: None,
        child_url: head_url,
    }


def test_a_publisher_refresh_renaming_only_the_head_employer_keeps_the_family(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    head_id, head_url, child_url = _board_family(durable_database, workspace_id, board)

    _staging(durable_database, tmp_path).checks.refresh_pending_discovery(
        workspace_id,
        head_id,
        _readable_check("Globex Retail Services"),
        actor="test",
        origin="test",
        correlation_id=None,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {
        head_url: None,
        child_url: head_url,
    }


def test_a_count_of_markets_is_not_stored_as_a_place(durable_database: Database) -> None:
    """A board summarizing several markets writes "4 Locations" where a place goes.

    Nothing downstream can tell it from a city, so it is kept as an unresolved
    value rather than a place.
    """
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex",
                "Data Platform Lead",
                "https://jobs.globex.test/1",
                "4 Locations",
                facts=published_listing_facts("synthetic_board", locations="4 Locations"),
            ),
            SourceListing("Globex", "Staff Engineer", "https://jobs.globex.test/2", "Toronto"),
        ],
        utc_now().date(),
        reason="Board scan.",
    )

    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery).order_by(PendingDiscovery.discovery_url)
        ).all()
    assert [item.location for item in staged] == ["", "Toronto"]
    assert staged[0].staged_facts["locations"] == {
        "values": [],
        "unresolved_values": ["4 Locations"],
        "provenance": "synthetic_board.location",
        "complete": False,
    }


def test_a_posting_somebody_found_reaches_the_queue_and_is_not_staged_twice(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"

    staged = service.stage_found_posting(
        workspace_id, url, utc_now().date(), title="Product Manager, Data Platform"
    )
    again = service.stage_found_posting(workspace_id, url, utc_now().date())

    assert staged is not None
    assert staged.outcome == "created"
    assert again is None
    queue = service.list_pending_discoveries(workspace_id)
    assert [item.discovery_url for item in queue] == [url]
    assert queue[0].employer == "Globex"
    assert queue[0].title == "Product Manager, Data Platform"
    assert "You added this posting" in queue[0].reason


def test_a_guided_campaign_staged_posting_states_the_campaign_proposed_it(
    durable_database: Database,
) -> None:
    """A campaign-staged pending row must not claim the person added it themselves."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://job-boards.greenhouse.io/globex-health/jobs/1001"

    staged = service.staging.stage_found_posting(
        workspace_id, url, utc_now().date(), origin="search.guided_campaign"
    )

    assert staged is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery_id)
        assert pending is not None
        assert pending.reason == CAMPAIGN_FOUND_POSTING_REASON
        assert pending.origin == "search.guided_campaign"
        assert "You added this posting" not in pending.reason


def test_a_board_harvested_campaign_posting_says_the_campaign_read_a_board(
    durable_database: Database,
) -> None:
    """A listing the campaign took off a board states that, and still names no source."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://job-boards.greenhouse.io/globex-health/jobs/1003"

    staged = service.staging.stage_found_posting(
        workspace_id,
        url,
        utc_now().date(),
        origin="search.guided_campaign",
        from_board_read=True,
    )

    assert staged is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery_id)
        assert pending is not None
        assert pending.reason == CAMPAIGN_BOARD_LISTING_REASON
        assert pending.source_id is None
        assert staged_from_single_posting(pending.reason)


def test_a_manually_added_posting_keeps_the_you_added_this_reason(
    durable_database: Database,
) -> None:
    """A person-pasted posting keeps the sentence that is true for that path."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://job-boards.greenhouse.io/globex-health/jobs/1002"

    staged = service.staging.stage_found_posting(workspace_id, url, utc_now().date(), origin="api")

    assert staged is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery_id)
        assert pending is not None
        assert pending.reason == FOUND_POSTING_REASON
        assert pending.origin == "api"


def test_a_requeued_posting_keeps_the_origin_that_first_staged_it(
    durable_database: Database,
) -> None:
    """The origin names what first staged a row, so requeueing it does not rewrite that."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://job-boards.greenhouse.io/globex-health/jobs/1003"

    staged = service.stage_found_posting(workspace_id, url, date(2026, 8, 24))
    assert staged is not None
    with durable_database.session() as session:
        first = session.get(PendingDiscovery, staged.pending_discovery.id)
        assert first is not None
        staged_origin = first.origin

    mark_reset_archived(durable_database, workspace_id, staged.pending_discovery.id)
    requeued = service.staging.stage_found_posting(
        workspace_id, url, date(2026, 8, 25), origin="search.guided_campaign"
    )

    assert requeued is not None
    assert requeued.requeued
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, staged.pending_discovery.id)
        assert row is not None
        assert row.origin == staged_origin


def test_a_found_posting_with_no_display_text_takes_its_names_from_the_slug(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = (
        "https://job-boards.greenhouse.io/globex-health/jobs/7788990011/staff-data-platform-manager"
    )

    staged = service.stage_found_posting(workspace_id, url, utc_now().date())

    assert staged is not None
    queue = service.list_pending_discoveries(workspace_id)
    assert queue[0].employer == "Globex Health"
    assert queue[0].title == "staff data platform manager"


def test_a_campaign_found_posting_never_derives_staged_facts_from_its_url(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = (
        "https://job-boards.greenhouse.io/globex-health/jobs/7788990011/staff-data-platform-manager"
    )

    staged = service.staging.stage_found_posting(
        workspace_id, url, utc_now().date(), derive_url_hints=False
    )

    assert staged is not None
    [pending] = service.list_pending_discoveries(workspace_id)
    assert pending.employer == ""
    assert pending.title == ""


_ESCAPED_TITLE = "Platform Reliability &amp; Data Lead"
_ESCAPED_EMPLOYER = "Globex Health &amp; Research"
_ESCAPED_LOCATION = "Toronto &#38; Remote"
_DECODED_NAMES = (
    "Platform Reliability & Data Lead",
    "Globex Health & Research",
    "Toronto & Remote",
)
_ESCAPED_FACTS = published_listing_facts("greenhouse", locations="Toronto")


def _escaped_check(*, names: bool) -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="The publisher restated the posting.",
        platform="greenhouse",
        title=_ESCAPED_TITLE if names else "",
        employer=_ESCAPED_EMPLOYER if names else "",
        location=_ESCAPED_LOCATION if names else "",
        facts=_ESCAPED_FACTS,
    )


@pytest.mark.parametrize(
    ("hints", "check", "from_board_read"),
    [
        pytest.param(True, None, False, id="caller-hints-without-a-check"),
        pytest.param(True, _escaped_check(names=False), True, id="caller-hints-from-a-board-read"),
        pytest.param(False, _escaped_check(names=True), False, id="publisher-check"),
    ],
)
def test_a_campaign_staged_posting_stores_its_names_with_html_entities_decoded(
    durable_database: Database, hints: bool, check: PostingCheck | None, from_board_read: bool
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    staged = service.staging.stage_found_posting(
        workspace_id,
        "https://job-boards.greenhouse.io/globex-health/jobs/1004",
        utc_now().date(),
        title=_ESCAPED_TITLE if hints else "",
        employer=_ESCAPED_EMPLOYER if hints else "",
        location=_ESCAPED_LOCATION if hints else "",
        origin="search.guided_campaign",
        publisher_check=check,
        derive_url_hints=False,
        from_board_read=from_board_read,
    )

    assert staged is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, staged.pending_discovery_id)
        assert pending is not None
        assert (pending.title, pending.employer, pending.location) == _DECODED_NAMES


def test_a_board_scan_stores_and_restates_names_with_html_entities_decoded(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    first = "https://globex.example.test/jobs/platform-reliability-lead"
    second = "https://globex.example.test/jobs/platform-data-lead"
    _pending(durable_database, workspace_id, url=second, first_seen_on=date(2026, 8, 24))
    listings = [
        SourceListing(
            employer=_ESCAPED_EMPLOYER,
            title=_ESCAPED_TITLE,
            url=url,
            location=_ESCAPED_LOCATION,
            facts=_ESCAPED_FACTS,
        )
        for url in (first, second)
    ]

    _stage(durable_database, tmp_path, workspace_id, listings, None)

    with durable_database.session() as session:
        rows = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        assert [(row.title, row.employer, row.location) for row in rows] == [_DECODED_NAMES] * 2


def test_a_publisher_refresh_restates_names_with_html_entities_decoded(
    durable_database: Database, tmp_path: Path
) -> None:
    pending = _refreshed_row(
        durable_database,
        tmp_path,
        url="https://globex.example.test/jobs/platform-reliability-lead",
        employer="Globex",
        check=_escaped_check(names=True),
    )

    assert (pending.title, pending.employer, pending.location) == _DECODED_NAMES


def test_a_staged_found_posting_rolls_back_with_its_callers_transaction(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    def stage_then_fail() -> None:
        with durable_database.session() as session:
            service.staging.stage_found_posting(
                workspace_id,
                "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10",
                utc_now().date(),
                existing_session=session,
            )
            raise RuntimeError("after staging")

    with pytest.raises(RuntimeError, match="after staging"):
        stage_then_fail()

    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery)).all() == []


def test_a_found_posting_on_an_unreadable_url_shape_stays_blank_rather_than_invented(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    staged = service.stage_found_posting(
        workspace_id, "https://careers.example.org/openings/4821", utc_now().date()
    )

    assert staged is not None
    queue = service.list_pending_discoveries(workspace_id)
    assert queue[0].employer == ""
    assert queue[0].title == ""


def test_two_ukg_postings_on_one_board_stage_as_two_discoveries(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    board = (
        "https://recruiting2.ultipro.com/GLO1001GLOBX/JobBoard/"
        "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/OpportunityDetail?opportunityId="
    )

    for opportunity in (
        "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
        "8d2e3f40-5b6c-4d7e-9f0a-1b2c3d4e5f60",
    ):
        assert (
            service.stage_found_posting(workspace_id, board + opportunity, utc_now().date())
            is not None
        )

    assert len(service.list_pending_discoveries(workspace_id)) == 2


def test_a_case_variant_of_a_dismissed_workday_posting_stays_out_of_the_queue(
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
        "https://initech.wd1.myworkdayjobs.com/Careers/job/Remote---Canada/"
        "Clinical-Informatics-Lead_R1234",
        utc_now().date(),
    )
    assert first is not None
    dismiss_pending_discovery(durable_database, workspace_id, first.pending_discovery.id)

    service.stage_found_posting(
        workspace_id,
        "https://initech.wd1.myworkdayjobs.com/careers/job/remote---canada/"
        "clinical-informatics-lead_r1234",
        utc_now().date(),
    )

    assert service.list_pending_discoveries(workspace_id) == []


@pytest.mark.parametrize(
    "second_path",
    [
        "Remote---USA/Clinical-Informatics-Lead_R1234",
        "Remote---Canada/Pharmacy-Systems-Analyst_R5678",
    ],
    ids=["regional copy of a withdrawn requisition", "another requisition on the host"],
)
def test_a_found_posting_stages_beside_a_withdrawn_row_or_another_requisition(
    durable_database: Database, tmp_path: Path, second_path: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    board = "https://initech.wd1.myworkdayjobs.com/Careers/job/"
    first = service.stage_found_posting(
        workspace_id, board + "Remote---Canada/Clinical-Informatics-Lead_R1234", utc_now().date()
    )
    assert first is not None
    _withdraw_on_refresh(durable_database, tmp_path, workspace_id, first.pending_discovery.id)

    second = service.stage_found_posting(workspace_id, board + second_path, utc_now().date())

    assert second is not None
    assert second.pending_discovery.withdrawn_at is None


def test_checked_posting_is_staged_with_publisher_facts_without_a_task(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    facts = published_listing_facts(
        "lever",
        locations=["Remote, Canada", "Remote, USA"],
        arrangements="remote",
        employment_types="Full-time",
    )
    checked = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Principal Product Manager",
        location="Remote, Canada, Remote, USA",
        facts=facts,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/00000000-0000-0000-0000-000000000000",
        date(2026, 8, 24),
        title="Unverified hint",
        employer="Globex",
        check_publisher_facts=True,
    )

    assert result is not None
    assert result.posting_check is not None
    assert result.posting_check.status == "readable"
    assert result.pending_discovery.title == "Principal Product Manager"
    assert result.pending_discovery.employer == "Globex"
    assert result.pending_discovery.location == "Remote, Canada, Remote, USA"
    assert result.pending_discovery.staged_facts.identity_trust == "unverified_hint"
    assert result.pending_discovery.staged_facts.arrangements.values == ["remote"]
    with durable_database.session() as session:
        assert session.scalars(select(BackgroundTask)).all() == []


def test_posting_check_refuses_an_unknown_workspace_before_publisher_io(
    durable_database: Database,
) -> None:
    checked = False

    def posting_check(_url: str, **_: object) -> PostingCheck:
        nonlocal checked
        checked = True
        raise AssertionError("publisher I/O must not start")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=posting_check,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with pytest.raises(NotFoundError, match="Unknown workspace"):
        service.check_found_posting("missing-workspace", "https://jobs.lever.co/globex/role")
    assert checked is False


def test_only_a_link_the_person_enters_is_checked_as_a_single_posting_read(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    url = "https://jobs.lever.co/globex/10000000-0000-0000-0000-000000000000"
    intents: list[PostingReadIntent] = []

    def posting_check(
        _url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> PostingCheck:
        intents.append(intent)
        return PostingCheck(status="readable", note="", platform="lever", title="Data Engineer")

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=posting_check,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.check_found_posting(workspace_id, url)
    staged = service.stage_found_posting(
        workspace_id, url, date(2026, 8, 24), check_publisher_facts=True
    )
    assert staged is not None
    service.refresh_pending_discovery(workspace_id, staged.pending_discovery.id)

    assert intents == [
        PostingReadIntent.SINGLE_POSTING,
        PostingReadIntent.SINGLE_POSTING,
        PostingReadIntent.CRAWL,
    ]


def test_failed_second_check_stages_an_unverified_recoverable_posting(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="unreadable",
        note="Lever returned HTTP 503.",
        platform="lever",
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.stage_found_posting(
        workspace_id,
        "https://jobs.lever.co/globex/10000000-0000-0000-0000-000000000000",
        date(2026, 8, 24),
        title="Human title hint",
        check_publisher_facts=True,
    )

    assert result is not None
    assert result.posting_check is not None
    assert result.posting_check.status == "unreadable"
    [pending] = service.queries.list_pending_discoveries(workspace_id)
    assert pending.content_status is PostingContentAccessStatus.NOT_YET_READ
    assert pending.retained_content_id is None


def test_unsupported_second_check_remains_a_manual_unverified_add(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="unsupported",
        note="No supported publisher reader is available.",
        platform="unknown",
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    result = service.stage_found_posting(
        workspace_id,
        "https://careers.example.test/postings/platform-planner",
        date(2026, 8, 24),
        title="Platform Planner",
        check_publisher_facts=True,
    )

    assert result is not None
    assert result.posting_check is not None
    assert result.posting_check.status == "unsupported"
    assert result.pending_discovery.title == "Platform Planner"
    assert result.pending_discovery.staged_facts.identity_trust == "unverified_hint"


def test_adding_an_archived_pending_discovery_requeues_the_same_record(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    checked = PostingCheck(
        status="readable",
        note="Lever published this posting metadata.",
        platform="lever",
        title="Current Platform Lead",
        location="Remote, Canada",
        facts=published_listing_facts("lever", locations="Remote, Canada"),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/04c7226d-b454-4352-8251-733d52a1e943"
    first_seen_on = date(2026, 8, 20)
    staged = service.stage_found_posting(
        workspace_id,
        url,
        first_seen_on,
        title="Archived Platform Lead",
        employer="Globex",
    )
    assert staged is not None
    staged_pending = staged.pending_discovery

    mark_reset_archived(durable_database, workspace_id, staged_pending.id, withdrawn=True)

    requeued = service.stage_found_posting(
        workspace_id, url, date(2026, 8, 25), check_publisher_facts=True
    )

    assert requeued is not None
    assert requeued.outcome == "requeued"
    assert requeued.pending_discovery.id == staged_pending.id
    assert requeued.pending_discovery.first_seen_on == first_seen_on
    assert requeued.pending_discovery.withdrawn_at is None
    assert requeued.pending_discovery.title == "Current Platform Lead"
    assert requeued.pending_discovery.employer == "Globex"
    assert requeued.pending_discovery.location == "Remote, Canada"
    assert requeued.pending_discovery.staged_facts.identity_trust == "unverified_hint"

    with durable_database.session() as session:
        restored = session.get(PendingDiscovery, staged_pending.id)
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "pending_discovery.requeued",
            )
        )
        assert restored is not None
        assert restored.archived_at is None
        assert restored.withdrawn_at is None
        assert event is not None
        assert event.entity_type == "pending_discovery"
        assert event.entity_id == staged_pending.id


def test_requeue_preserves_a_publisher_trusted_identity_field(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    url = "https://jobs.lever.co/globex/14c7226d-b454-4352-8251-733d52a1e943"
    complete = PostingCheck(
        status="readable",
        note="fixture",
        platform="lever",
        employer="Globex",
        title="Platform Lead",
        location="Remote, Canada",
        facts=published_listing_facts("lever", locations="Remote, Canada"),
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: complete,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staged = service.stage_found_posting(
        workspace_id, url, date(2026, 8, 24), check_publisher_facts=True
    )
    assert staged is not None
    mark_reset_archived(durable_database, workspace_id, staged.pending_discovery.id)

    partial = complete.__class__(
        status="readable",
        note="fixture",
        platform="lever",
        title="Senior Platform Lead",
        location="Remote, Canada",
        facts=complete.facts,
    )
    requeue_service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_checker=lambda _url, **_: partial,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    requeued = requeue_service.stage_found_posting(
        workspace_id, url, date(2026, 8, 25), check_publisher_facts=True
    )

    assert requeued is not None
    assert requeued.pending_discovery.employer == "Globex"
    assert requeued.pending_discovery.title == "Senior Platform Lead"
    assert requeued.pending_discovery.staged_facts.identity_trust == "publisher"


def test_adding_an_already_tracked_posting_remains_a_conflict(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    url = "https://jobs.lever.co/globex/36bb6180-7295-4f1b-9865-c4495f2d04d3"
    with durable_database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Globex",
            normalized_name="globex",
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Platform Lead",
            normalized_title="platform lead",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="application",
                url=url,
                normalized_url=url,
                is_primary=True,
            )
        )

    result = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).stage_found_posting(workspace_id, url, date(2026, 8, 25))

    assert result is None


def test_a_found_posting_naming_the_local_machine_is_refused(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with pytest.raises(ValidationError, match="cannot be read"):
        service.stage_found_posting(workspace_id, "http://127.0.0.1/jobs/1", utc_now().date())

    assert service.list_pending_discoveries(workspace_id) == []


def test_correcting_a_pending_discovery_title_persists_and_projects_the_row(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert staged is not None
    staged_pending = staged.pending_discovery
    original_reason = staged_pending.reason
    original_first_seen = staged_pending.first_seen_on
    with durable_database.session() as session:
        original_row = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.id == staged_pending.id)
        )
        assert original_row is not None
        original_fingerprint = original_row.fingerprint
        original_source_id = original_row.source_id

    corrected = service.correct_pending_discovery_title(
        workspace_id,
        staged_pending.id,
        "Principal Data Product Manager",
        correlation_id="corr-1",
    )

    assert corrected.id == staged_pending.id
    assert corrected.title == "Principal Data Product Manager"
    assert corrected.discovery_url == url
    assert corrected.reason == original_reason
    assert corrected.first_seen_on == original_first_seen

    with durable_database.session() as session:
        after = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.id == staged_pending.id)
        )
        assert after is not None
        assert after.fingerprint == original_fingerprint
        assert after.source_id == original_source_id
        assert after.reason == original_reason
        assert after.first_seen_on == original_first_seen


def test_correcting_a_missing_pending_discovery_raises_not_found(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with pytest.raises(NotFoundError, match="does not exist"):
        service.correct_pending_discovery_title(
            workspace_id, "00000000-0000-0000-0000-000000000000", "Whatever"
        )


def test_correcting_an_archived_pending_discovery_raises_conflict(
    durable_database: Database,
) -> None:

    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert staged is not None
    pending_id = staged.pending_discovery.id
    with durable_database.session() as session:
        row = session.scalar(select(PendingDiscovery).where(PendingDiscovery.id == pending_id))
        assert row is not None
        row.archived_at = utc_now()

    with pytest.raises(ConflictError, match="archived"):
        service.correct_pending_discovery_title(workspace_id, pending_id, "New Title")


def test_correcting_a_pending_with_an_active_evaluation_task_raises_conflict(
    durable_database: Database,
) -> None:
    from clearscout.application.tasks import EvaluationWorkflowPayload, TaskQueueService

    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert staged is not None
    pending_id = staged.pending_discovery.id
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload=EvaluationWorkflowPayload(pending_discovery_id=pending_id).model_dump(),
        idempotency_key=f"evaluate-{pending_id}",
    )

    with pytest.raises(ConflictError, match="in flight"):
        service.correct_pending_discovery_title(workspace_id, pending_id, "New Title")


def test_correcting_a_pending_with_an_active_guided_campaign_raises_conflict(
    durable_database: Database,
) -> None:
    from clearscout.application.tasks import TaskQueueService

    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert staged is not None
    pending_id = staged.pending_discovery.id
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="search.guided_campaign",
        payload={},
        idempotency_key=f"guided-{pending_id}",
    )

    with pytest.raises(ConflictError, match="in flight"):
        service.correct_pending_discovery_title(workspace_id, pending_id, "New Title")


def test_correcting_a_pending_named_in_an_active_evaluate_batch_raises_conflict(
    durable_database: Database,
) -> None:
    from clearscout.application.tasks.payloads import DiscoveryEvaluateBatchPayload

    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    guarded_url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    other_url = "https://jobs.lever.co/globex/a1111111-4c2e-4b7d-9f1a-6e3d2c1b0a99"
    guarded = service.stage_found_posting(workspace_id, guarded_url, utc_now().date())
    other = service.stage_found_posting(workspace_id, other_url, utc_now().date())
    assert guarded is not None
    assert other is not None
    guarded_id = guarded.pending_discovery.id
    other_id = other.pending_discovery.id
    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate_batch",
        payload=DiscoveryEvaluateBatchPayload(pending_discovery_ids=[guarded_id]).model_dump(),
        idempotency_key=f"evaluate-batch-{guarded_id}",
    )

    with pytest.raises(ConflictError, match="in flight"):
        service.correct_pending_discovery_title(workspace_id, guarded_id, "New Title")
    # A row the running batch never named corrects normally.
    service.correct_pending_discovery_title(workspace_id, other_id, "New Title")


def test_correcting_a_title_writes_a_hint_corrected_audit_event(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = service.stage_found_posting(workspace_id, url, utc_now().date(), title="Old Title")
    assert staged is not None
    pending_id = staged.pending_discovery.id

    service.correct_pending_discovery_title(
        workspace_id, pending_id, "New Title", actor="local_user", correlation_id="corr-2"
    )

    from clearscout.infrastructure.persistence.models import AuditEvent

    with durable_database.session() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "pending_discovery.hint_corrected",
                )
            )
        )
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "pending_discovery"
    assert event.entity_id == pending_id
    assert event.details["old_title"] == "Old Title"
    assert event.details["new_title"] == "New Title"
    assert event.created_by == "local_user"
    assert event.correlation_id == "corr-2"


def test_correcting_a_title_clears_a_disputed_review_with_its_dispute_and_audits_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    url = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
    staged = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).stage_found_posting(workspace_id, url, utc_now().date(), title="Old Title")
    assert staged is not None
    pending_id = staged.pending_discovery.id
    record_relevance_review(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        review_id = session.scalars(
            select(RelevanceReview.id).where(RelevanceReview.pending_discovery_id == pending_id)
        ).one()
    RelevanceDisputeService(durable_database).dispute(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        relevance_review_id=review_id,
        note=None,
        audit=audit_columns("local_user", "test", None),
    )

    PendingDiscoveryHintsService(durable_database).correct_title(
        workspace_id, pending_id, "New Title"
    )

    with durable_database.session() as session:
        assert session.scalars(select(RelevanceReview.id)).all() == []
        assert session.scalars(select(RelevanceReviewDispute.id)).all() == []
        cleared = session.scalars(
            select(AuditEvent.details).where(AuditEvent.action == "relevance_dispute.cleared")
        ).all()
    assert cleared == [{"pending_discovery_id": pending_id, "relevance_review_id": review_id}]


def test_repeated_pending_discovery_updates_mutable_board_facts(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://boards.greenhouse.io/globex/jobs/1?utm_source=feed"
    service.stage_listings(
        workspace_id,
        [SourceListing("Globex", "Data Lead", url, "Remote", "REQ-1")],
        utc_now().date(),
        reason="First scan.",
    )

    created = service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex",
                "Principal Data Lead",
                "https://boards.greenhouse.io/globex/jobs/1",
                "Toronto",
                "REQ-2",
            )
        ],
        utc_now().date(),
        reason="Later scan.",
    )

    assert created.queued == []
    queue = service.list_pending_discoveries(workspace_id)
    assert len(queue) == 1
    assert queue[0].title == "Principal Data Lead"
    assert queue[0].location == "Toronto"
    assert queue[0].reason == "Later scan."


_LISTING_URL = "https://boards.greenhouse.io/globex/jobs/1"
_RECOVERED_URL = "https://careers.globex.example/jobs/data-lead"


def _recover_elsewhere(
    database: Database, tmp_path: Path, workspace_id: str, source_id: str | None = None
) -> str:
    """Stage the board listing, then store a verified read of it at another address."""
    _stage(database, tmp_path, workspace_id, [_board_listing("Globex", _LISTING_URL)], source_id)
    content = PostingContentResolutionService(
        database, UnexpectedContentResolver(), storage=runtime_storage(database, tmp_path)
    )
    with database.session() as session:
        pending_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.workspace_id == workspace_id)
        ).one()
    [target] = content.preflight(workspace_id, [pending_id]).targets
    content.store_assisted_result(
        workspace_id,
        target,
        ResolvedPostingContent(
            requested_url=_RECOVERED_URL,
            final_url=_RECOVERED_URL,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=b"Operations Lead\nRun the regional operations desk.",
        ),
        None,
    )
    return pending_id


def _rows_as_read(
    database: Database, workspace_id: str
) -> list[tuple[str, str, PendingDiscoveryReadiness]]:
    with database.session() as session:
        rows = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        return [
            (
                row.id,
                row.canonical_url,
                pending_discovery_readiness(
                    row,
                    session.get(PendingPostingContent, row.current_content_id)
                    if row.current_content_id
                    else None,
                ),
            )
            for row in rows
        ]


def test_a_rescan_of_its_listing_keeps_a_recovered_row_at_its_recovered_address(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id)

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", _LISTING_URL)], None)

    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]


def test_a_scan_listing_the_recovered_address_refreshes_the_recovered_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id)

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", f"{_RECOVERED_URL}/?utm_source=board")],
        None,
    )

    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]


def _listing_board_and_careers_site(database: Database, workspace_id: str) -> tuple[str, str]:
    return (
        board_source(database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"),
        board_source(database, workspace_id, "Globex careers", "https://careers.globex.example"),
    )


def _source_row(database: Database, pending_id: str) -> tuple[str, str | None, bool]:
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        return row.discovery_url, row.source_id, row.withdrawn_at is not None


def test_two_sources_listing_the_row_in_turn_keep_it_at_its_recovered_address(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board, careers = _listing_board_and_careers_site(durable_database, workspace_id)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id, board)

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _RECOVERED_URL)],
        careers,
    )
    _stage(
        durable_database, tmp_path, workspace_id, [_board_listing("Globex", _LISTING_URL)], board
    )

    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]
    assert _source_row(durable_database, pending_id) == (_LISTING_URL, board, False)


@pytest.mark.parametrize("recovered_first", [True, False])
def test_one_read_listing_both_addresses_keeps_the_row_at_its_recovered_address(
    durable_database: Database, tmp_path: Path, recovered_first: bool
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board, _careers = _listing_board_and_careers_site(durable_database, workspace_id)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id, board)
    pair = [_board_listing("Globex", _RECOVERED_URL), _board_listing("Globex", _LISTING_URL)]

    _stage(durable_database, tmp_path, workspace_id, pair if recovered_first else pair[::-1], board)

    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]


def test_a_refresh_only_read_of_the_recovered_address_refreshes_without_moving_the_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board, careers = _listing_board_and_careers_site(durable_database, workspace_id)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id, board)
    listed = _board_listing("Globex", _RECOVERED_URL)

    staged = _staging(durable_database, tmp_path).stage_listings(
        workspace_id,
        [listed],
        date(2026, 8, 27),
        reason="Board scan matched the title filter.",
        source_id=careers,
        refresh_only=True,
    )

    assert (staged.inserted, staged.requeued) == ([listed], [])
    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]
    assert _source_row(durable_database, pending_id) == (_LISTING_URL, careers, False)


def _complete_read_yesterday(database: Database, source_id: str) -> None:
    """Record a complete read of the source finishing after the row's recovered read."""
    finished = utc_now()
    with database.session() as session:
        session.add(
            SourceScan(
                source_id=source_id,
                scanned_on=finished.date() - timedelta(days=1),
                query_label="title-filter",
                status="completed",
                read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                raw_count=1,
                read_count=1,
                skip_reasons={},
                result_count=1,
                new_count=0,
                notes="",
                scan_started_at=finished,
                scan_completed_at=finished,
                duration_ms=0,
                complete=True,
            )
        )


def test_a_recovered_row_the_listing_board_drops_stays_with_the_source_listing_its_address(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board, careers = _listing_board_and_careers_site(durable_database, workspace_id)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id, board)
    at_careers = [_board_listing("Globex", _RECOVERED_URL)]
    _stage(durable_database, tmp_path, workspace_id, at_careers, careers)
    _complete_read_yesterday(durable_database, board)

    _staging(durable_database, tmp_path).withdraw_absent(workspace_id, board, [], utc_now().date())
    _stage(durable_database, tmp_path, workspace_id, at_careers, careers)

    with durable_database.session() as session:
        pulled = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action.in_(
                    ["pending_discovery.withdrawn_absent", "pending_discovery.relisted"]
                ),
            )
        ).all()
    assert pulled == []
    assert _source_row(durable_database, pending_id) == (_LISTING_URL, careers, False)
    assert _rows_as_read(durable_database, workspace_id) == [
        (pending_id, _RECOVERED_URL, PendingDiscoveryReadiness.READY)
    ]


@pytest.mark.parametrize("reader", ["careers", "board"])
def test_a_complete_read_listing_only_the_recovered_address_keeps_the_row(
    durable_database: Database, tmp_path: Path, reader: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board, careers = _listing_board_and_careers_site(durable_database, workspace_id)
    pending_id = _recover_elsewhere(durable_database, tmp_path, workspace_id, board)
    source_id = careers if reader == "careers" else board
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _RECOVERED_URL)],
        source_id,
    )
    _complete_read_yesterday(durable_database, source_id)

    withdrawn = _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, source_id, [_RECOVERED_URL], utc_now().date()
    )

    assert withdrawn == []
    assert _source_row(durable_database, pending_id) == (_LISTING_URL, source_id, False)


def test_a_row_whose_listing_address_changed_form_follows_the_listing(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    tracked = f"{_LISTING_URL}?utm_source=feed"
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", tracked)], None)

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", _LISTING_URL)], None)

    with durable_database.session() as session:
        [row] = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        assert (row.discovery_url, row.canonical_url) == (_LISTING_URL, _LISTING_URL)


_WORKDAY_LOCALE_URL = (
    "https://acme.wd1.myworkdayjobs.com/en-US/acme_careers/job/Product-Manager_R10001"
)


@pytest.mark.parametrize(
    ("listed", "staged_rows"),
    [
        (
            "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10001",
            1,
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10002",
            2,
        ),
    ],
    ids=["same requisition", "another requisition"],
)
def test_a_board_listing_at_a_workday_path_variant_refreshes_the_staged_requisition(
    durable_database: Database, tmp_path: Path, *, listed: str, staged_rows: int
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Acme", _WORKDAY_LOCALE_URL)],
        None,
    )

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Acme", listed)], None)

    with durable_database.session() as session:
        rows = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
    assert len(rows) == staged_rows


_CAREERS_EN_URL = "https://careers.example.com/en/careers/principal-data-engineer-500001/job/"


@pytest.mark.parametrize(
    ("listed", "staged_urls"),
    [
        (_CAREERS_EN_URL.replace("/en/", "/jp/"), [_CAREERS_EN_URL]),
        (
            "https://careers.example.com/en/careers/staff-data-engineer-500002/job/",
            sorted(
                [
                    _CAREERS_EN_URL,
                    "https://careers.example.com/en/careers/staff-data-engineer-500002/job/",
                ]
            ),
        ),
    ],
    ids=["same path under another locale", "another path"],
)
def test_a_careers_site_listing_under_another_locale_refreshes_the_staged_row(
    durable_database: Database, tmp_path: Path, *, listed: str, staged_urls: list[str]
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _CAREERS_EN_URL, "")],
        None,
    )

    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", listed, "")], None)

    with durable_database.session() as session:
        urls = session.scalars(
            select(PendingDiscovery.discovery_url).where(
                PendingDiscovery.workspace_id == workspace_id
            )
        ).all()
    assert sorted(urls) == staged_urls


def test_a_found_posting_under_another_locale_requeues_the_english_row_with_its_names(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _CAREERS_EN_URL, "", location="Springfield")],
        None,
    )
    with durable_database.session() as session:
        english_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.workspace_id == workspace_id)
        ).one()
    mark_reset_archived(durable_database, workspace_id, english_id)
    japanese = PostingCheck(
        status="readable",
        note="The publisher restated the posting.",
        platform="",
        title="Data Engineer (Tokyo office)",
        employer="Globex KK",
        location="Tokyo",
        facts=published_listing_facts("", locations="Tokyo"),
    )

    requeued = _staging(durable_database, tmp_path).stage_found_posting(
        workspace_id,
        _CAREERS_EN_URL.replace("/en/", "/jp/"),
        date(2026, 8, 27),
        publisher_check=japanese,
    )

    assert requeued is not None and requeued.requeued
    with durable_database.session() as session:
        english = session.get(PendingDiscovery, english_id)
        assert english is not None
        assert (english.employer, english.title, english.location) == (
            "Globex",
            "Operations Lead",
            "Springfield",
        )
        assert english.archived_at is None


_ACME_LOCALE_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Fleet-Planner_R20001"
_ACME_CANADA_URL = (
    "https://acme.wd5.myworkdayjobs.com/careers/job/Canada-Remote/Fleet-Planner_R20001"
)
_ACME_US_URL = "https://acme.wd5.myworkdayjobs.com/careers/job/US-Remote/Fleet-Planner_R20001"
_ACME_OTHER_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Route-Analyst_R20002"


def test_a_complete_read_listing_a_workday_path_variant_keeps_the_staged_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Acme")
    staged = [_board_listing("Acme", _ACME_LOCALE_URL), _board_listing("Acme", _ACME_OTHER_URL)]
    _stage(durable_database, tmp_path, workspace_id, staged, board)
    _stage(
        durable_database, tmp_path, workspace_id, [_board_listing("Acme", _ACME_CANADA_URL)], board
    )

    gone = _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, board, [_ACME_CANADA_URL], date(2026, 8, 26)
    )

    assert gone == ["Operations Lead"]
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_ACME_LOCALE_URL]
    with durable_database.session() as session:
        withdrawn = session.scalars(
            select(PendingDiscovery.discovery_url).where(
                PendingDiscovery.workspace_id == workspace_id,
                PendingDiscovery.withdrawn_at.is_not(None),
            )
        ).all()
    assert withdrawn == [_ACME_OTHER_URL]


def test_a_complete_read_listing_one_regional_url_withdraws_the_unlisted_one(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Acme")
    family = [
        _board_listing("Acme", _ACME_CANADA_URL, "R20001", location="Northtown"),
        _board_listing("Acme", _ACME_US_URL, "R20001", location="Southport"),
    ]
    _stage(durable_database, tmp_path, workspace_id, family, board)
    _stage(durable_database, tmp_path, workspace_id, family[1:], board)

    gone = _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, board, [_ACME_US_URL], date(2026, 8, 26)
    )

    assert gone == ["Operations Lead"]
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_ACME_US_URL]


def test_a_scan_counts_a_workday_path_variant_of_a_staged_requisition_as_known(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Acme")
    _stage(
        durable_database, tmp_path, workspace_id, [_board_listing("Acme", _ACME_LOCALE_URL)], board
    )
    variant = _board_listing("Acme", _ACME_CANADA_URL)

    partition = _staging(durable_database, tmp_path).partition_scan_listings(
        workspace_id, [variant], new_limit=0
    )

    assert partition == ([], [variant], [])


def test_a_scan_counts_the_read_address_of_a_re_pointed_row_as_known(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _recover_elsewhere(durable_database, tmp_path, workspace_id)
    listed = _board_listing("Globex", _RECOVERED_URL)

    partition = _staging(durable_database, tmp_path).partition_scan_listings(
        workspace_id, [listed], new_limit=0
    )

    assert partition == ([], [listed], [])


def test_a_workday_path_variant_refreshes_the_live_row_of_its_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    for url in (_ACME_LOCALE_URL, _ACME_US_URL):
        _pending(durable_database, workspace_id, url=url, first_seen_on=date(2026, 8, 20))
    with durable_database.session() as session:
        # The twin the table returns last is the one withdrawn, whatever order ids take.
        live_row, withdrawn_row = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
        withdrawn_row.withdrawn_at = utc_now()
        live_id, withdrawn_id, live_url = live_row.id, withdrawn_row.id, live_row.discovery_url

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Acme", _ACME_CANADA_URL, location="Northtown")],
        None,
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == [live_url]
    with durable_database.session() as session:
        live = session.get(PendingDiscovery, live_id)
        twin = session.get(PendingDiscovery, withdrawn_id)
        assert live is not None and twin is not None
        assert live.location == "Northtown"
        assert twin.withdrawn_at is not None


def test_a_workday_path_variant_refreshes_the_head_of_a_regional_family_seen_the_same_day(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    # The variant's id sorts first, so only the head preference can pick the head.
    head_id, variant_id = "ffffffff-head", "00000000-variant"
    with durable_database.session() as session:
        for row_id, url, variant_of in (
            (head_id, _ACME_CANADA_URL, None),
            (variant_id, _ACME_US_URL, head_id),
        ):
            session.add(
                PendingDiscovery(
                    id=row_id,
                    workspace_id=workspace_id,
                    fingerprint=url_sha256(url),
                    employer="Acme",
                    title="Operations Lead",
                    location="Remote",
                    discovery_url=url,
                    canonical_url=url,
                    reason="Board scan matched the title filter.",
                    first_seen_on=date(2026, 8, 20),
                    regional_variant_of=variant_of,
                )
            )
            session.flush()

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Acme", _ACME_LOCALE_URL, location="Northtown")],
        None,
    )

    with durable_database.session() as session:
        head = session.get(PendingDiscovery, head_id)
        variant = session.get(PendingDiscovery, variant_id)
        assert head is not None and variant is not None
        assert (head.location, variant.location) == ("Northtown", "Remote")


def test_two_workday_regional_urls_of_one_requisition_in_one_scan_fold_as_variants(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Acme")

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [
            _board_listing("Acme", _ACME_CANADA_URL, "R20001", location="Northtown"),
            _board_listing("Acme", _ACME_US_URL, "R20001", location="Southport"),
        ],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {
        _ACME_CANADA_URL: None,
        _ACME_US_URL: _ACME_CANADA_URL,
    }
    with durable_database.session() as session:
        head = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.discovery_url == _ACME_CANADA_URL)
        ).one()
        assert head.location == "Northtown"


def test_a_board_refresh_preserves_a_human_corrected_title(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://boards.greenhouse.io/globex/jobs/1"
    service.stage_listings(
        workspace_id,
        [SourceListing("Globex", "Original title", url, "Remote")],
        utc_now().date(),
        reason="First scan.",
    )
    [pending] = service.list_pending_discoveries(workspace_id)
    service.correct_pending_discovery_title(workspace_id, pending.id, "Human corrected title")

    service.stage_listings(
        workspace_id,
        [SourceListing("Globex", "Publisher revised title", url, "Northtown")],
        utc_now().date(),
        reason="Later scan.",
    )

    [refreshed] = service.list_pending_discoveries(workspace_id)
    assert refreshed.title == "Human corrected title"
    assert refreshed.location == "Northtown"


def test_structured_listing_facts_are_updated_with_a_pending_discovery(
    durable_database: Database,
) -> None:
    """A repeated board read replaces the mutable staging facts as one document."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.example.test/opening-1"
    first = SourceListing(
        "Globex",
        "Platform Lead",
        url,
        "Remote",
        facts=published_listing_facts(
            "synthetic_board", locations="Remote", employment_types="Permanent"
        ),
    )
    revised = SourceListing(
        "Globex",
        "Platform Lead",
        url,
        "Northtown",
        facts=published_listing_facts(
            "synthetic_board", locations="Northtown", employment_types="Full-time"
        ),
    )

    service.stage_listings(workspace_id, [first], utc_now().date(), reason="First read.")
    service.stage_listings(workspace_id, [revised], utc_now().date(), reason="Second read.")

    with durable_database.session() as session:
        stored = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        assert stored is not None
        assert stored.staged_facts["schema_version"] == 5
        assert stored.staged_facts["locations"] == {
            "values": ["Northtown"],
            "unresolved_values": [],
            "provenance": "synthetic_board.location",
            "complete": True,
        }
        assert stored.staged_facts["employment_types"]["values"] == ["Full-time"]


def test_a_board_read_silent_on_place_keeps_the_places_an_earlier_read_stated(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.example.test/opening-1"
    declared = SourceListing(
        "Globex",
        "Platform Lead",
        url,
        "Northtown, Ontario, Canada",
        facts=published_listing_facts(
            "schema_org",
            locations="Northtown, Ontario, Canada",
            employment_types="Full-time",
        ),
    )
    silent = SourceListing(
        "Globex", "Platform Lead", url, "", facts=published_listing_facts("synthetic_board")
    )

    service.stage_listings(workspace_id, [declared], utc_now().date(), reason="First read.")
    service.stage_listings(workspace_id, [silent], utc_now().date(), reason="Second read.")

    with durable_database.session() as session:
        stored = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        assert stored is not None
        facts = StagedListingFacts.from_json(stored.staged_facts)
        assert facts.locations.values == ("Northtown, Ontario, Canada",)
        assert facts.locations.provenance == "schema_org.location"
        assert facts.locations.complete
        assert facts.employment_types.values == ("Full-time",)


def _dated_listing(posted: str) -> SourceListing:
    facts = published_listing_facts("greenhouse", locations="Northtown")
    return SourceListing(
        "Globex",
        "Platform Lead",
        "https://jobs.example.test/opening-1",
        "Northtown",
        facts=replace(
            facts,
            posted_date=publisher_posted_date(
                posted, basis="first_published", grade=PostedDateGrade.FIRST_POSTED
            ),
        ),
    )


def test_board_reads_keep_the_earliest_posted_date_and_record_a_later_one_as_a_repost(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    for posted, read_on in (
        ("2026-07-29T10:00:00-04:00", date(2026, 8, 1)),
        ("2026-09-29T10:00:00-04:00", date(2026, 9, 30)),
        ("", date(2026, 10, 2)),
    ):
        service.stage_listings(workspace_id, [_dated_listing(posted)], read_on, reason="Read.")

    with durable_database.session() as session:
        stored = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        assert stored is not None
        posted_date = StagedListingFacts.from_json(stored.staged_facts).posted_date
    assert posted_date is not None
    assert posted_date.day == date(2026, 7, 29)
    assert posted_date.reposted
    assert (posted_date.latest.wording, posted_date.latest.read_on) == (
        "2026-09-29T10:00:00-04:00",
        date(2026, 9, 30),
    )


def test_a_first_read_echoing_its_own_day_with_another_year_stages_no_sortable_date(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.stage_listings(
        workspace_id, [_dated_listing("2024-10-22")], date(2026, 10, 22), reason="Read."
    )

    (row,) = service.list_pending_discoveries(workspace_id)
    assert row.staged_facts.posted_date is not None
    assert row.staged_facts.posted_date.earliest.wording == "2024-10-22"
    assert row.staged_facts.posted_date.needs_clarification
    assert row.staged_facts.posted_date.day is None


@pytest.mark.parametrize(
    ("restated", "kept"),
    [("Northtown, ON", ()), ("", ("onsite",))],
    ids=["place-restated-without-suffix", "place-silent"],
)
def test_an_arrangement_read_off_a_place_lasts_only_as_long_as_that_place(
    durable_database: Database, restated: str, kept: tuple[str, ...]
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.example.test/opening-1"
    suffixed = "Northtown, ON (On-site)"
    first = SourceListing(
        "Globex",
        "Platform Lead",
        url,
        suffixed,
        facts=published_listing_facts("synthetic_board", locations=suffixed),
    )
    second = SourceListing(
        "Globex",
        "Platform Lead",
        url,
        restated,
        facts=published_listing_facts("synthetic_board", locations=restated or None),
    )

    service.stage_listings(workspace_id, [first], utc_now().date(), reason="First read.")
    service.stage_listings(workspace_id, [second], utc_now().date(), reason="Second read.")

    with durable_database.session() as session:
        stored = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        assert stored is not None
        assert StagedListingFacts.from_json(stored.staged_facts).arrangements.values == kept


def test_requeue_backfills_a_blank_employer_from_a_supported_board_url(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    url = "https://jobs.lever.co/globex/04c7226d-b454-4352-8251-733d52a1e943"
    first = service.stage_found_posting(workspace_id, url, utc_now().date())
    assert first is not None
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, first.pending_discovery.id)
        assert pending is not None
        pending.employer = ""
    mark_reset_archived(durable_database, workspace_id, first.pending_discovery.id)

    requeued = service.stage_found_posting(workspace_id, url, utc_now().date())

    assert requeued is not None
    assert requeued.outcome == "requeued"
    assert requeued.pending_discovery.id == first.pending_discovery.id
    assert requeued.pending_discovery.employer == "Globex"


def test_backfill_employer_from_greenhouse_url_uses_tenant_slug(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("", "", "https://job-boards.greenhouse.io/acme-health/jobs/1")],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Acme Health"


def test_backfill_employer_from_lever_url_uses_tenant_slug(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("", "Data Lead", "https://jobs.lever.co/globex/uuid-1")],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Globex"


def test_backfill_employer_from_ashby_url_uses_tenant_slug(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("", "Data Lead", "https://jobs.ashbyhq.com/example/uuid-1")],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Example"


def test_backfill_employer_from_workday_url_uses_subdomain(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "",
                "",
                "https://globex.wd5.myworkdayjobs.com/en-US/careers/job/Remote/Data-Lead_R123",
            )
        ],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Globex"


def test_backfill_ignores_urls_that_name_no_known_board(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("", "Data Lead", "https://careers.example.com/postings/42")],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == ""


def test_backfill_never_overwrites_a_stated_employer(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Acme Health, Inc.",
                "Data Lead",
                "https://job-boards.greenhouse.io/globex/jobs/1",
            )
        ],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Acme Health, Inc."


def test_a_slug_backfilled_employer_is_staged_as_an_unverified_hint(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("", "Data Lead", "https://job-boards.greenhouse.io/acme-health/jobs/1")],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Acme Health"
        assert not StagedListingFacts.from_json(row.staged_facts).publisher_vouched


def test_a_stated_employer_keeps_its_publisher_trust_through_staging(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Acme Health, Inc.",
                "",
                "https://job-boards.greenhouse.io/acme-health/jobs/1",
            )
        ],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Acme Health, Inc."
        assert StagedListingFacts.from_json(row.staged_facts).publisher_vouched


def test_a_slug_title_does_not_cost_a_stated_employer_its_publisher_trust(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Acme Health, Inc.",
                "",
                "https://job-boards.greenhouse.io/acme-health/jobs/1/staff-systems-analyst",
            )
        ],
        utc_now().date(),
        reason="scan",
    )
    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        assert row.employer == "Acme Health, Inc."
        assert row.title == "staff systems analyst"
        assert StagedListingFacts.from_json(row.staged_facts).publisher_vouched


def _stage_twice(database: Database, workspace_id: str, listing: SourceListing) -> PendingDiscovery:
    """Stage one listing, then stage it again so the refresh branch rewrites the row."""
    service = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    for _ in range(2):
        service.stage_listings(workspace_id, [listing], utc_now().date(), reason="scan")
    with database.session() as session:
        return session.scalars(select(PendingDiscovery)).one()


def test_a_rescanned_listing_with_no_display_text_keeps_the_names_its_slug_gives(
    durable_database: Database,
) -> None:
    row = _stage_twice(
        durable_database,
        scan_workspace(durable_database),
        SourceListing(
            "",
            "",
            "https://job-boards.greenhouse.io/globex-health/jobs/1/staff-data-platform-manager",
        ),
    )

    assert row.employer == "Globex Health"
    assert row.title == "staff data platform manager"


def test_a_rescanned_listing_on_an_unsupported_url_shape_stays_blank_rather_than_invented(
    durable_database: Database,
) -> None:
    row = _stage_twice(
        durable_database,
        scan_workspace(durable_database),
        SourceListing("", "", "https://careers.example.org/openings/4821"),
    )

    assert row.employer == ""
    assert row.title == ""


def test_a_degraded_rescan_keeps_the_names_an_earlier_read_established(
    durable_database: Database,
) -> None:
    """A board read that says nothing must not erase what a publisher already said.

    The slug cannot help an unsupported URL shape, so without this the second
    pass writes the empty string over a name the row already carried.
    """
    workspace_id = scan_workspace(durable_database)
    url = "https://careers.example.org/openings/4821"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("Northwind Fabrication", "Systems Steward", url, location="Remote")],
        utc_now().date(),
        reason="scan",
    )

    service.stage_listings(
        workspace_id, [SourceListing("", "", url)], utc_now().date(), reason="scan"
    )

    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
    assert row.employer == "Northwind Fabrication"
    assert row.title == "Systems Steward"
    assert row.location == "Remote"


@pytest.mark.parametrize(
    ("was_vouched", "listed_employer", "employer", "vouched"),
    [
        (False, "", "Acme", False),
        (False, "Globex", "Globex", True),
        (True, "", "Acme", True),
    ],
)
def test_a_rescan_vouches_only_for_the_names_the_listing_states(
    durable_database: Database,
    listed_employer: str,
    employer: str,
    *,
    was_vouched: bool,
    vouched: bool,
) -> None:
    workspace_id = scan_workspace(durable_database)
    url = "https://careers.example.org/openings/4821"
    pending_id = _pending(durable_database, workspace_id, url=url, first_seen_on=utc_now().date())
    if was_vouched:
        with durable_database.session() as session:
            stored = session.get(PendingDiscovery, pending_id)
            assert stored is not None
            stored.staged_facts = published_listing_facts("greenhouse").as_json()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                listed_employer,
                "Operations Lead",
                url,
                location="Remote",
                facts=published_listing_facts("publisher_html", locations="Remote"),
            )
        ],
        utc_now().date(),
        reason="scan",
    )

    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
    assert row.employer == employer
    assert StagedListingFacts.from_json(row.staged_facts).publisher_vouched is vouched


def test_a_degraded_rescan_keeps_the_requisition_id_an_earlier_read_established(
    durable_database: Database,
) -> None:
    """A board read silent on the requisition must not erase the stored identity."""
    workspace_id = scan_workspace(durable_database)
    url = "https://careers.example.org/openings/4821"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [SourceListing("Northwind Fabrication", "Systems Steward", url, requisition_id="REQ-4821")],
        utc_now().date(),
        reason="scan",
    )

    service.stage_listings(
        workspace_id, [SourceListing("", "", url)], utc_now().date(), reason="scan"
    )

    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
    assert row.requisition_id == "REQ-4821"


def test_a_verified_active_body_keeping_an_absent_row_records_it_as_verified_current(
    durable_database: Database, tmp_path: Path
) -> None:
    """The evidence that outvoted board absence is stated on the row a person reads."""
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = employer_board(durable_database, workspace_id, "Acme")
    url = "https://acme.example.test/careers/operations-lead"
    pending_id = _pending(
        durable_database,
        workspace_id,
        url=url,
        first_seen_on=date(2026, 8, 24),
        source_id=source_id,
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        content = pending_posting_content(
            workspace_id,
            pending_id,
            url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
        )
        session.add(content)
        session.flush()
        row.current_content_id = content.id

    withdrawn = _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, source_id, [], date(2026, 8, 25)
    )

    assert withdrawn == []
    with durable_database.session() as session:
        kept = session.get(PendingDiscovery, pending_id)
        assert kept is not None
        assert kept.withdrawn_at is None
        assert (
            StagedListingFacts.from_json(kept.staged_facts).freshness
            is FreshnessState.VERIFIED_CURRENT
        )


def test_a_verified_active_row_storing_an_older_facts_document_keeps_what_it_stored(
    durable_database: Database, tmp_path: Path
) -> None:
    """A staged-facts document this reader cannot read keeps its facts instead of being blanked."""
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = employer_board(durable_database, workspace_id, "Acme")
    url = "https://acme.example.test/careers/operations-lead"
    stored_facts = {
        "schema_version": 2,
        "identity_trust": "publisher",
        "locations": {
            "values": ["Remote"],
            "unresolved_values": [],
            "provenance": "publisher",
            "complete": True,
        },
    }
    pending_id = _pending(
        durable_database,
        workspace_id,
        url=url,
        first_seen_on=date(2026, 8, 24),
        source_id=source_id,
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        content = pending_posting_content(
            workspace_id,
            pending_id,
            url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
        )
        session.add(content)
        session.flush()
        row.current_content_id = content.id
        row.staged_facts = stored_facts

    withdrawn = _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, source_id, [], date(2026, 8, 25)
    )

    assert withdrawn == []
    with durable_database.session() as session:
        kept = session.get(PendingDiscovery, pending_id)
        assert kept is not None
        assert kept.staged_facts == {
            **stored_facts,
            "freshness": FreshnessState.VERIFIED_CURRENT.value,
        }


_PAGED_ROLE_URL = "https://acme.example.test/careers/operations-lead"
_PAGED_READ = WithdrawingRead(raw_count=40, read_count=40, complete=True, listing_pages=2)


def _paged_absence(
    database: Database, tmp_path: Path, read: WithdrawingRead = _PAGED_READ
) -> tuple[str, str, str]:
    """Stage one row and run a complete read that misses it; return workspace, source and row."""
    workspace_id = _workspace(database, tmp_path)
    source_id = employer_board(database, workspace_id, "Acme")
    pending_id = _pending(
        database,
        workspace_id,
        url=_PAGED_ROLE_URL,
        first_seen_on=date(2026, 8, 24),
        source_id=source_id,
    )
    _withdraw_missing(database, tmp_path, workspace_id, source_id, read)
    return workspace_id, source_id, pending_id


def _withdraw_missing(
    database: Database,
    tmp_path: Path,
    workspace_id: str,
    source_id: str,
    read: WithdrawingRead = _PAGED_READ,
    present: tuple[str, ...] = (),
) -> list[str]:
    commands = TaskCommands(database, TaskEventLog(database))
    return _staging(database, tmp_path).withdraw_absent(
        workspace_id,
        source_id,
        list(present),
        utc_now().date(),
        correlation_id="scan-task",
        read=read,
        check_absent=queue_absence_checks(commands, workspace_id, "scan-task"),
    )


def _queued_checks(database: Database) -> list[list[str]]:
    with database.session() as session:
        return [
            task.payload["pending_discovery_ids"]
            for task in session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "discovery.refresh_batch")
            )
        ]


def _settle_checks(database: Database) -> None:
    with database.session() as session:
        for task in session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "discovery.refresh_batch")
        ):
            task.status = "completed"


def _end_checks(database: Database, status: str) -> list[str]:
    with database.session() as session:
        tasks = list(
            session.scalars(
                select(BackgroundTask).where(BackgroundTask.task_type == "discovery.refresh_batch")
            )
        )
        for task in tasks:
            task.status = status
        return [task.id for task in tasks]


def _withdrawn_at(database: Database, pending_id: str) -> datetime | None:
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        return row.withdrawn_at


def _checking_service(database: Database, checked: PostingCheck) -> SourceScanService:
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_checker=lambda _url, **_: checked,
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def test_a_multi_page_reads_first_miss_queues_a_check_and_an_active_body_keeps_the_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)

    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        content = pending_posting_content(
            workspace_id,
            pending_id,
            _PAGED_ROLE_URL,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
        )
        session.add(content)
        session.flush()
        row.current_content_id = content.id

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None


def test_a_closed_check_withdraws_a_row_a_multi_page_read_missed(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, _source_id, pending_id = _paged_absence(durable_database, tmp_path)
    assert _withdrawn_at(durable_database, pending_id) is None

    closed = PostingCheck(status="closed", note="The publisher answered HTTP 404.")
    _checking_service(durable_database, closed).refresh_pending_discovery(workspace_id, pending_id)

    assert _withdrawn_at(durable_database, pending_id) is not None
    assert _closed_causes(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingWithdrawalCause.PUBLISHER_CLOSED
    }


def test_a_failed_check_keeps_a_missed_row_until_the_next_complete_read_also_misses_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    errored = PostingCheck(status="unreadable", note="The publisher answered HTTP 503.")
    with pytest.raises(PublisherUnverifiedRefreshError):
        _checking_service(durable_database, errored).refresh_pending_discovery(
            workspace_id, pending_id
        )
    assert _withdrawn_at(durable_database, pending_id) is None
    _settle_checks(durable_database)

    gone = _withdraw_missing(durable_database, tmp_path, workspace_id, source_id)

    assert gone == ["Operations Lead"]
    assert _withdrawn_at(durable_database, pending_id) is not None
    assert _queued_checks(durable_database) == [[pending_id]]


def test_a_read_that_settles_twice_does_not_count_its_own_mark_as_a_second_miss(
    durable_database: Database, tmp_path: Path
) -> None:
    read = replace(_PAGED_READ, started_at=utc_now() - timedelta(minutes=5))
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path, read)

    _settle_checks(durable_database)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id, read) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]


def test_a_second_miss_keeps_the_mark_while_its_absence_check_is_still_queued(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.absent_since = utc_now() - timedelta(hours=1)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]


@pytest.mark.parametrize("ended", ["cancelled", "failed"])
def test_a_second_miss_after_an_unfinished_check_marks_the_row_again_and_queues_a_new_check(
    durable_database: Database, tmp_path: Path, ended: str
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    _end_checks(durable_database, ended)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id], [pending_id]]

    _settle_checks(durable_database)
    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == [
        "Operations Lead"
    ]


@pytest.mark.parametrize("bucket", ["failures", "content_failures"])
def test_a_completed_check_whose_read_of_the_row_failed_marks_the_row_again(
    durable_database: Database, tmp_path: Path, bucket: str
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    with durable_database.session() as session:
        for task in session.scalars(
            select(BackgroundTask).where(BackgroundTask.task_type == "discovery.refresh_batch")
        ):
            task.status = "completed"
            task.progress = {
                bucket: [
                    {
                        "pending_discovery_id": pending_id,
                        "error_category": "ConnectError",
                        "error_message": "The publisher did not answer.",
                    }
                ]
            }

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id], [pending_id]]


def test_a_second_miss_with_no_check_on_record_marks_the_row_again_and_queues_one(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = employer_board(durable_database, workspace_id, "Acme")
    pending_id = _pending(
        durable_database,
        workspace_id,
        url=_PAGED_ROLE_URL,
        first_seen_on=date(2026, 8, 24),
        source_id=source_id,
    )
    earlier = utc_now() - timedelta(hours=1)
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.absent_since = earlier

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.absent_since is not None
        assert row.absent_since > earlier


@pytest.mark.parametrize("unsettled", ["needs_review", "committing"])
def test_a_second_miss_keeps_the_mark_while_its_check_is_still_active(
    durable_database: Database, tmp_path: Path, unsettled: str
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    _end_checks(durable_database, unsettled)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]


class _CheckContext:
    """Run one queued absence check against its stored task."""

    def __init__(self, task: BackgroundTask) -> None:
        self.task_id = task.id
        self.task = task
        self.queue = self

    def get(self, task_id: str) -> BackgroundTask:
        assert task_id == self.task_id
        return self.task

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        del stage, payload

    def raise_if_cancelled(self) -> None:
        return None


def _run_queued_checks(database: Database, service: SourceScanService) -> None:
    handler = DiscoveryRefreshBatchHandler(service, content=None)  # type: ignore[arg-type]
    with database.session() as session:
        queued = session.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "discovery.refresh_batch",
                BackgroundTask.status == "queued",
            )
        ).all()
        session.expunge_all()
    for task in queued:
        result = handler(_CheckContext(task), task.payload)  # type: ignore[arg-type]
        with database.session() as session:
            stored = session.get(BackgroundTask, task.id)
            assert stored is not None
            stored.status = "completed"
            stored.progress = result


def test_a_dead_link_check_lets_the_next_miss_withdraw_the_row(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    dead = PostingCheck(
        status="unreadable",
        note="Greenhouse hosted careers page returned HTTP 404",
        platform="greenhouse",
        http_status=404,
    )
    service = _checking_service(durable_database, dead)

    for _miss in range(2):
        _run_queued_checks(durable_database, service)
        if _withdraw_missing(durable_database, tmp_path, workspace_id, source_id):
            break

    assert _withdrawn_at(durable_database, pending_id) is not None
    assert _queued_checks(durable_database) == [[pending_id]]


@pytest.mark.parametrize(
    "answer",
    [
        PostingCheck(
            status="unreadable",
            note="Greenhouse single posting returned HTTP 404, and the board listing held no jobs.",
            platform="greenhouse",
        ),
        PostingCheck(
            status="unreadable",
            note="Workday returned HTTP 403.",
            platform="workday",
            http_status=403,
        ),
    ],
)
def test_a_refresh_that_names_no_gone_status_is_not_a_dead_link(
    durable_database: Database, tmp_path: Path, answer: PostingCheck
) -> None:
    workspace_id, _source_id, pending_id = _paged_absence(durable_database, tmp_path)

    with pytest.raises(PublisherUnverifiedRefreshError):
        _checking_service(durable_database, answer).refresh_pending_discovery(
            workspace_id, pending_id
        )


def test_a_dismissed_marked_row_withdraws_on_the_next_miss_without_a_check(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = employer_board(durable_database, workspace_id, "Acme")
    pending_id = _pending(
        durable_database,
        workspace_id,
        url=_PAGED_ROLE_URL,
        first_seen_on=date(2026, 8, 24),
        source_id=source_id,
    )
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.absent_since = utc_now() - timedelta(hours=1)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == [
        "Operations Lead"
    ]
    assert _queued_checks(durable_database) == []


def test_a_retried_absence_check_holds_the_row_until_it_completes(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    [cancelled] = _end_checks(durable_database, "cancelled")
    TaskCommands(durable_database, TaskEventLog(durable_database)).retry(cancelled)

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _queued_checks(durable_database) == [[pending_id], [pending_id]]

    _settle_checks(durable_database)
    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == [
        "Operations Lead"
    ]
    assert _queued_checks(durable_database) == [[pending_id], [pending_id]]


def test_a_complete_read_listing_a_marked_row_resets_its_absence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    _withdraw_missing(
        durable_database, tmp_path, workspace_id, source_id, present=(_PAGED_ROLE_URL,)
    )

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id], [pending_id]]


def test_an_incomplete_read_listing_a_marked_row_resets_its_absence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, source_id, pending_id = _paged_absence(durable_database, tmp_path)
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Acme", _PAGED_ROLE_URL)],
        source_id,
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.absent_since is None

    assert _withdraw_missing(durable_database, tmp_path, workspace_id, source_id) == []
    assert _withdrawn_at(durable_database, pending_id) is None


_HIMALAYAS_EXPIRY = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _himalayas_listing(url: str, expiry: datetime | None) -> SourceListing:
    job: dict[str, Any] = {
        "title": "Clinical Systems Analyst",
        "applicationLink": url,
        "guid": url.rsplit("/", 1)[-1],
        "companyName": "Globex",
        "locationRestrictions": ["Northtown"],
    }
    if expiry is not None:
        job["expiryDate"] = int(expiry.timestamp())
    listing = source_listing(HIMALAYAS_MAPPING, Posting(job, employer=""))
    assert listing is not None
    return listing


def _himalayas_source(database: Database, workspace_id: str, name: str = "Himalayas") -> str:
    return board_source(
        database, workspace_id, name, "https://himalayas.app/jobs", source_type="aggregator"
    )


def _staging_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    monkeypatch.setattr(staging_service, "utc_now", lambda: at)


def _actions(database: Database, pending_id: str) -> list[str]:
    with database.session() as session:
        return list(
            session.scalars(
                select(AuditEvent.action)
                .where(
                    AuditEvent.entity_id == pending_id,
                    AuditEvent.action.in_(
                        ["pending_discovery.relisted", "pending_discovery.withdrawn_expired"]
                    ),
                )
                .order_by(AuditEvent.created_at)
            )
        )


def _state_listing_end(database: Database, pending_id: str, expiry: datetime) -> None:
    facts = _himalayas_listing("https://boards.example.test/jobs/any", expiry).facts
    with database.session() as session:
        session.get_one(PendingDiscovery, pending_id).staged_facts = facts.as_json()


def test_a_staged_row_leaves_the_queue_once_its_stated_expiry_passes(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    url = "https://boards.example.test/jobs/expiring-1"
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        source_id,
    )
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, source_id, [url])

    before = staging.withdraw_expired(
        workspace_id, source_id, now=_HIMALAYAS_EXPIRY - timedelta(seconds=1)
    )
    assert before == []
    assert _source_row(durable_database, pending_id)[2] is False

    after = staging.withdraw_expired(workspace_id, source_id, now=_HIMALAYAS_EXPIRY)

    assert after == ["Clinical Systems Analyst"]
    assert _source_row(durable_database, pending_id)[2] is True
    with durable_database.session() as session:
        details = session.scalars(
            select(AuditEvent.details).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.withdrawn_expired",
            )
        ).one()
    assert details["reason"] == (
        "The board stated this job stays listed until 2026-11-20T12:00:00+00:00."
    )

    again = staging.withdraw_expired(
        workspace_id, source_id, now=_HIMALAYAS_EXPIRY + timedelta(days=1)
    )

    assert again == []
    assert _actions(durable_database, pending_id) == ["pending_discovery.withdrawn_expired"]


def _closed_causes(
    database: Database, tmp_path: Path, workspace_id: str
) -> dict[str, PendingWithdrawalCause | None]:
    service = SourceScanService(
        database,
        storage=runtime_storage(database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    return {
        item.id: item.withdrawal_cause
        for item in service.list_pending_discoveries(workspace_id, closed_only=True)
    }


def test_a_row_withdrawn_past_its_stated_end_reports_the_listing_ended(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    url = "https://boards.example.test/jobs/ended-1"
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        source_id,
    )
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, source_id, [url])
    staging.withdraw_expired(workspace_id, source_id, now=_HIMALAYAS_EXPIRY)

    assert _closed_causes(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingWithdrawalCause.LISTING_ENDED
    }


def test_a_row_withdrawn_as_absent_keeps_that_cause_after_a_later_stated_end(
    durable_database: Database, tmp_path: Path
) -> None:
    one_page = WithdrawingRead(raw_count=12, read_count=12, complete=True, listing_pages=1)
    workspace_id, _source_id, pending_id = _paged_absence(durable_database, tmp_path, one_page)
    _state_listing_end(durable_database, pending_id, _HIMALAYAS_EXPIRY)

    assert _closed_causes(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingWithdrawalCause.ABSENT
    }


def test_a_relisted_then_rewithdrawn_row_reports_its_latest_cause(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    url = "https://boards.example.test/jobs/relisted-1"
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        source_id,
    )
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, source_id, [url])
    past = _HIMALAYAS_EXPIRY + timedelta(days=1)
    _staging_clock(monkeypatch, past)
    staging.withdraw_expired(workspace_id, source_id, now=past)
    extended = _himalayas_listing(url, _HIMALAYAS_EXPIRY + timedelta(days=30))
    _stage(durable_database, tmp_path, workspace_id, [extended], source_id)
    assert _source_row(durable_database, pending_id)[2] is False

    one_page = WithdrawingRead(raw_count=12, read_count=12, complete=True, listing_pages=1)
    _withdraw_missing(durable_database, tmp_path, workspace_id, source_id, one_page)

    assert _closed_causes(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingWithdrawalCause.ABSENT
    }


def test_a_withdrawn_row_with_no_withdrawal_event_reports_an_unrecorded_cause(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    pending_id = _pending(
        durable_database, workspace_id, url=_PAGED_ROLE_URL, first_seen_on=date(2026, 8, 24)
    )
    with durable_database.session() as session:
        session.get_one(PendingDiscovery, pending_id).withdrawn_at = utc_now()

    assert _closed_causes(durable_database, tmp_path, workspace_id) == {
        pending_id: PendingWithdrawalCause.UNRECORDED
    }


def test_another_source_relisting_the_job_drops_the_listing_end_it_never_stated(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    himalayas = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/77"
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        himalayas,
    )
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, himalayas, [url])
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse)
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY + timedelta(days=1))

    for _sweep in range(2):
        assert (
            staging.withdraw_expired(
                workspace_id, greenhouse, now=_HIMALAYAS_EXPIRY + timedelta(days=1)
            )
            == []
        )
        _stage(
            durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse
        )

    assert _source_row(durable_database, pending_id) == (url, greenhouse, False)
    assert _actions(durable_database, pending_id) == []


def test_a_board_serving_a_job_past_its_stated_end_does_not_relist_it(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    url = "https://boards.example.test/jobs/served-late-1"
    listing = _himalayas_listing(url, _HIMALAYAS_EXPIRY)
    _stage(durable_database, tmp_path, workspace_id, [listing], source_id)
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, source_id, [url])
    past = _HIMALAYAS_EXPIRY + timedelta(days=1)
    _staging_clock(monkeypatch, past)

    for _sweep in range(2):
        staging.withdraw_expired(workspace_id, source_id, now=past)
        _stage(durable_database, tmp_path, workspace_id, [listing], source_id)

    assert _source_row(durable_database, pending_id)[2] is True
    assert _actions(durable_database, pending_id) == ["pending_discovery.withdrawn_expired"]

    extended = _himalayas_listing(url, _HIMALAYAS_EXPIRY + timedelta(days=30))
    _stage(durable_database, tmp_path, workspace_id, [extended], source_id)

    assert _source_row(durable_database, pending_id)[2] is False
    assert staging.withdraw_expired(workspace_id, source_id, now=past) == []


def test_an_expired_listing_elsewhere_leaves_an_employer_board_row_alone(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    past = _HIMALAYAS_EXPIRY + timedelta(days=1)
    _staging_clock(monkeypatch, past)
    workspace_id = _workspace(durable_database, tmp_path)
    himalayas = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/88"
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse)
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, greenhouse, [url])

    for _sweep in range(3):
        _stage(
            durable_database,
            tmp_path,
            workspace_id,
            [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
            himalayas,
        )
        staging.withdraw_expired(workspace_id, himalayas, now=past)
        _stage(
            durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse
        )
        staging.withdraw_expired(workspace_id, greenhouse, now=past)

    assert _actions(durable_database, pending_id) == []
    assert _source_row(durable_database, pending_id) == (url, greenhouse, False)


def test_a_row_himalayas_expired_settles_under_the_employer_board_that_relists_it(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    himalayas = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/99"
    served = _himalayas_listing(url, _HIMALAYAS_EXPIRY)
    _stage(durable_database, tmp_path, workspace_id, [served], himalayas)
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, himalayas, [url])
    past = _HIMALAYAS_EXPIRY + timedelta(days=1)
    _staging_clock(monkeypatch, past)
    staging.withdraw_expired(workspace_id, himalayas, now=past)

    for _sweep in range(3):
        _stage(
            durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse
        )
        staging.withdraw_expired(workspace_id, greenhouse, now=past)
        _stage(durable_database, tmp_path, workspace_id, [served], himalayas)
        staging.withdraw_expired(workspace_id, himalayas, now=past)

    assert _actions(durable_database, pending_id) == [
        "pending_discovery.withdrawn_expired",
        "pending_discovery.relisted",
    ]
    assert _source_row(durable_database, pending_id) == (url, greenhouse, False)


@pytest.mark.parametrize("held_by", ["keep", "dismissed"])
def test_a_kept_or_dismissed_row_is_withdrawn_once_its_stated_end_passes(
    durable_database: Database, tmp_path: Path, held_by: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    pending_id = _pending(
        durable_database,
        workspace_id,
        url="https://boards.example.test/jobs/held-1",
        first_seen_on=date(2026, 11, 1),
        source_id=source_id,
    )
    _state_listing_end(durable_database, pending_id, _HIMALAYAS_EXPIRY)
    if held_by == "dismissed":
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    else:
        with durable_database.session() as session:
            set_pending_markers(
                session, workspace_id, pending_id, favorite=False, kept=True, keep_note=None
            )

    withdrawn = _staging(durable_database, tmp_path).withdraw_expired(
        workspace_id, source_id, now=_HIMALAYAS_EXPIRY
    )

    assert withdrawn == ["Operations Lead"]
    assert _source_row(durable_database, pending_id)[2] is True
    with durable_database.session() as session:
        dismissed = set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))
    assert (pending_id in dismissed) is (held_by == "dismissed")


def test_an_expired_head_releases_its_live_copies(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    head_id = _pending(
        durable_database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 11, 1),
        source_id=source_id,
        requisition_id="4102",
    )
    _pending(
        durable_database,
        workspace_id,
        url=_FRAMED_URL,
        employer="Globex",
        first_seen_on=date(2026, 11, 2),
        source_id=source_id,
        requisition_id="4102",
        variant_of=head_id,
    )
    _state_listing_end(durable_database, head_id, _HIMALAYAS_EXPIRY)

    _staging(durable_database, tmp_path).withdraw_expired(
        workspace_id, source_id, now=_HIMALAYAS_EXPIRY
    )

    assert _variant_of_by_url(durable_database, workspace_id)[_FRAMED_URL] is None


def test_a_listing_already_past_its_stated_expiry_is_never_staged(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    expired = "https://boards.example.test/jobs/expired-1"
    current = "https://boards.example.test/jobs/current-1"

    staged = _staging(durable_database, tmp_path).stage_listings(
        workspace_id,
        [
            _himalayas_listing(expired, utc_now() - timedelta(hours=1)),
            _himalayas_listing(current, utc_now() + timedelta(days=30)),
        ],
        utc_now().date(),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )

    assert [item.url for item in staged.inserted] == [current]
    assert [row[1] for row in _rows_as_read(durable_database, workspace_id)] == [current]


def test_a_row_with_no_stated_expiry_or_only_a_closing_date_stays_queued(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    source_id = _himalayas_source(durable_database, workspace_id)
    unstated = "https://boards.example.test/jobs/unstated-1"
    closing = _board_listing("Globex", "https://boards.example.test/jobs/closing-1")
    closing = replace(
        closing,
        facts=replace(
            closing.facts,
            closing_date=publisher_closing_date("", valid_through="2026-11-01"),
        ),
    )
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(unstated, None), closing],
        source_id,
    )

    withdrawn = _staging(durable_database, tmp_path).withdraw_expired(
        workspace_id, source_id, now=_HIMALAYAS_EXPIRY + timedelta(days=365)
    )

    assert withdrawn == []
    assert {row[1] for row in _rows_as_read(durable_database, workspace_id)} == {
        unstated,
        closing.url,
    }
    with durable_database.session() as session:
        assert (
            session.scalars(
                select(PendingDiscovery.id).where(PendingDiscovery.withdrawn_at.is_not(None))
            ).all()
            == []
        )


def test_a_one_page_read_withdraws_a_missed_row_without_a_check(
    durable_database: Database, tmp_path: Path
) -> None:
    one_page = WithdrawingRead(raw_count=12, read_count=12, complete=True, listing_pages=1)

    _workspace_id, _source_id, pending_id = _paged_absence(durable_database, tmp_path, one_page)

    assert _withdrawn_at(durable_database, pending_id) is not None
    assert _queued_checks(durable_database) == []


def test_a_one_page_jibe_read_ending_on_an_empty_fetch_withdraws_its_first_miss(
    durable_database: Database, tmp_path: Path
) -> None:
    requested: list[int] = []

    class OnePageJibe(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            page = int(url.split("page=")[1].split("&", maxsplit=1)[0])
            requested.append(page)
            jobs = [
                {
                    "data": {
                        "title": f"Role {index}",
                        "req_id": f"REQ-{index}",
                        "apply_url": f"https://jobs.acme.test/jobs/{index}/apply",
                    }
                }
                for index in (range(20) if page == 1 else ())
            ]
            return {"jobs": jobs}

    markup = (
        "https://jobs.acme.test/careers\n<html><head>"
        '<link rel="stylesheet" href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'
        "</head><body><p>Openings</p></body></html>"
    )
    read = OnePageJibe(markup).listings("Acme", "https://www.acme.test/careers")
    assert read is not None
    assert read.complete is True
    assert requested == [1, 2]

    _workspace_id, _source_id, pending_id = _paged_absence(
        durable_database,
        tmp_path,
        WithdrawingRead(read.raw_count, len(read.listings), read.complete, read.listing_pages),
    )

    assert _withdrawn_at(durable_database, pending_id) is not None
    assert _queued_checks(durable_database) == []


def test_a_page_numbered_jibe_read_spanning_two_pages_holds_its_first_miss(
    durable_database: Database, tmp_path: Path
) -> None:
    class TwoPageJibe(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            page = int(url.split("page=")[1].split("&", maxsplit=1)[0])
            jobs = [
                {
                    "data": {
                        "title": f"Role {index}",
                        "req_id": f"REQ-{index}",
                        "apply_url": f"https://jobs.acme.test/jobs/{index}/apply",
                    }
                }
                for index in range((page - 1) * 20, min(page * 20, 22))
            ]
            return {"totalCount": 22, "jobs": jobs}

    markup = (
        "https://jobs.acme.test/careers\n<html><head>"
        '<link rel="stylesheet" href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'
        "</head><body><p>Openings</p></body></html>"
    )
    read = TwoPageJibe(markup).listings("Acme", "https://www.acme.test/careers")
    assert read is not None
    assert read.complete is True

    _workspace_id, _source_id, pending_id = _paged_absence(
        durable_database,
        tmp_path,
        WithdrawingRead(read.raw_count, len(read.listings), read.complete, read.listing_pages),
    )

    assert _withdrawn_at(durable_database, pending_id) is None
    assert _queued_checks(durable_database) == [[pending_id]]


_LOGIN_URL = "https://careers-globex.example.test/jobs/4102/login"
_FRAMED_URL = "https://careers-globex.example.test/jobs/4102/job?in_iframe=1"
_MOBILE_URL = "https://m.careers-globex.example.test/jobs/4102/job"


@pytest.mark.parametrize(
    ("copies", "dismissed", "families", "visible"),
    [
        ([_FRAMED_URL], False, {_FRAMED_URL: None}, [_FRAMED_URL]),
        (
            [_FRAMED_URL, _MOBILE_URL],
            False,
            {_FRAMED_URL: None, _MOBILE_URL: _FRAMED_URL},
            [_FRAMED_URL],
        ),
        ([_FRAMED_URL], True, {_FRAMED_URL: _LOGIN_URL}, []),
    ],
    ids=["one_copy", "two_copies", "dismissed_head"],
)
def test_a_head_a_complete_read_withdraws_releases_its_live_copies(
    durable_database: Database,
    tmp_path: Path,
    copies: list[str],
    dismissed: bool,
    families: dict[str, str | None],
    visible: list[str],
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    head_id = _pending(
        durable_database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="4102",
    )
    for day, url in enumerate(copies, start=21):
        _pending(
            durable_database,
            workspace_id,
            url=url,
            employer="Globex",
            first_seen_on=date(2026, 8, day),
            source_id=board,
            requisition_id="4102",
            variant_of=head_id,
        )
    if dismissed:
        dismiss_pending_discovery(durable_database, workspace_id, head_id)

    _staging(durable_database, tmp_path).withdraw_absent(
        workspace_id, board, copies, date(2026, 8, 26)
    )

    variant_of = _variant_of_by_url(durable_database, workspace_id)
    assert {url: variant_of[url] for url in copies} == families
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert [
        item.discovery_url for item in service.list_pending_discoveries(workspace_id)
    ] == visible
    with durable_database.session() as session:
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.regional_variant_released"
            )
        ).all()
        assert sorted(event.details["reason"] for event in released) == [
            "its head was withdrawn"
        ] * (0 if dismissed else len(copies))


@pytest.mark.parametrize("head_relisted", [False, True])
def test_a_new_copy_folds_under_a_withdrawn_head_only_when_the_same_read_relists_it(
    durable_database: Database, tmp_path: Path, head_relisted: bool
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    head_id = _pending(
        durable_database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="4102",
    )
    with durable_database.session() as session:
        head = session.get(PendingDiscovery, head_id)
        assert head is not None
        head.withdrawn_at = utc_now()
    listings = [_board_listing("Globex", _FRAMED_URL, "4102")]
    if head_relisted:
        listings.append(_board_listing("Globex", _LOGIN_URL, "4102"))

    _stage(durable_database, tmp_path, workspace_id, listings, board)

    assert _variant_of_by_url(durable_database, workspace_id)[_FRAMED_URL] == (
        _LOGIN_URL if head_relisted else None
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert [item.discovery_url for item in service.list_pending_discoveries(workspace_id)] == [
        _LOGIN_URL if head_relisted else _FRAMED_URL
    ]


def _login_family(database: Database, tmp_path: Path) -> tuple[str, str]:
    """Stage a login-page head with a live framed copy folded under it."""
    workspace_id = _workspace(database, tmp_path)
    board = employer_board(database, workspace_id, "Globex")
    head_id = _pending(
        database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="4102",
    )
    _pending(
        database,
        workspace_id,
        url=_FRAMED_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 21),
        source_id=board,
        requisition_id="4102",
        variant_of=head_id,
    )
    return workspace_id, head_id


def _queued_urls(database: Database, tmp_path: Path, workspace_id: str) -> list[str]:
    service = SourceScanService(
        database,
        storage=runtime_storage(database, tmp_path),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    return [item.discovery_url for item in service.list_pending_discoveries(workspace_id)]


def _assert_the_copy_is_queued(
    database: Database, tmp_path: Path, workspace_id: str, head_id: str
) -> None:
    assert _queued_urls(database, tmp_path, workspace_id) == [_FRAMED_URL]
    with database.session() as session:
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.regional_variant_released"
            )
        ).all()
        assert [
            (event.details["discovery_url"], event.details["released_from_head"])
            for event in released
        ] == [(_FRAMED_URL, head_id)]


def test_withdrawing_a_head_a_publisher_check_found_closed_queues_its_live_copy(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head_id = _login_family(durable_database, tmp_path)

    _staging(durable_database, tmp_path).checks.withdraw_pending_discovery(
        workspace_id,
        head_id,
        note="The publisher check found this posting gone.",
        actor="test",
        origin="test",
        correlation_id=None,
    )

    _assert_the_copy_is_queued(durable_database, tmp_path, workspace_id, head_id)


def _withdraw_on_refresh(
    database: Database, tmp_path: Path, workspace_id: str, pending_id: str
) -> None:
    _staging(database, tmp_path).checks.withdraw_pending_discovery(
        workspace_id,
        pending_id,
        note="The publisher check found this posting gone.",
        actor="test",
        origin="test",
        correlation_id=None,
    )


_CLOSING_URL = "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"


def _enqueue_evaluation(database: Database, workspace_id: str, pending_id: str) -> None:
    from clearscout.application.tasks import EvaluationWorkflowPayload

    TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate",
        payload=EvaluationWorkflowPayload(pending_discovery_id=pending_id).model_dump(),
        idempotency_key=f"evaluate-{pending_id}",
    )


@pytest.mark.parametrize("withdrawn", [False, True])
def test_withdrawing_an_archived_row_on_refresh_is_refused(
    durable_database: Database, tmp_path: Path, withdrawn: bool
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = _pending(
        durable_database, workspace_id, url=_CLOSING_URL, first_seen_on=date(2026, 8, 24)
    )
    mark_reset_archived(durable_database, workspace_id, pending_id, withdrawn=withdrawn)

    with pytest.raises(ConflictError, match="Only an active pending discovery"):
        _withdraw_on_refresh(durable_database, tmp_path, workspace_id, pending_id)


def test_withdrawing_a_row_with_an_evaluation_in_flight_is_refused(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = _pending(
        durable_database, workspace_id, url=_CLOSING_URL, first_seen_on=date(2026, 8, 24)
    )
    _enqueue_evaluation(durable_database, workspace_id, pending_id)

    with pytest.raises(ConflictError, match="in flight"):
        _withdraw_on_refresh(durable_database, tmp_path, workspace_id, pending_id)
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.withdrawn_at is None


@pytest.mark.parametrize("in_flight", [False, True])
def test_withdrawing_an_already_withdrawn_row_on_refresh_leaves_it_as_it_was(
    durable_database: Database, tmp_path: Path, in_flight: bool
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = _pending(
        durable_database, workspace_id, url=_CLOSING_URL, first_seen_on=date(2026, 8, 24)
    )
    _withdraw_on_refresh(durable_database, tmp_path, workspace_id, pending_id)
    with durable_database.session() as session:
        first = session.get(PendingDiscovery, pending_id)
        assert first is not None
        withdrawn_at = first.withdrawn_at
    if in_flight:
        _enqueue_evaluation(durable_database, workspace_id, pending_id)

    _withdraw_on_refresh(durable_database, tmp_path, workspace_id, pending_id)

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.withdrawn_at == withdrawn_at
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.withdrawn_on_refresh",
            )
        ).all()
    assert len(events) == 1


def test_applying_a_saved_closure_to_a_head_queues_its_live_copy(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head_id = _login_family(durable_database, tmp_path)
    with durable_database.session() as session:
        head = session.get(PendingDiscovery, head_id)
        assert head is not None
        closure = pending_posting_content(
            workspace_id, head_id, _LOGIN_URL, posting_status=PostingStatus.CLOSED
        )
        session.add(closure)
        session.flush()
        head.current_content_id = closure.id

    _staging(durable_database, tmp_path).checks.reconcile_saved_closures(
        workspace_id, [head_id], actor="test", origin="test", correlation_id=None
    )

    _assert_the_copy_is_queued(durable_database, tmp_path, workspace_id, head_id)


def test_a_released_family_is_headed_by_a_readable_copy_before_an_earlier_unreadable_one(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head_id = _login_family(durable_database, tmp_path)
    mobile_id = _pending(
        durable_database,
        workspace_id,
        url=_MOBILE_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 22),
        requisition_id="4102",
        variant_of=head_id,
    )
    with durable_database.session() as session:
        mobile = session.get(PendingDiscovery, mobile_id)
        assert mobile is not None
        mobile.content_status = PostingContentAccessStatus.READABLE

    _staging(durable_database, tmp_path).checks.withdraw_pending_discovery(
        workspace_id,
        head_id,
        note="The publisher check found this posting gone.",
        actor="test",
        origin="test",
        correlation_id=None,
    )

    variant_of = _variant_of_by_url(durable_database, workspace_id)
    assert (variant_of[_MOBILE_URL], variant_of[_FRAMED_URL]) == (None, _MOBILE_URL)


@pytest.mark.parametrize("head_first", [True, False])
def test_a_released_family_folds_whole_under_its_relisted_head(
    durable_database: Database, tmp_path: Path, head_first: bool
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    head_id = _pending(
        durable_database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="4102",
    )
    for day, url in ((21, _FRAMED_URL), (22, _MOBILE_URL)):
        _pending(
            durable_database,
            workspace_id,
            url=url,
            employer="Globex",
            first_seen_on=date(2026, 8, day),
            source_id=board,
            requisition_id="4102",
            variant_of=head_id,
        )
    staging = _staging(durable_database, tmp_path)
    staging.withdraw_absent(workspace_id, board, [_FRAMED_URL, _MOBILE_URL], date(2026, 8, 26))
    assert _variant_of_by_url(durable_database, workspace_id)[_MOBILE_URL] == _FRAMED_URL
    urls = (
        [_LOGIN_URL, _FRAMED_URL, _MOBILE_URL]
        if head_first
        else [_FRAMED_URL, _MOBILE_URL, _LOGIN_URL]
    )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", url, "4102") for url in urls],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id) == {
        _LOGIN_URL: None,
        _FRAMED_URL: _LOGIN_URL,
        _MOBILE_URL: _LOGIN_URL,
    }
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_LOGIN_URL]
    with durable_database.session() as session:
        moved = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.regional_variant_moved"
            )
        ).one()
        assert (moved.details["discovery_url"], moved.details["to_head"]) == (_MOBILE_URL, head_id)


def _dismissed_then_restored_by_person(database: Database, workspace_id: str, row_id: str) -> None:
    """Leave a row whose latest disposition is the person's own restore."""
    dismiss_pending_discovery(database, workspace_id, row_id)
    service = DiscoveryDispositionService(database)
    restore = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": "human_review",
            "reason": "Returned to review",
            "pending_discovery_ids": [row_id],
        }
    )
    preview = service.preview(workspace_id, restore)
    service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**restore.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=None,
    )


def _link(database: Database, row_id: str, head_id: str) -> None:
    with database.session() as session:
        row = session.get(PendingDiscovery, row_id)
        assert row is not None
        row.regional_variant_of = head_id


def test_a_released_family_is_headed_by_a_copy_the_person_restored_over_a_dismissed_one(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, head_id = _login_family(durable_database, tmp_path)
    with durable_database.session() as session:
        framed_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == _FRAMED_URL)
        ).one()
    dismiss_pending_discovery(durable_database, workspace_id, framed_id)
    mobile_id = _pending(
        durable_database,
        workspace_id,
        url=_MOBILE_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 22),
        requisition_id="4102",
    )
    _dismissed_then_restored_by_person(durable_database, workspace_id, mobile_id)
    _link(durable_database, mobile_id, head_id)
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_LOGIN_URL]

    _staging(durable_database, tmp_path).checks.withdraw_pending_discovery(
        workspace_id,
        head_id,
        note="The publisher check found this posting gone.",
        actor="test",
        origin="test",
        correlation_id=None,
    )

    variant_of = _variant_of_by_url(durable_database, workspace_id)
    assert (variant_of[_MOBILE_URL], variant_of[_FRAMED_URL]) == (None, _MOBILE_URL)
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_MOBILE_URL]


@pytest.mark.parametrize("owned_by", ["restore", "adding it"])
def test_a_copy_the_person_owns_stays_out_when_its_head_folds_under_a_dismissed_head(
    durable_database: Database, tmp_path: Path, owned_by: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    dismissed_id, parent_id, child_id = (
        _pending(
            durable_database,
            workspace_id,
            url=url,
            employer="Globex",
            first_seen_on=date(2026, 8, day),
            source_id=board if url != _MOBILE_URL or owned_by == "restore" else None,
            requisition_id="4102",
        )
        for day, url in ((20, _LOGIN_URL), (21, _FRAMED_URL), (22, _MOBILE_URL))
    )
    if owned_by == "restore":
        _dismissed_then_restored_by_person(durable_database, workspace_id, child_id)
    else:
        with durable_database.session() as session:
            session.get_one(PendingDiscovery, child_id).reason = FOUND_POSTING_REASON
    dismiss_pending_discovery(durable_database, workspace_id, dismissed_id)
    _link(durable_database, child_id, parent_id)
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_FRAMED_URL]

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _FRAMED_URL, "4102")],
        board,
    )

    variant_of = _variant_of_by_url(durable_database, workspace_id)
    assert (variant_of[_FRAMED_URL], variant_of[_MOBILE_URL]) == (_LOGIN_URL, None)
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_MOBILE_URL]
    with durable_database.session() as session:
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "pending_discovery.regional_variant_released"
            )
        ).one()
        assert (released.entity_id, released.details["released_from_head"]) == (
            child_id,
            parent_id,
        )


@pytest.mark.parametrize("held_by", ["nothing", "restore", "favorite", "keep"])
def test_a_row_the_person_restored_or_marked_never_folds_under_a_dismissed_head(
    durable_database: Database, tmp_path: Path, held_by: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    head_id = _pending(
        durable_database,
        workspace_id,
        url=_LOGIN_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="4102",
    )
    dismiss_pending_discovery(durable_database, workspace_id, head_id)
    row_id = _pending(
        durable_database,
        workspace_id,
        url=_FRAMED_URL,
        employer="Globex",
        first_seen_on=date(2026, 8, 21),
        source_id=board,
        requisition_id="4102",
    )
    if held_by == "restore":
        _dismissed_then_restored_by_person(durable_database, workspace_id, row_id)
    elif held_by != "nothing":
        with durable_database.session() as session:
            set_pending_markers(
                session,
                workspace_id,
                row_id,
                favorite=held_by == "favorite",
                kept=held_by == "keep",
                keep_note=None,
            )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex", _FRAMED_URL, "4102")],
        board,
    )

    assert _variant_of_by_url(durable_database, workspace_id)[_FRAMED_URL] == (
        _LOGIN_URL if held_by == "nothing" else None
    )


_TRACKED_WORKDAY = (
    "https://initech.wd1.myworkdayjobs.com/en-US/Careers/job/Springfield/Data-Engineer_R-1001"
)
_TRACKED_WORKDAY_CASE_VARIANT = (
    "https://initech.wd1.myworkdayjobs.com/en-us/careers/job/springfield/data-engineer_R-1001"
)


def _track_job(database: Database, workspace_id: str, url: str) -> None:
    with database.session() as session:
        employer = Employer(workspace_id=workspace_id, name="Initech", normalized_name="initech")
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Engineer",
            normalized_title="data engineer",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="discovery",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
            )
        )


def test_a_case_variant_of_a_tracked_workday_posting_is_not_staged_again(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    _track_job(durable_database, workspace_id, _TRACKED_WORKDAY)
    variant = SourceListing(
        employer="Initech",
        title="Data Engineer",
        url=_TRACKED_WORKDAY_CASE_VARIANT,
        location="Springfield",
    )

    found = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).stage_found_posting(workspace_id, _TRACKED_WORKDAY_CASE_VARIANT, utc_now().date())
    unstaged = _staging(durable_database, tmp_path).unstaged_listings(workspace_id, [variant])

    assert (found, unstaged) == (None, [])


_TWIN_UUID = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"
_OTHER_UUID = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"


def _careers_wrapper(job: str, host: str = "careers.globex.example.test") -> str:
    return f"https://{host}/job/{job}/operations-lead/"


@pytest.mark.parametrize("ats_employer", ["Globex Systems", "Globex (Lever)", "Globex"])
def test_an_ats_listing_of_a_dismissed_careers_wrapper_uuid_stays_out_of_the_queue(
    durable_database: Database, tmp_path: Path, ats_employer: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    wrapper_id = _pending(
        durable_database,
        workspace_id,
        url=_careers_wrapper(_TWIN_UUID),
        employer="Globex Systems",
        first_seen_on=date(2026, 8, 20),
        requisition_id=_TWIN_UUID,
    )
    dismiss_pending_discovery(durable_database, workspace_id, wrapper_id)
    ats_url = f"https://jobs.lever.co/globex/{_TWIN_UUID}"

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing(ats_employer, ats_url, _TWIN_UUID)],
        board,
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == []
    with durable_database.session() as session:
        assert wrapper_id in set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))


def _dismissed_wrapper(
    database: Database, workspace_id: str, url: str, requisition: str, *, withdrawn: bool
) -> str:
    wrapper_id = _pending(
        database,
        workspace_id,
        url=url,
        employer="Globex Systems",
        first_seen_on=date(2026, 8, 24),
        requisition_id=requisition,
    )
    dismiss_pending_discovery(database, workspace_id, wrapper_id)
    if withdrawn:
        with database.session() as session:
            session.get_one(PendingDiscovery, wrapper_id).withdrawn_at = utc_now()
    return wrapper_id


def _head_of(database: Database, pending_id: str) -> str | None:
    with database.session() as session:
        return session.get_one(PendingDiscovery, pending_id).regional_variant_of


@pytest.mark.parametrize("withdrawn", [False, True], ids=["dismissed", "dismissed-and-withdrawn"])
@pytest.mark.parametrize("wrapper_requisition", [_TWIN_UUID, ""], ids=["stated", "path-only"])
def test_a_revived_ats_row_folds_under_its_later_dismissed_careers_wrapper_twin(
    durable_database: Database, tmp_path: Path, withdrawn: bool, wrapper_requisition: str
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    ats_url = f"https://jobs.lever.co/globex/{_TWIN_UUID}"
    ats_id = _pending(
        durable_database,
        workspace_id,
        url=ats_url,
        employer="Globex (Lever)",
        first_seen_on=date(2026, 8, 21),
        source_id=board,
        requisition_id=_TWIN_UUID,
    )
    mark_reset_archived(durable_database, workspace_id, ats_id)
    wrapper_id = _dismissed_wrapper(
        durable_database,
        workspace_id,
        _careers_wrapper(_TWIN_UUID),
        wrapper_requisition,
        withdrawn=withdrawn,
    )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex (Lever)", ats_url, _TWIN_UUID)],
        board,
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == []
    assert _head_of(durable_database, ats_id) == wrapper_id


@pytest.mark.parametrize(
    ("wrapper_url", "listing_employer", "listing_url", "requisition", "folds"),
    [
        (
            _careers_wrapper(_TWIN_UUID),
            "Globex (Lever)",
            f"https://jobs.lever.co/globex/{_TWIN_UUID}",
            _TWIN_UUID,
            True,
        ),
        (
            _careers_wrapper(_TWIN_UUID),
            "Globex",
            f"https://jobs.lever.co/globex/{_OTHER_UUID}",
            _OTHER_UUID,
            False,
        ),
        (
            _careers_wrapper("4102"),
            "Initech",
            _careers_wrapper("4102", "careers.initech.example.test"),
            "4102",
            False,
        ),
    ],
    ids=["same-uuid", "other-uuid", "short-id-other-host"],
)
@pytest.mark.parametrize("withdrawn", [False, True], ids=["dismissed", "dismissed-and-withdrawn"])
@pytest.mark.parametrize("path_only", [False, True], ids=["stated", "path-only"])
def test_a_new_ats_listing_folds_under_a_withdrawn_or_dismissed_wrapper_only_on_its_uuid(
    durable_database: Database,
    tmp_path: Path,
    withdrawn: bool,
    path_only: bool,
    wrapper_url: str,
    listing_employer: str,
    listing_url: str,
    requisition: str,
    folds: bool,
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, listing_employer)
    stated = "" if path_only else requisition if requisition == "4102" else _TWIN_UUID
    wrapper_id = _dismissed_wrapper(
        durable_database, workspace_id, wrapper_url, stated, withdrawn=withdrawn
    )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing(listing_employer, listing_url, requisition)],
        board,
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == (
        [] if folds else [listing_url]
    )
    assert _variant_of_by_url(durable_database, workspace_id)[listing_url] == (
        wrapper_url if folds else None
    )
    with durable_database.session() as session:
        assert wrapper_id in set(session.scalars(dismissed_pending_discovery_ids(workspace_id)))


def _track_wrapper(
    database: Database, workspace_id: str, url: str, employer: str, *, archived: bool = False
) -> None:
    with database.session() as session:
        owner = Employer(
            workspace_id=workspace_id, name=employer, normalized_name=employer.casefold()
        )
        session.add(owner)
        session.flush()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=owner.id,
            title="Operations Lead",
            normalized_title="operations lead",
            first_seen_at=utc_now(),
            archived_at=utc_now() if archived else None,
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="discovery",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
            )
        )


@pytest.mark.parametrize(
    ("tracked_url", "listing_employer", "listing_url", "requisition", "staged"),
    [
        (
            _careers_wrapper(_TWIN_UUID),
            "Globex (Lever)",
            f"https://jobs.lever.co/globex/{_TWIN_UUID}",
            _TWIN_UUID,
            False,
        ),
        (
            _careers_wrapper(_TWIN_UUID),
            "Globex",
            f"https://jobs.lever.co/globex/{_TWIN_UUID}",
            "",
            False,
        ),
        (
            _careers_wrapper(_TWIN_UUID),
            "Globex",
            f"https://jobs.lever.co/globex/{_OTHER_UUID}",
            _OTHER_UUID,
            True,
        ),
        (
            _careers_wrapper("4102"),
            "Initech",
            _careers_wrapper("4102", "careers.initech.example.test"),
            "4102",
            True,
        ),
    ],
    ids=["same-uuid", "same-uuid-path-only", "other-uuid", "short-id-other-host"],
)
def test_an_ats_listing_of_a_tracked_careers_wrapper_uuid_is_not_staged(
    durable_database: Database,
    tmp_path: Path,
    tracked_url: str,
    listing_employer: str,
    listing_url: str,
    requisition: str,
    staged: bool,
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _track_wrapper(durable_database, workspace_id, tracked_url, "Globex Systems")
    listing = _board_listing(listing_employer, listing_url, requisition)

    staging = _staging(durable_database, tmp_path)
    unstaged = staging.unstaged_listings(workspace_id, [listing])
    refreshes: list[tuple[str, SourceListing]] = []
    with durable_database.session() as session:
        staging.stage_listings_in_session(
            workspace_id,
            [listing],
            date(2026, 8, 26),
            reason="Board scan matched the title filter.",
            session=session,
            refreshes=refreshes,
        )

    assert (unstaged, _queued_urls(durable_database, tmp_path, workspace_id)) == (
        ([listing], [listing_url]) if staged else ([], [])
    )
    assert [refreshed for _job, refreshed in refreshes] == ([] if staged else [listing])


@pytest.mark.parametrize(
    ("listing_url", "requisition", "staged"),
    [
        (f"https://jobs.lever.co/globex/{_TWIN_UUID}", _TWIN_UUID, False),
        (f"https://jobs.lever.co/globex/{_TWIN_UUID}", "", False),
        (f"https://jobs.lever.co/globex/{_OTHER_UUID}", _OTHER_UUID, True),
    ],
    ids=["same-uuid", "same-uuid-path-only", "other-uuid"],
)
def test_an_ats_listing_of_a_closed_tracked_job_uuid_stays_out_of_the_queue_unrefreshed(
    durable_database: Database,
    tmp_path: Path,
    listing_url: str,
    requisition: str,
    staged: bool,
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    tracked_url = _careers_wrapper(_TWIN_UUID)
    _track_wrapper(durable_database, workspace_id, tracked_url, "Globex Systems", archived=True)
    listing = _board_listing("Globex (Lever)", listing_url, requisition)

    staging = _staging(durable_database, tmp_path)
    unstaged = staging.unstaged_listings(workspace_id, [listing])
    refreshes: list[tuple[str, SourceListing]] = []
    with durable_database.session() as session:
        staging.stage_listings_in_session(
            workspace_id,
            [listing],
            date(2026, 8, 26),
            reason="Board scan matched the title filter.",
            session=session,
            refreshes=refreshes,
        )

    assert (unstaged, _queued_urls(durable_database, tmp_path, workspace_id)) == (
        ([listing], [listing_url]) if staged else ([], [])
    )
    assert refreshes == []
    assert sorted(_tracked_addresses(durable_database)) == sorted(
        [tracked_url] if staged else [tracked_url, listing_url]
    )


def test_a_relisting_of_an_open_tracked_job_keeps_its_primary_url(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    tracked_url = _careers_wrapper(_TWIN_UUID)
    relisting_url = f"https://jobs.lever.co/globex/{_TWIN_UUID}"
    _track_wrapper(durable_database, workspace_id, tracked_url, "Globex Systems")
    listing = _board_listing("Globex (Lever)", relisting_url, _TWIN_UUID)

    with durable_database.session() as session:
        _staging(durable_database, tmp_path).stage_listings_in_session(
            workspace_id,
            [listing],
            date(2026, 8, 26),
            reason="Board scan matched the title filter.",
            session=session,
            refreshes=[],
        )

    with durable_database.session() as session:
        rows = {
            row.url: (row.is_primary, row.url_type) for row in session.scalars(select(PostingUrl))
        }
    assert rows == {tracked_url: (True, "discovery"), relisting_url: (False, "discovery")}


def test_a_relisting_of_an_open_tracked_job_with_no_primary_url_becomes_it(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    tracked_url = _careers_wrapper(_TWIN_UUID)
    relisting_url = f"https://jobs.lever.co/globex/{_TWIN_UUID}"
    _track_wrapper(durable_database, workspace_id, tracked_url, "Globex Systems")
    with durable_database.session() as session:
        session.scalars(select(PostingUrl)).one().is_primary = False

    with durable_database.session() as session:
        _staging(durable_database, tmp_path).stage_listings_in_session(
            workspace_id,
            [_board_listing("Globex (Lever)", relisting_url, _TWIN_UUID)],
            date(2026, 8, 26),
            reason="Board scan matched the title filter.",
            session=session,
            refreshes=[],
        )

    with durable_database.session() as session:
        rows = {row.url: row.is_primary for row in session.scalars(select(PostingUrl))}
    assert rows == {tracked_url: False, relisting_url: True}


_UKG_BOARD = (
    "https://recruiting2.example-ukg.com/GLO1001/JobBoard/11111111-2222-4333-8444-555555555555"
)
_UKG_TRACKED = f"{_UKG_BOARD}/OpportunityDetail?opportunityId=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_UKG_OTHER = f"{_UKG_BOARD}/OpportunityDetail?opportunityId=3b5ffc18-aadb-486c-a211-83993625c798"


def _tracked_addresses(database: Database) -> list[str]:
    with database.session() as session:
        return list(session.scalars(select(PostingUrl.url)))


def test_a_second_opening_on_a_tracked_ukg_board_stages_by_hand(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _track_wrapper(durable_database, workspace_id, _UKG_TRACKED, "Globex")

    found = _staging(durable_database, tmp_path).stage_found_posting(
        workspace_id, _UKG_OTHER, date(2026, 8, 26)
    )

    assert found is not None
    assert _tracked_addresses(durable_database) == [_UKG_TRACKED]


def test_a_second_opening_on_a_tracked_ukg_board_stages_from_a_scan(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex Systems")
    _track_wrapper(durable_database, workspace_id, _UKG_TRACKED, "Globex")
    listing = _board_listing("Globex", _UKG_OTHER, "REQ-2002")
    staging = _staging(durable_database, tmp_path)

    unstaged = staging.unstaged_listings(workspace_id, [listing])
    refreshes: list[tuple[str, SourceListing]] = []
    with durable_database.session() as session:
        staging.stage_listings_in_session(
            workspace_id,
            [listing],
            date(2026, 8, 26),
            reason="Board scan matched the title filter.",
            source_id=board,
            session=session,
            refreshes=refreshes,
        )

    assert (unstaged, refreshes) == ([listing], [])
    assert _queued_urls(durable_database, tmp_path, workspace_id) == [_UKG_OTHER]
    assert _tracked_addresses(durable_database) == [_UKG_TRACKED]


def test_a_short_requisition_never_folds_under_a_later_dismissed_row_of_another_source(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = employer_board(durable_database, workspace_id, "Globex")
    careers = employer_board(durable_database, workspace_id, "Globex Careers")
    url = "https://boards.greenhouse.io/globex/jobs/4102"
    row_id = _pending(
        durable_database,
        workspace_id,
        url=url,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        source_id=board,
        requisition_id="R-100",
    )
    dismissed_id = _pending(
        durable_database,
        workspace_id,
        url="https://careers.globex.example.test/jobs/r-100",
        employer="Globex",
        first_seen_on=date(2026, 8, 24),
        source_id=careers,
        requisition_id="R-100",
    )
    dismiss_pending_discovery(durable_database, workspace_id, dismissed_id)

    _stage(
        durable_database, tmp_path, workspace_id, [_board_listing("Globex", url, "R-100")], board
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == [url]
    assert _head_of(durable_database, row_id) is None


def test_a_wrapper_listing_stating_no_requisition_folds_under_a_dismissed_ats_row_on_its_uuid(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    careers = employer_board(durable_database, workspace_id, "Globex Systems")
    ats_url = f"https://jobs.lever.co/globex/{_TWIN_UUID}"
    ats_id = _pending(
        durable_database,
        workspace_id,
        url=ats_url,
        employer="Globex",
        first_seen_on=date(2026, 8, 20),
        requisition_id=_TWIN_UUID,
    )
    dismiss_pending_discovery(durable_database, workspace_id, ats_id)
    wrapper_url = _careers_wrapper(_TWIN_UUID)

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_board_listing("Globex Systems", wrapper_url, "")],
        careers,
    )

    assert _queued_urls(durable_database, tmp_path, workspace_id) == []
    assert _variant_of_by_url(durable_database, workspace_id)[wrapper_url] == ats_url


def test_a_found_posting_loads_only_the_row_holding_its_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    board = "https://initech.wd1.myworkdayjobs.com/en-US/Careers/job/Springfield/"
    held = _pending(
        durable_database,
        workspace_id,
        url=f"{board}Data-Engineer_R-1001",
        first_seen_on=date(2026, 8, 20),
    )
    for number in range(2, 12):
        _pending(
            durable_database,
            workspace_id,
            url=f"{board}Analyst-{number}_R-10{number:02d}",
            first_seen_on=date(2026, 8, 20),
        )
    loaded: list[str] = []

    def _count(row: PendingDiscovery, _context: object) -> None:
        loaded.append(row.id)

    event.listen(PendingDiscovery, "load", _count)
    try:
        _staging(durable_database, tmp_path).stage_found_posting(
            workspace_id, f"{board.lower()}data-engineer_R-1001", date(2026, 8, 26)
        )
    finally:
        event.remove(PendingDiscovery, "load", _count)

    assert loaded == [held]


_LOCALE_BOARD = "https://careers.example.com/{}/careers/principal-data-engineer-500001/job/"


def _locale_listing(locale: str, title: str) -> SourceListing:
    return SourceListing(
        employer="Initech",
        title=title,
        url=_LOCALE_BOARD.format(locale),
        location="Remote",
        facts=published_listing_facts("greenhouse", locations="Remote"),
    )


def test_a_locale_variant_listing_keeps_the_english_row_names(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_locale_listing("en", "Principal Data Engineer")],
        None,
    )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_locale_listing("jp", "Principal Data Engineer (Japan)")],
        None,
    )

    with durable_database.session() as session:
        titles = session.scalars(
            select(PendingDiscovery.title).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
    assert titles == ["Principal Data Engineer"]


def test_a_locale_variant_matches_the_english_row_among_rows_sharing_its_requisition(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    for locale, title, first_seen_on in (
        ("jp", "Principal Data Engineer (Japan)", date(2026, 8, 18)),
        ("en", "Principal Data Engineer", date(2026, 8, 20)),
    ):
        _pending(
            durable_database,
            workspace_id,
            url=_LOCALE_BOARD.format(locale),
            title=title,
            first_seen_on=first_seen_on,
        )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_locale_listing("kr", "Principal Data Engineer (Korea)")],
        None,
    )

    with durable_database.session() as session:
        titles = session.scalars(
            select(PendingDiscovery.title).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
    assert sorted(titles) == ["Principal Data Engineer", "Principal Data Engineer (Japan)"]


def _closing_date(database: Database, pending_id: str) -> PublisherClosingDate | None:
    with database.session() as session:
        staged = session.get_one(PendingDiscovery, pending_id).staged_facts
    return StagedListingFacts.from_json(staged).closing_date


def test_a_live_aggregator_listing_leaves_an_employer_board_row_with_its_board(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    aggregator = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/101"
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse)
    (pending_id,) = _staging(durable_database, tmp_path).open_rows_at(
        workspace_id, greenhouse, [url]
    )

    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        aggregator,
    )

    assert _source_row(durable_database, pending_id) == (url, greenhouse, False)
    assert _closing_date(durable_database, pending_id) is None


def test_an_aggregator_redating_an_employer_board_row_never_withdraws_it(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace(durable_database, tmp_path)
    aggregator = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/102"
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse)
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, greenhouse, [url])

    for redating in range(2):
        expiry = _HIMALAYAS_EXPIRY + timedelta(days=30 * redating)
        _staging_clock(monkeypatch, expiry - timedelta(days=7))
        _stage(
            durable_database,
            tmp_path,
            workspace_id,
            [_himalayas_listing(url, expiry)],
            aggregator,
        )
        past = expiry + timedelta(days=1)
        _staging_clock(monkeypatch, past)
        staging.withdraw_expired(workspace_id, aggregator, now=past)
        _stage(
            durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse
        )
        staging.withdraw_expired(workspace_id, greenhouse, now=past)

    assert _actions(durable_database, pending_id) == []
    assert _source_row(durable_database, pending_id) == (url, greenhouse, False)


def test_an_employer_board_withdrawal_stands_while_an_aggregator_still_lists_the_row(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    aggregator = _himalayas_source(durable_database, workspace_id)
    greenhouse = board_source(
        durable_database, workspace_id, "Globex board", "https://boards.greenhouse.io/globex"
    )
    url = "https://boards.greenhouse.io/globex/jobs/103"
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], greenhouse)
    staging = _staging(durable_database, tmp_path)
    (pending_id,) = staging.open_rows_at(workspace_id, greenhouse, [url])

    _complete_read_yesterday(durable_database, greenhouse)
    for _cycle in range(2):
        staging.withdraw_absent(workspace_id, greenhouse, [], utc_now().date())
        _stage(
            durable_database,
            tmp_path,
            workspace_id,
            [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
            aggregator,
        )

    assert _source_row(durable_database, pending_id) == (url, greenhouse, True)
    assert "pending_discovery.relisted" not in _actions(durable_database, pending_id)


def test_a_row_with_no_source_follows_an_aggregator_listing_and_its_end(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    aggregator = _himalayas_source(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/globex/jobs/104"
    _stage(durable_database, tmp_path, workspace_id, [_board_listing("Globex", url)], None)
    with durable_database.session() as session:
        pending_id = session.scalars(
            select(PendingDiscovery.id).where(PendingDiscovery.discovery_url == url)
        ).one()
    listing = _himalayas_listing(url, _HIMALAYAS_EXPIRY)

    _stage(durable_database, tmp_path, workspace_id, [listing], aggregator)

    assert _source_row(durable_database, pending_id) == (url, aggregator, False)
    assert _closing_date(durable_database, pending_id) == listing.facts.closing_date


def test_an_aggregator_row_takes_a_later_aggregator_listing_and_its_end(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _staging_clock(monkeypatch, _HIMALAYAS_EXPIRY - timedelta(days=7))
    workspace_id = _workspace(durable_database, tmp_path)
    first = _himalayas_source(durable_database, workspace_id)
    second = _himalayas_source(durable_database, workspace_id, "Remote roles")
    url = "https://boards.example.test/jobs/aggregated-1"
    _stage(
        durable_database,
        tmp_path,
        workspace_id,
        [_himalayas_listing(url, _HIMALAYAS_EXPIRY)],
        first,
    )
    (pending_id,) = _staging(durable_database, tmp_path).open_rows_at(workspace_id, first, [url])
    extended = _himalayas_listing(url, _HIMALAYAS_EXPIRY + timedelta(days=30))

    _stage(durable_database, tmp_path, workspace_id, [extended], second)

    assert _source_row(durable_database, pending_id) == (url, second, False)
    assert _closing_date(durable_database, pending_id) == extended.facts.closing_date
