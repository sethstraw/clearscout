"""Blinded review resolution stays evidence-bound, family-bound, and provenance-complete."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from evals.inspect.labeling import (
    OPERATIONAL_RECEIPT_DOMAIN,
    PROTOCOL_VERSION,
    REVIEW_CONTEXT_DOMAIN,
    REVIEWER_PACKET_DOMAIN,
    CandidatePool,
    ExtractionQaSet,
    GuardrailsSnapshot,
    InventorySnapshot,
    Operation,
    OperationalAttestations,
    OperationalReceipt,
    ReviewContext,
    ReviewerPacket,
    ReviewerPacketCase,
    ReviewFamily,
    RubricText,
    checked_runtime_identity,
    domain_sha256,
    protocol_version,
)
from evals.inspect.review_resolution import (
    AcceptedPair,
    Adjudication,
    AdjudicationDecision,
    BlindnessAttestation,
    CaseRuling,
    ChallengeClass,
    Confidence,
    FrozenReview,
    LabelProvenance,
    MatchDecision,
    RelabelRound,
    RelevanceDecision,
    ResolutionSet,
    ResolvedLabel,
    ReviewArtifact,
    ReviewerRole,
    ReviewJudgment,
    ReviewSet,
    adjudication_input_sha256,
    field_agreement,
    krippendorff_alpha,
    match_material,
    material_disagreements,
    review_domain,
    round_finality,
)
from evals.inspect.selection import (
    SELECTION_MANIFEST_DOMAIN,
    SelectionManifest,
    select_cases,
    select_held_out,
)
from evals.support.inventory import synthetic_guardrails, synthetic_inventory
from pydantic import TypeAdapter, ValidationError

from clearscout.application.tailoring.stages.jobspec import Dimension
from clearscout.application.tailoring.stages.match import Classification, Strength
from tests.support.match_selection import (
    HELD_OUT,
    SET_SIZE,
    candidate_pool,
    case_uuid,
    manifest_body,
    qa_set,
    resolved_label,
)

CASE_A = UUID("10000000-0000-4000-8000-000000000001")
CASE_B = UUID("10000000-0000-4000-8000-000000000002")
HASH_A = "a" * 64
HASH_B = "b" * 64
MATCH = ReviewFamily.MATCH
RELEVANCE = ReviewFamily.RELEVANCE_REVIEW


def _context(
    family: ReviewFamily = MATCH,
    *,
    rubric_version: str | None = None,
    rubric_text: str = "Judge only from this synthetic context.",
) -> ReviewContext:
    inventory = InventorySnapshot.from_inventory(synthetic_inventory())
    guardrails = GuardrailsSnapshot.from_guardrails(synthetic_guardrails())
    status = guardrails.work_authorization
    assert status is not None
    body = {
        "protocol_version": protocol_version(family),
        "rubric_version": rubric_version or f"{family.value}-rubric-v1",
        "rubric_text": rubric_text,
        "synthetic_inventory": inventory,
        "synthetic_inventory_hash": inventory.content_hash,
        "asserted_status_lines": (status,),
        "guardrails": guardrails,
        "valid_entry_ids": tuple(item.id for item in inventory.entries),
    }
    return ReviewContext.model_validate(
        {**body, "review_context_sha256": domain_sha256(REVIEW_CONTEXT_DOMAIN, body)}
    )


def _packet(context: ReviewContext, *case_ids: UUID) -> ReviewerPacket:
    cases = tuple(
        ReviewerPacketCase(case_id=case_id, requirement_text=f"Requirement {case_id}")
        for case_id in case_ids
    )
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_pool_sha256": HASH_A,
        "extraction_qa_sha256": HASH_B,
        "review_context_sha256": context.review_context_sha256,
        "rubric_version": context.rubric_version,
        "rubric_text": context.rubric_text,
        "synthetic_inventory": context.synthetic_inventory,
        "asserted_status_lines": context.asserted_status_lines,
        "guardrails": context.guardrails,
        "cases": cases,
    }
    return ReviewerPacket.model_validate(
        {**body, "packet_sha256": domain_sha256(REVIEWER_PACKET_DOMAIN, body)}
    )


def _blindness(**changes: bool) -> BlindnessAttestation:
    values = {
        "historical_outcomes_unseen": True,
        "other_reviewer_output_unseen": True,
        "evaluated_output_unseen": True,
        "future_provider_identity_unseen": True,
        "future_selection_unseen": True,
        "untrusted_requirement_treated_as_data": True,
        **changes,
    }
    return BlindnessAttestation.model_validate(values)


def _match_decision(
    context: ReviewContext,
    *,
    classification: Classification = Classification.DIRECT,
    strength: Strength | None = None,
    dimension: Dimension = Dimension.HARD,
    challenge_class: ChallengeClass = ChallengeClass.CLEAR_SUPPORTED,
    evidence: tuple[str, ...] | None = None,
    asserted_status_fact: str | None = None,
) -> MatchDecision:
    if evidence is None:
        evidence = (context.valid_entry_ids[0],)
    if strength is None:
        uncited = classification in {Classification.GAP, Classification.ASSERTED}
        strength = Strength.NONE if uncited else Strength.STRONG
    return MatchDecision.model_validate(
        {
            "family": MATCH,
            "classification": classification,
            "strength": strength,
            "dimension": dimension,
            "challenge_class": challenge_class,
            "decisive_entry_ids": evidence,
            "asserted_status_fact": asserted_status_fact,
        }
    )


def _relevance_decision(
    *,
    core_work_alignment: str = "direct",
    core_qualification_alignment: str = "established",
    hard_condition: str = "passes",
    decisive_basis: str = "core_match",
    recommendation: str = "review",
) -> RelevanceDecision:
    return RelevanceDecision.model_validate(
        {
            "family": RELEVANCE,
            "core_work_alignment": core_work_alignment,
            "core_qualification_alignment": core_qualification_alignment,
            "hard_condition": hard_condition,
            "decisive_basis": decisive_basis,
            "recommendation": recommendation,
        }
    )


def _judgment_from(
    material: MatchDecision | RelevanceDecision,
    *,
    case_id: UUID = CASE_A,
    family: ReviewFamily = MATCH,
    declared: str | None = None,
    rationale: str = "The synthetic record directly supports the requirement.",
    confidence: Confidence = Confidence.HIGH,
) -> ReviewJudgment:
    body = {
        "protocol_version": declared or protocol_version(family),
        "family": family,
        "case_id": case_id,
        "material": material,
        "rationale": rationale,
        "confidence": confidence,
        "blindness": _blindness(),
    }
    return ReviewJudgment.model_validate(
        {
            **body,
            "judgment_sha256": domain_sha256(review_domain(family, ReviewArtifact.JUDGMENT), body),
        }
    )


def _judgment(
    context: ReviewContext,
    *,
    case_id: UUID = CASE_A,
    rationale: str = "The synthetic record directly supports the requirement.",
    confidence: Confidence = Confidence.HIGH,
    **decision: Any,
) -> ReviewJudgment:
    return _judgment_from(
        _match_decision(context, **decision),
        case_id=case_id,
        rationale=rationale,
        confidence=confidence,
    )


def _receipt(
    operation: Operation,
    *,
    agent_id: str,
    input_sha256: str,
    output_sha256: str,
    family: ReviewFamily = MATCH,
) -> OperationalReceipt:
    moment = datetime(2026, 8, 26, 20, tzinfo=UTC)
    body = {
        "protocol_version": protocol_version(family),
        "operation": operation,
        "agent_id": agent_id,
        "agent_kind": "agent",
        "runtime_identity": checked_runtime_identity(
            None, {"agentId": agent_id, "resolvedModel": f"runtime-{agent_id}"}, agent_id=agent_id
        ),
        "started_at": moment,
        "completed_at": moment,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "attestations": OperationalAttestations(
            source_scope_respected=True,
            historical_outcomes_unseen=True,
            evaluated_output_unseen=True,
        ),
    }
    return OperationalReceipt.model_validate(
        {**body, "receipt_sha256": domain_sha256(OPERATIONAL_RECEIPT_DOMAIN, body)}
    )


def _frozen(
    role: str,
    packet: ReviewerPacket | None,
    judgment: ReviewJudgment,
    receipt: OperationalReceipt,
) -> FrozenReview:
    body = {
        "protocol_version": judgment.protocol_version,
        "family": judgment.family,
        "reviewer_role": role,
        "packet_sha256": packet.packet_sha256 if packet is not None else None,
        "judgment": judgment,
        "operational_receipt_sha256": receipt.receipt_sha256,
    }
    return FrozenReview.model_validate(
        {
            **body,
            "frozen_review_sha256": domain_sha256(
                review_domain(judgment.family, ReviewArtifact.FROZEN_REVIEW), body
            ),
        }
    )


def _review_pair(
    context: ReviewContext,
    packet: ReviewerPacket,
    review_b_judgment: ReviewJudgment | None = None,
) -> tuple[FrozenReview, FrozenReview, OperationalReceipt, OperationalReceipt]:
    judgment_a = _judgment(context)
    judgment_b = review_b_judgment or _judgment(context, rationale="Independent agreement.")
    receipt_a = _receipt(
        Operation.REVIEW_A,
        agent_id="reviewer-a",
        input_sha256=packet.packet_sha256,
        output_sha256=judgment_a.judgment_sha256,
    )
    receipt_b = _receipt(
        Operation.REVIEW_B,
        agent_id="reviewer-b",
        input_sha256=packet.packet_sha256,
        output_sha256=judgment_b.judgment_sha256,
    )
    return (
        _frozen("A", packet, judgment_a, receipt_a),
        _frozen("B", packet, judgment_b, receipt_b),
        receipt_a,
        receipt_b,
    )


def _review_set(
    packet: ReviewerPacket, context: ReviewContext, a: FrozenReview, b: FrozenReview
) -> ReviewSet:
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "packet_a_sha256": packet.packet_sha256,
        "packet_b_sha256": packet.packet_sha256,
        "review_context_sha256": context.review_context_sha256,
        "reviews_a": (a,),
        "reviews_b": (b,),
    }
    return ReviewSet.model_validate(
        {
            **body,
            "review_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.REVIEW_SET), body
            ),
        }
    )


def _decision(context: ReviewContext, classification: Classification) -> AdjudicationDecision:
    evidence = (
        ()
        if classification in {Classification.GAP, Classification.ASSERTED}
        else (context.valid_entry_ids[0],)
    )
    status = context.asserted_status_lines[0] if classification is Classification.ASSERTED else None
    material = _match_decision(
        context,
        classification=classification,
        challenge_class=(
            ChallengeClass.ABSENT_EVIDENCE
            if classification is Classification.GAP
            else ChallengeClass.CLEAR_SUPPORTED
        ),
        evidence=evidence,
        asserted_status_fact=status,
    )
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "case_id": CASE_A,
        "material": material,
        "rationale": "The adjudicator resolved the material fields from the frozen context.",
        "confidence": Confidence.HIGH,
    }
    return AdjudicationDecision.model_validate(
        {
            **body,
            "decision_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.ADJUDICATION_DECISION), body
            ),
        }
    )


def _adjudication(
    context: ReviewContext, a: FrozenReview, b: FrozenReview
) -> tuple[Adjudication, OperationalReceipt]:
    decision = _decision(context, Classification.GAP)
    input_sha256 = adjudication_input_sha256(a, b)
    receipt = _receipt(
        Operation.ADJUDICATE,
        agent_id="adjudicator",
        input_sha256=input_sha256,
        output_sha256=decision.decision_sha256,
    )
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "case_id": CASE_A,
        "review_a_sha256": a.frozen_review_sha256,
        "review_b_sha256": b.frozen_review_sha256,
        "disagreements": material_disagreements(a.judgment, b.judgment),
        "input_sha256": input_sha256,
        "decision": decision,
        "operational_receipt_sha256": receipt.receipt_sha256,
    }
    return (
        Adjudication.model_validate(
            {
                **body,
                "adjudication_sha256": domain_sha256(
                    review_domain(MATCH, ReviewArtifact.ADJUDICATION), body
                ),
            }
        ),
        receipt,
    )


def _label(
    source: str,
    a: FrozenReview,
    b: FrozenReview,
    material: MatchDecision | RelevanceDecision,
    adjudication: Adjudication | None = None,
) -> ResolvedLabel:
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "case_id": CASE_A,
        "material": material,
        "source": source,
        "review_a_sha256": a.frozen_review_sha256,
        "review_b_sha256": b.frozen_review_sha256,
        "adjudication_sha256": (
            adjudication.adjudication_sha256 if adjudication is not None else None
        ),
    }
    return ResolvedLabel.model_validate(
        {
            **body,
            "resolved_label_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLVED_LABEL), body
            ),
        }
    )


def _provenance(**changes: Any) -> LabelProvenance:
    values: dict[str, Any] = {
        "protocol_version": protocol_version(RELEVANCE),
        "family": RELEVANCE,
        "rubric_version": "relevance-rubric-v1",
        "reviewer_a": "labeller-a",
        "reviewer_b": "labeller-b",
        "disagreements": (),
        **changes,
    }
    return LabelProvenance.model_validate(values)


def test_review_resolution_vocabularies_are_closed() -> None:
    assert {item.value for item in ChallengeClass} == {
        "clear_supported",
        "boilerplate_or_status",
        "soft_or_transferable",
        "absent_evidence",
        "partial_contribution",
    }
    assert {item.value for item in Confidence} == {"high", "medium", "low"}


def _rubric() -> str:
    return (
        Path(__file__).resolve().parents[2] / "evals/inspect/prompts/match_reviewer.md"
    ).read_text(encoding="utf-8")


def test_the_reviewer_rubric_offers_every_challenge_class() -> None:
    """A class the rubric never names is one blind reviewers can never choose."""
    rubric = _rubric()
    assert [item.value for item in ChallengeClass if f"`{item.value}`" not in rubric] == []


def test_the_reviewer_rubric_offers_every_strength() -> None:
    """A strength the rubric never names is one blind reviewers can never record."""
    rubric = _rubric()
    assert [item.value for item in Strength if f"`{item.value}`" not in rubric] == []


def test_the_reviewer_rubric_reserves_gap_for_no_related_action() -> None:
    """A related action below the required level is TRANSFERABLE, as the match prompt says."""
    rubric = " ".join(_rubric().split())
    assert "`GAP`: no entry shows a concrete related capability or action." in rubric


def test_the_reviewer_rubric_fits_the_frozen_review_context() -> None:
    """The rubric is frozen into every review context, so it must fit the text bound."""
    TypeAdapter(RubricText).validate_python(_rubric())


@pytest.mark.parametrize(
    "boundary",
    [
        "does not label the disposition",
        "name a disposition the requirement asks for",
        "helped people do that work narrates the trait, so it is `TAILORING_ONLY` unless it "
        "labels the disposition",
        "or an unmet minimum of years, licence, or regulated domain the requirement states, "
        "leaves it materially partial: use `weak`, however closely the capability fits",
        "A `TRANSFERABLE` match whose difference is only context, scale, or an adjacent tool",
        "Only what the requirement asks of the applicant is a qualifier",
        'examples after "such as"',
        "descriptors no record could state are not",
        'an "or" list is met by any one of its items',
        "No other unmet qualifier forces `weak`",
        "facts that only make it likely leave it unmet",
        "a different action toward the same people, such as organizing a team's work "
        "against an ask to mediate disagreements between its members",
        "help with work other than what the trait names",
        "A different tool, and a programming language counts as one, is related only within "
        "the record's ecosystem",
        "related action means this same pattern",
        "Match the whole requirement, including qualifiers such as production use, years, "
        "scale, seniority, licence, and regulated domain.",
        "Partial evidence is not a direct match.",
        "Do not infer a skill from a title, employer, degree, neighbouring skill, or general "
        "intelligence.",
        "Do not treat familiarity as production experience or one product in a category as "
        "experience with every product in that category.",
        "constrain `DIRECT` and `TAILORING_ONLY`; they do not automatically require `GAP`.",
        "concretely demonstrates a related capability or action but differs materially in "
        "tool, domain, scale, or context",
        "React evidence for a Next.js requirement, or a required language that is a superset "
        "of the record's language or runs on its runtime, such as JavaScript evidence for "
        "TypeScript or Java evidence for Kotlin.",
        "with a years or proficiency qualifier, such as 4+ years of Elixir, is `GAP` unless "
        "the record shows that tool.",
        'examples after "such as" or "including"',
        'An "including" list names examples of the ask, not items each required, so evidence '
        "of the ask answers it without every item.",
        "A requirement whose only object is a team the applicant would work with, such as "
        "design, UX, QA, data science, sales, or marketing, asks for work across functions: "
        "evidence of that work answers it whichever functions the record names, and a function "
        "the record never mentions is not a missing element.",
        "That clause compares actions, so it applies only when the requirement names one, as "
        "mediating disagreements does.",
        "A requirement naming only a disposition, such as energy, emotional intelligence, or "
        "composure, names no action for evidence to differ from: an entry narrating behavior a "
        "reviewer would read as that disposition is `TAILORING_ONLY` unless it labels the "
        "disposition, and `GAP` is for a record that narrates no such behavior.",
        "lead, own, or seniority ask still applies",
        "A requirement that asks for a named tool, such as SQL, is answered only by an entry "
        "that names that tool or a tool of its ecosystem as this rule describes; an entry naming "
        "neither does not answer it, however close its work, and is not cited for it.",
        "names related work in the record is wrong: cite that work and classify it `TRANSFERABLE`.",
        'such as "helped with", "contributed to", or "worked on", is never `DIRECT`.',
        "A minimum the record meets, such as years its dated roles cover, is answered: it "
        "lowers neither the classification nor the strength.",
        "answers a named-degree requirement `DIRECT` even though its name differs",
        "For a requirement naming an academic degree at a level (bachelor's, master's, "
        "doctorate) in a field, compare levels directly: a stated credential at an equal or "
        "higher level can answer it, a lower level cannot.",
        "is a specific authorization, not a rung on a ladder: it is never satisfied by a "
        "different or higher credential and keeps falling under this rule and the "
        "asserted-eligibility rule.",
        "This forbids inferring a SKILL from a degree; it does not forbid crediting a "
        "CREDENTIAL requirement with a credential the record states.",
        "The field judgment is not yours to make: credit it only where the applicant's "
        "distinctions rules mark the record's field equivalent-or-deeper or adjacent; where no "
        "distinction rule speaks to the field, or no credential reaches the required level, "
        "classify `GAP`.",
        "The limited contribution is what a truthful rewrite would keep, not a missing "
        "qualifier: when an entry names the requested capability as work the applicant took "
        "part in and meets every qualifier the requirement states, classify it "
        "`TAILORING_ONLY` at `moderate` strength.",
        "The requirement's action verb, such as improves, builds, or delivers, is not an "
        "ownership ask.",
    ],
)
def test_the_reviewer_rubric_shares_the_product_behavioral_boundary(boundary: str) -> None:
    """Reviewers and the match stage draw the behavioral and strength boundaries the same way."""
    product = (Path(__file__).resolve().parents[2] / "prompts/match_system.md").read_text(
        encoding="utf-8"
    )
    assert (boundary in " ".join(product.split()), boundary in " ".join(_rubric().split())) == (
        True,
        True,
    )


def test_each_family_keeps_its_own_hash_domain() -> None:
    """A judgment cannot be replayed into another family, because the domain differs."""
    assert review_domain(MATCH, ReviewArtifact.JUDGMENT) == "clearscout-match-judgment-v1"
    assert (
        review_domain(RELEVANCE, ReviewArtifact.JUDGMENT)
        == "clearscout-relevance-review-judgment-v1"
    )
    assert review_domain(RELEVANCE, ReviewArtifact.REVIEW_SET) == (
        "clearscout-relevance-review-judgment-set-v1"
    )
    assert len({review_domain(MATCH, item) for item in ReviewArtifact}) == len(ReviewArtifact)


def test_blindness_attestation_refuses_any_false_statement() -> None:
    with pytest.raises(ValidationError, match="Input should be True"):
        _blindness(other_reviewer_output_unseen=False)


@pytest.mark.parametrize(
    ("classification", "dimension", "evidence", "status", "message"),
    [
        (Classification.DIRECT, Dimension.HARD, (), None, "requires decisive evidence"),
        (Classification.GAP, Dimension.HARD, ("b-r1a",), None, "cannot cite"),
        (Classification.ASSERTED, Dimension.BEHAVIORAL, (), "status", "hard dimension"),
        (Classification.ASSERTED, Dimension.HARD, (), None, "asserted_status_fact"),
        (Classification.GAP, Dimension.HARD, (), "status", "only for ASSERTED"),
    ],
)
def test_review_judgment_enforces_classification_evidence_semantics(
    classification: Classification,
    dimension: Dimension,
    evidence: tuple[str, ...],
    status: str | None,
    message: str,
) -> None:
    context = _context()
    with pytest.raises(ValidationError, match=message):
        _judgment(
            context,
            classification=classification,
            dimension=dimension,
            evidence=evidence,
            asserted_status_fact=status,
        )


@pytest.mark.parametrize(
    ("classification", "strength", "evidence", "status"),
    [
        (Classification.DIRECT, Strength.NONE, None, None),
        (Classification.TAILORING_ONLY, Strength.NONE, None, None),
        (Classification.TRANSFERABLE, Strength.NONE, None, None),
        (Classification.GAP, Strength.WEAK, (), None),
        (Classification.ASSERTED, Strength.MODERATE, (), "status"),
    ],
)
def test_review_judgment_ties_strength_to_the_classification(
    classification: Classification,
    strength: Strength,
    evidence: tuple[str, ...] | None,
    status: str | None,
) -> None:
    """Evidence-backed labels carry a strength; uncited labels carry none."""
    with pytest.raises(ValidationError, match="strength other than none|must use strength none"):
        _judgment(
            _context(),
            classification=classification,
            strength=strength,
            evidence=evidence,
            asserted_status_fact=status,
        )


def test_a_strength_split_alone_is_a_material_disagreement() -> None:
    """Two reviewers agreeing on the class but not the strength still need adjudication."""
    context = _context()
    packet = _packet(context, CASE_A)
    moderate = _judgment(context, strength=Strength.MODERATE, rationale="Most of it is answered.")
    a, b, _, _ = _review_pair(context, packet, moderate)

    assert material_disagreements(a.judgment, b.judgment) == ("strength",)
    with pytest.raises(ValueError, match="requires adjudication"):
        _label("agreement", a, b, a.judgment.material).validate_inputs(a, b, None)
    agreement = field_agreement(_review_set(packet, context, a, b), "strength")
    assert (agreement.pairs, agreement.disagreements) == (1, 1)


def test_review_judgment_requires_context_evidence_and_status_facts() -> None:
    context = _context()
    unknown = _judgment(context, evidence=("unknown-entry",))
    with pytest.raises(ValueError, match="outside the frozen review context"):
        unknown.validate_context(context)
    asserted = _judgment(
        context,
        classification=Classification.ASSERTED,
        evidence=(),
        asserted_status_fact="An invented status fact.",
    )
    with pytest.raises(ValueError, match="asserted_status_fact"):
        asserted.validate_context(context)
    supported_assertion = _judgment(
        context,
        classification=Classification.ASSERTED,
        challenge_class=ChallengeClass.BOILERPLATE_OR_STATUS,
        evidence=(),
        asserted_status_fact=context.asserted_status_lines[0],
    )
    supported_assertion.validate_context(context)


def test_review_judgment_requires_unique_canonical_evidence_ids() -> None:
    context = _context()
    with pytest.raises(ValidationError, match="must be unique"):
        _judgment(
            context,
            evidence=(context.valid_entry_ids[0], context.valid_entry_ids[0]),
        )
    with pytest.raises(ValidationError, match="must be sorted"):
        _judgment(
            context,
            evidence=(context.valid_entry_ids[1], context.valid_entry_ids[0]),
        )


def test_a_relevance_judgment_records_the_axes_and_the_recommendation() -> None:
    """The relevance family's material decision is the four axes and the verdict they allow."""
    judgment = _judgment_from(_relevance_decision(), family=RELEVANCE)

    assert judgment.material.material_fields == (
        "core_work_alignment",
        "core_qualification_alignment",
        "hard_condition",
        "decisive_basis",
        "recommendation",
    )
    assert judgment.protocol_version == "clearscout-relevance-review-development-v1"


