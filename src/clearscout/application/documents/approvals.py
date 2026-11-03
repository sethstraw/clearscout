"""Human review decisions and citation overrides for document versions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.documents.models import (
    ReviewDecisionResponse,
    TailoringReviewResponse,
)
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tailoring import changes
from clearscout.application.workspaces.records.access import add_audit_event
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    ApprovalDecision,
    Artifact,
    ReviewProposal,
    TailoringRun,
)


class ApprovalService:
    """Own human review decisions and citation override records."""

    def __init__(self, database: Database) -> None:
        """Configure approval persistence."""
        self.database = database

    def review(self, tailoring_run_id: str, workspace_id: str) -> TailoringReviewResponse:
        """Return digest-bound proposals and their latest human decisions."""
        with self.database.session() as session:
            run = session.get(TailoringRun, tailoring_run_id)
            if run is None or run.workspace_id != workspace_id:
                raise NotFoundError("Tailoring review does not exist in this workspace.")
            proposals = session.scalars(
                select(ReviewProposal)
                .where(ReviewProposal.tailoring_run_id == run.id)
                .order_by(ReviewProposal.created_at, ReviewProposal.proposal_key)
            ).all()
            rows: list[dict[str, Any]] = []
            for proposal in proposals:
                decisions = session.scalars(
                    select(ApprovalDecision)
                    .where(ApprovalDecision.review_proposal_id == proposal.id)
                    .order_by(ApprovalDecision.decided_at)
                ).all()
                rows.append(
                    {
                        "id": proposal.id,
                        "proposal_key": proposal.proposal_key,
                        "slot": proposal.slot,
                        "text": proposal.text,
                        "source_ids": list(proposal.source_ids),
                        "digest": proposal.digest,
                        "payload": dict(proposal.payload or {}),
                        "decisions": [_decision_record(item) for item in decisions],
                    }
                )
            return TailoringReviewResponse.model_validate(
                {
                    "tailoring_run_id": run.id,
                    "proposals": rows,
                    "checks": _recorded_findings(run.manifest),
                }
            )

    def decide(
        self,
        proposal_id: str,
        workspace_id: str,
        *,
        proposal_digest: str,
        decision: str,
        replacement_text: str | None,
        replacement_source_ids: list[str],
        correlation_id: str | None = None,
    ) -> ReviewDecisionResponse:
        """Append a human decision only when it names the exact reviewed digest."""
        if decision not in {"accept", "reject", "replace"}:
            raise ValidationError("Approval decision must be accept, reject, or replace.")
        if decision == "replace" and not (replacement_text or "").strip():
            raise ValidationError("A replacement decision requires replacement_text.")
        if decision != "replace" and replacement_text is not None:
            raise ValidationError("Only a replacement decision may carry replacement_text.")
        with self.database.session() as session:
            proposal = session.get(ReviewProposal, proposal_id)
            run = (
                session.get(TailoringRun, proposal.tailoring_run_id)
                if proposal is not None
                else None
            )
            if proposal is None or run is None or run.workspace_id != workspace_id:
                raise NotFoundError("Review proposal does not exist in this workspace.")
            if proposal.digest != proposal_digest:
                raise ConflictError(
                    "The proposal changed after this review was opened. Reload it before deciding."
                )
            payload = {
                "proposal_digest": proposal.digest,
                "decision": decision,
                "replacement_text": replacement_text,
                "replacement_source_ids": replacement_source_ids,
            }
            manifest_sha = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            existing = session.scalar(
                select(ApprovalDecision).where(
                    ApprovalDecision.review_proposal_id == proposal.id,
                    ApprovalDecision.manifest_sha256 == manifest_sha,
                )
            )
            if existing is not None:
                return _decision_record(existing)
            record = ApprovalDecision(
                review_proposal_id=proposal.id,
                decision=decision,
                replacement_text=replacement_text,
                replacement_source_ids=replacement_source_ids,
                manifest_sha256=manifest_sha,
                decided_at=utc_now(),
                origin="human_review",
                correlation_id=correlation_id,
            )
            session.add(record)
            session.flush()
            add_audit_event(
                session,
                workspace_id=workspace_id,
                action="approval.decided",
                entity_type="review_proposal",
                entity_id=proposal.id,
                details={"decision": decision, "proposal_digest": proposal.digest},
                origin="human_review",
                correlation_id=correlation_id,
            )
            return _decision_record(record)


def _record_overrides(
    session: Session,
    *,
    base: Artifact,
    saved: Artifact,
    broken: list[dict[str, Any]],
    reason: str,
    correlation_id: str | None,
) -> None:
    """Record explicit human reasons for citation-breaking document edits."""
    if base.tailoring_run_id is None:  # Guarded by save_version before this helper.
        raise ConflictError("Citation override has no originating tailoring run.")
    for item in broken:
        proposal = None
        proposal_id = item.get("proposal_id")
        if isinstance(proposal_id, str) and proposal_id:
            proposal = session.get(ReviewProposal, proposal_id)
            if proposal is not None and proposal.tailoring_run_id != base.tailoring_run_id:
                proposal = None
        if proposal is None:
            payload = {
                "kind": "editor_override",
                "base_artifact_id": base.id,
                "base_sha256": base.sha256,
                "line_number": item["line_number"],
                "text": item["text"],
                "source_ids": item["source_ids"],
            }
            proposal = ReviewProposal(
                tailoring_run_id=base.tailoring_run_id,
                proposal_key=f"editor:{base.id}:{item['line_number']}",
                slot=f"document/line[{item['line_number']}]",
                text=str(item["text"]),
                source_ids=list(item["source_ids"]),
                digest=changes.digest(payload),
                payload=payload,
                correlation_id=correlation_id,
            )
            session.add(proposal)
            session.flush()
        decision_payload = {
            "proposal_digest": proposal.digest,
            "base_artifact_id": base.id,
            "saved_artifact_id": saved.id,
            "saved_sha256": saved.sha256,
            "reason": reason,
        }
        manifest_sha = hashlib.sha256(
            json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        session.add(
            ApprovalDecision(
                review_proposal_id=proposal.id,
                decision="replace",
                replacement_text=str(item.get("replacement", "")),
                replacement_source_ids=[],
                manifest_sha256=manifest_sha,
                decided_at=utc_now(),
                correlation_id=correlation_id,
            )
        )


def _recorded_findings(manifest: Any) -> list[dict[str, Any]]:
    """Keep missing historical findings unknown and refuse malformed stored data."""
    run = manifest.get("run") if isinstance(manifest, dict) else None
    recorded = run.get("findings") if isinstance(run, dict) else None
    if recorded is None:
        return []
    if not isinstance(recorded, list) or any(not isinstance(item, dict) for item in recorded):
        raise ValidationError("Tailoring run findings must be a list of check records.")
    return recorded


def _decision_record(decision: ApprovalDecision) -> ReviewDecisionResponse:
    """Return one append-only human approval decision."""
    return ReviewDecisionResponse.model_validate(
        {
            "id": decision.id,
            "review_proposal_id": decision.review_proposal_id,
            "decision": decision.decision,
            "replacement_text": decision.replacement_text,
            "replacement_source_ids": list(decision.replacement_source_ids),
            "manifest_sha256": decision.manifest_sha256,
            "decided_at": decision.decided_at,
        }
    )
