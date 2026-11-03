"""Querying the job ledger: filters, refusals, projected facts, and query cost."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select

from clearscout.application import job_search
from clearscout.application.contracts import (
    CompensationInput,
    EligibilityInput,
    EvaluationScores,
    ScoreComponentInput,
)
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import JobListQuery, ManualJobReviewInput
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.domain.search_parameters import JobSearchParameters, Location, WorkEligibility
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import AuditEvent, JobPosting, PostingObservation
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import NOW, evaluation_commit, workspace_with_committed_evaluation


def test_closing_dates_project_independently_from_recorded_review_text(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "date-projection")
    job_search.save_manual_job_review(
        durable_database,
        scenario.workspace_id,
        scenario.job_posting_id,
        ManualJobReviewInput(recorded_deadline="Rolling applications"),
    )
    with durable_database.session() as session:
        job = session.get_one(JobPosting, scenario.job_posting_id)
        job.closing_date_override = date(2026, 10, 21)
        job.closing_date_override_updated_at = NOW
        observation = session.scalar(
            select(PostingObservation).where(PostingObservation.job_posting_id == job.id)
        )
        assert observation is not None
        session.add(
            PostingObservation(
                job_posting_id=job.id,
                observed_at=observation.observed_at + timedelta(days=1),
                posting_status="active",
                publisher_closing_date=publisher_closing_date(
                    "Applications close: 2026-10-19",
                    source_url="https://example.test/jobs/lead",
                    observed_at=NOW,
                ).as_json(),
            )
        )
    rows, total = job_search.list_jobs(
        durable_database, JobListQuery(workspace_id=scenario.workspace_id)
    )
    detail = job_search.job_detail(
        durable_database, scenario.job_posting_id, workspace_id=scenario.workspace_id
    )
    assert total == 1
    for row in (rows[0], detail):
        assert row.closing_date_override == date(2026, 10, 21)
        assert row.publisher_closing_date is not None
        assert row.publisher_closing_date.normalized_value == "2026-10-19"
        assert row.recorded_deadline == "Rolling applications"
    assert detail.latest_observation is not None
    assert detail.latest_observation.publisher_closing_date == detail.publisher_closing_date


def test_the_ledger_finds_a_job_by_search_text_and_decision(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "search-filter")
    workspace_id, job_posting_id = scenario.workspace_id, scenario.job_posting_id

    rows, total = job_search.list_jobs(
        durable_database,
        JobListQuery(
            workspace_id=workspace_id,
            search="schemas",
            decisions=("apply_now",),
            sort="fit_score",
        ),
    )

    assert total == 1
    assert rows[0].job_posting_id == job_posting_id


def test_the_active_filter_keeps_a_job_never_observed(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "unobserved")
    with durable_database.session() as session:
        observed = session.get_one(JobPosting, scenario.job_posting_id)
        unobserved = JobPosting(
            workspace_id=scenario.workspace_id,
            employer_id=observed.employer_id,
            title="Mirrored From Ledger",
            normalized_title="mirrored from ledger",
            first_seen_at=NOW,
            created_by="test",
            origin="test",
        )
        session.add(unobserved)
        session.flush()
        unobserved_id = unobserved.id

    def listed(*statuses: str) -> set[str]:
        rows, _ = job_search.list_jobs(
            durable_database,
            JobListQuery(workspace_id=scenario.workspace_id, posting_statuses=statuses),
        )
        return {row.job_posting_id for row in rows}

    assert unobserved_id in listed("active")
    assert unobserved_id not in listed("closed")


def test_human_scores_order_review_without_rewriting_evaluation(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="review-order", name="Review order"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    first = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Example One",
            requisition_id="REVIEW-1",
            canonical_url="https://example.test/jobs/review-1",
            discovery_url="https://example.test/jobs/review-1",
        ),
    )
    second = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Example Two",
            requisition_id="REVIEW-2",
            canonical_url="https://example.test/jobs/review-2",
            discovery_url="https://example.test/jobs/review-2",
            scores=EvaluationScores(
                fit=ScoreComponentInput(value=10),
                compensation=ScoreComponentInput(value=20),
                eligibility=ScoreComponentInput(value=30),
                evidence=ScoreComponentInput(value=40),
                opportunity=ScoreComponentInput(value=50),
            ),
        ),
    )
    assert first.fit_score is not None and second.fit_score is not None
    assert first.overall_score is not None and second.overall_score is not None
    assert first.fit_score > second.fit_score
    assert first.overall_score > second.overall_score
    original_fit = first.fit_score
    original_overall = first.overall_score
    job_search.save_manual_job_review(
        durable_database,
        workspace.id,
        first.job_posting_id,
        ManualJobReviewInput(
            recorded_fit_score=0,
            recorded_compensation_score=0,
            recorded_overall_score=0,
        ),
    )

    fit_rows, _ = job_search.list_jobs(
        durable_database,
        JobListQuery(workspace_id=workspace.id, sort="fit_score", descending=False),
    )
    overall_rows, _ = job_search.list_jobs(
        durable_database,
        JobListQuery(workspace_id=workspace.id, sort="recorded_score", descending=False),
    )
    assert [row.job_posting_id for row in fit_rows] == [first.job_posting_id, second.job_posting_id]
    assert [row.job_posting_id for row in overall_rows] == [
        first.job_posting_id,
        second.job_posting_id,
    ]
    assert fit_rows[0].recorded_fit_score == 0
    assert fit_rows[0].fit_score == original_fit
    assert overall_rows[0].recorded_overall_score == 0
    assert overall_rows[0].overall_score == original_overall
    for sort in ("recorded_fit_score", "recorded_compensation_score", "tracker_composite_score"):
        legacy_rows, _ = job_search.list_jobs(
            durable_database,
            JobListQuery(workspace_id=workspace.id, sort=sort, descending=False),
        )
        assert legacy_rows[0].job_posting_id == first.job_posting_id


def test_older_review_total_is_read_until_a_new_review_clears_it(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "older-review")
    with durable_database.session() as session:
        session.add(
            AuditEvent(
                workspace_id=scenario.workspace_id,
                action="job.manual_review_saved",
                entity_type="job_posting",
                entity_id=scenario.job_posting_id,
                details={
                    "tracker_composite_score": 73,
                    "recorded_posted_pay": "Synthetic source pay",
                },
            )
        )

    prior = job_search.job_detail(durable_database, scenario.job_posting_id, scenario.workspace_id)
    assert prior.recorded_overall_score == 73
    assert prior.recorded_compensation == "Synthetic source pay"

    cleared = job_search.save_manual_job_review(
        durable_database,
        scenario.workspace_id,
        scenario.job_posting_id,
        ManualJobReviewInput(recorded_overall_score=None),
    )
    assert cleared.recorded_overall_score is None
    assert cleared.recorded_compensation == ""
    assert cleared.evaluation_id == prior.evaluation_id
    assert cleared.overall_score == prior.overall_score


def test_an_unsupported_sort_key_is_refused(durable_database: Database, tmp_path: Path) -> None:
    workspace_id = workspace_with_committed_evaluation(
        durable_database, tmp_path, "unsupported-sort"
    ).workspace_id

    with pytest.raises(ValidationError, match="Unsupported job sort"):
        job_search.list_jobs(
            durable_database, JobListQuery(workspace_id=workspace_id, sort="secret")
        )


def test_a_page_holding_no_rows_is_refused(durable_database: Database, tmp_path: Path) -> None:
    workspace_id = workspace_with_committed_evaluation(
        durable_database, tmp_path, "empty-page"
    ).workspace_id

    with pytest.raises(ValidationError, match="Pagination"):
        job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace_id, limit=0))


def test_job_list_and_summary_query_counts_are_bounded(
    durable_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = []
    for ledger_size in (1, 40):
        workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
            slug=f"query-count-{ledger_size}",
            name=f"Query Count {ledger_size}",
        )
        artifacts = artifact_service(durable_database, tmp_path / "artifacts")
        for index in range(ledger_size):
            job_search.commit_evaluation(
                durable_database,
                artifacts,
                evaluation_commit(
                    workspace.id,
                    employer=f"Synthetic Employer {index}",
                    title=f"Synthetic Product Role {index}",
                    requisition_id=f"SYN-{index}",
                    canonical_url=f"https://example.test/jobs/synthetic-{index}",
                    discovery_url=f"https://example.test/jobs/synthetic-{index}",
                ),
            )

        def forbid_file_read(*args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("job projections must not read artifact bytes")

        monkeypatch.setattr(artifacts, "read", forbid_file_read)
        statements: list[str] = []

        def count_statement(*args: object, sink: list[str] = statements) -> None:
            sink.append(str(args[2]))

        event.listen(durable_database.engine, "before_cursor_execute", count_statement)
        try:
            rows, total = job_search.list_jobs(
                durable_database, JobListQuery(workspace_id=workspace.id, completeness="complete")
            )
            list_count = len(statements)
            statements.clear()
            summary = job_search.workspace_summary(durable_database, workspace.id)
            summary_count = len(statements)
        finally:
            event.remove(durable_database.engine, "before_cursor_execute", count_statement)

        assert total == ledger_size
        assert len(rows) == min(ledger_size, 50)
        assert all(not any(key.startswith("_") for key in row.model_dump()) for row in rows)
        assert summary.total_jobs == ledger_size
        # Ceilings, so reading the same rows in fewer statements still passes. The
        # property is that neither number moves with ledger_size.
        assert list_count <= 11  # Four projection reads and seven input-currentness reads.
        assert summary_count <= 15  # Eight summary reads and seven input-currentness reads.

        counts.append((list_count, summary_count))
    assert counts[0] == counts[1], "currentness reads must not grow with the ledger"


def test_list_jobs_includes_compensation_and_eligibility(
    durable_database: Database, tmp_path: Path
) -> None:
    """Job summaries in the ledger include compensation and eligibility arrays."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ledger", name="Ledger"
    )
    SearchParametersService(durable_database).save(
        workspace.id,
        JobSearchParameters(
            locations=[
                Location(
                    key="sf",
                    country="United States",
                    eligibility=WorkEligibility.AUTHORIZED,
                )
            ]
        ),
    )
    proposal = evaluation_commit(
        workspace.id,
        compensation=[
            CompensationInput(
                component_type="base",
                amount_min=Decimal(120000),
                amount_max=Decimal(150000),
                currency="USD",
                basis="annual",
                geography="San Francisco",
                description="Base salary",
            )
        ],
        eligibility=EligibilityInput(
            workspace_location_key="sf",
            arrangement="Remote",
            details="Anywhere in US",
        ),
    )

    job_search.commit_evaluation(
        durable_database, artifact_service(durable_database, tmp_path / "artifacts"), proposal
    )
    jobs, total = job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))

    assert total == 1
    assert len(jobs) == 1
    job = jobs[0]

    assert len(job.compensation_components) == 1
    comp = job.compensation_components[0]
    assert comp.component_type == "base"
    assert comp.amount_min == 120000.0
    assert comp.amount_max == 150000.0
    assert comp.currency == "USD"
    assert comp.basis == "annual"

    assert len(job.eligibility_records) == 1
    elig = job.eligibility_records[0]
    assert elig.workspace_location_key == "sf"
    assert elig.arrangement == "Remote"


@pytest.mark.parametrize(
    ("place", "countries"),
    [("Toronto, ON", ["Canada"]), ("Boston", ["United States"]), ("not stated", [])],
)
def test_ledger_summary_and_detail_name_the_countries_of_the_matched_place(
    durable_database: Database, tmp_path: Path, place: str, countries: list[str]
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="countries", name="Countries"
    )
    job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(
            workspace.id,
            eligibility=EligibilityInput(workspace_location_key=place, arrangement="Hybrid"),
        ),
    )

    jobs, _ = job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace.id))
    summary = job_search.workspace_summary(durable_database, workspace.id)

    assert [job.countries for job in jobs] == [countries]
    assert [job.countries for job in summary.highest_intrinsic] == [countries]
    detail = job_search.job_detail(durable_database, jobs[0].job_posting_id, workspace.id)
    assert detail.countries == countries
