"""Validate the relevance-review calibration corpus and the labels frozen from it.

The tracked corpus is input only. The reviewed half lives in the gitignored
scoring dataset, and ``RelevanceReviewLabel`` is its schema.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evals.inspect.labeling import ReviewFamily
from evals.inspect.review_resolution import LabelProvenance, RelevanceDecision
from evals.support.inventory import synthetic_inventory
from tailorcv.application.discovery.relevance_review_spans import posting_spans
from tailorcv.application.job_search.models import (
    RelevanceReviewCoreWorkAlignment,
    RelevanceReviewDecisiveBasis,
    RelevanceReviewHardCondition,
    RelevanceReviewQualificationAlignment,
    RelevanceReviewVerdict,
)

CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "relevance_review_corpus.jsonl"

CorpusId = Annotated[str, Field(pattern=r"^rrp-[0-9]{3}$")]
SyntheticCvHash = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]

# Two section labels ``search_summary`` renders, which free prose never carries.
# The summary drops a section whose field is empty, so a case may omit the rest.
REQUIRED_CONTEXT_HEADERS: tuple[str, ...] = (
    "Opportunity thesis (person-stated preference, not evidence):",
    "Places this person may work:",
)


class _CorpusModel(BaseModel):
    """Keep public calibration inputs free of labels and incidental fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RelevanceReviewPosting(_CorpusModel):
    """One synthetic posting supplied to a future relevance-review solver."""

    id: CorpusId
    employer: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: Annotated[str, Field(min_length=80, max_length=20_000)]


class RelevanceReviewInput(_CorpusModel):
    """The complete solver input for one calibration case."""

    search_context: Annotated[str, Field(min_length=1, max_length=4_000)]
    posting: RelevanceReviewPosting

    @model_validator(mode="after")
    def states_the_search_as_the_stage_renders_it(self) -> Self:
        """Refuse a context that paraphrases the search instead of stating it."""
        missing = [
            header for header in REQUIRED_CONTEXT_HEADERS if header not in self.search_context
        ]
        if missing:
            raise ValueError(f"search_context is missing {', '.join(missing)}")
        return self


class RelevanceReviewCorpusCase(_CorpusModel):
    """One unlabeled synthetic relevance-review calibration case."""

    id: CorpusId
    split: Literal["development"]
    cv_hash: SyntheticCvHash
    input: RelevanceReviewInput

    @model_validator(mode="after")
    def uses_the_case_identifier_for_the_posting(self) -> Self:
        """Keep stable citation IDs attached to one unambiguous case."""
        if self.id != self.input.posting.id:
            raise ValueError("case id must match input.posting.id")
        return self

    @property
    def citation_ids(self) -> tuple[str, ...]:
        """Derive the citation vocabulary without storing copied source offsets."""
        return tuple(span.id for span in posting_spans(self.id, self.input.posting.body))


# A frozen scoring row is one corpus case beside its reviewed label, so any
# other key is a misspelling that would silently drop what it meant to state.
_ROW_FIELDS: tuple[str, ...] = tuple(RelevanceReviewCorpusCase.model_fields)


class RelevanceReviewAxes(_CorpusModel):
    """The four classification axes one frozen row's reviewed label states."""

    core_work_alignment: RelevanceReviewCoreWorkAlignment
    core_qualification_alignment: RelevanceReviewQualificationAlignment
    hard_condition: RelevanceReviewHardCondition
    decisive_basis: RelevanceReviewDecisiveBasis


class RelevanceReviewLabel(_CorpusModel):
    """The reviewed half of one frozen relevance-review scoring row.

    An absent ``provenance`` is a row whose review record nobody has written
    down yet, which is a different fact from a record stating no disagreement.
    """

    expected_recommendation: RelevanceReviewVerdict
    expected_axes: RelevanceReviewAxes
    axes_confidence: Literal["high", "low"]
    provenance: LabelProvenance | None = None

    @model_validator(mode="after")
    def states_a_verdict_the_shipped_stage_allows(self) -> Self:
        """Refuse a frozen label whose axes and recommendation cannot co-occur."""
        RelevanceDecision.model_validate(
            {
                "family": ReviewFamily.RELEVANCE_REVIEW,
                **self.expected_axes.model_dump(),
                "recommendation": self.expected_recommendation,
            }
        )
        if self.provenance is not None and self.provenance.family is not (
            ReviewFamily.RELEVANCE_REVIEW
        ):
            raise ValueError("provenance names another review family")
        return self

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> RelevanceReviewLabel:
        """Read the reviewed label out of one frozen scoring row."""
        unknown = sorted(set(row) - set(cls.model_fields) - set(_ROW_FIELDS))
        if unknown:
            raise ValueError(f"frozen row carries unknown field(s): {', '.join(unknown)}")
        # The row comes from JSONL, so the strict provenance record is validated as JSON.
        payload = {name: row[name] for name in cls.model_fields if name in row}
        return cls.model_validate_json(json.dumps(payload))


class RelevanceReviewCorpusError(ValueError):
    """The relevance-review calibration corpus violated its input contract."""


def load_relevance_review_corpus(
    path: Path = CORPUS_PATH,
) -> tuple[RelevanceReviewCorpusCase, ...]:
    """Load a stable, unlabeled corpus tied to the shipped synthetic inventory."""
    cases: list[RelevanceReviewCorpusCase] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RelevanceReviewCorpusError(f"{path.name}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = RelevanceReviewCorpusCase.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as error:
            raise RelevanceReviewCorpusError(f"{path.name}:{line_number}: {error}") from error
        cases.append(case)
    if not cases:
        raise RelevanceReviewCorpusError(f"{path.name}: contains no calibration cases")
    ids = tuple(case.id for case in cases)
    if len(ids) != len(set(ids)):
        raise RelevanceReviewCorpusError(f"{path.name}: duplicate case id")
    expected_cv_hash = synthetic_inventory().content_hash
    for case in cases:
        if case.cv_hash != expected_cv_hash:
            raise RelevanceReviewCorpusError(
                f"{path.name}: {case.id} names {case.cv_hash}, expected {expected_cv_hash}"
            )
        if not case.citation_ids:
            raise RelevanceReviewCorpusError(f"{path.name}: {case.id} has no citation spans")
    return tuple(cases)
