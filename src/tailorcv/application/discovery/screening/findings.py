"""Screening codes, standings, findings and the screening record."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

GENERIC_US_REMOTE = re.compile(
    r"(?i)^\s*(?:(?:us|united states)\s*[-,]?\s*remote|remote\s*[-,(]?\s*"
    r"(?:us|united states)\)?)[\s.]*$"
)


class ScreeningStanding(StrEnum):
    """Whether one staged fact answers, refuses, or cannot answer a setting."""

    MATCH = "match"
    REFUSE = "refuse"
    UNKNOWN = "unknown"


class ConstraintConfidence(StrEnum):
    """How completely published facts answer the configured search constraints."""

    PURSUE = "pursue"
    INVESTIGATE = "investigate"
    NEEDS_VERIFICATION = "needs_verification"
    UNCERTAIN_COMP = "uncertain_comp"
    EXCLUDED = "excluded"


class ScreeningDimension(StrEnum):
    """The configured boundary one finding reads."""

    TITLE = "title"
    EMPLOYER = "employer"
    SENIORITY = "seniority"
    LOCATION = "location"
    EMPLOYMENT_TYPE = "employment_type"
    ARRANGEMENT = "arrangement"
    COMPENSATION = "compensation"
    CONDITION = "condition"


class ScreeningCode(StrEnum):
    """Stable reasons callers may act on without parsing explanatory prose."""

    TITLE_MATCH = "title_match"
    TITLE_EXCLUDED = "title_excluded"
    TITLE_TEST_REQUISITION = "title_test_requisition"
    TITLE_MISMATCH = "title_mismatch"
    TITLE_NEAR_MATCH = "title_near_match"
    TITLE_ROLE_FAMILY_REVIEW = "title_role_family_review"
    TITLE_SPECIALIST_IC_REVIEW = "title_specialist_ic_review"
    TITLE_DOMAIN_REVIEW = "title_domain_review"
    TITLE_ROLE_FAMILY_CONFLICT = "title_role_family_conflict"
    TITLE_EXTRA_ROLE_WORDS = "title_extra_role_words"
    TITLE_UNRESTRICTED = "title_unrestricted"
    TITLE_UNVERIFIED = "title_unverified"
    EMPLOYER_MATCH = "employer_match"
    EMPLOYER_EXCLUDED = "employer_excluded"
    EMPLOYER_UNKNOWN = "employer_unknown"
    EMPLOYER_UNVERIFIED = "employer_unverified"
    SENIORITY_MATCH = "seniority_match"
    SENIORITY_OUTSIDE = "seniority_outside"
    SENIORITY_UNKNOWN = "seniority_unknown"
    SENIORITY_UNVERIFIED = "seniority_unverified"
    LOCATION_MATCH = "location_match"
    LOCATION_UNRESTRICTED = "location_unrestricted"
    LOCATION_OUTSIDE = "location_outside"
    LOCATION_INELIGIBLE = "location_ineligible"
    LOCATION_MISSING = "location_missing"
    LOCATION_UNRESOLVED = "location_unresolved"
    LOCATION_UNVERIFIED = "location_unverified"
    LOCATION_RESIDENCE_REQUIRED = "location_residence_required"
    EMPLOYMENT_MATCH = "employment_match"
    EMPLOYMENT_EXCLUDED = "employment_excluded"
    EMPLOYMENT_OUTSIDE = "employment_outside"
    EMPLOYMENT_UNKNOWN = "employment_unknown"
    ARRANGEMENT_MATCH = "arrangement_match"
    ARRANGEMENT_REFUSED = "arrangement_refused"
    ARRANGEMENT_OFFICE_LIMIT = "arrangement_office_limit"
    ARRANGEMENT_REMOTE_UNCONFIRMED = "arrangement_remote_unconfirmed"
    # Inferred, not published: no remote statement and every named site is a remote-only place.
    ARRANGEMENT_REMOTE_ONLY_SITES = "arrangement_remote_only_sites"
    ARRANGEMENT_UNKNOWN = "arrangement_unknown"
    TRAVEL_OVER_LIMIT = "travel_over_limit"
    CONDITION_EXCLUDED = "condition_excluded"
    COMPENSATION_MATCH = "compensation_match"
    COMPENSATION_BELOW_MINIMUM = "compensation_below_minimum"
    COMPENSATION_UNKNOWN = "compensation_unknown"
    COMPENSATION_STATED = "compensation_stated"
    COMPENSATION_TARGET_MET = "compensation_target_met"
    COMPENSATION_BELOW_TARGET = "compensation_below_target"


@dataclass(frozen=True)
class ScreeningFinding:
    """One machine-readable answer about a staged discovery."""

    dimension: ScreeningDimension
    standing: ScreeningStanding
    code: ScreeningCode
    subject: str = ""
    # The person-facing reason, set only where the subject alone cannot carry it.
    because: str = ""


@dataclass(frozen=True)
class DiscoveryScreening:
    """The deterministic screening findings for one staged discovery."""

    findings: tuple[ScreeningFinding, ...]
    # Whether the board's location fact was complete. The specialist-IC lane may
    # expand an unresolved location into review only when it was not.
    location_fact_complete: bool = True

    @property
    def standing(self) -> ScreeningStanding:
        """Return the safest combined standing across all findings."""
        standings = {finding.standing for finding in self.findings}
        if ScreeningStanding.REFUSE in standings:
            return ScreeningStanding.REFUSE
        if ScreeningStanding.UNKNOWN in standings:
            return ScreeningStanding.UNKNOWN
        return ScreeningStanding.MATCH


def constraint_confidence(screening: DiscoveryScreening) -> ConstraintConfidence:
    """Bucket configured constraints from deterministic screening findings."""
    if screening.standing is ScreeningStanding.REFUSE:
        return ConstraintConfidence.EXCLUDED
    unknown_codes = {
        finding.code
        for finding in screening.findings
        if finding.standing is ScreeningStanding.UNKNOWN
    }
    if unknown_codes & {
        ScreeningCode.LOCATION_MISSING,
        ScreeningCode.LOCATION_UNRESOLVED,
        ScreeningCode.LOCATION_UNVERIFIED,
    }:
        return ConstraintConfidence.NEEDS_VERIFICATION
    if ScreeningCode.COMPENSATION_UNKNOWN in unknown_codes:
        return ConstraintConfidence.UNCERTAIN_COMP
    if unknown_codes:
        return ConstraintConfidence.INVESTIGATE
    return ConstraintConfidence.PURSUE
