"""Title screening against the saved roles, focus and exclusions."""

from __future__ import annotations

import re
from typing import TypeVar

from tailorcv.application.discovery.screening.employer import excluded_employer
from tailorcv.application.discovery.screening.findings import (
    DiscoveryScreening,
    ScreeningCode,
    ScreeningDimension,
    ScreeningFinding,
    ScreeningStanding,
)
from tailorcv.application.discovery.titles import (
    TITLE_OVERLAP,
    declares_adjacent_families,
    distinct_shared_words,
    names_a_bare_function,
    occupation_disagrees,
    role_family_conflict,
    role_words,
    shared_share,
    shares_occupation_noun,
    shares_strong_role_family,
    states_a_specific_occupation,
    title_contains_phrase,
    title_words,
)
from tailorcv.domain.identity import mentions, normalize_text
from tailorcv.domain.search_parameters import (
    ClusterKind,
    JobSearchParameters,
)
from tailorcv.domain.search_parameters.roles import title_excluded
from tailorcv.domain.staged_facts import PublishedFact

_T = TypeVar("_T")

# Publishers mark internal test requisitions on live boards with these phrases.
_TEST_REQUISITION = re.compile(r"\b(?:do\W+not\W+apply|test\W+req(?:uisition)?)\b", re.IGNORECASE)


def publisher_test_marker(title: str) -> str | None:
    """Return the publisher test-requisition marker a title carries, if any."""
    found = _TEST_REQUISITION.search(title)
    return found.group(0) if found is not None else None


def _title_code(screening: DiscoveryScreening) -> ScreeningCode:
    return next(
        finding.code
        for finding in screening.findings
        if finding.dimension is ScreeningDimension.TITLE
    )


def _title_review_tier(candidate: tuple[_T, DiscoveryScreening]) -> int:
    code = _title_code(candidate[1])
    if code is ScreeningCode.TITLE_MATCH:
        return 0
    return 2 if _is_review_lane(code) else 1


def _is_review_lane(code: ScreeningCode) -> bool:
    """Return whether this title answer consumes the bounded relevance-review lane."""
    return code in {
        ScreeningCode.TITLE_ROLE_FAMILY_REVIEW,
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        ScreeningCode.TITLE_DOMAIN_REVIEW,
    }


def select_title_survivors(
    candidates: list[tuple[_T, DiscoveryScreening]], limit: int
) -> tuple[list[_T], list[_T], list[_T]]:
    """Prefer stronger title answers without discarding uncertain candidates."""
    eligible = [item for item, _screening in sorted(candidates, key=_title_review_tier)]
    return eligible[:limit], [], eligible[limit:]


def _excluded_title(title: str, parameters: JobSearchParameters) -> str | None:
    """Return the shared title exclusion decision."""
    return title_excluded(title, parameters.seniority.excluded_titles)


def _strict_title_finding(title: str, wanted: list[str]) -> ScreeningFinding:
    """Screen a title under the explicitly literal matching policy."""
    matched = next((item for item in wanted if item.casefold() in title.casefold()), None)
    return ScreeningFinding(
        ScreeningDimension.TITLE,
        ScreeningStanding.MATCH if matched is not None else ScreeningStanding.UNKNOWN,
        ScreeningCode.TITLE_MATCH if matched is not None else ScreeningCode.TITLE_MISMATCH,
        matched or "",
    )


# Ends a specialist subject whose focus term came from the posting summary, not the title.
POSTING_SUBJECT_SUFFIX = " (posting)"


def _domain_title_finding(title: str, parameters: JobSearchParameters) -> ScreeningFinding | None:
    """Keep a configured domain term available for review without claiming role fit."""
    term = _first_configured_title_token(
        parameters.seniority.strong_domain_tokens, title_words(title)
    )
    if term is None:
        return None
    return ScreeningFinding(
        ScreeningDimension.TITLE,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.TITLE_DOMAIN_REVIEW,
        term,
        f"The title states the configured domain term {term}; its role needs review.",
    )


