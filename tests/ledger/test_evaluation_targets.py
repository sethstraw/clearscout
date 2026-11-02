"""How a proposal is matched to a job: durable identifiers, and residence gating."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tailorcv.application import job_search
from tailorcv.application.contracts import EligibilityInput
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.evaluation_targets import (
    evaluated_jobs_for_pending,
    find_job_by_durable_identifier,
)
from tailorcv.application.workspaces import SearchParametersService, WorkspaceService
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.search_parameters import JobSearchParameters, Location, WorkEligibility
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Employer,
    JobPosting,
    PostingIdentifier,
    Workspace,
)
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit, pending_discovery


def _committed_job(
    database: Database, artifacts_root: Path, workspace_id: str, **identifiers: object
) -> str:
    """Commit one evaluation and return the job posting it was recorded against."""
    detail = job_search.commit_evaluation(
        database,
        artifact_service(database, artifacts_root),
        evaluation_commit(workspace_id, **identifiers),
    )
    return detail.job_posting_id


def _seed(
    database: Database,
    *,
    employer_name: str,
    title: str,
    requisition_value: str | None = None,
    url_value: str | None = None,
) -> tuple[str, str]:
    """Seed one workspace, employer, job, and named identifiers. Returns (workspace_id, job_id)."""
    with database.session() as session:
        workspace = Workspace(slug=f"ws-{employer_name}-{title}", name="Workspace")
        session.add(workspace)
        session.flush()
        employer = Employer(
            workspace_id=workspace.id,
            name=employer_name,
            normalized_name=normalize_text(employer_name),
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace.id,
            employer_id=employer.id,
            title=title,
            normalized_title=normalize_text(title),
            first_seen_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        session.add(job)
        session.flush()
        if requisition_value is not None:
            session.add(
                PostingIdentifier(
                    workspace_id=workspace.id,
                    job_posting_id=job.id,
                    identifier_type="requisition",
                    identifier_value=requisition_value,
                )
            )
        if url_value is not None:
            session.add(
                PostingIdentifier(
                    workspace_id=workspace.id,
                    job_posting_id=job.id,
                    identifier_type="url",
                    identifier_value=url_value,
                )
            )
        session.flush()
        return workspace.id, job.id


def test_a_different_requisition_id_keeps_two_postings_distinct(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _seed(
        durable_database,
        employer_name="Acme Health",
        title="Platform Director",
        requisition_value=f"{normalize_text('Acme Health')}|{normalize_text('REQ-100')}",
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Acme Health",
        title="Platform Director",
        requisition_id="REQ-200",
        canonical_url="",
        discovery_url="",
    )

    assert committed != job_id


def test_a_different_canonical_url_keeps_two_postings_distinct(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _seed(
        durable_database,
        employer_name="Globex",
        title="Platform Director",
        url_value="https://example.invalid/jobs/1",
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Globex",
        title="Platform Director",
        requisition_id="",
        canonical_url="https://example.invalid/jobs/2",
        discovery_url="",
    )

    assert committed != job_id


def test_employer_and_title_alone_still_map_two_openings_together(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _seed(
        durable_database,
        employer_name="Acme Health",
        title="Platform Director",
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Acme Health",
        title="Platform Director",
        requisition_id="",
        canonical_url="",
        discovery_url="",
    )

    assert committed == job_id


def test_a_matching_requisition_short_circuits_before_urls(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _seed(
        durable_database,
        employer_name="Acme Health",
        title="Platform Director",
        requisition_value=f"{normalize_text('Acme Health')}|{normalize_text('REQ-100')}",
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Acme Health",
        title="Some other title",
        requisition_id="REQ-100",
        canonical_url="https://example.invalid/nonmatching",
        discovery_url="",
    )

    assert committed == job_id


def test_a_discovery_url_matches_when_canonical_url_is_empty(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _seed(
        durable_database,
        employer_name="Globex",
        title="Platform Director",
        url_value="https://example.invalid/jobs/3",
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Globex",
        title="Platform Director",
        requisition_id="",
        canonical_url="",
        discovery_url="https://example.invalid/jobs/3",
    )

    assert committed == job_id


_BOARD_UUID = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"


def _job_under_one_spelling(
    database: Database, artifacts_root: Path, requisition: str
) -> tuple[str, str]:
    """Commit one posting whose employer states ``requisition``, returning (workspace, job)."""
    workspace_id, _unrelated = _seed(
        database, employer_name="Acme Health", title="Platform Director"
    )
    job_id = _committed_job(
        database,
        artifacts_root,
        workspace_id,
        employer="Acme Health",
        requisition_id=requisition,
        canonical_url="",
        discovery_url="",
    )
    return workspace_id, job_id


def test_a_board_uuid_names_one_posting_under_two_employer_spellings(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _job_under_one_spelling(
        durable_database, tmp_path / "artifacts", _BOARD_UUID
    )

    with durable_database.session() as session:
        found = find_job_by_durable_identifier(
            session,
            workspace_id=workspace_id,
            employer="Acme Health Systems",
            requisition_id=_BOARD_UUID.upper(),
            canonical_url="https://careers.acme.example/job/other",
            discovery_url="",
        )

    assert found is not None and found.id == job_id


def test_a_pending_row_stating_a_board_uuid_under_another_spelling_is_already_evaluated(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _job_under_one_spelling(
        durable_database, tmp_path / "artifacts", _BOARD_UUID
    )
    row = pending_discovery(
        workspace_id, employer="Acme Health Systems", created_at=datetime(2026, 8, 11, tzinfo=UTC)
    )
    row.requisition_id = _BOARD_UUID

    with durable_database.session() as session:
        matched = evaluated_jobs_for_pending(session, workspace_id, [row])

    assert matched[row.id].job_posting_id == job_id


def test_an_employer_requisition_under_another_employer_spelling_is_another_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id = _job_under_one_spelling(
        durable_database, tmp_path / "artifacts", "REQ-100"
    )

    committed = _committed_job(
        durable_database,
        tmp_path / "artifacts",
        workspace_id,
        employer="Acme Health Systems",
        requisition_id="REQ-100",
        canonical_url="",
        discovery_url="",
    )

    assert committed != job_id


def _declare_two_markets(database: Database, slug: str) -> str:
    """Create a workspace declaring one authorized and one ineligible market."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(slug=slug, name=slug)
    SearchParametersService(database).save(
        workspace.id,
        JobSearchParameters(
            locations=[
                Location(
                    key="home_market",
                    country="Atlantis",
                    eligibility=WorkEligibility.AUTHORIZED,
                ),
                Location(
                    key="far_market",
                    country="Elsewhere",
                    eligibility=WorkEligibility.INELIGIBLE,
                ),
            ]
        ),
    )
    return workspace.id


