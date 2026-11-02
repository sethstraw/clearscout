"""Append the component records that make up one committed evaluation.

Scores, cited evidence, factors, compensation, and eligibility all hang off an
evaluation that already exists, so every function here writes inside the
transaction its caller opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.contracts import (
    EvaluationCommit,
    ScoreEvidenceInput,
)
from tailorcv.application.failures import ValidationError
from tailorcv.application.job_search._support import _assert_score_traceability
from tailorcv.application.job_search.posting_bodies import (
    posting_body_text,
    posting_source_bytes,
)
from tailorcv.domain.search_parameters.compensation import ANNUAL_EQUIVALENT, CompensationBasis
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    CompensationComponent,
    EligibilityRecord,
    EvaluationFactor,
    EvaluationFactorEvidence,
    EvaluationScore,
    EvaluationScoreEvidence,
    JobEvaluation,
    JobPosting,
    MasterCvEntry,
    MatchEvidence,
    PostingObservation,
)


@dataclass(frozen=True)
class _EvaluationComponentContext:
    """Shared transaction state for one evaluation's component records."""

    session: Session
    proposal: EvaluationCommit
    job: JobPosting
    observation: PostingObservation
    evaluation: JobEvaluation
    candidate_artifact: Artifact
    evidence_ids: dict[str, str]
    requirement_identities: dict[str, tuple[str, str]]
    weights: dict[str, Decimal]
    audit: dict[str, Any]


def _evaluation_components(context: _EvaluationComponentContext) -> None:
    """Append component families for one evaluation in the caller's transaction."""
    score_rows = _persist_evaluation_scores(context)
    evidence_rows = _persist_match_evidence(context)
    score_by_component = {row.component: row for row in score_rows}
    score_links = [
        EvaluationScoreEvidence(
            evaluation_score_id=score_by_component[component].id,
            match_evidence_id=evidence.id,
            direction=item.direction,
            **context.audit,
        )
        for evidence, item in evidence_rows
        for component in set(item.components)
    ]
    _assert_score_traceability(score_rows, score_links)
    context.session.add_all(score_links)
    _persist_factors(context, evidence_rows)
    _persist_compensation_and_eligibility(context)


def _persist_evaluation_scores(context: _EvaluationComponentContext) -> list[EvaluationScore]:
    """Append score rows and assign their durable identifiers."""
    findings = context.proposal.scores.named()
    score_rows = [
        EvaluationScore(
            evaluation_id=context.evaluation.id,
            component=component,
            score=finding.value,
            weight=context.weights[component],
            finding_state=finding.finding_state,
            no_evidence_reason=finding.reason or None,
            standing_source=finding.standing_source,
            **context.audit,
        )
        for component, finding in findings.items()
    ]
    context.session.add_all(score_rows)
    context.session.flush()
    return score_rows


def _entry_keys(session: Session, master_cv_version_id: str | None) -> dict[str, str]:
    """Return each entry id's durable identity in the version an evaluation read."""
    if master_cv_version_id is None:
        return {}
    return {
        row.entry_id: row.entry_key
        for row in session.scalars(
            select(MasterCvEntry).where(MasterCvEntry.master_cv_version_id == master_cv_version_id)
        )
    }


def _cited_requirement_identity(
    requirement_identities: dict[str, tuple[str, str]], requirement_id: str | None
) -> tuple[str | None, str | None]:
    """Return the canonical identity of the requirement this evidence cites.

    Evidence anchored to the whole evaluation cites no requirement and stores
    neither identity.
    """
    if requirement_id is None:
        return None, None
    identity = requirement_identities.get(requirement_id)
    if identity is None:
        raise ValidationError(
            f"Evidence cites requirement {requirement_id!r}, which this evaluation did not "
            "state. The evaluation cannot be stored without a durable identity for it."
        )
    return identity


