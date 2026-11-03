"""Archiving a job hides it from the ledger without losing anything beneath it."""

from __future__ import annotations

from pathlib import Path

import pytest

from clearscout.application import job_search
from clearscout.application.contracts import ApplicationEventInput
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.job_search.models import JobListQuery
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import ApplicationStatus
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import NOW, evaluation_commit


def test_job_archive_is_reversible_audited_and_preserves_history(
    durable_database: Database, tmp_path: Path
) -> None:
    workspaces = WorkspaceService(durable_database, runtime_storage(durable_database))
    workspace = workspaces.create(slug="archive", name="Archive")
    other = workspaces.create(slug="other", name="Other")
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    detail = job_search.commit_evaluation(
        durable_database, artifacts, evaluation_commit(workspace.id)
    )
    job_id = str(detail.job_posting_id)
    with durable_database.engine.connect() as connection:
        before = {
            table: int(
                connection.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one()  # noqa: S608
            )
            for table in ("applications", "application_events", "artifacts", "job_evaluations")
        }

    with pytest.raises(NotFoundError, match="this workspace"):
        job_search.archive_job(durable_database, job_id, other.id)
    archived = job_search.archive_job(
        durable_database, job_id, workspace.id, correlation_id="archive-1"
    )
    assert archived.archived is True and archived.changed is True
    assert job_search.archive_job(durable_database, job_id, workspace.id).changed is False
    assert job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))[1] == 0
    assert job_search.workspace_summary(durable_database, workspace.id).total_jobs == 0
    # Reading an archived job is how restoring it is reached; every write beneath
    # it stays refused.
    archived_items, archived_total = job_search.list_jobs(
        durable_database, JobListQuery(workspace_id=workspace.id, include_archived=True)
    )
    assert archived_total == 1
    assert archived_items[0].job_posting_id == job_id
    assert archived_items[0].archived_at is not None
    assert job_search.job_detail(durable_database, job_id, workspace.id).archived_at is not None
    with pytest.raises(NotFoundError):
        job_search.append_application_event(
            durable_database,
            job_id,
            ApplicationEventInput(
                status=ApplicationStatus.APPLIED,
                occurred_at=NOW,
                notes="Must not append beneath an archived job.",
            ),
            workspace_id=workspace.id,
            idempotency_key="archived-write",
        )
    with pytest.raises(NotFoundError, match="active workspace"):
        job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace.id,
                rationale="A changed evaluation must not revive an archived job.",
                supersedes_evaluation_id=detail.evaluation_id,
            ),
        )

    restored = job_search.unarchive_job(
        durable_database, job_id, workspace.id, correlation_id="unarchive-1"
    )
    assert restored.archived is False and restored.changed is True
    restored_detail = job_search.job_detail(durable_database, job_id, workspace.id)
    assert restored_detail.evaluation_id == detail.evaluation_id
    assert restored_detail.archived_at is None
    assert job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))[1] == 1
    with durable_database.engine.connect() as connection:
        after = {
            table: int(
                connection.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one()  # noqa: S608
            )
            for table in before
        }
        actions = connection.exec_driver_sql(
            "SELECT action FROM audit_events WHERE entity_id = ? ORDER BY created_at, id",
            (job_id,),
        ).fetchall()
    assert after == before
    assert [row[0] for row in actions][-2:] == ["job.archived", "job.unarchived"]


def test_bulk_archive_is_atomic_audited_and_idempotent(
    durable_database: Database, tmp_path: Path
) -> None:
    workspaces = WorkspaceService(durable_database, runtime_storage(durable_database))
    workspace = workspaces.create(slug="bulk-archive", name="Bulk Archive")
    other = workspaces.create(slug="bulk-other", name="Bulk Other")
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    first = job_search.commit_evaluation(
        durable_database, artifacts, evaluation_commit(workspace.id)
    )
    second = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Globex",
            title="Data Director",
            requisition_id="REQ-43",
            canonical_url="https://example.test/jobs/43",
            discovery_url="https://example.test/jobs/43",
        ),
    )
    foreign = job_search.commit_evaluation(durable_database, artifacts, evaluation_commit(other.id))
    job_ids = [str(first.job_posting_id), str(second.job_posting_id)]

    with pytest.raises(ValidationError, match="1 to 250"):
        job_search.archive_jobs(
            durable_database,
            [f"job-{index}" for index in range(251)],
            workspace.id,
        )
    with pytest.raises(NotFoundError, match="unknown jobs"):
        job_search.archive_jobs(
            durable_database,
            [job_ids[0], str(foreign.job_posting_id)],
            workspace.id,
        )
    assert job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))[1] == 2

    archived = job_search.archive_jobs(
        durable_database, job_ids, workspace.id, correlation_id="bulk-archive-1"
    )
    assert archived.job_posting_ids == job_ids
    assert archived.requested_count == 2
    assert archived.archived_count == 2
    assert archived.already_archived_count == 0
    repeated = job_search.archive_jobs(durable_database, job_ids, workspace.id)
    assert repeated.archived_count == 0
    assert repeated.already_archived_count == 2
    with durable_database.engine.connect() as connection:
        audits = [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT entity_id, correlation_id FROM audit_events "
                "WHERE action = 'job.archived' AND entity_id IN (?, ?) ORDER BY entity_id",
                tuple(job_ids),
            )
        ]
    assert audits == sorted((job_id, "bulk-archive-1") for job_id in job_ids)