def _specialist_ic_finding(
    title: str, parameters: JobSearchParameters, summary: str = ""
) -> ScreeningFinding | None:
    """Retain specialist noun evidence, or configured domain evidence for an unfamiliar role."""
    role_noun = specialist_role_noun(title, parameters)
    if role_noun is None:
        return _domain_title_finding(title, parameters)
    tokens = title_words(title)
    domain_token = _first_configured_title_token(parameters.seniority.strong_domain_tokens, tokens)
    in_title = domain_token or next(
        (term for term in _focus_terms(parameters) if title_contains_phrase(title, term)), None
    )
    if in_title is not None:
        return _specialist_finding(
            f"{role_noun}, {in_title}",
            f"The title states {role_noun} and {in_title}, so it is kept for relevance review.",
        )
    in_summary = next(iter(summary_focus_terms(parameters, summary)), None)
    if in_summary is not None:
        return _specialist_finding(
            f"{role_noun}, {in_summary}{POSTING_SUBJECT_SUFFIX}",
            f"The posting states {in_summary}, so this {role_noun} title is kept for "
            "relevance review.",
        )
    phrase = _saved_role_phrase(title, parameters)
    if phrase is not None:
        return _specialist_finding(
            phrase, f"The title states {phrase}, so it is kept for relevance review."
        )
    return None


def _saved_role_phrase(title: str, parameters: JobSearchParameters) -> str | None:
    """Return the first explicitly saved role noun stated in the title."""
    return next(
        (
            noun.strip().casefold()
            for noun in parameters.seniority.specialist_ic_role_nouns
            if title_contains_phrase(title, noun)
        ),
        None,
    )


def _specialist_finding(subject: str, because: str) -> ScreeningFinding:
    return ScreeningFinding(
        ScreeningDimension.TITLE,
        ScreeningStanding.UNKNOWN,
        ScreeningCode.TITLE_SPECIALIST_IC_REVIEW,
        subject,
        because,
    )


def summary_focus_terms(parameters: JobSearchParameters, summary: str) -> tuple[str, ...]:
    """Return the saved multi-word focus terms a posting summary states, in stated order."""
    haystack = summary.casefold()
    return tuple(term for term in _summary_retention_terms(parameters) if mentions(haystack, term))


def specialist_role_noun(title: str, parameters: JobSearchParameters) -> str | None:
    """Return the first saved specialist role noun a title carries."""
    return _first_configured_title_token(
        parameters.seniority.specialist_ic_role_nouns, title_words(title)
    )


def summary_may_retain_title(
    finding: ScreeningFinding, title: str, parameters: JobSearchParameters
) -> bool:
    """Return whether a focus term in the posting's summary could keep this unmatched title."""
    return (
        finding.code is ScreeningCode.TITLE_MISMATCH
        and parameters.seniority.title_matching != "strict"
        and specialist_role_noun(title, parameters) is not None
        and bool(_summary_retention_terms(parameters))
    )


def _summary_retention_terms(parameters: JobSearchParameters) -> tuple[str, ...]:
    """Return the multi-word focus terms whose mention in a summary may keep a title."""
    return tuple(term for term in _focus_terms(parameters) if " " in term)


def _focus_terms(parameters: JobSearchParameters) -> tuple[str, ...]:
    """Return every stated capability and domain term, casefolded and in stated order.

    A cluster's name is the person's label for the group, not posting vocabulary,
    so only its stated terms retain a title; ranking reads the name as well.
    """
    return tuple(
        dict.fromkeys(
            term.strip().casefold()
            for kind in (ClusterKind.CAPABILITY, ClusterKind.DOMAIN)
            for cluster in parameters.focus.stated_clusters(kind)
            for term in cluster.terms
            if term.strip()
        )
    )


def body_states_saved_focus(parameters: JobSearchParameters, text: str) -> str | None:
    """Return the first saved capability or domain term the publisher body mentions."""
    if not text.strip():
        return None
    haystack = text.casefold()
    return next(
        (term for term in _focus_terms(parameters) if mentions(haystack, term)),
        None,
    )


def _first_configured_title_token(
    configured: list[str], title_tokens: frozenset[str]
) -> str | None:
    """Return the first complete operator-configured term present in a title."""
    for item in configured:
        configured_tokens = title_words(item)
        if configured_tokens and configured_tokens <= title_tokens:
            return item.strip().casefold()
    return None


