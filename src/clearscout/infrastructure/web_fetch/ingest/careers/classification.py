"""Extract publisher links and classify career pages from structural evidence."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import SplitResult, parse_qsl, urlencode, urljoin, urlsplit

from clearscout.application.discovery.titles import (
    names_a_leadership_rung,
    states_a_specific_occupation,
)
from clearscout.domain.records import PageClassification
from clearscout.infrastructure.web_fetch.ingest.careers.parser import (
    CHROME_CONTEXTS,
    LabelSource,
    PublisherPageParser,
    RawLink,
    StructuralContext,
    collapse_whitespace,
    same_origin,
)

__all__ = ["LinkRelation", "PageEvidence", "PublisherLink", "inspect_publisher_page"]

_CONTENT_CONTEXTS = frozenset({"article", "card", "list", "main", "section", "table"})
_TRACKING_KEYS = frozenset(
    {"_ga", "_gl", "dclid", "fbclid", "gclid", "gbraid", "mc_cid", "mc_eid", "msclkid", "wbraid"}
)
_ERROR_WORDS = re.compile(
    r"\b(?:access denied|captcha|error|forbidden|not found|robot|unavailable)\b", re.IGNORECASE
)
_OPAQUE_SEGMENT = re.compile(r"[A-Za-z0-9_]{6,}|\d{3,}")
_APPLICATION_WORDS = re.compile(
    r"\b(?:apply|application|resume|résumé|curriculum vitae|cv)\b", re.IGNORECASE
)
_POSTING_SECTION_WORDS = re.compile(
    r"\b(?:about the role|about you|benefits|compensation|duties (?:and|&) tasks|"
    r"how to apply|job summary|qualifications|requirements|responsibilities|role overview|"
    r"what you['\u2019]ll (?:bring|do)|what we['\u2019]re looking for|who you are)\b",
    re.IGNORECASE,
)
_EMPLOYMENT_TYPE = re.compile(
    r"\b(?:full[- ]time|part[- ]time|contract|temporary|internship)\b", re.IGNORECASE
)
_TITLE_SEPARATOR = re.compile(r" \| | - ")
# A posting's own body beside a rail of other postings runs at least this long unbroken.
_BODY_BESIDE_LINKS_CHARACTERS = 1_000


class LinkRelation(StrEnum):
    """A link relationship observed without assigning the target a page role."""

    SAME_ORIGIN = "same-origin"
    EXTERNAL_HANDOFF = "external-handoff"
    PUBLISHER_CONTENT = "publisher-content"
    NAVIGATION_CHROME = "navigation-chrome"
    SUBSTANTIVE_LABEL = "substantive-label"
    REPEATED_TARGET_SHAPE = "repeated-target-shape"


@dataclass(frozen=True)
class PublisherLink:
    """One publisher-declared link and the evidence available at its source."""

    fetch_url: str
    label: str
    label_source: LabelSource
    source_url: str
    structural_context: StructuralContext
    target_shape: str
    cluster_size: int
    relations: tuple[LinkRelation, ...]
    collection_pagination: bool = False


@dataclass(frozen=True)
class PageEvidence:
    """Pure page classification plus inspectable evidence for its conclusion."""

    classification: PageClassification
    title: str
    links: tuple[PublisherLink, ...]
    candidate_links: tuple[PublisherLink, ...]
    evidence: tuple[str, ...]
    # The page's rel="canonical" address, resolved against the page; empty when it names none.
    canonical_url: str = ""
    # Visible text outside header, navigation, and footer chrome.
    text_outside_chrome: str = ""
    # All text a reader sees, chrome included; scripts, comments, and hidden elements are not.
    visible_text: str = ""
    # The document's <title>, which on an employer's own site often ends in the site's name.
    page_title: str = ""
    posting_body_markup: str = ""


@dataclass(frozen=True)
class _PageSignals:
    title: str
    page_title: str
    content: str
    # The content, or on a page with no main or article frame, its unlinked text outside chrome.
    body_text: str
    all_text: str
    links: tuple[PublisherLink, ...]
    candidates: tuple[PublisherLink, ...]
    section_count: int
    # The retained run of text outside chrome that no clustered link interrupts.
    body_stretch: str
    body_stretch_chunks: frozenset[int]
    # A posting's own address carries an identifier token; a team or department hub's does not.
    path_carries_identifier: bool
    # The title came from the page's one heading marked as its job title.
    title_marked: bool = False
    posting_body_markup: str = ""
    multiple_role_bodies: bool = False
    # The title is the leading content text that the referring link's label repeats.
    title_referred: bool = False
    # The title is the h1 of a header promoted to the page's content frame.
    title_framed: bool = False


def inspect_publisher_page(
    source_url: str, page: str, *, referring_link: PublisherLink | None = None
) -> PageEvidence:
    """Classify publisher HTML from content and structure, never a vendor guess."""
    parser = PublisherPageParser(page)
    parser.feed(page)
    header = _content_header(parser)
    if header is not None:
        parser = PublisherPageParser(page, content_header=header)
        parser.feed(page)
    signals = _page_signals(source_url, parser, referring_link)
    evidence = _classification_evidence(parser, signals)
    classification = _classify(parser, signals, referring_link)
    if classification is PageClassification.ERROR_OR_CHALLENGE:
        evidence.append("short page carries an error or challenge signal")
    elif classification is PageClassification.JS_SHELL:
        evidence.append("page has scripts but no publisher-readable content")
    elif classification is PageClassification.NAVIGATION:
        evidence.append(_navigation_evidence(signals.links))
    elif (
        classification is PageClassification.POSTING
        and not parser.has_jsonld_posting
        and _clustered_links(signals)
    ):
        evidence.append("a role's own body stands apart from its repeated links")

    posting_title = signals.title if classification is PageClassification.POSTING else ""
    if not posting_title and classification is PageClassification.POSTING and referring_link:
        posting_title = referring_link.label
    return PageEvidence(
        classification,
        posting_title,
        signals.links,
        signals.candidates,
        tuple(evidence),
        urljoin(source_url, parser.canonical) if parser.canonical else "",
        " ".join(text for text, in_chrome in parser.text_chunks if not in_chrome),
        " ".join(parser.visible_parts),
        signals.page_title,
        signals.posting_body_markup if classification is PageClassification.POSTING else "",
    )


def _content_header(parser: PublisherPageParser) -> int | None:
    """Return the header that holds the page's only <h1> and most of its visible text."""
    if parser.content_parts or len(parser.h1_headings) != 1 or len(parser.h1_starts) != 1:
        return None
    sizes = [
        0 if index in parser.title_chunks else len(text)
        for index, (text, _) in enumerate(parser.text_chunks)
    ]
    heading = parser.h1_starts[0]
    return next(
        (
            index
            for index, (start, end) in enumerate(parser.header_spans)
            if start <= heading < end
            and 2 * sum(sizes[start:end]) > sum(sizes)
            and _header_frames_one_role(parser, start, end)
        ),
        None,
    )