def _persist_match_evidence(
    context: _EvaluationComponentContext,
) -> list[tuple[MatchEvidence, ScoreEvidenceInput]]:
    """Validate selected source text and append its cited evidence rows."""
    body_source = posting_source_bytes(context.proposal.posting_body)
    body_text = posting_body_text(context.proposal.posting_body, body_source)
    entry_keys = _entry_keys(context.session, context.evaluation.master_cv_version_id)
    evidence_rows: list[tuple[MatchEvidence, ScoreEvidenceInput]] = []
    for item in context.proposal.score_evidence:
        requirement_id, requirement_key = _cited_requirement_identity(
            context.requirement_identities, item.requirement_id
        )
        source_text = (
            body_text
            if item.source_kind == "posting"
            else context.proposal.candidate_record.content
        )
        artifact_id = (
            context.observation.body_artifact_id
            if item.source_kind == "posting"
            else context.candidate_artifact.id
        )
        if item.char_start is not None and item.char_end is not None:
            exact = (
                item.char_end <= len(source_text)
                and source_text[item.char_start : item.char_end] == item.text
            )
        else:
            exact = item.text in source_text
        if not exact:
            raise ValidationError(
                "Evidence text must be verbatim at its stored source span or locator."
            )
        evidence = MatchEvidence(
            id=context.evidence_ids[item.key],
            evaluation_id=context.evaluation.id,
            artifact_id=artifact_id,
            master_cv_entry_id=item.master_cv_entry_id,
            master_cv_entry_key=entry_keys.get(item.master_cv_entry_id or ""),
            requirement_id=requirement_id,
            requirement_key=requirement_key,
            evidence_text=item.text,
            char_start=item.char_start,
            char_end=item.char_end,
            locator=item.locator,
            source_kind=item.source_kind,
            strength=item.strength,
            classification=item.classification,
            **context.audit,
        )
        context.session.add(evidence)
        evidence_rows.append((evidence, item))
    context.session.flush()
    return evidence_rows


def _persist_factors(
    context: _EvaluationComponentContext,
    evidence_rows: list[tuple[MatchEvidence, ScoreEvidenceInput]],
) -> None:
    """Append factor rows and their evidence links."""
    evidence_by_key = {item.key: evidence for evidence, item in evidence_rows}
    factor_rows: list[tuple[EvaluationFactor, Any]] = []
    for factor_type, values in (
        ("pro", context.proposal.pros),
        ("con", context.proposal.cons),
        ("gap", context.proposal.gaps),
        ("risk", context.proposal.risks),
    ):
        for position, factor in enumerate(values):
            row = EvaluationFactor(
                evaluation_id=context.evaluation.id,
                factor_type=factor_type,
                position=position,
                text=factor.text,
                **context.audit,
            )
            context.session.add(row)
            factor_rows.append((row, factor))
    context.session.flush()
    context.session.add_all(
        EvaluationFactorEvidence(
            evaluation_factor_id=row.id,
            match_evidence_id=evidence_by_key[key].id,
            direction=item.direction,
            **context.audit,
        )
        for row, item in factor_rows
        for key in set(item.evidence_keys)
    )


def _annualized(amount: Decimal | None, basis: CompensationBasis) -> Decimal | None:
    return amount * ANNUAL_EQUIVALENT[basis] if amount is not None else None


def _persist_compensation_and_eligibility(context: _EvaluationComponentContext) -> None:
    """Append current-observation compensation and eligibility records."""
    context.session.add_all(
        CompensationComponent(
            job_posting_id=context.job.id,
            observation_id=context.observation.id,
            component_type=item.component_type,
            amount_min=item.amount_min,
            amount_max=item.amount_max,
            annualized_min=_annualized(item.amount_min, item.basis),
            annualized_max=_annualized(item.amount_max, item.basis),
            currency=item.currency,
            basis=item.basis,
            geography=item.geography,
            description=item.description,
            **context.audit,
        )
        for item in context.proposal.compensation
    )
    context.session.add(
        EligibilityRecord(
            job_posting_id=context.job.id,
            observation_id=context.observation.id,
            workspace_location_key=context.proposal.eligibility.workspace_location_key,
            region_rule=context.proposal.eligibility.region_rule,
            arrangement=context.proposal.eligibility.arrangement,
            details=context.proposal.eligibility.details,
            **context.audit,
        )
    )
