"""Role-family, adjacency, rung, and bare-function title rules decide what a title may claim."""

import pytest

from clearscout.application.discovery import titles as titles_module
from clearscout.application.discovery.screening import (
    ScreeningCode,
    ScreeningFinding,
    ScreeningStanding,
    screen_discovery,
    screen_discovery_title,
    supports_bulk_staging,
    supports_title_expansion,
)
from clearscout.application.discovery.titles import (
    ladder_words,
    names_a_bare_function,
    names_a_leadership_rung,
    occupation_disagrees,
    role_family_conflict,
    shares_occupation_noun,
)
from clearscout.domain.search_parameters import (
    CountryNames,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.domain.staged_facts import PublishedFact
from clearscout.domain.taxonomy.data import Family
from tests.support.scenarios import (
    DATA_PRODUCT_TARGET_TITLES,
    data_product_search,
    home_market_search,
    published_location_facts,
    titles,
)


def test_a_low_overlap_role_family_stays_available_for_bounded_review() -> None:
    screening = screen_discovery(
        "Example Cooperative",
        "Senior Product Manager - Platform Services",
        "Freedonia",
        home_market_search("Senior Clinical Data Product Manager"),
        ["Senior Clinical Data Product Manager"],
        published_location_facts("Freedonia"),
    )

    assert screening.standing is ScreeningStanding.UNKNOWN
    assert screening.findings[0].code in {
        ScreeningCode.TITLE_MATCH,
        ScreeningCode.TITLE_ROLE_FAMILY_REVIEW,
    }
    assert screening.findings[0].subject == "Senior Clinical Data Product Manager"


@pytest.mark.parametrize(
    "title",
    [
        "Director, Drug Information",
        "Associate Director, Medical Information",
    ],
)
def test_same_family_leadership_synonyms_reach_review(title: str) -> None:
    screening = screen_discovery(
        "Example Pharma",
        title,
        "Freedonia",
        home_market_search("Medical Information Director"),
        ["Medical Information Director"],
        published_location_facts("Freedonia"),
    )

    assert screening.findings[0].code in {
        ScreeningCode.TITLE_MATCH,
        ScreeningCode.TITLE_ROLE_FAMILY_REVIEW,
    }
    assert supports_bulk_staging(screening, include_bounded_review=True)


@pytest.mark.parametrize(
    ("found", "wanted"),
    [
        ("Biomedical Writer", "Medical Writer"),
        ("Scientific Business Analyst", "Clinical Business Analyst"),
        ("LIMS Business Analyst", "Clinical Business Analyst"),
        ("Clinical Evidence Lead", "Clinical Evidence Manager"),
        ("Business Analyst", "Clinical Business Analyst"),
        ("Scientific Business Analyst", "Requirements Analyst"),
    ],
)
def test_compound_titles_in_one_role_family_never_conflict(found: str, wanted: str) -> None:
    assert role_family_conflict(found, wanted) is None
    assert role_family_conflict(found, "Plant Manager") is not None


@pytest.mark.parametrize(
    ("found", "wanted", "family"),
    [
        ("Nurse", "Clinical Evidence Manager", "nursing"),
        ("Registered Nurse", "Clinical Evidence Manager", "nursing"),
        ("BCBA", "Clinical Business Analyst", "behavioral_health"),
        ("Senior Copywriter", "Medical Writer", "editorial"),
        ("Payer Evidence Lead", "Clinical Evidence Lead", "market_access"),
        ("HEOR Manager", "Clinical Evidence Manager", "market_access"),
        ("Clinical Research Associate", "Clinical Evidence Manager", "clinical_research"),
        ("People Operations Manager", "Plant Manager", "human_resources"),
        ("Medical Content Operations Manager", "Production Manager", "medical_information"),
    ],
)
def test_a_title_in_a_distinct_role_family_conflicts_with_its_own_family(
    found: str, wanted: str, family: str
) -> None:
    assert role_family_conflict(found, wanted) == family


@pytest.mark.parametrize("found", ["Manager", "Analyst", "Specialist", "Engineer"])
@pytest.mark.parametrize("wanted", ["Medical Writer", "Plant Manager", "Policy Analyst"])
def test_a_generic_title_noun_names_no_role_family_to_conflict_with(
    found: str, wanted: str
) -> None:
    assert role_family_conflict(found, wanted) is None


def _family_review_title_finding(title: str) -> ScreeningFinding:
    return screen_discovery_title(
        title,
        PublishedFact(),
        data_product_search(),
        list(DATA_PRODUCT_TARGET_TITLES),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Front Line Manufacturing Manager - Downstream Bulk Packaging Materials",
        "Senior Manager, Manufacturing - Downstream Bulk Packaging Materials",
        "Manager, New Product Introduction and Manufacturing Execution Systems",
    ],
)
def test_manufacturing_titles_conflict_with_every_data_product_target(title: str) -> None:
    """Manufacturing is a distinct-occupation token, never a data-product review."""
    finding = _family_review_title_finding(title)
    assert finding.code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT


