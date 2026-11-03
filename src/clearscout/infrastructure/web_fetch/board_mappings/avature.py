"""Avature's posting page: the fields it renders, titled by its JobPosting or its og:title."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    og_titles,
    page_posting_declaration,
)
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from clearscout.infrastructure.web_fetch.listing_facts import extended_locations

_FIELD = "article__content__view__field"
_LOCALE = re.compile(r"[a-z]{2}_[A-Z]{2}")
_PORTAL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Portals name their fields; these are the labels that state a place or a schedule.
_PLACE_LABELS = ("location", "locations", "city")
_COUNTRY_LABELS = ("country",)
_TYPE_LABELS = ("full-time/part-time", "employment type", "job type", "schedule", "time type")


def avature_portal(url: str, url_path: str = "") -> tuple[str, str] | None:
    """Return the host and portal (with any stated locale) an Avature address names.

    ``url_path`` is the portal path the page's markup declares; it wins when the
    address does not lead with it.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or "@" in parts.netloc:
        return None
    segments = parts.path.strip("/").split("/")
    localized = _LOCALE.fullmatch(segments[0]) is not None and len(segments) > 1
    stated = segments[:2] if localized else segments[:1]
    host = parts.netloc.casefold()
    if url_path and stated[-1].casefold() != url_path.casefold():
        return host, url_path
    if _PORTAL_NAME.fullmatch(stated[-1]) is None:
        return None
    return host, "/".join(stated)


def avature_sitemap_url(host: str, portal: str) -> str:
    """Return the sitemap index one Avature portal publishes in its robots.txt."""
    return f"https://{host}/{portal}/sitemap_index.xml"


def _path_locale(address: str) -> str:
    """Return the locale an address's first path segment states, or ""."""
    first = urlsplit(address).path.strip("/").split("/")[0]
    return first if _LOCALE.fullmatch(first) else ""


def _chosen_locale(portal: str, locales: list[str]) -> str:
    """Return the portal's own locale, else en_US when listed, else the first one listed."""
    if "/" in portal:
        return portal.split("/", maxsplit=1)[0]
    stated = [locale for locale in locales if locale]
    return "en_US" if "en_US" in stated else next(iter(stated), "")


def avature_sitemaps(children: list[str], portal: str) -> list[str]:
    """Return the child sitemaps an index lists for the one locale this portal is read in."""
    locale = _chosen_locale(portal, [_path_locale(child) for child in children])
    return [child for child in children if _path_locale(child) in {"", locale}]


def avature_openings(pages: list[str], host: str, portal: str) -> list[dict[str, str]]:
    """Return one row per JobDetail page the sitemap lists for this portal.

    A portal named without a locale keeps its pages in one locale, chosen as
    ``avature_sitemaps`` chooses, so each posting is kept once.
    """
    locale = "" if "/" in portal else rf"(?:(?P<locale>{_LOCALE.pattern})/)?"
    detail = re.compile(
        rf"https?://{re.escape(host)}/{locale}{re.escape(portal)}"
        r"/JobDetail/[^/?#]+/(?P<job>\d+)/?",
        re.IGNORECASE,
    )
    matches = [match for page in pages if (match := detail.fullmatch(page)) is not None]
    chosen = _chosen_locale(portal, [_path_locale(match.string) for match in matches])
    return [
        {"id": match.group("job"), "url": match.string}
        for match in matches
        if _path_locale(match.string) in {"", chosen}
    ]


def _text(markup: str) -> str:
    return " ".join(strip_tags(markup).split())


def _fields(page: str) -> list[tuple[str, str]]:
    """Return each rendered field's lowercased label ("" when it has none) and value markup."""
    fields: list[tuple[str, str]] = []
    for field in html_blocks(page, "div", class_name=_FIELD):
        label = next(iter(html_blocks(field.inner, "div", class_name=f"{_FIELD}__label")), None)
        value = next(iter(html_blocks(field.inner, "div", class_name=f"{_FIELD}__value")), None)
        if value is None:
            continue
        # A locations field can state its label inline instead of in a label element.
        name = "locations" if label is None and field.has_class("field--locations") else ""
        fields.append((_text(label.inner).casefold() if label else name, value.inner.strip()))
    # Other portal templates render each field as a paragraph of title and value spans.
    for paragraph in html_blocks(page, "p", class_name="paragraph"):
        spans = {
            block.attrs["data-map"]: block.inner
            for block in html_blocks(paragraph.inner, "span")
            if block.attrs.get("data-map") in {"item-title", "item-value"}
        }
        if {"item-title", "item-value"} <= spans.keys():
            title = _text(spans["item-title"]).removesuffix(":").strip().casefold()
            fields.append((title, spans["item-value"].strip()))
    return fields


def _labelled(fields: list[tuple[str, str]], labels: tuple[str, ...]) -> list[str]:
    return [text for label, value in fields if label in labels and (text := _text(value))]


def _places(fields: list[tuple[str, str]]) -> list[str]:
    """Return each place field's value, or the structured places it lists instead."""
    places: list[str] = []
    for label, value in fields:
        if label not in _PLACE_LABELS:
            continue
        listed = [
            ", ".join(
                text
                for item in html_blocks(group.inner, "span", class_name="MultipleDataSetFieldValue")
                if (text := _text(item.inner))
            )
            for group in html_blocks(value, "ul", class_name="MultipleDataSetFields")
        ]
        if any(listed):
            places.extend(place for place in listed if place)
        elif text := _text(value):
            places.append(text)
    return places


def _description(page: str, fields: list[tuple[str, str]]) -> str:
    """Return the unlabelled fields, or the rich-text body a portal template renders instead."""
    unlabelled = [value for label, value in fields if not label and _text(value)]
    if unlabelled:
        return "\n".join(unlabelled)
    return "\n".join(
        block.inner
        for block in html_blocks(page, "div")
        if block.attrs.get("itemprop") == "description" and _text(block.inner)
    )


def avature_page_posting(page: str) -> Mapping[str, Any] | None:
    """Return the posting a page renders, or None when it names no title.

    The title is the declared JobPosting's, else the page's og:title.
    """
    declared = next(iter(job_postings(page)), None)
    title = declared.title if declared is not None else next(iter(og_titles(page) or []), "")
    if not title.strip():
        return None
    fields = _fields(page)
    places = extended_locations(_places(fields), _labelled(fields, _COUNTRY_LABELS))
    types = _labelled(fields, _TYPE_LABELS)
    description = _description(page, fields)
    if declared is None:
        return {
            "title": title.strip(),
            "description": description,
            "employer": "",
            "places": list(places),
            "places_complete": False,
            "employment_types": types,
        }
    return {
        "title": declared.title,
        "description": description or declared.description,
        "employer": declared.employer,
        "places": list(places or declared.locations),
        "places_complete": False,
        "employment_types": types or list(declared.employment_types),
    }


AVATURE_MAPPING = page_posting_declaration("avature")
