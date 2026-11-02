"""The deterministic evaluation rules, exercised without a database or a run."""

from __future__ import annotations

from tailorcv.application.tailoring.fit.structure import Answer
from tailorcv.application.tailoring.stages.jobspec import HardGate, Importance, Requirement
from tailorcv.application.tailoring.stages.match import Classification, EvidenceMap, Match, Strength
from tailorcv.application.tailoring.stages.spans import locate
from tailorcv.application.tailoring.workflows.evaluation_policy import (
    arrangement_cap,
    compensation_score,
    decision,
    deterministic_scores,
    eligibility_score,
    factors,
    gap_requirements,
    supported_requirements,
    unread_residence_gate,
)
from tailorcv.domain.records import EvaluationDecision, EvidenceDirection
from tailorcv.domain.search_parameters import OUTSIDE_DECLARED


def _requirement(identifier: str, importance: Importance = Importance.MUST_HAVE) -> Requirement:
    return Requirement(id=identifier, text=f"Requirement {identifier}", importance=importance)


def _match(identifier: str, classification: Classification, strength: Strength) -> Match:
    return Match(requirement_id=identifier, classification=classification, strength=strength)


def test_a_posting_with_no_extracted_requirements_scores_zero_rather_than_dividing_by_it() -> None:
    scores = deterministic_scores([], EvidenceMap(), "pursue")

    assert (scores.fit, scores.evidence, scores.supported) == (0, 0, 0)
    assert scores.opportunity == 90


def test_an_unrecognised_outcome_scores_the_middle_rather_than_the_best_one() -> None:
    assert deterministic_scores([], EvidenceMap(), "pursue").opportunity == 90
    assert deterministic_scores([], EvidenceMap(), "skip").opportunity == 20
    assert deterministic_scores([], EvidenceMap(), "something new").opportunity == 50


def test_fit_weighs_each_requirement_by_importance_and_evidence_by_count() -> None:
    requirements = [_requirement("r1"), _requirement("r2", Importance.NICE_TO_HAVE)]
    evidence = EvidenceMap(
        matches=(
            _match("r1", Classification.DIRECT, Strength.STRONG),
            _match("r2", Classification.GAP, Strength.NONE),
        )
    )

    scores = deterministic_scores(requirements, evidence, "pursue")

    # One of two requirements is answered, so evidence is a count and fit is not:
    # the answered one is the heavier of the two.
    assert scores.supported == 1
    assert scores.evidence == 50
    assert scores.fit > scores.evidence


def test_an_asserted_requirement_counts_as_neither_supported_nor_a_gap() -> None:
    requirements = [_requirement("r1")]
    evidence = EvidenceMap(matches=(_match("r1", Classification.ASSERTED, Strength.MODERATE),))

    assert deterministic_scores(requirements, evidence, "pursue").supported == 0
    assert supported_requirements(requirements, evidence) == []
    # It was answered, by a claim nothing has shown, so it is not reported back
    # as a gap to go and find evidence for.
    assert gap_requirements(requirements, evidence) == []


def test_a_requirement_with_no_match_at_all_is_a_gap() -> None:
    requirements = [_requirement("r1")]

    assert gap_requirements(requirements, EvidenceMap()) == requirements


def test_a_closed_or_ineligible_posting_decides_before_the_run_outcome_does() -> None:
    assert decision("pursue", "closed", "authorized", "US")[0] == (
        EvaluationDecision.CLOSED_EVIDENCE
    )
    assert decision(
        "pursue", "active", "ineligible", "US", place_reading="Reside in Ruritania"
    ) == (
        EvaluationDecision.INELIGIBLE,
        "the posting's place rules you out, so this is ineligible: Reside in Ruritania",
    )


def test_a_required_credential_the_person_lacks_refuses_even_a_pursue_outcome() -> None:
    """A declared missing credential is a hard exclusion, quoted like an ineligible place."""
    assert decision(
        "pursue", "active", "authorized", "US", credential_gate="Active locksmith license required"
    ) == (
        EvaluationDecision.INELIGIBLE,
        "the posting states as absolute a credential you declared you lack, so this is "
        "ineligible: Active locksmith license required",
    )


