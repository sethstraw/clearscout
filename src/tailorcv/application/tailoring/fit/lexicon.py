"""Vocabulary and thresholds used by the deterministic fit reading."""

from __future__ import annotations

import re

from tailorcv.application.tailoring.stages.match import EXAMPLE_CUES, GATE_OVERLAP
from tailorcv.domain.countries import SUBDIVISION_CODES
from tailorcv.domain.requirements import PREFERRED_MARKERS
from tailorcv.domain.search_parameters import ARRANGEMENTS, CompensationBasis
from tailorcv.domain.search_parameters import LEVELS as LEVEL_ORDER
from tailorcv.domain.taxonomy import SEPARATOR, says
from tailorcv.shared.quantities import (
    DAYS_PER_MONTH,
    FIGURE,
    WEEKS_PER_MONTH,
    read_figure,
    stated_days,
    unit,
)
from tailorcv.shared.text import sentence_breaks, split_at

NOT_STATED = "not stated"

MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*$")
# Dash, asterisk, plus, and the round/square glyph bullets boards use; the
# sanitizer already folds dash-glyph bullets to "-", so only glyphs remain here.
_BULLET_GLYPHS = "".join(
    chr(code)
    for code in (0x2022, 0x2023, 0x2043, 0x2219, 0x25CF, 0x25CB, 0x25AA, 0x25AB, 0x25E6, 0x25A0)
)
BULLET = re.compile(rf"^(?:[-*+{_BULLET_GLYPHS}]|\d+[.)])\s+(?P<text>.+?)\s*$")
SPELLED_YEARS = dict(
    zip(
        [
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
        ],
        range(1, 16),
        strict=True,
    )
)
RANGE_SEPARATOR = f"(?:-|{chr(0x2013)}|{chr(0x2014)}|to)"
# A years range adds the words a sentence joins two figures with to the pay separators.
_YEARS_SEP = rf"\s*(?:-to-|{RANGE_SEPARATOR}|\bor\b|\bthrough\b|\band\b)\s*"
_SPELLED = "|".join(SPELLED_YEARS)
SPELLED_FIGURE = rf"\b(?:{_SPELLED})\b"
_WORD = rf"{SPELLED_FIGURE}(?:\s*\(\d{{1,2}}\+?\))?"
_PLUS = r"(?:\s*\+|\s*-?\s*plus\b|\s+or\s+more\b)?"
_UNIT = r"(?i:years?|yrs?)\b\.?"
_NEXT = r"['\u2019]?\s*"
# "of", "in" or an experience word right after the unit, or a possessive unit whose
# experience word follows a few words later: "two years' clinical research experience".
_EXPERIENCE_NEXT = (
    rf"(?={_NEXT}(?:of|in|with|experience|professional|relevant)\b"
    r"|['\u2019]\s*(?:[\w-]+\s+){1,3}experience\b)"
)
_STUDY = (
    r"(?:an?\s+(?:accredited|degree|graduate\s+program|doctoral\s+program)|(?:under)?graduate|college"
    r"|university|school(?:ing)?|stud(?:y|ies)|coursework"
    r"|(?:higher\s+)?education(?!\s+(?!(?:in|at|from|or|and)\b)\w))"
)
# The work a years figure is spent on, read right after it. Study after "in" or "of" is
# schooling, "of" before an age, a hire date or a determiner is a deadline or an age, and
# "education" is study only as the head noun, so "education technology" is work.
WORK_NEXT = re.compile(
    rf"{_NEXT}(?:as|with|in(?!-|\s+{_STUDY}\b)|of(?!\s+(?:age|hire|employment|service|joining"
    rf"|start\w*|the|a|an|this|that|our|your|their|its|{_STUDY})\b)|working|leading"
    r"|building|managing|designing|developing|owning|delivering|running|supporting)\b",
    re.IGNORECASE,
)
# A figure after "within", "every", "after", "commit to" or "for the first" is a deadline,
# a cadence or a tenure the role sets, not years the candidate brings.
DEADLINE_BEFORE = re.compile(
    r"\b(?:within|every|after|commit(?:ting|ment)?\s+to|(?:for|during)\s+the\s+first)"
    r"\s+(?:(?:a|the|at\s+least)\s+)?$",
    re.IGNORECASE,
)
# A range is one match whose figure is its first number. A figure or range with a spelled
# side counts only when an experience word follows, which keeps "one of the years" and
# "in five years we grew" out; an all-digit one needs no such word.
YEARS = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:{_PLUS}(?:{_YEARS_SEP}\d{{1,2}}{_PLUS})?\s*-?\s*{_UNIT}"
    rf"|(?i:{_PLUS}{_YEARS_SEP}{_WORD}{_PLUS}\s*{_UNIT}{_EXPERIENCE_NEXT}))"
    rf"|(?i:\b({_SPELLED})\b(?:\s*\(\d{{1,2}}\+?\))?{_PLUS}"
    rf"(?:{_YEARS_SEP}(?:\d{{1,2}}|{_WORD}){_PLUS})?\s*{_UNIT}{_EXPERIENCE_NEXT})"
)


def years_figure(match: re.Match[str]) -> int:
    """Return the figure a YEARS match states, written in digits or as a word."""
    return int(match.group(1)) if match.group(1) else SPELLED_YEARS[match.group(2).casefold()]


LABELLED_TITLE = re.compile(
    r"^(?:job\s+)?(?:title|position|role)\s*:\s*(?P<text>.+)$", re.IGNORECASE
)
TERMINAL = (".", "!", "?", ",", ";")
# A finished sentence, which ends a unit where a hard wrap does not.
SENTENCE_END = (".", "!", "?")
_CLAUSE_JOIN = re.compile(r";")
_PHRASE_JOIN = re.compile(r"[,;]|\b(?:and|but)\b", re.IGNORECASE)


def clause_breaks(text: str) -> list[tuple[int, int]]:
    """Return the span of each clause break in ``text``: a sentence end or a semicolon.

    A clause of nothing but a preference cue ("... license. Preferred") is the one before's.
    """
    breaks = sorted(
        [*sentence_breaks(text), *(found.span() for found in _CLAUSE_JOIN.finditer(text))]
    )
    ends = [start for start, _ in breaks[1:]] + [len(text)] * bool(breaks)
    return [
        (start, end)
        for (start, end), stop in zip(breaks, ends, strict=True)
        if not (
            (cues := preference_cues(text[end:stop])) and not names_beyond(text[end:stop], cues)
        )
    ]


def phrase_breaks(text: str) -> list[tuple[int, int]]:
    """Return the span of each phrase break: a clause break, a comma, "and" or "but"."""
    return sorted(
        [*sentence_breaks(text), *(found.span() for found in _PHRASE_JOIN.finditer(text))]
    )


def clauses(text: str) -> list[str]:
    """Return the clauses of ``text``, unstripped, split at each clause break."""
    return split_at(text, clause_breaks(text))


# Longer colon-terminated lines are treated as prose, not headings.
HEADING_WORDS = 10
BARE_HEADING_WORDS = 8

SAME_REQUIREMENT = GATE_OVERLAP

# Example cues without "including", which a degree list may open on.
EXAMPLE_CUE = re.compile(EXAMPLE_CUES, re.IGNORECASE)

