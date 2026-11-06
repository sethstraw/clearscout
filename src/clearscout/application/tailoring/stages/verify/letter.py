"""Verify a drafted cover letter against the evidence the resume was held to."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from itertools import groupby

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring.fit.lexicon import HEADING_WORDS
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.draft import (
    LETTER_ECHO_WORDS,
    MAXIMUM_LETTER_WORDS,
    CoverLetter,
    DraftedLine,
    SourceType,
)
from clearscout.application.tailoring.stages.select import Plan
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.results import Finding, Outcome
from clearscout.shared.text import sentence_pieces

from .checks import factual_review, figures, ownership_verbs
from .citations import cited, quoted, uncitable_ids
from .guardrail_checks import characters, first_person_hits, forbidden, protected
from .lexicon import BARE_I, CLAIM_VERBS, FIRST_PERSON_VERB, WORD, bare_word
from .report import KeywordCoverage, Report

# An "I" right after one of these capitalized heads is a Roman numeral, as in
# "Phase I trials"; after lowercase prose, as in "at this stage I", it is the pronoun.
_NUMERAL_HEAD = re.compile(r"\b(?:Phase|Level|Type|Class|Title|Grade|Stage|Tier|Schedule|Part)\s+$")

# A closing that says what the work makes of the candidate rather than stating the work:
# "It taught me", "This is why I", "That is the care I would apply", "My work gives me",
# or "I would bring". "That work showed my team a 30% drop" states the work.
_SUMMARY_WORD = r"[\w'\u2019-]+"
_SUMMARY_HEAD = (
    r"(?:(?:That|This|These|Those|Both|Together|Taken\s+together|All\s+of\s+this"
    rf"|Each\s+of\s+these)\b,?(?:\s+{_SUMMARY_WORD}){{0,3}}?|My(?:\s+{_SUMMARY_WORD}){{1,5}}?)"
)
_SUMMARY_BE = r"(?:\s+(?:is|was|are|were)|['\u2019]s)"
_EVALUATIVE_VERB = (
    r"(?:(?:has|have|had)\s+)?(?:show(?:s|ed|n)?|demonstrat(?:e|es|ed)|reflect(?:s|ed)?"
    r"|equip(?:s|ped)?|prepar(?:e|es|ed)|teach(?:es)?|taught|gi(?:ve|ves|ven)|gave"
    r"|lets|means?)"
)
# "It" usually names a system, as in "It showed me 1,200 duplicates", so only a verb
# that shapes the candidate makes it a summary.
_IT_SHAPED_ME = (
    r"It\s+(?:(?:has|had)\s+)?(?:taught|teaches|prepared|prepares|equipped|equips)\s+me\b"
)
_OFFER_VERB = r"(?:bring|apply|contribute|offer)\b"
_FIRST_PERSON_MODAL = r"I(?:\s+(?:would|will|can|could)|['\u2019]ll)\b"
# "I'd" is "I would" only before an offered verb; "the migration I'd scoped" is "I had".
_OFFERING = rf"(?:{_FIRST_PERSON_MODAL}|I['\u2019]d(?=\s+(?:\w+\s+)?{_OFFER_VERB}))"
_FIRST_PERSON_OFFER = (
    r"(?:(?:In\s+this\s+role|Here),\s+)?"
    rf"(?:(?:{_FIRST_PERSON_MODAL}|I['\u2019]d)(?:\s+\w+)?"
    r"|I(?:\s+am|['\u2019]m)\s+(?:ready|eager|keen)\s+to"
    rf"|I\s+(?:want|hope)\s+to)\s+{_OFFER_VERB}"
)
_SUMMARY_CLOSING = re.compile(
    rf"^\W*(?:{_SUMMARY_HEAD}(?:\s+{_EVALUATIVE_VERB}|{_SUMMARY_BE}\s+(?:why|how))"
    r"(?:\s+(?:how|why|what|that|where|the\s+way))?\s+(?:me|I|myself)\b"
    rf"|{_SUMMARY_HEAD}{_SUMMARY_BE}(?:\s+{_SUMMARY_WORD}){{0,4}}?\s+{_OFFERING}"
    rf"|{_IT_SHAPED_ME}|{_FIRST_PERSON_OFFER})"
)

# Three or more short items joined by commas or a conjunction name terms, not claims;
# an item holding a claim verb is a clause, so a series of clauses never matches.
_ITEM_WORD = rf"(?!(?:{'|'.join(sorted(CLAIM_VERBS | {'and', 'or'}))})\b)[\w-]+"
_TERM = rf"{_ITEM_WORD}(?: {_ITEM_WORD}){{0,2}}"
_SERIES = re.compile(rf"\b(?:{_TERM}(?:,| and| or) +){{2,}}(?:and |or )?{_TERM}", re.IGNORECASE)

# A sentence about the posting or research the letter was written from, a role that "calls
# for" something, or what was supplied. "Its research centers on" is the employer's own work,
# so research counts only when attributed ("as the research notes") or as "company research".
_DOCUMENT_NOUN = (
    r"(?:(?:the|this|that|its|their|your|[A-Z][\w&-]*['\u2019]s)\s+)"
    # A job title before the noun counts only when capitalized: "the Data Engineer posting".
    r"(?-i:(?:[A-Z][\w&/+-]*\s+){0,3})"
    r"(?:(?:job|role|position)\s+(?:posting|description|listing|ad(?:vert(?:isement)?)?)"
    r"|posting|listing)"
)
_RESEARCH_NOUN = r"(?:the|this|that)\s+(?:company\s+)?research"
_SOURCE_SUBJECT = rf"(?:{_DOCUMENT_NOUN}|(?:the|this|that)\s+company\s+research)"
_SOURCE_NOUN = rf"(?:{_DOCUMENT_NOUN}|{_RESEARCH_NOUN})"
_SOURCE_VERB = (
    r"(?:(?:also|further|clearly|explicitly|specifically|repeatedly)\s+)?"
    r"(?:emphasi[sz]es|notes|says|states|mentions|describes|highlights|stresses|lists"
    r"|asks(?:\s+for)?|calls\s+for|seeks|requires|wants|looks\s+for|focuses\s+on"
    r"|centers\s+on|points\s+to|names|signals|suggests|indicates|outlines|specifies"
    r"|underscores|identifies|references|frames|puts|places|shows)\b"
)
_ROLE_DEMAND = (
    r"^\W*(?:This|The)\s+(?:role|position|opening)\s+(?:also\s+)?"
    r"(?:calls\s+for|asks(?:\s+for)?|requires|seeks|is\s+looking\s+for|looks\s+for"
    r"|wants|demands|expects|emphasi[sz]es|stresses|highlights|prioriti[sz]es)\b"
)
# What was supplied counts only when missing, scarce, or speaking: "No research was provided",
# "was not supplied", "Beyond the details supplied", "The information provided says". A noun
# provided "to" or "by" someone is the candidate's or employer's own work.
_INPUT_NOUN = r"(?:(?:company\s+)?research|information|details|sources?|materials?|context)"
_HANDED = r"(?:supplied|provided|given)\b"
_HANDED_OVER = rf"{_HANDED}(?!\s+(?:to|by)\b)"
_SUPPLIED = (
    rf"\b(?:no|little|limited|scant)\s+{_INPUT_NOUN}\s+"
    rf"(?:(?:was|were|has\s+been|have\s+been)\s+)?{_HANDED_OVER}"
    rf"|\b{_INPUT_NOUN}\s+(?:[\w-]+\s+){{0,3}}?(?:was|were|is|are|has|have)"
    rf"(?:\s+not|n['\u2019]t)\s+(?:been\s+)?{_HANDED_OVER}"
    rf"|\bbeyond\s+(?:the\s+{_INPUT_NOUN}\s+|what\s+(?:was|were)\s+){_HANDED_OVER}"
    rf"|\b(?:the|this|that)\s+{_INPUT_NOUN}\s+{_HANDED}\s+{_SOURCE_VERB}"
)
_SOURCE_TALK = re.compile(
    rf"\b{_SOURCE_SUBJECT}\s+{_SOURCE_VERB}"
    rf"|\bas\s+{_RESEARCH_NOUN}\s+{_SOURCE_VERB}"
    rf"|\b(?:according\s+to|per)\s+{_SOURCE_NOUN}"
    r"|\b(?:named|described|listed|stated|mentioned|outlined|noted)\s+in\s+"
    rf"{_SOURCE_NOUN}|{_ROLE_DEMAND}|{_SUPPLIED}",
    re.IGNORECASE,
)


def check_letter(
    *,
    letter: CoverLetter,
    plan: Plan,
    inventory: CvInventory,
    guardrails: Guardrails,
    research_source_ids: tuple[str, ...] = (),
    research: tuple[ResearchSource, ...] = (),
) -> Report:
    """Check a cover letter against the same evidence the resume was held to.

    First person is allowed here, unlike the resume, because a letter that
    avoids it reads like a ransom note. Required strings are left out: they are
    checked against the contact header, which a letter does not carry.
    """
    body = "\n".join(paragraph.text for paragraph in letter.paragraphs)
    lines = tuple(letter.paragraphs)
    research_ids = (*research_source_ids, *(source.id for source in research))
    findings = (
        cited(lines, plan, "letter provenance", additional_ids=research_ids, allow_company=True),
        figures(lines, inventory, "letter numbers", research=research),
        _letter_completeness(body, inventory),
        _claim_ownership(lines),
        _source_talk(lines),
        ownership_verbs(
            [line for line in lines if SourceType.CANDIDATE in line.source_types],
            inventory,
            first_person=True,
        ),
        factual_review(lines, inventory, research=research),
        _letter_echo(_echoes(letter, inventory, research)),
        _letter_closing(body),
        _letter_length(body),
        characters(body, guardrails, source="the letter"),
        forbidden(body, body, guardrails),
        protected(body, guardrails),
    )
    return Report(
        findings=findings,
        coverage=KeywordCoverage(present=(), missing=()),
        uncitable=uncitable_ids(inventory, {*plan.drafted_ids(), *research_ids}),
    )


def _claim_ownership(lines: Sequence[DraftedLine]) -> Finding:
    """Do not support an explicit first-person achievement with company sources."""
    offenders = [
        sentence
        for line in lines
        if SourceType.COMPANY in line.source_types
        for sentence in sentence_pieces(line.text.strip())
        # Every bare I counts, since "At Globex I ran" follows a capitalized word too.
        if BARE_I.search(sentence) or first_person_hits(sentence)
    ]
    if offenders:
        return Finding(
            "claim ownership",
            Outcome.FAIL,
            f"{len(offenders)} candidate claim(s) cite company evidence: {quoted(offenders)}",
        )
    return Finding(
        "claim ownership", Outcome.PASS, "No explicit candidate claim uses company evidence"
    )


def _source_talk(lines: Sequence[DraftedLine]) -> Finding:
    """Send back sentences about the posting or research rather than about the employer."""
    offenders = [
        sentence
        for line in lines
        for sentence in sentence_pieces(line.text.strip())
        if _SOURCE_TALK.search(sentence)
    ]
    if offenders:
        return Finding(
            "letter source talk",
            Outcome.FAIL,
            f"{len(offenders)} sentence(s) talk about the posting or research instead of the "
            "employer; make the role, the employer, its product, or whom it serves the "
            "subject and say what it does, not what the role calls for, and "
            f"never say what was or was not supplied: {quoted(offenders)}",
        )
    return Finding("letter source talk", Outcome.PASS, "no sentence talks about its sources")


def _letter_completeness(body: str, inventory: CvInventory) -> Finding:
    """Reject missing bodies and salutations/signatures presented as bodies."""
    substantive = re.sub(re.escape(inventory.name), "", body, flags=re.IGNORECASE)
    substantive = re.sub(
        r"\b(?:sincerely|regards|thanks|thank you|best wishes|dear hiring team)\b",
        "",
        substantive,
        flags=re.IGNORECASE,
    )
    complete = len(WORD.findall(substantive)) >= 4
    return Finding(
        "letter completeness",
        Outcome.PASS if complete else Outcome.FAIL,
        "substantive body present"
        if complete
        else "Write a substantive evidence-backed body; a greeting or signature is incomplete.",
    )


def _ordinary_words(sentence: str) -> list[tuple[int, str]]:
    """Return each ordinary word of one sentence, casefolded, with its ``WORD`` index.

    Figures, acronyms, and capitalized words after the first are facts the numbers
    check wants copied exactly, so an echo does not count them. The gap they leave
    in the indices separates the words on either side.
    """
    return [
        (index, bare_word(word))
        for index, word in enumerate(WORD.findall(sentence))
        if not (word[0].isdigit() or (len(word) > 1 and word.isupper()))
        and (index == 0 or not word[0].isupper())
    ]


def _unbroken_windows(
    ordinary: Sequence[tuple[int, str]], length: int
) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Yield each window of ``length`` ordinary words holding no skipped word, with its offset."""
    for start in range(len(ordinary) - length + 1):
        window = ordinary[start : start + length]
        if window[-1][0] - window[0][0] == length - 1:
            yield start, tuple(word for _, word in window)


