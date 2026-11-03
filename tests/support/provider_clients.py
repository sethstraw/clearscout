"""Build official provider adapters around a caller-owned HTTP client."""

from __future__ import annotations

from typing import Any

import httpx

from clearscout.application.config import ProviderName


def provider_for_http(
    provider: ProviderName, http: httpx.Client, *, max_retries: int | None = None
) -> Any:
    """Return the real provider adapter with network traffic confined to ``http``."""
    client_options: dict[str, Any] = {"api_key": "not-a-real-key", "http_client": http}
    if max_retries is not None:
        client_options["max_retries"] = max_retries

    if provider is ProviderName.ANTHROPIC:
        from anthropic import Anthropic

        from clearscout.infrastructure.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key="not-a-real-key",
            client=Anthropic(**client_options),
        )

    from openai import OpenAI

    from clearscout.infrastructure.providers.openai import OpenAIProvider

    return OpenAIProvider(
        api_key="not-a-real-key",
        client=OpenAI(**client_options),
    )
