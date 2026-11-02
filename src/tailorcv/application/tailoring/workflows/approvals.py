"""Rebuild a stored review with the decisions a person recorded against it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.infrastructure.persistence.models import (
    ApprovalDecision,
    Artifact,
    ReviewProposal,
    TailoringRun,
)

if TYPE_CHECKING:
    from tailorcv.application.tailoring.workflows.service import WorkflowService


def _stored_decisions(
    service: WorkflowService, workspace_id: str, tailoring_run_id: str
) -> tuple[str, dict[str, ApprovalDecision]]:
    """Return the review artifact id and the latest decision for every proposal."""
    with service.database.session() as session:
        run = session.get(TailoringRun, tailoring_run_id)
        if run is None or run.workspace_id != workspace_id:
            raise NotFoundError("Approval tailoring run does not exist in this workspace.")
        review_artifact = session.scalar(
            select(Artifact).where(
                Artifact.workspace_id == workspace_id,
                Artifact.tailoring_run_id == run.id,
                Artifact.logical_type == "tailoring.review.json",
            )
        )
        if review_artifact is None:
            raise ValidationError("The tailoring run has no stored review artifact.")
        proposals = session.scalars(
            select(ReviewProposal)
            .where(ReviewProposal.tailoring_run_id == run.id)
            .order_by(ReviewProposal.created_at, ReviewProposal.proposal_key)
        ).all()
        decisions: dict[str, ApprovalDecision] = {}
        for proposal in proposals:
            decision = session.scalar(
                select(ApprovalDecision)
                .where(ApprovalDecision.review_proposal_id == proposal.id)
                .order_by(ApprovalDecision.decided_at.desc())
            )
            if decision is None:
                raise ValidationError(
                    "Every proposal needs an explicit decision before generating "
                    "an approved package."
                )
            decisions[proposal.proposal_key] = decision
        return review_artifact.id, decisions


def _parsed_review_manifest(
    service: WorkflowService, artifact_id: str, workspace_id: str
) -> dict[str, Any]:
    """Load the stored review JSON and refuse a shape the approval path cannot apply."""
    stored = service.documents.read(artifact_id, workspace_id)
    try:
        manifest = json.loads(stored.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("The stored review artifact is not valid review JSON.") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("proposals"), list):
        raise ValidationError("The stored review artifact has an unsupported shape.")
    return manifest


def _apply_decision(item: dict[str, Any], decision: ApprovalDecision) -> None:
    """Write one stored decision onto its review proposal entry."""
    item["decision"] = "reject" if decision.decision == "replace" else decision.decision
    if decision.decision == "replace" and decision.replacement_text is not None:
        item["replacement"] = {
            "text": decision.replacement_text,
            "source_ids": list(decision.replacement_source_ids),
            "requirement_ids": list(item.get("requirement_ids", [])),
        }
    else:
        item.pop("replacement", None)


def _approval_manifest(
    service: WorkflowService, workspace_id: str, tailoring_run_id: str
) -> dict[str, Any]:
    """Rebuild the original review JSON with explicit stored decisions applied."""
    artifact_id, decisions = _stored_decisions(service, workspace_id, tailoring_run_id)
    manifest = _parsed_review_manifest(service, artifact_id, workspace_id)
    for item in manifest["proposals"]:
        if not isinstance(item, dict):
            continue
        decision = decisions.get(str(item.get("id", "")))
        if decision is None:
            raise ValidationError("Stored approval decisions do not cover the review proposal set.")
        _apply_decision(item, decision)
    return manifest
