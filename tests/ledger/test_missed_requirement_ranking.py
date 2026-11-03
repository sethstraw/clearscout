"""Ranker downranks a discovery whose title carries a missed requirement text."""

from __future__ import annotations

from clearscout.application.discovery.ranking import (
    DEFAULT_MISSED_REQUIREMENT_PENALTY,
    rank,
)
from clearscout.domain.search_parameters import JobSearchParameters


def _empty_parameters() -> JobSearchParameters:
    return JobSearchParameters()


def test_rank_applies_no_penalty_when_missed_set_is_empty() -> None:
    """An empty missed set ranks exactly as passing none at all."""
    baseline = rank("Backend Engineer", "Remote", _empty_parameters(), [])
    with_empty = rank(
        "Backend Engineer",
        "Remote",
        _empty_parameters(),
        [],
        missed_requirement_texts=frozenset(),
    )

    assert baseline.score == with_empty.score
    assert baseline.because == with_empty.because


def test_rank_applies_penalty_when_title_carries_a_missed_text() -> None:
    """One matched missed text subtracts the default penalty from the score."""
    baseline = rank("Senior Kubernetes Engineer", "Remote", _empty_parameters(), [])

    with_penalty = rank(
        "Senior Kubernetes Engineer",
        "Remote",
        _empty_parameters(),
        [],
        missed_requirement_texts=frozenset({"kubernetes"}),
    )

    assert with_penalty.score == baseline.score + DEFAULT_MISSED_REQUIREMENT_PENALTY
    assert any("missed before" in reason for reason in with_penalty.because)


def test_rank_stacks_penalty_when_more_than_one_missed_text_matches() -> None:
    """The penalty compounds by the count of distinct matches, not by weight."""
    baseline = rank("Senior Kubernetes and Terraform Engineer", "Remote", _empty_parameters(), [])

    with_penalty = rank(
        "Senior Kubernetes and Terraform Engineer",
        "Remote",
        _empty_parameters(),
        [],
        missed_requirement_texts=frozenset({"kubernetes", "terraform"}),
    )

    assert with_penalty.score == baseline.score + 2 * DEFAULT_MISSED_REQUIREMENT_PENALTY


def test_rank_honours_explicit_penalty_argument() -> None:
    """A caller-supplied penalty overrides the default, including zero."""
    baseline = rank("Kubernetes Engineer", "Remote", _empty_parameters(), [])
    disabled = rank(
        "Kubernetes Engineer",
        "Remote",
        _empty_parameters(),
        [],
        missed_requirement_texts=frozenset({"kubernetes"}),
        missed_requirement_penalty=0,
    )

    assert disabled.score == baseline.score
    assert all("missed before" not in reason for reason in disabled.because)


def test_rank_also_reads_the_residence_line() -> None:
    """A missed text in the residence rule counts the same as one in the title."""
    baseline = rank("Backend Engineer", "Remote within Canada", _empty_parameters(), [])

    with_penalty = rank(
        "Backend Engineer",
        "Remote within Canada",
        _empty_parameters(),
        [],
        missed_requirement_texts=frozenset({"canada"}),
    )

    assert with_penalty.score == baseline.score + DEFAULT_MISSED_REQUIREMENT_PENALTY
