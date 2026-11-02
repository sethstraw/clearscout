"""Workflows executed as durable tasks: the registry, the worker, and the discovery phases."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tailorcv.application.documents.approvals import ApprovalService
from tailorcv.application.failures import ValidationError
from tailorcv.application.master_cv import cv
from tailorcv.application.tailoring.workflows import (
    BatchOutput,
    TailoringHandler,
    TailorOutput,
    WorkflowService,
)
from tailorcv.application.tasks import TaskQueueService, validate_task_payload
from tailorcv.application.workspaces.records import WorkspaceRecords
from tailorcv.bootstrap.api import create_app
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.domain import TailoringOutcome
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    EvaluationFactor,
    JobEvaluation,
    MasterCvVersion,
    ReviewProposal,
    TailoringRun,
)
from tailorcv.infrastructure.web_fetch.company_research import ResearchSource
from tailorcv.shared.costs import Ledger
from tests.support.artifacts import runtime_storage
from tests.support.runners import ScriptedPipelineRunner, serialized_artifacts
from tests.support.scenarios import seed_reusable_tailoring_run, studio_workspace

ANSWER = "I led two engineers and deployed Python systems in production."
CONFIRMED_POINTS = [
    "the exact factual wording",
    "the metrics, including that there are none",
    "the ownership level",
    "the scope",
    "the evidence category",
    "the job gaps it relates to",
    "that its source is user-confirmed discovery",
    "what remains ambiguous",
]


class ExplodingResearchFetcher:
    def fetch(
        self, _careers_url: str, *, posting_urls: Collection[str] = ()
    ) -> tuple[ResearchSource, ...]:
        raise AssertionError("reuse must happen before employer research")


class ExplodingTailoringRunner:
    def tailor(self, *_args: Any, **_kwargs: Any) -> TailorOutput:
        raise AssertionError("reuse must happen before the pipeline runner")

    def compare(self, *_args: Any, **_kwargs: Any) -> BatchOutput:
        raise AssertionError("the tailoring test must not compare jobs")


def test_registered_workflows_execute_through_injected_existing_pipeline_boundary(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    assert (
        validate_task_payload("workflow.tailor", {"job_posting_id": job_id})["cover_letter"] is True
    )

    queue = TaskQueueService(durable_database)
    runner = ScriptedPipelineRunner()
    worker = Worker(
        durable_database,
        worker_id="studio",
        handlers=default_handlers(
            durable_database, artifact_root=artifact_root, workflow_runner=runner
        ),
    )
    tailor_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={
            "job_posting_id": job_id,
            "cover_letter": True,
            "regenerate": False,
        },
        idempotency_key="tailor",
    )
    assert worker.run_once()
    tailored = queue.get(str(tailor_task.id))
    assert tailored.status == "completed"
    assert tailored.progress["outcome"] == "passed"
    assert tailored.progress["message"] == "Checks passed; the critic did not read the resume."
    with durable_database.session() as session:
        run = session.scalar(select(TailoringRun).where(TailoringRun.task_id == tailor_task.id))
        assert run is not None
        resume = session.scalar(
            select(Artifact).where(
                Artifact.tailoring_run_id == run.id,
                Artifact.logical_type == "resume.markdown",
            )
        )
        assert resume is not None
        assert resume.version_number == 1
        assert resume.provenance["prompt_suite"] == "prompt-a"
        assert resume.provenance["line_citations"][0]["source_ids"] == ["E1"]
        research = session.scalar(
            select(Artifact).where(
                Artifact.tailoring_run_id == run.id,
                Artifact.logical_type == "company_research.posting.text",
            )
        )
        assert research is not None
        assert research.provenance["source_id"] == "company.posting"
        assert [item.id for item in runner.last_research] == ["company.posting"]
        proposal = session.scalar(
            select(ReviewProposal).where(ReviewProposal.tailoring_run_id == run.id)
        )
        assert proposal is not None
        proposal_id = proposal.id
        proposal_digest = proposal.digest
        run_id = run.id

    workflow_service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), runner
    )
    with pytest.raises(ValidationError, match="explicit decision"):
        workflow_service.approval_manifest(workspace_id, run_id)
    approval_service = ApprovalService(durable_database)
    approval_service.decide(
        proposal_id,
        workspace_id,
        proposal_digest=proposal_digest,
        decision="accept",
        replacement_text=None,
        replacement_source_ids=[],
    )
    approval_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.approve",
        payload={
            "job_posting_id": job_id,
            "approval_tailoring_run_id": run_id,
            "cover_letter": True,
            "regenerate": False,
        },
        idempotency_key="approve",
    )
    assert worker.run_once()
    assert queue.get(str(approval_task.id)).status == "completed"
    assert runner.last_approval is not None
    assert runner.last_approval["proposals"][0]["decision"] == "accept"
    approval_service.decide(
        proposal_id,
        workspace_id,
        proposal_digest=proposal_digest,
        decision="replace",
        replacement_text="Built systems with reviewed wording.",
        replacement_source_ids=["E1"],
    )
    replacement = workflow_service.approval_manifest(workspace_id, run_id)["proposals"][0]
    assert replacement["decision"] == "reject"
    assert replacement["replacement"]["source_ids"] == ["E1"]
    with TestClient(
        create_app(durable_database.url, artifact_root), base_url="http://localhost"
    ) as client:
        review_response = client.get(
            f"/api/v1/tailoring-runs/{run_id}/review",
            params={"workspace_id": workspace_id},
        )
        assert review_response.status_code == 200
        decision_response = client.post(
            f"/api/v1/review-proposals/{proposal_id}/decisions",
            json={
                "workspace_id": workspace_id,
                "proposal_digest": proposal_digest,
                "decision": "reject",
                "replacement_text": None,
                "replacement_source_ids": [],
            },
        )
        assert decision_response.status_code == 200


def test_a_queued_tailoring_run_reuses_an_exact_run_without_research_or_provider_work(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    persisted = seed_reusable_tailoring_run(
        durable_database, service, workspace_id, job_id, master_version_id
    )
    queue = TaskQueueService(durable_database)
    reuse_task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={
            "job_posting_id": job_id,
            "cover_letter": True,
            "regenerate": False,
        },
        idempotency_key="preflight-reuse",
    )
    reuse_service = WorkflowService(
        durable_database,
        runtime_storage(durable_database, artifact_root),
        ExplodingTailoringRunner(),
        research_fetcher=ExplodingResearchFetcher(),
    )
    worker = Worker(
        durable_database,
        worker_id="reuse",
        handlers={"workflow.tailor": TailoringHandler(reuse_service)},
    )

    assert worker.run_once()

    completed = queue.get(str(reuse_task.id))
    assert completed.status == "completed"
    assert completed.progress == {
        "reuse_available": True,
        "tailoring_run_id": persisted["tailoring_run_id"],
        "artifact_ids": persisted["artifact_ids"],
        "cost_usd": 0,
    }
    with durable_database.session() as session:
        assert len(session.scalars(select(TailoringRun)).all()) == 1


@pytest.mark.parametrize(
    ("outcome", "resume_draws", "message"),
    [
        (
            TailoringOutcome.PASSED,
            [{"alignment": 4}],
            "Checks passed and the critic read the resume.",
        ),
        (TailoringOutcome.PASSED, [], "Checks passed; the critic did not read the resume."),
        (TailoringOutcome.CHECKS_FAILED, [{"alignment": 2}], None),
    ],
)
def test_a_stored_run_says_whether_the_critic_read_the_resume(
    durable_database: Database,
    tmp_path: Path,
    outcome: TailoringOutcome,
    resume_draws: list[dict[str, int]],
    message: str | None,
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="critic-message",
    )
    persisted = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(task.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(
                {"resume.markdown": ("text/markdown", b"# Candidate\n")},
                manifest={
                    "inputs": {},
                    "run": {"critic_draws": {"resume": resume_draws, "cover_letter": []}},
                },
                review={},
            ),
            outcome=outcome,
            ledger=Ledger(),
        ),
        started_at=datetime.now(UTC),
    )

    assert persisted["outcome"] == outcome
    assert persisted.get("message") == message


def _evaluated_studio(
    database: Database, tmp_path: Path
) -> tuple[str, str, TaskQueueService, Worker]:
    """Seed the studio workspace with one gap-bearing evaluation and a worker to answer it."""
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        database, tmp_path / "workspace", artifact_root
    )
    with database.session() as session:
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            decision="investigate",
            rationale="One synthetic gap.",
            payload_sha256="b" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        session.add(
            EvaluationFactor(
                evaluation_id=evaluation.id,
                factor_type="gap",
                position=1,
                text="Python leadership",
            )
        )
        evaluation_id = evaluation.id
    WorkspaceRecords.for_database(database).notes.create_note(
        workspace_id=workspace_id,
        entity_type="evaluation",
        entity_id=evaluation_id,
        title="Evaluation note",
        body="Not evidence",
    )
    worker = Worker(
        database,
        worker_id="workflow-more",
        handlers=default_handlers(
            database, artifact_root=artifact_root, workflow_runner=ScriptedPipelineRunner()
        ),
    )
    return workspace_id, job_id, TaskQueueService(database), worker


def _answer_the_gap(workspace_id: str, job_id: str, queue: TaskQueueService, worker: Worker) -> Any:
    """Run the answer phase and return its completed task."""
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.discovery",
        payload={"job_posting_id": job_id, "replies": [ANSWER]},
        idempotency_key="discover-answer",
    )
    assert worker.run_once()
    return queue.get(str(task.id))


def test_discovery_answers_a_gap_and_places_it_against_a_master_cv_role(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, queue, worker = _evaluated_studio(durable_database, tmp_path)

    discovered = _answer_the_gap(workspace_id, job_id, queue, worker)

    assert discovered.status == "completed"
    assert discovered.progress["answered"] is True


def test_discovery_previews_the_wording_it_would_write(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, queue, worker = _evaluated_studio(durable_database, tmp_path)
    discovered = _answer_the_gap(workspace_id, job_id, queue, worker)
    role_key = discovered.progress["placements"]["roles"][0]["key"]

    preview = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.discovery",
        payload={
            "job_posting_id": job_id,
            "replies": [ANSWER],
            "proposal": {
                "text": ANSWER,
                "scope": "Python systems",
                "role_key": role_key,
                "section": "",
                "ambiguities": [],
            },
        },
        idempotency_key="discover-preview",
    )
    assert worker.run_once()

    previewed = queue.get(str(preview.id))
    assert previewed.status == "completed"
    assert previewed.progress["proposal"][0].strip().startswith("wording:")


def test_discovery_writes_a_confirmed_claim_to_the_canonical_master_cv_only(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, queue, worker = _evaluated_studio(durable_database, tmp_path)
    discovered = _answer_the_gap(workspace_id, job_id, queue, worker)
    role_key = discovered.progress["placements"]["roles"][0]["key"]

    confirmed = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.discovery",
        payload={
            "job_posting_id": job_id,
            "replies": [ANSWER],
            "proposal": {
                "text": ANSWER,
                "scope": "Python systems",
                "role_key": role_key,
                "section": "",
                "ambiguities": [],
            },
            "confirmed_points": CONFIRMED_POINTS,
            "write_confirmed": True,
        },
        idempotency_key="discover-write",
    )
    assert worker.run_once()

    written = queue.get(str(confirmed.id))
    assert written.status == "completed"
    assert written.progress["written"]["entry_ids"]
    assert written.progress["written"]["master_cv_version_id"]
    immutable_source = tmp_path / "workspace" / "master.md"
    assert ANSWER not in immutable_source.read_text(encoding="utf-8")
    canonical = tmp_path / "workspace" / "master-cv" / "CV_Master.md"
    assert ANSWER in canonical.read_text(encoding="utf-8")
    with durable_database.session() as session:
        versions = session.scalars(select(MasterCvVersion)).all()
        assert len(versions) == 2


def test_discovery_parses_one_master_cv_across_proposal_and_write_phases(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_root = tmp_path / "workspace"
    workspace_id, job_id, _, _ = studio_workspace(durable_database, workspace_root, artifact_root)
    role_key = cv.parse_cv(workspace_root / "master.md").roles[0].key
    parse_calls: list[Path] = []

    def count_parse(path: Path) -> cv.CvInventory:
        parse_calls.append(path)
        return cv.parse_cv(path)

    monkeypatch.setattr("tailorcv.application.tailoring.workflows.handlers.parse_cv", count_parse)
    queue = TaskQueueService(durable_database)
    worker = Worker(
        durable_database,
        worker_id="discovery-cv",
        handlers=default_handlers(durable_database, artifact_root=artifact_root),
    )
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.discovery",
        payload={
            "job_posting_id": job_id,
            "subject": "Python leadership",
            "replies": [ANSWER],
            "proposal": {
                "text": ANSWER,
                "scope": "Python systems",
                "role_key": role_key,
                "section": "",
                "ambiguities": [],
            },
            "confirmed_points": CONFIRMED_POINTS,
            "write_confirmed": True,
        },
        idempotency_key="discovery-cv",
    )

    assert worker.run_once()
    assert queue.get(str(task.id)).status == "completed"
    assert len(parse_calls) == 1


@pytest.mark.parametrize(
    ("task_type", "payload", "message"),
    [
        (
            "workflow.discovery",
            {"job_posting_id": "job-a", "confirmed_points": [CONFIRMED_POINTS[0]]},
            "require a proposal",
        ),
        (
            "workflow.discovery",
            {
                "job_posting_id": "job-a",
                "proposal": {"text": "claim", "role_key": "", "section": ""},
            },
            "exactly one",
        ),
        (
            "workflow.discovery",
            {
                "job_posting_id": "job-a",
                "proposal": {"text": "claim", "role_key": "role-1", "section": ""},
                "write_confirmed": True,
            },
            "confirmed_points",
        ),
    ],
)
def test_a_discovery_payload_names_one_placement_and_confirms_before_it_writes(
    task_type: str, payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_task_payload(task_type, payload)
