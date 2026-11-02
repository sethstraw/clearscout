"""Sentence grammar for a posting's attendance commitments and remote-work offers."""

from __future__ import annotations

import re

from tailorcv.shared.layout import ITEM_MARKER
from tailorcv.shared.quantities import (
    DAY_COUNT,
    DAYS_PER_MONTH,
    DAYS_PER_WEEK,
    FIGURE,
    NUMBER,
    PER,
    stated_days,
    unit,
)
from tailorcv.shared.text import sanitize, sentence_breaks, split_at

# Every spelling of an office arrangement word, for the patterns that name one.
OFFICE_ARRANGEMENT_WORDS = r"(?:on[\s-]?site|in[\s-]?office|in[\s-]?person)"

# A collapsed description has only sentence ends to split on; a retained body also has
# blank lines and item lines, each of which ends an unpunctuated line.
_LINE_BREAK = re.compile(rf"\n\s*\n|\n[^\S\n]*(?={ITEM_MARKER.pattern})")


def sentences(text: str) -> list[str]:
    """Split plain text at sentence ends and line breaks, dropping empty fragments."""
    breaks = sorted(
        [*sentence_breaks(text), *(found.span() for found in _LINE_BREAK.finditer(text))]
    )
    return [piece.strip() for piece in split_at(text, breaks) if piece.strip()]


def plain(text: str) -> str:
    """Return text with markup tags removed and sanitized."""
    return sanitize(re.sub(r"<[^>]+>", " ", text))


_WORK_MODEL = (
    r"(?:(?:site|remote|field|office)-(?:essential|by-design|based|first)"
    r"|on-?site|in-office|hybrid|remote|field|office)"
)
# A sentence stating a rule only for some roles, or gated on a condition, never
# commits this posting to attendance or a remote offer by itself.
_CONDITIONAL_SENTENCE = re.compile(
    r"\bif (?:this|the) (?:role|position|job) is\b"
    rf"|\bfor {_WORK_MODEL}(?:(?:\s*,\s*|,?\s+(?:and|or)\s+){_WORK_MODEL})* roles\b"
    r"|\bwhere applicable\b"
    r"|\bdepending on\b"
    r"|\bsome (?:roles|positions)\b"
    r"|\bfor (?:employees|candidates|staff|those|people) (?:within|near|who live)\b"
    r"|\bfor (?:roles|positions|jobs) (?:listed|posted|designated|marked|classified) as\b",
    re.IGNORECASE,
)

# Attendance only: an "If you live near..." cadence binds local hires, and the
# remote-offer reader must still see the offer such a sentence implies.
_CONDITIONAL_ATTENDANCE_SENTENCE = re.compile(
    r"^\W*if\b|\bif (?:the |a )?(?:selected |successful )?(?:candidate|employee|hire|you)\b"
    r"|\bif (?:based |located )?in\b|\bunless\b"
    # A cadence bound to a commute radius binds only nearby hires.
    r"|\bwithin\s+(?:about\s+|approximately\s+|roughly\s+)?(?:an?\s+)?[\w-]+\s+"
    r"(?:miles?|km|kilometers?|commut\w*|radius)\b|\bcommuting\s+distance\b"
    r"|\b[\w-]+[\s-]based\s+(?:\w+\s+)?(?:candidates|employees|applicants|hires|staff)\b"
    r"|\bwhen\s+(?:working|travel(?:l)?ing|visiting)\b"
    # A remote alternative in the same sentence makes the office one a choice.
    r"|\bor\b[^.;]{0,80}?\bremote(?:ly)?\b"
    rf"|\bremote\b[^.;]{{0,60}}?(?:\bor\b|/)\s*(?:\w+\s+){{0,6}}?{OFFICE_ARRANGEMENT_WORDS}",
    re.IGNORECASE,
)

_REMOTE_REFUSED_SENTENCE = re.compile(
    r"\bnot remote\b|\bno remote\b|\bnot a remote\b"
    r"|\bnot eligible for remote\b|\bnot available for remote\b|\bnot open to remote\b",
    re.IGNORECASE,
)

_NEGATED_HYBRID_SENTENCE = re.compile(
    r"\bnot (?:a )?hybrid\b|\bno (?:office|in-office) (?:attendance|requirement)\b"
    r"|\bnot\s+(?:an?\s+)?(?:fully\s+)?(?:on[- ]?site|in[- ]office)\s+(?:role|position|job)\b",
    re.IGNORECASE,
)