def _series_words(sentence: str) -> set[int]:
    """Return the ``WORD`` indices of the joints of each ``_SERIES`` of terms in ``sentence``."""
    words = list(WORD.finditer(sentence))
    joints: set[int] = set()
    for match in _SERIES.finditer(sentence):
        inside = [(i, w) for i, w in enumerate(words) if match.start() <= w.start() < match.end()]
        positions = _joint_positions(sentence, [word for _, word in inside])
        # The final item starts after the last separator, so its first word is a joint too.
        if positions and positions[-1] + 1 < len(inside):
            positions.append(positions[-1] + 1)
        joints.update(inside[position][0] for position in positions)
    return joints


def _joint_positions(sentence: str, inside: list[re.Match[str]]) -> list[int]:
    """Return the positions of a series' conjunctions and of the words ending its items.

    An item may be three words long, so only its edge at a separator is list, not context.
    """
    texts = [word.group().lower() for word in inside]
    return [
        position
        for position, word in enumerate(inside)
        if texts[position] in {"and", "or"}
        or texts[position + 1 : position + 2] in (["and"], ["or"])
        or sentence.startswith(",", word.end())
    ]


def _word_runs(text: str, length: int) -> set[tuple[str, ...]]:
    """Return every run of ``length`` consecutive ordinary words in ``text``."""
    return {
        window
        for sentence in sentence_pieces(text)
        for _, window in _unbroken_windows(_ordinary_words(sentence), length)
    }


