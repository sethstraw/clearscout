"""Cross-posting view of what the master CV could not answer."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest

from clearscout.application import job_search
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import JobDetailResponse
from clearscout.application.workspaces import WorkspaceService
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import JobEvaluation
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit
from tests.support.scenarios import fit_report as _fit_report

GOVERNANCE = (
    "Five or more years owning a regulated data platform, including the\n"
    "retention and deletion policy that governs it"
)
# The package re-exports the query function under its module name, so the module
# holding the cap has to be reached through the import system.
GAPS_MODULE = import_module("clearscout.application.job_search.requirement_gaps")

ON_CALL = "Shares a weekly on-call rotation (evenings, weekends, statutory holidays)"
ANSWERED = "Lead a data platform"
UNSETTLED = "Reads and writes technical Portuguese"


def _commit(
    database: Database,
    artifacts: EvidenceArtifactService,
    workspace_id: str,
    *,
    employer: str,
    index: int,
    requirements: list[dict[str, object]],
    supersedes: str | None = None,
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
            supersedes_evaluation_id=supersedes,
        ),
    )


def test_the_same_unanswered_requirement_groups_across_postings(
    durable_database: Database, tmp_path: Path
) -> None:
    """A requirement stored as unmet is a gap; an answered or unsettled one is not."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="gaps", name="Gaps"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    governance: dict[str, object] = {
        "requirement_id": "REQ-7",
        "importance": "must_have",
        "text": GOVERNANCE,
        "unmet": True,
    }
    globex = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            governance,
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": ANSWERED,
                "evidence_keys": ["candidate-platform"],
            },
        ],
    )
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Initech",
        index=2,
        requirements=[
            governance,
            {
                "requirement_id": "REQ-9",
                "importance": "nice_to_have",
                "text": UNSETTLED,
                "established": False,
            },
        ],
    )
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Northwind",
        index=3,
        requirements=[
            {
                "requirement_id": "REQ-4",
                "importance": "nice_to_have",
                "text": ON_CALL,
                "unmet": True,
            }
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert page.evaluations_read == 3
    assert page.gap_count == 2
    assert page.omitted_count == 0
    assert [gap.text for gap in page.gaps] == [GOVERNANCE, ON_CALL]
    assert [source.employer for source in page.gaps[0].raised_by] == ["Globex", "Initech"]
    assert [source.employer for source in page.gaps[1].raised_by] == ["Northwind"]
    assert page.gaps[0].raised_by[0].job_posting_id == globex.job_posting_id
    assert page.gaps[0].raised_by[0].importance == "must_have"
    assert page.gaps[1].raised_by[0].importance == "nice_to_have"


def test_an_unmet_requirement_quoted_from_the_posting_is_still_a_gap(
    durable_database: Database, tmp_path: Path
) -> None:
    """The posting quote that locates a requirement is not CV evidence answering it."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="quoted", name="Quoted"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": GOVERNANCE,
                "evidence_keys": ["posting-location"],
                "unmet": True,
            }
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert [gap.text for gap in page.gaps] == [GOVERNANCE]


def test_the_summary_states_the_rule_two_postings_are_grouped_under(
    durable_database: Database, tmp_path: Path
) -> None:
    """The panel names the rule so grouping cannot be misread as a claim of sameness."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="rule", name="Rule"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": GOVERNANCE,
                "unmet": True,
            }
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert "grouped" in page.grouping_rule.casefold()
    assert "same thing" in page.grouping_rule.casefold()


def test_case_and_whitespace_differences_group_together(
    durable_database: Database, tmp_path: Path
) -> None:
    """Two postings with the same requirement in different casing share one gap."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="normal", name="Normalize"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    original = "Five years leading a data platform"
    variant = "five years  leading a data\nplatform"
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {"requirement_id": "REQ-1", "importance": "must_have", "text": original, "unmet": True}
        ],
    )
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Initech",
        index=2,
        requirements=[
            {"requirement_id": "REQ-1", "importance": "must_have", "text": variant, "unmet": True}
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert page.gap_count == 1
    assert len(page.gaps[0].raised_by) == 2


def test_min_raised_drops_singletons(durable_database: Database, tmp_path: Path) -> None:
    """A one-posting gap is filtered out when the reader wants recurring ones only."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="min", name="Min"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    shared: dict[str, object] = {
        "requirement_id": "REQ-1",
        "importance": "must_have",
        "text": GOVERNANCE,
        "unmet": True,
    }
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            shared,
            {
                "requirement_id": "REQ-2",
                "importance": "nice_to_have",
                "text": ON_CALL,
                "unmet": True,
            },
        ],
    )
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Initech",
        index=2,
        requirements=[shared],
    )

    unfiltered = job_search.requirement_gaps(durable_database, workspace.id)
    recurring = job_search.requirement_gaps(durable_database, workspace.id, min_raised=2)

    assert unfiltered.gap_count == 2
    assert recurring.gap_count == 1
    assert recurring.gaps[0].text == GOVERNANCE


