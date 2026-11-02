"""Commit immutable evaluations and their evidence-backed components.

This module owns the transaction. Target resolution, source storage, the fit
report, and the component families are appended by the modules beside it, all
inside the one session opened here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from tailorcv.application.contracts import EvaluationCommit
from tailorcv.application.discovery.pending import retire_pending
from tailorcv.application.discovery.posting_text import publisher_closing_date
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import ConflictError, ValidationError
from tailorcv.application.job_search.evaluation_components import (
    _evaluation_components,
    _EvaluationComponentContext,
)
from tailorcv.application.job_search.evaluation_fit_report import (
    _fit_report_artifact,
    _fit_report_payload,
)
from tailorcv.application.job_search.evaluation_requirements import (
    merge_hard_gates_into_requirements,
)
from tailorcv.application.job_search.evaluation_sources import (
    _candidate_record,
    _posting_artifacts,
)
from tailorcv.application.job_search.evaluation_targets import (
    _identifiers,
    _named_source,
    _resolve_target,
    _transfer_pending_closing_date,
)
from tailorcv.application.job_search.models import JobDetailResponse
from tailorcv.application.job_search.posting_bodies import (
    posting_body_text,
    posting_extraction_method,
    posting_source_bytes,
)
from tailorcv.application.job_search.postings import RequirementIdentityService
from tailorcv.application.job_search.read_models import _job_detail
from tailorcv.application.job_search.score_policies import score_policy
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.domain import new_id
from tailorcv.domain.staged_facts import PublisherClosingDate
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    JobEvaluation,
    PostingObservation,
)

if TYPE_CHECKING:
    from tailorcv.infrastructure.persistence.database import Database

# A supersession race reaches the driver as one of these, and only these mean
# the current evaluation moved underneath this commit.
SUPERSESSION_CONSTRAINTS = (
    "uq_job_evaluation_version",
    "uq_evaluation_supersedes",
    "job_evaluations.job_posting_id, job_evaluations.version_number",
    "job_evaluations.supersedes_evaluation_id",
)


def commit_evaluation(
    database: Database,
    artifacts: EvidenceArtifactService,
    proposal: EvaluationCommit,
    *,
    actor: str = "local_user",
    origin: str = "api",
    correlation_id: str | None = None,
    pending_discovery_id: str | None = None,
    job_posting_id: str | None = None,
) -> JobDetailResponse:
    """Append an evaluation and translate a concurrent supersession race."""
    try:
        return JobDetailResponse.model_validate(
            _commit_evaluation(
                database,
                artifacts,
                proposal,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                pending_discovery_id=pending_discovery_id,
                job_posting_id=job_posting_id,
            )
        )
    except IntegrityError as exc:
        message = str(exc.orig).casefold()
        if proposal.supersedes_evaluation_id and any(
            marker in message for marker in SUPERSESSION_CONSTRAINTS
        ):
            raise ConflictError(
                "The current evaluation changed during this commit. "
                "Refresh the job, compare the new current version, and retry."
            ) from exc
        raise


def _commit_evaluation(
    database: Database,
    artifacts: EvidenceArtifactService,
    proposal: EvaluationCommit,
    *,
    actor: str,
    origin: str,
    correlation_id: str | None,
    pending_discovery_id: str | None,
    job_posting_id: str | None,
) -> dict[str, Any]:
    """Append one complete evaluation version as an immutable transaction."""
    digest = proposal.digest()
    audit = audit_columns(actor, origin, correlation_id)
    with database.session() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        target = _resolve_target(
            session,
            proposal,
            digest=digest,
            pending_discovery_id=pending_discovery_id,
            job_posting_id=job_posting_id,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        job, pending, current_evaluation = target.job, target.pending, target.current
        if pending is not None:
            _transfer_pending_closing_date(session, job, pending)
        if target.replay:
            if pending is not None:
                retire_pending(session, pending)
            return _job_detail(database, session, job.id)

        _identifiers(
            session,
            job,
            proposal,
            source=_named_source(session, proposal),
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        observation_id = new_id()
        evaluation_id = new_id()
        body_artifact = _posting_artifacts(
            artifacts,
            session,
            proposal,
            observation_id=observation_id,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        master_cv_version, candidate_artifact = _candidate_record(
            artifacts,
            session,
            proposal,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
        weight_set, weights = score_policy(session, proposal)
        evidence_ids = {item.key: new_id() for item in proposal.score_evidence}
        merged = merge_hard_gates_into_requirements(
            hard_gates=proposal.fit_report.hard_gates,
            requirements=proposal.fit_report.requirements,
        )
        stated_requirements = merged.rows
        requirement_keys = RequirementIdentityService.identify(
            session,
            previous_evaluation_id=(
                current_evaluation.id if current_evaluation is not None else None
            ),
            current=stated_requirements,
        )
        cited_requirement_identities = {
            stated_id: (merged_id, requirement_keys[merged_id])
            for stated_id, merged_id in merged.carried_by.items()
            if merged_id in requirement_keys
        }
        fit_report_artifact = _fit_report_artifact(
            artifacts,
            session,
            proposal,
            job_id=job.id,
            evaluation_id=evaluation_id,
            evidence_ids=evidence_ids,
            weights=weights,
            weight_set=weight_set,
            master_cv_version=master_cv_version,
            audit=audit,
        )
        closing_date = (
            PublisherClosingDate.from_json(
                proposal.posting_body.publisher_closing_date.model_dump(mode="json")
            )
            if proposal.posting_body.publisher_closing_date is not None
            else publisher_closing_date(
                posting_body_text(
                    proposal.posting_body, posting_source_bytes(proposal.posting_body)
                ),
                source_url=proposal.canonical_url or proposal.discovery_url,
                observed_at=proposal.posting_body.fetched_at or proposal.observed_at,
            )
        )
        bound_date = closing_date.with_artifacts(
            body_artifact_id=body_artifact.id, publisher_artifact_id=None
        )
        closing_date = replace(
            bound_date,
            artifact_id=closing_date.artifact_id or bound_date.artifact_id,
            statements=tuple(
                original if original.artifact_id else bound
                for original, bound in zip(
                    closing_date.statements, bound_date.statements, strict=True
                )
            ),
        )
        observation = PostingObservation(
            id=observation_id,
            job_posting_id=job.id,
            observed_at=proposal.observed_at,
            posting_status=proposal.posting_status,
            location=proposal.location,
            residence_rule=proposal.residence_rule,
            employment_type=proposal.employment_type,
            payroll_arrangement=proposal.payroll_arrangement,
            body_artifact_id=body_artifact.id,
            extraction_method=posting_extraction_method(proposal.posting_body),
            fetched_at=proposal.posting_body.fetched_at,
            http_status=proposal.posting_body.http_status,
            robots_basis=proposal.posting_body.robots_basis,
            content_sha256=body_artifact.sha256,
            publisher_closing_date=closing_date.as_json(),
            **audit,
        )
        session.add(observation)
        session.flush()
        evaluation = JobEvaluation(
            id=evaluation_id,
            job_posting_id=job.id,
            version_number=(
                current_evaluation.version_number + 1 if current_evaluation is not None else 1
            ),
            supersedes_evaluation_id=(
                current_evaluation.id if current_evaluation is not None else None
            ),
            observation_id=observation.id,
            master_cv_version_id=master_cv_version.id,
            score_weight_set_id=weight_set.id,
            search_parameter_set_id=proposal.fit_report.search_parameter_set_id,
            fit_report_artifact_id=fit_report_artifact.id,
            fit_report_payload=_fit_report_payload(proposal, evidence_ids, requirement_keys),
            decision=proposal.decision.value,
            rationale=proposal.rationale,
            scope_summary=proposal.scope_summary,
            qualifications_summary=proposal.qualifications_summary,
            payload_sha256=digest,
            evaluated_at=proposal.evaluated_at,
            source_timestamp=proposal.source_timestamp,
            **audit,
        )
        session.add(evaluation)
        session.flush()
        RequirementIdentityService.persist(
            session,
            evaluation=evaluation,
            current=stated_requirements,
            keys=requirement_keys,
            audit=audit,
        )
        _evaluation_components(
            _EvaluationComponentContext(
                session=session,
                proposal=proposal,
                job=job,
                observation=observation,
                evaluation=evaluation,
                candidate_artifact=candidate_artifact,
                evidence_ids=evidence_ids,
                requirement_identities=cited_requirement_identities,
                weights=weights,
                audit=audit,
            )
        )
        session.add(
            AuditEvent(
                workspace_id=proposal.workspace_id,
                action=(
                    "evaluation.superseded"
                    if evaluation.supersedes_evaluation_id
                    else "evaluation.committed"
                ),
                entity_type="job_evaluation",
                entity_id=evaluation.id,
                details={
                    "job_posting_id": job.id,
                    "payload_sha256": digest,
                    "version_number": evaluation.version_number,
                    "supersedes_evaluation_id": evaluation.supersedes_evaluation_id,
                },
                **audit,
            )
        )
        session.flush()
        detail = _job_detail(database, session, job.id, allow_staged=True)
        missing = detail["completeness"]["missing"]
        if missing:
            raise ValidationError("Evaluation commitment is incomplete: " + ", ".join(missing))
        if pending is not None:
            retire_pending(session, pending)
        return detail
