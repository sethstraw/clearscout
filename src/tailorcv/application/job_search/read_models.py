"""Assemble durable job and evaluation read models."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from tailorcv.application.failures import NotFoundError, ValidationError
from tailorcv.application.job_search._support import (
    _application_event,
    _compensation_component,
    _decimal_float,
    _observation,
    _posting_requirement,
)
from tailorcv.application.job_search.models import JobDetailResponse
from tailorcv.application.job_search.queries import (
    _jobs_statement,
    _overall_score,
    _with_completeness,
    place_countries,
)
from tailorcv.application.workspaces.parameters import parameter_snapshot_in
from tailorcv.application.workspaces.settings import WorkspaceSettingsService
from tailorcv.domain.rulings import Ruling
from tailorcv.infrastructure.persistence.models import (
    Application,
    ApplicationEvent,
    CompensationComponent,
    EligibilityRecord,
    EvaluationFactor,
    EvaluationFactorEvidence,
    EvaluationScore,
    EvaluationScoreEvidence,
    JobEvaluation,
    JobPosting,
    MatchEvidence,
    PostingObservation,
    PostingRequirement,
    RequirementRuling,
    ScoreWeightSet,
)

if TYPE_CHECKING:
    from tailorcv.infrastructure.persistence.database import Database


@dataclass
class _JobDetailParts:
    """Loaded records needed to project one job-detail response."""

    detail: dict[str, Any]
    evaluation_id: str | None
    latest_observation: PostingObservation | None
    observations: Sequence[PostingObservation]
    observation_by_artifact: dict[str, str]
    evidence: Sequence[MatchEvidence]
    requirements: Sequence[PostingRequirement]
    factors: Sequence[EvaluationFactor]
    scores: Sequence[EvaluationScore]
    score_links: Sequence[EvaluationScoreEvidence]
    factor_links: Sequence[EvaluationFactorEvidence]
    evaluation: JobEvaluation | None
    weight_set: ScoreWeightSet | None
    events: Sequence[ApplicationEvent]
    evaluation_history: list[dict[str, Any]]
    observation_count: int
    evaluation_count: int
    application_event_count: int
    compensation: Sequence[CompensationComponent]
    eligibility: Sequence[EligibilityRecord]
    enable_dispute_reextraction: bool
    enable_dispute_rematch: bool
    disputed_fit_ruling_count: int
    disputed_extraction_ruling_count: int
    orphaned_rulings: Sequence[dict[str, Any]]


def job_detail(
    database: Database,
    job_posting_id: str,
    workspace_id: str,
    *,
    observation_offset: int = 0,
    evaluation_offset: int = 0,
    application_event_offset: int = 0,
    history_limit: int = 50,
) -> JobDetailResponse:
    """Return one job after proving workspace ownership.

    An archived job is readable here and reports its archived_at, because a page
    that cannot be opened cannot offer to restore itself. Writing beneath an
    archived job stays refused where those writes are handled.
    """
    with database.session() as session:
        return JobDetailResponse.model_validate(
            _job_detail(
                database,
                session,
                job_posting_id,
                workspace_id=workspace_id,
                include_archived=True,
                observation_offset=observation_offset,
                evaluation_offset=evaluation_offset,
                application_event_offset=application_event_offset,
                history_limit=history_limit,
            )
        )


def _job_detail(
    database: Database,
    session: Session,
    job_posting_id: str,
    *,
    workspace_id: str | None = None,
    allow_staged: bool = False,
    include_archived: bool = False,
    observation_offset: int = 0,
    evaluation_offset: int = 0,
    application_event_offset: int = 0,
    history_limit: int = 50,
) -> dict[str, Any]:
    """Load and project the durable records behind one job detail."""
    parts = _load_job_detail_parts(
        database,
        session,
        job_posting_id,
        workspace_id=workspace_id,
        include_archived=include_archived,
        observation_offset=observation_offset,
        evaluation_offset=evaluation_offset,
        application_event_offset=application_event_offset,
        history_limit=history_limit,
    )
    detail = _project_job_detail(parts, allow_staged=allow_staged)
    from tailorcv.application.job_search.evaluation_currentness import attach_evaluation_inputs

    owner = workspace_id or str(
        session.scalar(select(JobPosting.workspace_id).where(JobPosting.id == job_posting_id))
    )
    snapshot = parameter_snapshot_in(session, owner)
    detail["countries"] = place_countries(detail["place"], snapshot.parameters.place_reader)
    parameters = {owner: snapshot}
    attach_evaluation_inputs(session, [detail], parameters=parameters)
    history = [
        {**item, "evaluation_id": item["id"], "job_posting_id": job_posting_id}
        for item in detail["evaluation_history"]
    ]
    attach_evaluation_inputs(session, history, parameters=parameters)
    for item, enriched in zip(detail["evaluation_history"], history, strict=True):
        item["evaluation_inputs"] = enriched["evaluation_inputs"]
    return detail


def _paged_observations(
    session: Session, job_posting_id: str, *, offset: int, limit: int
) -> tuple[Sequence[PostingObservation], int]:
    statement = (
        select(PostingObservation)
        .where(PostingObservation.job_posting_id == job_posting_id)
        .order_by(PostingObservation.observed_at, PostingObservation.id)
    )
    count = int(
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )
    rows = session.scalars(statement.offset(offset).limit(limit)).all()
    return rows, count


def _latest_observation(session: Session, job_posting_id: str) -> PostingObservation | None:
    """Return the authoritative latest posting observation."""
    return session.scalar(
        select(PostingObservation)
        .where(PostingObservation.job_posting_id == job_posting_id)
        .order_by(PostingObservation.observed_at.desc(), PostingObservation.id.desc())
        .limit(1)
    )


def _observation_ids_by_artifact(
    session: Session,
    job_posting_id: str,
    evaluation: JobEvaluation | None,
    evidence: Sequence[MatchEvidence],
) -> dict[str, str]:
    """Resolve current-evaluation evidence without loading unbounded history."""
    if evaluation is None or evaluation.observation_id is None:
        return {}
    observation = session.scalar(
        select(PostingObservation).where(
            PostingObservation.id == evaluation.observation_id,
            PostingObservation.job_posting_id == job_posting_id,
        )
    )
    evidence_artifact_ids = {item.artifact_id for item in evidence if item.artifact_id}
    if observation is None or observation.body_artifact_id not in evidence_artifact_ids:
        return {}
    return {observation.body_artifact_id: observation.id}


def _paged_application_events(
    session: Session, application: Application | None, *, offset: int, limit: int
) -> tuple[Sequence[ApplicationEvent], int]:
    if application is None:
        return (), 0
    statement = (
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application.id)
        .order_by(ApplicationEvent.created_at, ApplicationEvent.id)
    )
    count = int(
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )
    rows = session.scalars(statement.offset(offset).limit(limit)).all()
    return rows, count


def _load_job_detail_parts(
    database: Database,
    session: Session,
    job_posting_id: str,
    *,
    workspace_id: str | None,
    include_archived: bool,
    observation_offset: int,
    evaluation_offset: int,
    application_event_offset: int,
    history_limit: int,
) -> _JobDetailParts:
    """Load the records that make up one job detail."""
    row = (
        session.execute(
            _jobs_statement(workspace_id, include_archived=include_archived).where(
                JobPosting.id == job_posting_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"Job {job_posting_id!r} does not exist.")
    if (
        min(observation_offset, evaluation_offset, application_event_offset) < 0
        or history_limit < 1
        or history_limit > 250
    ):
        raise ValidationError("History pagination requires offset >= 0 and 1 <= limit <= 250.")
    evaluation_id = str(row["evaluation_id"]) if row["evaluation_id"] is not None else None
    evaluation = session.get(JobEvaluation, evaluation_id) if evaluation_id else None
    observations, observation_count = _paged_observations(
        session, job_posting_id, offset=observation_offset, limit=history_limit
    )
    latest_observation = _latest_observation(session, job_posting_id)
    evidence = session.scalars(
        select(MatchEvidence)
        .where(MatchEvidence.evaluation_id == evaluation_id)
        .order_by(MatchEvidence.created_at, MatchEvidence.id)
    ).all()
    observation_by_artifact = _observation_ids_by_artifact(
        session, job_posting_id, evaluation, evidence
    )
    evidence_ids = {item.id for item in evidence}
    requirements = session.scalars(
        select(PostingRequirement)
        .where(PostingRequirement.job_evaluation_id == evaluation_id)
        .order_by(PostingRequirement.position, PostingRequirement.id)
    ).all()
    factors = session.scalars(
        select(EvaluationFactor)
        .where(EvaluationFactor.evaluation_id == evaluation_id)
        .order_by(EvaluationFactor.factor_type, EvaluationFactor.position)
    ).all()
    scores = session.scalars(
        select(EvaluationScore)
        .where(EvaluationScore.evaluation_id == evaluation_id)
        .order_by(EvaluationScore.created_at, EvaluationScore.id)
    ).all()
    score_ids = {item.id for item in scores}
    score_links = session.scalars(
        select(EvaluationScoreEvidence)
        .join(
            EvaluationScore,
            EvaluationScore.id == EvaluationScoreEvidence.evaluation_score_id,
        )
        .where(EvaluationScore.evaluation_id == evaluation_id)
        .order_by(EvaluationScoreEvidence.created_at, EvaluationScoreEvidence.id)
    ).all()
    score_links = [
        item
        for item in score_links
        if item.evaluation_score_id in score_ids and item.match_evidence_id in evidence_ids
    ]
    factor_links = session.scalars(
        select(EvaluationFactorEvidence)
        .join(
            EvaluationFactor,
            EvaluationFactor.id == EvaluationFactorEvidence.evaluation_factor_id,
        )
        .where(EvaluationFactor.evaluation_id == evaluation_id)
        .order_by(EvaluationFactorEvidence.created_at, EvaluationFactorEvidence.id)
    ).all()
    weight_set = (
        session.get(ScoreWeightSet, evaluation.score_weight_set_id)
        if evaluation is not None and evaluation.score_weight_set_id
        else None
    )
    application = session.scalar(
        select(Application).where(Application.job_posting_id == job_posting_id)
    )
    events, application_event_count = _paged_application_events(
        session, application, offset=application_event_offset, limit=history_limit
    )
    evaluation_history, evaluation_count = _evaluation_history(
        session,
        job_posting_id,
        evaluation_id,
        offset=evaluation_offset,
        limit=history_limit,
    )
    compensation = session.scalars(
        select(CompensationComponent)
        .where(CompensationComponent.job_posting_id == job_posting_id)
        .order_by(CompensationComponent.created_at, CompensationComponent.id)
    ).all()
    eligibility = session.scalars(
        select(EligibilityRecord)
        .where(EligibilityRecord.job_posting_id == job_posting_id)
        .order_by(EligibilityRecord.created_at, EligibilityRecord.id)
    ).all()
    posting_hint = session.scalar(
        select(JobPosting.posting_hint).where(JobPosting.id == job_posting_id)
    )
    (
        enable_dispute_reextraction,
        enable_dispute_rematch,
        disputed_fit_ruling_count,
        disputed_extraction_ruling_count,
    ) = (
        _dispute_projection(database, session, workspace_id, job_posting_id, evaluation_id)
        if evaluation_id is not None
        else (False, False, 0, 0)
    )
    owning_workspace_id = workspace_id or session.scalar(
        select(JobPosting.workspace_id).where(JobPosting.id == job_posting_id)
    )
    orphaned_rulings = (
        _orphaned_rulings(session, str(owning_workspace_id), job_posting_id, evaluation_id)
        if owning_workspace_id and evaluation_id is not None
        else []
    )
    detail = dict(row)
    detail["posting_hint"] = posting_hint
    return _JobDetailParts(
        detail=detail,
        evaluation_id=evaluation_id,
        latest_observation=latest_observation,
        observations=observations,
        observation_by_artifact=observation_by_artifact,
        evidence=evidence,
        requirements=requirements,
        factors=factors,
        scores=scores,
        score_links=score_links,
        factor_links=factor_links,
        evaluation=evaluation,
        weight_set=weight_set,
        events=events,
        evaluation_history=evaluation_history,
        observation_count=observation_count,
        evaluation_count=evaluation_count,
        application_event_count=application_event_count,
        compensation=compensation,
        eligibility=eligibility,
        enable_dispute_reextraction=enable_dispute_reextraction,
        enable_dispute_rematch=enable_dispute_rematch,
        disputed_fit_ruling_count=disputed_fit_ruling_count,
        disputed_extraction_ruling_count=disputed_extraction_ruling_count,
        orphaned_rulings=orphaned_rulings,
    )


def _dispute_projection(
    database: Database,
    session: Session,
    workspace_id: str | None,
    job_posting_id: str,
    current_evaluation_id: str,
) -> tuple[bool, bool, int, int]:
    """Return paid-path flags and current-evaluation dispute counts."""
    owning_workspace_id = workspace_id or session.scalar(
        select(JobPosting.workspace_id).where(JobPosting.id == job_posting_id)
    )
    if not owning_workspace_id:
        raise NotFoundError(f"Job {job_posting_id!r} does not exist.")
    settings = WorkspaceSettingsService(database).current(owning_workspace_id)
    return (
        settings.learning.enable_dispute_reextraction,
        settings.learning.enable_dispute_rematch,
        _count_disputed_rulings(
            session, owning_workspace_id, current_evaluation_id, Ruling.DISPUTED_FIT
        ),
        _count_disputed_rulings(
            session, owning_workspace_id, current_evaluation_id, Ruling.DISPUTED_EXTRACTION
        ),
    )


def _orphaned_rulings(
    session: Session,
    workspace_id: str,
    job_posting_id: str,
    current_evaluation_id: str,
) -> list[dict[str, Any]]:
    """Return active rulings whose requirement_key is on a past evaluation but not the current."""
    dispute_values = (Ruling.DISPUTED_FIT.value, Ruling.DISPUTED_EXTRACTION.value)
    historical_keys = select(PostingRequirement.requirement_key).where(
        PostingRequirement.job_posting_id == job_posting_id
    )
    current_keys = select(PostingRequirement.requirement_key).where(
        PostingRequirement.job_evaluation_id == current_evaluation_id
    )
    rows = session.scalars(
        select(RequirementRuling)
        .where(
            RequirementRuling.workspace_id == workspace_id,
            RequirementRuling.ruling.in_(dispute_values),
            RequirementRuling.requirement_key.in_(historical_keys),
            RequirementRuling.requirement_key.notin_(current_keys),
        )
        .order_by(RequirementRuling.updated_at.desc())
    ).all()
    if not rows:
        return []
    texts_query = session.execute(
        select(PostingRequirement.requirement_key, PostingRequirement.text)
        .where(
            PostingRequirement.job_posting_id == job_posting_id,
            PostingRequirement.requirement_key.in_(row.requirement_key for row in rows),
        )
        .order_by(PostingRequirement.created_at.desc())
    ).all()
    latest_text: dict[str, str] = {}
    for key, text in texts_query:
        latest_text.setdefault(key, text)
    return [
        {
            "requirement_key": row.requirement_key,
            "ruling": row.ruling,
            "note": row.note,
            "updated_at": row.updated_at,
            "requirement_text": latest_text.get(row.requirement_key, ""),
        }
        for row in rows
    ]


def _count_disputed_rulings(
    session: Session,
    workspace_id: str,
    current_evaluation_id: str,
    ruling: Ruling,
) -> int:
    """Count rulings whose requirement is still in the current evaluation.

    Rulings whose requirement_key dropped out of the current evaluation are
    counted separately by ``_orphaned_rulings`` and drive a different UI.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(RequirementRuling)
            .where(
                RequirementRuling.workspace_id == workspace_id,
                RequirementRuling.ruling == ruling.value,
                select(PostingRequirement.id)
                .where(
                    PostingRequirement.job_evaluation_id == current_evaluation_id,
                    PostingRequirement.requirement_key == RequirementRuling.requirement_key,
                )
                .exists(),
            )
        )
        or 0
    )


