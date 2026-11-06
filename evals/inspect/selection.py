"""Deterministic selection and minimal dataset projection for match evaluation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clearscout.application.tailoring.stages.jobspec import Dimension, Importance
from clearscout.application.tailoring.stages.match import Classification, Strength
from evals.inspect.labeling import (
    PROTOCOL_VERSION,
    CandidateContent,
    CandidatePool,
    ExtractionQaSet,
    InventoryHash,
    NonBlank,
    ProtocolVersion,
    Sha256,
    Split,
    bytes_sha256,
    canonical_json_bytes,
    domain_sha256,
)
from evals.inspect.review_resolution import (
    CaseRuling,
    ChallengeClass,
    ResolutionSet,
    ResolvedLabel,
    match_material,
)

SELECTION_KEY_DOMAIN = "clearscout-match-selection-key-v1"
SPLIT_RESOLUTION_DOMAIN = "clearscout-match-split-resolution-v1"
SELECTION_MANIFEST_DOMAIN = "clearscout-match-selection-manifest-v1"
JOB_MANIFEST_DOMAIN = "clearscout-match-job-manifest-v1"
JOB_SPEC_ORIGIN = "evals.golden.match_job"
SELECTION_ALGORITHM: Literal["lexicographic-feasible-v1"] = "lexicographic-feasible-v1"


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RequiredSlot(_ArtifactModel):
    """One frozen slot and the number of cases it takes; a None field admits any value."""

    classification: Classification | None
    dimension: Dimension
    challenge_class: ChallengeClass | None = None
    cases: int = Field(ge=1)

    def admits(self, item: _EligibleCase) -> bool:
        """Return whether one case may fill this slot."""
        return (
            self.classification in {None, item.classification}
            and self.dimension is item.dimension
            and self.challenge_class in {None, item.challenge_class}
        )


# The matrix cells never name a challenge class and the participation stratum
# never names a classification, so neither presupposes what reviewers label.
_REQUIRED_SLOTS: tuple[RequiredSlot, ...] = (
    RequiredSlot(classification=Classification.DIRECT, dimension=Dimension.HARD, cases=2),
    RequiredSlot(classification=Classification.DIRECT, dimension=Dimension.BEHAVIORAL, cases=1),
    RequiredSlot(classification=Classification.TAILORING_ONLY, dimension=Dimension.HARD, cases=1),
    RequiredSlot(
        classification=Classification.TAILORING_ONLY, dimension=Dimension.BEHAVIORAL, cases=2
    ),
    RequiredSlot(classification=Classification.TRANSFERABLE, dimension=Dimension.HARD, cases=4),
    RequiredSlot(
        classification=Classification.TRANSFERABLE, dimension=Dimension.BEHAVIORAL, cases=1
    ),
    RequiredSlot(classification=Classification.ASSERTED, dimension=Dimension.HARD, cases=2),
    RequiredSlot(classification=Classification.GAP, dimension=Dimension.HARD, cases=1),
    RequiredSlot(classification=Classification.GAP, dimension=Dimension.BEHAVIORAL, cases=2),
    RequiredSlot(
        classification=None,
        dimension=Dimension.HARD,
        challenge_class=ChallengeClass.PARTIAL_CONTRIBUTION,
        cases=3,
    ),
)
_TOTAL_CASES = sum(slot.cases for slot in _REQUIRED_SLOTS)


def split_resolution_sha256(
    pool: CandidatePool, resolution_set: ResolutionSet, split: Split
) -> str:
    """Bind the resolved labels of one split's candidates and no other label."""
    in_split = {item.case_id for item in pool.candidates if item.publication_gate.split == split}
    return domain_sha256(
        SPLIT_RESOLUTION_DOMAIN,
        {
            "split": split,
            "resolved_label_sha256": sorted(
                item.resolved_label_sha256
                for item in resolution_set.labels
                if item.case_id in in_split
            ),
        },
    )


