"""Parse publisher HTML into text chunks, links, headings and structural signals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from itertools import accumulate
from typing import Literal, cast
from urllib.parse import SplitResult, urljoin, urlsplit

StructuralContext = Literal[
    "article", "card", "footer", "header", "list", "main", "navigation", "other", "section", "table"
]
LabelSource = Literal["visible", "aria-label", "title", "none"]
CHROME_CONTEXTS = frozenset({"footer", "header", "navigation"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# A data-testid ending in "job-title" marks the heading a job view titles itself with.
_MARKED_TITLE = re.compile(r"(?:^|[-_])job-title$", re.IGNORECASE)


@dataclass(frozen=True)
class RawLink:
    """One anchor as the parser saw it, before any page role is assigned."""

    href: str
    visible_label: str
    fallback_label: str
    fallback_source: LabelSource
    context: StructuralContext
    # The slice of the parser's text chunks that the link's own text occupies.
    text_span: tuple[int, int]
    summary_span: tuple[int, int] | None = None
    pagination: bool = False


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    text_start: int = 0
    pagination: dict[str, list[str]] | None = None
    markup_start: int = 0
    header_index: int = -1
    # A header holding the page's only <h1> and most of its text is the content frame.
    frames_content: bool = False


class PublisherPageParser(HTMLParser):
    """Collect the text, links, headings and frames a publisher page declares."""

    def __init__(self, page: str = "", content_header: int | None = None) -> None:
        """Prepare to read ``page``, treating header ``content_header`` as its content frame."""
        super().__init__(convert_charrefs=True)
        self.page = page
        self.content_header = content_header
        # Each <header>'s slice of text_chunks, in document order.
        self.header_spans: list[tuple[int, int]] = []
        self.h1_starts: list[int] = []
        self.title_chunks: set[int] = set()
        self._line_offsets = tuple(accumulate(map(len, page.splitlines(keepends=True)), initial=0))
        self.preview_frames: list[int] = []
        self.preview_titles: list[tuple[int, int]] = []
        self.preview_details: list[tuple[int, int, str]] = []
        self.preview_sections: list[str] = []
        self.links: list[RawLink] = []
        self.title_parts: list[str] = []
        self.h1_headings: list[tuple[str, ...]] = []
        self.section_headings: list[str] = []
        self._section_label_chunks: set[int] = set()
        self.h2_headings: list[tuple[str, ...]] = []
        self.h3_headings: list[tuple[str, ...]] = []
        # Each marked job-title heading's slice of text_chunks.
        self.marked_titles: list[tuple[int, int]] = []
        self.text_chunks: list[tuple[str, bool]] = []
        self.content_parts: list[str] = []
        self.all_text_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.has_form = False
        self.has_file_input = False
        self.has_password_input = False
        self.has_primary_password_input = False
        self.has_jsonld_posting = False
        self.invalid_jsonld = False
        self.heading_spans: list[tuple[int, int]] = []
        self.body_frames: list[tuple[int, int, str, bool]] = []
        self.canonical = ""
        self.og_url = ""
        self.script_count = 0
        self._stack: list[_Element] = []
        self._active_link: tuple[str, str, LabelSource, StructuralContext] | None = None
        self._active_link_text: list[str] = []
        self._active_link_image_text: list[str] = []
        self._active_link_start = 0
        self._active_pagination = False
        self._h1_parts: list[str] | None = None
        self._h2_parts: list[str] | None = None
        self._h3_parts: list[str] | None = None
        self._jsonld_parts: list[str] | None = None
        self._marked_title: tuple[str, int] | None = None
        self._search_results: list[_Element] = []
        self._pagination_scripts: list[str] = []
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element and start whatever link, heading or script it begins."""
        lowered = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        element = _Element(
            lowered, attributes, len(self.text_chunks), markup_start=self._markup_offset()
        )
        self._stack.append(element)
        self._open_header(element)
        self._open_search_results(element)
        self._open_marked_title(lowered, attributes)
        if lowered == "a":
            self._open_link(attributes)
        elif lowered == "img":
            self._retain_image_label(attributes)
        elif lowered == "h1" and self._h1_parts is None:
            self._h1_parts = []
            self.h1_starts.append(len(self.text_chunks))
        elif lowered == "h2":
            self._h2_parts = []
        elif lowered == "h3":
            self._h3_parts = []
        elif lowered == "form":
            self.has_form |= not any(_is_hidden(item) for item in self._stack)
        elif lowered == "input":
            self._retain_input_signals(attributes)
        elif lowered in ("link", "meta"):
            self._retain_self_address(lowered, attributes)
        elif lowered == "script":
            self.script_count += 1
            self._script_parts = []
            if attributes.get("src"):
                self._pagination_scripts.append(attributes["src"])
            if "ld+json" in attributes.get("type", "").casefold():
                self._jsonld_parts = []

    def _open_header(self, element: _Element) -> None:
        if element.tag == "header":
            element.header_index = len(self.header_spans)
            element.frames_content = element.header_index == self.content_header
            self.header_spans.append((element.text_start, element.text_start))

    def _close_header(self, element: _Element) -> None:
        if element.header_index >= 0:
            self.header_spans[element.header_index] = (element.text_start, len(self.text_chunks))

    def _retain_self_address(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "link" and "canonical" in attributes.get("rel", "").casefold().split():
            self.canonical = self.canonical or attributes.get("href", "").strip()
        elif tag == "meta" and attributes.get("property", "").casefold() == "og:url":
            self.og_url = self.og_url or attributes.get("content", "").strip()

    def _retain_input_signals(self, attributes: dict[str, str]) -> None:
        input_type = attributes.get("type", "").casefold()
        visible = not any(_is_hidden(item) for item in self._stack[:-1])
        self.has_file_input |= visible and input_type == "file"
        password = (
            visible
            and input_type == "password"
            and "hidden" not in attributes
            and attributes.get("aria-hidden", "").casefold() != "true"
            and not _HIDING_STYLE.search(attributes.get("style", ""))
        )
        self.has_password_input |= password
        self.has_primary_password_input |= password and self._context() not in CHROME_CONTEXTS

    def _open_search_results(self, element: _Element) -> None:
        if "jResultsContent" in element.attrs.get("class", "").split() and not any(
            _element_context(item) in CHROME_CONTEXTS for item in self._stack
        ):
            element.pagination = {}
            self._search_results.append(element)

    def _retain_image_label(self, attributes: dict[str, str]) -> None:
        alt = collapse_whitespace(attributes.get("alt", ""))
        if (
            self._active_link is not None
            and alt
            and not any(_is_hidden(item) for item in self._stack)
        ):
            self._active_link_image_text.append(alt)

    def _open_link(self, attributes: dict[str, str]) -> None:
        aria_label = collapse_whitespace(attributes.get("aria-label", ""))
        fallback = aria_label or collapse_whitespace(attributes.get("title", ""))
        source: LabelSource = "aria-label" if aria_label else "title" if fallback else "none"
        self._active_link = (attributes.get("href", ""), fallback, source, self._context())
        self._active_pagination = (
            "page-numbers" in attributes.get("class", "").split()
            and not any(_is_hidden(item) for item in self._stack)
            and any(
                "pagination" in element.attrs.get("class", "").split() for element in self._stack
            )
        )
        self._active_link_text = []
        self._active_link_image_text = []
        self._active_link_start = len(self.text_chunks)

    def _open_marked_title(self, tag: str, attributes: dict[str, str]) -> None:
        """Start collecting a heading whose data-testid marks it as the job title."""
        if (
            tag in _HEADING_TAGS
            and self._marked_title is None
            and _MARKED_TITLE.search(attributes.get("data-testid", ""))
        ):
            self._marked_title = (tag, len(self.text_chunks))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Read a self-closing tag as an open followed by a close."""
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        """Record visible text against every open link, heading and frame."""
        cleaned = collapse_whitespace(data)
        if not cleaned:
            return
        tags = {element.tag for element in self._stack}
        if self._jsonld_parts is not None and "script" in tags:
            self._jsonld_parts.append(data)
        if self._script_parts is not None and "script" in tags:
            self._script_parts.append(data)
        # Bundled script vocabulary ("captcha", "error") is not text a reader sees.
        if tags.intersection({"script", "style"}) or any(
            _is_hidden(element) for element in self._stack
        ):
            return
        self.all_text_parts.append(cleaned)
        if not any(_is_hidden(element) for element in self._stack):
            self.visible_parts.append(cleaned)
        in_chrome = any(_element_context(element) in CHROME_CONTEXTS for element in self._stack)
        self.text_chunks.append((cleaned, in_chrome))
        if tags.intersection({"h2", "h3", "strong", "b"}) and not in_chrome:
            self._section_label_chunks.add(len(self.text_chunks) - 1)
        if self._h2_parts is not None:
            self._h2_parts.append(cleaned)
        if self._h3_parts is not None:
            self._h3_parts.append(cleaned)
        if self._active_link is not None:
            self._active_link_text.append(cleaned)
        if "title" in tags:
            self.title_parts.append(cleaned)
            self.title_chunks.add(len(self.text_chunks) - 1)
        if self._h1_parts is not None:
            self._h1_parts.append(cleaned)
        if tags.intersection({"h2", "h3"}):
            self.section_headings.append(cleaned)
        if self._in_content_frame(tags):
            self.content_parts.append(cleaned)

    def _in_content_frame(self, tags: set[str]) -> bool:
        return bool(tags.intersection({"main", "article"})) or any(
            element.frames_content for element in self._stack
        )

    def handle_endtag(self, tag: str) -> None:
        """Close an element and keep the link, heading, preview or body it finished."""
        lowered = tag.casefold()
        if lowered == "a" and self._active_link is not None:
            href, fallback, source, context = self._active_link
            self.links.append(
                RawLink(
                    href,
                    collapse_whitespace(" ".join(self._active_link_text))
                    or (
                        collapse_whitespace(" ".join(self._active_link_image_text))
                        if not fallback
                        else ""
                    ),
                    fallback,
                    source,
                    context,
                    (self._active_link_start, len(self.text_chunks)),
                    pagination=self._active_pagination,
                )
            )
            self._active_link = None
            self._active_link_text = []
            self._active_link_image_text = []
        if lowered == "h2" and self._h2_parts is not None:
            self.h2_headings.append(tuple(self._h2_parts))
            self._h2_parts = None
        if lowered == "h3" and self._h3_parts is not None:
            self.h3_headings.append(tuple(self._h3_parts))
            self._h3_parts = None
        if self._marked_title is not None and lowered == self._marked_title[0]:
            self.marked_titles.append((self._marked_title[1], len(self.text_chunks)))
            self._marked_title = None
        if lowered == "script":
            self._close_script()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == lowered:
                self._close_header(self._stack[index])
                self._retain_link_summary(self._stack[index])
                self._retain_pagination_metadata(index)
                self._retain_preview(index)
                self._retain_body(index)
                del self._stack[index:]
                break
        # An enclosing element's end tag closes an <h1> its page left open.
        if self._h1_parts is not None and all(element.tag != "h1" for element in self._stack):
            if self._h1_parts:
                self.h1_headings.append(tuple(self._h1_parts))
            self._h1_parts = None

    def _retain_link_summary(self, element: _Element) -> None:
        if not (
            element.tag in {"li", "tr"}
            or (element.tag == "div" and _element_context(element) in {"list", "card"})
        ):
            return
        for link_index in range(len(self.links) - 1, -1, -1):
            link = self.links[link_index]
            if link.text_span[0] < element.text_start:
                break
            if link.summary_span is None:
                self.links[link_index] = replace(
                    link, summary_span=(element.text_start, len(self.text_chunks))
                )

    def _close_script(self) -> None:
        if self._jsonld_parts is not None:
            value = "".join(self._jsonld_parts)
            try:
                json.loads(value)
            except (json.JSONDecodeError, TypeError):
                self.invalid_jsonld = True
            self.has_jsonld_posting |= contains_job_posting(value)
            self._jsonld_parts = None
        if self._script_parts is not None:
            self._pagination_scripts.extend(
                re.findall(
                    r"\.script\(\s*[\"'](/job_list\.js(?:\?[^\"']*)?)[\"']",
                    "".join(self._script_parts),
                )
            )
            self._script_parts = None

    def _retain_pagination_metadata(self, index: int) -> None:
        element = self._stack[index]
        metadata_id = element.attrs.get("id", "")
        if metadata_id not in {"jPaginateNumPages", "jPaginateCurrPage"}:
            return
        results = next(
            (item for item in reversed(self._stack[:index]) if item.pagination is not None), None
        )
        if results is not None and results.pagination is not None:
            text = collapse_whitespace(
                " ".join(part for part, _ in self.text_chunks[element.text_start :])
            )
            results.pagination.setdefault(metadata_id, []).append(text)

    def _markup_offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[min(line - 1, len(self._line_offsets) - 1)] + column

    def _retain_preview(self, index: int) -> None:
        element = self._stack[index]
        classes = element.attrs.get("class", "").split()
        parents = self._stack[:index]
        if (
            element.tag == "div"
            and "job-preview" in classes
            and _element_context(element) == "main"
        ):
            self.preview_frames.append(element.markup_start)
        frames = [
            parent
            for parent in parents
            if "job-preview" in parent.attrs.get("class", "").split()
            and _element_context(parent) == "main"
        ]
        if len(frames) != 1 or any(
            _element_context(parent) in CHROME_CONTEXTS for parent in parents
        ):
            return
        span = (element.text_start, len(self.text_chunks))
        if (
            element.tag == "span"
            and "job-preview-title" in classes
            and any(
                "job-preview-header" in parent.attrs.get("class", "").split() for parent in parents
            )
        ):
            self.preview_titles.append(span)
        elif element.tag == "div" and "job-preview-details" in classes:
            end = self.page.find(">", self._markup_offset()) + 1
            self.preview_details.append((*span, self.page[element.markup_start : end]))
        elif "job-listing-header" in classes and any(
            "job-preview-details" in parent.attrs.get("class", "").split() for parent in parents
        ):
            self.preview_sections.append(
                collapse_whitespace(
                    " ".join(text for text, _ in self.text_chunks[span[0] : span[1]])
                )
            )

    def _retain_body(self, index: int) -> None:
        element = self._stack[index]
        if any(
            _is_hidden(item) or _element_context(item) in CHROME_CONTEXTS
            for item in self._stack[: index + 1]
        ):
            return
        span = (element.text_start, len(self.text_chunks))
        if element.tag in _HEADING_TAGS:
            self.heading_spans.append(span)
        named = bool(
            {"job-description-bg", "job-description"}.intersection(
                element.attrs.get("class", "").casefold().split()
            )
        )
        if element.tag not in {"section", "article"} and not named:
            return
        end = self.page.find(">", self._markup_offset()) + 1
        self.body_frames.append((*span, self.page[element.markup_start : end], named))

    def _context(self) -> StructuralContext:
        contexts = [
            context
            for element in reversed(self._stack[:-1])
            if (context := _element_context(element)) is not None
        ]
        # A link under chrome is chrome, as its text is, whatever list or card sits nearer.
        return next(
            (context for context in contexts if context in CHROME_CONTEXTS),
            contexts[0] if contexts else "other",
        )

    def _pagination_link(self, source_url: str) -> RawLink | None:
        source = urlsplit(source_url)
        if source.scheme not in {"http", "https"} or not source.netloc:
            return None
        address = re.fullmatch(
            r"(?P<base>/(?:[^/]+/)*jobs/search/)(?P<search_id>[1-9]\d*)"
            r"(?:/page(?P<page>[1-9]\d*))?/?",
            source.path,
        )
        if address is None or len(self._search_results) != 1:
            return None
        script_declared = False
        for script in self._pagination_scripts:
            try:
                target = urlsplit(urljoin(source_url, script))
            except ValueError:
                continue
            if target.path == "/job_list.js" and same_origin(source, target):
                script_declared = True
                break
        if not script_declared:
            return None
        results = self._search_results[0]
        metadata = results.pagination or {}
        if results.attrs.get("data-jsid") != address["search_id"]:
            return None
        total_parts = metadata.get("jPaginateNumPages", [])
        current_parts = metadata.get("jPaginateCurrPage", [])
        if len(total_parts) != 1 or len(current_parts) != 1:
            return None
        if not all(
            re.fullmatch(r"[1-9]\d{0,8}(?:\.0+)?", value) for value in total_parts + current_parts
        ):
            return None
        total = int(total_parts[0].split(".")[0])
        current = int(current_parts[0].split(".")[0])
        if str(current) != (address["page"] or "1") or current >= total:
            return None
        next_url = source._replace(
            path=f"{address['base']}{address['search_id']}/page{current + 1}", fragment=""
        ).geturl()
        return RawLink(next_url, "", "Next page", "none", "list", (0, 0))


# Void elements never close, so one left on the stack must not hide the text after it.
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)


def _is_hidden(element: _Element) -> bool:
    """Return whether an element keeps its text from a reader of the rendered page."""
    if element.tag in _VOID_TAGS:
        return False
    return (
        element.tag in {"template", "noscript"}
        or "hidden" in element.attrs
        or element.attrs.get("aria-hidden", "").casefold() == "true"
        or _HIDING_STYLE.search(element.attrs.get("style", "")) is not None
    )


def _element_context(element: _Element) -> StructuralContext | None:
    if element.tag in {"footer", "header", "article", "main"}:
        return "main" if element.frames_content else cast(StructuralContext, element.tag)
    role = element.attrs.get("role", "").casefold()
    if element.tag == "nav" or role == "navigation":
        return "navigation"
    if element.tag == "section":
        return "section"
    if element.tag in {"table", "tbody", "tr", "td"}:
        return "table"
    if element.tag in {"ul", "ol", "li"}:
        return "list"
    classes = element.attrs.get("class", "").casefold().split()
    if any(name.replace("-", "_") in {"job_list_row", "job_row"} for name in classes):
        return "list"
    if any("card" in name for name in classes):
        return "card"
    if role == "main":
        return "main"
    return None


def same_origin(source: SplitResult, target: SplitResult) -> bool:
    """Return whether two addresses share scheme and host."""
    return (
        source.scheme.casefold() == target.scheme.casefold()
        and source.netloc.casefold() == target.netloc.casefold()
    )


def contains_job_posting(value: str) -> bool:
    """Return whether JSON-LD text declares a JobPosting anywhere in its graph."""
    try:
        loaded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return False
    pending = list(loaded) if isinstance(loaded, list) else [loaded]
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if any(isinstance(kind, str) and kind.casefold() == "jobposting" for kind in types):
            return True
        graph = item.get("@graph")
        if isinstance(graph, list):
            pending.extend(graph)
    return False


def collapse_whitespace(value: str) -> str:
    """Collapse every run of whitespace to one space."""
    return " ".join(value.split())