def _header_frames_one_role(parser: PublisherPageParser, start: int, end: int) -> bool:
    """Return whether a header's own heading and links describe one role, not a site."""
    heading = collapse_whitespace(" ".join(parser.h1_headings[0]))
    body = " ".join(text for text, _ in parser.text_chunks[start:end])
    opening = " ".join(re.split(r"(?<=[.!?])\s", body.replace(heading, "", 1).strip())[:2])
    # The header's own text would outweigh an error notice, so its opening and title decide.
    if _ERROR_WORDS.search(f"{heading} {opening} {' '.join(parser.title_parts)}"):
        return False
    sections = _role_sections(body)
    outside = [raw for raw in parser.links if not start <= raw.text_span[0] < end]
    roles = Counter(
        _parent_shape(raw.href)
        for raw in outside
        if states_a_specific_occupation(label := raw.visible_label or raw.fallback_label)
        or names_a_leadership_rung(label)
    )
    applications = {
        raw.href
        for raw in outside
        if _APPLICATION_WORDS.search(raw.visible_label or raw.fallback_label)
    }
    if len(applications) >= 2 or any(count >= 2 for count in roles.values()):
        return False
    if not sections:
        return False
    if states_a_specific_occupation(heading) or names_a_leadership_rung(heading):
        return True
    # A heading that names no role needs the header's own sections and application link.
    return len(sections) >= 2 and any(
        start <= raw.text_span[0] < end
        and _APPLICATION_WORDS.search(raw.visible_label or raw.fallback_label)
        for raw in parser.links
    )


def _role_sections(text: str) -> set[str]:
    """Return the posting section words in text that only a role's own page names."""
    return {word.casefold() for word in _POSTING_SECTION_WORDS.findall(text)} - {
        "benefits",
        "compensation",
        "how to apply",
    }


def _classification_evidence(parser: PublisherPageParser, signals: _PageSignals) -> list[str]:
    evidence: list[str] = []
    if parser.invalid_jsonld:
        evidence.append("publisher structured data is not valid JSON")
    if parser.has_jsonld_posting:
        evidence.append("publisher declared JobPosting structured data")
    if signals.title_marked:
        evidence.append("page marks one heading as its job title")
    elif signals.title:
        evidence.append("page has a primary heading")
    if len(signals.body_text) >= 240:
        evidence.append("page body contains substantive text")
    if signals.section_count >= 2:
        evidence.append("main content has multiple sections")
    if _POSTING_SECTION_WORDS.search(signals.body_text):
        evidence.append("content names a posting section")
    if parser.has_form or parser.has_file_input:
        evidence.append("page contains an application form")
    if _clustered_links(signals):
        evidence.append("repeated substantive links share a target shape")
    return evidence


def _is_challenge_page(parser: PublisherPageParser, signals: _PageSignals) -> bool:
    error_text = f"{signals.page_title} {signals.title} {signals.all_text}"
    collection_text = collapse_whitespace(
        " ".join(text for text, in_chrome in parser.text_chunks if not in_chrome)
    )
    password_wall = parser.has_primary_password_input or (
        parser.has_password_input and len(collection_text) < 240
    )
    challenged = _ERROR_WORDS.search(error_text) or password_wall
    readable_collection = (
        _clustered_links(signals) and len(collection_text) >= 240 and not password_wall
    )
    return bool(challenged and len(signals.content) < 240 and not readable_collection)


