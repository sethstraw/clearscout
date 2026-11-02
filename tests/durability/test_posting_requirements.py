"""Requirement identity threads one posting's requirements across re-evaluations."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from tailorcv.application import job_search
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.job_search.models import JobDetailResponse
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PostingRequirement
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit
from tests.support.scenarios import fit_report as _fit_report


def _commit(
    database: Database,
    artifacts: EvidenceArtifactService,
    workspace_id: str,
    *,
    requirements: list[dict[str, object]],
    hard_gates: list[dict[str, object]] | None = None,
    supersedes: str | None = None,
    note: str = "First reading of the posting.",
) -> JobDetailResponse:
    """Commit one evaluation of one posting, superseding the previous when given.

    ``note`` is written to the proposal's rationale so its digest differs from
    the previous commit's.
    """
    report = _fit_report(*requirements)
    if hard_gates is not None:
        report["hard_gates"] = hard_gates
    return job_search.commit_evaluation(
        database,
        artifacts,
        evaluation_commit(
            workspace_id,
            employer="Globex",
            requisition_id="REQ-42",
            canonical_url="https://example.test/jobs/42",
            discovery_url="https://example.test/jobs/42",
            fit_report=report,
            supersedes_evaluation_id=supersedes,
            rationale=note,
        ),
    )


def _keys_by_id(database: Database, evaluation_id: str | None) -> dict[str, str]:
    assert evaluation_id is not None
    """Return the durable key each stored requirement carries, keyed by positional id."""
    with database.session() as session:
        rows = session.scalars(
            select(PostingRequirement).where(PostingRequirement.job_evaluation_id == evaluation_id)
        ).all()
    return {row.requirement_id: row.requirement_key for row in rows}


def _keys_by_kind(database: Database, evaluation_id: str | None) -> dict[str | None, str]:
    assert evaluation_id is not None
    """Return the durable key each stored requirement carries, keyed by its gate kind."""
    with database.session() as session:
        rows = session.scalars(
            select(PostingRequirement).where(PostingRequirement.job_evaluation_id == evaluation_id)
        ).all()
    return {row.gate_kind: row.requirement_key for row in rows}


def test_a_requirement_keeps_its_key_across_re_evaluations_of_a_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    """A citation stored under a requirement's identity resolves after a rerun."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="carry", name="Carry"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    requirement: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": "Lead a governed data platform",
        "evidence_keys": ["candidate-platform"],
    }

    first = _commit(durable_database, artifacts, workspace.id, requirements=[requirement])
    rerun = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[requirement],
        supersedes=first.evaluation_id,
        note="Second reading after the master CV was updated.",
    )

    original_keys = _keys_by_id(durable_database, first.evaluation_id)
    rerun_keys = _keys_by_id(durable_database, rerun.evaluation_id)
    assert original_keys["REQ-1"] == rerun_keys["REQ-1"]


def test_a_reworded_requirement_within_a_posting_keeps_its_key(
    durable_database: Database, tmp_path: Path
) -> None:
    """A slight rewording under one posting's scope carries the identity forward."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="reword", name="Reword"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    original: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": "Lead a governed data platform including retention and deletion policy",
        "evidence_keys": ["candidate-platform"],
    }
    reworded: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": "Lead a governed data platform, including its retention and deletion policy",
        "evidence_keys": ["candidate-platform"],
    }

    first = _commit(durable_database, artifacts, workspace.id, requirements=[original])
    rerun = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[reworded],
        supersedes=first.evaluation_id,
        note="Second reading after the master CV was updated.",
    )

    original_keys = _keys_by_id(durable_database, first.evaluation_id)
    rerun_keys = _keys_by_id(durable_database, rerun.evaluation_id)
    assert original_keys["REQ-1"] == rerun_keys["REQ-1"]


def test_two_gates_quoting_one_requirement_line_each_keep_their_key_across_re_evaluations(
    durable_database: Database, tmp_path: Path
) -> None:
    """Each gate's identity follows its kind, whichever gate the rerun states first."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="two-gates", name="Two Gates"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    line = "Bachelor's degree and 5+ years of data engineering experience"
    requirement: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": line,
        "evidence_keys": [],
    }
    degree: dict[str, object] = {
        "requirement_id": "gate-1",
        "importance": "must_have",
        "text": line,
        "evidence_keys": [],
        "gate_kind": "degree",
    }
    years = {**degree, "requirement_id": "gate-2", "gate_kind": "minimum_years"}

    first = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[requirement],
        hard_gates=[degree, years],
    )
    rerun = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[requirement],
        hard_gates=[{**years, "requirement_id": "gate-1"}, {**degree, "requirement_id": "gate-2"}],
        supersedes=first.evaluation_id,
        note="Second reading after the master CV was updated.",
    )

    assert _keys_by_kind(durable_database, rerun.evaluation_id) == _keys_by_kind(
        durable_database, first.evaluation_id
    )