def test_applying_now_needs_a_place_fit_rule_as_well_as_a_pursue_outcome() -> None:
    assert decision("pursue", "active", "authorized", "US") == (EvaluationDecision.APPLY_NOW, "")
    assert decision("pursue", "active", OUTSIDE_DECLARED, "") == (
        EvaluationDecision.INVESTIGATE,
        "the place reading puts the posting in none of your markets, so this is investigate "
        "rather than apply now",
    )
    assert decision("pursue", "active", "", "", place_reading="names two of your places") == (
        EvaluationDecision.INVESTIGATE,
        "the place reading did not settle which of your markets the posting is in, so this "
        "is investigate rather than apply now: names two of your places",
    )
    assert decision("skip", "active", "authorized", "US") == (EvaluationDecision.DEPRIORITIZE, "")


_RESIDENTS_ONLY = "Open to current Springfield residents ONLY."
_RESIDENCE_POSTING = f"# Data Engineer at Initech\n\nRemote within the US.\n{_RESIDENTS_ONLY}"
_NO_RESIDENCE = Answer[str]("Residence the posting requires")


def _location_gate(text: str = _RESIDENTS_ONLY, *, blocking: bool = True) -> HardGate:
    return HardGate(
        kind="location",
        text=text,
        blocking=blocking,
        source_span=locate(text, _RESIDENCE_POSTING),
    )


def test_a_verified_residence_gate_the_rules_did_not_read_caps_apply_now_at_investigate() -> None:
    """The rules placed the posting eligible but read no residence, so the gate stands unread."""
    gate = unread_residence_gate((_location_gate(),), _NO_RESIDENCE)

    assert decision("pursue", "active", "authorized", "US", True, residence_gate=gate) == (
        EvaluationDecision.INVESTIGATE,
        "the posting states a residence rule the place reading did not read, so this is "
        f"investigate rather than apply now: {_RESIDENTS_ONLY}",
    )


def test_a_residence_gate_caps_nothing_once_the_rules_read_one_or_it_is_not_verified() -> None:
    read = Answer("Residence the posting requires", value="Springfield")

    assert unread_residence_gate((_location_gate(),), read) == ""
    assert unread_residence_gate((_location_gate("Must live on the Moon"),), _NO_RESIDENCE) == ""
    assert unread_residence_gate((_location_gate(blocking=False),), _NO_RESIDENCE) == ""
    assert decision("pursue", "active", "authorized", "US", residence_gate="") == (
        EvaluationDecision.APPLY_NOW,
        "",
    )


def test_a_premium_target_requires_a_stated_pay_target() -> None:
    assert decision("pursue", "active", "authorized", "CA", False)[0] == (
        EvaluationDecision.APPLY_NOW
    )
    assert decision("pursue", "active", "authorized", "CA", True) == (
        EvaluationDecision.PREMIUM_TARGET,
        "",
    )


def test_a_pay_floor_above_the_stretch_is_apply_now_rather_than_premium_target() -> None:
    assert decision(
        "pursue", "active", "authorized", "CA", True, pay_above_stretch="CAD annual"
    ) == (
        EvaluationDecision.APPLY_NOW,
        "the posting's pay floor is above your CAD annual stretch figure, which can mean a "
        "role pitched at another level, so this is apply now rather than premium target",
    )
    assert decision("pursue", "active", "authorized", "CA", True, pay_above_stretch="") == (
        EvaluationDecision.PREMIUM_TARGET,
        "",
    )


def test_an_arrangement_that_does_not_add_up_stops_a_premium_target_at_apply_now() -> None:
    conflicted: Answer[str] = Answer(
        "Where the work happens",
        note="the LinkedIn arrangement tag conflicts with a remote place the posting lists",
        conflict=True,
    )

    capped, reason = arrangement_cap(EvaluationDecision.PREMIUM_TARGET, conflicted)

    assert capped == EvaluationDecision.APPLY_NOW
    assert reason == (
        "the posting's working arrangement does not add up, so this is apply now rather "
        "than premium target: the LinkedIn arrangement tag conflicts with a remote place "
        "the posting lists"
    )