def _classify(
    parser: PublisherPageParser,
    signals: _PageSignals,
    referring_link: PublisherLink | None,
) -> PageClassification:
    if _is_challenge_page(parser, signals):
        return PageClassification.ERROR_OR_CHALLENGE
    if signals.multiple_role_bodies:
        return (
            PageClassification.COLLECTION
            if _clustered_links(signals)
            else PageClassification.AMBIGUOUS
        )
    if parser.has_jsonld_posting:
        return PageClassification.POSTING
    corroborated = _corroborates_posting(parser, signals, referring_link)
    clustered = _clustered_links(signals)
    # Role links promoted from outside a content frame never outweigh a corroborated posting.
    framed = any(link.structural_context in _CONTENT_CONTEXTS for link in clustered)
    if clustered and not (
        corroborated
        and (not framed or signals.posting_body_markup or _body_stands_apart(signals, parser))
    ):
        return PageClassification.COLLECTION
    if corroborated:
        return PageClassification.POSTING
    if parser.has_file_input or (parser.has_form and _APPLICATION_WORDS.search(signals.all_text)):
        return PageClassification.APPLICATION_CHANNEL
    if parser.script_count > 0 and len(signals.all_text) < 80 and not parser.links:
        return PageClassification.JS_SHELL
    if _is_navigation(signals.links):
        return PageClassification.NAVIGATION
    return PageClassification.AMBIGUOUS


def _page_signals(
    source_url: str, parser: PublisherPageParser, referring_link: PublisherLink | None
) -> _PageSignals:
    next_page = parser._pagination_link(source_url)
    links = _publisher_links(source_url, parser.links + ([next_page] if next_page else []))
    candidates = tuple(link for link in links if _is_content_link(link))
    body_stretch, body_stretch_chunks = _longest_body_stretch(source_url, parser, candidates)
    body_labels = _body_section_labels(parser, body_stretch_chunks)
    page_title = collapse_whitespace(" ".join(parser.title_parts))
    title_parts = (
        _primary_heading(parser.h1_headings, page_title)
        or _lone_titled_heading(
            parser.h2_headings,
            page_title,
            referring_link=referring_link,
            body=(body_stretch, body_labels),
        )
        or _lone_titled_heading(
            parser.h3_headings,
            page_title,
            referring_link=referring_link,
            body=(body_stretch, body_labels),
        )
    )
    content = collapse_whitespace(" ".join(parser.content_parts))
    marked = _marked_title(parser, title_parts, content)
    if marked is not None:
        title_parts, content = marked
    preview = _preview_detail(source_url, parser)
    if preview is not None:
        title_parts, content, _markup = preview
    owned_bodies = _owned_posting_bodies(source_url, parser)
    owned = owned_bodies[0] if len(owned_bodies) == 1 else None
    if owned is not None:
        title_parts, content, _markup, body_stretch_chunks = owned
        body_stretch = content
    path_carries_identifier = _carries_identifier(urlsplit(source_url).path)
    # A block guess could contradict declared posting data.
    block, referred = (
        (None, False)
        if title_parts or content or parser.has_jsonld_posting
        else _untitled_block(parser, page_title, path_carries_identifier, referring_link)
    )
    if block is not None:
        title_parts = (parser.text_chunks[block][0],)
        content = _text_after(parser, block + 1)
    title = collapse_whitespace(" ".join(title_parts))
    # Repeated title headings do not supply independent posting sections.
    title_sections = (
        0
        if not title
        else sum(
            len(parts)
            for parts in parser.h2_headings + parser.h3_headings
            if collapse_whitespace(" ".join(parts)).casefold() == title.casefold()
        )
    )
    signals = _PageSignals(
        title=title,
        page_title=page_title,
        content=content,
        body_text=content,
        all_text=collapse_whitespace(" ".join(parser.all_text_parts)),
        links=links,
        candidates=candidates,
        section_count=max(
            len(parser.section_headings) - title_sections,
            len(parser.preview_sections) if preview is not None else 0,
        ),
        body_stretch=body_stretch,
        body_stretch_chunks=body_stretch_chunks,
        path_carries_identifier=path_carries_identifier,
        title_marked=marked is not None or preview is not None,
        posting_body_markup=owned[2]
        if owned is not None
        else preview[2]
        if preview is not None
        else "",
        multiple_role_bodies=len(owned_bodies) > 1,
        title_referred=referred,
        title_framed=parser.content_header is not None,
    )
    if title and not content:
        return replace(signals, body_text=_frameless_body(source_url, parser, signals))
    return signals


def _marked_title(
    parser: PublisherPageParser, title_parts: tuple[str, ...], content: str
) -> tuple[tuple[str, ...], str] | None:
    """Return the title and body of an untitled page from its one marked heading, if any."""
    # Card grids mark every card's heading alike, so only a lone marked heading titles a page.
    if title_parts or len(parser.marked_titles) != 1:
        return None
    start, end = parser.marked_titles[0]
    return tuple(text for text, _ in parser.text_chunks[start:end]), content or _text_after(
        parser, end
    )


def _body_role_headings(
    parser: PublisherPageParser, start: int, end: int, named: bool
) -> list[tuple[int, int]]:
    linked = _linked_chunks(parser)
    roles = [
        span
        for span in parser.heading_spans
        if start <= span[0] < span[1] <= end
        and not linked.intersection(range(*span))
        and (
            heading := collapse_whitespace(
                " ".join(text for text, _ in parser.text_chunks[span[0] : span[1]])
            )
        )
        and (
            states_a_specific_occupation(heading)
            or any(
                start <= raw.text_span[0] < raw.text_span[1] <= end
                and re.fullmatch(
                    rf"Apply(?: for)? {re.escape(heading)}",
                    raw.visible_label or raw.fallback_label,
                    re.IGNORECASE,
                )
                for raw in parser.links
            )
        )
    ]
    if roles or not named:
        return roles
    primary = _primary_heading(
        parser.h1_headings, collapse_whitespace(" ".join(parser.title_parts))
    )
    if not primary:
        return []
    title = collapse_whitespace(" ".join(primary))
    if not (
        states_a_specific_occupation(title)
        or collapse_whitespace(" ".join(parser.title_parts)).casefold().startswith(title.casefold())
    ):
        return []
    return [
        span
        for span in parser.heading_spans
        if not linked.intersection(range(*span))
        and tuple(text for text, _ in parser.text_chunks[span[0] : span[1]]) == primary
    ]


