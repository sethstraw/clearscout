"""Rules for what a stated posting requirement is not.

Postings embed regulatory notices, benefits, section headings, and HTML
fragments among their qualification lists. These are filtered where they enter
the record and again at read time, since stored evaluations may still carry them.
"""

from __future__ import annotations

import re
from typing import Literal, get_args

RequirementImportance = Literal["must_have", "nice_to_have", "responsibility"]
REQUIREMENT_IMPORTANCES: tuple[str, ...] = get_args(RequirementImportance)

GateKind = Literal[
    "location",
    "onsite_cadence",
    "travel",
    "work_authorization",
    "sponsorship",
    "time_zone",
    "employment_type",
    "compensation",
    "degree",
    "minimum_years",
    "credential",
    "clearance",
    "leadership",
    "other",
]
GATE_KINDS: tuple[str, ...] = get_args(GateKind)

# Conditions about the person's circumstances, which the search parameters
# answer, because pairing them to a career record always fails.
PARAMETER_KINDS: frozenset[GateKind] = frozenset(
    {
        "location",
        "onsite_cadence",
        "travel",
        "work_authorization",
        "sponsorship",
        "time_zone",
        "employment_type",
        "compensation",
    }
)

# Wording that states the person's working hours or spoken language. Words that also
# name technical work (shift, overnight, on-call, multilingual, a clock time) need a
# qualifier, and an on-call rotation stays a record question.
_SCHEDULE_OR_LANGUAGE = re.compile(
    r"\b(?:weekends?|weekdays?|evenings?|nights?|overtime)\b"
    r"|\bon[- ]call\b(?!\s+(?:cent(?:er|re)s?|rotations?)\b)"
    r"|\bovernight\s+(?:shifts?|hours|work|availability)\b"
    r"|\b(?:statutory|public|bank)\s+holidays?\b"
    r"|\b(?:rotating|night|day|evening|weekend|overnight|split|early|late|variable)"
    r"\s+shifts?\b(?!-)"
    r"|\bmonday\s+(?:to|through|-)\s+(?:friday|saturday|sunday)\b"
    r"|\b(?:until|till|before|after|from|between)\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\b"
    r"|\bhours\s*(?:per|a|each|/)\s*(?:week|day|month)\b"
    r"|\bavailable\s+to\s+work\b"
    r"|\b(?:bi|tri|multi)lingual\b(?!\s+(?:nlp|models?|search|llms?)\b)|\bmother\s+tongue\b"
    r"|\bnative[- ](?:level|speaker|fluency|proficiency)\b|\bnative\s+\w+\s+speakers?\b"
    r"|\bcefr\b|\b[abc][12]\+?\s+level\b",
    re.IGNORECASE,
)
_PROFICIENCY = re.compile(
    r"\b(?:fluen(?:t|cy)|proficien(?:t|cy)|spoken|written|speak(?:s|ing)?)\b", re.IGNORECASE
)
_HUMAN_LANGUAGE = re.compile(
    r"\b(?:english|french|spanish|german|portuguese|italian|dutch|mandarin|cantonese|chinese"
    r"|japanese|korean|arabic|hindi|russian|polish|turkish|vietnamese|tagalog|punjabi|hebrew"
    r"|swedish|norwegian|danish|finnish|greek|ukrainian|romanian|urdu|bengali|farsi|persian"
    r"|thai|indonesian)\b",
    re.IGNORECASE,
)


def states_schedule_or_language(text: str) -> bool:
    """Return whether a condition asks about the person's working hours or spoken language."""
    return _SCHEDULE_OR_LANGUAGE.search(text) is not None or (
        _PROFICIENCY.search(text) is not None and _HUMAN_LANGUAGE.search(text) is not None
    )


