"""The shared text sanitizer folds board character-noise to ASCII."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tailorcv.shared import text
from tailorcv.shared.text import sanitize, sanitize_with_offsets


def test_smart_quotes_fold_to_straight() -> None:
    source = "what you" + chr(0x2019) + "ll need and the " + chr(0x201C) + "role" + chr(0x201D)
    assert sanitize(source) == 'what you\'ll need and the "role"'


def test_non_breaking_and_narrow_spaces_become_ordinary_spaces() -> None:
    source = "Senior" + chr(0x00A0) + "Engineer" + chr(0x202F) + "role"
    assert sanitize(source) == "Senior Engineer role"


def test_zero_width_characters_are_removed_so_a_word_matches() -> None:
    source = "remote" + chr(0x200B) + "-first" + chr(0xFEFF)
    assert sanitize(source) == "remote-first"


def test_html_entities_are_decoded() -> None:
    assert sanitize("Data &amp; Platform &#39;lead&#39;") == "Data & Platform 'lead'"


def test_dash_glyphs_fold_to_a_hyphen() -> None:
    source = "pages 10" + chr(0x2013) + "20 and state" + chr(0x2014) + "of-the-art"
    assert sanitize(source) == "pages 10-20 and state-of-the-art"


@pytest.mark.parametrize(
    ("source", "folded"),
    [
        ("\uff13\uff0d\uff15 \uff59\uff45\uff41\uff52\uff53", "3-5 years"),
        ("\uff21\uff22\uff23 \uff05\uff0b", "ABC %+"),
        ("\uffe1120,000 and \uffe5900", "\u00a3120,000 and \u00a5900"),
        ("3\u301c5 days", "3~5 days"),
        ("3\uff5e5 days", "3~5 days"),
        ("3\u223c5 days", "3~5 days"),
        ("3\u30305 days", "3~5 days"),
        ("3\u20535 days", "3~5 days"),
    ],
)
def test_fullwidth_forms_and_range_tildes_fold_to_ascii(source: str, folded: str) -> None:
    assert sanitize(source) == folded


@pytest.mark.parametrize(
    ("source", "folded"),
    [("R&amp;amp;D", "R&D"), ("R\uff06amp;D", "R&D"), ("&amp;#8211;", "-")],
)
def test_an_entity_that_decodes_to_another_entity_decodes_fully(source: str, folded: str) -> None:
    assert sanitize(source) == folded


_NOISY = st.lists(
    st.sampled_from(
        [
            *"aZ09&#;x- ~",
            "&amp;",
            "&#8211;",
            "&lt;",
            "&nbsp;",
            "&#39;",
            "\u00a0",
            "\u2013",
            "\u2019",
            "\u200b",
            "\u301c",
            "\uff06",
            "\uff13",
            "\uff5e",
            "\uffe1",
        ]
    ),
    max_size=20,
).map("".join)


@given(_NOISY)
@example("R&amp;amp;D")
@example("\uff06amp;")
def test_folding_is_idempotent(text: str) -> None:
    once = sanitize(text)
    assert sanitize(once) == once


@given(_NOISY)
@example("R&amp;amp;D &#x26;lt;")
@example("&amp;&#8211;\uff06amp;")
@example("&#\uff13")
@example("&#3\u200b9;")
def test_offset_folding_matches_the_sanitizer_and_keeps_raw_order(text: str) -> None:
    folded, spans = sanitize_with_offsets(text)
    assert folded == sanitize(text)
    assert len(spans) == len(folded)
    assert spans == sorted(spans)
    assert all(0 <= start < end <= len(text) for start, end in spans)


def test_each_decoded_character_maps_to_its_whole_raw_entity() -> None:
    raw = "R&amp;D&nbsp;&#39;x&#39;y&#59;"
    folded, spans = sanitize_with_offsets(raw)
    assert folded == "R&D 'x'y;"
    assert [raw[start:end] for start, end in spans] == [
        "R",
        "&amp;",
        "D",
        "&nbsp;",
        "&#39;",
        "x",
        "&#39;",
        "y",
        "&#59;",
    ]


@pytest.mark.parametrize(
    ("escaped", "folded"),
    [
        ("&" + "amp;" * 20000 + "lt;", "<"),
        ("R&" + "AMP;" * 20000 + "D", "R&D"),
        ("\uff06" + "amp;" * 20000 + "#8211;", "-"),
    ],
)
def test_a_long_run_of_escaped_ampersands_decodes_to_one(escaped: str, folded: str) -> None:
    assert sanitize(escaped) == folded


def test_line_structure_is_preserved() -> None:
    source = "Requirements:\n" + chr(0x2022) + " Five years\n\nNext"
    assert sanitize(source) == "Requirements:\n" + chr(0x2022) + " Five years\n\nNext"


def test_is_idempotent_and_leaves_clean_text_unchanged() -> None:
    clean = "Senior Data Engineer\nRemote - United States\n- Five years of experience"
    assert sanitize(clean) == clean
    assert sanitize(sanitize(clean)) == clean


def test_empty_text_passes_through() -> None:
    assert sanitize("") == ""


@pytest.mark.parametrize(
    "prose",
    [
        "Ph.D. in chemistry or a related field",
        "M.S. or B.S. in statistics",
        "Licensed in the U.S. Insurance background helps",
        "Reports to the Sr. Manager of operations",
        "Acme Inc. Offices are downtown",
        "Tools such as SQL, Python, etc. are used daily",
        "Clinical workflows (e.g. triage) and intake",
        "Clinical workflows, i.e. triage and intake",
        "Five years. then more",
        "Unit No. 5 is downtown",
    ],
)
def test_an_abbreviation_or_a_lowercase_continuation_ends_no_sentence(prose: str) -> None:
    assert text.sentences(prose) == [prose]


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        ("SQL is required. Python is a plus.", ["SQL is required.", "Python is a plus."]),
        ("Remote in Canada!  5 days a week.", ["Remote in Canada!", "5 days a week."]),
        ("Is it hybrid? (Yes) it is.", ["Is it hybrid?", "(Yes) it is."]),
        (
            "Associate I. Additional responsibilities include triage.",
            ["Associate I.", "Additional responsibilities include triage."],
        ),
        ("Analyst II. Duties include intake.", ["Analyst II.", "Duties include intake."]),
        ("Pharmacist III. Duties include intake.", ["Pharmacist III.", "Duties include intake."]),
        ("Hired at Level I. The team grows.", ["Hired at Level I.", "The team grows."]),
        ("Work on Plan B. Then report.", ["Work on Plan B.", "Then report."]),
        ("Use SQL, Python, etc. Report weekly.", ["Use SQL, Python, etc.", "Report weekly."]),
        (
            "Sectors (retail, logistics, etc.) Experience helps.",
            ["Sectors (retail, logistics, etc.)", "Experience helps."],
        ),
        ('He said "Ship it." Then it shipped.', ['He said "Ship it."', "Then it shipped."]),
    ],
)
def test_a_sentence_ends_before_a_capital_or_a_figure(prose: str, expected: list[str]) -> None:
    assert text.sentences(prose) == expected


@pytest.mark.parametrize(
    "tag", ["#LI-REMOTE", "#LI-Remote", "#LI-HYBRID", "#LI-ONSITE", "#LI-DNI", "#LI-AB1"]
)
@pytest.mark.parametrize("before", ["Familiarity with scheduling tools", "Report weekly."])
def test_a_recruiter_tag_stands_as_its_own_sentence(tag: str, before: str) -> None:
    after = "Any data provided is stored under our Privacy Policy."
    assert text.sentences(f"{before} {tag} {after}") == [before, tag, after]
    assert text.sentences(f"{before} {tag}") == [before, tag]


_SHORT_WORDS = ("incl", "esp", "min", "max", "yrs", "req", "pref")


@pytest.mark.parametrize(
    "word", [form for word in _SHORT_WORDS for form in (word, word.capitalize(), word.upper())]
)
@pytest.mark.parametrize("following", ["Level 2 tools", "3 years", "SQL and Python"])
def test_a_short_word_ending_in_a_period_ends_no_sentence(word: str, following: str) -> None:
    prose = f"Experience with BI tools {word}. {following} preferred"
    assert text.sentences(prose) == [prose]


_INITIALISMS = ("U.S.", "U.K.", "E.U.", "U.S.A.")
_CLAUSE_OPENINGS = (
    "This role is hybrid.",
    "The team is remote.",
    "We hire widely.",
    "You will travel.",
    "Our office is downtown.",
    "Must hold a licence.",
    "Candidates apply online.",
    "Everyone attends on Mondays.",
)
_NAME_CONTINUATIONS = (
    "Citizen status is required.",
    "Army veterans are welcome.",
    "Department of Labor rules apply.",
    "Insurance background helps.",
    "Remote roles are listed here.",
)


@pytest.mark.parametrize("initialism", _INITIALISMS)
@pytest.mark.parametrize("opening", _CLAUSE_OPENINGS)
def test_an_initialism_ends_a_sentence_before_a_word_opening_a_clause(
    initialism: str, opening: str
) -> None:
    head = f"Teams work across the {initialism}"
    assert text.sentences(f"{head} {opening}") == [head, opening]


@pytest.mark.parametrize("initialism", _INITIALISMS)
@pytest.mark.parametrize("continuation", _NAME_CONTINUATIONS)
def test_an_initialism_stays_whole_before_a_name_it_opens(
    initialism: str, continuation: str
) -> None:
    prose = f"Teams work across the {initialism} {continuation}"
    assert text.sentences(prose) == [prose]


@pytest.mark.parametrize("company", ["Inc.", "Ltd.", "LLC.", "Co.", "Corp."])
def test_a_company_word_ends_a_sentence_before_a_word_opening_a_clause(company: str) -> None:
    assert text.sentences(f"We are Acme {company} The role is hybrid.") == [
        f"We are Acme {company}",
        "The role is hybrid.",
    ]


@pytest.mark.parametrize("answer", ["No.", "no.", "NO."])
def test_no_before_a_word_is_an_answer_that_ends_its_sentence(answer: str) -> None:
    assert text.sentences(f"Visa sponsorship: {answer} Must be authorized to work.") == [
        f"Visa sponsorship: {answer}",
        "Must be authorized to work.",
    ]


@pytest.mark.parametrize("tag", ["#LI-Remote", "#LI-HYBRID", "#LI-AB1"])
def test_a_recruiter_tag_inside_a_sentence_stays_in_it(tag: str) -> None:
    prose = f"Mention {tag} when you apply."
    assert text.sentences(prose) == [prose]
