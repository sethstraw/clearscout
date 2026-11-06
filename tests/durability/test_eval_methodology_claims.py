"""The evaluation methodology's claims about dataset shape and verifier coverage.

A reader trusts the methodology when judging whether a measurement means
anything, so its checkable claims are asserted here against the tracked
evaluation fixtures and the shipped verifiers.

``PLANTED_CHECKS`` and ``UNPLANTED_CHECKS`` are the coverage ledger:
``check_coverage_is_declared`` refuses a shipped check nobody has decided about,
and a declared check no shipped verifier runs.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from evals.inspect import runner
from evals.inspect.solvers.document_scripted import (
    cover_scripted_solver,
    draft_scripted_solver,
)
from evals.runner import EvalError
from evals.support.documents import research_sources
from evals.support.inventory import synthetic_inventory

from clearscout.application.tailoring.stages.verify import SUPPORT_CHECKS
from clearscout.shared.results import Outcome

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"
BASELINES = Path(__file__).resolve().parents[2] / "evals" / "baselines"

DOCUMENT_DATASETS = ("draft_cases.jsonl", "cover_cases.jsonl", "critic_cases.jsonl")
MATCH_DATASETS = ("match_scripted.jsonl", "match_scripted_multi.jsonl")
CLASSIFICATIONS = frozenset({"DIRECT", "TAILORING_ONLY", "TRANSFERABLE", "ASSERTED", "GAP"})

# The shipped check each document case plants a defect for, by case id.
PLANTED_CHECKS = {
    "sources cited": "uncited_summary",
    "numbers traced": "invented_figure",
    "duration scope": "moved_duration",
    "first person": "first_person_summary",
    "must-have coverage": "dropped_must_have",
    "ownership verbs": "promoted_ownership",
    "letter provenance": "uncited_paragraph",
    "letter numbers": "invented_letter_figure",
    "letter length": "overlong_letter",
    "letter echo": "echoed_evidence",
    "letter closing": "closing_on_what_work_taught",
    "letter source talk": "posting_as_subject",
}

# A shipped check no case plants a defect for, and the input that is missing.
UNPLANTED_CHECKS = {
    "skills grouping": (
        "No document case regroups the CV's own skills headings, so nothing here "
        "would notice this check going silent."
    ),
    "factual review": (
        "Every case observes this standing warning, and no case plants a defect "
        "that turns it into a failure."
    ),
    "repeated phrasing": (
        "No document case repeats one drafted phrasing across lines, so nothing "
        "here would notice this check going silent."
    ),
    "document structure": (
        "No document case assembles a resume with sections missing, so nothing "
        "here would notice this check going silent."
    ),
    "character policy": (
        "No document case writes non-ASCII punctuation into drafted text, so "
        "nothing here would notice this check going silent."
    ),
    "source characters": (
        "No document case copies non-ASCII punctuation out of the CV, so nothing "
        "here would notice this check going silent."
    ),
    "forbidden strings": (
        "The defect this check catches is planted in unit tests rather than in a "
        "dataset case, so nothing here would notice this check going silent."
    ),
    "required strings": (
        "The defect this check catches is planted in unit tests rather than in a "
        "dataset case, so nothing here would notice this check going silent."
    ),
    "protected strings": (
        "The defect this check catches is planted in unit tests rather than in a "
        "dataset case, so nothing here would notice this check going silent."
    ),
    "keyword coverage": (
        "No document case drops posting keywords below the shipped floor, so "
        "nothing here would notice this check going silent."
    ),
    "letter completeness": (
        "No cover case omits the contact details a letter must carry, so nothing "
        "here would notice this check going silent."
    ),
    "claim ownership": (
        "Every cover case supplies the posting as company research, but no case writes "
        "a first-person sentence into a paragraph citing it, so nothing here would "
        "notice this check going silent."
    ),
}


def cases(dataset: str) -> list[dict[str, Any]]:
    """Return every case in a tracked evaluation fixture."""
    return [
        json.loads(line)
        for line in (FIXTURES / dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@lru_cache(maxsize=1)
def shipped_checks() -> frozenset[str]:
    """Return the check names the shipped resume and letter verifiers emit."""
    resume = draft_scripted_solver()(cases("draft_cases.jsonl")[0])
    letter = cover_scripted_solver()(cases("cover_cases.jsonl")[0])
    return frozenset(
        finding["check"] for report in (resume, letter) for finding in report["findings"]
    )


def check_coverage_is_declared(shipped: frozenset[str] | None = None) -> None:
    """Raise when a shipped verifier check carries no stated coverage decision."""
    names = shipped_checks() if shipped is None else shipped
    both = set(PLANTED_CHECKS) & set(UNPLANTED_CHECKS)
    if both:
        raise EvalError(f"checks declared both planted and not: {sorted(both)}")
    declared = set(PLANTED_CHECKS) | set(UNPLANTED_CHECKS)
    undeclared = names - declared
    if undeclared:
        raise EvalError(f"shipped checks nobody decided about: {sorted(undeclared)}")
    retired = declared - names
    if retired:
        raise EvalError(f"declared checks no shipped verifier runs: {sorted(retired)}")


def test_the_ledger_passes_on_the_checks_as_they_ship() -> None:
    check_coverage_is_declared()


def test_a_new_shipped_check_fails_until_somebody_decides_about_it() -> None:
    with pytest.raises(EvalError) as raised:
        check_coverage_is_declared(shipped_checks() | {"salary range"})

    assert "salary range" in str(raised.value)


def test_a_check_no_verifier_runs_any_more_is_refused() -> None:
    """A deleted check leaves a ledger row that would otherwise pass unnoticed."""
    with pytest.raises(EvalError) as raised:
        check_coverage_is_declared(shipped_checks() - {"skills grouping"})

    assert "skills grouping" in str(raised.value)


def test_a_check_declared_both_ways_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(UNPLANTED_CHECKS, "sources cited", "claimed by both maps")

    with pytest.raises(EvalError) as raised:
        check_coverage_is_declared()

    assert "both planted and not" in str(raised.value)


def _reasons_are_sentences() -> None:
    for name, reason in UNPLANTED_CHECKS.items():
        assert reason.strip().endswith("."), name
        assert len(reason.split()) >= 8, name


def test_every_unplanted_reason_reads_as_a_sentence_a_reviewer_can_disagree_with() -> None:
    _reasons_are_sentences()


def test_a_fragment_instead_of_a_reason_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fragment gives a reviewer nothing concrete to push back on."""
    monkeypatch.setitem(UNPLANTED_CHECKS, "skills grouping", "no case reaches this")

    with pytest.raises(AssertionError):
        _reasons_are_sentences()


