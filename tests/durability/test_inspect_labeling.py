"""The blinded-labeling artifacts fail closed before any reviewer or provider runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from evals.inspect.labeling import (
    CANDIDATE_POOL_DOMAIN,
    EXTRACTION_QA_DOMAIN,
    FROZEN_RUBRIC_SHA256,
    NORMALIZATION_DOMAIN,
    OPERATIONAL_RECEIPT_DOMAIN,
    PROTOCOL_VERSION,
    REVIEW_CONTEXT_DOMAIN,
    REVIEWER_PACKET_DOMAIN,
    CandidateContent,
    CandidatePool,
    ExtractionQaFinding,
    ExtractionQaSet,
    GuardrailsSnapshot,
    InventorySnapshot,
    Operation,
    OperationalAttestations,
    OperationalReceipt,
    PublicationGate,
    ReviewContext,
    ReviewerPacket,
    ReviewerPacketCase,
    Split,
    bytes_sha256,
    canonical_json_bytes,
    checked_runtime_identity,
    domain_sha256,
    normalize_requirement,
    normalized_requirement_sha256,
    reject_forbidden_packet_keys,
    require_frozen_rubric,
    rubric_sha256,
)
from evals.support.inventory import synthetic_guardrails, synthetic_inventory
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_RUBRIC = REPO_ROOT / "evals/inspect/prompts/match_reviewer.md"

CASE_A = UUID("10000000-0000-4000-8000-000000000001")
CASE_B = UUID("10000000-0000-4000-8000-000000000002")
CASE_C = UUID("10000000-0000-4000-8000-000000000003")
SOURCE_A = UUID("20000000-0000-4000-8000-000000000001")
SOURCE_B = UUID("20000000-0000-4000-8000-000000000002")
SOURCE_C = UUID("20000000-0000-4000-8000-000000000003")
ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
PLACEHOLDER_HASH = "0" * 64
AGENT = "reviewer-agent-1"
LAUNCH = {"agentId": AGENT, "resolvedModel": "synthetic-model-1"}
CHECKED = "synthetic-model-1 (source: agent launch result resolvedModel)"


def _review_context(
    rubric_version: str = "match-rubric-v1",
    rubric_text: str = "Classify the requirement only from the frozen synthetic context.",
) -> ReviewContext:
    inventory = InventorySnapshot.from_inventory(synthetic_inventory())
    guardrails = GuardrailsSnapshot.from_guardrails(synthetic_guardrails())
    authorization = guardrails.work_authorization
    assert authorization is not None
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "rubric_version": rubric_version,
        "rubric_text": rubric_text,
        "synthetic_inventory": inventory,
        "synthetic_inventory_hash": inventory.content_hash,
        "asserted_status_lines": (authorization,),
        "guardrails": guardrails,
        "valid_entry_ids": tuple(item.id for item in inventory.entries),
    }
    return ReviewContext(
        protocol_version=PROTOCOL_VERSION,
        rubric_version=rubric_version,
        rubric_text=rubric_text,
        synthetic_inventory=inventory,
        synthetic_inventory_hash=inventory.content_hash,
        asserted_status_lines=(authorization,),
        guardrails=guardrails,
        valid_entry_ids=tuple(item.id for item in inventory.entries),
        review_context_sha256=domain_sha256(REVIEW_CONTEXT_DOMAIN, body),
    )


def _candidate(
    context: ReviewContext,
    *,
    case_id: UUID = CASE_A,
    source_id: UUID = SOURCE_A,
    artifact_sha256: str = ARTIFACT_A,
    requirement_text: str = "Build reliable Python services.",
    split: Split = "development",
) -> CandidateContent:
    return CandidateContent(
        protocol_version=PROTOCOL_VERSION,
        case_id=case_id,
        source_id=source_id,
        source_artifact_sha256=artifact_sha256,
        requirement_text=requirement_text,
        normalized_requirement_sha256=normalized_requirement_sha256(requirement_text),
        synthetic_inventory_hash=context.synthetic_inventory_hash,
        review_context_sha256=context.review_context_sha256,
        publication_gate=PublicationGate(
            sanitized=True,
            privacy_review_passed=True,
            redistribution_approved=True,
            split=split,
        ),
    )


def _candidate_pool(
    context: ReviewContext, candidates: tuple[CandidateContent, ...]
) -> CandidatePool:
    ordered = tuple(sorted(candidates, key=lambda item: str(item.case_id)))
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "review_context_sha256": context.review_context_sha256,
        "candidates": [item.model_dump(mode="json") for item in ordered],
    }
    return CandidatePool(
        protocol_version=PROTOCOL_VERSION,
        review_context_sha256=context.review_context_sha256,
        candidates=ordered,
        candidate_pool_sha256=domain_sha256(CANDIDATE_POOL_DOMAIN, body),
    )


def _qa_set(pool: CandidatePool, findings: tuple[ExtractionQaFinding, ...]) -> ExtractionQaSet:
    ordered = tuple(sorted(findings, key=lambda item: str(item.case_id)))
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_pool_sha256": pool.candidate_pool_sha256,
        "review_context_sha256": pool.review_context_sha256,
        "findings": [item.model_dump(mode="json") for item in ordered],
    }
    return ExtractionQaSet(
        protocol_version=PROTOCOL_VERSION,
        candidate_pool_sha256=pool.candidate_pool_sha256,
        review_context_sha256=pool.review_context_sha256,
        findings=ordered,
        extraction_qa_sha256=domain_sha256(EXTRACTION_QA_DOMAIN, body),
    )


def _approved(case_id: UUID) -> ExtractionQaFinding:
    return ExtractionQaFinding(
        case_id=case_id,
        source_span_exact=True,
        section_boundary_valid=True,
        atomic=True,
        duplicate_handling_valid=True,
        employer_redacted=True,
        approved=True,
    )


def _packet(pool: CandidatePool, qa: ExtractionQaSet, context: ReviewContext) -> ReviewerPacket:
    cases = tuple(
        ReviewerPacketCase(case_id=item.case_id, requirement_text=item.requirement_text)
        for item in reversed(pool.candidates)
    )
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_pool_sha256": pool.candidate_pool_sha256,
        "extraction_qa_sha256": qa.extraction_qa_sha256,
        "review_context_sha256": context.review_context_sha256,
        "rubric_version": context.rubric_version,
        "rubric_text": context.rubric_text,
        "synthetic_inventory": context.synthetic_inventory,
        "asserted_status_lines": context.asserted_status_lines,
        "guardrails": context.guardrails,
        "cases": cases,
    }
    return ReviewerPacket(
        protocol_version=PROTOCOL_VERSION,
        candidate_pool_sha256=pool.candidate_pool_sha256,
        extraction_qa_sha256=qa.extraction_qa_sha256,
        review_context_sha256=context.review_context_sha256,
        rubric_version=context.rubric_version,
        rubric_text=context.rubric_text,
        synthetic_inventory=context.synthetic_inventory,
        asserted_status_lines=context.asserted_status_lines,
        guardrails=context.guardrails,
        cases=cases,
        packet_sha256=domain_sha256(REVIEWER_PACKET_DOMAIN, body),
    )


def _packet_with_cases(
    packet: ReviewerPacket, cases: tuple[ReviewerPacketCase, ...]
) -> ReviewerPacket:
    body = {
        "protocol_version": packet.protocol_version,
        "candidate_pool_sha256": packet.candidate_pool_sha256,
        "extraction_qa_sha256": packet.extraction_qa_sha256,
        "review_context_sha256": packet.review_context_sha256,
        "rubric_version": packet.rubric_version,
        "rubric_text": packet.rubric_text,
        "synthetic_inventory": packet.synthetic_inventory,
        "asserted_status_lines": packet.asserted_status_lines,
        "guardrails": packet.guardrails,
        "cases": cases,
    }
    return ReviewerPacket(
        protocol_version=packet.protocol_version,
        candidate_pool_sha256=packet.candidate_pool_sha256,
        extraction_qa_sha256=packet.extraction_qa_sha256,
        review_context_sha256=packet.review_context_sha256,
        rubric_version=packet.rubric_version,
        rubric_text=packet.rubric_text,
        synthetic_inventory=packet.synthetic_inventory,
        asserted_status_lines=packet.asserted_status_lines,
        guardrails=packet.guardrails,
        cases=cases,
        packet_sha256=domain_sha256(REVIEWER_PACKET_DOMAIN, body),
    )


def test_canonical_json_is_utf8_compact_sorted_and_preserves_null() -> None:
    assert canonical_json_bytes({"z": None, "é": "✓", "a": 1}) == (
        b'{"a":1,"z":null,"\xc3\xa9":"\xe2\x9c\x93"}'
    )
    assert domain_sha256("domain-a", {"value": 1}) != domain_sha256("domain-b", {"value": 1})
    with pytest.raises(TypeError, match="string keys"):
        canonical_json_bytes({1: "not canonical"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="ASCII"):
        domain_sha256("dömain", "value")


def test_exact_byte_hash_distinguishes_a_final_newline() -> None:
    assert bytes_sha256(b'{"id":1}') != bytes_sha256(b'{"id":1}\n')


def test_requirement_normalization_has_frozen_unicode_and_whitespace_semantics() -> None:
    fullwidth_api = "".join(chr(code) for code in (0xFF21, 0xFF30, 0xFF29))
    source = f"  Build\u00a0{fullwidth_api}s\r\nwith\tPython.  "
    assert normalize_requirement(source) == "Build APIs with Python."
    assert normalized_requirement_sha256(source) == domain_sha256(
        NORMALIZATION_DOMAIN, "Build APIs with Python."
    )
    with pytest.raises(ValueError, match="normalize to empty"):
        normalized_requirement_sha256(" \r\n\t ")


def test_candidate_rejects_unknown_fields_non_uuid4_and_stale_text_digest() -> None:
    context = _review_context()
    payload = _candidate(context).model_dump(mode="python")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CandidateContent.model_validate({**payload, "expected_classification": "DIRECT"})
    with pytest.raises(ValidationError, match="UUIDv4"):
        CandidateContent.model_validate({**payload, "case_id": UUID(int=0)})
    with pytest.raises(ValidationError, match="does not match requirement_text"):
        CandidateContent.model_validate({**payload, "normalized_requirement_sha256": "f" * 64})


def test_candidate_pool_requires_order_unique_requirements_context_and_exact_hash() -> None:
    context = _review_context()
    first = _candidate(context)
    second = _candidate(
        context,
        case_id=CASE_B,
        source_id=SOURCE_B,
        artifact_sha256=ARTIFACT_B,
        requirement_text="Operate a Kubernetes platform.",
    )
    pool = _candidate_pool(context, (first, second))
    assert len(pool.candidates) == 2
    with pytest.raises(ValidationError, match="ordered by case_id"):
        CandidatePool(
            protocol_version=PROTOCOL_VERSION,
            review_context_sha256=context.review_context_sha256,
            candidates=(second, first),
            candidate_pool_sha256=pool.candidate_pool_sha256,
        )
    duplicate = first.model_copy(update={"case_id": CASE_B, "source_id": SOURCE_B})
    with pytest.raises(ValidationError, match="duplicate normalized requirement"):
        _candidate_pool(context, (first, duplicate))
    with pytest.raises(ValidationError, match="does not match candidate content"):
        CandidatePool.model_validate(
            {**pool.model_dump(mode="python"), "candidate_pool_sha256": PLACEHOLDER_HASH}
        )


def test_extraction_qa_derives_approval_and_requires_exact_candidate_coverage() -> None:
    context = _review_context()
    pool = _candidate_pool(context, (_candidate(context),))
    finding = _approved(CASE_A)
    qa = _qa_set(pool, (finding,))
    qa.validate_candidates(pool)
    with pytest.raises(ValidationError, match="approved must equal"):
        ExtractionQaFinding(
            **{
                **finding.model_dump(mode="python"),
                "atomic": False,
            }
        )
    empty_other = _candidate_pool(
        context,
        (
            _candidate(
                context,
                case_id=CASE_B,
                source_id=SOURCE_B,
                artifact_sha256=ARTIFACT_B,
                requirement_text="Operate Kubernetes.",
            ),
        ),
    )
    wrong_coverage = _qa_set(empty_other, (_approved(CASE_A),))
    with pytest.raises(ValueError, match="cover every candidate"):
        wrong_coverage.validate_candidates(empty_other)


def test_review_context_binds_inventory_status_rubric_and_guardrails() -> None:
    context = _review_context()
    assert context.asserted_status_lines == (synthetic_guardrails().work_authorization,)
    payload = context.model_dump(mode="python")
    with pytest.raises(ValidationError, match="synthetic_inventory_hash"):
        ReviewContext.model_validate({**payload, "synthetic_inventory_hash": "f" * 16})
    with pytest.raises(ValidationError, match="asserted status lines"):
        ReviewContext.model_validate(
            {**payload, "asserted_status_lines": ("Citizen of Freedonia.",)}
        )
    with pytest.raises(ValidationError, match="does not match review context"):
        ReviewContext.model_validate({**payload, "rubric_text": "Changed rubric."})


def test_forbidden_packet_key_scan_is_recursive_but_does_not_scan_text_values() -> None:
    reject_forbidden_packet_keys(
        {"cases": [{"requirement_text": "Experience building model scoring services."}]}
    )
    with pytest.raises(ValueError, match="Current-Classification"):
        reject_forbidden_packet_keys({"cases": [{"nested": {"Current-Classification": "DIRECT"}}]})
    with pytest.raises(ValueError, match="metadata"):
        reject_forbidden_packet_keys({"cases": [], "metadata": {}})
    with pytest.raises(ValueError, match="expected_strength"):
        reject_forbidden_packet_keys({"cases": [{"expected_strength": "moderate"}]})


def test_reviewer_packet_is_an_exact_blind_projection_of_approved_inputs() -> None:
    context = _review_context()
    candidates = (
        _candidate(context),
        _candidate(
            context,
            case_id=CASE_B,
            source_id=SOURCE_B,
            artifact_sha256=ARTIFACT_B,
            requirement_text="Operate a Kubernetes platform.",
        ),
    )
    pool = _candidate_pool(context, candidates)
    qa = _qa_set(pool, (_approved(CASE_A), _approved(CASE_B)))
    packet = _packet(pool, qa, context)
    packet.validate_inputs(pool, qa, context)
    serialized = packet.model_dump(mode="json")
    assert "source_id" not in canonical_json_bytes(serialized).decode("utf-8")
    assert "expected_classification" not in canonical_json_bytes(serialized).decode("utf-8")

    changed_case = packet.cases[0].model_copy(update={"requirement_text": "Changed."})
    changed_cases = (changed_case, *packet.cases[1:])
    changed = _packet_with_cases(packet, changed_cases)
    with pytest.raises(ValueError, match="differs from the candidate pool"):
        changed.validate_inputs(pool, qa, context)

    wrong_inventory = candidates[0].model_copy(update={"synthetic_inventory_hash": "f" * 16})
    wrong_pool = _candidate_pool(context, (wrong_inventory, candidates[1]))
    wrong_qa = _qa_set(wrong_pool, (_approved(CASE_A), _approved(CASE_B)))
    wrong_packet = _packet(wrong_pool, wrong_qa, context)
    with pytest.raises(ValueError, match="candidate inventory"):
        wrong_packet.validate_inputs(wrong_pool, wrong_qa, context)


def test_packet_order_is_bound_without_changing_the_candidate_pool() -> None:
    context = _review_context()
    pool = _candidate_pool(
        context,
        (
            _candidate(context),
            _candidate(
                context,
                case_id=CASE_B,
                source_id=SOURCE_B,
                artifact_sha256=ARTIFACT_B,
                requirement_text="Operate a Kubernetes platform.",
            ),
        ),
    )
    qa = _qa_set(pool, (_approved(CASE_A), _approved(CASE_B)))
    first = _packet(pool, qa, context)
    second = _packet_with_cases(first, tuple(reversed(first.cases)))
    assert first.packet_sha256 != second.packet_sha256
    assert first.candidate_pool_sha256 == second.candidate_pool_sha256
    second.validate_inputs(pool, qa, context)


def _receipt() -> OperationalReceipt:
    started = datetime(2026, 8, 26, 12, tzinfo=UTC)
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": Operation.EXTRACT,
        "agent_id": AGENT,
        "agent_kind": "agent",
        "runtime_identity": CHECKED,
        "started_at": started,
        "completed_at": started + timedelta(minutes=2),
        "input_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "attestations": OperationalAttestations(
            source_scope_respected=True,
            historical_outcomes_unseen=True,
            evaluated_output_unseen=True,
        ),
    }
    return OperationalReceipt.model_validate(
        {**body, "receipt_sha256": domain_sha256(OPERATIONAL_RECEIPT_DOMAIN, body)}
    )


def test_operational_receipt_is_utc_chronological_and_hash_bound() -> None:
    receipt = _receipt()
    started = receipt.started_at
    assert receipt.operation is Operation.EXTRACT
    changed = {**receipt.model_dump(mode="python"), "completed_at": started - timedelta(1)}
    with pytest.raises(ValidationError, match="cannot predate"):
        OperationalReceipt.model_validate(changed)
    changed = {**receipt.model_dump(mode="python"), "started_at": started.replace(tzinfo=None)}
    with pytest.raises(ValidationError, match="aware UTC"):
        OperationalReceipt.model_validate(changed)
    with pytest.raises(ValidationError, match="does not match operational receipt"):
        OperationalReceipt.model_validate(
            {**receipt.model_dump(mode="python"), "agent_id": "different-agent"}
        )


def test_the_tracked_reviewer_rubric_is_the_text_its_version_froze() -> None:
    """Editing the rubric file without registering a new version fails here."""
    text = TRACKED_RUBRIC.read_text(encoding="utf-8")

    assert rubric_sha256(text) in FROZEN_RUBRIC_SHA256.values()
    require_frozen_rubric(_review_context("match-review-rubric-v2", text))


def test_every_receipted_review_context_keeps_its_version_frozen_rubric() -> None:
    receipted = sorted((REPO_ROOT / "evals/golden/receipts").glob("*/review-context.json"))
    contexts = [ReviewContext.model_validate_json(path.read_bytes()) for path in receipted]

    assert contexts
    require_frozen_rubric(*contexts)


def test_a_rubric_changed_under_a_frozen_version_is_refused() -> None:
    text = TRACKED_RUBRIC.read_text(encoding="utf-8")
    edited = text.replace("`GAP`", "`GAP` (edited)", 1)

    with pytest.raises(ValueError, match="without a rubric_version change"):
        require_frozen_rubric(_review_context("match-review-rubric-v2", edited))
    require_frozen_rubric(_review_context("match-review-rubric-v3", edited))


def test_two_rounds_cannot_share_a_version_over_different_rubric_text() -> None:
    first = _review_context("synthetic-rubric-v1", "Judge only from the synthetic context.")
    second = _review_context("synthetic-rubric-v1", "Judge from the synthetic context alone.")

    require_frozen_rubric(first, first)
    with pytest.raises(ValueError, match="without a rubric_version change"):
        require_frozen_rubric(first, second)


def test_a_reselection_pool_keeps_every_case_in_the_split_it_was_frozen_in() -> None:
    context = _review_context()
    held_out = _candidate(
        context,
        case_id=CASE_B,
        source_id=SOURCE_B,
        requirement_text="Operate managed databases.",
        split="holdout",
    )
    prior = _candidate_pool(context, (_candidate(context), held_out))
    grown = _candidate_pool(
        context,
        (
            *prior.candidates,
            _candidate(
                context,
                case_id=CASE_C,
                source_id=SOURCE_C,
                requirement_text="Write clear design documents.",
            ),
        ),
    )

    grown.require_splits_kept(prior)


@pytest.mark.parametrize(
    ("case_id", "source_id", "requirement_text"),
    [
        (CASE_B, SOURCE_B, "Write clear design documents."),
        (CASE_C, SOURCE_B, "Write clear design documents."),
        (CASE_C, SOURCE_C, "Operate  managed  databases."),
    ],
    ids=["same-case", "same-source", "same-requirement"],
)
def test_a_case_cannot_move_between_splits_across_a_reselection(
    case_id: UUID, source_id: UUID, requirement_text: str
) -> None:
    context = _review_context()
    held_out = _candidate(
        context,
        case_id=CASE_B,
        source_id=SOURCE_B,
        requirement_text="Operate managed databases.",
        split="holdout",
    )
    prior = _candidate_pool(context, (_candidate(context), held_out))
    moved = _candidate(
        context,
        case_id=case_id,
        source_id=source_id,
        artifact_sha256=ARTIFACT_B,
        requirement_text=requirement_text,
    )
    changed = _candidate_pool(context, (_candidate(context), moved))

    with pytest.raises(ValueError, match="between splits"):
        changed.require_splits_kept(prior)


def test_one_pool_cannot_split_a_source_across_development_and_holdout() -> None:
    context = _review_context()
    leaked = _candidate(
        context,
        case_id=CASE_B,
        source_id=SOURCE_A,
        requirement_text="Operate managed databases.",
        split="holdout",
    )

    with pytest.raises(ValidationError, match="crosses splits"):
        _candidate_pool(context, (_candidate(context), leaked))


def test_a_launch_result_identity_is_recorded_with_that_source() -> None:
    assert checked_runtime_identity("synthetic-model-1", LAUNCH, agent_id=AGENT) == CHECKED
    assert checked_runtime_identity(None, LAUNCH, agent_id=AGENT) == CHECKED


def test_a_claimed_identity_the_launch_result_contradicts_is_refused() -> None:
    with pytest.raises(ValueError, match="does not match the launch result"):
        checked_runtime_identity("synthetic-model-2", LAUNCH, agent_id=AGENT)


@pytest.mark.parametrize(
    "launch",
    [{**LAUNCH, "agentId": "reviewer-agent-2"}, {"resolvedModel": "synthetic-model-1"}],
    ids=["another-agent", "no-agent"],
)
def test_a_launch_result_must_belong_to_the_receipted_agent(launch: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="agentId"):
        checked_runtime_identity(None, launch, agent_id=AGENT)


def test_a_launch_result_without_a_resolved_model_is_refused() -> None:
    with pytest.raises(ValueError, match="runtime identity"):
        checked_runtime_identity(None, {"agentId": AGENT, "model": "m"}, agent_id=AGENT)


@pytest.mark.parametrize(
    "identity",
    ["synthetic-model-1", "synthetic-model-1 (source: orchestrating session, unchecked)"],
    ids=["no-source", "unchecked"],
)
def test_a_receipt_refuses_an_identity_no_launch_result_checked(identity: str) -> None:
    receipt = _receipt()

    with pytest.raises(ValidationError, match="launch result"):
        OperationalReceipt.model_validate(
            {**receipt.model_dump(mode="python"), "runtime_identity": identity}
        )
