"""Both master-CV proposal aggregations produce the right rows and stay idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from clearscout.application.master_cv.proposal_aggregation import refresh_master_cv_proposals
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import SearchProposalState, new_id
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvProposal,
    MasterCvVersion,
    PostingRequirement,
    RequirementRuling,
)
from tests.support.artifacts import runtime_storage

BASE = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_current_version_id_is_absent_before_a_master_cv_import(
    durable_database: Database,
) -> None:
    """A workspace with no candidate profile has no current master-CV version."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="no-cv", name="No CV"
    )

    with durable_database.session() as session:
        assert current_version_id(session, str(workspace.id)) is None


def test_current_version_id_uses_effective_time_then_id(
    durable_database: Database,
) -> None:
    """Every consumer resolves the same deterministic current version."""
    workspace_id, _, first_id = _seed_workspace_with_cv(durable_database)
    tied_id = new_id()
    latest_id = new_id()
    with durable_database.session() as session:
        profile = session.scalar(
            select(CandidateProfile).where(CandidateProfile.workspace_id == workspace_id)
        )
        assert profile is not None
        session.add(
            MasterCvVersion(
                id=tied_id,
                candidate_profile_id=profile.id,
                source_uri="tied",
                effective_at=BASE,
                content_sha256="1" * 64,
                byte_size=1,
            )
        )
        session.flush()
        assert current_version_id(session, workspace_id) == max(first_id, tied_id)
        session.add(
            MasterCvVersion(
                id=latest_id,
                candidate_profile_id=profile.id,
                source_uri="latest",
                effective_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
                content_sha256="2" * 64,
                byte_size=1,
            )
        )
        session.flush()
        assert current_version_id(session, workspace_id) == latest_id


def _seed_workspace_with_cv(database: Database) -> tuple[str, str, str]:
    """Return (workspace_id, employer_id, master_cv_version_id) for a workspace with a CV."""
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="wsp", name="Workspace"
    )
    workspace_id = str(workspace.id)
    with database.session() as session:
        employer = Employer(
            id=new_id(),
            workspace_id=workspace_id,
            name="Globex",
            normalized_name="globex",
        )
        session.add(employer)
        session.flush()
        profile = CandidateProfile(id=new_id(), workspace_id=workspace_id, display_name="C")
        session.add(profile)
        session.flush()
        version = MasterCvVersion(
            id=new_id(),
            candidate_profile_id=profile.id,
            source_uri="seed",
            effective_at=BASE,
            content_sha256="0" * 64,
            byte_size=1,
        )
        session.add(version)
        session.flush()
        session.commit()
        return workspace_id, str(employer.id), str(version.id)


def _add_posting_with_requirement(
    database: Database,
    *,
    workspace_id: str,
    employer_id: str,
    master_cv_version_id: str,
    slug: str,
    requirement_key: str,
    requirement_text: str,
) -> str:
    """Insert one posting, one evaluation citing the CV version, one requirement."""
    with database.session() as session:
        posting = JobPosting(
            id=new_id(),
            workspace_id=workspace_id,
            employer_id=employer_id,
            title=f"Role {slug}",
            normalized_title=slug,
            first_seen_at=BASE,
        )
        session.add(posting)
        session.flush()
        evaluation = JobEvaluation(
            id=new_id(),
            job_posting_id=posting.id,
            master_cv_version_id=master_cv_version_id,
            version_number=1,
            decision="apply_now",
            rationale="",
            payload_sha256="a" * 64,
            evaluated_at=BASE,
        )
        session.add(evaluation)
        session.flush()
        session.add(
            PostingRequirement(
                id=new_id(),
                job_posting_id=posting.id,
                job_evaluation_id=evaluation.id,
                requirement_id="r01",
                requirement_key=requirement_key,
                text=requirement_text,
                importance="must_have",
                position=0,
            )
        )
        session.commit()
        return str(posting.id)