# Three points require one title phrase or three body phrases.
TITLE_WEIGHT = 3
MINIMUM_FAMILY_EVIDENCE = 3
FAMILY_MARGIN = 2
MINIMUM_SPECIALTY_EVIDENCE = 2
SPECIALTY_MARGIN = 1

# Require two years before combining a level and experience reading.
LEVEL_TOLERANCE = 2

# Bound extracted evidence so it remains readable in reports.
QUOTE = 140

# Names that head a requirement section only when nothing else is their subject: "Your key
# strengths" does, "Our key strengths" and "What we look for in a vendor" do not.
CANDIDATE_HEADINGS = ("what we look for", "key strengths")
# Phrases that also open ordinary sentences ("To qualify for the bonus, ..."), so they name a
# section only as a heading line, never as a label run into prose.
CLAUSE_HEADINGS = ("you bring", "what do you need for", "you need to have", "to qualify for")
REQUIREMENT_HEADINGS = (
    "requirement",
    "qualification",
    "what you bring",
    "what you'll bring",
    "what you will bring",
    "what you will need",
    "what you'll need",
    "what you need",
    *CLAUSE_HEADINGS,
    "what you should have",
    "who you are",
    "you have",
    "must have",
    "skills and experience",
    "experience required",
    "we are looking for",
    "what we're looking for",
    *CANDIDATE_HEADINGS,
    "what we require",
    "about you",
    "required",
    "competencies",
    "qualities",
    "skills and abilities",
    "skills & abilities",
    "skills & experience",
    "education and experience",
    "education & experience",
)
PREFERRED_HEADINGS = (
    "preferred",
    "not required",
    "nice to have",
    "nice-to-have",
    "bonus",
    "a plus",
    "desirable",
    "desired",
    "would be great",
    "even better",
    "also great",
    "great to have",
    "great-to-have",
    "stand out",
)
# "Optional" heads a preferred section alone or before one section noun, never as a word
# of a longer heading: "Travel Optional" and "Optional Team Offsites" are not sections.
OPTIONAL_HEADING = re.compile(
    r"\W*optional(?:\W+(?:skills|qualifications|requirements|experience))?\W*", re.IGNORECASE
)
RESPONSIBILITY_HEADINGS = (
    "responsibilit",
    "what you will do",
    "what you'll do",
    "the role",
    "day to day",
    "day-to-day",
    "duties",
    "your impact",
    "in this role",
    "what success looks like",
    "first 90 days",
    "30/60/90",
    "your first year",
    "be doing",
)
# What the employer gives, never what the role asks of a person.
BENEFIT_HEADINGS = ("benefit", "perks", "compensation", "salary", "pay range")
BOILERPLATE_HEADINGS = (
    *BENEFIT_HEADINGS,
    "equal opportunity",
    "eeo",
    "diversity",
    "how to apply",
    "about us",
    "about the company",
    "about the team",
    "our values",
    "our mission",
    "accommodation",
    "legal",
    "privacy",
    "disclaimer",
    "why join",
    "physical requirement",
    "physical job requirement",
    "physical demands",
    "work environment",
    "working conditions",
    "who we are",
    "what does this mean",
)

# Legal and benefits boilerplate is excluded from requirement extraction.
BOILERPLATE_LINES = (
    "equal opportunity",
    "without regard to",
    "reasonable accommodation",
    "401(k)",
    "401 k",
    "we offer",
    "benefits include",
    "paid time off",
    "e-verify",
    "affirmative action",
    "drug free workplace",
    "drug-free workplace",
    "at-will employment",
    "applicants will receive consideration",
    "protected veteran",
    "annual compliance training",
)
# Benefits and legal words that can also be the subject of a qualification, as in
# "Experience administering 401k plans"; they mark boilerplate unless a
# QUALIFICATION_CUE opens the line and it names no pay.
BOILERPLATE_SUBJECTS = (
    "accommodation",
    "privacy policy",
    "privacy notice",
    "401k",
    "employer match",
    "benefits package",
    "company benefits",
    "benefits to support",
    "perks and benefits",
    "competitive pay",
    "competitive compensation",
)

# A first line that opens with company prose rather than naming the role. Keyed
# on the employer-sentence phrasing, not on length, so a long or bilingual title
# is still read as the title.
BLURB_CUES = (
    "we believe",
    "we are driven",
    "we combine",
    "mission-driven",
    "is dedicated to",
    "recognizes the importance",
    "our mission",
    "we're a team",
    "we are a team",
    "we support",
    "we help",
    "for more information",
)

MUST_MARKERS = (
    "must have",
    "must be",
    "must possess",
    "is required",
    "are required",
    "required",
    "requires",
    "minimum of",
    "at least",
    "no less than",
    "mandatory",
    "you will need",
)
# "Beneficial to" names who gains, not a wish: "deciding what is beneficial to patients".
_BENEFIT_TO = re.compile(r"\bbeneficial\s+to\b", re.IGNORECASE)
# A sentence opened by a wish: "Ideally, you are based in", "Preferably based in".
WISH_OPENER = re.compile(r"\W*(?:ideally|preferably)\b", re.IGNORECASE)


# "Prior leadership experience ideal", "would be ideal": "ideal" closing its clause.
# "The ideal candidate has 5+ years" opens a requirement instead.
_IDEAL = re.compile(r"\bideal\s*(?:[.;,!)]|$)", re.IGNORECASE)
# "Desired" and "not required" wish only beside the one thing they qualify, a degree
# mention or a heading word; across a whole line "desired results" is an adjective.
MENTION_PREFERENCE_MARKERS = (*PREFERRED_MARKERS, "desired", "not required")


# The grammar around a preference cue, which names nothing: "is", "would be", "this is".
CUE_FILLER = frozenset(
    ("is", "are", "be", "would", "will", "also", "strongly", "highly", "very", "a", "an", "but")
)


def names_beyond(text: str, cues: tuple[str, ...]) -> bool:
    """Return whether ``text`` names something besides its preference ``cues``."""
    words = set(SEPARATOR.split(text.casefold())) - {"", "this", "it"} - CUE_FILLER
    for cue in cues:
        words -= set(SEPARATOR.split(cue.casefold()))
    return bool(words)


def preference_cues(text: str, cues: tuple[str, ...] = PREFERRED_MARKERS) -> tuple[str, ...]:
    """Return the preference cues ``text`` states, never counting "beneficial to"."""
    found = says(_BENEFIT_TO.sub("to", text), cues)
    return (*found, "ideal") if _IDEAL.search(text) else found


# A qualification the posting asks a person to hold. Under no requirement heading
# a stance marker alone is not enough (pay, mission, and legal prose carry one),
# so a headingless prose line must also name one of these to count.
QUALIFICATION_CUE = (
    "year",
    "experience",
    "degree",
    "bachelor",
    "master",
    "mba",
    "phd",
    "doctorate",
    "diploma",
    "certification",
    "certified",
    "licens",
    "proficien",
    "expertise",
    "expert in",
    "knowledge of",
    "familiar",
    "skill",
    "ability to",
    "background in",
    "track record",
    "fluent",
    "understanding of",
    "competen",
)

