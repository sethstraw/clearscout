"""Explainable computed completeness for durable job evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompletenessInputs:
    """Truth values gathered from durable rows and verified artifacts."""

    posting_body: bool
    score_evidence: bool
    fit_report: bool
    observed_status_eligibility: bool
    normalized_identity: bool


def evaluate_completeness(job_id: str, value: CompletenessInputs) -> dict[str, Any]:
    """Return status, missing codes, and one repair action per criterion."""
    checks = (
        (
            "posting_body",
            "Posting body is stored and verified",
            value.posting_body,
            "posting",
        ),
        (
            "score_evidence",
            "Every score resolves to evidence",
            value.score_evidence,
            "evidence",
        ),
        (
            "fit_report",
            "Fit report is stored and verified",
            value.fit_report,
            "evaluation",
        ),
        (
            "observed_status_eligibility",
            "Posting status and eligibility were observed",
            value.observed_status_eligibility,
            "overview",
        ),
        (
            "normalized_identity",
            "Employer and title are normalized and deduplicated",
            value.normalized_identity,
            "overview",
        ),
    )
    missing = [code for code, _, complete, _ in checks if not complete]
    status = "complete" if not missing else "incomplete"
    return {
        "status": status,
        "missing": missing,
        "checks": [
            {
                "code": code,
                "label": label,
                "complete": complete,
                "action": f"/jobs/{job_id}?tab={action}",
            }
            for code, label, complete, action in checks
        ],
    }
