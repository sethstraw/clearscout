"""Failure types that carry an action, not a stack trace.

Every error a user can plausibly cause has a type here. Internal invariant
violations stay as plain assertions and exceptions; those are bugs.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderFailure(StrEnum):
    """A provider failure a caller handles differently from any other."""

    RESPONSE_GONE = "response_gone"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"


class ClearscoutError(Exception):
    """Base class for failures that should print as a message, not a traceback.

    The CLI catches this type, prints it to stderr, and exits with ``exit_code``.
    """

    exit_code = 1
    # Set only on a create-time failure; True means the provider rejected the
    # request before any response id existed, so nothing was billed.
    certainly_not_created: bool = False
    # Set by provider translation so callers branch on the failure, not its wording.
    category: ProviderFailure | None = None
    # True when the provider refused the account rather than this request, so a
    # sweep over further rows cannot succeed by moving on.
    account_level: bool = False


class ConfigError(ClearscoutError):
    """Configuration is missing, malformed, or points at a path that is not there."""

    exit_code = 2


class AuthError(ClearscoutError):
    """No usable API key was found for the selected provider."""

    exit_code = 2
    account_level = True


class IngestError(ClearscoutError):
    """A job description could not be turned into usable text."""

    exit_code = 3


class NoRequirementsError(IngestError):
    """The posting text was read, but no requirement could be extracted from it."""


class CvParseError(ClearscoutError):
    """The master CV could not be parsed into an evidence inventory."""

    exit_code = 3


class QuotaError(ClearscoutError):
    """The account has no credit left, so no call can succeed until it is topped up."""

    exit_code = 2
    account_level = True


class ProviderError(ClearscoutError):
    """The model provider returned something unusable after its own retries."""

    exit_code = 3


class CapacityError(ProviderError):
    """The provider had no capacity on the requested tier and billed nothing."""

    # True when the tier accepted the request and then stopped answering, which
    # a wait on the same tier is unlikely to cure.
    stalled: bool = False


class BudgetError(ClearscoutError):
    """The draft could not be brought inside the page budget without fabricating."""

    exit_code = 1


class ApprovalError(ClearscoutError):
    """A review approval cannot be applied to this run, so nothing was written."""

    exit_code = 5


class DiscoveryError(ClearscoutError):
    """Discovery refused to turn something into evidence, so nothing was written."""

    exit_code = 6
