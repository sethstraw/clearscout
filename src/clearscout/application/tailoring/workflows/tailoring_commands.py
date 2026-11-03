"""Persist one tailoring run and everything it produced, in a single transaction."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from clearscout.application.tailoring.workflows._support import _prompt_lineage
from clearscout.application.tailoring.workflows.models import TailorOutput as WorkflowTailorOutput
from clearscout.application.workspaces.spend import record_calls
from clearscout.domain import RESEARCH_ARTIFACT_TYPES, TailoringOutcome, new_id, utc_now
from clearscout.infrastructure.persistence.models import Artifact, ReviewProposal, TailoringRun
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.costs import Ledger

if TYPE_CHECKING:
    from clearscout.application.tailoring.workflows.service import WorkflowService


def _persist_tailoring(
    service: WorkflowService,
    *,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    task_id: str,
    output: WorkflowTailorOutput,
    started_at: datetime,
    prompt_version_ids: tuple[str, ...] = (),
    research: tuple[ResearchSource, ...] = (),
) -> dict[str, Any]:
    """Persist one complete immutable run and append every output as a document version."""
    try:
        inputs = output.artifacts.manifest.get("inputs", {})
        inputs = inputs if isinstance(inputs, dict) else {}
        run_digest = hashlib.sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run_id = new_id()
        return _persist_tailoring_inside_session(
            service,
            workspace_id=workspace_id,
            job_id=job_id,
            master_version_id=master_version_id,
            task_id=task_id,
            output=output,
            started_at=started_at,
            run_id=run_id,
            run_digest=run_digest,
            inputs=inputs,
            prompt_version_ids=prompt_version_ids,
            research=research,
        )
    except BaseException:
        with service.database.session() as fallback:
            record_calls(
                fallback,
                workspace_id=workspace_id,
                task_id=task_id,
                ledger=output.ledger,
                provider="configured",
                origin="workflow",
                tailoring_run_id=None,
            )
        raise


def _persist_tailoring_inside_session(
    service: WorkflowService,
    *,
    workspace_id: str,
    job_id: str,
    master_version_id: str,
    task_id: str,
    output: WorkflowTailorOutput,
    started_at: datetime,
    run_id: str,
    run_digest: str,
    inputs: dict[str, Any],
    prompt_version_ids: tuple[str, ...],
    research: tuple[ResearchSource, ...],
) -> dict[str, Any]:
    with service.database.session() as session:
        run = TailoringRun(
            id=run_id,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            master_cv_version_id=master_version_id,
            task_id=task_id,
            outcome=output.outcome,
            started_at=started_at,
            completed_at=utc_now(),
            manifest=output.artifacts.manifest,
            origin="workflow.tailor",
        )
        session.add(run)
        session.flush()
        _prompt_lineage(session, prompt_version_ids, tailoring_run_id=run.id)
        proposal_by_text = _proposals(session, run.id, output.artifacts.review)
        total_cost = output.ledger.total_dollars
        served_models = output.ledger.served_models()
        artifacts: list[Artifact] = []
        for source in research:
            artifacts.append(
                service.documents.store_generated(
                    session,
                    workspace_id=workspace_id,
                    job_posting_id=job_id,
                    tailoring_run_id=run.id,
                    logical_type=RESEARCH_ARTIFACT_TYPES[source.kind],
                    mime_type="text/plain",
                    content=source.text.encode("utf-8"),
                    author_type="system",
                    provenance={
                        "task_id": task_id,
                        "source_id": source.id,
                        "source_url": source.url,
                        "method": source.method,
                    },
                )
            )
        for logical_type, mime_type, content in output.artifacts.artifact_items():
            provenance: dict[str, Any] = {
                "task_id": task_id,
                "prompt_suite": inputs.get("prompt_suite", ""),
                "master_cv_hash": inputs.get("cv_hash", ""),
                "settings_hash": inputs.get("settings_hash", ""),
                "served_models": served_models,
                "cost_usd": total_cost,
                "run_identity_sha256": run_digest,
            }
            if logical_type in {"resume.markdown", "cover_letter.markdown"}:
                provenance["line_citations"] = _line_citations(content, proposal_by_text)
            artifacts.append(
                service.documents.store_generated(
                    session,
                    workspace_id=workspace_id,
                    job_posting_id=job_id,
                    tailoring_run_id=run.id,
                    logical_type=logical_type,
                    mime_type=mime_type,
                    content=content,
                    author_type="model",
                    provenance=provenance,
                )
            )
        _ledger(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            ledger=output.ledger,
            tailoring_run_id=run.id,
        )
        persisted: dict[str, Any] = {
            "tailoring_run_id": run.id,
            "outcome": run.outcome,
            "artifact_ids": [item.id for item in artifacts],
            "cost_usd": total_cost,
            "served_models": served_models,
        }
        if output.outcome == TailoringOutcome.PASSED:
            persisted["message"] = _passed_message(output.artifacts.manifest)
        return persisted


def _passed_message(manifest: dict[str, Any]) -> str:
    """Say that the checks passed, and whether the critic read the resume they passed."""
    run = manifest.get("run")
    draws = run.get("critic_draws") if isinstance(run, dict) else None
    # Every critique records at least one draw, so an empty list means none was asked for.
    if isinstance(draws, dict) and draws.get("resume"):
        return "Checks passed and the critic read the resume."
    return "Checks passed; the critic did not read the resume."


def _proposals(
    session: Session, tailoring_run_id: str, review: dict[str, Any]
) -> dict[str, list[ReviewProposal]]:
    raw = review.get("proposals", [])
    if not isinstance(raw, list):
        return {}
    by_text: dict[str, list[ReviewProposal]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        key = str(item.get("id", ""))
        digest = str(item.get("digest", ""))
        if not text or not key or not digest:
            continue
        sources = item.get("evidence_ids", [])
        proposal = ReviewProposal(
            tailoring_run_id=tailoring_run_id,
            proposal_key=key,
            slot=str(item.get("slot", "")),
            text=text,
            source_ids=[str(value) for value in sources] if isinstance(sources, list) else [],
            digest=digest,
            payload=item,
            origin="workflow.tailor",
        )
        session.add(proposal)
        session.flush()
        by_text.setdefault(text, []).append(proposal)
    return by_text


def _ledger(
    session: Session,
    *,
    workspace_id: str,
    task_id: str,
    ledger: Ledger,
    tailoring_run_id: str | None = None,
) -> None:
    # The ledger records the model, not the vendor, so a run can only say the
    # configured provider served the call.
    record_calls(
        session,
        workspace_id=workspace_id,
        task_id=task_id,
        ledger=ledger,
        provider="configured",
        origin="workflow",
        tailoring_run_id=tailoring_run_id,
    )


def _line_citations(
    content: bytes, proposals: dict[str, list[ReviewProposal]]
) -> list[dict[str, Any]]:
    text = content.decode("utf-8", errors="replace")
    available = {key: list(value) for key, value in proposals.items()}
    citations: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        candidates = available.get(line.strip()) or available.get(line.lstrip("- ").strip())
        if not candidates:
            continue
        proposal = candidates.pop(0)
        if not proposal.source_ids:
            continue
        citations.append(
            {
                "line_number": number,
                "text": line,
                "source_ids": list(proposal.source_ids),
                "proposal_id": proposal.id,
                "digest": proposal.digest,
            }
        )
    return citations
