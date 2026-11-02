"""Retrieval metrics preserve counts, rank, and opportunity identity."""

from decimal import Decimal

import pytest
from evals.retrieval_metrics import (
    CandidateJudgement,
    CandidateOutcome,
    CoverageRun,
    DiscoverySignalCount,
    Rate,
    measure_retrieval,
)

from tailorcv.application.discovery.ranking import DiscoverySignal


def _judgements() -> tuple[CandidateJudgement, ...]:
    return (
        CandidateJudgement("alpha-ca", "alpha", True, True),
        CandidateJudgement("alpha-us", "alpha", True, True),
        CandidateJudgement("bravo", "bravo", True, True),
        CandidateJudgement("closed", "closed", False, False),
        CandidateJudgement("blocked", "blocked", False, False),
        CandidateJudgement("unknown", "unknown", None, False),
        CandidateJudgement("noise", "noise", True, False),
    )


def _outcomes() -> tuple[CandidateOutcome, ...]:
    return (
        CandidateOutcome("alpha-ca", 1, True, True),
        CandidateOutcome("alpha-us", 2, True, True),
        CandidateOutcome("noise", 3, True, True),
        CandidateOutcome("bravo", 4, True, True),
        CandidateOutcome("closed", 5, True, True),
        CandidateOutcome("blocked", None, False, False),
        CandidateOutcome("unknown", None, False, None),
    )


def test_retrieval_metrics_count_distinct_opportunities_and_keep_denominators() -> None:
    metrics = measure_retrieval(
        _judgements(),
        _outcomes(),
        CoverageRun(
            requested_sources=frozenset({"board", "search", "network"}),
            completed_sources=frozenset({"board", "network", "unplanned"}),
            requested_employer_families=frozenset({"terminology", "drug-data"}),
            completed_employer_families=frozenset({"drug-data"}),
        ),
        cutoffs=(4, 1, 2),
    )

    assert [(item.cutoff, item.recall) for item in metrics.eligible_recall] == [
        (1, Rate(1, 2)),
        (2, Rate(1, 2)),
        (4, Rate(2, 2)),
    ]
    assert metrics.staged_precision == Rate(3, 5)
    assert metrics.staged_precision.value == pytest.approx(0.6)
    assert metrics.hard_gate_false_positive_rate == Rate(1, 2)
    assert metrics.source_coverage == Rate(2, 3)
    assert metrics.employer_family_coverage == Rate(1, 2)
    assert metrics.regional_duplicate_rate == Rate(1, 7)


def test_zero_denominators_remain_undefined() -> None:
    metrics = measure_retrieval((), (), CoverageRun(), cutoffs=(5,))

    assert metrics.eligible_recall[0].recall.value is None
    assert metrics.staged_precision.value is None
    assert metrics.hard_gate_false_positive_rate.value is None
    assert metrics.source_coverage.value is None
    assert metrics.employer_family_coverage.value is None
    assert metrics.regional_duplicate_rate.value is None
    assert metrics.verified_precision.value is None
    assert metrics.stale_link_rate.value is None
    assert metrics.cost_per_viable_candidate.value is None
    assert metrics.discovery_signal_counts == ()


@pytest.mark.parametrize(
    ("outcomes", "match"),
    [
        ((CandidateOutcome("missing", 1, False, None),), "no judgement"),
        (
            (
                CandidateOutcome("alpha-ca", 1, False, None),
                CandidateOutcome("alpha-us", 1, False, None),
            ),
            "ranks must be unique",
        ),
        ((CandidateOutcome("alpha-ca", 0, False, None),), "ranks must be positive"),
        ((CandidateOutcome("alpha-ca", None, True, True),), "must have a rank"),
    ],
)
def test_retrieval_metrics_reject_unreproducible_outcomes(
    outcomes: tuple[CandidateOutcome, ...],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        measure_retrieval(_judgements(), outcomes, CoverageRun())


@pytest.mark.parametrize("cutoffs", [(), (0,), (5, 5)])
def test_retrieval_metrics_reject_invalid_cutoffs(cutoffs: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="cutoffs"):
        measure_retrieval(_judgements(), _outcomes(), CoverageRun(), cutoffs=cutoffs)


def test_rate_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        Rate(2, 1)


def test_wilson_interval_brackets_the_rate_and_is_undefined_without_a_denominator() -> None:
    interval = Rate(8, 10).wilson_interval()
    assert interval is not None
    low, high = interval
    assert low == pytest.approx(0.4901, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)
    assert low < 0.8 < high
    assert Rate(0, 0).wilson_interval() is None


def test_wilson_interval_stays_within_the_unit_range_at_the_extremes() -> None:
    assert Rate(3, 3).wilson_interval() == pytest.approx((0.4385, 1.0), abs=1e-4)
    assert Rate(0, 3).wilson_interval() == pytest.approx((0.0, 0.5615), abs=1e-4)


def test_retrieval_metrics_report_verified_precision_staleness_cost_and_signal_counts() -> None:
    metrics = measure_retrieval(
        (
            CandidateJudgement("good", "good", True, True),
            CandidateJudgement("stale", "stale", True, True),
            CandidateJudgement("noise", "noise", True, False),
        ),
        (
            CandidateOutcome(
                "good",
                1,
                True,
                True,
                verified=True,
                viable=True,
                discovery_signal=DiscoverySignal.TITLE,
            ),
            CandidateOutcome(
                "stale",
                2,
                True,
                True,
                verified=False,
                stale=True,
                discovery_signal=DiscoverySignal.TITLE,
            ),
            CandidateOutcome(
                "noise",
                3,
                False,
                True,
                discovery_signal=DiscoverySignal.DOMAIN_INTERSECTION,
            ),
        ),
        CoverageRun(),
        total_dollars=Decimal("0.60"),
    )

    # Staged precision counts the stale row as a success. Verified precision
    # refuses to count it at all, which is the whole difference between them.
    assert metrics.staged_precision == Rate(2, 2)
    assert metrics.verified_precision == Rate(1, 1)
    assert metrics.stale_link_rate == Rate(1, 3)
    assert metrics.cost_per_viable_candidate.value == Decimal("0.60")
    # Responsibility surfaced nothing and is still reported, because an omitted
    # row cannot be told apart from a signal nobody measured.
    assert metrics.discovery_signal_counts == (
        DiscoverySignalCount(DiscoverySignal.TITLE, 2, 1),
        DiscoverySignalCount(DiscoverySignal.RESPONSIBILITY, 0, 0),
        DiscoverySignalCount(DiscoverySignal.DOMAIN_INTERSECTION, 1, 0),
        DiscoverySignalCount(None, 0, 0),
    )


def test_a_candidate_no_signal_explains_is_counted_as_unattributed() -> None:
    """A row nothing surfaced is reported, not folded into a signal that did."""
    metrics = measure_retrieval(
        (
            CandidateJudgement("named", "named", True, True),
            CandidateJudgement("unexplained", "unexplained", True, True),
        ),
        (
            CandidateOutcome(
                "named",
                1,
                True,
                True,
                viable=True,
                discovery_signal=DiscoverySignal.TITLE,
            ),
            CandidateOutcome("unexplained", 2, True, True, viable=True),
        ),
        CoverageRun(),
    )

    assert DiscoverySignalCount(None, 1, 1) in metrics.discovery_signal_counts
    assert DiscoverySignalCount(DiscoverySignal.TITLE, 1, 1) in metrics.discovery_signal_counts
