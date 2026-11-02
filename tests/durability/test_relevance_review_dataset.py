"""The synthetic relevance-review corpus remains unlabeled and citable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from evals.inspect.relevance_review_dataset import (
    CORPUS_PATH,
    REQUIRED_CONTEXT_HEADERS,
    RelevanceReviewCorpusError,
    load_relevance_review_corpus,
)
from evals.support.inventory import synthetic_inventory

from tailorcv.application.discovery.search_parameter_summary import search_summary
from tailorcv.domain.search_parameters import (
    Employment,
    JobSearchParameters,
    Lane,
    Location,
    Seniority,
    TargetTitle,
)


def _first_payload() -> dict[str, object]:
    return cast(
        dict[str, object], json.loads(CORPUS_PATH.read_text(encoding="utf-8").splitlines()[0])
    )


def test_checked_in_corpus_is_synthetic_input_with_stable_citations() -> None:
    cases = load_relevance_review_corpus()

    assert len(cases) == 16
    assert {case.split for case in cases} == {"development"}
    assert {case.cv_hash for case in cases} == {synthetic_inventory().content_hash}
    assert all(case.citation_ids[0] == f"{case.id}:span-0001" for case in cases)
    assert all("expected" not in case.input.model_dump_json() for case in cases)


def test_no_accepted_case_names_the_synthetic_candidate_own_employer() -> None:
    """An employer shared with the CV makes a posting look like an internal move."""
    employers = {case.input.posting.employer for case in load_relevance_review_corpus()}

    assert len(employers) == 16
    assert not any("Northwind" in employer for employer in employers)


def test_the_required_context_headers_are_the_shipped_search_summary_sections() -> None:
    """Pin the corpus contract to the renderer, so a relabeled section fails here."""
    rendered = search_summary(
        JobSearchParameters(
            opportunity_thesis="Platform work that keeps payment systems reliable",
            seniority=Seniority(
                target_titles=[
                    TargetTitle(title="Senior Data Platform Engineer", lane=Lane.SPECIALIST)
                ],
                excluded_titles=["Vice President of Engineering"],
            ),
            employment=Employment(types=["full_time"], payroll_arrangements=["Canadian payroll"]),
            locations=[Location(key="calgary", country="Canada", region="Alberta")],
            excluded_conditions=[("on site five days a week", "no daily office attendance")],
        )
    )

    for header in REQUIRED_CONTEXT_HEADERS:
        assert header in rendered


def test_loader_rejects_a_free_prose_search_context(tmp_path: Path) -> None:
    payload = _first_payload()
    input_payload = cast(dict[str, object], payload["input"])
    input_payload["search_context"] = "Evaluate this posting against the synthetic CV inventory."
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RelevanceReviewCorpusError, match="search_context is missing"):
        load_relevance_review_corpus(path)


def test_loader_rejects_a_corpus_for_a_different_synthetic_cv(tmp_path: Path) -> None:
    payload = _first_payload()
    payload["cv_hash"] = "0" * 16
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RelevanceReviewCorpusError, match="expected"):
        load_relevance_review_corpus(path)


def test_loader_rejects_a_label_in_the_public_input_fixture(tmp_path: Path) -> None:
    payload = _first_payload()
    payload["expected_verdict"] = "strong"
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RelevanceReviewCorpusError, match="expected_verdict"):
        load_relevance_review_corpus(path)


def test_loader_rejects_a_posting_identifier_from_another_case(tmp_path: Path) -> None:
    payload = _first_payload()
    input_payload = cast(dict[str, object], payload["input"])
    posting_payload = cast(dict[str, object], input_payload["posting"])
    posting_payload["id"] = "rrp-004"
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RelevanceReviewCorpusError, match="case id must match"):
        load_relevance_review_corpus(path)


def test_loader_rejects_duplicate_case_identifiers(tmp_path: Path) -> None:
    payload = json.dumps(_first_payload())
    path = tmp_path / "corpus.jsonl"
    path.write_text(f"{payload}\n{payload}\n", encoding="utf-8")

    with pytest.raises(RelevanceReviewCorpusError, match="duplicate case id"):
        load_relevance_review_corpus(path)
