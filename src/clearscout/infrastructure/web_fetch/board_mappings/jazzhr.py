"""JazzHR's board links joined with declared or rendered posting details."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from clearscout.infrastructure.web_fetch.board_mappings.mapper import (
    Posting,
    plain_description,
    stated,
)
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import (
    page_posting,
    page_posting_declaration,
)
from clearscout.infrastructure.web_fetch.ingest.html_blocks import HtmlBlock, html_blocks

# An opening's link, absolute on any tenant host or relative to the board page.
_OPENING = re.compile(
    r"""href\s*=\s*["'](?:https?://(?P<host>[A-Za-z0-9.-]+))?"""
    r"""/apply/(?P<job>[A-Za-z0-9]{10})/(?P<slug>[^"'/?#\s]+)""",
    re.IGNORECASE,
)


def jazzhr_board_url(tenant: str) -> str:
    """Return the server-rendered page listing every opening of one JazzHR tenant."""
    return f"https://{tenant}.applytojob.com/apply"


def jazzhr_openings(page: str, tenant: str) -> list[dict[str, str]]:
    """Return one row per opening link the board page carries for this tenant."""
    own_host = f"{tenant}.applytojob.com".casefold()
    return [
        {
            "id": link.group("job"),
            "url": f"{jazzhr_board_url(tenant)}/{link.group('job')}/{link.group('slug')}",
        }
        for link in _OPENING.finditer(page)
        if link.group("host") is None or link.group("host").casefold() == own_host
    ]