def selection_key_sha256(
    *,
    protocol_version: ProtocolVersion,
    candidate_pool_sha256: Sha256,
    resolution_sha256: Sha256,
    case_id: UUID,
    resolved_label_sha256: Sha256,
) -> str:
    """Bind one selection key to its pool, its split's resolution digest, and its label."""
    return domain_sha256(
        SELECTION_KEY_DOMAIN,
        {
            "protocol_version": protocol_version,
            "candidate_pool_sha256": candidate_pool_sha256,
            "resolution_sha256": resolution_sha256,
            "case_id": str(case_id),
            "resolved_label_sha256": resolved_label_sha256,
        },
    )


class SelectedCase(_ArtifactModel):
    """One resolved candidate selected into the development set or held out of it."""

    position: int = Field(ge=1)
    case_id: UUID
    source_id: UUID
    normalized_requirement_sha256: Sha256
    resolved_label_sha256: Sha256
    selection_key: Sha256
    classification: Classification
    strength: Strength
    dimension: Dimension
    challenge_class: ChallengeClass

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        """Require opaque candidate and source identities to remain UUIDv4."""
        if self.case_id.version != 4:
            raise ValueError("selected case_id must be a UUIDv4")
        if self.source_id.version != 4:
            raise ValueError("selected source_id must be a UUIDv4")
        return self


class SelectionConstraints(_ArtifactModel):
    """The frozen slots and uniqueness conditions; no condition may be relaxed."""

    total_cases: int
    slots: tuple[RequiredSlot, ...]
    one_slot_per_case: Literal[True]
    all_challenge_classes_present: Literal[True]
    unique_case_ids: Literal[True]
    unique_source_ids: Literal[True]
    unique_normalized_requirement_digests: Literal[True]
    unique_resolution_digests: Literal[True]

    @model_validator(mode="after")
    def validate_frozen_slots(self) -> Self:
        """Refuse constraints that state any slot matrix other than the frozen one."""
        if self.slots != _REQUIRED_SLOTS or self.total_cases != _TOTAL_CASES:
            raise ValueError("selection constraints differ from the frozen slots")
        return self


SELECTION_CONSTRAINTS = SelectionConstraints(
    total_cases=_TOTAL_CASES,
    slots=_REQUIRED_SLOTS,
    one_slot_per_case=True,
    all_challenge_classes_present=True,
    unique_case_ids=True,
    unique_source_ids=True,
    unique_normalized_requirement_digests=True,
    unique_resolution_digests=True,
)


class ReviewReference(_ArtifactModel):
    """The two frozen reviewer records supporting one selected label."""

    case_id: UUID
    review_a_sha256: Sha256
    review_b_sha256: Sha256


class AdjudicationReference(_ArtifactModel):
    """The adjudication record supporting one selected disputed label."""

    case_id: UUID
    adjudication_sha256: Sha256


@dataclass(frozen=True)
class _EligibleCase:
    case_id: UUID
    source_id: UUID
    normalized_requirement_sha256: str
    resolved_label_sha256: str
    selection_key: str
    classification: Classification
    strength: Strength
    dimension: Dimension
    challenge_class: ChallengeClass


