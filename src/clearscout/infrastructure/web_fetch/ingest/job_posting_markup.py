"""Read `schema.org/JobPosting` markup a page publishes about itself.

The markup is the publisher's own statement of the title, dates, location, and
employer, and copying it infers nothing, so a posting read this way carries the
same provenance as one read from a board API.

Linked data is read first, and microdata only when a page declares none. RDFa
is not read.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import islice
from typing import Any

from clearscout.domain.countries import territory_by_prefix, territory_name

# A posting body longer than this is a page that embedded something else. The
# ingest ceiling is the same number for the same reason.
MAX_DESCRIPTION_CHARACTERS = 120_000

# One page can legitimately carry a handful of postings, as an index page does.
# Past that it is a listing dump, and the board reader is the right tool.
MAX_POSTINGS = 25

_JOB_POSTING = "jobposting"


@dataclass(frozen=True)
class BaseSalaryMarkup:
    """The `baseSalary` a posting states, as written and not yet interpreted."""

    currency: str = ""
    minimum: str = ""
    maximum: str = ""
    unit: str = ""


@dataclass(frozen=True)
class JobPostingMarkup:
    """What one `JobPosting` block states, copied rather than interpreted."""

    title: str
    description: str = ""
    # The node keys whose text the description carries, in the order it states them.
    description_paths: tuple[str, ...] = ()
    employer: str = ""
    # The first place stated; every other one a posting names is in `locations`.
    location: str = ""
    identifier: str = ""
    url: str = ""
    valid_through: str = ""
    date_posted: str = ""
    locations: tuple[str, ...] = ()
    # True only when every jobLocation entry yielded a place and nothing opens remote.
    locations_complete: bool = False
    employment_types: tuple[str, ...] = ()
    base_salary: BaseSalaryMarkup | None = None
    # JSON text of the whole node this was read from, description included.
    node: str = ""


class _ScriptBlocks(HTMLParser):
    """Collect the body of every `application/ld+json` script tag."""

    def __init__(self) -> None:
        """Start with no block open and nothing collected."""
        super().__init__()
        self.blocks: list[str] = []
        self._collecting = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open collection for a linked-data script and nothing else."""
        if tag.casefold() != "script":
            return
        declared = (dict(attrs).get("type") or "").casefold()
        self._collecting = declared.split(";")[0].strip() == "application/ld+json"

    def handle_data(self, data: str) -> None:
        """Keep the raw text of an open linked-data block."""
        if self._collecting:
            self.blocks.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close collection at the end of the script."""
        if tag.casefold() == "script":
            self._collecting = False


_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"})
# A sibling of the same kind ends these without a closing tag.
_IMPLIED_END_TAGS = frozenset({"li", "p", "dt", "dd", "option", "tr", "td", "th"})
_VALUE_ATTRIBUTES = {"meta": "content", "a": "href", "link": "href", "time": "datetime"}


class _MicrodataItems(HTMLParser):
    """Collect every top-level microdata item as the node shape linked data uses."""

    def __init__(self) -> None:
        """Start with no element open and nothing collected."""
        super().__init__()
        self.items: list[dict[str, Any]] = []
        self._open_tags: list[str] = []
        # Each open item or text property, with the open-tag depth it closes at.
        self._scopes: list[tuple[int, dict[str, Any]]] = []
        self._texts: list[tuple[int, dict[str, Any], str, list[str]]] = []

    @staticmethod
    def _state(item: dict[str, Any], names: str, value: Any) -> None:
        for name in names.split():
            if name in item:
                item[name] = [*_flat(item[name]), value]
            else:
                item[name] = value

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an item, or read a property from its attribute or its coming text."""
        tag = tag.casefold()
        if tag in _IMPLIED_END_TAGS and self._open_tags and self._open_tags[-1] == tag:
            self.handle_endtag(tag)
        stated = dict(attrs)
        names = stated.get("itemprop") or ""
        parent = self._scopes[-1][1] if self._scopes else None
        void = tag in _VOID_TAGS
        self._keep_markup(f"<{tag}>")
        if not void:
            self._open_tags.append(tag)
        if "itemscope" in stated:
            self._open_item(stated, names, parent, void=void)
        elif names and parent is not None:
            self._read_property(tag, stated, names, parent, void=void)

    def _open_item(
        self,
        stated: dict[str, str | None],
        names: str,
        parent: dict[str, Any] | None,
        *,
        void: bool,
    ) -> None:
        item: dict[str, Any] = {"@type": (stated.get("itemtype") or "").rsplit("/", 1)[-1]}
        if names and parent is not None:
            self._state(parent, names, item)
        else:
            self.items.append(item)
        if not void:
            self._scopes.append((len(self._open_tags), item))

    def _read_property(
        self,
        tag: str,
        stated: dict[str, str | None],
        names: str,
        parent: dict[str, Any],
        *,
        void: bool,
    ) -> None:
        attribute = "content" if "content" in stated else _VALUE_ATTRIBUTES.get(tag)
        if attribute is not None:
            self._state(parent, names, stated.get(attribute) or "")
        elif not void:
            self._texts.append((len(self._open_tags), parent, names, []))

    def _keep_markup(self, markup: str) -> None:
        # A description keeps its block structure, as a linked-data description does.
        for _, _, names, parts in self._texts:
            if names == "description":
                parts.append(markup)

    def handle_data(self, data: str) -> None:
        """Keep text for every property still open around it."""
        for _, _, _, parts in self._texts:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close the element and whatever item or text property it opened."""
        tag = tag.casefold()
        if tag not in self._open_tags:
            return
        # An unclosed child ends with its parent, as a browser would read it.
        depth = len(self._open_tags) - self._open_tags[::-1].index(tag)
        del self._open_tags[depth - 1 :]
        while self._texts and self._texts[-1][0] >= depth:
            _, item, names, parts = self._texts.pop()
            text = "".join(parts)
            self._state(item, names, text if names == "description" else " ".join(text.split()))
        self._keep_markup(f"</{tag}>")
        while self._scopes and self._scopes[-1][0] >= depth:
            self._scopes.pop()


_NAMED_KEYS = ("name", "@value", "value")
# A PropertyValue identifier names its scheme, such as "Requisition ID", and
# states the requisition itself in the value.
_IDENTIFIER_KEYS = ("value", "@value", "name")


def _text(value: Any, keys: tuple[str, ...] = _NAMED_KEYS) -> str:
    """Reduce a schema.org value to one plain string, or to nothing."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return _text(value[key], keys)
        return ""
    if isinstance(value, list):
        for item in value:
            found = _text(item, keys)
            if found:
                return found
    return ""