_INTERVIEW_SENTENCE = re.compile(r"\binterview\b", re.IGNORECASE)
_CUSTOMER_VISIT_SENTENCE = re.compile(
    r"\b(?:customer|client) (?:site|visits?)\b|\b(?:at|in) (?:an? |the )?(?:customer|client)"
    r"|\bon[- ]?site\s+with\s+(?:our\s+|your\s+)?(?:customers?|clients?)\b"
    r"|\b(?:customer|client)s?'?\s+on[- ]?site\b"
    r"|\btravel\w*\s+to\s+(?:an?\s+|the\s+)?(?:customer|client)",
    re.IGNORECASE,
)
# Someone else's cadence, or a policy that has ended, is not this role's attendance.
_OTHER_ATTENDANCE_SENTENCE = re.compile(
    r"\b(?:team|colleagues|manager|people|employees|staff)\s+"
    r"(?:averages?|spends?|works?|comes?|come in)\b"
    r"|\b\w+\s+(?:that|who)\s+(?:is|are|averages?|spends?|works?|comes?)\b"
    # Attendance-only: in the remote-offer reader this would hide a real offer.
    r"|\b(?:local|in-area|nearby)\s+(?:hires|candidates|employees)\b"
    r"|\bnot enforced\b|\bpreviously\b|\bformerly\b|\blast year\b"
    r"|\b(?:previous|former)\s+(?:policy|requirement|schedule|arrangement)\b"
    r"|\bwere required\b|\bhas ended\b|\bused to\b"
    r"|\b(?:many|some|most|certain|other|all)\s+(?:of\s+(?:our|the|\w+['\u2019]s)\s+)?"
    r"(?:roles|positions|jobs|employees)\b",
    re.IGNORECASE,
)
_TRAVEL_PERCENT_SENTENCE = re.compile(
    r"\btravel\b[^.]{0,30}\d{1,3}\s*%|\d{1,3}\s*%[^.]{0,30}\btravel\b"
    r"|days?\s+(?:a|per|each)\s+(?:week|month)\s+of\s+travel",
    re.IGNORECASE,
)
# A one-time onboarding or training visit is not an ongoing office cadence.
_ONBOARDING_SENTENCE = re.compile(
    r"\b(?:for|during)\s+(?:\w+\s+)?(?:onboarding|training|orientation)\b"
    r"|\b(?:onboarding|training|orientation)\s+(?:is|will\s+be|lasts?|week|period|days?)\b",
    re.IGNORECASE,
)
_HYBRID_NONATTENDANCE = re.compile(
    r"\bhybrid\s+(?:environment|culture|cloud|infrastructure|integration)\b",
    re.IGNORECASE,
)
# A cadence beside a perk ("... receive lunch") states a benefit, not a requirement.
_BENEFIT_SENTENCE = re.compile(
    r"\breceive\b|\beligible for\b|\bqualify for\b"
    r"|\bfree\s+(?:lunch(?:es)?|meals?|snacks?|parking|food|coffee)\b",
    re.IGNORECASE,
)

_ONSITE_WORD = r"(?:on[- ]?site|in[- ]office)"
# A capitalized place right after "in" or "at" names where the role sits; "at the
# intersection" or "in a customer's" do not.
_NAMED_PLACE = r"\s+(?:at|in)\s+(?:the\s+|our\s+)?(?-i:[A-Z0-9])"
_HYBRID_ROLE_PHRASE = re.compile(
    rf"\bhybrid (?:{_ONSITE_WORD} )?"
    r"(?:role|position|job|(?:work(?:ing)? )?(?:schedule|arrangement|model))\b"
    r"|\b(?:role|position|job) (?:is|will be) (?:a )?hybrid\b",
    re.IGNORECASE,
)
# Counts alone only when the clause ends there or names where the role is based;
# "a hybrid role at the intersection of..." describes the work, not attendance.
_HYBRID_ROLE_ASSERTED = re.compile(
    r"(?:\b(?:is|will be) (?:a )?hybrid (?:role|position|job)"
    r"|\b(?:role|position|job) (?:is|will be) (?:a )?hybrid)"
    rf"(?=\s*(?:[.;!)]|$|(?:,\s*)?(?:based|located)\b)|{_NAMED_PLACE})"
    rf"|(?<!\bthe )\bhybrid (?:work(?:ing)? )?(?:role|position|job|arrangement|schedule)"
    rf"(?={_NAMED_PLACE})"
    # A work-model field ("Work Location Assignment: Hybrid") or a stated classification.
    r"|\b(?:work(?:place)?|location|arrangement)(?:\s+(?:location|model|arrangement|type"
    r"|assignment|style)){0,2}\s*:\s*(?:(?:primarily|mostly|[\w-]+site|with)\s+){0,4}hybrid\b(?!\s*(?:/|or\b)\s*remote)"
    r"|\bclassified\s+as\s+[\"'\u201c]?hybrid\b",
    re.IGNORECASE,
)
# "based in trust" names no place; a capitalized name or an office does.
_BASED_IN_PHRASE = re.compile(
    r"\b(?:based|located) (?:in|at|out of)\s+(?:(?:the|our|one\s+of\s+our)\s+)?"
    r"(?:(?-i:[A-Z0-9])|(?:\w+\s+)?offices?\b|(?:multiple|several|various)\s+locations\b)",
    re.IGNORECASE,
)
_WEEKDAY = r"(?:mon|tue|tues|wed|wednes|thu|thur|thurs|fri)(?:days?)?\b"
# A "Hybrid:" label governs an office phrase only across weekday names and
# connectors ("Hybrid: Tuesday-Thursday in the office"), never across other words.
_LABEL_TO_OFFICE = re.compile(
    rf"(?:\s|[-,&/.]|\b{_WEEKDAY}|\b(?:to|through|and)\b)*",
    re.IGNORECASE,
)
_ONSITE_ROLE_PHRASE = re.compile(
    r"\b(?:role|position|job) (?:is|will be) (?:fully )?(?:based )?(?:on[- ]?site|in[- ]office)\b"
    r"|\bis (?:an? )?(?:fully )?(?:on[- ]?site|in[- ]office) (?:role|position)\b"
    r"|\b(?:role|position|job)\b[^.;]{0,20}\bbased (?:fully )?(?:on[- ]?site|in[- ]office)"
    r" (?:in|at|out of)\b"
    r"|\bin (?:the|our) office (?:monday\s*(?:-|to|through)\s*friday|five days)\b"
    rf"|\bis (?:an? )?(?:fully )?{_ONSITE_WORD}(?:,?\s+[\w-]+){{0,3}}?\s+"
    r"(?:role|position|job|opportunity)\b"
    rf"|\b{_ONSITE_WORD} (?:role|position|job|opportunity)\b"
    # "Our team is based on-site at" and "scientists working on-site at" place other people.
    rf"|(?<!\bbased )(?<!\bworking )\b{_ONSITE_WORD},?(?:\s+(?:working|located|based))?"
    rf"{_NAMED_PLACE}",
    re.IGNORECASE,
)
_ATTENDANCE_REQUIREMENT = re.compile(
    r"\byou (?:will|must|are required|need)\b"
    r"|\bthis (?:role|position) (?:is|requires|will be)\b"
    r"|\brequir(?:ed|es|ing)\b|\bexpect(?:ed|ation)\b|\bmust\b|\bminimum\b"
    r"|\bcommit(?:s|ted|ment)?\s+to\b",
    re.IGNORECASE,
)
# Travel to office locations names a travel cadence, not a personal attendance one.
_TRAVEL_TO_OFFICE_SENTENCE = re.compile(
    r"\btravel\b.{0,40}\b(?:in|(?:in|from|to) (?:the|our)) office\b"
    r"|\btravel\w*\s+(?:on[- ]?site|in[- ]person)\b"
    r"|\b(?:quarterly|annual(?:ly)?)\s+(?:[\w-]+\s+){0,2}"
    r"(?:meetings?|visits?|gatherings?|off-?sites?|summits?|events?|retreats?)\b"
    r"|\btimes\s+(?:a|per|each)\s+(?:year|quarter)\b",
    re.IGNORECASE,
)