class SelectionManifestBody(_ArtifactModel):
    """The complete hash-bound selection receipt excluding its own digest."""

    protocol_version: ProtocolVersion
    selection_algorithm: Literal["lexicographic-feasible-v1"]
    synthetic_inventory_hash: InventoryHash
    review_context_sha256: Sha256
    candidate_pool_sha256: Sha256
    extraction_qa_sha256: Sha256
    review_set_sha256: Sha256
    resolution_set_sha256: Sha256
    constraints: SelectionConstraints
    ordered_cases: tuple[SelectedCase, ...] = Field(
        min_length=_TOTAL_CASES, max_length=_TOTAL_CASES
    )
    # Every QA-approved, resolved candidate the pool froze in the holdout split.
    held_out_cases: tuple[SelectedCase, ...] = Field(min_length=1)
    review_references: tuple[ReviewReference, ...]
    adjudication_references: tuple[AdjudicationReference, ...] = ()
    # Each ruling widens one selected case's scored label to an accepted-label set.
    rulings: tuple[CaseRuling, ...]
    dataset_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest_body(self) -> Self:
        """Enforce a complete slot assignment, ordering, and uniqueness."""
        self._validate_uniqueness()
        self._validate_positions(self.ordered_cases)
        self._validate_positions(self.held_out_cases)
        self._validate_matrix()
        self._validate_references()
        return self

    @property
    def all_cases(self) -> tuple[SelectedCase, ...]:
        """Return the development cases followed by the held-out cases."""
        return (*self.ordered_cases, *self.held_out_cases)

    def _validate_positions(self, cases: tuple[SelectedCase, ...]) -> None:
        if tuple(item.position for item in cases) != tuple(range(1, len(cases) + 1)):
            raise ValueError(f"selected case positions must be exactly 1 through {len(cases)}")
        keys = tuple(item.selection_key for item in cases)
        if keys != tuple(sorted(keys)):
            raise ValueError("selected cases must be ordered by selection key")

    def _validate_keys(self, pool: CandidatePool, resolution_set: ResolutionSet) -> None:
        splits: tuple[tuple[Split, tuple[SelectedCase, ...]], ...] = (
            ("development", self.ordered_cases),
            ("holdout", self.held_out_cases),
        )
        for split, cases in splits:
            resolution = split_resolution_sha256(pool, resolution_set, split)
            if any(
                item.selection_key
                != selection_key_sha256(
                    protocol_version=self.protocol_version,
                    candidate_pool_sha256=self.candidate_pool_sha256,
                    resolution_sha256=resolution,
                    case_id=item.case_id,
                    resolved_label_sha256=item.resolved_label_sha256,
                )
                for item in cases
            ):
                raise ValueError("selection keys do not match their frozen inputs")

    def _validate_matrix(self) -> None:
        if {item.challenge_class for item in self.ordered_cases} != set(ChallengeClass):
            raise ValueError("selection must represent every challenge class")
        cases = tuple(
            _EligibleCase(**item.model_dump(exclude={"position"})) for item in self.ordered_cases
        )
        if _first_feasible_indices(cases) is None:
            raise ValueError("selected cases admit no assignment of one case to each frozen slot")

    def _validate_uniqueness(self) -> None:
        cases = self.all_cases
        identities = (
            ("case IDs", [item.case_id for item in cases]),
            ("source IDs", [item.source_id for item in cases]),
            (
                "normalized requirement digests",
                [item.normalized_requirement_sha256 for item in cases],
            ),
            ("resolution digests", [item.resolved_label_sha256 for item in cases]),
        )
        for name, values in identities:
            if len(values) != len(set(values)):
                raise ValueError(f"selected {name} must be unique")

    def _validate_references(self) -> None:
        _validate_reference_coverage(
            [item.case_id for item in self.all_cases],
            self.review_references,
            self.adjudication_references,
        )

    def validate_inputs(
        self,
        pool: CandidatePool,
        qa: ExtractionQaSet,
        resolution_set: ResolutionSet,
    ) -> None:
        """Resolve every selected field against the candidate and resolution sets."""
        _require_upstream(self, pool, qa, resolution_set)
        candidates = {item.case_id: item for item in pool.candidates}
        labels = {item.case_id: item for item in resolution_set.labels}
        approved = {item.case_id for item in qa.findings if item.approved}
        for selected in self.all_cases:
            if selected.case_id not in approved:
                raise ValueError("every selected case must be approved by extraction QA")
            candidate = candidates.get(selected.case_id)
            label = labels.get(selected.case_id)
            if candidate is None or label is None:
                raise ValueError("selected case must exist in both candidate and resolution sets")
            if (
                selected.source_id != candidate.source_id
                or selected.normalized_requirement_sha256 != candidate.normalized_requirement_sha256
                or self.synthetic_inventory_hash != candidate.synthetic_inventory_hash
            ):
                raise ValueError("selected case differs from its candidate content")
            self._validate_label(selected, label)
        self._validate_keys(pool, resolution_set)
        if self.ordered_cases != _select_development(pool, qa, resolution_set):
            raise ValueError(
                "manifest cases are not the lexicographically first feasible selection"
            )
        if self.held_out_cases != _positioned(_eligible_cases(pool, qa, resolution_set, "holdout")):
            raise ValueError("manifest held-out cases are not the held-out cases of the pool")
        projected = serialize_match_dataset(
            self.all_cases, pool, self.synthetic_inventory_hash, self.rulings
        )
        if bytes_sha256(projected) != self.dataset_sha256:
            raise ValueError("dataset_sha256 does not match the exact projected JSONL bytes")

    def _validate_label(self, selected: SelectedCase, label: ResolvedLabel) -> None:
        resolved = match_material(label)
        if (
            selected.resolved_label_sha256 != label.resolved_label_sha256
            or selected.classification is not resolved.classification
            or selected.strength is not resolved.strength
            or selected.dimension is not resolved.dimension
            or selected.challenge_class is not resolved.challenge_class
        ):
            raise ValueError("selected case differs from its resolved label")
        _require_label_references(self.review_references, self.adjudication_references, label)


