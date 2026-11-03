"""Employment type findings."""

from __future__ import annotations

import re

from clearscout.application.discovery.screening.findings import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import (
    JobSearchParameters,
)
from clearscout.domain.staged_facts import PublishedFact
from clearscout.shared.text import sanitize

_EMPLOYMENT_CATEGORIES = {
    "apprenticeship",
    "contract",
    "fixed term",
    "freelance",
    "full time",
    "internship",
    "part time",
    "permanent",
    "seasonal",
    "temporary",
}


# Each written form of a category, matched as whole words in normalized text.
_EMPLOYMENT_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("full time", re.compile(r"\bfull ?time\b")),
    ("part time", re.compile(r"\bpart ?time\b")),
    ("fixed term", re.compile(r"\bfixed ?term\b")),
    ("contract", re.compile(r"\b(?:contract(?:or|s)?|consultant)\b")),
    ("temporary", re.compile(r"\btemp(?:orary)?\b")),
    ("internship", re.compile(r"\b(?:intern(?:s|ship)?|co ?op)\b")),
    ("permanent", re.compile(r"\bpermanent\b")),
    ("seasonal", re.compile(r"\bseasonal\b")),
    ("freelance", re.compile(r"\bfreelance(?:r)?\b")),
    ("apprenticeship", re.compile(r"\bapprentice(?:ship)?\b")),
)


# Permanence says how long a job lasts, not its hours, so it never refuses an hours type.
_TENURE_ONLY = frozenset({"permanent"})


# "Contract to hire" and "Contract with option to FTE" may become the permanent role a
# person wants, so a contract or temp label naming a later hire names no category.
_FIXED_TERM = re.compile(r"\b(?:contract(?:or|s)?|temp(?:orary)?|c2h|cth)\b")


_CONVERTS = re.compile(r"\b(?:hire|perm(?:anent)?|fte|full ?time|conver\w*|c2h|cth)\b")


def _employment_categories(value: str) -> set[str]:
    """Map a publisher label to every employment category users configure that it names."""
    normalized = normalize_text(sanitize(value))
    if _FIXED_TERM.search(normalized) and _CONVERTS.search(normalized):
        return set()
    return {category for category, form in _EMPLOYMENT_FORMS if form.search(normalized)}


def _configured_employment_categories(values: list[str]) -> set[str]:
    return {
        category
        for value in values
        if value.strip()
        for category in (_employment_categories(value) or {normalize_text(value)})
    }


def employment_finding(
    fact: PublishedFact, parameters: JobSearchParameters
) -> ScreeningFinding | None:
    """Screen recognized publisher employment categories against configured categories."""
    employment = parameters.employment
    allowed = _configured_employment_categories(employment.types)
    excluded = _configured_employment_categories(employment.excluded_types)
    if not allowed and not excluded:
        return None
    if not fact.complete:
        return ScreeningFinding(
            ScreeningDimension.EMPLOYMENT_TYPE,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYMENT_UNKNOWN,
        )
    # One key per stated value, formed the way a configured value is, so the two compare.
    found = _configured_employment_categories(list(fact.values))
    if not found:
        return ScreeningFinding(
            ScreeningDimension.EMPLOYMENT_TYPE,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYMENT_UNKNOWN,
        )
    standing, code, subject = _employment_decision(found, allowed, excluded)
    return ScreeningFinding(ScreeningDimension.EMPLOYMENT_TYPE, standing, code, subject)


def _employment_decision(
    found: set[str], allowed: set[str], excluded: set[str]
) -> tuple[ScreeningStanding, ScreeningCode, str]:
    """Refuse only when every type the posting offers is refused; one open type keeps it."""
    offered = found - excluded
    if not offered:
        return (
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYMENT_EXCLUDED,
            ", ".join(sorted(found)),
        )
    matched = offered & allowed
    if matched:
        return (
            ScreeningStanding.MATCH,
            ScreeningCode.EMPLOYMENT_MATCH,
            ", ".join(sorted(matched)),
        )
    # An unrecognized label may name an accepted kind, so only known kinds refuse.
    refusing = offered - _TENURE_ONLY
    if allowed and refusing and refusing <= _EMPLOYMENT_CATEGORIES:
        return (
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYMENT_OUTSIDE,
            ", ".join(sorted(refusing)),
        )
    if allowed:
        return (
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYMENT_UNKNOWN,
            "",
        )
    return (
        ScreeningStanding.MATCH,
        ScreeningCode.EMPLOYMENT_MATCH,
        ", ".join(sorted(offered)),
    )
