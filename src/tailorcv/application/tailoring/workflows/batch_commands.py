"""Persist one batch comparison and the membership that pins its inputs."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from tailorcv.application.failures import ValidationError
from tailorcv.application.tailoring.workflows._support import _prompt_lineage
from tailorcv.application.tailoring.workflows.models import BatchOutput as WorkflowBatchOutput
from tailorcv.application.workspaces.spend import guarantee_billed
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.models import BatchMember, BatchRun

if TYPE_CHECKING:
    from tailorcv.application.tailoring.workflows.service import WorkflowService


def _persist_batch(
    service: WorkflowService,
    *,
    workspace_id: str,
    job_ids: tuple[str, ...],
    task_id: str,
    output: WorkflowBatchOutput,
    priorities: tuple[int, ...],
    notes: tuple[str, ...],
    prompt_version_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Persist per-job reports and their immutable comparison membership."""
    with ExitStack() as stack:
        for screened in output.screenings:
            stack.enter_context(
                guarantee_billed(
                    service.database,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    ledger=screened.ledger,
                    provider="configured",
                    origin="workflow",
                )
            )
        if len(output.screenings) != len(job_ids):
            raise ValidationError("Batch output did not preserve one screening per input job.")
        session = stack.enter_context(service.database.session())
        for position, (job_id, screened) in enumerate(
            zip(job_ids, output.screenings, strict=True), 1
        ):
            service.documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type="screening.report.markdown",
                mime_type="text/markdown",
                content=screened.report,
                author_type="model",
                provenance={"task_id": task_id, "batch_position": position},
            )
        # Filed against every job in the comparison, so each has a route to it.
        provenance = {"task_id": task_id, "job_posting_ids": list(job_ids)}
        comparisons = [
            service.documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=job_id,
                tailoring_run_id=None,
                logical_type="batch.report.markdown",
                mime_type="text/markdown",
                content=output.report,
                author_type="model",
                provenance=provenance,
            )
            for job_id in job_ids
        ]
        comparison = comparisons[0]
        service.documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_ids[0],
            tailoring_run_id=None,
            logical_type="batch.record.json",
            mime_type="application/json",
            content=output.record,
            author_type="system",
            provenance=provenance,
        )
        batch_row = BatchRun(
            workspace_id=workspace_id,
            task_id=task_id,
            result_artifact_id=comparison.id,
            result_sha256=hashlib.sha256(output.report).hexdigest(),
            completed_at=utc_now(),
            origin="workflow.batch",
        )
        session.add(batch_row)
        session.flush()
        _prompt_lineage(
            session,
            prompt_version_ids,
            batch_run_id=batch_row.id,
        )
        for position, (job_id, priority, note) in enumerate(
            zip(job_ids, priorities, notes, strict=True), 1
        ):
            session.add(
                BatchMember(
                    batch_run_id=batch_row.id,
                    job_posting_id=job_id,
                    position=position,
                    priority=priority,
                    note=note,
                    origin="workflow.batch",
                )
            )
        return {
            "batch_run_id": batch_row.id,
            "artifact_id": comparison.id,
            "job_posting_ids": list(job_ids),
        }
