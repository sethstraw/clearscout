"""Compare two job titles by the words that carry meaning, and filter on stated ones."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache

from tailorcv.domain.search_parameters import LEVELS
from tailorcv.domain.search_parameters.roles import TITLE_NOISE, title_word_sequence
from tailorcv.domain.taxonomy import FAMILIES

_RAW_WORD = re.compile(r"[a-z0-9]+")
# An unspaced hyphen stays inside a word ("Co-Lead"); only a spaced dash ends a clause.
_TITLE_CLAUSE_BREAK = re.compile(r"[,:|(]|\s[-\u2013\u2014]\s")

# More than two-thirds of a stated title's role words.
TITLE_OVERLAP = 2 / 3

# Tokens that name a distinct occupation. A candidate carrying one of these
# names a different role family than a wanted title that does not, even when
# generic role words like "manager" overlap.
ROLE_FAMILY_TOKENS = frozenset(
    {
        "marketing",
        "sales",
        "recruiting",
        "recruiter",
        "accounting",
        "finance",
        "legal",
        "manufacturing",
    }
)

_GENERIC_TITLE_NOUNS = frozenset(
    {
        "advisor",
        "analyst",
        "consultant",
        "coordinator",
        "director",
        "engineer",
        "engineering",
        "head",
        "lead",
        "manager",
        "officer",
        "president",
        "principal",
        "specialist",
        "vp",
    }
)

_OCCUPATION_NOUNS = _GENERIC_TITLE_NOUNS | {
    "architect",
    "associate",
    "actuary",
    "attorney",
    "developer",
    "executive",
    "fellow",
    "nurse",
    "pharmacist",
    "physician",
    "programmer",
    "researcher",
    "scientist",
    "steward",
    "technician",
    "technologist",
    "therapist",
    "writer",
}
_LEADERSHIP_OCCUPATION_NOUNS = frozenset(
    {"director", "head", "lead", "manager", "president", "principal", "vp"}
)
_SPECIFIC_OCCUPATION_NOUNS = _OCCUPATION_NOUNS - _LEADERSHIP_OCCUPATION_NOUNS

# Agent-noun endings name a person ("engineer", "hygienist"); "-ent" is
# excluded since it collides with the abstract ending "-ment" ("management"),
# so an agent noun ending in it ("president") stays in _OCCUPATION_NOUNS.
_AGENT_NOUN_SUFFIXES = ("ist", "ian", "ant", "or", "er")
_AGENT_NOUN_EXCLUSIONS = frozenset({"senior", "junior", "other", "center", "master"})
# Nominalized-function endings name an area or department, not a person
# ("management", "informatics", "administration"); a few common department
# words fit no such ending and stay a short explicit list.
_FUNCTION_HEAD_SUFFIXES = ("ment", "ing", "ics", "tion", "sion", "ance", "ence", "ity")
_FUNCTION_HEAD_WORDS = frozenset({"services", "affairs", "sales", "research", "support"})


def _is_role_noun(word: str) -> bool:
    """Return whether a raw title word names a person doing the work, in any field."""
    if word in _AGENT_NOUN_EXCLUSIONS:
        return False
    return word in _OCCUPATION_NOUNS or word.endswith(_AGENT_NOUN_SUFFIXES)


def _is_function_head(word: str) -> bool:
    """Return whether a raw title word names an area or department, in any field."""
    singular = word[:-1] if word.endswith("s") and not word.endswith("ss") else word
    return word in _FUNCTION_HEAD_WORDS or singular.endswith(_FUNCTION_HEAD_SUFFIXES)


_ADJACENT_ROLE_FAMILIES = frozenset(
    {
        frozenset(("medical_writing", "medical_affairs")),
        frozenset(("medical_writing", "medical_information")),
        frozenset(("clinical_evidence", "clinical_development")),
        frozenset(("medical_information", "medical_affairs")),
        frozenset(("medical_information", "regulatory_affairs")),
        frozenset(("medical_information", "pharmacovigilance")),
        frozenset(("medical_information", "life_science_data")),
        frozenset(("medical_affairs", "clinical_development")),
        frozenset(("medical_affairs", "market_access")),
        frozenset(("regulatory_affairs", "quality_gxp")),
        frozenset(("quality_gxp", "cmc_manufacturing")),
        frozenset(("life_science_data", "biostatistics")),
        frozenset(("life_science_data", "clinical_research")),
        frozenset(("life_science_data", "clinical_development")),
        frozenset(("life_science_data", "pharmacovigilance")),
    }
)


def title_words(title: str) -> frozenset[str]:
    """Return the words of a title that say something about the work."""
    return frozenset(title_word_sequence(title))


def title_contains_phrase(title: str, phrase: str) -> bool:
    """Return whether a normalized role phrase occurs contiguously in a title."""
    title_sequence = title_word_sequence(title)
    phrase_sequence = title_word_sequence(phrase)
    width = len(phrase_sequence)
    return bool(width) and any(
        title_sequence[index : index + width] == phrase_sequence
        for index in range(len(title_sequence) - width + 1)
    )


def role_words(title: str) -> frozenset[str]:
    """Return title words without the ladder rung scored elsewhere."""
    return title_words(title) - set(LEVELS)


def shares_a_stated_occupation_noun(found: str, wanted: str) -> bool:
    """Return whether both titles state one shared occupation word, never a leadership fallback."""
    return bool(role_words(found) & role_words(wanted) & _OCCUPATION_NOUNS)


def _leadership_rungs(words: tuple[str, ...]) -> frozenset[str]:
    """Return the words that state their own leadership rung, not a modifier.

    "Data Platform Lead" states a rung; "Principal Data Scientist" only borrows
    "principal" to modify "scientist", a person noun stated later, so it names none.
    """
    return frozenset(
        word
        for index, word in enumerate(words)
        if word in _LEADERSHIP_OCCUPATION_NOUNS
        and not any(_is_modified_person_noun(later) for later in words[index + 1 :])
    )


def _is_modified_person_noun(word: str) -> bool:
    """Return whether a word is a person noun, not a ladder word or an area like "engineering"."""
    return word in _SPECIFIC_OCCUPATION_NOUNS and word not in LEVELS and not _is_function_head(word)


def _clause_leadership_rungs(
    title: str, read_words: Callable[[str], tuple[str, ...]]
) -> frozenset[str]:
    """Return the rungs each clause states, so a person noun modifies only its own clause."""
    return frozenset[str]().union(
        *(_leadership_rungs(read_words(clause)) for clause in _TITLE_CLAUSE_BREAK.split(title))
    )


def _unmodified_leadership_rungs(title: str) -> frozenset[str]:
    """Return leadership rung words a title states as its own rung."""
    return _clause_leadership_rungs(title, title_word_sequence)


def shares_occupation_noun(found: str, wanted: str) -> bool:
    """Return whether two titles state the same or a parallel occupation noun.

    Any two leadership rungs that name their own rung agree, whatever domain word sits beside one.
    """
    found_leadership = _unmodified_leadership_rungs(found)
    wanted_leadership = _unmodified_leadership_rungs(wanted)
    return shares_a_stated_occupation_noun(found, wanted) or bool(
        found_leadership and wanted_leadership
    )


def _raw_title_words(title: str) -> tuple[str, ...]:
    """Return title words in published order, before any noun equivalence folds them."""
    return tuple(word for word in _RAW_WORD.findall(title.casefold()) if word not in TITLE_NOISE)


def names_a_bare_function(title: str) -> bool:
    """Return whether a title's raw words state no role noun and end on a function word."""
    raw = _raw_title_words(title)
    if not raw or any(_is_role_noun(word) for word in raw):
        return False
    return _is_function_head(raw[-1])


