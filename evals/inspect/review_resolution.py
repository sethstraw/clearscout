"""Hash-bound judgments and adjudication for blinded labeling reviews.

One protocol serves several evaluation families. A family names both the hash
domain and the material decision its reviewers record, so an artifact belonging
to one family cannot validate as another family's.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.inspect.labeling import (
    PACKETED_FAMILIES,
    BoundedText,
    CandidatePool,
    ExtractionQaSet,
    NonBlank,
    Operation,
    OperationalReceipt,
    ProtocolVersion,
    ReviewContext,
    ReviewerPacket,
    ReviewFamily,
    Sha256,
    domain_sha256,
    protocol_version,
    require_frozen_rubric,
)
from tailorcv.application.discovery.relevance_review import ALLOWED_VERDICTS
from tailorcv.application.job_search.models import (
    RelevanceReviewCoreWorkAlignment,
    RelevanceReviewDecisiveBasis,
    RelevanceReviewHardCondition,
    RelevanceReviewQualificationAlignment,
    RelevanceReviewVerdict,
)
from tailorcv.application.tailoring.stages.jobspec import Dimension
from tailorcv.application.tailoring.stages.match import Classification, Strength

if TYPE_CHECKING:
    from evals.inspect.selection import SelectionManifest

type ReviewerRole = Literal["A", "B"]
type ResolutionSource = Literal["agreement", "adjudication"]
type AdjudicatorKind = Literal["agent", "maintainer"]


class ReviewArtifact(StrEnum):
    """The artifact kinds the protocol keeps in separate hash domains."""

    JUDGMENT = "judgment"
    FROZEN_REVIEW = "frozen-review"
    REVIEW_SET = "judgment-set"
    ADJUDICATION_INPUT = "adjudication-input"
    ADJUDICATION_DECISION = "adjudication-decision"
    ADJUDICATION = "adjudication"
    RESOLVED_LABEL = "resolved-label"
    RESOLUTION_SET = "resolution-set"


def review_domain(family: ReviewFamily, artifact: ReviewArtifact) -> str:
    """Return the hash domain one artifact kind uses inside one family."""
    return f"tailorcv-{family.value}-{artifact.value}-v1"


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChallengeClass(StrEnum):
    """Input-defined strata used to keep distinct matching problems visible."""

    CLEAR_SUPPORTED = "clear_supported"
    BOILERPLATE_OR_STATUS = "boilerplate_or_status"
    SOFT_OR_TRANSFERABLE = "soft_or_transferable"
    ABSENT_EVIDENCE = "absent_evidence"
    # The closest evidence words the applicant's part in the requested work as
    # limited ("helped with", "contributed to"); "worked with" people is not this.
    PARTIAL_CONTRIBUTION = "partial_contribution"


class Confidence(StrEnum):
    """A reviewer's ordinal confidence in one rubric judgment."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BlindnessAttestation(_ArtifactModel):
    """Fail-closed statements required from every independent reviewer."""

    historical_outcomes_unseen: Literal[True]
    other_reviewer_output_unseen: Literal[True]
    evaluated_output_unseen: Literal[True]
    future_provider_identity_unseen: Literal[True]
    future_selection_unseen: Literal[True]
    untrusted_requirement_treated_as_data: Literal[True]


def _require_uuid4(value: UUID, name: str) -> None:
    if value.version != 4:
        raise ValueError(f"{name} must be a UUIDv4")


class MaterialDecision(_ArtifactModel):
    """The family-specific fields two blind reviewers must agree on."""

    family: ReviewFamily

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return this decision's material field names in canonical order."""
        return tuple(name for name in cls.model_fields if name != "family")

    @property
    def material_fields(self) -> tuple[str, ...]:
        """Return this family's material field names in canonical order."""
        return type(self).field_names()

    def validate_context(self, context: ReviewContext) -> None:
        """Require whatever this decision cites to exist in the frozen context."""
        del context


_EVIDENCE_CLASSIFICATIONS = frozenset(
    {Classification.DIRECT, Classification.TAILORING_ONLY, Classification.TRANSFERABLE}
)


