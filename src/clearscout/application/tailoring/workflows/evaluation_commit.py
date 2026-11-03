"""Pure translation of screening findings into an evaluation commit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from clearscout.application.contracts import (
    CandidateRecordInput,
    EligibilityInput,
    EvaluationCommit,
    EvaluationFactorInput,
    EvaluationScores,
    PostingBodyInput,
    PublisherClosingDateResponse,
    ScoreComponentInput,
)
from clearscout.application.tailoring.fit import disagreements
from clearscout.application.tailoring.fit.structure import Reading
from clearscout.application.tailoring.workflows.evaluation_evidence import (
    FALLBACK_CHARACTERS,
    EvidenceAnchors,
    disagreement_evidence,
    posting_body_anchor,
    reading_evidence,
    requirement_evidence,
)
from clearscout.application.tailoring.workflows.evaluation_policy import (
    arrangement_cap,
    compensation_score,
    deterministic_scores,
    eligibility_score,
    gap_requirements,
    unread_residence_gate,
)
from clearscout.application.tailoring.workflows.evaluation_policy import (
    decision as evaluation_decision,
)
from clearscout.application.tailoring.workflows.evaluation_policy import (
    factors as evaluation_factors,
)
from clearscout.application.tailoring.workflows.evaluation_report import (
    project_fit_report,
    project_refusal_report,
)
from clearscout.application.tailoring.workflows.models import EvaluationOutput
from clearscout.domain.records import (
    EvaluationDecision,
    EvidenceDirection,
    PostingStatus,
    RobotsReadBasis,
    ScoreComponent,
)
from clearscout.domain.staged_facts import PublisherClosingDate


@dataclass(frozen=True)
class EvaluationCommitInputs:
    """Stored facts used to translate one screening result."""

    workspace_id: str
    supersedes_evaluation_id: str | None
    employer: str
    title: str
    location: str
    requisition_id: str
    canonical_url: str
    discovery_url: str
    first_seen_at: datetime
    observed_at: datetime
    posting_status: str
    residence_rule: str
    employment_type: str
    payroll_arrangement: str
    source_timestamp: str | None
    extraction_method: str
    fetched_at: datetime | None
    http_status: int | None
    robots_basis: RobotsReadBasis | None
    master_content: bytes
    master_source_uri: str
    master_effective_at: datetime
    search_parameter_set_id: str | None = None
    publisher_closing_date: PublisherClosingDate | None = None


def _eligibility_details(reading: Reading) -> str:
    """Return what the place and the arrangement answers were each read from."""
    labelled = (("Place", reading.place_fit), ("Arrangement", reading.arrangement_fit))
    return ". ".join(
        f"{label}: {answer.because()}" for label, answer in labelled if answer.because()
    )


def _joined_rationale(reasons: Sequence[str]) -> str:
    """Return the model's reasons in order, separated so one cannot run into the next."""
    joined = ""
    for reason in reasons:
        text = reason.strip()
        if not text:
            continue
        if joined:
            joined += " " if joined[-1] in ".!?;:" else ". "
        joined += text
    return joined


