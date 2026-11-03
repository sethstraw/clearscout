"""TELUS AI Community's job post, as its public single-post endpoint states it."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)

# TELUS publishes no board list, so a post is read only from the address a person holds.
TELUS_POST = re.compile(
    r"(?:^|//)(?:www\.)?telusinternational\.ai/cmp/public/jobs/available/(?P<job>\d+)(?=[/?#]|$)"
)


def telus_post_url(post_id: str) -> str:
    """Return the public endpoint for one TELUS AI Community job post."""
    return f"https://api.telusinternational.ai/apapi/v1/jobs-entry/job-posts/{post_id}/public"


def telus_withdrawn(post: Mapping[str, Any]) -> bool:
    """Return whether the post states a status other than active; an absent one proves nothing."""
    status = stated_text(post, ("status",)).casefold()
    return bool(status) and status != "active"


def _places(posting: Posting) -> list[str]:
    """Return the country or region the post names; a global post names none."""
    return [
        name
        for field in ("country", "region")
        if (name := stated_text(posting.record, (field, "name")))
    ]


def _compensation(posting: Posting) -> str | None:
    pay = stated(posting.record, ("compensation",))
    if not isinstance(pay, Mapping):
        return None
    low = stated_text(pay, ("amount",))
    high = stated_text(pay, ("amount_max_range",))
    if not low:
        return None
    unit = stated_text(pay, ("unit",)).replace("_", " ")
    span = f"{low}-{high}" if high and high != low else low
    return " ".join(part for part in (span, stated_text(pay, ("currency",)), unit) if part)


TELUS_MAPPING = BoardDeclaration(
    platform="telus",
    title=("title",),
    # A site-level fact: this community site lists only TELUS Digital's own openings.
    employer=lambda _posting: "TELUS Digital",
    requisition_id=("id",),
    places=_places,
    # The qualifications are a separate field the post page shows below the description.
    description=(("description",), ("qualification_path",)),
    compensation=_compensation,
)
