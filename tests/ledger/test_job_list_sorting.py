"""Sorting the job ledger: which keys are accepted, and which order a score gives."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from clearscout.application import job_search
from clearscout.application.contracts import EvaluationScores, ScoreComponentInput
from clearscout.application.job_search.models import JobListQuery
from clearscout.application.workspaces import WorkspaceService
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.database import clone
from tests.support.scenarios import evaluation_commit


class _Ledger(NamedTuple):
    """A committed ledger and the postings in it, in the order the fixture wrote them."""

    database: Database
    workspace_id: str
    job_posting_ids: tuple[str, ...]


@pytest.fixture(scope="module")
def single_job_ledger(
    migrated_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[_Ledger]:
    """One committed evaluation the sort-key cases share, because every case only reads it."""
    root = tmp_path_factory.mktemp("sort-keys")
    database = Database(clone(migrated_template, root / "durable.sqlite3"))
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="sort-keys", name="Sort keys"
    )
    created = job_search.commit_evaluation(
        database, artifact_service(database, root / "artifacts"), evaluation_commit(workspace.id)
    )
    yield _Ledger(database, workspace.id, (created.job_posting_id,))
    database.dispose()


@pytest.fixture(scope="module")
def scored_pair_ledger(
    migrated_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[_Ledger]:
    """Two committed evaluations, higher scores first, shared because every case only reads them."""
    root = tmp_path_factory.mktemp("score-sorting")
    database = Database(clone(migrated_template, root / "durable.sqlite3"))
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="score-sorting",
        name="Score Sorting",
    )
    artifacts = artifact_service(database, root / "artifacts")
    higher = job_search.commit_evaluation(database, artifacts, evaluation_commit(workspace.id))
    lower = job_search.commit_evaluation(
        database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Beta Health",
            title="Associate Product Manager",
            requisition_id="REQ-43",
            canonical_url="https://example.test/jobs/43",
            discovery_url="https://example.test/jobs/43",
            scores=EvaluationScores(
                fit=ScoreComponentInput(value=10),
                compensation=ScoreComponentInput(value=20),
                eligibility=ScoreComponentInput(value=30),
                evidence=ScoreComponentInput(value=40),
                opportunity=ScoreComponentInput(value=50),
            ),
        ),
    )
    yield _Ledger(database, workspace.id, (higher.job_posting_id, lower.job_posting_id))
    database.dispose()


@pytest.mark.parametrize(
    "sort_field",
    [
        "employer",
        "title",
        "decision",
        "fit_score",
        "compensation_score",
        "eligibility_score",
        "evidence_score",
        "opportunity_score",
        "overall_score",
        "application_status",
        "completeness",
        "evaluated_at",
    ],
)
def test_every_supported_sort_key_is_accepted(single_job_ledger: _Ledger, sort_field: str) -> None:
    rows, total = job_search.list_jobs(
        single_job_ledger.database,
        JobListQuery(workspace_id=single_job_ledger.workspace_id, sort=sort_field),
    )

    assert total == 1
    assert rows[0].job_posting_id == single_job_ledger.job_posting_ids[0]


@pytest.mark.parametrize(
    "sort_field",
    [
        "fit_score",
        "compensation_score",
        "eligibility_score",
        "evidence_score",
        "opportunity_score",
        "overall_score",
    ],
)
def test_job_ledger_sorts_the_complete_result_set_by_every_score(
    scored_pair_ledger: _Ledger, sort_field: str
) -> None:
    higher_id, lower_id = scored_pair_ledger.job_posting_ids

    ascending, total = job_search.list_jobs(
        scored_pair_ledger.database,
        JobListQuery(
            workspace_id=scored_pair_ledger.workspace_id,
            sort=sort_field,
            descending=False,
            limit=1,
        ),
    )
    descending, _ = job_search.list_jobs(
        scored_pair_ledger.database,
        JobListQuery(
            workspace_id=scored_pair_ledger.workspace_id,
            sort=sort_field,
            descending=True,
            limit=1,
        ),
    )

    assert total == 2
    assert ascending[0].job_posting_id == lower_id
    assert descending[0].job_posting_id == higher_id
