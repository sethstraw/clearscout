"""Grade one guided campaign attempt by the checks the campaign runner itself applies.

An attempt passes when it finished, stayed within its search cap, served every branch
it was given as the runner credits branches, named no excluded title or employer in a
query, and picked no URL the published fetch policy refuses. When the solver recorded
admission's reads of the picks, a pick also fails for repeating an earlier pick or for
a publisher read naming an excluded title or employer. Every other pick measure warns
or is reported only as a rate; each read title falls in one of three bins: plausible,
a role-family conflict, or a mismatch.
"""

from __future__ import annotations

from typing import Any

from clearscout.application.discovery.dispositions import BODY_REFUSAL_CATEGORIES
from clearscout.application.discovery.guided_campaign_admission import (
    NOT_CAPTURED_DETAIL,
    classify_url,
)
from clearscout.application.discovery.posting_identity import (
    posting_identity_keys,
    posting_url_key,
)
from clearscout.application.discovery.screening import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    screen_discovery_title,
)
from clearscout.application.discovery.screening.employer import excluded_employer
from clearscout.domain.campaigns import CapturedUrlClassification, CapturedUrlOutcome
from clearscout.domain.identity import mentions, normalize_text
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.search_parameters.roles import title_excluded
from clearscout.domain.staged_facts import PublishedFact

UNFINISHED = "unfinished"
OVER_CAP = "search_count_out_of_bounds"
UNCREDITED = "branch_uncredited"
EXCLUDED = "excluded_term"
REFUSED_PICK = "refused_pick"
UNATTRIBUTABLE = "unattributable_search"
NON_POSTING_PICK = "non_posting_pick"
DUPLICATE_PICK = "duplicate_pick"
EXCLUDED_TITLE_PICK = "excluded_title_pick"
EXCLUDED_EMPLOYER_PICK = "excluded_employer_pick"
UNGROUNDED_PICK = "ungrounded_pick"
CLOSED_PICK = "closed_pick"
UNREAD_PICK = "unread_pick"
BOARD_UNPROVEN = "board_unproven"
TITLE_MISFIT = "title_misfit"
PLACE_MISFIT = "place_misfit"
HELD_OUT_PICK = "held_out_pick"
PICK_READS_FAILED = "pick_reads_failed"

# Outcomes admission reaches only after reading one active posting with a title.
_OPEN_POSTING = frozenset(
    {CapturedUrlOutcome.STAGED, CapturedUrlOutcome.SCREENED_OUT, CapturedUrlOutcome.HELD_OUT}
)
_ADMITTED = frozenset({CapturedUrlOutcome.STAGED, CapturedUrlOutcome.BOARD_ADMITTED})
_ALREADY_HELD = frozenset({CapturedUrlOutcome.KNOWN_POSTING, CapturedUrlOutcome.REGISTERED_BOARD})
_PLACE_DIMENSIONS = frozenset({ScreeningDimension.LOCATION, ScreeningDimension.ARRANGEMENT})
# Hold-outs refuse these two place codes outside the body refusal categories.
_PLACE_REFUSALS = BODY_REFUSAL_CATEGORIES | {
    ScreeningCode.LOCATION_INELIGIBLE.value,
    ScreeningCode.ARRANGEMENT_REMOTE_ONLY_SITES.value,
}
PLAUSIBLE, CONFLICT, MISMATCH = "plausible", "conflict", "mismatch"
_TITLE_BINS = {
    ScreeningCode.TITLE_MATCH: PLAUSIBLE,
    ScreeningCode.TITLE_NEAR_MATCH: PLAUSIBLE,
    ScreeningCode.TITLE_EXTRA_ROLE_WORDS: PLAUSIBLE,
    ScreeningCode.TITLE_ROLE_FAMILY_REVIEW: PLAUSIBLE,
    ScreeningCode.TITLE_SPECIALIST_IC_REVIEW: PLAUSIBLE,
    ScreeningCode.TITLE_DOMAIN_REVIEW: PLAUSIBLE,
    ScreeningCode.TITLE_UNRESTRICTED: PLAUSIBLE,
    # An unvouched title admission could not refuse, so it stays undecided.
    ScreeningCode.TITLE_UNVERIFIED: PLAUSIBLE,
    ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT: CONFLICT,
    ScreeningCode.TITLE_MISMATCH: MISMATCH,
    ScreeningCode.TITLE_EXCLUDED: MISMATCH,
    ScreeningCode.TITLE_TEST_REQUISITION: MISMATCH,
}
_BIN_ORDER = (PLAUSIBLE, CONFLICT, MISMATCH)
# Pooled run rates below these floors are warned in the run header, never failed.
RATE_FLOORS = {"admitted": 0.70}


