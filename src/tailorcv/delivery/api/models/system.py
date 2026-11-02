"""System and provider API contracts."""

from typing import Literal

from pydantic import Field

from tailorcv.application.config import Effort, ProviderName, Tier
from tailorcv.infrastructure.providers.credentials import Source
from tailorcv.shared.models import StrictModel

# Longer than either vendor's key format, short enough to refuse a pasted
# document.
MAX_KEY_LENGTH = 500


# free_mode is the negation of the check that refuses a paid task at enqueue.
class HealthResponse(StrictModel):
    """Read-only service and migration health."""

    status: Literal["ok", "degraded"]
    version: str
    database_exists: bool
    database_reachable: bool
    schema_current: bool
    current_revision: str | None
    head_revision: str | None
    worker_available: bool
    free_mode: bool = Field(
        description="True while the configured provider has no API key; paid actions are refused."
    )


class ApiOnlyRootResponse(StrictModel):
    """Help for an installation without the single-page application."""

    name: str
    detail: str
    documentation: str


class ProviderTierResponse(StrictModel):
    """The model and effort one tier of work is configured to use."""

    tier: Tier
    model: str
    effort: Effort | None


class ProviderResponse(StrictModel):
    """What is configured for one provider and whether a key is available.

    Presence and origin only. No field carries key material.
    """

    provider: ProviderName
    key_present: bool
    key_source: Source | None
    # Disagrees with key_present when both places hold a key, and only this one
    # can be removed here.
    stored_in_keyring: bool
    environment_variable: str
    tiers: list[ProviderTierResponse]


class ProviderKeyBody(StrictModel):
    """An API key on its way to the system keyring, and nowhere else."""

    key: str = Field(
        max_length=MAX_KEY_LENGTH,
        description="Sent once, stored in the system keyring, and never returned.",
    )