def test_an_unconfigured_workspace_does_not_gate_on_residence(
    durable_database: Database, tmp_path: Path
) -> None:
    """A person who has declared no locations has not declared themselves ineligible."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="open", name="Open"
    )
    proposal = evaluation_commit(
        workspace.id, eligibility=EligibilityInput(workspace_location_key="somewhere_specific")
    )

    detail = job_search.commit_evaluation(
        durable_database, artifact_service(durable_database, tmp_path / "artifacts"), proposal
    )

    assert detail.decision == "apply_now"


def test_declared_locations_gate_an_actionable_decision(
    durable_database: Database, tmp_path: Path
) -> None:
    """Once locations exist, an actionable decision has to name a workable one."""
    workspace_id = _declare_two_markets(durable_database, "gated")
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")

    accepted = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace_id, eligibility=EligibilityInput(workspace_location_key="home_market")
        ),
    )
    assert accepted.decision == "apply_now"

    with pytest.raises(ValidationError, match="records 'far_market'"):
        job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace_id, eligibility=EligibilityInput(workspace_location_key="far_market")
            ),
        )
    with pytest.raises(ValidationError, match="not a location this workspace can work in"):
        job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace_id, eligibility=EligibilityInput(workspace_location_key="never_declared")
            ),
        )


def test_a_multi_market_disjunction_is_workable_when_any_market_is(
    durable_database: Database, tmp_path: Path
) -> None:
    """A posting remote in several declared markets is workable if any one of them is."""
    workspace_id = _declare_two_markets(durable_database, "disjunct")
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")

    accepted = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace_id,
            eligibility=EligibilityInput(workspace_location_key="far_market or home_market"),
        ),
    )
    assert accepted.decision == "apply_now"

    with pytest.raises(ValidationError, match="not a location this workspace can work in"):
        job_search.commit_evaluation(
            durable_database,
            artifacts,
            evaluation_commit(
                workspace_id,
                eligibility=EligibilityInput(workspace_location_key="far_market or never_declared"),
            ),
        )
