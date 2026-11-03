"""Opt-in semantic evaluation orchestration without a provider dependency.

The production request compiler and official provider adapters belong in the
executor supplied by a paid run. This module owns the safer parts that can be
tested without a provider: dataset selection, candidate-order randomization,
closed defect labels, cost aggregation, and private artifact boundaries.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clearscout.domain import ClaimBoundary, EvidenceStatus, GapClass
from clearscout.shared.costs import CallManifest, Usage
from evals.dataset import SemanticAnnotation, Split, Suite


class DefectClass(StrEnum):
    """A semantic defect label used in reports instead of an aggregate score."""

    FABRICATION = "fabrication"
    QUALIFIER_DRIFT = "qualifier_drift"
    HIDDEN_GAP = "hidden_gap"
    BAD_EQUIVALENCE = "bad_equivalence"
    KEYWORD_STUFFING = "keyword_stuffing"
    GENERIC_VOICE = "generic_voice"
    MISSED_EVIDENCE = "missed_evidence"
    SCAN_FAILURE = "scan_failure"
    BOUNDARY_FAILURE = "boundary_failure"
    IMPORT_FABRICATION = "import_fabrication"
    IMPORT_LOSS = "import_loss"
    IMPORT_MISPLACEMENT = "import_misplacement"


class HardFailureClass(StrEnum):
    """A release-blocking semantic failure."""

    UNSUPPORTED_CLAIM = "unsupported_claim"
    INVALID_CITATION = "invalid_citation"
    IMMUTABLE_FACT_CHANGED = "immutable_fact_changed"
    INJECTION_LEAK = "injection_leak"
    SCHEMA_FAILURE = "schema_failure"
    HARD_GATE_BYPASSED = "hard_gate_bypassed"


class RankingQuestion(StrEnum):
    """Independent questions that must not collapse into one score."""

    ACTIONABLE = "actionable"
    INTRINSIC_FIT = "intrinsic_fit"
    GATED_UPSIDE = "gated_upside"


class FunnelStage(StrEnum):
    """The smallest application funnel correction a critic can identify."""

    DISCOVERY = "discovery"
    FIRST_SCREEN = "first_screen"
    HIRING_MANAGER_READ = "hiring_manager_read"
    TECHNICAL_INTERVIEW = "technical_interview"
    EXECUTIVE_READ = "executive_read"
    APPLICATION_LOGISTICS = "application_logistics"


class SourceAuthority(StrEnum):
    """Which source is allowed to support a semantic claim."""

    MASTER_CV = "master_cv"
    APPROVED_ASSERTED_FACT = "approved_asserted_fact"
    FETCHED_POSTING = "fetched_posting"
    EMPLOYER_SOURCE = "employer_source"
    HISTORICAL_DRAFT = "historical_draft"
    MODEL_PROSE = "model_prose"
    ARCHIVE_SILENCE = "archive_silence"


class EvidenceResolution(StrEnum):
    """Order in which deterministic and interpretive evidence readings occur."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    INTERPRETED = "interpreted"


class EvaluationOutcome(StrEnum):
    """One durable evaluation attempt outcome."""

    COMPLETED = "completed"
    DEFERRED = "deferred"
    FAILED = "failed"


def _inside_repository(directory: Path) -> bool:
    """Return whether a path sits inside the published repository."""
    repository = Path(__file__).resolve().parent.parent
    return repository == directory.resolve() or repository in directory.resolve().parents