PEOPLE_MANAGEMENT = (
    "direct reports",
    "manage a team of",
    "managing a team of",
    "lead a team of",
    "leading a team of",
    "people management",
    "line management",
    "performance reviews",
    "performance review cycle",
    "hire and develop",
    "hiring and developing",
    "career development of",
    "supervise a team",
    "staff supervision",
    "manage engineers",
    "manage analysts",
    "your reports",
)
INDIVIDUAL_CONTRIBUTOR = (
    "individual contributor",
    "no direct reports",
    "without direct reports",
    "not a people management role",
    "this role does not manage",
)
MANAGEMENT_TITLES = (
    "manager",
    "director",
    "head of",
    "vp",
    "svp",
    "evp",
    "vice president",
    "chief",
    "supervisor",
)

# Separate inflated titles from senior responsibilities.
SUPERVISED = (
    "under close supervision",
    "under the direction of",
    "under supervision",
    "with guidance from",
    "entry level",
    "entry-level",
    "no prior experience",
)

# Use years only to detect conflicts between titles and experience floors.
LEVEL_EVIDENCE: dict[str, tuple[tuple[str, ...], int]] = {
    "intern": (("intern", "internship", "co-op", "co op"), 0),
    "junior": (("junior", "jr", "entry level", "graduate", "trainee", "apprentice"), 0),
    "associate": (("associate",), 1),
    "mid": (("mid level", "mid-level", "intermediate"), 2),
    "senior": (("senior", "sr"), 4),
    "lead": (("lead", "team lead"), 5),
    "staff": (("staff",), 6),
    "principal": (("principal", "distinguished"), 8),
    "director": (("director", "head of"), 8),
    "executive": (("vp", "svp", "evp", "vice president", "chief"), 10),
}
LEVELS: tuple[tuple[str, tuple[str, ...], int], ...] = tuple(
    (name, *LEVEL_EVIDENCE[name]) for name in LEVEL_ORDER
)

# An org-chart clause, up to the next comma or stop: a level token in "reports
# to the VP" names the manager's rank, while "a senior role, reporting to" keeps its own.
ORG_CONTEXT = re.compile(
    r"(?:\breport(?:s|ing)?\s+(?:directly\s+)?(?:to|into)\b|\bled\s+by\b|\bmanaged\s+by\b"
    r"|\breporting\s+line\b)[^,;.]*",
    re.IGNORECASE,
)
_RANK = (
    r"(?:c|vp|svp|evp|vice\s+president|chief|executive|director|senior|sr)"
    r"(?:[-\u2010\u2011 ](?:suite|level))?"
)
# The rank of a buyer, audience or supported leader: "SVP or C-level stakeholders",
# "presenting to the VP". It names the counterparty's level, never this role's.
COUNTERPARTY_RANK = re.compile(
    rf"\b{_RANK}(?:\s*(?:,|/|\bor\b|\band\b)\s*{_RANK})*\s+(?:(?:customer|client)\s+relationships"
    r"|stakeholders|audiences|sponsors"
    r"|decision[- ]makers|executives|leaders(?:hip)?|buyers|customers|clients|personas)\b"
    r"|\b(?:sell(?:s|ing)?\s+to|present(?:s|ing)?\s+to|support(?:s|ing)?"
    r"|relationships?\s+with|partner(?:s|ing)?\s+with|advis(?:e|es|ing)"
    r"|influenc(?:e|es|ing))\s+(?:(?:the|our|their|your)\s+)?"
    rf"{_RANK}(?:\s*(?:,|/|\bor\b|\band\b)\s*{_RANK})*\b",
    re.IGNORECASE,
)
# "Lead" as a role noun ("team lead", "lead engineer"), told apart from the far
# commoner body verb ("lead projects", "lead discussions"), which is not a level.
LEAD_AS_NOUN = re.compile(
    r"\b(?:team|tech(?:nical)?|engineering|squad|practice|group|project|data)\s+lead\b"
    r"|\blead\s+(?:engineer|developer|manager|scientist|analyst|designer|architect|product|data)\b",
    re.IGNORECASE,
)

# LinkedIn documents hyphenated arrangement tags ("#LI-Remote"); publisher bodies also
# write them with an underscore, no separator, or "On-site". They are tags, not words.
_LINKEDIN_TAG = r"#\s?li[-_]?"
_LINKEDIN_TAG_WORDS = {"remote": "remote", "hybrid": "hybrid", "on site": "on-?site"}
LINKEDIN_ARRANGEMENT_PATTERNS: dict[str, re.Pattern[str]] = {
    arrangement: re.compile(rf"(?<![a-z0-9_-]){_LINKEDIN_TAG}{word}(?![a-z0-9_-])", re.IGNORECASE)
    for arrangement, word in _LINKEDIN_TAG_WORDS.items()
}
LINKEDIN_ARRANGEMENT_TOKEN = re.compile(
    rf"{_LINKEDIN_TAG}(?:{'|'.join(_LINKEDIN_TAG_WORDS.values())})[a-z0-9_-]*", re.IGNORECASE
)
# Remote joined to a place code or qualifier: "USA-IL-Remote", "U.S. Remote",
# "Remote - US", "Remote (US)". "Remote-friendly" and "Occasional remote" are prose.
REMOTE_PLACE_NAME = re.compile(
    r"^\W*(?:[A-Z]{2,}|(?:[A-Z]\.){2,})(?:[- ]+[A-Z]{2,})*[- ]+(?i:remote)\W*$"
    r"|^\W*(?i:remote)\s*(?:-\s+|\()[A-Z]"
)
# A bare "Remote" is a place only as one entry of a list ("Remote, Canada").
BARE_REMOTE_PLACE = re.compile(r"^\W*remote\W*$", re.IGNORECASE)
# A line naming attendance describes an arrangement, not a list of places.
ATTENDANCE_IN_PLACE_LINE = re.compile(r"\b(?:hybrid|on[- ]?site|office|days?)\b", re.IGNORECASE)
LOCATION_LABEL = re.compile(r"^\W*locations?\s*:\s*", re.IGNORECASE)
# Each part of a place list is a few words; a longer part is a sentence. A bare
# "-" separator in "USA - New York City" is not one of the place's words.
PLACE_WORDS = 4
PLACE_WORD = re.compile(r"\S*[^\W\d_]\S*")
REMOTE = (
    "fully remote",
    "remote first",
    "work from home",
    "remote",
)
# An unambiguous whole-role remote declaration, which outweighs a stated hybrid.
FULLY_REMOTE = (
    "fully remote",
    "remote first",
    "remote-first",
    "100% remote",
    "100 percent remote",
)
# Listing words that offer remote work and name no place.
REMOTE_LISTING_WORDS = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(phrase).replace(r"\ ", r"[- ]")
        for phrase in sorted(
            {*REMOTE, *FULLY_REMOTE, "home based", "work anywhere", "anywhere"},
            key=len,
            reverse=True,
        )
    )
    + r")(?!\w)",
    re.IGNORECASE,
)
# Wording that rules remote work out, which a bare mention of it would otherwise offer.
REMOTE_REFUSED = (
    "not remote",
    "no remote",
    "not a remote",
    "not eligible for remote",
    "not available for remote",
    "not open to remote",
    "remote work is not",
    "remote is not",
    "cannot work remotely",
    "not be performed remotely",
    "not be located remotely",
    "not have the ability to be located remotely",
    "not have the ability to work remotely",
    "not be able to work remotely",
    "no fully remote",
    "not fully remote",
    "non remote",
    "isn't remote",
    "remote isn't available",
    "remote work isn't available",
    "remote work isn't offered",
    "remote isn't an option",
    "remote work isn't an option",
)
# A remote label answered no: "Remote: No", "Remote eligible: No".
REMOTE_ANSWERED_NO = re.compile(
    r"\bremote(?:\s+(?:eligible|eligibility|work|option))?\s*:\s*no\b(?=\s*(?:[.,;!)]|$))",
    re.IGNORECASE,
)