def _require_upstream(
    body: SelectionManifestBody | JobManifestBody,
    pool: CandidatePool,
    qa: ExtractionQaSet,
    resolution_set: ResolutionSet,
) -> None:
    """Refuse a manifest body that names another pool, context, QA, or resolution."""
    qa.validate_candidates(pool)
    if body.candidate_pool_sha256 != pool.candidate_pool_sha256:
        raise ValueError("selection manifest does not reference the candidate pool")
    if body.review_context_sha256 != pool.review_context_sha256:
        raise ValueError("selection manifest does not reference the candidate review context")
    if body.extraction_qa_sha256 != qa.extraction_qa_sha256:
        raise ValueError("selection manifest does not reference the extraction QA set")
    if body.resolution_set_sha256 != resolution_set.resolution_set_sha256:
        raise ValueError("selection manifest does not reference the resolution set")
    if body.review_set_sha256 != resolution_set.review_set_sha256:
        raise ValueError("selection manifest does not reference the resolved review set")


def _require_label_references(
    review_references: Sequence[ReviewReference],
    adjudication_references: Sequence[AdjudicationReference],
    label: ResolvedLabel,
) -> None:
    """Refuse review or adjudication references that differ from one resolved label."""
    reference = next(item for item in review_references if item.case_id == label.case_id)
    if (
        reference.review_a_sha256 != label.review_a_sha256
        or reference.review_b_sha256 != label.review_b_sha256
    ):
        raise ValueError("selected review references differ from the resolved label")
    adjudication = next(
        (item for item in adjudication_references if item.case_id == label.case_id),
        None,
    )
    if label.adjudication_sha256 is None:
        if adjudication is not None:
            raise ValueError("agreed selected label cannot reference adjudication")
    elif adjudication is None or adjudication.adjudication_sha256 != label.adjudication_sha256:
        raise ValueError("adjudicated selected label requires its adjudication reference")


def _validate_reference_coverage(
    case_ids: Sequence[UUID],
    review_references: Sequence[ReviewReference],
    adjudication_references: Sequence[AdjudicationReference],
) -> None:
    """Require one review reference per case and adjudication only for those cases."""
    review_ids = [item.case_id for item in review_references]
    if set(review_ids) != set(case_ids) or len(review_ids) != len(set(review_ids)):
        raise ValueError("review references must cover every selected case exactly once")
    adjudication_ids = [item.case_id for item in adjudication_references]
    if len(adjudication_ids) != len(set(adjudication_ids)):
        raise ValueError("adjudication references contain duplicate case IDs")
    if not set(adjudication_ids).issubset(case_ids):
        raise ValueError("adjudication references must belong to selected cases")


