"""Read the role's work arrangement and location from the posting."""

from __future__ import annotations

import re
from dataclasses import replace

from clearscout.application.tailoring.fit.facts.arrangement_lines import Rewritten
from clearscout.application.tailoring.fit.facts.arrangement_vocabulary import (
    ANY_PLACE,
    ARRANGEMENT_LABEL,
    ARRANGEMENT_WORD,
    IN_PLACE_WORD,
    OFFERED_WORDS,
    ROLE_ARRANGEMENT_REFERENCE,
    ROLE_SUBJECT,
)
from clearscout.application.tailoring.fit.lexicon import (
    ARRANGEMENT_EVIDENCE,
    ATTENDANCE_IN_PLACE_LINE,
    BARE_REMOTE_PLACE,
    FULLY_REMOTE,
    HYBRID_ROLE,
    INFREQUENT_ATTENDANCE,
    LINKEDIN_ARRANGEMENT_PATTERNS,
    LINKEDIN_ARRANGEMENT_TOKEN,
    LOCATION_LABEL,
    OFFICE_PRESENCE,
    PLACE_WORD,
    PLACE_WORDS,
    REMOTE,
    REMOTE_PLACE_NAME,
    WEEKLY_SHARE,
    office_days,
    refuses_remote,
)
from clearscout.application.tailoring.fit.structure import (
    Answer,
    first_saying,
    quote_line,
    unshortened,
)
from clearscout.domain.arrangement_grammar import (
    attendance_is_hedged,
    attendance_statement,
    remote_offer_statement,
)
from clearscout.domain.countries import (
    DEFAULT_NAMES,
    subdivision_names,
)
from clearscout.domain.taxonomy import says
from clearscout.shared.text import sentence_pieces

# "..., or remotely within Canada" offers remote work beside an office arrangement;
# "visits performed on-site or remotely" only says how a task is done.
_REMOTE_ALTERNATIVE = re.compile(
    r"(?<!on-site\s)(?<!on\ssite\s)(?<!onsite\s)(?<!in-person\s)(?<!in\sperson\s)(?<!in-office\s)"
    r"(?:\bor\s+(?:work(?:ing)?\s+)?remotely\b"
    r"|\bor\s+(?:fully\s+)?remote\s+(?:within|from|in|anywhere)\b)",
    re.IGNORECASE,
)
_ARRANGEMENT_SPOT = re.compile(rf"\b(?:{ANY_PLACE}|relocate\s+to)\b", re.IGNORECASE)
# A title naming remote as its arrangement: "Analyst (Remote, US)", "Analyst - Remote".
# A word straight after it names a field instead: "- Remote Sensing".
_TITLE_REMOTE = re.compile(
    r"(?:[(\[]\s*|\s[-|\u2013\u2014]\s*)remote"
    r"(?=\s*(?:[()\[\],/|]|[-\u2013\u2014]\s|\.?\s*$))",
    re.IGNORECASE,
)
# "If this role is remote, ..." and a "For remote roles:" label state what follows
# from an arrangement, not one, through the end of their sentence.
_ROLE_ARRANGEMENT_CONDITION = re.compile(
    rf"\b(?:if|when|where|should|unless)\s+{ROLE_ARRANGEMENT_REFERENCE.pattern}\s+(?:is|be)\s+"
    rf"(?:\w+\s+)?(?:{ARRANGEMENT_WORD}|virtual)\b"
    rf"|\bfor\s+(?:\w+\s+)?{ARRANGEMENT_WORD}\s+(?:roles|positions|jobs)\s*:",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r";|,\s+(?:and|but|so|while|though|although|whereas|yet|with|who|which)\s+", re.IGNORECASE
)
_SUBDIVISION_NAMES = "|".join(
    sorted(
        {re.escape(name) for country in DEFAULT_NAMES for name in subdivision_names(country)},
        key=len,
        reverse=True,
    )
)
# Only a role-led remote sentence whose condition excludes local residents (unless you
# live in, if you live outside) offers it; a rank, a trip or a tenure is no place, and
# "but within" a place smaller than a country bounds the condition to locals.
_ROLE_LED = re.compile(
    r"^\W*(?:(?:this|the)\s+)?(?:role|position|job)\b.*\b(?:"
    r"unless\s+(?:you|the\s+candidate)\s+(?:live|lives|reside|resides|are|is)\s+(?:with)?in"
    r"|if\s+(?:you|the\s+candidate)\s+(?:live|lives|reside|resides|are|is)\s+"
    r"(?:outside|beyond|more\s+than|away))\b"
    r"(?!\s+(?:an?\s+(?:[\w-]+\s+){0,2}(?:position|role)\b|(?:on\s+)?business\s+travel\b"
    r"|\S+\s+years?\s+into\b|an?\s+state\s+where\s+we(?:\s+are|'re|\s+do)\s+not\b))"
    r"(?![^.;]*\bbut\s+(?:still\s+)?(?:with)?in\s+(?:the\s+state\s+of\b"
    rf"|the\s+[\w.' -]{{1,40}}?\barea\b|(?:{_SUBDIVISION_NAMES})\b))",
    re.IGNORECASE,
)
# The role itself declared remote, in either word order, wherever the line sits. A
# sentence that is only "Work Anywhere" labels the role; a perk offering it does not.
_ROLE_DECLARED_REMOTE = re.compile(
    r"\b(?:this|the) (?:role|position|job) (?:is|will be) "
    r"(?:fully |entirely |permanently )?remote\b"
    r"|\bthis is an? (?:fully |entirely |permanently )?remote (?:role|position|job)\b"
    r"|\b(?:this|the)\s+(?:role|position|job)\s+(?:enables?|allows?|permits?)\s+"
    r"(?:associates|employees|you)\s+to\s+work\s+virtually\s+full[- ]time\b"
    r"|\b(?:this|the)\s+(?:role|position|job)\s+(?:is|will\s+be)\s+"
    r"(?:virtual\s+full[- ]time|full[- ]time\s+virtual)\b"
    r"|\bthis\s+is\s+an?\s+full[- ]time\s+virtual\s+(?:role|position|job)\b"
    r"|^\W*work (?:from )?anywhere\W*$",
    re.IGNORECASE,
)