def test_a_resolved_arrangement_leaves_a_premium_target_standing() -> None:
    resolved = Answer("Where the work happens", "remote", evidence=("Remote, Canada",))

    assert arrangement_cap(EvaluationDecision.PREMIUM_TARGET, resolved) == (
        EvaluationDecision.PREMIUM_TARGET,
        "",
    )


def test_monitor_is_an_adjudication_label_that_no_run_can_reach() -> None:
    """It marks an employer worth watching, which this evaluation does not measure.

    The member stays because the adjudicated corpus labels cases with it, so
    removing it would make those annotations unreadable.
    """
    reached = {
        decision(outcome, status, eligibility, rule, met)[0]
        for outcome in ("pursue", "skip", "cautious pursue")
        for status in ("active", "closed")
        for eligibility in ("authorized", "ineligible", "sponsorship_required", "")
        for rule in ("US", "CA", "")
        for met in (False, True)
    }

    assert EvaluationDecision.MONITOR not in reached


def test_an_unstated_location_or_pay_abstains_and_says_why() -> None:
    unstated = eligibility_score("", "")
    assert unstated.value is None
    assert unstated.reason == "The posting location is not stated."

    assert eligibility_score("authorized", "").value == 100
    assert eligibility_score("sponsorship required", "").value == 60
    assert eligibility_score("ineligible", "").value == 0

    no_pay = compensation_score(None, "")
    assert no_pay.value is None
    assert no_pay.reason == "The posting states no comparable pay."
    assert compensation_score("at or above the floor", "").value == 100
    assert compensation_score("below the floor", "").value == 0


def test_a_supplied_reason_survives_rather_than_being_replaced_by_the_default() -> None:
    assert eligibility_score("", "The posting names two countries.").reason == (
        "The posting names two countries."
    )
    assert compensation_score(None, "The range is hourly.").reason == "The range is hourly."


def test_factors_fall_back_to_a_true_statement_when_nothing_was_extracted() -> None:
    pros, cons = factors([], EvidenceMap(), {}, "posting-body")

    # Saying nothing here would read as agreement, so both sides name the run.
    assert [factor.text for factor in pros] == ["The current posting was read successfully."]
    assert [factor.text for factor in cons] == [
        "Model-derived requirements still need human review."
    ]
    assert pros[0].evidence_keys == ["posting-body"]
    assert cons[0].direction == EvidenceDirection.LIMITS


def test_factors_cite_the_evidence_keys_collected_for_each_requirement() -> None:
    requirements = [_requirement("r1"), _requirement("r2")]
    evidence = EvidenceMap(
        matches=(
            _match("r1", Classification.DIRECT, Strength.STRONG),
            _match("r2", Classification.GAP, Strength.NONE),
        )
    )
    keys = {"r1": ["requirement-1-candidate-1"], "r2": ["requirement-2-posting"]}

    pros, cons = factors(requirements, evidence, keys, "unused")

    assert [factor.evidence_keys for factor in pros] == [["requirement-1-candidate-1"]]
    assert [factor.evidence_keys for factor in cons] == [["requirement-2-posting"]]
    assert all(factor.direction == EvidenceDirection.LIMITS for factor in cons)


def test_a_weak_match_is_a_partial_match_among_the_cons_rather_than_a_pro() -> None:
    requirements = [_requirement("r1"), _requirement("r2"), _requirement("r3")]
    evidence = EvidenceMap(
        matches=(
            _match("r1", Classification.DIRECT, Strength.STRONG),
            _match("r2", Classification.TRANSFERABLE, Strength.MODERATE),
            _match("r3", Classification.TRANSFERABLE, Strength.WEAK),
        )
    )
    keys = {"r1": ["k1"], "r2": ["k2"], "r3": ["k3"]}

    pros, cons = factors(requirements, evidence, keys, "unused")

    assert [factor.text for factor in pros] == ["Requirement r1", "Requirement r2"]
    assert [factor.text for factor in cons] == ["Partial match: Requirement r3"]
    assert cons[0].evidence_keys == ["k3"]
    assert cons[0].direction == EvidenceDirection.LIMITS
