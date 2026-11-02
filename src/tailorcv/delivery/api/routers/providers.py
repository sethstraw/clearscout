"""Providers HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter

from tailorcv.application.config import ProviderName, Settings, Tier, TierModels
from tailorcv.application.failures import UnavailableError, ValidationError
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import ApiStateDep, SettingsDep
from tailorcv.delivery.api.models.system import (
    ProviderKeyBody,
    ProviderResponse,
    ProviderTierResponse,
)
from tailorcv.infrastructure.providers.credentials import ENVIRONMENT_VARIABLES, KeyStore, find
from tailorcv.shared.errors import AuthError

router = APIRouter()


@router.get(
    f"{API_PREFIX}/providers",
    response_model=list[ProviderResponse],
    tags=["providers"],
    summary="List each provider, its configured models, and whether a key is available",
)
def list_providers(
    state: ApiStateDep,
    current: SettingsDep,
) -> list[ProviderResponse]:
    """Report where a key would come from, never what it is; account-wide, so no workspace."""
    return [provider_state(provider, state.key_store, current) for provider in ProviderName]


@router.put(
    f"{API_PREFIX}/providers/{{provider}}/key",
    response_model=ProviderResponse,
    tags=["providers"],
    summary="Store one provider's API key in the system keyring",
)
def store_provider_key(
    provider: ProviderName,
    body: ProviderKeyBody,
    state: ApiStateDep,
    current: SettingsDep,
) -> ProviderResponse:
    """Take a key once and answer with presence only."""
    key = body.key.strip()
    if not key:
        raise ValidationError("An API key is required; the value sent was blank.")
    try:
        state.key_store.set(provider, key)
    except AuthError as error:
        # The backend's own message, with any echo of the key removed.
        raise UnavailableError(str(error).replace(key, "[redacted]")) from error
    return provider_state(provider, state.key_store, current)


@router.delete(
    f"{API_PREFIX}/providers/{{provider}}/key",
    response_model=ProviderResponse,
    tags=["providers"],
    summary="Remove one provider's stored key from the system keyring",
)
def forget_provider_key(
    provider: ProviderName,
    state: ApiStateDep,
    current: SettingsDep,
) -> ProviderResponse:
    """Remove the stored key. A key set in the environment is untouched."""
    state.key_store.forget(provider)
    # forget swallows backend failures, so removal is confirmed by reading.
    if state.key_store.get(provider) is not None:
        raise UnavailableError(
            f"The system keyring still holds a {provider.value} key after the "
            "request to remove it. Remove it with your operating system's "
            "credential manager."
        )
    return provider_state(provider, state.key_store, current)


def provider_state(provider: ProviderName, store: KeyStore, settings: Settings) -> ProviderResponse:
    """Describe one provider's credential without letting the credential out."""
    found = find(provider, store=store)
    return ProviderResponse(
        provider=provider,
        key_present=found is not None,
        key_source=found.source if found is not None else None,
        stored_in_keyring=store.get(provider) is not None,
        environment_variable=ENVIRONMENT_VARIABLES[provider],
        tiers=provider_tiers(settings.tiers_for(provider)),
    )


def provider_tiers(tiers: TierModels) -> list[ProviderTierResponse]:
    """Project configured provider tiers into safe response models."""
    return [
        ProviderTierResponse(tier=tier, model=choice.model, effort=choice.effort)
        for tier, choice in (
            (Tier.EXTRACTION, tiers.extraction),
            (Tier.DRAFTING, tiers.drafting),
        )
    ]