def _flat(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for entry in value for item in _flat(entry)]
    return [value]


def _one_settlement(locality: str, region: str) -> tuple[str, str]:
    """Return the locality and region, dropping a region that restates the locality."""
    # Only one name spelled twice is a restatement. A region that merely shares the
    # locality's words, New York over York or Kansas beside Kansas City, names the
    # wider place that separates two settlements of one name.
    if locality and locality.casefold().split() == region.casefold().split():
        return locality, ""
    return locality, region


def _address_place(address: Any) -> str:
    """Join the address parts a PostalAddress states; a street alone names no place."""
    if not isinstance(address, dict):
        return _text(address)
    locality, region = _one_settlement(
        _text(address.get("addressLocality")), _text(address.get("addressRegion"))
    )
    country = _text(address.get("addressCountry"))
    # A bare "IN" or "CA" here is a country; left as a code it reads as a US state.
    # A short mixed-case value is a truncated word ("To" for Tokyo), never a code.
    code = country.isupper() or len(country) > 2
    if not code and country and (named := territory_by_prefix(region)):
        # The same truncation shifts the country word into the region ("Japa", "To").
        return ", ".join(part for part in (locality, named) if part)
    parts = [locality, region, (territory_name(country) if code else "") or country]
    return ", ".join(part for part in parts if part)


# A remote requirement names a country, so "DE" there is Germany, while a node
# calling itself a state or a city means Delaware by the same two letters.
_SUBNATIONAL_TYPES = frozenset({"state", "city"})


def _applicant_area(node: Any) -> str:
    """Name one applicantLocationRequirements entry, expanding a country stated as a code."""
    area = _text(node)
    if isinstance(node, dict) and _types(node) & _SUBNATIONAL_TYPES:
        return area
    return territory_name(area) or area


def _entry_places(entry: Any) -> tuple[list[str], bool]:
    """Return the places one jobLocation entry states and whether it was fully read."""
    if not isinstance(entry, dict):
        text = _text(entry)
        return ([text], True) if text else ([], False)
    stated_address = "address" in entry
    addresses = _flat(entry["address"]) if stated_address else [entry]
    places = [place for place in map(_address_place, addresses) if place]
    if places and len(places) == len(addresses):
        return places, True
    name = _text(entry.get("name"))
    # A name beside an unreadable address may be a building, not the place itself.
    return ([*places, name], not stated_address) if name else (places, False)