class SelectionManifest(_ArtifactModel):
    """A selection body accompanied by its non-self-referential digest."""

    body: SelectionManifestBody
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Bind the complete manifest body without hashing the digest itself."""
        if self.manifest_sha256 != domain_sha256(SELECTION_MANIFEST_DOMAIN, self.body):
            raise ValueError("manifest_sha256 does not match selection manifest content")
        return self

    def validate_inputs(
        self,
        pool: CandidatePool,
        qa: ExtractionQaSet,
        resolution_set: ResolutionSet,
    ) -> None:
        """Resolve the manifest body against its frozen upstream artifacts."""
        self.body.validate_inputs(pool, qa, resolution_set)


def _eligible_cases(
    pool: CandidatePool, qa: ExtractionQaSet, resolution_set: ResolutionSet, split: Split
) -> tuple[_EligibleCase, ...]:
    qa.validate_candidates(pool)
    approved = {item.case_id for item in qa.findings if item.approved}
    labels = {item.case_id: item for item in resolution_set.labels}
    resolution = split_resolution_sha256(pool, resolution_set, split)
    eligible = []
    for candidate in pool.candidates:
        label = labels.get(candidate.case_id)
        if (
            candidate.publication_gate.split != split
            or candidate.case_id not in approved
            or label is None
        ):
            continue
        resolved = match_material(label)
        eligible.append(
            _EligibleCase(
                case_id=candidate.case_id,
                source_id=candidate.source_id,
                normalized_requirement_sha256=candidate.normalized_requirement_sha256,
                resolved_label_sha256=label.resolved_label_sha256,
                selection_key=selection_key_sha256(
                    protocol_version=PROTOCOL_VERSION,
                    candidate_pool_sha256=pool.candidate_pool_sha256,
                    resolution_sha256=resolution,
                    case_id=candidate.case_id,
                    resolved_label_sha256=label.resolved_label_sha256,
                ),
                classification=resolved.classification,
                strength=resolved.strength,
                dimension=resolved.dimension,
                challenge_class=resolved.challenge_class,
            )
        )
    return tuple(sorted(eligible, key=lambda item: item.selection_key))


def _remaining_after(
    remaining: tuple[int, ...], item: _EligibleCase
) -> tuple[tuple[int, ...], ...]:
    """Return the open-slot counts left by each slot the case can fill."""
    return tuple(
        tuple(value - 1 if index == position else value for index, value in enumerate(remaining))
        for position, slot in enumerate(_REQUIRED_SLOTS)
        if remaining[position] and slot.admits(item)
    )


def _has_basic_capacity(
    eligible: tuple[_EligibleCase, ...], start: int, remaining: tuple[int, ...]
) -> bool:
    return all(
        sum(slot.admits(item) for item in eligible[start:]) >= needed
        for slot, needed in zip(_REQUIRED_SLOTS, remaining, strict=True)
        if needed
    )


def _first_feasible_indices(eligible: tuple[_EligibleCase, ...]) -> tuple[int, ...] | None:
    initial_remaining = tuple(slot.cases for slot in _REQUIRED_SLOTS)

    @cache
    def search(
        start: int,
        remaining: tuple[int, ...],
        source_ids: frozenset[UUID],
        normalized_digests: frozenset[str],
        resolution_digests: frozenset[str],
        challenges: frozenset[ChallengeClass],
    ) -> tuple[int, ...] | None:
        slots = sum(remaining)
        if slots == 0:
            return () if challenges == frozenset(ChallengeClass) else None
        if len(eligible) - start < slots or not _has_basic_capacity(eligible, start, remaining):
            return None
        for index in range(start, len(eligible)):
            item = eligible[index]
            if (
                item.source_id in source_ids
                or item.normalized_requirement_sha256 in normalized_digests
                or item.resolved_label_sha256 in resolution_digests
            ):
                continue
            # A case fitting several slots takes the one whose suffix sorts first.
            suffixes = [
                suffix
                for next_remaining in _remaining_after(remaining, item)
                if (
                    suffix := search(
                        index + 1,
                        next_remaining,
                        source_ids | {item.source_id},
                        normalized_digests | {item.normalized_requirement_sha256},
                        resolution_digests | {item.resolved_label_sha256},
                        challenges | {item.challenge_class},
                    )
                )
                is not None
            ]
            if suffixes:
                return (index, *min(suffixes))
        return None

    return search(0, initial_remaining, frozenset(), frozenset(), frozenset(), frozenset())


def select_cases(
    pool: CandidatePool,
    qa: ExtractionQaSet,
    resolution_set: ResolutionSet,
    *,
    previous_pool: CandidatePool | None,
) -> tuple[SelectedCase, ...]:
    """Return the first feasible development-case tuple, keeping ``previous_pool``'s splits."""
    if previous_pool is not None:
        pool.require_splits_kept(previous_pool)
    return _select_development(pool, qa, resolution_set)


def select_held_out(
    pool: CandidatePool,
    qa: ExtractionQaSet,
    resolution_set: ResolutionSet,
    *,
    previous_pool: CandidatePool | None,
) -> tuple[SelectedCase, ...]:
    """Return every QA-approved, resolved holdout candidate, keeping ``previous_pool``'s splits."""
    if previous_pool is not None:
        pool.require_splits_kept(previous_pool)
    return _positioned(_eligible_cases(pool, qa, resolution_set, "holdout"))