# "Remote work is not available outside Canada" limits where remote work happens.
_WHERE_LIMIT = re.compile(
    r"[^a-z0-9+]+(?:[a-z0-9+]+[^a-z0-9+]+){0,2}?"
    r"(?:(?:from\s+)?outside|abroad|overseas|internationally"
    r"|in\s+(?:the\s+following|certain|some|these|select)\s+(?:states|countries|provinces|regions)"
    r"|(?-i:(?:available|eligible|permitted|allowed|offered|possible)\s+(?:in|from)\s+[A-Z]))\b",
    re.IGNORECASE,
)


def refuses_remote(line: str) -> bool:
    """Return whether a line rules remote work out, not only where it may happen."""
    refusals = (
        match
        for phrase in says(line, REMOTE_REFUSED)
        for match in re.finditer(
            r"\b" + r"[^a-z0-9+]+".join(map(re.escape, SEPARATOR.split(phrase))) + r"\b",
            line,
            re.IGNORECASE,
        )
    )
    return (
        any(_WHERE_LIMIT.match(line, match.end()) is None for match in refusals)
        or REMOTE_ANSWERED_NO.search(line) is not None
    )


HYBRID = (
    "hybrid",
    "days per week in the office",
    "days a week in the office",
    "days in office",
)
# A hybrid stated as this role's own arrangement, office cadence, or named remote days, not a
# bare "hybrid" tag or an employer-wide "hybrid work model" that names no cadence.
HYBRID_ROLE = re.compile(
    r"\bhybrid\s+(?:role|position|job)\b"
    r"|\b(?:role|position|job)\s+is\s+hybrid\b"
    r"|\bdays?\s+(?:a|per|each)\s+week\s+in\s+(?:the\s+)?office\b"
    r"|\bdays?\s+in\s+(?:the\s+)?office\b"
    r"|\b[1-4]\s*\+?\s*office\s+days?\b"
    r"|\b(?:work[\s-]+from[\s-]+home|wfh|remote)\s+days?\b",
    re.IGNORECASE,
)
ON_SITE = (
    "on site",
    "onsite",
    "in office",
    "in person",
    "at our office",
    "relocate to",
)

# Keep arrangement names aligned with search parameter vocabulary.
ARRANGEMENT_EVIDENCE: dict[str, tuple[str, ...]] = dict(
    zip(ARRANGEMENTS, (REMOTE, HYBRID, ON_SITE), strict=True)
)

