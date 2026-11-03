from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.bootstrap.api import create_app
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SourceReadOutcome,
    new_id,
    utc_now,
)
from clearscout.domain.identity import normalize_text
from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.domain.search_parameters import CountryNames, JobSearchParameters, Location
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    BackgroundTask,
    Employer,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PendingPostingContent,
    Source,
    SourceScan,
)
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import (
    evaluation_commit,
    pending_posting_content,
    seed_scan_continuation,
)


def test_user_can_attach_attested_text_to_a_policy_gated_pending_posting(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="manual-posting", name="Manual posting")
        .id
    )
    with database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="c" * 64,
            employer="Northwind Research",
            title="Medical Information Lead",
            discovery_url="https://publisher.example.test/jobs/medical-information-lead",
            content_status="policy_refused",
            reason="Public posting proposed by search.",
            first_seen_on=date(2026, 9, 11),
        )
        session.add(pending)
        session.flush()
        pending_id = pending.id
    posting_body = "Medical Information Lead\n" + (
        "Current responsibilities, qualifications, and role details. " * 10
    )
    posting_url = "https://publisher.example.test/jobs/medical-information-lead"
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            missing_attestation = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/content",
                json={
                    "content": posting_body,
                    "expected_current_content_id": None,
                    "expected_url": posting_url,
                },
            )
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/content",
                json={
                    "content": posting_body,
                    "attested_current_exact_posting": True,
                    "expected_current_content_id": None,
                    "expected_url": posting_url,
                },
            )
            page = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page")
        with database.session() as session:
            evidence = session.get(PendingPostingContent, response.json()["evidence_id"])
    finally:
        database.dispose()

    assert missing_attestation.status_code == 422
    assert response.status_code == 200
    assert response.json()["readiness"] == "ready"
    assert evidence is not None
    assert evidence.created_by == "local_user"
    assert evidence.extraction_method == "user_supplied_text"
    assert page.json()["ready"]["items"][0]["id"] == pending_id


class _UnusedResolver:
    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        raise AssertionError("Manual remediation must not read the publisher.")


def _pending_with_observation(
    database: Database, tmp_path: Path, slug: str, observation: ResolvedPostingContent
) -> tuple[str, str, str]:
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug=slug, name=slug).id
    )
    with database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="d" * 64,
            employer="Aster Systems",
            title="Clinical Data Lead",
            discovery_url=observation.requested_url,
            reason="Public posting proposed by search.",
            first_seen_on=date(2026, 9, 11),
        )
        session.add(pending)
        session.flush()
        pending_id = pending.id
    evidence_id = PostingContentResolutionService(
        database, _UnusedResolver(), storage=runtime_storage(database, tmp_path / "artifacts")
    ).store_prefetched_result(
        workspace_id, pending_id, observation, None, origin="discovery.refresh_batch"
    )
    return workspace_id, pending_id, evidence_id


@pytest.mark.parametrize(
    ("status", "http_status"),
    [
        (PostingContentAccessStatus.FETCH_FAILED, 503),
        (PostingContentAccessStatus.SESSION_REQUIRED, 401),
        (PostingContentAccessStatus.POLICY_REFUSED, None),
    ],
)
def test_attached_text_binds_the_unusable_observation_the_queue_showed(
    durable_database_url: str,
    tmp_path: Path,
    status: PostingContentAccessStatus,
    http_status: int | None,
) -> None:
    url = "https://careers.aster.example.test/jobs/clinical-data-lead"
    database = Database(durable_database_url)
    workspace_id, pending_id, evidence_id = _pending_with_observation(
        database,
        tmp_path,
        f"manual-{status.value}",
        ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=status,
            identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
            posting_status=None,
            extraction_method=None,
            fetched_at=datetime(2026, 9, 11, 9, tzinfo=UTC),
            http_status=http_status,
            note="The publisher did not return a readable posting.",
        ),
    )
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="canada_remote", country="Canada")],
            countries=[CountryNames(country="Canada")],
        ),
    )
    body = "Clinical Data Lead\nLocation: Amsterdam, Netherlands\n" + (
        "Own study data review and reconciliation. " * 12
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            [shown] = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page"
            ).json()["attention"]["items"]
            attached = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/content",
                json={
                    "content": body,
                    "attested_current_exact_posting": True,
                    "expected_current_content_id": shown["current_content_id"],
                    "expected_url": shown["canonical_url"] or shown["discovery_url"],
                },
            )
            page = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page").json()
        # The same body read by a scan is refused, so non-dismissal above is not vacuous.
        scanned_url = f"{url}-scanned"
        with database.session() as session:
            scanned = PendingDiscovery(
                workspace_id=workspace_id,
                fingerprint="e" * 64,
                employer="Aster Systems",
                title="Clinical Data Lead",
                discovery_url=scanned_url,
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 9, 11),
            )
            session.add(scanned)
            session.flush()
            scanned_id = scanned.id
        PostingContentResolutionService(
            database, _UnusedResolver(), storage=runtime_storage(database, tmp_path / "artifacts")
        ).store_prefetched_result(
            workspace_id,
            scanned_id,
            ResolvedPostingContent(
                requested_url=scanned_url,
                final_url=scanned_url,
                content_status=PostingContentAccessStatus.READABLE,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                fetched_at=datetime(2026, 9, 11, 10, tzinfo=UTC),
                content=body.encode("utf-8"),
                http_status=200,
                publisher_markup_present=True,
                identity_evidence={"publisher_declaration": True},
                note="Verified publisher body.",
            ),
            None,
            origin="source.scan",
        )
        with database.session() as session:
            dismissed = set(
                session.scalars(
                    select(PendingDiscoveryDispositionEvent.pending_discovery_id).where(
                        PendingDiscoveryDispositionEvent.action == "dismiss"
                    )
                )
            )
    finally:
        database.dispose()

    assert shown["retained_content_id"] is None
    assert shown["current_content_id"] == evidence_id
    assert attached.status_code == 200, attached.json()
    assert attached.json()["readiness"] == "ready"
    assert [item["id"] for item in page["ready"]["items"]] == [pending_id]
    assert dismissed == {scanned_id}


