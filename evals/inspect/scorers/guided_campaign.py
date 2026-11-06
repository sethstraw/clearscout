"""Grade one guided campaign attempt by the checks the campaign runner itself applies.

An attempt passes when it finished, stayed within its search cap, served every branch
it was given as the runner credits branches, named no excluded title or employer in a
query, and picked no URL the published fetch policy refuses.
"""

from __future__ import annotations

from typing import Any

from clearscout.application.discovery.guided_campaign_admission import classify_url
from clearscout.domain.campaigns import CapturedUrlClassification
from clearscout.domain.identity import mentions, normalize_text
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.search_parameters.roles import title_excluded

UNFINISHED = "unfinished"
OVER_CAP = "search_count_out_of_bounds"
UNCREDITED = "branch_uncredited"
EXCLUDED = "excluded_term"
REFUSED_PICK = "refused_pick"
UNATTRIBUTABLE = "unattributable_search"
NON_POSTING_PICK = "non_posting_pick"


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
            },
            "explanation": (
                f"searches={count}/{cap} branches={credited}/{len(branches)} "
                f"excluded={len(excluded)} refused_picks={len(refused)} picks={len(picks)}"
            ),
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = campaign_case  # type: ignore[attr-defined]
    return score
