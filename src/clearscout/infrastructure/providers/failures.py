"""Turning a vendor's error into one that says what to do next.

Both SDKs raise exceptions carrying an HTTP status and a JSON body. This reads
the status and the vendor's own error code and raises a clearscout error whose
text names the fix. The classification is duck-typed rather than done against
SDK classes, so the module imports without either vendor package installed.
"""

from __future__ import annotations

from typing import Any

from clearscout.application.config import ProviderName
from clearscout.infrastructure.providers.credentials import CONSOLE_URLS
from clearscout.shared.errors import (
    AuthError,
    CapacityError,
    ClearscoutError,
    ProviderError,
    ProviderFailure,
    QuotaError,
)

BILLING_URLS = {
    ProviderName.ANTHROPIC: "https://console.anthropic.com/settings/billing",
    ProviderName.OPENAI: "https://platform.openai.com/settings/organization/billing",
}

# The vendors disagree on where the reason lives and what to call it.
CODE_FIELDS = ("code", "type")

OUT_OF_MONEY = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
        "billing_not_active",
    }
)

NO_SUCH_MODEL = frozenset({"model_not_found", "not_found_error"})

OVERLOADED = frozenset({"overloaded_error"})

# OpenAI refuses a discounted-tier request it has no spare capacity for.
NO_SPARE_CAPACITY = frozenset({"resource_unavailable"})

# A timeout or a conflict may have landed after the vendor created the response,
# so a 4xx here is not certainly a rejection before creation.
_AMBIGUOUS_CREATION_STATUSES = frozenset({408, 409})

# The enum values are what a user types after --provider.
DISPLAY_NAMES = {
    ProviderName.ANTHROPIC: "Anthropic",
    ProviderName.OPENAI: "OpenAI",
}


def translate(
    error: Exception,
    provider: ProviderName,
    action: str,
    *,
    creating: bool = False,
    not_found: str = "model",
) -> ClearscoutError:
    """Return the error to raise in place of a raw SDK failure.

    ``action`` completes the sentence "clearscout could not ...", so a message
    stays specific about which call failed without each caller writing it out.
    ``not_found="response"`` words a 404 as a missing response rather than an unknown model.
    ``creating`` marks most 4xx as ``certainly_not_created``; 408 and 409 stay
    ambiguous, since either can land after the vendor already created the response.
    """
    status = _status(error)
    built, category = _built(error, status, provider, action, not_found)
    built.category = category
    if creating:
        built.certainly_not_created = (
            status is not None
            and 400 <= status < 500
            and status not in _AMBIGUOUS_CREATION_STATUSES
        )
    return built


def _built(
    error: Exception, status: int | None, provider: ProviderName, action: str, not_found: str
) -> tuple[ClearscoutError, ProviderFailure | None]:
    """Return the translated error and the category a caller may branch on."""
    code = _code(error)
    name = DISPLAY_NAMES[provider]
    category: ProviderFailure | None = None

    if code in OUT_OF_MONEY or status == 402:
        built: ClearscoutError = QuotaError(
            f"Your {name} account is out of credit, so clearscout could not {action}.\n"
            f"Nothing was billed.\n\n"
            f"Add credit at {BILLING_URLS[provider]}, or run the same command against\n"
            f"the other provider with --provider {_other(provider)}."
        )
    elif status in (401, 403):
        built = AuthError(
            f"Your {name} key was refused, so clearscout could not {action}.\n"
            f"Check that the key is still active at {CONSOLE_URLS[provider]}, then\n"
            f"store the current one with: clearscout auth set {provider.value}"
        )
    elif code in NO_SUCH_MODEL or status == 404:
        built, category = _not_found(name, action, not_found)
    elif code in OVERLOADED or status == 529:
        category = ProviderFailure.OVERLOADED
        built = ProviderError(
            f"{name} is overloaded right now, so clearscout could not {action}.\n"
            f"Nothing was billed. Try the same command again in a few minutes."
        )
    elif code in NO_SPARE_CAPACITY:
        built = CapacityError(
            f"{name} had no spare capacity on the discounted tier, so clearscout could not\n"
            f"{action}. Nothing was billed. Retry, or run it on the standard tier."
        )
    elif status == 429:
        category = ProviderFailure.RATE_LIMITED
        built = ProviderError(
            f"{name} rate-limited the request, so clearscout could not {action}.\n"
            f"This is a speed limit rather than a spending one. Wait a minute and retry;\n"
            f"if it persists, your account tier may cap concurrent requests."
        )
    elif status is not None and status >= 500:
        built = ProviderError(
            f"{name} returned a server error ({status}), so clearscout could not {action}.\n"
            f"This is the provider's end, not yours. Try again shortly."
        )
    else:
        built = ProviderError(
            f"clearscout could not {action} because {name} returned: {_detail(error)}"
            f"{_identified(code, status)}"
        )
    return built, category


def _identified(code: str | None, status: int | None) -> str:
    """Return the vendor code and HTTP status as a trailing note, or nothing."""
    named = [f"code {code}"] if code else []
    if status is not None:
        named.append(f"HTTP {status}")
    return f" ({', '.join(named)})" if named else ""


def _not_found(
    name: str, action: str, not_found: str
) -> tuple[ClearscoutError, ProviderFailure | None]:
    if not_found == "response":
        return ProviderError(
            f"{name} could not find that background response, so clearscout could not "
            f"{action}.\nIt may have already finished, expired, or been cancelled."
        ), ProviderFailure.RESPONSE_GONE
    return ProviderError(
        f"{name} does not recognise the configured model, so clearscout could not "
        f"{action}.\nRun 'clearscout doctor' to see the models this account can "
        f"actually use;\nit prints a settings block you can paste."
    ), None


def _status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _code(error: Exception) -> str | None:
    for field in CODE_FIELDS:
        value = getattr(error, field, None)
        if isinstance(value, str) and value:
            return value
    inner = _error_object(error)
    for field in CODE_FIELDS:
        value = inner.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _detail(error: Exception) -> str:
    message = _error_object(error).get("message")
    if isinstance(message, str) and message:
        return message
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(error)


def _error_object(error: Exception) -> dict[str, Any]:
    """Return the ``error`` sub-object of the response body, or an empty dict.

    Both vendors nest the useful fields under ``error``, but a body can also be
    a string, ``None``, or already unwrapped.
    """
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return {}
    inner = body.get("error")
    if isinstance(inner, dict):
        return inner
    return body


def _other(provider: ProviderName) -> str:
    """Return the provider that is not this one, for the fallback suggestion."""
    if provider is ProviderName.ANTHROPIC:
        return ProviderName.OPENAI.value
    return ProviderName.ANTHROPIC.value
