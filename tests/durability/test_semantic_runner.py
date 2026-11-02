"""The semantic runner stays opt-in, blinded, and private by construction."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytest
from evals.dataset import (
    FitAnnotation,
    FitScores,
    HardGateVerdict,
    SemanticAnnotation,
    Split,
    Suite,
)
from evals.semantic import (
    ClaimBoundary,
    CriticFinding,
    DefectClass,
    DerivativeSubset,
    EvaluationAttempt,
    EvaluationOutcome,
    EvidenceAssessment,
    EvidenceResolution,
    EvidenceStatus,
    GapClass,
    RankingQuestion,
    RunMode,
    SemanticCaseResult,
    SemanticJudgement,
    SemanticRunConfig,
    SemanticRunError,
    SourceAuthority,
    StageMeasurement,
    partition_dataset,
    run_semantic_evaluation,
    validate_evaluation_history,
)

from tailorcv.domain.records import EvaluationDecision
from tailorcv.shared.costs import Usage


def _annotation(
    name: str, split: Split = Split.DEVELOPMENT, *, adversarial: bool = False
) -> FitAnnotation:
    return FitAnnotation(
        annotation_id=name,
        task="fit",
        split=split,
        suites=(Suite.ADVERSARIAL,) if adversarial else (),
        retrieved_on=date(2026, 8, 1),
        adjudicated_on=date(2026, 8, 2),
        artifact_ref=f"artifact-{name}",
        sanitized_content_sha256=(name.encode().hex() * 64)[:64],
        rubric_version="fit-v1",
        sanitized=True,
        privacy_review_passed=True,
        redistribution_approved=True,
        hard_gate=HardGateVerdict.CLEAR,
        decision=EvaluationDecision.APPLY_NOW,
        scores=FitScores(
            role_fit=80,
            compensation=80,
            eligibility=100,
            evidence=80,
            opportunity=80,
        ),
    )


def _config(
    *,
    artifact_dir: Path | None = None,
    capture_content: bool = False,
    max_dollars: str = "1.00",
) -> SemanticRunConfig:
    return SemanticRunConfig(
        enabled=True,
        approval_id="approval-1",
        max_dollars=Decimal(max_dollars),
        artifact_dir=artifact_dir,
        capture_content=capture_content,
        seed=7,
    )


class _Executor:
    def __init__(
        self,
        dollars: Decimal | None = Decimal("0.10"),
        served: str = "test-model",
        searches: int = 0,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.dollars = dollars
        self.served = served
        self.searches = searches

    def run(self, annotation: SemanticAnnotation, candidate: str) -> SemanticCaseResult:
        self.calls.append((annotation.annotation_id, candidate))
        return SemanticCaseResult(
            annotation_id=annotation.annotation_id,
            candidate=cast(Literal["baseline", "candidate"], candidate),
            stages=(
                StageMeasurement(
                    stage="matching",
                    model="test-model",
                    served_model=self.served,
                    usage=Usage(input_tokens=10, output_tokens=4),
                    dollars=self.dollars,
                    milliseconds=12,
                    searches=self.searches,
                ),
            ),
            defects=(DefectClass.GENERIC_VOICE,) if candidate == "candidate" else (),
            content={"request": "private request", "response": "private response"},
        )


def test_partition_dataset_keeps_adversarial_as_an_orthogonal_suite() -> None:
    development = _annotation("development")
    adversarial = _annotation("adversarial", adversarial=True)
    regression = _annotation("regression", Split.REGRESSION)

    partitions = partition_dataset((regression, adversarial, development))

    assert [item.annotation_id for item in partitions.development] == [
        "adversarial",
        "development",
    ]
    assert [item.annotation_id for item in partitions.for_split(Split.DEVELOPMENT)] == [
        "adversarial",
        "development",
    ]
    assert [
        item.annotation_id for item in partitions.for_split(Split.DEVELOPMENT, adversarial=True)
    ] == ["adversarial"]
    assert [item.annotation_id for item in partitions.regression] == ["regression"]


def test_semantic_evaluation_is_disabled_before_the_executor_is_called() -> None:
    executor = _Executor()

    with pytest.raises(SemanticRunError, match="disabled"):
        run_semantic_evaluation((_annotation("case"),), executor, SemanticRunConfig())

    assert executor.calls == []


def test_semantic_judgement_rejects_apply_now_without_verified_support() -> None:
    with pytest.raises(ValueError, match="apply_now requires"):
        SemanticJudgement(
            classification="DIRECT",
            evidence_status=EvidenceStatus.UNRESOLVED,
            source_authority=SourceAuthority.MASTER_CV,
            hard_gate_clear=True,
            evidence_supported=True,
            apply_now=True,
        )

    gap = SemanticJudgement(
        classification="GAP",
        evidence_status=EvidenceStatus.VERIFIED,
        source_authority=SourceAuthority.FETCHED_POSTING,
        gap_class=GapClass.BOUNDED_LEARNABLE,
    )
    assert gap.gap_class is GapClass.BOUNDED_LEARNABLE


def test_semantic_judgement_preserves_boundary_and_ranking_labels() -> None:
    judgement = SemanticJudgement(
        classification="TRANSFERABLE",
        evidence_status=EvidenceStatus.CORROBORATED,
        source_authority=SourceAuthority.MASTER_CV,
        claim_boundaries=(ClaimBoundary.TEAM_CONTRIBUTION, ClaimBoundary.ADJACENT),
        ranking_question=RankingQuestion.INTRINSIC_FIT,
        evidence_supported=True,
    )

    assert judgement.claim_boundaries == (ClaimBoundary.TEAM_CONTRIBUTION, ClaimBoundary.ADJACENT)
    assert judgement.ranking_question is RankingQuestion.INTRINSIC_FIT


def test_evidence_assessment_is_deterministic_first_and_can_abstain() -> None:
    exact = EvidenceAssessment(
        resolution=EvidenceResolution.EXACT,
        confidence=Decimal("0.99"),
        status=EvidenceStatus.VERIFIED,
    )
    interpreted = EvidenceAssessment(
        resolution=EvidenceResolution.INTERPRETED,
        confidence=Decimal("0.60"),
        status=EvidenceStatus.UNRESOLVED,
    )

    assert not exact.needs_review
    assert interpreted.needs_review


def test_derivative_subset_and_critic_rules_are_falsifiable() -> None:
    with pytest.raises(ValueError, match="subset"):
        DerivativeSubset(
            master_evidence_ids=frozenset({"master-1"}),
            selected_evidence_ids=frozenset({"invented-1"}),
        )
    with pytest.raises(ValueError, match="taste-only"):
        CriticFinding(taste_only=True, triggers_redraft=True)

    finding = CriticFinding(
        defect=DefectClass.GENERIC_VOICE,
        triggers_redraft=True,
    )
    assert finding.triggers_redraft


def test_evaluation_history_allows_deferred_retry_but_requires_explicit_rescoring() -> None:
    validate_evaluation_history(
        (
            EvaluationAttempt(
                attempt_id="attempt-1", posting_id="posting-1", outcome=EvaluationOutcome.DEFERRED
            ),
            EvaluationAttempt(
                attempt_id="attempt-2", posting_id="posting-1", outcome=EvaluationOutcome.COMPLETED
            ),
            EvaluationAttempt(
                attempt_id="attempt-3",
                posting_id="posting-1",
                outcome=EvaluationOutcome.COMPLETED,
                explicit_re_evaluation=True,
            ),
        )
    )
    with pytest.raises(ValueError, match="more than once"):
        validate_evaluation_history(
            (
                EvaluationAttempt(
                    attempt_id="attempt-1",
                    posting_id="posting-1",
                    outcome=EvaluationOutcome.COMPLETED,
                ),
                EvaluationAttempt(
                    attempt_id="attempt-2",
                    posting_id="posting-1",
                    outcome=EvaluationOutcome.COMPLETED,
                ),
            )
        )


def test_semantic_evaluation_swaps_candidate_order_and_aggregates_defects() -> None:
    annotations = tuple(_annotation(f"case-{index}") for index in range(4))
    executor = _Executor()

    report = run_semantic_evaluation(annotations, executor, _config())

    assert len(report.results) == 8
    assert all(set(plan.candidate_order) == {"baseline", "candidate"} for plan in report.plans)
    assert [candidate for _, candidate in executor.calls] == [
        candidate for plan in report.plans for candidate in plan.candidate_order
    ]
    assert report.defect_counts == {DefectClass.GENERIC_VOICE: 4}
    assert report.hard_failure_counts == {}
    assert report.usage == Usage(input_tokens=80, output_tokens=32)
    assert report.dollars == Decimal("0.80")
    assert all(result.content == {} for result in report.results)
    assert report.artifact_files == ()


def test_a_baseline_runs_each_case_once_because_it_has_nothing_to_compare() -> None:
    """A baseline that ran the blinded pair would pay twice for one measurement."""
    executor = _Executor()
    annotations = tuple(_annotation(f"case-{index}") for index in range(3))

    report = run_semantic_evaluation(annotations, executor, _config(), mode=RunMode.BASELINE)

    assert [candidate for _, candidate in executor.calls] == ["baseline"] * 3
    assert len(report.results) == 3
    assert all(plan.candidate_order == ("baseline",) for plan in report.plans)
    assert report.dollars == Decimal("0.30")


def test_a_report_totals_the_hosted_searches_no_token_count_shows() -> None:
    report = run_semantic_evaluation((_annotation("case"),), _Executor(searches=3), _config())

    assert report.searches == 6


def test_semantic_evaluation_captures_content_only_in_the_private_root(tmp_path: Path) -> None:
    report = run_semantic_evaluation(
        (_annotation("case"),),
        _Executor(),
        _config(artifact_dir=tmp_path, capture_content=True),
    )

    assert len(report.artifact_files) == 2
    for name in report.artifact_files:
        path = tmp_path / name
        assert path.is_file()
        assert "private response" in path.read_text(encoding="utf-8")


def test_captured_content_cannot_be_written_into_the_repository() -> None:
    """Exact prompts and answers may not land in the published tree.

    Being gitignored would not make it safe, because the public-data audit skips
    ignored files. The refusal comes at configuration time, before a run pays.
    """
    inside = Path(__file__).resolve().parent.parent.parent / "evals" / "captured"

    with pytest.raises(ValueError, match="outside the repository working tree"):
        _config(artifact_dir=inside, capture_content=True)


def test_semantic_evaluation_retains_unpriced_results() -> None:
    report = run_semantic_evaluation((_annotation("case"),), _Executor(dollars=None), _config())
    assert len(report.results) == 2
    assert report.dollars is None


def test_all_paid_results_are_retained_above_the_estimate(tmp_path: Path) -> None:
    annotations = tuple(_annotation(f"case-{index}") for index in range(5))
    report = run_semantic_evaluation(
        annotations,
        _Executor(),
        _config(artifact_dir=tmp_path, capture_content=True, max_dollars="0.50"),
    )
    assert len(report.results) == 10
    assert report.dollars == Decimal("1.00")
    assert len(report.artifact_files) == 10
    for name in report.artifact_files:
        assert (tmp_path / name).is_file()


def test_an_executor_failure_keeps_the_cases_already_paid_for(tmp_path: Path) -> None:
    """A provider error partway through must not cost the whole run's evidence."""

    class _Failing(_Executor):
        def run(self, annotation: SemanticAnnotation, candidate: str) -> SemanticCaseResult:
            if len(self.calls) == 2:
                raise RuntimeError("provider went away")
            return super().run(annotation, candidate)

    annotations = tuple(_annotation(f"case-{index}") for index in range(4))

    with pytest.raises(SemanticRunError, match="provider went away") as raised:
        run_semantic_evaluation(
            annotations,
            _Failing(),
            _config(artifact_dir=tmp_path, capture_content=True),
        )

    report = raised.value.report
    assert report is not None
    assert len(report.results) == 2
    assert len(report.artifact_files) == 2


def test_a_report_names_every_stage_a_different_model_answered() -> None:
    """A comparison whose model moved under it explains a difference it did not cause.

    Both vendors ship pinned snapshots today, so this is what an alias or a
    routed deprecation would look like rather than a setting anyone can choose.
    """
    report = run_semantic_evaluation(
        (_annotation("case"),), _Executor(served="test-model-2026-02-16"), _config()
    )

    assert not report.reproducible
    assert len(report.substitutions) == 2
    assert "asked test-model, served test-model-2026-02-16" in report.substitutions[0]


def test_a_report_is_reproducible_when_every_stage_ran_on_the_model_it_named() -> None:
    report = run_semantic_evaluation((_annotation("case"),), _Executor(), _config())

    assert report.reproducible
    assert report.substitutions == ()


def test_enabled_semantic_configuration_requires_approval() -> None:
    with pytest.raises(ValueError, match="approval_id"):
        SemanticRunConfig(enabled=True)
