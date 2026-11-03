"""Historical scores retain their meaning when current workspace inputs change."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from clearscout.application.job_search import (
    acknowledge_evaluation_currentness,
    job_detail,
    list_jobs,
)
from clearscout.application.job_search.models import JobListQuery
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    EvaluationCurrentnessReview,
    JobEvaluation,
    MasterCvVersion,
    PostingObservation,
)
from tests.support.scenarios import studio_workspace


def test_reading_an_old_score_reports_unknown_inputs_without_rewriting_history(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, version_id = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    with durable_database.session() as session:
        session.add(
            JobEvaluation(
                job_posting_id=job_id,
                master_cv_version_id=version_id,
                decision="investigate",
                rationale="Synthetic historical assessment",
                payload_sha256="b" * 64,
                evaluated_at=datetime.now(UTC),
                fit_report_payload={},
            )
        )
    before = job_detail(durable_database, job_id, workspace_id)
    by_name = {item.name: item for item in before.evaluation_inputs}
    assert by_name["search_parameters"].status == "unknown"
    assert by_name["build"].recorded is None
    assert by_name["cv"].status == "current"
    with durable_database.session() as session:
        evaluation = session.scalar(
            select(JobEvaluation).where(JobEvaluation.job_posting_id == job_id)
        )
        assert evaluation is not None
        original = dict(evaluation.fit_report_payload or {})
        old_cv = session.get(MasterCvVersion, evaluation.master_cv_version_id)
        assert old_cv is not None
        session.add(
            MasterCvVersion(
                candidate_profile_id=old_cv.candidate_profile_id,
                content_sha256="a" * 64,
                source_uri="synthetic:new-cv",
                effective_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    after = job_detail(durable_database, job_id, workspace_id)
    assert {item.name: item.status for item in after.evaluation_inputs}["cv"] == "changed"
    assert after.evaluation_id == before.evaluation_id
    assert after.overall_score == before.overall_score
    rows, _ = list_jobs(durable_database, JobListQuery(workspace_id=workspace_id))
    assert rows[0].evaluation_inputs == after.evaluation_inputs
    assert after.evaluation_history[0].evaluation_inputs == after.evaluation_inputs
    with durable_database.session() as session:
        saved = session.get(JobEvaluation, after.evaluation_id)
        assert saved is not None
        assert saved.fit_report_payload == original


@pytest.mark.parametrize("changed", [None, "search_parameters", "posting", "prompts", "build"])
def test_currentness_compares_exact_inputs_without_rewriting_the_assessment(
    durable_database: Database, tmp_path: Path, monkeypatch, changed: str | None
) -> None:
    from clearscout.application import prompts
    from clearscout.application.job_search import evaluation_currentness
    from clearscout.application.workspaces import SearchParametersService
    from clearscout.application.workspaces.parameters import parameter_snapshot_in
    from clearscout.application.workspaces.prompt_library import PromptLibraryService
    from clearscout.domain.search_parameters import JobSearchParameters, Seniority
    from clearscout.infrastructure.build import BuildIdentity, current_build
    from clearscout.infrastructure.persistence.models import PostingObservation

    workspace_id, job_id, _, version_id = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    parameters = SearchParametersService(durable_database)
    parameters.save(workspace_id, JobSearchParameters(seniority=Seniority(minimum="senior")))
    build = current_build()
    with durable_database.session() as session:
        observation = PostingObservation(
            job_posting_id=job_id,
            observed_at=datetime.now(UTC),
            posting_status="active",
            origin="test",
        )
        session.add(observation)
        session.flush()
        report = {
            "prompt_fingerprint": prompts.suite_fingerprint(),
            "code_revision": build.revision,
        }
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            master_cv_version_id=version_id,
            observation_id=observation.id,
            search_parameter_set_id=parameter_snapshot_in(session, workspace_id).parameter_set_id,
            decision="investigate",
            rationale="Synthetic assessment",
            payload_sha256="c" * 64,
            evaluated_at=datetime.now(UTC),
            fit_report_payload=report,
        )
        session.add(evaluation)
        session.flush()
        evaluation_id = evaluation.id
    before = job_detail(durable_database, job_id, workspace_id)
    assert {item.status for item in before.evaluation_inputs} == {"current"}
    assert all(item.recorded == item.current and item.recorded for item in before.evaluation_inputs)
    if changed == "search_parameters":
        parameters.save(workspace_id, JobSearchParameters(seniority=Seniority(minimum="staff")))
    elif changed == "posting":
        with durable_database.session() as session:
            session.add(
                PostingObservation(
                    job_posting_id=job_id,
                    observed_at=datetime.now(UTC) + timedelta(days=1),
                    posting_status="closed",
                    origin="test",
                )
            )
    elif changed == "prompts":
        PromptLibraryService(durable_database).save(
            workspace_id,
            "draft_system",
            prompts.packaged("draft_system") + "\nSynthetic currentness instruction.\n",
        )
    elif changed == "build":
        monkeypatch.setattr(
            evaluation_currentness,
            "current_build",
            lambda: BuildIdentity("sha256:" + "d" * 64, build.package_version),
        )
    after = job_detail(durable_database, job_id, workspace_id)
    for item in after.evaluation_inputs:
        assert item.status == ("changed" if item.name == changed else "current")
    assert after.evaluation_id == before.evaluation_id == evaluation_id
    assert after.overall_score == before.overall_score
    rows, _ = list_jobs(durable_database, JobListQuery(workspace_id=workspace_id))
    assert rows[0].evaluation_inputs == after.evaluation_inputs
    assert after.evaluation_history[0].evaluation_inputs == after.evaluation_inputs
    with durable_database.session() as session:
        saved = session.get(JobEvaluation, evaluation_id)
        assert saved is not None
        assert saved.fit_report_payload == report


def test_currentness_keeps_an_unchanged_posting_current_after_a_refresh(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, version_id = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    with durable_database.session() as session:
        evaluated_observation = PostingObservation(
            job_posting_id=job_id,
            observed_at=datetime.now(UTC),
            posting_status="active",
            content_sha256="a" * 64,
            origin="test",
        )
        session.add(evaluated_observation)
        session.flush()
        session.add(
            JobEvaluation(
                job_posting_id=job_id,
                master_cv_version_id=version_id,
                observation_id=evaluated_observation.id,
                decision="investigate",
                rationale="Synthetic assessment",
                payload_sha256="c" * 64,
                evaluated_at=datetime.now(UTC),
            )
        )
        session.add(
            PostingObservation(
                job_posting_id=job_id,
                observed_at=datetime.now(UTC) + timedelta(days=1),
                posting_status="active",
                content_sha256="a" * 64,
                origin="test",
            )
        )
    detail = job_detail(durable_database, job_id, workspace_id)
    inputs = {item.name: item for item in detail.evaluation_inputs}
    assert inputs["posting"].status == "current"


def test_review_acknowledgement_retains_the_score_until_inputs_change_again(
    durable_database: Database, tmp_path: Path
) -> None:
    from clearscout.application.workspaces import SearchParametersService
    from clearscout.domain.search_parameters import JobSearchParameters, Seniority

    workspace_id, job_id, _, version_id = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    SearchParametersService(durable_database).save(
        workspace_id, JobSearchParameters(seniority=Seniority(minimum="senior"))
    )
    with durable_database.session() as session:
        original = PostingObservation(
            job_posting_id=job_id,
            observed_at=datetime.now(UTC),
            posting_status="active",
            content_sha256="a" * 64,
            origin="test",
        )
        session.add(original)
        session.flush()
        evaluation = JobEvaluation(
            job_posting_id=job_id,
            master_cv_version_id=version_id,
            observation_id=original.id,
            decision="investigate",
            rationale="Synthetic assessment",
            payload_sha256="c" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.flush()
        session.add(
            PostingObservation(
                job_posting_id=job_id,
                observed_at=datetime.now(UTC) + timedelta(days=1),
                posting_status="active",
                content_sha256="b" * 64,
                origin="test",
            )
        )
        evaluation_id = evaluation.id
    before = job_detail(durable_database, job_id, workspace_id)
    assert before.evaluation_review.status == "needs_review"
    assert before.evaluation_review.reason == "posting_changed"

    with durable_database.session() as session:
        acknowledge_evaluation_currentness(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            evaluation_id=evaluation_id,
        )

    reviewed = job_detail(durable_database, job_id, workspace_id)
    assert reviewed.evaluation_review.status == "reviewed"
    assert reviewed.evaluation_review.reason == "posting_changed"
    assert reviewed.evaluation_review.reviewed_at is not None
    assert reviewed.evaluation_id == evaluation_id
    with durable_database.session() as session:
        currentness_review = session.scalar(
            select(EvaluationCurrentnessReview).where(
                EvaluationCurrentnessReview.evaluation_id == evaluation_id
            )
        )
        assert currentness_review is not None
        review_id = currentness_review.id
    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE evaluation_currentness_reviews SET origin = 'rewritten' WHERE id = :id"),
            {"id": review_id},
        )
    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(
            text("DELETE FROM evaluation_currentness_reviews WHERE id = :id"), {"id": review_id}
        )
    with durable_database.session() as session:
        session.add(
            PostingObservation(
                job_posting_id=job_id,
                observed_at=datetime.now(UTC) + timedelta(days=2),
                posting_status="active",
                content_sha256="d" * 64,
                origin="test",
            )
        )
    reopened = job_detail(durable_database, job_id, workspace_id)
    assert reopened.evaluation_review.status == "needs_review"
