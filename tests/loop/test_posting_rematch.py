from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, select

from tailorcv.application import job_search
from tailorcv.application.contracts import ReportRequirementInput
from tailorcv.application.discovery.posting_rematch import (
    PostingRefreshWithRematchHandler,
    PostingRematchService,
)
from tailorcv.application.job_search.postings import RequirementRulingService
from tailorcv.application.master_cv.commands import register_master_cv
from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.application.tailoring import fit
from tailorcv.application.tailoring.stages.context import citable_entries
from tailorcv.application.tailoring.stages.jobspec import (
    Dimension,
    Importance,
    JobSpec,
    Requirement,
)
from tailorcv.application.tailoring.stages.match import EvidenceMap
from tailorcv.application.tailoring.workflows.evaluation_report import project_fit_report
from tailorcv.application.tailoring.workflows.models import EvaluationOutput
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import (
    LearningLoopPolicyService,
    WorkspaceService,
    WorkspaceSettingsService,
)
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.bootstrap.worker import Worker
from tailorcv.domain.policy import LearningLoopPolicy
from tailorcv.domain.rulings import Ruling
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    CostRecord,
    ModelCall,
    PostingRequirement,
    RequirementRuling,
    Workspace,
)
from tailorcv.infrastructure.providers.cache import ResponseCache
from tailorcv.infrastructure.providers.client import Client
from tailorcv.infrastructure.web_fetch.ingest import JDText
from tailorcv.shared.costs import Ledger
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.doubles import ScriptedProvider
from tests.support.scenarios import evaluation_commit
from tests.support.schema import literals


def _seed_fit_dispute(
    database: Database,
    artifact_root: Path,
    *,
    enable_rematch: bool,
    behavioral: bool = False,
) -> tuple[str, str, str, str]:
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug=f"rematch-{'on' if enable_rematch else 'off'}",
        name="Rematch",
    )
    LearningLoopPolicyService(database).save(
        workspace.id,
        LearningLoopPolicy(enable_dispute_rematch=enable_rematch),
        expected_version=0,
    )
    cv_path = Path(__file__).resolve().parents[1] / "fixtures" / "master_cv.md"
    cv_text = cv_path.read_text(encoding="utf-8")
    inventory = parse_cv(cv_path)
    entries = tuple(entry for entry in citable_entries(inventory) if entry.text in cv_text)
    previous_entry, new_entry = entries[:2]
    artifacts = artifact_service(database, artifact_root)
    with database.session() as session:
        workspace_row = session.get(Workspace, workspace.id)
        assert workspace_row is not None
        register_master_cv(
            session,
            artifacts,
            workspace=workspace_row,
            content=cv_text.encode("utf-8"),
            inventory=inventory,
            original_source_uri=cv_path.as_uri(),
            actor="test",
            origin="test",
            correlation_id=None,
        )
    proposal = evaluation_commit(workspace.id)
    prior_evidence = proposal.score_evidence[0].model_copy(
        update={
            "text": previous_entry.text,
            "char_start": cv_text.index(previous_entry.text),
            "char_end": cv_text.index(previous_entry.text) + len(previous_entry.text),
            "master_cv_entry_id": previous_entry.id,
        }
    )
    proposal = proposal.model_copy(
        update={
            "candidate_record": proposal.candidate_record.model_copy(
                update={"content": cv_text, "source_uri": cv_path.as_uri()}
            ),
            "score_evidence": [prior_evidence, *proposal.score_evidence[1:]],
        }
    )
    if behavioral:
        proposal = proposal.model_copy(
            update={
                "fit_report": proposal.fit_report.model_copy(
                    update={"requirements": _behavioral_requirements(inventory)}
                )
            }
        )
    committed = job_search.commit_evaluation(
        database,
        artifacts,
        proposal,
    )
    with database.session() as session:
        requirement = session.scalar(
            select(PostingRequirement).where(
                PostingRequirement.job_evaluation_id == committed.evaluation_id,
                PostingRequirement.requirement_id == "REQ-1",
            )
        )
        assert requirement is not None
        requirement_key = requirement.requirement_key
    RequirementRulingService(database).set(
        workspace_id=workspace.id,
        requirement_key=requirement_key,
        ruling=Ruling.DISPUTED_FIT,
        note="The current CV contains more evidence.",
        audit=audit_columns("local_user", "test", None),
    )
    return workspace.id, committed.job_posting_id, previous_entry.id, new_entry.id


def _provider(entry_id: str) -> ScriptedProvider:
    def answer(schema: type[BaseModel]) -> dict[str, object]:
        requirement_id = literals(schema, "requirement_id")[0]
        return {
            "matches": [
                {
                    "requirement_id": requirement_id,
                    "entry_ids": [entry_id],
                    "classification": "DIRECT",
                    "strength": "strong",
                    "note": "Direct evidence.",
                }
            ]
        }

    return ScriptedProvider({"EvidenceMap": answer})