_ANY_DAY_COUNT = re.compile(DAY_COUNT, re.IGNORECASE)
_PART_WEEK_COUNT = r"(?:[1-4]|one|two|three|four)"
# Office time stated as part of the week without a weekly day count.
_PART_WEEK_ATTENDANCE = re.compile(
    rf"\b{_PART_WEEK_COUNT}-day\s+{_ONSITE_WORD}\s+(?:role|position|job|schedule)\b"
    # "travel for ... on-site sessions 20-30% of the time" is a travel share, and "an
    # on-site role spends 10% of the time" a share of the work, not of attendance.
    rf"|^(?:(?!\btravel\b)[^.;])*?\b(?:be|work|report)\s+(?:\w+ly\s+)?{_ONSITE_WORD}\b"
    r"[^.;]{0,30}?"
    r"\b[1-9]\d?\s*%\s+of\s+(?:the|your)\s+(?:time|week)\b"
    r"|\b(?:in|from)\s+(?:the|our)\s+office\s+part\s+of\s+the\s+week\b"
    rf"|\b{_PART_WEEK_COUNT}\s+(?:office|in-office|on-?site)\s+days\b",
    re.IGNORECASE,
)
_HYBRID_WORD = re.compile(r"\bhybrid\b", re.IGNORECASE)
_PLACED_SOMEONE = re.compile(r"\b(?:based|located)\s+$", re.IGNORECASE)
_FIRST_DAY_COUNT = re.compile(
    rf"{DAY_COUNT}(?P<period>{PER}(?:{unit('week')}|{unit('month')})|\s+weekly\b)?", re.IGNORECASE
)
# A label ("Hybrid:", "Hybrid -", "Hybrid;") or a bare "hybrid" right before a count.
_HYBRID_LABEL_ANYWHERE = re.compile(
    rf"\bhybrid(?:\s+work(?:ing)?)?(?:\s*[:;,]|\s+-(?=\s)|(?=\s*{DAY_COUNT}))", re.IGNORECASE
)
_LABELLED_OFFICE_WORD = re.compile(
    rf"\s+(?:{OFFICE_ARRANGEMENT_WORDS}|in\s+(?:the\s+)?office)\b", re.IGNORECASE
)
_AWAY_FROM_OFFICE = re.compile(r"\b(?:from|at) home\b|\bremote(?:ly)?\b", re.IGNORECASE)
# A semicolon or a comma opening a new clause ends the office phrase's clause; a
# comma inside a place name ("Burlington, MA, at least") does not.
_CLAUSE_BREAK = re.compile(
    r";|,\s+(?:and|but|or|while|working|with|on)\b|\s+and\s+(?:work|be|spend)\b", re.IGNORECASE
)
# A weekday or "work" after the office phrase starts a schedule only when a range,
# another weekday, or a count follows ("Tuesday lunch", "work orders" do not).
_SCHEDULE_WORD = (
    rf"{_WEEKDAY}(?=\s*(?:[-,/(&]|to\b|through\b|and\s+{_WEEKDAY}|{NUMBER}\b))"
    rf"|(?:work|downtown)(?=\s*(?:[.,;)]|$|{NUMBER}\b))"
    r"|by(?=\s+\d)"
)
# An office phrase names attendance only when no noun follows it ("the office
# phone line", "in-office team", "on-site support" describe something else).
_OFFICE_FOLLOWS = (
    rf"(?=\s*(?:[.,;:!?)(-]|$|(?:{NUMBER})x\b"
    rf"|(?:{NUMBER}|in|on|at|for|from|with|and|or|to|every|each"
    rf"|is|are|required|presence|attendance|days?|regularly|(?:a\s+)?minimum)\b|{_SCHEDULE_WORD}))"
)
_OFFICE_PHRASE = re.compile(
    rf"\b(?:in|from) (?:the|our)(?:\s+\w+){{0,2}}\s+office\b{_OFFICE_FOLLOWS}"
    rf"|\bon campus\b{_OFFICE_FOLLOWS}"
    rf"|\b(?:to|at) (?:the|our)(?:\s+\w+){{0,2}}\s+office\b{_OFFICE_FOLLOWS}"
    # "on-site and virtual workshops": joined to another adjective, it describes a noun.
    rf"|\b(?:on-?site|in-office)\b(?!\s+(?:and|or)\s+\w+\s+\w){_OFFICE_FOLLOWS}",
    re.IGNORECASE,
)
# A bare "in office", with no article or hyphen, is an office phrase only right
# next to its own day count ("Office 365", "in office supply audits" never qualify).
_BARE_IN_OFFICE_WITH_CADENCE = re.compile(
    rf"(?:{FIGURE})\s*(?:-\s*)?{unit('day')}\s+(?:(?:a|per|each)\s+(?:week|month)\s+)?in office\b"
    r"(?=\s*(?:[.,;:!?)]|$|(?:per|each|a|every|or|during|while|is|are|in|at)\b))"
    rf"|\bin office\s+(?:{FIGURE})\s*(?:-\s*)?{unit('day')}",
    re.IGNORECASE,
)
# A negated, optional, or chosen cadence never commits this posting to attendance.
_SOFTENED_ATTENDANCE_SENTENCE = re.compile(
    r"(?:\bnot|n't)\s+(?:\w+\s+){0,2}requir\w*|\bno\s+longer\b|\bnever\b|\bno\s+requirement\b"
    r"|\bnobody\b|\bno\s+one\b|\b(?:don'?t|do\s+not|does\s+not)\s+(?:require|mandate)\b"
    r"|\bnot\s+mandatory\b|\boptional\b|\bvoluntary\b|\bas\s+(?:required|needed)\b"
    r"|\bencouraged\b|\bmay\s+be\s+(?:required|expected)\b|\bchoose\b|\boccasional(?:ly)?\b"
    r"|\bprefer\b|\bmay\s+consider\b"
    # "preferably located in EST" prefers a place, not the attendance.
    r"|\b(?:ideally|preferabl[ey]|preferred)\b(?!,?\s+(?:located|based|in)\b)"
    r"|\b(?:most|some)\s+weeks\b",
    re.IGNORECASE,
)
_DAYS = rf"(?:{FIGURE})\s*(?:-\s*)?{unit('day')}"
_SOFTENER = r"\b(?:preferred|ideally|preferabl[ey])\b"
# A softened count in an aside ("(4 days preferred)") softens that count, not the requirement.
_SOFTENED_ASIDE = re.compile(
    rf"\([^()]{{0,80}}?{_DAYS}[^()]{{0,80}}?{_SOFTENER}[^()]{{0,80}}\)"
    rf"|,[^,;()]{{0,80}}?{_DAYS}[^,;()]{{0,80}}?{_SOFTENER}[^,;()]{{0,80}}$",
    re.IGNORECASE,
)
# A ceiling on office days commits nobody to attend, yet it still names attendance.
_CAPPED_CADENCE = re.compile(rf"\bup\s+to\s+{NUMBER}\b", re.IGNORECASE)
# A trial-period cadence is not the posting's standing attendance commitment.
_TEMPORARY_ATTENDANCE_SENTENCE = re.compile(
    rf"\b(?:for|in|during)\s+(?:the|your)\s+first\s+(?:{NUMBER}\s+)?(?:day|week|month|year)s?\b"
    r"|\bprobation(?:ary)?\b|\binitially\b|\bto start\b",
    re.IGNORECASE,
)