def test_a_relevance_judgment_refuses_a_combination_the_stage_never_allows() -> None:
    """A reviewer cannot record axes the shipped class definitions would reject."""
    with pytest.raises(ValidationError, match="no combination the stage allows"):
        _relevance_decision(core_work_alignment="absent", core_qualification_alignment="absent")


def test_a_judgment_cannot_declare_another_familys_protocol_version() -> None:
    """The declared version and the family are bound together, not stated twice."""
    with pytest.raises(ValidationError, match="protocol_version does not match"):
        _judgment_from(
            _relevance_decision(),
            family=RELEVANCE,
            declared="clearscout-match-development-v1",
        )


def test_a_judgment_cannot_carry_another_familys_decision() -> None:
    """A relevance decision inside a match judgment is a family confusion, not a label."""
    with pytest.raises(ValidationError, match="decision family does not match"):
        _judgment_from(_relevance_decision(), family=MATCH)


def test_review_set_requires_complete_coverage_and_distinct_reviewer_agents() -> None:
    context = _context()
    packet = _packet(context, CASE_A)
    a, b, receipt_a, receipt_b = _review_pair(context, packet)
    review_set = _review_set(packet, context, a, b)
    review_set.validate_inputs(packet, packet, context, (receipt_a, receipt_b))
    with pytest.raises(ValueError, match="receipt hash does not resolve"):
        review_set.validate_inputs(packet, packet, context, (receipt_a,))
    same_agent = _receipt(
        Operation.REVIEW_B,
        agent_id=receipt_a.agent_id,
        input_sha256=packet.packet_sha256,
        output_sha256=b.judgment.judgment_sha256,
    )
    b_same_agent = _frozen("B", packet, b.judgment, same_agent)
    same_agent_set = _review_set(packet, context, a, b_same_agent)
    with pytest.raises(ValueError, match="distinct agents"):
        same_agent_set.validate_inputs(packet, packet, context, (receipt_a, same_agent))
    larger_packet = _packet(context, CASE_A, CASE_B)
    partial_a, partial_b, partial_receipt_a, partial_receipt_b = _review_pair(
        context, larger_packet
    )
    partial_set = _review_set(larger_packet, context, partial_a, partial_b)
    with pytest.raises(ValueError, match="every approved case"):
        partial_set.validate_inputs(
            larger_packet,
            larger_packet,
            context,
            (partial_receipt_a, partial_receipt_b),
        )


