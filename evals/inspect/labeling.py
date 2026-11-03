"""Strict, hash-bound artifacts for blinded match-labeling preparation.

The artifacts defined here contain synthetic evaluation context and sanitized
posting requirements. Private orchestration may use them, but source mappings,
agent prompts, and historical outcomes never belong in reviewer packets.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

from clearscout.application.master_cv.cv import CvInventory, EntryKind
from clearscout.application.tailoring.guardrails import Guardrails

type MatchProtocolVersion = Literal["clearscout-match-development-v1"]
type RelevanceProtocolVersion = Literal["clearscout-relevance-review-development-v1"]
type ProtocolVersion = MatchProtocolVersion | RelevanceProtocolVersion
PROTOCOL_VERSION: MatchProtocolVersion = "clearscout-match-development-v1"
NORMALIZATION_DOMAIN = "clearscout-match-normalized-requirement-v1"
CANDIDATE_POOL_DOMAIN = "clearscout-match-candidate-pool-v1"
EXTRACTION_QA_DOMAIN = "clearscout-match-extraction-qa-v1"
REVIEW_CONTEXT_DOMAIN = "clearscout-match-review-context-v1"
REVIEWER_PACKET_DOMAIN = "clearscout-match-reviewer-packet-v1"
OPERATIONAL_RECEIPT_DOMAIN = "clearscout-match-operational-receipt-v1"
RUBRIC_DOMAIN = "clearscout-review-rubric-v1"

# A registered rubric version names exactly one text; a changed text needs a new version.
FROZEN_RUBRIC_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "match-review-rubric-v1": (
            "500c49cc92d19521deb3b9be993226bad86460acb85faee2120d1d0fb7ebcb67"
        ),
        "match-review-rubric-v2": (
            "3fef1d5557945e20329d7b661161f59a0d9ac2fb712abac7c285890f805be767"
        ),
    }
)


class ReviewFamily(StrEnum):
    """An evaluation family whose labels go through the blinded review protocol."""

    MATCH = "match"
    RELEVANCE_REVIEW = "relevance-review"


_PROTOCOL_VERSIONS: dict[ReviewFamily, ProtocolVersion] = {
    ReviewFamily.MATCH: "clearscout-match-development-v1",
    ReviewFamily.RELEVANCE_REVIEW: "clearscout-relevance-review-development-v1",
}


def protocol_version(family: ReviewFamily) -> ProtocolVersion:
    """Return the development protocol version one family's artifacts declare."""
    return _PROTOCOL_VERSIONS[family]


# The candidate pool, extraction QA, and reviewer packet are built around a
# requirement span, so only the match family has a blind reviewer packet.
PACKETED_FAMILIES: frozenset[ReviewFamily] = frozenset({ReviewFamily.MATCH})


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
InventoryHash = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
NonBlank = Annotated[str, Field(min_length=1)]
BoundedText = Annotated[str, Field(min_length=1, max_length=4_000)]
# The rubric ports the match prompt's rules, so it outgrows the free-text cap.
RubricText = Annotated[str, Field(min_length=1, max_length=10_000)]