def _counts_as_cadence(text: str) -> bool:
    """Return whether the first day count in text has its own weekly or 4+ monthly unit."""
    first = _FIRST_DAY_COUNT.search(text)
    if first is None or first["period"] is None:
        return False
    minimum = 4 if "mo" in first["period"].casefold() else 1
    return stated_days(first) >= minimum


def _required_here(sentence: str, office: re.Match[str]) -> bool:
    """Return whether a requirement or "Hybrid:" label governs this office phrase's clause."""
    labels = list(_HYBRID_LABEL_ANYWHERE.finditer(sentence[: office.start()]))
    if labels and _LABEL_TO_OFFICE.fullmatch(sentence[labels[-1].end() : office.start()]):
        return True
    before = sentence[max(0, office.start() - 40) : office.start()].rsplit(";", 1)[-1]
    after = _CLAUSE_BREAK.split(sentence[office.end() : office.end() + 60], maxsplit=1)[0]
    return bool(_ATTENDANCE_REQUIREMENT.search(before) or _ATTENDANCE_REQUIREMENT.search(after))


def _office_cadence(sentence: str, office: re.Match[str]) -> str | None:
    """Return "hybrid" when a required weekly or 4+ monthly count belongs to the office phrase."""
    # The requirement and the count must belong to this phrase's clause, never
    # another one ("work 5 days per week, with 2 days in office each quarter").
    if not _required_here(sentence, office):
        return None
    if office.re is _BARE_IN_OFFICE_WITH_CADENCE:
        counted = sentence[office.start() : office.end() + 12]
        counted = re.sub(r"\s+in office\b", "", counted, flags=re.IGNORECASE)
        return "hybrid" if _counts_as_cadence(counted) else None
    before = sentence[max(0, office.start() - 20) : office.start()].rsplit(";", 1)[-1]
    after = _CLAUSE_BREAK.split(sentence[office.end() : office.end() + 60], maxsplit=1)[0]
    leading = DAYS_PER_WEEK.search(before)
    if _counts_as_cadence(after) or (leading is not None and stated_days(leading) >= 1):
        return "hybrid"
    return None


