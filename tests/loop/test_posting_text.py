"""What a posting body states about place and residence, and what it does not."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from itertools import product

import pytest

from clearscout.application.discovery.posting_text import (
    closure_signal,
    lists_many_postings,
    posting_facts,
    posting_fragments,
    publisher_closing_date,
    role_based_place,
)
from clearscout.infrastructure.web_fetch.ingest.url import from_html
from tests.support.suite import FIXTURES

_POSTING_FORMATS = FIXTURES / "posting_formats"

_PROSE = (
    "Halcyon Data is proud to be a remote-first company! The majority of our roles "
    "are remote and you can work almost anywhere within the country of employment. "
)


def test_silkroad_missing_position_page_states_that_the_posting_closed() -> None:
    body = (
        "We apologize for the inconvenience, but we cannot find this position. "
        "Please view our current openings here."
    )

    assert closure_signal(body)


def test_a_stated_location_is_read_from_its_label() -> None:
    body = "# Platform Product Manager\nLocation: Toronto, Canada\nEmployment type: Full-time\n"

    assert posting_facts(body).location == "Toronto, Canada"


def test_a_relaid_body_states_the_same_fragments() -> None:
    """A refetch that marks the stored lines as items changes no compared fragment."""
    stored = f"Requirements\n{chr(0x2022)} SQL required\n\nSalary: $90,000 - $110,000"
    relaid = "Requirements\n- SQL required\n\nSalary: $90,000 - $110,000"

    assert posting_fragments(stored) == posting_fragments(relaid)
    assert posting_fragments(relaid)["requirements"] == ["SQL required"]
    assert posting_fragments(relaid)["compensation"] == ["Salary: $90,000 - $110,000"]


@pytest.mark.parametrize("marker", ["- ", "1. "])
def test_a_label_written_as_a_list_item_is_still_read(marker: str) -> None:
    body = f"# Platform Product Manager\n{marker}Location: Toronto, Canada\n"

    assert posting_facts(body).location == "Toronto, Canada"


@pytest.mark.parametrize(
    "place",
    [
        "US Remote",
        "USA Remote",
        "U.S. Remote",
        "U.S.A. Remote",
        "United States Remote",
        "Remote, USA",
        "Remote - U.S.",
        "Remote, U.S.A.",
        "Remote (United States)",
        "Remote, Canada",
    ],
)
def test_an_unlabelled_country_remote_header_is_still_a_stated_location(place: str) -> None:
    body = f"Principal Data Analyst\n{place}\nBuild governed healthcare datasets."

    assert posting_facts(body).location == place


@pytest.mark.parametrize("line", ["Remote, usage-based billing team", "Remote, Canadarm lab"])
def test_a_word_that_opens_with_a_country_name_is_not_a_country_remote_place(line: str) -> None:
    assert posting_facts(f"Principal Data Analyst\n{line}\n").location == ""


def test_generic_remote_work_copy_is_not_promoted_to_a_location() -> None:
    body = "The team supports remote work and flexible hours."

    assert posting_facts(body).location == ""


def test_a_title_abbreviating_a_word_is_still_a_title() -> None:
    """A period inside "Sr." or "D.C." ends a word, not the statement."""
    body = "Title: Sr. Product Manager - Senior Living\nLocation: Washington, D.C. Metro Area\n"

    facts = posting_facts(body)

    assert facts.title == "Sr. Product Manager - Senior Living"
    assert facts.location == "Washington, D.C. Metro Area"


def test_a_heading_that_swallowed_the_page_states_no_title() -> None:
    body = f"# Platform Product Manager. {_PROSE * 2}\n"

    assert posting_facts(body).title == ""


def test_a_label_followed_by_body_prose_states_no_location() -> None:
    body = f"Halcyon Data Careers\n- Location: {_PROSE * 2}\nApply now"

    assert posting_facts(body).location == ""


def test_a_page_that_collapsed_to_one_line_states_the_residence_sentence() -> None:
    body = (
        "Skip to main content Careers at Halcyon Data Search jobs Sign in "
        "Location: Toronto, Canada Apply now Share this job About the role "
        "We are hiring a Platform Product Manager to own the ingestion pipeline. "
        "Applicants must be authorized to work in Canada; sponsorship is not "
        "available for this position. Equal opportunity employer."
    )

    facts = posting_facts(body)

    assert facts.residence_rule == (
        "Applicants must be authorized to work in Canada; sponsorship is not "
        "available for this position."
    )


def test_a_rule_inside_an_unbroken_run_of_text_states_no_rule() -> None:
    """A run with no sentence of its own is refused rather than cut to a length."""
    body = (
        "Skip to main content "
        + "we protect your data wherever it flows " * 8
        + "sponsorship is not available"
    )

    assert posting_facts(body).residence_rule == ""


_CARDS = (
    ("Director of Scheduling Platform Engineering", "Northwind Regional Freight"),
    ("Senior Manager, Depot Timetable Systems", "Globex Freight North"),
    ("Principal Engineer, Fleet Planning Services", "Initrode Logistics"),
    ("Head of Transit Data Platform Delivery", "Acme Rail Partners"),
    ("Lead Architect, Journey Planning Platform", "Initech Transit Group"),
    ("Staff Engineer, Depot Operations Tooling", "Northwind Freight Works"),
)


def test_a_page_of_job_cards_reads_as_many_postings_though_no_line_repeats() -> None:
    """Each card extracts as its title twice and its employer, run together on one line."""
    lines = [
        "Jobs",
        "Companies",
        *(f"{title}{title}{employer}" for title, employer in _CARDS),
        "Terms",
    ]

    assert len(set(lines)) == len(lines)
    assert lists_many_postings("\n".join(lines))


def _extracted(shape: str) -> str:
    """Return the text the shipped reader pulls out of one corpus posting page."""
    body = (_POSTING_FORMATS / shape / "posting.html").read_text(encoding="utf-8")
    return from_html(f"https://boards.example.com/{shape}", body).text


def test_a_search_results_page_read_by_the_shipped_reader_is_refused() -> None:
    """The reader puts each card title on two lines in a row, which no posting does."""
    assert lists_many_postings(_extracted("whole_page_listing_html"))


@pytest.mark.parametrize(
    "shape",
    [
        "declared_paragraphs_html",
        "declared_one_line_html",
        "declared_bare_list_html",
        "declared_markup_html",
    ],
)
def test_a_single_posting_read_by_the_shipped_reader_is_kept(shape: str) -> None:
    """Refusing a page throws the posting away, so a real body must survive the rule."""
    assert not lists_many_postings(_extracted(shape))


def test_a_posting_that_repeats_one_long_phrase_is_still_one_posting() -> None:
    body = (
        "# Director of Scheduling Platform Engineering\n"
        "Location: Calderfield, Canada\n"
        "Northwind Regional Freight runs the regional depot network and is hiring a "
        "director to own the scheduling platform end to end.\n"
        "You will own the scheduling platform roadmap. You will own the scheduling "
        "platform roadmap, and you will own its delivery.\n"
        "Requirements: ten years of platform engineering, five of them leading teams.\n"
        "We are an equal opportunity employer, and we welcome applicants from every "
        "background in every market we serve.\n"
    )

    assert not lists_many_postings(body)


def test_one_long_line_repeated_many_times_over_is_not_a_page_of_job_cards() -> None:
    """A card states its title on exactly two lines; a longer run is boilerplate."""
    body = "\n".join(["Operations Lead runs the depot rota and owns the nightly handover."] * 9)

    assert not lists_many_postings(body)


def test_a_short_run_of_repeated_headings_is_not_a_page_of_job_cards() -> None:
    body = "\n".join(f"{title}{title}" for title, _employer in _CARDS[:3])

    assert not lists_many_postings(body)


def test_the_people_an_employer_serves_are_not_a_residence_rule() -> None:
    body = (
        "About Halcyon Data\n"
        "We build software for the residents of long-term care homes.\n"
        "If you are based in California, read this notice for California residents.\n"
    )

    assert posting_facts(body).residence_rule == ""


@pytest.mark.parametrize(
    ("subject", "verb"),
    list(
        product(
            ("Our seed funding round", "Open enrollment for 2026", "The deal", "The incident"),
            ("has closed", "has now closed", "is closed", "was closed", "has expired"),
        )
    ),
)
def test_something_other_than_the_posting_closing_is_no_closure(subject: str, verb: str) -> None:
    assert not closure_signal(f"{subject} {verb}, and we are hiring.")


@pytest.mark.parametrize(
    ("condition", "closing"),
    list(
        product(
            ("until", "Until", "till", "once", "when", "unless"),
            ("the position is filled", "this role has been filled", "applications have closed"),
        )
    ),
)
def test_a_closure_the_posting_only_anticipates_is_no_closure(condition: str, closing: str) -> None:
    assert not closure_signal(f"Applications are accepted {condition} {closing}.")


_CONDITIONS = (
    "if",
    "If",
    "after",
    "before",
    "should",
    "in case",
    "as soon as",
    "whenever",
    "by the time",
    "in the event",
    "in the event that",
    "provided that",
)
_SUBJECTS = ("the position", "this job", "the role", "this posting", "the vacancy", "it")
_TENSES = ("is filled", "has been filled", "has closed", "is closed", "has expired")


@pytest.mark.parametrize(
    ("condition", "subject", "tense"), list(product(_CONDITIONS, _SUBJECTS, _TENSES))
)
def test_a_conditional_or_future_filling_never_closes_an_open_posting(
    condition: str, subject: str, tense: str
) -> None:
    body = f"We may close this posting early {condition} {subject} {tense}. Apply today."

    assert not closure_signal(body)


@pytest.mark.parametrize(
    "page",
    [
        "This position has been filled.",
        "Sorry, this vacancy has now expired.",
        "Thank you for your interest. The job has closed.",
        "If you have questions, contact us. This position has been filled.",
    ],
)
def test_a_posting_stating_its_own_closure_still_closes(page: str) -> None:
    assert closure_signal(page)


@pytest.mark.parametrize(
    ("subject", "verb"),
    list(
        product(
            ("This posting", "This position", "This job", "The role", "This job posting", "It"),
            ("has closed", "has been filled", "has expired", "has now closed"),
        )
    )
    + list(
        product(
            ("This posting", "This position", "The role", "This opportunity"),
            ("is closed", "is no longer available", "is now filled", "is filled"),
        )
    ),
)
def test_the_posting_itself_closing_is_a_closure(subject: str, verb: str) -> None:
    assert closure_signal(f"{subject} {verb}.")


@pytest.mark.parametrize(
    ("subject", "verb"),
    list(
        product(
            ("This requisition", "This position", "This job", "The role", "This posting"),
            (
                "is not open for new applications",
                "is no longer open for applications",
                "is not open for applications",
                "is no longer open for new applications",
                "is not currently accepting applications",
                "is not accepting applications",
                "is not currently accepting applications at this time",
            ),
        )
    ),
)
def test_a_posting_not_open_for_applications_is_a_closure(subject: str, verb: str) -> None:
    assert closure_signal(f"{subject} {verb}.")


@pytest.mark.parametrize(
    "sentence",
    [
        "Applications are open.",
        "This position is open to new graduates.",
        "This position is not open to agencies.",
        "We are not accepting agency submissions for this role.",
        "This role is open for applications until March.",
        "This position is not open for relocation applications from agencies.",
        "We are not accepting applications from recruitment agencies.",
        "We are not accepting applications from agencies.",
        "This position is not accepting applications via email.",
        "This role is not accepting applications through recruitment agencies.",
        "This position is not accepting applications from outside Canada.",
        "This job is not accepting applications outside Canada.",
        "This role is not accepting applications by third parties.",
        "This position is not open for applications until 1 March.",
        "This role is not open for applications for visa sponsorship.",
        "This job is not accepting applications at this location.",
        "This posting is not accepting applications on LinkedIn.",
        "This position is not accepting applications at this time from recruitment agencies.",
        "This role is not accepting applications at this time for visa sponsorship.",
        "This position is not currently accepting applications at this time "
        "from candidates outside the EU.",
    ],
)
def test_an_open_posting_refusing_some_applicants_is_no_closure(sentence: str) -> None:
    assert not closure_signal(sentence)


_DUTIES = ("confirm", "ensure", "make sure", "check that", "verify", "so that")
_DUTY_STATES = ("is closed", "is filled", "is closed out", "is expired")
_DUTY_TAILS = (" in Zendesk", " correctly", " on time", " daily", " by day five", "")


@pytest.mark.parametrize(
    ("duty", "state", "tail"), list(product(_DUTIES, _DUTY_STATES, _DUTY_TAILS))
)
def test_a_duty_that_something_is_closed_or_filled_is_no_closure(
    duty: str, state: str, tail: str
) -> None:
    assert not closure_signal(f"Own every ticket and {duty} it {state}{tail}.")


@pytest.mark.parametrize(
    ("subject", "phrase"),
    list(
        product(
            ("This position", "The role", "This job", "This listing", "This requisition"),
            (
                "is closed to agency submissions",
                "is closed to third-party recruiters",
                "has been closed to applicants who require sponsorship",
                "is filled by an internal-mobility process first",
                "is filled on a first-come, first-served basis",
                "is filled internally only if no external candidates apply",
                "was filled last year and is now open again",
            ),
        )
    ),
)
def test_a_posting_closed_to_some_applicants_or_filled_some_way_stays_open(
    subject: str, phrase: str
) -> None:
    assert not closure_signal(f"{subject} {phrase}.")


@pytest.mark.parametrize(
    ("condition", "closing"),
    list(
        product(
            (
                "until such time as",
                "until a candidate is selected and",
                "until we find the right person, at which point",
                "once interviews finish and",
            ),
            ("the position is filled", "this role has been filled", "the position is closed"),
        )
    ),
)
def test_an_anticipating_word_anywhere_in_the_clause_is_no_closure(
    condition: str, closing: str
) -> None:
    assert not closure_signal(f"This posting will remain open {condition} {closing}.")


def test_applications_closed_for_another_programme_is_no_closure() -> None:
    page = "Applications have closed for our 2025 summer internship; this full-time role is open."

    assert not closure_signal(page)


@pytest.mark.parametrize(
    "note",
    [
        "Lever no longer lists this posting, so it has closed.",
        "Ashby holds 12 open jobs and none of them is 42, so it has closed.",
        "no longer\u00a0accepting applications",
        "no longer\naccepting applications",
        "Applications have closed.",
    ],
)
def test_a_reader_note_or_page_stating_closure_still_closes(note: str) -> None:
    assert closure_signal(note)


@pytest.mark.parametrize(
    "line",
    [
        "**Location:** Toronto, ON",
        "**Location**: Toronto, ON",
        "\u2022 Location: Toronto, ON",
        "Location - Toronto, ON",
        "Location \u2013 Toronto, ON",
        "Job Location: Toronto, ON",
        "Work Location: Toronto, ON",
        "Locations: Toronto, ON",
        "Location(s): Toronto, ON",
    ],
)
def test_every_location_label_form_states_the_location(line: str) -> None:
    assert posting_facts(f"{line}\nWe build freight software.").location == "Toronto, ON"


@pytest.mark.parametrize(
    "sentence",
    [
        "Must be a permanent resident. You will negotiate vendor contracts.",
        "Experience with contract management systems.",
        "Temporary housing is provided for relocation.",
    ],
)
def test_a_word_about_something_else_states_no_employment_type(sentence: str) -> None:
    assert posting_facts(sentence).employment_type == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "Work with us remote or hybrid.",
        "Join us remote-first.",
        "Tell us remote work matters to you.",
    ],
)
def test_the_pronoun_us_beside_remote_states_no_location(sentence: str) -> None:
    assert posting_facts(sentence).location == ""


_COUNTRY_PAIRS = (("US", "Canada"), ("Canada", "US"), ("USA", "UK"), ("United Kingdom", "U.S."))
_PLACE_JOINERS = (" or ", " and ", " & ", "/", " / ", ", ")


@pytest.mark.parametrize(
    ("first", "joiner", "second", "shape"),
    [
        (first, joiner, second, shape)
        for first, second in _COUNTRY_PAIRS
        for joiner in _PLACE_JOINERS
        for shape in ("remote in the {}", "Remote - {}", "Remote, {}", "{} remote")
    ],
)
def test_a_remote_phrase_joining_two_countries_states_no_single_location(
    first: str, joiner: str, second: str, shape: str
) -> None:
    places = f"{first}{joiner}{second}"
    body = f"Principal Data Analyst\nThis role is {shape.format(places)}.\n"

    assert posting_facts(body).location == ""


@pytest.mark.parametrize(
    ("first", "joiner", "second"),
    [(first, joiner, second) for first, second in _COUNTRY_PAIRS for joiner in _PLACE_JOINERS],
)
def test_two_country_remote_phrases_joined_state_no_single_location(
    first: str, joiner: str, second: str
) -> None:
    body = f"Principal Data Analyst\n{first} Remote{joiner}{second} Remote\n"

    assert posting_facts(body).location == ""


@pytest.mark.parametrize(
    ("line", "location"),
    [
        ("US Remote, full-time", "US Remote"),
        ("Remote (United States)", "Remote (United States)"),
        ("Remote - Canada", "Remote - Canada"),
        ("Remote, United States", "Remote, United States"),
        ("Remote, United States, full-time", "Remote, United States"),
        ("Remote - United States, $120,000 - $150,000", "Remote - United States"),
        ("Remote - USA", "Remote - USA"),
        ("Remote - USA or remote in any time zone", "Remote - USA"),
        ("Canada Remote", "Canada Remote"),
        ("Canada Remote, Canada", "Canada Remote"),
        ("United States - Remote, United States of America", "United States - Remote"),
        ("Remote - U.S., USA", "Remote - U.S."),
    ],
)
def test_a_remote_phrase_naming_one_country_is_still_the_location(line: str, location: str) -> None:
    assert posting_facts(f"Principal Data Analyst\n{line}\n").location == location


@pytest.mark.parametrize(
    "arrangement",
    ["In person", "In-person", "On-site", "Onsite", "Remote", "Fully remote", "Hybrid", "Hybrid."],
)
def test_a_location_label_naming_only_an_arrangement_states_no_place(arrangement: str) -> None:
    labelled = f"Principal Data Analyst\nLocation: {arrangement}\n"
    alone = f"Principal Data Analyst\nWork Location: {arrangement}\n"
    after_a_place = f"Location: Winnipeg, MB\nWork Location: {arrangement}\n"

    assert posting_facts(labelled).location == ""
    assert posting_facts(alone).location == ""
    assert posting_facts(after_a_place).location == "Winnipeg, MB"


@pytest.mark.parametrize(
    "value",
    [
        f"{arrangement} - {people} residents only"
        for arrangement in ("Remote", "Hybrid", "Fully remote")
        for people in ("Canadian", "US", "U.K.")
    ]
    + ["Remote - 3 days per quarter in Austin", "Remote, Canadian citizens only"],
)
def test_a_location_label_whose_place_is_in_its_clause_keeps_the_whole_value(value: str) -> None:
    assert posting_facts(f"Location: {value}\nWe build freight software.").location == value


@pytest.mark.parametrize(
    ("value", "place"),
    [
        (
            "Burlington, MA (Boston) working 3 days in office (4 days preferred) U.S. "
            "residents only",
            "Burlington, MA (Boston)",
        ),
        ("Toronto, ON - 3 days in office", "Toronto, ON"),
        ("Austin, TX (three days a week in the office)", "Austin, TX"),
        ("Denver, CO, 2-3 days per week onsite", "Denver, CO"),
        ("Leeds, UK; UK residents only", "Leeds, UK"),
        ("Chicago, IL - US citizens only", "Chicago, IL"),
        ("Florence, SC (Hybrid, 3+ days onsite)", "Florence, SC"),
        ("Newark, NJ (2-3 days per week in our Newark office)", "Newark, NJ"),
        ("Ottawa and Toronto, Canada - Hybrid", "Ottawa and Toronto, Canada - Hybrid"),
    ],
)
def test_a_location_label_keeps_its_place_before_an_office_day_or_residency_clause(
    value: str, place: str
) -> None:
    assert posting_facts(f"Location: {value}\nWe build freight software.").location == place


@pytest.mark.parametrize(
    "rule",
    [
        "Must reside within approx. 50 miles of St. Louis.",
        "Must reside near Mt. Pleasant, SC.",
        "Must reside in Ft. Worth, TX.",
    ],
)
def test_an_abbreviation_does_not_end_a_residence_rule(rule: str) -> None:
    assert posting_facts(rule).residence_rule == rule


@pytest.mark.parametrize(
    "sentence",
    [
        "We believe in everyone's right to work with dignity.",
        "You will help clients understand whether they are eligible to work with us.",
        "Help members understand whether they are eligible to work while on benefits.",
        "Support customers who cannot sponsor their own benefits plan.",
        "Our clients don't sponsor events.",
        "We will not sponsor your conference travel.",
        "Familiarity with residence permit processes required.",
        "Must have completed a medical residency in the US required for licensure.",
        "Clinical residency required.",
        "Halcyon Data complies with all laws governing work authorization and employment "
        "eligibility verification requirements of the Immigration and Nationality Act.",
        "Halcyon Data will consider for employment all qualified applicants, including those "
        "with arrest and conviction records, in a manner consistent with the requirements of "
        "applicable state and local laws; Pursuant to the Immigration Reform and Control Act, "
        "all new hires must provide documentation of identity and work authorization within "
        "three days of hire.",
        "We help sponsors, CROs and sites run decentralized trials.",
        "Partner with the executive sponsor on roadmap decisions.",
        "Employer sponsored health plan with low or no premium.",
        "Comfort with PE sponsor reporting expectations.",
    ],
)
def test_a_sentence_about_something_else_states_no_residence_rule(sentence: str) -> None:
    body = f"Principal Data Analyst\n{sentence}\n"

    assert posting_facts(body).residence_rule == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "You must have the right to work in the UK.",
        "Applicants must be eligible to work in Canada.",
        "We do not sponsor.",
        "We are unable to sponsor visas for this role.",
        "We cannot provide sponsorship at this time.",
        "US residency is required.",
        "Residency in Canada is required.",
        "Sponsorship for this role is not guaranteed.",
        "Halcyon Data does not provide sponsorship for employment visa status "
        "(e.g., H-1B, OPT or TN status) for this position.",
        "Qualified candidates must be legally authorized to be employed in the United States.",
        "This job is not eligible for employment sponsorship.",
        "Applicants must not require employer sponsored work authorization now or in the future.",
        "This role is eligible for Halcyon Data-sponsored immigration support.",
        "Candidates must be authorized to work in the U.S. and complete employment "
        "eligibility verification (Form I-9) upon hire.",
    ],
)
def test_a_residence_or_sponsorship_rule_is_still_a_residence_rule(sentence: str) -> None:
    assert posting_facts(f"Principal Data Analyst\n{sentence}\n").residence_rule == sentence


def test_a_long_line_whose_abbreviation_hides_a_sentence_end_states_its_rule_sentence() -> None:
    stating = (
        "To be considered for employment, applicants must have a valid U.S. work "
        "authorization allowing work without restrictions with Halcyon Data in the U.S."
    )
    line = (
        f"{stating} At this time, we are unable to provide support or provide "
        "sponsorship for immigration benefits such as work visas."
    )

    assert len(line) > 250
    assert posting_facts(f"Principal Data Analyst\n{line}\n").residence_rule == stating


def test_a_dotted_country_abbreviation_inside_a_long_rule_does_not_cut_the_rule_short() -> None:
    sentence = (
        "This is a remote position open to candidates across our regional service area, and "
        "because of state payroll registration and regular travel to client sites, you must "
        "reside in the U.S. State of Texas or Oklahoma to be considered."
    )

    assert len(sentence) > 200
    assert posting_facts(f"Principal Data Analyst\n{sentence}\n").residence_rule in {"", sentence}


@pytest.mark.parametrize("form", ["Form W-2", "Form W 2", "form W2"])
def test_the_w2_tax_form_states_no_payroll_arrangement(form: str) -> None:
    assert posting_facts(f"Knowledge of {form} processing.").payroll_arrangement == ""


_SLUGS = (
    "after-sales-service-engineer",
    "when-data-meets-care",
    "before-you-apply",
    "until-further-notice-analyst",
    "if-you-love-data",
    "once-upon-a-team",
)


@pytest.mark.parametrize(
    ("slug", "status", "tail"),
    list(product(_SLUGS, ("404", "410"), ("", ".", ". If the posting moved, pass the new URL."))),
)
def test_a_reader_http_note_closes_whatever_its_url_slug_says(
    slug: str, status: str, tail: str
) -> None:
    assert closure_signal(f"https://jobs.example.com/job/{slug}/4412 returned HTTP {status}{tail}")


@pytest.mark.parametrize(
    ("chrome", "phrase"),
    list(
        product(
            (
                "Job Search Results When you are ready apply below",
                "Careers Before you apply Benefits",
                "Sign in if you already have an account",
                "Home > Jobs > After Sales Manager",
            ),
            (
                "The job you requested was not found",
                "This listing has expired",
                "We cannot find this position",
                "No longer accepting applications",
            ),
        )
    ),
)
def test_a_board_closure_phrase_closes_after_any_page_chrome(chrome: str, phrase: str) -> None:
    assert closure_signal(f"{chrome} {phrase}")


@pytest.mark.parametrize(
    "rule",
    [
        f"Completion of a {place} residency in {specialty} is required."
        for place in ("US", "U.S.", "Canadian")
        for specialty in ("internal medicine", "family medicine", "pharmacy")
    ]
    + [
        f"{specialty} residency in the {place} is required."
        for specialty in ("Psychiatry", "Pediatric", "Surgical", "Pharmacy")
        for place in ("US", "United States", "UK")
    ]
    + [f"PGY{year} residency required." for year in ("1", "2", "-1")]
    + ["Residency program in the US required.", "A completed US residency is required."],
)
def test_a_training_residency_states_no_residence_rule(rule: str) -> None:
    assert posting_facts(f"Clinical Pharmacist\n{rule}\n").residence_rule == ""


_READ_AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
_END_LABELS = (
    "Posting End Date:",
    "Posting Close Date:",
    "Closing Date:",
    "Close Date:",
    "Job Posting End Date:",
    "Posting Expiry Date:",
    "Application Deadline:",
    "Apply By",
    "Applications Close",
    "**Posting End Date:**",
    "- Closing Date -",
)
_DATE_FORMATS: tuple[Callable[[date], str], ...] = (
    lambda day: day.strftime("%d-%b-%Y").upper(),
    lambda day: day.strftime("%d %b %Y"),
    lambda day: f"{day.strftime('%B')} {day.day}, {day.year}",
    lambda day: f"{day.day} {day.strftime('%B')} {day.year}",
    lambda day: day.isoformat(),
)
_END_DAYS = (
    (date(2021, 10, 29), True),
    (date(2026, 9, 26), True),
    (date(2026, 9, 27), False),
    (date(2026, 12, 1), False),
)


def _end_date_body(line: str) -> str:
    return f"Senior Data Analyst\nExample Co\n{line}\nBuild dashboards for our field teams.\n"


@pytest.mark.parametrize(
    ("label", "form", "day", "passed"),
    [
        (label, form, day, passed)
        for label, form in product(_END_LABELS, _DATE_FORMATS)
        for day, passed in _END_DAYS
    ],
)
def test_a_labelled_posting_end_date_closes_only_once_it_has_passed(
    label: str, form: Callable[[date], str], day: date, passed: bool
) -> None:
    body = _end_date_body(f"{label} {form(day)}")

    assert publisher_closing_date(body).has_passed(_READ_AT) is passed


@pytest.mark.parametrize(
    "line",
    [
        "Contract End Date: 01-JAN-2020",
        "Assignment End Date: 01-JAN-2020",
        "Term End Date: 01-JAN-2020",
        "Start Date: 01-JAN-2020",
        "Program Application Deadline: 01-JAN-2020",
        "Project Close Date: 01-JAN-2020",
        "Posting End Date:",
        "Posting End Date: Open until filled",
        "Posting End Date: 10/03/2020",
        "Posting End Date: 30-FEB-2020",
        "Apply by sending your CV before 01-JAN-2020",
        "Please apply by 01-JAN-2020",
    ],
)
def test_a_date_that_is_not_the_postings_own_close_leaves_it_open(line: str) -> None:
    assert publisher_closing_date(_end_date_body(line)).has_passed(_READ_AT) is not True


@pytest.mark.parametrize("gap", ["\n", "\n\n", ":\n", ":\n\n"])
def test_a_posting_end_date_on_the_line_after_its_label_is_still_read(gap: str) -> None:
    body = _end_date_body(f"Application deadline{gap}29-OCT-2021")

    assert publisher_closing_date(body).has_passed(_READ_AT) is True


def test_a_year_less_date_after_its_label_is_never_guessed() -> None:
    assert publisher_closing_date(_end_date_body("Apply by\n14 March")).has_passed(_READ_AT) is None


def test_two_end_labels_that_disagree_leave_the_posting_open() -> None:
    body = _end_date_body("Posting End Date: 29-OCT-2021\nApplication Deadline: 30-DEC-2026")

    assert publisher_closing_date(body).has_passed(_READ_AT) is None


def test_two_end_labels_that_agree_close_the_posting() -> None:
    body = _end_date_body("Posting End Date: 29-OCT-2021\nApplication Deadline: October 29, 2021")

    assert publisher_closing_date(body).has_passed(_READ_AT) is True


@pytest.mark.parametrize(
    ("value", "normalized", "precision", "zone", "uncertain"),
    [
        ("2026-10-19", "2026-10-19", "date", None, False),
        ("2026-10-19T17:30:00-06:00", "2026-10-19T17:30:00-06:00", "timestamp", "-06:00", False),
        ("2026-10-19T17:30:00Z", "2026-10-19T17:30:00+00:00", "timestamp", "+00:00", False),
        ("2026-10-19T17:30:00", "2026-10-19T17:30:00", "timestamp", None, False),
        ("2026-02-30", None, None, None, True),
        ("2026-10-19T17:30:00+06:99", None, None, None, True),
        ("2026-10-19T17:30:00+00:60", None, None, None, True),
        ("2026-10-19T17:30:00-00:60", None, None, None, True),
        ("2026-10-19T17:30:00+24:00", None, None, None, True),
        ("10/11/2026", None, None, None, True),
        ("19 October", None, None, None, True),
    ],
)
def test_publisher_closing_date_preserves_precision_and_missing_timezone(
    value: str, normalized: str | None, precision: str | None, zone: str | None, uncertain: bool
) -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    fact = publisher_closing_date(
        "", valid_through=value, source_url="https://example.test/jobs/42", observed_at=_READ_AT
    )

    assert fact.normalized_value == normalized
    assert fact.precision == precision
    assert fact.timezone == zone
    assert fact.needs_clarification is uncertain
    assert fact.statements[0].wording == value
    assert fact.statements[0].basis == "valid_through"
    assert fact.source_url == "https://example.test/jobs/42"
    assert fact.observed_at == _READ_AT


@pytest.mark.parametrize(
    ("label", "value"),
    [(label, form(date(2026, 10, 19))) for label, form in product(_END_LABELS, _DATE_FORMATS)],
)
def test_labelled_closing_dates_retain_original_publisher_wording(label: str, value: str) -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    wording = f"{label} {value}"
    fact = publisher_closing_date(_end_date_body(wording), observed_at=_READ_AT)

    assert fact.normalized_value == "2026-10-19"
    assert fact.precision == "date"
    assert not fact.needs_clarification
    assert fact.statements[0].wording == wording
    assert fact.statements[0].basis == "body_label"


def test_conflicting_closing_dates_are_not_resolved_to_the_earliest() -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    fact = publisher_closing_date(
        _end_date_body("Closing date: October 20, 2026"),
        valid_through="2026-10-19",
        observed_at=_READ_AT,
    )

    assert fact.needs_clarification
    assert fact.normalized_value is None
    assert len(fact.statements) == 2


@pytest.mark.parametrize("value", ["Until filled", "Open until filled", "Rolling applications"])
def test_rolling_closing_statements_are_retained_without_an_invented_date(value: str) -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    fact = publisher_closing_date(_end_date_body(f"Closing date: {value}"), observed_at=_READ_AT)

    assert fact.normalized_value is None
    assert not fact.needs_clarification
    assert fact.statements[0].wording == f"Closing date: {value}"


@pytest.mark.parametrize(
    "line",
    [
        "Project deadline: October 19, 2026",
        "Contract End Date: 2026-10-19",
        "Interview date: October 19, 2026",
        "Benefits enrollment closes October 19, 2026",
    ],
)
def test_other_dates_are_not_publisher_closing_dates(line: str) -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    fact = publisher_closing_date(_end_date_body(line), observed_at=_READ_AT)

    assert fact.statements == ()
    assert fact.normalized_value is None
    assert not fact.needs_clarification


def test_a_body_time_suffix_is_never_silently_dropped() -> None:
    from clearscout.application.discovery.posting_text import publisher_closing_date

    fact = publisher_closing_date(
        _end_date_body("Closing date: October 19, 2026 at 5 pm Mountain"), observed_at=_READ_AT
    )

    assert fact.needs_clarification
    assert fact.normalized_value is None
    assert "5 pm Mountain" in fact.statements[0].wording


@pytest.mark.parametrize(
    ("value", "normalized", "precision", "zone", "day"),
    [
        ("2026-09-25", "2026-09-25", "date", None, date(2026, 9, 25)),
        ("2026-9-5", "2026-09-05", "date", None, date(2026, 9, 5)),
        (
            "2026-10-15T17:55:00Z",
            "2026-10-15T17:55:00+00:00",
            "timestamp",
            "+00:00",
            date(2026, 10, 15),
        ),
        (
            "2026-10-15T17:55:00+0000",
            "2026-10-15T17:55:00+00:00",
            "timestamp",
            "+00:00",
            date(2026, 10, 15),
        ),
        (
            "2026-09-29T17:43:08.392+00:00",
            "2026-09-29T17:43:08.392000+00:00",
            "timestamp",
            "+00:00",
            date(2026, 9, 29),
        ),
        # The calendar day is the UTC day, as first_seen_on is.
        (
            "2026-09-01T22:04:50-04:00",
            "2026-09-01T22:04:50-04:00",
            "timestamp",
            "-04:00",
            date(2026, 9, 2),
        ),
        ("1789000000000", "2026-09-10T00:26:40+00:00", "timestamp", "+00:00", date(2026, 9, 10)),
        (
            "Thu Sep 17 02:00:00 UTC 2026",
            "2026-09-17T02:00:00+00:00",
            "timestamp",
            "+00:00",
            date(2026, 9, 17),
        ),
        # Another zone abbreviation is ambiguous, so the timestamp stays naive.
        (
            "Thu Sep 17 02:00:00 EDT 2026",
            "2026-09-17T02:00:00",
            "timestamp",
            None,
            date(2026, 9, 17),
        ),
        ("Oct 21, 2026", "2026-10-21", "date", None, date(2026, 10, 21)),
    ],
)
def test_publisher_posted_dates_normalize_each_board_format(
    value: str, normalized: str, precision: str, zone: str | None, day: date
) -> None:
    from clearscout.application.discovery.posting_text import publisher_posted_date
    from clearscout.domain.staged_facts import PostedDateGrade

    fact = publisher_posted_date(value, basis="published", grade=PostedDateGrade.FIRST_POSTED)

    assert fact is not None
    assert fact.earliest.wording == value
    assert fact.earliest.basis == "published"
    assert fact.earliest.normalized_value == normalized
    assert fact.earliest.precision == precision
    assert fact.earliest.timezone == zone
    assert fact.day == day
    assert not fact.needs_clarification


@pytest.mark.parametrize("value", ["Posted 30+ Days Ago", "Posted Today", "about 4 hours", " "])
def test_relative_posted_text_is_never_a_posted_date(value: str) -> None:
    from clearscout.application.discovery.posting_text import publisher_posted_date
    from clearscout.domain.staged_facts import PostedDateGrade

    assert publisher_posted_date(value, basis="postedOn", grade=PostedDateGrade.BOARD_DATE) is None


@pytest.mark.parametrize("value", ["2026-02-30", "2026-10-19T17:30:00+24:00", "Week 40 of 2026"])
def test_an_unreadable_posted_date_keeps_its_wording_and_needs_clarification(value: str) -> None:
    from clearscout.application.discovery.posting_text import publisher_posted_date
    from clearscout.domain.staged_facts import PostedDateGrade

    fact = publisher_posted_date(value, basis="datePosted", grade=PostedDateGrade.BOARD_DATE)

    assert fact is not None
    assert fact.earliest.wording == value
    assert fact.needs_clarification
    assert fact.day is None


@pytest.mark.parametrize(
    ("subject", "predicate", "place"),
    list(
        product(
            ("This role", "The position", "This job", "The opportunity"),
            ("is based in", "is located in", "will be based in", "is based out of"),
            ("Lakeside, Poland", "Fairview, TX", "Ridgeton, Ontario"),
        )
    ),
)
def test_a_sentence_placing_the_role_states_its_place(
    subject: str, predicate: str, place: str
) -> None:
    text = f"Operations Lead\n\nWe ship parcels. {subject} {predicate} {place}. Apply today."

    assert role_based_place(text) == place


@pytest.mark.parametrize(
    "sentence",
    [
        "This role is based in Boston or Tokyo.",
        "This role is based in the US/UK.",
        "This role is based in London and New York.",
        "This role is based in Lakeside, Poland or remote.",
        "This role is based in Lakeside, Poland, or can be done remotely.",
        "This role can be based in Lakeside, Poland.",
        "This role may be based in Lakeside, Poland.",
        "This role could be located in Lakeside, Poland.",
        "This role is based in our office.",
        "This role is located at our headquarters.",
        "This role is located in our downtown office.",
        "This role is based remotely.",
        "This role is located on-site.",
        "This role is based in Remote.",
        "This role is not based in Lakeside, Poland.",
        "The team is based in Birmingham.",
        "Our company is headquartered in Cambridge.",
        "Our company is located in Lakeside, Poland.",
        "We are located in Georgia.",
        "London, Ontario office culture is collaborative.",
    ],
)
def test_a_sentence_that_does_not_place_the_role_alone_states_no_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == ""


@pytest.mark.parametrize(
    ("subject", "facility", "trailer"),
    list(
        product(
            (
                "This is a permanent role based in",
                "This is an exciting full-time position located in",
                "This is a job based out of",
                "This role is based in",
            ),
            (
                "",
                "our Global Capability Centre in ",
                "the regional office in ",
                "our new engineering hub in ",
                "our HQ in ",
            ),
            (
                "",
                " and hybrid working with the expectation for some office presence",
                " with flexible hours",
                ", with some travel",
                " where you will join the carrier desk",
            ),
        )
    ),
)
def test_a_role_sentence_naming_a_facility_or_a_clause_states_the_place(
    subject: str, facility: str, trailer: str
) -> None:
    text = f"Operations Lead\n\n{subject} {facility}Lakeside, Poland{trailer}. Apply today."

    assert role_based_place(text) == "Lakeside, Poland"


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a permanent role based in our office in Lakeside or our hub in Fairview.",
        "This is a permanent role based in one of our offices in Lakeside, Poland.",
        "This is a role that can be based in Lakeside, Poland.",
        "This is a permanent role based in our office.",
        "This is a permanent role based in our headquarters.",
        "This is a permanent role based in Lakeside, Poland or remotely.",
        "This is a permanent role based in our offices across Europe.",
        "This is a permanent role based in Lakeside and Fairview.",
        "This is a permanent role based in Lakeside/Fairview.",
        "This role is based in our centre in Lakeside or Fairview.",
    ],
)
def test_a_role_sentence_naming_no_single_facility_place_states_no_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == ""


_ROLE_SUBJECTS = (
    "This role is based in",
    "This is a permanent role based in",
    "The position is located in",
    "This job will be based out of",
)


@pytest.mark.parametrize(
    ("subject", "tail"),
    list(
        product(
            _ROLE_SUBJECTS,
            (
                ", with remote work possible from Canada",
                ", with the option to work fully remotely from anywhere",
                " and remote candidates in Canada are welcome",
                " and remote-friendly",
                ", where possible, but remote is fine",
                " and remote work two days a week",
                " with work from home available",
                ", with remote considered",
                " (preferred)",
                ", though we are flexible on location",
                " though we are flexible about where you work",
                ", depending on candidate location",
                " depending on where the successful candidate lives",
                ", subject to candidate location",
                " but can be performed anywhere in North America",
                ", but candidates elsewhere will be considered",
                ", although other locations are possible",
                ", unless you prefer to work elsewhere",
                " (ideally)",
                ", location negotiable",
            ),
        )
    ),
)
def test_a_role_place_followed_by_a_remote_offer_or_a_hedge_states_no_place(
    subject: str, tail: str
) -> None:
    text = f"Operations Lead\n\n{subject} Lakeside, Poland{tail}. Apply today."

    assert role_based_place(text) == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "If the role is based in Lakeside, Poland, a Lakeside weighting applies.",
        "Where the role is based in Lakeside, Poland, local benefits apply.",
        "When this position is based in Ireland, a relocation package is offered.",
        "Should the position be located in Lakeside, Poland, travel is covered.",
        "Benefits vary, and once the role is based in Lakeside, Poland, local terms apply.",
        "Pay differs if this role is located in Lakeside, Poland.",
        "Unless the role is based in Fairview, TX, no relocation is offered.",
    ],
)
def test_a_sentence_anticipating_where_the_role_is_based_states_no_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "You must live in the country the position is located in order to be considered.",
        "Applicants must reside in the region the role is based in for tax reasons.",
        "We hire in each city the job is located in today.",
        "Tell us which office the role is based in and why.",
        "Candidates near the site this role is based out of are preferred.",
    ],
)
def test_a_relative_clause_about_where_the_role_sits_states_no_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "This is a fully remote position based in London, UK.",
        "This is a remote role based in Lakeside, Poland.",
        "This is a remote-first job located in Fairview, TX.",
        "This is a permanent remote position based out of Ridgeton, Ontario.",
        "This is a work-from-home role based in Lakeside, Poland.",
        "This is a fully distributed role based in Lakeside, Poland.",
    ],
)
def test_a_remote_role_with_a_nominal_base_states_no_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == ""


_LIST_MARKERS = (
    "-",
    "*",
    "+",
    "\u2022",
    "\u2023",
    "\u25cf",
    "\u25aa",
    "\u2013",
    "\u2014",
    "1.",
    "2)",
    "a)",
)
_FACILITY_SENTENCE = (
    "This is a permanent role based in our Global Capability Centre in Lakeside, Poland"
    " and hybrid working with the expectation for some office presence."
)


@pytest.mark.parametrize(
    ("marker", "heading", "after"),
    list(
        product(
            _LIST_MARKERS,
            ("", "Operations Lead\n\n"),
            ("", " Flexibility may be required in line with business need."),
        )
    ),
)
def test_a_role_place_opening_a_list_item_states_the_place(
    marker: str, heading: str, after: str
) -> None:
    text = f"{heading}{marker} {_FACILITY_SENTENCE}{after}"

    assert role_based_place(text) == "Lakeside, Poland"


@pytest.mark.parametrize("marker", _LIST_MARKERS)
def test_a_list_item_opening_a_relative_clause_states_no_place(marker: str) -> None:
    text = (
        f"Operations Lead\n\n{marker} the country the position is located in order to be"
        " considered."
    )

    assert role_based_place(text) == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "Please note: this role is based in Lakeside, Poland.",
        "We ship parcels, and this role is based in Lakeside, Poland.",
        "Great news; the position is located in Lakeside, Poland.",
    ],
)
def test_a_role_place_after_a_clause_boundary_states_the_place(sentence: str) -> None:
    assert role_based_place(f"Operations Lead\n\n{sentence} Apply today.") == "Lakeside, Poland"
