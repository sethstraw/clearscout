"""Workspace and master-CV API contracts."""

from typing import Literal

from pydantic import Field

from clearscout.application.config import ProviderName
from clearscout.application.job_search.models import SourceTerms
from clearscout.application.master_cv import MAX_CV_BYTES
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.workspaces import (
    CitationDriftResponse,
    CitationImpactResponse,
    MasterCvContentResponse,
    MasterCvDiffResponse,
    MasterCvEditResponse,
    MasterCvImpactResponse,
    MasterCvImportResponse,
    MasterCvVersionResponse,
    WorkspaceResponse,
)
from clearscout.domain import SourceKind
from clearscout.domain.policy import LearningLoopPolicy
from clearscout.shared.models import StrictModel


class WorkspaceCreate(StrictModel):
    """User-owned workspace creation request."""

    slug: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=240)
    root_path: str | None = None
    provider: ProviderName | None = None


class GuardrailBody(StrictModel):
    """Complete replacement rules with an optimistic concurrency precondition."""

    rules: Guardrails
    expected_version: int | None = Field(default=None, ge=0)


class LearningLoopPolicyBody(StrictModel):
    """Complete learning-loop policy with an optimistic concurrency precondition."""

    policy: LearningLoopPolicy
    expected_version: int | None = Field(default=None, ge=0)


class SourceCreate(StrictModel):
    """One source to sweep, named and addressed by hand.

    An employer board is named by its employer; an aggregator is named by itself.
    """

    name: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    source_type: SourceKind = SourceKind.EMPLOYER_BOARD
    terms: SourceTerms = Field(default_factory=SourceTerms)


class FoundPostingCreate(StrictModel):
    """One posting somebody found themselves, addressed by its link.

    Title, employer, and place are hints shown in the queue until the posting is
    evaluated and states them itself.
    """

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)
    employer: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=500)
    check_publisher_facts: bool = False


class FoundPostingCheck(StrictModel):
    """One posting link to read through a supported publisher API."""

    url: str = Field(min_length=1, max_length=2000)


class PendingDiscoveryTitleUpdate(StrictModel):
    """A fix to a staged discovery's title hint; employer and location stay the publisher's."""

    title: str = Field(min_length=1, max_length=500)


class PendingPostingTextAttach(StrictModel):
    """Posting text a user obtained and attests belongs to this exact current URL."""

    content: str = Field(min_length=400, max_length=120_000)
    attested_current_exact_posting: Literal[True]
    expected_current_content_id: str | None
    expected_url: str = Field(min_length=1, max_length=2000)


class PendingPostingIdentityConfirm(StrictModel):
    """Confirmation that retained publisher evidence names this exact posting."""

    attested_same_posting: Literal[True]
    expected_current_content_id: str = Field(min_length=1)
    expected_url: str = Field(min_length=1, max_length=2000)


class SourcePreviewRequest(StrictModel):
    """One address to resolve before it is saved as a source."""

    url: str = Field(min_length=1, max_length=2000)
    source_type: SourceKind = SourceKind.EMPLOYER_BOARD


class SourceEdit(StrictModel):
    """The parts of a source a person may change. An omitted field stands.

    A changed name renames the employer this source is bound to and the
    discoveries staged under the old name as well.
    """

    name: str | None = Field(default=None, min_length=1, max_length=300)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    # Written to the bound employer only; empty clears it, and a job board or ATS
    # address is refused.
    careers_url: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=100)
    terms: SourceTerms | None = None


class MasterCvEdit(StrictModel):
    """Proposed master CV Markdown, for an impact check or for an in-place edit."""

    content: str = Field(min_length=1, max_length=MAX_CV_BYTES)
    # Only a save reads this; the impact check ignores it.
    confirm_reread: bool = False


__all__ = [
    "CitationDriftResponse",
    "CitationImpactResponse",
    "GuardrailBody",
    "LearningLoopPolicyBody",
    "MasterCvContentResponse",
    "MasterCvDiffResponse",
    "MasterCvEdit",
    "MasterCvEditResponse",
    "MasterCvImpactResponse",
    "MasterCvImportResponse",
    "MasterCvVersionResponse",
    "PendingDiscoveryTitleUpdate",
    "PendingPostingIdentityConfirm",
    "PendingPostingTextAttach",
    "SourceCreate",
    "SourceEdit",
    "SourcePreviewRequest",
    "WorkspaceCreate",
    "WorkspaceResponse",
]
