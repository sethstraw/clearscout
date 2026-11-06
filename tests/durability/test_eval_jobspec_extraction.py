"""The live jobspec extraction task: its dataset, its scorer, and its stage wiring."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest
from evals.inspect.runner import dataset_samples
from evals.inspect.scorers.jobspec_extraction import requirement_extraction
from evals.inspect.solvers.jobspec_live import extraction_result, posting
from evals.inspect.tasks import jobspec_extraction_live_task as task
from scripts.audit_public_data import EVAL_EMPLOYERS

from clearscout.application.config import Stage
from clearscout.application.tailoring.stages.jobspec import (
    Dimension,
    Importance,
    JobSpecExtraction,
    RawCoreOccupation,
    RawHardGate,
    RawRequirement,
    spec_from_extraction,
)

MUST, NICE, DUTY = Importance.MUST_HAVE, Importance.NICE_TO_HAVE, Importance.RESPONSIBILITY


def _samples() -> list[dict[str, Any]]:
    return dataset_samples(task.DATASET_PATH, split="development")


def _case(case_id: str) -> dict[str, Any]:
    return next(row for row in _samples() if row["id"] == case_id)


def _score(
    case_id: str,
    items: Sequence[tuple[str, Importance] | tuple[str, Importance, str]],
    gates: Sequence[RawHardGate] = (),
) -> dict[str, Any]:
    sample = _case(case_id)
    raw = JobSpecExtraction(
        title="Synthetic role",
        requirements=[
            RawRequirement(text=item[-1], quote_or_span=item[0], importance=item[1])
            if len(item) == 3
            else RawRequirement(text=item[0], quote_or_span=item[0], importance=item[1])
            for item in items
        ],
        hard_gates=list(gates),
    )
    spec = spec_from_extraction(raw, posting(sample))
    score: dict[str, Any] = requirement_extraction()(sample, extraction_result(spec))
    return score


DUTY_CASE_ITEMS = [
    ("Build weekly dashboards that track store conversion", DUTY),
    ("2+ years of experience in a product or business analytics role", MUST),
    ("Advanced SQL", MUST),
]


def test_the_task_declares_the_jobspec_stage() -> None:
    assert Stage.JOBSPEC.value == task.STAGE


def test_every_case_loads_with_spans_its_posting_contains() -> None:
    samples = _samples()
    scorer = requirement_extraction()

    assert len({sample["id"] for sample in samples}) == len(samples) >= 8
    for sample in samples:
        scorer.validate_sample(sample)
    assert any(not sample["expected"] for sample in samples)


def test_every_case_names_only_a_synthetic_employer() -> None:
    for sample in _samples():
        employer_line = sample["input"].splitlines()[2]
        assert any(re.match(rf"{name}\b", employer_line) for name in EVAL_EMPLOYERS), sample["id"]


def test_a_label_its_posting_lacks_is_refused() -> None:
    sample = {
        **_case("duty-list"),
        "expected": [{"span": "Fluent Klingon", "importance": "must_have"}],
    }

    with pytest.raises(ValueError, match="lacks"):
        requirement_extraction().validate_sample(sample)


def test_a_complete_extraction_passes_and_ignores_duties() -> None:
    score = _score("duty-list", DUTY_CASE_ITEMS)

    assert score["value"] is True
    assert score["metadata"]["precision"] == score["metadata"]["recall"] == 1.0
    assert score["requirements"] == {"total": 2, "passed": 2}


def test_a_missed_requirement_fails_recall_and_names_the_miss() -> None:
    score = _score("duty-list", DUTY_CASE_ITEMS[:2])

    assert score["value"] is False
    assert score["metadata"]["recall"] == 0.5
    missed = [row["span"] for row in score["metadata"]["expected"] if row["outcome"] == "missed"]
    assert missed == ["Advanced SQL"]


def test_boilerplate_read_as_a_requirement_fails_precision() -> None:
    score = _score(
        "boilerplate-notices",
        [
            ("Registered Nurse license in Colorado", MUST),
            ("2+ years of experience configuring electronic health record order sets", MUST),
            ("401(k) with employer match", NICE),
        ],
    )

    assert score["value"] is False
    assert score["metadata"]["boilerplate"] == 1
    assert score["metadata"]["precision"] == pytest.approx(2 / 3)


def test_a_quote_the_posting_lacks_fails_the_row() -> None:
    score = _score("duty-list", [*DUTY_CASE_ITEMS, ("Five years of Rust", MUST)])

    assert score["value"] is False
    assert score["metadata"]["not_in_posting"] == 1
    assert score["metadata"]["recall"] == 1.0


def test_a_disagreeing_importance_fails_on_a_matched_requirement() -> None:
    score = _score(
        "duty-list",
        [DUTY_CASE_ITEMS[1], ("Advanced SQL", NICE)],
    )

    assert score["value"] is False
    assert score["metadata"]["importance_agreement"] == 0.5
    assert score["requirements"] == {"total": 2, "passed": 1}


def test_a_split_degree_ladder_misses_the_whole_span() -> None:
    score = _score(
        "degree-ladder",
        [
            ("Bachelor's degree in statistics with 8 years of experience", MUST),
            ("a PhD with 4 years of experience", MUST),
            ("Experience designing clinical trial analysis plans", MUST),
            ("Proficiency in SAS or R", MUST),
        ],
    )

    assert score["value"] is False
    assert score["metadata"]["expected"][0]["outcome"] == "missed"


SQL_QUOTE = "comfortable writing and tuning SQL against large Postgres databases"
PROSE_REST: list[tuple[str, Importance] | tuple[str, Importance, str]] = [
    (
        "at least five years of experience building batch and streaming data pipelines in Python",
        MUST,
    ),
    ("Familiarity with Apache Airflow is a plus", NICE, "Familiarity with Apache Airflow."),
    ("worked with healthcare claims data", NICE, "Experience working with healthcare claims data."),
]


def test_a_requirement_text_dropping_a_qualifier_its_quote_carries_fails() -> None:
    score = _score(
        "prose-requirements",
        [
            *PROSE_REST,
            (SQL_QUOTE, MUST, "Comfortable writing SQL against large Postgres databases."),
        ],
    )

    assert score["value"] is False
    assert score["metadata"]["text_fidelity"] == [{"quote": SQL_QUOTE, "missing": ["tuning"]}]


def test_a_split_whose_sibling_texts_cover_the_quote_keeps_its_fidelity() -> None:
    score = _score(
        "prose-requirements",
        [
            *PROSE_REST,
            (SQL_QUOTE, MUST, "Comfortable writing SQL against large Postgres databases."),
            (SQL_QUOTE, MUST, "Comfortable tuning SQL against large Postgres databases."),
        ],
    )

    assert score["value"] is True
    assert score["metadata"]["text_fidelity"] == []


NICE_SECTION = [
    ("4+ years of experience training and deploying machine learning models", MUST),
    ("Strong Python skills", MUST),
    ("Experience with medical imaging data", NICE),
    ("Publications at a peer-reviewed machine learning venue", NICE),
    ("Familiarity with FDA software as a medical device guidance", NICE),
]


def test_a_one_word_quote_does_not_recall_the_span_around_it() -> None:
    score = _score(
        "nice-to-have-section", [*NICE_SECTION[:2], ("imaging", NICE), *NICE_SECTION[3:]]
    )

    assert score["value"] is False
    missed = [row["span"] for row in score["metadata"]["expected"] if row["outcome"] == "missed"]
    assert missed == ["Experience with medical imaging data"]


def test_a_quote_bundling_two_bullets_recalls_only_one() -> None:
    bundled = (
        "Experience with medical imaging data - "
        "Publications at a peer-reviewed machine learning venue"
    )
    score = _score("nice-to-have-section", [*NICE_SECTION[:2], (bundled, NICE), NICE_SECTION[4]])

    assert score["value"] is False
    assert score["metadata"]["recall"] == pytest.approx(4 / 5)


def test_a_posting_with_no_requirements_passes_on_an_empty_extraction() -> None:
    score = requirement_extraction()(_case("no-stated-requirements"), extraction_result(None))

    assert score["value"] is True


def _labelled_reading(case_id: str) -> tuple[list[tuple[str, Importance]], list[RawHardGate]]:
    """Return the requirements and gates an extraction agreeing with every label reads."""
    sample = _case(case_id)
    items = [(item["span"], Importance(item["importance"])) for item in sample["expected"]]
    gates = [
        RawHardGate(
            kind=gate["kind"],
            text=gate["span"],
            quote_or_span=gate["span"],
            blocking=gate["blocking"],
        )
        for gate in sample["gates"]
    ]
    return items, gates


@pytest.mark.parametrize("case_id", ["conditional-gates", "long-posting-conditional-gates"])
def test_an_extraction_agreeing_with_every_gate_label_passes(case_id: str) -> None:
    items, gates = _labelled_reading(case_id)
    score = _score(case_id, items, gates)

    assert score["value"] is True, score["explanation"]
    assert score["metadata"]["gates"]["agreed"] == len(gates)


def test_a_conditional_gate_read_as_blocking_fails_by_outcome() -> None:
    items, gates = _labelled_reading("conditional-gates")
    remote = next(index for index, gate in enumerate(gates) if gate.text.startswith("If this role"))
    gates[remote] = gates[remote].model_copy(update={"blocking": True})
    score = _score("conditional-gates", items, gates)

    assert score["value"] is False
    outcomes = {row["span"]: row["outcome"] for row in score["metadata"]["gates"]["expected"]}
    assert outcomes[gates[remote].text] == "blocking_mismatch"


def test_a_gate_read_with_another_kind_fails_by_outcome() -> None:
    items, gates = _labelled_reading("conditional-gates")
    gates[3] = gates[3].model_copy(update={"kind": "work_authorization"})
    score = _score("conditional-gates", items, gates)

    assert score["value"] is False
    assert score["metadata"]["gates"]["expected"][3]["outcome"] == "kind_mismatch"


def test_a_missed_gate_fails_the_row() -> None:
    items, gates = _labelled_reading("conditional-gates")
    score = _score("conditional-gates", items, gates[:-1])

    assert score["value"] is False
    assert score["metadata"]["gates"]["expected"][-1]["outcome"] == "missed"


@pytest.mark.parametrize(("blocking", "passes"), [(True, False), (False, True)])
def test_an_unlabelled_gate_fails_only_when_it_blocks(blocking: bool, passes: bool) -> None:
    items, gates = _labelled_reading("conditional-gates")
    extra = RawHardGate(
        kind="location",
        text="This position is based in the United States.",
        quote_or_span="This position is based in the United States.",
        blocking=blocking,
    )
    score = _score("conditional-gates", items, [*gates, extra])

    assert score["value"] is passes
    assert score["metadata"]["gates"]["unlabelled_blocking"] == int(blocking)


def test_a_case_without_gate_labels_does_not_score_its_gates() -> None:
    extra = RawHardGate(
        kind="other", text="Advanced SQL", quote_or_span="Advanced SQL", blocking=True
    )
    score = _score("duty-list", DUTY_CASE_ITEMS, [extra])

    assert score["value"] is True
    assert "gates" not in score["metadata"]


@pytest.mark.parametrize(
    "gate",
    [
        {"span": "Advanced SQL", "kind": "skill", "blocking": True},
        {"span": "Advanced SQL", "kind": "other"},
        {"span": "Fluent Klingon", "kind": "other", "blocking": True},
    ],
)
def test_a_gate_label_with_an_unknown_kind_or_a_missing_span_is_refused(
    gate: dict[str, Any],
) -> None:
    sample = {**_case("duty-list"), "gates": [gate]}

    with pytest.raises(ValueError, match="gate"):
        requirement_extraction().validate_sample(sample)


def test_the_long_posting_is_production_sized_with_two_conditional_gates() -> None:
    sample = _case("long-posting-conditional-gates")
    conditional = [
        gate for gate in sample["gates"] if gate["span"].startswith("If ") and not gate["blocking"]
    ]

    assert 8000 <= len(sample["input"]) <= 10000
    assert len(sample["expected"]) >= 30
    assert len(conditional) == 2


def test_the_result_carries_gates_dimension_and_core_occupation() -> None:
    sample = _case("conditional-gates")
    raw = JobSpecExtraction(
        title="Senior Data Engineer",
        requirements=[
            RawRequirement(
                text="5+ years of experience building data pipelines in Python",
                importance=MUST,
                dimension=Dimension.HARD,
            )
        ],
        hard_gates=[
            RawHardGate(
                kind="minimum_years",
                text="5+ years of experience building data pipelines in Python",
                blocking=True,
            )
        ],
        core_occupation=RawCoreOccupation(
            text="Data engineer", quote_or_span="Senior Data Engineer, Contoso Freight"
        ),
    )
    result = extraction_result(spec_from_extraction(raw, posting(sample)))

    assert result["requirements"][0]["dimension"] == "hard"
    assert result["hard_gates"] == [
        {
            "kind": "minimum_years",
            "text": "5+ years of experience building data pipelines in Python",
            "quote": "5+ years of experience building data pipelines in Python",
            "blocking": True,
            "verified": True,
        }
    ]
    assert result["core_occupation"] == {
        "text": "Data engineer",
        "quote": "Senior Data Engineer, Contoso Freight",
        "verified": True,
    }
    assert extraction_result(None)["hard_gates"] == []
    assert extraction_result(None)["core_occupation"] is None
