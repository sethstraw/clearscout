"""Render and store the fit report an evaluation is committed with.

Both the stored payload and the rendered Markdown carry the durable evidence
identifiers rather than the transient keys a proposal used, so every citation in
either one resolves to a row committed by the same transaction.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from tailorcv.application.contracts import EvaluationCommit
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search.evaluation_requirements import (
    merge_hard_gates_into_requirements,
)
from tailorcv.application.job_search.fit_reports import render_fit_report
from tailorcv.infrastructure.persistence.models import Artifact, MasterCvVersion, ScoreWeightSet


def _fit_report_payload(
    proposal: EvaluationCommit,
    evidence_ids: dict[str, str],
    requirement_keys: dict[str, str],
) -> dict[str, Any]:
    """Store structured findings with durable evidence IDs, not transient proposal keys.

    ``requirement_keys`` maps each requirement's positional id to the durable
    identity carried across re-evaluations of this posting. Stamped onto each
    stored requirement so a reader can group without joining the requirement
    table.
    """
    payload = proposal.fit_report.model_dump(mode="json")
    payload["requirements"] = [
        item.model_dump(mode="json")
        for item in merge_hard_gates_into_requirements(
            hard_gates=proposal.fit_report.hard_gates,
            requirements=proposal.fit_report.requirements,
        ).rows
    ]
    payload.pop("hard_gates")
    _replace_evidence_keys(payload, evidence_ids)
    _stamp_requirement_keys(payload, requirement_keys)
    return payload


def _replace_evidence_keys(payload: dict[str, Any], evidence_ids: dict[str, str]) -> None:
    """Replace proposal-local evidence keys throughout one stored report."""
    for section in ("reading", "disagreements", "requirements", "provenance"):
        for item in payload.get(section, []):
            if isinstance(item, dict) and isinstance(item.get("evidence_keys"), list):
                item["evidence_keys"] = [
                    evidence_ids.get(key, key) for key in item["evidence_keys"]
                ]


def _stamp_requirement_keys(payload: dict[str, Any], requirement_keys: dict[str, str]) -> None:
    """Attach durable requirement identities to requirement-bearing sections."""
    for item in payload.get("requirements", []):
        if not isinstance(item, dict) or "requirement_id" not in item:
            continue
        requirement_id = str(item["requirement_id"])
        key = requirement_keys.get(requirement_id)
        if key is None:
            raise ValidationError(
                f"The stored fit report states requirement {requirement_id!r} with no durable "
                "identity, so the evaluation cannot be stored."
            )
        item["requirement_key"] = key


def _fit_report_artifact(
    artifacts: EvidenceArtifactService,
    session: Session,
    proposal: EvaluationCommit,
    *,
    job_id: str,
    evaluation_id: str,
    evidence_ids: dict[str, str],
    weights: dict[str, Decimal],
    weight_set: ScoreWeightSet,
    master_cv_version: MasterCvVersion,
    audit: dict[str, Any],
) -> Artifact:
    """Render the report from the stored findings and store it as an artifact."""
    report_markdown = render_fit_report(
        proposal,
        evidence_ids={
            key: f"[{evidence_id}](/jobs/{job_id}?tab=evidence&evidence={evidence_id})"
            for key, evidence_id in evidence_ids.items()
        },
        weights=weights,
        weight_set_name=weight_set.name,
        weight_set_version=weight_set.version,
    )
    return artifacts.store(
        session,
        workspace_id=proposal.workspace_id,
        logical_type="evaluation.fit_report.markdown",
        content=report_markdown.encode("utf-8"),
        relative_path=(f"{proposal.workspace_id}/evaluations/{evaluation_id}/fit-report.md"),
        mime_type="text/markdown",
        related_entity_type="job_evaluation",
        related_entity_id=evaluation_id,
        provenance={
            "source": "stored_evaluation_findings",
            "prompt_fingerprint": proposal.fit_report.prompt_fingerprint,
            "master_cv_sha256": master_cv_version.content_sha256,
            "score_weight_set_id": weight_set.id,
        },
        audit=audit,
    )