class _JazzhrFieldIds(HTMLParser):
    """Retain field starts so incomplete duplicate markup cannot appear unique."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.counts: Counter[str] = Counter()
        self.classes: Counter[str] = Counter()
        self.tags: Counter[str] = Counter()
        self.labels: Counter[str] = Counter()
        self.inputs: list[dict[str, str]] = []
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        stated_attrs = {key: value or "" for key, value in attrs}
        self.tags[tag] += 1
        self.classes.update(stated_attrs.get("class", "").split())
        for key, value in attrs:
            if key == "id" and value:
                self.counts[value] += 1
            if key == "title" and value:
                self.labels[value] += 1
        if tag == "input" and stated_attrs.get("id") == "resumator-job-value":
            self.inputs.append(stated_attrs)


def _jazzhr_rendered_field(page: str, tag: str, identifier: str, field_ids: _JazzhrFieldIds) -> str:
    blocks = [block for block in html_blocks(page, tag) if block.attrs.get("id") == identifier]
    if len(blocks) != 1 or field_ids.counts[identifier] != 1:
        return ""
    return plain_description(blocks[0].inner)


def _jazzhr_unique_block(
    page: str,
    tag: str,
    field_ids: _JazzhrFieldIds,
    *,
    identifier: str = "",
    class_name: str = "",
) -> HtmlBlock | None:
    blocks = [
        block
        for block in html_blocks(page, tag, class_name=class_name or None)
        if not identifier or block.attrs.get("id") == identifier
    ]
    count = field_ids.counts[identifier] if identifier else field_ids.classes[class_name]
    return blocks[0] if len(blocks) == count == 1 else None


def _jazzhr_responsive_sections(
    page: str, field_ids: _JazzhrFieldIds
) -> tuple[HtmlBlock, HtmlBlock, HtmlBlock] | None:
    header = _jazzhr_unique_block(page, "div", field_ids, class_name="job-header")
    body = _jazzhr_unique_block(page, "div", field_ids, class_name="page-body")
    if header is None or body is None or not body.has_class("job-details"):
        return None
    description = _jazzhr_unique_block(body.inner, "div", field_ids, identifier="job-description")
    application = _jazzhr_unique_block(
        body.inner, "div", field_ids, identifier="resumator-application-form"
    )
    if description is None or application is None:
        return None
    if (
        _JazzhrFieldIds(header.inner).classes["page-body"]
        or _JazzhrFieldIds(body.inner).classes["job-header"]
    ):
        return None
    description_ids = _JazzhrFieldIds(description.inner)
    if (
        description_ids.counts["resumator-application-form"]
        or _JazzhrFieldIds(application.inner).counts["job-description"]
        or description_ids.tags["form"]
        or description_ids.tags["nav"]
    ):
        return None
    return header, description, application


def _jazzhr_labelled_field(page: str, label: str, *, identifier: str = "") -> str:
    fields = _JazzhrFieldIds(page)
    blocks = [block for block in html_blocks(page, "div") if block.attrs.get("title") == label]
    if len(blocks) != 1 or fields.labels[label] != 1:
        return ""
    if identifier and (blocks[0].attrs.get("id") != identifier or fields.counts[identifier] != 1):
        return ""
    value = plain_description(blocks[0].inner)
    return re.sub(rf"^{re.escape(label)}\s*:\s*", "", value, flags=re.IGNORECASE).strip()


def _jazzhr_responsive_posting(page: str, field_ids: _JazzhrFieldIds) -> Mapping[str, Any] | None:
    sections = _jazzhr_responsive_sections(page, field_ids)
    if sections is None:
        return None
    header, description, application = sections
    header_fields = _JazzhrFieldIds(header.inner)
    container = _jazzhr_unique_block(header.inner, "div", header_fields, class_name="container")
    if container is None or header_fields.tags["h2"] != 1:
        return None
    titles = html_blocks(container.inner, "h2")
    inputs = _JazzhrFieldIds(application.inner).inputs
    if len(titles) != 1 or len(inputs) != 1 or field_ids.counts["resumator-job-value"] != 1:
        return None
    identity = inputs[0]
    if identity.get("type") != "hidden" or not identity.get("value", "").strip():
        return None
    title = plain_description(titles[0].inner)
    body = plain_description(description.inner)
    if not title or not body:
        return None
    attributes = _jazzhr_unique_block(
        header.inner, "div", header_fields, class_name="job-attributes-container"
    )
    location = _jazzhr_labelled_field(attributes.inner, "Location") if attributes else ""
    employment = (
        _jazzhr_labelled_field(attributes.inner, "Type", identifier="resumator-job-employment")
        if attributes
        else ""
    )
    return {
        "title": title,
        "description": body,
        "application_id": identity["value"],
        "places": [location] if location else [],
        "places_complete": False,
        "employment_types": [employment] if employment else [],
    }


def _jazzhr_title(posting: Posting) -> str:
    identity = stated(posting.record, ("detail", "application_id"))
    if identity is not None and identity != stated(posting.record, ("row", "id")):
        return ""
    title = stated(posting.record, ("detail", "title"))
    return title if isinstance(title, str) else ""


def jazzhr_page_posting(page: str) -> Mapping[str, Any] | None:
    """Return the declared posting or an unambiguous rendered JazzHR job detail."""
    declared = page_posting(page)
    if declared is not None:
        return declared
    field_ids = _JazzhrFieldIds(page)
    title = _jazzhr_rendered_field(page, "div", "resumator-job-title", field_ids)
    description = _jazzhr_rendered_field(page, "div", "resumator-job-description", field_ids)
    if not title or not description:
        return _jazzhr_responsive_posting(page, field_ids)
    location = _jazzhr_rendered_field(page, "h2", "resumator-job-location", field_ids)
    employment = _jazzhr_rendered_field(page, "h2", "resumator-job-employment", field_ids)
    location = re.sub(r"^Location\s*:\s*", "", location, flags=re.IGNORECASE).strip()
    employment = re.sub(r"^Type\s*:\s*", "", employment, flags=re.IGNORECASE).strip()
    return {
        "title": title,
        "description": description,
        "places": [location] if location else [],
        "places_complete": False,
        "employment_types": [employment] if employment else [],
    }


JAZZHR_MAPPING = page_posting_declaration("jazzhr", title=_jazzhr_title)
