"""Materialized missed-requirement rows survive re-refresh and rebuild cleanly."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from clearscout.application.discovery.missed_requirement_reader import (
    missed_requirement_texts_in,
    normalize,
    rebuild_for_workspace,
    refresh_for_evaluation,
)
from clearscout.application.failures import ValidationError
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvVersion,
    MissedRequirementText,
    RequirementRuling,
)
from tests.support.artifacts import runtime_storage


def _seed_posting(database: Database, slug: str = "miss-check") -> tuple[str, str, str]:
    """Return (workspace_id, cv_version_id, evaluation_id) for a minimal fixture."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug=slug, name="Miss Check"
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
                        "unmet": True,
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
                    "unmet": True,
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
                    "unmet": True,
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


def test_refresh_keeps_an_unmet_requirement_quoted_from_the_posting(
    durable_database: Database,
) -> None:
    """A posting quote that locates an unmet requirement does not answer it."""
    workspace_id, cv_version_id, evaluation_id = _seed_posting(durable_database)

    with durable_database.session() as session:
        refresh_for_evaluation(
            session,
            workspace_id=workspace_id,
            master_cv_version_id=cv_version_id,
            evaluation_id=evaluation_id,
            payload={
                "requirements": [
                    {
                        "requirement_id": "R4",
                        "importance": "must_have",
                        "text": "Terraform",
                        "evidence_keys": ["requirement-1-posting"],
                        "span_status": "verified",
                        "established": True,
                        "unmet": True,
                        "requirement_key": "req-quoted",
                    }
                ]
            },
        )
        session.commit()

    with durable_database.session() as session:
        texts = missed_requirement_texts_in(session, workspace_id, cv_version_id)
    assert texts == frozenset({normalize("Terraform")})


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
        "unmet": True,
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
        "unmet": True,
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


def _unmet(key: str, text: str) -> dict[str, object]:
    return {
        "requirement_id": key,
        "importance": "must_have",
        "text": text,
        "evidence_keys": [],
        "span_status": "needs_review",
        "established": True,
        "unmet": True,
        "requirement_key": key,
    }


def seed_evaluated_history(database: Database) -> tuple[str, str]:
    """Seed a superseded and a current evaluation with no projection rows."""
    workspace_id, cv_version_id, first_id = _seed_posting(database)
    with database.session() as session:
        first = session.get(JobEvaluation, first_id)
        assert first is not None
        session.add(
            JobEvaluation(
                id=str(uuid.uuid4()),
                job_posting_id=first.job_posting_id,
                version_number=2,
                supersedes_evaluation_id=first_id,
                master_cv_version_id=cv_version_id,
                fit_report_payload={
                    "requirements": [
                        _unmet("req-2", "Terraform module authoring"),
                        _unmet("req-3", "Equal employment opportunity employer"),
                        _unmet("req-4", "Go services in production"),
                    ]
                },
                decision="deprioritize",
                rationale="test",
                payload_sha256="1" * 64,
                evaluated_at=datetime.now(UTC),
            )
        )
        session.add(
            RequirementRuling(
                workspace_id=workspace_id,
                requirement_key="req-4",
                ruling="met",
                updated_at=utc_now(),
            )
        )
        session.commit()
    return workspace_id, cv_version_id


def stored_missed_rows(database: Database, workspace_id: str) -> list[tuple[str, str]]:
    """Return (evaluation id, text) for every projection row in the workspace."""
    with database.session() as session:
        rows = session.scalars(
            select(MissedRequirementText).where(MissedRequirementText.workspace_id == workspace_id)
        ).all()
        return sorted((row.source_evaluation_id, row.requirement_text) for row in rows)


def test_rebuild_fills_rows_for_every_evaluation_and_repeats_identically(
    durable_database: Database,
) -> None:
    """Superseded and current evaluations both land; boilerplate and hidden texts do not."""
    workspace_id, cv_version_id = seed_evaluated_history(durable_database)

    with durable_database.session() as session:
        written = rebuild_for_workspace(session, workspace_id)
        session.commit()
    first = stored_missed_rows(durable_database, workspace_id)
    with durable_database.session() as session:
        repeated = rebuild_for_workspace(session, workspace_id)
        session.commit()

    with durable_database.session() as session:
        texts = missed_requirement_texts_in(session, workspace_id, cv_version_id)
    assert texts == frozenset(
        {
            normalize("Five years of Kubernetes operations experience"),
            normalize("Terraform module authoring"),
        }
    )
    assert len({evaluation_id for evaluation_id, _ in first}) == 2
    assert written == repeated == 2
    assert stored_missed_rows(durable_database, workspace_id) == first


def test_rebuilding_one_workspace_leaves_another_workspaces_rows(
    durable_database: Database,
) -> None:
    workspace_id, _ = seed_evaluated_history(durable_database)
    other_id, other_cv_id, other_evaluation_id = _seed_posting(durable_database, slug="other")
    with durable_database.session() as session:
        evaluation = session.get(JobEvaluation, other_evaluation_id)
        assert evaluation is not None
        refresh_for_evaluation(
            session,
            workspace_id=other_id,
            master_cv_version_id=other_cv_id,
            evaluation_id=other_evaluation_id,
            payload=evaluation.fit_report_payload,
        )
        session.commit()
    before = stored_missed_rows(durable_database, other_id)

    with durable_database.session() as session:
        rebuild_for_workspace(session, workspace_id)
        session.commit()

    assert before
    assert stored_missed_rows(durable_database, other_id) == before


def test_the_rebuild_command_fills_the_rows_and_says_how_many(
    durable_database: Database, durable_database_url: str
) -> None:
    from typer.testing import CliRunner

    from clearscout.bootstrap.cli import app

    workspace_id, _ = seed_evaluated_history(durable_database)

    result = CliRunner().invoke(
        app,
        [
            "db",
            "rebuild-missed-requirements",
            "--workspace-id",
            workspace_id,
            "--database-url",
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Rebuilt 2 missed-requirement rows" in result.output
    assert {text for _, text in stored_missed_rows(durable_database, workspace_id)} == {
        normalize("Five years of Kubernetes operations experience"),
        normalize("Terraform module authoring"),
    }
