"""The scoring policy: what a weight set is, and what a zero weight means.

The shipped default gives evidence zero weight, because folding evidence quality
into the overall makes a job score higher for having been documented better. A
zero-weighted component is not part of the composite and cannot suppress one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from clearscout.application import job_search
from clearscout.application.contracts import (
    SCORE_COMPONENTS,
    EvaluationCommit,
    EvaluationScores,
    ScoreComponentInput,
    score_weight_digest,
)
from clearscout.application.job_search.models import JobListQuery
from clearscout.application.workspaces import WorkspaceService
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migrations.versions import v0001_schema
from clearscout.infrastructure.persistence.models import ScoreWeightComponent, ScoreWeightSet
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit

EVIDENCE_FREE = {
    "fit": Decimal("0.40"),
    "compensation": Decimal("0.25"),
    "eligibility": Decimal("0.25"),
    "evidence": Decimal("0.00"),
    "opportunity": Decimal("0.10"),
}


def _component(value: int | None) -> ScoreComponentInput:
    """One component, abstaining with a reason when no value is given."""
    if value is None:
        return ScoreComponentInput(value=None, reason="No evidence was stored for this component.")
    return ScoreComponentInput(value=value)


def _scores(**values: int | None) -> EvaluationScores:
    """Build a component set, defaulting every unnamed component to 80."""
    return EvaluationScores(**{name: _component(values.get(name, 80)) for name in SCORE_COMPONENTS})


def test_zero_weighted_component_is_absent_from_the_composite() -> None:
    """Evidence cannot move the overall once its weight is zero."""
    high = _scores(evidence=100).overall(EVIDENCE_FREE)
    low = _scores(evidence=0).overall(EVIDENCE_FREE)
    assert high == low == 80


def test_zero_weighted_component_may_abstain_without_suppressing_the_overall() -> None:
    """A zero-weighted component is outside the composite, so abstaining leaves it scoreable."""
    assert _scores(evidence=None).overall(EVIDENCE_FREE) == 80


def test_a_weighted_component_still_suppresses_the_overall() -> None:
    """Abstention still propagates for anything that actually counts."""
    assert _scores(fit=None).overall(EVIDENCE_FREE) is None
    assert _scores(opportunity=None).overall(EVIDENCE_FREE) is None


def test_overall_is_the_weighted_sum_of_what_counts() -> None:
    scores = _scores(fit=100, compensation=60, eligibility=40, opportunity=20, evidence=0)
    # 100*0.40 + 60*0.25 + 40*0.25 + 20*0.10 = 40 + 15 + 10 + 2
    assert scores.overall(EVIDENCE_FREE) == 67


def test_migrated_database_carries_the_shipped_policy(durable_database: Database) -> None:
    """A fresh database ships exactly one fallback policy, at version 1.

    Migrations stay self-contained, so the revision carries its digest as a
    constant instead of calling the helper. Recomputing it here is what stops
    that constant and `score_weight_digest` diverging. The component
    assertions hold the shape the commit path would otherwise reject.
    """
    assert score_weight_digest(dict(v0001_schema.DEFAULT_WEIGHTS)) == (
        v0001_schema.DEFAULT_WEIGHT_DIGEST
    )
    with durable_database.session() as session:
        sets = session.scalars(
            select(ScoreWeightSet).where(ScoreWeightSet.workspace_id.is_(None))
        ).all()
        by_version = {item.version: item for item in sets}
        assert set(by_version) == {1}, "only one policy should be shipped"
        assert by_version[1].content_sha256 == v0001_schema.DEFAULT_WEIGHT_DIGEST

        rows = session.scalars(
            select(ScoreWeightComponent).where(
                ScoreWeightComponent.weight_set_id == v0001_schema.DEFAULT_WEIGHT_SET_ID
            )
        ).all()
        weights = {row.component: Decimal(row.weight) for row in rows}

    assert set(weights) == set(SCORE_COMPONENTS)
    assert sum(weights.values(), Decimal(0)) == Decimal(1)
    assert weights["evidence"] == Decimal(0)


def test_null_score_preserves_not_stated_and_suppresses_composite(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="abstain", name="Abstain"
    )
    payload = evaluation_commit(workspace.id).model_dump(mode="python")
    payload["scores"]["compensation"] = {
        "value": None,
        "reason": "The posting states no compensation range.",
        "conflict": False,
    }
    for item in payload["score_evidence"]:
        item["components"] = [
            component for component in item["components"] if component != "compensation"
        ]
    proposal = EvaluationCommit.model_validate(payload)

    detail = job_search.commit_evaluation(
        durable_database, artifact_service(durable_database, tmp_path / "artifacts"), proposal
    )

    compensation = next(
        item for item in detail.score_arithmetic if item.component == "compensation"
    )
    assert compensation.score is None
    assert compensation.finding_state == "not_stated"
    assert compensation.reason == "The posting states no compensation range."
    assert {item.component for item in detail.score_arithmetic} == {
        "fit",
        "compensation",
        "eligibility",
        "evidence",
        "opportunity",
    }
    assert detail.overall_score is None
    assert detail.completeness.status == "complete"

    exact = job_search.commit_evaluation(
        durable_database,
        artifact_service(durable_database, tmp_path / "artifacts"),
        evaluation_commit(
            workspace.id,
            employer="Aster Systems",
            title="Exact Product Manager",
            requisition_id="REQ-43",
            canonical_url="https://example.test/jobs/43",
            discovery_url="https://example.test/jobs/43",
            scores=EvaluationScores(
                fit=ScoreComponentInput(value=68),
                compensation=ScoreComponentInput(value=68),
                eligibility=ScoreComponentInput(value=68),
                evidence=ScoreComponentInput(value=68),
                opportunity=ScoreComponentInput(value=68),
            ),
        ),
    )

    exact_rows, total = job_search.list_jobs(
        durable_database, JobListQuery(workspace_id=workspace.id, sort="overall_score")
    )
    assert total == 2
    assert exact_rows[0].job_posting_id == exact.job_posting_id
    assert exact_rows[1].overall_score is None
    assert exact_rows[1].overall_score_lower_bound == 68
    assert exact_rows[1].unscored_weighted_component_count == 1

    for descending in (False, True):
        recorded_rows, _ = job_search.list_jobs(
            durable_database,
            JobListQuery(
                workspace_id=workspace.id,
                sort="recorded_score",
                descending=descending,
            ),
        )
        assert [row.job_posting_id for row in recorded_rows] == [
            exact.job_posting_id,
            detail.job_posting_id,
        ]


def _client(database_url: str, artifacts: Path) -> TestClient:
    return TestClient(create_app(database_url, artifacts), base_url="http://localhost")


def test_policies_are_appended_never_edited(durable_database_url: str, tmp_path: Path) -> None:
    """Authoring a policy adds a version; it never rewrites an existing one."""
    with _client(durable_database_url, tmp_path / "artifacts") as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "weights", "name": "Weights"}
        ).json()["id"]

        listed = client.get(f"/api/v1/workspaces/{workspace_id}/score-weight-sets")
        assert listed.status_code == 200
        shipped = listed.json()
        assert [item["version"] for item in shipped] == [1]
        assert all(item["is_shipped_default"] for item in shipped)
        # The shipped default is what a new evaluation would use.
        assert [item["id"] for item in shipped if item["is_active"]] == [shipped[0]["id"]]

        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/score-weight-sets",
            json={
                "name": "Pay first",
                "weights": {
                    "fit": "0.30",
                    "compensation": "0.40",
                    "eligibility": "0.20",
                    "evidence": "0.00",
                    "opportunity": "0.10",
                },
            },
        )
        assert created.status_code == 201
        assert created.json()["version"] == 1
        assert created.json()["is_shipped_default"] is False
        assert created.json()["is_active"] is True

        after = client.get(f"/api/v1/workspaces/{workspace_id}/score-weight-sets").json()
        assert len(after) == len(shipped) + 1
        assert {item["name"] for item in after} == {"ClearScout default", "Pay first"}

        # Authoring a policy makes it this workspace's own, so it takes over
        # from the shipped default without anybody naming it anywhere.
        assert [item["name"] for item in after if item["is_active"]] == ["Pay first"]

        # The shipped policy is otherwise untouched: same id, same version, same
        # weights as before the write.
        def _without_active(items: list[dict[str, object]]) -> list[dict[str, object]]:
            return [{k: v for k, v in item.items() if k != "is_active"} for item in items]

        assert _without_active([item for item in after if item["is_shipped_default"]]) == (
            _without_active(shipped)
        )


def test_a_policy_that_could_not_score_an_evaluation_is_refused(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The commit path and the authoring path hold the same boundary."""
    with _client(durable_database_url, tmp_path / "artifacts") as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "invalid", "name": "Invalid"}
        ).json()["id"]
        endpoint = f"/api/v1/workspaces/{workspace_id}/score-weight-sets"

        short = client.post(
            endpoint,
            json={"name": "Does not sum", "weights": {**dict.fromkeys(SCORE_COMPONENTS, "0.10")}},
        )
        assert short.status_code == 422
        assert "sum to 1" in short.json()["detail"]

        missing = client.post(
            endpoint,
            json={"name": "Missing a component", "weights": {"fit": "1.00"}},
        )
        assert missing.status_code == 422
        assert missing.json()["type"] == "urn:clearscout:problem:request-validation"

        unexpected = client.post(
            endpoint,
            json={
                "name": "Unknown component",
                "weights": {
                    **dict.fromkeys(SCORE_COMPONENTS, "0.20"),
                    "novelty": "0.00",
                },
            },
        )
        assert unexpected.status_code == 422
        assert unexpected.json()["type"] == "urn:clearscout:problem:request-validation"

        unnamed = client.post(
            endpoint,
            json={
                "name": "   ",
                "weights": {
                    "fit": "0.40",
                    "compensation": "0.25",
                    "eligibility": "0.25",
                    "evidence": "0.00",
                    "opportunity": "0.10",
                },
            },
        )
        assert unnamed.status_code == 422


def test_identical_weights_are_the_same_policy(durable_database_url: str, tmp_path: Path) -> None:
    """The digest is unique across the database, so duplicates are named as such.

    Without this the insert would fail on a constraint the user cannot see.
    """
    with _client(durable_database_url, tmp_path / "artifacts") as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "duplicate", "name": "Duplicate"}
        ).json()["id"]
        shipped = {
            "fit": "0.40",
            "compensation": "0.25",
            "eligibility": "0.25",
            "evidence": "0.00",
            "opportunity": "0.10",
        }
        clash = client.post(
            f"/api/v1/workspaces/{workspace_id}/score-weight-sets",
            json={"name": "My own weights", "weights": shipped},
        )
        assert clash.status_code == 409
        assert "already exist" in clash.json()["detail"]
        assert "ClearScout default" in clash.json()["detail"]