# An office-day count counts days per week, and the sentence has to say so: "3-4 days a
# week", "three (3) days per week", "3x per week". A range reads by its bottom.
_PER_WEEK = r"(?:\b(?:per|a|each|every)\s+(?:week|wk)\b|\bweekly\b|/\s*(?:week|wk)\b)"
_OFFICE_DAYS = re.compile(
    rf"(?P<days>{FIGURE})\s*-?\s*(?:{unit('day')}(?=[^.]*?{_PER_WEEK})"
    rf"|(?:x|times)\s*(?={_PER_WEEK}))",
    re.IGNORECASE,
)
_WEEK_DAYS = 7
# A cadence set only for people near an office or for a subset of places the clause opens
# with: "If you live within 50 miles", "For these locations, 3 days". "local to Toronto" and
# "employees near retirement" are none.
_PEOPLE = (
    r"(?:employees|those|people|candidates|staff|team\s+members|anyone|applicants|hires|talent)\b"
)
_DISTANCE = r"(?:miles?|mi\b\.?|km\b|kilomet(?:er|re)s?|minutes?|mins?\b|" + unit("hour") + ")"
_OFFICE_WORD = r"(?:offices?|hubs?|sites?|campus(?:es)?|locations?|headquarters|hq|clinics?)"
# Near an office word or a named place, never "near retirement" or "close to the bedside".
_NEAR_WHAT = (
    rf"\s+(?:(?:one\s+of\s+)?(?:our|the|a|an|your)\s+)?"
    rf"(?:(?:[\w.'-]+\s+){{0,3}}?{_OFFICE_WORD}|(?-i:[A-Z]))"
)
_NEARNESS = (
    rf"(?:(?:near|close\s+to){_NEAR_WHAT}|commut(?:e|ing|able)\s+to{_NEAR_WHAT}"
    rf"|local\s+to\s+(?:[\w.'-]+\s+){{0,3}}?{_OFFICE_WORD}"
    rf"|within\s+(?:(?:a|an)\s+)?(?:{FIGURE}\s*-?\s*)?(?:{_DISTANCE}|commut\w*|driving))\b"
)
# A country, a remote word, or "in nature" places no hybrid week.
_NO_HYBRID_PLACE = (
    r"(?!(?:the\s+)?(?:united\b|us\b|u\.s\.|usa\b|america\b|anywhere\b|remote|nature\b))"
)
# City pairs use state or province codes, or UK; "SQL, BI" lists skills.
_SUBDIVISION_CODE = "|".join(sorted(SUBDIVISION_CODES))
# A place name opens with a capital, so "a hybrid role in clinical informatics" names none.
_CAPITAL = r"(?=(?-i:[A-Z]))"
# A field of work after "hybrid role in": "Data Engineering", "Product Management".
_NOT_A_FIELD = (
    r"(?!(?:[\w'.-]+\s+){0,2}(?:engineering|science|sciences|analytics|operations|marketing"
    r"|sales|finance|accounting|design|informatics|development|management|strategy|security"
    r"|infrastructure|product|technology|software|support|consulting|compliance|data)\b)"
)
# Words a "Hybrid - X" or "X (Hybrid)" label pairs with that name a schedule, not a place.
_NOT_A_LABEL_PLACE = (
    r"(?!(?:flexib\w*|schedule|work\w*|model|role|position|option\w*|type|arrangement"
    r"|employment|job|location\s+type)\b)"
)
# What puts a hybrid week somewhere: an office or site, nearness to one, a city the
# role is based in, a "City, ST" pair, a "Location" label naming more than a country,
# or a "Hybrid" label joined to a place by a comma, dash or brackets.
HYBRID_PLACE = re.compile(
    rf"^\W*hybrid(?:\s+based)?\s*(?:,|\(|[-|]\s)\s*{_NO_HYBRID_PLACE}{_NOT_A_LABEL_PLACE}"
    r"[^\W\d_]"
    rf"|^\W*{_NOT_A_LABEL_PLACE}[^\W\d_][\w.' ]{{0,40}}?\s(?:[-|]\s*|\(\s*)hybrid\b\W*$"
    "|"
    r"\b(?:in[- ]?office|on[- ]?site|onsite|in[- ]person)\b"
    rf"|\b(?:our|the|an?|its|your)\s+(?:[\w.,'-]+\s+){{0,3}}?{_OFFICE_WORD}\b"
    rf"|\b{_NEARNESS}"
    rf"|\b(?:hybrid|based|located|headquartered)\s+(?:in|at|out\s+of)\s+{_NO_HYBRID_PLACE}\w"
    rf"|\bhybrid\s+(?:role|position|job)\s+(?:is\s+)?(?:in|at)\s+{_NO_HYBRID_PLACE}{_NOT_A_FIELD}"
    rf"{_CAPITAL}[\w'.-]*(?:\s+{_CAPITAL}[\w'.-]*){{0,2}}"
    rf"(?:,\s*{_CAPITAL}(?:{_SUBDIVISION_CODE}|UK))?\s*(?:[.;!]|$)"
    rf"|\b(?:office|campus|site|hub|headquarters|hq)\s+(?:at|in)\s+{_NO_HYBRID_PLACE}[^\W\d_]"
    rf"|\bhybrid\s+based\s*\(\s*{_NO_HYBRID_PLACE}[^\W\d_]"
    rf"|\b[^\W\d_][\w.'-]*,\s*{_CAPITAL}(?:{_SUBDIVISION_CODE}|UK)\b"
    # "Initech, Co. and Globex" abbreviates a company; "Denver, CO" names a state.
    r"(?!(?<=co)\.\s+(?:and|&)\s)"
    r"(?=\s*-\s*hybrid\b|\s+-\s|\s*(?:[()|,.;]|$))"
    rf"|(?:^|\s)\W*(?:work\s+)?locations?\s*[:-]\s*{_NO_HYBRID_PLACE}\w",
    re.IGNORECASE,
)
_PLACES = r"(?:locations|cities|hubs|offices|sites|areas|markets|regions|metros)"
_WHO = (
    rf"(?:\bif\s+you\s+(?:live|reside|are)\s+"
    rf"|\b{_PEOPLE}\s+(?:(?:who|that)\s+(?:(?:are|live)\s+)?)?)"
)
_ROLE_MENTION = r"\bthis\s+(?:role|position|job)\b"
# A mention identifies this role beside the employer's named work types.
THIS_ROLE = re.compile(
    rf"{_ROLE_MENTION}|\bthis\s+is\s+one\s+of\s+(?:our|the)\b",
    re.IGNORECASE,
)
# "Many roles are on-site" and "Roles at our campus are on-site" speak for some of the
# employer's roles unless a qualifier includes this one.
HEDGED_ROLES = re.compile(
    r"\b(?:many|some|most|certain|several|select|a\s+few)\s+(?:of\s+(?:our|the)\s+)?"
    r"(?:roles|positions|jobs)\b|(?<!\ball\s)\b(?:roles|positions|jobs)\s+(?:at|in|across)\s"
    r"|\b(?:for|in)\s+such\s+(?:roles|positions|jobs)\b",
    re.IGNORECASE,
)
# The office context an office-day count has to sit beside to be about attendance.
OFFICE_PRESENCE = re.compile(
    r"\b(?:in[- ]?office|on[- ]?site|onsite|in the office|in person|at our office)\b",
    re.IGNORECASE,
)
# A plural-role policy binds this role through inclusion or its own attendance statement.
_ROLE_POLICY_BINDING = re.compile(
    r"(?P<inclusion>\bincluding\s+(?:this\s+(?:one|role|position|job)"
    r"|(?:the|this)\s+incumbent's\s+(?:role|position|job)))\b"
    rf"|{_ROLE_MENTION}\s*(?:[,(-]\s*like\s+(?:{HEDGED_ROLES.pattern})\s*[,)-])?\s+"
    r"(?:(?:is|will\s+be)\s+(?:fully\s+)?(?:hybrid|on[- ]?site|in[- ]office)\b"
    r"|works?\s+(?:on[- ]?site|in[- ]office)\b"
    rf"|requires\s+(?:(?:a\s+)?minimum\s+of\s+)?{_OFFICE_DAYS.pattern}"
    rf"[^.;]{{0,60}}?(?:{OFFICE_PRESENCE.pattern}))",
    re.IGNORECASE,
)
_WITHOUT_INCLUSION = re.compile(r"\bwithout\s*$", re.IGNORECASE)
_ATTENDANCE_CONDITION = re.compile(
    rf"{_WHO}(?:(?:located|living|based)\s+)?{_NEARNESS}"
    rf"|\A\W*(?:for\s+{_PEOPLE}\s+)?(?:for|in|at)\s+"
    rf"(?:(?:these|those)\s+{_PLACES}|the\s+{_PLACES}\s+(?:above|below|listed))\b",
    re.IGNORECASE,
)
# A cadence for local people is conditional only where others are offered something else:
# "Local candidates attend 2 days" beside "or fully remote" or "candidates outside Springfield".
_FOR_LOCAL_PEOPLE = re.compile(
    rf"\blocal\s+{_PEOPLE}|\A\W*locals\b|{_WHO}local\b(?!\s+to\b)", re.IGNORECASE
)
_NON_LOCAL_ALTERNATIVE = re.compile(
    rf"(?<!\bnot\s)(?<!\bno\s)(?<!\bnon-)\bremote(?:ly)?\b|\bwork\s+from\s+home\b|\bnon-?local\b"
    rf"|\b{_PEOPLE}\s+(?:(?:who\s+(?:are|live)|living|based|located)\s+)?(?:outside|beyond)\b",
    re.IGNORECASE,
)


def role_policy_bindings(text: str) -> tuple[re.Match[str], ...]:
    """Return the positive qualifiers binding a plural-role policy to this role."""
    return tuple(
        match
        for match in _ROLE_POLICY_BINDING.finditer(text)
        if match.group("inclusion") is None
        or not (
            _NEGATED.search(text, 0, match.start())
            or _WITHOUT_INCLUSION.search(text, 0, match.start())
        )
    )


def office_days(text: str, *, posting: str | None = None) -> int | None:
    """Return the weekly office days a sanitized text states, a range by its bottom.

    A monthly count reads as its whole days per week, so fewer than one a week is none.
    A clause that sets the cadence only under a condition of place, or for some of the
    employer's roles, states none; one for local people is a condition when ``posting``
    (or ``text``) offers others an alternative.
    """
    local = _NON_LOCAL_ALTERNATIVE.search(text if posting is None else posting) is not None
    kept = " ".join(
        clause
        for clause in clauses(text)
        if not (
            _ATTENDANCE_CONDITION.search(clause)
            or (HEDGED_ROLES.search(clause) and not role_policy_bindings(clause))
            or (local and _FOR_LOCAL_PEOPLE.search(clause))
        )
    )
    for found in _OFFICE_DAYS.finditer(kept):
        figure = read_figure(found["days"])
        if figure is not None and 1 <= figure.floor <= _WEEK_DAYS:
            return int(figure.floor)
    for found in DAYS_PER_MONTH.finditer(kept):
        if 1 <= (weekly := int(stated_days(found) / WEEKS_PER_MONTH)) <= _WEEK_DAYS:
            return weekly
    return None


# Attendance stated as a percentage share of the working week.
WEEKLY_SHARE = re.compile(r"\b(\d{1,3})\s*(?:%|per ?cent)", re.IGNORECASE)

# Office attendance stated less often than weekly.
INFREQUENT_ATTENDANCE = re.compile(
    r"\b(?:per|a|each|every)\s+(?:month|quarter|year)\b"
    r"|\b(?:monthly|quarterly|annually)\b"
    r"|\b(?:a few|several|two|three|four)\s+times\s+(?:a|per)\s+(?:month|quarter|year)\b"
    r"|\boccasional(?:ly)?\b",
    re.IGNORECASE,
)

