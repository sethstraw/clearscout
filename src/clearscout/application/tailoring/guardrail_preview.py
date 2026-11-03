"""Preview guardrail phrases against the current master CV before a paid draft.

Runs no model and writes nothing. It answers, deterministically, what the same
checks in :mod:`clearscout.application.tailoring.stages.verify` would already
find in the evidence a draft is built from, so a rule can be inspected before
it becomes a drafting requirement.
"""

from __future__ import annotations

import re

from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.context import states_work_authorization
from clearscout.application.tailoring.stages.verify.guardrail_checks import (
    anchored_phrase_pattern,
    forbidden,
    phrase_present_normalized,
    required,
)
from clearscout.shared.models import StrictModel
from clearscout.shared.results import Outcome

MAX_REPORTED_OFFSETS = 3


class PhraseMatch(StrictModel):
    """Where one configured phrase was found in the master CV, if at all."""

    phrase: str
    offsets: list[int]
    matched: bool
    redraftable: bool


class WorkAuthorizationPrecedence(StrictModel):
    """Which work-authorization line a drafted resume would actually carry."""

    cv_line: str | None
    drafting_line: str | None
    effective: str | None
    drops_cv_line: bool


class GuardrailPreviewReport(StrictModel):
    """Per-rule phrase matches against the current master CV.

    Distinctions ship as guidance to the drafter and are never checked by any
    deterministic rule, so they carry no entry here.
    """

    forbidden: list[PhraseMatch]
    required: list[PhraseMatch]
    protected: list[PhraseMatch]
    work_authorization_precedence: WorkAuthorizationPrecedence


def preview_against_master_cv(rules: Guardrails, cv_markdown: str) -> GuardrailPreviewReport:
    """Report every configured phrase's presence in the current master CV."""
    return GuardrailPreviewReport(
        forbidden=[_forbidden_match(phrase, cv_markdown) for phrase in rules.forbidden],
        required=[_required_match(phrase, cv_markdown) for phrase in rules.required],
        protected=[_protected_match(phrase, cv_markdown) for phrase in rules.protected],
        work_authorization_precedence=_work_authorization_precedence(rules, cv_markdown),
    )


def _forbidden_match(phrase: str, cv_markdown: str) -> PhraseMatch:
    """Report where a forbidden phrase already appears in the master CV.

    Reuses ``forbidden`` scoped to one phrase, so a match here is exactly the
    match a drafted resume that copied this text would fail on.
    """
    # The preview scans the master CV rather than a drafted document, so the text
    # searched is also the text written and no hit reads as copied from elsewhere.
    finding = forbidden(
        cv_markdown, cv_markdown, Guardrails(forbidden=[phrase]) if phrase else Guardrails()
    )
    matched = finding.outcome is Outcome.FAIL
    return PhraseMatch(
        phrase=phrase,
        offsets=_anchored_offsets(phrase, cv_markdown)[:MAX_REPORTED_OFFSETS],
        matched=matched,
        redraftable=finding.redraftable,
    )


def _required_match(phrase: str, cv_markdown: str) -> PhraseMatch:
    """Report whether a required phrase exists as evidence to draft from.

    Reuses ``required``, which never treats a missing phrase as fixable by a
    redraft: it is either already in the master CV, or a rule that can only be
    satisfied by adding it there.
    """
    finding = required(cv_markdown, Guardrails(required=[phrase]) if phrase else Guardrails())
    matched = finding.outcome is not Outcome.FAIL
    return PhraseMatch(
        phrase=phrase,
        offsets=_literal_offsets(phrase, cv_markdown)[:MAX_REPORTED_OFFSETS],
        matched=matched,
        redraftable=finding.redraftable,
    )


def _protected_match(phrase: str, cv_markdown: str) -> PhraseMatch:
    """Report every place a protected phrase is mentioned in the master CV.

    ``protected`` itself compares a draft against its own source text, which
    is trivially satisfied when both sides are the master CV, so this reuses
    ``phrase_present_normalized`` directly: the same case-insensitive,
    whitespace-collapsed match ``protected`` is built on.
    """
    matched = phrase_present_normalized(cv_markdown, phrase)
    return PhraseMatch(
        phrase=phrase,
        offsets=_normalized_offsets(phrase, cv_markdown)[:MAX_REPORTED_OFFSETS],
        matched=matched,
        redraftable=True,
    )


def _literal_offsets(phrase: str, text: str) -> list[int]:
    """Return every start position of ``phrase`` as a case-sensitive substring."""
    if not phrase:
        return []
    offsets: list[int] = []
    start = 0
    while (index := text.find(phrase, start)) != -1:
        offsets.append(index)
        start = index + 1
    return offsets


def _anchored_offsets(phrase: str, text: str) -> list[int]:
    """Return every start position where ``phrase`` appears as whole words, in any case."""
    pattern = anchored_phrase_pattern(phrase)
    if not pattern:
        return []
    return [match.start() for match in re.finditer(pattern, text, re.IGNORECASE)]


def _normalized_offsets(phrase: str, text: str) -> list[int]:
    """Return every start position where ``phrase`` appears in any case or spacing."""
    words = phrase.split()
    if not words:
        return []
    pattern = r"\s+".join(re.escape(word) for word in words)
    return [match.start() for match in re.finditer(pattern, text, re.IGNORECASE)]


def _work_authorization_precedence(
    rules: Guardrails, cv_markdown: str
) -> WorkAuthorizationPrecedence:
    """Report which work-authorization line a draft would actually carry.

    Mirrors the precedence ``Document.header`` applies: a configured drafting
    line replaces every master-CV line whose subject is work authorization.
    """
    cv_line = next(
        (line.strip() for line in cv_markdown.splitlines() if states_work_authorization(line)),
        None,
    )
    drafting_line = rules.work_authorization.strip() if rules.work_authorization else None
    drafting_line = drafting_line or None
    return WorkAuthorizationPrecedence(
        cv_line=cv_line,
        drafting_line=drafting_line,
        effective=drafting_line or cv_line,
        drops_cv_line=bool(drafting_line) and bool(cv_line),
    )