class _SemanticModel(BaseModel):
    """Keep evaluation records immutable and closed to misspelled fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceAssessment(_SemanticModel):
    """A confidence reading that preserves deterministic-first abstention."""

    resolution: EvidenceResolution
    confidence: Decimal = Field(ge=0, le=1)
    threshold: Decimal = Field(default=Decimal("0.75"), gt=0, le=1)
    status: EvidenceStatus

    @property
    def needs_review(self) -> bool:
        """Return whether this assessment must be reviewed before it is used."""
        return self.confidence < self.threshold or self.status is EvidenceStatus.UNRESOLVED


class DerivativeSubset(_SemanticModel):
    """The invariant that a derivative may only select master evidence."""

    master_evidence_ids: frozenset[str] = frozenset()
    selected_evidence_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def selected_ids_are_a_subset(self) -> DerivativeSubset:
        """Reject a derivative that cites an identifier outside its master set."""
        if not self.selected_evidence_ids <= self.master_evidence_ids:
            raise ValueError("selected evidence must be a subset of master evidence")
        return self


class EvaluationAttempt(_SemanticModel):
    """One attempt in the exact-once evaluation history."""

    attempt_id: str = Field(min_length=1, max_length=64)
    posting_id: str = Field(min_length=1, max_length=128)
    outcome: EvaluationOutcome
    explicit_re_evaluation: bool = False


class CriticFinding(_SemanticModel):
    """A critic finding that can trigger redrafting only for a real defect."""

    defect: DefectClass | None = None
    funnel_stage: FunnelStage | None = None
    taste_only: bool = False
    triggers_redraft: bool = False

    @model_validator(mode="after")
    def taste_cannot_trigger_redraft(self) -> CriticFinding:
        """Keep subjective preference from becoming an automatic rewrite."""
        if self.taste_only and self.triggers_redraft:
            raise ValueError("taste-only findings cannot trigger a redraft")
        if self.triggers_redraft and self.defect is None and self.funnel_stage is None:
            raise ValueError("a redraft must name a defect or funnel stage")
        return self


class StageMeasurement(_SemanticModel):
    """Usage and latency returned by one candidate's stage execution.

    ``model`` is what the stage asked for and ``served_model`` is what the
    provider says answered. An empty ``served_model`` means the executor did not
    report one.
    """

    stage: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    served_model: str = Field(default="", max_length=128)
    usage: Usage = Field(default_factory=Usage)
    dollars: Decimal | None = Field(default=None, ge=0)
    milliseconds: int = Field(default=0, ge=0)
    # Provider-hosted searches this call billed for, which no token count shows.
    searches: int = Field(default=0, ge=0)
    # The tier that served the call, which is what it was priced from.
    service_tier: str = Field(default="", max_length=32)

    @classmethod
    def from_manifest(cls, entry: CallManifest) -> StageMeasurement:
        """Carry one provider call into the evaluation vocabulary."""
        return cls(
            stage=entry.stage,
            model=entry.model,
            served_model=entry.served,
            usage=entry.usage,
            dollars=Decimal(str(entry.dollars)) if entry.dollars is not None else None,
            milliseconds=entry.milliseconds,
            searches=entry.search_request_count,
            service_tier=entry.service_tier,
        )

    @property
    def substituted(self) -> bool:
        """Return whether a different model answered than the one asked for."""
        return bool(self.served_model) and self.served_model != self.model


class SemanticJudgement(_SemanticModel):
    """One private human or calibrated label used to interpret a result."""

    classification: Literal["DIRECT", "TAILORING_ONLY", "TRANSFERABLE", "ASSERTED", "GAP"]
    evidence_status: EvidenceStatus
    source_authority: SourceAuthority
    claim_boundaries: tuple[ClaimBoundary, ...] = ()
    gap_class: GapClass | None = None
    ranking_question: RankingQuestion | None = None
    funnel_stage: FunnelStage | None = None
    hard_gate_clear: bool = False
    evidence_supported: bool = False
    apply_now: bool = False

    @model_validator(mode="after")
    def enforce_abstention_and_authority(self) -> SemanticJudgement:
        """Prevent a private label from treating rejected text as evidence."""
        unusable = {
            EvidenceStatus.UNRESOLVED,
            EvidenceStatus.REJECTED,
            EvidenceStatus.DUPLICATE,
        }
        forbidden_sources = {
            SourceAuthority.HISTORICAL_DRAFT,
            SourceAuthority.MODEL_PROSE,
            SourceAuthority.ARCHIVE_SILENCE,
        }
        if self.apply_now and (
            not self.hard_gate_clear
            or not self.evidence_supported
            or self.evidence_status in unusable
        ):
            raise ValueError("apply_now requires a clear gate and supported evidence")
        if self.evidence_supported and (
            self.evidence_status in unusable or self.source_authority in forbidden_sources
        ):
            raise ValueError("unsupported or non-authoritative material cannot support evidence")
        if self.classification == "GAP" and self.gap_class is None:
            raise ValueError("a GAP judgement requires a gap_class")
        return self


class SemanticCaseResult(_SemanticModel):
    """One executor result for one annotation and one candidate."""

    annotation_id: str = Field(min_length=1, max_length=64)
    candidate: Literal["baseline", "candidate"]
    stages: tuple[StageMeasurement, ...] = ()
    defects: tuple[DefectClass, ...] = ()
    hard_failures: tuple[HardFailureClass, ...] = ()
    judgements: tuple[SemanticJudgement, ...] = ()
    # Exact request and response text is accepted only for an approved private run.
    content: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def stage_names_are_unique(self) -> SemanticCaseResult:
        """Reject a result that cannot be interpreted by stage."""
        names = [item.stage for item in self.stages]
        if len(set(names)) != len(names):
            raise ValueError("stage measurements must have unique names")
        return self


class SemanticRunConfig(_SemanticModel):
    """Approval boundary for one semantic evaluation."""

    enabled: bool = False
    approval_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_dollars: Decimal | None = Field(default=None, ge=0)
    artifact_dir: Path | None = None
    capture_content: bool = False
    seed: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_paid_run_controls(self) -> SemanticRunConfig:
        """Refuse an enabled run without approval."""
        if not self.enabled:
            if self.capture_content:
                raise ValueError("content capture requires an enabled semantic run")
            return self
        if self.approval_id is None:
            raise ValueError("enabled semantic runs require: approval_id")
        if self.capture_content and self.artifact_dir is None:
            raise ValueError("content capture requires artifact_dir")
        if self.artifact_dir is not None and _inside_repository(self.artifact_dir):
            raise ValueError(
                "artifact_dir must sit outside the repository working tree: the exact prompts "
                "and responses written there are private and this repository is public"
            )
        return self


class SemanticExecutor(Protocol):
    """The paid boundary supplied by a caller after its approval is recorded."""

    def run(self, annotation: SemanticAnnotation, candidate: str) -> SemanticCaseResult:
        """Execute one candidate through the production compiler and adapters."""
        ...


class DatasetPartitions(_SemanticModel):
    """Primary splits plus the orthogonal adversarial suite."""

    development: tuple[SemanticAnnotation, ...] = ()
    regression: tuple[SemanticAnnotation, ...] = ()
    holdout: tuple[SemanticAnnotation, ...] = ()
    adversarial: tuple[SemanticAnnotation, ...] = ()

    def for_split(
        self, split: Split, *, adversarial: bool = False
    ) -> tuple[SemanticAnnotation, ...]:
        """Return one primary split, optionally restricted to adversarial cases."""
        selected = {
            Split.DEVELOPMENT: self.development,
            Split.REGRESSION: self.regression,
            Split.HOLDOUT: self.holdout,
        }[split]
        if adversarial:
            return tuple(item for item in selected if item in self.adversarial)
        return selected


def partition_dataset(annotations: Iterable[SemanticAnnotation]) -> DatasetPartitions:
    """Partition annotations without duplicating an item across primary splits."""
    items = tuple(annotations)
    ids = [item.annotation_id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("annotation identifiers must be unique")
    return DatasetPartitions(
        development=tuple(item for item in items if item.split is Split.DEVELOPMENT),
        regression=tuple(item for item in items if item.split is Split.REGRESSION),
        holdout=tuple(item for item in items if item.split is Split.HOLDOUT),
        adversarial=tuple(item for item in items if Suite.ADVERSARIAL in item.suites),
    )


def validate_evaluation_history(attempts: Iterable[EvaluationAttempt]) -> None:
    """Enforce one immutable completion unless a re-evaluation is explicit."""
    items = tuple(attempts)
    attempt_ids = [item.attempt_id for item in items]
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("evaluation attempt identifiers must be unique")
    completed: dict[str, int] = {}
    for item in items:
        if item.outcome is not EvaluationOutcome.COMPLETED:
            continue
        count = completed.get(item.posting_id, 0)
        if count and not item.explicit_re_evaluation:
            raise ValueError(
                f"posting {item.posting_id!r} was completed more than once without "
                "explicit re-evaluation"
            )
        completed[item.posting_id] = count + 1


class RunMode(StrEnum):
    """Whether a run measures one configuration or compares two."""

    BASELINE = "baseline"
    COMPARISON = "comparison"


class ComparisonPlan(_SemanticModel):
    """The order one annotation's candidates run in.

    A comparison carries both candidates; a baseline carries one.
    """

    annotation_id: str = Field(min_length=1, max_length=64)
    candidate_order: tuple[Literal["baseline", "candidate"], ...] = Field(
        min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def runs_each_candidate_once(self) -> ComparisonPlan:
        """Prevent a run from silently running one side twice."""
        if len(set(self.candidate_order)) != len(self.candidate_order):
            raise ValueError("candidate_order must not repeat a candidate")
        return self


class SemanticReport(_SemanticModel):
    """Structured output from a semantic comparison."""

    run_id: str = Field(min_length=1, max_length=64)
    split: Split
    seed: int
    plans: tuple[ComparisonPlan, ...]
    results: tuple[SemanticCaseResult, ...]
    defect_counts: dict[DefectClass, int]
    hard_failure_counts: dict[HardFailureClass, int]
    usage: Usage
    dollars: Decimal | None
    searches: int = 0
    # Every stage a different model answered than the one it asked for.
    substitutions: tuple[str, ...] = ()
    artifact_files: tuple[str, ...] = ()

    @property
    def reproducible(self) -> bool:
        """Return whether every stage ran on the model it named."""
        return not self.substitutions


class SemanticRunError(RuntimeError):
    """A semantic run was not authorized or produced an unusable report.

    ``report`` carries the cases that completed before the run stopped. It is
    ``None`` only when the run was refused before anything was paid for.
    """

    def __init__(self, message: str, report: SemanticReport | None = None) -> None:
        """Carry the partial report alongside the reason the run stopped."""
        super().__init__(message)
        self.report = report


def run_semantic_evaluation(
    annotations: Sequence[SemanticAnnotation],
    executor: SemanticExecutor,
    config: SemanticRunConfig,
    *,
    split: Split = Split.DEVELOPMENT,
    adversarial: bool = False,
    mode: RunMode = RunMode.COMPARISON,
) -> SemanticReport:
    """Run a blinded two-candidate comparison through an injected executor.

    ``mode`` selects a baseline instead, which runs each case once. The default
    configuration refuses before the executor is called, and the executor is
    injected so tests stay offline.
    """
    if not config.enabled:
        raise SemanticRunError("semantic evaluation is disabled")
    selected = partition_dataset(annotations).for_split(split, adversarial=adversarial)
    if not selected:
        raise SemanticRunError(f"the {split.value} semantic split is empty")

    plans = _comparison_plans(selected, config.seed, mode)
    run_id = _run_id(config.seed, split, plans)
    by_id = {item.annotation_id: item for item in selected}
    results: list[SemanticCaseResult] = []
    artifact_files: list[str] = []
    try:
        for plan in plans:
            annotation = by_id[plan.annotation_id]
            for candidate in plan.candidate_order:
                result = executor.run(annotation, candidate)
                if result.annotation_id != annotation.annotation_id:
                    raise SemanticRunError(
                        f"executor returned {result.annotation_id!r} "
                        f"for {annotation.annotation_id!r}"
                    )
                if result.candidate != candidate:
                    raise SemanticRunError(
                        f"executor returned {result.candidate!r} for {candidate!r}"
                    )
                results.append(
                    result if config.capture_content else result.model_copy(update={"content": {}})
                )
                # Written per case: a later refusal must not discard what was
                # already paid for, which is the one thing nobody can recompute.
                artifact_files.extend(_write_private_artifact(run_id, result, config))
    except Exception as error:
        partial = _report(split, config.seed, plans, results, run_id).model_copy(
            update={"artifact_files": tuple(artifact_files)}
        )
        message = str(error) if isinstance(error, SemanticRunError) else f"executor failed: {error}"
        raise SemanticRunError(message, report=partial) from error

    report = _report(split, config.seed, plans, results, run_id)
    return report.model_copy(update={"artifact_files": tuple(artifact_files)})


def _comparison_plans(
    annotations: Sequence[SemanticAnnotation], seed: int, mode: RunMode
) -> tuple[ComparisonPlan, ...]:
    """Return a reproducible shuffled case order with swapped pair order."""
    # Keyed by a digest rather than process randomness, so one seed is one order.
    keyed = sorted(
        annotations,
        key=lambda item: hashlib.blake2s(f"{seed}:{item.annotation_id}".encode()).digest(),
    )
    return tuple(
        ComparisonPlan(
            annotation_id=item.annotation_id,
            candidate_order=_order(index, mode),
        )
        for index, item in enumerate(keyed)
    )


def _order(index: int, mode: RunMode) -> tuple[Literal["baseline", "candidate"], ...]:
    """Return the candidates one case runs, swapping the pair on alternate cases."""
    if mode is RunMode.BASELINE:
        return ("baseline",)
    return ("candidate", "baseline") if index % 2 else ("baseline", "candidate")


def _run_id(seed: int, split: Split, plans: tuple[ComparisonPlan, ...]) -> str:
    """Return the identifier for one planned run, known before any case runs."""
    run_key = "|".join(
        (
            str(seed),
            split.value,
            *(f"{plan.annotation_id}:{plan.candidate_order}" for plan in plans),
        )
    )
    return hashlib.blake2s(run_key.encode("utf-8"), digest_size=16).hexdigest()


def _report(
    split: Split,
    seed: int,
    plans: tuple[ComparisonPlan, ...],
    results: Sequence[SemanticCaseResult],
    run_id: str,
) -> SemanticReport:
    """Aggregate stage usage and defect labels without inventing a score."""
    usage = Usage()
    dollars: Decimal | None = Decimal(0)
    defects: Counter[DefectClass] = Counter()
    hard_failures: Counter[HardFailureClass] = Counter()
    substitutions: list[str] = []
    searches = 0
    for result in results:
        defects.update(result.defects)
        hard_failures.update(result.hard_failures)
        for stage in result.stages:
            usage += stage.usage
            searches += stage.searches
            if stage.substituted:
                substitutions.append(
                    f"{result.annotation_id}/{result.candidate}/{stage.stage}: "
                    f"asked {stage.model}, served {stage.served_model}"
                )
            if stage.dollars is None:
                dollars = None
            elif dollars is not None:
                dollars += stage.dollars
    return SemanticReport(
        run_id=run_id,
        split=split,
        seed=seed,
        plans=plans,
        results=tuple(results),
        defect_counts=dict(defects),
        hard_failure_counts=dict(hard_failures),
        usage=usage,
        dollars=dollars,
        searches=searches,
        substitutions=tuple(substitutions),
    )


def _write_private_artifact(
    run_id: str, result: SemanticCaseResult, config: SemanticRunConfig
) -> tuple[str, ...]:
    """Write one case's captured content under the caller's private root."""
    if not config.capture_content or config.artifact_dir is None or not result.content:
        return ()
    root = config.artifact_dir
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{run_id}-{result.annotation_id}-{result.candidate}.json"
    (root / filename).write_text(
        json.dumps(result.content, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return (filename,)


__all__ = [
    "ClaimBoundary",
    "ComparisonPlan",
    "CriticFinding",
    "DatasetPartitions",
    "DefectClass",
    "DerivativeSubset",
    "EvaluationAttempt",
    "EvaluationOutcome",
    "EvidenceAssessment",
    "EvidenceResolution",
    "EvidenceStatus",
    "FunnelStage",
    "GapClass",
    "HardFailureClass",
    "RankingQuestion",
    "RunMode",
    "SemanticCaseResult",
    "SemanticExecutor",
    "SemanticJudgement",
    "SemanticReport",
    "SemanticRunConfig",
    "SemanticRunError",
    "SourceAuthority",
    "StageMeasurement",
    "partition_dataset",
    "run_semantic_evaluation",
    "validate_evaluation_history",
]
