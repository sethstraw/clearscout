"""Outcome types a batching solver yields to the runner.

They live outside ``evals.inspect.runner`` because ``eval.sh`` runs the runner
as ``__main__``, where a class defined in it is not the one a solver imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clearscout.infrastructure.providers.base import UnusableResponseError


@dataclass(frozen=True)
class FailedCall:
    """A call the provider answered unusably, with the batch identity its rows record."""

    error: UnusableResponseError
    batch: dict[str, Any] | None = None


BatchOutcome = dict[str, Any] | FailedCall
