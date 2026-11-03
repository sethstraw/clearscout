"""Read supported ATS title slugs without inventing posting text."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.platforms.workday import WORKDAY, workday_posting_path

_GREENHOUSE_TITLE_SLUG = re.compile(r"/jobs/\d+/([^/?#]+)")


def title_from_url_slug(url: str) -> str | None:
    """Return a display title read from a supported ATS URL slug, or None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    workday = host_match(WORKDAY, url)
    slug = (
        _workday_title_slug(workday) if workday else _title_slug(parsed.netloc.lower(), parsed.path)
    )
    if not slug:
        return None
    decoded = unquote(slug).replace("-", " ").replace("_", " ").strip()
    if not decoded or not re.search(r"[A-Za-z]", decoded):
        return None
    return re.sub(r"\s+", " ", decoded)


def _title_slug(host: str, path: str) -> str | None:
    """Return the title segment carried by a supported board path."""
    slug: str | None = None
    if host.endswith("greenhouse.io"):
        match = _GREENHOUSE_TITLE_SLUG.search(path)
        if match:
            slug = match.group(1)
    elif host.endswith(".icims.com"):
        match = re.search(r"/jobs/\d+/([^/]+)/job", path)
        if match:
            slug = match.group(1)
    return slug


def _workday_title_slug(match: re.Match[str]) -> str | None:
    """Return Workday's posting segment without its requisition suffix."""
    parts = workday_posting_path(match).split("/")
    if len(parts) < 2:
        return None
    last = parts[-1]
    return last.rsplit("_", 1)[0] if "_" in last else last
