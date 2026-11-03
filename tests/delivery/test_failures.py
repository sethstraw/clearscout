"""Every provider failure a user can hit must name the fix, not the JSON.

Written against the shapes the real SDKs produce, including a 429 whose meaning
is "add money" rather than "slow down".
"""

from __future__ import annotations

import pytest

from clearscout.application.config import ProviderName
from clearscout.infrastructure.providers.failures import translate
from clearscout.shared.errors import AuthError, ProviderError, ProviderFailure, QuotaError


class FakeApiError(Exception):
    """Stands in for an SDK error, carrying only the attributes both vendors set."""

    def __init__(
        self,
        message: str = "boom",
        *,
        status_code: int | None = None,
        body: object = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if body is not None:
            self.body = body
        if code is not None:
            self.code = code


def openai_no_credit() -> FakeApiError:
    """Return an error observed from the live API."""
    return FakeApiError(
        "Error code: 429",
        status_code=429,
        body={
            "error": {
                "message": "You have no credits remaining. Add credits to continue.",
                "type": "insufficient_quota",
                "param": None,
                "code": "credit_balance_exhausted",
            }
        },
    )


def test_empty_openai_account_is_a_quota_error() -> None:
    error = translate(openai_no_credit(), ProviderName.OPENAI, "screen this posting")
    assert isinstance(error, QuotaError)
    assert error.exit_code == 2


def test_quota_message_says_add_credit_and_offers_the_other_provider() -> None:
    text = str(translate(openai_no_credit(), ProviderName.OPENAI, "screen this posting"))
    assert "out of credit" in text
    assert "https://platform.openai.com/settings/organization/billing" in text
    assert "--provider anthropic" in text
    assert "screen this posting" in text


def test_prose_uses_the_company_name_and_commands_use_the_flag_value() -> None:
    # "Your openai account" reads like a typo; "--provider OpenAI" would not work.
    text = str(translate(FakeApiError(status_code=401), ProviderName.OPENAI, "verify that key"))
    assert "Your OpenAI key" in text
    assert "clearscout auth set openai" in text


def test_quota_message_reassures_that_nothing_was_billed() -> None:
    # A failed run must never leave someone wondering whether they paid for it.
    text = str(translate(openai_no_credit(), ProviderName.OPENAI, "screen this posting"))
    assert "Nothing was billed" in text


def test_anthropic_credit_exhaustion_points_at_the_anthropic_console() -> None:
    raw = FakeApiError(
        "credit balance is too low",
        status_code=400,
        body={"error": {"type": "billing_not_active", "message": "credit balance is too low"}},
    )
    error = translate(raw, ProviderName.ANTHROPIC, "draft the resume")
    assert isinstance(error, QuotaError)
    assert "console.anthropic.com/settings/billing" in str(error)
    assert "--provider openai" in str(error)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_an_auth_error_with_the_command_to_fix_it(status: int) -> None:
    error = translate(FakeApiError(status_code=status), ProviderName.OPENAI, "verify that key")
    assert isinstance(error, AuthError)
    assert "clearscout auth set openai" in str(error)


def test_an_unknown_model_sends_the_user_to_doctor() -> None:
    raw = FakeApiError(
        "The model does not exist",
        status_code=404,
        body={"error": {"code": "model_not_found", "message": "The model does not exist"}},
    )
    error = translate(raw, ProviderName.OPENAI, "screen this posting")
    assert isinstance(error, ProviderError)
    assert "clearscout doctor" in str(error)


def test_a_rate_limit_is_not_confused_with_running_out_of_money() -> None:
    # Both arrive as 429. The difference is the code, and conflating them would
    # send someone to a billing page over a problem that fixes itself.
    raw = FakeApiError(
        "Rate limit reached",
        status_code=429,
        body={"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached"}},
    )
    error = translate(raw, ProviderName.OPENAI, "screen this posting")
    assert isinstance(error, ProviderError)
    assert not isinstance(error, QuotaError)
    assert "speed limit rather than a spending one" in str(error)


def test_no_discounted_capacity_is_told_apart_from_a_rate_limit() -> None:
    """Both arrive as a 429, and only one is answered by waiting for the speed limit."""
    raw = FakeApiError(
        "Resource unavailable",
        status_code=429,
        body={"error": {"code": "resource_unavailable", "message": "Resource unavailable"}},
    )
    error = translate(raw, ProviderName.OPENAI, "screen this posting")
    assert "no spare capacity on the discounted tier" in str(error)
    assert "Nothing was billed" in str(error)
    assert "speed limit" not in str(error)


