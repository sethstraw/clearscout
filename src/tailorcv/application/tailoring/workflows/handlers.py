"""Durable task handlers for workflow execution."""

from __future__ import annotations

import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from tailorcv.application import prompts
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.postings.rulings import (
    RequirementRulingService,
    dispute_block,
)
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.master_cv.cv import CvInventory, parse_cv
from tailorcv.application.tailoring import elicit
from tailorcv.application.tailoring.pipeline import StoredMasterCv
from tailorcv.application.tailoring.stages.jobspec import StoredPostingName
from tailorcv.application.tailoring.workflows._support import _posting_name
from tailorcv.application.tasks import TaskContext
from tailorcv.application.workspaces.spend import record_calls
from tailorcv.infrastructure.artifacts.store import file_uri_path
from tailorcv.infrastructure.persistence.models import Artifact, EvaluationFactor, JobEvaluation
from tailorcv.shared.costs import Ledger
from tailorcv.shared.costs.prices import ledger_for

from .context import _WorkflowInputs
from .service import WorkflowService

# Intake retains an uploaded CV as ``<stem>-<first 12 hex of its digest><suffix>``.
RETAINED_DIGEST = re.compile(r"-[0-9a-f]{12}(?=\.[^.]+$)")


class TailoringHandler:
    """Run the full tailoring pipeline as a durable task."""

    def __init__(self, service: WorkflowService) -> None:
        """Bind the handler to reusable workflow persistence."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Run and persist a complete versioned package."""
        started = datetime.now(UTC)
        workspace_id = _task_workspace(context)
        inputs = self.service.inputs(workspace_id, str(payload["job_posting_id"]))
        context.progress("inputs_verified", {"job_posting_id": inputs.job.id})
        context.raise_if_cancelled()
        wants_letter = bool(payload.get("cover_letter", True))
        approval = (
            self.service.approval_manifest(workspace_id, str(payload["approval_tailoring_run_id"]))
            if payload.get("approval_tailoring_run_id")
            else None
        )
        if not bool(payload.get("regenerate", False)):
            preflight = self.service.tailoring_preflight(
                inputs,
                cover_letter=wants_letter,
                approval=approval,
            )
            if preflight["reuse_available"]:
                context.progress("package_reused", preflight)
                return preflight
        research = self.service.company_research(inputs) if wants_letter else ()
        rulings = RequirementRulingService(self.service.database)
        active_disputes = dispute_block(
            [
                *rulings.active_fit_disputes(workspace_id, job_posting_id=inputs.job.id),
                *rulings.active_extraction_disputes(workspace_id, job_posting_id=inputs.job.id),
            ]
        )
        context.raise_if_cancelled()
        ledger = ledger_for(inputs.settings.prices)
        try:
            with tempfile.TemporaryDirectory(prefix="tailorcv-tailor-") as temporary:
                source, master = _materialize(inputs, Path(temporary))
                with prompts.overriding(_prompt_text(inputs)):
                    result = self.service.runner.tailor(
                        source,
                        inputs.settings,
                        master_cv=master,
                        cover_letter=wants_letter,
                        research=research,
                        force=bool(payload.get("regenerate", False)),
                        approval=approval,
                        posting_hint=inputs.job.posting_hint,
                        publisher_url=inputs.posting_url,
                        active_disputes=active_disputes or None,
                        service_tier=inputs.settings.unattended_tier(),
                        ledger=ledger,
                        raise_if_cancelled=context.raise_if_cancelled,
                        stored_name=_stored_name(self.service, inputs),
                        stored_cv=_stored_cv(self.service, inputs),
                    )
        except BaseException:
            with self.service.database.session() as session:
                record_calls(
                    session,
                    workspace_id=workspace_id,
                    task_id=context.task_id,
                    ledger=ledger,
                    provider=inputs.settings.provider,
                    origin="workflow",
                )
            raise
        persisted = self.service.persist_tailoring(
            workspace_id=workspace_id,
            job_id=inputs.job.id,
            master_version_id=inputs.master_version.id,
            task_id=context.task_id,
            output=result,
            started_at=started,
            prompt_version_ids=_prompt_versions(inputs),
            research=research,
        )
        context.progress("package_stored", persisted)
        context.raise_if_cancelled()
        return persisted


