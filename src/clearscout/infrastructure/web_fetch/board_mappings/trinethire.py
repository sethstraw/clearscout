"""TriNet Hire's rendered job table joined with each posting page's declared or rendered body."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import plain_description
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    page_posting,
    page_posting_declaration,
)
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks

_OPENING = re.compile(
    r"""href\s*=\s*["'](?:https?://app\.trinethire\.com)?/companies/"""
    r"""(?P<company>\d+)-(?P<slug>[A-Za-z0-9-]+)/jobs/(?P<job>\d+)(?P<title>-[A-Za-z0-9-]*)?"""
    r"""(?=[?#"'])""",
    re.IGNORECASE,
)
_PAGINATION = re.compile(r"""class\s*=\s*["'][^"']*\bpagination\b""", re.IGNORECASE)
_NEXT = re.compile(r"""\brel\s*=\s*["']?next\b""", re.IGNORECASE)
_EMPLOYMENT = re.compile(
    r"\b(?:full[- ]?time|part[- ]?time|contract(?:or)?|temporary|seasonal|intern(?:ship)?|"
    r"per diem|freelance)\b",
    re.IGNORECASE,
)


def trinethire_board_url(org: str) -> str:
    """Return the server-rendered table listing every opening of one TriNet Hire company."""
    return f"https://app.trinethire.com/companies/{org}/jobs"


def trinethire_openings(page: str, company: str) -> tuple[list[dict[str, str]], int] | None:
    """Return the job-table openings of this numeric company and how many rows failed to parse.

    None means the page renders no job table at all.
    """
    rows = html_blocks(page, "tr", class_name="job")
    if not rows and not html_blocks(page, "tr", class_name="no-job"):
        return None
    openings = []
    for row in rows:
        link = _OPENING.search(row.inner)
        if link is not None and link.group("company") == company:
            path = f"{link.group('company')}-{link.group('slug')}"
            openings.append(
                {
                    "id": link.group("job"),
                    "url": f"{trinethire_board_url(path)}/{link.group('job')}"
                    f"{link.group('title') or ''}",
                }
            )
    return openings, len(rows) - len(openings)


def trinethire_paged(page: str, company: str) -> bool:
    """Return whether the board page carries any sign of a further page of openings."""
    return (
        _PAGINATION.search(page) is not None
        or _NEXT.search(page) is not None
        or re.search(
            rf"""href\s*=\s*["'][^"']*/companies/{re.escape(company)}-[A-Za-z0-9-]+/jobs/?"""
            r"""\?(?:[^"'#]*&(?:amp;)?)?page=""",
            page,
            re.IGNORECASE,
        )
        is not None
    )


def _one(page: str, tag: str, class_name: str) -> str:
    blocks = html_blocks(page, tag, class_name=class_name)
    return blocks[0].inner if len(blocks) == 1 else ""


def trinethire_page_posting(page: str) -> Mapping[str, Any] | None:
    """Return the declared posting, else the rendered title, meta, and body; None without both."""
    declared = page_posting(page)
    if declared is not None:
        return declared
    title = plain_description(_one(page, "h3", "job-name"))
    bodies = [
        block
        for block in html_blocks(page, "div", class_name="job-descr")
        if block.has_class("content")
    ]
    description = plain_description(bodies[0].inner) if len(bodies) == 1 else ""
    if not title or not description:
        return None
    meta = [
        plain_description(item.inner) for item in html_blocks(_one(page, "ul", "job-meta"), "li")
    ]
    return {
        "title": title,
        "description": description,
        "places": [item for item in meta if item and not _EMPLOYMENT.search(item)],
        "places_complete": False,
        "employment_types": [item for item in meta if item and _EMPLOYMENT.search(item)],
    }


TRINETHIRE_MAPPING = page_posting_declaration("trinethire")