def _label_cadence(sentence: str, label: re.Match[str]) -> bool:
    """Return whether the clause right after a "Hybrid:" label states its own cadence."""
    days = _LABEL_TO_OFFICE.match(sentence, label.end())
    start = days.end() if days is not None else label.end()
    unplaced = _LABELLED_OFFICE_WORD.sub("", sentence[start : start + 50])[:30]
    counted = _CLAUSE_BREAK.split(unplaced)[0]
    # "Hybrid 3 days a week from home" counts the days away from the office.
    return not _AWAY_FROM_OFFICE.search(counted) and _counts_as_cadence(counted)


def _attendance_kind(sentence: str) -> str | None:
    """Return "hybrid" or "on_site" when this one sentence commits to attendance."""
    if any(_label_cadence(sentence, label) for label in _HYBRID_LABEL_ANYWHERE.finditer(sentence)):
        return "hybrid"
    if _HYBRID_ROLE_ASSERTED.search(sentence) or (
        _HYBRID_ROLE_PHRASE.search(sentence)
        and (
            _OFFICE_PHRASE.search(sentence)
            or _BASED_IN_PHRASE.search(sentence)
            or DAYS_PER_WEEK.search(sentence)
            or DAYS_PER_MONTH.search(sentence)
        )
    ):
        return "hybrid"
    if _PART_WEEK_ATTENDANCE.search(sentence):
        return "hybrid"
    # An explicit office cadence outranks a bare on-site statement in the same sentence.
    offices = [*_OFFICE_PHRASE.finditer(sentence), *_BARE_IN_OFFICE_WITH_CADENCE.finditer(sentence)]
    week = DAYS_PER_WEEK.search(sentence)
    if any(_office_cadence(sentence, office) is not None for office in offices):
        full_week = week is not None and stated_days(week) >= _FULL_WEEK
        return "on_site" if full_week and not _HYBRID_WORD.search(sentence) else "hybrid"
    if _ONSITE_ROLE_PHRASE.search(sentence):
        # A placed on-site statement under a "Hybrid:" label or with a part-week count is hybrid.
        part_week = week is not None and stated_days(week) < _FULL_WEEK
        return "hybrid" if part_week or _HYBRID_LABEL_ANYWHERE.search(sentence) else "on_site"
    # A required office phrase with no day count at all is standing attendance;
    # "2 days per quarter" names a visit cadence, and "clinicians based on-site" other staff.
    if not _ANY_DAY_COUNT.search(sentence) and any(
        _required_here(sentence, office) and not _PLACED_SOMEONE.search(sentence, 0, office.start())
        for office in offices
    ):
        return "on_site"
    return None


def attendance_statement(text: str, *, capped: bool = True) -> tuple[str, str] | None:
    """Return (kind, evidence sentence) for one sentence that commits to office attendance.

    Reads sentence by sentence rather than by line: a conditional, negated, or
    merely descriptive ("hybrid cloud") sentence never counts, and neither does
    an interview, a customer visit, a travel percentage, or a visit cadence
    under four days a month. ``capped=False`` also reads an "up to N days" cadence.
    """
    for sentence in sentences(plain(text)):
        if attendance_is_hedged(sentence, capped=capped):
            continue
        kind = _attendance_kind(sentence)
        if kind is not None:
            return kind, sentence
    return None


def attendance_is_hedged(sentence: str, *, capped: bool = True) -> bool:
    """Return whether a sentence stops short of committing this role to standing attendance.

    ``capped=False`` lets an "up to N days" cadence through, for a caller that
    only needs to know attendance was named.
    """
    return bool(
        _CONDITIONAL_SENTENCE.search(sentence)
        or _CONDITIONAL_ATTENDANCE_SENTENCE.search(sentence)
        or _NEGATED_HYBRID_SENTENCE.search(sentence)
        or _REMOTE_REFUSED_SENTENCE.search(sentence)
        or _INTERVIEW_SENTENCE.search(sentence)
        or _CUSTOMER_VISIT_SENTENCE.search(sentence)
        or _OTHER_ATTENDANCE_SENTENCE.search(sentence)
        or _TRAVEL_PERCENT_SENTENCE.search(sentence)
        or _TRAVEL_TO_OFFICE_SENTENCE.search(sentence)
        or _HYBRID_NONATTENDANCE.search(sentence)
        or _BENEFIT_SENTENCE.search(sentence)
        or _SOFTENED_ATTENDANCE_SENTENCE.search(_SOFTENED_ASIDE.sub(" ", sentence))
        or (capped and _CAPPED_CADENCE.search(sentence))
        or _TEMPORARY_ATTENDANCE_SENTENCE.search(sentence)
        or _ONBOARDING_SENTENCE.search(sentence)
    )


