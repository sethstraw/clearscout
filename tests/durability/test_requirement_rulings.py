"""Workspace rulings drop or mark the requirement they cover in the gaps read model."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from clearscout.application import job_search
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.job_search.models import JobDetailResponse
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.domain.rulings import Ruling
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import AuditEvent, PostingRequirement
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit
from tests.support.scenarios import fit_report as _fit_report


def _commit(
    database: Database,
    artifacts: EvidenceArtifactService,
    workspace_id: str,
    *,
    employer: str,
    index: int,
    requirements: list[dict[str, object]],
) -> JobDetailResponse:
    return job_search.commit_evaluation(
        database,
        artifacts,
        evaluation_commit(
            workspace_id,
            employer=employer,
            requisition_id=f"REQ-{index}",
            canonical_url=f"https://example.test/jobs/{index}",
            discovery_url=f"https://example.test/jobs/{index}",
            fit_report=_fit_report(*requirements),
        ),
    )


def _first_requirement_key(
    database: Database, evaluation_id: str | None, requirement_id: str
) -> str:
    assert evaluation_id is not None
    with database.session() as session:
        row = session.scalar(
            select(PostingRequirement).where(
                PostingRequirement.job_evaluation_id == evaluation_id,
                PostingRequirement.requirement_id == requirement_id,
            )
        )
    assert row is not None
    return row.requirement_key


def test_met_ruling_drops_the_requirement_from_the_gaps_panel(
    durable_database: Database, tmp_path: Path
) -> None:
    """A user assertion that the CV meets a requirement removes it from the gaps view."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ruling-met", name="Met"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    committed = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Ship weekly",
                "unmet": True,
            },
            {
                "requirement_id": "REQ-3",
                "importance": "must_have",
                "text": "SOC 2 audit",
                "unmet": True,
            },
        ],
    )
    ship_key = _first_requirement_key(durable_database, committed.evaluation_id, "REQ-1")

    baseline = job_search.requirement_gaps(durable_database, workspace.id)
    assert {gap.text for gap in baseline.gaps} == {"Ship weekly", "SOC 2 audit"}

    RequirementRulingService(durable_database).set(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        ruling=Ruling.MET,
        note="I ship weekly on the platform team.",
        audit=audit_columns("local_user", "test", None),
    )

    after = job_search.requirement_gaps(durable_database, workspace.id)
    assert {gap.text for gap in after.gaps} == {"SOC 2 audit"}


@pytest.mark.parametrize("ruling", [Ruling.DISPUTED_FIT, Ruling.DISPUTED_EXTRACTION])
def test_disputed_ruling_keeps_the_requirement_visible_with_a_flag(
    durable_database: Database, tmp_path: Path, ruling: Ruling
) -> None:
    """A dispute is not a hide; the row stays with the ruling shown alongside."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ruling-disputed", name="Disputed"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    committed = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Ship weekly",
                "unmet": True,
            },
        ],
    )
    ship_key = _first_requirement_key(durable_database, committed.evaluation_id, "REQ-1")
    RequirementRulingService(durable_database).set(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        ruling=ruling,
        note="I disagree with this reading.",
        audit=audit_columns("local_user", "test", None),
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert [gap.text for gap in page.gaps] == ["Ship weekly"]
    assert page.gaps[0].raised_by[0].ruling == ruling.value


def test_clearing_a_ruling_restores_the_requirement_to_the_gaps_panel(
    durable_database: Database, tmp_path: Path
) -> None:
    """A cleared ruling is not a delete of the requirement; the gap comes back."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ruling-clear", name="Clear"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    committed = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Ship weekly",
                "unmet": True,
            },
        ],
    )
    ship_key = _first_requirement_key(durable_database, committed.evaluation_id, "REQ-1")
    service = RequirementRulingService(durable_database)
    service.set(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        ruling=Ruling.NOT_APPLICABLE,
        note=None,
        audit=audit_columns("local_user", "test", None),
    )
    assert job_search.requirement_gaps(durable_database, workspace.id).gap_count == 0

    cleared = service.clear(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        audit=audit_columns("local_user", "test", None),
    )

    assert cleared is True
    restored = job_search.requirement_gaps(durable_database, workspace.id)
    assert [gap.text for gap in restored.gaps] == ["Ship weekly"]


def test_ruling_attaches_to_the_merged_requirement_key(
    durable_database: Database, tmp_path: Path
) -> None:
    """A ruling on a gate that repeats a requirement text finds one merged row."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ruling-merged", name="Merged"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    committed = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-9",
                "importance": "must_have",
                "text": "Canada remote",
                "unmet": True,
            },
        ],
    )
    merged_key = _first_requirement_key(durable_database, committed.evaluation_id, "REQ-9")

    RequirementRulingService(durable_database).set(
        workspace_id=workspace.id,
        requirement_key=merged_key,
        ruling=Ruling.DISPUTED_FIT,
        note="Five to six years, not eight or more.",
        audit=audit_columns("local_user", "test", None),
    )

    with durable_database.session() as session:
        rows = session.scalars(
            select(PostingRequirement).where(
                PostingRequirement.job_evaluation_id == committed.evaluation_id,
                PostingRequirement.requirement_key == merged_key,
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].is_hard_gate is True

    page = job_search.requirement_gaps(durable_database, workspace.id)
    gap = next(gap for gap in page.gaps if gap.text == "Canada remote")
    assert gap.raised_by[0].ruling == Ruling.DISPUTED_FIT.value


def test_every_ruling_change_writes_an_audit_event(
    durable_database: Database, tmp_path: Path
) -> None:
    """A ruling that lands with no audit trail cannot be recovered later."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="ruling-audit", name="Audit"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    committed = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Ship weekly",
                "unmet": True,
            },
        ],
    )
    ship_key = _first_requirement_key(durable_database, committed.evaluation_id, "REQ-1")
    service = RequirementRulingService(durable_database)

    service.set(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        ruling=Ruling.MET,
        note="one",
        audit=audit_columns("local_user", "test", None),
    )
    service.clear(
        workspace_id=workspace.id,
        requirement_key=ship_key,
        audit=audit_columns("local_user", "test", None),
    )

    with durable_database.session() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace.id,
                AuditEvent.entity_type == "requirement_ruling",
            )
            .order_by(AuditEvent.created_at)
        ).all()
    assert [event.action for event in events] == ["ruling.set", "ruling.cleared"]
