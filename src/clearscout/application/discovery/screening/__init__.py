"""Deterministic screening of discovered postings against the saved search."""

from clearscout.application.discovery.screening.arrangement import (
    ArrangementSource,
    arrangement_categories,
    resolve_arrangement_categories,
)
from clearscout.application.discovery.screening.arrangement_terms import ArrangementCategory
from clearscout.application.discovery.screening.bulk import (
    BulkStagingRefusal,
    bulk_staging_refusal,
    supports_bulk_staging,
    supports_title_expansion,
)
from clearscout.application.discovery.screening.findings import (
    GENERIC_US_REMOTE,
    ConstraintConfidence,
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
    constraint_confidence,
)
from clearscout.application.discovery.screening.holdouts import (
    StructuredHoldout,
    office_only_at_remote_only_sites,
    structured_arrangement_holdout,
    structured_holdout,
    structured_location_holdout,
)
from clearscout.application.discovery.screening.location import stated_places
from clearscout.application.discovery.screening.screen import screen_discovery
from clearscout.application.discovery.screening.titles import (
    body_states_saved_focus,
    screen_discovery_title,
    select_title_survivors,
    summary_focus_terms,
    summary_may_retain_title,
)

__all__ = [
    "GENERIC_US_REMOTE",
    "ArrangementCategory",
    "ArrangementSource",
    "BulkStagingRefusal",
    "ConstraintConfidence",
    "DiscoveryScreening",
    "ScreeningCode",
    "ScreeningDimension",
    "ScreeningFinding",
    "ScreeningStanding",
    "StructuredHoldout",
    "arrangement_categories",
    "body_states_saved_focus",
    "bulk_staging_refusal",
    "constraint_confidence",
    "office_only_at_remote_only_sites",
    "resolve_arrangement_categories",
    "screen_discovery",
    "screen_discovery_title",
    "select_title_survivors",
    "stated_places",
    "structured_arrangement_holdout",
    "structured_holdout",
    "structured_location_holdout",
    "summary_focus_terms",
    "summary_may_retain_title",
    "supports_bulk_staging",
    "supports_title_expansion",
]
