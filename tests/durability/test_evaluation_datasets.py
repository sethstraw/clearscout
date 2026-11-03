"""Sanitized historical annotations stay strict and reproducible."""

from pathlib import Path

import pytest
from evals.dataset import (
    DatasetError,
    FitAnnotation,
    RetrievalAnnotation,
    load_annotation,
    load_dataset,
)

from clearscout.domain.records import EvaluationDecision

FIT_ANNOTATION = """
annotation_id = "historical-fit"
task = "fit"
split = "regression"
suites = ["adversarial"]
retrieved_on = 2025-04-03
adjudicated_on = 2025-04-04
artifact_ref = "historical-posting-001"
sanitized_content_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
rubric_version = "historical-v1"
sanitized = true
privacy_review_passed = true
redistribution_approved = true
hard_gate = "clear"
decision = "premium_target"
tags = ["leadership", "canada"]

[scores]
role_fit = 85
compensation = 75
eligibility = 100
evidence = 70
opportunity = 90
"""

RETRIEVAL_ANNOTATION = """
annotation_id = "recent-discovery"
task = "retrieval"
split = "holdout"
retrieved_on = 2026-08-11
adjudicated_on = 2026-08-12
artifact_ref = "recent-discovery-001"
sanitized_content_sha256 = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
rubric_version = "retrieval-v1"
sanitized = true
privacy_review_passed = true
redistribution_approved = true
relevance = "adjacent"
disposition = "investigate"
tags = ["specialist", "canada"]
"""


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_annotations_load_in_filename_order_with_distinct_task_contracts(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "recent-discovery", RETRIEVAL_ANNOTATION)
    _write(tmp_path, "historical-fit", FIT_ANNOTATION)

    annotations = load_dataset(tmp_path)

    assert [item.annotation_id for item in annotations] == [
        "historical-fit",
        "recent-discovery",
    ]
    assert isinstance(annotations[0], FitAnnotation)
    assert isinstance(annotations[1], RetrievalAnnotation)
    assert annotations[0].decision is EvaluationDecision.PREMIUM_TARGET
    assert annotations[0].scores.model_dump() == {
        "role_fit": 85,
        "compensation": 75,
        "eligibility": 100,
        "evidence": 70,
        "opportunity": 90,
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("sanitized = true", "sanitized = false"),
        ("privacy_review_passed = true", "privacy_review_passed = false"),
        ("redistribution_approved = true", "redistribution_approved = false"),
    ],
)
def test_annotations_require_every_publication_gate(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    path = _write(tmp_path, "recent-discovery", RETRIEVAL_ANNOTATION.replace(field, replacement))

    with pytest.raises(DatasetError, match=field.partition(" =")[0]):
        load_annotation(path)


@pytest.mark.parametrize(
    "artifact_ref",
    # An absolute path without a home-directory shape: the rule under test is
    # that a reference carries no path at all, and a literal /Users/... here
    # would be a machine path the public-data audit refuses on sight.
    ["/absolute/private/posting.md", "../private/posting.md", "https://example.com/job"],
)
def test_artifact_references_cannot_embed_paths_or_urls(
    tmp_path: Path,
    artifact_ref: str,
) -> None:
    body = RETRIEVAL_ANNOTATION.replace(
        'artifact_ref = "recent-discovery-001"',
        f'artifact_ref = "{artifact_ref}"',
    )
    path = _write(tmp_path, "recent-discovery", body)

    with pytest.raises(DatasetError, match="artifact_ref"):
        load_annotation(path)


def test_annotation_identifier_must_match_its_file_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "different-name", RETRIEVAL_ANNOTATION)

    with pytest.raises(DatasetError, match="does not match the file name"):
        load_annotation(path)


def test_fit_annotations_reject_unknown_decisions(tmp_path: Path) -> None:
    invented = FIT_ANNOTATION.replace('decision = "premium_target"', 'decision = "great_match"')
    path = _write(tmp_path, "historical-fit", invented)

    with pytest.raises(DatasetError, match="decision"):
        load_annotation(path)