def _echoes(
    letter: CoverLetter, inventory: CvInventory, research: tuple[ResearchSource, ...]
) -> list[tuple[str, str]]:
    """Return each letter sentence repeating its cited evidence, and what it copies.

    The letter prompt shows an entry with its details, so a run copied from either counts;
    each is read on its own, since a parent need not end in a period. A paragraph's
    sentences are also held to the company sources that paragraph cites.
    """
    cited_ids = {
        source_id
        for paragraph in letter.paragraphs
        for source_id, kind in zip(paragraph.source_ids, paragraph.source_types, strict=False)
        if kind is SourceType.CANDIDATE
    }
    copied: set[tuple[str, ...]] = set()
    for source_id in sorted(cited_ids):
        if entry := inventory.entry(source_id):
            for text in (entry.text, *entry.details):
                copied |= _word_runs(text, LETTER_ECHO_WORDS)
    company = {source.id: _word_runs(source.text, LETTER_ECHO_WORDS) for source in research}
    echoes = []
    for paragraph in letter.paragraphs:
        held: set[tuple[str, ...]] = set().union(
            *(
                company.get(source_id, set())
                for source_id, kind in zip(
                    paragraph.source_ids, paragraph.source_types, strict=False
                )
                if kind is SourceType.COMPANY
            )
        )
        echoes += [
            (sentence.strip(), phrase)
            for sentence in sentence_pieces(paragraph.text)
            if (
                phrase := max(
                    _copied_phrase(sentence, copied),
                    _copied_phrase(sentence, held, prose=True),
                    key=len,
                )
            )
        ]
    return echoes