def test_identity_confirmation_binds_the_ambiguous_observation_the_queue_showed(
    durable_database_url: str, tmp_path: Path
) -> None:
    url = "https://careers.aster.example.test/jobs/clinical-data-lead"
    database = Database(durable_database_url)
    workspace_id, pending_id, evidence_id = _pending_with_observation(
        database,
        tmp_path,
        "manual-identity",
        ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.AMBIGUOUS,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=datetime(2026, 9, 11, 9, tzinfo=UTC),
            content=b"Data Lead\nOwn study data review and reconciliation.",
            http_status=200,
            identity_evidence={"publisher_title": "Data Lead"},
            note="The publisher heading did not match the staged title.",
            robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
        ),
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            [shown] = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page"
            ).json()["attention"]["items"]
            confirmed = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/confirm-identity",
                json={
                    "attested_same_posting": True,
                    "expected_current_content_id": shown["current_content_id"],
                    "expected_url": shown["canonical_url"] or shown["discovery_url"],
                },
            )
            page = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page").json()
    finally:
        database.dispose()

    assert shown["readiness"] == "identity_ambiguous"
    assert shown["current_robots_basis"] == "single_posting_read"
    assert shown["retained_content_id"] is None
    assert shown["current_content_id"] == evidence_id
    assert confirmed.status_code == 200, confirmed.json()
    assert [item["id"] for item in page["ready"]["items"]] == [pending_id]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, {"ready": 1, "attention": 1}),
        ({"search": "attention"}, {"ready": 0, "attention": 1}),
        ({"readiness": "ready"}, {"ready": 1, "attention": 0}),
    ],
)
def test_pending_discovery_changes_counts_new_visible_rows_by_lane(
    durable_database_url: str,
    tmp_path: Path,
    filters: dict[str, str],
    expected: dict[str, int],
) -> None:
    """Rows older than the snapshot, hidden rows, and rows the page filters drop go uncounted."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="changes-lanes", name="Changes lanes")
        .id
    )
    snapshot_before = datetime(2026, 1, 2, tzinfo=UTC)
    old_seen_at = datetime(2026, 1, 1, tzinfo=UTC)
    new_seen_at = datetime(2026, 1, 3, tzinfo=UTC)
    ready_url = "https://jobs.example.test/opening/ready"
    with database.session() as session:
        session.add(
            PendingDiscovery(
                fingerprint="1" * 64,
                workspace_id=workspace_id,
                employer="Initech",
                title="Old role",
                discovery_url="https://jobs.example.test/opening/old",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 1, 1),
                created_at=old_seen_at,
            )
        )
        ready = PendingDiscovery(
            fingerprint="2" * 64,
            workspace_id=workspace_id,
            employer="Globex",
            title="Ready role",
            discovery_url=ready_url,
            canonical_url=ready_url,
            reason="Publisher-listed role.",
            first_seen_on=date(2026, 1, 3),
            created_at=new_seen_at,
        )
        session.add(ready)
        session.flush()
        artifact = Artifact(
            workspace_id=workspace_id,
            logical_type="posting.body.text",
            mime_type="text/plain",
            storage_uri="memory://ready-role",
            byte_size=9,
            sha256="c" * 64,
        )
        session.add(artifact)
        session.flush()
        session.add(
            pending_posting_content(
                workspace_id,
                ready.id,
                ready_url,
                identity_status=PostingIdentityStatus.VERIFIED,
                posting_status=PostingStatus.ACTIVE,
                artifact_id=artifact.id,
                extraction_method=PostingContentExtractionMethod.STATIC_HTML,
                content_sha256="b" * 64,
                fetched_at=new_seen_at,
            )
        )
        session.flush()
        content = session.scalars(
            select(PendingPostingContent).where(
                PendingPostingContent.pending_discovery_id == ready.id
            )
        ).one()
        ready.current_content_id = content.id
        session.add(
            PendingDiscovery(
                fingerprint="3" * 64,
                workspace_id=workspace_id,
                employer="Globex",
                title="Attention role",
                discovery_url="https://jobs.example.test/opening/attention",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 1, 3),
                created_at=new_seen_at,
            )
        )
        session.add(
            PendingDiscovery(
                fingerprint="4" * 64,
                workspace_id=workspace_id,
                employer="Globex",
                title="Archived role",
                discovery_url="https://jobs.example.test/opening/archived",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 1, 3),
                created_at=new_seen_at,
                archived_at=new_seen_at,
            )
        )
        session.add(
            PendingDiscovery(
                fingerprint="5" * 64,
                workspace_id=workspace_id,
                employer="Globex",
                title="Withdrawn role",
                discovery_url="https://jobs.example.test/opening/withdrawn",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 1, 3),
                created_at=new_seen_at,
                withdrawn_at=new_seen_at,
            )
        )
        session.add(
            PendingDiscovery(
                fingerprint="6" * 64,
                workspace_id=workspace_id,
                employer="Globex",
                title="Variant role",
                discovery_url="https://jobs.example.test/opening/variant",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 1, 3),
                created_at=new_seen_at,
                regional_variant_of=ready.id,
            )
        )
        dismissed = PendingDiscovery(
            fingerprint="7" * 64,
            workspace_id=workspace_id,
            employer="Globex",
            title="Dismissed role",
            discovery_url="https://jobs.example.test/opening/dismissed",
            reason="Publisher-listed role.",
            first_seen_on=date(2026, 1, 3),
            created_at=new_seen_at,
        )
        session.add(dismissed)
        session.flush()
        session.add(
            PendingDiscoveryDispositionEvent(
                workspace_id=workspace_id,
                pending_discovery_id=dismissed.id,
                action="dismiss",
                category="hard_gate_excluded",
                reason="Excluded by a saved rule.",
                selection_mode="rule",
                snapshot_sha256="0" * 64,
                created_at=new_seen_at,
            )
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/changes",
                params={"snapshot_before": snapshot_before.isoformat(), **filters},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "newer_visible": expected,
        "follow_up": {
            "active_sources": 0,
            "automatic_pending_count": 0,
            "frontier_completed_count": 0,
        },
    }


def test_pending_discovery_changes_reports_active_publisher_follow_up(
    durable_database_url: str, tmp_path: Path
) -> None:
    """An active source.continue task is the only thing that counts a scan as active."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="changes-follow-up", name="Changes follow-up")
        .id
    )
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Example",
            normalized_name=normalize_text("Example"),
        )
        session.add(employer)
        session.flush()
        source = Source(
            workspace_id=workspace_id,
            employer_id=employer.id,
            name="Example careers",
            source_type="employer_board",
            url="https://boards.example.test/jobs",
            priority=1,
            enabled=True,
        )
        session.add(source)
        session.flush()
        scan_moment = utc_now()
        scan = SourceScan(
            source_id=source.id,
            scanned_on=scan_moment.date(),
            query_label="title-filter",
            status="incomplete",
            read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            raw_count=0,
            read_count=0,
            skip_reasons={},
            result_count=0,
            new_count=0,
            notes="",
            scan_started_at=scan_moment,
            scan_completed_at=scan_moment,
            duration_ms=0,
        )
        session.add(scan)
        session.flush()
        frontier: list[dict[str, object]] = [
            {"url": f"https://boards.example.test/jobs/{index}", "status": "completed"}
            for index in range(3)
        ]
        frontier.append(
            {
                "url": "https://boards.example.test/jobs/page/2",
                "label": "Next role",
                "source_url": "https://boards.example.test/jobs",
                "relations": ["publisher-content"],
            }
        )
        continuation_id = seed_scan_continuation(session, workspace_id, scan.id, frontier)
        session.add(
            BackgroundTask(
                workspace_id=workspace_id,
                task_type="source.continue",
                status="running",
                payload={
                    "continuation_id": continuation_id,
                    "generation": 1,
                    "include_policy_deferred": False,
                    "no_progress_count": 0,
                },
                payload_sha256="0" * 64,
                idempotency_key="source-continuation-test",
            )
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/changes",
                params={"snapshot_before": utc_now().isoformat()},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json()["follow_up"] == {
        "active_sources": 1,
        "automatic_pending_count": 1,
        "frontier_completed_count": 3,
    }


def test_pending_closure_reconciliation_uses_only_current_saved_closure_evidence(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="saved-closure", name="Saved closure")
        .id
    )
    observed_at = utc_now()
    with database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint="b" * 64,
            employer="Northwind Research",
            title="Platform Steward",
            discovery_url="https://jobs.example.test/opening/2",
            reason="Publisher-listed role.",
            first_seen_on=date(2026, 8, 28),
        )
        session.add(pending)
        session.flush()
        old = pending_posting_content(
            workspace_id,
            pending.id,
            pending.discovery_url,
            content_status=PostingContentAccessStatus.NOT_YET_READ,
            posting_status=PostingStatus.CLOSED,
            fetched_at=observed_at,
        )
        session.add(old)
        session.flush()
        current = pending_posting_content(
            workspace_id,
            pending.id,
            pending.discovery_url,
            content_status=PostingContentAccessStatus.NOT_YET_READ,
            posting_status=PostingStatus.CLOSED,
            fetched_at=observed_at,
        )
        session.add(current)
        session.flush()
        pending.current_content_id = current.id
        pending_id = pending.id
        evidence_id = current.id
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/reconcile-closures",
                json={"pending_discovery_ids": [pending_id]},
            )
            repeated = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/reconcile-closures",
                json={"pending_discovery_ids": [pending_id]},
            )
            duplicate = client.post(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/reconcile-closures",
                json={"pending_discovery_ids": [pending_id, pending_id]},
            )
        with database.session() as session:
            persisted = session.get(PendingDiscovery, pending_id)
            audits = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "pending_discovery.saved_closure_applied",
                )
            ).all()
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "pending_discovery_id": pending_id,
                "outcome": "applied",
                "evidence_id": evidence_id,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            }
        ]
    }
    assert repeated.status_code == 200
    assert repeated.json()["results"][0]["outcome"] == "unchanged"
    assert duplicate.status_code == 422
    assert persisted is not None
    assert persisted.withdrawn_at == observed_at
    assert len(audits) == 1


