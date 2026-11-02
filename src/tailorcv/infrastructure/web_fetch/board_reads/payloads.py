"""Required fields, row identities, and page markup in a board's answers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting

if TYPE_CHECKING:
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport


MAX_API_RESPONSE_BYTES = 32_000_000


def required_list_field(payload: Any, name: str, platform: str) -> list[Any]:
    """Return a complete-feed list or refuse an envelope that could mimic empty."""
    value = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(value, list):
        raise ValueError(f"{platform} feed did not return a {name} list")
    return value


def required_feed_root(
    text: str, tag: str, platform: str, url: str, *, namespace: str = ""
) -> Element:
    """Return a feed's parsed root, or refuse a body that is not that platform's XML feed.

    ``namespace`` is a URI the platform's feed declares even when it holds no items.
    """
    root: Element | None
    try:
        root = fromstring(text)
    except (ParseError, DefusedXmlException):
        root = None
    if root is None or root.tag != tag or namespace not in text:
        raise ValueError(f"{platform} did not return an XML feed at {url}")
    return root


def row_id(row: Any, key: str = "id") -> str:
    """Return a list row's stated job id, or empty when it states none."""
    return str(row.get(key) or "").strip() if isinstance(row, dict) else ""


def distinct_by_id(rows: list[Any], key: str = "id", seen: set[str] | None = None) -> list[Any]:
    """Keep the first row for each job id not in ``seen``, and every row that states none.

    A given ``seen`` gains the kept ids, so successive pages stay distinct.
    """
    seen = set() if seen is None else seen
    kept: list[Any] = []
    for row in rows:
        job_id = row_id(row, key)
        if job_id in seen:
            continue
        if job_id:
            seen.add(job_id)
        kept.append(row)
    return kept


def page_markup(page: str) -> str:
    """Remove the fetcher's final-URL prefix before parsing publisher markup."""
    first, separator, remainder = page.partition("\n")
    return remainder if separator and first.startswith(("http://", "https://")) else page


def page_posting_detail(
    fetcher: BoardTransport,
) -> Callable[[dict[str, Any]], Mapping[str, Any] | None]:
    """Return a detail reader declaring each row's posting page JobPosting markup."""
    return lambda row: page_posting(
        page_markup(fetcher.get_text(str(row["url"]), max_bytes=MAX_API_RESPONSE_BYTES))
    )