def _relevance_review(
    context: ReviewContext, role: ReviewerRole, agent_id: str, **decision: str
) -> tuple[FrozenReview, OperationalReceipt]:
    judgment = _judgment_from(_relevance_decision(**decision), family=RELEVANCE)
    receipt = _receipt(
        Operation.REVIEW_A if role == "A" else Operation.REVIEW_B,
        agent_id=agent_id,
        input_sha256=context.review_context_sha256,
        output_sha256=judgment.judgment_sha256,
        family=RELEVANCE,
    )
    return _frozen(role, None, judgment, receipt), receipt


def test_a_relevance_review_binds_to_its_context_rather_than_a_reviewer_packet() -> None:
    """The reviewer packet is built around a requirement span, so this family has none."""
    context = _context(RELEVANCE)
    review, receipt = _relevance_review(context, "A", "labeller-a")

    review.validate_inputs(None, context, receipt)

    assert review.packet_sha256 is None


@pytest.mark.parametrize("family", [MATCH, RELEVANCE])
def test_a_packet_digest_is_recorded_exactly_for_the_family_that_has_a_packet(
    family: ReviewFamily,
) -> None:
    """A relevance review naming a match packet would bind one family's blind input to another."""
    match_context = _context()
    packet = _packet(match_context, CASE_A)
    judgment = (
        _judgment(match_context)
        if family is MATCH
        else _judgment_from(_relevance_decision(), family=RELEVANCE)
    )
    receipt = _receipt(
        Operation.REVIEW_A,
        agent_id="reviewer-a",
        input_sha256=packet.packet_sha256,
        output_sha256=judgment.judgment_sha256,
    )

    with pytest.raises(ValidationError, match="packet digest is recorded exactly"):
        _frozen("A", None if family is MATCH else packet, judgment, receipt)


