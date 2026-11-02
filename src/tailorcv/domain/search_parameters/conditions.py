"""Match a posting's stated requirements against configured hard-exclusion conditions.

An excluded condition is a free-text "Requires X" the person cannot satisfy. A
statement (a must-have requirement line, or a blocking hard gate the model read)
refuses the person only when it names the condition's credential and offers no
alternative the condition never named, which the person might hold. Anything
short of that abstains rather than refusing, because dropping a role the person
qualifies for is worse than screening one extra role by hand.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tailorcv.domain.requirements import PREFERRED_MARKERS, WAIVED
from tailorcv.domain.taxonomy import says

_TOKEN = re.compile(r"[a-z0-9]+")
# Words that carry no credential meaning, dropped before comparing a condition to a
# statement so a shared "requires" or "the" never counts as agreement.
_NOISE = frozenset(
    {
        "a",
        "an",
        "the",
        "or",
        "and",
        "of",
        "as",
        "in",
        "to",
        "for",
        "with",
        "that",
        "this",
        "is",
        "be",
        "requires",
        "require",
        "required",
        "requiring",
        "must",
        "have",
        "hold",
        "holding",
        "core",
        "central",
        "work",
        "stack",
        "role",
        "you",
        "your",
        "we",
        "our",
        "including",
        "such",
        "any",
        "active",
    }
)
# Credential-class stems: a match needs one of these so incidental overlap on a
# domain word (a "project management" requirement, say) is not read as a credential
# gate. Kept generic; the specific credential a person lacks lives in the condition.
_CREDENTIAL = frozenset({"licen", "licence", "regis", "certi", "degre"})
# "Partner with licensed clinicians" describes the people the role works beside.
_OTHERS_LICENSED = re.compile(r"\blicen[cs]ed\s+[a-z]+(?:ians|ists|ers|als|ors|ses)\b")
# A credential written as an abbreviation ("MD", "PharmD"), read case-sensitively so
# the verb "do" never matches a condition's "DO".
_ABBREVIATION = re.compile(r"\b[A-Z][A-Za-z]*[A-Z]\b")
_DOTTED = re.compile(r"\b(?:[A-Z][a-z]?\.){2,}")
# "MD and PhD scientists" or "Physician (MD) teams" names the people the role works
# beside: an abbreviation run, then a plural noun at most one word later.
_OTHERS_ABBREVIATED = re.compile(
    r"\b[A-Z][A-Za-z]*[A-Z](?:\s*(?:/|,|&|\band\b|\bor\b)\s*[A-Z][A-Za-z]*[A-Z])*\)?"
    r"\s+(?:[a-z]+\s+)?[A-Za-z]{2,}[a-rt-z]s\b"
)
_CLAUSE_TOKEN = re.compile(r"[A-Za-z0-9]+|[/,]")
# A clause opening on "or" continues the alternatives before it: "MD or DO; or PharmD".
_CLAUSE_BREAK = re.compile(r";(?!\s*or\b)|(?<=[.!?])\s+(?!or\b)", re.IGNORECASE)
_SEPARATORS = frozenset({"/", ",", "or"})
_WISHES = (*PREFERRED_MARKERS, "preferably", "desired", "advantage", "asset")
_NEED_WAIVED = re.compile(r"(?:\bnot|\bnever|n't)\s+need\b", re.IGNORECASE)
# Words that may open a clause before the credential it asks for: "An MD", "You are a PA".
_OPENERS = frozenset({"a", "an", "the", "you", "are"})
# Physician nouns a name may gloss as the candidate's own credential: "Oncologist (MD)".
_PHYSICIAN_NOUNS = frozenset(
    {
        "physician",
        "surgeon",
        "internist",
        "hospitalist",
        "intensivist",
        "pediatrician",
        "paediatrician",
        "obstetrician",
        "geriatrician",
        "oncologist",
        "cardiologist",
        "radiologist",
        "psychiatrist",
        "neurologist",
        "pathologist",
        "dermatologist",
        "anesthesiologist",
        "anaesthesiologist",
        "endocrinologist",
        "gastroenterologist",
        "gynecologist",
        "gynaecologist",
        "hematologist",
        "haematologist",
        "nephrologist",
        "neonatologist",
        "ophthalmologist",
        "pulmonologist",
        "rheumatologist",
        "urologist",
    }
)
# Clinical specialties and settings written as abbreviations, which no candidate holds.
_SPECIALTIES = frozenset(
    {
        *("ENT", "GI", "GU", "OB", "GYN", "OBGYN", "ONC", "HEME", "HIV"),
        *("ICU", "NICU", "PICU", "CCU", "ER", "ED", "EM", "IM", "FM"),
    }
)
# A preposition before a name run puts it in a place or a colleague: "with physicians (MD)".
_PREPOSITIONS = frozenset(
    {"in", "at", "of", "to", "from", "by", "on", "near", "with", "for", "alongside"}
)
_CREDENTIAL_ADJECTIVES = (
    r"(?:(?:state|licen[cs]ed|registered|certified)\s*(?:/|\bor\b|\band\b)?\s*)*"
)
# Spelled forms of an abbreviation a condition may name, with the context word a
# statement must carry; leading credential adjectives are absorbed into the name.
_SPELLED_OUT = {
    "CPhT": (
        re.compile(rf"\b{_CREDENTIAL_ADJECTIVES}(?:pharmacy\s+)?technician\b", re.IGNORECASE),
        "pharm",
    ),
}
# A singular technician is the credential holder; plural technicians are colleagues.
_TECHNICIAN = re.compile(r"\btechnician\b", re.IGNORECASE)


def _stem(token: str) -> str:
    """Fold license/licence/licensure and registered/registration onto one stem."""
    return token[:5] if len(token) > 5 else token


def _significant(text: str) -> set[str]:
    return {_stem(t) for t in _TOKEN.findall(text.casefold()) if t not in _NOISE}


def _undotted(text: str) -> str:
    """Write "M.D." and "Ph.D." as "MD" and "PhD"."""
    return _DOTTED.sub(lambda match: match.group().replace(".", ""), text)


def _used_as_credential(words: list[str], names: frozenset[str], cues: set[str]) -> bool:
    """Whether a lone name opens the clause or sits beside a credential word.

    "MD required", "An MD with", "Oncologist (MD) with", "Licensed PE"; not "in the US"
    or "PE-backed", where the same letters name a place or a market.
    """
    for index, word in enumerate(words):
        if word not in names:
            continue
        glossed = index > 0 and words[index - 1].casefold() in _PHYSICIAN_NOUNS
        opening = words[: index - 1] if glossed else words[:index]
        if all(before.casefold() in _OPENERS for before in opening):
            return True
        beside = words[max(index - 1, 0) : index] + words[index + 1 : index + 2]
        if any(_stem(other.casefold()) in cues for other in beside):
            return True
    return False


def _abbreviated(statement: str, names: frozenset[str]) -> str:
    """Write a spelled-out credential the condition names by abbreviation as that name."""
    for name, (spelled, context) in _SPELLED_OUT.items():
        if name in names and context in statement.casefold():
            statement = spelled.sub(name, statement)
    return statement


def _after_preposition(words: Iterable[str]) -> bool:
    """Whether a preposition among the words puts what follows in a place or a colleague."""
    return not _PREPOSITIONS.isdisjoint(word.casefold() for word in words)


def _neighbours(tokens: list[str], index: int) -> tuple[str | None, str | None]:
    """The nearest non-separator tokens before and after a separator."""
    before = next((t for t in reversed(tokens[:index]) if t.casefold() not in _SEPARATORS), None)
    after = next((t for t in tokens[index + 1 :] if t.casefold() not in _SEPARATORS), None)
    return before, after


def _ends_run(tokens: list[str], index: int) -> bool:
    """Whether a comma after a name is followed by prose rather than another alternative."""
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if following is None or following.casefold() in _SEPARATORS:
        return False
    return _ABBREVIATION.fullmatch(following) is None


def _in_later_prose(tokens: list[str], index: int, names: frozenset[str]) -> bool:
    """Whether a separator sits in prose after the last name.

    Prose is an "and" conjunct, or a phrase that a comma ending the names opens and a
    qualifying preposition ties to them: "MD or DO, specializing in Cardiology or
    Surgery". A separator after a comma or before "equivalent" may offer an
    alternative, and so may one before an abbreviation outside such a phrase.
    """
    after = _neighbours(tokens, index)[1]
    if (after is not None and after.casefold() == "equivalent") or _offers_degree(tokens, index):
        return False
    abbreviated = after is not None and _ABBREVIATION.fullmatch(after) is not None
    conjunct = attached = False
    for position in range(index - 1, -1, -1):
        token = tokens[position]
        if token in names:
            ended = tokens[position + 1] == "," and _ends_run(tokens, position + 1)
            qualified = ended and attached
            return tokens[index - 1] != "," and (qualified or (conjunct and not abbreviated))
        following = tokens[position + 1]
        conjunct = conjunct or (token.casefold() == "and" and following != "/")
        attached = attached or _qualifies(tokens, position)
    return False


def _offers_degree(tokens: list[str], index: int) -> bool:
    """Whether the abbreviation after a separator reads as a credential on offer.

    Any abbreviation does but a listed specialty ("OB/GYN", "ENT or GI"), and that one
    too when a credential word follows it.
    """
    at = next(
        (p for p in range(index + 1, len(tokens)) if tokens[p].casefold() not in _SEPARATORS),
        None,
    )
    if at is None or _ABBREVIATION.fullmatch(tokens[at]) is None:
        return False
    following = tokens[at + 1].casefold() if at + 1 < len(tokens) else ""
    return tokens[at] not in _SPECIALTIES or _stem(following) in _CREDENTIAL


def _qualifies(tokens: list[str], position: int) -> bool:
    """Whether a preposition opens a qualifier ("specializing in", ", with"), not a credential."""
    before = tokens[position - 1].casefold() if position else ""
    is_preposition = tokens[position].casefold() in _PREPOSITIONS
    return is_preposition and (before == "," or before.endswith(("ing", "ed")))


def _leaves_names(
    tokens: list[str], index: int, names: frozenset[str], named: tuple[bool, bool], joined: bool
) -> bool:
    """Whether a separator that joins no two names leaves the names' requirement alone."""
    comma = tokens[index] == ","
    if not any(named):
        return comma or _in_later_prose(tokens, index, names)
    if not (comma and joined and named[0] and _ends_run(tokens, index)):
        return False
    # "with physicians (MD or DO), pharmacists and nurses" lists colleagues.
    first = next(position for position, token in enumerate(tokens) if token in names)
    return not _after_preposition(tokens[:first])