def campaign_case(sample: dict[str, Any]) -> JobSearchParameters:
    """Return a row's parameter set, refusing a row that compiles no title branch to check."""
    case = sample.get("id")
    if not str(sample.get("why") or "").strip():
        raise ValueError(f"sample {case!r} needs a why")
    case_input = sample.get("input")
    if not isinstance(case_input, dict) or not isinstance(case_input.get("parameters"), dict):
        raise ValueError(f"sample {case!r} needs input.parameters")
    parameters = JobSearchParameters.model_validate(case_input["parameters"])
    if not parameters.seniority.tracked_titles:
        raise ValueError(f"sample {case!r} names no target title")
    return parameters


def _excluded_hits(queries: list[str], parameters: JobSearchParameters) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    employers = parameters.employers.excluded_employers
    for query in queries:
        if term := title_excluded(query, parameters.seniority.excluded_titles):
            hits.append({"query": query, "term": term})
        for employer in employers:
            if mentions(normalize_text(query), normalize_text(employer)):
                hits.append({"query": query, "term": employer})
    return hits


def _pick_rows(solver_output: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for kind in ("postings", "boards"):
        for pick in solver_output.get(kind) or []:
            classification, _ = classify_url(
                str(pick["url"]),
                known_source_roots=frozenset(),
                known_posting_fingerprints=frozenset(),
            )
            rows.append({**pick, "kind": kind, "classification": classification.value})
    return rows


def _place_refused(read: dict[str, Any]) -> bool:
    return any(
        finding["dimension"] in _PLACE_DIMENSIONS and finding["code"] in _PLACE_REFUSALS
        for finding in read.get("findings") or []
    )


def _title_finding(
    read: dict[str, Any], titles: list[str], parameters: JobSearchParameters
) -> ScreeningFinding:
    """Screen the read title against one branch's titles with the facts admission screened."""
    vouched = bool(read.get("publisher_vouched"))
    finding = screen_discovery_title(
        str(read.get("title") or ""),
        PublishedFact(values=tuple(read.get("role_aliases") or ())),
        parameters,
        titles,
        "\n".join(read.get("focus_terms") or ()),
        employer=str(read.get("employer") or "") if vouched else "",
    )
    # Admission lets an unvouched title admit a row but never refuse one.
    if not vouched and finding.standing is ScreeningStanding.REFUSE:
        return ScreeningFinding(
            ScreeningDimension.TITLE, ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_UNVERIFIED
        )
    return finding


def title_bin(finding: ScreeningFinding) -> str:
    """Return the plausible, conflict or mismatch bin of one title finding."""
    binned = _TITLE_BINS.get(finding.code)
    if binned is None:
        raise ValueError(f"no title bin for screening code {finding.code.value!r}")
    return binned


def _title_reading(
    read: dict[str, Any], claimed: list[list[str]], parameters: JobSearchParameters
) -> tuple[bool, str | None]:
    """Return whether the title is excluded and its most favorable bin, None with no branch."""
    findings = [_title_finding(read, titles, parameters) for titles in claimed or [[]]]
    excluded = any(finding.code is ScreeningCode.TITLE_EXCLUDED for finding in findings)
    if not claimed:
        return excluded, None
    return excluded, min((title_bin(finding) for finding in findings), key=_BIN_ORDER.index)


def _grade_open_posting(
    pick: dict[str, Any],
    read: dict[str, Any],
    titles: dict[str, list[str]],
    parameters: JobSearchParameters,
    grade: dict[str, Any],
) -> None:
    claimed = [titles[key] for key in pick.get("branch_keys") or [] if titles.get(key)]
    excluded, binned = _title_reading(read, claimed, parameters)
    grade["title_bin"] = binned
    grade["place_fit"] = not _place_refused(read)
    if excluded:
        grade["failed"].append(EXCLUDED_TITLE_PICK)
    if binned == MISMATCH:
        grade["warned"].append(TITLE_MISFIT)
    if not grade["place_fit"]:
        grade["warned"].append(PLACE_MISFIT)
    elif read["outcome"] == CapturedUrlOutcome.HELD_OUT.value:
        grade["warned"].append(HELD_OUT_PICK)


def _grade_read(
    pick: dict[str, Any],
    read: dict[str, Any],
    titles: dict[str, list[str]],
    parameters: JobSearchParameters,
    grade: dict[str, Any],
) -> None:
    outcome = CapturedUrlOutcome(read["outcome"])
    grade["admitted"] = outcome in _ADMITTED
    grade["novel"] = outcome not in _ALREADY_HELD
    if not grade["novel"]:
        return
    if excluded_employer(str(read.get("employer") or ""), parameters) is not None:
        grade["failed"].append(EXCLUDED_EMPLOYER_PICK)
    if pick["kind"] == "boards":
        if outcome is not CapturedUrlOutcome.BOARD_ADMITTED:
            grade["warned"].append(BOARD_UNPROVEN)
        return
    grade["open"] = outcome in _OPEN_POSTING
    if not grade["open"]:
        closed = read.get("posting_status") == "closed"
        grade["warned"].append(CLOSED_PICK if closed else UNREAD_PICK)
        return
    _grade_open_posting(pick, read, titles, parameters, grade)


def _pick_grades(
    picks: list[dict[str, Any]],
    reads: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    parameters: JobSearchParameters,
) -> list[dict[str, Any]]:
    """Grade each pick from admission's recorded read of it; a refused pick has none."""
    by_key = {posting_url_key(str(read["url"])): read for read in reads}
    titles = {str(branch["key"]): list(branch.get("titles") or []) for branch in branches}
    seen: set[str] = set()
    grades = []
    for pick in picks:
        read = by_key.get(posting_url_key(str(pick["url"])))
        grade: dict[str, Any] = {
            "url": pick["url"],
            "kind": pick["kind"],
            "branch_keys": pick.get("branch_keys") or [],
            "outcome": None if read is None else read["outcome"],
            "title": "" if read is None else read.get("title", ""),
            "employer": "" if read is None else read.get("employer", ""),
            "failed": [],
            "warned": [],
        }
        identities = posting_identity_keys(str(pick["url"]))
        duplicate = bool(identities & seen)
        seen |= identities
        if duplicate:
            grade["failed"].append(DUPLICATE_PICK)
        elif read is not None:
            grade["grounded"] = read.get("detail") != NOT_CAPTURED_DETAIL
            if grade["grounded"]:
                _grade_read(pick, read, titles, parameters, grade)
            else:
                grade["warned"].append(UNGROUNDED_PICK)
        grades.append(grade)
    return grades


def _reader_failed(grades: list[dict[str, Any]]) -> bool:
    """Return whether every posting admission tried to read failed short of a closure."""
    tried = [grade for grade in grades if grade.get("open") is not None]
    return bool(tried) and all(
        grade["outcome"] == CapturedUrlOutcome.UNREADABLE.value and UNREAD_PICK in grade["warned"]
        for grade in tried
    )


def _rate(grades: list[dict[str, Any]], measure: str) -> dict[str, int]:
    judged = [grade[measure] for grade in grades if grade.get(measure) is not None]
    return {"passed": sum(1 for value in judged if value), "total": len(judged)}


def _bin_rate(postings: list[dict[str, Any]], name: str) -> dict[str, int]:
    binned = [grade["title_bin"] for grade in postings if grade.get("title_bin") is not None]
    return {"passed": binned.count(name), "total": len(binned)}


def _pick_quality(grades: list[dict[str, Any]]) -> dict[str, Any]:
    """Return precision-style rates over the graded picks, with every flagged pick."""
    postings = [grade for grade in grades if grade["kind"] == "postings"]
    boards = [grade for grade in grades if grade["kind"] == "boards"]
    return {
        "rates": {
            "admitted": {
                "passed": sum(1 for grade in grades if grade.get("admitted")),
                "total": len(grades),
            },
            "grounded": _rate(grades, "grounded"),
            "novel": _rate(grades, "novel"),
            "open_posting": _rate(postings, "open"),
            "board_proven": _rate(boards, "admitted"),
            **{f"title_{name}": _bin_rate(postings, name) for name in _BIN_ORDER},
            "place_fit": _rate(postings, "place_fit"),
        },
        "floors": RATE_FLOORS,
        "flagged": [grade for grade in grades if grade["failed"] or grade["warned"]],
    }


def _rates_text(quality: dict[str, Any] | None) -> str:
    if quality is None:
        return ""
    rates = quality["rates"].items()
    return "".join(f" {name}={rate['passed']}/{rate['total']}" for name, rate in rates)


def campaign_attempt() -> Any:
    """Return the scorer for one guided campaign attempt's searches and picks."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        parameters = campaign_case(sample)
        queries = [str(query) for query in solver_output.get("queries") or []]
        branches = list(solver_output.get("branches") or [])
        count = int(solver_output.get("search_count") or 0)
        cap = int(solver_output.get("search_cap") or 0)
        excluded = _excluded_hits(queries, parameters)
        refused = list(solver_output.get("refused_picks") or [])
        picks = _pick_rows(solver_output)
        uncredited = [branch["label"] for branch in branches if not branch["queries"]]
        failed: list[str] = []
        if solver_output.get("status") != "complete":
            failed.append(UNFINISHED)
        if not 1 <= count <= cap:
            failed.append(OVER_CAP)
        failed += [UNCREDITED] * len(uncredited)
        failed += [EXCLUDED] * len(excluded)
        failed += [REFUSED_PICK] * len(refused)
        warned = [UNATTRIBUTABLE] * int(solver_output.get("unattributable_searches") or 0)
        warned += [
            NON_POSTING_PICK
            for pick in picks
            if pick["kind"] == "postings"
            and pick["classification"] == CapturedUrlClassification.OTHER.value
        ]
        if solver_output.get("reads_error"):
            warned.append(PICK_READS_FAILED)
        reads = solver_output.get("reads")
        grades = _pick_grades(picks, reads, branches, parameters) if isinstance(reads, list) else []
        failed += [label for grade in grades for label in grade["failed"]]
        warned += [label for grade in grades for label in grade["warned"]]
        if _reader_failed(grades):
            warned.append(PICK_READS_FAILED)
        quality = _pick_quality(grades) if isinstance(reads, list) else None
        credited = len(branches) - len(uncredited)
        return {
            "value": not failed,
            "requirements": {"total": len(branches), "passed": credited},
            "checks": {"failed": failed, "warned": warned},
            "metadata": {
                "status": solver_output.get("status"),
                "search_count": count,
                "search_cap": cap,
                "branches": branches,
                "uncredited_branches": uncredited,
                "excluded_hits": excluded,
                "refused_picks": refused,
                "picks": picks,
                "pick_quality": quality,
            },
            "explanation": (
                f"searches={count}/{cap} branches={credited}/{len(branches)} "
                f"excluded={len(excluded)} refused_picks={len(refused)} picks={len(picks)}"
                f"{_rates_text(quality)}"
            ),
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = campaign_case  # type: ignore[attr-defined]
    return score