# Travel stated as a share of the role. "up to 25%" and "25% travel" both count.
TRAVEL_PERCENT = re.compile(
    r"\b(?:travel|travelling|traveling)\b[^.]{0,40}?(\d{1,3})\s*(?:%|per ?cent)"
    r"|(\d{1,3})\s*(?:%|per ?cent)(?:(?!\bremote\b)[^.;]){0,40}?"
    r"\b(?:travel|travelling|traveling)\b",
    re.IGNORECASE,
)
# "less than 25%" bounds the share from above without stating it.
TRAVEL_BELOW = re.compile(r"(?:\bless\s+than|\bunder|\bbelow|<)\s*$", re.IGNORECASE)
# What a cost, perk, or payment beside pay is called. A percentage beside one is no
# share of the working time, and a figure beside one is no salary.
TRAVEL_COST = re.compile(
    r"\b(?:expenses?|costs?|reimburs\w*|stipends?|per\s+diems?|bonus(?:es)?|commissions?"
    r"|allowances?|budgets?|vouchers?|revenue|insurance|match(?:ed|ing)?|discounts?|fees?"
    r"|benefits?)\b",
    re.IGNORECASE,
)

# North American working zones, as hours from UTC at standard time. Offsets are
# compared, not names, because a posting writes its tolerance in hours.
TIME_ZONE_OFFSETS = {
    "newfoundland": -3,
    "atlantic": -4,
    "eastern": -5,
    "central": -6,
    "mountain": -7,
    "pacific": -8,
    "alaska": -9,
    "hawaii": -10,
}
_ZONE_NAMES = "|".join(TIME_ZONE_OFFSETS)

# A zone name standing alone, not part of a longer name: "Asia-Pacific", "Mountain View".
_ZONE = rf"(?<![\w-])(?<!\basia\s)(?:{_ZONE_NAMES})(?![\w-])"
# A zone name or list before "time" ("Eastern (EST) time zone", "Central US time"), or one
# ending its phrase right after "time zone". A zone after a zone and a join is a list tail,
# never a match start, which keeps the scan linear.
_ZONE_JOIN = r"\s*(?:,|/|\band/or\b|\bor\b|\band\b)\s*"
_LIST_TAIL = "".join(
    rf"(?<!\b{name}{join})"
    for name in TIME_ZONE_OFFSETS
    for join in (",", ", ", "/", " / ", " or ", " and ", " and/or ")
)
REQUIRED_TIME_ZONE = re.compile(
    rf"(?=(?i:{_ZONE_NAMES})){_LIST_TAIL}(?P<zone>{_ZONE})(?:{_ZONE_JOIN}{_ZONE})*"
    r"(?:\s*\([^()]{1,12}\))?(?:\s+(?:US|U\.S\.))?(?:\s+(?:standard|daylight))?\s+time\b"
    rf"|\btime\s*zones?\s*(?:[:(]|\bof\b|\bis\b)?\s*(?:the\s+)?(?P<zone_after>{_ZONE})"
    r"(?=\s*(?:[^\w\s-]|$|(?:or|and|only|time|standard|hours?)\b))",
    re.IGNORECASE,
)

# The hours either side of a required zone a posting will still accept.
TIME_ZONE_TOLERANCE = re.compile(
    rf"\b(?:within\s+)?(?:an?\s+)?(?P<hours>{FIGURE})\s*-?\s*{unit('hour')}[^.]{{0,30}}?"
    r"\b(?:offset|of|from|either side)\b",
    re.IGNORECASE,
)

# Words that turn a named zone into a requirement rather than a mention.
TIME_ZONE_REQUIRED = re.compile(
    r"\b(?:must|required|require[sd]?|need[s]? to)\b[^.]{0,80}?\b(?:based|located|reside|work)\b"
    r"|\b(?:based|located)\s+in\b",
    re.IGNORECASE,
)

# Currency codes handled here are the four the search covers.
CURRENCY_CODES = ("USD", "CAD", "GBP", "EUR")
_CODES = "|".join(CURRENCY_CODES)
# A trailing k/K denotes thousands; `_figure` multiplies it out.
_FIGURE = r"\d+(?:[, ]\d{3})*(?:\.\d{1,2})?[kK]?"

# Only parse figures adjacent to a currency symbol or code. The maximum group
# inherits the leading currency when the range writes the symbol only once.
QUOTED_PAY = re.compile(
    rf"(?:(?P<code>\b(?:{_CODES})\b)\s*[$]?|(?P<symbol>[$\u00a3\u20ac]))\s*"
    rf"(?P<amount>{_FIGURE})"
    rf"(?:\s*{RANGE_SEPARATOR}\s*(?:[$\u00a3\u20ac]\s*)?(?P<maximum>{_FIGURE}))?"
)
# A figure pair with no currency beside it.
BARE_RANGE = re.compile(rf"(?P<amount>{_FIGURE})\s*{RANGE_SEPARATOR}\s*(?P<maximum>{_FIGURE})")
# Some boards write the code after the figure, as in "194,636.40USD".
TRAILING_PAY = re.compile(rf"(?P<amount>{_FIGURE})\s*(?P<code>\b(?:{_CODES})\b)")
# A currency code written anywhere, including a heading with no figure.
CURRENCY_MENTION = re.compile(rf"\b(?:{_CODES})\b")
# Words marking a line as being about pay.
PAY_CONTEXT = re.compile(
    r"\b(?:salary|salaries|compensation|pay|base|remuneration|wage|earnings|"
    r"total\s+rewards|range|hourly|annualiz(?:e|ed|es|ing|ation)?)\b",
    re.IGNORECASE,
)
PAY_PERIODS: tuple[tuple[CompensationBasis, re.Pattern[str]], ...] = (
    (
        "annual",
        re.compile(
            r"\b(?:annual(?:ly)?|yearly|per\s+(?:year|annum)|a\s+year)\b|/year\b", re.IGNORECASE
        ),
    ),
    (
        "monthly",
        re.compile(r"\b(?:monthly|per\s+month|a\s+month)\b|/month\b", re.IGNORECASE),
    ),
    (
        "daily",
        re.compile(r"\b(?:daily|per\s+day|a\s+day)\b|/day\b", re.IGNORECASE),
    ),
    # "per HR" names the department, so only a lowercase or title-case hr is an hour.
    (
        "hourly",
        re.compile(
            r"\b(?:hourly|per\s+(?:hour|(?-i:hr|Hr))|an\s+hour)\b|/\s*(?:hour|hr)\b",
            re.IGNORECASE,
        ),
    ),
)
# A pay period no compensation basis names, annualised by how often it falls in a year.
BIWEEKLY_PAY = re.compile(
    r"\b(?:bi-?weekly|fortnightly|every\s+(?:two|2)\s+weeks)\b", re.IGNORECASE
)
BIWEEKLY_PERIODS = 26
# Preserve a bare dollar sign when no unique currency is stated.
SYMBOL_CURRENCIES = {"\u00a3": "GBP", "\u20ac": "EUR"}
# A country prefix on the dollar sign, as in "C$" or "US$", names its currency outright.
DOLLAR_PREFIX = re.compile(r"\b(C|CA|US)\$")
DOLLAR_PREFIX_CURRENCIES = {"C": "CAD", "CA": "CAD", "US": "USD"}