def test_a_review_cannot_resolve_against_another_familys_context() -> None:
    """The context carries the rubric, so the wrong one is a review of another question."""
    context = _context(RELEVANCE)
    review, receipt = _relevance_review(context, "A", "labeller-a")

    with pytest.raises(ValueError, match="context belongs to another family"):
        review.validate_inputs(None, _context(), receipt)


def test_a_review_set_requires_the_reviewer_packets_its_family_has() -> None:
    """A set is packet coverage, so a family with no packet cannot state one."""
    context = _context(RELEVANCE)
    review_a, _ = _relevance_review(context, "A", "labeller-a")
    review_b, _ = _relevance_review(
        context,
        "B",
        "labeller-b",
        core_work_alignment="transferable",
        decisive_basis="transferable_match",
        recommendation="investigate",
    )
    body = {
        "protocol_version": protocol_version(RELEVANCE),
        "family": RELEVANCE,
        "packet_a_sha256": HASH_A,
        "packet_b_sha256": HASH_B,
        "review_context_sha256": context.review_context_sha256,
        "reviews_a": (review_a,),
        "reviews_b": (review_b,),
    }

    with pytest.raises(ValidationError, match="reviewer packets"):
        ReviewSet.model_validate(
            {
                **body,
                "review_set_sha256": domain_sha256(
                    review_domain(RELEVANCE, ReviewArtifact.REVIEW_SET), body
                ),
            }
        )