def _copied_phrase(sentence: str, copied: set[tuple[str, ...]], *, prose: bool = False) -> str:
    """Return the sentence's own words spanning its longest copied claim, or nothing.

    A run states an action when it holds a claim verb or the verb the sentence's
    "I" takes. A run stating none names terms of art, which carry no claim and have
    no accurate paraphrase, until it runs longer than a heading and reads as prose;
    a series of terms does not count toward the run's length. With ``prose`` every
    run counts, series included, since a company page's wording is not a CV's terms of art.
    """
    ordinary = _ordinary_words(sentence)
    covered = [False] * len(ordinary)
    for start, window in _unbroken_windows(ordinary, LETTER_ECHO_WORDS):
        if window in copied:
            covered[start : start + LETTER_ECHO_WORDS] = [True] * LETTER_ECHO_WORDS
    runs = [
        [pair for pair, _ in run]
        for inside, run in groupby(zip(ordinary, covered, strict=True), key=lambda item: item[1])
        if inside
    ]
    series = set() if prose else _series_words(sentence)
    predicates = _first_person_verbs(sentence)
    claims = [
        run
        for run in runs
        if (length := sum(index not in series for index, _ in run)) >= LETTER_ECHO_WORDS
        and (
            prose
            or length > HEADING_WORDS
            or any(word in CLAIM_VERBS or index in predicates for index, word in run)
        )
    ]
    longest = max(claims, key=len, default=[])
    return " ".join(WORD.findall(sentence)[longest[0][0] : longest[-1][0] + 1]) if longest else ""


