"""Anonymous Taleo enterprise and classic career-section searches with publisher-declared scope."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import partial
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxml import html  # type: ignore[import-untyped]

from tailorcv.application.discovery.posting_text import publisher_closing_date
from tailorcv.domain.records import utc_now
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    MAX_API_RESPONSE_BYTES,
    PublisherSearchPage,
    held_continuation,
    held_target,
    page_markup,
    page_posting_detail,
    publisher_search_detail,
    publisher_search_read,
    required_list_field,
    unreadable_continuation,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks
from tailorcv.infrastructure.web_fetch.policy import HostHeldError

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

_ADDRESS = r"https://(?P<host>[a-zA-Z0-9-]+\.taleo\.net)/careersection/(?P<site>[a-zA-Z0-9_-]+)/"
TALEO_BOARD = re.compile(_ADDRESS + r"jobsearch\.ftl(?:\?(?P<query>[^#]*))?$")
TALEO_JOB = re.compile(_ADDRESS + r"jobdetail\.ftl(?:\?(?P<query>[^#]*))?$")
_REASON = "taleo_search_page"
_MAPPING = page_posting_declaration("taleo")
_FILTERS = ("LOCATION", "JOB_FIELD")
_ADVANCED = (
    "ORGANIZATION",
    "LOCATION",
    "JOB_FIELD",
    "JOB_NUMBER",
    "URGENT_JOB",
    "EMPLOYEE_STATUS",
    "WILL_TRAVEL",
    "JOB_SHIFT",
)
# The pager form a classic (FTL) section's list page posts; its endpoint answers a GET too.
_CLASSIC_PAGER = {
    "ftlpageid": "reqListBasicPage",
    "ftlinterfaceid": "requisitionListInterface",
    "ftlcompid": "rlPager",
    "jsfCmdId": "rlPager",
    "ftlcompclass": "PagerComponent",
    "ftlcallback": "ftlPager_processResponse",
    "ftlajaxid": "ftlx1",
}
_CELL, _SECTION, _PART = "!|!", "!$!", "!*!"
_LISTED_DATE = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}")


@dataclass(frozen=True)
class _SearchContext:
    endpoint: str
    detail_root: str
    query: dict[str, str]
    headers: dict[str, str]
    body: dict[str, Any]
    page: int
    classic: bool = False


def _query(url: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key in result and result[key] != value:
            raise ValueError("Taleo source states conflicting query values")
        result[key] = value
    return result


def _settings_object(script: str, start: int) -> str:
    depth, quote, escaped = 1, "", False
    for index in range(start, len(script)):
        character = script[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return script[start:index]
    raise ValueError("Taleo root declared an incomplete search context")


def _settings(page: str) -> dict[str, str] | None:
    declarations: list[str] = []
    for block in html_blocks(page, "script"):
        contexts = re.finditer(r"['\"]fs/FacetedSearchSettings['\"]\s*:\s*\{", block.inner)
        declarations.extend(_settings_object(block.inner, match.end()) for match in contexts)
    if not declarations:
        return None
    if len(declarations) != 1:
        raise ValueError("Taleo root did not declare one faceted-search context")
    result: dict[str, str] = {}
    for field in ("lang", "portalNo", "urlOrigin", "urlCode", "defaultSortingField"):
        matches = re.findall(rf"\b{field}\s*:\s*['\"]([^'\"]*)['\"]", declarations[0])
        if len(matches) != 1:
            raise ValueError("Taleo root did not declare an unambiguous search scope")
        result[field] = matches[0]
    orders = re.findall(r"\bdefaultOrder\s*:\s*(true|false)\b", declarations[0])
    if len(orders) != 1:
        raise ValueError("Taleo root did not declare its default search order")
    result["defaultOrder"] = orders[0]
    return result


def _selections(value: str, fields: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[str]] = {}
    if value and value != "null":
        for clause in value.split("|"):
            match = re.fullmatch(r"([A-Z_]+)\(([^()]*)\)", clause)
            if match is None or match[1] not in fields or match[1] in selected:
                raise ValueError("Taleo source states an unsupported search filter")
            selected[match[1]] = match[2].split(",") if match[2] else []
    return {
        "searchFilterSelections": [
            {"id": field, "selectedValues": selected.get(field, [])} for field in fields
        ]
    }


def _timezone(query: dict[str, str]) -> dict[str, str]:
    if not ({"tz", "tzname"} & query.keys()):
        return {"tz": "GMT+00:00", "tzname": "UTC"}
    offset = re.fullmatch(r"GMT[+-](\d{2}):(\d{2})", query.get("tz", ""))
    name = query.get("tzname", "")
    if offset is None or int(offset[1]) > 23 or int(offset[2]) > 59 or not name:
        raise ValueError("Taleo source did not state a valid timezone header group")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Taleo source states an unknown timezone name") from exc
    return {"tz": query["tz"], "tzname": name}


def _criteria_fields(tree: Any) -> dict[str, str]:
    fields = {}
    for node in tree.xpath(
        '//input[contains(concat(" ", normalize-space(@class), " "), " criteriaFieldInput ")]'
    ):
        field = str(node.get("id", ""))
        if not field or field in fields:
            raise ValueError("Taleo root declared ambiguous search fields")
        if node.get("isolf") == "true":
            values = tree.xpath("//input[@id=$id]/@value", id=field + "-olfid")
            if len(values) != 1:
                raise ValueError("Taleo root did not state the selected location identifier")
            fields[field] = str(values[0])
        else:
            fields[field] = str(node.get("value", ""))
    return fields


def _request_body(page: str, settings: dict[str, str], query: dict[str, str]) -> dict[str, Any]:
    tree = html.fromstring(page)
    fields = _criteria_fields(tree)
    options = tree.xpath(
        '//select[@id="sortBySelect"]/option[@value=$value]/@sortid',
        value=settings["defaultSortingField"],
    )
    if len(options) != 1:
        raise ValueError("Taleo root did not state its default sorting identifier")
    sort, ascending = str(options[0]), settings["defaultOrder"]
    if query.get("s") and query["s"] != "null":
        ordered = re.fullmatch(r"(\d+)\|([AD])", query["s"])
        if ordered is None:
            raise ValueError("Taleo source states malformed sorting")
        sort, ascending = ordered[1], "true" if ordered[2] == "A" else "false"
    multiline = query.get("multiline", "false")
    if multiline not in {"true", "false"}:
        raise ValueError("Taleo source states malformed multiline selection")
    return {
        "multilineEnabled": multiline == "true",
        "sortingSelection": {"sortBySelectionParam": sort, "ascendingSortingOrder": ascending},
        "fieldData": {"fields": fields, "valid": True},
        "filterSelectionParam": _selections(query.get("f", ""), _FILTERS),
        "advancedSearchFiltersSelectionParam": _selections(query.get("a", ""), _ADVANCED),
    }


def _config(fetcher: BoardTransport, source_url: str) -> _SearchContext:
    match = TALEO_BOARD.fullmatch(source_url)
    if match is None:
        raise ValueError("Taleo source did not name a career-section search")
    source = urlsplit(source_url)
    query = _query(source_url)
    page = page_markup(fetcher.get_text(source_url, max_bytes=MAX_API_RESPONSE_BYTES))
    settings = _settings(page)
    if settings is None:
        return _classic_config(page, f"{source.scheme}://{source.netloc}", match["site"], query)
    if (
        settings["urlOrigin"] != f"{source.scheme}://{source.netloc}"
        or settings["urlCode"] != match["site"]
    ):
        raise ValueError("Taleo root declared another tenant or career section")
    for key, field in (("lang", "lang"), ("portal", "portalNo")):
        if key in query and query[key] != settings[field]:
            raise ValueError("Taleo root declared another locale or portal")
        query[key] = settings[field]
    if (
        not re.fullmatch(r"[a-zA-Z]{2}(?:[-_][a-zA-Z]{2})?", query["lang"])
        or not query["portal"].isdigit()
    ):
        raise ValueError("Taleo root declared malformed locale or portal")
    headers = _timezone(query)
    current = int(query.get("pageNo", "1"))
    if current < 1:
        raise ValueError("Taleo source did not state a positive page")
    body = _request_body(page, settings, query)
    endpoint_query = urlencode({key: query[key] for key in ("lang", "portal")})
    endpoint = f"{settings['urlOrigin']}/careersection/rest/jobboard/searchjobs?{endpoint_query}"
    detail_root = f"{settings['urlOrigin']}/careersection/{match['site']}/jobdetail.ftl"
    return _SearchContext(endpoint, detail_root, query, headers, body, current)


def _classic_config(page: str, origin: str, site: str, query: dict[str, str]) -> _SearchContext:
    if "Career Section Unavailable" in page:
        raise ValueError("Taleo says this career section is unavailable")
    tree = html.fromstring(page)
    if tree.xpath('//input[@id="ftlpageid"]/@value') != [_CLASSIC_PAGER["ftlpageid"]]:
        raise ValueError("Taleo root declared neither a faceted search nor a classic job list")
    if set(query) - {"lang"}:
        raise ValueError("Taleo classic section search filters are not supported")
    declared = [str(value) for value in tree.xpath('//input[@id="lang"]/@value')]
    if len(declared) != 1 or query.get("lang", declared[0]) != declared[0]:
        raise ValueError("Taleo root declared another or no locale")
    if not re.fullmatch(r"[a-zA-Z]{2}(?:[-_][a-zA-Z]{2})?", declared[0]):
        raise ValueError("Taleo root declared a malformed locale")
    root = f"{origin}/careersection/{site}/"
    return _SearchContext(
        endpoint=root + "jobsearch.ajax",
        detail_root=root + "jobdetail.ftl",
        query={"lang": declared[0], "portal": ""},
        headers={},
        body={},
        page=1,
        classic=True,
    )


def _ftl_text(cell: str) -> str:
    """Decode one FTL cell: percent-encoded, with its delimiters backslash-escaped."""
    return re.sub(r"\\(.)", r"\1", unquote(cell), flags=re.DOTALL).strip()


def _record_starts(cells: list[str]) -> list[int]:
    # A record opens with its id and title twice.
    return [
        index
        for index in range(len(cells) - 3)
        if cells[index].isdigit()
        and not cells[index + 1].isdigit()
        and cells[index] == cells[index + 2]
        and cells[index + 1] == cells[index + 3]
    ]


def _classic_row(
    record: str, title: str, cells: list[str], context: _SearchContext
) -> dict[str, Any]:
    stated = [
        cell for cell in cells if cell.strip() and cell != record and cell not in {"true", "false"}
    ]
    number = next((cell for cell in stated if cell.isdigit()), "")
    if not number or not title:
        raise ValueError("Taleo classic row did not state its job number and title")
    dates = [cell for cell in stated if _LISTED_DATE.fullmatch(cell)]
    fields = [unescape(_ftl_text(cell)) for cell in stated if cell not in {number, *dates}]
    # The layout seen so far states job type, location, site, union, then department;
    # any other count leaves the cells unnamed rather than guessing which is a place.
    named = len(fields) == 5
    return {
        "id": number,
        "requisitionId": record,
        "title": title,
        "url": context.detail_root
        + "?"
        + urlencode({"job": number, "lang": context.query["lang"]}),
        "posted": dates[0] if dates else "",
        "closing": dates[1] if len(dates) > 1 else "",
        "fields": fields,
        "places": [fields[1]] if named else [],
        "employmentTypes": [fields[0]] if named else [],
    }


def _classic_rows(cells: list[str], context: _SearchContext) -> list[dict[str, Any]]:
    starts = _record_starts(cells)
    rows: dict[str, dict[str, Any]] = {}
    for start, bound in zip(starts, [*starts[1:], len(cells)], strict=True):
        # A record's cells run to its Apply cell, or to the next record in another locale.
        end = next((at for at in range(start + 4, bound) if cells[at] == "Apply"), bound)
        title = unescape(_ftl_text(cells[start + 1]))
        row = _classic_row(cells[start], title, cells[start + 4 : end], context)
        rows.setdefault(row["id"], row)
    return list(rows.values())


def _classic_request(
    fetcher: BoardTransport, context: _SearchContext, page: int
) -> PublisherSearchPage:
    query = {**_CLASSIC_PAGER, "rlPager.currentPage": page, "lang": context.query["lang"]}
    address = context.endpoint + "?" + urlencode(query)
    reply = page_markup(fetcher.get_text(address, max_bytes=MAX_API_RESPONSE_BYTES))
    listed, _separator, trailer = reply.rpartition(_SECTION)
    pairs = trailer.split(_CELL)
    paging = dict(zip(pairs[::2], pairs[1::2], strict=False))
    current, size, total = (
        paging.get(key, "")
        for key in ("rlPager.currentPage", "listRequisition.size", "listRequisition.nbElements")
    )
    if pairs[0] != "ftlerrors" or not (current.isdigit() and size.isdigit() and total.isdigit()):
        raise ValueError("Taleo classic list did not state its paging")
    if paging["ftlerrors"].strip():
        raise ValueError("Taleo classic list answered with an error")
    if int(current) != page:
        raise ValueError("Taleo search did not return the requested page")
    if int(size) <= 0:
        raise ValueError("Taleo search did not state a positive page size")
    rows = _classic_rows(listed.split(_CELL), context)
    last_page = (int(total) + int(size) - 1) // int(size)
    return PublisherSearchPage(rows, page + 1 if rows and page < last_page else None, int(total))


def _classic_detail(fetcher: BoardTransport, row: dict[str, Any]) -> Mapping[str, Any] | None:
    page = page_markup(fetcher.get_text(str(row["url"]), max_bytes=MAX_API_RESPONSE_BYTES))
    history = html.fromstring(page).xpath('//input[@id="initialHistory"]/@value')
    if len(history) != 1:
        return None
    parts: list[str] = []
    for cell in str(history[0]).split(_CELL):
        part = _ftl_text(cell[len(_PART) :]) if cell.startswith(_PART) else ""
        # The page states each description part twice in a row.
        if part and (not parts or parts[-1] != part):
            parts.append(part)
    if not parts:
        return None
    places = list(row.get("places") or [])
    return {
        "title": str(row.get("title") or ""),
        "description": "\n".join(parts),
        "places": places,
        "places_complete": bool(places),
        "employment_types": list(row.get("employmentTypes") or []),
    }


def _request(
    fetcher: BoardTransport, context: _SearchContext, page: int, *, prior_total: int | None = None
) -> PublisherSearchPage:
    payload = fetcher.post_json_with_headers(
        context.endpoint, {**context.body, "pageNo": page}, headers=context.headers
    )
    if not isinstance(payload, dict) or payload.get("careerSectionUnAvailable") is not False:
        raise ValueError("Taleo search did not state an available career section")
    paging = payload.get("pagingData")
    if (
        not isinstance(paging, dict)
        or type(paging.get("currentPageNo")) is not int
        or paging["currentPageNo"] != page
    ):
        raise ValueError("Taleo search did not return the requested page")
    total = paging.get("totalCount")
    if type(total) is not int or total < 0:
        raise ValueError("Taleo search did not state a nonnegative total")
    page_size = paging.get("pageSize")
    if type(page_size) is not int or page_size <= 0:
        raise ValueError("Taleo search did not state a positive page size")
    rows = []
    for row in required_list_field(payload, "requisitionList", "Taleo"):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("contestNo"), str)
            or not row["contestNo"].strip()
            or not isinstance(row.get("jobId"), str)
        ):
            raise ValueError("Taleo search row did not state its job identifiers")
        columns, linked = row.get("column"), row.get("linkedColumn")
        if (
            not isinstance(columns, list)
            or type(linked) is not int
            or not 0 <= linked < len(columns)
            or not isinstance(columns[linked], str)
            or not columns[linked].strip()
        ):
            raise ValueError("Taleo search row did not state its linked title")
        detail_query = {
            "job": row["contestNo"],
            "lang": context.query["lang"],
            "portal": context.query["portal"],
            **context.headers,
        }
        rows.append(
            {
                **row,
                "id": row["contestNo"],
                "title": columns[linked].strip(),
                "url": context.detail_root + "?" + urlencode(detail_query),
            }
        )
    boundary_total = total or prior_total or 0
    last_page = (boundary_total + page_size - 1) // page_size
    next_page = page + 1 if rows and (boundary_total == 0 or page < last_page) else None
    return PublisherSearchPage(rows, next_page, total)


def _search(
    fetcher: BoardTransport,
    employer: str,
    source_url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    context = _config(fetcher, source_url)
    prior_total = None
    if target is not None:
        legacy_total = dict(parse_qsl(urlsplit(target.url).query)).get("__reader_total", "")
        prior_total = int(legacy_total) if legacy_total else None
        if target.detail_checkpoint is not None:
            prior_total = target.detail_checkpoint.listing_total
    if prior_total is not None and prior_total <= 0:
        prior_total = None

    closing: dict[str, str] = {}

    def request(page: int) -> PublisherSearchPage:
        nonlocal prior_total
        if context.classic:
            answer = _classic_request(fetcher, context, page)
            closing.update((str(row["id"]), str(row["closing"])) for row in answer.rows)
            return answer
        answer = _request(fetcher, context, page, prior_total=prior_total)
        if answer.total is not None and answer.total > 0:
            prior_total = answer.total
        return answer

    read = publisher_search_read(
        fetcher,
        employer,
        source_url=source_url,
        reason=_REASON,
        first_page=context.page,
        request=request,
        mapping=_MAPPING,
        detail=(
            partial(_classic_detail, fetcher) if context.classic else page_posting_detail(fetcher)
        ),
        target=target,
    )
    if not closing:
        return read
    return replace(
        read,
        listings=[
            replace(
                listing,
                facts=replace(
                    listing.facts,
                    closing_date=publisher_closing_date(
                        listing.published_description,
                        valid_through=closing.get(listing.requisition_id, ""),
                        source_url=listing.url,
                        observed_at=utc_now(),
                    ),
                ),
            )
            for listing in read.listings
        ],
    )


def _read(fetcher: BoardTransport, employer: str, _groups: dict[str, str], url: str) -> SourceRead:
    return _search(fetcher, employer, url)


def _scope(url: str) -> tuple[str, str, str, str] | None:
    match = TALEO_BOARD.fullmatch(url) or TALEO_JOB.fullmatch(url)
    if match is None:
        return None
    query = _query(url)
    return (
        match["host"].casefold(),
        match["site"].casefold(),
        query.get("lang", ""),
        query.get("portal", ""),
    )


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    if _scope(target.url) != _scope(target.source_url) or _scope(target.source_url) is None:
        return unreadable_continuation(target, "invalid_taleo_search_scope")
    return _search(fetcher, employer, target.source_url, target)


def _continue_detail(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    checkpoint = target.detail_checkpoint
    if TALEO_JOB.fullmatch(target.url) is None or checkpoint is None:
        return unreadable_continuation(target, "invalid_taleo_detail_scope")
    try:
        context = _config(fetcher, target.source_url)
    except HostHeldError as held:
        return held_continuation(held_target(target, held))
    source_scope = _scope(target.source_url)
    if (
        source_scope is None
        or _scope(target.url) != (*source_scope[:2], context.query["lang"], context.query["portal"])
        or checkpoint.board_url != target.url
        or checkpoint.pending_ids != (_query(target.url).get("job", ""),)
    ):
        return unreadable_continuation(target, "invalid_taleo_detail_scope")
    return publisher_search_detail(
        fetcher,
        employer,
        target,
        mapping=_MAPPING,
        detail=(
            partial(_classic_detail, fetcher) if context.classic else page_posting_detail(fetcher)
        ),
    )


def _root(url: str, _match: re.Match[str]) -> str:
    parsed = urlsplit(url)
    query = _query(url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.replace("jobdetail.ftl", "jobsearch.ftl"),
            urlencode(
                {
                    key: value
                    for key, value in query.items()
                    if key in {"lang", "portal", "tz", "tzname"}
                }
            ),
            "",
        )
    )


PLATFORM = BoardPlatform(
    name="taleo",
    board_patterns=(TALEO_BOARD,),
    adapter=_read,
    job_patterns=(TALEO_JOB,),
    source_roots=((TALEO_JOB, _root),),
    identity_rules=(
        (
            TALEO_BOARD,
            lambda match: "/".join(
                (match["host"], match["site"], _query(match[0]).get("portal", ""))
            ).casefold(),
        ),
    ),
    continuations={_REASON: _continue, "taleo_search_detail": _continue_detail},
    label="Taleo",
)