def test_every_planted_check_names_a_case_that_declares_it() -> None:
    """The ledger claims a case pins the check, so the case has to say so."""
    declared = {
        case["id"]: set(case["expect"]["failing_checks"]) | set(case["expect"]["warning_checks"])
        for dataset in ("draft_cases.jsonl", "cover_cases.jsonl")
        for case in cases(dataset)
    }

    for name, sample_id in PLANTED_CHECKS.items():
        assert name in declared[sample_id], name


def test_claim_ownership_is_a_support_check_no_document_case_reaches() -> None:
    """METHODOLOGY names this the gap worth stating, so both halves are asserted."""
    assert "claim ownership" in SUPPORT_CHECKS
    assert "claim ownership" not in PLANTED_CHECKS
    for case in cases("cover_cases.jsonl"):
        assert research_sources(case)[0].id == "company.posting", case["id"]
        outcomes = {
            finding["check"]: finding["outcome"]
            for finding in cover_scripted_solver()(case)["findings"]
        }
        assert outcomes["claim ownership"] == Outcome.PASS.value, case["id"]


def test_factual_review_is_a_standing_warning_on_every_resume_case() -> None:
    """A clean resume run is a grounding floor only because this warning never clears."""
    for case in cases("draft_cases.jsonl"):
        assert "factual review" in case["expect"]["warning_checks"], case["id"]