def test_director_engineering_is_a_mismatch_not_a_family_review() -> None:
    """Engineering is a specific occupation noun; a bare leadership overlap is not evidence."""
    finding = _family_review_title_finding("Director Engineering")
    assert finding.code is ScreeningCode.TITLE_MISMATCH


def test_manager_product_software_engineering_stays_reviewable() -> None:
    """An adjacent product-and-engineering title must not be excluded outright."""
    finding = _family_review_title_finding("Manager, Product Software Engineering")
    assert finding.standing is not ScreeningStanding.REFUSE
    assert finding.code is not ScreeningCode.TITLE_MISMATCH


def test_a_communications_director_does_not_inherit_the_product_family() -> None:
    """A single shared generic word ("product") is not domain evidence."""
    finding = _family_review_title_finding("Executive Director, Product Communications")
    assert finding.code is ScreeningCode.TITLE_MISMATCH


@pytest.mark.parametrize(
    "title",
    [
        "Senior Logistics Data Product Manager",
        "Associate Director, Data Governance and Interoperability",
        "Director, Logistics Data Informatics",
    ],
)
def test_adjacent_titles_stay_reviewable_after_the_family_precision_fix(title: str) -> None:
    """The precision rules above must not turn a genuinely adjacent title into a mismatch."""
    finding = _family_review_title_finding(title)
    assert finding.code is not ScreeningCode.TITLE_MISMATCH
    assert finding.code is not ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT


def test_a_bare_function_head_downgrades_to_near_match_not_mismatch() -> None:
    """A bare function head names a team rather than a person, and still stages."""
    finding = _family_review_title_finding("Logistics Data Management")
    assert finding.code is ScreeningCode.TITLE_NEAR_MATCH


def test_a_role_noun_anywhere_in_the_title_keeps_the_full_match() -> None:
    """A stated role noun, even a leadership one, exempts the title from the bare-function rule."""
    finding = _family_review_title_finding("Director, Logistics Data Management")
    assert finding.code is ScreeningCode.TITLE_MATCH


# A synthetic table: the shipped taxonomy declares no non-medical adjacent pair.
_ADJACENT_TEST_FAMILIES = (
    Family(
        key="structural_safety",
        label="Structural Safety",
        sector="engineering",
        titles=("Structural Safety Director",),
        terms=(),
    ),
    Family(
        key="civil_safety",
        label="Civil Safety",
        sector="engineering",
        titles=("Civil Safety Manager",),
        terms=(),
    ),
    Family(
        key="logistics_safety",
        label="Logistics Safety",
        sector="logistics",
        titles=("Logistics Safety Manager",),
        terms=(),
    ),
)


_ADJACENT_TEST_PAIRS = frozenset({frozenset(("structural_safety", "civil_safety"))})


@pytest.fixture
def synthetic_adjacent_families(monkeypatch: pytest.MonkeyPatch):
    """Swap in a small family table with one declared-adjacent pair, no shipped families."""
    monkeypatch.setattr(titles_module, "FAMILIES", _ADJACENT_TEST_FAMILIES)
    monkeypatch.setattr(titles_module, "_ADJACENT_ROLE_FAMILIES", _ADJACENT_TEST_PAIRS)
    titles_module._role_families.cache_clear()
    yield
    titles_module._role_families.cache_clear()


def _family_review_finding(title: str, wanted: str) -> ScreeningFinding:
    return screen_discovery_title(title, PublishedFact(), home_market_search(wanted), [wanted])


def test_a_same_rung_adjacent_family_with_a_shared_word_reaches_bounded_review(
    synthetic_adjacent_families: None,
) -> None:
    """Adjacent families both naming the same occupation rung grant review on one shared word."""
    finding = _family_review_finding("Civil Safety Manager", "Structural Safety Director")
    assert finding.code is ScreeningCode.TITLE_ROLE_FAMILY_REVIEW
    assert finding.standing is ScreeningStanding.UNKNOWN
    assert supports_title_expansion(finding, include_bounded_review=True) is True
    assert supports_title_expansion(finding, include_bounded_review=False) is False


