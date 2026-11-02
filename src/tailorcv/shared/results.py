"""One vocabulary for "did this pass", shared by verification, rendering, and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    """The verdict of a single check."""

    PASS = "pass"  # noqa: S105 - a verdict, not a credential
    WARN = "warn"
    FAIL = "fail"

    @property
    def failed(self) -> bool:
        """Return whether this outcome should stop a run."""
        return self is Outcome.FAIL


@dataclass(frozen=True)
class Finding:
    """One check, its verdict, and enough detail to act on it.

    ``redraftable`` says whether writing the resume again could clear this; a
    fault in text copied verbatim from the master CV cannot be. ``advisories``
    are notes for the person reviewing, never sent back to a redraft.
    """

    check: str
    outcome: Outcome
    detail: str = ""
    redraftable: bool = True
    advisories: tuple[str, ...] = ()

    def line(self) -> str:
        """Render the finding as one aligned line."""
        return f"{self.outcome.value.upper():<4} {self.check}: {self.detail}".rstrip(": ")


def table_cell(text: str, limit: int | None = None) -> str:
    """Flatten text so it cannot break out of a Markdown table cell.

    A caller may supply its own ``limit`` when its table gives the cell a fixed
    amount of room. Without one, only flattening and escaping are applied.
    """
    flat = " ".join(text.split()).replace("|", "\\|")
    if limit is None or len(flat) <= limit:
        return flat
    # Below the width of the "..." marker there is no room to truncate with one,
    # so cut hard rather than slicing from the end with a negative index.
    if limit < 3:
        return flat[: max(limit, 0)]
    # Cut at a word boundary so a truncated cell never ends mid-word.
    cut = flat[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


def worst(findings: tuple[Finding, ...]) -> Outcome:
    """Return the most severe outcome across ``findings``."""
    if any(finding.outcome is Outcome.FAIL for finding in findings):
        return Outcome.FAIL
    if any(finding.outcome is Outcome.WARN for finding in findings):
        return Outcome.WARN
    return Outcome.PASS