def _select_development(
    pool: CandidatePool, qa: ExtractionQaSet, resolution_set: ResolutionSet
) -> tuple[SelectedCase, ...]:
    eligible = _eligible_cases(pool, qa, resolution_set, "development")
    indices = _first_feasible_indices(eligible)
    if indices is None:
        raise ValueError("no feasible selection fills the frozen slots")
    return _positioned(eligible[index] for index in indices)


def _positioned(cases: Iterable[_EligibleCase]) -> tuple[SelectedCase, ...]:
    return tuple(
        SelectedCase(
            position=position,
            case_id=item.case_id,
            source_id=item.source_id,
            normalized_requirement_sha256=item.normalized_requirement_sha256,
            resolved_label_sha256=item.resolved_label_sha256,
            selection_key=item.selection_key,
            classification=item.classification,
            strength=item.strength,
            dimension=item.dimension,
            challenge_class=item.challenge_class,
        )
        for position, item in enumerate(cases, 1)
    )


def _rulings_by_case(
    rulings: Sequence[CaseRuling], case_ids: Sequence[UUID]
) -> dict[UUID, CaseRuling]:
    """Index rulings by case, refusing a second ruling or one naming an unheld case."""
    ruled = {item.case_id: item for item in rulings}
    if len(ruled) != len(rulings):
        raise ValueError("a case carries at most one ruling")
    if not ruled.keys() <= set(case_ids):
        raise ValueError("a ruling names a case the selection does not hold")
    return ruled


def _expected_pair(
    classification: Classification, strength: Strength, ruling: CaseRuling | None
) -> tuple[str | list[str], str | list[str]]:
    """Return a row's expected label, or a ruling's accepted pairs as two index-paired lists."""
    if ruling is None:
        return classification.value, strength.value
    pairs = [(item.classification.value, item.strength.value) for item in ruling.accepted]
    if (classification.value, strength.value) not in pairs:
        raise ValueError("a ruling must accept the case's resolved label")
    return [label for label, _ in pairs], [grade for _, grade in pairs]


def serialize_match_dataset(
    selected_cases: tuple[SelectedCase, ...],
    pool: CandidatePool,
    synthetic_inventory_hash: InventoryHash,
    rulings: Sequence[CaseRuling],
) -> bytes:
    """Project selected cases to the exact minimal public match JSONL bytes.

    A ruled case's row carries its ruling's accepted pairs as two index-paired lists.
    """
    candidates = {item.case_id: item for item in pool.candidates}
    ruled = _rulings_by_case(rulings, [item.case_id for item in selected_cases])
    lines: list[bytes] = []
    for selected in selected_cases:
        candidate = candidates.get(selected.case_id)
        if candidate is None:
            raise ValueError("cannot project a selected case absent from the candidate pool")
        if candidate.synthetic_inventory_hash != synthetic_inventory_hash:
            raise ValueError("cannot project a candidate from another synthetic inventory")
        case_id = str(selected.case_id)
        classification, strength = _expected_pair(
            selected.classification, selected.strength, ruled.get(selected.case_id)
        )
        lines.append(
            canonical_json_bytes(
                {
                    "id": case_id,
                    "input": candidate.requirement_text,
                    "requirement_text": candidate.requirement_text,
                    "requirement_key": case_id,
                    "dimension": selected.dimension.value,
                    "cv_hash": synthetic_inventory_hash,
                    "split": candidate.publication_gate.split,
                    "expected_classification": classification,
                    "expected_strength": strength,
                }
            )
        )
    return b"\n".join(lines) + b"\n"


def project_match_dataset(
    manifest: SelectionManifest,
    pool: CandidatePool,
    qa: ExtractionQaSet,
    resolution_set: ResolutionSet,
) -> bytes:
    """Validate all frozen inputs and return the bound public dataset bytes."""
    manifest.validate_inputs(pool, qa, resolution_set)
    return serialize_match_dataset(
        manifest.body.all_cases,
        pool,
        manifest.body.synthetic_inventory_hash,
        manifest.body.rulings,
    )


class PostingRequirement(_ArtifactModel):
    """One posting requirement in posting order, with the importance its section gives it."""

    case_id: UUID
    importance: Importance