@pytest.mark.parametrize(
    "title",
    ["Civil Safety Coordinator", "Civil Safety Team"],
)
def test_a_cross_occupation_adjacent_pair_stays_a_mismatch(
    synthetic_adjacent_families: None, title: str
) -> None:
    """Family adjacency alone is not enough: the occupation rung must also agree."""
    finding = _family_review_finding(title, "Structural Safety Director")
    assert finding.code is ScreeningCode.TITLE_MISMATCH


def test_a_recognized_non_adjacent_family_conflicts(
    synthetic_adjacent_families: None,
) -> None:
    """A recognized family that is not declared adjacent still conflicts."""
    finding = _family_review_finding("Logistics Safety Manager", "Structural Safety Director")
    assert finding.code is ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT


def test_a_role_family_conflict_token_still_conflicts(
    synthetic_adjacent_families: None,
) -> None:
    """A distinct-occupation token still overrides family adjacency."""
    assert role_family_conflict("Finance Manager", "Structural Safety Director") == "finance"


def test_an_identical_title_still_matches(synthetic_adjacent_families: None) -> None:
    finding = _family_review_finding("Structural Safety Director", "Structural Safety Director")
    assert finding.code is ScreeningCode.TITLE_MATCH


@pytest.mark.parametrize(
    "title",
    ["Nursing Administration", "Revenue Operations", "Logistics Data Management"],
)
def test_an_area_only_title_in_any_field_names_a_bare_function(title: str) -> None:
    """The bare-function rule reads by English morphology, not a per-field word list."""
    assert names_a_bare_function(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Director of Nursing, Emergency Services",
        "Revenue Operations Manager",
        "Director, Logistics Data Management",
        "Geologist",
        "Dental Hygienist",
        "Tax Accountant",
        "Registered Nurse",
    ],
)
def test_a_stated_role_noun_in_any_field_is_never_a_bare_function(title: str) -> None:
    """A title stating its own person noun never reads as an area, in any profession."""
    assert names_a_bare_function(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "Program Director",
        "Head of Quality",
        "Manager, Regulatory Affairs",
        "Senior Manager, Quality Systems",
        "Vice President, Operations",
        "Data Platform Lead",
        "Director, Data Science",
        "Head of Clinical Data",
        "Manager, Data Engineering",
        "Principal, Reference Data",
        "Manager, Executive Communications",
        "Director, Associate Relations",
        "Associate Director, Data Reporting Analyst",
        "Director, Developer Relations",
        "Principal, Data Scientist Programs",
        "Director (Data Scientist Programs)",
        "Head: Data Scientist Programs",
        "Director | Data Scientist Programs",
        "Director \u2013 Data Scientist Programs",
    ],
)
def test_a_title_headed_by_its_own_rung_names_a_leadership_rung(title: str) -> None:
    """A leadership rung with no occupation noun behind it states the job itself."""
    assert names_a_leadership_rung(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Career",
        "Discover Your Career",
        "Career Center",
        "Partner With Us",
        "Employer of Choice",
        "Newsletter",
        "Customer Stories",
        "Investor Relations",
        "Talent Management",
        "Lead Scientist",
        "Principal Data Scientist",
        "Lead Clinical Pharmacist",
        "Head Nurse Educator",
        "Lead Data Analyst",
        "Principal Business Consultant - Life Sciences R&D",
        "Senior Principal Health Data Scientist",
        "Principal-Level Data Scientist",
        "Co-Lead Data Scientist",
    ],
)
def test_a_marketing_or_modifier_phrase_names_no_leadership_rung(title: str) -> None:
    """An agent-noun ending, an area name, and a rung used as a modifier each state no rung."""
    assert names_a_leadership_rung(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "SVP, Product Management - Data Products & Strategy",
        "Svp",
        "svp, product",
        "SVP of Data",
        "Data Strategy SVP",
        "EVP Product",
    ],
)
def test_an_executive_abbreviation_states_the_executive_rung(title: str) -> None:
    """SVP and EVP read like VP in any case and position."""
    assert ladder_words(title) == frozenset({"executive"})


@pytest.mark.parametrize(
    "title",
    [
        "Senior Vice President, Clinical Development",
        "Senior VP, Data",
        "Senior SVP, Data",
        "Senior EVP Product",
        "Senior Executive Vice President, Data",
    ],
)
def test_senior_ahead_of_a_vice_president_rank_is_part_of_the_rank(title: str) -> None:
    """A senior vice president sits on the executive rung alone, not also on the senior rung."""
    assert ladder_words(title) == frozenset({"executive"})