def _posting_body_candidate(
    parser: PublisherPageParser,
    start: int,
    end: int,
    linked: set[int],
    *,
    require_sections: bool = True,
) -> tuple[str, frozenset[int]] | None:
    chunks = frozenset(
        index
        for index in range(start, end)
        if not parser.text_chunks[index][1] and index not in linked
    )
    body = collapse_whitespace(" ".join(parser.text_chunks[index][0] for index in sorted(chunks)))
    sections = {word.casefold() for word in _POSTING_SECTION_WORDS.findall(body)}
    application = any(
        start <= raw.text_span[0] < raw.text_span[1] <= end
        and _APPLICATION_WORDS.search(raw.visible_label or raw.fallback_label)
        for raw in parser.links
    )
    if (
        len(body) < 240
        or (require_sections and len(sections - {"benefits", "compensation", "how to apply"}) < 2)
        or not application
    ):
        return None
    return body, chunks


def _has_identified_named_body(source_url: str, parser: PublisherPageParser) -> bool:
    source = urlsplit(source_url)
    try:
        canonical = urlsplit(urljoin(source_url, parser.canonical))
    except ValueError:
        return False
    named_spans = {(start, end) for start, end, _, named in parser.body_frames if named}
    named_owners = [
        span
        for span in named_spans
        if not any(
            span != other and span[0] <= other[0] < other[1] <= span[1] for other in named_spans
        )
    ]
    return (
        bool(parser.canonical)
        and _carries_identifier(source.path)
        and same_origin(source, canonical)
        and source.path == canonical.path
        and len(named_owners) == 1
    )


def _named_body_title_matches(parser: PublisherPageParser, span: tuple[int, int]) -> bool:
    title = collapse_whitespace(" ".join(text for text, _ in parser.text_chunks[span[0] : span[1]]))
    page_title = collapse_whitespace(" ".join(parser.title_parts))
    return (
        page_title.casefold().startswith(title.casefold())
        and not re.fullmatch(r"careers?|jobs?|job description|description", title, re.IGNORECASE)
        and not _POSTING_SECTION_WORDS.fullmatch(title)
    )


_OwnedBodyCandidate = tuple[tuple[str, ...], str, str, frozenset[int], frozenset[str]]


def _owned_application_urls(
    source_url: str, parser: PublisherPageParser, start: int, end: int
) -> frozenset[str]:
    addresses = set()
    for raw in parser.links:
        if not (
            start <= raw.text_span[0] < raw.text_span[1] <= end
            and _APPLICATION_WORDS.search(raw.visible_label or raw.fallback_label)
        ):
            continue
        try:
            addresses.add(_normalize_fetch_url(urljoin(source_url, raw.href)))
        except ValueError:
            continue
    return frozenset(addresses)


def _retain_owned_body(owners: list[_OwnedBodyCandidate], candidate: _OwnedBodyCandidate) -> None:
    for index, existing in enumerate(owners):
        nested = candidate[3] <= existing[3] or existing[3] <= candidate[3]
        if nested and candidate[4] == existing[4]:
            if len(candidate[2]) < len(existing[2]):
                owners[index] = candidate
            return
    owners.append(candidate)


def _owned_posting_bodies(
    source_url: str,
    parser: PublisherPageParser,
) -> list[tuple[tuple[str, ...], str, str, frozenset[int]]]:
    linked = _linked_chunks(parser)
    identified = _has_identified_named_body(source_url, parser)
    owned: dict[tuple[int, int], list[_OwnedBodyCandidate]] = {}
    for start, end, markup, named in parser.body_frames:
        headings = _body_role_headings(parser, start, end, named)
        relaxed = (
            named
            and identified
            and bool(headings)
            and all(_named_body_title_matches(parser, span) for span in headings)
        )
        evidence = _posting_body_candidate(parser, start, end, linked, require_sections=not relaxed)
        if evidence is None:
            continue
        body, chunks = evidence
        applications = _owned_application_urls(source_url, parser, start, end)
        for span in headings:
            title_parts = tuple(text for text, _ in parser.text_chunks[span[0] : span[1]])
            candidate = (title_parts, body, markup, chunks, applications)
            _retain_owned_body(owned.setdefault(span, []), candidate)
    return [
        (title, body, markup, chunks)
        for owners in owned.values()
        for title, body, markup, chunks, _ in owners
    ]


