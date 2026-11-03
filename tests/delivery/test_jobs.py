"""Job posting routes: request validation, status mapping, and response shape."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.tasks import TaskQueueService
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    JobPosting,
    PendingDiscovery,
    PostingObservation,
    Workspace,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import MemoryStore
from tests.support.scenarios import (
    evaluation_commit,
    pending_posting_content,
    seed_pending_discovery,
    workspace_with_committed_evaluation,
)
from tests.support.suite import SUITE_PROVIDER


def test_pending_discovery_can_be_tracked_and_applied_without_evaluation(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "manual-track", "name": "Manual track"}
        ).json()["id"]
        pending_id = seed_pending_discovery(
            database,
            workspace_id,
            employer="Example",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        tracked = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/track"
        )
        assert tracked.status_code == 200, tracked.text
        job_id = tracked.json()["job_posting_id"]
        assert tracked.json()["evaluation_id"] is None
        assert tracked.json()["decision"] is None

        ledger = client.get("/api/v1/jobs", params={"workspace_id": workspace_id})
        assert ledger.status_code == 200, ledger.text
        assert ledger.json()["total"] == 1
        assert ledger.json()["items"][0]["job_posting_id"] == job_id
        assert ledger.json()["items"][0]["overall_score"] is None

        replay = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/track"
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["job_posting_id"] == job_id

        review = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={"priority": "high", "fit": "possible", "note": "Needs a closer look."},
        )
        assert review.status_code == 200, review.text
        assert review.json()["manual_priority"] == "high"
        assert review.json()["manual_fit"] == "possible"
        assert review.json()["manual_note"] == "Needs a closer look."
        ranked = client.get("/api/v1/jobs", params={"workspace_id": workspace_id})
        assert ranked.json()["items"][0]["manual_priority"] == "high"

        recorded = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={
                "priority": "high",
                "recorded_fit_score": 77,
                "recorded_compensation_score": 68,
                "recorded_overall_score": 79,
                "recorded_compensation": "USD 120,000-150,000 annually",
                "recorded_work_model": "Remote in Canada",
                "recorded_eligibility": "Applicant assessed as eligible",
                "next_action": "Submit package",
                "recorded_deadline": "2026-01-20 17:00 MST",
                "recorded_source": "Synthetic source record, 2026-01-02",
                "note": "Source values, not model scores.",
            },
        )
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["recorded_fit_score"] == 77
        assert recorded.json()["recorded_overall_score"] == 79
        assert recorded.json()["fit_score"] is None
        assert recorded.json()["recorded_compensation"] == "USD 120,000-150,000 annually"
        assert (
            client.get("/api/v1/jobs", params={"workspace_id": workspace_id}).json()["items"][0][
                "recorded_compensation_score"
            ]
            == 68
        )
        for sort in (
            "recorded_fit_score",
            "recorded_compensation_score",
            "tracker_composite_score",
        ):
            legacy_sort = client.get(
                "/api/v1/jobs", params={"workspace_id": workspace_id, "sort": sort}
            )
            assert legacy_sort.status_code == 200, legacy_sort.text
            assert legacy_sort.json()["items"][0]["job_posting_id"] == job_id

        invalid = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={"recorded_fit_score": 101},
        )
        assert invalid.status_code == 422

        cleared = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={"priority": "high", "recorded_fit_score": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["recorded_fit_score"] is None
        assert cleared.json()["recorded_compensation"] == ""

        another_pending = seed_pending_discovery(
            database,
            workspace_id,
            employer="Globex",
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
        )
        another = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{another_pending}/track"
        )
        assert another.status_code == 200, another.text
        another_id = another.json()["job_posting_id"]
        client.put(
            f"/api/v1/jobs/{another_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={"priority": "low", "fit": "weak", "note": "Lower priority."},
        )
        sorted_jobs = client.get(
            "/api/v1/jobs",
            params={"workspace_id": workspace_id, "sort": "manual_priority", "descending": True},
        )
        assert sorted_jobs.status_code == 200, sorted_jobs.text
        assert [item["job_posting_id"] for item in sorted_jobs.json()["items"]] == [
            job_id,
            another_id,
        ]

        applied = client.post(
            f"/api/v1/jobs/{job_id}/application-events",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "submitted-1"},
            json={"status": "applied", "occurred_at": "2026-01-03T12:00:00Z"},
        )
        assert applied.status_code == 201, applied.text
        detail = client.get(f"/api/v1/jobs/{job_id}", params={"workspace_id": workspace_id})
        assert detail.status_code == 200, detail.text
        assert detail.json()["application_status"] == "applied"

        evaluated = client.post(
            "/api/v1/evaluations",
            json=evaluation_commit(
                workspace_id,
                employer="Example",
                title="Data Platform Lead",
                requisition_id="REQ-1",
                canonical_url="https://boards.greenhouse.io/example/jobs/1",
                discovery_url="https://boards.greenhouse.io/example/jobs/1",
            ).model_dump(mode="json"),
        )
        assert evaluated.status_code == 201, evaluated.text
        assert evaluated.json()["job_posting_id"] == job_id
        assert evaluated.json()["application_status"] == "applied"
        assert evaluated.json()["manual_priority"] == "high"

    database.dispose()


def test_tracking_retains_the_current_publisher_date_and_body_without_another_read(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    artifacts = EvidenceArtifactService(runtime_storage(database))
    fetched_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "date-track", "name": "Date track"}
        ).json()["id"]
        pending_id = seed_pending_discovery(
            database, workspace_id, employer="Example", created_at=fetched_at
        )
        with database.session() as session:
            pending = session.get(PendingDiscovery, pending_id)
            assert pending is not None
            body = b"Data Platform Lead\nPosting End Date: 2026-09-01"
            artifact = artifacts.store(
                session,
                workspace_id=workspace_id,
                logical_type="posting.body.text",
                content=body,
                relative_path=f"{workspace_id}/pending/{pending_id}/body.txt",
                mime_type="text/plain",
                related_entity_type="pending_discovery",
                related_entity_id=pending_id,
                provenance={"source_url": pending.canonical_url},
                audit={"created_by": "worker", "origin": "test"},
            )
            retained_date = (
                publisher_closing_date(
                    body.decode(),
                    source_url=pending.canonical_url,
                    artifact_id=artifact.id,
                    observed_at=fetched_at,
                )
                .with_artifacts(body_artifact_id=artifact.id, publisher_artifact_id=None)
                .as_json()
            )
            content = pending_posting_content(
                workspace_id,
                pending_id,
                pending.canonical_url,
                identity_status="verified",
                posting_status="active",
                artifact_id=artifact.id,
                content_sha256=artifact.sha256,
                extraction_method="static_html",
                fetched_at=fetched_at,
                publisher_closing_date=retained_date,
            )
            session.add(content)
            session.flush()
            pending.current_content_id = content.id
            artifact_id = artifact.id
            content_sha256 = content.content_sha256
        tracked = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/track"
        )
        assert tracked.status_code == 200, tracked.text
        assert tracked.json()["evaluation_id"] is None
        assert tracked.json()["observation_count"] == 1
        job_id = tracked.json()["job_posting_id"]
        replay = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/track"
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["observation_count"] == 1
        with database.session() as session:
            observation = session.scalar(
                select(PostingObservation).where(PostingObservation.job_posting_id == job_id)
            )
            assert observation is not None
            assert observation.publisher_closing_date == retained_date
            assert observation.body_artifact_id == artifact_id
            assert observation.content_sha256 == content_sha256
            assert observation.fetched_at == fetched_at
            assert observation.posting_status == "active"
    database.dispose()


def test_tracking_does_not_promote_ambiguous_pending_content(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "ambiguous-track", "name": "Ambiguous track"}
        ).json()["id"]
        pending_id = seed_pending_discovery(
            database,
            workspace_id,
            employer="Example",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        with database.session() as session:
            pending = session.get(PendingDiscovery, pending_id)
            assert pending is not None
            content = pending_posting_content(
                workspace_id,
                pending_id,
                pending.canonical_url,
                identity_status="ambiguous",
                posting_status="active",
            )
            session.add(content)
            session.flush()
            pending.current_content_id = content.id
        tracked = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/track"
        )
        assert tracked.status_code == 200, tracked.text
        assert tracked.json()["observation_count"] == 0
        assert tracked.json()["latest_observation"] is None
    database.dispose()


def test_tracking_refuses_an_archived_identity(durable_database_url: str, tmp_path: Path) -> None:
    database = Database(durable_database_url)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "archived-track", "name": "Archived track"}
        ).json()["id"]
        first_id = seed_pending_discovery(
            database,
            workspace_id,
            employer="Example",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        job_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{first_id}/track"
        ).json()["job_posting_id"]
        archived = client.post(
            f"/api/v1/jobs/{job_id}/archive", params={"workspace_id": workspace_id}
        )
        assert archived.status_code == 200
        second_id = seed_pending_discovery(
            database,
            workspace_id,
            employer="Example",
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        rejected = client.post(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{second_id}/track"
        )
        assert rejected.status_code == 409
        assert "Restore it" in rejected.text
        assert (
            client.get(
                "/api/v1/jobs", params={"workspace_id": workspace_id, "include_archived": True}
            ).json()["total"]
            == 1
        )
    database.dispose()


def test_job_detail_pages_application_history_and_reports_its_total(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "history", "name": "History"}
        ).json()["id"]
        created = client.post(
            "/api/v1/evaluations",
            json=evaluation_commit(workspace_id).model_dump(mode="json"),
        )
        job_id = created.json()["job_posting_id"]
        for index, status in enumerate(("package_ready", "applied", "interviewing")):
            response = client.post(
                f"/api/v1/jobs/{job_id}/application-events",
                params={"workspace_id": workspace_id},
                json={
                    "status": status,
                    "occurred_at": f"2026-01-0{index + 1}T12:00:00Z",
                    "notes": f"Event {index + 1}",
                },
                headers={"Idempotency-Key": f"history-{index}"},
            )
            assert response.status_code == 201

        first = client.get(
            f"/api/v1/jobs/{job_id}",
            params={"workspace_id": workspace_id, "history_limit": 1},
        )
        second = client.get(
            f"/api/v1/jobs/{job_id}",
            params={
                "workspace_id": workspace_id,
                "history_limit": 1,
                "application_event_offset": 1,
            },
        )

    assert first.status_code == 200
    assert first.json()["application_event_count"] == 3
    assert [item["notes"] for item in first.json()["application_events"]] == ["Event 1"]
    assert [item["notes"] for item in second.json()["application_events"]] == ["Event 2"]


def test_reevaluate_endpoint_enqueues_discovery_evaluate_for_the_posting(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    try:
        TaskQueueService(database).renew_worker_presence(
            worker_id="worker-reeval", ttl_seconds=3600
        )
    finally:
        database.dispose()
    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            key_store=MemoryStore({SUITE_PROVIDER: "test-key-not-real"}),
        ),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "reeval", "name": "Reeval"}
        ).json()["id"]
        job_id = client.post(
            "/api/v1/evaluations", json=evaluation_commit(workspace_id).model_dump(mode="json")
        ).json()["job_posting_id"]

        response = client.post(
            f"/api/v1/jobs/{job_id}/reevaluate",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "reeval-1"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["task_type"] == "discovery.evaluate"
        database = Database(durable_database_url)
        try:
            with database.session() as session:
                queued = session.get(BackgroundTask, body["id"])
                assert queued is not None
                assert queued.payload == {"job_posting_id": job_id, "fresh": True}
        finally:
            database.dispose()
        assert body["status"] in {"queued", "leased", "running"}


def test_archived_jobs_stay_reachable_and_can_be_restored_over_http(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The browser can undo an archive without dropping to a shell.

    Reading, manual corrections, and restoring stay open while archived.
    """
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "restore", "name": "Restore"}
        ).json()["id"]
        job_id = client.post(
            "/api/v1/evaluations", json=evaluation_commit(workspace_id).model_dump(mode="json")
        ).json()["job_posting_id"]

        assert (
            client.post(
                f"/api/v1/jobs/{job_id}/archive", params={"workspace_id": workspace_id}
            ).status_code
            == 200
        )
        hidden = client.get("/api/v1/jobs", params={"workspace_id": workspace_id})
        assert hidden.json()["total"] == 0
        shown = client.get(
            "/api/v1/jobs", params={"workspace_id": workspace_id, "include_archived": True}
        )
        assert shown.json()["total"] == 1
        assert shown.json()["items"][0]["archived_at"] is not None
        detail = client.get(f"/api/v1/jobs/{job_id}", params={"workspace_id": workspace_id})
        assert detail.status_code == 200
        assert detail.json()["archived_at"] is not None

        corrected = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={"recorded_fit_score": 75, "note": "Corrected after archive."},
        )
        assert corrected.status_code == 200
        assert corrected.json()["archived_at"] is not None
        assert corrected.json()["recorded_fit_score"] == 75
        # Archiving still blocks workflow writes that change the posting's state.
        refused = client.post(
            f"/api/v1/jobs/{job_id}/application-events",
            params={"workspace_id": workspace_id},
            json={"status": "package_ready", "occurred_at": "2026-01-03T12:00:00Z"},
            headers={"Idempotency-Key": "archived-write-1"},
        )
        assert refused.status_code == 404

        restored = client.post(
            f"/api/v1/jobs/{job_id}/unarchive", params={"workspace_id": workspace_id}
        )
        assert restored.status_code == 200
        assert restored.json()["archived"] is False and restored.json()["changed"] is True
        assert (
            client.get("/api/v1/jobs", params={"workspace_id": workspace_id}).json()["total"] == 1
        )
        assert (
            client.get(f"/api/v1/jobs/{job_id}", params={"workspace_id": workspace_id}).json()[
                "archived_at"
            ]
            is None
        )


