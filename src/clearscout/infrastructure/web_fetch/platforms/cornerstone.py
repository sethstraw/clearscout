"""Cornerstone external career sites with root-issued anonymous search context."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from clearscout.application.discovery.posting_text import (
    publisher_closing_date,
    publisher_posted_date,
)
from clearscout.domain.records import utc_now
from clearscout.domain.staged_facts import PostedDateGrade
from clearscout.infrastructure.web_fetch.board_mappings.mapper import stated_text
from clearscout.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from clearscout.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform
from clearscout.infrastructure.web_fetch.board_reads import (
    PublisherSearchPage,
    RecoverableSearchBoard,
    page_markup,
    publisher_search_or_recovery,
)

if TYPE_CHECKING:
    from clearscout.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport

CORNERSTONE_BOARD = re.compile(
    HOST_START + r"(?P<host>(?P<tenant>[a-z0-9-]+)\.csod\.com)/ux/ats/careersite/"
    r"(?P<site>\d+)/(?P<page>home|search|jobdetail)(?=[/?#]|$)"
)
# Tenants declare either id for the anonymous career-site visitor.
_ANONYMOUS_USERS = frozenset({-101, -103})
_PENDING = "cornerstone_search_page"
_EFFECTIVE = ("row", "publisher", "postingEffectiveDate")
# The search API writes this date as US month-first numeric text.
_MAPPING = page_posting_declaration(
    "cornerstone",
    posted=lambda posting: publisher_posted_date(
        stated_text(posting.record, _EFFECTIVE),
        basis=".".join(_EFFECTIVE),
        grade=PostedDateGrade.BOARD_DATE,
        month_first=True,
    ),
)


def _object(page: str, name: str) -> dict[str, Any]:
    matches = list(re.finditer(re.escape(name) + r"\s*=\s*(?=\{)", page))
    if len(matches) != 1:
        raise ValueError("Cornerstone public bootstrap is missing or ambiguous.")
    value, _ = json.JSONDecoder().raw_decode(page[matches[0].end() :])
    if not isinstance(value, dict):
        raise ValueError("Cornerstone public bootstrap must be an object.")
    return value


def _context(
    fetcher: BoardTransport, url: str, groups: dict[str, str]
) -> tuple[dict[str, Any], str]:
    fetched = fetcher.get_text(url)
    if fetched.startswith("http") and fetched.partition("\n")[0] != url:
        raise ValueError("Cornerstone bootstrap left the selected public root.")
    page = page_markup(fetched)
    context = _object(page, "csod.context")
    route = _object(page, "csodPlayerRouteInfo")
    cloud = (context.get("endpoints") or {}).get("cloud", "")
    parsed = urlsplit(cloud)
    if not (
        context.get("user") in _ANONYMOUS_USERS
        and context.get("officeUser") is None
        and context.get("package") == route.get("package") == "career-site"
        and context.get("corp") == groups["tenant"]
        and str(route.get("cid")) == groups["site"]
        and route.get("page") == context.get("page") == "home"
        and isinstance(context.get("token"), str)
        and context["token"].strip()
        and parsed.scheme == "https"
        and parsed.hostname in {"api.csod.com", "uk.api.csod.com"}
        and parsed.path == "/"
        and not parsed.query
        and not parsed.username
    ):
        raise ValueError("Cornerstone search requires the selected root's anonymous context.")
    query = parse_qs(urlsplit(url).query)
    if set(query) - {"c"}:
        raise ValueError("Cornerstone selected search filters are not supported.")
    if query.get("c", [groups["tenant"]])[0] != groups["tenant"]:
        raise ValueError("Cornerstone selected tenant conflicts with its public root.")
    return context, cloud + "rec-job-search/external/jobs"


def _body(context: dict[str, Any], site: str, page: int) -> dict[str, Any]:
    return {
        "careerSiteId": int(site),
        "careerSitePageId": int(site),
        "pageNumber": page,
        "pageSize": 25,
        "cultureId": context["cultureID"],
        "cultureName": context["cultureName"],
        "searchText": "",
        "states": [],
        "countryCodes": [],
        "cities": [],
        "placeID": "",
        "radius": None,
        "postingsWithinDays": None,
        "customFieldCheckboxKeys": [],
        "customFieldDropdowns": [],
        "customFieldRadios": [],
    }


def _rows(
    records: list[dict[str, Any]], groups: dict[str, str], expiry: dict[str, str]
) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Cornerstone search contains an invalid requisition record.")
        item = str(record.get("requisitionId") or "")
        query = urlencode({"c": groups["tenant"]})
        path = "/".join(
            ("", "ux", "ats", "careersite", groups["site"], "home", "requisition", item)
        )
        address = f"https://{groups['host']}{path}?{query}"
        places = [
            ", ".join(str(place[key]) for key in ("city", "state", "country") if place.get(key))
            for place in record.get("locations", [])
        ]
        rows.append(
            {
                "id": item,
                "url": address,
                "title": record.get("displayJobTitle", ""),
                "publisher": record,
                "detail": {
                    "title": record.get("displayJobTitle", ""),
                    "description": record.get("externalDescription", ""),
                    "places": places,
                },
            }
        )
        closing = str(record.get("postingExpirationDate") or "")
        expiry[item] = "" if closing == "-" else closing
    return rows


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    url: str,
    *,
    target: SourceContinuationTarget | None = None,
    recovery: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read only the public root's scoped anonymous external search."""
    context, endpoint = _context(fetcher, url, groups)
    expiry: dict[str, str] = {}

    def request(page: int) -> PublisherSearchPage:
        payload = fetcher.post_json_with_headers(
            endpoint,
            _body(context, groups["site"], page),
            # The public career page issues this anonymous token to every visitor.
            headers={"Authorization": "Bearer " + context["token"]},
        )
        data = payload.get("data")
        if (
            payload.get("status") != "Success"
            or not isinstance(data, dict)
            or not isinstance(data.get("requisitions"), list)
        ):
            raise ValueError("Cornerstone search returned no valid requisition list.")
        rows = _rows(data["requisitions"], groups, expiry)
        return PublisherSearchPage(rows, page + 1 if rows else None, data.get("totalCount"))

    read = publisher_search_or_recovery(
        fetcher,
        employer,
        source_url=url,
        reason=_PENDING,
        first_page=1,
        request=request,
        mapping=_MAPPING,
        detail=lambda row: row["detail"] or None,
        target=target,
        recovery=recovery,
    )
    return replace(
        read,
        listings=[
            replace(
                listing,
                facts=replace(
                    listing.facts,
                    closing_date=publisher_closing_date(
                        listing.published_description,
                        valid_through=expiry.get(listing.requisition_id, ""),
                        source_url=listing.url,
                        observed_at=utc_now(),
                    ),
                ),
            )
            for listing in read.listings
        ],
    )


def _source_groups(target: SourceContinuationTarget) -> dict[str, str]:
    """Return the public career site a retained target's search was read under."""
    match = CORNERSTONE_BOARD.search(target.source_url)
    if match is None:
        raise ValueError("Cornerstone continuation has no supported public source.")
    return match.groupdict()


_BOARD = RecoverableSearchBoard(_read, _source_groups)

PLATFORM = BoardPlatform(
    name="cornerstone",
    label="Cornerstone",
    board_patterns=(CORNERSTONE_BOARD,),
    adapter=_read,
    continuations={_PENDING: _BOARD.resume, "cornerstone_search_detail": _BOARD.resume_detail},
)