def test_a_repeated_requirement_line_keeps_each_key_across_re_evaluations(
    durable_database: Database, tmp_path: Path
) -> None:
    """Two statements of one text pair with the previous two in reading order."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="repeat", name="Repeat"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    repeated: list[dict[str, object]] = [
        {
            "requirement_id": requirement_id,
            "importance": "must_have",
            "text": "Lead a governed data platform",
            "evidence_keys": [],
        }
        for requirement_id in ("REQ-1", "REQ-2")
    ]

    first = _commit(durable_database, artifacts, workspace.id, requirements=repeated)
    rerun = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=repeated,
        supersedes=first.evaluation_id,
        note="Second reading after the master CV was updated.",
    )

    assert _keys_by_id(durable_database, rerun.evaluation_id) == _keys_by_id(
        durable_database, first.evaluation_id
    )


def test_a_new_requirement_starts_its_own_key(durable_database: Database, tmp_path: Path) -> None:
    """A requirement that does not match a prior one gets a fresh identity."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="new-req", name="New"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    original: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": "Lead a governed data platform",
        "evidence_keys": ["candidate-platform"],
    }
    fresh: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": "Read and write technical Portuguese daily",
        "evidence_keys": ["candidate-platform"],
    }

    first = _commit(durable_database, artifacts, workspace.id, requirements=[original])
    rerun = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[fresh],
        supersedes=first.evaluation_id,
        note="Second reading after the master CV was updated.",
    )

    original_keys = _keys_by_id(durable_database, first.evaluation_id)
    rerun_keys = _keys_by_id(durable_database, rerun.evaluation_id)
    assert original_keys["REQ-1"] != rerun_keys["REQ-1"]


def test_match_evidence_carries_the_requirement_key_of_the_cited_requirement(
    durable_database: Database, tmp_path: Path
) -> None:
    """A citation stored under a positional id must also carry the durable identity."""
    from tailorcv.infrastructure.persistence.models import MatchEvidence

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="evidence", name="Evidence"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")

    first = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Lead a data platform",
                "evidence_keys": ["candidate-platform"],
            }
        ],
    )

    with durable_database.session() as session:
        evidence_rows = session.scalars(
            select(MatchEvidence).where(MatchEvidence.evaluation_id == first.evaluation_id)
        ).all()
        keyed = {
            row.requirement_id: row.requirement_key for row in evidence_rows if row.requirement_id
        }
    requirement_keys = _keys_by_id(durable_database, first.evaluation_id)
    for requirement_id, key in keyed.items():
        assert key == requirement_keys[requirement_id]


def test_gate_and_requirement_share_one_row_after_commit1_projection(
    durable_database: Database, tmp_path: Path
) -> None:
    """A hard gate whose text repeats a requirement commits as one durable row."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="gate-fold", name="Gate Fold"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    fit_report = _fit_report(
        {
            "requirement_id": "REQ-9",
            "importance": "must_have",
            "text": "Canada remote",
            "evidence_keys": [],
        }
    )

    committed = job_search.commit_evaluation(
        durable_database,
        artifacts,
        evaluation_commit(
            workspace.id,
            employer="Globex",
            requisition_id="REQ-42",
            canonical_url="https://example.test/jobs/42",
            discovery_url="https://example.test/jobs/42",
            fit_report=fit_report,
        ),
    )

    with durable_database.session() as session:
        rows = session.scalars(
            select(PostingRequirement).where(
                PostingRequirement.job_evaluation_id == committed.evaluation_id,
                PostingRequirement.text == "Canada remote",
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].requirement_id == "REQ-9"
    assert rows[0].is_hard_gate is True
    assert rows[0].requirement_key


def test_the_first_evaluation_assigns_a_new_key_per_requirement(
    durable_database: Database, tmp_path: Path
) -> None:
    """No prior evaluation means every current requirement earns a fresh key."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="fresh-eval", name="Fresh"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")

    first = _commit(
        durable_database,
        artifacts,
        workspace.id,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": "Lead a data platform",
                "evidence_keys": ["candidate-platform"],
            },
            {
                "requirement_id": "REQ-2",
                "importance": "nice_to_have",
                "text": "Own an on-call rotation",
                "evidence_keys": [],
                "established": False,
            },
        ],
    )

    keys = _keys_by_id(durable_database, first.evaluation_id)
    assert set(keys) == {"REQ-1", "REQ-2"}
    assert keys["REQ-1"] and keys["REQ-2"]
    assert keys["REQ-1"] != keys["REQ-2"]