def test_bulk_archive_endpoint_rejects_duplicate_selection(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "bulk-api", "name": "Bulk API"}
        ).json()["id"]
        job_id = client.post(
            "/api/v1/evaluations", json=evaluation_commit(workspace_id).model_dump(mode="json")
        ).json()["job_posting_id"]

        duplicate = client.post(
            "/api/v1/jobs/archive",
            params={"workspace_id": workspace_id},
            json={"job_posting_ids": [job_id, job_id]},
        )
        assert duplicate.status_code == 422
        too_many = client.post(
            "/api/v1/jobs/archive",
            params={"workspace_id": workspace_id},
            json={"job_posting_ids": [f"job-{index}" for index in range(251)]},
        )
        assert too_many.status_code == 422
        archived = client.post(
            "/api/v1/jobs/archive",
            params={"workspace_id": workspace_id},
            json={"job_posting_ids": [job_id]},
        )
        assert archived.status_code == 200
        assert archived.json() == {
            "job_posting_ids": [job_id],
            "requested_count": 1,
            "archived_count": 1,
            "already_archived_count": 0,
        }


@pytest.mark.parametrize("archived", [False, True])
def test_personal_closing_date_edits_preserve_publisher_evidence_and_manual_review(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archived: bool
) -> None:
    database = Database(durable_database_url)
    seeded = workspace_with_committed_evaluation(database, tmp_path, "closing-edit")
    workspace_id, job_id = seeded.workspace_id, seeded.detail.job_posting_id
    moment = datetime(2026, 10, 8, 12, tzinfo=UTC)
    monkeypatch.setattr("clearscout.application.job_search.commands.utc_now", lambda: moment)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        review = client.put(
            f"/api/v1/jobs/{job_id}/manual-review",
            params={"workspace_id": workspace_id},
            json={
                "priority": "high",
                "note": "Check the instructions.",
                "recorded_deadline": "Friday at 5 pm",
            },
        )
        assert review.status_code == 200, review.text
        before = review.json()
        if archived:
            response = client.post(
                f"/api/v1/jobs/{job_id}/archive", params={"workspace_id": workspace_id}
            )
            assert response.status_code == 200
        edited = client.put(
            f"/api/v1/jobs/{job_id}/closing-date",
            params={"workspace_id": workspace_id},
            json={"closing_date_override": "2026-11-02"},
            headers={"X-Correlation-ID": "personal-date"},
        )
        assert edited.status_code == 200, edited.text
        after = edited.json()
        assert after["closing_date_override"] == "2026-11-02"
        for field in (
            "publisher_closing_date",
            "recorded_deadline",
            "manual_reviewed_at",
            "manual_note",
            "evaluation_id",
            "observation_count",
            "decision",
            "application_status",
        ):
            assert after[field] == before[field]
        assert (after["archived_at"] is not None) is archived
        reloaded = client.get(f"/api/v1/jobs/{job_id}", params={"workspace_id": workspace_id})
        assert reloaded.json()["closing_date_override"] == "2026-11-02"
        rows = client.get(
            "/api/v1/jobs", params={"workspace_id": workspace_id, "include_archived": archived}
        ).json()["items"]
        assert rows[0]["closing_date_override"] == "2026-11-02"
        reset = client.put(
            f"/api/v1/jobs/{job_id}/closing-date",
            params={"workspace_id": workspace_id},
            json={"closing_date_override": None},
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["closing_date_override"] is None
        assert reset.json()["recorded_deadline"] == "Friday at 5 pm"
    with database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.closing_date_override is None
        assert job.closing_date_override_updated_at == moment
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id == job_id, AuditEvent.action == "job.closing_date_changed")
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ).all()
        assert len(events) == 2
        assert {tuple(sorted(event.details.items())) for event in events} == {
            tuple(
                sorted(
                    {
                        "old_closing_date_override": None,
                        "closing_date_override": "2026-11-02",
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "old_closing_date_override": "2026-11-02",
                        "closing_date_override": None,
                    }.items()
                )
            ),
        }
        assert any(event.correlation_id == "personal-date" for event in events)
        assert session.scalar(select(BackgroundTask)) is None
    database.dispose()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"closing_date_override": "2026-11-02T00:00:00Z"},
        {"closing_date_override": 1793577600},
        {"closing_date_override": "2026-02-30"},
        {"closing_date_override": "20261102"},
        {"closing_date_override": True},
    ],
)
def test_personal_closing_date_refuses_missing_or_non_date_values(
    durable_database_url: str, tmp_path: Path, body: dict[str, object]
) -> None:
    database = Database(durable_database_url)
    seeded = workspace_with_committed_evaluation(database, tmp_path, "invalid-date")
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.put(
            f"/api/v1/jobs/{seeded.detail.job_posting_id}/closing-date",
            params={"workspace_id": seeded.workspace_id},
            json=body,
        )
    assert response.status_code == 422, response.text
    assert response.json()["type"] == "urn:clearscout:problem:request-validation"
    with database.session() as session:
        job = session.get(JobPosting, seeded.detail.job_posting_id)
        assert job is not None and job.closing_date_override_updated_at is None
    database.dispose()


