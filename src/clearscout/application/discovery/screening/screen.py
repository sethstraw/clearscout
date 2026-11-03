"""The full screening of one discovered posting."""

from __future__ import annotations

from clearscout.application.discovery.screening.arrangement import arrangement_finding
from clearscout.application.discovery.screening.compensation import compensation_finding
from clearscout.application.discovery.screening.employer import (
    employer_finding,
    excluded_employer,
    seniority_finding,
)
from clearscout.application.discovery.screening.employment import employment_finding
from clearscout.application.discovery.screening.findings import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.application.discovery.screening.location import place_finding
from clearscout.application.discovery.screening.titles import screen_discovery_title
from clearscout.domain.search_parameters import (
    JobSearchParameters,
)
from clearscout.domain.staged_facts import StagedListingFacts


def screen_discovery(
    employer: str,
    title: str,
    location: str,
    parameters: JobSearchParameters,
    searched: list[str],
    facts: StagedListingFacts,
    summary: str = "",
    *,
    source_employer: str = "",
) -> DiscoveryScreening:
    """Return typed findings for every configured boundary staged facts can answer."""
    identity_trusted = facts.publisher_vouched
    # The employer that registered the board is operator-configured, so its
    # exclusion refuses even when the posting's own identity is unvouched.
    source_excluded = excluded_employer(source_employer, parameters)
    location_finding = (
        place_finding(location, facts.locations, parameters, facts.arrangements)
        if identity_trusted
        else ScreeningFinding(
            ScreeningDimension.LOCATION,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.LOCATION_UNVERIFIED,
        )
    )
    location_key = (
        location_finding.subject if location_finding.code is ScreeningCode.LOCATION_MATCH else None
    )
    optional = (
        ScreeningFinding(
            ScreeningDimension.EMPLOYER,
            ScreeningStanding.REFUSE,
            ScreeningCode.EMPLOYER_EXCLUDED,
            source_excluded,
        )
        if source_excluded is not None
        else employer_finding(employer, parameters)
        if identity_trusted
        else ScreeningFinding(
            ScreeningDimension.EMPLOYER,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.EMPLOYER_UNVERIFIED,
        )
        if parameters.employers.excluded_employers
        else None,
        seniority_finding(title, parameters)
        if identity_trusted
        else ScreeningFinding(
            ScreeningDimension.SENIORITY,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.SENIORITY_UNVERIFIED,
        )
        if parameters.seniority.stated
        else None,
        employment_finding(facts.employment_types, parameters),
        arrangement_finding(
            facts.arrangements,
            parameters,
            location_key,
            identity_trusted=identity_trusted,
            location_fact=facts.locations,
            location=location,
        ),
        compensation_finding(facts.compensation, parameters, location_key),
    )
    # A rescreen has no listing summary; the focus terms it stated were stored at staging.
    title_finding = screen_discovery_title(
        title,
        facts.role_aliases,
        parameters,
        searched,
        summary or "\n".join(facts.focus_terms.values),
        employer=employer if identity_trusted else "",
        source_employer=source_employer,
    )
    # A title nobody vouched for may admit a row, since a match is still evidence of
    # one, but it may never refuse a row.
    if not identity_trusted and title_finding.standing is ScreeningStanding.REFUSE:
        title_finding = ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.TITLE_UNVERIFIED,
        )
    return DiscoveryScreening(
        findings=(
            title_finding,
            location_finding,
            *(finding for finding in optional if finding is not None),
        ),
        location_fact_complete=facts.locations.complete,
    )