def _behavioral_requirements(inventory: CvInventory) -> list[ReportRequirementInput]:
    """Project the seeded requirements with REQ-1 behavioral, the way evaluation stores them."""
    posting = "Lead a data platform. Canada remote role."
    output = EvaluationOutput(
        spec=JobSpec(
            title="Product Manager, Data Platform",
            company="Acme Health",
            seniority=None,
            location="Canada",
            requirements=(
                Requirement(
                    id="REQ-1",
                    text="Lead a data platform",
                    importance=Importance.MUST_HAVE,
                    dimension=Dimension.BEHAVIORAL,
                ),
                Requirement(id="REQ-2", text="Canada remote", importance=Importance.MUST_HAVE),
            ),
            keywords=(),
            hard_gates=(),
            origin="posting.txt",
        ),
        evidence=EvidenceMap(),
        inventory=inventory,
        ledger=Ledger(),
        jd=JDText(text=posting, origin="posting.txt", method="direct_text"),
        reading=fit.read(posting, inventory, today=date(2026, 8, 11)),
        outcome="pursue",
        reasons=("The posting was evaluated.",),
        prompt_fingerprint="test-suite-v1",
    )
    return project_fit_report(
        output,
        candidate_text=posting,
        requirement_keys={"REQ-1": ["candidate-platform"], "REQ-2": ["posting-location"]},
        hard_gate_evidence_keys=(),
        reading_keys={name: [] for name, _ in output.reading.named()},
        disagreements=[],
        fallback_evidence_key="posting-location",
    ).requirements


def _run_service(
    database: Database,
    artifact_root: Path,
    workspace_id: str,
    job_id: str,
    provider: ScriptedProvider,
    tmp_path: Path,
) -> dict[str, object]:
    settings = WorkspaceSettingsService(database).current(workspace_id)
    with ResponseCache(enabled=False, directory=tmp_path / "cache") as cache:

        def build_client() -> Client:
            return Client(
                provider=provider,
                settings=settings,
                cache=cache,
                ledger=Ledger(),
            )

        return PostingRematchService(
            database, storage=runtime_storage(database, artifact_root)
        ).rematch(workspace_id, job_id, build_client)


def test_rematch_reverses_only_for_a_new_durable_evidence_key(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id, _, new_entry_id = _seed_fit_dispute(
        durable_database, root, enable_rematch=True
    )

    result = _run_service(
        durable_database,
        root,
        workspace_id,
        job_id,
        _provider(new_entry_id),
        tmp_path,
    )

    with durable_database.session() as session:
        ruling = session.scalar(select(RequirementRuling))
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "ruling.reversed_by_rematch")
        )
    assert result["model_calls_made"] == 1
    assert ruling is not None and ruling.ruling == Ruling.REVERSED.value
    assert event is not None
    assert event.details["previous_evidence_keys"] != event.details["new_evidence_keys"]
    assert event.details["master_cv_version_id"] == result["master_cv_version_id"]


def test_rematch_preserves_a_behavioral_requirement_dimension(
    durable_database: Database, tmp_path: Path
) -> None:
    """The rematch stage receives the dimension the evaluation stored for the requirement."""
    root = tmp_path / "artifacts"
    workspace_id, job_id, _, new_entry_id = _seed_fit_dispute(
        durable_database, root, enable_rematch=True, behavioral=True
    )
    provider = _provider(new_entry_id)

    result = _run_service(
        durable_database,
        root,
        workspace_id,
        job_id,
        provider,
        tmp_path,
    )

    assert result["model_calls_made"] == 1
    reversed_ids = result["reversed_ruling_ids"]
    assert isinstance(reversed_ids, list)
    assert len(reversed_ids) == 1
    assert len(provider.calls) == 1
    assert "(must_have, behavioral)" in provider.calls[0][1].task.text


def test_rematch_keeps_a_fit_dispute_when_the_match_repeats_prior_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id, previous_entry_id, _ = _seed_fit_dispute(
        durable_database, root, enable_rematch=True
    )

    _run_service(
        durable_database,
        root,
        workspace_id,
        job_id,
        _provider(previous_entry_id),
        tmp_path,
    )

    with durable_database.session() as session:
        ruling = session.scalar(select(RequirementRuling))
        reversal_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "ruling.reversed_by_rematch")
        )
    assert ruling is not None and ruling.ruling == Ruling.DISPUTED_FIT.value
    assert reversal_count == 0


def test_rematch_worker_short_circuits_before_credentials_when_flag_is_off(
    durable_database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifacts"
    workspace_id, job_id, _, new_entry_id = _seed_fit_dispute(
        durable_database, root, enable_rematch=False
    )
    provider = _provider(new_entry_id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh_with_rematch",
        payload={"job_posting_id": job_id},
        idempotency_key="rematch-off",
    )
    worker = Worker(
        durable_database,
        worker_id="rematch-worker",
        handlers={
            "posting.refresh_with_rematch": PostingRefreshWithRematchHandler(
                durable_database,
                storage=runtime_storage(durable_database, root),
                provider=provider,
            )
        },
    )

    assert worker.run_once()
    assert queue.get(task.id).status.value == "completed"
    with durable_database.session() as session:
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert session.scalar(select(func.count()).select_from(CostRecord)) == 0
    assert provider.calls == []