@pytest.mark.parametrize("target", ["missing", "other_workspace", "archived_workspace"])
def test_personal_closing_date_requires_an_owned_job_in_an_active_workspace(
    durable_database_url: str, tmp_path: Path, target: str
) -> None:
    database = Database(durable_database_url)
    seeded = workspace_with_committed_evaluation(database, tmp_path, "owned-date")
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace_id, job_id = seeded.workspace_id, seeded.detail.job_posting_id
        if target == "missing":
            job_id = "missing-job"
        elif target == "other_workspace":
            workspace_id = client.post(
                "/api/v1/workspaces", json={"slug": "other", "name": "Other"}
            ).json()["id"]
        else:
            with database.session() as session:
                workspace = session.get(Workspace, workspace_id)
                assert workspace is not None
                workspace.archived_at = datetime(2026, 10, 8, tzinfo=UTC)
        response = client.put(
            f"/api/v1/jobs/{job_id}/closing-date",
            params={"workspace_id": workspace_id},
            json={"closing_date_override": "2026-11-02"},
        )
    assert response.status_code == 404, response.text
    with database.session() as session:
        job = session.get(JobPosting, seeded.detail.job_posting_id)
        assert job is not None and job.closing_date_override_updated_at is None
    database.dispose()


@pytest.mark.parametrize("personal_date", [date(2026, 11, 2), None])
@pytest.mark.parametrize("regional", [False, True])
def test_free_tracking_transfers_the_family_date_and_replay_preserves_later_edits(
    durable_database_url: str, tmp_path: Path, personal_date: date | None, regional: bool
) -> None:
    database = Database(durable_database_url)
    moment = datetime(2026, 10, 8, 12, tzinfo=UTC)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "free-date", "name": "Free date"}
        ).json()["id"]
        pending_id = seed_pending_discovery(
            database, workspace_id, employer="Example", created_at=moment
        )
        with database.session() as session:
            head = session.get(PendingDiscovery, pending_id)
            assert head is not None
            head.closing_date_override = personal_date
            head.closing_date_override_updated_at = moment
        tracked_pending_id = pending_id
        if regional:
            tracked_pending_id = seed_pending_discovery(
                database, workspace_id, employer="Example", created_at=moment
            )
            with database.session() as session:
                variant = session.get(PendingDiscovery, tracked_pending_id)
                assert variant is not None
                variant.regional_variant_of = pending_id
        path = f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{tracked_pending_id}/track"
        tracked = client.post(path)
        assert tracked.status_code == 200, tracked.text
        expected = personal_date.isoformat() if personal_date else None
        assert tracked.json()["closing_date_override"] == expected
        job_id = tracked.json()["job_posting_id"]
        with database.session() as session:
            job = session.get(JobPosting, job_id)
            assert job is not None
            assert job.closing_date_override_updated_at == moment
            head = session.get(PendingDiscovery, pending_id)
            assert head is not None and head.archived_at is not None
            assert head.closing_date_override == personal_date
            assert head.closing_date_override_updated_at == moment
        edited = client.put(
            f"/api/v1/jobs/{job_id}/closing-date",
            params={"workspace_id": workspace_id},
            json={"closing_date_override": "2026-12-01"},
        )
        assert edited.status_code == 200, edited.text
        replay = client.post(path)
        assert replay.status_code == 200, replay.text
        assert replay.json()["closing_date_override"] == "2026-12-01"
        assert replay.json()["observation_count"] == tracked.json()["observation_count"]
    database.dispose()