def _preview_detail(
    source_url: str, parser: PublisherPageParser
) -> tuple[tuple[str, ...], str, str] | None:
    """Return one role's titled detail block under its own posting address."""
    path = urlsplit(source_url).path.rstrip("/").split("/")
    sections = {section.casefold() for section in parser.preview_sections}
    if (
        len(path) < 2
        or path[-2].casefold() != "details"
        or not _carries_identifier(path[-1])
        or parser.has_form
        or len(parser.preview_frames) != 1
        or len(parser.preview_titles) != 1
        or len(parser.preview_details) != 1
        or "description" not in sections
        or not {"requirements", "salary description"}.intersection(sections)
    ):
        return None
    start, end = parser.preview_titles[0]
    title_parts = tuple(text for text, _ in parser.text_chunks[start:end])
    title = collapse_whitespace(" ".join(title_parts))
    if not (states_a_specific_occupation(title) or names_a_leadership_rung(title)):
        return None
    start, end, markup = parser.preview_details[0]
    linked = _linked_chunks(parser)
    body = collapse_whitespace(
        " ".join(
            text
            for index, (text, in_chrome) in enumerate(parser.text_chunks[start:end], start)
            if not in_chrome and index not in linked
        )
    )
    return (title_parts, body, markup) if len(body) >= 240 else None


def _frameless_body(source_url: str, parser: PublisherPageParser, signals: _PageSignals) -> str:
    """Return the unlinked text outside chrome unless repeated links run through it."""
    # Plain-div links count here whether or not they were promoted to candidates.
    shapes = Counter(
        _parent_shape(link.fetch_url)
        for link in signals.links
        if link.structural_context not in CHROME_CONTEXTS and _substantive_label(link.label)
    )
    if any(count >= 2 for count in shapes.values()):
        repeated = tuple(
            replace(link, cluster_size=shapes[_parent_shape(link.fetch_url)])
            for link in signals.links
            if link.structural_context not in CHROME_CONTEXTS
            and _substantive_label(link.label)
            and shapes[_parent_shape(link.fetch_url)] >= 2
        )
        body, body_chunks = _longest_body_stretch(source_url, parser, repeated)
        beside = replace(
            signals,
            candidates=repeated,
            title_marked=False,
            body_stretch=body,
            body_stretch_chunks=body_chunks,
        )
        if not (_beside_no_posting_links(beside) or _body_stands_apart(beside, parser)):
            return ""
        section_words = {word.casefold() for word in _POSTING_SECTION_WORDS.findall(body)}
        return (
            collapse_whitespace(body)
            if len(body) >= _BODY_BESIDE_LINKS_CHARACTERS and len(section_words) >= 2
            else ""
        )
    linked = _linked_chunks(parser)
    return collapse_whitespace(
        " ".join(
            text
            for index, (text, in_chrome) in enumerate(parser.text_chunks)
            if not in_chrome and index not in linked
        )
    )


