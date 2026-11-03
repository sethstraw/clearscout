"""Select the posting lines that state the role's own work arrangement."""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import takewhile

from clearscout.application.tailoring.fit.facts.arrangement_vocabulary import (
    ANY_PLACE,
    ARRANGEMENT_LABEL,
    ARRANGEMENT_WORD,
    AWAY,
    IN_PLACE_WORD,
    OFFERED_WORDS,
    ROLE_ARRANGEMENT_REFERENCE,
    ROLE_SUBJECT,
)
from clearscout.application.tailoring.fit.lexicon import (
    HEDGED_ROLES,
    HYBRID_ROLE,
    LINKEDIN_ARRANGEMENT_TOKEN,
    MUST_MARKERS,
    OFFICE_PRESENCE,
    THIS_ROLE,
    WISH_OPENER,
    office_days,
    phrase_breaks,
    role_policy_bindings,
)
from clearscout.application.tailoring.fit.structure import (
    Block,
    SectionKind,
    unwaived,
)
from clearscout.domain.arrangement_grammar import (
    attendance_statement,
)
from clearscout.domain.taxonomy import says
from clearscout.shared.layout import RECRUITER_TAG
from clearscout.shared.text import sentence_pieces

_INTERVIEW_SECTION_HEADINGS = ("our interview practices",)
_HIRING_ACTORS = (
    "applicant",
    "applicants",
    "candidate",
    "candidates",
    "interview",
    "interviews",
)
_HIRING_PROCESSES = (
    "recruitment process",
    "hiring process",
    "selection process",
    "interview process",
)
_INTERVIEW_WORDS = ("interview", "interviews", "interviewing")
_ELSEWHERE_WORDS = (
    "travel",
    "travels",
    "traveling",
    "travelling",
    "customer",
    "customers",
    "client",
    "clients",
)
# A section listing what the employer gives, where an arrangement is one more perk.
_OFFER_HEADING = re.compile(r"\bbenefits?\b|\bperks?\b|\bwhat\s+we\s+offer\b", re.IGNORECASE)
# "our (vibrant) Springfield office": an office named by its place, never "our home office".
_NAMED_OFFICE = re.compile(
    r"\b(?:our|the)\s+(?:(?-i:[a-z])[\w'-]*\s+){0,2}"
    r"(?:(?!home\s)(?-i:[A-Z])[\w.'-]*\s+){1,3}office\b",
    re.IGNORECASE,
)
# Symbols such as an emoji bullet, which separate the items of a perks list run into one line.
_LIST_SYMBOLS = re.compile(r"[^\w\s.,;:!?()'\"&/%$+-]+")
_WEEKDAY = re.compile(r"\b(?:mon|tues|wednes|thurs|fri)days?\b", re.IGNORECASE)
_ATTENDANCE_WORD = re.compile(rf"\bhybrid\b|\bin-person\b|{OFFICE_PRESENCE.pattern}", re.IGNORECASE)
# "Speak with your recruiter about on-site expectations for your role" says whom to
# ask, not where the work happens.
_REFERRAL_ACTOR = re.compile(
    r"\b(?:recruiters?|recruiting\s+(?:team|partner)s?|talent\s+(?:acquisition\s+)?(?:team|partner)s?"
    r"|hiring\s+(?:managers?|teams?)|people\s+partners?)\b",
    re.IGNORECASE,
)
_REFERRAL_ASK = re.compile(
    r"\b(?:speak|talk|ask|discuss|contact|consult|check|reach\s+out|connect|share|provide"
    r"|explain|confirm|clarify|learn|more\s+information)\b",
    re.IGNORECASE,
)
_REFERRED_TOPIC = re.compile(
    rf"\b{ARRANGEMENT_WORD}\s+(?:work\s+)?(?:expectations?|requirements?|polic(?:y|ies)|guidelines)\b",
    re.IGNORECASE,
)
_TASK_NOUN = (
    r"(?:monitoring(?:\s+visits?)?|visits?|support|audits?|inspections?|assessments?"
    r"|evaluations?|training)"
)
_LISTED = r"\s*(?:,|/|&|\band\b|\bor\b)\s*(?:(?:and|or)\s+)?"
_METHOD = r"(?:agile|waterfall|scrum|kanban|iterative|incremental|predictive|adaptive)"
# How a message travels: "by phone or in person" names a channel, not a workplace.
_CHANNEL = (
    r"(?:(?:by|via|over|through)\s+(?:the\s+)?)?"
    r"(?:(?:tele)?phone|e-?mail|video(?:\s+calls?)?|chat|text|zoom|mail|letter|verbally"
    r"|virtual(?:ly)?)"
)
# An attendance word describing something other than where the work happens. A pair
# such as "on-site and remote monitoring" names a task, so the whole pair goes first.
_NOT_AN_ARRANGEMENT = re.compile(
    r"\b(?:on[- ]?site|in[- ]person|in[- ]office|remote(?:ly)?)\s*(?:,|/|\band\b|\bor\b)\s*"
    rf"(?:on[- ]?site|in[- ]person|in[- ]office|remote(?:ly)?)\s+{_TASK_NOUN}\b"
    rf"|\b(?:on[- ]?site|in[- ]person)\s+{_TASK_NOUN}\b"
    r"|\bpolic(?:y|ies)\s+(?:on|for)\s+(?:hybrid\s*/\s*virtual|virtual\s*/\s*hybrid)"
    r"\s+work\b"
    r"|\b(?:we\s+are|we're|we\s+remain)\s+remote[- ]friendly\b"
    r"|\bhybrid\s+(?:clouds?|batch|streaming|delivery|ecosystems?|infrastructure|architectures?"
    r"|workloads?|systems?|solutions?|deployments?|integrations?|platforms?|networks?|storage"
    r"|applications?|apps?|methodolog(?:y|ies)|frameworks?|project\s+management)\b"
    r"|\bhybrid\s+(?:(?!(?:role|position|job|schedule|work\w*)\b)[\w-]+\s+){0,2}"
    r"(?:programs?|programmes?|events?|education|learning|meetings?|conferences?|courses?"
    r"|classes|curricul(?:um|a)|sessions?|webinars?|trainings?)\b"
    rf"|\b(?:{_METHOD}{_LISTED})+hybrid\b"
    rf"|\b{_CHANNEL}(?:{_LISTED}{_CHANNEL}){{0,3}}{_LISTED}in[- ]person\b"
    rf"|\bin[- ]person(?:{_LISTED}{_CHANNEL})+\b"
    r"|\b(?:on[- ]?site|in[- ]person|in[- ]office)\s+(?:customers?|clients?|interviews?|visits?"
    r"|collaboration|(?:customer\s+)?(?:service|support)\s+experience"
    r"|execution|events?|logistics|workshops?|presentations?|conferences?|summits?)\b"
    r"|\bon[- ]?site\s+(?:amenit(?:y|ies)|parking|gyms?|fitness\s+cent(?:er|re)s?|cafeterias?"
    r"|cafes?|dining|child\s?care|day\s?care|clinics?|ev\s+charging|bike\s+(?:storage|racks?)"
    r"|showers?|lab(?:oratory)?\s+space|facilities)\b"
    r"|\bremote[- ]friendly\s+(?:culture|company|environment|workplace|organi[sz]ation)\b"
    r"|\bremote(?:ly)?[\s-]+(?:patients?|care|monitor(?:ing|ed)?|sensing|access|diagnostics?"
    r"|health(?:care)?|medicine|devices?|areas?|communities|regions?|populations?)\b",
    re.IGNORECASE,
)
# "Candidates who can work onsite will be given preference" wishes for the attendance.
_WISHED = re.compile(r"\bprefer(?:red|ably|ence)\b|\bideally\b", re.IGNORECASE)
# "Onsite in Boston (local candidates preferred)" and "PCR preferred - onsite": a wish or a
# belief set off by a bracket, a bar, or a spaced dash leaves the other side unqualified.
_SET_OFF = re.compile(r"[()|]|\s[-\N{EN DASH}\N{EM DASH}]\s")
# A wish after "with" or "who" also qualifies only the words that follow it.
_WISH_SPLIT = re.compile(rf"{_SET_OFF.pattern}|\b(?:with|who)\b", re.IGNORECASE)
# "Candidates willing to relocate to Springfield are encouraged to apply" invites; the
# relocation it names is the candidate's, not the role's arrangement.
_INVITED = re.compile(r"\b(?:encouraged|welcomed?|invited)\s+to\s+apply\b", re.IGNORECASE)
_RELOCATION = re.compile(r"\brelocate\s+to\b", re.IGNORECASE)
# "We have always believed that an in-person culture matters" states a value, not a workplace.
_EMPLOYER_BELIEF = re.compile(
    r"\bwe(?:['\u2019]ve)?\s+(?:(?:have|always|strongly|firmly|truly|deeply|still)\s+){0,3}"
    r"believed?\b",
    re.IGNORECASE,
)
# "Hybrid, Agile and Waterfall" lists methods, unless the role or a label states hybrid first.
_HYBRID_LED_METHODS = re.compile(rf"\bhybrid(?:{_LISTED}{_METHOD})+\b", re.IGNORECASE)
_OPERATION = (
    r"(?:operations?|systems?|infrastructure|data\s+cent(?:er|re)s?|manufacturing|support"
    r"|stud(?:y|ies)|equipment|servers?|hardware)"
)
# "Owns on-site systems" names what the work is on, but only where the operational noun
# heads its phrase: "on-site data center technician" and "... work" place the role.
_ON_SITE_OPERATIONS = re.compile(
    rf"\bon[- ]?site\s+(?:{_OPERATION}\s+)*{_OPERATION}"
    r"(?=\s*(?:[.,;:)&/]|$)|\s+(?:and|or|of|for|in|at|to|with|across|including|that|which"
    r"|related|relating|supporting|used|within|throughout)\b)",
    re.IGNORECASE,
)
_PLACE_OBJECT = (
    r"(?!(?:our|the)\s+(?:[\w-]+\s+){1,3}?levels?\b)"
    r"(?:our|the)\s+(?:[\w-]+\s+){0,2}?(?:sites?|offices?|data\s+cent(?:er|re)s?"
    r"|facilit(?:y|ies)|campus(?:es)?|plants?|labs?|laborator(?:y|ies))\b"
)
# "Cloud-based" and "role-based access" are compounds that place nothing, nor does "works
# in partnership". "On-site operations at our campus" places the work; "at the site level"
# and "staff at our plants" do not.
_PLACES_ROLE = re.compile(
    r"\bwork(?:s|ing)?\s+(?:in\s+(?!(?:\w+\s+)?(?:partnership|collaboration|coordination"
    r"|tandem|concert)\b)|at\b|from\b|on[- ]?site\b)"
    r"|(?<!\bwith\s)(?<!\bon\s)(?<!\bfor\s)(?<!\bto\s)(?<!\bof\s)"
    rf"\bon[- ]?site\s+(?:{_OPERATION}\s+)*{_OPERATION}\s+at\s+{_PLACE_OBJECT}"
    r"|\b(?:we\s+are|we['\u2019]re)\s+hiring\s+for\s+on[- ]?site\b"
    r"|(?<!-)\bbased\b|\brequires?\b.*\bwork\b"
    r"|\byou(?:'ll|\s+will)\s+(?:\w+\s+)?work\b|(?<!-)\b(?:role|position|job)\b(?!-)"
    r"|(?:\b100\s*%|\bfully|\bentirely)\s+on[- ]?site\b",
    re.IGNORECASE,
)
# What opens a fragment or a label's value: "On-site operations", "Schedule: ", "Title - ".
_FRAGMENT_OR_LABEL_END = re.compile(r"(?:^\W*|[:|(\u2013\u2014-]\s*)$")
# "Strong leadership based on site" places somebody else, never the reader.
_ANOTHER_PARTY_ON_SITE = re.compile(
    r"\b(?:leadership|leaders|executives|management|teams?|staff|scientists|researchers"
    r"|colleagues|employees|engineers)\s+(?:(?:is|are)\s+)?(?:based|working|located)"
    r"\s+on[- ]?site\b",
    re.IGNORECASE,
)
_WORKPLACE_CHOICE = re.compile(
    r"\b(?:choose\s+where\s+(?:you|they|employees)\s+work"
    r"|choice(?:\s+and\s+flexibility)?\s+to\s+work)\b",
    re.IGNORECASE,
)
_EITHER = r"(?:\s*,\s*(?:(?:and\s*/\s*or|or|and)\s+)?|\s+(?:and\s*/\s*or|or|and)\s+)"
_PLACE_LIST = re.compile(
    rf"\b{ANY_PLACE}(?:{_EITHER}{ANY_PLACE})+\b|\bnot\s+all\s+work\b.*", re.IGNORECASE
)
# "Programs that are digital, live virtual, in-person, and hybrid" lists delivery formats:
# a list opening with a format word names no workplace, whatever else it lists.
_FORMAT = r"(?:digital|on[- ]demand|self[- ]paced|online|recorded|live)"
_FORMAT_ITEM = rf"(?:(?:live\s+)?(?:{ANY_PLACE}|{_FORMAT}))"
_FORMAT_LIST = re.compile(
    rf"\b(?!live\b){_FORMAT}(?:{_EITHER}{_FORMAT_ITEM}){{2,}}\b", re.IGNORECASE
)
_WORK_TYPE = r"(?:site|remote|field|office|home)-\w+(?:-\w+)*"
_WORK_TYPE_ITEM = rf"(?:{_WORK_TYPE}|{ARRANGEMENT_WORD})"
# "Site-by-design roles may be eligible for hybrid" speaks for a type of the
# employer's roles, and none of its words, remote included, says this role's type.
_TYPED_ROLES = re.compile(
    rf"\b(?:{_WORK_TYPE_ITEM}{_EITHER})*{_WORK_TYPE}(?:{_EITHER}{_WORK_TYPE_ITEM})*"
    r"\s+(?:roles|positions|jobs)\b",
    re.IGNORECASE,
)
_AWAY_WORD = re.compile(rf"\b{AWAY}\b", re.IGNORECASE)
# A heading states an arrangement only as a label: its last word, in parentheses, or
# after a dash or comma. "Browse Hybrid Jobs" names something else.
_HEADING_ARRANGEMENT = re.compile(
    rf"(?:\(\s*|-\s*|,\s*){ARRANGEMENT_WORD}\b|\b{ARRANGEMENT_WORD}\W*$", re.IGNORECASE
)
_CANDIDATE_COMMITMENT = re.compile(
    r"^\W*(?:(?:the|a|an)\s+)?(?:(?:successful|ideal)\s+)?(?:candidates?|incumbents?)\s+"
    r"(?:will|must|shall|needs?\s+to|(?:is|are)\s+(?:required|expected)\s+to)\b",
    re.IGNORECASE,
)