class MatchDecision(MaterialDecision):
    """The material fields one match reviewer or adjudicator decides."""

    family: Literal[ReviewFamily.MATCH]
    classification: Classification
    strength: Strength
    dimension: Dimension
    challenge_class: ChallengeClass
    decisive_entry_ids: tuple[NonBlank, ...] = ()
    asserted_status_fact: NonBlank | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Enforce the evidence and strength a classification may and must carry."""
        if tuple(sorted(self.decisive_entry_ids)) != self.decisive_entry_ids:
            raise ValueError("decisive_entry_ids must be sorted")
        if len(self.decisive_entry_ids) != len(set(self.decisive_entry_ids)):
            raise ValueError("decisive_entry_ids must be unique")
        label = self.classification.value
        if self.classification in _EVIDENCE_CLASSIFICATIONS:
            if not self.decisive_entry_ids:
                raise ValueError(f"{label} requires decisive evidence")
            if self.strength is Strength.NONE:
                raise ValueError(f"{label} requires a strength other than none")
        else:
            if self.decisive_entry_ids:
                raise ValueError(f"{label} cannot cite inventory evidence")
            if self.strength is not Strength.NONE:
                raise ValueError(f"{label} must use strength none")
        if self.classification is Classification.ASSERTED:
            if self.dimension is not Dimension.HARD:
                raise ValueError("ASSERTED judgments must use the hard dimension")
            if self.asserted_status_fact is None:
                raise ValueError("ASSERTED judgments require an asserted_status_fact")
        elif self.asserted_status_fact is not None:
            raise ValueError("asserted_status_fact is valid only for ASSERTED judgments")
        return self

    def validate_context(self, context: ReviewContext) -> None:
        """Require cited evidence and status facts to exist in the frozen context."""
        if set(self.decisive_entry_ids) - set(context.valid_entry_ids):
            raise ValueError("decisive evidence is outside the frozen review context")
        if (
            self.asserted_status_fact is not None
            and self.asserted_status_fact not in context.asserted_status_lines
        ):
            raise ValueError("asserted_status_fact is outside the frozen review context")


class RelevanceDecision(MaterialDecision):
    """The material fields one relevance reviewer or adjudicator decides."""

    family: Literal[ReviewFamily.RELEVANCE_REVIEW]
    core_work_alignment: RelevanceReviewCoreWorkAlignment
    core_qualification_alignment: RelevanceReviewQualificationAlignment
    hard_condition: RelevanceReviewHardCondition
    decisive_basis: RelevanceReviewDecisiveBasis
    recommendation: RelevanceReviewVerdict

    @model_validator(mode="after")
    def validate_combination(self) -> Self:
        """Refuse axes and a recommendation the shipped class definitions never allow."""
        combination = (
            self.core_work_alignment,
            self.core_qualification_alignment,
            self.hard_condition,
            self.decisive_basis,
            self.recommendation,
        )
        if combination not in ALLOWED_VERDICTS:
            raise ValueError(
                "the axes and recommendation form no combination the stage allows: "
                + ", ".join(combination)
            )
        return self


type Material = Annotated[MatchDecision | RelevanceDecision, Field(discriminator="family")]

_MATERIAL_MODELS: dict[ReviewFamily, type[MaterialDecision]] = {
    ReviewFamily.MATCH: MatchDecision,
    ReviewFamily.RELEVANCE_REVIEW: RelevanceDecision,
}


def family_material_fields(family: ReviewFamily) -> tuple[str, ...]:
    """Return the material fields one family decides, in canonical order."""
    return _MATERIAL_MODELS[family].field_names()


def _validate_family(
    family: ReviewFamily, declared: ProtocolVersion, material: Material | None = None
) -> None:
    if declared != protocol_version(family):
        raise ValueError("protocol_version does not match the artifact family")
    if material is not None and material.family is not family:
        raise ValueError("decision family does not match the artifact family")


class ReviewJudgment(_ArtifactModel):
    """One independent reviewer's evidence-bound judgment for an opaque case."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    case_id: UUID
    material: Material
    rationale: BoundedText
    confidence: Confidence
    blindness: BlindnessAttestation
    judgment_sha256: Sha256

    @model_validator(mode="after")
    def validate_judgment(self) -> Self:
        """Bind the judgment to its family and content."""
        _require_uuid4(self.case_id, "case_id")
        _validate_family(self.family, self.protocol_version, self.material)
        body = self.model_dump(mode="json", exclude={"judgment_sha256"})
        if self.judgment_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.JUDGMENT), body
        ):
            raise ValueError("judgment_sha256 does not match review judgment content")
        return self

    def validate_context(self, context: ReviewContext) -> None:
        """Require cited evidence and status facts to exist in the frozen context."""
        self.material.validate_context(context)