def _parent_shape(url: str) -> str:
    """Return a link's host, query keys, and parent path with any digit-bearing segment as {id}."""
    # _target_shape leaves a hyphenated id such as /job/R-1234/<slug> literal.
    parsed = urlsplit(url)
    parents = [
        "{id}" if _opaque_segment(segment) or any(char.isdigit() for char in segment) else segment
        for segment in parsed.path.rstrip("/").split("/")[:-1]
        if segment
    ]
    keys = sorted({key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    return f"{parsed.netloc.casefold()}/{'/'.join(parents)}?{'&'.join(keys)}"


def _linked_chunks(parser: PublisherPageParser) -> set[int]:
    """Return the indexes of text chunks that belong to a link's own text."""
    return {index for raw in parser.links for index in range(*raw.text_span)}


def _text_after(parser: PublisherPageParser, index: int) -> str:
    """Return the page's non-chrome text from one text chunk onward."""
    return " ".join(text for text, in_chrome in parser.text_chunks[index:] if not in_chrome)


def _primary_heading(headings: list[tuple[str, ...]], page_title: str) -> tuple[str, ...]:
    """Return the first <h1> the page's <title> begins with, else the first <h1>."""
    if not headings:
        return ()
    title = page_title.casefold()
    return next(
        (
            parts
            for parts in headings
            if title.startswith(collapse_whitespace(" ".join(parts)).casefold())
        ),
        headings[0],
    )


def _lone_titled_heading(
    headings: list[tuple[str, ...]],
    page_title: str,
    *,
    referring_link: PublisherLink | None = None,
    body: tuple[str, frozenset[str]] = ("", frozenset()),
) -> tuple[str, ...]:
    """Return the unique role heading the page's <title> repeats."""
    titled = {
        collapse_whitespace(" ".join(parts)).casefold(): parts
        for parts in headings
        if _substantive_label(" ".join(parts))
        and collapse_whitespace(" ".join(parts)).casefold() in page_title.casefold()
    }
    if len(titled) != 1:
        return ()
    (parts,) = titled.values()
    heading = collapse_whitespace(" ".join(parts))
    if states_a_specific_occupation(heading) or names_a_leadership_rung(heading):
        return parts
    if (
        referring_link is None
        or LinkRelation.REPEATED_TARGET_SHAPE not in referring_link.relations
        or _TITLE_SEPARATOR.split(page_title, maxsplit=1)[0].casefold() != heading.casefold()
        or heading.casefold() not in body[1]
    ):
        return ()
    sections = {
        word.casefold() for label in body[1] for word in _POSTING_SECTION_WORDS.findall(label)
    } - {"benefits", "compensation", "how to apply"}
    metadata_label = re.fullmatch(
        rf"{re.escape(heading)}(?: (?:full[- ]time|part[- ]time|contract|temporary)"
        r"(?:, [^<>]+)?(?: See Job Details(?: .*)?)?)?",
        referring_link.label,
        re.IGNORECASE,
    )
    return (
        parts
        if (
            metadata_label
            and len(body[0]) >= _BODY_BESIDE_LINKS_CHARACTERS
            and len(sections) >= 2
            and any(_APPLICATION_WORDS.search(label) for label in body[1])
        )
        else ()
    )


def _titled_block(parser: PublisherPageParser, page_title: str) -> int | None:
    """Return the text block repeating the role the <title> leads with, as a chunk index."""
    segments = _TITLE_SEPARATOR.split(page_title, maxsplit=1)
    leading = segments[0].strip()
    if len(segments) < 2 or not (
        states_a_specific_occupation(leading) or names_a_leadership_rung(leading)
    ):
        return None
    leading = leading.casefold()
    linked = _linked_chunks(parser)
    return next(
        (
            index
            for index, (text, in_chrome) in enumerate(parser.text_chunks)
            if not in_chrome and index not in linked and text.casefold() == leading
        ),
        None,
    )


def _untitled_block(
    parser: PublisherPageParser,
    page_title: str,
    path_carries_identifier: bool,
    referring_link: PublisherLink | None,
) -> tuple[int | None, bool]:
    """Return the text block that titles an untitled page, and whether a referral chose it."""
    # A page-title guess needs a posting's own address.
    block = _titled_block(parser, page_title) if path_carries_identifier else None
    # A first- to third-level heading already had its chance to title the posting.
    headings = parser.h1_headings or parser.h2_headings or parser.h3_headings
    if block is not None or referring_link is None or headings:
        return block, False
    block = _referred_block(parser, referring_link.label)
    return block, block is not None


def _referred_block(parser: PublisherPageParser, label: str) -> int | None:
    """Return the leading content text block when the referring label repeats it."""
    linked = _linked_chunks(parser)
    chunks = [
        index
        for index, (_text, in_chrome) in enumerate(parser.text_chunks)
        if not in_chrome and index not in linked and index not in parser.title_chunks
    ]
    if not chunks:
        return None
    # Compared without spaces, so "Full-time , Remote" in a label matches "Full-time, Remote".
    leading = "".join(parser.text_chunks[chunks[0]][0].split()).casefold()
    wanted = "".join(label.split()).casefold()
    following = "".join("".join(parser.text_chunks[index][0].split()) for index in chunks)
    return (
        chunks[0]
        if leading and wanted.startswith(leading) and following.casefold().startswith(wanted)
        else None
    )


def _longest_body_stretch(
    source_url: str, parser: PublisherPageParser, candidates: tuple[PublisherLink, ...]
) -> tuple[str, frozenset[int]]:
    """Return one substantive sectioned run, or the longest run when none is sectioned."""
    clustered = {link.fetch_url.casefold() for link in candidates if link.cluster_size >= 2}
    breaks: set[int] = set()
    linked: set[int] = set()
    for raw in parser.links:
        if _normalize_fetch_url(urljoin(source_url, raw.href)).casefold() in clustered:
            breaks.add(raw.text_span[0])
            linked.update(range(*raw.text_span))
    runs: list[list[int]] = [[]]
    for index, (_text, in_chrome) in enumerate(parser.text_chunks):
        if index in breaks:
            runs.append([])
        if index not in linked and not in_chrome:
            runs[-1].append(index)
    bodies = [
        (" ".join(parser.text_chunks[index][0] for index in run), frozenset(run)) for run in runs
    ]
    substantive = [
        (body, chunks)
        for body, chunks in bodies
        if len(body) >= _BODY_BESIDE_LINKS_CHARACTERS
        and len({word.casefold() for word in _POSTING_SECTION_WORDS.findall(body)}) >= 2
    ]
    if len(substantive) > 1:
        return "", frozenset()
    return substantive[0] if substantive else max(bodies, key=lambda body: len(body[0]))


def _clustered_links(signals: _PageSignals) -> tuple[PublisherLink, ...]:
    return tuple(link for link in signals.candidates if link.cluster_size >= 2)


def _body_section_labels(parser: PublisherPageParser, chunks: frozenset[int]) -> frozenset[str]:
    """Return section and application labels owned by one unlinked body."""
    summary_chunks = {
        index
        for raw in parser.links
        if raw.summary_span is not None
        for index in range(*raw.summary_span)
    }
    labels = {
        parser.text_chunks[index][0].casefold()
        for index in parser._section_label_chunks.intersection(chunks)
        - summary_chunks
        - _linked_chunks(parser)
    }
    labels.update(
        label.casefold()
        for raw in parser.links
        if raw.text_span[0] in chunks - summary_chunks
        and _APPLICATION_WORDS.search(label := raw.visible_label or raw.fallback_label)
    )
    return frozenset(labels)


def _body_stands_apart(signals: _PageSignals, parser: PublisherPageParser) -> bool:
    """Return whether one role's long body, not its repeated links, carries the page."""
    title = signals.title.casefold()
    section_words = {
        word.casefold() for word in _POSTING_SECTION_WORDS.findall(signals.body_stretch)
    }
    if len(signals.body_stretch) < _BODY_BESIDE_LINKS_CHARACTERS or len(section_words) < 2:
        return False
    clustered = _clustered_links(signals)
    role_sections = {
        word.casefold()
        for label in _body_section_labels(parser, signals.body_stretch_chunks)
        for word in _POSTING_SECTION_WORDS.findall(label)
    } - {"benefits", "compensation", "how to apply"}
    titled_links = [
        link
        for link in clustered
        if re.fullmatch(
            rf"{re.escape(title)}(?: \((?:\d+[- ]months? )?contract\))?",
            link.label.casefold(),
        )
    ]
    self_titled = (
        signals.page_title.casefold().startswith(title)
        and title in signals.body_stretch.casefold()
        and len(role_sections) >= 2
        and any(link.fetch_url == _normalize_fetch_url(link.source_url) for link in titled_links)
    )
    return self_titled or (
        (signals.path_carries_identifier or _beside_no_posting_links(signals))
        and (states_a_specific_occupation(signals.title) or names_a_leadership_rung(signals.title))
        and not titled_links
    )


def _beside_no_posting_links(signals: _PageSignals) -> bool:
    """Return whether the title sits beside repeated links that name no posting."""
    clustered = _clustered_links(signals)
    # A search page's preview pane marks its title too, beside result cards that carry ids.
    if any(
        _carries_identifier(parsed.path)
        or any(_carries_identifier(value) for _, value in parse_qsl(parsed.query))
        for parsed in (urlsplit(link.fetch_url) for link in clustered)
    ):
        return False
    # A category page links its postings by slug alone, so an unmarked title needs a site menu.
    return signals.title_marked or not any(
        states_a_specific_occupation(link.label) or names_a_leadership_rung(link.label)
        for link in clustered
    )


def _carries_identifier(text: str) -> bool:
    """Return whether a URL path or query value holds an identifier token."""
    return any(_opaque_segment(token) for token in re.split(r"[^A-Za-z0-9_]+", text) if token)


def _publisher_links(source_url: str, raw_links: list[RawLink]) -> tuple[PublisherLink, ...]:
    source = urlsplit(source_url)
    unique: dict[str, PublisherLink] = {}
    for raw in raw_links:
        link = _make_publisher_link(source_url, source, raw)
        if link is None:
            continue
        key = link.fetch_url.casefold()
        existing = unique.get(key)
        if existing is None or (
            link.structural_context in _CONTENT_CONTEXTS
            and existing.structural_context not in _CONTENT_CONTEXTS
        ):
            unique[key] = link
    return _with_cluster_evidence(tuple(unique.values()))


def _with_occupation_listings(links: tuple[PublisherLink, ...]) -> tuple[PublisherLink, ...]:
    """Mark loose same-origin links as content when most links of their shape name roles."""
    # A site builder may put its role list in a header or bare divs; nav and footer stay chrome.
    loose = [
        link
        for link in links
        if link.structural_context not in _CONTENT_CONTEXTS | {"navigation", "footer"}
        and LinkRelation.SAME_ORIGIN in link.relations
    ]
    shapes = Counter(link.target_shape for link in loose)
    roles = Counter(
        link.target_shape
        for link in loose
        if states_a_specific_occupation(link.label) or names_a_leadership_rung(link.label)
    )
    # An unlabelled link of a role shape is usually an icon to the board's search page.
    listed = {
        id(link)
        for link in loose
        if collapse_whitespace(link.label)
        and roles[link.target_shape] >= 2
        and 2 * roles[link.target_shape] > shapes[link.target_shape]
    }
    return tuple(
        replace(
            link,
            relations=tuple(
                LinkRelation.PUBLISHER_CONTENT
                if relation is LinkRelation.NAVIGATION_CHROME
                else relation
                for relation in link.relations
            ),
        )
        if id(link) in listed
        else link
        for link in links
    )


def _is_content_link(link: PublisherLink) -> bool:
    return link.structural_context in _CONTENT_CONTEXTS or (
        LinkRelation.PUBLISHER_CONTENT in link.relations
    )


def _with_cluster_evidence(links: tuple[PublisherLink, ...]) -> tuple[PublisherLink, ...]:
    links = _with_occupation_listings(links)
    shape_counts = Counter(
        link.target_shape
        for link in links
        if _substantive_label(link.label) and _is_content_link(link)
    )
    return tuple(
        replace(
            link,
            cluster_size=shape_counts.get(link.target_shape, 1),
            relations=link.relations
            + (
                (LinkRelation.REPEATED_TARGET_SHAPE,)
                if shape_counts.get(link.target_shape, 1) >= 2
                else ()
            ),
        )
        for link in links
    )


def _make_publisher_link(
    source_url: str, source: SplitResult, raw: RawLink
) -> PublisherLink | None:
    fetch_url = _normalize_fetch_url(urljoin(source_url, raw.href))
    parsed = urlsplit(fetch_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    label = raw.visible_label or collapse_whitespace(raw.fallback_label)
    relations = [
        LinkRelation.SAME_ORIGIN if same_origin(source, parsed) else LinkRelation.EXTERNAL_HANDOFF
    ]
    relations.append(
        LinkRelation.PUBLISHER_CONTENT
        if raw.context in _CONTENT_CONTEXTS
        else LinkRelation.NAVIGATION_CHROME
    )
    if _substantive_label(label):
        relations.append(LinkRelation.SUBSTANTIVE_LABEL)
    return PublisherLink(
        fetch_url,
        label,
        "visible" if raw.visible_label else raw.fallback_source,
        source_url,
        raw.context,
        _target_shape(parsed),
        1,
        tuple(relations),
        collection_pagination=_collection_pagination(raw, source, parsed),
    )


def _collection_pagination(raw: RawLink, source: SplitResult, target: SplitResult) -> bool:
    if not raw.pagination or not same_origin(source, target) or source.path != target.path:
        return False
    paging_keys = {"page", "paged", "offset", "start"}
    before = dict(parse_qsl(source.query, keep_blank_values=True))
    after = dict(parse_qsl(target.query, keep_blank_values=True))
    positions = [value for key, value in after.items() if key in paging_keys]
    return (
        len(positions) == 1
        and positions[0].isdigit()
        and {key: value for key, value in before.items() if key not in paging_keys}
        == {key: value for key, value in after.items() if key not in paging_keys}
    )


def _corroborates_posting(
    parser: PublisherPageParser,
    signals: _PageSignals,
    referring_link: PublisherLink | None,
) -> bool:
    """Return whether publisher structure and a posting-specific signal together mark a posting."""
    heading = signals.title
    if not heading or not signals.body_text:
        return False
    if signals.title_referred and not _referral_agrees(parser, signals):
        return False
    # A title chosen for looking like a role cannot also be the posting-specific signal.
    chosen = signals.title_referred or signals.title_framed
    if chosen and not (_role_sections(signals.body_text) or signals.posting_body_markup):
        return False
    structural = [
        len(signals.body_text) >= 240,
        signals.section_count >= 2,
    ]
    posting_specific = [
        bool(
            {word.casefold() for word in _POSTING_SECTION_WORDS.findall(signals.body_text)}
            - {"benefits", "compensation", "how to apply"}
        ),
        bool(signals.posting_body_markup),
        parser.has_form and bool(_APPLICATION_WORDS.search(signals.all_text)),
        # A repeated target shape is why the page was probed at all, so it marks a
        # posting only for a leadership heading the occupation entry below misses;
        # counting one heading twice would reach the threshold with no structure.
        referring_link is not None
        and LinkRelation.REPEATED_TARGET_SHAPE in referring_link.relations
        and names_a_leadership_rung(heading)
        and not states_a_specific_occupation(heading),
        states_a_specific_occupation(heading) and not chosen,
        any(
            LinkRelation.PUBLISHER_CONTENT in link.relations
            and _APPLICATION_WORDS.search(link.label)
            for link in signals.links
        ),
    ]
    return any(posting_specific) and sum(structural) + sum(posting_specific) >= 2


def _referral_agrees(parser: PublisherPageParser, signals: _PageSignals) -> bool:
    """Return whether a referred leading title has a role's own evidence and no role list."""
    roles = Counter(
        _parent_shape(link.fetch_url)
        for link in signals.links
        if states_a_specific_occupation(link.label) or names_a_leadership_rung(link.label)
    )
    if any(count >= 2 for count in roles.values()):
        return False
    return bool(
        _role_sections(signals.body_text)
        or any(
            len(text) <= 40 and _EMPLOYMENT_TYPE.search(text)
            for text, in_chrome in parser.text_chunks
            if not in_chrome
        )
        or any(
            link.structural_context not in CHROME_CONTEXTS and _APPLICATION_WORDS.search(link.label)
            for link in signals.links
        )
    )


def _target_shape(parsed: SplitResult) -> str:
    # Boards often place the opaque posting id before a readable slug.
    segments = [
        "{id}" if _opaque_segment(segment) else segment
        for segment in parsed.path.split("/")
        if segment
    ]
    query_keys = sorted(
        {
            key.casefold()
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            if not _tracking_parameter(key) and key.casefold() != "ref"
        }
    )
    if segments and not query_keys:
        segments[-1] = "{item}"
    return (
        f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}/{'/'.join(segments)}?"
        f"{'&'.join(query_keys)}"
    )


def _opaque_segment(segment: str) -> bool:
    """Recognize an identifier token: digits, letters mixed with digits, or inner capitals."""
    if not _OPAQUE_SEGMENT.fullmatch(segment):
        return False
    has_digit = any(char.isdigit() for char in segment)
    has_alpha = any(char.isalpha() for char in segment)
    inner_capital = any(char.isupper() for char in segment[1:]) and any(
        char.islower() for char in segment
    )
    return segment.isdigit() or (has_digit and has_alpha) or inner_capital


def _normalize_fetch_url(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _tracking_parameter(key)
        ],
        doseq=True,
    )
    return parsed._replace(query=query, fragment="").geturl()


def _tracking_parameter(key: str) -> bool:
    lowered = key.casefold()
    return lowered.startswith("utm_") or lowered in _TRACKING_KEYS


def _substantive_label(label: str) -> bool:
    cleaned = collapse_whitespace(label)
    return 2 <= len(cleaned) <= 300 and not cleaned.startswith(("http://", "https://"))


def _navigation_weight(links: tuple[PublisherLink, ...]) -> int:
    return sum(link.structural_context in CHROME_CONTEXTS for link in links)


def _content_weight(links: tuple[PublisherLink, ...]) -> int:
    return sum(link.structural_context in _CONTENT_CONTEXTS for link in links)


def _is_navigation(links: tuple[PublisherLink, ...]) -> bool:
    if not links:
        return False
    return all(link.structural_context in CHROME_CONTEXTS for link in links) or (
        _navigation_weight(links) > _content_weight(links)
    )


def _navigation_evidence(links: tuple[PublisherLink, ...]) -> str:
    if all(link.structural_context in CHROME_CONTEXTS for link in links):
        return "links occur only in navigation chrome"
    return "navigation links outweigh content links"