class Rewritten(str):
    """An arrangement line with words removed, remembering the sentence it was read from."""

    source: str

    def __new__(cls, text: str, source: str) -> Rewritten:
        """Return the rewritten text carrying its source sentence."""
        line = super().__new__(cls, text)
        line.source = source
        return line


def work_arrangement_lines(blocks: tuple[Block, ...]) -> tuple[str, ...]:
    """Remove hiring logistics while retaining arrangement text that precedes them."""
    retained: list[str] = []
    before = takewhile(
        lambda block: _first_marker(block.heading.casefold(), _INTERVIEW_SECTION_HEADINGS) is None,
        blocks,
    )
    lines = (
        (
            line,
            _OFFER_HEADING.search(block.heading) is not None,
            block.kind is SectionKind.PREFERRED,
        )
        for block in before
        for line in (_labelled_heading(block.heading), *block.lines)
        if line
    )
    for line, offered, preferred in lines:
        for sentence in _tags_with_what_follows(sentence_pieces(line)):
            candidate, interview_section_started = _arrangement_sentence(
                sentence, offered=offered, preferred=preferred
            )
            if candidate.strip():
                retained.append(
                    candidate if candidate == sentence else Rewritten(candidate, sentence)
                )
            if interview_section_started:
                return tuple(retained)
    return tuple(retained)


