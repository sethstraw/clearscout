"""What the workspace summary puts in front of the user, and what it holds back."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from tailorcv.application import job_search
from tailorcv.application.contracts import (
    ApplicationEventInput,
    EvaluationCommit,
    EvaluationScores,
    ScoreComponentInput,
)
from tailorcv.application.failures import NotFoundError
from tailorcv.application.job_search.models import JobListQuery, ManualJobReviewInput
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.domain import ApplicationStatus, EvaluationDecision
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PostingObservation
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import (
    NOW,
    employer_board,
    evaluation_commit,
    tracked_board_posting,
)


def _posting(workspace_id: str, slug: str, **changes: object) -> EvaluationCommit:
    """Build one distinct posting so several can share a workspace."""
    return evaluation_commit(
        workspace_id,
        employer=f"Employer {slug}",
        requisition_id=f"REQ-{slug}",
        canonical_url=f"https://example.test/jobs/{slug}",
        discovery_url=f"https://example.test/jobs/{slug}",
        **changes,
    )


def _observe_closure(database: Database, job_posting_id: str) -> None:
    """Record the publisher closing a posting after its evaluation."""
    with database.session() as session:
        session.add(
            PostingObservation(
                job_posting_id=job_posting_id,
                observed_at=NOW + timedelta(days=1),
                posting_status="closed",
                origin="test",
            )
        )


def test_the_command_centre_leads_with_what_can_be_acted_on_today(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="acting", name="Acting"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    pipeline = {
        ApplicationStatus.PACKAGE_READY: "Drafted",
        ApplicationStatus.APPLIED: "Submitted",
        ApplicationStatus.INTERVIEWING: "Scheduled",
        ApplicationStatus.OFFER: "Offered",
    }
    staged: dict[str, str] = {}
    for index, (status, note) in enumerate(pipeline.items()):
        job = job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace.id,
                employer=f"Employer {index}",
                requisition_id=f"REQ-{index}",
                canonical_url=f"https://example.test/jobs/{index}",
                discovery_url=f"https://example.test/jobs/{index}",
            ),
        )
        staged[status.value] = job.job_posting_id
        job_search.append_application_event(
            durable_database,
            job.job_posting_id,
            ApplicationEventInput(status=status, occurred_at=NOW + timedelta(hours=1), notes=note),
            idempotency_key=f"move-{index}",
        )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert summary.actionable_applications == len(pipeline)
    assert [item.job_posting_id for item in summary.actionable] == [
        staged["offer"],
        staged["interviewing"],
        staged["applied"],
        staged["package_ready"],
    ]


def test_a_single_evaluated_job_summarizes_as_an_empty_funnel(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="empty-funnel", name="Empty funnel"
    )
    created = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(workspace.id),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert summary.total_jobs == 1
    assert summary.actionable_applications == 0
    assert summary.application_funnel.model_dump() == {
        "package_ready": 0,
        "applied": 0,
        "interviewing": 0,
        "offer": 0,
    }
    assert summary.highest_intrinsic[0].job_posting_id == created.job_posting_id
    assert summary.highest_upside == []


def test_an_unknown_workspace_has_no_summary(durable_database: Database) -> None:
    with pytest.raises(NotFoundError):
        job_search.workspace_summary(durable_database, "missing")


def test_unevaluated_manual_scores_do_not_crowd_evaluated_jobs_out_of_best_fit(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="fit-crowding", name="Fit crowding"
    )
    source_id = employer_board(durable_database, workspace.id, "Globex")
    unevaluated_ids = []
    for index in range(4):
        job_id = tracked_board_posting(
            durable_database,
            workspace.id,
            source_id,
            title=f"Tracked Data Engineer {index}",
            url=f"https://globex.example.test/jobs/tracked-{index}",
        )
        unevaluated_ids.append(job_id)
        job_search.save_manual_job_review(
            durable_database,
            workspace.id,
            job_id,
            ManualJobReviewInput(recorded_fit_score=100),
        )
    evaluated = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(workspace.id),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [item.job_posting_id for item in summary.highest_intrinsic] == [evaluated.job_posting_id]
    assert summary.total_jobs == 5
    rows, total = job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))
    assert total == 5
    tracked = [row for row in rows if row.job_posting_id in unevaluated_ids]
    assert len(tracked) == 4
    assert all(row.evaluation_id is None and row.recorded_fit_score == 100 for row in tracked)


def test_best_fit_requires_a_stated_fit_and_preserves_manual_scores_including_zero(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="fit-eligibility", name="Fit eligibility"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    jobs = {}
    for slug, fit, reviewed in [
        ("scored", 20, None),
        ("zero-review", 90, 0),
        ("unknown-fit", None, None),
        ("reviewed-abstention", None, 30),
    ]:
        job = job_search.commit_evaluation(
            durable_database,
            artifacts,
            _posting(
                workspace.id,
                slug,
                scores=EvaluationScores(
                    fit=ScoreComponentInput(
                        value=fit,
                        reason="The CV does not state comparable role evidence."
                        if fit is None
                        else "",
                    ),
                    compensation=ScoreComponentInput(value=80),
                    eligibility=ScoreComponentInput(value=100),
                    evidence=ScoreComponentInput(value=80),
                    opportunity=ScoreComponentInput(value=70),
                ),
            ),
        )
        jobs[slug] = job
        if reviewed is not None:
            job_search.save_manual_job_review(
                durable_database,
                workspace.id,
                job.job_posting_id,
                ManualJobReviewInput(recorded_fit_score=reviewed),
            )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [item.job_posting_id for item in summary.highest_intrinsic] == [
        jobs["reviewed-abstention"].job_posting_id,
        jobs["scored"].job_posting_id,
        jobs["zero-review"].job_posting_id,
    ]
    assert summary.highest_intrinsic[0].fit_score is None
    assert summary.highest_intrinsic[0].recorded_fit_score == 30
    assert summary.highest_intrinsic[2].fit_score == 90
    assert summary.highest_intrinsic[2].recorded_fit_score == 0


def test_an_ineligible_job_is_not_offered_as_a_best_fit(
    durable_database: Database, tmp_path: Path
) -> None:
    """A refusal is separate from relevance, so the best-fit panel skips it.

    The panel shows a fit number alone, so a top-scoring ineligible job would read
    as the next useful work while being work the user cannot take.
    """
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="refusal", name="Refusal"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    refused = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Northwind Analytics",
            requisition_id="REQ-INELIGIBLE",
            canonical_url="https://example.test/jobs/ineligible",
            discovery_url="https://example.test/jobs/ineligible",
            decision=EvaluationDecision.INELIGIBLE,
            scores=EvaluationScores(
                fit=ScoreComponentInput(value=95),
                compensation=ScoreComponentInput(value=80),
                eligibility=ScoreComponentInput(value=30),
                evidence=ScoreComponentInput(value=80),
                opportunity=ScoreComponentInput(value=70),
            ),
        ),
    )
    open_to_apply = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Globex Health",
            requisition_id="REQ-OPEN",
            canonical_url="https://example.test/jobs/open",
            discovery_url="https://example.test/jobs/open",
            scores=EvaluationScores(
                fit=ScoreComponentInput(value=70),
                compensation=ScoreComponentInput(value=80),
                eligibility=ScoreComponentInput(value=100),
                evidence=ScoreComponentInput(value=80),
                opportunity=ScoreComponentInput(value=70),
            ),
        ),
    )
    revisit = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Initech Robotics",
            requisition_id="REQ-REVISIT",
            canonical_url="https://example.test/jobs/revisit",
            discovery_url="https://example.test/jobs/revisit",
            decision=EvaluationDecision.INVESTIGATE,
        ),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [item.job_posting_id for item in summary.highest_intrinsic] == [
        revisit.job_posting_id,
        open_to_apply.job_posting_id,
    ]
    # The revisit panel is about undecided work, so an eligibility refusal does
    # not change it.
    assert [item.job_posting_id for item in summary.highest_upside] == [revisit.job_posting_id]
    rows, total = job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))
    assert total == 3
    assert refused.job_posting_id in {row.job_posting_id for row in rows}


def test_a_prepared_package_is_shown_by_the_actionable_panel_and_not_repeated(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="prepared", name="Prepared"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    job = job_search.commit_evaluation(
        durable_database, artifacts, _posting(workspace.id, "prepared")
    )
    job_search.append_application_event(
        durable_database,
        job.job_posting_id,
        ApplicationEventInput(
            status=ApplicationStatus.PACKAGE_READY,
            occurred_at=NOW + timedelta(hours=1),
            notes="Drafted",
        ),
        idempotency_key="prepared-1",
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [item.job_posting_id for item in summary.actionable] == [job.job_posting_id]
    assert summary.decided_without_outcome == []
    assert summary.decided_without_outcome_count == 0


def test_an_undecided_job_is_not_a_missing_outcome(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="undecided", name="Undecided"
    )
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _posting(workspace.id, "revisit", decision=EvaluationDecision.INVESTIGATE),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert summary.decided_without_outcome == []
    assert summary.decided_without_outcome_count == 0


def test_an_archived_job_is_no_longer_waiting_on_an_outcome(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="archived", name="Archived"
    )
    job = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        _posting(workspace.id, "archived"),
    )
    job_search.archive_job(durable_database, job.job_posting_id, workspace.id)

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert summary.decided_without_outcome == []
    assert summary.decided_without_outcome_count == 0


def test_a_posting_that_closed_while_waiting_is_offered_before_the_oldest_decision(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ordering", name="Ordering"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    closed = job_search.commit_evaluation(
        durable_database, artifacts, _posting(workspace.id, "closed", evaluated_at=NOW)
    )
    _observe_closure(durable_database, closed.job_posting_id)
    oldest = job_search.commit_evaluation(
        durable_database,
        artifacts,
        _posting(workspace.id, "oldest", evaluated_at=NOW - timedelta(days=20)),
    )
    recent = job_search.commit_evaluation(
        durable_database,
        artifacts,
        _posting(workspace.id, "recent", evaluated_at=NOW - timedelta(days=2)),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [item.job_posting_id for item in summary.decided_without_outcome] == [
        closed.job_posting_id,
        oldest.job_posting_id,
        recent.job_posting_id,
    ]
    assert summary.decided_without_outcome_count == 3


def test_recording_an_application_takes_the_job_off_the_waiting_list(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="recorded", name="Recorded"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    submitted = job_search.commit_evaluation(
        durable_database, artifacts, _posting(workspace.id, "submitted")
    )
    waiting = job_search.commit_evaluation(
        durable_database, artifacts, _posting(workspace.id, "waiting")
    )
    before = job_search.workspace_summary(durable_database, workspace.id)
    job_search.append_application_event(
        durable_database,
        submitted.job_posting_id,
        ApplicationEventInput(
            status=ApplicationStatus.APPLIED,
            occurred_at=NOW + timedelta(hours=2),
            notes="Submitted",
        ),
        idempotency_key="recorded-1",
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert before.decided_without_outcome_count == 2
    assert [item.job_posting_id for item in summary.decided_without_outcome] == [
        waiting.job_posting_id
    ]
    assert summary.decided_without_outcome_count == 1


def test_a_softened_second_evaluation_takes_the_job_off_the_waiting_list(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="softened", name="Softened"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    proposal = _posting(workspace.id, "softened")
    created = job_search.commit_evaluation(durable_database, artifacts, proposal)
    job_search.commit_evaluation(
        durable_database,
        artifacts,
        proposal.model_copy(
            update={
                "decision": EvaluationDecision.MONITOR,
                "rationale": "Scope narrowed after a second reading.",
                "supersedes_evaluation_id": created.evaluation_id,
            }
        ),
    )

    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert summary.decided_without_outcome == []
    assert summary.decided_without_outcome_count == 0