class BatchHandler:
    """Run isolated screening and comparison as a durable task."""

    def __init__(self, service: WorkflowService) -> None:
        """Bind the handler to reusable workflow persistence."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Screen every posting separately and persist their comparison."""
        workspace_id = _task_workspace(context)
        job_ids = tuple(str(item) for item in payload["job_posting_ids"])
        all_inputs = tuple(self.service.inputs(workspace_id, job_id) for job_id in job_ids)
        first = all_inputs[0]
        if any(item.master_version.id != first.master_version.id for item in all_inputs):
            raise ValidationError("Batch jobs did not resolve to one master CV version.")
        priorities = tuple(int(item) for item in payload.get("priorities", [0] * len(job_ids)))
        notes = tuple(str(item) for item in payload.get("notes", [""] * len(job_ids)))
        context.progress("inputs_verified", {"job_posting_ids": list(job_ids)})
        context.raise_if_cancelled()
        ledgers: list[Ledger] = []
        try:
            with tempfile.TemporaryDirectory(prefix="tailorcv-batch-") as temporary:
                root = Path(temporary)
                master = root / "master.md"
                master.write_bytes(first.master_content)
                sources: list[Path] = []
                for position, item in enumerate(all_inputs, 1):
                    source = root / f"posting-{position}.txt"
                    source.write_bytes(item.posting_content)
                    sources.append(source)
                with prompts.overriding(_prompt_text(first)):
                    result = self.service.runner.compare(
                        tuple(sources),
                        first.settings,
                        master_cv=master,
                        output_dir=root / "output",
                        priorities=priorities,
                        notes=notes,
                        service_tier=first.settings.unattended_tier(),
                        ledgers=ledgers,
                        raise_if_cancelled=context.raise_if_cancelled,
                        stored_names=tuple(_stored_name(self.service, item) for item in all_inputs),
                    )
        except BaseException:
            with self.service.database.session() as session:
                for ledger in ledgers:
                    record_calls(
                        session,
                        workspace_id=workspace_id,
                        task_id=context.task_id,
                        ledger=ledger,
                        provider=first.settings.provider,
                        origin="workflow",
                    )
            raise
        persisted = self.service.persist_batch(
            workspace_id=workspace_id,
            job_ids=job_ids,
            task_id=context.task_id,
            output=result,
            priorities=priorities,
            notes=notes,
            prompt_version_ids=_prompt_versions(first),
        )
        context.progress("comparison_stored", persisted)
        context.raise_if_cancelled()
        return persisted