def test_material_disagreements_ignore_rationale_and_confidence() -> None:
    context = _context()
    a = _judgment(context)
    same_material = _judgment(
        context,
        rationale="A different explanation of the same material result.",
        confidence=Confidence.LOW,
    )
    assert material_disagreements(a, same_material) == ()
    gap = _judgment(
        context,
        classification=Classification.GAP,
        challenge_class=ChallengeClass.ABSENT_EVIDENCE,
        evidence=(),
    )
    assert material_disagreements(a, gap) == (
        "classification",
        "strength",
        "challenge_class",
        "decisive_entry_ids",
    )


def test_material_disagreements_name_the_axes_the_relevance_family_split_over() -> None:
    """The disagreement record follows the family's own material fields."""
    a = _judgment_from(_relevance_decision(), family=RELEVANCE)
    b = _judgment_from(
        _relevance_decision(
            core_work_alignment="transferable",
            decisive_basis="transferable_match",
            recommendation="investigate",
        ),
        family=RELEVANCE,
    )

    assert material_disagreements(a, b) == (
        "core_work_alignment",
        "decisive_basis",
        "recommendation",
    )


def test_material_disagreements_refuse_reviews_from_two_families() -> None:
    context = _context()

    with pytest.raises(ValueError, match="same family"):
        material_disagreements(
            _judgment(context), _judgment_from(_relevance_decision(), family=RELEVANCE)
        )


def _agreement_set(
    context: ReviewContext,
    packet: ReviewerPacket,
    rows: tuple[tuple[UUID, Classification, Classification], ...],
) -> ReviewSet:
    def decision(classification: Classification) -> MatchDecision:
        evidence = () if classification in {Classification.GAP, Classification.ASSERTED} else None
        return _match_decision(context, classification=classification, evidence=evidence)

    return _decided_set(
        context, packet, tuple((cid, decision(ca), decision(cb)) for cid, ca, cb in rows)
    )