def _labelled_heading(heading: str) -> str:
    """Return a heading that labels an arrangement, as "Raleigh, NC (Hybrid)" does, else nothing."""
    return heading if _HEADING_ARRANGEMENT.search(heading) else ""


def _tags_with_what_follows(pieces: list[str]) -> list[str]:
    """Rejoin a recruiter tag run into the sentence after it, which decides what it says.

    "#LI-Onsite Benefits include a 401(k)" is one benefits sentence, however it is cut.
    """
    joined: list[str] = []
    for piece in pieces:
        if joined and not RECRUITER_TAG.sub("", joined[-1]).strip():
            joined[-1] = f"{joined[-1]} {piece}"
        else:
            joined.append(piece)
    return joined


def _arrangement_sentence(
    sentence: str, *, offered: bool = False, preferred: bool = False
) -> tuple[str, bool]:
    folded = sentence.casefold()
    section_start = _first_marker(folded, _INTERVIEW_SECTION_HEADINGS)
    if section_start is not None:
        return sentence[:section_start], True
    # An office mention in an interview sentence is where the interview happens,
    # not where the job does, so it carries no arrangement.
    if says(sentence, _INTERVIEW_WORDS) and (
        OFFICE_PRESENCE.search(sentence) or re.search(r"\bin-person\b", sentence, re.IGNORECASE)
    ):
        return "", False
    if says(sentence, _HIRING_ACTORS) and says(sentence, _HIRING_PROCESSES):
        actor_start = _first_marker(folded, _HIRING_ACTORS)
        if actor_start is not None:
            return sentence[:actor_start], False
    kept = _NOT_AN_ARRANGEMENT.sub("", _FORMAT_LIST.sub("", _without_attributes(sentence)))
    sentence = ";".join(_without_either_way(_placed_clause(clause)) for clause in kept.split(";"))
    subject = ROLE_SUBJECT.search(sentence)
    # A preferred block's cadence is a wish unless the sentence commits the role.
    if (
        preferred
        and not says(unwaived(sentence), MUST_MARKERS)
        and not (subject and not says(subject.group(), ("candidate", "incumbent")))
        and _CANDIDATE_COMMITMENT.search(sentence) is None
        and LINKEDIN_ARRANGEMENT_TOKEN.search(sentence) is None
    ):
        sentence = _ATTENDANCE_WORD.sub("", sentence)
    # A sentence that states this role's own attendance keeps every word of it.
    if _states_role_attendance(sentence):
        return sentence, False
    if not says(unwaived(sentence), MUST_MARKERS):
        sentence = _without_unbinding_attendance(sentence)
    # A site somebody travels to, or a customer's site, is not where the role is based.
    if says(sentence, _ELSEWHERE_WORDS):
        return OFFICE_PRESENCE.sub("", sentence), False
    # An arrangement offered among perks is an option, not a requirement. A recruiter
    # tag closing a perks list, or a perk placing the work at a named office, still
    # states the role's attendance.
    if says(sentence, OFFERED_WORDS) or (
        offered
        and LINKEDIN_ARRANGEMENT_TOKEN.search(sentence) is None
        and not _placed_at_named_office(sentence)
    ):
        return _ATTENDANCE_WORD.sub("", _without_either_way(sentence)), False
    if not ROLE_SUBJECT.search(sentence):
        sentence = _ANOTHER_PARTY_ON_SITE.sub("", sentence)
    return _without_either_way(sentence), False


