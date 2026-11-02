"""Jobsyn search feeds scoped by a public RecruiTrooster site's configuration."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin, urlsplit

from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting_declaration
from tailorcv.infrastructure.web_fetch.board_platform import BoardPlatform
from tailorcv.infrastructure.web_fetch.board_reads import (
    PublisherSearchPage,
    RecoverableSearchBoard,
    page_markup,
    publisher_search_or_recovery,
)
from tailorcv.infrastructure.web_fetch.ingest.html_blocks import html_blocks

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

JOBSYN_CONFIG = re.compile(r"""["']x-origin["']\s*:\s*["'](?P<origin>[a-z0-9.-]+)["']""")
# A hosted site (dejobs.org) names only the API; its app then sends its own hostname as x-origin.
JOBSYN_HOSTED = re.compile(
    r"""["']api-url["']\s*:\s*["']https://prod-search-api\.jobsyn\.org/api/["']"""
)
_IMPORT = re.compile(r"\bfrom\s*[`\"'](\./[A-Za-z0-9_-]+\.js)[`\"']")
_API_BASE = "https://prod-search-api.jobsyn.org/api/"
_ENDPOINT = _API_BASE + "v1/solr/search"
_MODULE_LIMIT = 24
_PENDING = "jobsyn_search_page"
_MAPPING = page_posting_declaration("jobsyn")