def test_the_scripted_match_contract_reaches_all_five_classifications() -> None:
    """The scripted mode claims to compose every classification, not a convenient subset."""
    scripted = {
        case["metadata"]["scripted_classification"] for case in cases("match_scripted.jsonl")
    }

    assert scripted == CLASSIFICATIONS


def test_no_tracked_match_fixture_carries_a_reviewed_label() -> None:
    """The scripted mode never receives the independently reviewed labels."""
    for dataset in MATCH_DATASETS:
        for case in cases(dataset):
            assert "expected_classification" not in case, case["id"]
            assert "reviewer" not in case, case["id"]


def test_every_tracked_match_fixture_names_the_synthetic_inventory_by_cv_hash() -> None:
    """A sample tied to another inventory needs its own wiring before the harness runs it."""
    expected = synthetic_inventory().content_hash

    for dataset in MATCH_DATASETS:
        for case in cases(dataset):
            assert case["cv_hash"] == expected, case["id"]


def test_a_match_solver_is_shown_the_dimension_and_not_the_frozen_label() -> None:
    """The dimension steers the shipped call; the label and reviewer are scorer-only."""
    sample = {
        "id": "case-1",
        "input": "Production Python service development",
        "requirement_key": "case-1",
        "dimension": "hard",
        "cv_hash": synthetic_inventory().content_hash,
        "split": "development",
        "expected_classification": "DIRECT",
        "reviewer": "reviewer-a",
    }

    projected = runner._solver_input(sample)

    assert projected["dimension"] == "hard"
    assert "expected_classification" not in projected
    assert "reviewer" not in projected


def test_no_document_case_carries_a_reviewer_label() -> None:
    """The document families score through the shipped verifiers, not a reviewed label."""
    for dataset in DOCUMENT_DATASETS:
        for case in cases(dataset):
            assert "expected_classification" not in case, case["id"]
            assert "reviewer" not in case, case["id"]
            assert set(case["expect"]) <= {
                "failing_checks",
                "warning_checks",
                "critic_outcome",
                "unsupported_claim",
            }, case["id"]


def test_every_critic_and_drafting_case_carries_its_own_canned_draft() -> None:
    """One shared draft would make every declared outcome but one unreachable."""
    for dataset in ("critic_cases.jsonl", "draft_cases.jsonl"):
        sampled = cases(dataset)

        drafts = {
            json.dumps(
                case["metadata"].get("scripted_draft") or case["metadata"]["scripted_letter"]
            )
            for case in sampled
        }

        assert len(drafts) == len(sampled), dataset


def test_the_drafting_families_share_one_candidate_cv() -> None:
    """A differing result between cases is about the posting, never about the person."""
    candidates = {
        (case["metadata"]["cv"], case["cv_hash"])
        for dataset in ("draft_cases.jsonl", "cover_cases.jsonl")
        for case in cases(dataset)
    }
    critic = {(case["metadata"]["cv"], case["cv_hash"]) for case in cases("critic_cases.jsonl")}

    # Critic cases on a second CV compare only against a control on that CV.
    assert len(candidates) == 1
    assert candidates <= critic


def test_every_baseline_records_a_scripted_run() -> None:
    """The methodology calls the baselines a deterministic suite, so none may hold live output.

    A baseline recorded against a provider would move with model quality, which
    is the coverage the methodology says these files do not give.
    """
    stored = sorted(BASELINES.glob("*.json"))
    assert stored, "no baselines found"

    sources = {
        path.name: json.loads(path.read_text(encoding="utf-8")).get("source") for path in stored
    }

    assert {name: source for name, source in sources.items() if source != "scripted"} == {}