# Wording that asks the person to travel, to have a fast connection, or to keep a
# workspace. Travel, internet, and office as subject matter of the work stay record questions.
_TRAVEL_AS_SUBJECT = (
    r"(?!\s+(?:industry|sector|nurs\w*|management|booking|agenc\w*|platforms?|software|apps?"
    r"|technology|compan\w*|market|medicine|health|vaccin\w*|insurance|expense\w*|costs?)\b)"
)
_WORKSPACE_QUALIFIER = r"(?:dedicated|quiet|private|distraction[- ]free|suitable|ergonomic)"
_LOGISTICS = re.compile(
    r"\b(?:able|ability|willing(?:ness)?|available|availability|prepared)\s+to\s+travel\b"
    r"|\b(?:requires?|requiring|will\s+require)\s+(?:some\s+|occasional\s+)?travel\b"
    + _TRAVEL_AS_SUBJECT
    + r"|\btravel(?:l?ing)?\s+(?:occasionally|periodically|regularly|frequently|up\s+to"
    r"|(?:is\s+)?required|as\s+(?:needed|required)|when\s+(?:needed|required)|if\s+required)\b"
    r"|\b(?:occasional|periodic|regular|frequent|limited|minimal|some|domestic|international)"
    r"\s+travel\b"
    + _TRAVEL_AS_SUBJECT
    + r"|\b\d{1,3}\s*%\s+(?:of\s+)?travel\b"
    + _TRAVEL_AS_SUBJECT
    + r"|\b(?:download|upload)\s+speeds?\s+(?:of\s+)?(?:at\s+least\s+)?\d"
    r"|\b(?:high[- ]speed|reliable|stable|dependable|consistent)\s+(?:home\s+(?:internet"
    r"|broadband|wi-?fi)\b|(?:internet|broadband|wi-?fi)\s+(?:connections?|connectivity"
    r"|access|speeds?)\b)"
    r"|\b" + _WORKSPACE_QUALIFIER + r"(?:,?\s+(?:and\s+)?" + _WORKSPACE_QUALIFIER + r")*"
    r",?\s+(?:home\s+office|work\s*space|office\s+space|work\s+area)\b"
    r"|\bhome\s+office\s+(?:set[- ]?up|space|equipment)\b",
    re.IGNORECASE,
)


def states_work_logistics(text: str) -> bool:
    """Return whether a condition asks the person to travel or to equip a remote workspace."""
    return _LOGISTICS.search(text) is not None


# Wording that waives the clause it sits in, matched against one clause at a time:
# "Coding is not required", a clause opening "No degree required", "A licence is optional",
# a clause opening "Optional:", or an aside closing on it, "(Django optional)".
WAIVED = re.compile(
    r"\b(?:not|never|isn't|aren't)\s+(?:an?\s+)?(?:required|requirement|mandatory|necessary"
    r"|essential|needed)\b"
    r"|^\W*no\s+(?!less\b|fewer\b)(?:[\w'-]+\s+){0,3}(?:is\s+|are\s+)?"
    r"(?:required|necessary|needed)\b"
    r"|\b(?:is|are)\s+(?:\w+\s+)?optional\b|^\W*optional\b|\boptional\s*\)",
    re.IGNORECASE,
)
_ASSET_WORDS = (
    "significant",
    "strong",
    "great",
    "valuable",
    "definite",
    "real",
    "major",
    "key",
    "big",
    "huge",
    "added",
    "additional",
)
# "Is a significant asset" and "are valuable assets" wish; "a key asset register" does not.
_ASSET_MARKERS = (
    "are assets",
    "be assets",
    *(
        f"{verb} {'an' if word[0] in 'aeiou' else 'a'} {word} asset"
        for verb in ("is", "be")
        for word in _ASSET_WORDS
    ),
    *(f"{verb} {word} assets" for verb in ("are", "be") for word in _ASSET_WORDS),
)
# Whole phrases, matched with `taxonomy.says`, that mark a stated wish.
PREFERRED_MARKERS = (
    "preferred",
    "an asset",
    *_ASSET_MARKERS,
    "preference for",
    "preference to",
    "nice to have",
    "a plus",
    "bonus",
    "ideally",
    "desirable",
    "helpful",
    "would be great",
    "advantageous",
    "would be an advantage",
    "will be an advantage",
    "is an advantage",
    "are an advantage",
    "is considered an advantage",
    "would be an added advantage",
    "is an added advantage",
    "would be a distinct advantage",
    "would be beneficial",
    "would be highly beneficial",
    "is beneficial",
    "are beneficial",
    "is highly beneficial",
    "are highly beneficial",
    "big plus",
    "huge plus",
    "strong plus",
    "definite plus",
    "added plus",
    "preference given",
    "good to have",
    "great to have",
    "would be nice",
)