# Reads raw words, since the domain layer's management/manager equivalence
# would turn an area name such as "Talent Management" into a rung.
def names_a_leadership_rung(title: str) -> bool:
    """Return whether a title states a leadership rung as its own job."""
    return bool(_clause_leadership_rungs(title, _raw_title_words))


def distinct_shared_words(found: str, wanted: str) -> frozenset[str]:
    """Return words both titles state, minus generic nouns that are not domain evidence."""
    return (role_words(found) & role_words(wanted)) - _OCCUPATION_NOUNS


def states_a_specific_occupation(title: str) -> bool:
    """Return whether a title names a real occupation word, not only a leadership rung."""
    return bool(role_words(title) & _SPECIFIC_OCCUPATION_NOUNS)


def occupation_disagrees(found: str, wanted: str) -> bool:
    """Return whether ``found`` names a specific occupation ``wanted`` lacks; a rung never does."""
    return states_a_specific_occupation(found) and not shares_occupation_noun(found, wanted)


def strong_role_families(title: str) -> frozenset[str]:
    """Return families a title names by a genuine 2+ word phrase, not a loose fragment.

    Family-review evidence must be a real named phrase in the title; the
    reverse-containment and distinctive-subset heuristics in ``_role_families``
    are too weak to justify pulling an unrelated title into that lane.
    """
    return frozenset(
        family.key
        for family in FAMILIES
        if any(
            title_contains_phrase(title, example) and len(role_words(example) - {"head"}) >= 2
            for example in family.titles
        )
    )


