"""Strict annotations for sanitized semantic evaluation artifacts."""

from __future__ import annotations

import tomllib
from collections import defaultdict
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from tailorcv.domain.records import EvaluationDecision

Slug = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
ArtifactReference = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
SanitizedContentSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Score = Annotated[int, Field(ge=0, le=100)]


class _DatasetModel(BaseModel):
    """Reject unknown annotation fields and keep labels immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Split(StrEnum):
    """The partition controlling prompt development and release decisions."""

    DEVELOPMENT = "development"
    REGRESSION = "regression"
    HOLDOUT = "holdout"


class Suite(StrEnum):
    """An orthogonal evaluation suite an annotation belongs to."""

    ADVERSARIAL = "adversarial"


class HardGateVerdict(StrEnum):
    """Whether deterministic facts permit this posting to proceed."""

    CLEAR = "clear"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Relevance(StrEnum):
    """A human label for one retrieved board row."""

    STRONG = "strong"
    ADJACENT = "adjacent"
    WEAK = "weak"
    IRRELEVANT = "irrelevant"
    UNKNOWN = "unknown"


class RetrievalDisposition(StrEnum):
    """The human disposition of a retrieved row before fit evaluation."""

    STAGE = "stage"
    REJECT = "reject"
    INVESTIGATE = "investigate"


class DispositionReason(StrEnum):
    """Why relevance and retrieval disposition intentionally differ."""

    DUPLICATE = "duplicate"
    HARD_GATE = "hard_gate"
    POLICY = "policy"
    STALE = "stale"
    UNVERIFIED = "unverified"


class AbstainedScore(_DatasetModel):
    """A component the scoring policy declined to score, and the reason it gave."""

    state: Literal["not_stated", "conflict"]
    reason: str = Field(min_length=1, max_length=500)


type ComponentScore = Score | AbstainedScore


class FitScores(_DatasetModel):
    """The five independent scores preserved from the historical ledger.

    Compensation and eligibility are the two components the scoring policy can
    abstain on. An abstention is stated; a missing key is a malformed file.
    """

    role_fit: Score
    compensation: ComponentScore
    eligibility: ComponentScore
    evidence: Score
    opportunity: Score

    def abstained(self) -> tuple[str, ...]:
        """Return the components carrying an abstention instead of a score."""
        return tuple(
            name
            for name in ("compensation", "eligibility")
            if isinstance(getattr(self, name), AbstainedScore)
        )


class _AnnotationBase(_DatasetModel):
    """Metadata shared by fit and retrieval annotations."""

    annotation_id: Slug
    split: Split
    suites: tuple[Suite, ...] = ()
    retrieved_on: date
    adjudicated_on: date
    artifact_ref: ArtifactReference
    sanitized_content_sha256: SanitizedContentSha256
    rubric_version: Slug
    sanitized: Literal[True]
    privacy_review_passed: Literal[True]
    redistribution_approved: Literal[True]
    tags: tuple[Slug, ...] = ()

    @model_validator(mode="after")
    def validate_shared_metadata(self) -> _AnnotationBase:
        """Reject chronology errors and duplicate classification labels."""
        if self.adjudicated_on < self.retrieved_on:
            raise ValueError("adjudicated_on cannot predate retrieved_on")
        if len(set(self.suites)) != len(self.suites):
            raise ValueError("suites must not contain duplicates")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must not contain duplicates")
        return self


class FitAnnotation(_AnnotationBase):
    """One sanitized, human-adjudicated historical fit record."""

    task: Literal["fit"]
    hard_gate: HardGateVerdict
    decision: EvaluationDecision
    scores: FitScores

    @model_validator(mode="after")
    def decision_agrees_with_hard_gate(self) -> FitAnnotation:
        """Prevent an actionable label from bypassing an unresolved gate."""
        actionable = {EvaluationDecision.APPLY_NOW, EvaluationDecision.PREMIUM_TARGET}
        if self.decision in actionable and self.hard_gate is not HardGateVerdict.CLEAR:
            raise ValueError("an actionable decision requires a clear hard gate")
        if (
            self.decision is EvaluationDecision.INELIGIBLE
            and self.hard_gate is not HardGateVerdict.BLOCKED
        ):
            raise ValueError("an ineligible decision requires a blocked hard gate")
        return self


class RetrievalAnnotation(_AnnotationBase):
    """One sanitized discovery labeled before paid fit evaluation."""

    task: Literal["retrieval"]
    relevance: Relevance
    disposition: RetrievalDisposition
    disposition_reason: DispositionReason | None = None

    @model_validator(mode="after")
    def disposition_agrees_with_relevance(self) -> RetrievalAnnotation:
        """Require an explicit reason when a relevant row is rejected."""
        relevant = {Relevance.STRONG, Relevance.ADJACENT}
        if self.disposition is RetrievalDisposition.STAGE and self.relevance in {
            Relevance.IRRELEVANT,
            Relevance.UNKNOWN,
        }:
            raise ValueError("irrelevant or unknown rows cannot be staged")
        needs_reason = (
            self.disposition is RetrievalDisposition.REJECT and self.relevance in relevant
        )
        if needs_reason and self.disposition_reason is None:
            raise ValueError("rejecting a relevant row requires disposition_reason")
        if not needs_reason and self.disposition_reason is not None:
            raise ValueError("disposition_reason is only valid for a rejected relevant row")
        return self


type SemanticAnnotation = Annotated[
    FitAnnotation | RetrievalAnnotation,
    Field(discriminator="task"),
]
ANNOTATION_ADAPTER: TypeAdapter[SemanticAnnotation] = TypeAdapter(SemanticAnnotation)


class DatasetError(ValueError):
    """A semantic annotation dataset violated its public contract."""


def load_annotation(path: Path) -> SemanticAnnotation:
    """Read one annotation with its file name attached to any failure."""
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DatasetError(f"{path.name}: {error}") from error
    try:
        annotation = ANNOTATION_ADAPTER.validate_python(payload)
    except ValidationError as error:
        raise DatasetError(f"{path.name}: {error}") from error
    if annotation.annotation_id != path.stem:
        raise DatasetError(
            f"{path.name}: annotation_id {annotation.annotation_id!r} does not match the file name"
        )
    return annotation


def load_dataset(directory: Path) -> tuple[SemanticAnnotation, ...]:
    """Read a dataset in stable order and reject partition leakage."""
    paths = sorted(directory.glob("*.toml"))
    if not paths:
        raise DatasetError(f"{directory}: contains no semantic annotations")
    annotations = tuple(load_annotation(path) for path in paths)
    splits_by_digest: dict[str, set[Split]] = defaultdict(set)
    for annotation in annotations:
        splits_by_digest[annotation.sanitized_content_sha256].add(annotation.split)
    leaked = sorted(digest for digest, splits in splits_by_digest.items() if len(splits) > 1)
    if leaked:
        raise DatasetError(f"sanitized artifacts cross dataset splits: {', '.join(leaked)}")
    return annotations