def _score_link_maps(
    score_links: Sequence[EvaluationScoreEvidence], score_by_id: dict[str, str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    links_by_evidence: dict[str, list[dict[str, Any]]] = {}
    links_by_score: dict[str, list[dict[str, Any]]] = {}
    for link in score_links:
        value = {
            "evidence_id": link.match_evidence_id,
            "direction": link.direction,
        }
        links_by_score.setdefault(link.evaluation_score_id, []).append(value)
        links_by_evidence.setdefault(link.match_evidence_id, []).append(
            {**value, "component": score_by_id[link.evaluation_score_id]}
        )
    return links_by_evidence, links_by_score


def _project_evidence_rows(
    evidence: Sequence[MatchEvidence],
    links_by_evidence: dict[str, list[dict[str, Any]]],
    observation_by_artifact: dict[str, str],
) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "text": item.evidence_text,
            "classification": item.classification,
            "artifact_id": item.artifact_id,
            "char_start": item.char_start,
            "char_end": item.char_end,
            "locator": item.locator,
            "source_kind": item.source_kind,
            "master_cv_entry_id": item.master_cv_entry_id,
            "requirement_id": item.requirement_id,
            "strength": item.strength,
            "score_links": links_by_evidence.get(item.id, []),
            "observation_id": (
                observation_by_artifact.get(item.artifact_id) if item.artifact_id else None
            ),
        }
        for item in evidence
    ]


