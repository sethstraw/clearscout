"""Declared field mappings for every board record the fetchers read.

Declarations are imported by name rather than discovered, so each one has a
visible consumer.
"""

from __future__ import annotations

from tailorcv.infrastructure.web_fetch.board_mappings.adp import (
    ADP_MAPPING,
    adp_item_id,
    adp_requisition,
    adp_requisition_url,
    adp_requisitions_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.arbeitnow import ARBEITNOW_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.ashby import (
    ASHBY_MAPPING,
)
from tailorcv.infrastructure.web_fetch.board_mappings.avature import (
    AVATURE_MAPPING,
    avature_openings,
    avature_page_posting,
    avature_portal,
    avature_sitemap_url,
    avature_sitemaps,
)
from tailorcv.infrastructure.web_fetch.board_mappings.bamboohr import (
    BAMBOOHR_MAPPING,
    bamboohr_detail_url,
    bamboohr_opening,
    bamboohr_withdrawn,
)
from tailorcv.infrastructure.web_fetch.board_mappings.breezy import (
    BREEZY_MAPPING,
    breezy_page_posting,
    breezy_page_url,
    breezy_positions_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.clearcompany import (
    CLEARCOMPANY_MAPPING,
    clearcompany_jobs_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.comeet import COMEET_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.dayforce import (
    DAYFORCE_MAPPING,
    dayforce_feed_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.dover import (
    DOVER_MAPPING,
    dover_detail_url,
    dover_job,
    dover_jobs_url,
    dover_listed,
)
from tailorcv.infrastructure.web_fetch.board_mappings.gem import GEM_MAPPING, gem_post_url
from tailorcv.infrastructure.web_fetch.board_mappings.getro import GETRO_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.greenhouse import GREENHOUSE_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.hibob import HIBOB_MAPPING, hibob_job_ads_url
from tailorcv.infrastructure.web_fetch.board_mappings.himalayas import HIMALAYAS_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.icims import (
    ICIMS_CLASSIC_MAPPING,
    ICIMS_MAPPING,
    icims_job_page,
    icims_openings,
    icims_sitemap_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.jazzhr import (
    JAZZHR_MAPPING,
    jazzhr_board_url,
    jazzhr_openings,
)
from tailorcv.infrastructure.web_fetch.board_mappings.jobvite import (
    JOBVITE_MAPPING,
    jobvite_board_url,
    jobvite_linked_pages,
    jobvite_more_pages,
    jobvite_openings,
)
from tailorcv.infrastructure.web_fetch.board_mappings.lever import LEVER_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.mapper import (
    BoardDeclaration,
    Posting,
    plain_description,
    posting_check,
    source_listings,
)
from tailorcv.infrastructure.web_fetch.board_mappings.oracle import (
    ORACLE_DETAIL_MAPPING,
    ORACLE_LISTING_MAPPING,
)
from tailorcv.infrastructure.web_fetch.board_mappings.page_posting import page_posting
from tailorcv.infrastructure.web_fetch.board_mappings.paylocity import (
    PAYLOCITY_MAPPING,
    paylocity_board_page_url,
    paylocity_feed_url,
    paylocity_page_jobs,
)
from tailorcv.infrastructure.web_fetch.board_mappings.peoplesoft import PEOPLESOFT_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.personio import PERSONIO_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.phenom import PHENOM_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.pinpoint import PINPOINT_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.recruitee import RECRUITEE_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.rippling import (
    RIPPLING_MAPPING,
    rippling_detail_url,
    rippling_job,
    rippling_jobs_url,
)
from tailorcv.infrastructure.web_fetch.board_mappings.smartrecruiters import SMARTRECRUITERS_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.successfactors import (
    SUCCESSFACTORS_MAPPING,
    successfactors_brand,
    successfactors_openings,
)
from tailorcv.infrastructure.web_fetch.board_mappings.taleobusiness import TALEOBUSINESS_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.teamtailor import TEAMTAILOR_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.telus import (
    TELUS_MAPPING,
    TELUS_POST,
    telus_post_url,
    telus_withdrawn,
)
from tailorcv.infrastructure.web_fetch.board_mappings.trinethire import (
    TRINETHIRE_MAPPING,
    trinethire_board_url,
    trinethire_openings,
    trinethire_page_posting,
    trinethire_paged,
)
from tailorcv.infrastructure.web_fetch.board_mappings.ukg import (
    UKG_MAPPING,
    UKG_OPPORTUNITY,
    ukg_opportunity,
    ukg_opportunity_url,
    ukg_withdrawn,
)
from tailorcv.infrastructure.web_fetch.board_mappings.workable import WORKABLE_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.workday import (
    WORKDAY_DETAIL_MAPPING,
    WORKDAY_LISTING_MAPPING,
)
from tailorcv.infrastructure.web_fetch.board_mappings.zoho import (
    ZOHO_MAPPING,
    zoho_board_url,
    zoho_openings,
)

BOARD_DECLARATIONS: tuple[BoardDeclaration, ...] = (
    ADP_MAPPING,
    ARBEITNOW_MAPPING,
    ASHBY_MAPPING,
    AVATURE_MAPPING,
    BAMBOOHR_MAPPING,
    BREEZY_MAPPING,
    CLEARCOMPANY_MAPPING,
    COMEET_MAPPING,
    DAYFORCE_MAPPING,
    DOVER_MAPPING,
    GEM_MAPPING,
    GETRO_MAPPING,
    GREENHOUSE_MAPPING,
    HIBOB_MAPPING,
    HIMALAYAS_MAPPING,
    ICIMS_MAPPING,
    JAZZHR_MAPPING,
    JOBVITE_MAPPING,
    LEVER_MAPPING,
    ORACLE_DETAIL_MAPPING,
    ORACLE_LISTING_MAPPING,
    PAYLOCITY_MAPPING,
    PEOPLESOFT_MAPPING,
    PERSONIO_MAPPING,
    PHENOM_MAPPING,
    PINPOINT_MAPPING,
    RECRUITEE_MAPPING,
    RIPPLING_MAPPING,
    SMARTRECRUITERS_MAPPING,
    SUCCESSFACTORS_MAPPING,
    TALEOBUSINESS_MAPPING,
    TEAMTAILOR_MAPPING,
    TELUS_MAPPING,
    TRINETHIRE_MAPPING,
    UKG_MAPPING,
    WORKABLE_MAPPING,
    WORKDAY_DETAIL_MAPPING,
    WORKDAY_LISTING_MAPPING,
    ZOHO_MAPPING,
)

__all__ = [
    "ADP_MAPPING",
    "ARBEITNOW_MAPPING",
    "ASHBY_MAPPING",
    "AVATURE_MAPPING",
    "BAMBOOHR_MAPPING",
    "BOARD_DECLARATIONS",
    "BREEZY_MAPPING",
    "CLEARCOMPANY_MAPPING",
    "COMEET_MAPPING",
    "DAYFORCE_MAPPING",
    "DOVER_MAPPING",
    "GEM_MAPPING",
    "GETRO_MAPPING",
    "GREENHOUSE_MAPPING",
    "HIBOB_MAPPING",
    "HIMALAYAS_MAPPING",
    "ICIMS_CLASSIC_MAPPING",
    "ICIMS_MAPPING",
    "JAZZHR_MAPPING",
    "JOBVITE_MAPPING",
    "LEVER_MAPPING",
    "ORACLE_DETAIL_MAPPING",
    "ORACLE_LISTING_MAPPING",
    "PAYLOCITY_MAPPING",
    "PEOPLESOFT_MAPPING",
    "PERSONIO_MAPPING",
    "PHENOM_MAPPING",
    "PINPOINT_MAPPING",
    "RECRUITEE_MAPPING",
    "RIPPLING_MAPPING",
    "SMARTRECRUITERS_MAPPING",
    "SUCCESSFACTORS_MAPPING",
    "TALEOBUSINESS_MAPPING",
    "TEAMTAILOR_MAPPING",
    "TELUS_MAPPING",
    "TELUS_POST",
    "TRINETHIRE_MAPPING",
    "UKG_MAPPING",
    "UKG_OPPORTUNITY",
    "WORKABLE_MAPPING",
    "WORKDAY_DETAIL_MAPPING",
    "WORKDAY_LISTING_MAPPING",
    "ZOHO_MAPPING",
    "BoardDeclaration",
    "Posting",
    "adp_item_id",
    "adp_requisition",
    "adp_requisition_url",
    "adp_requisitions_url",
    "avature_openings",
    "avature_page_posting",
    "avature_portal",
    "avature_sitemap_url",
    "avature_sitemaps",
    "bamboohr_detail_url",
    "bamboohr_opening",
    "bamboohr_withdrawn",
    "breezy_page_posting",
    "breezy_page_url",
    "breezy_positions_url",
    "clearcompany_jobs_url",
    "dayforce_feed_url",
    "dover_detail_url",
    "dover_job",
    "dover_jobs_url",
    "dover_listed",
    "gem_post_url",
    "hibob_job_ads_url",
    "icims_job_page",
    "icims_openings",
    "icims_sitemap_url",
    "jazzhr_board_url",
    "jazzhr_openings",
    "jobvite_board_url",
    "jobvite_linked_pages",
    "jobvite_more_pages",
    "jobvite_openings",
    "page_posting",
    "paylocity_board_page_url",
    "paylocity_feed_url",
    "paylocity_page_jobs",
    "plain_description",
    "posting_check",
    "rippling_detail_url",
    "rippling_job",
    "rippling_jobs_url",
    "source_listings",
    "successfactors_brand",
    "successfactors_openings",
    "telus_post_url",
    "telus_withdrawn",
    "trinethire_board_url",
    "trinethire_openings",
    "trinethire_page_posting",
    "trinethire_paged",
    "ukg_opportunity",
    "ukg_opportunity_url",
    "ukg_withdrawn",
    "zoho_board_url",
    "zoho_openings",
]
