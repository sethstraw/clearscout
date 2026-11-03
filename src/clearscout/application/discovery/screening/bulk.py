"""Whether a saved search supports bulk staging and title expansion."""

from __future__ import annotations

from enum import StrEnum

from clearscout.application.discovery.screening.findings import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)


class BulkStagingRefusal(StrEnum):
    """Which check refused a listing from automatic board staging."""

    # Mirrors FunnelReason values; funnel_audit imports this module, so it cannot import back.

    INSUFFICIENT_TITLE_EVIDENCE = "insufficient_title_evidence"
    SENIORITY_OUTSIDE_BAND = "seniority_outside_band"
    LOCATION_OUTSIDE_SAVED_PLACES = "location_outside_saved_places"


# An adjacent title earns a body-backed review even when board metadata cannot
# resolve its market or its absence; the later read may settle either.
_BULK_STAGING_LOCATION_ALLOWED = {
    ScreeningCode.LOCATION_MATCH,
    ScreeningCode.LOCATION_UNRESTRICTED,
    ScreeningCode.LOCATION_UNRESOLVED,
    ScreeningCode.LOCATION_MISSING,
    ScreeningCode.LOCATION_UNVERIFIED,
}


def bulk_staging_refusal(
    screening: DiscoveryScreening, *, include_bounded_review: bool = False
) -> BulkStagingRefusal | None:
    """Return which check refused bulk board staging, or None when none did."""
    title = next(
        finding for finding in screening.findings if finding.dimension is ScreeningDimension.TITLE
    )
    if not supports_title_expansion(title, include_bounded_review=include_bounded_review):
        return BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE
    location = next(
        finding
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.LOCATION
    )
    if location.code not in _BULK_STAGING_LOCATION_ALLOWED:
        return BulkStagingRefusal.LOCATION_OUTSIDE_SAVED_PLACES
    return None


def supports_bulk_staging(
    screening: DiscoveryScreening, *, include_bounded_review: bool = False
) -> bool:
    """Require title evidence before expanding an entire board into review."""
    return bulk_staging_refusal(screening, include_bounded_review=include_bounded_review) is None


def supports_title_expansion(
    title: ScreeningFinding, *, include_bounded_review: bool = False
) -> bool:
    """Return whether a title justifies a detail read or bulk-staging slot."""
    if title.standing is ScreeningStanding.MATCH:
        return True
    supported_codes = {
        ScreeningCode.TITLE_NEAR_MATCH,
        ScreeningCode.TITLE_EXTRA_ROLE_WORDS,
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        ScreeningCode.TITLE_DOMAIN_REVIEW,
    }
    if include_bounded_review:
        supported_codes.add(ScreeningCode.TITLE_ROLE_FAMILY_REVIEW)
    return title.code in supported_codes
