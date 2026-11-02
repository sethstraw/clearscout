"""One drafting run: what it was produced under, proposed, and cost.

The drafted files live in the artifact store. What is here is the run's
identity, the prompt versions in force, the digest-bound proposals a human
answered, and the provider usage the run incurred.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tailorcv.domain import TAILORING_OUTCOMES
from tailorcv.infrastructure.persistence.models.base import AuditMixin, Base, IdMixin, _allowed
from tailorcv.infrastructure.persistence.types import UTCDateTime


class TailoringRun(IdMixin, AuditMixin, Base):
    """Immutable run identity and outcome; files remain in the artifact store."""

    __tablename__ = "tailoring_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "task_id", name="uq_tailoring_run_task"),
        CheckConstraint(_allowed("outcome", TAILORING_OUTCOMES), name="ck_tailoring_outcome"),
    )

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_posting_id: Mapped[str | None] = mapped_column(ForeignKey("job_postings.id"))
    master_cv_version_id: Mapped[str | None] = mapped_column(ForeignKey("master_cv_versions.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("background_tasks.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(80), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RunPromptVersion(IdMixin, AuditMixin, Base):
    """Which overridden prompt one stored workflow was produced under.

    A workflow reads a suite rather than a single prompt, so lineage is a row
    per override in force and no rows at all under the packaged suite. The
    reference cannot dangle because versions and runs refuse DELETE.
    """

    __tablename__ = "run_prompt_versions"
    __table_args__ = (
        UniqueConstraint("tailoring_run_id", "prompt_version_id", name="uq_run_prompt_version"),
        UniqueConstraint("batch_run_id", "prompt_version_id", name="uq_batch_prompt_version"),
        CheckConstraint(
            "(tailoring_run_id IS NOT NULL AND batch_run_id IS NULL) OR "
            "(tailoring_run_id IS NULL AND batch_run_id IS NOT NULL)",
            name="ck_run_prompt_one_run",
        ),
    )

    prompt_version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    tailoring_run_id: Mapped[str | None] = mapped_column(ForeignKey("tailoring_runs.id"))
    batch_run_id: Mapped[str | None] = mapped_column(ForeignKey("batch_runs.id"))


class ModelCall(IdMixin, AuditMixin, Base):
    """One provider request with the exact served-model and usage facts.

    The request digests identify the request: the same four values name the
    same call, and a changed one names which part of it moved. The bodies
    themselves live in `model_call_envelopes`.
    """

    __tablename__ = "model_calls"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("background_tasks.id"))
    tailoring_run_id: Mapped[str | None] = mapped_column(ForeignKey("tailoring_runs.id"))
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(160), nullable=False)
    served_model: Mapped[str | None] = mapped_column(String(160))
    provider_response_id: Mapped[str | None] = mapped_column(String(300))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # These two count the provider's prompt cache on a billed call.
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A different cache: true means TailorCV replayed a stored answer and no
    # provider call happened, so the two counters above are necessarily zero.
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    completion_reason: Mapped[str | None] = mapped_column(String(80))
    # Null means the request set no effort, which is not the same as low.
    effort: Mapped[str | None] = mapped_column(String(40))
    # Null in these three means the request left the vendor default in force.
    verbosity: Mapped[str | None] = mapped_column(String(40))
    image_detail: Mapped[str | None] = mapped_column(String(40))
    # The most the images could have cost, null when they were not measurable.
    image_token_ceiling: Mapped[int | None] = mapped_column(Integer)
    service_tier: Mapped[str | None] = mapped_column(String(80))
    # True when the discounted tier refused the call and it was answered on standard.
    resent_on_standard: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    code_revision: Mapped[str | None] = mapped_column(String(192))
    package_version: Mapped[str | None] = mapped_column(String(64))
    prompt_names: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    instructions_digest: Mapped[str | None] = mapped_column(String(64))
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
    task_digest: Mapped[str | None] = mapped_column(String(64))
    schema_digest: Mapped[str | None] = mapped_column(String(64))
    tool_policy: Mapped[str | None] = mapped_column(String(40))
    tool_version: Mapped[str | None] = mapped_column(String(80))
    search_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Queries and the pages they read. Provenance, never a quoted passage.
    searches: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # What the provider billed for, which exceeds `searches` when one errored.
    search_request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # False for a response that was billed for and could not be used.
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # TailorCV's own response cache key, derived for every call, hit or miss.
    cache_key: Mapped[str | None] = mapped_column(String(64))
    # Set only on a hit, as the cache file's own UTC text rather than a parsed time.
    cached_at: Mapped[str | None] = mapped_column(String(40))
    # Null when no provider answered, which is a cache hit or a failed send.
    response_digest: Mapped[str | None] = mapped_column(String(64))


class ModelCallEnvelope(IdMixin, AuditMixin, Base):
    """The provider's own request and response bodies for one model call.

    Each row holds the two documents for the `model_calls` row it names.
    """

    __tablename__ = "model_call_envelopes"
    __table_args__ = (UniqueConstraint("model_call_id", name="uq_model_call_envelope"),)

    model_call_id: Mapped[str] = mapped_column(ForeignKey("model_calls.id"), nullable=False)
    # The request carries prompt text including CV evidence, which stays local.
    request_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    response_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ResponseCacheArchive(IdMixin, AuditMixin, Base):
    """A response rescued from the on-disk response cache, which overwrites itself.

    The cache keys on request text the workspace never stores, so no row here
    can be attributed to any particular model call.
    """

    __tablename__ = "response_cache_archive"
    __table_args__ = (UniqueConstraint("cache_key", "stored_at", name="uq_response_cache_archive"),)

    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # The cache file's own UTC text stamp rather than a parsed time.
    stored_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    revision: Mapped[str | None] = mapped_column(String(192))
    package_version: Mapped[str | None] = mapped_column(String(64))


class CostRecord(IdMixin, AuditMixin, Base):
    """Original-currency cost attached to a model call."""

    __tablename__ = "cost_records"

    model_call_id: Mapped[str] = mapped_column(ForeignKey("model_calls.id"), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(12), nullable=False, default="USD")
    pricing_source: Mapped[str] = mapped_column(String(300), nullable=False, default="configured")
    # Which price data produced `amount`. Null means it was not recorded, which
    # is unknown rather than a stated version.
    price_snapshot: Mapped[str | None] = mapped_column(String(120))


class ReviewProposal(IdMixin, AuditMixin, Base):
    """Digest-protected line proposal from one tailoring run."""

    __tablename__ = "review_proposals"
    __table_args__ = (UniqueConstraint("tailoring_run_id", "proposal_key", name="uq_proposal"),)

    tailoring_run_id: Mapped[str] = mapped_column(ForeignKey("tailoring_runs.id"), nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(160), nullable=False)
    slot: Mapped[str] = mapped_column(String(300), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ApprovalDecision(IdMixin, AuditMixin, Base):
    """Append-only human decision for one digest-bound proposal."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("review_proposal_id", "manifest_sha256", name="uq_approval"),
    )

    review_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("review_proposals.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    replacement_text: Mapped[str | None] = mapped_column(Text)
    replacement_source_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