def _without_unbinding_attendance(sentence: str) -> str:
    """Drop the attendance words an employer's belief, an opening wish, or a wishing phrase states.

    "Onsite in Springfield, local candidates preferred" keeps the onsite its own phrase states.
    """
    if LINKEDIN_ARRANGEMENT_TOKEN.search(sentence):
        return sentence
    if WISH_OPENER.match(sentence):
        return _ATTENDANCE_WORD.sub("", sentence)
    if _INVITED.search(sentence):
        sentence = _RELOCATION.sub("", sentence)
    # A belief beside work placed at a named office still places it; a wish there still wishes.
    if not _placed_at_named_office(sentence):
        set_off = [found.span() for found in re.finditer(rf":|{_SET_OFF.pattern}", sentence)]
        sentence = _rewrite_phrases(
            sentence, sorted([*phrase_breaks(sentence), *set_off]), _without_believed
        )
    return _rewrite_phrases(sentence, phrase_breaks(sentence), _without_wished)


def _rewrite_phrases(
    sentence: str, breaks: list[tuple[int, int]], rewrite: Callable[[str], str]
) -> str:
    """Apply ``rewrite`` to each phrase between ``breaks``, keeping the breaks themselves."""
    kept: list[str] = []
    start = 0
    for begin, end in [*breaks, (len(sentence), len(sentence))]:
        kept.append(rewrite(sentence[start : max(begin, start)]))
        kept.append(sentence[max(begin, start) : max(end, start)])
        start = max(end, start)
    return "".join(kept)


