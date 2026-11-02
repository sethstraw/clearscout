"""Materialized missed-requirement rows survive re-refresh and rebuild cleanly."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.missed_requirement_reader import (
    missed_requirement_texts_in,
    normalize,
    refresh_for_evaluation,
)
from tailorcv.application.failures import ValidationError
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvVersion,
    MissedRequirementText,
)
from tests.support.artifacts import runtime_storage


def _seed_posting(database: Database) -> tuple[str, str, str]:
    """Return (workspace_id, cv_version_id, evaluation_id) for a minimal fixture."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="miss-check", name="Miss Check"
    )
    workspace_id = str(workspace.id)
    with database.session() as session:
        profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
        session.add(profile)
        session.flush()
        cv_version = MasterCvVersion(
            id=str(uuid.uuid4()),
            candidate_profile_id=profile.id,
            content_sha256="0" * 64,
            source_uri="test://cv",
            effective_at=utc_now(),
            byte_size=10,
        )
        session.add(cv_version)
        session.flush()
        employer = Employer(workspace_id=workspace_id, name="Globex", normalized_name="globex")
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Backend Engineer",
            normalized_title="backend engineer",
            first_seen_at=utc_now(),
        )
        session.add(posting)
        session.flush()
        evaluation = JobEvaluation(
            id=str(uuid.uuid4()),
            job_posting_id=posting.id,
            version_number=1,
            supersedes_evaluation_id=None,
            master_cv_version_id=cv_version.id,
            fit_report_payload={
                "requirements": [
                    {
                        "requirement_id": "R1",
                        "importance": "must_have",
                        "text": "Five years of Kubernetes operations experience",
                        "evidence_keys": [],
                        "span_status": "needs_review",
                        "established": True,
                        "dimension": "hard",
                        "requirement_key": "req-1",
                    }
                ],
            },
            decision="deprioritize",
            rationale="test",
            payload_sha256="0" * 64,
            evaluated_at=datetime.now(UTC),
        )
        session.add(evaluation)
        session.commit()
        return workspace_id, cv_version.id, evaluation.id


def test_refresh_writes_one_row_per_missed_requirement(
    durable_database: Database,
) -> None:
    """A payload with one missed requirement produces one materialized row."""
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)

    with durable_database.session() as session:
        evaluation = session.get(JobEvaluation, evaluation_id)
        assert evaluation is not None
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload=evaluation.fit_report_payload,
        )
        session.commit()

    with durable_database.session() as session:
        texts = missed_requirement_texts_in(session, workspace_id, cv_version_id)

    assert texts == frozenset({normalize("Five years of Kubernetes operations experience")})


def test_refresh_replaces_prior_rows_when_payload_changes(
    durable_database: Database,
) -> None:
    """A second refresh with a different payload drops the earlier rows."""
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)

    with durable_database.session() as session:
        first_payload = {
            "requirements": [
                {
                    "requirement_id": "R1",
                    "importance": "must_have",
                    "text": "Kubernetes",
                    "evidence_keys": [],
                    "span_status": "needs_review",
                    "established": True,
                    "requirement_key": "r-1",
                }
            ]
        }
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload=first_payload,
        )
        session.commit()

        second_payload = {
            "requirements": [
                {
                    "requirement_id": "R2",
                    "importance": "must_have",
                    "text": "Terraform",
                    "evidence_keys": [],
                    "span_status": "needs_review",
                    "established": True,
                    "requirement_key": "r-2",
                }
            ]
        }
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload=second_payload,
        )
        session.commit()

    with durable_database.session() as session:
        rows = session.scalars(
            select(MissedRequirementText).where(
                MissedRequirementText.source_evaluation_id == evaluation_id
            )
        ).all()
    assert {row.requirement_text for row in rows} == {normalize("Terraform")}


def test_refresh_ignores_requirements_with_evidence(
    durable_database: Database,
) -> None:
    """A requirement citing evidence is not a missed one, no row is written."""
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)

    with durable_database.session() as session:
        payload = {
            "requirements": [
                {
                    "requirement_id": "R3",
                    "importance": "must_have",
                    "text": "Anything",
                    "evidence_keys": ["evidence-id"],
                    "span_status": "needs_review",
                    "established": True,
                    "requirement_key": "req-cited",
                }
            ]
        }
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload=payload,
        )
        session.commit()

    with durable_database.session() as session:
        texts = missed_requirement_texts_in(session, workspace_id, cv_version_id)
    assert texts == frozenset()


def test_refresh_reads_canonical_gate_requirements(
    durable_database: Database,
) -> None:
    """Flagged gate requirements materialize like every other requirement."""
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)
    repeated = {
        "requirement_id": "R1",
        "importance": "must_have",
        "text": "Kubernetes operations",
        "evidence_keys": [],
        "span_status": "needs_review",
        "established": True,
        "dimension": "hard",
        "requirement_key": "req-1",
    }
    gate_only = {
        "requirement_id": "G1",
        "importance": "must_have",
        "text": "Calm incident leadership",
        "evidence_keys": [],
        "span_status": "needs_review",
        "established": True,
        "dimension": "behavioral",
        "requirement_key": "gate-1",
    }
    with durable_database.session() as session:
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload={
                "requirements": [
                    {**repeated, "is_hard_gate": True, "gate_kind": "minimum_years"},
                    {**gate_only, "is_hard_gate": True},
                ],
            },
        )
        session.commit()

    with durable_database.session() as session:
        texts = missed_requirement_texts_in(session, workspace_id, cv_version_id)

    assert texts == frozenset(
        {normalize("Kubernetes operations"), normalize("Calm incident leadership")}
    )


def test_refresh_refuses_a_malformed_stored_requirement(durable_database: Database) -> None:
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)

    with (
        durable_database.session() as session,
        pytest.raises(ValidationError, match="invalid shape"),
    ):
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload={"requirements": [{"text": "Missing its persisted contract"}]},
        )