def build_evaluation_commit(
    inputs: EvaluationCommitInputs, output: EvaluationOutput, *, credential_gate: str = ""
) -> EvaluationCommit:
    """Translate existing screen findings into the immutable evaluation contract.

    ``credential_gate`` is the posting's statement of a credential the person declared they lack.
    """
    candidate_text = inputs.master_content.decode("utf-8")
    anchors = EvidenceAnchors(output.jd.text, candidate_text)
    evidence = anchors.items
    requirement_keys = requirement_evidence(
        anchors, output.spec.requirements, output.evidence, output.inventory
    )
    posting_body_anchor(anchors)
    reading_keys = reading_evidence(anchors, output.reading)

    place = output.reading.place_fit
    place_fit_rule = ""
    place_status = ""
    if place.value:
        place_fit_rule, _, place_status = place.value.partition(": ")
    eligibility = eligibility_score(place_status, place.note)
    if eligibility.value is not None and not reading_keys["place_fit"]:
        reading_keys["place_fit"] = [
            anchors.posting(
                "reading-place-fit",
                place.value or place_fit_rule,
                components=[ScoreComponent.ELIGIBILITY],
            )
        ]

    compensation = compensation_score(
        output.reading.pay_fit.value,
        output.reading.pay_fit.note,
        output.reading.pay_standing,
    )
    if compensation.value is not None and not reading_keys["pay_fit"]:
        reading_keys["pay_fit"] = [
            anchors.posting(
                "reading-pay-fit",
                output.reading.pay_fit.value or "pay finding",
                components=[ScoreComponent.COMPENSATION],
            )
        ]

    scores = deterministic_scores(output.spec.requirements, output.evidence, output.outcome)
    chosen, decision_reason = evaluation_decision(
        output.outcome,
        inputs.posting_status,
        place_status,
        place_fit_rule,
        output.reading.pay_target_met,
        credential_gate=credential_gate,
        place_reading=place.because(),
        residence_gate=unread_residence_gate(
            output.spec.hard_gates, output.reading.residence_requirement
        ),
        pay_above_stretch=output.reading.pay_above_stretch,
    )
    decision, cap_reason = arrangement_cap(chosen, output.reading.location)

    # A gate quote states no coverage verdict, so it cites no requirement and the
    # requirement's own rows alone carry its verdict.
    hard_gate_evidence_keys = [
        anchors.posting(f"hard-gate-{position}", gate.text)
        for position, gate in enumerate(output.spec.hard_gates, 1)
    ]
    fit_report = project_fit_report(
        output,
        candidate_text=candidate_text,
        requirement_keys=requirement_keys,
        hard_gate_evidence_keys=hard_gate_evidence_keys,
        reading_keys=reading_keys,
        disagreements=disagreement_evidence(
            anchors,
            disagreements(output.reading, output.spec),
            reading_keys,
            hard_gate_evidence_keys,
            evidence[0].key,
        ),
        fallback_evidence_key=evidence[0].key,
        search_parameter_set_id=inputs.search_parameter_set_id,
    )
    gaps = gap_requirements(output.spec.requirements, output.evidence)
    pros, cons = evaluation_factors(
        output.spec.requirements, output.evidence, requirement_keys, evidence[0].key
    )
    return EvaluationCommit(
        **_observed_posting(inputs, output.jd.text),
        decision=decision,
        rationale=(
            _joined_rationale((decision_reason, cap_reason, *output.reasons))
            or f"Evaluation outcome: {output.outcome}."
        ),
        scope_summary=(
            "Extracted focus: " + ", ".join(output.spec.keywords)
            if output.spec.keywords
            else "The posting states no stable keyword summary."
        ),
        qualifications_summary=(
            f"{scores.supported} of {len(output.spec.requirements)} extracted requirements "
            "have candidate-record support."
        ),
        scores=EvaluationScores(
            fit=ScoreComponentInput(value=scores.fit),
            compensation=compensation,
            eligibility=eligibility,
            evidence=ScoreComponentInput(value=scores.evidence),
            opportunity=ScoreComponentInput(value=scores.opportunity),
        ),
        pros=pros,
        cons=cons,
        gaps=cons[: len(gaps)],
        risks=[],
        compensation=list(output.reading.pay_extractions),
        eligibility=EligibilityInput(
            workspace_location_key=place_fit_rule or "not stated",
            arrangement=output.reading.location.render(),
            details=_eligibility_details(output.reading),
        ),
        score_evidence=evidence,
        fit_report=fit_report,
    )