# Zone-table detection. A "geographic zone" heading on one line, followed by
# per-zone range lines, is one aggregated pay disclosure.
ZONE_HEADER = re.compile(
    r"(?:by\s+geographic\s+zone|geographic\s+differentials|"
    r"pay\s+zones?|salary\s+zones?)",
    re.IGNORECASE,
)
ZONE_LINE = re.compile(r"^\s*(?:zone|tier|region)\s*[A-Z0-9]+\s*[:\-\u2013]", re.IGNORECASE)

# Per-jurisdiction stanzas concatenated onto one physical line. Each stanza
# is its own candidate line once split.
JURISDICTION_SPLIT = re.compile(
    r"(?=(?:If\s+performed\s+in\s+|Applicants?\s+in\s+|For\s+positions?\s+in\s+))"
)

# Country tokens the extractor accepts even when the reader did not spell them.
# US-state names are treated as evidence of United States for currency-inference
# purposes only, so a state override that carries a bare $ resolves as USD.
US_STATES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
    "washington dc",
    "washington, dc",
    "washington d.c.",
)
CA_PROVINCES = (
    "alberta",
    "british columbia",
    "manitoba",
    "new brunswick",
    "newfoundland",
    "nova scotia",
    "ontario",
    "prince edward island",
    "quebec",
    "saskatchewan",
)
# Country-implying tokens. The extractor uses these to resolve a bare $ when
# the reader's declared aliases did not match. Word-boundary matched only.
COUNTRY_TOKENS: dict[str, tuple[str, ...]] = {
    "United States": (
        "united states",
        "u.s.a.",
        "u.s.",
        "usa",
        "us only",
        "us-only",
        "us based",
        "us-based",
        "americans",
        *US_STATES,
    ),
    "Canada": ("canada", "canadian", *CA_PROVINCES),
    "United Kingdom": (
        "united kingdom",
        "u.k.",
        "uk only",
        "uk-only",
        "great britain",
    ),
}
# Currency implied by country, for the four in-scope currencies.
COUNTRY_CURRENCY: dict[str, str] = {
    "United States": "USD",
    "Canada": "CAD",
    "United Kingdom": "GBP",
}
# Uppercase-only tokens: "US"/"UK" without a period are ambiguous when
# casefolded ("us" is a pronoun). Matched case-sensitively at word boundaries.
COUNTRY_CASED_TOKENS: dict[str, tuple[str, ...]] = {
    "United States": ("US", "USA"),
    "United Kingdom": ("UK",),
}


def country_hint(text: str) -> str:
    """Return the country ``text`` names, or empty when it names none or several."""
    folded = text.casefold()
    named = {
        country
        for country, aliases in COUNTRY_TOKENS.items()
        if any(alias in folded for alias in aliases)
    }
    named |= {
        country
        for country, aliases in COUNTRY_CASED_TOKENS.items()
        if any(re.search(rf"\b{alias}\b", text) for alias in aliases)
    }
    return next(iter(named)) if len(named) == 1 else ""


NO_SPONSORSHIP = (
    "will not sponsor",
    "unable to sponsor",
    "not able to sponsor",
    "do not sponsor",
    "does not sponsor",
    "no sponsorship",
    "without sponsorship",
    "not provide sponsorship",
    "not offer sponsorship",
)
SPONSORSHIP = ("sponsorship available", "will sponsor", "we sponsor", "sponsorship is available")
AUTHORIZED = (
    "authorized to work",
    "authorised to work",
    "legally authorized",
    "eligible to work",
    "right to work",
    "work permit",
)
CLEARANCE = ("security clearance", "clearance required", "active clearance")
HELD_CREDENTIALS = (
    "licensed",
    "licensure",
    "driver's license",
    "drivers license",
    "valid license",
    "active license",
    "fluent in",
    "fluency in",
)

# Somebody else as the subject of a residence verb: "teams located in", "clients who are
# based in", "a partner that is based in".
_OTHER_SUBJECT = re.compile(
    r"\b(?:teams?|customers?|clients?|partners?|stakeholders?|colleagues?|vendors?|suppliers?"
    r"|users?|patients?|people|staff|members?|employees|offices?|groups?|participants?"
    r"|subjects|volunteers?|students?|trainees?|attendees?|enrollees?|recipients?"
    r"|beneficiar(?:y|ies)|caregivers?|families|children|residents)"
    r"\s+(?:(?:who|that|which)\s+)?(?:(?:is|are)\s+)?$",
    re.IGNORECASE,
)
# Whom the employer serves rather than where it hires: "trusted by hospitals across the
# US", "customers throughout Canada". A team's own members are hiring-side.
_MARKET_AUDIENCE = (
    r"(?:customers?|clients?|patients?|(?<!team\s)(?<!staff\s)members?|users?|consumers?"
    r"|hospitals?|clinics?|health\s+systems?|pharmac(?:y|ies)|payers?|families|communities)"
)
_MARKET_REACH = re.compile(
    r"\b(?:trusted\s+by|used\s+by|relied\s+(?:up)?on\s+by|chosen\s+by|sold\s+to)\b"
    rf"|\b(?:available\s+to|serv(?:e|es|ed|ing))\s+(?:\w+\s+){{0,2}}?"
    rf"(?:{_MARKET_AUDIENCE}|everyone)\b"
    rf"|\b{_MARKET_AUDIENCE}\s+(?:\w+\s+){{0,2}}?"
    r"(?:in|across|throughout|nationwide|around|all\s+over)\b",
    re.IGNORECASE,
)
_RESIDENCE_MODAL = re.compile(r"\b(?:must|required to|need to|expected to)\b", re.IGNORECASE)
_RESIDENCE_VERB = re.compile(
    r"\b(?:be\s+)?(?:located|based|resid(?:e|ing)|living|live)\b", re.IGNORECASE
)
_RESIDENCE_GAP = 40
# An explicit requirement to live in a stated place, with no modal before it. The place
# is resolved separately.
_RESIDENCE_STATED = re.compile(
    r"\bmust\s+be\s+a\s+resident\b"
    r"|\bfor\s+residents?\s+of\b[^.]{0,40}?\bonly\b"
    r"|\bopen\s+only\s+to\s+(?:candidates?|applicants?|residents?)\b",
    re.IGNORECASE,
)
# "not", "n't", "never", "no" or "no longer" with up to two adverbs before the requirement;
# "not only" asserts.
_NEGATED = re.compile(
    r"(?:\bnot|n't|\bnever|\bno(?:\s+longer)?)\s+(?:(?!only\b)(?:\w+ly|ever|yet)\s+){0,2}$",
    re.IGNORECASE,
)
# The same requirement accepts work authorization or citizenship instead of residence. A
# dotted run ("U.S.") may sit in the gap: a dot touching a letter, or a closing dot before a
# lowercase word.
WORK_AUTHORIZATION_ALTERNATIVE = re.compile(
    r"\bor\b(?:[^.;]|\.(?=[a-z])|(?<=\.[a-z])\.(?=\s+(?-i:[a-z]))){0,60}?"
    r"\b(?:authori[sz]ed\s+to\s+work|legally\s+authori[sz]ed|eligible\s+to\s+work"
    r"|right\s+to\s+work|work\s+(?:authori[sz]ation|permit)|citizens?(?:hip)?)\b",
    re.IGNORECASE,
)
# A clause placing the candidate in a time zone rather than a country.
TIME_ZONE_MENTION = re.compile(r"\btime\s*zones?\b", re.IGNORECASE)