@pytest.mark.parametrize(
    ("target_date", "target_edited"),
    [
        (None, False),
        (date(2026, 12, 1), True),
        (None, True),
    ],
)
def test_free_tracking_keeps_a_prior_target_edit_including_a_reset(
    durable_database_url: str, tmp_path: Path, target_date: date | None, target_edited: bool
) -> None:
    database = Database(durable_database_url)
    seeded = workspace_with_committed_evaluation(
        database,
        tmp_path,
        "target-date",
        employer="Example",
        requisition_id="REQ-1",
        canonical_url="https://boards.greenhouse.io/example/jobs/1",
        discovery_url="https://boards.greenhouse.io/example/jobs/1",
    )
    pending_moment = datetime(2026, 10, 8, 12, tzinfo=UTC)
    target_moment = datetime(2026, 10, 7, 12, tzinfo=UTC)
    pending_id = seed_pending_discovery(
        database, seeded.workspace_id, employer="Example", created_at=pending_moment
    )
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        job = session.get(JobPosting, seeded.detail.job_posting_id)
        assert pending is not None and job is not None
        pending.closing_date_override = date(2026, 11, 2)
        pending.closing_date_override_updated_at = pending_moment
        job.closing_date_override = target_date
        job.closing_date_override_updated_at = target_moment if target_edited else None
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.post(
            f"/api/v1/workspaces/{seeded.workspace_id}/pending-discoveries/{pending_id}/track"
        )
    assert response.status_code == 200, response.text
    expected = target_date if target_edited else date(2026, 11, 2)
    assert response.json()["closing_date_override"] == (expected.isoformat() if expected else None)
    with database.session() as session:
        job = session.get(JobPosting, seeded.detail.job_posting_id)
        assert job is not None
        assert job.closing_date_override_updated_at == (
            target_moment if target_edited else pending_moment
        )
    database.dispose()