def build_refusal_commit(
    inputs: EvaluationCommitInputs,
    *,
    posting_text: str,
    condition: str,
    reason: str,
    eligibility: EligibilityInput | None,
    code_revision: str | None,
) -> EvaluationCommit:
    """Record a rule refusal as an evaluation that states its gate and scores nothing.

    ``eligibility`` is what an earlier read of this exact body established, if any.
    """
    anchors = EvidenceAnchors(posting_text, inputs.master_content.decode("utf-8"))
    posting_key = anchors.posting("posting-body", posting_text[:FALLBACK_CHARACTERS])
    abstention = ScoreComponentInput(
        value=None, reason=f"The {condition} rule refused this posting before any fit analysis."
    )
    closed = inputs.posting_status != PostingStatus.ACTIVE
    return EvaluationCommit(
        **_observed_posting(inputs, posting_text),
        decision=EvaluationDecision.CLOSED_EVIDENCE if closed else EvaluationDecision.INELIGIBLE,
        rationale=f"Refused by the {condition} rule: {reason}.",
        scores=EvaluationScores(
            fit=abstention,
            compensation=abstention,
            eligibility=abstention,
            evidence=abstention,
            opportunity=abstention,
        ),
        pros=[
            EvaluationFactorInput(
                text="The stored posting body is kept with this rule decision.",
                evidence_keys=[posting_key],
            )
        ],
        # A rule gate locates no quote, so its reason cites no span of the body.
        cons=[EvaluationFactorInput(text=reason, direction=EvidenceDirection.LIMITS)],
        eligibility=eligibility or EligibilityInput(workspace_location_key="not stated"),
        score_evidence=anchors.items,
        fit_report=project_refusal_report(
            condition,
            reason,
            evidence_key=posting_key,
            search_parameter_set_id=inputs.search_parameter_set_id,
            code_revision=code_revision,
        ),
    )


class _ObservedPosting(TypedDict):
    """The evaluation-commit fields that come from the posting and CV, not the verdict."""

    workspace_id: str
    supersedes_evaluation_id: str | None
    employer: str
    title: str
    location: str
    requisition_id: str
    canonical_url: str
    discovery_url: str
    first_seen_at: datetime
    observed_at: datetime
    posting_status: PostingStatus
    residence_rule: str
    employment_type: str
    payroll_arrangement: str
    evaluated_at: datetime
    source_timestamp: str | None
    posting_body: PostingBodyInput
    candidate_record: CandidateRecordInput


def _observed_posting(inputs: EvaluationCommitInputs, posting_text: str) -> _ObservedPosting:
    """Return the posting identity, observation, and source bodies every commit carries."""
    return {
        "workspace_id": inputs.workspace_id,
        "supersedes_evaluation_id": inputs.supersedes_evaluation_id,
        # The ledger's name for a posting is the program's copy; the model's reading never names it.
        "employer": inputs.employer,
        "title": inputs.title,
        "location": inputs.location,
        "requisition_id": inputs.requisition_id,
        "canonical_url": inputs.canonical_url,
        "discovery_url": inputs.discovery_url,
        "first_seen_at": inputs.first_seen_at,
        "observed_at": inputs.observed_at,
        "posting_status": PostingStatus(inputs.posting_status),
        "residence_rule": inputs.residence_rule,
        "employment_type": inputs.employment_type,
        "payroll_arrangement": inputs.payroll_arrangement,
        "evaluated_at": inputs.observed_at,
        "source_timestamp": inputs.source_timestamp,
        "posting_body": PostingBodyInput(
            logical_type="posting.body.text",
            content=posting_text,
            extraction_method=inputs.extraction_method,
            fetched_at=inputs.fetched_at,
            http_status=inputs.http_status,
            robots_basis=inputs.robots_basis,
            publisher_closing_date=(
                PublisherClosingDateResponse.model_validate(inputs.publisher_closing_date.as_json())
                if inputs.publisher_closing_date is not None
                else None
            ),
        ),
        "candidate_record": CandidateRecordInput(
            content=inputs.master_content.decode("utf-8"),
            source_uri=inputs.master_source_uri,
            effective_at=inputs.master_effective_at,
        ),
    }
