"""Build the canonical job ledger statement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Integer, Select, and_, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.job_search._support import (
    _compensation_component,
    _current_evaluation_id_expression,
)
from clearscout.application.job_search.evaluation_currentness import attach_evaluation_inputs
from clearscout.application.job_search.models import (
    JobEmployerFacetResponse,
    JobListQuery,
    JobSummaryResponse,
    WorkspaceSummaryResponse,
)
from clearscout.application.portability.completeness import (
    CompletenessInputs,
    evaluate_completeness,
)
from clearscout.application.workspaces.parameters import parameter_snapshot_in
from clearscout.domain import (
    PURSUED_STATUSES,
    SCORE_COMPONENTS,
    ApplicationStatus,
    EvaluationDecision,
)
from clearscout.domain.identity import normalize_text
from clearscout.domain.places import PlaceReader
from clearscout.infrastructure.persistence.models import (
    Application,
    ApplicationEvent,
    Artifact,
    AuditEvent,
    CompensationComponent,
    EligibilityRecord,
    Employer,
    EvaluationScore,
    EvaluationScoreEvidence,
    JobEvaluation,
    JobPosting,
    MatchEvidence,
    PostingObservation,
    ScoreWeightSet,
    Workspace,
)

if TYPE_CHECKING:
    from clearscout.infrastructure.persistence.database import Database


def _current_status_expression() -> Any:
    """Select the status of the newest canonical event on the active application."""
    return (
        select(ApplicationEvent.status)
        .join(Application, Application.id == ApplicationEvent.application_id)
        .where(
            Application.job_posting_id == JobPosting.id,
            Application.archived_at.is_(None),
        )
        .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )


def _application_id_expression() -> Any:
    """Select the active application identity carried by a job projection."""
    return (
        select(Application.id)
        .where(
            Application.job_posting_id == JobPosting.id,
            Application.archived_at.is_(None),
        )
        .order_by(Application.created_at.desc(), Application.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )


def _with_completeness(row: dict[str, Any], *, allow_staged: bool = False) -> dict[str, Any]:
    """Attach actionable completeness from the bounded list projection."""
    job_id = str(row["job_posting_id"])
    row["completeness"] = evaluate_completeness(
        job_id,
        CompletenessInputs(
            posting_body=bool(row.pop("_posting_body"))
            or (allow_staged and bool(row.pop("_posting_body_staged"))),
            score_evidence=bool(row.pop("_score_evidence")),
            fit_report=bool(row.pop("_fit_report"))
            or (allow_staged and bool(row.pop("_fit_report_staged"))),
            observed_status_eligibility=bool(row.pop("_observed_status_eligibility")),
            normalized_identity=bool(row.pop("_normalized_identity")),
        ),
    )
    row.pop("_completeness_status", None)
    row.pop("_posting_body_staged", None)
    row.pop("_fit_report_staged", None)
    return row


def place_countries(place: str | None, reader: PlaceReader) -> list[str]:
    """Name every country a matched saved location states or suggests."""
    return list(reader.read(place or "").countries)


def _enrich_with_facts(
    session: Session, items: list[dict[str, Any]], workspace_id: str
) -> list[dict[str, Any]]:
    """Add compensation, eligibility, and place countries to each job summary.

    Two reads for the whole page rather than two per row, which is why the
    projection does not carry them.
    """
    if not items:
        return items

    snapshot = parameter_snapshot_in(session, workspace_id)
    attach_evaluation_inputs(session, items, parameters={workspace_id: snapshot})
    place_reader = snapshot.parameters.place_reader

    identifiers = [item["job_posting_id"] for item in items]
    comp_map: dict[str, list[dict[str, Any]]] = {jid: [] for jid in identifiers}
    elig_map: dict[str, list[dict[str, Any]]] = {jid: [] for jid in identifiers}

    comps = session.scalars(
        select(CompensationComponent).where(CompensationComponent.job_posting_id.in_(identifiers))
    ).all()
    for comp in comps:
        comp_map[comp.job_posting_id].append(_compensation_component(comp))

    eligs = session.scalars(
        select(EligibilityRecord).where(EligibilityRecord.job_posting_id.in_(identifiers))
    ).all()
    for elig in eligs:
        elig_map[elig.job_posting_id].append(
            {
                "workspace_location_key": elig.workspace_location_key,
                "region_rule": elig.region_rule,
                "arrangement": elig.arrangement,
                "details": elig.details,
            }
        )

    for item in items:
        jid = item["job_posting_id"]
        item["compensation_components"] = comp_map[jid]
        item["eligibility_records"] = elig_map[jid]
        item["countries"] = place_countries(item["place"], place_reader)

    return items


def _compensation_standing_source(evaluation_id: Any) -> Any:
    return (
        select(EvaluationScore.standing_source)
        .where(
            EvaluationScore.evaluation_id == evaluation_id,
            EvaluationScore.component == "compensation",
        )
        .correlate(JobEvaluation)
        .scalar_subquery()
    )


def _evaluation_score_evidence_context(evaluation_id: Any) -> tuple[Any, Any, Any]:
    """Build the shared score/evidence predicates for a saved evaluation."""
    score = aliased(EvaluationScore)
    link = aliased(EvaluationScoreEvidence)
    has_evidence = exists(
        select(1).select_from(link).where(link.evaluation_score_id == score.id)
    ).correlate(score)
    component_count = (
        select(func.count(score.id))
        .where(score.evaluation_id == evaluation_id)
        .correlate(JobEvaluation)
        .scalar_subquery()
    )
    return score, has_evidence, component_count


def _overall_score(evaluation_id: Any) -> Any:
    """Derive the weighted total from the five supported component rows."""
    score, has_evidence, component_count = _evaluation_score_evidence_context(evaluation_id)
    missing_weighted_component = exists(
        select(1)
        .select_from(score)
        .where(
            score.evaluation_id == evaluation_id,
            score.weight > 0,
            or_(score.score.is_(None), ~has_evidence),
        )
    ).correlate(JobEvaluation)
    weighted_sum = (
        select(func.sum(score.score * score.weight))
        .where(
            score.evaluation_id == evaluation_id,
            score.score.is_not(None),
            has_evidence,
        )
        .correlate(JobEvaluation)
        .scalar_subquery()
    )
    return case(
        (
            and_(component_count == len(SCORE_COMPONENTS), ~missing_weighted_component),
            cast(func.round(weighted_sum), Integer),
        ),
        else_=None,
    )


def _overall_score_lower_bound(evaluation_id: Any) -> Any:
    """Return the supported weighted contribution when its stored policy is complete."""
    score, has_evidence, component_count = _evaluation_score_evidence_context(evaluation_id)
    weight_sum = (
        select(func.sum(score.weight))
        .where(score.evaluation_id == evaluation_id)
        .correlate(JobEvaluation)
        .scalar_subquery()
    )
    invalid_weight = exists(
        select(1)
        .select_from(score)
        .where(
            score.evaluation_id == evaluation_id,
            or_(score.weight.is_(None), score.weight < 0),
        )
    ).correlate(JobEvaluation)
    supported_sum = (
        select(func.coalesce(func.sum(score.score * score.weight), 0))
        .where(score.evaluation_id == evaluation_id, score.score.is_not(None), has_evidence)
        .correlate(JobEvaluation)
        .scalar_subquery()
    )
    return case(
        (
            and_(
                component_count == len(SCORE_COMPONENTS),
                ~invalid_weight,
                func.abs(weight_sum - 1) < 0.000001,
            ),
            cast(func.round(supported_sum), Integer),
        ),
        else_=None,
    )


def _unscored_weighted_component_count(evaluation_id: Any) -> Any:
    """Count positive-weight categories excluded from a lower bound."""
    score = aliased(EvaluationScore)
    link = aliased(EvaluationScoreEvidence)
    has_evidence = exists(
        select(1).select_from(link).where(link.evaluation_score_id == score.id)
    ).correlate(score)
    return (
        select(func.count(score.id))
        .where(
            score.evaluation_id == evaluation_id,
            score.weight > 0,
            or_(score.score.is_(None), ~has_evidence),
        )
        .correlate(JobEvaluation)
        .scalar_subquery()
    )


def _posting_body_exists(
    observation: Any,
    body_artifact: Any,
    latest_observation_id: Any,
    *,
    integrity_status: str,
) -> Any:
    """Return the ownership and integrity predicate for the latest posting body."""
    conditions = [
        observation.id == latest_observation_id,
        observation.job_posting_id == JobPosting.id,
        body_artifact.workspace_id == JobPosting.workspace_id,
        body_artifact.archived_at.is_(None),
        body_artifact.sha256 == observation.content_sha256,
        body_artifact.integrity_status == integrity_status,
    ]
    if integrity_status == "verified":
        conditions.append(body_artifact.last_verified_at.is_not(None))
    return exists(
        select(1)
        .select_from(observation)
        .join(body_artifact, body_artifact.id == observation.body_artifact_id)
        .where(*conditions)
        .correlate(JobPosting)
    )


def _latest_observation_id(observation: Any) -> Any:
    """Select the id of the newest observation of the correlated posting."""
    return (
        select(observation.id)
        .where(observation.job_posting_id == JobPosting.id)
        .order_by(observation.observed_at.desc(), observation.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )


def _jobs_statement(
    workspace_id: str | None, *, include_archived: bool = False
) -> Select[*tuple[Any, ...]]:
    observation = aliased(PostingObservation)
    body_artifact = aliased(Artifact)
    fit_artifact = aliased(Artifact)
    score = aliased(EvaluationScore)
    score_link = aliased(EvaluationScoreEvidence)
    score_evidence = aliased(MatchEvidence)
    manual_review = aliased(AuditEvent)
    latest_manual_review_id = (
        select(AuditEvent.id)
        .where(
            AuditEvent.workspace_id == JobPosting.workspace_id,
            AuditEvent.action == "job.manual_review_saved",
            AuditEvent.entity_type == "job_posting",
            AuditEvent.entity_id == JobPosting.id,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )

    current_status = func.coalesce(
        _current_status_expression(), ApplicationStatus.NOT_APPLIED.value
    ).label("application_status")
    latest_observation_id = _latest_observation_id(observation)
    posting_body_ok = _posting_body_exists(
        observation, body_artifact, latest_observation_id, integrity_status="verified"
    )
    posting_body_staged = _posting_body_exists(
        observation, body_artifact, latest_observation_id, integrity_status="staged"
    )
    anchored_score_evidence = exists(
        select(1)
        .select_from(score_link)
        .join(score_evidence, score_evidence.id == score_link.match_evidence_id)
        .where(
            score_link.evaluation_score_id == score.id,
            score_evidence.artifact_id.is_not(None),
            or_(
                score_evidence.char_start.is_not(None),
                func.length(func.trim(score_evidence.locator)) > 0,
            ),
        )
        .correlate(score)
    )
    invalid_score = exists(
        select(1)
        .select_from(score)
        .where(
            score.evaluation_id == JobEvaluation.id,
            or_(
                and_(
                    score.score.is_(None),
                    or_(
                        score.no_evidence_reason.is_(None),
                        func.length(func.trim(score.no_evidence_reason)) == 0,
                    ),
                ),
                and_(
                    or_(score.score.is_not(None), score.finding_state == "conflict"),
                    ~anchored_score_evidence,
                ),
            ),
        )
        .correlate(JobEvaluation)
    )
    score_evidence_ok = and_(
        exists(
            select(1)
            .select_from(score)
            .where(score.evaluation_id == JobEvaluation.id)
            .correlate(JobEvaluation)
        ),
        ~invalid_score,
    )
    fit_report_ok = exists(
        select(1)
        .select_from(fit_artifact)
        .where(
            fit_artifact.id == JobEvaluation.fit_report_artifact_id,
            fit_artifact.workspace_id == JobPosting.workspace_id,
            fit_artifact.archived_at.is_(None),
            fit_artifact.byte_size > 0,
            fit_artifact.integrity_status == "verified",
            fit_artifact.last_verified_at.is_not(None),
        )
        .correlate(JobEvaluation, JobPosting)
    )
    fit_report_staged = exists(
        select(1)
        .select_from(fit_artifact)
        .where(
            fit_artifact.id == JobEvaluation.fit_report_artifact_id,
            fit_artifact.workspace_id == JobPosting.workspace_id,
            fit_artifact.archived_at.is_(None),
            fit_artifact.byte_size > 0,
            fit_artifact.integrity_status == "staged",
        )
        .correlate(JobEvaluation, JobPosting)
    )
    observed_status_eligibility_ok = exists(
        select(1)
        .select_from(observation)
        .where(
            observation.id == latest_observation_id,
            observation.job_posting_id == JobPosting.id,
            func.length(func.trim(observation.posting_status)) > 0,
            exists(
                select(1)
                .select_from(EligibilityRecord)
                .where(
                    EligibilityRecord.job_posting_id == JobPosting.id,
                    EligibilityRecord.observation_id == observation.id,
                )
                .correlate(observation, JobPosting)
            ),
        )
        .correlate(JobEvaluation, JobPosting)
    )
    # Deduplication belongs to the identity path (PostingIdentifier lookup at
    # commit time), not to the completeness projection: two distinct postings
    # from the same employer can legitimately share a normalized title.
    normalized_identity_ok = and_(
        func.length(Employer.normalized_name) > 0,
        func.length(JobPosting.normalized_title) > 0,
        Employer.normalized_name == func.clearscout_normalize(Employer.name),
        JobPosting.normalized_title == func.clearscout_normalize(JobPosting.title),
    )
    completeness_status = case(
        (
            and_(
                posting_body_ok,
                score_evidence_ok,
                fit_report_ok,
                observed_status_eligibility_ok,
                normalized_identity_ok,
            ),
            "complete",
        ),
        else_="incomplete",
    ).label("_completeness_status")

    def component_score(component: str) -> Any:
        return (
            select(
                case(
                    (
                        exists(
                            select(1)
                            .select_from(score_link)
                            .where(score_link.evaluation_score_id == EvaluationScore.id)
                        ),
                        EvaluationScore.score,
                    ),
                    else_=None,
                )
            )
            .where(
                EvaluationScore.evaluation_id == JobEvaluation.id,
                EvaluationScore.component == component,
            )
            .correlate(JobEvaluation)
            .scalar_subquery()
        )

    statement = (
        select(
            JobPosting.id.label("job_posting_id"),
            Employer.name.label("employer"),
            JobPosting.title,
            JobPosting.first_seen_at,
            JobPosting.archived_at,
            JobEvaluation.id.label("evaluation_id"),
            JobEvaluation.version_number.label("evaluation_version"),
            JobEvaluation.decision,
            JobEvaluation.rationale,
            JobEvaluation.scope_summary,
            JobEvaluation.qualifications_summary,
            JobEvaluation.evaluated_at,
            manual_review.details["priority"].as_string().label("manual_priority"),
            manual_review.details["fit"].as_string().label("manual_fit"),
            func.coalesce(manual_review.details["note"].as_string(), "").label("manual_note"),
            manual_review.created_at.label("manual_reviewed_at"),
            manual_review.details["recorded_fit_score"].as_integer().label("recorded_fit_score"),
            manual_review.details["recorded_compensation_score"]
            .as_integer()
            .label("recorded_compensation_score"),
            case(
                (
                    func.json_type(manual_review.details, "$.recorded_overall_score").is_not(None),
                    manual_review.details["recorded_overall_score"].as_integer(),
                ),
                else_=manual_review.details["tracker_composite_score"].as_integer(),
            ).label("recorded_overall_score"),
            case(
                (
                    func.json_type(manual_review.details, "$.recorded_compensation").is_not(None),
                    manual_review.details["recorded_compensation"].as_string(),
                ),
                else_=func.coalesce(manual_review.details["recorded_posted_pay"].as_string(), ""),
            ).label("recorded_compensation"),
            func.coalesce(manual_review.details["recorded_work_model"].as_string(), "").label(
                "recorded_work_model"
            ),
            func.coalesce(manual_review.details["recorded_eligibility"].as_string(), "").label(
                "recorded_eligibility"
            ),
            func.coalesce(manual_review.details["next_action"].as_string(), "").label(
                "next_action"
            ),
            func.coalesce(manual_review.details["recorded_deadline"].as_string(), "").label(
                "recorded_deadline"
            ),
            func.coalesce(manual_review.details["recorded_source"].as_string(), "").label(
                "recorded_source"
            ),
            component_score("fit").label("fit_score"),
            component_score("compensation").label("compensation_score"),
            component_score("eligibility").label("eligibility_score"),
            component_score("evidence").label("evidence_score"),
            component_score("opportunity").label("opportunity_score"),
            _overall_score(JobEvaluation.id).label("overall_score"),
            _overall_score_lower_bound(JobEvaluation.id).label("overall_score_lower_bound"),
            _unscored_weighted_component_count(JobEvaluation.id).label(
                "unscored_weighted_component_count"
            ),
            _compensation_standing_source(JobEvaluation.id).label("compensation_standing_source"),
            # Older evaluations have no policy version and remain unknown.
            (
                select(ScoreWeightSet.name)
                .where(ScoreWeightSet.id == JobEvaluation.score_weight_set_id)
                .correlate(JobEvaluation)
                .scalar_subquery()
            ).label("score_policy_name"),
            (
                select(ScoreWeightSet.version)
                .where(ScoreWeightSet.id == JobEvaluation.score_weight_set_id)
                .correlate(JobEvaluation)
                .scalar_subquery()
            ).label("score_policy_version"),
            current_status,
            _application_id_expression().label("application_id"),
            (
                select(func.count(ApplicationEvent.id))
                .where(ApplicationEvent.application_id == _application_id_expression())
                .correlate(JobPosting)
                .scalar_subquery()
            ).label("application_event_count"),
            # Report the committed eligibility result, not a current re-evaluation.
            (
                select(func.max(EligibilityRecord.workspace_location_key))
                .where(EligibilityRecord.job_posting_id == JobPosting.id)
                .correlate(JobPosting)
                .scalar_subquery()
            ).label("place"),
            (
                select(observation.posting_status)
                .where(observation.id == latest_observation_id)
                .correlate(JobPosting)
                .scalar_subquery()
            ).label("posting_status"),
            (
                select(observation.observed_at)
                .where(observation.id == latest_observation_id)
                .correlate(JobPosting)
                .scalar_subquery()
            ).label("last_verified_at"),
            JobPosting.closing_date_override,
            (
                select(observation.publisher_closing_date)
                .where(observation.id == latest_observation_id)
                .correlate(JobPosting)
                .scalar_subquery()
            ).label("publisher_closing_date"),
            posting_body_ok.label("_posting_body"),
            posting_body_staged.label("_posting_body_staged"),
            score_evidence_ok.label("_score_evidence"),
            fit_report_ok.label("_fit_report"),
            fit_report_staged.label("_fit_report_staged"),
            observed_status_eligibility_ok.label("_observed_status_eligibility"),
            normalized_identity_ok.label("_normalized_identity"),
            completeness_status,
        )
        .join(Employer, Employer.id == JobPosting.employer_id)
        .outerjoin(manual_review, manual_review.id == latest_manual_review_id)
        .outerjoin(
            JobEvaluation,
            JobEvaluation.id == _current_evaluation_id_expression(),
        )
        .where(Employer.archived_at.is_(None))
    )
    if not include_archived:
        statement = statement.where(JobPosting.archived_at.is_(None))
    if workspace_id is not None:
        statement = statement.where(JobPosting.workspace_id == workspace_id)
    return statement


# Completeness sorts by its projected status because it is derived.
SORTABLE = (
    "evaluated_at",
    "first_seen_at",
    "employer",
    "title",
    "decision",
    "recorded_score",
    "overall_score",
    "fit_score",
    "compensation_score",
    "eligibility_score",
    "evidence_score",
    "opportunity_score",
    "application_status",
    "manual_priority",
    "manual_fit",
)

LEGACY_REVIEW_SORTS = {
    "recorded_fit_score": "fit_score",
    "recorded_compensation_score": "compensation_score",
    "tracker_composite_score": "recorded_score",
}

COMPLETENESS_FILTERS = frozenset({"", "complete", "incomplete", "incomplete_only"})


def list_jobs(database: Database, query: JobListQuery) -> tuple[list[JobSummaryResponse], int]:
    """Return a filtered page with current application state derived from history.

    Archived jobs stay out by default. Asking for them is the only way to find a
    job that was archived by mistake, since nothing else lists it.
    """
    _validate_job_list_query(query)
    with database.session() as session:
        page, projection = _job_page(query)
        total = int(
            session.scalar(select(func.count()).select_from(page.order_by(None).subquery())) or 0
        )
        ordered = _ordered_job_page(page, projection, query)
        rows = session.execute(ordered.offset(query.offset).limit(query.limit)).mappings()
        items = [_with_completeness(dict(row)) for row in rows]
        return [
            JobSummaryResponse.model_validate(item)
            for item in _enrich_with_facts(session, items, query.workspace_id)
        ], total


def job_employer_facets(database: Database, query: JobListQuery) -> list[JobEmployerFacetResponse]:
    """Count postings per employer under every filter except the employer selection."""
    _validate_job_list_query(query)
    with database.session() as session:
        page = _job_page(query.model_copy(update={"employers": ()}))[0].subquery()
        postings = func.count(func.distinct(page.c.job_posting_id))
        rows = session.execute(
            select(page.c.employer, postings)
            .group_by(page.c.employer)
            .order_by(postings.desc(), page.c.employer)
        ).all()
        return [JobEmployerFacetResponse(name=name, count=int(count)) for name, count in rows]


def _job_page(query: JobListQuery) -> tuple[Any, Any]:
    statement = _jobs_statement(query.workspace_id, include_archived=query.include_archived)
    projection = statement.where(*_job_filters(query)).subquery()
    return _filtered_job_page(projection, query), projection


def _validate_job_list_query(query: JobListQuery) -> None:
    if query.limit < 1 or query.limit > 250 or query.offset < 0:
        raise ValidationError("Pagination requires offset >= 0 and 1 <= limit <= 250.")
    if query.completeness not in COMPLETENESS_FILTERS:
        raise ValidationError("Unsupported completeness filter.")


def _job_filters(query: JobListQuery) -> list[Any]:
    filters: list[Any] = []
    term = query.search.strip()
    if term:
        latest, stated = aliased(PostingObservation), aliased(PostingObservation)
        location = (
            select(stated.location)
            .where(stated.id == _latest_observation_id(latest))
            .correlate(JobPosting)
            .scalar_subquery()
        )
        filters.append(
            or_(
                *(
                    column.icontains(term, autoescape=True)
                    for column in (
                        Employer.name,
                        JobPosting.title,
                        JobEvaluation.scope_summary,
                        JobEvaluation.rationale,
                        location,
                    )
                )
            )
        )
    if query.employers:
        filters.append(
            Employer.normalized_name.in_(sorted({normalize_text(name) for name in query.employers}))
        )
    if query.decisions:
        filters.append(JobEvaluation.decision.in_(query.decisions))
    if query.application_statuses:
        filters.append(_current_status_expression().in_(query.application_statuses))
    if query.places:
        filters.append(
            JobPosting.id.in_(
                select(EligibilityRecord.job_posting_id).where(
                    EligibilityRecord.workspace_location_key.in_(query.places)
                )
            )
        )
    return filters


def _filtered_job_page(projection: Any, query: JobListQuery) -> Any:
    page = select(projection)
    if query.completeness == "complete":
        page = page.where(projection.c._completeness_status == "complete")
    elif query.completeness:
        page = page.where(projection.c._completeness_status != "complete")
    if query.posting_statuses:
        status_match = projection.c.posting_status.in_(query.posting_statuses)
        # A job never observed has no status; "active" means anything not closed.
        if "active" in query.posting_statuses:
            status_match = or_(status_match, projection.c.posting_status.is_(None))
        page = page.where(status_match)
    return page


def _review_score_expression(projection: Any, component: str) -> Any:
    if component == "overall":
        return func.coalesce(
            projection.c.recorded_overall_score,
            projection.c.overall_score,
            projection.c.overall_score_lower_bound,
        )
    return func.coalesce(
        projection.c[f"recorded_{component}_score"], projection.c[f"{component}_score"]
    )


def _ordered_job_page(page: Any, projection: Any, query: JobListQuery) -> Any:
    sort_columns = {name: projection.c[name] for name in SORTABLE if name != "recorded_score"}
    sort_columns["fit_score"] = _review_score_expression(projection, "fit")
    sort_columns["compensation_score"] = _review_score_expression(projection, "compensation")
    sort_columns["manual_priority"] = case(
        (projection.c.manual_priority == "high", 3),
        (projection.c.manual_priority == "medium", 2),
        (projection.c.manual_priority == "low", 1),
    )
    sort_columns["manual_fit"] = case(
        (projection.c.manual_fit == "strong", 3),
        (projection.c.manual_fit == "possible", 2),
        (projection.c.manual_fit == "weak", 1),
    )
    sort_columns["completeness"] = projection.c._completeness_status
    if (
        query.sort not in SORTABLE
        and query.sort not in LEGACY_REVIEW_SORTS
        and query.sort != "completeness"
    ):
        raise ValidationError(f"Unsupported job sort: {query.sort}.")
    sort_key = LEGACY_REVIEW_SORTS.get(query.sort, query.sort)
    # The evaluation floor is less precise than a person's score or an exact evaluation total.
    column = (
        _review_score_expression(projection, "overall")
        if sort_key == "recorded_score"
        else sort_columns[sort_key]
    )
    order = column.desc() if query.descending else column.asc()
    tie_breakers: list[Any] = []
    if sort_key == "recorded_score":
        tie_breakers.append(
            case(
                (
                    or_(
                        projection.c.recorded_overall_score.is_not(None),
                        projection.c.overall_score.is_not(None),
                    ),
                    0,
                ),
                else_=1,
            )
        )
    elif sort_key == "overall_score":
        tie_breakers.append(
            case((projection.c.compensation_standing_source == "compared", 0), else_=1)
        )
    elif sort_key in {"manual_priority", "manual_fit"}:
        tie_breakers.append(projection.c.first_seen_at.desc())
    tie_breakers.append(projection.c.job_posting_id)
    return page.order_by(order.nullslast(), *tie_breakers)


# Command-center sections expose strongest fits and undecided jobs.
UNDECIDED = (EvaluationDecision.INVESTIGATE, EvaluationDecision.MONITOR)
# A refusal is separate from relevance, so a job the user cannot take is never
# offered as a best fit.
UNOFFERABLE = (EvaluationDecision.INELIGIBLE,)
# A strong decision the user has not answered yet with an application event.
DECIDED = (EvaluationDecision.APPLY_NOW, EvaluationDecision.PREMIUM_TARGET)
HIGHLIGHT_LIMIT = 4
# This list expands in place to the whole set rather than paging.
DECIDED_WITHOUT_OUTCOME_LIMIT = 250


def _decided_without_outcome(projection: Any) -> Any:
    """Match a strong decision whose application still has no recorded outcome."""
    return and_(
        projection.c.decision.in_(DECIDED),
        projection.c.application_status == ApplicationStatus.NOT_APPLIED.value,
    )


def workspace_summary(database: Database, workspace_id: str) -> WorkspaceSummaryResponse:
    """Compute command-center totals from the complete workspace ledger."""
    with database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or workspace.archived_at is not None:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
        projection = _jobs_statement(workspace_id).subquery()
        aggregate = (
            session.execute(
                select(
                    func.count().label("total_jobs"),
                    func.sum(
                        case(
                            (projection.c.application_status.in_(PURSUED_STATUSES), 1),
                            else_=0,
                        )
                    ).label("actionable_applications"),
                    func.sum(
                        case((projection.c._completeness_status != "complete", 1), else_=0)
                    ).label("incomplete_jobs"),
                    func.sum(case((_decided_without_outcome(projection), 1), else_=0)).label(
                        "decided_without_outcome_count"
                    ),
                    *(
                        func.sum(
                            case((projection.c.application_status == status, 1), else_=0)
                        ).label(status)
                        for status in PURSUED_STATUSES
                    ),
                )
            )
            .mappings()
            .one()
        )
        highlights = _highlights(session, projection, workspace_id)
        return WorkspaceSummaryResponse.model_validate(
            {
                "total_jobs": int(aggregate["total_jobs"] or 0),
                "actionable_applications": int(aggregate["actionable_applications"] or 0),
                "incomplete_jobs": int(aggregate["incomplete_jobs"] or 0),
                "decided_without_outcome_count": int(
                    aggregate["decided_without_outcome_count"] or 0
                ),
                "application_funnel": {
                    status: int(aggregate[status] or 0) for status in PURSUED_STATUSES
                },
                **highlights,
            }
        )


def _highlights(
    session: Session, projection: Any, workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Pick the few jobs the command center leads with, by four different reads."""

    def top(
        *, ordering: list[Any], restrict: Any = None, limit: int = HIGHLIGHT_LIMIT
    ) -> list[dict[str, Any]]:
        statement = select(projection)
        if restrict is not None:
            statement = statement.where(restrict)
        rows = session.execute(
            statement.order_by(
                *ordering,
                projection.c.employer,
                projection.c.title,
                projection.c.job_posting_id,
            ).limit(limit)
        ).mappings()
        return [_with_completeness(dict(row)) for row in rows]

    views = {
        "highest_intrinsic": top(
            ordering=[_review_score_expression(projection, "fit").desc().nullslast()],
            restrict=and_(
                projection.c.evaluation_id.is_not(None),
                _review_score_expression(projection, "fit").is_not(None),
                or_(
                    projection.c.decision.is_(None),
                    projection.c.decision.notin_(UNOFFERABLE),
                ),
            ),
        ),
        "highest_upside": top(
            ordering=[_review_score_expression(projection, "overall").desc().nullslast()],
            restrict=projection.c.decision.in_(UNDECIDED),
        ),
        "actionable": top(
            ordering=[
                case(
                    *(
                        (projection.c.application_status == status, urgency)
                        for urgency, status in enumerate(PURSUED_STATUSES)
                    ),
                    else_=-1,
                )
                .desc()
                .nullslast()
            ],
            restrict=projection.c.application_status.in_(PURSUED_STATUSES),
        ),
        # A posting that closed while the decision waited is the most urgent row.
        "decided_without_outcome": top(
            ordering=[
                case((projection.c.posting_status == "closed", 0), else_=1).asc(),
                projection.c.evaluated_at.asc(),
            ],
            restrict=_decided_without_outcome(projection),
            limit=DECIDED_WITHOUT_OUTCOME_LIMIT,
        ),
    }
    # Enrich all sections in one pass because jobs can appear in several sections.
    _enrich_with_facts(session, [row for rows in views.values() for row in rows], workspace_id)
    return views