def _project_requirement_rows(
    requirements: Sequence[PostingRequirement],
) -> list[dict[str, Any]]:
    return [
        _posting_requirement(
            {
                "requirement_id": item.requirement_id,
                "requirement_key": item.requirement_key,
                "text": item.text,
                "importance": item.importance,
                "position": item.position,
            }
        ).model_dump()
        for item in requirements
    ]


def _factor_evidence_ids(
    factor_links: Sequence[EvaluationFactorEvidence],
) -> dict[str, list[str]]:
    """Group linked evidence identifiers under the factor each link names."""
    grouped: dict[str, list[str]] = {item.evaluation_factor_id: [] for item in factor_links}
    for item in factor_links:
        grouped[item.evaluation_factor_id].append(item.match_evidence_id)
    return grouped


def _project_factor_rows(
    factors: Sequence[EvaluationFactor], factor_evidence: dict[str, list[str]]
) -> list[dict[str, Any]]:
    return [
        {
            "type": item.factor_type,
            "position": item.position,
            "text": item.text,
            "evidence_ids": factor_evidence.get(item.id, []),
        }
        for item in factors
    ]


def _project_score_policy(weight_set: ScoreWeightSet | None) -> dict[str, Any]:
    return {
        "id": weight_set.id if weight_set else "",
        "name": weight_set.name if weight_set else "stored weights",
        "version": weight_set.version if weight_set else 0,
    }