class FrozenReview(_ArtifactModel):
    """An immutable judgment bound to its blind input and agent receipt."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    reviewer_role: ReviewerRole
    packet_sha256: Sha256 | None = None
    judgment: ReviewJudgment
    operational_receipt_sha256: Sha256
    frozen_review_sha256: Sha256

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        """Bind the frozen judgment to its role, packet, and receipt digest."""
        _validate_family(self.family, self.protocol_version)
        if self.judgment.family is not self.family:
            raise ValueError("frozen review judgment belongs to another family")
        if (self.packet_sha256 is None) is (self.family in PACKETED_FAMILIES):
            raise ValueError("a packet digest is recorded exactly for a family with a packet")
        body = self.model_dump(mode="json", exclude={"frozen_review_sha256"})
        if self.frozen_review_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.FROZEN_REVIEW), body
        ):
            raise ValueError("frozen_review_sha256 does not match frozen review content")
        return self

    def validate_inputs(
        self,
        packet: ReviewerPacket | None,
        context: ReviewContext,
        receipt: OperationalReceipt,
    ) -> None:
        """Resolve and validate this review's packet, context, and receipt."""
        if context.protocol_version != protocol_version(self.family):
            raise ValueError("review context belongs to another family")
        if (packet is None) is (self.family in PACKETED_FAMILIES):
            raise ValueError("a reviewer packet is supplied exactly for a family that has one")
        # A family with no packet reviews the frozen context itself, so that is
        # what its blind input digest resolves to.
        blind_input = context.review_context_sha256
        if packet is not None:
            blind_input = packet.packet_sha256
            if self.packet_sha256 != packet.packet_sha256:
                raise ValueError("frozen review does not reference the supplied packet")
            if packet.review_context_sha256 != context.review_context_sha256:
                raise ValueError("review packet does not reference the supplied context")
            if self.judgment.case_id not in {item.case_id for item in packet.cases}:
                raise ValueError("review judgment case is absent from its packet")
        self.judgment.validate_context(context)
        if self.operational_receipt_sha256 != receipt.receipt_sha256:
            raise ValueError("review receipt hash does not resolve to the supplied receipt")
        expected_operation = Operation.REVIEW_A if self.reviewer_role == "A" else Operation.REVIEW_B
        if receipt.operation is not expected_operation:
            raise ValueError("review receipt operation does not match reviewer role")
        if receipt.input_sha256 != blind_input:
            raise ValueError("review receipt input does not match the blind input")
        if receipt.output_sha256 != self.judgment.judgment_sha256:
            raise ValueError("review receipt output does not match review judgment")
        if not all(
            (
                receipt.attestations.source_scope_respected,
                receipt.attestations.historical_outcomes_unseen,
                receipt.attestations.evaluated_output_unseen,
            )
        ):
            raise ValueError("review receipt attestations must all be true")


def _ordered_reviews(
    reviews: tuple[FrozenReview, ...], role: ReviewerRole, family: ReviewFamily
) -> None:
    if not reviews:
        raise ValueError(f"reviewer {role} must supply at least one review")
    if any(review.reviewer_role != role for review in reviews):
        raise ValueError(f"reviewer {role} set contains the wrong reviewer role")
    if any(review.family is not family for review in reviews):
        raise ValueError(f"reviewer {role} set contains another family's review")
    if tuple(sorted(reviews, key=lambda item: str(item.judgment.case_id))) != reviews:
        raise ValueError(f"reviewer {role} records must be ordered by case_id")
    case_ids = [review.judgment.case_id for review in reviews]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError(f"reviewer {role} records contain duplicate case IDs")


def _receipt_index(receipts: Sequence[OperationalReceipt]) -> dict[str, OperationalReceipt]:
    by_hash = {receipt.receipt_sha256: receipt for receipt in receipts}
    if len(by_hash) != len(receipts):
        raise ValueError("supplied operational receipts contain duplicate hashes")
    return by_hash


