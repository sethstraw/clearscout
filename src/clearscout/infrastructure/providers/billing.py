"""OpenAI Organization billing reader: actual billed cost for a date range.

Reads ``GET /v1/organization/costs``, which reports the organization's actual
dollar cost in daily buckets. That endpoint needs an organization admin API
key, distinct from the inference key, and reports the whole organization's
spend rather than any one workspace or task. The admin key is never logged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from clearscout.application.config import ProviderName

if TYPE_CHECKING:  # pragma: no cover - import cost is only paid at runtime
    from openai import OpenAI

# The endpoint caps a page at 180 daily buckets; ask for the most to page least.
_MAX_BUCKETS = 180
# Bound a runaway pagination cursor so a bad response cannot loop forever.
_MAX_PAGES = 400
_COSTS_PATH = "/organization/costs"

# A getter takes a path and query params and returns one parsed JSON page.
Getter = Callable[[str, dict[str, Any]], dict[str, Any]]


class OpenAIBillingError(Exception):
    """The organization billing read could not be completed.

    Raised for a missing, invalid, or under-scoped admin key, or any other
    provider refusal. The message never carries key material.
    """


@dataclass(frozen=True)
class BilledCost:
    """OpenAI's actual billed cost for one window, summed across daily buckets.

    Organization-wide: this is every charge on the admin key's organization in
    the window, not only the calls ClearScout made.
    """

    start_time: int
    end_time: int
    amount: Decimal
    currency: str


class OrganizationBilling(Protocol):
    """The billing read the reconciliation depends on."""

    def costs(self, start_time: int, end_time: int) -> BilledCost:
        """Return the organization's actual billed cost for a Unix-seconds window."""
        ...


class OpenAIBillingClient:
    """Query ``GET /v1/organization/costs`` for actual billed cost in a window."""

    name = ProviderName.OPENAI

    def __init__(
        self,
        admin_key: str,
        *,
        client: OpenAI | None = None,
        getter: Getter | None = None,
        transport: Any | None = None,
        timeout: float | None = None,
    ) -> None:
        """Build an SDK-backed getter, or accept one so tests never touch the network."""
        self._getter = getter or _sdk_getter(admin_key, client, transport, timeout)

    def costs(self, start_time: int, end_time: int) -> BilledCost:
        """Return the organization's actual billed cost between two Unix seconds.

        ``start_time`` is inclusive and ``end_time`` exclusive, matching the API.
        Pages through every daily bucket and sums the result amounts.
        """
        params: dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
            "bucket_width": "1d",
            "limit": _MAX_BUCKETS,
        }
        total = Decimal(0)
        currency: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._read(params)
            subtotal, page_currency = _page_totals(page)
            total += subtotal
            if currency is None:
                currency = page_currency
            if not page.get("has_more") or not (cursor := page.get("next_page")):
                break
            params = {**params, "page": cursor}
        return BilledCost(
            start_time=start_time,
            end_time=end_time,
            amount=total,
            currency=currency or "USD",
        )

    def _read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch one page, translating any provider error without leaking the key."""
        from openai import OpenAIError

        try:
            return self._getter(_COSTS_PATH, params)
        except OpenAIError as error:
            raise OpenAIBillingError(_unavailable_reason(error)) from error


def _page_totals(page: dict[str, Any]) -> tuple[Decimal, str | None]:
    """Sum one page's bucket amounts and report its currency."""
    subtotal = Decimal(0)
    currency: str | None = None
    for bucket in page.get("data") or []:
        for result in bucket.get("results") or []:
            amount = result.get("amount") or {}
            value = amount.get("value")
            if value is not None:
                subtotal += Decimal(str(value))
            if currency is None and amount.get("currency"):
                currency = str(amount["currency"]).upper()
    return subtotal, currency


def _sdk_getter(
    admin_key: str, client: OpenAI | None, transport: Any | None, timeout: float | None
) -> Getter:
    """Return a getter that reads pages through the official OpenAI SDK."""
    sdk = client or _build_client(admin_key, transport, timeout)

    def getter(path: str, params: dict[str, Any]) -> dict[str, Any]:
        # cast_to=dict[str, Any] returns the raw JSON page rather than a typed model.
        return sdk.get(path, cast_to=dict[str, Any], options={"params": params})

    return getter


def _build_client(admin_key: str, transport: Any | None, timeout: float | None) -> OpenAI:
    """Build the SDK client, mirroring the inference adapter's construction."""
    try:
        from openai import DefaultHttpxClient
        from openai import OpenAI as SyncOpenAI
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise OpenAIBillingError("The openai package is not installed.") from error
    options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    if transport is not None:
        options["http_client"] = DefaultHttpxClient(transport=transport)
    return SyncOpenAI(api_key=admin_key, max_retries=2, **options)


def _unavailable_reason(error: Exception) -> str:
    """Explain a billing failure without repeating provider text that may echo input."""
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return (
            "OpenAI refused the organization billing read. The admin key is missing, "
            "invalid, or lacks the organization admin (api.usage.read) scope."
        )
    if status is not None:
        return f"OpenAI could not return organization billing (HTTP {status})."
    return "OpenAI could not be reached for the organization billing read."
