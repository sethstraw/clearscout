"""Compare immutable assessment inputs with the workspace's current inputs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application import prompts
from clearscout.application.contracts import RULE_REFUSAL_PROMPT_FINGERPRINT
from clearscout.application.failures import ValidationError
from clearscout.application.job_search.models import EvaluationInputResponse
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.workspaces.parameters import (
    SearchParametersSnapshot,
    parameter_snapshot_in,
)
from clearscout.application.workspaces.prompt_library import overrides_in
from clearscout.application.workspaces.records.access import audit_columns, require_owned_entity
from clearscout.domain import utc_now
from clearscout.infrastructure.build import current_build
from clearscout.infrastructure.persistence.models import (
    AuditEvent,
    EvaluationCurrentnessReview,
    JobEvaluation,
    JobPosting,
    PostingObservation,
)


def attach_evaluation_inputs(
    session: Session,
    items: list[dict[str, Any]],
    *,
    parameters: Mapping[str, SearchParametersSnapshot] | None = None,
) -> None:
    """Add currentness without changing a score or inventing historical identities.

    `parameters` carries snapshots the caller already read, keyed by workspace.
    """
    if not items:
        return
    evaluations = _evaluations_for_items(session, items)
    latest_observation_ids = _latest_observation_ids(session, items)
    observations = _observations_for_evaluations(session, evaluations, latest_observation_ids)
    reviews_by_evaluation = _reviews_by_evaluation(session, evaluations)
    current_by_workspace = _current_inputs_by_workspace(session, evaluations, parameters or {})
    items_by_evaluation = {item["evaluation_id"]: item for item in items}
    for evaluation, workspace_id in evaluations:
        inputs, review = _evaluation_inputs_and_review(
            evaluation,
            workspace_id=workspace_id,
            current_by_workspace=current_by_workspace,
            latest_observation_ids=latest_observation_ids,
            observations=observations,
            reviews_by_evaluation=reviews_by_evaluation,
        )
        items_by_evaluation[evaluation.id]["evaluation_review"] = review
        items_by_evaluation[evaluation.id]["evaluation_inputs"] = inputs
    for item in items:
        item.setdefault("evaluation_inputs", [])


def _evaluations_for_items(
    session: Session, items: list[dict[str, Any]]
) -> list[tuple[JobEvaluation, str]]:
    return [
        (evaluation, workspace_id)
        for evaluation, workspace_id in session.execute(
            select(JobEvaluation, JobPosting.workspace_id)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(JobEvaluation.id.in_([item["evaluation_id"] for item in items]))
        )
    ]


def _latest_observation_ids(session: Session, items: list[dict[str, Any]]) -> dict[str, str]:
    ranked = (
        select(
            PostingObservation.job_posting_id,
            PostingObservation.id,
            func.row_number()
            .over(
                partition_by=PostingObservation.job_posting_id,
                order_by=(PostingObservation.observed_at.desc(), PostingObservation.id.desc()),
            )
            .label("position"),
        )
        .where(PostingObservation.job_posting_id.in_([item["job_posting_id"] for item in items]))
        .subquery()
    )
    return dict(
        session.execute(
            select(ranked.c.job_posting_id, ranked.c.id).where(ranked.c.position == 1)
        ).all()
    )


def _observations_for_evaluations(
    session: Session,
    evaluations: list[tuple[JobEvaluation, str]],
    latest_observation_ids: dict[str, str],
) -> dict[str, PostingObservation]:
    observation_ids = set(latest_observation_ids.values()) | {
        evaluation.observation_id for evaluation, _ in evaluations if evaluation.observation_id
    }
    return {
        observation.id: observation
        for observation in session.scalars(
            select(PostingObservation).where(PostingObservation.id.in_(observation_ids))
        )
    }


def _reviews_by_evaluation(
    session: Session, evaluations: list[tuple[JobEvaluation, str]]
) -> dict[str, dict[str, EvaluationCurrentnessReview]]:
    reviews_by_evaluation: dict[str, dict[str, EvaluationCurrentnessReview]] = {}
    for review in session.scalars(
        select(EvaluationCurrentnessReview).where(
            EvaluationCurrentnessReview.evaluation_id.in_(
                [evaluation.id for evaluation, _ in evaluations]
            )
        )
    ):
        reviews_by_evaluation.setdefault(review.evaluation_id, {})[review.snapshot_sha256] = review
    return reviews_by_evaluation


def _current_inputs_by_workspace(
    session: Session,
    evaluations: list[tuple[JobEvaluation, str]],
    known: Mapping[str, SearchParametersSnapshot],
) -> dict[str, dict[str, str | None]]:
    current_by_workspace: dict[str, dict[str, str | None]] = {}
    for workspace_id in {workspace_id for _, workspace_id in evaluations}:
        overrides = {key: text for key, (_, text) in overrides_in(session, workspace_id).items()}
        with prompts.overriding(overrides):
            fingerprint = prompts.suite_fingerprint()
        snapshot = (
            known[workspace_id]
            if workspace_id in known
            else parameter_snapshot_in(session, workspace_id)
        )
        current_by_workspace[workspace_id] = {
            "cv": current_version_id(session, workspace_id),
            "search_parameters": snapshot.parameter_set_id,
            "prompts": fingerprint,
            "build": current_build().revision or None,
        }
    return current_by_workspace


def _evaluation_inputs_and_review(
    evaluation: JobEvaluation,
    *,
    workspace_id: str,
    current_by_workspace: dict[str, dict[str, str | None]],
    latest_observation_ids: dict[str, str],
    observations: dict[str, PostingObservation],
    reviews_by_evaluation: dict[str, dict[str, EvaluationCurrentnessReview]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report = evaluation.fit_report_payload or {}
    recorded = {
        "cv": evaluation.master_cv_version_id,
        "search_parameters": evaluation.search_parameter_set_id,
        "posting": evaluation.observation_id,
        "prompts": report.get("prompt_fingerprint"),
        "build": report.get("code_revision"),
    }
    current = {
        **current_by_workspace[workspace_id],
        "posting": latest_observation_ids.get(evaluation.job_posting_id),
    }
    inputs = [
        EvaluationInputResponse.model_validate(
            {
                "name": name,
                "recorded": value,
                "current": current[name],
                "status": _input_status(name, value, current[name], observations),
            }
        ).model_dump()
        for name, value in recorded.items()
    ]
    review_status, reason = _review_status(inputs)
    current_posting_id = current["posting"]
    current_posting = observations.get(current_posting_id) if current_posting_id else None
    snapshot = _review_snapshot(current, current_posting)
    matching_review = (
        reviews_by_evaluation.get(evaluation.id, {}).get(snapshot["sha256"])
        if snapshot is not None
        else None
    )
    return inputs, {
        "status": "reviewed"
        if matching_review and review_status == "needs_review"
        else review_status,
        "reason": reason,
        "reviewed_at": matching_review.reviewed_at if matching_review else None,
    }


def _input_status(
    name: str,
    recorded: str | None,
    current: str | None,
    observations: dict[str, PostingObservation],
) -> str:
    if name == "posting":
        return _posting_status(observations.get(recorded or ""), observations.get(current or ""))
    if name == "prompts" and recorded == RULE_REFUSAL_PROMPT_FINGERPRINT:
        return "unknown"
    return _status(recorded, current)


def current_review_snapshot(
    session: Session, evaluation: JobEvaluation, workspace_id: str
) -> dict[str, str] | None:
    """Return the exact material inputs a person may acknowledge as reviewed."""
    current_observation = session.scalar(
        select(PostingObservation)
        .where(PostingObservation.job_posting_id == evaluation.job_posting_id)
        .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        .limit(1)
    )
    current = {
        "cv": current_version_id(session, workspace_id),
        "search_parameters": parameter_snapshot_in(session, workspace_id).parameter_set_id,
    }
    return _review_snapshot(current, current_observation)


def acknowledge_evaluation_currentness(
    session: Session,
    *,
    workspace_id: str,
    job_posting_id: str,
    evaluation_id: str,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
) -> None:
    """Record a free human decision to retain one historical evaluation."""
    evaluation = require_owned_entity(session, workspace_id, "evaluation", evaluation_id)
    if evaluation.job_posting_id != job_posting_id:
        raise ValidationError("That evaluation does not belong to this posting.")
    item: dict[str, Any] = {
        "evaluation_id": evaluation.id,
        "job_posting_id": job_posting_id,
    }
    attach_evaluation_inputs(session, [item])
    review = item["evaluation_review"]
    if review["status"] != "needs_review":
        raise ValidationError("Only an evaluation with changed inputs can be marked reviewed.")
    snapshot = current_review_snapshot(session, evaluation, workspace_id)
    if snapshot is None:
        raise ValidationError("Current posting or decision-criteria identities are unavailable.")
    existing = session.scalar(
        select(EvaluationCurrentnessReview).where(
            EvaluationCurrentnessReview.evaluation_id == evaluation.id,
            EvaluationCurrentnessReview.snapshot_sha256 == snapshot["sha256"],
        )
    )
    if existing is not None:
        return
    audit = audit_columns(actor, origin, correlation_id)
    record = EvaluationCurrentnessReview(
        evaluation_id=evaluation.id,
        snapshot_sha256=snapshot["sha256"],
        posting_content_sha256=snapshot["posting_content_sha256"],
        posting_status=snapshot["posting_status"],
        master_cv_version_id=snapshot["master_cv_version_id"],
        search_parameter_set_id=snapshot["search_parameter_set_id"],
        reviewed_at=utc_now(),
        **audit,
    )
    session.add(record)
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            action="evaluation.currentness_reviewed",
            entity_type="job_evaluation",
            entity_id=evaluation.id,
            details={"job_posting_id": job_posting_id, "snapshot_sha256": snapshot["sha256"]},
            **audit,
        )
    )


def _review_status(inputs: list[dict[str, Any]]) -> tuple[str, str | None]:
    posting = next((item for item in inputs if item["name"] == "posting"), None)
    if posting is not None and posting["status"] == "changed":
        return "needs_review", "posting_changed"
    if any(
        item["name"] in {"cv", "search_parameters"} and item["status"] == "changed"
        for item in inputs
    ):
        return "needs_review", "criteria_changed"
    if not inputs or posting is None or posting["status"] == "unknown":
        return "unknown", None
    return "current", None


def _review_snapshot(
    current: dict[str, str | None], observation: PostingObservation | None
) -> dict[str, str] | None:
    if (
        observation is None
        or not observation.content_sha256
        or not observation.posting_status
        or not current.get("cv")
        or not current.get("search_parameters")
    ):
        return None
    cv_version_id = current["cv"]
    parameter_set_id = current["search_parameters"]
    if cv_version_id is None or parameter_set_id is None:
        return None
    values: dict[str, str] = {
        "posting_content_sha256": observation.content_sha256,
        "posting_status": observation.posting_status,
        "master_cv_version_id": cv_version_id,
        "search_parameter_set_id": parameter_set_id,
    }
    return {**values, "sha256": sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()}


def _status(recorded: str | None, current: str | None) -> str:
    if not recorded or not current:
        return "unknown"
    return "current" if recorded == current else "changed"


def _posting_status(recorded: PostingObservation | None, current: PostingObservation | None) -> str:
    """Compare material posting inputs, not append-only observation identifiers."""
    if recorded is None or current is None:
        return "unknown"
    if recorded.posting_status != current.posting_status:
        return "changed"
    if recorded.content_sha256 and current.content_sha256:
        return "current" if recorded.content_sha256 == current.content_sha256 else "changed"
    return "current" if recorded.id == current.id else "unknown"