def test_resetting_the_pending_queue_rescreens_source_less_rows_over_http(
    durable_database_url: str, tmp_path: Path
) -> None:
    """POST .../reset reports source-less rows that no later board scan can find."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="reset", name="Reset").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/pending-discoveries"
            first = client.post(
                base,
                json={"url": "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"},
            )
            second = client.post(
                base,
                json={"url": "https://jobs.lever.co/initech/a1111111-4c2e-4b7d-9f1a-6e3d2c1b0a99"},
            )
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            assert len(client.get(base).json()) == 2

            reset = client.post(f"{base}/reset")
            assert reset.status_code == 200, reset.text
            assert reset.json() == {
                "archived_count": 0,
                "kept_count": 0,
                "rescreened_count": 2,
            }
            assert len(client.get(base).json()) == 2
    finally:
        database.dispose()


def test_a_kept_pending_discovery_is_dismissed_over_http_only_when_confirmed(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="keep-pending", name="Keep pending")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/pending-discoveries"
            created = client.post(
                base,
                json={"url": "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"},
            )
            assert created.status_code == 200, created.text
            pending_id = created.json()["pending_discovery"]["id"]

            marked = client.put(
                f"{base}/{pending_id}/markers",
                json={"favorite": True, "kept": True, "keep_note": "Hiring manager replied."},
                headers={"X-Correlation-ID": "keep-api-1"},
            )
            assert marked.status_code == 200, marked.text
            assert (marked.json()["favorite"], marked.json()["kept"]) == (True, True)
            assert marked.json()["keep_note"] == "Hiring manager replied."
            queue = client.get(base).json()
            assert (queue[0]["id"], queue[0]["kept"]) == (pending_id, True)
            note_without_keep = client.put(
                f"{base}/{pending_id}/markers",
                json={"favorite": True, "kept": False, "keep_note": "Orphan note."},
            )
            assert note_without_keep.status_code == 422, note_without_keep.text

            dispositions = f"/api/v1/workspaces/{workspace_id}/pending-dispositions"
            dismissal = {
                "action": "dismiss",
                "selection_mode": "explicit",
                "category": "out_of_scope_title",
                "reason": "  I read the role and it falls outside my current search.\t ",
                "pending_discovery_ids": [pending_id],
            }
            refused = client.post(f"{dispositions}/preview", json=dismissal)
            assert refused.status_code == 409, refused.text

            confirmed = {**dismissal, "dismiss_marked": True}
            preview = client.post(f"{dispositions}/preview", json=confirmed)
            assert preview.status_code == 200, preview.text
            assert preview.json()["category"] == dismissal["category"]
            assert preview.json()["reason"] == dismissal["reason"]
            changed_reason = client.post(
                f"{dispositions}/apply",
                json={
                    **confirmed,
                    "reason": "I changed my explanation after reading the preview.",
                    "snapshot_sha256": preview.json()["snapshot_sha256"],
                },
            )
            assert changed_reason.status_code == 409, changed_reason.text
            assert changed_reason.json()["type"] == "urn:clearscout:problem:conflict"
            assert changed_reason.json()["detail"] == (
                "That disposition preview is stale. Preview the selection again."
            )
            applied = client.post(
                f"{dispositions}/apply",
                json={**confirmed, "snapshot_sha256": preview.json()["snapshot_sha256"]},
                headers={"X-Correlation-ID": "dismiss-api-1"},
            )
            assert applied.status_code == 200, applied.text
            [disposition] = applied.json()
            assert disposition["category"] == dismissal["category"]
            assert disposition["reason"] == dismissal["reason"]
            assert disposition["pending_discovery_id"] == pending_id
            assert disposition["selection_mode"] == "explicit"
            history = client.get(f"{base}/{pending_id}/dispositions")
            assert history.status_code == 200, history.text
            assert history.json() == applied.json()
            assert client.get(base).json() == []
        with database.session() as session:
            stored = session.get(PendingDiscoveryDispositionEvent, disposition["id"])
            assert stored is not None
            assert stored.reason == dismissal["reason"]
            assert (stored.created_by, stored.origin, stored.correlation_id) == (
                "local_user",
                "api",
                "dismiss-api-1",
            )
            row = session.get(PendingDiscovery, pending_id)
            assert row is not None
            assert (row.favorite, row.kept, row.keep_note) == (False, False, None)
            event = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.entity_id == pending_id,
                    AuditEvent.action == "pending_discovery.markers_changed",
                )
            )
            assert event is not None
            assert (event.origin, event.correlation_id) == ("api", "keep-api-1")
    finally:
        database.dispose()


def test_adding_a_reset_archived_pending_discovery_reports_that_it_was_requeued(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="requeue", name="Requeue")
        .id
    )
    url = "https://jobs.lever.co/globex/3295bcc2-6948-40f6-9ef2-e2176ef934b2"
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            endpoint = f"/api/v1/workspaces/{workspace_id}/pending-discoveries"
            created = client.post(endpoint, json={"url": url})
            assert created.status_code == 200, created.text
            created_body = created.json()
            assert created_body["outcome"] == "created"
            pending_id = created_body["pending_discovery"]["id"]

            duplicate = client.post(endpoint, json={"url": url})
            assert duplicate.status_code == 409

            with database.session() as session:
                pending = session.get(PendingDiscovery, pending_id)
                assert pending is not None
                pending.archived_at = utc_now()
                pending.withdrawn_at = utc_now()
                session.add(
                    AuditEvent(
                        id=new_id(),
                        workspace_id=workspace_id,
                        action="pending_discovery.queue_reset",
                        entity_type="pending_discovery",
                        entity_id=pending_id,
                        details={"discovery_url": url},
                        created_at=pending.archived_at,
                        created_by="local_user",
                        origin="pending_queue.reset",
                    )
                )

            requeued = client.post(
                endpoint,
                json={"url": url},
                headers={"X-Correlation-ID": "requeue-1"},
            )
            assert requeued.status_code == 200, requeued.text
            requeued_body = requeued.json()
            assert requeued_body["outcome"] == "requeued"
            assert requeued_body["pending_discovery"]["id"] == pending_id
            assert requeued_body["pending_discovery"]["withdrawn_at"] is None

        with database.session() as session:
            event = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.action == "pending_discovery.requeued",
                )
            )
            assert event is not None
            assert event.entity_id == pending_id
            assert event.origin == "api"
            assert event.correlation_id == "requeue-1"
    finally:
        database.dispose()


def test_source_preview_resolves_a_board_linked_from_a_careers_page(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A careers page that only links its board still resolves before saving."""
    careers_page = (
        "https://acme.example/careers\n"
        '<html><body><a href="https://acme.wd5.myworkdayjobs.com/en-US/External">'
        "Open roles</a></body></html>"
    )
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: careers_page)
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="preview", name="Preview")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/sources/preview",
                json={"url": "https://acme.example/careers"},
            )

        assert response.status_code == 200
        assert response.json()["platform"] == "workday"
        assert response.json()["resolved_url"] == "https://acme.example/careers"
        assert response.json()["note"] == (
            "Recognized Workday board. Scan results show actual coverage and any publisher limits."
        )
    finally:
        database.dispose()