def _rule(database: Database, workspace_id: str, requirement_key: str, note: str) -> None:
    with database.session() as session:
        session.add(
            RequirementRuling(
                id=new_id(),
                workspace_id=workspace_id,
                requirement_key=requirement_key,
                ruling="met",
                note=note,
                updated_at=BASE,
            )
        )
        session.commit()


def test_never_met_aggregation_proposes_add_missing(durable_database: Database) -> None:
    """A requirement recurring across three postings without a ruling produces one add_missing."""
    workspace_id, employer_id, version_id = _seed_workspace_with_cv(durable_database)
    key = "req-abcdef"
    for slug in ("alpha", "beta", "gamma"):
        _add_posting_with_requirement(
            durable_database,
            workspace_id=workspace_id,
            employer_id=employer_id,
            master_cv_version_id=version_id,
            slug=slug,
            requirement_key=key,
            requirement_text="5 years of routing",
        )

    report = refresh_master_cv_proposals(durable_database, workspace_id)

    assert report.add_missing == 1
    assert report.promote_transferable == 0
    with durable_database.session() as session:
        rows = session.scalars(
            select(MasterCvProposal).where(MasterCvProposal.workspace_id == workspace_id)
        ).all()
    assert len(rows) == 1
    assert rows[0].kind == "add_missing"
    assert rows[0].requirement_key == key
    assert rows[0].posting_count == 3
    assert rows[0].evidence_note is None
    assert rows[0].state == SearchProposalState.PROPOSED.value


def test_met_aggregation_proposes_promote_transferable(
    durable_database: Database,
) -> None:
    """Same note ruled Met on three postings produces one promote proposal."""
    workspace_id, employer_id, version_id = _seed_workspace_with_cv(durable_database)
    key = "req-metx"
    for slug in ("alpha", "beta", "gamma"):
        _add_posting_with_requirement(
            durable_database,
            workspace_id=workspace_id,
            employer_id=employer_id,
            master_cv_version_id=version_id,
            slug=slug,
            requirement_key=key,
            requirement_text="Team leadership",
        )
    _rule(durable_database, workspace_id, key, "Managed a three-person team.")

    report = refresh_master_cv_proposals(durable_database, workspace_id)

    assert report.promote_transferable == 1
    assert report.add_missing == 0
    with durable_database.session() as session:
        rows = session.scalars(
            select(MasterCvProposal).where(MasterCvProposal.workspace_id == workspace_id)
        ).all()
    assert len(rows) == 1
    assert rows[0].kind == "promote_transferable"
    assert rows[0].requirement_key == key
    assert rows[0].evidence_note == "Managed a three-person team."
    assert rows[0].posting_count == 3


def test_aggregation_is_idempotent(durable_database: Database) -> None:
    """A second run inserts nothing when the state is unchanged."""
    workspace_id, employer_id, version_id = _seed_workspace_with_cv(durable_database)
    key = "req-idem"
    for slug in ("alpha", "beta", "gamma"):
        _add_posting_with_requirement(
            durable_database,
            workspace_id=workspace_id,
            employer_id=employer_id,
            master_cv_version_id=version_id,
            slug=slug,
            requirement_key=key,
            requirement_text="X",
        )
    first = refresh_master_cv_proposals(durable_database, workspace_id)
    second = refresh_master_cv_proposals(durable_database, workspace_id)

    assert first.add_missing == 1
    assert second.add_missing == 0
    assert second.promote_transferable == 0


def test_below_threshold_produces_nothing(durable_database: Database) -> None:
    """Two matching postings do not meet the default threshold of three."""
    workspace_id, employer_id, version_id = _seed_workspace_with_cv(durable_database)
    key = "req-two"
    for slug in ("alpha", "beta"):
        _add_posting_with_requirement(
            durable_database,
            workspace_id=workspace_id,
            employer_id=employer_id,
            master_cv_version_id=version_id,
            slug=slug,
            requirement_key=key,
            requirement_text="X",
        )
    report = refresh_master_cv_proposals(durable_database, workspace_id)

    assert report.add_missing == 0
    assert report.promote_transferable == 0
