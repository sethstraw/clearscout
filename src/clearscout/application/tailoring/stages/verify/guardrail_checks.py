"""Hold text to the person's rules: first person, characters, and configured phrases."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.shared.results import Finding, Outcome

from .lexicon import BARE_I, PRONOUN

ALLOWED_NON_ASCII = frozenset({"•"})


def first_person(text: str, guardrails: Guardrails) -> Finding:
    """Enforce the no-first-person rule when the rules ask for it."""
    if guardrails.first_person:
        return Finding("first person", Outcome.PASS, "allowed by your rules")
    if hits := first_person_hits(text):
        return Finding("first person", Outcome.FAIL, f"found {', '.join(sorted(set(hits)))}")
    return Finding("first person", Outcome.PASS, "no first-person pronouns")


def _offenders(text: str) -> list[str]:
    """Return the characters in ``text`` that violate the ASCII policy."""
    return sorted(
        {
            character
            for character in text
            if ord(character) > 127 and character not in ALLOWED_NON_ASCII
        }
    )


def _describe(offenders: list[str]) -> str:
    """Name each offending character so it can be found and removed."""
    return ", ".join(f"{c!r} ({unicodedata.name(c, 'unnamed')})" for c in offenders)


def first_person_hits(text: str) -> list[str]:
    """Return every genuine first-person pronoun in ``text``.

    The lowercase forms are pronouns wherever they appear; their capitalized
    forms only at the start of a sentence. A bare "I" is a pronoun unless the
    word before it is capitalized, which keeps "Phase I" and "Type I error" out;
    those arrive in titles copied from the CV, where no rewrite can remove them.
    """
    hits: list[str] = []
    for match in PRONOUN.finditer(text):
        word = match.group(0)
        if word[0].islower() or _starts_sentence(text, match.start()):
            hits.append(word)
    for match in BARE_I.finditer(text):
        if not _follows_capitalised_word(text, match.start()):
            hits.append(match.group(0))
    return hits


def _starts_sentence(text: str, index: int) -> bool:
    """Return whether the token at ``index`` opens a sentence."""
    before = text[:index].rstrip()
    return not before or before[-1] in ".!?"


def _follows_capitalised_word(text: str, index: int) -> bool:
    """Return whether the token at ``index`` follows a capitalized word.

    "Phase I" and "Type I" are enumerations; "and I" is a pronoun.
    """
    before = text[:index].rstrip()
    if not before:
        return False
    previous = before.split()[-1]
    return previous[:1].isupper()


def characters(text: str, guardrails: Guardrails, source: str) -> Finding:
    """Enforce ASCII punctuation on model-authored prose."""
    if not guardrails.ascii_only:
        return Finding("character policy", Outcome.PASS, "ASCII policy disabled in your rules")
    if offenders := _offenders(text):
        return Finding(
            "character policy", Outcome.FAIL, f"non-ASCII in {source}: {_describe(offenders)}"
        )
    return Finding("character policy", Outcome.PASS, "ASCII punctuation only")


def characters_in_source(markdown: str, written: str, guardrails: Guardrails) -> Finding:
    """Report non-ASCII that came from the master CV rather than the model.

    Marked as not redraftable, because it arrived by being copied and will still
    be there after any number of rewrites. The remedy is to edit the CV.
    """
    if not guardrails.ascii_only:
        return Finding("source characters", Outcome.PASS, "ASCII policy disabled in your rules")
    copied = _offenders(markdown)
    from_model = set(_offenders(written))
    offenders = [character for character in copied if character not in from_model]
    if offenders:
        return Finding(
            "source characters",
            Outcome.FAIL,
            f"non-ASCII copied from your master CV: {_describe(offenders)}. "
            "Fix it there; no rewrite can remove it.",
            redraftable=False,
        )
    return Finding("source characters", Outcome.PASS, "nothing non-ASCII copied from the CV")


def forbidden(markdown: str, written: str, guardrails: Guardrails) -> Finding:
    """Reject any phrase the guardrails file forbids, drafted or copied.

    Matched in any case with runs of whitespace collapsed and anchored to word
    boundaries. A hit missing from ``written`` arrived by being copied, so no
    rewrite can clear it and the whole finding stops being redraftable.
    """
    hits = [value for value in guardrails.forbidden if _phrase_present_anchored(markdown, value)]
    drafted = [value for value in hits if _phrase_present_anchored(written, value)]
    copied = [value for value in hits if value not in drafted]
    if copied:
        wrote = f"written: {', '.join(drafted)}. " if drafted else ""
        return Finding(
            "forbidden strings",
            Outcome.FAIL,
            f"{wrote}copied from your master CV: {', '.join(copied)}. "
            "Fix it there; no rewrite can remove it.",
            redraftable=False,
        )
    if hits:
        return Finding("forbidden strings", Outcome.FAIL, f"present: {', '.join(hits)}")
    return Finding("forbidden strings", Outcome.PASS, "none present")


def required(
    markdown: str,
    guardrails: Guardrails,
    *,
    drafted: Mapping[str, str] | None = None,
    recorded: Sequence[str] = (),
) -> Finding:
    """Require every string the guardrails file says must always appear.

    ``drafted`` maps each entry the drafter was shown and that printed to its text: a
    phrase one carries was dropped, so a redraft citing it can restore it. ``recorded``
    is the rest of the record, which only changes the advice.
    """
    missing = [value for value in guardrails.required if value and value not in markdown]
    if not missing:
        return Finding("required strings", Outcome.PASS, "all present")
    shown = drafted or {}
    carriers = {
        value: [entry for entry, text in shown.items() if value in text] for value in missing
    }
    if all(carriers.values()):
        located = "; ".join(f"{value} (in {', '.join(ids)})" for value, ids in carriers.items())
        return Finding(
            "required strings",
            Outcome.FAIL,
            f"missing: {located}. Restore each word for word on a line citing that entry.",
        )
    if all(any(value in text for text in (*shown.values(), *recorded)) for value in missing):
        return Finding(
            "required strings",
            Outcome.FAIL,
            f"missing: {', '.join(missing)}. Your master CV has these, but not in "
            "evidence drafted for this posting.",
            redraftable=False,
        )
    listed = ", ".join(missing)
    return Finding(
        "required strings",
        Outcome.FAIL,
        f"missing: {listed}. These are copied from your master CV, "
        "so add them there or drop them from your rules.",
        redraftable=False,
    )


def protected(markdown: str, guardrails: Guardrails) -> Finding:
    """Require a protected phrase verbatim wherever it is mentioned.

    The trigger is the phrase itself, matched case-insensitively and with
    runs of whitespace collapsed, so a variant spelling of the phrase still
    demands the exact configured form. An ordinary use of a common first
    word does not fire the rule.
    """
    broken = [
        phrase
        for phrase in guardrails.protected
        if phrase_present_normalized(markdown, phrase) and phrase not in markdown
    ]
    if broken:
        return Finding(
            "protected strings",
            Outcome.FAIL,
            f"phrase mentioned but not written verbatim: {'; '.join(broken)}",
        )
    return Finding("protected strings", Outcome.PASS, "intact where mentioned")


def phrase_present_normalized(markdown: str, phrase: str) -> bool:
    """Return whether the phrase appears in any case with collapsed whitespace."""
    needle = re.sub(r"\s+", " ", phrase).strip().casefold()
    if not needle:
        return False
    haystack = re.sub(r"\s+", " ", markdown).casefold()
    return needle in haystack


def _phrase_present_anchored(markdown: str, phrase: str) -> bool:
    """Return whether the phrase appears as whole words, in any case or spacing."""
    pattern = anchored_phrase_pattern(phrase)
    return bool(pattern) and re.search(pattern, markdown, re.IGNORECASE) is not None


def anchored_phrase_pattern(phrase: str) -> str:
    """Return a whole-word, whitespace-tolerant pattern, empty when the phrase has no words."""
    # Word-character lookarounds rather than \b, so a phrase ending in punctuation matches.
    words = phrase.split()
    if not words:
        return ""
    return r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