class ReviewSet(_ArtifactModel):
    """Complete A and B reviews for the same approved blind case set."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    packet_a_sha256: Sha256
    packet_b_sha256: Sha256
    review_context_sha256: Sha256
    reviews_a: tuple[FrozenReview, ...] = Field(min_length=1)
    reviews_b: tuple[FrozenReview, ...] = Field(min_length=1)
    review_set_sha256: Sha256

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        """Require canonical, equal A/B coverage and bind the complete set."""
        _validate_family(self.family, self.protocol_version)
        if self.family not in PACKETED_FAMILIES:
            raise ValueError("a review set covers the reviewer packets its family has")
        _ordered_reviews(self.reviews_a, "A", self.family)
        _ordered_reviews(self.reviews_b, "B", self.family)
        if {item.judgment.case_id for item in self.reviews_a} != {
            item.judgment.case_id for item in self.reviews_b
        }:
            raise ValueError("reviewers A and B must cover the same cases")
        body = self.model_dump(mode="json", exclude={"review_set_sha256"})
        if self.review_set_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.REVIEW_SET), body
        ):
            raise ValueError("review_set_sha256 does not match review set content")
        return self

    def validate_inputs(
        self,
        packet_a: ReviewerPacket,
        packet_b: ReviewerPacket,
        context: ReviewContext,
        receipts: Sequence[OperationalReceipt],
    ) -> None:
        """Require exact packet coverage and distinct receipt-backed reviewer agents."""
        if self.packet_a_sha256 != packet_a.packet_sha256:
            raise ValueError("review set does not reference reviewer A packet")
        if self.packet_b_sha256 != packet_b.packet_sha256:
            raise ValueError("review set does not reference reviewer B packet")
        if self.review_context_sha256 != context.review_context_sha256:
            raise ValueError("review set does not reference the supplied context")
        approved = {item.case_id for item in packet_a.cases}
        if approved != {item.case_id for item in packet_b.cases}:
            raise ValueError("reviewer packets must contain the same approved cases")
        if approved != {item.judgment.case_id for item in self.reviews_a}:
            raise ValueError("review set must cover every approved case exactly once")
        by_hash = _receipt_index(receipts)
        agents: dict[ReviewerRole, set[str]] = {"A": set(), "B": set()}
        review_groups: tuple[tuple[ReviewerRole, ReviewerPacket, tuple[FrozenReview, ...]], ...] = (
            ("A", packet_a, self.reviews_a),
            ("B", packet_b, self.reviews_b),
        )
        for role, packet, reviews in review_groups:
            for review in reviews:
                receipt = by_hash.get(review.operational_receipt_sha256)
                if receipt is None:
                    raise ValueError("review receipt hash does not resolve")
                review.validate_inputs(packet, context, receipt)
                agents[role].add(receipt.agent_id)
        if len(agents["A"]) != 1 or len(agents["B"]) != 1:
            raise ValueError("each reviewer role must resolve to exactly one agent")
        if agents["A"] == agents["B"]:
            raise ValueError("reviewer A and reviewer B must be distinct agents")

    def paired_reviews(self) -> tuple[tuple[FrozenReview, FrozenReview], ...]:
        """Return A/B reviews paired in canonical case order."""
        reviews_b = {item.judgment.case_id: item for item in self.reviews_b}
        return tuple((item, reviews_b[item.judgment.case_id]) for item in self.reviews_a)


def material_disagreements(review_a: ReviewJudgment, review_b: ReviewJudgment) -> tuple[str, ...]:
    """Return the ordered material fields that require adjudication."""
    if review_a.family is not review_b.family:
        raise ValueError("paired reviews must belong to the same family")
    return tuple(
        field
        for field in review_a.material.material_fields
        if getattr(review_a.material, field) != getattr(review_b.material, field)
    )


@dataclass(frozen=True)
class FieldAgreement:
    """Reviewer agreement on one material field across paired blinded reviews."""

    field: str
    pairs: int
    disagreements: int
    cohen_kappa: float | None


def field_agreement(review_set: ReviewSet, field: str) -> FieldAgreement:
    """Report the raw disagreement count on one field beside Cohen's kappa for A against B."""
    pairs = review_set.paired_reviews()
    values_a = [_decision_value(review_a, field) for review_a, _ in pairs]
    values_b = [_decision_value(review_b, field) for _, review_b in pairs]
    disagreements = sum(a != b for a, b in zip(values_a, values_b, strict=True))
    return FieldAgreement(field, len(pairs), disagreements, _cohen_kappa(values_a, values_b))


def _decision_value(review: FrozenReview, field: str) -> Hashable:
    material = review.judgment.material
    if field not in material.material_fields:
        raise ValueError(f"{field} is not a material field of the {material.family.value} family")
    value: Hashable = getattr(material, field)
    return value


def _cohen_kappa(values_a: Sequence[Hashable], values_b: Sequence[Hashable]) -> float | None:
    """Return Cohen's kappa for two raters, or None when chance agreement is total."""
    total = len(values_a)
    if total == 0:
        return None
    observed = sum(a == b for a, b in zip(values_a, values_b, strict=True)) / total
    expected = sum(
        values_a.count(value) / total * (values_b.count(value) / total)
        for value in set(values_a) | set(values_b)
    )
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


FINAL_ROUND_ALPHA = 0.8


def krippendorff_alpha(values_a: Sequence[Hashable], values_b: Sequence[Hashable]) -> float | None:
    """Return nominal Krippendorff's alpha for two raters who both rated every item.

    None means no value varies, so agreement cannot be told from chance.
    """
    pairable = 2 * len(values_a)
    counts = Counter([*values_a, *values_b])
    expected_pairs = pairable * pairable - sum(count * count for count in counts.values())
    if expected_pairs == 0:
        return None
    disagreements = sum(a != b for a, b in zip(values_a, values_b, strict=True))
    return 1.0 - (pairable - 1) * 2 * disagreements / expected_pairs


type AcceptedLabel = tuple[str | None, str | None]


def _label_values(value: object, fold: Callable[[str], str]) -> tuple[str | None, ...]:
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"an accepted-label set must name non-empty labels: {value!r}")
        return tuple(fold(item.strip()) for item in value)
    return ((fold(value.strip()) or None) if isinstance(value, str) else None,)