def shares_strong_role_family(found: str, wanted: str) -> bool:
    """Return whether both titles name the same family by a strong contiguous phrase."""
    return bool(strong_role_families(found) & strong_role_families(wanted))


def declares_adjacent_families(found: str, wanted: str) -> bool:
    """Return whether the taxonomy's curated table calls a family of each title adjacent."""
    return any(
        frozenset((found_family, wanted_family)) in _ADJACENT_ROLE_FAMILIES
        for found_family in _role_families(found)
        for wanted_family in _role_families(wanted)
    )


# "Executive" is a rung only ahead of a leadership noun, not in "Account Executive"
# or "Executive Assistant". "Associate Director" sits on the director rung alone,
# and "Executive Director" on the executive rung alone.
_LEADERSHIP_NOUNS = frozenset({"director", "vice", "vp", "svp", "evp", "president"})


# Each names a vice-president rank outright, so none needs a following
# leadership noun the way bare "executive" does. "avp" is left out: an
# assistant or associate vice president states no rung.
_EXECUTIVE_ALIASES = frozenset({"vp", "svp", "evp"})


def ladder_words(title: str) -> frozenset[str]:
    """Return the title words that state a rung on the ladder."""
    words = title_word_sequence(title)
    found = {
        word for index, word in enumerate(words) if word in LEVELS and _states_a_rung(words, index)
    }
    if _states_vice_president(words):
        found.add("executive")
    return frozenset(found)


def _names_vice_president_at(words: tuple[str, ...], index: int) -> bool:
    """Return whether the words starting at ``index`` name a vice-president rank."""
    if index >= len(words):
        return False
    return words[index] in _EXECUTIVE_ALIASES or (
        words[index] == "vice" and index + 1 < len(words) and words[index + 1] == "president"
    )


def _states_vice_president(words: tuple[str, ...]) -> bool:
    """Return whether the title states the vice-president rank itself.

    "Associate" and "assistant" modify the rank rather than stating it, the
    same way "associate" does for "Associate Director", so neither states a rung.
    """
    return any(
        _names_vice_president_at(words, index)
        and not (index and words[index - 1] in {"associate", "assistant"})
        for index in range(len(words))
    )


def _states_a_rung(words: tuple[str, ...], index: int) -> bool:
    word = words[index]
    following = words[index + 1] if index + 1 < len(words) else ""
    ahead_of_leader = following in _LEADERSHIP_NOUNS
    if word == "executive":
        return ahead_of_leader
    if word == "associate":
        return not ahead_of_leader
    if word == "senior":
        rank = index + 2 if following == "executive" else index + 1
        return not _names_vice_president_at(words, rank)
    if word == "director":
        return not (index and words[index - 1] == "executive")
    return True


def role_family_conflict(found: str, wanted: str) -> str | None:
    """Return a distinct-occupation token in ``found`` absent from ``wanted``, if any."""
    extra = (role_words(found) - role_words(wanted)) & ROLE_FAMILY_TOKENS
    if extra:
        return next(iter(sorted(extra)))
    found_families = _role_families(found)
    wanted_families = _role_families(wanted)
    if (
        found_families
        and wanted_families
        and found_families.isdisjoint(wanted_families)
        and not declares_adjacent_families(found, wanted)
    ):
        return next(iter(sorted(found_families)))
    return None


@lru_cache(maxsize=4096)
def _role_families(title: str) -> frozenset[str]:
    """Return shipped families explicitly named by a title or occupation phrase."""
    words = role_words(title)
    return frozenset(
        family.key
        for family in FAMILIES
        if any(
            title_contains_phrase(title, example)
            or (
                title_contains_phrase(example, title)
                and len(role_words(title) - _GENERIC_TITLE_NOUNS) >= 2
            )
            or (
                len(distinctive := role_words(example) - _GENERIC_TITLE_NOUNS) >= 2
                and distinctive <= words
            )
            for example in family.titles
        )
    )


def shared_share(found: str, wanted: str) -> float:
    """Return what fraction of ``wanted``'s meaningful words ``found`` carries."""
    asked = role_words(wanted)
    if not asked:
        return 0.0
    return len(role_words(found) & asked) / len(asked)


def answers_title(found: str, wanted: str) -> bool:
    """Return whether ``found`` carries enough of ``wanted``'s role words."""
    if not title_words(wanted):
        return False
    return shared_share(found, wanted) > TITLE_OVERLAP


def best_title_answer(found: str, wanted: list[str]) -> tuple[str, float] | None:
    """Return the strongest stated title this title actually answers."""
    answers = [
        (candidate, shared_share(found, candidate))
        for candidate in wanted
        if answers_title(found, candidate)
    ]
    return max(answers, key=lambda item: item[1], default=None)