@pytest.mark.parametrize(
    ("title", "rungs"),
    [
        ("Senior Director, Data Platform", {"senior", "director"}),
        ("Senior Manager, Quality Systems", {"senior"}),
        ("Senior Vice Provost, Research", {"senior"}),
        ("Senior Vice Dean", {"senior"}),
    ],
)
def test_senior_ahead_of_anything_but_a_vice_president_keeps_the_senior_rung(
    title: str, rungs: set[str]
) -> None:
    """Only a vice-president rank absorbs "senior"; a director, provost, or dean keeps it."""
    assert ladder_words(title) == frozenset(rungs)


def test_the_sr_abbreviation_states_no_senior_rung() -> None:
    """The "Sr" abbreviation is no ladder word, so a Sr. Director reads the director rung alone."""
    assert ladder_words("Sr. Director, Data") == frozenset({"director"})


@pytest.mark.parametrize(
    "title",
    [
        "AVP, Data Products",
        "Associate SVP, Data",
        "Assistant Vice President, Data",
        "Associate Vice President, Data",
    ],
)
def test_an_assistant_or_associate_vice_president_states_no_rung(title: str) -> None:
    """Assistant and associate modify the vice-president rank rather than stating it."""
    assert ladder_words(title) == frozenset()


def test_an_executive_director_abbreviated_ed_states_no_rung() -> None:
    """ED also opens emergency-department and Ed-Tech titles, so it stays unread as a rank."""
    assert ladder_words("ED, Data Science") == frozenset()


@pytest.mark.parametrize(
    "title",
    [
        "Engineer",
        "Consultant",
        "Pharmacist",
        "Attorney, Litigation",
        "Associate, Business Development",
        "Actuary, Pricing",
        "Fellow, Data Science",
        "Executive, Business Development",
        "Account Executive, Enterprise Sales",
        "Product Marketing",
        "Talent Acquisition",
    ],
)
def test_an_identical_title_always_stays_a_full_match(title: str) -> None:
    """An exact target-title match never downgrades, whether or not it is a bare function."""
    finding = _rung_agreement_finding(title, title)
    assert finding.standing is ScreeningStanding.MATCH
    assert finding.code is ScreeningCode.TITLE_MATCH


@pytest.mark.parametrize(
    ("principal", "senior"),
    [
        ("Principal Data Scientist", "Senior Data Scientist"),
        ("Principal Health Data Scientist", "Senior Health Data Scientist"),
        ("Principal Financial Analyst", "Senior Financial Analyst"),
        ("Lead Clinical Pharmacist", "Senior Clinical Pharmacist"),
    ],
)
def test_a_leadership_word_modifying_an_occupation_reads_like_any_other_rung(
    principal: str, senior: str
) -> None:
    """The occupation after the modifier is the job, so the rung word never changes the answer."""
    target = "Associate Director, Reference Data"
    assert shares_occupation_noun(principal, target) is shares_occupation_noun(senior, target)
    assert occupation_disagrees(principal, target) is True
    assert occupation_disagrees(senior, target) is True
    assert _rung_agreement_finding(target, principal).code is ScreeningCode.TITLE_MISMATCH
    assert _rung_agreement_finding(target, senior).code is ScreeningCode.TITLE_MISMATCH


def _rung_agreement_finding(target: str, title: str) -> ScreeningFinding:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(target_titles=titles(target)),
    )
    return screen_discovery_title(title, PublishedFact(), parameters, [target])


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("Manager, Platform Engineering", "Director, Platform Engineering"),
        ("Manager, Platform Engineering", "Associate Director, Platform Engineering"),
        ("Nurse Manager, Emergency Services", "Director of Nursing, Emergency Services"),
        (
            "Nurse Manager, Emergency Services",
            "Associate Director of Nursing, Emergency Services",
        ),
        ("Director, Reference Data", "Associate Director, Data Reporting Analyst"),
        ("Manager, Partner Relations", "Director, Developer Relations"),
    ],
)
def test_a_higher_leadership_rung_reads_no_worse_than_the_manager_rung(
    target: str, title: str
) -> None:
    """Director and Associate Director of the same function read no worse than Manager of it."""
    finding = _rung_agreement_finding(target, title)
    assert finding.standing is not ScreeningStanding.REFUSE
    assert finding.code is not ScreeningCode.TITLE_MISMATCH


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("Manager, Platform Engineering", "Director, Manufacturing Operations"),
        ("Nurse Manager, Emergency Services", "Director, Manufacturing Operations"),
    ],
)
def test_a_different_kind_of_work_still_mismatches_regardless_of_rung(
    target: str, title: str
) -> None:
    """Rung agreement never papers over a genuinely different occupation."""
    finding = _rung_agreement_finding(target, title)
    assert finding.code in (ScreeningCode.TITLE_MISMATCH, ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT)
