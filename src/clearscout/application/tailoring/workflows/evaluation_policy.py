"""The deterministic part of an evaluation: scores, decision, and factors.

Nothing here reads a database, a provider, or a file. It takes the findings a run
already produced and returns the numbers and sentences the commit stores, so the
rules can be exercised directly rather than through a committed transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from clearscout.application.contracts import EvaluationFactorInput, ScoreComponentInput
from clearscout.application.tailoring.fit.structure import Answer
from clearscout.application.tailoring.stages.jobspec import HardGate, Requirement
from clearscout.application.tailoring.stages.match import Classification, EvidenceMap, Strength
from clearscout.domain.records import EvaluationDecision, EvidenceDirection
from clearscout.domain.search_parameters import OUTSIDE_DECLARED

# Asserted and gap both mean the candidate record did not support the
# requirement, so neither counts towards evidence. Only gap is reported back as
# one, because an asserted claim is a different thing to go and check.
UNSUPPORTED = frozenset({Classification.GAP, Classification.ASSERTED})

_OPPORTUNITY = {"pursue": 90, "cautious pursue": 60, "skip": 20}

# An answered requirement this weak is listed against the posting, not for it.
_PARTIAL = frozenset({Strength.WEAK, Strength.NONE})

# Below sponsorship, which an employer can solve, and above ineligible, which
# nobody can. A place the reader did not declare ranks a posting down.
OUTSIDE_DECLARED_SCORE = 20


@dataclass(frozen=True)
class DeterministicScores:
    """The three scores derived from requirements and matches, and their count."""

    fit: int
    evidence: int
    opportunity: int
    supported: int


def eligibility_score(status: str, reason: str) -> ScoreComponentInput:
    """Score the residence finding, abstaining when the posting does not state one."""
    if status == "authorized":
        return ScoreComponentInput(value=100)
    if status == "sponsorship required":
        return ScoreComponentInput(value=60)
    if status == "ineligible":
        return ScoreComponentInput(value=0)
    if status == OUTSIDE_DECLARED:
        return ScoreComponentInput(value=OUTSIDE_DECLARED_SCORE)
    return ScoreComponentInput(value=None, reason=reason or "The posting location is not stated.")


def compensation_score(
    value: str | None, reason: str, standing: int | None = None
) -> ScoreComponentInput:
    """Score pay against the band, abstaining without a comparable figure.

    ``standing`` is where the quoted pay falls between the band's minimum,
    target, and stretch. Without it the score can only say whether the floor was
    cleared. A value starting with ``shown:`` is a range the extractor
    surfaced without a comparable band, so its standing source is extracted.
    """
    if value is not None:
        source: Literal["compared", "extracted"] = (
            "extracted" if value.startswith("shown:") else "compared"
        )
        if standing is not None:
            return ScoreComponentInput(value=standing, standing_source=source)
        return ScoreComponentInput(
            value=100 if value.startswith("at or above") else 0,
            standing_source=source,
        )
    return ScoreComponentInput(value=None, reason=reason or "The posting states no comparable pay.")


def decision(
    outcome: str,
    posting_status: str,
    eligibility: str,
    place_fit_rule: str,
    pay_target_met: bool = False,
    credential_gate: str = "",
    place_reading: str = "",
    residence_gate: str = "",
    pay_above_stretch: str = "",
) -> tuple[EvaluationDecision, str]:
    """Choose the ledger decision, letting a closed or ineligible posting win first.

    ``place_fit_rule`` is the market the place-fit reading placed the posting
    in, not the residence rule the posting states, and ``place_reading`` is what
    that reading was read from. A posting worth applying to whose pay floor meets
    the band's target is a premium target, unless ``pay_above_stretch`` names the
    stretch that floor is above. ``MONITOR`` is not reached
    here: it marks an employer worth watching rather than anything this
    evaluation measures. ``credential_gate`` is the posting's statement of a
    credential the person declared they lack, a hard exclusion the pre-screen
    may not have caught, since it can live in a model-read gate rather than a
    must-have line. ``residence_gate`` is a stated residence rule the place
    reading did not read, from :func:`unread_residence_gate`.

    Returns the decision and what moved it from the one the outcome alone
    gives, which is empty when nothing did.
    """
    if posting_status != "active":
        return EvaluationDecision.CLOSED_EVIDENCE, (
            f"the last reading of this posting says it is {posting_status}, so this is "
            "closed evidence"
        )
    if eligibility == "ineligible":
        return EvaluationDecision.INELIGIBLE, _quoting(
            "the posting's place rules you out, so this is ineligible", place_reading
        )
    if credential_gate:
        return EvaluationDecision.INELIGIBLE, _quoting(
            "the posting states as absolute a credential you declared you lack, so this is "
            "ineligible",
            credential_gate,
        )
    if outcome == "pursue" and not place_fit_rule:
        found = (
            "puts the posting in none of your markets"
            if eligibility == OUTSIDE_DECLARED
            else "did not settle which of your markets the posting is in"
        )
        return EvaluationDecision.INVESTIGATE, _quoting(
            f"the place reading {found}, so this is investigate rather than apply now",
            place_reading,
        )
    if outcome == "pursue" and residence_gate:
        return EvaluationDecision.INVESTIGATE, _quoting(
            "the posting states a residence rule the place reading did not read, so this is "
            "investigate rather than apply now",
            residence_gate,
        )
    if outcome == "pursue":
        return _pursued(pay_target_met, pay_above_stretch)
    if outcome == "skip":
        return EvaluationDecision.DEPRIORITIZE, ""
    return EvaluationDecision.INVESTIGATE, ""


def _pursued(pay_target_met: bool, pay_above_stretch: str) -> tuple[EvaluationDecision, str]:
    """Return premium target for pay at the target, unless its floor is above the stretch."""
    if not pay_target_met:
        return EvaluationDecision.APPLY_NOW, ""
    if pay_above_stretch:
        return EvaluationDecision.APPLY_NOW, (
            f"the posting's pay floor is above your {pay_above_stretch} stretch figure, "
            "which can mean a role pitched at another level, so this is apply now rather "
            "than premium target"
        )
    return EvaluationDecision.PREMIUM_TARGET, ""


def unread_residence(gate: HardGate, residence: Answer[str]) -> bool:
    """Return whether a verified blocking location gate stands where the rules read no residence."""
    return gate.kind == "location" and gate.verified_blocking and residence.value is None


def unread_residence_gate(gates: Sequence[HardGate], residence: Answer[str]) -> str:
    """Return the text of the first gate :func:`unread_residence` holds for, or empty."""
    return next((gate.text for gate in gates if unread_residence(gate, residence)), "")


def _quoting(reason: str, reading: str) -> str:
    """Return a reason followed by the reading it came from, when there is one."""
    return f"{reason}: {reading}" if reading else reason


def arrangement_cap(
    chosen: EvaluationDecision, location: Answer[str]
) -> tuple[EvaluationDecision, str]:
    """Stop a premium target at apply now when the posting's arrangement does not add up.

    Returns the decision and the reason for the cap, which is empty when nothing was capped.
    """
    if chosen is not EvaluationDecision.PREMIUM_TARGET or not location.conflict:
        return chosen, ""
    return EvaluationDecision.APPLY_NOW, (
        "the posting's working arrangement does not add up, so this is apply now rather "
        f"than premium target: {location.note}"
    )


def deterministic_scores(
    requirements: Sequence[Requirement], evidence: EvidenceMap, outcome: str
) -> DeterministicScores:
    """Derive fit, evidence, and opportunity from the requirements and their matches."""
    supported = sum(1 for match in evidence.matches if match.classification not in UNSUPPORTED)
    # Evidence is what the record already shows. A transferable claim answers a
    # requirement without evidencing it.
    evidenced = sum(
        1 for match in evidence.matches if match.classification is Classification.DIRECT
    )
    return DeterministicScores(
        fit=evidence.weighted_fit(requirements),
        evidence=round(evidenced / len(requirements) * 100) if requirements else 0,
        opportunity=_OPPORTUNITY.get(outcome, 50),
        supported=supported,
    )


def supported_requirements(
    requirements: Sequence[Requirement], evidence: EvidenceMap
) -> list[Requirement]:
    """Return the requirements a candidate record actually answered."""
    return [
        requirement
        for requirement in requirements
        if (match := evidence.for_requirement(requirement.id)) is not None
        and match.classification.value not in {"GAP", "ASSERTED"}
    ]


def gap_requirements(
    requirements: Sequence[Requirement], evidence: EvidenceMap
) -> list[Requirement]:
    """Return the requirements reported back as gaps.

    An asserted match is not one. It was answered, by a claim that has not been
    shown, which is a different thing to go and check.
    """
    return [
        requirement
        for requirement in requirements
        if (match := evidence.for_requirement(requirement.id)) is None
        or match.classification.value == "GAP"
    ]


def factors(
    requirements: Sequence[Requirement],
    evidence: EvidenceMap,
    requirement_keys: dict[str, list[str]],
    first_key: str,
) -> tuple[list[EvaluationFactorInput], list[EvaluationFactorInput]]:
    """Build the pros and cons, falling back to a statement about the run itself.

    A posting with no extracted requirements still has to say something true, and
    "nothing to report" would read as agreement. Gaps lead the cons, ahead of the
    partial matches a weak answer makes.
    """
    supported = supported_requirements(requirements, evidence)
    partial = [
        requirement
        for requirement in supported
        if (match := evidence.for_requirement(requirement.id)) is not None
        and match.strength in _PARTIAL
    ]
    pros = [
        EvaluationFactorInput(
            text=requirement.text,
            evidence_keys=requirement_keys[requirement.id],
        )
        for requirement in supported
        if requirement not in partial
    ] or [
        EvaluationFactorInput(
            text="The current posting was read successfully.", evidence_keys=[first_key]
        )
    ]
    cons = [
        EvaluationFactorInput(
            text=requirement.text,
            evidence_keys=requirement_keys[requirement.id],
            direction=EvidenceDirection.LIMITS,
        )
        for requirement in gap_requirements(requirements, evidence)
    ] + [
        EvaluationFactorInput(
            text=f"Partial match: {requirement.text}",
            evidence_keys=requirement_keys[requirement.id],
            direction=EvidenceDirection.LIMITS,
        )
        for requirement in partial
    ] or [
        EvaluationFactorInput(
            text="Model-derived requirements still need human review.",
            evidence_keys=[first_key],
            direction=EvidenceDirection.LIMITS,
        )
    ]
    return pros, cons