def _without_believed(phrase: str) -> str:
    return _ATTENDANCE_WORD.sub("", phrase) if _EMPLOYER_BELIEF.search(phrase) else phrase


def _without_wished(phrase: str) -> str:
    """Drop the attendance words a wish governs, those no split separates from it."""
    wish = _WISHED.search(phrase)
    if wish is None:
        return phrase

    # "Preferred - ability to work onsite" labels what follows; "PCR preferred - onsite" does not.
    labels = re.search(r"\w", phrase[: wish.start()]) is None

    def drop(match: re.Match[str]) -> str:
        if match.start() < wish.start():
            split = _WISH_SPLIT.search(phrase, match.end(), wish.start())
            # "On-site (preferred)": a set-off holding only the wish qualifies what precedes it.
            if (
                split
                and _SET_OFF.fullmatch(split.group())
                and not re.search(r"\w", phrase[split.end() : wish.start()])
            ):
                split = None
        else:
            split = None if labels else _SET_OFF.search(phrase, wish.end(), match.start())
        return match.group() if split else ""

    return _ATTENDANCE_WORD.sub(drop, phrase)


def _placed_at_named_office(sentence: str) -> bool:
    """Say whether one list item names both an attendance word and an office by its place."""
    return any(
        _NAMED_OFFICE.search(item) and _ATTENDANCE_WORD.search(item)
        for item in _LIST_SYMBOLS.split(sentence)
    )