class _ArtifactModel(BaseModel):
    """Reject unknown fields and make validated artifact content immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def canonical_json_bytes(value: BaseModel | Mapping[str, Any] | Sequence[Any] | str) -> bytes:
    """Serialize a JSON value with the one canonical encoding used by artifacts."""
    _require_string_keys(value)
    payload: Any = to_jsonable_python(value)
    _require_string_keys(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_string_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON mappings require string keys")
            _require_string_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _require_string_keys(child)


def domain_sha256(domain: str, value: BaseModel | Mapping[str, Any] | Sequence[Any] | str) -> str:
    """Hash canonical JSON under an ASCII domain separated by one NUL byte."""
    try:
        prefix = domain.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("hash domains must be ASCII") from error
    return hashlib.sha256(prefix + b"\0" + canonical_json_bytes(value)).hexdigest()


def bytes_sha256(content: bytes) -> str:
    """Hash exact artifact bytes without JSON canonicalization."""
    return hashlib.sha256(content).hexdigest()


def normalize_requirement(text: str) -> str:
    """Apply the frozen requirement normalization without substantive rewriting."""
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", normalized.strip())


def normalized_requirement_sha256(text: str) -> str:
    """Return the domain-separated digest of normalized requirement text."""
    normalized = normalize_requirement(text)
    if not normalized:
        raise ValueError("requirement text must not normalize to empty")
    return domain_sha256(NORMALIZATION_DOMAIN, normalized)


def _uuid4(value: UUID, field_name: str) -> None:
    if value.version != 4:
        raise ValueError(f"{field_name} must be a UUIDv4")


type Split = Literal["development", "holdout"]


class PublicationGate(_ArtifactModel):
    """The four corpus gates every candidate source must pass, and its frozen split."""

    sanitized: Literal[True]
    privacy_review_passed: Literal[True]
    redistribution_approved: Literal[True]
    split: Split


class CandidateContent(_ArtifactModel):
    """One unlabeled requirement with no historical outcome or source identity."""

    protocol_version: MatchProtocolVersion
    case_id: UUID
    source_id: UUID
    source_artifact_sha256: Sha256
    requirement_text: BoundedText
    normalized_requirement_sha256: Sha256
    synthetic_inventory_hash: InventoryHash
    review_context_sha256: Sha256
    publication_gate: PublicationGate

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Bind opaque IDs and normalized text to the candidate content."""
        _uuid4(self.case_id, "case_id")
        _uuid4(self.source_id, "source_id")
        expected = normalized_requirement_sha256(self.requirement_text)
        if self.normalized_requirement_sha256 != expected:
            raise ValueError("normalized_requirement_sha256 does not match requirement_text")
        return self


