"""The approved paid boundary that runs one fit case through production code.

Every model call is made by the production screening pipeline and the official
provider adapters; nothing here builds a request. Results are compared against
the human-adjudicated ledger label.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from evals.dataset import AbstainedScore, FitAnnotation, SemanticAnnotation
from evals.semantic import (
    DefectClass,
    HardFailureClass,
    SemanticCaseResult,
    StageMeasurement,
)
from tailorcv.application.config import Settings
from tailorcv.application.tailoring.workflows.evaluation_policy import (
    arrangement_cap,
    compensation_score,
    deterministic_scores,
    eligibility_score,
)
from tailorcv.application.tailoring.workflows.evaluation_policy import (
    decision as ledger_decision,
)
from tailorcv.application.tailoring.workflows.models import EvaluationOutput
from tailorcv.application.tailoring.workflows.runner import PipelineWorkflowRunner
from tailorcv.domain.records import EvaluationDecision
from tailorcv.shared.costs import ServiceTier

# A component this far from the adjudicated score counts as a disagreement.
SCORE_TOLERANCE = 25

# A dated posting status is never current truth. Every archived body is read as
# an open posting, and a closed-evidence label is not comparable on decision.
ASSUMED_POSTING_STATUS = "active"

ACTIONABLE = {EvaluationDecision.APPLY_NOW, EvaluationDecision.PREMIUM_TARGET}


@dataclass
class FitExecutor:
    """Run one annotated posting through the production screening pipeline."""

    artifacts: Path
    master_cv: Path
    settings: Settings
    api_key: str | None = None
    capture: bool = False
    # None runs on the provider's unattended tier, since nobody waits on a fit run.
    service_tier: ServiceTier | None = None
    runner: PipelineWorkflowRunner = field(default_factory=PipelineWorkflowRunner)
    # Told about each finished case, which is what makes a long paid run visible.
    observer: Callable[[SemanticCaseResult], None] | None = None

    def run(self, annotation: SemanticAnnotation, candidate: str) -> SemanticCaseResult:
        """Screen one posting and label how the result differs from its adjudication."""
        if not isinstance(annotation, FitAnnotation):
            raise TypeError(f"{annotation.annotation_id} is not a fit annotation")
        posting = self.artifacts / annotation.artifact_ref
        if not posting.is_file():
            raise FileNotFoundError(f"{annotation.annotation_id}: no artifact at {posting}")

        output = self.runner.evaluate(
            posting,
            self.settings,
            master_cv=self.master_cv,
            service_tier=self.service_tier or self.settings.unattended_tier(),
        )

        observed = _observed(output)
        defects, hard_failures = _compare(annotation, observed, output)
        result = SemanticCaseResult(
            annotation_id=annotation.annotation_id,
            candidate=cast(Literal["baseline", "candidate"], candidate),
            stages=tuple(StageMeasurement.from_manifest(entry) for entry in output.ledger.entries),
            defects=defects,
            hard_failures=hard_failures,
            content=_content(annotation, output, observed) if self.capture else {},
        )
        if self.observer is not None:
            self.observer(result)
        return result


@dataclass(frozen=True)
class _Observed:
    """What this run concluded, in the same shape the ledger label carries."""

    role_fit: int
    evidence: int
    opportunity: int
    compensation: int | None
    eligibility: int | None
    decision: EvaluationDecision
    requirements: int


def _observed(output: EvaluationOutput) -> _Observed:
    """Apply the production scoring policy to one screening result."""
    reading = output.reading
    place_fit_rule, _, place_status = (reading.place_fit.value or "").partition(": ")
    eligibility = eligibility_score(place_status, reading.place_fit.note)
    compensation = compensation_score(
        reading.pay_fit.value, reading.pay_fit.note, reading.pay_standing
    )
    scores = deterministic_scores(output.spec.requirements, output.evidence, output.outcome)
    chosen, _ = ledger_decision(
        output.outcome,
        ASSUMED_POSTING_STATUS,
        place_status,
        place_fit_rule,
        reading.pay_target_met,
    )
    return _Observed(
        role_fit=scores.fit,
        evidence=scores.evidence,
        opportunity=scores.opportunity,
        compensation=compensation.value,
        eligibility=eligibility.value,
        decision=arrangement_cap(chosen, reading.location)[0],
        requirements=len(output.spec.requirements),
    )


def _compare(
    annotation: FitAnnotation, observed: _Observed, output: EvaluationOutput
) -> tuple[tuple[DefectClass, ...], tuple[HardFailureClass, ...]]:
    """Label how this run differs from the adjudicated record."""
    defects: list[DefectClass] = []
    hard: list[HardFailureClass] = []

    if not observed.requirements:
        defects.append(DefectClass.SCAN_FAILURE)

    for name in ("role_fit", "evidence", "opportunity", "compensation", "eligibility"):
        label = getattr(annotation.scores, name)
        run = getattr(observed, name)
        if isinstance(label, AbstainedScore):
            # The adjudication left this open. The run scores only from what the
            # posting states, and establishing it is not a defect.
            continue
        if run is None:
            defects.append(DefectClass.MISSED_EVIDENCE)
        elif run - label > SCORE_TOLERANCE:
            defects.append(DefectClass.BAD_EQUIVALENCE)
        elif label - run > SCORE_TOLERANCE:
            defects.append(DefectClass.MISSED_EVIDENCE)

    if annotation.hard_gate.value == "blocked" and observed.decision in ACTIONABLE:
        hard.append(HardFailureClass.HARD_GATE_BYPASSED)

    entry_ids = {entry.id for entry in output.inventory.entries}
    cited = {identifier for item in output.evidence.matches for identifier in item.entry_ids}
    if cited - entry_ids:
        hard.append(HardFailureClass.INVALID_CITATION)

    return tuple(defects), tuple(hard)


def _content(
    annotation: FitAnnotation, output: EvaluationOutput, observed: _Observed
) -> dict[str, str]:
    """Return the private record a blind review needs to read this case.

    The rendered prompts are not reproduced. They are a deterministic function
    of the retained posting artifact, the master CV snapshot, and the prompt
    suite fingerprint recorded here.
    """
    return {
        "artifact_ref": annotation.artifact_ref,
        "sanitized_content_sha256": annotation.sanitized_content_sha256,
        "prompt_fingerprint": output.prompt_fingerprint,
        "jobspec": output.spec.model_dump_json(indent=2),
        "evidence": output.evidence.model_dump_json(indent=2),
        "outcome": output.outcome,
        "observed": json.dumps(
            {
                "role_fit": observed.role_fit,
                "evidence": observed.evidence,
                "opportunity": observed.opportunity,
                "compensation": observed.compensation,
                "eligibility": observed.eligibility,
                "decision": observed.decision.value,
                "requirements": observed.requirements,
            },
            indent=2,
        ),
        "adjudicated": annotation.model_dump_json(indent=2),
    }


__all__ = ["ASSUMED_POSTING_STATUS", "SCORE_TOLERANCE", "FitExecutor"]
