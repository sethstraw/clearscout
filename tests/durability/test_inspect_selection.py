"""Selection and projection preserve the frozen development-set contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from evals.inspect.labeling import (
    PROTOCOL_VERSION,
    CandidatePool,
    ExtractionQaSet,
    bytes_sha256,
    domain_sha256,
)
from evals.inspect.review_resolution import (
    AcceptedPair,
    CaseRuling,
    ChallengeClass,
    ResolutionSet,
    ResolvedLabel,
    match_material,
)
from evals.inspect.selection import (
    SELECTION_CONSTRAINTS,
    SELECTION_MANIFEST_DOMAIN,
    AdjudicationReference,
    SelectedCase,
    SelectionConstraints,
    SelectionManifest,
    project_match_dataset,
    select_cases,
    select_held_out,
    selection_key_sha256,
    serialize_match_dataset,
    split_resolution_sha256,
)
from evals.inspect.solvers import clearscout_live
from pydantic import ValidationError

from clearscout.application.tailoring.stages.jobspec import Dimension
from clearscout.application.tailoring.stages.match import Classification, Strength
from tests.support.match_selection import (
    CASES,
    HELD_OUT,
    INVENTORY_HASH,
    MATRIX,
    PARTIAL,
    SET_SIZE,
    STRATUM_LABELS,
    candidate_content,
    candidate_pool,
    case_uuid,
    manifest_body,
    pool_from_candidates,
    qa_set,
    resolution_from_labels,
    resolution_set,
    resolved_label,
    selected_cases,
    selection_manifest,
)

NO_ASSIGNMENT = "admit no assignment of one case to each frozen slot"


def _write_live_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: SelectionManifest,
) -> Path:
    path = tmp_path / "selection-manifest.json"
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST", str(path))
    monkeypatch.setenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256", manifest.manifest_sha256)
    monkeypatch.setenv(
        "CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256", bytes_sha256(path.read_bytes())
    )
    return path


def _live_manifest_identity(manifest: SelectionManifest) -> dict[str, Any]:
    return clearscout_live._selection_manifest_identity(
        split="development", limit=len(manifest.body.ordered_cases)
    )


def _replace_selected(
    selected: tuple[SelectedCase, ...], index: int, **changes: object
) -> tuple[SelectedCase, ...]:
    replacement = SelectedCase.model_validate(
        {**selected[index].model_dump(mode="python"), **changes}
    )
    return (*selected[:index], replacement, *selected[index + 1 :])


def _pool_and_resolution(
    labels: dict[int, ResolvedLabel],
) -> tuple[CandidatePool, ExtractionQaSet, ResolutionSet]:
    pool = pool_from_candidates(tuple(candidate_content(index) for index in labels))
    return pool, qa_set(pool), resolution_from_labels(tuple(labels.values()))


def test_selection_manifest_accepts_the_exact_frozen_matrix_and_stratum() -> None:
    pool = candidate_pool()
    qa = qa_set(pool)
    resolution = resolution_set()
    manifest = selection_manifest(pool, resolution)
    manifest.validate_inputs(pool, qa, resolution)
    assert [item.position for item in manifest.body.ordered_cases] == list(range(1, 20))
    assert [item.selection_key for item in manifest.body.ordered_cases] == sorted(
        item.selection_key for item in manifest.body.ordered_cases
    )


@pytest.mark.parametrize("label", [Classification.GAP, Classification.TRANSFERABLE])
def test_a_partial_contribution_case_fills_the_stratum_whatever_its_label(
    label: Classification,
) -> None:
    labels = {
        index: resolved_label(index, classification=label if index > len(MATRIX) else None)
        for index in range(1, SET_SIZE + 1)
    }
    pool, qa, resolution = _pool_and_resolution(labels)

    selected = select_cases(pool, qa, resolution, previous_pool=None)

    stratum = [item for item in selected if item.challenge_class is PARTIAL]
    assert len(selected) == SET_SIZE
    assert [item.classification for item in stratum] == [label] * 3


def test_a_partial_contribution_case_may_fill_a_matrix_cell() -> None:
    direct_behavioral = MATRIX.index((Classification.DIRECT, Dimension.BEHAVIORAL)) + 1
    spare = SET_SIZE + 1
    labels = {
        index: resolved_label(index)
        for index in range(1, SET_SIZE + 1)
        if index != direct_behavioral
    }
    labels[spare] = resolved_label(
        spare,
        classification=Classification.DIRECT,
        dimension=Dimension.BEHAVIORAL,
        challenge=PARTIAL,
    )
    pool, qa, resolution = _pool_and_resolution(labels)

    selected = select_cases(pool, qa, resolution, previous_pool=None)

    assert len(selected) == SET_SIZE
    assert case_uuid(spare) in {item.case_id for item in selected}


@pytest.mark.parametrize(
    "stratum_change",
    [
        {"challenge": ChallengeClass.CLEAR_SUPPORTED},
        {"dimension": Dimension.BEHAVIORAL},
    ],
    ids=["not-partial", "behavioral"],
)
def test_selector_refuses_a_pool_whose_participation_cases_cannot_fill_the_stratum(
    stratum_change: dict[str, Any],
) -> None:
    labels = {index: resolved_label(index) for index in range(1, SET_SIZE)}
    labels[SET_SIZE] = resolved_label(SET_SIZE, **stratum_change)
    # A spare for every matrix cell shows capacity elsewhere cannot stand in.
    for offset, (classification, dimension) in enumerate(MATRIX, SET_SIZE + 1):
        labels[offset] = resolved_label(
            offset,
            classification=classification,
            dimension=dimension,
            challenge=ChallengeClass.CLEAR_SUPPORTED,
        )
    pool, qa, resolution = _pool_and_resolution(labels)

    with pytest.raises(ValueError, match="no feasible selection fills the frozen slots"):
        select_cases(pool, qa, resolution, previous_pool=None)


def test_selector_returns_the_lexicographically_first_feasible_tuple() -> None:
    spare = SET_SIZE + 1
    pool = pool_from_candidates((*candidate_pool().candidates, candidate_content(spare)))
    resolution = resolution_from_labels(
        (
            *resolution_set().labels,
            resolved_label(spare, challenge=ChallengeClass.CLEAR_SUPPORTED),
        )
    )
    qa = qa_set(pool)
    selected = select_cases(pool, qa, resolution, previous_pool=None)
    # Three DIRECT hard cases compete for that cell's two slots.
    direct_hard = {
        case_uuid(index)
        for index, (classification, dimension, _) in enumerate(CASES, 1)
        if (classification, dimension) == (Classification.DIRECT, Dimension.HARD)
    } | {case_uuid(spare)}
    alternatives = sorted(
        (
            selection_key_sha256(
                protocol_version=PROTOCOL_VERSION,
                candidate_pool_sha256=pool.candidate_pool_sha256,
                resolution_sha256=split_resolution_sha256(pool, resolution, "development"),
                case_id=item.case_id,
                resolved_label_sha256=item.resolved_label_sha256,
            ),
            item.case_id,
        )
        for item in resolution.labels
        if item.case_id in direct_hard
    )
    selected_ids = {item.case_id for item in selected}
    assert len(alternatives) == 3
    assert {case_id for _, case_id in alternatives[:2]} <= selected_ids
    assert alternatives[2][1] not in selected_ids
    assert tuple(item.selection_key for item in selected) == tuple(
        sorted(item.selection_key for item in selected)
    )


def test_manifest_rejects_a_later_feasible_selection() -> None:
    spare = SET_SIZE + 1
    pool = pool_from_candidates((*candidate_pool().candidates, candidate_content(spare)))
    resolution = resolution_from_labels(
        (
            *resolution_set().labels,
            resolved_label(spare, challenge=ChallengeClass.CLEAR_SUPPORTED),
        )
    )
    qa = qa_set(pool)
    expected = select_cases(pool, qa, resolution, previous_pool=None)
    expected_ids = {item.case_id for item in expected}
    omitted_label = next(
        item
        for item in resolution.labels
        if item.case_id not in expected_ids and item.case_id != case_uuid(HELD_OUT)
    )
    omitted_candidate = next(
        item for item in pool.candidates if item.case_id == omitted_label.case_id
    )
    omitted = SelectedCase(
        position=1,
        case_id=omitted_label.case_id,
        source_id=omitted_candidate.source_id,
        normalized_requirement_sha256=omitted_candidate.normalized_requirement_sha256,
        resolved_label_sha256=omitted_label.resolved_label_sha256,
        selection_key=selection_key_sha256(
            protocol_version=PROTOCOL_VERSION,
            candidate_pool_sha256=pool.candidate_pool_sha256,
            resolution_sha256=split_resolution_sha256(pool, resolution, "development"),
            case_id=omitted_label.case_id,
            resolved_label_sha256=omitted_label.resolved_label_sha256,
        ),
        classification=match_material(omitted_label).classification,
        strength=match_material(omitted_label).strength,
        dimension=match_material(omitted_label).dimension,
        challenge_class=match_material(omitted_label).challenge_class,
    )
    replaceable = next(
        item
        for item in expected
        if item.classification is omitted.classification and item.dimension is omitted.dimension
    )
    later_cases = tuple(
        sorted(
            (*tuple(item for item in expected if item.case_id != replaceable.case_id), omitted),
            key=lambda item: item.selection_key,
        )
    )
    later_cases = tuple(
        SelectedCase.model_validate({**item.model_dump(mode="python"), "position": position})
        for position, item in enumerate(later_cases, 1)
    )
    body = manifest_body(pool, resolution, qa=qa, selected=later_cases)

    with pytest.raises(ValueError, match="not the lexicographically first feasible selection"):
        body.validate_inputs(pool, qa, resolution)


def test_manifest_requires_the_exact_qa_set_and_approved_selected_cases() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    original_qa = qa_set(pool)
    body = manifest_body(pool, resolution, qa=original_qa)
    stale_qa = qa_set(pool, approved_rationale="A different frozen QA receipt.")
    with pytest.raises(ValueError, match="does not reference the extraction QA set"):
        body.validate_inputs(pool, stale_qa, resolution)

    rejected_case = body.ordered_cases[0].case_id
    rejected_qa = qa_set(pool, rejected=frozenset({rejected_case}))
    rejected_body = manifest_body(pool, resolution, qa=rejected_qa)
    with pytest.raises(ValueError, match="must be approved by extraction QA"):
        rejected_body.validate_inputs(pool, rejected_qa, resolution)


def test_selection_key_binds_pool_resolution_case_and_resolved_label() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    selected = selected_cases(pool, resolution)[0]
    assert selected.selection_key != selection_key_sha256(
        protocol_version=PROTOCOL_VERSION,
        candidate_pool_sha256="f" * 64,
        resolution_sha256=split_resolution_sha256(pool, resolution, "development"),
        case_id=selected.case_id,
        resolved_label_sha256=selected.resolved_label_sha256,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("position", "positions"),
        ("classification", NO_ASSIGNMENT),
        ("dimension", NO_ASSIGNMENT),
        ("stratum", NO_ASSIGNMENT),
        ("challenge", "every challenge class"),
        ("case", "case IDs must be unique"),
        ("source", "source IDs must be unique"),
        ("normalized", "normalized requirement digests must be unique"),
        ("resolution", "resolution digests must be unique"),
    ],
)
def test_manifest_rejects_each_structural_matrix_violation(mutation: str, message: str) -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    selected = selected_cases(pool, resolution)
    second = selected[1]
    matrix_index = next(
        index for index, item in enumerate(selected) if item.challenge_class is not PARTIAL
    )
    stratum_index = next(
        index for index, item in enumerate(selected) if item.challenge_class is PARTIAL
    )
    changes: dict[str, object]
    if mutation == "position":
        changes = {"position": 2}
    elif mutation == "classification":
        matrix_case = selected[matrix_index]
        other = next(
            item for item in selected if item.classification is not matrix_case.classification
        )
        selected = _replace_selected(selected, matrix_index, classification=other.classification)
        changes = {}
    elif mutation == "dimension":
        hard_index = next(
            index
            for index, item in enumerate(selected)
            if item.dimension is Dimension.HARD and item.challenge_class is not PARTIAL
        )
        selected = _replace_selected(selected, hard_index, dimension=Dimension.BEHAVIORAL)
        changes = {}
    elif mutation == "stratum":
        selected = _replace_selected(
            selected, stratum_index, challenge_class=ChallengeClass.CLEAR_SUPPORTED
        )
        changes = {}
    elif mutation == "challenge":
        missing = ChallengeClass.ABSENT_EVIDENCE
        selected = tuple(
            item.model_copy(
                update={
                    "challenge_class": (
                        ChallengeClass.CLEAR_SUPPORTED
                        if item.challenge_class is missing
                        else item.challenge_class
                    )
                }
            )
            for item in selected
        )
        changes = {}
    elif mutation == "case":
        changes = {"case_id": second.case_id}
    elif mutation == "source":
        changes = {"source_id": second.source_id}
    elif mutation == "normalized":
        changes = {"normalized_requirement_sha256": second.normalized_requirement_sha256}
    else:
        changes = {"resolved_label_sha256": second.resolved_label_sha256}
    if changes:
        selected = _replace_selected(selected, 0, **changes)
    with pytest.raises(ValidationError, match=message):
        manifest_body(pool, resolution, selected=selected)


def test_a_manifest_key_must_match_its_frozen_inputs() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    forged = _replace_selected(selected_cases(pool, resolution), 0, selection_key="0" * 64)
    body = manifest_body(pool, resolution, selected=forged)

    with pytest.raises(ValueError, match="selection keys do not match"):
        body.validate_inputs(pool, qa_set(pool), resolution)


def test_relabelling_a_held_out_case_leaves_the_development_keys_alone() -> None:
    pool = candidate_pool()
    qa = qa_set(pool)
    resolution = resolution_set()
    relabelled_held_out = resolution_from_labels(
        (
            *(item for item in resolution.labels if item.case_id != case_uuid(HELD_OUT)),
            resolved_label(HELD_OUT, classification=Classification.GAP),
        )
    )
    relabelled_development = resolution_from_labels(
        (
            *(item for item in resolution.labels if item.case_id != case_uuid(1)),
            resolved_label(1, challenge=ChallengeClass.ABSENT_EVIDENCE),
        )
    )

    def keys(labels: ResolutionSet) -> list[str]:
        return [item.selection_key for item in select_cases(pool, qa, labels, previous_pool=None)]

    assert keys(relabelled_held_out) == keys(resolution)
    assert set(keys(relabelled_development)).isdisjoint(keys(resolution))


def test_selection_refuses_a_pool_that_moves_a_case_the_previous_pool_froze() -> None:
    pool = candidate_pool()
    qa = qa_set(pool)
    resolution = resolution_set()
    previous = pool_from_candidates(
        tuple(candidate_content(index) for index in (*range(1, SET_SIZE + 1), HELD_OUT))
    )

    with pytest.raises(ValueError, match="between splits"):
        select_cases(pool, qa, resolution, previous_pool=previous)
    with pytest.raises(ValueError, match="between splits"):
        select_held_out(pool, qa, resolution, previous_pool=previous)


def test_each_supported_class_requires_one_hard_and_one_behavioral_case() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    selected = selected_cases(pool, resolution)
    direct_hard = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.DIRECT and item.dimension is Dimension.HARD
    )
    tailoring = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.TAILORING_ONLY
        and item.dimension is Dimension.BEHAVIORAL
    )
    changed = _replace_selected(selected, direct_hard, dimension=Dimension.BEHAVIORAL)
    changed = _replace_selected(changed, tailoring, dimension=Dimension.HARD)
    with pytest.raises(ValidationError, match=NO_ASSIGNMENT):
        manifest_body(pool, resolution, selected=changed)


def test_asserted_cases_cannot_be_behavioral_even_when_dimension_counts_balance() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    selected = selected_cases(pool, resolution)
    asserted = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.ASSERTED
    )
    behavioral_gap = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.GAP and item.dimension is Dimension.BEHAVIORAL
    )
    changed = _replace_selected(selected, asserted, dimension=Dimension.BEHAVIORAL)
    changed = _replace_selected(changed, behavioral_gap, dimension=Dimension.HARD)
    with pytest.raises(ValidationError, match=NO_ASSIGNMENT):
        manifest_body(pool, resolution, selected=changed)


def test_valid_keys_must_still_be_lexicographically_ordered() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    selected = selected_cases(pool, resolution)
    swapped = (selected[1], selected[0], *selected[2:])
    repositioned = tuple(
        item.model_copy(update={"position": position}) for position, item in enumerate(swapped, 1)
    )
    with pytest.raises(ValidationError, match="ordered by selection key"):
        manifest_body(pool, resolution, selected=repositioned)


def test_review_references_require_exact_selected_coverage() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    body = manifest_body(pool, resolution)
    duplicate = (body.review_references[0], *body.review_references[:-1])
    with pytest.raises(ValidationError, match="cover every selected case"):
        manifest_body(pool, resolution, review_references=duplicate)


def test_constraints_state_every_slot_of_the_nineteen_case_set() -> None:
    constraints = SelectionConstraints.model_validate_json(SELECTION_CONSTRAINTS.model_dump_json())

    cells = [
        (slot.classification, slot.dimension, slot.challenge_class, slot.cases)
        for slot in constraints.slots
    ]
    assert constraints.total_cases == SET_SIZE == 19
    assert sum(cases for _, _, challenge, cases in cells if challenge is None) == len(MATRIX)
    assert [
        (classification, dimension)
        for classification, dimension, challenge, cases in cells
        if challenge is None
        for _ in range(cases)
    ] == list(MATRIX)
    assert cells[-1] == (None, Dimension.HARD, PARTIAL, len(STRATUM_LABELS))


@pytest.mark.parametrize(
    "change",
    [{"total_cases": 13}, {"slots": SELECTION_CONSTRAINTS.slots[:-1]}],
    ids=["total", "slots"],
)
def test_constraints_refuse_any_other_slot_matrix(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="differ from the frozen slots"):
        SelectionConstraints.model_validate({**dict(SELECTION_CONSTRAINTS), **change})


def test_manifest_hash_is_fail_closed() -> None:
    body = manifest_body(candidate_pool(), resolution_set())
    with pytest.raises(ValidationError, match="manifest_sha256"):
        SelectionManifest(body=body, manifest_sha256="f" * 64)


def test_manifest_cross_validation_rejects_candidate_and_resolution_drift() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    body = manifest_body(pool, resolution)
    selected = body.ordered_cases
    unused_source = UUID("30000000-0000-4000-8000-000000000001")
    source_drift = _replace_selected(selected, 0, source_id=unused_source)
    drift_body = manifest_body(pool, resolution, selected=source_drift)
    drift_manifest = SelectionManifest(
        body=drift_body,
        manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, drift_body),
    )
    with pytest.raises(ValueError, match="differs from its candidate"):
        drift_manifest.validate_inputs(pool, qa_set(pool), resolution)

    first_direct = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.DIRECT and item.dimension is Dimension.HARD
    )
    first_tailoring = next(
        index
        for index, item in enumerate(selected)
        if item.classification is Classification.TAILORING_ONLY
        and item.dimension is Dimension.HARD
        and item.challenge_class is not PARTIAL
    )
    changed = _replace_selected(
        selected, first_direct, classification=Classification.TAILORING_ONLY
    )
    changed = _replace_selected(changed, first_tailoring, classification=Classification.DIRECT)
    label_drift = manifest_body(pool, resolution, selected=changed)
    label_manifest = SelectionManifest(
        body=label_drift,
        manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, label_drift),
    )
    with pytest.raises(ValueError, match="differs from its resolved label"):
        label_manifest.validate_inputs(pool, qa_set(pool), resolution)


def test_manifest_cross_validation_rejects_strength_drift() -> None:
    """A selected case cannot state a strength its resolved label does not carry."""
    pool = candidate_pool()
    resolution = resolution_set()
    selected = manifest_body(pool, resolution).ordered_cases
    cited = next(index for index, item in enumerate(selected) if item.strength is Strength.MODERATE)
    drifted = manifest_body(
        pool, resolution, selected=_replace_selected(selected, cited, strength=Strength.WEAK)
    )

    with pytest.raises(ValueError, match="differs from its resolved label"):
        drifted.validate_inputs(pool, qa_set(pool), resolution)


def test_manifest_cross_validation_rejects_review_adjudication_and_byte_drift() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    body = manifest_body(pool, resolution)
    wrong_ref = body.review_references[0].model_copy(update={"review_a_sha256": "f" * 64})
    refs = (wrong_ref, *body.review_references[1:])
    review_drift = manifest_body(pool, resolution, review_references=refs)
    with pytest.raises(ValueError, match="review references differ"):
        review_drift.validate_inputs(pool, qa_set(pool), resolution)

    extraneous = AdjudicationReference(
        case_id=body.ordered_cases[0].case_id,
        adjudication_sha256="e" * 64,
    )
    adjudication_drift = manifest_body(pool, resolution, adjudication_references=(extraneous,))
    with pytest.raises(ValueError, match="agreed selected label"):
        adjudication_drift.validate_inputs(pool, qa_set(pool), resolution)

    byte_drift = manifest_body(pool, resolution, dataset_sha256="f" * 64)
    with pytest.raises(ValueError, match="exact projected JSONL bytes"):
        byte_drift.validate_inputs(pool, qa_set(pool), resolution)


def test_dataset_projection_is_minimal_canonical_and_newline_terminated() -> None:
    pool = candidate_pool()
    qa = qa_set(pool)
    resolution = resolution_set()
    manifest = selection_manifest(pool, resolution)
    content = project_match_dataset(manifest, pool, qa, resolution)
    assert content.endswith(b"\n") and not content.endswith(b"\n\n")
    rows = [json.loads(line) for line in content.decode("utf-8").splitlines()]
    assert len(rows) == SET_SIZE + 1
    expected_fields = {
        "id",
        "input",
        "requirement_text",
        "requirement_key",
        "dimension",
        "cv_hash",
        "split",
        "expected_classification",
        "expected_strength",
    }
    assert all(set(row) == expected_fields for row in rows)
    labels = {str(item.case_id): match_material(item) for item in resolution.labels}
    assert {row["id"]: row["expected_strength"] for row in rows} == {
        case_id: material.strength.value for case_id, material in labels.items()
    }
    assert all(row["id"] == row["requirement_key"] for row in rows)
    assert all(row["input"] == row["requirement_text"] for row in rows)
    assert all(row["cv_hash"] == INVENTORY_HASH for row in rows)
    assert {row["id"]: row["split"] for row in rows if row["split"] != "development"} == {
        str(case_uuid(HELD_OUT)): "holdout"
    }
    assert b'"rationale"' not in content
    assert b'"decisive_entry_ids"' not in content
    assert b'"source_id"' not in content
    assert b'"review_a_sha256"' not in content
    assert bytes_sha256(content) == manifest.body.dataset_sha256


def _ruling(index: int, *extra: tuple[Classification, Strength]) -> CaseRuling:
    resolved = match_material(resolved_label(index))
    pairs = ((resolved.classification, resolved.strength), *extra)
    return CaseRuling(
        case_id=case_uuid(index),
        accepted=tuple(
            AcceptedPair(classification=label, strength=grade) for label, grade in pairs
        ),
        basis="The rubric leaves this case open between the listed labels.",
    )


def test_a_ruling_writes_its_accepted_pairs_into_the_manifest_bound_dataset() -> None:
    pool = candidate_pool()
    qa = qa_set(pool)
    resolution = resolution_set()
    ruling = _ruling(1, (Classification.TRANSFERABLE, Strength.WEAK))
    manifest = selection_manifest(pool, resolution, (ruling,))

    content = project_match_dataset(manifest, pool, qa, resolution)

    rows = {row["id"]: row for row in map(json.loads, content.decode("utf-8").splitlines())}
    ruled = rows[str(case_uuid(1))]
    assert ruled["expected_classification"] == ["DIRECT", "TRANSFERABLE"]
    assert ruled["expected_strength"] == ["moderate", "weak"]
    assert rows[str(case_uuid(2))]["expected_classification"] == "DIRECT"
    assert bytes_sha256(content) == manifest.body.dataset_sha256
    assert manifest.body.rulings == (ruling,)


def test_a_manifest_whose_dataset_omits_its_rulings_is_refused() -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    unruled = manifest_body(pool, resolution)
    body = unruled.model_copy(
        update={"rulings": (_ruling(1, (Classification.TRANSFERABLE, Strength.WEAK)),)}
    )

    with pytest.raises(ValueError, match="exact projected JSONL bytes"):
        body.validate_inputs(pool, qa_set(pool), resolution)


@pytest.mark.parametrize(
    ("ruling", "message"),
    [
        (
            CaseRuling(
                case_id=case_uuid(1),
                accepted=(
                    AcceptedPair(classification=Classification.GAP, strength=Strength.NONE),
                    AcceptedPair(
                        classification=Classification.TRANSFERABLE, strength=Strength.WEAK
                    ),
                ),
                basis="Accepts two labels, neither of them the resolved one.",
            ),
            "resolved label",
        ),
        (_ruling(SET_SIZE + 7, (Classification.GAP, Strength.NONE)), "does not hold"),
    ],
    ids=["resolved-label-missing", "unselected-case"],
)
def test_serialization_refuses_a_ruling_that_does_not_widen_a_selected_label(
    ruling: CaseRuling, message: str
) -> None:
    pool = candidate_pool()
    resolution = resolution_set()
    body = manifest_body(pool, resolution)

    with pytest.raises(ValueError, match=message):
        serialize_match_dataset(body.all_cases, pool, INVENTORY_HASH, (ruling,))


@pytest.mark.parametrize(
    ("accepted", "message"),
    [
        ((("DIRECT", "moderate"),), "at least 2 items"),
        ((("DIRECT", "moderate"), ("GAP", "strong")), "pairs GAP with strength strong"),
        ((("DIRECT", "moderate"), ("DIRECT", "moderate")), "each classification and strength"),
    ],
    ids=["one-label", "gap-with-strength", "repeated-pair"],
)
def test_a_ruling_accepts_at_least_two_distinct_rubric_labels(
    accepted: tuple[tuple[str, str], ...], message: str
) -> None:
    ruling = {
        "case_id": str(case_uuid(1)),
        "accepted": [{"classification": label, "strength": grade} for label, grade in accepted],
        "basis": "A ruling needs a basis.",
    }

    with pytest.raises(ValidationError, match=message):
        CaseRuling.model_validate_json(json.dumps(ruling))


def test_a_held_out_case_never_fills_a_development_slot() -> None:
    """The held-out case is the only one left for its cell, and it still stays out."""
    direct_behavioral = MATRIX.index((Classification.DIRECT, Dimension.BEHAVIORAL)) + 1
    labels = {index: resolved_label(index) for index in range(1, SET_SIZE + 1)}
    pool = pool_from_candidates(
        tuple(
            candidate_content(index, "holdout" if index == direct_behavioral else "development")
            for index in labels
        )
    )
    resolution = resolution_from_labels(tuple(labels.values()))

    with pytest.raises(ValueError, match="no feasible selection fills the frozen slots"):
        select_cases(pool, qa_set(pool), resolution, previous_pool=None)
    held_out = select_held_out(pool, qa_set(pool), resolution, previous_pool=None)
    assert [item.case_id for item in held_out] == [case_uuid(direct_behavioral)]


def test_a_manifest_must_hold_a_case_out() -> None:
    with pytest.raises(ValidationError, match="held_out_cases"):
        manifest_body(candidate_pool(), resolution_set(), held_out=())


def test_a_manifest_cannot_list_a_development_case_as_held_out() -> None:
    spare = SET_SIZE + 1
    pool = pool_from_candidates((*candidate_pool().candidates, candidate_content(spare)))
    resolution = resolution_from_labels(
        (
            *resolution_set().labels,
            resolved_label(spare, challenge=ChallengeClass.CLEAR_SUPPORTED),
        )
    )
    qa = qa_set(pool)
    selected = select_cases(pool, qa, resolution, previous_pool=None)
    development = next(
        item
        for item in selected_cases(pool, resolution)
        if item.case_id not in {chosen.case_id for chosen in selected}
    )
    # Keyed as a held-out case, so only its split is wrong.
    unselected = development.model_copy(
        update={
            "selection_key": selection_key_sha256(
                protocol_version=PROTOCOL_VERSION,
                candidate_pool_sha256=pool.candidate_pool_sha256,
                resolution_sha256=split_resolution_sha256(pool, resolution, "holdout"),
                case_id=development.case_id,
                resolved_label_sha256=development.resolved_label_sha256,
            )
        }
    )
    relabelled = tuple(
        item.model_copy(update={"position": position})
        for position, item in enumerate(
            sorted(
                (*selected_cases(pool, resolution, "holdout"), unselected),
                key=lambda item: item.selection_key,
            ),
            1,
        )
    )
    body = manifest_body(pool, resolution, qa=qa, selected=selected, held_out=relabelled)

    with pytest.raises(ValueError, match="not the held-out cases"):
        body.validate_inputs(pool, qa, resolution)


def test_the_held_out_split_survives_a_reselection() -> None:
    prior_pool = candidate_pool()
    prior = selection_manifest(prior_pool, resolution_set())
    added_dev, added_held = SET_SIZE + 1, HELD_OUT + 1
    pool = pool_from_candidates(
        (
            *prior_pool.candidates,
            candidate_content(added_dev),
            candidate_content(added_held, "holdout"),
        )
    )
    resolution = resolution_from_labels(
        (
            *resolution_set().labels,
            resolved_label(added_dev, challenge=ChallengeClass.CLEAR_SUPPORTED),
            resolved_label(added_held),
        )
    )
    qa = qa_set(pool)

    development = {
        item.case_id for item in select_cases(pool, qa, resolution, previous_pool=prior_pool)
    }
    held_out = {
        item.case_id for item in select_held_out(pool, qa, resolution, previous_pool=prior_pool)
    }

    prior_held_out = {item.case_id for item in prior.body.held_out_cases}
    assert prior_held_out <= held_out
    assert not prior_held_out & development
    assert held_out == {case_uuid(HELD_OUT), case_uuid(added_held)}


def test_live_identity_binds_the_exact_private_selection_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = candidate_pool()
    manifest = selection_manifest(pool, resolution_set())
    path = _write_live_manifest(tmp_path, monkeypatch, manifest)

    identity = _live_manifest_identity(manifest)

    assert identity == {
        "selection_manifest_path": str(path.resolve()),
        "selection_manifest_sha256": manifest.manifest_sha256,
        "selection_manifest_bytes_sha256": bytes_sha256(path.read_bytes()),
        "dataset_sha256": manifest.body.dataset_sha256,
        "review_context_sha256": manifest.body.review_context_sha256,
        "synthetic_inventory_hash": manifest.body.synthetic_inventory_hash,
        "candidate_pool_sha256": manifest.body.candidate_pool_sha256,
        "extraction_qa_sha256": manifest.body.extraction_qa_sha256,
        "review_set_sha256": manifest.body.review_set_sha256,
        "resolution_set_sha256": manifest.body.resolution_set_sha256,
    }


def test_live_identity_requires_an_approved_selection_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST", raising=False)
    monkeypatch.delenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256", raising=False)
    monkeypatch.delenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256", raising=False)

    with pytest.raises(RuntimeError, match="private selection manifest"):
        clearscout_live._selection_manifest_identity(split="development", limit=SET_SIZE)


def test_live_identity_refuses_manifest_approval_or_byte_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = candidate_pool()
    manifest = selection_manifest(pool, resolution_set())
    path = _write_live_manifest(tmp_path, monkeypatch, manifest)
    monkeypatch.setenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256", "0" * 64)

    with pytest.raises(RuntimeError, match="does not match the approved SHA-256"):
        _live_manifest_identity(manifest)

    monkeypatch.setenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256", manifest.manifest_sha256)
    monkeypatch.setenv("CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="manifest bytes"):
        _live_manifest_identity(manifest)

    monkeypatch.setenv(
        "CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256", bytes_sha256(path.read_bytes())
    )
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid selection manifest"):
        _live_manifest_identity(manifest)


def test_live_identity_refuses_another_synthetic_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = candidate_pool()
    body = manifest_body(pool, resolution_set()).model_copy(
        update={"synthetic_inventory_hash": "0" * 16}
    )
    manifest = SelectionManifest(
        body=body,
        manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, body),
    )
    _write_live_manifest(tmp_path, monkeypatch, manifest)

    with pytest.raises(RuntimeError, match="another synthetic inventory"):
        _live_manifest_identity(manifest)


def test_live_identity_admits_an_explicit_holdout_run_over_every_held_out_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = selection_manifest(candidate_pool(), resolution_set())
    _write_live_manifest(tmp_path, monkeypatch, manifest)

    identity = clearscout_live._selection_manifest_identity(
        split="holdout", limit=len(manifest.body.held_out_cases)
    )

    assert identity["selection_manifest_sha256"] == manifest.manifest_sha256


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ({"split": "regression"}, "development and holdout"),
        ({"split": "holdout"}, "sample limit"),
        ({"limit": 9}, "sample limit"),
    ],
)
def test_live_identity_refuses_scope_outside_the_selection_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: dict[str, Any],
    message: str,
) -> None:
    pool = candidate_pool()
    manifest = selection_manifest(pool, resolution_set())
    _write_live_manifest(tmp_path, monkeypatch, manifest)
    approved_scope: dict[str, Any] = {
        "split": "development",
        "limit": len(manifest.body.ordered_cases),
    }
    approved_scope.update(scope)

    with pytest.raises(RuntimeError, match=message):
        clearscout_live._selection_manifest_identity(**approved_scope)


def test_the_round_in_force_reproduces_the_tracked_match_dataset() -> None:
    golden = Path(__file__).resolve().parents[2] / "evals/golden"
    receipts = golden / "receipts/match-labeling-2026-09-25"

    def receipt(name: str, field: str | None = None) -> str:
        text = (receipts / name).read_text(encoding="utf-8")
        return text if field is None else json.dumps(json.loads(text)[field])

    dataset = project_match_dataset(
        SelectionManifest.model_validate_json(receipt("selection-manifest.json")),
        CandidatePool.model_validate_json(receipt("candidates.json", "candidate_pool")),
        ExtractionQaSet.model_validate_json(receipt("extraction-qa.json")),
        ResolutionSet.model_validate_json(receipt("resolution-bundle.json", "resolution_set")),
    )

    assert dataset == (golden / "match.jsonl").read_bytes()