def accepted_labels(classification: object, strength: object) -> tuple[AcceptedLabel, ...]:
    """Return the classification and strength pairs one frozen match row accepts.

    A contested row lists its accepted set. Two lists pair index by index, a single
    value pairs with every member of the other list, and every pair is a rubric label.
    """
    classes = _label_values(classification, str.upper)
    strengths = _label_values(strength, str.lower)
    if isinstance(classification, list) and isinstance(strength, list):
        if len(classes) != len(strengths):
            raise ValueError("an accepted-label set pairs each classification with one strength")
        pairs = tuple(zip(classes, strengths, strict=True))
    elif isinstance(strength, list):
        pairs = tuple((classes[0], item) for item in strengths)
    else:
        pairs = tuple((item, strengths[0]) for item in classes)
    for label, grade in pairs:
        _require_rubric_pair(label, grade)
    return pairs


def _require_rubric_pair(label: str | None, grade: str | None) -> None:
    """Refuse an accepted label outside the rubric or a strength it never pairs with."""
    if label not in set(Classification) or (grade is not None and grade not in set(Strength)):
        raise ValueError(f"an accepted label names no rubric value: {label}/{grade}")
    if grade is not None and (Classification(label) in _EVIDENCE_CLASSIFICATIONS) == (
        grade == Strength.NONE
    ):
        raise ValueError(f"an accepted label pairs {label} with strength {grade}")


class AcceptedPair(_ArtifactModel):
    """One classification and strength a contested case accepts."""

    classification: Classification
    strength: Strength

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        """Refuse a strength the rubric never pairs with the classification."""
        _require_rubric_pair(self.classification, self.strength)
        return self


class CaseRuling(_ArtifactModel):
    """A ruling that a contested case accepts more than one label, with its basis."""

    case_id: UUID
    accepted: tuple[AcceptedPair, ...] = Field(min_length=2)
    basis: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def validate_ruling(self) -> Self:
        """Require an opaque case id and distinct accepted pairs."""
        _require_uuid4(self.case_id, "case_id")
        if len(set(self.accepted)) != len(self.accepted):
            raise ValueError("a ruling accepts each classification and strength pair once")
        return self


@dataclass(frozen=True)
class RelabelRound:
    """One relabeling round: its review context, both blind reviews, and the labels."""

    context: ReviewContext
    reviews: ReviewSet
    labels: ResolutionSet

    def scored_labels(self, name: str) -> dict[UUID, tuple[Hashable, ...]]:
        """Return each case's scored label after checking the round's artifacts bind."""
        if self.reviews.review_context_sha256 != self.context.review_context_sha256:
            raise ValueError(f"the {name} round's reviews do not reference its review context")
        if self.labels.review_set_sha256 != self.reviews.review_set_sha256:
            raise ValueError(f"the {name} round's labels do not resolve its reviews")
        return {item.case_id: _scored_label(item.material) for item in self.labels.labels}


@dataclass(frozen=True)
class RoundFinality:
    """Whether a relabeling round settles its labels, with the evidence for each condition.

    Finality is judged over the cases both rounds labeled. A case new to this round
    is provisional until a later round repeats its label.
    """

    rubric_version_unchanged: bool
    changed_case_ids: tuple[UUID, ...]
    provisional_case_ids: tuple[UUID, ...]
    dropped_case_ids: tuple[UUID, ...]
    contested_case_ids: tuple[UUID, ...]
    uncontested_alpha: float | None
    all_cases_alpha: float | None

    @property
    def final(self) -> bool:
        """Return whether the rubric held, shared uncontested labels repeated, and agreed."""
        return (
            self.rubric_version_unchanged
            and not self.changed_case_ids
            and self.uncontested_alpha is not None
            and self.uncontested_alpha >= FINAL_ROUND_ALPHA
        )


def _scored_label(material: Material) -> tuple[Hashable, ...]:
    """Return the classification and strength a frozen match row scores."""
    if not isinstance(material, MatchDecision):
        raise ValueError("round finality compares match labels")
    return (material.classification, material.strength)


def _alpha(reviews: ReviewSet, cases: frozenset[UUID] | None = None) -> float | None:
    pairs = [
        (_scored_label(a.judgment.material), _scored_label(b.judgment.material))
        for a, b in reviews.paired_reviews()
        if cases is None or a.judgment.case_id in cases
    ]
    return krippendorff_alpha([a for a, _ in pairs], [b for _, b in pairs])


