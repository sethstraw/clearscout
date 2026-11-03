"""The batch operation: its payload rules, its durable run, and what it bills."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from clearscout.application.documents import DocumentService
from clearscout.application.failures import ValidationError
from clearscout.application.prompts import packaged
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.application.tailoring.workflows import BatchOutput, WorkflowService
from clearscout.application.tasks import TaskQueueService, validate_task_payload
from clearscout.application.workspaces.prompt_library import PromptLibraryService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    Artifact,
    BatchMember,
    BatchRun,
    Employer,
    JobPosting,
    ModelCall,
    PostingObservation,
    RunPromptVersion,
)
from clearscout.shared.costs import CallManifest, Ledger, Usage
from tests.support.artifacts import runtime_storage
from tests.support.runners import ScriptedPipelineRunner
from tests.support.scenarios import studio_workspace


def test_persist_batch_refuses_a_screening_count_that_does_not_match_its_inputs(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    with durable_database.session() as session:
        employer = session.scalar(select(Employer).where(Employer.workspace_id == workspace_id))
        assert employer is not None
        second = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="No body",
            normalized_title="no body",
            first_seen_at=datetime.now(UTC),
        )
        session.add(second)
        session.flush()
        second_job_id = second.id

    with pytest.raises(ValidationError, match="one screening per input"):
        service.persist_batch(
            workspace_id=workspace_id,
            job_ids=(job_id, second_job_id),
            task_id="unused",
            output=BatchOutput(report=b"", record=b"", screenings=()),
            priorities=(0, 0),
            notes=("", ""),
        )


def test_a_batch_task_records_its_members_prompt_lineage_and_a_report_every_job_reaches(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, first_job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    with durable_database.session() as session:
        first_job = session.get(JobPosting, first_job_id)
        assert first_job is not None
        second_job = JobPosting(
            workspace_id=workspace_id,
            employer_id=first_job.employer_id,
            title="Platform Manager",
            normalized_title="platform manager",
            first_seen_at=datetime.now(UTC),
        )
        session.add(second_job)
        session.flush()
        posting = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=second_job.id,
            tailoring_run_id=None,
            logical_type="posting.body.text",
            mime_type="text/plain",
            content=b"Acme needs a platform manager with Python experience.",
            provenance={"source": "test"},
            author_type="system",
        )
        session.add(
            PostingObservation(
                job_posting_id=second_job.id,
                observed_at=datetime.now(UTC),
                posting_status="active",
                body_artifact_id=posting.id,
            )
        )
        second_job_id = second_job.id

    queue = TaskQueueService(durable_database)
    PromptLibraryService(durable_database).save(
        workspace_id,
        "jobspec_system",
        packaged("jobspec_system") + "\nBatch-specific instruction.\n",
    )
    runner = ScriptedPipelineRunner()
    worker = Worker(
        durable_database,
        worker_id="workflow-more",
        handlers=default_handlers(
            durable_database, artifact_root=artifact_root, workflow_runner=runner
        ),
    )
    batch_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.batch",
        payload={
            "job_posting_ids": [first_job_id, second_job_id],
            "priorities": [2, 1],
            "notes": ["first", "second"],
        },
        idempotency_key="batch",
    )

    assert worker.run_once()
    batch_result = queue.get(str(batch_task.id))
    assert batch_result.status == "completed"
    assert batch_result.progress["job_posting_ids"] == [first_job_id, second_job_id]
    assert runner.last_stored_names == (
        StoredPostingName(title="Platform Lead", employer="Acme"),
        StoredPostingName(title="Platform Manager", employer="Acme"),
    )
    with durable_database.session() as session:
        batch_run = session.scalar(select(BatchRun))
        assert batch_run is not None
        members = session.scalars(select(BatchMember)).all()
        assert {member.job_posting_id for member in members} == {first_job_id, second_job_id}
        lineage = session.scalar(
            select(RunPromptVersion).where(RunPromptVersion.batch_run_id == batch_run.id)
        )
        assert lineage is not None
        # A comparison is about every job in it, so every job can reach it: the
        # ledger tells the user to open any selected job's documents.
        filed = session.scalars(
            select(Artifact.related_entity_id).where(
                Artifact.logical_type == "batch.report.markdown",
                Artifact.related_entity_type == "job_posting",
            )
        ).all()
        assert set(filed) == {first_job_id, second_job_id}


def test_a_batch_payload_defaults_its_priorities_and_notes_to_one_per_job() -> None:
    defaulted = validate_task_payload(
        "workflow.batch",
        {"job_posting_ids": ["job-a", "job-b"]},
    )

    assert defaulted["priorities"] == [0, 0]
    assert defaulted["notes"] == ["", ""]


@pytest.mark.parametrize(
    ("task_type", "payload", "message"),
    [
        ("workflow.batch", {"job_posting_ids": ["job-a", "job-a"]}, "unique"),
        (
            "workflow.batch",
            {"job_posting_ids": ["job-a", "job-b"], "priorities": [1]},
            "priorities must match",
        ),
        (
            "workflow.batch",
            {"job_posting_ids": ["job-a", "job-b"], "notes": ["one"]},
            "notes must match",
        ),
        ("posting.refresh_batch", {"job_posting_ids": ["job-a", "job-a"]}, "unique"),
    ],
)
def test_a_batch_payload_names_a_distinct_job_set_with_matching_columns(
    task_type: str, payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_task_payload(task_type, payload)


@pytest.mark.parametrize("failure", ["cancel_after_result", "stage_failure", "mismatched_result"])
def test_batch_keeps_billing_when_cancelled_or_a_later_posting_fails(
    durable_database, tmp_path, failure
):
    from clearscout.application.tailoring.workflows import ScreenOutput

    workspace_id, job_id, other_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    documents = DocumentService(
        durable_database, runtime_storage(durable_database, tmp_path / "artifacts")
    )
    with durable_database.session() as session:
        first = session.get(JobPosting, job_id)
        other = JobPosting(
            workspace_id=workspace_id,
            employer_id=first.employer_id,
            title="Engineering Lead",
            normalized_title="engineering lead",
            first_seen_at=datetime.now(UTC),
        )
        session.add(other)
        session.flush()
        body = documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=other.id,
            tailoring_run_id=None,
            logical_type="posting.body.text",
            mime_type="text/plain",
            content=b"Engineering leadership role.",
            provenance={"source": "test"},
            author_type="system",
        )
        session.add(
            PostingObservation(
                job_posting_id=other.id,
                observed_at=datetime.now(UTC),
                posting_status="active",
                body_artifact_id=body.id,
            )
        )
        other_id = other.id
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.batch",
        payload={"job_posting_ids": [job_id, other_id]},
        idempotency_key=failure,
    )

    class InterruptedRunner(ScriptedPipelineRunner):
        def compare(self, *args, **kwargs):
            ledger = Ledger()
            ledger.record(CallManifest("match", "model-a", Usage(input_tokens=20)))
            if "ledgers" in kwargs:
                kwargs["ledgers"].append(ledger)
            if failure == "stage_failure":
                raise RuntimeError("second posting failed")
            if failure == "mismatched_result":
                return BatchOutput(
                    report=b"comparison",
                    record=b"{}",
                    screenings=(ScreenOutput(b"one", "pursue", ledger),),
                )
            queue.request_cancellation(str(task.id))
            return BatchOutput(
                report=b"comparison",
                record=b"{}",
                screenings=(
                    ScreenOutput(b"one", "pursue", ledger),
                    ScreenOutput(b"two", "pursue", Ledger()),
                ),
            )

    worker = Worker(
        durable_database,
        worker_id="batch-billing",
        handlers=default_handlers(
            durable_database,
            artifact_root=tmp_path / "artifacts",
            workflow_runner=InterruptedRunner(),
        ),
    )
    assert worker.run_once()
    assert queue.get(str(task.id)).status == (
        "cancelled" if failure == "cancel_after_result" else "failed"
    )
    with durable_database.session() as session:
        assert (
            len(session.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()) == 1
        )
        if failure == "cancel_after_result":
            assert session.scalar(select(BatchRun).where(BatchRun.task_id == task.id)) is not None
