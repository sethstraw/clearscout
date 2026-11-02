"""Synthetic candidate pools, resolved labels, and selection manifests for match labeling."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from evals.inspect.labeling import (
    CANDIDATE_POOL_DOMAIN,
    EXTRACTION_QA_DOMAIN,
    PROTOCOL_VERSION,
    CandidateContent,
    CandidatePool,
    ExtractionQaFinding,
    ExtractionQaSet,
    PublicationGate,
    ReviewFamily,
    Split,
    bytes_sha256,
    domain_sha256,
    normalized_requirement_sha256,
)
from evals.inspect.review_resolution import (
    CaseRuling,
    ChallengeClass,
    MatchDecision,
    ResolutionSet,
    ResolvedLabel,
    ReviewArtifact,
    match_material,
    review_domain,
)
from evals.inspect.selection import (
    SELECTION_ALGORITHM,
    SELECTION_CONSTRAINTS,
    SELECTION_MANIFEST_DOMAIN,
    AdjudicationReference,
    ReviewReference,
    SelectedCase,
    SelectionManifest,
    SelectionManifestBody,
    selection_key_sha256,
    serialize_match_dataset,
    split_resolution_sha256,
)
from evals.support.inventory import synthetic_inventory

from tailorcv.application.tailoring.stages.jobspec import Dimension
from tailorcv.application.tailoring.stages.match import Classification, Strength

MATCH = ReviewFamily.MATCH
CONTEXT_HASH = "c" * 64
REVIEW_SET_HASH = "d" * 64
INVENTORY_HASH = synthetic_inventory().content_hash

MATRIX = (
    (Classification.DIRECT, Dimension.HARD),
    (Classification.DIRECT, Dimension.HARD),
    (Classification.DIRECT, Dimension.BEHAVIORAL),
    (Classification.TAILORING_ONLY, Dimension.HARD),
    (Classification.TAILORING_ONLY, Dimension.BEHAVIORAL),
    (Classification.TAILORING_ONLY, Dimension.BEHAVIORAL),
    (Classification.TRANSFERABLE, Dimension.HARD),
    (Classification.TRANSFERABLE, Dimension.HARD),
    (Classification.TRANSFERABLE, Dimension.HARD),
    (Classification.TRANSFERABLE, Dimension.HARD),
    (Classification.TRANSFERABLE, Dimension.BEHAVIORAL),
    (Classification.ASSERTED, Dimension.HARD),
    (Classification.ASSERTED, Dimension.HARD),
    (Classification.GAP, Dimension.HARD),
    (Classification.GAP, Dimension.BEHAVIORAL),
    (Classification.GAP, Dimension.BEHAVIORAL),
)
MATRIX_CHALLENGES = (
    (
        ChallengeClass.CLEAR_SUPPORTED,
        ChallengeClass.BOILERPLATE_OR_STATUS,
        ChallengeClass.SOFT_OR_TRANSFERABLE,
        ChallengeClass.ABSENT_EVIDENCE,
    )
    * 4
)[: len(MATRIX)]
PARTIAL = ChallengeClass.PARTIAL_CONTRIBUTION
STRATUM_LABELS = (Classification.TAILORING_ONLY, Classification.TRANSFERABLE, Classification.GAP)
CASES: tuple[tuple[Classification, Dimension, ChallengeClass], ...] = (
    *(
        (classification, dimension, challenge)
        for (classification, dimension), challenge in zip(MATRIX, MATRIX_CHALLENGES, strict=True)
    ),
    *((classification, Dimension.HARD, PARTIAL) for classification in STRATUM_LABELS),
)
SET_SIZE = len(CASES)
HELD_OUT = SET_SIZE + 50


def case_uuid(index: int) -> UUID:
    """Return the synthetic case id numbered ``index``."""
    return UUID(f"10000000-0000-4000-8000-{index:012d}")


def source_uuid(index: int) -> UUID:
    """Return the synthetic source id numbered ``index``."""
    return UUID(f"20000000-0000-4000-8000-{index:012d}")


def candidate_content(
    index: int, split: Split = "development", *, review_context_sha256: str = CONTEXT_HASH
) -> CandidateContent:
    """Return one synthetic candidate frozen in ``split`` for one review context."""
    return CandidateContent(
        protocol_version=PROTOCOL_VERSION,
        case_id=case_uuid(index),
        source_id=source_uuid(index),
        source_artifact_sha256=f"{index:064x}",
        requirement_text=f"Synthetic requirement {index}.",
        normalized_requirement_sha256=normalized_requirement_sha256(
            f"Synthetic requirement {index}."
        ),
        synthetic_inventory_hash=INVENTORY_HASH,
        review_context_sha256=review_context_sha256,
        publication_gate=PublicationGate(
            sanitized=True,
            privacy_review_passed=True,
            redistribution_approved=True,
            split=split,
        ),
    )


def pool_from_candidates(
    candidates: tuple[CandidateContent, ...], review_context_sha256: str = CONTEXT_HASH
) -> CandidatePool:
    """Return a hash-bound pool of the candidates in case-id order."""
    candidates = tuple(sorted(candidates, key=lambda item: str(item.case_id)))
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "review_context_sha256": review_context_sha256,
        "candidates": [item.model_dump(mode="json") for item in candidates],
    }
    return CandidatePool(
        protocol_version=PROTOCOL_VERSION,
        review_context_sha256=review_context_sha256,
        candidates=candidates,
        candidate_pool_sha256=domain_sha256(CANDIDATE_POOL_DOMAIN, body),
    )


def candidate_pool(review_context_sha256: str = CONTEXT_HASH) -> CandidatePool:
    """Return the nineteen development candidates and one held-out candidate."""
    context = review_context_sha256
    return pool_from_candidates(
        (
            *(
                candidate_content(index, review_context_sha256=context)
                for index in range(1, SET_SIZE + 1)
            ),
            candidate_content(HELD_OUT, "holdout", review_context_sha256=context),
        ),
        context,
    )


def resolved_label(
    index: int,
    *,
    classification: Classification | None = None,
    dimension: Dimension | None = None,
    challenge: ChallengeClass | None = None,
) -> ResolvedLabel:
    """Return an agreed label for case ``index``, its matrix cell unless overridden."""
    default_classification, default_dimension, default_challenge = CASES[
        index - 1 if index <= SET_SIZE else 0
    ]
    classification = classification or default_classification
    dimension = dimension or default_dimension
    cited = classification in {
        Classification.DIRECT,
        Classification.TAILORING_ONLY,
        Classification.TRANSFERABLE,
    }
    evidence = ("b-r1a",) if cited else ()
    strength = Strength.MODERATE if cited else Strength.NONE
    status = (
        "Authorized to work in Canada without sponsorship."
        if classification is Classification.ASSERTED
        else None
    )
    material = MatchDecision.model_validate(
        {
            "family": MATCH,
            "classification": classification,
            "strength": strength,
            "dimension": dimension,
            "challenge_class": challenge or default_challenge,
            "decisive_entry_ids": evidence,
            "asserted_status_fact": status,
        }
    )
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "family": MATCH,
        "case_id": case_uuid(index),
        "material": material,
        "source": "agreement",
        "review_a_sha256": f"{index + 100:064x}",
        "review_b_sha256": f"{index + 200:064x}",
        "adjudication_sha256": None,
    }
    return ResolvedLabel.model_validate(
        {
            **body,
            "resolved_label_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLVED_LABEL), body
            ),
        }
    )


def qa_set(
    pool: CandidatePool,
    *,
    rejected: frozenset[UUID] = frozenset(),
    approved_rationale: str = "",
) -> ExtractionQaSet:
    """Return extraction QA approving every candidate except ``rejected``."""
    findings = tuple(
        ExtractionQaFinding(
            case_id=candidate.case_id,
            source_span_exact=candidate.case_id not in rejected,
            section_boundary_valid=True,
            atomic=True,
            duplicate_handling_valid=True,
            employer_redacted=True,
            defect_codes=("source_span_mismatch",) if candidate.case_id in rejected else (),
            rationale=(
                "The source span did not match."
                if candidate.case_id in rejected
                else approved_rationale
            ),
            approved=candidate.case_id not in rejected,
        )
        for candidate in pool.candidates
    )
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_pool_sha256": pool.candidate_pool_sha256,
        "review_context_sha256": pool.review_context_sha256,
        "findings": findings,
    }
    return ExtractionQaSet.model_validate(
        {
            **body,
            "extraction_qa_sha256": domain_sha256(EXTRACTION_QA_DOMAIN, body),
        }
    )


def resolution_from_labels(labels: tuple[ResolvedLabel, ...]) -> ResolutionSet:
    """Return a hash-bound resolution set of the labels."""
    labels = tuple(sorted(labels, key=lambda item: str(item.case_id)))
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "family": MATCH,
        "review_set_sha256": REVIEW_SET_HASH,
        "labels": labels,
        "adjudications": (),
    }
    return ResolutionSet.model_validate(
        {
            **body,
            "resolution_set_sha256": domain_sha256(
                review_domain(MATCH, ReviewArtifact.RESOLUTION_SET), body
            ),
        }
    )


def resolution_set() -> ResolutionSet:
    """Return a label for every case in ``candidate_pool``."""
    return resolution_from_labels(
        tuple(resolved_label(index) for index in (*range(1, SET_SIZE + 1), HELD_OUT))
    )


def selected_cases(
    pool: CandidatePool, resolution: ResolutionSet, split: Split = "development"
) -> tuple[SelectedCase, ...]:
    """Return every labeled case of ``split`` keyed and positioned as selection does."""
    candidates = {item.case_id: item for item in pool.candidates}
    keyed = []
    for label in resolution.labels:
        candidate = candidates[label.case_id]
        if candidate.publication_gate.split != split:
            continue
        keyed.append(
            (
                selection_key_sha256(
                    protocol_version=PROTOCOL_VERSION,
                    candidate_pool_sha256=pool.candidate_pool_sha256,
                    resolution_sha256=split_resolution_sha256(pool, resolution, split),
                    case_id=label.case_id,
                    resolved_label_sha256=label.resolved_label_sha256,
                ),
                candidate,
                label,
            )
        )
    return tuple(
        SelectedCase(
            position=position,
            case_id=label.case_id,
            source_id=candidate.source_id,
            normalized_requirement_sha256=candidate.normalized_requirement_sha256,
            resolved_label_sha256=label.resolved_label_sha256,
            selection_key=key,
            classification=match_material(label).classification,
            strength=match_material(label).strength,
            dimension=match_material(label).dimension,
            challenge_class=match_material(label).challenge_class,
        )
        for position, (key, candidate, label) in enumerate(sorted(keyed), 1)
    )


def manifest_body(
    pool: CandidatePool,
    resolution: ResolutionSet,
    *,
    qa: ExtractionQaSet | None = None,
    selected: tuple[SelectedCase, ...] | None = None,
    held_out: tuple[SelectedCase, ...] | None = None,
    review_references: tuple[ReviewReference, ...] | None = None,
    adjudication_references: tuple[AdjudicationReference, ...] = (),
    rulings: Sequence[CaseRuling] = (),
    dataset_sha256: str | None = None,
) -> SelectionManifestBody:
    """Return a manifest body over the given cases, projecting its dataset digest."""
    qa = qa or qa_set(pool)
    selected = selected or selected_cases(pool, resolution)
    held_out = selected_cases(pool, resolution, "holdout") if held_out is None else held_out
    labels = {item.case_id: item for item in resolution.labels}
    review_references = review_references or tuple(
        ReviewReference(
            case_id=item.case_id,
            review_a_sha256=labels[item.case_id].review_a_sha256,
            review_b_sha256=labels[item.case_id].review_b_sha256,
        )
        for item in (*selected, *held_out)
    )
    projected = serialize_match_dataset((*selected, *held_out), pool, INVENTORY_HASH, rulings)
    return SelectionManifestBody(
        protocol_version=PROTOCOL_VERSION,
        selection_algorithm=SELECTION_ALGORITHM,
        synthetic_inventory_hash=INVENTORY_HASH,
        review_context_sha256=pool.review_context_sha256,
        candidate_pool_sha256=pool.candidate_pool_sha256,
        extraction_qa_sha256=qa.extraction_qa_sha256,
        review_set_sha256=resolution.review_set_sha256,
        resolution_set_sha256=resolution.resolution_set_sha256,
        constraints=SELECTION_CONSTRAINTS,
        ordered_cases=selected,
        held_out_cases=held_out,
        review_references=review_references,
        adjudication_references=adjudication_references,
        rulings=tuple(rulings),
        dataset_sha256=dataset_sha256 or bytes_sha256(projected),
    )


def selection_manifest(
    pool: CandidatePool, resolution: ResolutionSet, rulings: Sequence[CaseRuling] = ()
) -> SelectionManifest:
    """Return a hash-bound manifest over the pool's labeled cases and any rulings."""
    body = manifest_body(pool, resolution, rulings=rulings)
    return SelectionManifest(
        body=body,
        manifest_sha256=domain_sha256(SELECTION_MANIFEST_DOMAIN, body),
    )