def _saying_arrangement(
    lines: tuple[str, ...], name: str, phrases: tuple[str, ...]
) -> tuple[str, str] | None:
    """Return the first line naming an arrangement; a line refusing remote names none."""
    if name != "remote":
        return first_saying(lines, phrases)
    lines = tuple(line for line in lines if not refuses_remote(line))
    if found := first_saying(lines, phrases):
        return found
    declared = next(
        (line for line in lines if _ROLE_DECLARED_REMOTE.search(_unconditional(line))), None
    )
    if declared is not None:
        return "anywhere", declared
    if _committed_attendance(lines) is not None or any(
        attendance_statement(line) is not None for line in lines
    ):
        return None
    offers = (remote_offer_statement(line) for line in lines)
    return next(((offer, offer) for offer in offers if offer and _ROLE_LED.match(offer)), None)


def _rare_attendance(lines: tuple[str, ...]) -> str | None:
    """Return the line stating office attendance less often than weekly."""
    for line in lines:
        if OFFICE_PRESENCE.search(line) is None:
            continue
        if office_days(line) is not None or WEEKLY_SHARE.search(line) is not None:
            continue
        if INFREQUENT_ATTENDANCE.search(line) is not None:
            return line
    return None


def _weekly_share(lines: tuple[str, ...]) -> str | None:
    """Return the line stating part of the working week in the office."""
    for line in lines:
        if OFFICE_PRESENCE.search(line) is None:
            continue
        if (found := WEEKLY_SHARE.search(line)) is not None and int(found.group(1)) < 100:
            return line
    return None