def _first_person_verbs(sentence: str) -> set[int]:
    """Return the ``WORD`` indices of the verbs ``FIRST_PERSON_VERB`` reads in ``sentence``."""
    starts = {
        match.start(1)
        for match in FIRST_PERSON_VERB.finditer(sentence)
        if not _NUMERAL_HEAD.search(sentence, 0, match.start())
    }
    return {index for index, word in enumerate(WORD.finditer(sentence)) if word.start() in starts}


def _letter_echo(echoes: Sequence[tuple[str, str]]) -> Finding:
    """Warn on letter sentences repeating a run of words, quoting each and the words it copies."""
    if echoes:
        listed = "; ".join(f"{sentence!r} copies {phrase!r}" for sentence, phrase in echoes)
        return Finding(
            "letter echo",
            Outcome.WARN,
            f"{len(echoes)} sentence(s) repeat {LETTER_ECHO_WORDS} or more consecutive words "
            f"of cited evidence; consider restating each in new words: {listed}",
            redraftable=False,
        )
    return Finding(
        "letter echo",
        Outcome.PASS,
        f"no sentence repeats {LETTER_ECHO_WORDS} words of cited evidence",
    )


def _letter_closing(body: str) -> Finding:
    """Warn when the letter's last sentence says what its work shows about the candidate."""
    last = sentence_pieces(body.strip())[-1]
    if _SUMMARY_CLOSING.match(last):
        return Finding(
            "letter closing",
            Outcome.WARN,
            "The last sentence says what the work shows about the candidate; end on part "
            f"of the work itself, what was built, decided, measured or changed: {last!r}",
            redraftable=False,
        )
    return Finding("letter closing", Outcome.PASS, "the letter closes on its work")


def _letter_length(text: str) -> Finding:
    """Count the letter's words against the limit its prompt states."""
    words = len(text.split())
    if words > MAXIMUM_LETTER_WORDS:
        return Finding(
            "letter length",
            Outcome.FAIL,
            f"{words} words, and the limit is {MAXIMUM_LETTER_WORDS}. Cut it, "
            "without dropping a cited claim.",
        )
    return Finding("letter length", Outcome.PASS, f"{words} words, within {MAXIMUM_LETTER_WORDS}")
