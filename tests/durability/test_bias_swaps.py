"""Pin the replay solver's read path against text-shaped swaps.

The bias invariants a live match stage should hold, that swapping the
candidate's name or the employer's does not shift a stored classification,
cannot be measured here without a real model call. This test pins the closest
deterministic property: the replay solver reports whatever the database stores,
regardless of the requirement text the sample presents. If the solver ever grew
a text-sensitive short-circuit, this test would fail. Measuring name and
employer swap bias itself belongs behind the harness's ``--live`` mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from evals.inspect.scorers.match_classification import stored_classification_consistency
from evals.inspect.solvers.clearscout_replay import clearscout_replay_solver

from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migration import upgrade
from clearscout.infrastructure.persistence.models.artifacts import Artifact
from clearscout.infrastructure.persistence.models.discovery import Employer, JobPosting
from clearscout.infrastructure.persistence.models.evaluations import (
    JobEvaluation,
    MatchEvidence,
    PostingRequirement,
)
from clearscout.infrastructure.persistence.models.workspaces import Workspace

REQUIREMENT_KEY = "a1a1a1a1-1111-4111-8111-111111111111"


def _seed_database(
    url: str, requirement_text: str, *, classification: str | None = "DIRECT"
) -> tuple[str, str]:
    """Insert one workspace, posting, evaluation, requirement, and match evidence.

    ``classification`` of ``None`` stores the requirement with no evidence row.
    """
    database = Database(url)
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            workspace = Workspace(slug="ws", name="Workspace")
            session.add(workspace)
            session.flush()
            employer = Employer(
                workspace_id=workspace.id,
                name="Contoso",
                normalized_name="contoso",
            )
            session.add(employer)
            session.flush()
            posting = JobPosting(
                workspace_id=workspace.id,
                employer_id=employer.id,
                title="Backend Engineer",
                normalized_title="backend engineer",
                first_seen_at=now,
            )
            session.add(posting)
            session.flush()
            artifact = Artifact(
                workspace_id=workspace.id,
                logical_type="posting.body.text",
                mime_type="text/plain",
                storage_uri="mem://fixture",
                byte_size=1,
                sha256="0" * 64,
            )
            session.add(artifact)
            session.flush()
            evaluation = JobEvaluation(
                job_posting_id=posting.id,
                version_number=1,
                decision="apply_now",
                rationale="",
                scope_summary="",
                qualifications_summary="",
                payload_sha256="0" * 64,
                evaluated_at=now,
            )
            session.add(evaluation)
            session.flush()
            session.add(
                PostingRequirement(
                    job_posting_id=posting.id,
                    job_evaluation_id=evaluation.id,
                    requirement_id="r01",
                    requirement_key=REQUIREMENT_KEY,
                    text=requirement_text,
                    importance="must_have",
                    position=1,
                )
            )
            if classification is not None:
                session.add(
                    MatchEvidence(
                        evaluation_id=evaluation.id,
                        artifact_id=artifact.id,
                        master_cv_entry_id="cv-1",
                        master_cv_entry_key=(
                            "e1e1e1e1-2222-4222-8222-222222222222"
                            if classification != "GAP"
                            else None
                        ),
                        requirement_id="r01",
                        requirement_key=REQUIREMENT_KEY,
                        evidence_text="shipped a python service",
                        locator="line:1",
                        source_kind="candidate",
                        classification=classification,
                        strength="strong" if classification != "GAP" else "none",
                    )
                )
            return posting.id, evaluation.id
    finally:
        database.dispose()


def _solve(url: str, posting_id: str, requirement_text: str) -> dict[str, object]:
    sample = {
        "id": "s1",
        "input": requirement_text,
        "target": "DIRECT",
        "metadata": {
            "posting_id": posting_id,
            "requirement_key": REQUIREMENT_KEY,
            "database_url": url,
        },
    }
    return dict(clearscout_replay_solver()(sample))


def test_stored_classification_is_independent_of_requirement_text(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.sqlite3"
    baseline_url = f"sqlite+pysqlite:///{baseline}"
    upgrade(baseline_url)
    posting_id, _ = _seed_database(baseline_url, "Ship production Python services at Contoso.")

    first = _solve(baseline_url, posting_id, "Ship production Python services.")
    second = _solve(
        baseline_url,
        posting_id,
        "Different Name should still see the same stored answer here.",
    )

    assert first["completion"] == "DIRECT"
    assert first["completion"] == second["completion"]
    assert first["metadata"]["entry_keys"] == second["metadata"]["entry_keys"]  # type: ignore[index]


def test_missing_stored_classification_is_reported_as_unknown(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    url = f"sqlite+pysqlite:///{empty}"
    upgrade(url)

    result = _solve(url, "posting-not-present", "Any requirement text.")

    assert result["completion"] == "UNKNOWN"
    assert result["metadata"]["missing"] is True  # type: ignore[index]


def test_a_requirement_with_no_stored_evidence_row_is_not_credited_as_a_gap(
    tmp_path: Path,
) -> None:
    """A requirement the ledger never answered fails against a frozen GAP label."""
    unanswered = tmp_path / "unanswered.sqlite3"
    url = f"sqlite+pysqlite:///{unanswered}"
    upgrade(url)
    posting_id, _ = _seed_database(url, "Ship production Python services.", classification=None)

    result = _solve(url, posting_id, "Ship production Python services.")
    score = stored_classification_consistency()({"current_classification": "GAP"}, result)

    assert result["completion"] == "GAP"
    assert result["metadata"]["outcome_complete"] is False  # type: ignore[index]
    assert result["metadata"]["missing"] is False  # type: ignore[index]
    assert score["value"] is False
    assert "run did not establish this requirement" in score["explanation"]


def test_a_stored_gap_row_is_credited_as_a_gap(tmp_path: Path) -> None:
    """A gap the ledger actually stores still agrees with a frozen GAP label."""
    stored = tmp_path / "stored-gap.sqlite3"
    url = f"sqlite+pysqlite:///{stored}"
    upgrade(url)
    posting_id, _ = _seed_database(url, "Ship production Python services.", classification="GAP")

    result = _solve(url, posting_id, "Ship production Python services.")
    score = stored_classification_consistency()({"current_classification": "GAP"}, result)

    assert result["completion"] == "GAP"
    assert result["metadata"]["outcome_complete"] is True  # type: ignore[index]
    assert score["value"] is True


def test_replay_refuses_a_sample_without_ledger_identity(monkeypatch) -> None:
    monkeypatch.delenv("CLEARSCOUT_EVAL_DATABASE_URL", raising=False)
    solver = clearscout_replay_solver()

    with pytest.raises(ValueError, match="posting_id"):
        solver({"metadata": {}})
    with pytest.raises(ValueError, match="requirement_key"):
        solver({"metadata": {"posting_id": "posting-1"}})
    with pytest.raises(ValueError, match="database_url"):
        solver({"metadata": {"posting_id": "posting-1", "requirement_key": REQUIREMENT_KEY}})