def _project_score_arithmetic(
    scores: Sequence[EvaluationScore], links_by_score: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Project each stored component score, blanking the ones no evidence supports."""
    products = {
        item.id: (
            Decimal(item.score) * Decimal(item.weight)
            if item.score is not None and item.weight is not None
            else None
        )
        for item in scores
    }
    return [
        {
            "component": item.component,
            "score": item.score if links_by_score.get(item.id) else None,
            "weight": float(item.weight) if item.weight is not None else None,
            "product": _decimal_float(products[item.id]) if links_by_score.get(item.id) else None,
            "finding_state": item.finding_state,
            "reason": item.no_evidence_reason or "",
            "standing_source": item.standing_source,
            "evidence_links": links_by_score.get(item.id, []),
        }
        for item in scores
    ]


def _unsupported_rows(
    scores: Sequence[EvaluationScore],
    factors: Sequence[EvaluationFactor],
    links_by_score: dict[str, list[dict[str, Any]]],
    factor_evidence: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """List the scores and factors that carry no stored evidence link."""
    rows: list[dict[str, Any]] = [
        {
            "kind": "score",
            "id": item.id,
            "label": item.component,
            "reason": item.no_evidence_reason or "No evidence link is stored.",
        }
        for item in scores
        if not links_by_score.get(item.id)
    ]
    rows.extend(
        {
            "kind": "factor",
            "id": item.id,
            "label": item.text,
            "reason": "No evidence link is stored.",
        }
        for item in factors
        if not factor_evidence.get(item.id)
    )
    return rows


def _project_eligibility_rows(
    eligibility: Sequence[EligibilityRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "workspace_location_key": item.workspace_location_key,
            "region_rule": item.region_rule,
            "arrangement": item.arrangement,
            "details": item.details,
        }
        for item in eligibility
    ]


def _project_job_detail(parts: _JobDetailParts, *, allow_staged: bool) -> dict[str, Any]:
    """Project loaded records into the job-detail wire shape."""
    detail = dict(parts.detail)
    detail["latest_observation"] = (
        _observation(parts.latest_observation) if parts.latest_observation is not None else None
    )
    detail["observations"] = [_observation(item) for item in parts.observations]
    detail["observation_count"] = parts.observation_count
    score_by_id = {item.id: item.component for item in parts.scores}
    links_by_evidence, links_by_score = _score_link_maps(parts.score_links, score_by_id)
    detail["evidence"] = _project_evidence_rows(
        parts.evidence, links_by_evidence, parts.observation_by_artifact
    )
    detail["requirements"] = _project_requirement_rows(parts.requirements)
    factor_evidence = _factor_evidence_ids(parts.factor_links)
    detail["factors"] = _project_factor_rows(parts.factors, factor_evidence)
    detail["score_policy"] = _project_score_policy(parts.weight_set)
    detail["score_arithmetic"] = _project_score_arithmetic(parts.scores, links_by_score)
    detail["unsupported"] = _unsupported_rows(
        parts.scores, parts.factors, links_by_score, factor_evidence
    )
    detail["fit_report_available"] = bool(
        parts.evaluation is not None and parts.evaluation.fit_report_artifact_id
    )
    detail["evaluation_history"] = parts.evaluation_history
    detail["evaluation_count"] = parts.evaluation_count
    detail["application_events"] = [_application_event(item) for item in parts.events]
    detail["application_event_count"] = parts.application_event_count
    detail["compensation_components"] = [
        _compensation_component(item) for item in parts.compensation
    ]
    detail["eligibility_records"] = _project_eligibility_rows(parts.eligibility)
    detail["enable_dispute_reextraction"] = parts.enable_dispute_reextraction
    detail["enable_dispute_rematch"] = parts.enable_dispute_rematch
    detail["disputed_fit_ruling_count"] = parts.disputed_fit_ruling_count
    detail["disputed_extraction_ruling_count"] = parts.disputed_extraction_ruling_count
    detail["orphaned_rulings"] = list(parts.orphaned_rulings)
    detail["orphaned_ruling_count"] = len(parts.orphaned_rulings)
    return _with_completeness(detail, allow_staged=allow_staged)


def _evaluation_history(
    session: Session,
    job_posting_id: str,
    current_evaluation_id: str | None,
    *,
    offset: int = 0,
    limit: int = 250,
) -> tuple[list[dict[str, Any]], int]:
    """Project the immutable chain and its score snapshots in newest-first order."""
    evidence_stmt = (
        select(EvaluationScore.id)
        .join(
            EvaluationScoreEvidence,
            EvaluationScoreEvidence.evaluation_score_id == EvaluationScore.id,
        )
        .group_by(EvaluationScore.id)
    )
    statement = (
        select(
            JobEvaluation.id,
            JobEvaluation.version_number,
            JobEvaluation.supersedes_evaluation_id,
            JobEvaluation.observation_id,
            JobEvaluation.master_cv_version_id,
            JobEvaluation.score_weight_set_id,
            JobEvaluation.fit_report_artifact_id,
            JobEvaluation.decision,
            JobEvaluation.rationale,
            JobEvaluation.scope_summary,
            JobEvaluation.qualifications_summary,
            JobEvaluation.evaluated_at,
            JobEvaluation.source_timestamp,
            func.max(
                case(
                    (
                        (EvaluationScore.component == "fit")
                        & (EvaluationScore.id.in_(evidence_stmt)),
                        EvaluationScore.score,
                    )
                )
            ).label("fit_score"),
            func.max(
                case(
                    (
                        (EvaluationScore.component == "compensation")
                        & (EvaluationScore.id.in_(evidence_stmt)),
                        EvaluationScore.score,
                    )
                )
            ).label("compensation_score"),
            func.max(
                case(
                    (
                        EvaluationScore.component == "compensation",
                        EvaluationScore.standing_source,
                    )
                )
            ).label("compensation_standing_source"),
            func.max(
                case(
                    (
                        (EvaluationScore.component == "eligibility")
                        & (EvaluationScore.id.in_(evidence_stmt)),
                        EvaluationScore.score,
                    )
                )
            ).label("eligibility_score"),
            func.max(
                case(
                    (
                        (EvaluationScore.component == "evidence")
                        & (EvaluationScore.id.in_(evidence_stmt)),
                        EvaluationScore.score,
                    )
                )
            ).label("evidence_score"),
            func.max(
                case(
                    (
                        (EvaluationScore.component == "opportunity")
                        & (EvaluationScore.id.in_(evidence_stmt)),
                        EvaluationScore.score,
                    )
                )
            ).label("opportunity_score"),
            _overall_score(JobEvaluation.id).label("overall_score"),
        )
        .outerjoin(EvaluationScore, EvaluationScore.evaluation_id == JobEvaluation.id)
        .where(JobEvaluation.job_posting_id == job_posting_id)
        .group_by(JobEvaluation.id)
        .order_by(JobEvaluation.version_number.desc(), JobEvaluation.id.desc())
    )
    total = int(
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )
    rows = session.execute(statement.offset(offset).limit(limit)).mappings()
    return (
        [{**dict(row), "is_current": str(row["id"]) == current_evaluation_id} for row in rows],
        total,
    )