@pytest.mark.parametrize("score", [-1, 101])
def test_fit_scores_stay_on_the_historical_scale(tmp_path: Path, score: int) -> None:
    body = FIT_ANNOTATION.replace("role_fit = 85", f"role_fit = {score}")
    path = _write(tmp_path, "historical-fit", body)

    with pytest.raises(DatasetError, match="role_fit"):
        load_annotation(path)


def test_fit_scores_state_an_abstention_the_scoring_policy_actually_makes(
    tmp_path: Path,
) -> None:
    """Compensation and eligibility abstain, and the label has to be able to say so."""
    body = FIT_ANNOTATION.replace(
        "compensation = 75",
        'compensation = { state = "not_stated", reason = "The posting states no comparable pay." }',
    )
    annotation = load_annotation(_write(tmp_path, "historical-fit", body))

    assert isinstance(annotation, FitAnnotation)
    assert annotation.scores.abstained() == ("compensation",)
    assert annotation.scores.role_fit == 85


def test_a_forgotten_score_cannot_pass_as_an_abstention(tmp_path: Path) -> None:
    body = FIT_ANNOTATION.replace("compensation = 75\n", "")
    path = _write(tmp_path, "historical-fit", body)

    with pytest.raises(DatasetError, match="compensation"):
        load_annotation(path)


def test_actionable_fit_annotations_require_a_clear_hard_gate(tmp_path: Path) -> None:
    body = FIT_ANNOTATION.replace('hard_gate = "clear"', 'hard_gate = "unknown"')
    path = _write(tmp_path, "historical-fit", body)

    with pytest.raises(DatasetError, match="actionable decision requires a clear"):
        load_annotation(path)


def test_ineligible_fit_annotations_require_a_blocked_hard_gate(tmp_path: Path) -> None:
    body = FIT_ANNOTATION.replace('decision = "premium_target"', 'decision = "ineligible"')
    path = _write(tmp_path, "historical-fit", body)

    with pytest.raises(DatasetError, match="requires a blocked hard gate"):
        load_annotation(path)


def test_rejected_relevant_rows_require_a_closed_reason(tmp_path: Path) -> None:
    body = RETRIEVAL_ANNOTATION.replace('disposition = "investigate"', 'disposition = "reject"')
    path = _write(tmp_path, "recent-discovery", body)

    with pytest.raises(DatasetError, match="requires disposition_reason"):
        load_annotation(path)


def test_retrieval_annotations_reject_fit_only_labels(tmp_path: Path) -> None:
    path = _write(tmp_path, "recent-discovery", RETRIEVAL_ANNOTATION + '\nhard_gate = "clear"\n')

    with pytest.raises(DatasetError, match="hard_gate"):
        load_annotation(path)


def test_annotations_reject_adjudication_before_retrieval(tmp_path: Path) -> None:
    body = RETRIEVAL_ANNOTATION.replace(
        "adjudicated_on = 2026-08-12",
        "adjudicated_on = 2026-08-10",
    )
    path = _write(tmp_path, "recent-discovery", body)

    with pytest.raises(DatasetError, match="cannot predate retrieved_on"):
        load_annotation(path)


def test_dataset_rejects_sanitized_artifact_leakage_between_splits(tmp_path: Path) -> None:
    _write(tmp_path, "historical-fit", FIT_ANNOTATION)
    leaked = RETRIEVAL_ANNOTATION.replace(
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    _write(tmp_path, "recent-discovery", leaked)

    with pytest.raises(DatasetError, match="cross dataset splits"):
        load_dataset(tmp_path)


def test_dataset_rejects_duplicate_tags(tmp_path: Path) -> None:
    body = RETRIEVAL_ANNOTATION.replace(
        'tags = ["specialist", "canada"]',
        'tags = ["canada", "canada"]',
    )
    _write(tmp_path, "recent-discovery", body)

    with pytest.raises(DatasetError, match="tags must not contain duplicates"):
        load_dataset(tmp_path)


def test_dataset_rejects_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="contains no semantic annotations"):
        load_dataset(tmp_path)


def test_malformed_toml_names_the_annotation_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken", 'annotation_id = "broken"\ntask = [')

    with pytest.raises(DatasetError, match="broken.toml"):
        load_annotation(path)
