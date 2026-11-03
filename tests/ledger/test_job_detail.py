"""The job detail projection: bounded history, current facts, and stored bands."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from clearscout.application import job_search
from clearscout.application.contracts import CompensationInput, EligibilityInput
from clearscout.application.failures import NotFoundError
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.domain.search_parameters import JobSearchParameters, Location, WorkEligibility
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PostingObservation
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import NOW, evaluation_commit


def test_job_detail_keeps_latest_and_evidence_links_outside_the_history_page(
    durable_database: Database, tmp_path: Path
) -> None:
    """The bounded chronology does not bound current facts or evidence targets."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="long-history", name="Long history"
    )
    created = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(workspace.id),
    )
    evaluated_observation_id = created.observations[0].id
    tied_at = NOW + timedelta(days=50)
    with durable_database.session() as session:
        for index in range(1, 52):
            session.add(
                PostingObservation(
                    id=f"observation-{index:03}",
                    job_posting_id=created.job_posting_id,
                    observed_at=tied_at if index >= 50 else NOW + timedelta(days=index),
                    posting_status="active",
                    location=f"Location {index}",
                    residence_rule="",
                    employment_type="",
                    payroll_arrangement="",
                    created_by="test",
                    origin="history_fixture",
                )
            )

    first = job_search.job_detail(
        durable_database, created.job_posting_id, workspace.id, history_limit=50
    )
    last = job_search.job_detail(
        durable_database,
        created.job_posting_id,
        workspace.id,
        observation_offset=50,
        history_limit=50,
    )

    assert first.observation_count == 52
    assert len(first.observations) == 50
    assert first.observations[0].id == evaluated_observation_id
    assert first.observations[-1].id == "observation-049"
    assert [item.id for item in last.observations] == ["observation-050", "observation-051"]
    assert first.latest_observation is not None
    assert first.latest_observation.id == "observation-051"
    assert last.latest_observation == first.latest_observation
    posting_evidence = next(item for item in last.evidence if item.source_kind == "posting")
    assert posting_evidence.observation_id == evaluated_observation_id


def test_job_detail_includes_compensation_and_eligibility_facts(
    durable_database: Database, tmp_path: Path
) -> None:
    """Compensation bands and eligibility facts appear in job detail response."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="facts", name="Facts"
    )
    SearchParametersService(durable_database).save(
        workspace.id,
        JobSearchParameters(
            locations=[
                Location(
                    key="local",
                    country="Canada",
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
                amount_min=Decimal(150000),
                amount_max=Decimal(180000),
                currency="CAD",
                basis="annual",
                geography="Toronto",
                description="Base salary plus equity",
            )
        ],
        eligibility=EligibilityInput(
            workspace_location_key="local",
            arrangement="Hybrid: 3 days in office, Toronto",
            details="Active; remote work 2 days per week",
        ),
    )

    detail = job_search.commit_evaluation(
        durable_database, artifact_service(durable_database, tmp_path / "artifacts"), proposal
    )
    job_id = detail.job_posting_id
    full_detail = job_search.job_detail(durable_database, job_id, workspace.id)

    assert len(full_detail.compensation_components) == 1
    comp = full_detail.compensation_components[0]
    assert comp.component_type == "base"
    assert comp.amount_min == 150000.0
    assert comp.amount_max == 180000.0
    assert comp.currency == "CAD"
    assert comp.basis == "annual"
    assert comp.geography == "Toronto"
    assert comp.description == "Base salary plus equity"

    assert len(full_detail.eligibility_records) == 1
    elig = full_detail.eligibility_records[0]
    assert elig.workspace_location_key == "local"
    assert elig.arrangement == "Hybrid: 3 days in office, Toronto"
    assert elig.details == "Active; remote work 2 days per week"


def test_an_unknown_job_has_no_detail(durable_database: Database) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="empty", name="Empty"
    )

    with pytest.raises(NotFoundError):
        job_search.job_detail(durable_database, "missing", workspace.id)