def _decided_set(
    context: ReviewContext,
    packet: ReviewerPacket,
    rows: tuple[tuple[UUID, MatchDecision, MatchDecision], ...],
    reviewers: tuple[str, str] = ("reviewer-a", "reviewer-b"),
) -> ReviewSet:
    def _frozen_for(
        role: str, operation: Operation, agent: str, case_id: UUID, material: MatchDecision
    ) -> FrozenReview:
        judgment = _judgment_from(material, case_id=case_id)
        receipt = _receipt(
            operation,
            agent_id=agent,
            input_sha256=packet.packet_sha256,
            output_sha256=judgment.judgment_sha256,
        )
        return _frozen(role, packet, judgment, receipt)

    reviews_a = tuple(
        sorted(
            (_frozen_for("A", Operation.REVIEW_A, reviewers[0], cid, ca) for cid, ca, _ in rows),
            key=lambda review: str(review.judgment.case_id),
        )
    )
    reviews_b = tuple(
        sorted(
            (_frozen_for("B", Operation.REVIEW_B, reviewers[1], cid, cb) for cid, _, cb in rows),
            key=lambda review: str(review.judgment.case_id),
        )
    )
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "packet_a_sha256": packet.packet_sha256,
        "packet_b_sha256": packet.packet_sha256,
        "review_context_sha256": context.review_context_sha256,
        "reviews_a": reviews_a,
        "reviews_b": reviews_b,
    }
    return ReviewSet.model_validate(
        {
            **body,
            "review_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.REVIEW_SET), body
            ),
        }
    )


def test_field_agreement_reports_disagreements_and_cohen_kappa() -> None:
    context = _context()
    case_c = UUID("10000000-0000-4000-8000-000000000003")
    packet = _packet(context, CASE_A, CASE_B, case_c)
    rows = (
        (CASE_A, Classification.DIRECT, Classification.DIRECT),
        (CASE_B, Classification.DIRECT, Classification.TRANSFERABLE),
        (case_c, Classification.GAP, Classification.GAP),
    )

    agreement = field_agreement(_agreement_set(context, packet, rows), "classification")
    assert agreement.pairs == 3
    assert agreement.disagreements == 1
    assert agreement.cohen_kappa == pytest.approx(0.5)


def test_field_agreement_refuses_a_field_the_family_does_not_decide() -> None:
    """Asking for another family's field would report an agreement nobody measured."""
    context = _context()
    packet = _packet(context, CASE_A)
    rows = ((CASE_A, Classification.DIRECT, Classification.DIRECT),)

    with pytest.raises(ValueError, match="not a material field"):
        field_agreement(_agreement_set(context, packet, rows), "recommendation")


def test_cohen_kappa_is_undefined_when_reviewers_never_vary() -> None:
    context = _context()
    packet = _packet(context, CASE_A, CASE_B)
    rows = (
        (CASE_A, Classification.DIRECT, Classification.DIRECT),
        (CASE_B, Classification.DIRECT, Classification.DIRECT),
    )

    assert (
        field_agreement(_agreement_set(context, packet, rows), "classification").cohen_kappa is None
    )


def test_krippendorff_alpha_measures_two_reviewers_on_nominal_labels() -> None:
    assert krippendorff_alpha(["D", "D", "G"], ["D", "T", "G"]) == pytest.approx(1 - 10 / 22)
    assert krippendorff_alpha(["D", "G"], ["D", "G"]) == 1.0
    assert krippendorff_alpha(["D", "D"], ["D", "D"]) is None


DIRECT, TRANSFERABLE, GAP = Classification.DIRECT, Classification.TRANSFERABLE, Classification.GAP
STRONG, MODERATE, NONE = Strength.STRONG, Strength.MODERATE, Strength.NONE
ROUND_CASES = (*range(1, SET_SIZE + 1), HELD_OUT)
CASE_7 = case_uuid(7)

type Label = tuple[Classification, Strength]


def _material(index: int, label: Label | None = None) -> MatchDecision:
    """Return case ``index``'s matrix decision, moved to ``label`` when one is given."""
    base = match_material(resolved_label(index, classification=label[0] if label else None))
    if label is None:
        return base
    return MatchDecision.model_validate({**base.model_dump(), "strength": label[1]})


def _matrix_label(index: int) -> Label:
    material = _material(index)
    return (material.classification, material.strength)


def _round(
    context: ReviewContext,
    *,
    moved: dict[int, Label] | None = None,
    split: dict[int, Label] | None = None,
    cases: tuple[int, ...] = ROUND_CASES,
    reviewers: tuple[str, str] = ("reviewer-a", "reviewer-b"),
) -> RelabelRound:
    """Return a round over ``cases``: ``moved`` changes both reviews, ``split`` only B's."""
    moved, split = moved or {}, split or {}
    rows = tuple(
        (case_uuid(index), _material(index, moved.get(index)), _material(index, split.get(index)))
        for index in cases
    )
    reviews = _decided_set(context, _packet(context, *(row[0] for row in rows)), rows, reviewers)
    labels = []
    # Finality reads the labels and both reviews, not how a split was settled.
    for review_a, review_b in reviews.paired_reviews():
        body = {
            "protocol_version": protocol_version(MATCH),
            "family": MATCH,
            "case_id": review_a.judgment.case_id,
            "material": review_a.judgment.material,
            "source": "agreement",
            "review_a_sha256": review_a.frozen_review_sha256,
            "review_b_sha256": review_b.frozen_review_sha256,
            "adjudication_sha256": None,
        }
        digest = domain_sha256(review_domain(MATCH, ReviewArtifact.RESOLVED_LABEL), body)
        labels.append(ResolvedLabel.model_validate({**body, "resolved_label_sha256": digest}))
    resolution: dict[str, Any] = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "review_set_sha256": reviews.review_set_sha256,
        "labels": tuple(labels),
        "adjudications": (),
    }
    digest = domain_sha256(review_domain(MATCH, ReviewArtifact.RESOLUTION_SET), resolution)
    return RelabelRound(
        context=context,
        reviews=reviews,
        labels=ResolutionSet.model_validate({**resolution, "resolution_set_sha256": digest}),
    )


def _previous(context: ReviewContext, **changes: Any) -> RelabelRound:
    """Return an earlier round of the same labels, reviewed by other agents."""
    return _round(context, reviewers=("earlier-reviewer-a", "earlier-reviewer-b"), **changes)


def _ruling(index: int, *accepted: Label) -> CaseRuling:
    return CaseRuling(
        case_id=case_uuid(index),
        accepted=tuple(
            AcceptedPair(classification=label, strength=grade) for label, grade in accepted
        ),
        basis="The rubric leaves this case open between these labels.",
    )