def test_source_preview_names_an_unreachable_platform_without_raising(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page naming no supported board resolves to no platform, not an error."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "no board here")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="unresolved", name="Unresolved")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/sources/preview",
                json={"url": "https://acme.example/careers"},
            )

        assert response.status_code == 200
        assert response.json()["platform"] is None
        assert "cannot read" in response.json()["note"]
    finally:
        database.dispose()


def test_revalidate_re_resolves_a_saved_sources_platform_and_persists_it(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revalidate updates the durable row, not just the response it returns."""
    page = {"content": "https://acme.example/careers\nno board yet"}
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: page["content"])
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="reval", name="Reval").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(
                base, json={"name": "Acme", "url": "https://acme.example/careers"}
            )
            assert created.json()["platform"] is None
            source_id = created.json()["id"]

            page["content"] = (
                "https://acme.example/careers\n"
                '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
            )
            revalidated = client.post(f"{base}/{source_id}/revalidate")
            assert revalidated.status_code == 200
            assert revalidated.json()["platform"] == "workday"

            listed = client.get(base).json()["items"]
            assert listed[0]["platform"] == "workday"
    finally:
        database.dispose()


def test_source_scan_history_derives_each_scans_publisher_follow_up_state(
    durable_database_url: str, tmp_path: Path
) -> None:
    """Each of none, waiting, active, complete, and superseded is reachable over HTTP."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="follow-up-states", name="Follow-up states")
        .id
    )

    def source(name: str, url: str) -> str:
        record = Source(
            workspace_id=workspace_id,
            name=name,
            source_type="employer_board",
            url=url,
            priority=1,
            enabled=True,
        )
        session.add(record)
        session.flush()
        return record.id

    def scan(source_id: str, scanned_on: date, *, complete: bool | None = None) -> str:
        moment = datetime.combine(scanned_on, datetime.min.time(), tzinfo=UTC)
        record = SourceScan(
            source_id=source_id,
            scanned_on=scanned_on,
            query_label="title-filter",
            status="incomplete",
            read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
            raw_count=0,
            read_count=0,
            skip_reasons={},
            result_count=0,
            new_count=0,
            notes="",
            complete=complete,
            scan_started_at=moment,
            scan_completed_at=moment,
            duration_ms=0,
        )
        session.add(record)
        session.flush()
        return record.id

    def continuation_with_frontier(scan_id: str, *, frontier_status: str) -> str:
        return seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [
                {
                    "url": "https://boards.example.test/jobs/page/2",
                    "normalized_url": f"https://boards.example.test/jobs/page/2?scan={scan_id}",
                    "label": "Next role",
                    "source_url": "https://boards.example.test/jobs",
                    "relations": ["publisher-content"],
                    "status": frontier_status,
                }
            ],
        )

    with database.session() as session:
        none_source_id = source("None source", "https://boards.example.test/none")
        none_scan = scan(none_source_id, date(2026, 1, 1))

        waiting_scan = scan(
            source("Waiting source", "https://boards.example.test/waiting"), date(2026, 1, 1)
        )
        continuation_with_frontier(waiting_scan, frontier_status="pending")

        active_scan = scan(
            source("Active source", "https://boards.example.test/active"), date(2026, 1, 1)
        )
        active_continuation_id = continuation_with_frontier(active_scan, frontier_status="pending")
        session.add(
            BackgroundTask(
                workspace_id=workspace_id,
                task_type="source.continue",
                status="running",
                payload={
                    "continuation_id": active_continuation_id,
                    "generation": 1,
                    "include_policy_deferred": False,
                    "no_progress_count": 0,
                },
                payload_sha256="0" * 64,
                idempotency_key="source-continuation-active",
            )
        )

        complete_scan = scan(
            source("Complete source", "https://boards.example.test/complete"), date(2026, 1, 1)
        )
        continuation_with_frontier(complete_scan, frontier_status="completed")

        superseded_source_id = source("Superseded source", "https://boards.example.test/superseded")
        superseded_scan = scan(superseded_source_id, date(2026, 1, 1), complete=True)
        continuation_with_frontier(superseded_scan, frontier_status="pending")
        scan(superseded_source_id, date(2026, 1, 2))

    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scans", params={"limit": 250}
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[none_scan]["follow_up_state"] == "none"
    assert by_id[none_scan]["automatic_pending_count"] == 0
    assert by_id[waiting_scan]["follow_up_state"] == "waiting"
    assert by_id[waiting_scan]["automatic_pending_count"] == 1
    assert by_id[active_scan]["follow_up_state"] == "active"
    assert by_id[active_scan]["automatic_pending_count"] == 1
    assert by_id[complete_scan]["follow_up_state"] == "complete"
    assert by_id[complete_scan]["automatic_pending_count"] == 0
    assert by_id[superseded_scan]["follow_up_state"] == "superseded"


