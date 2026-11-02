"""Employer exclusion and seniority findings."""

from __future__ import annotations

from tailorcv.application.discovery.screening.findings import (
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from tailorcv.application.discovery.titles import (
    ladder_words,
)
from tailorcv.domain.identity import mentions
from tailorcv.domain.search_parameters import (
    LEVELS,
    JobSearchParameters,
)


def excluded_employer(employer: str, parameters: JobSearchParameters) -> str | None:
    """Return the configured exclusion the employer name mentions, if any."""
    if not employer.strip():
        return None
    return next(
        (
            item
            for item in parameters.employers.excluded_employers
            if item.strip() and mentions(employer.casefold(), item.strip().casefold())
        ),
        None,
    )


def employer_finding(employer: str, parameters: JobSearchParameters) -> ScreeningFinding | None:
    """Screen the employer name only against explicit name exclusions."""
    if not any(item.strip() for item in parameters.employers.excluded_employers):
        return None
    if not employer.strip():
        return ScreeningFinding(
            ScreeningDimension.EMPLOYER,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYER_UNKNOWN,
        )
    excluded = excluded_employer(employer, parameters)
    if excluded is not None:
        return ScreeningFinding(
            ScreeningDimension.EMPLOYER,
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYER_EXCLUDED,
            excluded,
        )
    return ScreeningFinding(
        ScreeningDimension.EMPLOYER,
        ScreeningStanding.MATCH,
        ScreeningCode.EMPLOYER_MATCH,
    )


def seniority_finding(title: str, parameters: JobSearchParameters) -> ScreeningFinding | None:
    """Keep title-inferred levels visible against the stated band without refusing a read."""
    if not parameters.seniority.stated:
        return None
    ladder = ladder_words(title)
    found = [level for level in LEVELS if level in ladder]
    if not found:
        return ScreeningFinding(
            ScreeningDimension.SENIORITY,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_UNKNOWN,
        )
    because = ""
    covered = {parameters.seniority.covers(level) for level in found}
    if covered == {True}:
        standing, code = ScreeningStanding.MATCH, ScreeningCode.SENIORITY_MATCH
    elif covered == {False}:
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.SENIORITY_OUTSIDE
        because = (
            "The title suggests a level outside the configured seniority band. "
            "Title-inferred seniority needs review."
        )
    else:
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.SENIORITY_UNKNOWN
    return ScreeningFinding(ScreeningDimension.SENIORITY, standing, code, ", ".join(found), because)