def _module_address(source: str, reference: str) -> str:
    address = urljoin(source, reference)
    parsed, root = urlsplit(address), urlsplit(source)
    if (
        parsed.scheme != root.scheme
        or parsed.netloc != root.netloc
        or not re.fullmatch(r"/_nuxt/[A-Za-z0-9_-]+\.js", parsed.path)
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("Jobsyn search module is outside the selected public root.")
    return address


def _module(fetcher: BoardTransport, address: str) -> str:
    response = fetcher.get_text(address)
    if response.startswith("http"):
        _module_address(address, response.partition("\n")[0])
    return page_markup(response)


def _declares(text: str, *, hosted: bool) -> bool:
    # A hosted root's config names the API base; its module joins that value and the path.
    configured = hosted and re.search(r"\.public\[[`\"']api-url[`\"']\]\}v1/solr/search", text)
    return (
        bool(configured)
        or _ENDPOINT in text
        or (
            _API_BASE in text
            and re.search(r"`v1/\$\{[A-Za-z_$]+\([A-Za-z_$]+\.source\)\}/search`", text) is not None
        )
    )


def _declared_endpoint(
    fetcher: BoardTransport, address: str, fetched: dict[str, str], *, hosted: bool
) -> bool:
    if address not in fetched:
        fetched[address] = _module(fetcher, address)
    body = fetched[address]
    if _ENDPOINT in body:
        return True
    route = re.search(
        r"name\s*:\s*[`\"']jobs[`\"']\s*,\s*path\s*:\s*[`\"']/jobs/[`\"']"
        r"\s*,\s*component\s*:[^{}]*?\bimport\([`\"'](?P<module>\./[A-Za-z0-9_-]+\.js)[`\"']\)",
        body,
    )
    if route is None:
        return False
    return _imports_declare(
        fetcher, _module_address(address, route["module"]), fetched, hosted=hosted
    )


def _imports_declare(
    fetcher: BoardTransport, route: str, fetched: dict[str, str], *, hosted: bool
) -> bool:
    """Return whether the route module or its imports two levels down declare the search."""
    declarations: dict[str, str] = {}
    frontier = [route]
    # The search template and its API base may sit in an import of an import.
    for _depth in range(3):
        for name in frontier:
            if name not in fetched:
                if len(fetched) >= _MODULE_LIMIT:
                    return False
                fetched[name] = _module(fetcher, name)
            declarations[name] = fetched[name]
        if _declares("\n".join(declarations.values()), hosted=hosted):
            return True
        imported = (
            _module_address(source, name)
            for source in frontier
            for name in _IMPORT.findall(declarations[source])
        )
        frontier = [name for name in dict.fromkeys(imported) if name not in declarations]
    return False


def _declared_origin(page: str, host: str | None) -> tuple[str | None, bool]:
    """Return the x-origin a root sends and whether its config names the jobsyn API."""
    declared, hosted = JOBSYN_CONFIG.search(page), JOBSYN_HOSTED.search(page) is not None
    if "api-url" in page and not hosted:
        raise ValueError("Jobsyn root names an API other than the jobsyn search.")
    if declared is not None:
        return declared["origin"], hosted
    return (host if hosted and "x-origin" not in page else None), hosted


def _configuration(fetcher: BoardTransport, url: str, origin: str) -> tuple[str, int]:
    root = urlsplit(url)
    if origin != root.hostname:
        raise ValueError("Jobsyn tenant origin must match the selected public root.")
    if root.path not in {"/", "/jobs/", "/jobs"} or root.query:
        raise ValueError("Jobsyn selected search scope is not supported.")
    fetched = fetcher.get_text(url)
    if fetched.startswith("http") and urlsplit(fetched.partition("\n")[0]).netloc != root.netloc:
        raise ValueError("Jobsyn public root redirected outside its tenant.")
    page = page_markup(fetched)
    named, hosted = _declared_origin(page, root.hostname)
    if named != origin or '"job-folder"' not in page:
        raise ValueError("Jobsyn public root does not declare its tenant scope.")
    if not re.search(r"""\bsource\s*:\s*["']solr["']""", page):
        raise ValueError("Jobsyn root does not declare Solr search.")
    sizes = re.findall(r"\bnum_items\s*:\s*([1-9]\d*)", page)
    if len(sizes) != 1:
        raise ValueError("Jobsyn root does not declare one positive search page size.")
    modules: dict[str, str] = {}
    for script in html_blocks(page, "script"):
        source = urljoin(url, script.attrs.get("src", ""))
        if not script.attrs.get("src") or urlsplit(source).netloc != root.netloc:
            continue
        if not urlsplit(source).path.startswith("/_nuxt/"):
            continue
        address = _module_address(url, script.attrs["src"])
        if _declared_endpoint(fetcher, address, modules, hosted=hosted):
            return _ENDPOINT, int(sizes[0])
    raise ValueError("Jobsyn search endpoint is not declared by the public application.")


def _rows(records: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    result = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Jobsyn search contains an invalid posting record.")
        guid = str(record.get("guid") or "")
        location = unicodedata.normalize("NFD", str(record.get("location_exact") or ""))
        location = re.sub(r'["\u0300-\u036f\u2019+:/]', "", location)
        location = "-".join(re.findall(r"[A-Za-z0-9_]+", location)).lower()
        title_slug = str(record.get("title_slug") or "")
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", guid) or not location or not title_slug:
            raise ValueError("Jobsyn record does not declare its detail identity.")
        address = urljoin(url, f"/{location}/{title_slug}/{guid}/job/")
        result.append(
            {
                "id": guid,
                "url": address,
                "title": record.get("title_exact", ""),
                "publisher": record,
                "detail": {
                    "title": record.get("title_exact", ""),
                    "description": record.get("description", ""),
                    "employer": record.get("company_exact", ""),
                    "places": [record.get("location_exact", "")],
                },
            }
        )
    return result


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    url: str,
    *,
    target: SourceContinuationTarget | None = None,
    recovery: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Transfer full descriptions from the root-declared tenant feed."""
    origin = groups.get("origin") or urlsplit(url).hostname or ""
    endpoint, page_size = _configuration(fetcher, url, origin)

    def request(page: int) -> PublisherSearchPage:
        payload = fetcher.get_json(
            endpoint + "?" + urlencode({"page": page, "num_items": page_size}),
            headers={"x-origin": origin},
        )
        records = payload.get("jobs")
        pagination = payload.get("pagination")
        if not isinstance(records, list) or not isinstance(pagination, dict):
            raise ValueError("Jobsyn search has no valid page boundary.")
        if pagination.get("page") != page or not isinstance(pagination.get("has_more_pages"), bool):
            raise ValueError("Jobsyn search returned a conflicting page boundary.")
        return PublisherSearchPage(
            _rows(records, url),
            page + 1 if pagination["has_more_pages"] else None,
            pagination.get("total"),
        )

    return publisher_search_or_recovery(
        fetcher,
        employer,
        source_url=url,
        reason=_PENDING,
        first_page=1,
        request=request,
        mapping=_MAPPING,
        detail=lambda row: row["detail"],
        target=target,
        recovery=recovery,
    )


_BOARD = RecoverableSearchBoard(
    _read, lambda target: {"origin": urlsplit(target.source_url).hostname or ""}
)

PLATFORM = BoardPlatform(
    name="jobsyn",
    label="Jobsyn",
    board_patterns=(JOBSYN_CONFIG, JOBSYN_HOSTED),
    adapter=_read,
    continuations={_PENDING: _BOARD.resume, "jobsyn_search_detail": _BOARD.resume_detail},
)