def test_api_evaluation_application_and_task_flow(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_response = client.post(
            "/api/v1/workspaces",
            json={"slug": "api", "name": "API"},
            headers={"X-Correlation-ID": "workspace-1"},
        )
        assert workspace_response.status_code == 201
        assert workspace_response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in workspace_response.headers["content-security-policy"]
        workspace_id = workspace_response.json()["id"]

        evaluation = client.post(
            "/api/v1/evaluations",
            json=evaluation_commit(workspace_id).model_dump(mode="json"),
        )
        assert evaluation.status_code == 201
        job_id = evaluation.json()["job_posting_id"]

        jobs = client.get(
            "/api/v1/jobs",
            params={"workspace_id": workspace_id, "search": "Acme"},
        )
        assert jobs.status_code == 200
        assert jobs.json()["total"] == 1
        # The ledger names the policy behind each overall. A workspace can
        # author its own, so two overalls are not comparable without knowing
        # which policy produced each.
        row = jobs.json()["items"][0]
        assert row["score_policy_name"] == "ClearScout default"
        assert row["score_policy_version"] == 1
        detail = client.get(f"/api/v1/jobs/{job_id}", params={"workspace_id": workspace_id}).json()
        assert {item["text"] for item in detail["evidence"]} == {
            "data platform",
            "Canada remote",
        }
        # The one shipped policy does not weight evidence.
        assert detail["score_policy"]["version"] == 1
        # The published parts reconstruct the published total. Evidence contributes a
        # product of zero rather than being absent from the breakdown.
        assert (
            sum(
                item["product"]
                for item in detail["score_arithmetic"]
                if item["product"] is not None
            )
            == 88.0
        )
        assert detail["overall_score"] == 88
        assert detail["evaluation_version"] == 1
        assert detail["evaluation_history"][0]["is_current"] is True
        candidate_evidence = next(
            item for item in detail["evidence"] if item["source_kind"] == "candidate"
        )
        candidate_source = client.get(
            f"/api/v1/evidence/{candidate_evidence['id']}/source",
            params={"workspace_id": workspace_id},
        )
        assert candidate_source.status_code == 200
        assert candidate_source.json()["body"] == "Led a governed data platform."
        assert (
            client.get(
                f"/api/v1/evidence/{candidate_evidence['id']}/source",
                params={"workspace_id": "different-workspace"},
            ).status_code
            == 404
        )
        report = client.get(
            f"/api/v1/jobs/{job_id}/fit-report", params={"workspace_id": workspace_id}
        )
        assert report.status_code == 200
        assert report.json()["evaluation_version"] == 1
        posting_evidence_id = next(
            item["id"] for item in detail["evidence"] if item["text"] == "Canada remote"
        )
        gate_requirement = next(
            item for item in report.json()["requirements"] if item["text"] == "Canada remote"
        )
        assert isinstance(gate_requirement.pop("requirement_key"), str)
        assert gate_requirement == {
            "requirement_id": "REQ-2",
            "importance": "must_have",
            "text": "Canada remote",
            "evidence_keys": [posting_evidence_id],
            "span_status": "needs_review",
            "established": True,
            "unmet": False,
            "dimension": "hard",
            "is_hard_gate": True,
            "gate_kind": None,
            "gate_quote": None,
            "gate_blocking": None,
        }
        assert report.json()["requirements"][0]["text"] == "Lead a data platform"
        assert "## Provenance" in report.json()["markdown"]
        assert f"/jobs/{job_id}?tab=evidence&evidence=" in report.json()["markdown"]
        assert (
            client.get(
                f"/api/v1/jobs/{job_id}/fit-report",
                params={"workspace_id": "different-workspace"},
            ).status_code
            == 404
        )
        exported = client.get(
            f"/api/v1/jobs/{job_id}/fit-report/export",
            params={"workspace_id": workspace_id},
        )
        assert exported.status_code == 200
        assert exported.headers["content-disposition"].startswith("attachment;")
        observation_id = detail["observations"][0]["id"]
        body = client.get(
            f"/api/v1/posting-observations/{observation_id}",
            params={"workspace_id": workspace_id},
        )
        assert body.status_code == 200
        assert "data platform" in body.json()["body"]
        assert (
            client.get(
                f"/api/v1/posting-observations/{observation_id}",
                params={"workspace_id": "different-workspace"},
            ).status_code
            == 404
        )
        compared = client.get(
            f"/api/v1/jobs/{job_id}/posting-diff",
            params={
                "workspace_id": workspace_id,
                "from": observation_id,
                "to": observation_id,
            },
        )
        assert compared.status_code == 200
        assert compared.json()["body_diff"] == ""

        event_body = {
            "status": "package_ready",
            "occurred_at": "2026-01-03T12:00:00Z",
            "notes": "Approved locally; nothing submitted.",
        }
        event = client.post(
            f"/api/v1/jobs/{job_id}/application-events",
            params={"workspace_id": workspace_id},
            json=event_body,
            headers={"Idempotency-Key": "ready-1"},
        )
        assert event.status_code == 201
        assert event.json()["status"] == "package_ready"
        replay = client.post(
            f"/api/v1/jobs/{job_id}/application-events",
            params={"workspace_id": workspace_id},
            json=event_body,
            headers={"Idempotency-Key": "ready-1"},
        )
        assert replay.json()["id"] == event.json()["id"]

        summary = client.get(f"/api/v1/workspaces/{workspace_id}/summary")
        assert summary.status_code == 200
        assert summary.json()["total_jobs"] == 1
        assert summary.json()["actionable_applications"] == 1
        assert summary.json()["application_funnel"]["package_ready"] == 1
        assert summary.json()["highest_intrinsic"][0]["job_posting_id"] == job_id
        assert client.get("/api/v1/workspaces/missing/summary").status_code == 404

        openapi = client.get("/api/v1/openapi.json")
        assert openapi.status_code == 200
        assert "/api/v1/evaluations" in openapi.json()["paths"]
        assert "/api/v1/workspaces/{workspace_id}/summary" in openapi.json()["paths"]


def test_fit_report_folds_a_stored_gate_that_repeats_a_requirement_text(
    durable_database_url: str, tmp_path: Path
) -> None:
    """A stored payload with a gate duplicating a requirement's text folds to one row."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "legacy-fold", "name": "Legacy Fold"}
        ).json()["id"]
        proposal = evaluation_commit(
            workspace_id,
            fit_report={
                "prompt_fingerprint": "test-suite-v1",
                "hard_gates": [
                    {
                        "requirement_id": "gate-1",
                        "importance": "must_have",
                        "text": "Lead a data platform",
                        "evidence_keys": ["candidate-platform"],
                        "is_hard_gate": True,
                        "gate_kind": "minimum_years",
                        "gate_blocking": True,
                    }
                ],
                "reading": [
                    {
                        "question": "Where the work happens",
                        "answer": "remote",
                        "evidence_keys": ["posting-location"],
                    }
                ],
                "requirements": [
                    {
                        "requirement_id": "REQ-1",
                        "importance": "must_have",
                        "text": "Lead a data platform",
                        "evidence_keys": ["candidate-platform"],
                    }
                ],
                "coverage_present": 1,
                "coverage_total": 1,
                "checks": [{"check": "factual support", "result": "pass"}],
                "length_budget": "No derivative document was generated during evaluation.",
                "provenance": [],
            },
        )

        evaluation = client.post("/api/v1/evaluations", json=proposal.model_dump(mode="json"))
        assert evaluation.status_code == 201
        job_id = evaluation.json()["job_posting_id"]

        report = client.get(
            f"/api/v1/jobs/{job_id}/fit-report", params={"workspace_id": workspace_id}
        ).json()

        matches = [
            item for item in report["requirements"] if item["text"] == "Lead a data platform"
        ]
        assert len(matches) == 1
        assert matches[0]["requirement_id"] == "REQ-1"
        assert matches[0]["is_hard_gate"] is True
        assert matches[0]["gate_kind"] == "minimum_years"
        assert matches[0]["gate_blocking"] is True


def test_application_event_api_rejects_new_regression_but_replays_the_original(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "event-order", "name": "Event order"}
        ).json()["id"]
        job_id = client.post(
            "/api/v1/evaluations",
            json=evaluation_commit(workspace_id).model_dump(mode="json"),
        ).json()["job_posting_id"]
        ready_body = {
            "status": "package_ready",
            "occurred_at": "2026-01-01T12:00:00Z",
        }
        endpoint = f"/api/v1/jobs/{job_id}/application-events"
        ready = client.post(
            endpoint,
            params={"workspace_id": workspace_id},
            json=ready_body,
            headers={"Idempotency-Key": "ready-original"},
        )
        assert ready.status_code == 201
        assert (
            client.post(
                endpoint,
                params={"workspace_id": workspace_id},
                json={"status": "applied", "occurred_at": "2026-01-02T12:00:00Z"},
                headers={"Idempotency-Key": "applied-next"},
            ).status_code
            == 201
        )

        replay = client.post(
            endpoint,
            params={"workspace_id": workspace_id},
            json=ready_body,
            headers={"Idempotency-Key": "ready-original"},
        )
        refused = client.post(
            endpoint,
            params={"workspace_id": workspace_id},
            json={"status": "package_ready", "occurred_at": "2026-01-03T12:00:00Z"},
            headers={"Idempotency-Key": "ready-regression"},
        )

    assert replay.status_code == 201
    assert replay.json()["id"] == ready.json()["id"]
    assert refused.status_code == 409
    assert "cannot be recorded after" in refused.json()["detail"]