def _role_specific_arrangements(lines: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Return explicit working-arrangement statements that apply to this role."""
    role_lines = tuple(
        line for line in map(_unconditional, lines) if ROLE_ARRANGEMENT_REFERENCE.search(line)
    )
    return {
        name: found
        for name, phrases in ARRANGEMENT_EVIDENCE.items()
        if (found := _saying_arrangement(role_lines, name, phrases)) is not None
    }


def _unconditional(line: str) -> str:
    """Return a line without what it states conditional on this role's own arrangement."""
    if _ROLE_ARRANGEMENT_CONDITION.search(line) is None:
        return line
    return " ".join(_before_condition(sentence) for sentence in sentence_pieces(line))


def _before_condition(sentence: str) -> str:
    found = _ROLE_ARRANGEMENT_CONDITION.search(sentence)
    return sentence if found is None else sentence[: found.start()]


def _linkedin_arrangements(lines: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Return only exact LinkedIn arrangement tags, preserving their source line."""
    stated: dict[str, tuple[str, str]] = {}
    for name, pattern in LINKEDIN_ARRANGEMENT_PATTERNS.items():
        for line in lines:
            if found := pattern.search(line):
                stated[name] = found.group(), line
                break
    return stated


def _without_linkedin_tags(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Keep malformed LinkedIn-like tokens from becoming ordinary arrangement words."""
    return tuple(LINKEDIN_ARRANGEMENT_TOKEN.sub("", line) for line in lines)


def _remote_place(lines: tuple[str, ...]) -> str | None:
    """Return the first line listing places, one of them remote, or None."""
    for line in lines:
        if refuses_remote(line) or ATTENDANCE_IN_PLACE_LINE.search(line):
            continue
        parts = [part.strip() for part in re.split(r"[,;|]", LOCATION_LABEL.sub("", line))]
        if not all(len(PLACE_WORD.findall(part)) <= PLACE_WORDS for part in parts):
            continue
        listed = len(parts) > 1
        if any(
            REMOTE_PLACE_NAME.search(part) or (listed and BARE_REMOTE_PLACE.search(part))
            for part in parts
        ):
            return line
    return None


def _labelled_remote(lines: tuple[str, ...], title: str) -> str | None:
    """Return a title or a label line that states remote outright, or None."""
    title_remote = _TITLE_REMOTE.search(title) and not IN_PLACE_WORD.search(title)
    if title_remote and not refuses_remote(title):
        return title
    for line in lines:
        rest = LOCATION_LABEL.sub("", line).strip()
        if label := ARRANGEMENT_LABEL.match(rest):
            rest = rest[label.end() :]
        if BARE_REMOTE_PLACE.match(rest):
            return line
    return None


def _linkedin_location(lines: tuple[str, ...], question: str, title: str) -> Answer[str] | None:
    """Read one uncontradicted LinkedIn tag without overriding this-role wording."""
    linkedin_stated = _linkedin_arrangements(lines)
    if not linkedin_stated:
        return None
    if len(linkedin_stated) > 1:
        evidence = tuple(
            quote_line(found[1], found[0]) for _, found in sorted(linkedin_stated.items())
        )
        return Answer(
            question,
            evidence=evidence,
            note="the posting gives conflicting LinkedIn arrangement tags",
            conflict=True,
        )
    name, tag = next(iter(linkedin_stated.items()))
    untagged = _without_linkedin_tags(lines)
    role_stated = _role_specific_arrangements(untagged)
    if role_stated and (len(role_stated) != 1 or name not in role_stated):
        role_evidence = tuple(
            quote_line(found[1], found[0]) for _, found in sorted(role_stated.items())
        )
        return Answer(
            question,
            evidence=(quote_line(tag[1], tag[0]), *role_evidence),
            note="the LinkedIn arrangement tag conflicts with this role's stated arrangement",
            conflict=True,
        )
    if name != "remote" and (place := _remote_place(untagged)) is not None:
        return Answer(
            question,
            evidence=(quote_line(tag[1], tag[0]), quote_line(place)),
            note="the LinkedIn arrangement tag conflicts with a remote place the posting lists",
            conflict=True,
        )
    # Recruiters paste one tag into every posting, so a remote title or label outweighs it.
    if name != "remote" and (stated := _labelled_remote(untagged, title)) is not None:
        return Answer(
            question,
            evidence=(quote_line(tag[1], tag[0]), quote_line(stated)),
            note="the LinkedIn arrangement tag conflicts with the remote arrangement the "
            "posting states",
            conflict=True,
        )
    return Answer(question, name, evidence=(quote_line(tag[1], tag[0]),))


def read_location(lines: tuple[str, ...], title: str) -> Answer[str]:
    """Return where the work happens, quoting the posting's own sentences."""
    answer = _arrangement_reading(lines, title)
    return replace(answer, evidence=tuple(_source_quote(quote, lines) for quote in answer.evidence))


def _source_quote(quote: str, lines: tuple[str, ...]) -> str:
    """Return ``quote`` re-taken from the sentence a rewritten line came from."""
    core = unshortened(quote)
    if any(core in " ".join(getattr(line, "source", line).split()) for line in lines):
        return quote
    for line in lines:
        if isinstance(line, Rewritten) and core in " ".join(line.split()):
            word = _ARRANGEMENT_SPOT.search(core)
            return quote_line(line.source, word.group() if word else None)
    return quote


def _arrangement_reading(lines: tuple[str, ...], title: str) -> Answer[str]:
    question = "Where the work happens"
    if linkedin := _linkedin_location(lines, question, title):
        return linkedin
    lines = _without_linkedin_tags(lines)
    stated = _stated_arrangements(lines)
    if not stated:
        return Answer(question, note="the posting names no working arrangement")
    if specific := _specific_arrangement(question, lines, stated):
        return specific
    return _general_arrangement(question, lines, stated)


def _stated_arrangements(lines: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    return {
        name: found
        for name, phrases in ARRANGEMENT_EVIDENCE.items()
        if (found := _saying_arrangement(lines, name, phrases)) is not None
    }


def _specific_arrangement(
    question: str, lines: tuple[str, ...], stated: dict[str, tuple[str, str]]
) -> Answer[str] | None:
    remote = stated.get("remote")
    if remote is not None and _declared_remote(lines):
        if (committed := _committed_attendance(lines)) is not None:
            return _perk_conflict(question, remote[1], committed)
        return None
    # Attendance rarer than weekly is remote work with visits; a stated share of
    # the week in an office is hybrid however the posting words it.
    if (rare := _rare_attendance(lines)) is not None and stated.keys() & {"hybrid", "on site"}:
        return Answer(question, "remote", evidence=(quote_line(rare),))
    alternative = next(
        (line for line in lines if _REMOTE_ALTERNATIVE.search(line) and not refuses_remote(line)),
        None,
    )
    if alternative is not None:
        return Answer(question, "remote", evidence=(quote_line(alternative),))
    perk = _remote_named_as_perk(lines)
    if (share := _weekly_share(lines)) is not None:
        return _hybrid_unless_perk_conflict(question, share, perk)
    # A role stated as hybrid names a split week, so a bare "work from home" beside
    # it is that split's remote half, and an employer-wide "hybrid work model" is
    # not a role-level cadence.
    role_hybrid = next((line for line in lines if HYBRID_ROLE.search(line)), None)
    if role_hybrid is not None:
        return _hybrid_unless_perk_conflict(question, role_hybrid, perk)
    return None


def _clauses(line: str) -> tuple[str, ...]:
    return tuple(
        clause for sentence in sentence_pieces(line) for clause in _CLAUSE_BOUNDARY.split(sentence)
    )


def _declared_remote(lines: tuple[str, ...]) -> bool:
    offered = tuple(line for line in lines if not refuses_remote(line))
    return first_saying(offered, FULLY_REMOTE) is not None or any(
        _ROLE_DECLARED_REMOTE.search(_unconditional(line)) for line in offered
    )


def _remote_named_as_perk(lines: tuple[str, ...]) -> str | None:
    """Return a sentence naming remote among perks when the role is not declared remote."""
    if _declared_remote(lines):
        return None
    offering = (
        line
        for line in lines
        if not refuses_remote(line) and first_saying((line,), REMOTE) is not None
    )
    return next((line for line in offering if says(line, OFFERED_WORDS)), None)


def _perk_conflict(question: str, perk: str, attendance: str) -> Answer[str]:
    # Whose attendance a sentence states cannot be read reliably, and an unknown
    # never refuses a posting where a wrong arrangement would.
    return Answer(
        question,
        evidence=tuple(dict.fromkeys((quote_line(perk), quote_line(attendance)))),
        note="the posting offers remote work and also states office attendance",
        conflict=True,
    )


def _hybrid_unless_perk_conflict(question: str, sentence: str, perk: str | None) -> Answer[str]:
    about_this_role = any(ROLE_SUBJECT.match(clause) for clause in _clauses(sentence))
    if perk is not None and not about_this_role:
        return _perk_conflict(question, perk, sentence)
    return Answer(question, "hybrid", evidence=(quote_line(sentence),))


def _committed_attendance(lines: tuple[str, ...]) -> str | None:
    """Return the clause that appears to commit this role to attend, or None."""
    for clause in (clause for line in lines for clause in _clauses(line)):
        if not ROLE_SUBJECT.match(clause) or attendance_is_hedged(clause):
            continue
        if (found := attendance_statement(clause)) is not None:
            return found[1]
        if OFFICE_PRESENCE.search(clause) and office_days(clause) is not None:
            return clause
    return None


def _general_arrangement(
    question: str, lines: tuple[str, ...], stated: dict[str, tuple[str, str]]
) -> Answer[str]:
    remote = stated.get("remote")
    perk = _remote_named_as_perk(lines)
    if perk is not None and (committed := _committed_attendance(lines)) is not None:
        return _perk_conflict(question, perk, committed)
    # A posting offering remote work is offering it whatever else it names, and
    # the attendance it also names stays in the evidence.
    if remote is not None:
        attendance = tuple(
            quote_line(found[1], found[0])
            for name, found in sorted(stated.items())
            if name in {"hybrid", "on site"} and found[1] != remote[1]
        )
        return Answer(question, "remote", evidence=(quote_line(remote[1], remote[0]), *attendance))
    if hybrid := stated.get("hybrid"):
        return Answer(question, "hybrid", evidence=(quote_line(hybrid[1], hybrid[0]),))
    if len(stated) > 1:
        return Answer(
            question,
            note="the posting says both "
            + " and ".join(f"{name} ({found[0]!r})" for name, found in sorted(stated.items())),
            conflict=True,
        )
    name, found = next(iter(stated.items()))
    return Answer(question, name, evidence=(quote_line(found[1], found[0]),))