def _title_overlap_standing(
    title: str, strongest: str, share: float, distinct_words: frozenset[str]
) -> tuple[ScreeningStanding, ScreeningCode] | None:
    """Return the standing an overlap above ``TITLE_OVERLAP`` earns, or None below it."""
    if share > TITLE_OVERLAP and role_words(title) - role_words(strongest):
        return ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_EXTRA_ROLE_WORDS
    if share > TITLE_OVERLAP and (distinct_words or role_words(title) == role_words(strongest)):
        # A bare function head ("Freight Operations Management") names a team, not a
        # person, against a title that does name one; near match still stages
        # the row for human review.
        if names_a_bare_function(title) and not names_a_bare_function(strongest):
            return ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_NEAR_MATCH
        return ScreeningStanding.MATCH, ScreeningCode.TITLE_MATCH
    return None


def _family_review_evidence(title: str, strongest: str, distinct_words: frozenset[str]) -> bool:
    """Return whether shared-word or family evidence justifies a bounded review.

    A shared occupation noun needs two distinct words alongside it, unless the
    title also states its own specific occupation word (a higher rung of the
    same function must not read worse than a literal rung match does); a strong
    named family needs at least one distinct word, and so does a same-rung pair
    whose families the taxonomy declares adjacent.
    """
    return bool(
        (len(distinct_words) >= 2 and shares_occupation_noun(title, strongest))
        or (
            distinct_words
            and shares_occupation_noun(title, strongest)
            and states_a_specific_occupation(title)
        )
        or (distinct_words and shares_strong_role_family(title, strongest))
        or (
            distinct_words
            and shares_occupation_noun(title, strongest)
            and declares_adjacent_families(title, strongest)
        )
    )


# Ordered as title expansion admits them: a review lane every caller takes beats
# the family lane only a bounded-review caller takes.
_TITLE_FAVOR = {
    ScreeningCode.TITLE_MATCH: 0,
    ScreeningCode.TITLE_NEAR_MATCH: 1,
    ScreeningCode.TITLE_EXTRA_ROLE_WORDS: 1,
    ScreeningCode.TITLE_SPECIALIST_IC_REVIEW: 1,
    ScreeningCode.TITLE_DOMAIN_REVIEW: 1,
    ScreeningCode.TITLE_ROLE_FAMILY_REVIEW: 2,
    ScreeningCode.TITLE_MISMATCH: 3,
}


def _guided_title_finding(
    title: str, wanted: list[str], parameters: JobSearchParameters, summary: str = ""
) -> ScreeningFinding:
    """Keep extra role words and exact-boundary overlaps for relevance review.

    The most favorable answer across non-conflicting stated titles wins, so adding
    a stated title never loses a title; higher share, then stated order, breaks ties.
    """
    non_conflicting = [item for item in wanted if role_family_conflict(title, item) is None]
    if not non_conflicting:
        if _saved_role_phrase(title, parameters) is not None:
            specialist = _specialist_ic_finding(title, parameters, summary)
            if specialist is not None:
                return specialist
        domain = _domain_title_finding(title, parameters)
        if domain is not None:
            return domain
        rejected = max(wanted, key=lambda item: shared_share(title, item))
        return ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.UNKNOWN,
            ScreeningCode.TITLE_ROLE_FAMILY_CONFLICT,
            rejected,
        )
    shares = [shared_share(title, item) for item in non_conflicting]
    answers = [
        (_guided_title_answer(title, item, share, parameters, summary), share)
        for item, share in zip(non_conflicting, shares, strict=True)
    ]
    return min(answers, key=lambda answer: (_TITLE_FAVOR[answer[0].code], -answer[1]))[0]


