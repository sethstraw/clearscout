"""Plain wording for a screening's refusals and unknowns."""

from __future__ import annotations

from clearscout.application.discovery.screening import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningFinding,
    ScreeningStanding,
)
from clearscout.application.job_search.models import DiscoveryRecommendation
from clearscout.domain.search_parameters import JobSearchParameters

REFUSAL_TEXT = {
    ScreeningCode.TITLE_EXCLUDED: "Your current settings exclude titles containing {subject}.",
    ScreeningCode.TITLE_TEST_REQUISITION: (
        "The employer marks this posting as a test requisition: {subject}."
    ),
    ScreeningCode.TITLE_MISMATCH: "The title no longer matches any current target title.",
    ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT: (
        "The title names a different role family than {subject}."
    ),
    ScreeningCode.LOCATION_OUTSIDE: (
        "The staged location is outside your current places: {subject}."
    ),
    ScreeningCode.LOCATION_INELIGIBLE: "The saved place is marked ineligible.",
    ScreeningCode.EMPLOYER_EXCLUDED: "The employer is excluded by the current settings.",
    ScreeningCode.SENIORITY_OUTSIDE: "The stated level is outside the current seniority band.",
    ScreeningCode.EMPLOYMENT_EXCLUDED: "The published employment type is explicitly excluded.",
    ScreeningCode.EMPLOYMENT_OUTSIDE: "The published employment type is outside the allowed types.",
    ScreeningCode.ARRANGEMENT_REFUSED: "The published work arrangement is refused for {subject}.",
    ScreeningCode.ARRANGEMENT_REMOTE_ONLY_SITES: (
        "No remote option was published, and every named site is a place you accept only as remote."
    ),
    ScreeningCode.ARRANGEMENT_OFFICE_LIMIT: (
        "The published office cadence exceeds the current limit."
    ),
    ScreeningCode.COMPENSATION_BELOW_MINIMUM: (
        "The published compensation maximum is below the configured minimum."
    ),
}


UNKNOWN_TEXT = {
    ScreeningCode.TITLE_MISMATCH: "The title does not match a current target title.",
    ScreeningCode.TITLE_UNVERIFIED: "The publisher has not confirmed the staged title.",
    ScreeningCode.TITLE_ROLE_FAMILY_REVIEW: (
        "The title shares a role family with {subject} and needs a person to judge the work."
    ),
    ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT: (
        "The title names a different role family than {subject}."
    ),
    ScreeningCode.TITLE_EXTRA_ROLE_WORDS: (
        "The title adds role words that need a person to interpret."
    ),
    ScreeningCode.TITLE_NEAR_MATCH: "The title is close to a target but may name different work.",
    ScreeningCode.TITLE_DOMAIN_REVIEW: (
        "The title contains configured domain evidence; review the posting responsibilities."
    ),
    ScreeningCode.LOCATION_MISSING: "The discovery did not retain a location to screen.",
    ScreeningCode.LOCATION_UNRESOLVED: (
        "The staged location does not resolve to a current saved place."
    ),
    ScreeningCode.LOCATION_UNRESTRICTED: (
        "No saved places are configured to screen this location."
    ),
    ScreeningCode.EMPLOYER_UNKNOWN: "The discovery did not retain an employer to screen.",
    ScreeningCode.SENIORITY_UNKNOWN: "The title does not state a level to screen.",
    ScreeningCode.EMPLOYMENT_UNKNOWN: (
        "The published facts do not completely state the employment type."
    ),
    ScreeningCode.ARRANGEMENT_REMOTE_UNCONFIRMED: (
        "The published {subject} arrangement does not settle remote eligibility in a saved country."
    ),
    ScreeningCode.ARRANGEMENT_UNKNOWN: (
        "The published facts do not completely state the work arrangement or office cadence."
    ),
    ScreeningCode.COMPENSATION_UNKNOWN: (
        "The published facts do not state comparable compensation completely."
    ),
}


def _first_finding(
    screening: DiscoveryScreening, standing: ScreeningStanding
) -> ScreeningFinding | None:
    """Return the first finding at one standing, which is the rendering priority."""
    return next((item for item in screening.findings if item.standing is standing), None)


# A stored finding with no subject renders this instead of formatting the
# template with an empty one ("...refused for .").
_NO_SUBJECT_TEXT = {
    ScreeningCode.ARRANGEMENT_REFUSED: "The published work arrangement is refused.",
}


def _display_subject(subject: str, parameters: JobSearchParameters) -> str:
    """Return a saved location's rendered label for its key, or the subject as given."""
    location = next((place for place in parameters.locations if place.key == subject), None)
    return location.render() if location is not None else subject


def _render_text(
    code: ScreeningCode, template: str, subject: str, parameters: JobSearchParameters
) -> str:
    """Fill a refusal or unknown template, falling back when the finding carries no subject."""
    if not subject:
        return _NO_SUBJECT_TEXT.get(code, template.format(subject=""))
    return template.format(subject=_display_subject(subject, parameters))


def refusal_text(refused: ScreeningFinding, parameters: JobSearchParameters) -> str:
    """Return the person-facing sentence for one refusing screening finding."""
    template = REFUSAL_TEXT.get(refused.code, "A current search boundary refuses this staged fact.")
    return _render_text(refused.code, template, refused.subject, parameters)


def render_screening(
    screening: DiscoveryScreening, parameters: JobSearchParameters
) -> tuple[DiscoveryRecommendation, str]:
    """Render typed screening without making another policy decision."""
    refused = _first_finding(screening, ScreeningStanding.REFUSE)
    if refused is not None:
        return "unlikely", refusal_text(refused, parameters)
    unknown = _first_finding(screening, ScreeningStanding.UNKNOWN)
    if unknown is not None and unknown.because:
        return "investigate", unknown.because
    if unknown is not None:
        template = UNKNOWN_TEXT.get(
            unknown.code, "The staged facts do not answer every current setting."
        )
        return "investigate", _render_text(unknown.code, template, unknown.subject, parameters)
    return "review", "The staged facts align with every deterministically screened setting."