def round_finality(
    previous: RelabelRound,
    current: RelabelRound,
    manifest: SelectionManifest,
    pool: CandidatePool,
    qa: ExtractionQaSet,
) -> RoundFinality:
    """Judge a relabeling round against the one before it.

    ``manifest`` must select from the current round's labels. A shared case it
    carries a ruling for is contested, neither repeating nor counting toward
    agreement, only while both rounds' labels stay inside the ruling.
    """
    require_frozen_rubric(previous.context, current.context)
    if previous.reviews.review_set_sha256 == current.reviews.review_set_sha256:
        raise ValueError("the previous and current rounds are the same review set")
    before = previous.scored_labels("previous")
    labels = current.scored_labels("current")
    if pool.review_context_sha256 != current.context.review_context_sha256:
        raise ValueError(
            f"the candidate pool's review context {pool.review_context_sha256} is not the"
            f" current round's {current.context.review_context_sha256}"
        )
    manifest.validate_inputs(pool, qa, current.labels)
    accepted = {
        ruling.case_id: {(pair.classification, pair.strength) for pair in ruling.accepted}
        for ruling in manifest.body.rulings
    }
    shared = frozenset(labels.keys() & before.keys())
    contested = frozenset(
        case
        for case in shared & accepted.keys()
        if before[case] in accepted[case] and labels[case] in accepted[case]
    )
    settled = shared - contested
    return RoundFinality(
        rubric_version_unchanged=previous.context.rubric_version == current.context.rubric_version,
        changed_case_ids=tuple(
            case for case in labels if case in settled and before[case] != labels[case]
        ),
        provisional_case_ids=tuple(case for case in labels if case not in before),
        dropped_case_ids=tuple(case for case in before if case not in labels),
        contested_case_ids=tuple(case for case in labels if case in contested),
        uncontested_alpha=_alpha(current.reviews, settled),
        all_cases_alpha=_alpha(current.reviews),
    )


class AdjudicationDecision(_ArtifactModel):
    """The adjudicator's resolved material fields for one disagreement."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    case_id: UUID
    material: Material
    rationale: BoundedText
    confidence: Confidence
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Bind the decision to its family and content."""
        _require_uuid4(self.case_id, "case_id")
        _validate_family(self.family, self.protocol_version, self.material)
        body = self.model_dump(mode="json", exclude={"decision_sha256"})
        if self.decision_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.ADJUDICATION_DECISION), body
        ):
            raise ValueError("decision_sha256 does not match adjudication decision content")
        return self

    def validate_context(self, context: ReviewContext) -> None:
        """Require cited evidence and status facts to exist in the frozen context."""
        self.material.validate_context(context)


def adjudication_input_sha256(review_a: FrozenReview, review_b: FrozenReview) -> str:
    """Bind the exact two frozen reviews supplied to an adjudicator."""
    if review_a.family is not review_b.family:
        raise ValueError("paired reviews must belong to the same family")
    return domain_sha256(
        review_domain(review_a.family, ReviewArtifact.ADJUDICATION_INPUT),
        {
            "review_a_sha256": review_a.frozen_review_sha256,
            "review_b_sha256": review_b.frozen_review_sha256,
        },
    )