# Any mention of remote work or work-from-home that is not conditional,
# negated, or refused counts as an offer. A false offer withholds a hold-out;
# a missed offer lets an office row at a remote-only market be held out, restorably.
_WHOLE_COUNTRY = r"(?:(?-i:US|USA)\b|U\.S\.(?:A\.)?|United\s+States\b|Canada\b)"
_COUNTRYWIDE = (
    rf"(?:anywhere\s+in\s+(?:the\s+)?{_WHOLE_COUNTRY}|across\s+(?:the\s+)?{_WHOLE_COUNTRY}"
    rf"|any\s+(?:{_WHOLE_COUNTRY}|canadian)\s+location\b)"
)
_PLACED_PERSON = (
    r"(?:candidates|applicants|people|talent|team\s+members|employees|you"
    r"|(?:this|the)\s+(?:role|position))"
)
_REMOTE_OFFER = re.compile(
    r"\bremote(?:ly)?\b|\bwork(?:ing)?[- ]from[- ]home\b|\bwfh\b|\bhome[- ]based\b"
    r"|\btelecommut\w*|\bwork(?:ing)?\s+virtually\s+(?:full[- ]?time|100\s*%)"
    r"|\bvirtual\s+(?:role|position|job)\b(?!\s+(?:tryout|fair|preview|simulation|assessment)s?\b)"
    r"|\bfully\s+distributed\b|\bwork(?:ing)?[- ](?:from[- ])?anywhere\b"
    r"|\b(?:fully|100\s*%|all)[- ]virtual\b|\bvirtual\s+(?:company|organi[sz]ation|workforce)\b"
    r"|\btelework\w*"
    # A person or the role placed anywhere in a whole country is remote work there;
    # care, clinics or customers across a country describe the employer.
    rf"|\b{_PLACED_PERSON}(?:\s+[\w-]+){{0,3}}?\s+(?:based|located|living|live|reside|residing"
    rf"|work|working|performed|done|hired)\s+(?:from\s+)?{_COUNTRYWIDE}"
    rf"|\b(?:open\s+to|hir(?:e|es|ing))\s+{_PLACED_PERSON}\s+{_COUNTRYWIDE}",
    re.IGNORECASE,
)
# Remote work on the days a hybrid schedule leaves outside the office is that
# schedule's remainder, not an offer to work remotely instead of attending.
_HYBRID_REMAINDER_REMOTE = re.compile(
    r"(?:\b(?:remaining|other)\s+(?:work(?:ing)?\s+)?days?|\bthe\s+rest\s+of\s+the\s+(?:week|time))"
    r"\b[^.;]{0,30}?\bremote(?:ly)?\b",
    re.IGNORECASE,
)
# "Fully remote, with office time on other days" keeps its offer through _hybrid_share.
_REMOTE_THEN_REMAINDER = re.compile(
    r"\bremote(?:ly)?\b[^.;]{0,30}?\b(?:for\s+|on\s+)?(?:the\s+)?(?:remaining|other)"
    r"\s+(?:work(?:ing)?\s+)?days?\b",
    re.IGNORECASE,
)
# A day count bound to "remote" in a hybrid sentence is that schedule's share, not an offer.
_CLAUSE_GAP = r"(?:(?!\bor\b|remote)[^.;,:]){0,20}?"
_DAYS_THEN_REMOTE = re.compile(rf"{DAY_COUNT}{_CLAUSE_GAP}\bremote(?:ly)?\b", re.IGNORECASE)
_REMOTE_THEN_DAYS = re.compile(rf"\bremote(?:ly)?\b{_CLAUSE_GAP}{DAY_COUNT}", re.IGNORECASE)
_HYBRID_CONTEXT = re.compile(r"\bhybrid\b|\boffice\s+presence\b", re.IGNORECASE)
# Remote time a hybrid model mixes with office time is part of that model.
_REMOTE_IN_HYBRID = re.compile(
    r"\bflexibility\s+(?:for|to|of)\s+(?:also\s+)?(?:work(?:ing)?\s+)?"
    r"(?:remote(?:ly)?|from\s+home)\b"
    r"|\bremote\s+flexibility\b"
    r"|\b(?:mix|split|blend|balance|combination)\b[^.;]{0,50}?\band\s+remote\b"
    r"|\bcombining\b[^.;]{0,50}?\bwith\s+remote\b"
    r"|\bhybrid\s*\(\s*remote\s*(?:&|and|/)\s*on[- ]?site\s*\)",
    re.IGNORECASE,
)
# Full-time remote, or five remote days, is an offer even inside a hybrid sentence.
_FULL_TIME_REMOTE = re.compile(r"\bfull(?:y|[- ]?time)\b|\b100\s*%", re.IGNORECASE)
_FULL_WEEK = 5
# Work models listed beside an employer-category cue name its categories, not this role's.
_WORK_MODEL_LIST = re.compile(
    rf"\b{_WORK_MODEL}(?:\s*,\s*{_WORK_MODEL})+,?\s+(?:and|or)\s+{_WORK_MODEL}\b", re.IGNORECASE
)
_EMPLOYER_CATEGORY_CUE = re.compile(
    r"\bwork(?:ing)?\s+models?\b|\bour\s+(?:roles|positions|jobs)\b|\bstructur(?:e|ed)\b"
    r"|\bclassified\b|\bfall\s+into\b|\bcategor(?:y|ies)\b|\bwork\s+schedules\b",
    re.IGNORECASE,
)
# "Experience leading remote teams" asks the candidate for a background.
_REMOTE_EXPERIENCE = re.compile(
    r"\bexperience\s+(?:\w+ing|in|with|within)\s+(?:[\w/-]+\s+){0,3}?remote(?:ly)?\b",
    re.IGNORECASE,
)
# A pay header scoped to "remote and/or California based roles" names a category.
_REMOTE_PAY_SCOPE = re.compile(
    r"\bremote\s+and\s*/\s*or\s+[a-z.]+(?:\s+[a-z.]+)?[\s-]+based\s+(?:roles|positions)\b",
    re.IGNORECASE,
)
# A policy's name ("Hybrid and Remote Work policy") offers nothing in a sentence
# that classifies the role as hybrid or office work.
_REMOTE_POLICY_NAME = re.compile(
    r"\b(?:hybrid\s+(?:and|&)\s+)?remote\s+work(?:ing)?\s+polic(?:y|ies)\b", re.IGNORECASE
)
_OFFICE_CLASSIFICATION = re.compile(
    rf"\b(?:classified\s+as|is)\s+(?:an?\s+)?[\"'“]?"
    rf"(?:hybrid|{OFFICE_ARRANGEMENT_WORDS}|office[\s-]based)\b",
    re.IGNORECASE,
)
# "Remote or field-based positions ... will be indicated in the job posting" defers
# the arrangement to each posting and offers nothing for this one.
_DEFERRED_REMOTE_ROLES = re.compile(
    r"\b(?:remote|field)(?:[- ]based)?(?:\s+or\s+(?:remote|field)(?:[- ]based)?)?"
    r"\s+(?:positions?|roles?|jobs?)\b"
    r"(?=[^.;]{0,80}?\barrangements?\b[^.;]{0,40}?\b(?:will\s+be|are|is)\s+"
    r"(?:indicated|noted|specified|stated|listed|identified|outlined)\s+(?:in|on|within)\s+"
    r"(?:the|each|their)\s+(?:job\s+)?(?:postings?|descriptions?|requisitions?)\b)",
    re.IGNORECASE,
)
_LINKEDIN_REMOTE_TAG = re.compile(r"#\s?li[-_]?remote\b", re.IGNORECASE)
_REMOTE_MENTION = re.compile(r"\bremote(?:ly)?\b", re.IGNORECASE)
_NEGATION_WORD = re.compile(
    r"\b(?:no|not|without|isn't|is not|aren't|are not|won't|will not|doesn't|does not"
    r"|cannot|can't|can not)\b",
    re.IGNORECASE,
)
# "Remote patient monitoring" and the like name a product or a team, not a
# remote-work offer for this role.
_REMOTE_NOUN_GUARD = re.compile(
    r"\bremote (?:patient|monitoring|sensing|access|team members?)\b", re.IGNORECASE
)
# A remote interview, home-based equipment or a remote task ("remote coverage") names a
# process, a requirement or the work itself; only that mention is removed, so an offer
# elsewhere in the sentence still reads.
_REMOTE_PROCESS_MENTION = re.compile(
    r"\bhome[- ]based\s+(?:internet|equipment|connectivity)\b"
    r"|\binterviews?\s+(?:(?:are|will\s+be)\s+)?(?:conducted|held)\s+remotely\b"
    r"|\bremote(?:ly)?\s+interviews?\b"
    r"|\bremote[- ](?:care|conferencing|technical\s+support|experts?|coverage"
    r"|detailing|diagnostics?|cach(?:e|es|ing)|execution|supervision|pilots?|calls?"
    r"|development|desktops?)\b"
    r"(?!(?:\s+(?!(?:to|of|for|with|and|or|across|in|on|at|by)\b)[\w-]+){0,2}?"
    r"\s+(?:roles?|positions?|jobs?|opportunit|nurses?|agents?"
    r"|specialists?|representatives?|coordinators?|engineers?|analysts?|managers?"
    r"|associates?|consultants?|technicians?|advisors?|developers?|cent(?:er|re))\b)"
    # Manner, tooling and teamwork wording says how work is done, not where the job is.
    r"|\b(?:effective(?:ly)?|efficient(?:ly)?|collaborat\w+|operat\w+)\s+(?:[\w-]+\s+)?"
    r"in\s+a\s+remote\s+(?:manner|fashion|way)\b"
    r"|\b(?:skills?|include[sd]?|including|comfortable|proficien\w*|experience)\b[^.;]{0,40}?"
    r"\bremotely\s+(?:on|through|via|over|using)\s+(?:[\w-]+\s+){0,3}?(?:networks?|systems?"
    r"|screen[- ]sharing|ticketing|tools?|platforms?|vpn|connections?|desktops?|software)\b"
    r"|\b(?:(?:support|assist|troubleshoot)(?:s|ing|ed)?|diagnos(?:e[sd]?|ing|is))\s+"
    r"(?:(?!work(?:s|ing|ed)?\b)[\w-]+\s+){0,2}?remotely\b"
    r"|\b(?:global|distributed|virtual|international)\s+(?:and|or|&)\s+remote\s+"
    r"(?:environments?|contexts?|settings?)\b",
    re.IGNORECASE,
)
_REMOTE_WORK_WORDS = (
    r"\bremote|\bwork(?:ing)?[- ]from[- ]home|\bwfh|\bhome[- ]office|\btelework(?:ing)?"
)
_REMOTE_FIRST = r"\bremote[- ](?:first|friendly)\b"
# A home-working perk, a trait asked of the candidate, or a remote-first employer that
# keeps offices describes the employer, not this role.
_REMOTE_PERK_OR_CULTURE = re.compile(
    rf"(?:{_REMOTE_WORK_WORDS})[\s-]+(?:(?:office|work(?:space)?|setup|set-up|equipment)[\s-]+)*"
    r"(?:stipends?|allowances?|reimbursements?|budgets?|benefits?|perks?)\b"
    r"|\b(?:stipends?|allowances?|reimbursements?|budgets?)\s+(?:for|to|toward)\s+(?:your\s+)?"
    rf"(?:{_REMOTE_WORK_WORDS})\b"
    # Money toward home-working kit may name it after other items, mid-list.
    r"|\b(?:stipends?|allowances?|reimbursements?|budgets?)\s+(?:of|for|to|toward)\s+"
    rf"(?:(?!{_REMOTE_WORK_WORDS}|\bwork(?:ing)?[- ]from)[^.;]){{0,40}}?"
    rf"(?:{_REMOTE_WORK_WORDS})[\s-]+(?:work[\s-]+)?(?:equipment|setup|set-up|space|furniture)\b"
    r"|\b(?:energi[sz]ed|excited|motivated|thrive|thriving|enjoy|love)\b[^.;]{0,30}?"
    rf"{_REMOTE_FIRST}"
    rf"|{_REMOTE_FIRST}[^.;]{{0,40}}?\bwith\s+(?:\w+\s+)?(?:hubs?|offices?|headquarters)\b",
    re.IGNORECASE,
)


