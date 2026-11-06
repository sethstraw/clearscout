"""The verification report and the posting keyword coverage it carries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from clearscout.application.master_cv.cv import CvInventory, normalize
from clearscout.application.tailoring.stages.jobspec import JobSpec
from clearscout.shared.results import Finding, Outcome

from .citations import worded
from .critic import critic_finding, doubts_support, misquoted_evidence
from .lexicon import WORD
from .rubric import CRITIC_CHECK, CRITIC_CHECKS, SUPPORT_CHECKS
from .verdicts import CriticVerdict

MINIMUM_KEYWORD_COVERAGE = 0.5


@dataclass(frozen=True)
class KeywordCoverage:
    """Which of the posting's exact phrases survived into the derivative."""

    present: tuple[str, ...]
    missing: tuple[str, ...]
    title_terms: tuple[str, ...] = ()

    @property
    def ratio(self) -> float:
        """Return the fraction of extracted keywords that appear in the output."""
        total = len(self.present) + len(self.missing)
        return 1.0 if total == 0 else len(self.present) / total


@dataclass(frozen=True)
class Report:
    """Every finding from one verification pass, with offender counts for the counted checks."""

    findings: tuple[Finding, ...]
    coverage: KeywordCoverage
    critic: CriticVerdict | None = None
    offenders: Mapping[str, int] = field(default_factory=dict)
    # Each sampled critic draw behind ``critic``, so the spread stays auditable.
    critic_draws: tuple[CriticVerdict, ...] = ()
    # Record ids the critic reads but this document's writer was never offered to cite.
    uncitable: frozenset[str] = frozenset()

    @property
    def passed(self) -> bool:
        """Return whether nothing failed."""
        return not self.failures

    @property
    def failures(self) -> tuple[Finding, ...]:
        """Return only the failing findings."""
        return tuple(finding for finding in self.findings if finding.outcome is Outcome.FAIL)

    def feedback(self) -> str:
        """Render the failures a redraft could actually clear."""
        return "\n".join(
            f"- {finding.check}: {finding.detail}"
            for finding in self.failures
            if finding.redraftable
        )

    def redraftable_checks(self) -> frozenset[str]:
        """Return the names of the failures a redraft could clear."""
        return frozenset(finding.check for finding in self.failures if finding.redraftable)

    @property
    def worth_redrafting(self) -> bool:
        """Return whether any failure is one a redraft could clear, so retrying can pay off."""
        return any(finding.redraftable for finding in self.failures)

    def unsupported_failures(self) -> int:
        """Count the failures that question whether the text is supported, not how it reads."""
        checked = sum(1 for finding in self.failures if finding.check in SUPPORT_CHECKS)
        doubted = self.critic is not None and doubts_support(self.critic)
        return checked + doubted

    def with_critic(
        self,
        verdict: CriticVerdict,
        check: str = CRITIC_CHECK,
        draws: Sequence[CriticVerdict] = (),
        *,
        document: str,
        inventory: CvInventory,
    ) -> Report:
        """Return this report with the critic's findings on the ``document`` it read."""
        finding = critic_finding(verdict, check, uncitable=self.uncitable, document=document)
        return replace(
            self,
            findings=(*self.findings, finding, *misquoted_evidence(verdict, check, inventory)),
            critic=verdict,
            critic_draws=tuple(draws),
        )

    def without_critic(self, reason: str) -> Report:
        """Return this report warning that the critic was not asked, and why."""
        finding = Finding(CRITIC_CHECK, Outcome.WARN, reason, redraftable=False)
        return replace(self, findings=(*self.findings, finding))

    def for_reader(self, known: Mapping[str, str]) -> Report:
        """Return this report with the critic findings' cited ids replaced by their text."""
        return replace(
            self,
            findings=tuple(
                replace(
                    finding,
                    detail=worded(finding.detail, known),
                    advisories=tuple(worded(text, known) for text in finding.advisories),
                )
                if finding.check in CRITIC_CHECKS
                else finding
                for finding in self.findings
            ),
        )


def _searchable_tokens(text: str) -> set[str]:
    """Return every word a posting phrase could reasonably be looking for.

    A hyphen is a typographic choice, not a different word, and the token
    pattern keeps hyphens so that ``Neo4j-backed`` and ``C++`` survive intact.
    Both forms are kept: the joined token so a hyphenated keyword still matches,
    and the parts so a spaced one does.
    """
    tokens: set[str] = set()
    for match in WORD.findall(text):
        folded = match.casefold()
        tokens.add(folded)
        if "-" in folded:
            tokens.update(part for part in folded.split("-") if part)
    return tokens


def keyword_coverage(markdown: str, spec: JobSpec) -> KeywordCoverage:
    """Score the posting's exact phrases against the derivative, deterministically.

    A coverage number a model produced is an opinion rather than a measurement,
    and the missing list is the part a person actually acts on. A phrase the
    posting's own title carries is scored in neither direction, because a
    truthful resume carries it only where the master CV does.
    """
    haystack = normalize(markdown)
    tokens = _searchable_tokens(markdown)
    title = normalize(spec.title)
    present: list[str] = []
    missing: list[str] = []
    title_terms: list[str] = []
    for keyword in spec.keywords:
        folded = normalize(keyword)
        if not folded:
            continue
        if folded in title:
            title_terms.append(keyword)
            continue
        found = folded in haystack or all(part in tokens for part in folded.split())
        (present if found else missing).append(keyword)
    return KeywordCoverage(
        present=tuple(present), missing=tuple(missing), title_terms=tuple(title_terms)
    )


def coverage_finding(coverage: KeywordCoverage) -> Finding:
    """Report coverage as advisory, because a keyword floor is not a fact.

    No published evidence ties a coverage percentage to an interview, so this
    warns and lists what is missing rather than blocking a truthful document
    over a number.
    """
    percentage = f"{coverage.ratio * 100:.0f}%"
    titled = (
        f"; posting title terms a truthful resume carries only if the CV does: "
        f"{', '.join(coverage.title_terms[:12])}"
        if coverage.title_terms
        else ""
    )
    if not coverage.missing:
        return Finding(
            "keyword coverage", Outcome.PASS, f"{percentage} of extracted keywords{titled}"
        )
    outcome = Outcome.PASS if coverage.ratio >= MINIMUM_KEYWORD_COVERAGE else Outcome.WARN
    return Finding(
        "keyword coverage",
        outcome,
        f"{percentage}; missing: {', '.join(coverage.missing[:12])}{titled}",
    )