def _derived(
    labelled: RelabelRound, *rulings: CaseRuling, pool: CandidatePool | None = None
) -> tuple[SelectionManifest, CandidatePool, ExtractionQaSet]:
    """Return the manifest the selection path derives from one round's labels, and its inputs."""
    pool = pool or candidate_pool(labelled.context.review_context_sha256)
    qa = qa_set(pool)
    resolution = labelled.labels
    body = manifest_body(
        pool,
        resolution,
        qa=qa,
        selected=select_cases(pool, qa, resolution, previous_pool=None),
        held_out=select_held_out(pool, qa, resolution, previous_pool=None),
        rulings=rulings,
    )
    manifest = SelectionManifest(
        body=body, manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, body)
    )
    return manifest, pool, qa


def test_a_round_is_final_when_rubric_labels_and_agreement_all_hold() -> None:
    context = _context()

    current = _round(context)
    finality = round_finality(_previous(context), current, *_derived(current))

    assert finality.final
    assert finality.rubric_version_unchanged
    assert finality.changed_case_ids == ()
    assert finality.uncontested_alpha == 1.0
    assert finality.all_cases_alpha == 1.0


def test_the_same_round_passed_as_its_own_predecessor_is_refused() -> None:
    """Without a second review set, every shared label would repeat and read final."""
    context = _context()
    current = _round(context)
    derived = _derived(current)

    with pytest.raises(ValueError, match="same review set"):
        round_finality(_round(context), current, *derived)


def test_a_moved_label_keeps_the_round_open_unless_a_ruling_accepts_both() -> None:
    context = _context()
    previous = _previous(context, moved={1: (DIRECT, STRONG)})
    current = _round(context)

    finality = round_finality(previous, current, *_derived(current))
    ruled = round_finality(
        previous, current, *_derived(current, _ruling(1, (DIRECT, MODERATE), (DIRECT, STRONG)))
    )

    assert not finality.final
    assert finality.changed_case_ids == (CASE_A,)
    assert ruled.contested_case_ids == (CASE_A,)
    assert ruled.changed_case_ids == ()
    assert ruled.final


def test_a_ruled_case_whose_labels_leave_the_ruling_is_judged_as_settled() -> None:
    """A ruling exempts a case only while both rounds' labels are ones it accepts."""
    context = _context()
    previous = _previous(context, moved={7: (DIRECT, STRONG)})
    current = _round(context, split={7: (GAP, NONE)})
    ruling = _ruling(7, (TRANSFERABLE, MODERATE), (GAP, NONE))

    finality = round_finality(previous, current, *_derived(current, ruling))

    assert not finality.final
    assert finality.changed_case_ids == (CASE_7,)
    assert finality.contested_case_ids == ()
    assert finality.uncontested_alpha == finality.all_cases_alpha


def test_agreement_is_reported_over_all_cases_beside_the_uncontested_ones() -> None:
    context = _context()
    split = {
        index: (_matrix_label(index)[0], STRONG)
        for index in ROUND_CASES
        if _matrix_label(index)[1] is MODERATE
    }
    rulings = tuple(_ruling(index, _matrix_label(index), split[index]) for index in sorted(split))

    previous, current = _previous(context, split=split), _round(context, split=split)
    finality = round_finality(previous, current, *_derived(current))
    ruled = round_finality(previous, current, *_derived(current, *rulings))

    assert finality.changed_case_ids == ()
    assert finality.uncontested_alpha == finality.all_cases_alpha
    assert not finality.final
    assert ruled.uncontested_alpha == 1.0
    assert ruled.all_cases_alpha == finality.all_cases_alpha
    assert ruled.final


def test_a_case_new_to_the_round_is_provisional_and_does_not_hold_it_open() -> None:
    context = _context()
    previous = _previous(context, cases=tuple(index for index in ROUND_CASES if index != 2))

    current = _round(context, split={2: (GAP, NONE)})
    finality = round_finality(previous, current, *_derived(current))

    assert finality.provisional_case_ids == (CASE_B,)
    assert finality.changed_case_ids == ()
    assert finality.uncontested_alpha == 1.0
    assert finality.final


def test_a_case_dropped_since_the_previous_round_is_reported() -> None:
    context = _context()
    previous = _previous(context, cases=(*ROUND_CASES, 99))

    current = _round(context)
    finality = round_finality(previous, current, *_derived(current))

    assert finality.dropped_case_ids == (case_uuid(99),)
    assert finality.final


def test_a_new_rubric_version_keeps_the_round_open() -> None:
    previous_context = _context(rubric_version="synthetic-rubric-v1")
    current_context = _context(
        rubric_version="synthetic-rubric-v2", rubric_text="Judge from the synthetic context alone."
    )

    current = _round(current_context)
    finality = round_finality(_previous(previous_context), current, *_derived(current))

    assert not finality.rubric_version_unchanged
    assert not finality.final


def test_a_rubric_edited_under_its_version_refuses_the_round() -> None:
    context = _context()
    edited = _context(rubric_text="Judge from the synthetic context alone.")
    current = _round(edited)
    derived = _derived(current)

    with pytest.raises(ValueError, match="without a rubric_version change"):
        round_finality(_previous(context), current, *derived)


def test_a_previous_round_whose_reviews_used_another_context_is_refused() -> None:
    context = _context()
    other = _context(rubric_version="synthetic-rubric-v9")
    previous = _previous(context)
    mismatched = RelabelRound(
        context=context, reviews=_round(other).reviews, labels=previous.labels
    )

    current = _round(context)
    derived = _derived(current)

    with pytest.raises(ValueError, match="previous round"):
        round_finality(mismatched, current, *derived)


def test_a_manifest_derived_from_another_rounds_labels_is_refused() -> None:
    context = _context()
    previous = _previous(context, moved={1: (DIRECT, STRONG)})
    current = _round(context)
    derived = _derived(previous)

    with pytest.raises(ValueError, match="does not reference the resolution set"):
        round_finality(previous, current, *derived)


def test_a_pool_built_for_another_review_context_is_refused() -> None:
    """The manifest binds the pool, so only the round's own context can catch this."""
    context = _context()
    current = _round(context)
    derived = _derived(
        current, pool=candidate_pool(_context(rubric_text="Other.").review_context_sha256)
    )

    with pytest.raises(ValueError, match="is not the current round's"):
        round_finality(_previous(context), current, *derived)


