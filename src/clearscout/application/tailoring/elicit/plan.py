"""Reviewable insertions for confirmed discovery material."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clearscout.application.master_cv.cv import CvInventory, Span, spans
from clearscout.shared.errors import DiscoveryError

from .vocabulary import CONTEXT_LINES

if TYPE_CHECKING:
    from .conversation import Confirmed


@dataclass(frozen=True)
class Edit:
    """One insertion into the master CV, as line numbers and text."""

    index: int
    lines: tuple[str, ...]
    under: str


def plan(source: str, inventory: CvInventory, confirmed: Sequence[Confirmed]) -> tuple[Edit, ...]:
    """Return the exact insertions confirmed material would make.

    Computed before anything is written and printed before anything is written,
    because a change to a file this size is not reviewable after the fact.
    """
    sections, roles = spans(source)
    lines = source.splitlines()
    edits: list[Edit] = []
    for item in confirmed:
        placement = item.material.placement
        span = (
            roles.get(placement.role_key) if placement.role_key else sections.get(placement.section)
        )
        if span is None:
            raise DiscoveryError(
                f"{placement.named!r} is not a heading in {inventory.source}.\n"
                "  The master CV may have been edited since discovery started. Read it and "
                "start again."
            )
        edits.append(
            Edit(
                index=_insertion_point(lines, span),
                lines=("", f"- {item.material.text}"),
                under=span.heading,
            )
        )
    return tuple(edits)


def _insertion_point(lines: Sequence[str], span: Span) -> int:
    """Return the line a new bullet goes on: after the block's last real line.

    Trailing blank lines belong to the gap before the next heading rather than to
    this block, so ignoring them would push a bullet under the wrong heading.
    """
    index = span.end
    while index > span.start + 1 and not lines[index - 1].strip():
        index -= 1
    return index


def rewritten(source: str, edits: Sequence[Edit]) -> str:
    """Return the master CV with every planned insertion applied.

    Applied from the bottom of the document upwards, so that an insertion never
    moves the line another insertion was computed against.
    """
    lines = source.splitlines()
    for edit in sorted(edits, key=lambda item: item.index, reverse=True):
        lines[edit.index : edit.index] = list(edit.lines)
    return "\n".join(lines) + "\n"


def preview(source: str, edits: Sequence[Edit]) -> str:
    """Return what the change looks like, in place, before it is made."""
    lines = source.splitlines()
    blocks: list[str] = []
    for edit in edits:
        start = max(0, edit.index - CONTEXT_LINES)
        before = [f"    {line}" for line in lines[start : edit.index] if line.strip()]
        after = [
            f"    {line}" for line in lines[edit.index : edit.index + CONTEXT_LINES] if line.strip()
        ]
        added = [f"  + {line}" for line in edit.lines if line.strip()]
        blocks.append("\n".join([f"Under {edit.under}:", "", *before, *added, *after]))
    return "\n\n".join(blocks)
