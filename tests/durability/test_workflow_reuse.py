"""Regression coverage for bounded tailoring reuse reads."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import event

from tailorcv import __version__
from tailorcv.application import prompts
from tailorcv.application.discovery import posting_identity as posting
from tailorcv.application.master_cv import cv
from tailorcv.application.tailoring import runs
from tailorcv.application.tailoring.workflows import TailorOutput, WorkflowService
from tailorcv.application.tasks import TaskQueueService
from tailorcv.domain import TailoringOutcome
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import BackgroundTask, TailoringRun
from tailorcv.infrastructure.web_fetch.company_research import fingerprint
from tailorcv.shared.costs import Ledger
from tests.support.artifacts import runtime_storage
from tests.support.runners import ScriptedPipelineRunner, serialized_artifacts
from tests.support.scenarios import studio_workspace


def test_preflight_bounds_completed_run_loading_without_hiding_an_older_reusable_run(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = WorkflowService(
        durable_database, runtime_storage(durable_database, artifact_root), ScriptedPipelineRunner()
    )
    inputs = service.inputs(workspace_id, job_id)
    with prompts.overriding({key: text for key, (_, text) in inputs.prompt_overrides.items()}):
        prompt_suite = prompts.suite_fingerprint()
    manifest: dict[str, Any] = {
        "posting": {"jd_hash": posting.content_hash(inputs.posting_content.decode("utf-8"))},
        "inputs": {
            "cv_hash": cv.content_hash(inputs.master_content.decode("utf-8")),
            "prompt_suite": prompt_suite,
            "settings_hash": runs.settings_hash(inputs.settings),
            "cover_letter": False,
            "research_hash": fingerprint(()),
            "trimmed": 0,
            "approval": "",
            "tailorcv": __version__,
        },
        "run": {"outcome": "passed"},
    }
    seed_task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="bounded-preflight-seed",
    )
    reusable = service.persist_tailoring(
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=str(seed_task.id),
        output=TailorOutput(
            artifacts=serialized_artifacts(
                {"resume.markdown": ("text/markdown", b"# Candidate\n")},
                manifest=manifest,
                review={},
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=Ledger(),
        ),
        started_at=datetime.now(UTC),
    )

    later = datetime.now(UTC) + timedelta(seconds=1)
    empty_payload_digest = hashlib.sha256(b"{}").hexdigest()
    identity_fields = tuple(manifest["inputs"])
    with durable_database.session() as session:
        for position in range(24):
            task = BackgroundTask(
                workspace_id=workspace_id,
                task_type="workflow.tailor",
                status="completed",
                payload={},
                payload_sha256=empty_payload_digest,
                idempotency_key=f"bounded-preflight-noise-{position}",
                completed_at=later + timedelta(seconds=position),
            )
            session.add(task)
            session.flush()
            candidate_manifest = copy.deepcopy(manifest)
            if position < 23:
                field_position = position % (len(identity_fields) + 1)
                if field_position == len(identity_fields):
                    candidate_manifest["posting"]["jd_hash"] = f"different-{position}"
                else:
                    key = identity_fields[field_position]
                    value = candidate_manifest["inputs"][key]
                    candidate_manifest["inputs"][key] = not value if isinstance(value, bool) else 1
            session.add(
                TailoringRun(
                    workspace_id=workspace_id,
                    job_posting_id=job_id,
                    master_cv_version_id=master_version_id,
                    task_id=task.id,
                    outcome="passed",
                    started_at=later,
                    completed_at=later + timedelta(seconds=position),
                    manifest=candidate_manifest,
                    origin="workflow.tailor",
                )
            )

    loaded_run_ids: list[str] = []

    def record_load(run: TailoringRun, _context: Any) -> None:
        loaded_run_ids.append(run.id)

    event.listen(TailoringRun, "load", record_load)
    try:
        preflight = service.tailoring_preflight(inputs, cover_letter=False, approval=None)
    finally:
        event.remove(TailoringRun, "load", record_load)

    assert preflight == {
        "reuse_available": True,
        "tailoring_run_id": reusable["tailoring_run_id"],
        "artifact_ids": reusable["artifact_ids"],
        "cost_usd": 0,
    }
    assert len(loaded_run_ids) <= 2