def test_a_ruling_the_manifest_dataset_does_not_carry_is_refused() -> None:
    """A ruling written into the manifest alone, not its bound dataset, contests nothing."""
    context = _context()
    previous = _previous(context, moved={1: (DIRECT, STRONG)})
    current = _round(context)
    manifest, pool, qa = _derived(current)
    body = manifest.body.model_copy(
        update={"rulings": (_ruling(1, (DIRECT, MODERATE), (DIRECT, STRONG)),)}
    )
    edited = SelectionManifest(
        body=body, manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, body)
    )

    with pytest.raises(ValueError, match="dataset_sha256 does not match"):
        round_finality(previous, current, edited, pool, qa)


def test_agreement_resolves_without_adjudication() -> None:
    context = _context()
    packet = _packet(context, CASE_A)
    a, b, receipt_a, receipt_b = _review_pair(context, packet)
    review_set = _review_set(packet, context, a, b)
    label = _label("agreement", a, b, a.judgment.material)
    label.validate_inputs(a, b, None)
    with pytest.raises(ValidationError, match="at least 1 item"):
        _adjudication(context, a, b)
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "review_set_sha256": review_set.review_set_sha256,
        "labels": (label,),
        "adjudications": (),
    }
    resolution = ResolutionSet.model_validate(
        {
            **body,
            "resolution_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLUTION_SET), body
            ),
        }
    )
    resolution.validate_inputs(review_set, context, (receipt_a, receipt_b))
    with pytest.raises(ValidationError, match="resolution_set_sha256"):
        ResolutionSet.model_validate(
            {**resolution.model_dump(mode="python"), "resolution_set_sha256": HASH_A}
        )


def test_material_disagreement_requires_a_distinct_agent_adjudication() -> None:
    context = _context()
    packet = _packet(context, CASE_A)
    gap = _judgment(
        context,
        classification=Classification.GAP,
        challenge_class=ChallengeClass.ABSENT_EVIDENCE,
        evidence=(),
    )
    a, b, receipt_a, receipt_b = _review_pair(context, packet, gap)
    review_set = _review_set(packet, context, a, b)
    with pytest.raises(ValueError, match="requires adjudication"):
        _label("agreement", a, b, a.judgment.material).validate_inputs(a, b, None)
    adjudication, receipt = _adjudication(context, a, b)
    label = _label("adjudication", a, b, adjudication.decision.material, adjudication)
    body = {
        "protocol_version": protocol_version(MATCH),
        "family": MATCH,
        "review_set_sha256": review_set.review_set_sha256,
        "labels": (label,),
        "adjudications": (adjudication,),
    }
    resolution = ResolutionSet.model_validate(
        {
            **body,
            "resolution_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLUTION_SET), body
            ),
        }
    )
    resolution.validate_inputs(review_set, context, (receipt_a, receipt_b, receipt))
    reused_agent = _receipt(
        Operation.ADJUDICATE,
        agent_id=receipt_a.agent_id,
        input_sha256=adjudication.input_sha256,
        output_sha256=adjudication.decision.decision_sha256,
    )
    reused_body = adjudication.model_dump(
        mode="python", exclude={"operational_receipt_sha256", "adjudication_sha256"}
    )
    reused_body["operational_receipt_sha256"] = reused_agent.receipt_sha256
    reused = Adjudication.model_validate(
        {
            **reused_body,
            "adjudication_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.ADJUDICATION), reused_body
            ),
        }
    )
    reused_label = _label("adjudication", a, b, reused.decision.material, reused)
    reused_set_body = {
        **body,
        "labels": (reused_label,),
        "adjudications": (reused,),
    }
    reused_set = ResolutionSet.model_validate(
        {
            **reused_set_body,
            "resolution_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLUTION_SET), reused_set_body
            ),
        }
    )
    with pytest.raises(ValueError, match="distinct from both reviewer agents"):
        reused_set.validate_inputs(review_set, context, (receipt_a, receipt_b, reused_agent))


def test_a_recorded_pass_with_no_disagreement_is_not_an_absent_record() -> None:
    """Zero disagreements is something two reviewers established; absence is nobody's record."""
    recorded = _provenance()

    assert recorded.disagreement_count == 0
    assert recorded.adjudicator is None
    assert recorded.disagreements == ()


def test_a_recorded_disagreement_names_the_fields_and_who_settled_them() -> None:
    """The adjudicator may be a person, so the record states which kind settled the split."""
    recorded = _provenance(
        disagreements=("hard_condition", "recommendation"),
        adjudicator="the maintainer",
        adjudicator_kind="maintainer",
    )

    assert recorded.disagreement_count == 2
    assert recorded.adjudicator_kind == "maintainer"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"disagreements": ("recommendation",)}, "recorded exactly for material disagreements"),
        (
            {"adjudicator": "the maintainer", "adjudicator_kind": "maintainer"},
            "recorded exactly for material disagreements",
        ),
        (
            {
                "disagreements": ("recommendation",),
                "adjudicator": "labeller-a",
                "adjudicator_kind": "agent",
            },
            "distinct from both reviewers",
        ),
        ({"reviewer_b": "labeller-a"}, "must be distinct"),
        (
            {"disagreements": ("recommendation", "recommendation")},
            "must be unique",
        ),
        (
            {
                "disagreements": ("classification",),
                "adjudicator": "the maintainer",
                "adjudicator_kind": "maintainer",
            },
            "does not decide",
        ),
        (
            {
                "disagreements": ("recommendation", "hard_condition"),
                "adjudicator": "the maintainer",
                "adjudicator_kind": "maintainer",
            },
            "canonically ordered",
        ),
        ({"review_a_sha256": HASH_A}, "both review digests"),
        (
            {"review_a_sha256": HASH_A, "review_b_sha256": HASH_B, "adjudication_sha256": HASH_A},
            "unadjudicated row cannot name an adjudication digest",
        ),
        ({"resolved_label_sha256": HASH_A}, "requires both hash-bound reviews"),
    ],
)
def test_a_half_written_provenance_record_is_refused(changes: dict[str, Any], message: str) -> None:
    """A placeholder record would claim a procedure nobody ran."""
    with pytest.raises(ValidationError, match=message):
        _provenance(**changes)


def test_a_record_that_never_states_its_disagreements_is_refused() -> None:
    """Silence is not a recorded pass on which the two reviewers differed on nothing."""
    values = {
        name: value for name, value in _provenance().model_dump().items() if name != "disagreements"
    }

    with pytest.raises(ValidationError, match="disagreements"):
        LabelProvenance.model_validate(values)
