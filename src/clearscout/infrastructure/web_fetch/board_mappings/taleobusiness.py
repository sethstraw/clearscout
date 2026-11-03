"""Taleo Business Edition's posting record, as its public per-section RSS feed states it."""

from __future__ import annotations

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated_text,
)

# The feed states each posting's place, id, and HTML body under this namespace.
TALEOBUSINESS_NAMESPACE = "urn:TBERss"


def _body(posting: Posting) -> str:
    """Return the item's HTML body, or its plain description when the feed omits one."""
    return stated_text(
        posting.record, (f"{{{TALEOBUSINESS_NAMESPACE}}}html-description",)
    ) or stated_text(posting.record, ("description",))


TALEOBUSINESS_MAPPING = BoardDeclaration(
    platform="taleobusiness",
    title=("title",),
    url=("link",),
    requisition_id=(f"{{{TALEOBUSINESS_NAMESPACE}}}reqId",),
    places=(f"{{{TALEOBUSINESS_NAMESPACE}}}location",),
    description=_body,
)