class Adjudication(_ArtifactModel):
    """A material disagreement bound to a third agent's resolution receipt."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    case_id: UUID
    review_a_sha256: Sha256
    review_b_sha256: Sha256
    disagreements: tuple[NonBlank, ...] = Field(min_length=1)
    input_sha256: Sha256
    decision: AdjudicationDecision
    operational_receipt_sha256: Sha256
    adjudication_sha256: Sha256

    @model_validator(mode="after")
    def validate_adjudication(self) -> Self:
        """Bind one canonically ordered material-disagreement resolution."""
        _require_uuid4(self.case_id, "case_id")
        _validate_family(self.family, self.protocol_version)
        if self.decision.family is not self.family:
            raise ValueError("adjudication decision belongs to another family")
        if self.decision.case_id != self.case_id:
            raise ValueError("adjudication decision case does not match")
        named = set(self.disagreements)
        ordered = tuple(field for field in self.decision.material.material_fields if field in named)
        if self.disagreements != ordered or len(self.disagreements) != len(named):
            raise ValueError("adjudication disagreements must be unique and canonically ordered")
        body = self.model_dump(mode="json", exclude={"adjudication_sha256"})
        if self.adjudication_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.ADJUDICATION), body
        ):
            raise ValueError("adjudication_sha256 does not match adjudication content")
        return self

    def validate_inputs(
        self,
        review_a: FrozenReview,
        review_b: FrozenReview,
        context: ReviewContext,
        receipt: OperationalReceipt,
    ) -> None:
        """Require a real disagreement and resolve the adjudicator receipt."""
        if review_a.judgment.case_id != self.case_id or review_b.judgment.case_id != self.case_id:
            raise ValueError("adjudication reviews do not match its case")
        if (
            self.review_a_sha256 != review_a.frozen_review_sha256
            or self.review_b_sha256 != review_b.frozen_review_sha256
        ):
            raise ValueError("adjudication does not reference the supplied reviews")
        disagreements = material_disagreements(review_a.judgment, review_b.judgment)
        if not disagreements or self.disagreements != disagreements:
            raise ValueError("adjudication is required exactly for material disagreements")
        expected_input = adjudication_input_sha256(review_a, review_b)
        if self.input_sha256 != expected_input:
            raise ValueError("adjudication input hash does not match the frozen reviews")
        self.decision.validate_context(context)
        if self.operational_receipt_sha256 != receipt.receipt_sha256:
            raise ValueError("adjudication receipt hash does not resolve to the supplied receipt")
        if receipt.operation is not Operation.ADJUDICATE:
            raise ValueError("adjudication receipt has the wrong operation")
        if receipt.input_sha256 != self.input_sha256:
            raise ValueError("adjudication receipt input does not match")
        if receipt.output_sha256 != self.decision.decision_sha256:
            raise ValueError("adjudication receipt output does not match")
        if not all(
            (
                receipt.attestations.source_scope_respected,
                receipt.attestations.historical_outcomes_unseen,
                receipt.attestations.evaluated_output_unseen,
            )
        ):
            raise ValueError("adjudication receipt attestations must all be true")


class ResolvedLabel(_ArtifactModel):
    """The material label derived from reviewer agreement or adjudication."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    case_id: UUID
    material: Material
    source: ResolutionSource
    review_a_sha256: Sha256
    review_b_sha256: Sha256
    adjudication_sha256: Sha256 | None = None
    resolved_label_sha256: Sha256

    @model_validator(mode="after")
    def validate_label(self) -> Self:
        """Bind the material label and require one valid resolution source."""
        _require_uuid4(self.case_id, "case_id")
        _validate_family(self.family, self.protocol_version, self.material)
        if (self.source == "agreement") != (self.adjudication_sha256 is None):
            raise ValueError("resolved label source and adjudication hash disagree")
        body = self.model_dump(mode="json", exclude={"resolved_label_sha256"})
        if self.resolved_label_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.RESOLVED_LABEL), body
        ):
            raise ValueError("resolved_label_sha256 does not match resolved label content")
        return self

    def validate_inputs(
        self,
        review_a: FrozenReview,
        review_b: FrozenReview,
        adjudication: Adjudication | None,
    ) -> None:
        """Derive the label exactly from agreement or a supplied adjudication."""
        if self.case_id != review_a.judgment.case_id or self.case_id != review_b.judgment.case_id:
            raise ValueError("resolved label reviews do not match its case")
        if (
            self.review_a_sha256 != review_a.frozen_review_sha256
            or self.review_b_sha256 != review_b.frozen_review_sha256
        ):
            raise ValueError("resolved label does not reference the supplied reviews")
        disagreements = material_disagreements(review_a.judgment, review_b.judgment)
        if not disagreements:
            if adjudication is not None or self.source != "agreement":
                raise ValueError("reviewer agreement must resolve without adjudication")
            expected = review_a.judgment.material
        else:
            if adjudication is None or self.source != "adjudication":
                raise ValueError("material disagreement requires adjudication")
            if self.adjudication_sha256 != adjudication.adjudication_sha256:
                raise ValueError("resolved label does not reference the supplied adjudication")
            expected = adjudication.decision.material
        if self.material != expected:
            raise ValueError("resolved label differs from its resolution source")


def match_material(label: ResolvedLabel) -> MatchDecision:
    """Return a resolved label's match decision, refusing another family's label."""
    if not isinstance(label.material, MatchDecision):
        raise ValueError("this resolved label does not carry a match decision")
    return label.material


class ResolutionSet(_ArtifactModel):
    """Exact resolved coverage of a frozen review set and its adjudications."""

    protocol_version: ProtocolVersion
    family: ReviewFamily
    review_set_sha256: Sha256
    labels: tuple[ResolvedLabel, ...] = Field(min_length=1)
    adjudications: tuple[Adjudication, ...] = ()
    resolution_set_sha256: Sha256

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        """Require canonical unique coverage and bind the complete resolution."""
        _validate_family(self.family, self.protocol_version)
        families = {item.family for item in self.labels} | {
            item.family for item in self.adjudications
        }
        if families != {self.family}:
            raise ValueError("resolution set mixes families")
        if tuple(sorted(self.labels, key=lambda item: str(item.case_id))) != self.labels:
            raise ValueError("resolved labels must be ordered by case_id")
        ordered_adjudications = tuple(
            sorted(self.adjudications, key=lambda item: str(item.case_id))
        )
        if ordered_adjudications != self.adjudications:
            raise ValueError("adjudications must be ordered by case_id")
        label_ids = [item.case_id for item in self.labels]
        adjudication_ids = [item.case_id for item in self.adjudications]
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("resolution set contains duplicate label cases")
        if len(adjudication_ids) != len(set(adjudication_ids)):
            raise ValueError("resolution set contains duplicate adjudication cases")
        if set(adjudication_ids) != {
            item.case_id for item in self.labels if item.source == "adjudication"
        }:
            raise ValueError("adjudications must exactly match adjudicated labels")
        body = self.model_dump(mode="json", exclude={"resolution_set_sha256"})
        if self.resolution_set_sha256 != domain_sha256(
            review_domain(self.family, ReviewArtifact.RESOLUTION_SET), body
        ):
            raise ValueError("resolution_set_sha256 does not match resolution set content")
        return self

    def validate_inputs(
        self,
        review_set: ReviewSet,
        context: ReviewContext,
        receipts: Sequence[OperationalReceipt],
    ) -> None:
        """Resolve every review and adjudication against provenance records."""
        if self.review_set_sha256 != review_set.review_set_sha256:
            raise ValueError("resolution set does not reference the supplied review set")
        pairs = {
            item.judgment.case_id: (item, other) for item, other in review_set.paired_reviews()
        }
        if set(pairs) != {item.case_id for item in self.labels}:
            raise ValueError("resolution set must cover every reviewed case exactly once")
        adjudications = {item.case_id: item for item in self.adjudications}
        by_hash = _receipt_index(receipts)
        reviewer_receipts = (*review_set.reviews_a, *review_set.reviews_b)
        if any(item.operational_receipt_sha256 not in by_hash for item in reviewer_receipts):
            raise ValueError("review receipt hash does not resolve")
        reviewer_agents = {
            by_hash[item.operational_receipt_sha256].agent_id for item in reviewer_receipts
        }
        for label in self.labels:
            review_a, review_b = pairs[label.case_id]
            adjudication = adjudications.get(label.case_id)
            label.validate_inputs(review_a, review_b, adjudication)
            if adjudication is None:
                continue
            receipt = by_hash.get(adjudication.operational_receipt_sha256)
            if receipt is None:
                raise ValueError("adjudication receipt hash does not resolve")
            adjudication.validate_inputs(review_a, review_b, context, receipt)
            if receipt.agent_id in reviewer_agents:
                raise ValueError("adjudicator must be distinct from both reviewer agents")