class JobPosting(_ArtifactModel):
    """The posting fields a whole-job row carries that its candidates do not."""

    source_id: UUID
    source_artifact_sha256: Sha256
    title: NonBlank
    company: NonBlank
    location: NonBlank | None
    requirements: tuple[PostingRequirement, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_posting(self) -> Self:
        """Require opaque identities and each requirement once."""
        ids = [item.case_id for item in self.requirements]
        if any(value.version != 4 for value in (self.source_id, *ids)):
            raise ValueError("posting source and case ids must be UUIDv4")
        if len(ids) != len(set(ids)):
            raise ValueError("a posting names each requirement once")
        return self

    def candidates(self, pool: CandidatePool, qa: ExtractionQaSet) -> tuple[CandidateContent, ...]:
        """Return the posting's QA-approved candidates in posting order, refusing any other set."""
        qa.validate_candidates(pool)
        of_source = {
            item.case_id: item for item in pool.candidates if item.source_id == self.source_id
        }
        if set(of_source) != {item.case_id for item in self.requirements}:
            raise ValueError(
                "the posting's requirements must be the pool's candidates of its source"
            )
        approved = {item.case_id for item in qa.findings if item.approved}
        if not set(of_source) <= approved:
            raise ValueError("every posting requirement must be approved by extraction QA")
        ordered = tuple(of_source[item.case_id] for item in self.requirements)
        if any(item.source_artifact_sha256 != self.source_artifact_sha256 for item in ordered):
            raise ValueError("a posting requirement cites another source artifact")
        if len({item.publication_gate.split for item in ordered}) != 1:
            raise ValueError("a posting's requirements share one split")
        return ordered


class JobLabel(_ArtifactModel):
    """One posting requirement's resolved label."""

    case_id: UUID
    resolved_label_sha256: Sha256
    classification: Classification
    strength: Strength
    dimension: Dimension
    challenge_class: ChallengeClass


def job_labels(posting: JobPosting, resolution_set: ResolutionSet) -> tuple[JobLabel, ...]:
    """Return each posting requirement's resolved label in posting order."""
    labels = {item.case_id: item for item in resolution_set.labels}
    resolved: list[JobLabel] = []
    for requirement in posting.requirements:
        label = labels.get(requirement.case_id)
        if label is None:
            raise ValueError("every posting requirement needs a resolved label")
        material = match_material(label)
        resolved.append(
            JobLabel(
                case_id=label.case_id,
                resolved_label_sha256=label.resolved_label_sha256,
                classification=material.classification,
                strength=material.strength,
                dimension=material.dimension,
                challenge_class=material.challenge_class,
            )
        )
    return tuple(resolved)


class JobManifestBody(_ArtifactModel):
    """The hash-bound receipt for one whole-job match row, excluding its own digest."""

    protocol_version: ProtocolVersion
    synthetic_inventory_hash: InventoryHash
    review_context_sha256: Sha256
    candidate_pool_sha256: Sha256
    extraction_qa_sha256: Sha256
    review_set_sha256: Sha256
    resolution_set_sha256: Sha256
    split: Split
    posting: JobPosting
    labels: tuple[JobLabel, ...]
    review_references: tuple[ReviewReference, ...]
    adjudication_references: tuple[AdjudicationReference, ...] = ()
    rulings: tuple[CaseRuling, ...]
    dataset_sha256: Sha256

    @model_validator(mode="after")
    def validate_job_body(self) -> Self:
        """Require one label per posting requirement in posting order, each referenced."""
        case_ids = [item.case_id for item in self.posting.requirements]
        if [item.case_id for item in self.labels] != case_ids:
            raise ValueError("job labels must follow the posting's requirements in order")
        _validate_reference_coverage(case_ids, self.review_references, self.adjudication_references)
        _rulings_by_case(self.rulings, case_ids)
        return self

    def validate_inputs(
        self, pool: CandidatePool, qa: ExtractionQaSet, resolution_set: ResolutionSet
    ) -> None:
        """Resolve the posting, labels, references, and dataset bytes against their inputs."""
        _require_upstream(self, pool, qa, resolution_set)
        candidates = self.posting.candidates(pool, qa)
        if candidates[0].publication_gate.split != self.split:
            raise ValueError("job manifest split differs from its candidates' split")
        if self.labels != job_labels(self.posting, resolution_set):
            raise ValueError("job labels differ from the resolved labels")
        resolved = {item.case_id: item for item in resolution_set.labels}
        for label in self.labels:
            _require_label_references(
                self.review_references, self.adjudication_references, resolved[label.case_id]
            )
        projected = serialize_match_job_dataset(
            self.posting, self.labels, pool, qa, self.synthetic_inventory_hash, self.rulings
        )
        if bytes_sha256(projected) != self.dataset_sha256:
            raise ValueError("dataset_sha256 does not match the exact projected JSONL bytes")


class JobManifest(_ArtifactModel):
    """A job manifest body accompanied by its non-self-referential digest."""

    body: JobManifestBody
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Bind the complete job manifest body without hashing the digest itself."""
        if self.manifest_sha256 != domain_sha256(JOB_MANIFEST_DOMAIN, self.body):
            raise ValueError("manifest_sha256 does not match job manifest content")
        return self

    def validate_inputs(
        self, pool: CandidatePool, qa: ExtractionQaSet, resolution_set: ResolutionSet
    ) -> None:
        """Resolve the manifest body against its frozen upstream artifacts."""
        self.body.validate_inputs(pool, qa, resolution_set)


def serialize_match_job_dataset(
    posting: JobPosting,
    labels: Sequence[JobLabel],
    pool: CandidatePool,
    qa: ExtractionQaSet,
    synthetic_inventory_hash: InventoryHash,
    rulings: Sequence[CaseRuling],
) -> bytes:
    """Project one posting to a single whole-job JSONL row with a label per requirement.

    The posting goes in ``job_spec``, which the solver sees; the labels go in
    ``expected``, keyed by requirement id, which only the scorer reads.
    """
    candidates = posting.candidates(pool, qa)
    if any(item.synthetic_inventory_hash != synthetic_inventory_hash for item in candidates):
        raise ValueError("cannot project a candidate from another synthetic inventory")
    if [item.case_id for item in labels] != [item.case_id for item in candidates]:
        raise ValueError("job labels must follow the posting's requirements in order")
    ruled = _rulings_by_case(rulings, [item.case_id for item in candidates])
    requirements: list[dict[str, str]] = []
    expected: list[dict[str, str | list[str]]] = []
    for requirement, candidate, label in zip(posting.requirements, candidates, labels, strict=True):
        case_id = str(candidate.case_id)
        requirements.append(
            {
                "id": case_id,
                "text": candidate.requirement_text,
                "importance": requirement.importance.value,
                "dimension": label.dimension.value,
            }
        )
        classification, strength = _expected_pair(
            label.classification, label.strength, ruled.get(label.case_id)
        )
        expected.append(
            {
                "requirement_id": case_id,
                "expected_classification": classification,
                "expected_strength": strength,
            }
        )
    row = {
        "id": str(posting.source_id),
        "cv_hash": synthetic_inventory_hash,
        "split": candidates[0].publication_gate.split,
        "job_spec": {
            "title": posting.title,
            "company": posting.company,
            "seniority": None,
            "location": posting.location,
            "requirements": requirements,
            "keywords": [],
            "hard_gates": [],
            "origin": JOB_SPEC_ORIGIN,
        },
        "expected": expected,
    }
    return canonical_json_bytes(row) + b"\n"


def project_match_job_dataset(
    manifest: JobManifest,
    pool: CandidatePool,
    qa: ExtractionQaSet,
    resolution_set: ResolutionSet,
) -> bytes:
    """Validate all frozen inputs and return the bound whole-job dataset bytes."""
    manifest.validate_inputs(pool, qa, resolution_set)
    body = manifest.body
    return serialize_match_job_dataset(
        body.posting, body.labels, pool, qa, body.synthetic_inventory_hash, body.rulings
    )


def load_live_manifest(content: bytes) -> SelectionManifest | JobManifest:
    """Validate a live run's manifest bytes as a selection manifest or a job manifest."""
    payload = json.loads(content)
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, dict) and "posting" in body:
        return JobManifest.model_validate_json(content)
    return SelectionManifest.model_validate_json(content)