class CandidatePool(_ArtifactModel):
    """The ordered, frozen candidate content presented to extraction QA."""

    protocol_version: MatchProtocolVersion
    review_context_sha256: Sha256
    candidates: tuple[CandidateContent, ...] = Field(min_length=1)
    candidate_pool_sha256: Sha256

    @model_validator(mode="after")
    def validate_pool(self) -> Self:
        """Require stable ordering, uniqueness, one context, and an exact digest."""
        ordered = tuple(sorted(self.candidates, key=lambda item: str(item.case_id)))
        if self.candidates != ordered:
            raise ValueError("candidate records must be ordered by case_id")
        if any(
            item.review_context_sha256 != self.review_context_sha256 for item in self.candidates
        ):
            raise ValueError("candidate review context does not match the pool")
        for name, values in (
            ("case_id", [item.case_id for item in self.candidates]),
            (
                "normalized requirement",
                [item.normalized_requirement_sha256 for item in self.candidates],
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"candidate pool contains a duplicate {name}")
        _split_crossing(self.candidates, self.candidates)
        body = {
            "protocol_version": self.protocol_version,
            "review_context_sha256": self.review_context_sha256,
            "candidates": [item.model_dump(mode="json") for item in ordered],
        }
        if self.candidate_pool_sha256 != domain_sha256(CANDIDATE_POOL_DOMAIN, body):
            raise ValueError("candidate_pool_sha256 does not match candidate content")
        return self

    def require_splits_kept(self, prior: CandidatePool) -> None:
        """Refuse a reselection pool that moves a prior case, source, or requirement."""
        _split_crossing(prior.candidates, self.candidates)


def _split_crossing(earlier: Sequence[CandidateContent], later: Sequence[CandidateContent]) -> None:
    """Refuse a case, source, or normalized requirement that sits in both splits."""
    splits: dict[tuple[str, object], set[Split]] = {}
    for item in (*earlier, *later):
        split = item.publication_gate.split
        for key in (
            ("case", item.case_id),
            ("source", item.source_id),
            ("requirement", item.normalized_requirement_sha256),
        ):
            splits.setdefault(key, set()).add(split)
    crossed = sorted(f"{kind} {value}" for (kind, value), found in splits.items() if len(found) > 1)
    if crossed:
        raise ValueError(f"candidate {crossed[0]} crosses splits: no case moves between splits")


class ExtractionQaFinding(_ArtifactModel):
    """One extraction-quality decision without a match-classification label."""

    case_id: UUID
    source_span_exact: bool
    section_boundary_valid: bool
    atomic: bool
    duplicate_handling_valid: bool
    employer_redacted: bool
    defect_codes: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")], ...] = ()
    rationale: Annotated[str, Field(max_length=2_000)] = ""
    approved: bool

    @model_validator(mode="after")
    def validate_approval(self) -> Self:
        """Derive approval from the five checks and absence of defects."""
        _uuid4(self.case_id, "case_id")
        checks_pass = all(
            (
                self.source_span_exact,
                self.section_boundary_valid,
                self.atomic,
                self.duplicate_handling_valid,
                self.employer_redacted,
            )
        )
        expected = checks_pass and not self.defect_codes
        if self.approved is not expected:
            raise ValueError("approved must equal the extraction-QA checks and defect list")
        if not self.approved and not self.rationale.strip():
            raise ValueError("rejected extraction QA requires a rationale")
        if len(self.defect_codes) != len(set(self.defect_codes)):
            raise ValueError("extraction-QA defect codes must be unique")
        return self


class ExtractionQaSet(_ArtifactModel):
    """A complete QA result bound to one candidate pool."""

    protocol_version: MatchProtocolVersion
    candidate_pool_sha256: Sha256
    review_context_sha256: Sha256
    findings: tuple[ExtractionQaFinding, ...] = Field(min_length=1)
    extraction_qa_sha256: Sha256

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        """Require stable unique findings and bind their exact content."""
        ordered = tuple(sorted(self.findings, key=lambda item: str(item.case_id)))
        if self.findings != ordered:
            raise ValueError("extraction-QA findings must be ordered by case_id")
        ids = [item.case_id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("extraction-QA findings contain a duplicate case_id")
        body = self.model_dump(mode="json", exclude={"extraction_qa_sha256"})
        if self.extraction_qa_sha256 != domain_sha256(EXTRACTION_QA_DOMAIN, body):
            raise ValueError("extraction_qa_sha256 does not match QA content")
        return self

    def validate_candidates(self, pool: CandidatePool) -> None:
        """Require exact case coverage and matching input identities."""
        if self.candidate_pool_sha256 != pool.candidate_pool_sha256:
            raise ValueError("extraction QA does not reference this candidate pool")
        if self.review_context_sha256 != pool.review_context_sha256:
            raise ValueError("extraction QA does not reference this review context")
        if {item.case_id for item in self.findings} != {item.case_id for item in pool.candidates}:
            raise ValueError("extraction QA must cover every candidate exactly once")


class InventoryRole(_ArtifactModel):
    """One role in the strict synthetic inventory projection."""

    key: str
    organization: str
    title: str | None
    title_of_record: str | None
    dates: str | None
    location: str | None
    start_year: int | None
    end_year: int | None
    current: bool
    order: int


class InventoryEntry(_ArtifactModel):
    """One citable entry in the strict synthetic inventory projection."""

    id: str
    kind: EntryKind
    section: str
    text: str
    role_key: str | None
    group: str | None
    label: str | None
    details: tuple[str, ...]
    order: int


class InventorySnapshot(_ArtifactModel):
    """The complete synthetic evidence vocabulary shown to reviewers."""

    content_hash: InventoryHash
    name: str
    contact_lines: tuple[str, ...]
    sections: tuple[str, ...]
    roles: tuple[InventoryRole, ...]
    entries: tuple[InventoryEntry, ...]

    @classmethod
    def from_inventory(cls, inventory: CvInventory) -> InventorySnapshot:
        """Project a product inventory onto the reviewer-visible fields."""
        return cls.model_validate(
            inventory.model_dump(mode="python", exclude={"source"}),
            strict=True,
        )


class GuardrailsSnapshot(_ArtifactModel):
    """The complete synthetic guardrail configuration shown to reviewers."""

    protected: tuple[str, ...]
    required: tuple[str, ...]
    forbidden: tuple[str, ...]
    work_authorization: str | None
    distinctions: tuple[str, ...]
    first_person: bool
    ascii_only: bool

    @classmethod
    def from_guardrails(cls, guardrails: Guardrails) -> GuardrailsSnapshot:
        """Project product guardrails onto an immutable reviewer snapshot."""
        payload = guardrails.model_dump(mode="python")
        for key in ("protected", "required", "forbidden", "distinctions"):
            payload[key] = tuple(payload[key])
        return cls.model_validate(payload, strict=True)


class ReviewContext(_ArtifactModel):
    """The synthetic facts and rubric against which all reviewers judge."""

    protocol_version: ProtocolVersion
    rubric_version: NonBlank
    rubric_text: RubricText
    synthetic_inventory: InventorySnapshot
    synthetic_inventory_hash: InventoryHash
    asserted_status_lines: tuple[NonBlank, ...] = Field(min_length=1)
    guardrails: GuardrailsSnapshot
    valid_entry_ids: tuple[NonBlank, ...] = Field(min_length=1)
    review_context_sha256: Sha256

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        """Bind the rubric, inventory, asserted facts, and evidence vocabulary."""
        if self.synthetic_inventory_hash != self.synthetic_inventory.content_hash:
            raise ValueError("synthetic_inventory_hash does not match the inventory")
        entry_ids = tuple(item.id for item in self.synthetic_inventory.entries)
        if self.valid_entry_ids != entry_ids or len(entry_ids) != len(set(entry_ids)):
            raise ValueError("valid_entry_ids must exactly match the unique inventory entries")
        available = {
            *self.synthetic_inventory.contact_lines,
            *(
                role.title_of_record
                for role in self.synthetic_inventory.roles
                if role.title_of_record
            ),
            *self.guardrails.protected,
        }
        if self.guardrails.work_authorization:
            available.add(self.guardrails.work_authorization)
        if not set(self.asserted_status_lines).issubset(available):
            raise ValueError("asserted status lines must come from the frozen synthetic context")
        body = self.model_dump(mode="json", exclude={"review_context_sha256"})
        if self.review_context_sha256 != domain_sha256(REVIEW_CONTEXT_DOMAIN, body):
            raise ValueError("review_context_sha256 does not match review context content")
        return self


def rubric_sha256(text: str) -> str:
    """Return the digest that freezes one rubric text under its version."""
    return domain_sha256(RUBRIC_DOMAIN, text)


def require_frozen_rubric(*contexts: ReviewContext) -> None:
    """Refuse a rubric text that differs from the one its version already froze."""
    frozen = dict(FROZEN_RUBRIC_SHA256)
    for context in contexts:
        digest = rubric_sha256(context.rubric_text)
        if frozen.setdefault(context.rubric_version, digest) != digest:
            raise ValueError(
                f"rubric {context.rubric_version!r} changed without a rubric_version change"
            )


class ReviewerPacketCase(_ArtifactModel):
    """One opaque requirement exposed to a reviewer agent."""

    case_id: UUID
    requirement_text: BoundedText

    @model_validator(mode="after")
    def validate_case_id(self) -> Self:
        """Reject identifiers that are not random UUIDv4 values."""
        _uuid4(self.case_id, "case_id")
        return self


class ReviewerPacket(_ArtifactModel):
    """The exact blind input one reviewer agent receives."""

    protocol_version: MatchProtocolVersion
    candidate_pool_sha256: Sha256
    extraction_qa_sha256: Sha256
    review_context_sha256: Sha256
    rubric_version: NonBlank
    rubric_text: RubricText
    synthetic_inventory: InventorySnapshot
    asserted_status_lines: tuple[NonBlank, ...] = Field(min_length=1)
    guardrails: GuardrailsSnapshot
    cases: tuple[ReviewerPacketCase, ...] = Field(min_length=1)
    packet_sha256: Sha256

    @model_validator(mode="after")
    def validate_packet(self) -> Self:
        """Require unique case IDs and bind the independently ordered packet."""
        ids = [item.case_id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("reviewer packet case IDs must be unique")
        body = self.model_dump(mode="json", exclude={"packet_sha256"})
        if self.packet_sha256 != domain_sha256(REVIEWER_PACKET_DOMAIN, body):
            raise ValueError("packet_sha256 does not match reviewer packet content")
        return self

    def validate_inputs(
        self, pool: CandidatePool, qa: ExtractionQaSet, context: ReviewContext
    ) -> None:
        """Verify that the packet is an exact projection of approved frozen inputs."""
        qa.validate_candidates(pool)
        if self.candidate_pool_sha256 != pool.candidate_pool_sha256:
            raise ValueError("reviewer packet does not reference this candidate pool")
        if self.extraction_qa_sha256 != qa.extraction_qa_sha256:
            raise ValueError("reviewer packet does not reference this extraction QA")
        if self.review_context_sha256 != context.review_context_sha256:
            raise ValueError("reviewer packet does not reference this review context")
        if any(
            item.synthetic_inventory_hash != context.synthetic_inventory_hash
            for item in pool.candidates
        ):
            raise ValueError("candidate inventory does not match the frozen review context")
        if (
            self.rubric_version != context.rubric_version
            or self.rubric_text != context.rubric_text
            or self.synthetic_inventory != context.synthetic_inventory
            or self.asserted_status_lines != context.asserted_status_lines
            or self.guardrails != context.guardrails
        ):
            raise ValueError("reviewer packet context differs from the frozen review context")
        approved = {finding.case_id for finding in qa.findings if finding.approved}
        candidate_text = {item.case_id: item.requirement_text for item in pool.candidates}
        if {item.case_id for item in self.cases} != approved:
            raise ValueError("reviewer packet must contain exactly the QA-approved cases")
        if any(candidate_text[item.case_id] != item.requirement_text for item in self.cases):
            raise ValueError("reviewer packet requirement text differs from the candidate pool")


_FORBIDDEN_PACKET_KEYS = frozenset(
    re.sub(r"[_\- ]", "", key.casefold())
    for key in (
        "decision",
        "decisions",
        "fit_decision",
        "hard_gate",
        "hard_gates",
        "score",
        "scores",
        "ruling",
        "rulings",
        "stored_classification",
        "current_classification",
        "expected_classification",
        "expected_strength",
        "target",
        "provider",
        "model",
        "prompt",
        "completion",
        "response",
        "solver_output",
        "database_url",
        "evaluation_id",
        "annotation_id",
        "artifact_ref",
        "source_filename",
        "source_path",
        "employer",
        "company",
        "title",
        "retrieved_on",
        "adjudicated_on",
        "historical_outcome",
        "metadata",
    )
)


def reject_forbidden_packet_keys(value: Any) -> None:
    """Reject outcome and source-identity keys recursively before packet validation."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("reviewer packet mappings require string keys")
            normalized = re.sub(r"[_\- ]", "", key.casefold())
            if normalized in _FORBIDDEN_PACKET_KEYS:
                raise ValueError(f"reviewer packet contains forbidden field {key!r}")
            reject_forbidden_packet_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            reject_forbidden_packet_keys(child)


class Operation(StrEnum):
    """A labeling operation whose provenance may be retained."""

    EXTRACT = "extract"
    EXTRACTION_QA = "extraction_qa"
    REVIEW_A = "review_a"
    REVIEW_B = "review_b"
    ADJUDICATE = "adjudicate"
    SELECT = "select"
    PROJECT_DATASET = "project_dataset"


class OperationalAttestations(_ArtifactModel):
    """The minimum blindness and source-scope assertions for an operation."""

    source_scope_respected: bool
    historical_outcomes_unseen: bool
    evaluated_output_unseen: bool


RUNTIME_IDENTITY_SOURCE = " (source: agent launch result resolvedModel)"


def checked_runtime_identity(
    claimed: str | None, launch_result: Mapping[str, Any], *, agent_id: str
) -> str:
    """Return the launch result's ``resolvedModel``, naming it as the identity's source.

    The launch result must name ``agent_id`` as its ``agentId``, and a claimed
    identity it contradicts is refused.
    """
    if launch_result.get("agentId") != agent_id:
        raise ValueError(
            f"the launch result's agentId {launch_result.get('agentId')!r} is not {agent_id!r}"
        )
    resolved = launch_result.get("resolvedModel")
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError("the launch result carries no resolvedModel runtime identity")
    if claimed is not None and claimed.strip() != resolved.strip():
        raise ValueError(
            f"claimed runtime identity {claimed.strip()!r} does not match the launch result's "
            f"resolvedModel {resolved.strip()!r}"
        )
    return resolved.strip() + RUNTIME_IDENTITY_SOURCE


class OperationalReceipt(_ArtifactModel):
    """Agent provenance stored beside, never inside, blind review packets."""

    protocol_version: ProtocolVersion
    operation: Operation
    agent_id: NonBlank
    agent_kind: Literal["agent"]
    runtime_identity: NonBlank
    started_at: datetime
    completed_at: datetime
    input_sha256: Sha256
    output_sha256: Sha256
    attestations: OperationalAttestations
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        """Require UTC chronology and bind all provenance content."""
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value.utcoffset() != UTC.utcoffset(value):
                raise ValueError(f"{name} must be an aware UTC datetime")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot predate started_at")
        model = self.runtime_identity.removesuffix(RUNTIME_IDENTITY_SOURCE)
        if model == self.runtime_identity or not model.strip():
            raise ValueError("runtime_identity must be a model an agent launch result resolved")
        body = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != domain_sha256(OPERATIONAL_RECEIPT_DOMAIN, body):
            raise ValueError("receipt_sha256 does not match operational receipt content")
        return self