def remote_perk_or_culture(text: str) -> bool:
    """Return whether text names a home-working perk, a candidate trait, or an employer culture."""
    return bool(_REMOTE_PERK_OR_CULTURE.search(text))


# "will not have the ability to be located remotely" negates from beyond the 30-character
# window; the negated ability must take the remote clause itself as its complement.
_REMOTE_ABILITY_NEGATED = re.compile(
    r"(?:\bnot|n't)\s+(?:\w+\s+){0,2}?"
    r"(?:ability|able|option|opportunity|eligible|permitted|allowed|possible)"
    r"(?:\s+at\s+this\s+time)?\s+(?:to|of|for)\s+"
    r"(?:(?:be|being|located|based|work|working|performed|done|fully|completely|entirely)\s+){0,3}"
    r"remote(?:ly)?\b",
    re.IGNORECASE,
)


# An offer word before "remote" makes a following "if not" the offer's condition.
_CONDITIONED_REMOTE_OFFER = re.compile(
    r"\b(?:open\s+to\s+remote(?:ly)?|remote(?:\s+work)?\s+(?:is\s+)?(?:available|considered))"
    r"\s+if\s+not\b",
    re.IGNORECASE,
)


def _remote_mention_negated(sentence: str) -> bool:
    """Return whether a negation word sits close to a "remote" mention, either order."""
    sentence = _CONDITIONED_REMOTE_OFFER.sub(lambda match: match.group(0)[:-3], sentence)
    return bool(_REMOTE_ABILITY_NEGATED.search(sentence)) or any(
        _NEGATION_WORD.search(sentence[max(0, match.start() - 30) : match.end() + 30])
        for match in _REMOTE_MENTION.finditer(sentence)
    )