def _guided_title_answer(
    title: str, strongest: str, share: float, parameters: JobSearchParameters, summary: str
) -> ScreeningFinding:
    """Answer a title against one stated title it shares ``share`` of the words with."""
    # Excluding generic occupation nouns (manager, director, engineer...) from shared-word
    # evidence keeps a bare title-shape overlap from reading as a genuine match.
    distinct_words = distinct_shared_words(title, strongest)
    disagrees = occupation_disagrees(title, strongest)
    overlap = _title_overlap_standing(title, strongest, share, distinct_words)
    if overlap is not None:
        standing, code = overlap
    elif share >= TITLE_OVERLAP and len(distinct_words) >= 2 and not disagrees:
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_NEAR_MATCH
    elif not disagrees and _family_review_evidence(title, strongest, distinct_words):
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_ROLE_FAMILY_REVIEW
    else:
        specialist = _specialist_ic_finding(title, parameters, summary)
        if specialist is not None:
            return specialist
        standing, code = ScreeningStanding.UNKNOWN, ScreeningCode.TITLE_MISMATCH
    return ScreeningFinding(ScreeningDimension.TITLE, standing, code, strongest)


def _title_finding(
    title: str, parameters: JobSearchParameters, searched: list[str], summary: str = ""
) -> ScreeningFinding:
    """Screen title identity while preserving ambiguous near matches for review."""
    excluded = _excluded_title(title, parameters)
    if excluded is not None:
        return ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.REFUSE,
            ScreeningCode.TITLE_EXCLUDED,
            excluded,
        )
    wanted = [item for item in searched if item.strip()]
    if not wanted:
        return ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.MATCH,
            ScreeningCode.TITLE_UNRESTRICTED,
        )
    if parameters.seniority.title_matching == "strict":
        return _strict_title_finding(title, wanted)
    return _guided_title_finding(title, wanted, parameters, summary)


def _employer_domain_title_finding(
    title: str,
    parameters: JobSearchParameters,
    employer: str,
    source_employer: str,
) -> ScreeningFinding | None:
    """Retain a saved role phrase with an explicitly associated employer domain."""
    phrase = _saved_role_phrase(title, parameters)
    if phrase is None or any(
        excluded_employer(name, parameters) is not None for name in (employer, source_employer)
    ):
        return None
    for name, context in (
        (employer, "publisher-confirmed employer"),
        (source_employer, "registered source employer"),
    ):
        identity = normalize_text(name)
        if not identity:
            continue
        for archetype in parameters.employers.stated_archetypes:
            if any(identity == normalize_text(member) for member in archetype.employer_names):
                return ScreeningFinding(
                    ScreeningDimension.TITLE,
                    ScreeningStanding.UNKNOWN,
                    ScreeningCode.TITLE_DOMAIN_REVIEW,
                    f"{phrase}, {name.strip()}, {archetype.name}",
                    f"The {context} {name.strip()} belongs to configured archetype "
                    f"{archetype.name}; the title states {phrase} and needs relevance review.",
                )
    return None


def screen_discovery_title(
    title: str,
    aliases: PublishedFact,
    parameters: JobSearchParameters,
    searched: list[str],
    summary: str = "",
    *,
    employer: str = "",
    source_employer: str = "",
) -> ScreeningFinding:
    """Screen copied title text and publisher-evidenced aliases as alternate names."""
    candidates = (title, *aliases.values)
    marker = publisher_test_marker(title)
    if marker is not None:
        return ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.REFUSE,
            ScreeningCode.TITLE_TEST_REQUISITION,
            marker,
        )
    excluded = next(
        (
            finding
            for candidate in candidates
            if (finding := _excluded_title(candidate, parameters)) is not None
        ),
        None,
    )
    if excluded is not None:
        return ScreeningFinding(
            ScreeningDimension.TITLE,
            ScreeningStanding.REFUSE,
            ScreeningCode.TITLE_EXCLUDED,
            excluded,
        )
    findings = tuple(
        _title_finding(candidate, parameters, searched, summary) for candidate in candidates
    )
    selected = next(
        finding
        for standing in (
            ScreeningStanding.MATCH,
            ScreeningStanding.UNKNOWN,
            ScreeningStanding.REFUSE,
        )
        for finding in findings
        if finding.standing is standing
    )
    if (
        parameters.seniority.title_matching == "strict"
        or selected.standing is ScreeningStanding.MATCH
    ):
        return selected
    return next(
        (
            domain_finding
            for candidate in candidates
            if (
                domain_finding := _employer_domain_title_finding(
                    candidate, parameters, employer, source_employer
                )
            )
            is not None
        ),
        selected,
    )
