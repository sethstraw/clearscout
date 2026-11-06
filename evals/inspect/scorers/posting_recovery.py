"""Grade one posting recovery answer against the case's known posting addresses.

A case passes when the outcome is candidate_ready and the picked candidate is an expected
URL under the program's exact URL key, or, for a case that names hosts, sits on one of them.
A case expecting neither passes only when no candidate survived validation.
"""

from __future__ import annotations

from typing import Any

from clearscout.domain.identity import exact_url_key, url_host

URL_HIT = "url"
HOST_HIT = "host"
NO_HIT = "none"
CITED_NOT_PICKED = "expected_cited_not_picked"


def expected_targets(sample: dict[str, Any]) -> dict[str, list[str]]:
    """Return a row's expected URLs and hosts, refusing a label that cannot be matched."""
    case = sample.get("id")
    if not str(sample.get("why") or "").strip():
        raise ValueError(f"sample {case!r} needs a why")
    target = sample.get("input")
    if not isinstance(target, dict) or not exact_url_key(str(target.get("known_url") or "")):
        raise ValueError(f"sample {case!r} needs an absolute known_url")
    expect = sample.get("expect")
    if not isinstance(expect, dict):
        raise ValueError(f"sample {case!r} needs an expect object")
    urls = expect.get("urls", [])
    hosts = expect.get("hosts", [])
    if not isinstance(urls, list) or not isinstance(hosts, list):
        raise ValueError(f"sample {case!r} needs urls and hosts lists")
    for url in urls:
        if not exact_url_key(str(url)):
            raise ValueError(f"sample {case!r} expects a URL the program cannot key: {url!r}")
    for host in hosts:
        if not isinstance(host, str) or not host or "/" in host:
            raise ValueError(f"sample {case!r} expects a malformed host: {host!r}")
    return {"urls": [str(url) for url in urls], "hosts": [host.casefold() for host in hosts]}


def _on_host(url: str, hosts: list[str]) -> bool:
    host = url_host(url)
    return any(host == expected or host.endswith(f".{expected}") for expected in hosts)


def _matched(candidates: list[dict[str, Any]], expected: dict[str, list[str]]) -> str:
    keys = {exact_url_key(url) for url in expected["urls"]}
    if any(exact_url_key(str(row["url"])) in keys for row in candidates):
        return URL_HIT
    hosts = expected["hosts"] or [url_host(url) for url in expected["urls"]]
    if any(_on_host(str(row["url"]), hosts) for row in candidates):
        return HOST_HIT
    return NO_HIT


def recovered_posting() -> Any:
    """Return the scorer for the candidates one recovery answer cited."""

    def score(sample: dict[str, Any], solver_output: dict[str, Any]) -> dict[str, Any]:
        expected = expected_targets(sample)
        candidates = list(solver_output.get("candidates") or [])
        outcome = str(solver_output.get("outcome") or "")
        wants_none = not expected["urls"] and not expected["hosts"]
        picked = solver_output.get("picked")
        matched = _matched([{"url": picked}] if picked else [], expected)
        url_only = bool(expected["urls"]) and not expected["hosts"]
        if wants_none:
            passed = not candidates and outcome == "not_found"
        else:
            # Production reads only a candidate_ready pick; other outcomes read nothing.
            hit = matched == URL_HIT or (matched == HOST_HIT and not url_only)
            passed = outcome == "candidate_ready" and hit
        failed = [] if passed else [f"miss:{outcome}" if not wants_none else "unexpected_candidate"]
        warned = [HOST_HIT + "_only"] if passed and matched == HOST_HIT else []
        if not passed and not wants_none and _matched(candidates, expected) == URL_HIT:
            warned.append(CITED_NOT_PICKED)
        return {
            "value": passed,
            "checks": {"failed": failed, "warned": warned},
            "metadata": {
                "outcome": outcome,
                "matched": matched,
                "picked": picked,
                "candidates": candidates,
                "hosted_searches": solver_output.get("hosted_searches", 0),
                "error": solver_output.get("error", ""),
            },
            "explanation": (
                f"outcome={outcome} matched={matched} candidates={len(candidates)} "
                f"searches={solver_output.get('hosted_searches', 0)}"
            ),
        }

    # The runner checks every row's label with this before it calls a solver.
    score.validate_sample = expected_targets  # type: ignore[attr-defined]
    return score
