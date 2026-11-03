"""Pure fit-report projection from completed evaluation findings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from clearscout.application.contracts import (
    RULE_REFUSAL_PROMPT_FINGERPRINT,
    FitReportInput,
    ReportCheckInput,
    ReportCostInput,
    ReportDisagreementInput,
    ReportReadingInput,
    ReportRequirementInput,
)
from clearscout.application.tailoring.fit import Answer, Disagreement
from clearscout.application.tailoring.stages.jobspec import HardGate
from clearscout.application.tailoring.stages.match import (
    Classification,
    GateAnswer,
    answered_gates,
    example_list_notes,
)
from clearscout.domain.search_parameters import OUTSIDE_DECLARED

from .evaluation_policy import gap_requirements, unread_residence
from .models import EvaluationOutput

_RULED_OUT = frozenset({OUTSIDE_DECLARED, "ineligible"})
_NO_DERIVATIVE = "No derivative application document was generated during evaluation."
_EXAMPLE_CHECK = "example-list notes"
_NONE_FLAGGED = (
    "No match note holds a listed example, or the employer's own name or offering, "
    "against the record."
)


def _posting_requirement(evidence_key: str) -> ReportRequirementInput:
    """Stand in for the requirements of a run that extracted none."""
    return ReportRequirementInput(
        requirement_id="posting",
        importance="responsibility",
        text="Review the stored posting body.",
        evidence_keys=[evidence_key],
    )


def _example_checks(output: EvaluationOutput) -> list[ReportCheckInput]:
    """Return an advisory row per note holding an unrequired item, or one row saying none did."""
    advisories = example_list_notes(output.spec, output.evidence)
    if not advisories:
        return [ReportCheckInput(check=_EXAMPLE_CHECK, result="none flagged", detail=_NONE_FLAGGED)]
    return [
        ReportCheckInput(check=_EXAMPLE_CHECK, result="advisory", detail=note)
        for note in advisories
    ]


def _unpaired(answer: GateAnswer) -> bool:
    """Return whether the record should answer this gate and the run paired nothing to it."""
    return answer.answered_by_record and answer.classification is None and not answer.partial


def _unmet(gate: HardGate, answer: GateAnswer, residence: Answer[str], place_status: str) -> bool:
    """Return whether the record, or for a location gate the place verdict, fails this gate."""
    if gate.kind == "location":
        return place_status in _RULED_OUT and not unread_residence(gate, residence)
    return answer.answered_by_record and answer.classification is Classification.GAP


def project_fit_report(
    output: EvaluationOutput,
    *,
    candidate_text: str,
    requirement_keys: Mapping[str, list[str]],
    hard_gate_evidence_keys: Sequence[str],
    reading_keys: Mapping[str, list[str]],
    disagreements: Sequence[tuple[Disagreement, list[str]]],
    fallback_evidence_key: str,
    search_parameter_set_id: str | None = None,
) -> FitReportInput:
    """Project one completed screen into its immutable report input.

    ``disagreements`` pairs each model-and-rules disagreement with the evidence keys it cites.
    """
    unresolved = {match.requirement_id for match in output.evidence.needs_review()}
    # The gap set the stored `gap` factors carry. Intersecting with `established` keeps an
    # unresolved requirement out: it reads as not established, not as unmet.
    gaps = {
        requirement.id
        for requirement in gap_requirements(output.spec.requirements, output.evidence)
    }
    requirements = [
        ReportRequirementInput(
            requirement_id=requirement.id,
            importance=requirement.importance.value,
            text=requirement.text,
            evidence_keys=requirement_keys.get(requirement.id, []),
            span_status=requirement.source_span.status,
            established=requirement.id not in unresolved,
            unmet=requirement.id in gaps and requirement.id not in unresolved,
            dimension=requirement.dimension.value,
        )
        for requirement in output.spec.requirements
    ]
    if not requirements:
        requirements.append(_posting_requirement(fallback_evidence_key))

    residence = output.reading.residence_requirement
    place_status = (output.reading.place_fit.value or "").partition(": ")[2]
    hard_gates = [
        ReportRequirementInput(
            requirement_id=f"gate-{position}",
            importance="must_have" if gate.verified_blocking else "nice_to_have",
            text=gate.text,
            evidence_keys=[hard_gate_evidence_keys[position - 1]],
            span_status=gate.source_span.status,
            is_hard_gate=True,
            gate_kind=gate.kind,
            gate_quote=gate.quote_or_span,
            gate_blocking=gate.verified_blocking,
            established=not _unpaired(answer) and not unread_residence(gate, residence),
            unmet=_unmet(gate, answer, residence, place_status),
        )
        for position, (gate, answer) in enumerate(
            zip(output.spec.hard_gates, answered_gates(output.spec, output.evidence), strict=True),
            1,
        )
    ]
    reading = [
        ReportReadingInput(
            question=answer.question,
            answer=None if answer.value is None else str(answer.value),
            evidence_keys=reading_keys[name],
            note=answer.note,
            conflict=answer.conflict,
        )
        for name, answer in output.reading.named()
    ]
    found = [
        ReportDisagreementInput(
            subject=item.subject,
            model_says=item.model_says,
            rules_say=item.rules_say,
            evidence_keys=keys,
        )
        for item, keys in disagreements
    ]
    candidate_folded = candidate_text.casefold()
    coverage_present = sum(
        1 for keyword in output.spec.keywords if keyword.casefold() in candidate_folded
    )

    return FitReportInput(
        prompt_fingerprint=output.prompt_fingerprint,
        search_parameter_set_id=search_parameter_set_id,
        code_revision=output.code_revision,
        hard_gates=hard_gates,
        reading=reading,
        disagreements=found,
        requirements=requirements,
        coverage_present=coverage_present,
        coverage_total=len(output.spec.keywords),
        unrecognized_requirements=list(output.evidence.unrecognized),
        checks=[
            ReportCheckInput(
                check="candidate evidence anchors",
                result="pass",
                detail="Every stated score is linked to stored source evidence.",
            ),
            *_example_checks(output),
        ],
        length_budget=_NO_DERIVATIVE,
        costs=[
            ReportCostInput(
                stage=item.stage,
                model=item.served or item.model,
                input_tokens=item.usage.input_tokens,
                output_tokens=item.usage.output_tokens,
                amount=None if item.dollars is None else Decimal(str(item.dollars)),
            )
            for item in output.ledger.entries
        ],
    )


def project_refusal_report(
    condition: str,
    reason: str,
    *,
    evidence_key: str,
    search_parameter_set_id: str | None,
    code_revision: str | None,
) -> FitReportInput:
    """Project a rule refusal into a report that names the gate and measures nothing else."""
    return FitReportInput(
        prompt_fingerprint=RULE_REFUSAL_PROMPT_FINGERPRINT,
        search_parameter_set_id=search_parameter_set_id,
        code_revision=code_revision,
        reading=[
            ReportReadingInput(
                question="Which rule refused this posting before any fit analysis?",
                answer=condition,
                note=reason,
            )
        ],
        requirements=[_posting_requirement(evidence_key)],
        coverage_present=0,
        coverage_total=0,
        checks=[ReportCheckInput(check="rule gate", result="refused", detail=reason)],
        length_budget=_NO_DERIVATIVE,
    )
