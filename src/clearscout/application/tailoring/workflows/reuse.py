"""Decide whether a stored package already answers this exact request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, or_, select

from clearscout import __version__
from clearscout.application import prompts
from clearscout.application.discovery import posting_identity as posting
from clearscout.application.master_cv import cv
from clearscout.application.tailoring import runs
from clearscout.application.tailoring.approval import Manifest as ApprovalManifest
from clearscout.application.tailoring.workflows.context import _WorkflowInputs
from clearscout.application.tailoring.workflows.research import _stored_company_research
from clearscout.infrastructure.persistence.models import Artifact, TailoringRun
from clearscout.infrastructure.web_fetch.company_research import fingerprint

if TYPE_CHECKING:
    from clearscout.application.tailoring.workflows.service import WorkflowService


def _no_reuse() -> dict[str, Any]:
    """Report that nothing stored matches, leaving the cost of the run unknown."""
    return {
        "reuse_available": False,
        "tailoring_run_id": None,
        "artifact_ids": [],
        "cost_usd": None,
    }


def _tailoring_preflight(
    service: WorkflowService,
    inputs: _WorkflowInputs,
    *,
    cover_letter: bool,
    approval: dict[str, Any] | None,
) -> dict[str, Any]:
    """Find an exact reusable package without research or provider calls."""
    research = _stored_company_research(service, inputs) if cover_letter else ()
    try:
        approval_fingerprint = (
            ApprovalManifest.model_validate(approval).fingerprint() if approval is not None else ""
        )
    except PydanticValidationError:
        return _no_reuse()
    with prompts.overriding({key: text for key, (_, text) in inputs.prompt_overrides.items()}):
        expected_inputs = {
            "cv_hash": cv.content_hash(inputs.master_content.decode("utf-8")),
            "prompt_suite": prompts.suite_fingerprint(),
            "settings_hash": runs.settings_hash(inputs.settings),
            "cover_letter": cover_letter,
            "research_hash": fingerprint(research),
            "trimmed": 0,
            "approval": approval_fingerprint,
            "clearscout": __version__,
        }
    expected_jd_hash = posting.content_hash(inputs.posting_content.decode("utf-8"))
    with service.database.session() as session:
        run_outcome = func.json_extract(TailoringRun.manifest, "$.run.outcome")
        required: tuple[str, ...] = ("resume.markdown",)
        if cover_letter:
            required += ("cover_letter.markdown",)
        candidate = session.scalars(
            select(TailoringRun)
            .where(
                TailoringRun.workspace_id == inputs.workspace.id,
                TailoringRun.job_posting_id == inputs.job.id,
                TailoringRun.completed_at.is_not(None),
                func.json_type(TailoringRun.manifest, "$.inputs") == "object",
                func.json_type(TailoringRun.manifest, "$.posting") == "object",
                func.json_type(TailoringRun.manifest, "$.run") == "object",
                func.json_extract(TailoringRun.manifest, "$.posting.jd_hash") == expected_jd_hash,
                *(
                    func.json_extract(TailoringRun.manifest, f"$.inputs.{key}") == value
                    for key, value in expected_inputs.items()
                ),
                or_(run_outcome.is_(None), run_outcome != "incomplete"),
                *(
                    select(Artifact.id)
                    .where(
                        Artifact.workspace_id == inputs.workspace.id,
                        Artifact.tailoring_run_id == TailoringRun.id,
                        Artifact.archived_at.is_(None),
                        Artifact.logical_type == logical_type,
                    )
                    .exists()
                    for logical_type in required
                ),
            )
            .order_by(TailoringRun.completed_at.desc(), TailoringRun.id.desc())
            .limit(1)
        ).first()
        if candidate is None:
            return _no_reuse()
        artifacts = session.scalars(
            select(Artifact)
            .where(
                Artifact.workspace_id == inputs.workspace.id,
                Artifact.tailoring_run_id == candidate.id,
                Artifact.archived_at.is_(None),
            )
            .order_by(Artifact.created_at, Artifact.id)
        ).all()
        return {
            "reuse_available": True,
            "tailoring_run_id": candidate.id,
            "artifact_ids": [artifact.id for artifact in artifacts],
            "cost_usd": 0,
        }
