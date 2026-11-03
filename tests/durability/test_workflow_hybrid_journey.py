"""One hybrid-career shortlist walked from evaluation through review to export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
from sqlalchemy import select

from clearscout.application.documents.approvals import ApprovalService
from clearscout.application.master_cv import parse_canonical_source
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.workflows import PipelineWorkflowRunner, WorkflowService
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.domain import TailoringOutcome
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.infrastructure.persistence.models import Artifact, JobEvaluation
from tests.support.artifacts import runtime_storage


class _HybridCv(NamedTuple):
    """The hybrid-career fixture CV: its path, its bytes, and its parsed inventory."""

    path: Path
    content: bytes
    inventory: CvInventory


@pytest.fixture(scope="module")
def hybrid_cv() -> _HybridCv:
    """Read and parse the hybrid CV once; bytes and a parsed inventory are read-only."""
    path = Path(__file__).parents[2] / "evals/cvs/hybrid_career.md"
    content = path.read_bytes()
    inventory = parse_canonical_source(path, content)
    assert inventory is not None and len(inventory.roles) == 5
    return _HybridCv(path, content, inventory)


@pytest.mark.parametrize(
    ("family", "fragments"),
    [
        (
            "Transit Reliability Lead",
            ("Built the telemetry replay", "Authored the only railway", "Supervised two workshop"),
        ),
        (
            "Instrumentation Program Lead",
            ("Designed a calibration protocol", "Coordinated replacement", "Mentored engineers"),
        ),
        (
            "Public Infrastructure Delivery Lead",
            (
                "Wrote the acceptance plan",
                "Negotiated a phased supplier",
                "Authored the only railway",
            ),
        ),
    ],
)
@pytest.mark.parametrize("readiness", ["factual_review", "missing_coverage", "critic_review"])
def test_hybrid_shortlist_retains_review_findings_and_export_lineage(
    durable_database, tmp_path, monkeypatch, hybrid_cv, family, fragments, readiness
):
    from clearscout.application import job_search
    from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
    from clearscout.application.job_search.models import JobListQuery
    from clearscout.application.master_cv import MasterCvService
    from clearscout.application.master_cv.cv import EntryKind
    from clearscout.application.tailoring.workflows.evaluation_commit import (
        EvaluationCommitInputs,
        build_evaluation_commit,
    )
    from tests.support.doubles import ScriptedProvider
    from tests.support.payloads import script
    from tests.support.suite import SUITE_KEY_VARIABLE, settings_for

    fixture = hybrid_cv.path
    master_content = hybrid_cv.content
    inventory = hybrid_cv.inventory
    selected = [
        next(entry for entry in inventory.entries if entry.text.startswith(part))
        for part in fragments
    ]
    summary = inventory.of_kind(EntryKind.SUMMARY)[0]
    answers = script({})
    answers["JobSpecExtraction"] = lambda _: {
        "title": family,
        "company": "Example Civic Systems",
        "location": "Canada remote",
        "requirements": [{"text": entry.text, "importance": "must_have"} for entry in selected],
        "keywords": [],
        "hard_gates": [],
    }
    answers["EvidenceMap"] = lambda _: {
        "matches": [
            {
                "requirement_id": f"r{index:02}",
                "entry_ids": [entry.id],
                "classification": "TRANSFERABLE" if readiness == "critic_review" else "DIRECT",
                "strength": "strong",
            }
            for index, entry in enumerate(selected, 1)
        ]
    }
    drafted_entries = selected[:-1] if readiness == "missing_coverage" else selected
    answers["ResumeDraft"] = lambda _: {
        "summary": {"text": summary.text, "source_ids": [summary.id]},
        "roles": [
            {
                "role_key": role.key,
                "bullets": [
                    {"text": entry.text, "source_ids": [entry.id]}
                    for entry in drafted_entries
                    if entry.role_key == role.key
                ],
            }
            for role in inventory.roles
            if any(entry.role_key == role.key for entry in drafted_entries)
        ],
    }
    answers["CoverLetter"] = lambda _: {
        "paragraphs": [
            {"text": " ".join(entry.text.split()[:6]), "source_ids": [entry.id]}
            for entry in selected[:2]
        ]
    }
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda *args, **kwargs: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "offline-test-key")
    root = tmp_path / "workspace"
    root.mkdir()
    storage = runtime_storage(durable_database, tmp_path / "artifacts")
    workspace_id = str(
        WorkspaceService(durable_database, storage)
        .create(slug="hybrid", name="Hybrid", root_path=str(root))
        .id
    )
    parameters = SearchParametersService(durable_database).save(workspace_id, JobSearchParameters())
    MasterCvService(durable_database, storage).import_source(
        workspace_id=workspace_id, filename="master.md", content=master_content
    )
    source = tmp_path / "posting.txt"
    source.write_text(
        f"{family} at Example Civic Systems. Canada remote. Full-time Canadian payroll. "
        + " ".join(entry.text for entry in selected)
    )
    settings = settings_for(fixture, tmp_path).model_copy(
        update={
            "rules": Guardrails(),
            "full_bullet_years": 5,
            "cache": False,
            "max_draft_attempts": 2,
        }
    )
    runner = PipelineWorkflowRunner()
    screened = runner.evaluate(source, settings, master_cv=fixture)
    now = datetime.now(UTC)
    publisher = "https://jobs.example.test/civic/hybrid-42"
    proposal = build_evaluation_commit(
        EvaluationCommitInputs(
            workspace_id=workspace_id,
            supersedes_evaluation_id=None,
            employer="Example Civic Systems",
            title=family,
            location="Canada remote",
            requisition_id="HYBRID-42",
            canonical_url=publisher,
            discovery_url=publisher,
            first_seen_at=now,
            observed_at=now,
            posting_status="active",
            residence_rule="Canada",
            employment_type="full-time",
            payroll_arrangement="Canadian payroll",
            source_timestamp=None,
            extraction_method="direct_text",
            fetched_at=now,
            http_status=200,
            robots_basis=None,
            master_content=master_content,
            master_source_uri=fixture.as_uri(),
            master_effective_at=now,
            search_parameter_set_id=str(parameters.id),
        ),
        screened,
    )
    committed = job_search.commit_evaluation(
        durable_database, EvidenceArtifactService(storage), proposal
    )
    rows, total = job_search.list_jobs(
        durable_database,
        JobListQuery(
            workspace_id=workspace_id,
            search=family,
        ),
    )
    assert total == 1
    assert rows[0].job_posting_id == committed.job_posting_id
    with durable_database.session() as session:
        evaluation = session.get(JobEvaluation, committed.evaluation_id)
        assert evaluation is not None
        assert len(evaluation.fit_report_payload["requirements"]) == 3
        assert all(item["evidence_keys"] for item in evaluation.fit_report_payload["requirements"])
        assert evaluation.fit_report_payload["search_parameter_set_id"] == str(parameters.id)
        assert evaluation.search_parameter_set_id == str(parameters.id)
    service = WorkflowService(durable_database, storage, runner)
    inputs = service.inputs(workspace_id, committed.job_posting_id)
    assert inputs.master_content == master_content
    assert inputs.posting_content == source.read_bytes()
    output = runner.tailor(
        source,
        settings,
        master_cv=fixture,
        cover_letter=True,
        force=True,
        approval=None,
        publisher_url=publisher,
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": committed.job_posting_id},
        idempotency_key="offline-hybrid-package",
    )
    persisted = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=committed.job_posting_id,
        master_version_id=inputs.master_version.id,
        task_id=str(task.id),
        output=output,
        started_at=now,
    )
    review = ApprovalService(durable_database).review(persisted["tailoring_run_id"], workspace_id)
    checks = {item.check: item for item in review.checks}
    assert checks["factual review"].outcome == "warn"
    assert "human" in checks["factual review"].detail.lower()
    with durable_database.session() as session:
        artifacts = session.scalars(
            select(Artifact).where(Artifact.id.in_(persisted["artifact_ids"]))
        ).all()
    exported = {
        item.logical_type: service.documents.read(item.id, workspace_id).content
        for item in artifacts
    }
    resume = exported["resume.markdown"].decode()
    assert all(entry.text in resume for entry in drafted_entries)
    report = exported["tailoring.report.markdown"].decode()
    assert publisher in report
    assert "factual review" in report
    manifest = json.loads(exported["tailoring.manifest.json"])
    assert manifest["posting"]["url"] == publisher
    assert manifest["inputs"]["code_revision"]
    assert len(review.proposals) > 0
    assert all(not proposal.decisions for proposal in review.proposals)
    if readiness == "missing_coverage":
        assert checks["must-have coverage"].outcome == "fail"
        assert selected[-1].id in checks["must-have coverage"].detail
        assert output.outcome is TailoringOutcome.CHECKS_FAILED
        assert manifest["run"]["outcome"] != "passed"
        assert sum("ResumeDraft" in name for name, _ in provider.calls) == 2
    else:
        assert "cover_letter.markdown" in exported
        if readiness == "critic_review":
            assert any("CriticVerdict" in name for name, _ in provider.calls)
