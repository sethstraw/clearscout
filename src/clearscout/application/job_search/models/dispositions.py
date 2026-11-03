"""Disposition selections, previews and applied dispositions."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, RootModel, model_validator

from clearscout.domain import (
    EXPLICIT_DISMISSAL_REASONS,
    ExplicitDismissalCategory,
)
from clearscout.shared.models import StrictModel


class _DispositionSelectionFields(StrictModel):
    """Fields shared by every pending-discovery disposition request."""

    pending_discovery_ids: list[str] = Field(min_length=1, max_length=250)


class RuleDispositionSelection(_DispositionSelectionFields):
    """A rule-derived dismissal carrying its category and supporting reason."""

    action: Literal["dismiss"]
    selection_mode: Literal["rule"]
    category: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def keep_evidence_unavailability_human_only(self) -> Self:
        """Reject the explicit stop reason on a rule-derived dismissal."""
        if self.category == ExplicitDismissalCategory.CURRENT_EVIDENCE_UNAVAILABLE:
            raise ValueError(
                "Current evidence unavailable is available only for explicit human dismissals."
            )
        return self


def _checked_other_reason(reason: str, job_id: str | None, pending_id: str | None) -> str:
    """Return the trimmed prose the `other` dismissal category requires."""
    trimmed = reason.strip()
    if not trimmed:
        raise ValueError("The 'other' dismissal category requires a reason.")
    if job_id is not None or pending_id is not None:
        raise ValueError("Duplicate references require a typed duplicate category.")
    return trimmed


def _assert_duplicate_references(
    category: ExplicitDismissalCategory, job_id: str | None, pending_id: str | None
) -> None:
    """Pair each duplicate category with the one reference kind it may carry."""
    if category is ExplicitDismissalCategory.DUPLICATE_POSTING and not job_id:
        raise ValueError("The duplicate posting category requires a tracked job reference.")
    if category is ExplicitDismissalCategory.DUPLICATE_PENDING and not pending_id:
        raise ValueError("The duplicate pending category requires a pending discovery reference.")
    if job_id is not None and category is not ExplicitDismissalCategory.DUPLICATE_POSTING:
        raise ValueError("A tracked job reference is available only for duplicate postings.")
    if pending_id is not None and category is not ExplicitDismissalCategory.DUPLICATE_PENDING:
        raise ValueError("A pending discovery reference is available only for pending duplicates.")


class ExplicitDismissDispositionSelection(_DispositionSelectionFields):
    """A human dismissal using the stable explicit-dismiss vocabulary."""

    action: Literal["dismiss"]
    selection_mode: Literal["explicit"]
    category: ExplicitDismissalCategory
    reason: str = Field(default="", max_length=2000)
    duplicate_of_job_id: str | None = None
    duplicate_of_pending_id: str | None = None
    # Without it, a favorite or kept family is left out of the dismissal.
    dismiss_marked: bool = False

    @model_validator(mode="after")
    def use_category_reason(self) -> Self:
        """Require `other` prose and supply category defaults only for blank reasons."""
        if self.category is ExplicitDismissalCategory.OTHER:
            self.reason = _checked_other_reason(
                self.reason, self.duplicate_of_job_id, self.duplicate_of_pending_id
            )
            return self
        _assert_duplicate_references(
            self.category, self.duplicate_of_job_id, self.duplicate_of_pending_id
        )
        if not self.reason.strip():
            self.reason = EXPLICIT_DISMISSAL_REASONS[self.category]
        return self


class ExplicitRestoreDispositionSelection(_DispositionSelectionFields):
    """A restore carrying the category of the dismissal it reverses."""

    action: Literal["restore"]
    selection_mode: Literal["explicit"]
    category: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2000)


DispositionSelectionPayload = (
    RuleDispositionSelection
    | ExplicitDismissDispositionSelection
    | ExplicitRestoreDispositionSelection
)


class DispositionSelection(RootModel[DispositionSelectionPayload]):
    """One rule dismissal, explicit dismissal, or explicit restore."""

    @property
    def action(self) -> Literal["dismiss", "restore"]:
        """Return the requested durable action."""
        return self.root.action

    @property
    def selection_mode(self) -> Literal["rule", "explicit"]:
        """Return whether a rule or person selected the disposition."""
        return self.root.selection_mode

    @property
    def category(self) -> str:
        """Return the durable disposition category."""
        return self.root.category

    @property
    def reason(self) -> str:
        """Return the durable plain-language disposition reason."""
        return self.root.reason

    @property
    def pending_discovery_ids(self) -> list[str]:
        """Return the selected pending-discovery identifiers."""
        return self.root.pending_discovery_ids

    @property
    def duplicate_of_job_id(self) -> str | None:
        """Return the tracked posting referenced by a duplicate dismissal."""
        return getattr(self.root, "duplicate_of_job_id", None)

    @property
    def duplicate_of_pending_id(self) -> str | None:
        """Return the pending discovery referenced by a pending duplicate dismissal."""
        return getattr(self.root, "duplicate_of_pending_id", None)

    @property
    def dismiss_marked(self) -> bool:
        """Return whether a person confirmed dismissing favorite or kept families."""
        return bool(getattr(self.root, "dismiss_marked", False))


class DispositionTargetResponse(StrictModel):
    """One human-readable posting identity included in a disposition preview."""

    id: str
    primary_id: str
    kind: Literal["primary", "regional_variant"]
    employer: str
    title: str
    location: str
    discovery_url: str
    canonical_url: str
    # Set while the source no longer lists the posting.
    withdrawn_at: datetime | None = None


class DispositionPreview(StrictModel):
    """Exact targets and immutable state digest for one confirmation."""

    snapshot_sha256: str
    search_parameter_set_id: str | None
    targets: list[DispositionTargetResponse]
    # Selected family heads left out because a favorite or keep marker holds them.
    skipped_marked_ids: list[str] = Field(default_factory=list)
    # Rows a restore leaves dismissed because their source withdrew the posting.
    skipped_withdrawn_ids: list[str] = Field(default_factory=list)
    action: Literal["dismiss", "restore"]
    category: str
    reason: str


class PendingDuplicateCandidateResponse(StrictModel):
    """One retained pending discovery that may be named as a duplicate target."""

    id: str
    employer: str
    title: str
    location: str
    discovery_url: str
    dismissed: bool


class RuleDispositionApply(RuleDispositionSelection):
    """A confirmed deterministic rule dismissal."""

    snapshot_sha256: str = Field(min_length=64, max_length=64)


class ExplicitDismissDispositionApply(ExplicitDismissDispositionSelection):
    """A confirmed explicit dismissal."""

    snapshot_sha256: str = Field(min_length=64, max_length=64)


class ExplicitRestoreDispositionApply(ExplicitRestoreDispositionSelection):
    """A confirmed explicit restore."""

    snapshot_sha256: str = Field(min_length=64, max_length=64)


DispositionApplyPayload = (
    RuleDispositionApply | ExplicitDismissDispositionApply | ExplicitRestoreDispositionApply
)


class DispositionApply(RootModel[DispositionApplyPayload]):
    """A confirmed preview applied only while its state remains current."""

    @property
    def snapshot_sha256(self) -> str:
        """Return the preview state digest being confirmed."""
        return self.root.snapshot_sha256


class PendingDispositionResponse(StrictModel):
    """One immutable disposition event for history and restoration."""

    id: str
    pending_discovery_id: str
    action: Literal["dismiss", "restore"]
    selection_mode: Literal["rule", "explicit", "automatic"]
    category: str
    reason: str
    # What appended the event. A refused evaluation dismisses the row itself, so
    # without this the queue cannot tell an automatic dismissal from a person's.
    origin: str
    created_at: datetime
    duplicate_of_job_id: str | None
    duplicate_of_pending_id: str | None
    supersedes_event_id: str | None


class DismissedDiscoveryResponse(PendingDispositionResponse):
    """Current dismissal with enough posting identity to review and restore it."""

    employer: str
    title: str
    location: str
    discovery_url: str
    canonical_url: str
