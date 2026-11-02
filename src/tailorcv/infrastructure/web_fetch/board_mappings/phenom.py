"""Phenom's posting record, as its career-site search widget states it."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    stated,
    stated_text,
)


def _places(posting: Posting) -> Any:
    record = posting.record
    return stated(record, ("cityStateCountry",)) or stated(record, ("location",))


def _requisition_id(posting: Posting) -> str:
    record = posting.record
    return stated_text(record, ("jobId",)) or stated_text(record, ("jobSeqNo",))


def _url(posting: Posting) -> str:
    """Prefer the stated application address, else fill the publisher job route."""
    explicit = stated_text(posting.record, ("applyUrl",))
    if explicit:
        return explicit
    route = posting.groups.get("job_route", "")
    base = posting.groups.get("route_base", "")
    try:
        parsed = urlsplit(base)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+", route) or route.startswith("/"):
        return ""
    if any(segment in {".", "..", ""} for segment in route.split("/")):
        return ""
    fields = re.findall(r":([A-Za-z][A-Za-z0-9_]*)", route)
    if not set(fields) & {"jobSeqNo", "jobId", "reqId"}:
        return ""
    values = {field: stated_text(posting.record, (field,)) for field in fields}
    if any(not value or value in {".", ".."} for value in values.values()):
        return ""
    filled = re.sub(
        r":([A-Za-z][A-Za-z0-9_]*)", lambda match: quote(values[match[1]], safe=""), route
    )
    if ":" in filled:
        return ""
    return urljoin(base, filled)


PHENOM_MAPPING = BoardDeclaration(
    platform="phenom",
    title=("title",),
    url=_url,
    requisition_id=_requisition_id,
    places=_places,
)