def _separators_join_names(tokens: list[str], names: frozenset[str]) -> bool | None:
    """Whether a clause's separators join names: None when none bears on a name.

    A separator beside a name must join it to another name, unless it is a comma
    ending a joined run before prose; any other offers an unnamed alternative.
    """
    joined = None
    for index, token in enumerate(tokens):
        if token.casefold() not in _SEPARATORS:
            continue
        before, after = _neighbours(tokens, index)
        named = (before in names, after in names)
        if all(named):
            joined = True
        elif not _leaves_names(tokens, index, names, named, bool(joined)):
            return False
    return joined


def _clause_requires_named(clause: str, names: frozenset[str], cues: set[str]) -> bool:
    """Whether one clause requires a named abbreviation and offers no unnamed alternative.

    A waiver or a wish in the clause keeps it from requiring anything.
    """
    tokens = _CLAUSE_TOKEN.findall(clause)
    if not names.intersection(tokens) or WAIVED.search(clause) or _NEED_WAIVED.search(clause):
        return False
    if says(clause, _WISHES):
        return False
    joined = _separators_join_names(tokens, names)
    if joined is not None:
        return joined
    # In upper-case text a lone "DO" or "MD" may be an ordinary word.
    words = [t for t in tokens if t.casefold() not in _SEPARATORS]
    has_lower = any(character.islower() for character in clause)
    return has_lower and _used_as_credential(words, names, cues)