def test_anthropic_overload_tells_the_user_to_wait() -> None:
    raw = FakeApiError(
        "Overloaded",
        status_code=529,
        body={"error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    error = translate(raw, ProviderName.ANTHROPIC, "draft the resume")
    assert "overloaded" in str(error)
    assert "Try the same command again" in str(error)


def test_a_server_error_is_blamed_on_the_provider() -> None:
    error = translate(FakeApiError(status_code=503), ProviderName.OPENAI, "draft the resume")
    assert "server error (503)" in str(error)
    assert "not yours" in str(error)


def test_an_unclassified_failure_still_quotes_the_vendor_sentence() -> None:
    raw = FakeApiError("something odd", body={"error": {"message": "something odd happened"}})
    error = translate(raw, ProviderName.OPENAI, "draft the resume")
    assert "something odd happened" in str(error)


@pytest.mark.parametrize(
    ("status", "code", "named"),
    [
        (None, "server_error", "(code server_error)"),
        (400, "invalid_value", "(code invalid_value, HTTP 400)"),
        (422, None, "(HTTP 422)"),
    ],
)
def test_an_unclassified_failure_names_its_code_and_status(
    status: int | None, code: str | None, named: str
) -> None:
    raw = FakeApiError("odd", status_code=status, body={"error": {"message": "Odd.", "code": code}})
    error = translate(raw, ProviderName.OPENAI, "draft the resume")
    assert (
        str(error) == f"clearscout could not draft the resume because OpenAI returned: Odd. {named}"
    )


def test_a_bare_exception_does_not_raise_while_being_translated() -> None:
    # This module runs when things have already gone wrong; it must not add
    # a failure of its own to an error that carries no status, body, or code.
    error = translate(RuntimeError("connection reset"), ProviderName.OPENAI, "draft the resume")
    assert isinstance(error, ProviderError)
    assert "connection reset" in str(error)


@pytest.mark.parametrize("body", ["a string body", None, {"message": "unwrapped"}, {"error": None}])
def test_odd_response_bodies_are_survived(body: object) -> None:
    raw = FakeApiError("fallback text", body=body)
    assert isinstance(translate(raw, ProviderName.OPENAI, "draft"), ProviderError)


def test_a_top_level_code_attribute_is_read_when_the_body_has_none() -> None:
    # The OpenAI SDK sets .code on the exception itself as well as in the body.
    raw = FakeApiError("no credits", status_code=429, code="insufficient_quota")
    assert isinstance(translate(raw, ProviderName.OPENAI, "draft"), QuotaError)


def test_payment_required_is_a_quota_error_without_any_code() -> None:
    assert isinstance(
        translate(FakeApiError(status_code=402), ProviderName.OPENAI, "draft"), QuotaError
    )


@pytest.mark.parametrize("status", [408, 409])
def test_a_timeout_or_conflict_status_is_not_certainly_not_created(status: int) -> None:
    error = translate(
        FakeApiError(status_code=status),
        ProviderName.OPENAI,
        "dispatch this search",
        creating=True,
    )
    assert error.certainly_not_created is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_an_unambiguous_client_error_is_certainly_not_created(status: int) -> None:
    error = translate(
        FakeApiError(status_code=status),
        ProviderName.OPENAI,
        "dispatch this search",
        creating=True,
    )
    assert error.certainly_not_created is True


@pytest.mark.parametrize(
    ("raw", "not_found", "category"),
    [
        (FakeApiError(status_code=404), "response", ProviderFailure.RESPONSE_GONE),
        (FakeApiError(status_code=404), "model", None),
        (FakeApiError(status_code=429), "model", ProviderFailure.RATE_LIMITED),
        (FakeApiError(status_code=529), "model", ProviderFailure.OVERLOADED),
        (FakeApiError(status_code=503), "model", None),
    ],
)
def test_a_translated_failure_carries_its_category_apart_from_its_wording(
    raw: FakeApiError, not_found: str, category: ProviderFailure | None
) -> None:
    error = translate(raw, ProviderName.OPENAI, "collect it", not_found=not_found)
    assert error.category == category


def test_every_message_is_ascii() -> None:
    # The whole package forbids smart punctuation; error text is not exempt.
    cases = [
        openai_no_credit(),
        FakeApiError(status_code=401),
        FakeApiError(status_code=404),
        FakeApiError(status_code=429),
        FakeApiError(status_code=529),
        FakeApiError(status_code=500),
    ]
    for provider in ProviderName:
        for raw in cases:
            message = str(translate(raw, provider, "draft the resume"))
            assert message.isascii(), f"{provider} produced non-ASCII: {message!r}"
