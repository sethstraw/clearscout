"""ADP Workforce Now career centers: paged requisitions joined to each one's detail."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tailorcv.infrastructure.web_fetch.board_mappings import (
    ADP_MAPPING,
    adp_item_id,
    adp_requisition,
    adp_requisition_url,
    adp_requisitions_url,
)
from tailorcv.infrastructure.web_fetch.board_platform import HOST_START, BoardPlatform, host_match
from tailorcv.infrastructure.web_fetch.board_reads import (
    required_list_field,
    short_of_total,
    unreadable_continuation,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport


def _adp_query(key: str, group: str, value: str) -> str:
    """Return a lookahead for one query parameter of an ADP career page, in any order."""
    separator = r"&(?:amp;|#0*38;|#x0*26;)?+"
    # An encoded ampersand can contain a hash before the URL fragment boundary.
    prefix = rf"""(?:{separator}|[^&#\s"'<>])*?"""
    return rf"(?=(?:{prefix}{separator})?{key}=(?P<{group}>{value}))"


# A client can publish distinct boards under separate `ccId` career centers.
_ADP_PAGE = (
    HOST_START + r"workforcenow\.adp\.com/mascsr/default/mdf/recruitment/recruitment\.html\?"
    rf"{_adp_query('cid', 'tenant', r'[0-9a-f-]{36}')}"
    rf"(?:{_adp_query('ccId', 'career_center', r'[a-z0-9_-]+')})?"
    rf"(?:{_adp_query('lang', 'lang', r'[a-z]{2}_[a-z]{2}')})?"
)
ADP_BOARD = re.compile(_ADP_PAGE, re.IGNORECASE)
ADP_JOB = re.compile(_ADP_PAGE + _adp_query("jobId", "job", r"\d+_\d+"), re.IGNORECASE)

ADP_PAGE = 20
ADP_MAX_PAGES = 100
_PENDING = "adp_detail_rows_pending"


def _board_url(groups: dict[str, str]) -> str:
    """Return the career-center root shared by source URLs and detail checkpoints."""
    lang = f"&lang={groups['lang']}" if groups.get("lang") else ""
    center = f"&ccId={groups['career_center']}" if groups.get("career_center") else ""
    return (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        f"?cid={groups['tenant']}{center}{lang}"
    )


def _enumeration_note(
    total: int | None, found: int, *, empty: bool, capped: bool, repeated: bool
) -> str:
    """Return why the requisition enumeration cannot certify the whole career center."""
    if empty and total is None:
        return (
            "The ADP career center returned no requisitions without a valid total. "
            "Check the career-center address and retry this source."
        )
    if capped:
        return f"The ADP career center filled all {ADP_MAX_PAGES} pages and states no total."
    if repeated:
        return "The ADP career center repeated a full requisition page."
    if total is not None and found < total:
        return f"The ADP career center states {total} requisitions and returned {found}."
    return ""


def _root(_url: str, match: re.Match[str]) -> str:
    return _board_url(match.groupdict(default=""))


def _read(
    fetcher: BoardTransport,
    employer: str,
    groups: dict[str, str],
    _url: str,
    *,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    """Read an ADP career center's requisitions and each one's body from its detail.

    Pages run until a short page or the stated ``meta.totalNumber``; a read finding fewer
    distinct requisitions than that total is not whole.
    """
    cid, lang = groups.get("tenant", ""), groups.get("lang", "")
    career_center = groups.get("career_center", "")
    payload = fetcher.get_json(
        adp_requisitions_url(cid, lang, top=ADP_PAGE, skip=1, career_center=career_center)
    )
    rows = required_list_field(payload, "jobRequisitions", "ADP")
    declared_total = (payload.get("meta") or {}).get("totalNumber")
    total = declared_total if type(declared_total) is int and declared_total >= 0 else None
    seen = {adp_item_id(row) for row in rows} - {""}
    page = rows
    capped = False
    repeated = False
    for pages in range(1, ADP_MAX_PAGES):
        if len(page) < ADP_PAGE or (isinstance(total, int) and len(seen) >= total):
            break
        page = required_list_field(
            fetcher.get_json(
                adp_requisitions_url(
                    cid,
                    lang,
                    top=ADP_PAGE,
                    skip=1 + pages * ADP_PAGE,
                    career_center=career_center,
                )
            ),
            "jobRequisitions",
            "ADP",
        )
        fresh = [row for row in page if adp_item_id(row) not in seen]
        if not fresh:
            repeated = len(page) >= ADP_PAGE
            break
        rows.extend(fresh)
        seen.update(adp_item_id(row) for row in fresh)
    else:
        capped = not isinstance(total, int)
    found = len(seen - {""})
    enumeration_note = _enumeration_note(
        total, found, empty=not rows, capped=capped, repeated=repeated
    )
    read = fetcher.detailed_read(
        employer,
        rows,
        mapping=ADP_MAPPING,
        detail=lambda item_id: adp_requisition(
            fetcher.get_json(adp_requisition_url(cid, item_id, lang, career_center=career_center))
        ),
        groups={"tenant": cid, "career_center": career_center, "lang": lang},
        row_id=adp_item_id,
        missing_fails=True,
        resume=(_board_url(groups), _PENDING),
        resume_target=target,
        enumeration_complete=not enumeration_note,
    )
    return short_of_total(read, enumeration_note) if enumeration_note else read


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    """Re-list the career center and resume its unresolved stable detail identities."""
    match = host_match(ADP_BOARD, target.url)
    if match is None:
        return unreadable_continuation(target, "invalid_adp_cursor")
    return _read(fetcher, employer, match.groupdict(default=""), target.url, target=target)


PLATFORM = BoardPlatform(
    name="adp",
    label="ADP",
    board_patterns=(ADP_BOARD,),
    adapter=_read,
    job_patterns=(ADP_JOB,),
    source_roots=((ADP_JOB, _root),),
    identity_rules=(
        (
            ADP_BOARD,
            lambda match: "/".join(
                value for value in (match.group("tenant"), match.group("career_center")) if value
            ).casefold(),
        ),
    ),
    single_tenant=False,
    continuations={_PENDING: _continue},
)