def _refuses(condition: str, statement: str) -> bool:
    """Whether one must-have statement requires a credential the condition rules out.

    A clause refuses when it uses one of the condition's abbreviated credentials as a
    credential, with no waiver, wish, or alternative the condition never names.
    Otherwise any disjunction abstains, since a prose branch cannot be reliably told
    excluded or satisfiable, and a single requirement refuses when it names the
    condition's credential class.
    """
    terms = _significant(condition)
    if not (terms & _CREDENTIAL):
        return False
    names = frozenset(_ABBREVIATION.findall(_undotted(condition)))
    worded = terms - _significant(" ".join(names))
    own = _OTHERS_ABBREVIATED.sub(" ", _abbreviated(_undotted(statement), names))
    cues = worded | _CREDENTIAL | {"requi"}
    if any(_clause_requires_named(c, names, cues) for c in _CLAUSE_BREAK.split(own)):
        return True
    folded = statement.casefold()
    if " or " in folded or "/" in statement:
        return False
    if _TECHNICIAN.search(statement) and "techn" not in worded:
        return False
    said = _significant(_OTHERS_LICENSED.sub(" ", folded))
    shared = worded & said
    # "Registered" and "licence" alone fit every licensed profession, so the
    # condition's own profession must appear too ("nurse" as "nursing").
    profession = {term[:4] for term in terms - _CREDENTIAL}
    names_it = any(term[:4] in profession for term in said - _CREDENTIAL)
    return len(shared) >= 2 and bool(shared & _CREDENTIAL) and names_it


def excluded_by(
    conditions: Iterable[tuple[str, str]], statements: Iterable[str]
) -> tuple[str, str] | None:
    """Return the first condition a statement refuses, or None to abstain.

    Only credential/licensure conditions are decided here; residence, arrangement,
    and capability conditions are owned by their own gates and the model's reading.
    """
    materialized = tuple(statements)
    for condition, reason in conditions:
        if any(_refuses(condition, statement) for statement in materialized):
            return condition, reason
    return None