class LabelProvenance(_ArtifactModel):
    """The blinded review record behind one frozen scoring row's label.

    A row that carries no record at all is a row nobody has recorded this for.
    A record states ``disagreements`` itself, and an empty tuple is the opposite
    fact: a recorded pass where the two reviewers differed on nothing.
    """

    protocol_version: ProtocolVersion
    family: ReviewFamily
    rubric_version: NonBlank
    reviewer_a: NonBlank
    reviewer_b: NonBlank
    disagreements: tuple[NonBlank, ...]
    adjudicator: NonBlank | None = None
    adjudicator_kind: AdjudicatorKind | None = None
    review_a_sha256: Sha256 | None = None
    review_b_sha256: Sha256 | None = None
    adjudication_sha256: Sha256 | None = None
    resolved_label_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        """Require a complete record: adjudication exactly where the reviewers differed."""
        _validate_family(self.family, self.protocol_version)
        if self.reviewer_a == self.reviewer_b:
            raise ValueError("reviewers A and B must be distinct")
        fields = family_material_fields(self.family)
        unknown = sorted(set(self.disagreements) - set(fields))
        if unknown:
            raise ValueError(f"the {self.family.value} family does not decide {', '.join(unknown)}")
        named = set(self.disagreements)
        if self.disagreements != tuple(field for field in fields if field in named):
            raise ValueError("recorded disagreements must be unique and canonically ordered")
        adjudicated = bool(self.disagreements)
        if adjudicated != (self.adjudicator is not None):
            raise ValueError("adjudication is recorded exactly for material disagreements")
        if (self.adjudicator is None) != (self.adjudicator_kind is None):
            raise ValueError("an adjudicator needs both an identity and a kind")
        if self.adjudicator in {self.reviewer_a, self.reviewer_b}:
            raise ValueError("the adjudicator must be distinct from both reviewers")
        if not adjudicated and self.adjudication_sha256 is not None:
            raise ValueError("an unadjudicated row cannot name an adjudication digest")
        bound = (self.review_a_sha256 is not None) and (self.review_b_sha256 is not None)
        if (self.review_a_sha256 is None) != (self.review_b_sha256 is None):
            raise ValueError("both review digests are recorded, or neither is")
        if not bound and (self.adjudication_sha256 or self.resolved_label_sha256):
            raise ValueError("a protocol digest requires both hash-bound reviews")
        return self

    @property
    def disagreement_count(self) -> int:
        """Return how many material fields the two blind reviews differed on."""
        return len(self.disagreements)

    def score_metadata(self) -> dict[str, Any]:
        """Return the record as a score reports it, beside its derived count."""
        return {**self.model_dump(mode="json"), "disagreement_count": self.disagreement_count}


def row_provenance(row: Mapping[str, Any], family: ReviewFamily) -> LabelProvenance | None:
    """Return one frozen scoring row's review record, or None when it carries none."""
    record = row.get("provenance")
    if record is None:
        return None
    # The row comes from JSONL, so the strict record is validated as JSON.
    provenance = LabelProvenance.model_validate_json(json.dumps(record))
    if provenance.family is not family:
        raise ValueError("provenance names another review family")
    return provenance