class DiscoveryHandler:
    """Advance a user-authored evidence-gap conversation as a durable task."""

    def __init__(self, service: WorkflowService) -> None:
        """Bind safe inputs and private artifact storage."""
        self.service = service

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Advance a transcript and optionally commit a confirmed CV fact."""
        workspace_id = _task_workspace(context)
        inputs = self.service.inputs(workspace_id, str(payload["job_posting_id"]))
        subject, gap_ids = self._subject(inputs.job.id, str(payload.get("subject", "")))
        line = elicit.Line(subject=subject, gap_ids=gap_ids, postings=(inputs.job.id,))
        for answer in payload.get("replies", []):
            question = line.next_question()
            if question is None:
                break
            line = line.with_reply(elicit.Reply(text=str(answer)))
        next_question = line.next_question()
        with tempfile.TemporaryDirectory(prefix="tailorcv-discovery-") as temporary:
            master = Path(temporary) / "master.md"
            master.write_bytes(inputs.master_content)
            inventory = parse_cv(master)
            state = _discovery_state(
                job_id=inputs.job.id,
                subject=subject,
                gap_ids=gap_ids,
                line=line,
                next_question=next_question,
                inventory=inventory,
            )
            proposal = payload.get("proposal")
            if isinstance(proposal, dict):
                described, written = self._proposed(
                    line=line,
                    proposal=proposal,
                    payload=payload,
                    inventory=inventory,
                    master=master,
                    workspace_id=workspace_id,
                    task_id=context.task_id,
                )
                state["proposal"] = described
                if written is not None:
                    state["written"] = written
        content = (json.dumps(state, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        with self.service.database.session() as session:
            artifact = self.service.documents.store_generated(
                session,
                workspace_id=workspace_id,
                job_posting_id=inputs.job.id,
                tailoring_run_id=None,
                logical_type="discovery.session.json",
                mime_type="application/json",
                content=content,
                author_type="human",
                provenance={"task_id": context.task_id, "source": "user_confirmed_discovery"},
            )
            state["artifact_id"] = artifact.id
        context.progress("discovery_state_stored", state)
        return state

    def _proposed(
        self,
        *,
        line: elicit.Line,
        proposal: dict[str, Any],
        payload: dict[str, Any],
        inventory: CvInventory,
        master: Path,
        workspace_id: str,
        task_id: str,
    ) -> tuple[list[str], dict[str, Any] | None]:
        """Describe a proposed fact and return what confirming and writing it produced."""
        pending = elicit.propose(
            line,
            text=str(proposal.get("text", "")),
            scope=str(proposal.get("scope", "")),
            placement=elicit.Placement(
                role_key=str(proposal.get("role_key", "")),
                section=str(proposal.get("section", "")),
            ),
            inventory=inventory,
            ambiguities=tuple(str(item) for item in proposal.get("ambiguities", [])),
        )
        described = pending.describe()
        points = payload.get("confirmed_points", [])
        if not points:
            return described, None
        confirmed = elicit.confirm(
            pending,
            (elicit.Point(str(item)) for item in points),
            at=datetime.now(UTC),
        )
        if not payload.get("write_confirmed"):
            return described, None
        written = elicit.write(
            master,
            (confirmed,),
            inventory=inventory,
            at=datetime.now(UTC),
        )
        version = MasterCvService(self.service.database, self.service.storage).import_source(
            workspace_id=workspace_id,
            filename="CV_Master.md",
            content=master.read_bytes(),
            replace=True,
            actor="local_user",
            origin="workflow.discovery",
            correlation_id=task_id,
        )
        return described, {
            "entry_ids": list(written.entry_ids),
            "journal": written.journal.name,
            "master_cv_version_id": version.master_cv_version_id,
        }

    def _subject(self, job_id: str, supplied: str) -> tuple[str, tuple[str, ...]]:
        with self.service.database.session() as session:
            evaluation = session.scalar(
                select(JobEvaluation)
                .where(JobEvaluation.job_posting_id == job_id)
                .order_by(JobEvaluation.version_number.desc(), JobEvaluation.id.desc())
                .limit(1)
            )
            gaps = (
                session.scalars(
                    select(EvaluationFactor)
                    .where(
                        EvaluationFactor.evaluation_id == evaluation.id,
                        EvaluationFactor.factor_type == "gap",
                    )
                    .order_by(EvaluationFactor.position)
                ).all()
                if evaluation is not None
                else []
            )
        if supplied.strip():
            matching = tuple(item.id for item in gaps if item.text == supplied.strip())
            return supplied.strip(), matching
        if not gaps:
            raise ValidationError("This job has no stored gap to ask about.")
        return gaps[0].text, (gaps[0].id,)


def _discovery_state(
    *,
    job_id: str,
    subject: str,
    gap_ids: tuple[str, ...],
    line: elicit.Line,
    next_question: elicit.Question | None,
    inventory: CvInventory,
) -> dict[str, Any]:
    """Describe the transcript so far and the placements a proposal may name."""
    return {
        "job_posting_id": job_id,
        "subject": subject,
        "gap_ids": list(gap_ids),
        "answered": line.answered,
        "closed": line.closed,
        "replies": [reply.text for reply in line.replies],
        "next_question": (
            {
                "id": next_question.id,
                "angle": next_question.angle.value,
                "text": next_question.text,
            }
            if next_question is not None
            else None
        ),
        "written": None,
        "placements": {
            "roles": [
                {
                    "key": role.key,
                    "label": " - ".join(item for item in (role.organization, role.title) if item),
                }
                for role in inventory.roles
            ],
            "sections": list(inventory.sections),
        },
    }


def _materialize(inputs: _WorkflowInputs, root: Path) -> tuple[Path, Path]:
    source = root / "posting.txt"
    master = root / "master.md"
    source.write_bytes(inputs.posting_content)
    master.write_bytes(inputs.master_content)
    return source, master


def _stored_name(service: WorkflowService, inputs: _WorkflowInputs) -> StoredPostingName:
    with service.database.session() as session:
        return _posting_name(
            session,
            workspace_id=inputs.workspace.id,
            title=inputs.job.title,
            employer=inputs.employer_name,
            body=inputs.posting_content,
            vouched=inputs.identity_vouched,
            posting_urls=inputs.posting_urls,
        )


def _stored_cv(service: WorkflowService, inputs: _WorkflowInputs) -> StoredMasterCv:
    """Name the master CV version by the file the person uploaded, when intake recorded one."""
    with service.database.session() as session:
        provenance = session.scalar(
            select(Artifact.provenance).where(
                Artifact.related_entity_type == "master_cv_version",
                Artifact.related_entity_id == inputs.master_version.id,
            )
        )
    uploaded = (provenance or {}).get("original_source_uri")
    path = file_uri_path(uploaded) if isinstance(uploaded, str) else None
    name = RETAINED_DIGEST.sub("", path.name) if path is not None else ""
    return StoredMasterCv(version_id=inputs.master_version.id, file_name=name)


def _prompt_text(inputs: _WorkflowInputs) -> dict[str, str]:
    return {key: text for key, (_, text) in inputs.prompt_overrides.items()}


def _prompt_versions(inputs: _WorkflowInputs) -> tuple[str, ...]:
    return tuple(version_id for version_id, _ in inputs.prompt_overrides.values())


def _task_workspace(context: TaskContext) -> str:
    task = context.queue.get(context.task_id)
    return str(task.workspace_id)