def gate_kind_label(kind: GateKind) -> str:
    """Return the words a recommendation reason uses for one gate kind."""
    return "a condition" if kind == "other" else kind.replace("_", " ")


_INPUT_DEVICE = r"(computers?|keyboards?|mouse|mice|screens?|monitors?)\b"

_BOILERPLATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Paperwork Reduction Act notices carried on federal-adjacent postings.
    re.compile(r"paperwork\s+reduction\s+act", re.IGNORECASE),
    re.compile(r"\bpublic\s+burden\s+statement\b", re.IGNORECASE),
    # Equal-employment and affirmative-action disclosures.
    re.compile(r"federal\s+contractor\s+or\s+subcontractor", re.IGNORECASE),
    re.compile(r"equal\s+employment\s+opportunity", re.IGNORECASE),
    re.compile(r"affirmative\s+action\s+(policy|program|plan|obligation)", re.IGNORECASE),
    # Application form UI labels that leaked into the posting text.
    re.compile(r"^\s*indicates\s+a\s+required\s+field\s*$", re.IGNORECASE),
    # HTML or template markup: no real requirement carries tags.
    re.compile(r"<\s*/?\s*[a-z][a-z0-9]*(\s|>|/)"),
    # Physical-demand and ADA-accommodation notices carried in postings.
    # Full phrases only: single physical words appear in legitimate product
    # requirements (customer empathy, hearing customer needs, standing offer).
    re.compile(
        r"\b(prolonged|extended|extensive|repetitive|repeated|frequent|"
        r"occasional|constant|continuous)\s+"
        r"(sitting|standing|walking|lifting|carrying|reaching|bending|"
        r"kneeling|stooping|crouching|crawling|climbing|typing|keyboard(ing)?|"
        r"keystrokes?|keyboard\s+use|computer\s+use|screen\s+time)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(prolonged|extended|extensive|repetitive|repeated|frequent|regular|"
        r"constant|continuous)\s+use\s+of\s+(an?\s+|the\s+)?" + _INPUT_DEVICE,
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(use|using|operate|operating|work\s+at|working\s+at)\s+(an?\s+|the\s+)?"
        + _INPUT_DEVICE
        + r"(,?\s+(and\s+)?(an?\s+)?"
        + _INPUT_DEVICE
        + r")*\s+(extensively|for\s+(long|extended|prolonged)\s+periods?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(long|extended|prolonged)\s+periods?\s+of\s+(time\s+)?(screen\s+time|"
        r"(in\s+front\s+of|at)\s+(an?\s+|the\s+)?(computer\s+)?" + _INPUT_DEVICE + r")",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(sit|sitting|stand|standing|walk|walking)\s+for\s+"
        r"(extended|long|prolonged|extensive)\s+periods?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(lift|lifting|carry|carrying|push|pushing|pull|pulling|move|moving)\s+"
        r"(objects?\s+)?(up\s+to\s+)?\d+\s*"
        r"(pounds|lbs?\.?|kilograms|kilos|kgs?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\breasonable\s+accommodations?\b", re.IGNORECASE),
    re.compile(
        r"\bability\s+to\s+(sit|stand|walk|lift|carry|reach|bend|kneel|"
        r"stoop|crouch|crawl|climb|type)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(close|distance|peripheral|color|corrected)\s+vision\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bvisual\s+acuity\b", re.IGNORECASE),
    re.compile(
        r"\bphysical\s+(demands?|requirements?|abilit(y|ies)|effort)\b",
        re.IGNORECASE,
    ),
)


def is_boilerplate_requirement(text: str) -> bool:
    """Return whether one candidate requirement text is regulatory or UI boilerplate."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _BOILERPLATE_PATTERNS)