def outside_market(line: str) -> str:
    """Return ``line`` less each clause's words from a customer-market cue to its end."""
    kept = []
    for clause in clauses(line):
        found = _MARKET_REACH.search(clause)
        kept.append(clause if found is None else clause[: found.start()])
    return " ".join(kept)


def residence_required(sentence: str) -> int | None:
    """Return where a sentence requires the candidate to live somewhere, or None.

    Modality-guarded, so a privacy notice's "if you are based in California" or an HQ's
    "headquarters is located in" requires nothing, and a residence verb whose subject is
    somebody else ("work with teams located in Canada") is about them.
    """
    for modal in _RESIDENCE_MODAL.finditer(sentence):
        if _NEGATED.search(sentence, 0, modal.start()):
            continue
        reach = sentence.find(".", modal.end())
        end = min(len(sentence) if reach < 0 else reach, modal.end() + _RESIDENCE_GAP + 12)
        for verb in _RESIDENCE_VERB.finditer(sentence, modal.end(), end):
            if verb.start() - modal.end() > _RESIDENCE_GAP:
                break
            if _OTHER_SUBJECT.search(sentence, 0, verb.start()) is None:
                return modal.start()
    for stated in _RESIDENCE_STATED.finditer(sentence):
        if not _NEGATED.search(sentence, 0, stated.start()):
            return stated.start()
    return None


_COMMUTABLE = (
    r"within\s+(?:(?:a\s+)?(?:reasonable\s+)?commut(?:able|ing)\s+distance"
    r"|(?:about\s+)?\d+\s*(?:miles?|mi|km|kilometers?))"
)
_LOCAL_RESIDENCE = re.compile(
    rf"(?:{_RESIDENCE_MODAL.pattern}|\bshould\b(?!\s+(?:ideally|preferably)\b))\s+(?:(?:\w+ly)\s+)?(?:reside|live)\s+"
    rf"(?:locally|(?P<near>{_COMMUTABLE}))\b"
    rf"|\bnot\s+(?:(?:living|residing|located|based)\s+)?(?P<far>{_COMMUTABLE})\b[^.;]{{0,80}}?"
    r"\bwill\s+not\s+be\s+considered\b"
    r"|(?<!\bis\s)\b(?P<relocation>relocation\s+(?:is\s+)?required)\b",
    re.IGNORECASE,
)
# A distance measured from anything but the one posting, such as "a client site" or "one of
# these offices", says nothing about the listing's place.
_POSTING_ANCHOR = re.compile(
    r"\s+(?:of|from|to)\s+(?!.*\b(?:client|customer|partner|airport)s?\b)"
    r"(?:the|our)\s+(?:[\w-]+\s+){0,2}?"
    r"(?:(?:office|headquarters|hq|campus|site|facility|workplace|branch|location)\b"
    r"(?!s\b)(?:\(s\))?|posting\s+location)",
    re.IGNORECASE,
)
_ANCHORED = re.compile(r"\s+(?:of|from|to)\b", re.IGNORECASE)
# Any of these anywhere in the sentence reads the nearness as conditional or scoped.
_LOCAL_CONDITION = re.compile(
    r"\b(?:if|when|unless|whether|where|except|though|although|otherwise|remote)\b"
    r"|\bfor\s+(?:some|certain|select|specific|particular|most|many)\b"
    r"|\bfor\s+(?:candidates?|applicants?|those|people|anyone|employees)\s+(?:who\s+)?"
    r"(?:live|living|resid\w*|located|based|within|near)\b",
    re.IGNORECASE,
)
# A form field's short answer after its label: "Relocation required, No".
_ANSWER_WORD = r"(?:no|n/?a|none|yes|negotiable|tbd|optional|maybe|not\s+applicable)\b\.?\s*$"
_FIELD_ANSWER = re.compile(rf"\s*[,:-]?\s*{_ANSWER_WORD}", re.IGNORECASE)
_RELOCATION_LABEL = re.compile(r"\brelocation\s+(?:is\s+)?required\s*$", re.IGNORECASE)
_CONJUNCTION = re.compile(r"\s(?:or|and|nor)\s+$", re.IGNORECASE)


_NEARNESS_CUE = re.compile(_NEARNESS, re.IGNORECASE)


def states_nearness(text: str) -> bool:
    """Return whether text places someone near, or within a distance of, somewhere."""
    return _NEARNESS_CUE.search(text) is not None


def local_residence_required(sentence: str) -> int | None:
    """Return where a sentence requires the candidate to live near the posting, or None.

    A condition, a negation, somebody else as the subject, a distance from another
    place, or a relocation field with an answer after it reads None.
    """
    if _LOCAL_CONDITION.search(sentence):
        return None
    for found in _LOCAL_RESIDENCE.finditer(sentence):
        if _local_requirement(sentence, found):
            return found.start()
    return None


def _local_requirement(sentence: str, found: re.Match[str]) -> bool:
    if _negated(sentence[: found.start()]):
        return False
    if _OTHER_SUBJECT.search(sentence, 0, found.start()):
        return False
    if found["relocation"]:
        rest = sentence[found.end() :]
        return _FIELD_ANSWER.match(rest) is None and re.match(r"\s*(?:[.,]|$)", rest) is not None
    near = "near" if found["near"] else "far"
    rest = sentence[found.end(near) :]
    return _ANCHORED.match(rest) is None or _POSTING_ANCHOR.match(rest) is not None


def answered_field(line: str, following: str) -> bool:
    """Return whether ``line`` ends in a relocation label that ``following`` answers."""
    return bool(_RELOCATION_LABEL.search(line)) and _FIELD_ANSWER.match(following) is not None


def _negated(prefix: str) -> bool:
    """Read a negation before the requirement or before up to three words coordinated with it."""
    if _NEGATED.search(prefix):
        return True
    conjunction = _CONJUNCTION.search(prefix)
    if conjunction is None:
        return False
    words = prefix[: conjunction.start()].split()
    return any(_NEGATED.search(" ".join(words[:-count]) + " ") for count in (1, 2, 3))


# Highest level first, with the level-less "a degree" last: the fit reader ranks by this order.
DEGREES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doctorate", ("phd", "ph d", "doctorate", "doctoral degree", "doctor of")),
    ("master's degree", ("master's degree", "masters degree", "master of", "mba", "msc")),
    (
        "bachelor's degree",
        ("bachelor's degree", "bachelors degree", "bachelor of", "undergraduate degree", "bsc"),
    ),
    ("associate degree", ("associate's degree", "associates degree", "associate degree")),
    ("a degree", ("advanced degree", "degree in", "university degree", "college degree")),
)
EQUIVALENT = (
    "or equivalent experience",
    "or equivalent practical experience",
    "equivalent combination of education and experience",
    "or equivalent work experience",
    "degree or equivalent",
)
# A clause offering another path to what the line asks for: one opening with "or", or
# "Equivalent experience may substitute for the degree", "in lieu of a degree".
_SUBSTITUTE = re.compile(
    r"^\W*(?:or|equivalent)\b|\bsubstitut\w*|\bin\s+(?:lieu|place)\s+of\b", re.IGNORECASE
)


def offers_substitute(clause: str) -> bool:
    """Return whether a clause offers a substitute or an alternative, not a requirement."""
    return _SUBSTITUTE.search(clause) is not None
