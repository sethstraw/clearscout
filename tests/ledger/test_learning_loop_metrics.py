"""Learning-loop metrics count the three durable signals the product exposes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.failures import NotFoundError
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.application.workspaces.metrics import LearningLoopMetricsService
from tailorcv.domain import SearchProposalState, new_id, utc_now
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.statements import derived_proposal_identity
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    DerivedSearchTitle,
    MasterCvProposal,
    MasterCvVersion,
    PendingDiscovery,
)
from tests.support.artifacts import runtime_storage


def _cv_version(database: Database, workspace_id: str, marker: str) -> str:
    """Insert one master CV version and return its id."""
    with database.session() as session:
        profile = session.scalars(
            select(CandidateProfile).where(CandidateProfile.workspace_id == workspace_id).limit(1)
        ).first()
        if profile is None:
            profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
            session.add(profile)
            session.flush()
        version = MasterCvVersion(
            candidate_profile_id=profile.id,
            content_sha256=(marker * 32)[:64],
            source_uri=f"file:///cv-{marker}.md",
            effective_at=utc_now(),
            byte_size=1,
        )
        session.add(version)
        session.flush()
        return str(version.id)


def _service(database: Database) -> LearningLoopMetricsService:
    return LearningLoopMetricsService(database, SourceScanQueries(database))


def test_reproposed_identity_counts_a_proposed_row_matching_a_prior_rejection(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="loop", name="Loop"
    )
    workspace_id = str(workspace.id)
    older = _cv_version(durable_database, workspace_id, "a")
    current = _cv_version(durable_database, workspace_id, "b")
    identity = derived_proposal_identity(normalize_text("Prompt Engineer"))
    with durable_database.session() as session:
        session.add_all(
            [
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=older,
                    title="Prompt Engineer",
                    normalized_title=normalize_text("Prompt Engineer"),
                    reason="prior",
                    evidence_ids=["e1"],
                    lane=None,
                    position=0,
                    state=SearchProposalState.REJECTED.value,
                    origin="test",
                    correlation_id=None,
                    carried_identity=identity,
                    decided_at=utc_now(),
                ),
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=current,
                    title="Prompt Engineer",
                    normalized_title=normalize_text("Prompt Engineer"),
                    reason="re-suggested",
                    evidence_ids=["e2"],
                    lane=None,
                    position=0,
                    state=SearchProposalState.PROPOSED.value,
                    origin="test",
                    correlation_id=None,
                    carried_identity=identity,
                ),
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=current,
                    title="Novel Title",
                    normalized_title=normalize_text("Novel Title"),
                    reason="fresh",
                    evidence_ids=["e3"],
                    lane=None,
                    position=1,
                    state=SearchProposalState.PROPOSED.value,
                    origin="test",
                    correlation_id=None,
                    carried_identity=None,
                ),
            ]
        )
        session.commit()

    metrics = _service(durable_database).read(workspace_id)

    assert metrics.reproposed_rejected_identities.numerator == 1
    assert metrics.reproposed_rejected_identities.denominator == 1


def test_master_cv_proposal_acceptance_is_accepted_over_total(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="cv", name="CV"
    )
    workspace_id = str(workspace.id)
    base = datetime(2026, 5, 1, tzinfo=UTC)
    with durable_database.session() as session:
        session.add_all(
            [
                MasterCvProposal(
                    id=new_id(),
                    workspace_id=workspace_id,
                    kind="add_missing",
                    requirement_key="req-a",
                    requirement_text="X",
                    posting_count=3,
                    first_seen_at=base,
                    last_seen_at=base,
                    proposed_at=base,
                    state=SearchProposalState.ACCEPTED.value,
                    decided_at=base,
                ),
                MasterCvProposal(
                    id=new_id(),
                    workspace_id=workspace_id,
                    kind="add_missing",
                    requirement_key="req-b",
                    requirement_text="Y",
                    posting_count=3,
                    first_seen_at=base,
                    last_seen_at=base,
                    proposed_at=base,
                    state=SearchProposalState.REJECTED.value,
                    decided_at=base,
                ),
            ]
        )
        session.commit()

    metrics = _service(durable_database).read(workspace_id)

    assert metrics.master_cv_proposal_acceptance.numerator == 1
    assert metrics.master_cv_proposal_acceptance.denominator == 2


def test_pending_queue_missed_matches_reports_zero_when_no_missed_texts(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="pq", name="PQ"
    )
    workspace_id = str(workspace.id)
    now = utc_now()
    with durable_database.session() as session:
        for index in range(2):
            session.add(
                PendingDiscovery(
                    id=new_id(),
                    workspace_id=workspace_id,
                    fingerprint=new_id(),
                    employer=f"Employer {index}",
                    title=f"Role {index}",
                    location="Remote",
                    requisition_id=f"REQ-{index}",
                    discovery_url=f"https://example.test/jobs/{index}",
                    canonical_url=f"https://example.test/jobs/{index}",
                    reason="matched",
                    first_seen_on=now.date(),
                )
            )
        session.commit()

    metrics = _service(durable_database).read(workspace_id)

    assert metrics.pending_queue_missed_matches.numerator == 0
    assert metrics.pending_queue_missed_matches.denominator == 2
    assert metrics.top_n == 10


def test_read_refuses_an_unknown_workspace(durable_database: Database) -> None:
    with pytest.raises(NotFoundError):
        _service(durable_database).read("does-not-exist")