def _without_unstated(
    sentence: str, pattern: re.Pattern[str], *, after_labels: bool = False
) -> str:
    """Drop each match of ``pattern`` that no role subject or label precedes.

    ``after_labels`` also keeps a match opening a fragment or a label's value, or one
    whose role subject opens after an inline label, as "Accountabilities: You will" does.
    """

    def drop(match: re.Match[str]) -> str:
        before = sentence[: match.start()]
        stated = bool(ROLE_SUBJECT.search(before) or ARRANGEMENT_LABEL.search(before))
        if after_labels and not stated:
            stated = bool(_FRAGMENT_OR_LABEL_END.search(before)) or any(
                ROLE_SUBJECT.search(part) for part in re.split(r":\s*", before)
            )
        return match.group() if stated else ""

    return pattern.sub(drop, sentence)


def _without_attributes(sentence: str) -> str:
    """Drop a "hybrid" method list, and in a sentence that places no role, on-site operations."""
    sentence = _without_unstated(sentence, _HYBRID_LED_METHODS)
    if _PLACES_ROLE.search(sentence) or _states_role_attendance(sentence):
        return sentence
    return _without_unstated(sentence, _ON_SITE_OPERATIONS, after_labels=True)


def _placed_clause(clause: str) -> str:
    """Return a clause without the arrangement words it states for other roles."""
    bindings = role_policy_bindings(clause)
    bound = bool(bindings)
    if _TYPED_ROLES.search(clause) and not (THIS_ROLE.search(clause) or bound):
        return ""
    hedged = HEDGED_ROLES.search(clause) is not None
    if (hedged and not bound) or _refers_elsewhere(clause):
        return _ATTENDANCE_WORD.sub("", clause)
    if hedged and bound:
        for inclusion in reversed(bindings):
            if inclusion.group("inclusion") is None:
                continue
            before = clause[: inclusion.start()].rstrip()
            after = clause[inclusion.end() :].lstrip()
            if before.endswith((",", "(", "-")):
                before = before[:-1].rstrip()
            if after.startswith((",", ")", "-")):
                after = after[1:].lstrip()
            clause = f"{before} {after}".strip()
    return clause


def _refers_elsewhere(clause: str) -> bool:
    return all(
        pattern.search(clause) for pattern in (_REFERRAL_ACTOR, _REFERRAL_ASK, _REFERRED_TOPIC)
    )


def _without_either_way(sentence: str) -> str:
    """Drop workplace lists and explicit home-or-office choices."""
    if (
        _WORKPLACE_CHOICE.search(sentence)
        and IN_PLACE_WORD.search(sentence)
        and _AWAY_WORD.search(sentence)
        and not says(unwaived(sentence), MUST_MARKERS)
        and office_days(sentence) is None
    ):
        return _ATTENDANCE_WORD.sub("", sentence)

    def drop(match: re.Match[str]) -> str:
        text = match.group()
        either = IN_PLACE_WORD.search(text) and _AWAY_WORD.search(text)
        return "" if either or text[:3].lower() == "not" else text

    return _PLACE_LIST.sub(drop, sentence)


def _states_role_attendance(sentence: str) -> bool:
    return (
        attendance_statement(sentence) is not None
        or office_days(sentence) is not None
        or any(
            pattern.search(sentence) is not None
            for pattern in (HYBRID_ROLE, ROLE_ARRANGEMENT_REFERENCE, _WEEKDAY)
        )
    )


def _first_marker(text: str, markers: tuple[str, ...]) -> int | None:
    starts = [text.find(marker) for marker in markers]
    present = [start for start in starts if start >= 0]
    return min(present) if present else None