def test_importance_filter_narrows_the_raised_by_rows(
    durable_database: Database, tmp_path: Path
) -> None:
    """A caller can ask for only the importances they act on."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="importance", name="Importance"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": GOVERNANCE,
                "unmet": True,
            },
            {
                "requirement_id": "REQ-2",
                "importance": "nice_to_have",
                "text": ON_CALL,
                "unmet": True,
            },
        ],
    )

    critical_only = job_search.requirement_gaps(
        durable_database, workspace.id, importance=["must_have"]
    )

    assert [gap.text for gap in critical_only.gaps] == [GOVERNANCE]


def test_regulatory_boilerplate_is_dropped_from_the_read_model(
    durable_database: Database, tmp_path: Path
) -> None:
    """A Paperwork Reduction Act notice extracted as a requirement is not a gap."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="boilerplate", name="Boilerplate"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[
            {
                "requirement_id": "REQ-1",
                "importance": "must_have",
                "text": (
                    "PUBLIC BURDEN STATEMENT: According to the Paperwork Reduction Act of 1995"
                    " no persons are required to respond to a collection of information."
                ),
                "unmet": True,
            },
            {"requirement_id": "REQ-2", "importance": "must_have", "text": ANSWERED, "unmet": True},
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert [gap.text for gap in page.gaps] == [ANSWERED]


def test_a_workspace_with_no_evaluation_reports_no_gaps(durable_database: Database) -> None:
    """An imported CV nothing has been evaluated against is empty, not unanswered."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="fresh", name="Fresh"
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert page.evaluations_read == 0
    assert page.gap_count == 0
    assert page.gaps == []


def test_a_bounded_list_says_how_many_gaps_it_left_out(
    durable_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the cap the reader is told the count, not left to compare two numbers."""
    monkeypatch.setattr(GAPS_MODULE, "MAX_GAPS", 2)
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="many", name="Many"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Initrode",
        index=1,
        requirements=[
            {
                "requirement_id": f"REQ-{position}",
                "importance": "must_have",
                "text": f"{GOVERNANCE} in region {position}",
                "unmet": True,
            }
            for position in range(5)
        ],
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert page.gap_count == 5
    assert len(page.gaps) == 2
    assert page.omitted_count == 3


def test_gap_grouping_refuses_a_malformed_stored_requirement() -> None:
    evaluation = JobEvaluation(
        evaluated_at=datetime(2026, 8, 24, tzinfo=UTC),
        fit_report_payload={
            "requirements": [
                {
                    "requirement_id": "GATE-1",
                    "importance": "must_have",
                    "text": GOVERNANCE,
                    "evidence_keys": [],
                    "unmet": True,
                    "span_status": "needs_review",
                    "is_hard_gate": True,
                },
                {"text": "Unreadable without an importance"},
            ],
        },
    )

    with pytest.raises(ValidationError, match="invalid shape"):
        GAPS_MODULE._group_gaps([("job-1", "Platform Lead", "Globex", evaluation)], None, {})


def test_one_canonical_gate_requirement_contributes_one_gap() -> None:
    """A canonical gate is represented by exactly one requirement row."""
    stored = {
        "requirement_id": "GATE-1",
        "requirement_key": "5d1d6d0a-0f0d-4a6f-9f0e-1f6b2a4c8d31",
        "importance": "must_have",
        "text": GOVERNANCE,
        "evidence_keys": [],
        "unmet": True,
        "span_status": "needs_review",
    }
    evaluation = JobEvaluation(
        evaluated_at=datetime(2026, 8, 24, tzinfo=UTC),
        fit_report_payload={
            "requirements": [
                {
                    **stored,
                    "is_hard_gate": True,
                    "gate_kind": "minimum_years",
                    "gate_blocking": True,
                }
            ]
        },
    )

    grouped = GAPS_MODULE._group_gaps([("job-1", "Platform Lead", "Globex", evaluation)], None, {})

    assert [(text, len(sources)) for text, sources in grouped.values()] == [(GOVERNANCE, 1)]


def test_a_rerun_that_answers_a_requirement_drops_it_from_that_posting(
    durable_database: Database, tmp_path: Path
) -> None:
    """Only each posting's current evaluation counts, so a rerun can close a gap."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="rerun", name="Rerun"
    )
    artifacts = artifact_service(durable_database, tmp_path / "artifacts")
    governance: dict[str, object] = {
        "requirement_id": "REQ-7",
        "importance": "must_have",
        "text": GOVERNANCE,
        "unmet": True,
    }
    first = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[governance],
    )
    beta = _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Beta",
        index=2,
        requirements=[governance],
    )
    _commit(
        durable_database,
        artifacts,
        workspace.id,
        employer="Globex",
        index=1,
        requirements=[{**governance, "unmet": False, "evidence_keys": ["candidate-platform"]}],
        supersedes=first.evaluation_id,
    )

    page = job_search.requirement_gaps(durable_database, workspace.id)

    assert page.evaluations_read == 2
    assert [gap.text for gap in page.gaps] == [GOVERNANCE]
    assert [source.job_posting_id for source in page.gaps[0].raised_by] == [beta.job_posting_id]