def _hybrid_share(match: re.Match[str]) -> str:
    """Blank remote time a hybrid schedule shares out, and keep it when it is full time."""
    around = match.string[max(0, match.start() - 20) : match.end() + 20]
    counted = "days" in match.re.groupindex and stated_days(match) >= _FULL_WEEK
    return match.group(0) if counted or _FULL_TIME_REMOTE.search(around) else " "


def _offered_remote(sentence: str) -> str:
    """Return the sentence without the remote mentions that describe something besides an offer."""
    offered = _REMOTE_PROCESS_MENTION.sub(" ", _HYBRID_REMAINDER_REMOTE.sub(" ", sentence))
    offered = _REMOTE_PERK_OR_CULTURE.sub(" ", offered)
    offered = _REMOTE_EXPERIENCE.sub(" ", _REMOTE_THEN_REMAINDER.sub(_hybrid_share, offered))
    offered = _DEFERRED_REMOTE_ROLES.sub(" ", _REMOTE_PAY_SCOPE.sub(" ", offered))
    if _OFFICE_CLASSIFICATION.search(sentence):
        offered = _REMOTE_POLICY_NAME.sub(" ", offered)
    if _EMPLOYER_CATEGORY_CUE.search(sentence):
        offered = _WORK_MODEL_LIST.sub(" ", offered)
    if _HYBRID_CONTEXT.search(sentence):
        for pattern in (_DAYS_THEN_REMOTE, _REMOTE_THEN_DAYS, _REMOTE_IN_HYBRID):
            offered = pattern.sub(_hybrid_share, offered)
    return offered


def remote_offer_statement(text: str) -> str | None:
    """Return the sentence stating a remote offer for this role, or None.

    A conditional ("For Hybrid Roles: ...") or explicitly refused or negated
    sentence never counts, so a hybrid-only boilerplate block cannot look
    like an offer, and "remote patient monitoring" never names one either.
    """
    conditioned_list = False
    for sentence in sentences(plain(text)):
        # Items under a conditional heading ("For remote roles:") share its condition.
        if conditioned_list and ITEM_MARKER.match(sentence):
            continue
        conditioned_list = bool(_CONDITIONAL_SENTENCE.search(sentence)) and sentence.endswith(":")
        if (
            _CONDITIONAL_SENTENCE.search(sentence)
            or _REMOTE_REFUSED_SENTENCE.search(sentence)
            or _remote_mention_negated(sentence)
        ):
            continue
        if _LINKEDIN_REMOTE_TAG.search(sentence):
            return sentence
        offered = _offered_remote(sentence)
        if _REMOTE_OFFER.search(offered) and not _REMOTE_NOUN_GUARD.search(offered):
            return sentence
    return None
