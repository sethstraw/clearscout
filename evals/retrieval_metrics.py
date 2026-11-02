"""Deterministic retrieval metrics over human-adjudicated candidates."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from tailorcv.application.discovery.ranking import DiscoverySignal


@dataclass(frozen=True)
class CandidateJudgement:
    """Human truth for one regional representation of an opportunity."""

    candidate_id: str
    opportunity_id: str
    eligible: bool | None
    should_stage: bool


@dataclass(frozen=True)
class CandidateOutcome:
    """The deterministic pipeline result for one candidate."""

    candidate_id: str
    rank: int | None
    staged: bool
    passed_hard_gate: bool | None
    # A board row nobody checked is not a verified opening.
    verified: bool = False
    viable: bool = False
    stale: bool = False
    discovery_signal: DiscoverySignal | None = None


@dataclass(frozen=True)
class CoverageRun:
    """Requested and completed discovery facets for one measured run."""

    requested_sources: frozenset[str] = frozenset()
    completed_sources: frozenset[str] = frozenset()
    requested_employer_families: frozenset[str] = frozenset()
    completed_employer_families: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Rate:
    """A count-backed rate that preserves an undefined denominator."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        """Reject counts that cannot describe a rate."""
        if self.numerator < 0 or self.denominator < 0:
            raise ValueError("rate counts cannot be negative")
        if self.numerator > self.denominator:
            raise ValueError("rate numerator cannot exceed its denominator")

    @property
    def value(self) -> float | None:
        """Return the rate, or None when there was nothing to measure."""
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    def wilson_interval(self, z: float = 1.96) -> tuple[float, float] | None:
        """Return the Wilson score interval, or None with no denominator.

        z defaults to 1.96, the two-sided 95% score.
        """
        if self.denominator == 0:
            return None
        n = self.denominator
        phat = self.numerator / n
        span = 1.0 + z * z / n
        center = (phat + z * z / (2 * n)) / span
        margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4 * n * n)) / span
        return max(0.0, center - margin), min(1.0, center + margin)


@dataclass(frozen=True)
class CostPerViableCandidate:
    """A cost with its viable-candidate denominator preserved."""

    total_dollars: Decimal | None
    viable_candidates: int

    def __post_init__(self) -> None:
        """Reject a cost that cannot describe a measured run."""
        if self.total_dollars is not None and self.total_dollars < 0:
            raise ValueError("total dollars cannot be negative")
        if self.viable_candidates < 0:
            raise ValueError("viable candidates cannot be negative")

    @property
    def value(self) -> Decimal | None:
        """Return the unit cost, or None when cost or yield is unknown."""
        if self.total_dollars is None or self.viable_candidates == 0:
            return None
        return self.total_dollars / self.viable_candidates


@dataclass(frozen=True)
class DiscoverySignalCount:
    """Total and viable results attributed to one signal, or to none of them."""

    signal: DiscoverySignal | None
    total: int
    viable: int

    def __post_init__(self) -> None:
        """Reject impossible signal counts."""
        if self.total < 0 or self.viable < 0 or self.viable > self.total:
            raise ValueError("discovery signal counts are inconsistent")


@dataclass(frozen=True)
class AtK:
    """One distinct-opportunity recall result at a rank cutoff."""

    cutoff: int
    recall: Rate


@dataclass(frozen=True)
class RetrievalMetrics:
    """The retrieval measurements used by prompt and pipeline evaluations."""

    eligible_recall: tuple[AtK, ...]
    staged_precision: Rate
    hard_gate_false_positive_rate: Rate
    source_coverage: Rate
    employer_family_coverage: Rate
    regional_duplicate_rate: Rate
    verified_precision: Rate
    stale_link_rate: Rate
    cost_per_viable_candidate: CostPerViableCandidate
    discovery_signal_counts: tuple[DiscoverySignalCount, ...]


def measure_retrieval(
    judgements: Iterable[CandidateJudgement],
    outcomes: Iterable[CandidateOutcome],
    coverage: CoverageRun,
    *,
    cutoffs: tuple[int, ...] = (5, 10, 20),
    total_dollars: Decimal | float | None = None,
) -> RetrievalMetrics:
    """Measure one retrieval run without invoking providers or production code."""
    truth = tuple(judgements)
    actual = tuple(outcomes)
    _validate_inputs(truth, actual, coverage, cutoffs)

    by_candidate = {item.candidate_id: item for item in truth}
    eligible_opportunities = {
        item.opportunity_id for item in truth if item.eligible is True and item.should_stage
    }
    recall = tuple(
        AtK(
            cutoff=cutoff,
            recall=Rate(
                numerator=len(
                    {
                        by_candidate[item.candidate_id].opportunity_id
                        for item in actual
                        if item.rank is not None
                        and item.rank <= cutoff
                        and by_candidate[item.candidate_id].eligible is True
                        and by_candidate[item.candidate_id].should_stage
                    }
                ),
                denominator=len(eligible_opportunities),
            ),
        )
        for cutoff in sorted(cutoffs)
    )

    staged = tuple(item for item in actual if item.staged)
    staged_relevant = sum(by_candidate[item.candidate_id].should_stage for item in staged)
    verified_staged = tuple(item for item in staged if item.verified)
    verified_relevant = sum(
        by_candidate[item.candidate_id].should_stage for item in verified_staged
    )

    known_ineligible = tuple(
        item for item in actual if by_candidate[item.candidate_id].eligible is False
    )
    false_admissions = sum(item.passed_hard_gate is True for item in known_ineligible)

    emitted_opportunities = {by_candidate[item.candidate_id].opportunity_id for item in actual}
    duplicate_representations = len(actual) - len(emitted_opportunities)
    # A signal that surfaced nothing is a finding, so every signal is reported
    # once there is a run to report on, and a row no signal explains is counted
    # as unattributed rather than folded into one that did.
    signal_counts = tuple(
        DiscoverySignalCount(
            signal=signal,
            total=sum(item.discovery_signal is signal for item in actual),
            viable=sum(item.discovery_signal is signal and item.viable for item in actual),
        )
        for signal in (*DiscoverySignal, None)
        if actual
    )

    return RetrievalMetrics(
        eligible_recall=recall,
        staged_precision=Rate(staged_relevant, len(staged)),
        hard_gate_false_positive_rate=Rate(
            false_admissions,
            len(known_ineligible),
        ),
        source_coverage=_coverage_rate(
            coverage.requested_sources,
            coverage.completed_sources,
        ),
        employer_family_coverage=_coverage_rate(
            coverage.requested_employer_families,
            coverage.completed_employer_families,
        ),
        regional_duplicate_rate=Rate(duplicate_representations, len(actual)),
        verified_precision=Rate(verified_relevant, len(verified_staged)),
        stale_link_rate=Rate(sum(item.stale for item in actual), len(actual)),
        cost_per_viable_candidate=CostPerViableCandidate(
            None if total_dollars is None else Decimal(str(total_dollars)),
            sum(item.viable for item in actual),
        ),
        discovery_signal_counts=signal_counts,
    )


def _coverage_rate(requested: frozenset[str], completed: frozenset[str]) -> Rate:
    return Rate(len(requested & completed), len(requested))


def _validate_inputs(
    judgements: tuple[CandidateJudgement, ...],
    outcomes: tuple[CandidateOutcome, ...],
    coverage: CoverageRun,
    cutoffs: tuple[int, ...],
) -> None:
    if not cutoffs or any(cutoff <= 0 for cutoff in cutoffs):
        raise ValueError("cutoffs must contain positive integers")
    if len(set(cutoffs)) != len(cutoffs):
        raise ValueError("cutoffs must not contain duplicates")

    judgement_ids = [item.candidate_id for item in judgements]
    if any(not item.candidate_id.strip() or not item.opportunity_id.strip() for item in judgements):
        raise ValueError("candidate and opportunity identifiers cannot be empty")
    if any(item.should_stage and item.eligible is not True for item in judgements):
        raise ValueError("a candidate should be staged only when eligibility is confirmed")
    if len(set(judgement_ids)) != len(judgement_ids):
        raise ValueError("candidate judgements must have unique identifiers")

    outcome_ids = [item.candidate_id for item in outcomes]
    if any(not item.candidate_id.strip() for item in outcomes):
        raise ValueError("candidate identifiers cannot be empty")
    if len(set(outcome_ids)) != len(outcome_ids):
        raise ValueError("candidate outcomes must have unique identifiers")
    if any(item.staged and item.rank is None for item in outcomes):
        raise ValueError("a staged candidate must have a rank")
    missing = sorted(set(outcome_ids) - set(judgement_ids))
    if missing:
        raise ValueError(f"outcomes have no judgement: {', '.join(missing)}")

    ranks = [item.rank for item in outcomes if item.rank is not None]
    if any(rank <= 0 for rank in ranks):
        raise ValueError("ranks must be positive integers")
    if len(set(ranks)) != len(ranks):
        raise ValueError("ranks must be unique")

    coverage_ids = (
        coverage.requested_sources
        | coverage.completed_sources
        | coverage.requested_employer_families
        | coverage.completed_employer_families
    )
    if any(not identifier.strip() for identifier in coverage_ids):
        raise ValueError("coverage identifiers cannot be empty")