def _stated_places(node: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    """Return every stated place and whether that list is complete.

    Publishers nest this inconsistently, and a remote posting often states a
    country and nothing else, so the parts are joined rather than required.
    """
    entries = _flat(node.get("jobLocation"))
    places: list[str] = []
    read_whole = bool(entries)
    for entry in entries:
        found, complete = _entry_places(entry)
        places.extend(found)
        read_whole = read_whole and complete
    # The type may be the bare token or a schema.org URL ending in it.
    remote = any(
        _text(kind).rsplit("/", 1)[-1].casefold() == "telecommute"
        for kind in _flat(node.get("jobLocationType"))
    )
    applicant_areas = [
        area
        for area in map(_applicant_area, _flat(node.get("applicantLocationRequirements")))
        if area
    ]
    if remote:
        places.extend([f"Remote, {area}" for area in applicant_areas] or ["Remote"])
    complete = read_whole and not remote and not node.get("applicantLocationRequirements")
    return tuple(dict.fromkeys(places)), complete


def _employment_types(node: dict[str, Any]) -> tuple[str, ...]:
    """Return each stated employment type, without the schema.org address some carry."""
    stated = (_text(kind) for kind in _flat(node.get("employmentType")))
    names = (kind.rsplit("/", 1)[-1] if kind.startswith("http") else kind for kind in stated)
    return tuple(dict.fromkeys(name for name in names if name))


def _base_salary(node: dict[str, Any]) -> BaseSalaryMarkup | None:
    """Copy a MonetaryAmount's parts; a bare amount states a figure and nothing else."""
    stated = next(iter(_flat(node.get("baseSalary"))), None)
    if not isinstance(stated, dict):
        amount = _text(stated)
        return BaseSalaryMarkup(maximum=amount) if amount else None
    value = stated.get("value")
    quantity = value if isinstance(value, dict) else {}
    single = _text(quantity.get("value")) if quantity else _text(value)
    return BaseSalaryMarkup(
        currency=_text(stated.get("currency")) or _text(quantity.get("currency")),
        minimum=_text(quantity.get("minValue")),
        maximum=_text(quantity.get("maxValue")) or single,
        unit=_text(quantity.get("unitText")) or _text(stated.get("unitText")),
    )


def _types(node: dict[str, Any]) -> set[str]:
    declared = node.get("@type", "")
    values = declared if isinstance(declared, list) else [declared]
    return {str(value).casefold() for value in values}


def _nodes(document: Any) -> list[dict[str, Any]]:
    """Walk the shapes publishers actually use: an object, a list, or a graph."""
    found: list[dict[str, Any]] = []
    if isinstance(document, list):
        for item in document:
            found.extend(_nodes(item))
        return found
    if not isinstance(document, dict):
        return found
    found.append(document)
    graph = document.get("@graph")
    if graph is not None:
        found.extend(_nodes(graph))
    return found


# The text sections a JobPosting states beside its description, in body order.
_SECTIONS = (
    ("responsibilities", "Responsibilities"),
    ("qualifications", "Qualifications"),
    ("skills", "Skills"),
    ("educationRequirements", "Education requirements"),
    ("experienceRequirements", "Experience requirements"),
)
# A credential node may state only its category, such as "bachelor degree".
_SECTION_KEYS = ("description", "name", "credentialCategory", "@value", "value")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def _texts(value: Any) -> list[str]:
    """Reduce a schema.org value to every string it states, keeping each list item."""
    return [text for item in _flat(value) if (text := _text(item, _SECTION_KEYS))]


def _containment_key(text: str) -> str:
    # Markup, list markers, and spacing differ between restatements of one sentence.
    plain = html.unescape(_TAG.sub(" ", text))
    return "".join(character for character in plain.casefold() if character.isalnum())


def _joined(sections: list[tuple[str, list[str]]]) -> str:
    """Join headed sections as paragraphs, or as block elements once any part is markup."""
    if not any(_TAG.search(text) for _, texts in sections for text in texts):
        return "\n\n".join(
            part for heading, texts in sections for part in (heading, *texts) if part
        )
    parts: list[str] = []
    for heading, texts in sections:
        if heading:
            parts.append(f"<h3>{heading}</h3>")
        parts.extend(
            text if _TAG.search(text) else f"<p>{html.escape(text, quote=False)}</p>"
            for text in texts
        )
    return "\n\n".join(parts)


def _body(node: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return the description joined with every section item it does not already state.

    The second value names the keys that contributed text, in body order.
    """
    descriptions = _texts(node.get("description"))
    sections = [("", descriptions)] if descriptions else []
    paths = ["description"] if descriptions else []
    stated = "".join(map(_containment_key, descriptions))
    for key, heading in _SECTIONS:
        fresh = [text for text in _texts(node.get(key)) if _containment_key(text) not in stated]
        if fresh:
            sections.append((heading, fresh))
            paths.append(key)
            stated += "".join(map(_containment_key, fresh))
    return _joined(sections)[:MAX_DESCRIPTION_CHARACTERS], tuple(paths)


def _employer(node: dict[str, Any], declared: dict[str, dict[str, Any]]) -> str:
    """Name the hiring organization, following a bare `@id` reference to its node."""
    organization = node.get("hiringOrganization")
    named = _text(organization)
    if named:
        return named
    for reference in _flat(organization):
        target = declared.get(str(reference.get("@id"))) if isinstance(reference, dict) else None
        if target is not None and (named := _text(target)):
            return named
    return ""


def _posting(node: dict[str, Any], declared: dict[str, dict[str, Any]]) -> JobPostingMarkup | None:
    title = _text(node.get("title")) or _text(node.get("name"))
    if not title:
        return None
    places, complete = _stated_places(node)
    description, description_paths = _body(node)
    return JobPostingMarkup(
        title=title,
        description=description,
        description_paths=description_paths,
        employer=_employer(node, declared),
        location=next(iter(places), ""),
        identifier=_text(node.get("identifier"), _IDENTIFIER_KEYS),
        url=_text(node.get("url")),
        valid_through=_text(node.get("validThrough")),
        date_posted=_text(node.get("datePosted")),
        locations=places,
        locations_complete=complete,
        employment_types=_employment_types(node),
        base_salary=_base_salary(node),
        node=json.dumps(node, ensure_ascii=False),
    )


# A legacy reference without its semicolon decodes too, so "Sales &times Marketing"
# would become "Sales x Marketing"; only a terminated reference is a reference here.
_ENTITY = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
# A JavaScript-style \' is not a JSON escape; an escaped backslash before it is kept.
_JS_APOSTROPHE = re.compile(r"(?<!\\)((?:\\\\)*)\\'")


def _decoded(value: Any) -> Any:
    """Decode entity references in every string a parsed document carries."""
    if isinstance(value, str):
        return _ENTITY.sub(lambda match: html.unescape(match.group()), value)
    if isinstance(value, list):
        return [_decoded(item) for item in value]
    if isinstance(value, dict):
        return {key: _decoded(item) for key, item in value.items()}
    return value


def _linked_data_nodes(body: str) -> Iterator[dict[str, Any]]:
    # HTMLParser reads a script as CDATA, so convert_charrefs never runs on a block.
    parser = _ScriptBlocks()
    parser.feed(body)
    for block in parser.blocks:
        # One broken or empty script on a page says nothing about the others.
        try:
            document = json.loads(block)
        except ValueError:
            try:
                document = json.loads(_JS_APOSTROPHE.sub(r"\1'", block))
            except ValueError:
                continue
        yield from _nodes(_decoded(document))


def _microdata_nodes(body: str) -> Iterator[dict[str, Any]]:
    microdata = _MicrodataItems()
    microdata.feed(body)
    yield from microdata.items


def microdata_date_posted(body: str) -> str:
    """Return the one datePosted the page's microdata items state, or "" for none or several."""
    stated = {
        text for item in _microdata_nodes(body) for text in _texts(item.get("datePosted")) if text
    }
    return stated.pop() if len(stated) == 1 else ""


def _declared(nodes: Iterator[dict[str, Any]]) -> tuple[JobPostingMarkup, ...]:
    every = list(nodes)
    by_id = {node["@id"]: node for node in every if isinstance(node.get("@id"), str)}
    postings = (_posting(node, by_id) for node in every if _JOB_POSTING in _types(node))
    return tuple(islice((posting for posting in postings if posting is not None), MAX_POSTINGS))


def job_postings(body: str) -> tuple[JobPostingMarkup, ...]:
    """Return every `JobPosting` the page declares, in document order; no markup is ordinary."""
    return _declared(_linked_data_nodes(body)) or _declared(_microdata_nodes(body))
